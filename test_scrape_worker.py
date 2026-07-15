#!/usr/bin/env python3
"""Tests for scrape_worker.py. No network, no portal, no credits.

The worker's claim→run→report core (process_one) is exercised with injected fake claim/patch
functions, so we assert the exact status transitions and terminal reporting without a server. The
subprocess+snapshot mechanics (run_discover/snapshot) are exercised against a STUB discover command
that writes a held row to a temp DB — proving the worker invokes an external CLI and reads its
result, without touching the real credit-spending scraper.

Run: python3 test_scrape_worker.py   (exit 0 = all green)
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import scrape_worker as W

_results = []
def check(name, cond):
    _results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)


class FakeApi:
    """Records patch calls and serves one claimable job (or none)."""
    def __init__(self, job):
        self.job = job
        self.claimed = False
        self.patches = []          # list of (job_id, fields)

    def claim(self):
        if self.job is None or self.claimed:
            return None
        self.claimed = True
        return self.job

    def patch(self, job_id, **fields):
        self.patches.append((job_id, fields))
        return {"id": job_id, **fields}

    def statuses(self):
        return [f.get("status") for _, f in self.patches if "status" in f]


STUB = '''#!/usr/bin/env python3
import argparse, json, os, sqlite3, sys
ap = argparse.ArgumentParser()
ap.add_argument("--case", default=""); ap.add_argument("--pattern", default="")
ap.add_argument("--individuals-only", action="store_true")
a = ap.parse_args()
if os.environ.get("STUB_FAIL") == "1":
    sys.stderr.write("boom: simulated scrape failure\\n"); sys.exit(3)
db = os.environ["STUB_DB"]
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE IF NOT EXISTS cases (case_number TEXT UNIQUE, property_type TEXT, "
             "case_track TEXT, account_status TEXT, account_note TEXT, prod_ready INTEGER DEFAULT 0, "
             "property_intel TEXT)")
cn = a.case or "TX-26-90001"
conn.execute("INSERT OR REPLACE INTO cases (case_number, property_type, account_status, prod_ready, "
             "property_intel) VALUES (?,?,?,?,?)", [cn, "real", "resolved", 0, '{"market_value": 100}'])
conn.commit(); conn.close()
print("stub scraped", cn)
print("SCRAPE_SUMMARY " + json.dumps({"found":1,"processed":1,"reused":0,"business":0,"skip_existing":0,"closed":0,"errors":0,"bpp":0}))
sys.exit(0)
'''


def run():
    d = Path(tempfile.mkdtemp())
    stub_path = d / "stub_discover.py"
    stub_path.write_text(STUB)
    stub_db = d / "pcpeak.db"
    discover_cmd = ["python3", str(stub_path)]

    # ── build_discover_args ──
    check("args for a case number", W.build_discover_args({"case_number": "TX-26-00009"}) ==
          ["--case", "TX-26-00009"])
    check("args for a pattern (individuals_only default on)",
          W.build_discover_args({"pattern": "TX-26"}) == ["--pattern", "TX-26", "--individuals-only"])
    check("args for a pattern with individuals_only off",
          W.build_discover_args({"pattern": "TX-26", "individuals_only": False}) == ["--pattern", "TX-26"])
    # include_closed: the moat is INCLUDED by default; --open-only only when explicitly opted out
    check("pattern default INCLUDES closed (no --open-only)",
          "--open-only" not in W.build_discover_args({"pattern": "TX-26"}))
    check("pattern include_closed=False → --open-only (narrow mode)",
          "--open-only" in W.build_discover_args({"pattern": "TX-26", "include_closed": False}))
    check("pattern include_closed=True → no --open-only",
          "--open-only" not in W.build_discover_args({"pattern": "TX-26", "include_closed": True}))

    # ── parse_summary: pulls the SCRAPE_SUMMARY line; ignores everything else ──
    out = ("Page 1: 110 cases | 2 to process\n"
           "  (108 CLOSED cases excluded (--open-only mode))\n"
           'SCRAPE_SUMMARY {"found":110,"processed":0,"reused":0,"business":2,"closed":108,"errors":0,"bpp":0}\n')
    s = W.parse_summary(out)
    check("parse_summary finds the breakdown", s and s["found"] == 110 and s["closed"] == 108)
    # the --case reused case: found=1, reused=1, business=0 (must NOT be lumped as business)
    s2 = W.parse_summary('SCRAPE_SUMMARY {"found":1,"processed":0,"reused":1,"business":0,"closed":0,"errors":0}')
    check("parse_summary carries distinct reused vs business buckets",
          s2["found"] == 1 and s2["reused"] == 1 and s2["business"] == 0)
    check("parse_summary None when absent", W.parse_summary("no summary here\n") is None)
    check("parse_summary ignores malformed line", W.parse_summary("SCRAPE_SUMMARY {bad json\n") is None)

    # ── empty queue: process_one returns False, no work ──
    api = FakeApi(None)
    handled = W.process_one(api.claim, api.patch, run_fn=None, snapshot_fn=None, log=lambda *_: None)
    check("empty queue → process_one returns False", handled is False)
    check("empty queue → no patch calls", api.patches == [])

    # ── happy path via fakes: running then done+result ──
    api = FakeApi({"id": 7, "label": "TX-26-00009", "request": {"case_number": "TX-26-00009"}})
    handled = W.process_one(
        api.claim, api.patch,
        run_fn=lambda req: (0, "scraped 1 case", ""),
        snapshot_fn=lambda req: {"found": 1, "cases": [{"case_number": "TX-26-00009", "prod_ready": 0}]},
        log=lambda *_: None)
    check("happy path → returns True", handled is True)
    check("happy path → status transitions running→done", api.statuses() == ["running", "done"])
    done_fields = api.patches[-1][1]
    check("happy path → done carries result with stdout_tail",
          done_fields["result"]["found"] == 1 and "stdout_tail" in done_fields["result"])
    check("happy path → result shows prod_ready=0 (held, not published)",
          done_fields["result"]["cases"][0]["prod_ready"] == 0)

    # ── failure path: non-zero exit → failed + error ──
    api = FakeApi({"id": 8, "label": "TX-26-00010", "request": {"case_number": "TX-26-00010"}})
    W.process_one(api.claim, api.patch,
                  run_fn=lambda req: (3, "", "boom"),
                  snapshot_fn=lambda req: {}, log=lambda *_: None)
    check("failure path → status running→failed", api.statuses() == ["running", "failed"])
    check("failure path → error mentions exit code", "exited 3" in api.patches[-1][1]["error"])

    # ── exception path: run_fn raises → reported failed, worker never crashes ──
    api = FakeApi({"id": 9, "label": "TX-26-00011", "request": {"case_number": "TX-26-00011"}})
    def boom(req):
        raise RuntimeError("kaboom")
    handled = W.process_one(api.claim, api.patch, run_fn=boom, snapshot_fn=lambda r: {}, log=lambda *_: None)
    check("exception path → still returns True (handled, not crashed)", handled is True)
    check("exception path → reported as failed with 'worker error'",
          api.statuses()[-1] == "failed" and "worker error" in api.patches[-1][1]["error"])

    # ── post-terminal error must NOT flip a done job to failed (the renamed-field crash class) ──
    api = FakeApi({"id": 11, "label": "TX-26-00011", "request": {"case_number": "TX-26-00011"}})
    def raising_log(*a):
        raise RuntimeError("log boom (post-terminal)")
    W.process_one(api.claim, api.patch, run_fn=lambda req: (0, "ok", ""),
                  snapshot_fn=lambda req: {"found": 1, "cases": []}, log=raising_log)
    check("post-terminal log error keeps DONE (not flipped to failed)",
          api.statuses() == ["running", "done"])

    # ── a successful SCRAPE refreshes the held mirror (a new held case may have appeared) ──
    refreshed = {"n": 0}
    api = FakeApi({"id": 20, "label": "TX-26-00020", "request": {"case_number": "TX-26-00020"}})
    W.process_one(api.claim, api.patch, run_fn=lambda req: (0, "ok", ""),
                  snapshot_fn=lambda req: {"found": 1, "cases": []}, log=lambda *_: None,
                  on_held_change=lambda: refreshed.__setitem__("n", refreshed["n"] + 1))
    check("successful scrape triggers a held-mirror refresh", refreshed["n"] == 1)
    # a FAILED scrape must NOT refresh (nothing changed)
    refreshed["n"] = 0
    api = FakeApi({"id": 21, "label": "TX-26-00021", "request": {"case_number": "TX-26-00021"}})
    W.process_one(api.claim, api.patch, run_fn=lambda req: (2, "", "boom"),
                  snapshot_fn=lambda req: {}, log=lambda *_: None,
                  on_held_change=lambda: refreshed.__setitem__("n", refreshed["n"] + 1))
    check("failed scrape does NOT refresh the held mirror", refreshed["n"] == 0)

    # ══ APPROVE-JOB DISPATCH ══
    # build the command exactly as a human would: sync_to_prod.py --approve CN --only CN
    rc, out, err = W.run_approve("TX-26-00010", sync_cmd=["python3", "-c",
        "import sys;print('ARGS',sys.argv[1:]);print('approved TX-26-00010')"], cwd=d)
    check("run_approve builds --approve CN --only CN and exits 0",
          rc == 0 and "ARGS ['--approve', 'TX-26-00010', '--only', 'TX-26-00010']" in out)

    # process_one dispatches an {"approve": CN} request to approve_fn (NOT run_fn) and publishes it
    published = {"n": 0}
    api = FakeApi({"id": 30, "label": "approve TX-26-00010", "request": {"approve": "TX-26-00010"}})
    def _run_should_not_be_called(req):
        raise AssertionError("run_fn (discover) must NOT be called for an approve job")
    W.process_one(api.claim, api.patch, run_fn=_run_should_not_be_called,
                  snapshot_fn=lambda req: {}, log=lambda *_: None,
                  approve_fn=lambda cn: (0, f"published {cn}", ""),
                  on_held_change=lambda: published.__setitem__("n", published["n"] + 1))
    check("approve job → status running→done (dispatched to approve_fn, not scrape)",
          api.statuses() == ["running", "done"])
    check("approve job → done result carries the approved case number",
          api.patches[-1][1]["result"].get("approved") == "TX-26-00010")
    check("successful approve refreshes the held mirror (case dropped off)", published["n"] == 1)

    # a FAILED approve → failed job, no mirror refresh
    published["n"] = 0
    api = FakeApi({"id": 31, "label": "approve TX-26-00011", "request": {"approve": "TX-26-00011"}})
    W.process_one(api.claim, api.patch, run_fn=None, snapshot_fn=lambda req: {}, log=lambda *_: None,
                  approve_fn=lambda cn: (5, "", "gate refused: held"),
                  on_held_change=lambda: published.__setitem__("n", published["n"] + 1))
    check("failed approve → status running→failed with the CLI's exit code",
          api.statuses() == ["running", "failed"] and "exited 5" in api.patches[-1][1]["error"])
    check("failed approve does NOT refresh the held mirror", published["n"] == 0)

    # ══ local_held_cases + sync_held against a temp DB ══
    held_db = d / "held.db"
    conn = sqlite3.connect(held_db)
    conn.execute("CREATE TABLE cases (case_number TEXT UNIQUE, property_address TEXT, defendant TEXT, "
                 "total_due_filing REAL, property_type TEXT, case_track TEXT, account_status TEXT, "
                 "prod_ready INTEGER DEFAULT 0)")
    conn.executemany("INSERT INTO cases (case_number, property_address, defendant, total_due_filing, "
                     "property_type, case_track, account_status, prod_ready) VALUES (?,?,?,?,?,?,?,?)", [
        ("TX-26-00009", "100 Main St", "DOE, JOHN", 12818.78, "real", "oos_timing", "resolved", 0),   # held
        ("TX-26-00010", "200 Oak Ave", "ROE, JANE", 4200.0, "real", "oos_timing", "needs_lookup", 0), # held
        ("TX-26-00011", "300 Elm",     "PUB, LIVE", 999.0,   "real", "oos_timing", "resolved", 1),    # approved → excluded
        ("TX-26-00012", "400 BPP",     "BIZ LLC",   50.0,    "personal", "personal_property", "resolved", 0),  # BPP → excluded
        ("TX-26-00013", "500 Unk",     "NEW",       0.0,     "unknown", "oos_timing", "resolved", 0),  # undetermined → excluded
    ])
    conn.commit(); conn.close()
    held = W.local_held_cases(db_path=held_db)
    nums = [h["case_number"] for h in held]
    check("local_held_cases returns ONLY prod_ready=0 real-property (excludes approved/BPP/unknown)",
          nums == ["TX-26-00009", "TX-26-00010"])
    check("local_held_cases carries preview fields (total_due aliased from total_due_filing)",
          held[0]["property_address"] == "100 Main St" and abs(held[0]["total_due"] - 12818.78) < 1e-6)

    # sync_held posts {"held": [...]} to the injected poster and returns the server count
    posted = {}
    def fake_post(body):
        posted["body"] = body
        return {"held": len(body["held"])}
    n = W.sync_held(fake_post, db_path=held_db)
    check("sync_held posts the local held set and returns the server count",
          n == 2 and [h["case_number"] for h in posted["body"]["held"]] == ["TX-26-00009", "TX-26-00010"])

    # missing DB → empty held set (no crash)
    check("local_held_cases on a missing DB → [] (no crash)",
          W.local_held_cases(db_path=d / "nope.db") == [])

    # ── real subprocess round-trip through the STUB discover command ──
    env_db = os.environ.get("STUB_DB")
    os.environ["STUB_DB"] = str(stub_db)
    os.environ.pop("STUB_FAIL", None)
    rc, out, err = W.run_discover({"case_number": "TX-26-77777"}, discover_cmd=discover_cmd, cwd=d)
    check("run_discover: stub exits 0", rc == 0 and "stub scraped TX-26-77777" in out)
    snap = W.snapshot({"case_number": "TX-26-77777"}, db_path=stub_db)
    check("snapshot reads the just-written case", snap["found"] == 1 and
          snap["cases"][0]["case_number"] == "TX-26-77777")
    check("snapshot reports prod_ready=0 + held note",
          snap["cases"][0]["prod_ready"] == 0 and "HELD" in snap["note"].upper())

    os.environ["STUB_FAIL"] = "1"
    rc, out, err = W.run_discover({"case_number": "TX-26-77778"}, discover_cmd=discover_cmd, cwd=d)
    check("run_discover: stub non-zero exit surfaces rc + stderr", rc == 3 and "boom" in err)

    # full worker cycle end-to-end against the stub (fake api + real run_discover + real snapshot)
    os.environ.pop("STUB_FAIL", None)
    api = FakeApi({"id": 10, "label": "TX-26-88888", "request": {"case_number": "TX-26-88888"}})
    W.process_one(api.claim, api.patch,
                  run_fn=lambda req: W.run_discover(req, discover_cmd=discover_cmd, cwd=d),
                  snapshot_fn=lambda req: W.snapshot(req, db_path=stub_db), log=lambda *_: None)
    check("full stub cycle → done with the scraped case in the result",
          api.statuses() == ["running", "done"] and
          api.patches[-1][1]["result"]["cases"][0]["case_number"] == "TX-26-88888")
    check("full stub cycle → result carries the parsed SCRAPE_SUMMARY breakdown",
          api.patches[-1][1]["result"].get("summary", {}).get("found") == 1)

    if env_db is not None:
        os.environ["STUB_DB"] = env_db

    print("-" * 56)
    total, passed = len(_results), sum(_results)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
