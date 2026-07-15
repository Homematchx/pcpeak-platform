#!/usr/bin/env python3
"""Tests for case-field history (case_snapshots) — Phase 0. No network.

A re-scrape/re-sync overwrites a case's fields in place; case_snapshots is the append-only diff that
keeps the history of what a case used to say. We assert the guarantees that matter:

  * genesis on a case's first write (source='initial', old=NULL) and a one-time BASELINE for cases
    already live before the table existed (no blank-history gap for the existing book);
  * narrow shape — ONE row per changed field, grouped by a per-write batch_id; no-op writes add nothing;
  * property_intel captured as sub-values (market_value, live balance) + a content hash, NOT the raw
    blob — and an enriched_at-only wiggle produces NO snapshot (hash stable);
  * EVIDENCE link — a status change (oos_date) with a matching docket line records that line; a status
    change with NO matching docket line leaves evidence NULL (the derivation-on-unchanged-data signal);
  * the CAPTURE BOUNDARY — history is at create_case (sync) granularity: an intermediate value never
    written via create_case is never seen (A→C, never B);
  * append-only — the restore-guard authorizer denies DELETE on case_snapshots.

Run: python3 backend/test_case_snapshots.py   (exit 0 = all green)
"""
import os
import sys
import tempfile
from pathlib import Path

_d = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent))
import main
main.DB_PATH = _d / "pcpeak.db"
main.LEDGER_DB_PATH = _d / "ledger.db"
from fastapi.testclient import TestClient

_results = []
def check(name, cond):
    _results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)


def snaps(cn, field=None):
    with main.get_db() as db:
        if field:
            rows = db.execute("SELECT * FROM ledger.case_snapshots WHERE case_number=? AND field=? "
                              "ORDER BY id", [cn, field]).fetchall()
        else:
            rows = db.execute("SELECT * FROM ledger.case_snapshots WHERE case_number=? "
                              "ORDER BY id", [cn]).fetchall()
        return [dict(r) for r in rows]


def run():
    main.init_db()
    with TestClient(main.app) as c:

        # ── genesis on first write (source='initial', old=NULL) ──
        c.post("/api/cases", json={"case_number": "TX-26-00001", "total_due_filing": 5000,
                                   "property_address": "1 Genesis St", "stage": "pre_judgment"})
        g = snaps("TX-26-00001")
        by = {r["field"]: r for r in g}
        check("first write → genesis rows (source=initial, old=NULL)",
              g and all(r["source"] == "initial" and r["old_value"] is None for r in g))
        check("genesis captured total_due_filing + property_address",
              by.get("total_due_filing", {}).get("new_value") == "5000"
              and by.get("property_address", {}).get("new_value") == "1 Genesis St")
        check("genesis rows share ONE batch_id", len({r["batch_id"] for r in g}) == 1)
        check("genesis does NOT seed an empty field (no oos_date row — it had no value)",
              "oos_date" not in by)

        # ── no-op write: identical payload → no new snapshot rows ──
        n0 = len(snaps("TX-26-00001"))
        c.post("/api/cases", json={"case_number": "TX-26-00001", "total_due_filing": 5000,
                                   "property_address": "1 Genesis St", "stage": "pre_judgment"})
        check("identical re-write adds NO snapshot rows (no-op)", len(snaps("TX-26-00001")) == n0)

        # ── diff on update: one field changes → one new row old→new, source='update' ──
        c.post("/api/cases", json={"case_number": "TX-26-00001", "total_due_filing": 6200})
        td = snaps("TX-26-00001", "total_due_filing")
        check("changing total_due_filing appends a row 5000→6200 (source=update)",
              len(td) == 2 and td[-1]["old_value"] == "5000" and td[-1]["new_value"] == "6200"
              and td[-1]["source"] == "update")

        # ── narrow shape + batch grouping: two fields in one write share a batch, distinct rows ──
        c.post("/api/cases", json={"case_number": "TX-26-00001",
                                   "total_due_filing": 6300, "property_address": "2 Moved Ave"})
        latest = c.get("/api/cases/TX-26-00001/snapshots", headers={}).json()["latest_batch"]
        check("one write with two changes → one batch, two distinct field rows",
              latest and len(latest["fields"]) == 2
              and {f["field"] for f in latest["fields"]} == {"total_due_filing", "property_address"})

        # ── property_intel: sub-values + hash, NOT the raw blob; enriched_at wiggle is a no-op ──
        c.post("/api/cases", json={"case_number": "TX-26-00002",
               "property_intel": '{"market_value": 100000, "current_tax_balance": 5000, "enriched_at": "t1"}'})
        pv = snaps("TX-26-00002", "pi_market_value")
        bal = snaps("TX-26-00002", "pi_tax_balance")
        check("property_intel captured as pi_market_value sub-value (not raw blob)",
              len(pv) == 1 and pv[0]["new_value"] == "100000")
        check("property_intel captured as pi_tax_balance (the live balance)",
              len(bal) == 1 and bal[0]["new_value"] == "5000")
        check("no raw property_intel field is snapshotted",
              not snaps("TX-26-00002", "property_intel"))
        base_hash = snaps("TX-26-00002", "property_intel_hash")
        # only enriched_at changes → hash stable → NO new rows
        n_before = len(snaps("TX-26-00002"))
        c.post("/api/cases", json={"case_number": "TX-26-00002",
               "property_intel": '{"market_value": 100000, "current_tax_balance": 5000, "enriched_at": "t2-LATER"}'})
        check("enriched_at-only change → NO new snapshot (content hash stable)",
              len(snaps("TX-26-00002")) == n_before)
        # a real content change → market_value row + a hash change
        c.post("/api/cases", json={"case_number": "TX-26-00002",
               "property_intel": '{"market_value": 120000, "current_tax_balance": 5000, "enriched_at": "t3"}'})
        pv2 = snaps("TX-26-00002", "pi_market_value")
        check("real market_value change appends 100000→120000",
              len(pv2) == 2 and pv2[-1]["old_value"] == "100000" and pv2[-1]["new_value"] == "120000")
        check("property_intel_hash also changed on real content change",
              len(snaps("TX-26-00002", "property_intel_hash")) == len(base_hash) + 1)

        # ── EVIDENCE: a status change WITH a matching docket line records that line ──
        # events first (mirrors sync order), then the case — so evidence is resolvable at write time
        c.post("/api/events/TX-26-00003", json=[
            {"date": "2026-06-16", "type": "order", "description": "ISSUE ORDER OF SALE"}])
        c.post("/api/cases", json={"case_number": "TX-26-00003", "oos_date": "2026-06-16",
                                   "oos_issued": 1})
        oo = snaps("TX-26-00003", "oos_date")
        with main.get_db() as db:
            ev_id = db.execute("SELECT id FROM docket_events WHERE case_number='TX-26-00003'").fetchone()[0]
        check("oos_date change links to the ISSUE ORDER OF SALE docket line (evidence)",
              len(oo) == 1 and oo[0]["evidence_event_id"] == ev_id
              and "ORDER OF SALE" in (oo[0]["evidence_desc"] or ""))

        # ── EVIDENCE NULL = derivation signal: oos_date set with NO matching docket line ──
        c.post("/api/cases", json={"case_number": "TX-26-00004", "oos_date": "2026-05-01"})
        oo4 = snaps("TX-26-00004", "oos_date")
        check("oos_date change with NO docket line → evidence NULL (derivation-on-unchanged-data signal)",
              len(oo4) == 1 and oo4[0]["evidence_event_id"] is None and oo4[0]["evidence_desc"] is None)

        # ── CAPTURE BOUNDARY: history is sync-granular. An intermediate value never written via
        # create_case is never seen — A then C (skipping B) yields A→C, never A→B→C. ──
        c.post("/api/cases", json={"case_number": "TX-26-00005", "total_due_filing": 100})  # A (genesis)
        c.post("/api/cases", json={"case_number": "TX-26-00005", "total_due_filing": 300})  # C (B=200 never written)
        b = snaps("TX-26-00005", "total_due_filing")
        vals = [(r["old_value"], r["new_value"]) for r in b]
        check("boundary: only create_case-written states are seen (A→C, no phantom B)",
              vals == [(None, "100"), ("100", "300")])

        # ── GET endpoint shape ──
        r = c.get("/api/cases/TX-26-00001/snapshots").json()
        check("GET snapshots returns count + newest-first rows + latest_batch",
              r["count"] > 0 and r["snapshots"][0]["id"] > r["snapshots"][-1]["id"]
              and r["latest_batch"] is not None)

        # ── export includes case_snapshots ──
        os.environ["LEDGER_EXPORT_TOKEN"] = "exp-secret"
        ex = c.get("/api/ledger/export", headers={"X-Ledger-Token": "exp-secret"}).json()
        check("ledger export now carries case_snapshots + its count",
              "case_snapshots" in ex and ex["counts"]["case_snapshots"] == len(ex["case_snapshots"])
              and ex["counts"]["case_snapshots"] > 0)

        # ── append-only: the restore-guard authorizer denies DELETE on case_snapshots ──
        denied = False
        try:
            with main.get_db() as db:
                db.execute("DELETE FROM ledger.case_snapshots WHERE case_number='TX-26-00001'")
        except sqlite3_err():
            denied = True
        check("DELETE on case_snapshots is DENIED (append-only, restore-guarded)", denied)

    # ── BASELINE backfill: a case present before the table had any snapshot gets a genesis baseline ──
    # Insert a case directly (bypassing create_case so it has no snapshot), then re-run init_db.
    with main.get_db() as db:
        db.execute("INSERT INTO cases (case_number, total_due_filing, property_address) "
                   "VALUES ('TX-26-09999', 42000, '9 Legacy Rd')")
    check("precondition: legacy case has no snapshots yet", len(snaps("TX-26-09999")) == 0)
    main.init_db()
    bl = snaps("TX-26-09999")
    check("init_db seeds a BASELINE for a pre-existing case (source=baseline, old=NULL)",
          bl and all(r["source"] == "baseline" and r["old_value"] is None for r in bl)
          and any(r["field"] == "total_due_filing" and r["new_value"] == "42000" for r in bl))
    n_bl = len(snaps("TX-26-09999"))
    main.init_db()   # idempotent
    check("re-running init_db does NOT duplicate the baseline (idempotent)",
          len(snaps("TX-26-09999")) == n_bl)

    print("-" * 56)
    total, passed = len(_results), sum(_results)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


def sqlite3_err():
    import sqlite3
    return sqlite3.DatabaseError


if __name__ == "__main__":
    sys.exit(run())
