#!/usr/bin/env python3
"""Local scrape worker — the Mac side of the front-end scrape trigger.

Scraping is LOCAL by design (the cloud has no browser). The live site can't reach this laptop
(NAT, and we don't want an inbound path), so control is inverted: the browser enqueues a job in
the cloud queue, and THIS worker polls that queue OUTBOUND, claims a job, runs the real
discover.py CLI locally, and PATCHes the status + a result snapshot back. Every connection
originates here; nothing connects into the Mac.

Because it invokes the actual `discover.py` CLI (never a reimplementation), every scrape inherits
every guardrail automatically — document selector, corroboration guard, BPP detection, account
status, and prod_ready DEFAULT-HELD. The worker does NOT publish on a scrape: scraped cases land
prod_ready=0 in the local DB and stay held until a deliberate approve+sync. The result it reports
is a preview.

Two job kinds flow through the SAME queue (dispatched by request shape in process_one):
  * SCRAPE  — request has case_number/pattern → runs `discover.py` (adds a held case).
  * APPROVE — request has {"approve": CN}    → runs the real `sync_to_prod.py --approve CN --only CN`
              (flips prod_ready=1 and publishes ONE already-held case). It NEVER creates data and
              only touches the named case — the same guardrails as running that CLI by hand.

The worker also keeps the browser's held-review view honest without any inbound path:
  * HEARTBEAT — every poll it POSTs /api/worker/heartbeat so the UI can show online/offline (the
                whole feature depends on the worker running).
  * HELD-SYNC — on startup and after every handled job it POSTs the current LOCAL held set
                (prod_ready=0, real-property) to /api/held/sync (full replace). That mirror is
                what the browser lists; a just-approved case drops off, new scrapes appear.

Run it on the Mac (one job at a time; scraping is serial — one browser):

    export SCRAPE_WORKER_TOKEN=...        # must match the value set in Railway env
    export PROD_URL=https://taxforeclosureanalyzer.com   # optional; this is the default
    python3 scrape_worker.py              # poll forever
    python3 scrape_worker.py --once       # drain at most one job, then exit (handy for testing)

Test seam: the HTTP calls, the discover command, and the approve command are injected/overridable
(SCRAPE_DISCOVER_CMD / SCRAPE_SYNC_CMD), so the whole claim->run->report flow — scrape AND approve —
is exercised in tests with stub commands: no network, no portal, no credits. See test_scrape_worker.py.
"""
import argparse
import json
import os
import shlex
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import certifi

BASE_DIR = Path(__file__).parent
PROD = os.environ.get("PROD_URL", "https://taxforeclosureanalyzer.com").rstrip("/")
DB_PATH = Path(os.environ.get("SYNC_DB", BASE_DIR / "data" / "db" / "pcpeak.db"))
CTX = ssl.create_default_context(cafile=certifi.where())
# Overridable so tests can inject a stub scraper instead of the real (credit-spending) CLI.
DISCOVER_CMD = shlex.split(os.environ.get("SCRAPE_DISCOVER_CMD", "python3 discover.py"))
# The real approve+publish CLI. An approve job shells out to this exactly as a human would:
# `sync_to_prod.py --approve CN --only CN` (flip prod_ready=1, then push ONLY that case).
SYNC_CMD = shlex.split(os.environ.get("SCRAPE_SYNC_CMD", "python3 sync_to_prod.py"))
RESULT_TRUNC = 1500

# Held-review preview fields: output key -> source column in the local `cases` table. The WHERE
# predicate below mirrors sync_to_prod.pending_cases EXACTLY, so the browser's held list is precisely
# the set `--approve` would consider (prod_ready=0, real-property, undetermined/BPP excluded).
HELD_PREVIEW_COLS = {
    "case_number": "case_number",
    "property_address": "property_address",
    "defendant": "defendant",
    "total_due": "total_due_filing",
    "property_type": "property_type",
    "case_track": "case_track",
    "account_status": "account_status",
}


# ── discover.py invocation (the actual pipeline — no logic duplicated here) ──
def build_discover_args(request):
    if request.get("case_number"):
        return ["--case", request["case_number"]]
    args = ["--pattern", request["pattern"]]
    if request.get("individuals_only", True):
        args.append("--individuals-only")
    # Discovery INCLUDES closed by default (the moat). Only narrow to open-only when the
    # request explicitly opts out — include_closed defaults True, so absence keeps the default.
    if not request.get("include_closed", True):
        args.append("--open-only")
    return args


def parse_summary(out):
    """Pull the machine-readable `SCRAPE_SUMMARY {json}` line discover.py prints at the end
    (Found / Processed / Skipped / Closed / Errors). Returns the dict, or None if absent.
    Decoupled from the human log format on purpose. Last match wins."""
    found = None
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("SCRAPE_SUMMARY "):
            try:
                found = json.loads(line[len("SCRAPE_SUMMARY "):])
            except ValueError:
                pass
    return found


def run_discover(request, discover_cmd=None, cwd=None, timeout=1200):
    """Run the real discover.py CLI. Returns (returncode, stdout, stderr)."""
    cmd = (discover_cmd or DISCOVER_CMD) + build_discover_args(request)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=str(cwd or BASE_DIR), timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def run_approve(case_number, sync_cmd=None, cwd=None, timeout=300):
    """Publish ONE already-held case via the real approval CLI: `sync_to_prod.py --approve CN --only CN`.
    This flips prod_ready=1 for that case and pushes only it — no other case is touched, and no case
    data is created (sync is additive/idempotent). Returns (returncode, stdout, stderr)."""
    cmd = (sync_cmd or SYNC_CMD) + ["--approve", case_number, "--only", case_number]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=str(cwd or BASE_DIR), timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def local_held_cases(db_path=None):
    """The current LOCAL held set — real-property cases not yet approved for prod (prod_ready=0),
    with a few preview fields. Predicate mirrors sync_to_prod.pending_cases EXACTLY, so this list is
    precisely what `--approve` would consider. Column-tolerant: missing columns default to NULL and
    absent predicate columns drop their term, so a pre-migration/partial DB never raises."""
    path = Path(db_path or DB_PATH)
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}
        if "case_number" not in cols:
            return []
        select = ", ".join(
            (f"{src} AS {out}" if src in cols else f"NULL AS {out}")
            for out, src in HELD_PREVIEW_COLS.items())
        where = ["case_number IS NOT NULL"]
        if "prod_ready" in cols:
            where.append("(prod_ready IS NULL OR prod_ready=0)")
        if "property_type" in cols:
            where.append("property_type IS NOT 'personal' AND property_type IS NOT 'unknown'")
        if "case_track" in cols:
            where.append("case_track IS NOT 'personal_property'")
        sql = f"SELECT {select} FROM cases WHERE {' AND '.join(where)} ORDER BY case_number"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def sync_held(post_fn, db_path=None):
    """Full-replace the cloud held-review mirror with the local held set. `post_fn(body)` POSTs to
    /api/held/sync. Returns the server's reported held count (or None). Best-effort by the caller —
    a failed mirror refresh never blocks job processing (the mirror self-heals on the next poll)."""
    resp = post_fn({"held": local_held_cases(db_path)})
    return (resp or {}).get("held") if isinstance(resp, dict) else None


def snapshot(request, db_path=None):
    """Read the just-scraped case(s) from the LOCAL DB for a preview of what the pipeline produced
    and how each guardrail landed. prod_ready is included so the UI can show the case is HELD."""
    path = Path(db_path or DB_PATH)
    out = {"cases": [],
           "note": "scraped cases are HELD (prod_ready=0) — review + approve to publish"}
    if not path.exists():
        out["warning"] = f"local DB not found at {path}"
        return out
    cols = ("case_number, property_type, case_track, account_status, account_note, prod_ready, "
            "(property_intel IS NOT NULL AND TRIM(property_intel)!='') AS enriched")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if request.get("case_number"):
            rows = conn.execute(f"SELECT {cols} FROM cases WHERE case_number=?",
                                [request["case_number"]]).fetchall()
        else:
            like = request["pattern"].rstrip("*") + "%"
            rows = conn.execute(f"SELECT {cols} FROM cases WHERE case_number LIKE ? "
                                "ORDER BY case_number", [like]).fetchall()
        out["cases"] = [dict(r) for r in rows]
        out["found"] = len(out["cases"])
    finally:
        conn.close()
    return out


def _refresh_held(on_held_change, label, log):
    """Best-effort held-mirror refresh after a terminal success. NEVER raises — the job already
    reached its real outcome; a failed mirror refresh must not touch that (it self-heals next poll)."""
    if not on_held_change:
        return
    try:
        on_held_change()
    except Exception as e:  # noqa: BLE001
        try:
            log(f"  … held-mirror refresh after {label} failed (self-heals next poll): {e}")
        except Exception:  # noqa: BLE001
            pass


# ── the testable core: claim one job, run it, report the outcome ──
def process_one(claim_fn, patch_fn, run_fn, snapshot_fn, log=print,
                approve_fn=None, on_held_change=None):
    """Claim the next job and drive it to a terminal state. Returns True if a job was handled,
    False if the queue was empty. Dispatches by request shape: an {"approve": CN} request PUBLISHES
    that one held case via approve_fn; anything else SCRAPES via run_fn. After any successful job the
    held set changed (a new held case, or one just published), so on_held_change() refreshes the
    cloud mirror. Any exception is reported as a failed job, never swallowed silently (the original
    Run-button bug was a background thread dying with no trace)."""
    job = claim_fn()
    if not job:
        return False
    job_id = job["id"]
    label = job.get("label") or ""
    request = job["request"]
    terminal_sent = False   # once done/failed is reported, a later exception must NOT flip it
    try:
        # ── APPROVE job: publish ONE held case via the real sync_to_prod.py --approve (never scrapes) ──
        if isinstance(request, dict) and request.get("approve"):
            cn = request["approve"]
            patch_fn(job_id, status="running", progress=f"approving {cn}…")
            rc, out, err = (approve_fn or run_approve)(cn)
            if rc == 0:
                result = {"approved": cn, "stdout_tail": (out or "")[-RESULT_TRUNC:],
                          "note": f"{cn} approved (prod_ready=1) and synced to prod"}
                patch_fn(job_id, status="done", progress="published", result=result)
                terminal_sent = True
                log(f"  ✓ published  job {job_id} ({label}) — {cn} live on prod")
                _refresh_held(on_held_change, label, log)   # it dropped off the held set
            else:
                detail = ((err or "") + "\n" + (out or "")).strip()[-RESULT_TRUNC:]
                patch_fn(job_id, status="failed", error=f"sync_to_prod exited {rc}\n{detail}")
                terminal_sent = True
                log(f"  ✗ failed  job {job_id} ({label}) — approve exited {rc}")
            return True

        # ── SCRAPE job: run the real discover.py; the case lands held ──
        patch_fn(job_id, status="running", progress=f"scraping {label}…")
        rc, out, err = run_fn(request)
        if rc == 0:
            result = snapshot_fn(request)
            result["stdout_tail"] = (out or "")[-RESULT_TRUNC:]
            summary = parse_summary(out)          # Found/Closed/Business breakdown, if present
            if summary:
                result["summary"] = summary
            patch_fn(job_id, status="done", progress="done", result=result)
            terminal_sent = True
            # Defensive .get() throughout — a log-formatting detail (e.g. a renamed summary field)
            # must NEVER raise here: the done patch is already sent, so an exception would be caught
            # below and wrongly re-report the job as failed.
            g = (summary or {}).get
            log(f"  ✓ done  job {job_id} ({label}) — {g('processed', 0)} new, held"
                + (f" (found {g('found', '?')}, {g('closed', 0)} closed, "
                   f"{g('business', 0)} business, {g('reused', 0)} reused)" if summary else ""))
            _refresh_held(on_held_change, label, log)   # a new held case may have appeared
        else:
            detail = ((err or "") + "\n" + (out or "")).strip()[-RESULT_TRUNC:]
            patch_fn(job_id, status="failed", error=f"discover exited {rc}\n{detail}")
            terminal_sent = True
            log(f"  ✗ failed  job {job_id} ({label}) — discover exited {rc}")
        return True
    except Exception as e:  # noqa: BLE001 — worker must never die silently; report + continue
        if terminal_sent:
            # The job already reached a terminal state (e.g. done); a post-terminal error is only
            # a logging/formatting issue — surface it if we can, but DON'T overwrite the real
            # outcome, and never let the log call itself escape.
            try:
                log(f"  !! post-terminal error on job {job_id} ({label}) — outcome kept: {e}")
            except Exception:  # noqa: BLE001
                pass
            return True
        try:
            patch_fn(job_id, status="failed", error=f"worker error: {e}")
        except Exception as e2:  # noqa: BLE001
            log(f"  !! could not report failure for job {job_id}: {e2}")
        log(f"  ✗ failed  job {job_id} ({label}) — worker error: {e}")
        return True


# ── real HTTP wiring (used by the CLI; tests inject fakes instead) ──
def _api(method, path, body, token):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        PROD + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-Worker-Token": token})
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else None


def _http_claim(token, worker_id):
    def claim():
        return (_api("POST", "/api/scrape-jobs/claim", {"worker_id": worker_id}, token) or {}).get("job")
    return claim


def _http_patch(token):
    def patch(job_id, **fields):
        return _api("PATCH", f"/api/scrape-jobs/{job_id}", fields, token)
    return patch


def _http_heartbeat(token, worker_id):
    def beat():
        return _api("POST", "/api/worker/heartbeat", {"worker_id": worker_id}, token)
    return beat


def _http_sync_held(token, db_path):
    def refresh():
        return sync_held(lambda body: _api("POST", "/api/held/sync", body, token), db_path)
    return refresh


def main():
    ap = argparse.ArgumentParser(description="Local scrape worker for the front-end trigger queue.")
    ap.add_argument("--once", action="store_true", help="Drain at most one job, then exit.")
    ap.add_argument("--interval", type=float, default=5.0, help="Seconds between polls (default 5).")
    ap.add_argument("--worker-id", default=os.environ.get("WORKER_ID", "mac-worker"))
    args = ap.parse_args()

    token = os.environ.get("SCRAPE_WORKER_TOKEN", "")
    if not token:
        print("ERROR: SCRAPE_WORKER_TOKEN not set — refusing to start (fail-closed)."); sys.exit(1)
    if not DB_PATH.exists():
        print(f"WARNING: local DB not found at {DB_PATH} — result snapshots will be empty until "
              f"discover.py creates it.")

    claim = _http_claim(token, args.worker_id)
    patch = _http_patch(token)
    beat = _http_heartbeat(token, args.worker_id)
    refresh_held = _http_sync_held(token, DB_PATH)
    print(f"scrape_worker → {PROD}  (worker_id={args.worker_id}, db={DB_PATH})")
    print(f"discover cmd: {' '.join(DISCOVER_CMD)}")
    print(f"sync cmd:     {' '.join(SYNC_CMD)}")

    def _safe(fn, what):
        """Run a best-effort side call (heartbeat / held-sync); a network hiccup must not stop polling."""
        try:
            return fn()
        except urllib.error.URLError as e:
            print(f"  … {what} couldn't reach {PROD}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  … {what} failed: {e}")

    # Announce liveness + publish the current held set before the first poll, so the browser's
    # held-review view is populated and shows the worker online even with an empty queue.
    _safe(beat, "startup heartbeat")
    n_held = _safe(refresh_held, "startup held-sync")
    if n_held is not None:
        print(f"held-review mirror synced: {n_held} case(s) awaiting approval")

    try:
        while True:
            _safe(beat, "heartbeat")   # each poll, so the UI's online indicator stays fresh
            try:
                handled = process_one(claim, patch, run_discover, snapshot,
                                      approve_fn=run_approve, on_held_change=refresh_held)
            except urllib.error.URLError as e:
                print(f"  … can't reach {PROD}: {e} (retrying)"); handled = False
            if args.once:
                if not handled:
                    print("queue empty — nothing to do.")
                break
            time.sleep(0 if handled else args.interval)  # burst through a backlog, then idle
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
