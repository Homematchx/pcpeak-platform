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
print("SCRAPE_SUMMARY " + json.dumps({"found":1,"processed":1,"skipped":0,"closed":0,"errors":0,"bpp":0}))
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

    # ── parse_summary: pulls the SCRAPE_SUMMARY line; ignores everything else ──
    out = ("Page 1: 110 cases | 2 to process\n"
           "  (108 CLOSED cases excluded by default — pass --include-closed to keep)\n"
           'SCRAPE_SUMMARY {"found":110,"processed":0,"skipped":2,"closed":108,"errors":0,"bpp":0}\n')
    s = W.parse_summary(out)
    check("parse_summary finds the breakdown", s and s["found"] == 110 and s["closed"] == 108)
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
