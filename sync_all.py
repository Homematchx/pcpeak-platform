#!/usr/bin/env python3
"""
sync_case.py-style push — name the case(s) you care about, push JUST those to prod, and SEE the
result. Per-case and on-demand: you decide which cases go up, one at a time or a few together.

  python3 sync_all.py TX-23-00569                 # push one case, then show its prod fields
  python3 sync_all.py TX-23-00569 TX-26-00777     # push a few
  python3 sync_all.py TX-23-00569 --dry-run       # PLAN only — show what would change, push nothing
  python3 sync_all.py --all --go                  # escape hatch: every case (rarely what you want)

WHAT IT DOES
  1. PLAN    — a dry-run diff of exactly what will change for the named case(s).
  2. PUSH    — sync_to_prod.py --only "<cases>" (idempotent, reconciling, gated). Naming a case IS
               the intent to push it; add --dry-run to stop at the plan.
  3. VERIFY  — re-reads EACH case from prod and prints its operative fields (oos_date, oos_issued,
               sale_pulled_date, live balance) so you can SEE it landed — no guessing.

WHAT IT DOES NOT DO — on purpose:
  * It does NOT re-scrape (that hits the portal + 2Captcha; stays a deliberate discover.py step).
  * It does NOT push cases you didn't name (no accidental fleet-wide sync).
  * It adds NO push logic — it shells out to the proven sync_to_prod.py (zero duplication).

Safe to run twice: the push is additive/idempotent, and the prod_ready + BPP gates mean a held or
archived-only field can never leak up.
"""
import json
import os
import ssl
import subprocess
import sqlite3
import sys
import urllib.request
from pathlib import Path

import certifi

HERE = Path(__file__).parent
DB_PATH = Path(os.environ.get("SYNC_DB", HERE / "data" / "db" / "pcpeak.db"))
PROD = os.environ.get("PROD_URL", "https://taxforeclosureanalyzer.com").rstrip("/")
CTX = ssl.create_default_context(cafile=certifi.where())
PY = sys.executable or "python3"


def api_get(path):
    req = urllib.request.Request(PROD + path, method="GET")
    with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
        return r.status, json.loads(r.read().decode() or "null")


def live_balance(row):
    try:
        return json.loads(row.get("property_intel") or "{}").get("current_tax_balance")
    except Exception:
        return None


def local_cases(case_nums):
    """Confirm the named cases exist locally (you can only push what you have)."""
    if not DB_PATH.exists():
        return None
    db = sqlite3.connect(str(DB_PATH))
    have = {r[0] for r in db.execute("SELECT case_number FROM cases")}
    db.close()
    return have


def run_sync(args):
    p = subprocess.run([PY, str(HERE / "sync_to_prod.py")] + args, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def line(msg=""):
    print(msg, flush=True)


def show_prod(cn):
    """Print a case's operative fields from prod — the proof it landed."""
    try:
        st, row = api_get("/api/cases/" + cn)
    except Exception as e:
        line(f"    {cn}: could not read from prod ({e})"); return False
    if st != 200 or not row:
        line(f"    {cn}: NOT FOUND on prod (HTTP {st})"); return False
    bal = live_balance(row)
    bal_s = f"${bal:,.2f}" if isinstance(bal, (int, float)) else "—"
    line(f"    {cn}: oos_issued={row.get('oos_issued')} · oos_date={row.get('oos_date') or '—'} "
         f"· sale_pulled_date={row.get('sale_pulled_date') or '—'} · stage={row.get('stage')} "
         f"· balance={bal_s}")
    return True


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    push_all = "--all" in argv
    go = "--go" in argv
    cases = [a.strip().upper() for a in argv if not a.startswith("-")]

    line("=" * 68)
    line("  PUSH TO PROD  ·  " + PROD)
    line("=" * 68)

    if push_all:
        if not go:
            line("--all needs --go (this pushes EVERY case). You asked for per-case; you probably")
            line("want:  python3 sync_all.py <CASE_NUMBER>"); return 2
        only_args = ["--update-existing"]
        target_desc = "ALL cases"
        cases = None
    else:
        if not cases:
            line("Name the case(s) to push, e.g.:")
            line(f"    {PY} sync_all.py TX-23-00569")
            line(f"    {PY} sync_all.py TX-23-00569 TX-26-00777")
            line(f"    {PY} sync_all.py TX-23-00569 --dry-run    (plan only)")
            return 2
        have = local_cases(cases)
        if have is None:
            line("FAIL: local DB not found at " + str(DB_PATH)); return 2
        missing = [c for c in cases if c not in have]
        if missing:
            line("FAIL: not in your local DB (can't push what you don't have): " + ", ".join(missing))
            line("      Re-scrape first:  python3 discover.py --case " + missing[0] + " --force")
            return 2
        only_args = ["--only", ",".join(cases)]
        target_desc = ", ".join(cases)

    line(f"target: {target_desc}")

    # ── 1. PLAN ──
    line("-" * 68)
    line("PLAN — what will change:")
    code, out = run_sync(only_args + ["--dry-run"])
    for l in out.splitlines():
        if any(k in l for k in ("would refresh", "would create", "reconcile:", "WARNING",
                                "new (local only)", "FAIL", "refused")):
            line("  " + l.strip())
    if code != 0:
        line(f"FAIL: pre-flight exited {code} — nothing pushed."); return code

    if dry:
        line("-" * 68)
        line("DRY-RUN — nothing pushed. Drop --dry-run to push.")
        return 0

    # ── 2. PUSH ──
    line("-" * 68)
    line("PUSHING…")
    code, out = run_sync(only_args)
    for l in out.splitlines():
        if any(k in l for k in ("created:", "updated:", "reconcile:", "verify:", "WARNING", "FAIL")):
            line("  " + l.strip())
    if code != 0:
        line(f"FAIL: push exited {code}."); return code

    # ── 3. VERIFY — show each case's fields on prod (the proof it landed) ──
    line("-" * 68)
    line("VERIFY — live on prod now:")
    ok = True
    if cases:
        for cn in cases:
            ok = show_prod(cn) and ok
    else:
        _, s = api_get("/api/stats")
        line(f"    prod total {s.get('total_all')} · reconcile "
             + ("OK" if s.get('active_cases',0)+s.get('watching_cases',0)+s.get('archived_cases',0)
                == s.get('total_all') else "MISMATCH"))

    line("=" * 68)
    line("  " + ("✅ PUSHED & VERIFIED" if ok else "❌ pushed, but verification found a problem"))
    line("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
