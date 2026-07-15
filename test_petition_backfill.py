#!/usr/bin/env python3
"""Tests for petition_backfill.py. No network, no real scraper (stub runner). LOCAL DB only.

Asserts: eligible-missing derivation excludes BPP/undetermined; the backfill loop classifies
recovered vs still-null by re-reading petition_href after each run; resume skips already-filled cases;
a runner that leaves petition_href null is reported still-null (not falsely recovered); reconcile adds up.

Run: python3 test_petition_backfill.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import petition_backfill as B

_results = []
def check(name, cond):
    _results.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)


def make_db(d):
    db = d / "pcpeak.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cases (case_number TEXT UNIQUE, petition_href TEXT, "
                 "property_type TEXT, case_track TEXT)")
    conn.executemany("INSERT INTO cases VALUES (?,?,?,?)", [
        ("TX-A", None, "real", "oos_timing"),      # eligible (missing)
        ("TX-B", "",   "real", "judged_pending"),  # eligible (blank)
        ("TX-C", "https://court/doc", "real", "oos_timing"),  # already has one (not missing)
        ("TX-D", None, "personal", "personal_property"),      # BPP → excluded
        ("TX-E", None, "unknown", "oos_timing"),              # undetermined → excluded
    ])
    conn.commit(); conn.close()
    return db


def run():
    d = Path(tempfile.mkdtemp())
    db = make_db(d)

    # ── eligible derivation ──
    eligible, total, excluded = B.eligible_missing(db_path=db)
    check("total missing counts ALL null/blank (incl BPP/unknown): 4", total == 4)
    check("eligible excludes BPP + undetermined → [TX-A, TX-B]", eligible == ["TX-A", "TX-B"])
    check("excluded count = 2 (TX-D BPP, TX-E unknown)", excluded == 2)

    # ── backfill with a stub runner that FILLS petition_href (simulates the fixed scraper) ──
    def good_runner(cn):
        conn = sqlite3.connect(db)
        conn.execute("UPDATE cases SET petition_href=? WHERE case_number=?",
                     [f"https://courtsportal/{cn}", cn]); conn.commit(); conn.close()
        return 0, f"petition select: ORIGINAL PETITION ({cn})"
    res = B.backfill(["TX-A", "TX-B"], runner=good_runner, db_path=db, log=lambda *_: None)
    check("both recovered", set(res["recovered"]) == {"TX-A", "TX-B"} and not res["still_null"])

    # ── resume: re-running skips cases that now have a petition_href (runner NOT called) ──
    called = []
    def tracking_runner(cn):
        called.append(cn); return 0, "should not be called"
    res2 = B.backfill(["TX-A", "TX-B"], runner=tracking_runner, db_path=db, log=lambda *_: None)
    check("resume: already-filled cases skipped (runner never called)", called == [])
    check("resume: still counts them recovered", set(res2["recovered"]) == {"TX-A", "TX-B"})

    # ── still-null: a runner that does NOT fill petition_href is reported still-null (not recovered) ──
    d2 = Path(tempfile.mkdtemp()); db2 = make_db(d2)
    def noop_runner(cn):
        return 0, "petition select: NO petition-type document found — recording none"
    res3 = B.backfill(["TX-A", "TX-B"], runner=noop_runner, db_path=db2, log=lambda *_: None)
    check("still-null: nothing recovered", res3["recovered"] == [])
    check("still-null: both listed with their 'petition select' line",
          {cn for cn, _ in res3["still_null"]} == {"TX-A", "TX-B"}
          and all("NO petition-type" in line for _, line in res3["still_null"]))

    # ── an exception in the runner is captured, not fatal ──
    d3 = Path(tempfile.mkdtemp()); db3 = make_db(d3)
    def boom_runner(cn):
        if cn == "TX-A":
            raise RuntimeError("scrape crashed")
        conn = sqlite3.connect(db3); conn.execute(
            "UPDATE cases SET petition_href='x' WHERE case_number=?", [cn]); conn.commit(); conn.close()
        return 0, "ok"
    res4 = B.backfill(["TX-A", "TX-B"], runner=boom_runner, db_path=db3, log=lambda *_: None)
    check("runner exception captured (not fatal), other case still processed",
          res4["errored"] and res4["errored"][0][0] == "TX-A" and res4["recovered"] == ["TX-B"])
    check("reconcile adds up (recovered+still_null+errored == attempted)",
          len(res4["recovered"]) + len(res4["still_null"]) + len(res4["errored"]) == 2)

    print("-" * 56)
    total_c, passed = len(_results), sum(_results)
    print(f"{passed}/{total_c} passed")
    return 0 if passed == total_c else 1


if __name__ == "__main__":
    sys.exit(run())
