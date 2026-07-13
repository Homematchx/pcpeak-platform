#!/usr/bin/env python3
"""Step-3 tests: log_prediction + reconcile + sweep_expired, wired into create_case (the write
path). Covers: new prediction logged; dedup on unchanged re-POST; new row on meaningful change;
BPP not logged; outcome reconciliation with signed error_days; idempotent reconcile; expiry.

Dates are relative to now() so the judged cases are genuine FORWARD predictions (projected_oos
in the future — not stale) regardless of when the test runs.

Run: python3 backend/test_ledger.py   (exit 0 = all green)
"""
import sys, tempfile
from pathlib import Path
from datetime import datetime, timedelta

_d = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent))
import main
main.DB_PATH = _d / "pcpeak.db"
main.LEDGER_DB_PATH = _d / "ledger.db"
from fastapi.testclient import TestClient

_results = []
def check(name, cond):
    _results.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

def ledger(cn):
    with main.get_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM ledger.prediction_ledger WHERE case_number=? ORDER BY id", [cn])]

def run():
    main.init_db()
    now = datetime.now()
    def d(days): return (now + timedelta(days=days)).strftime("%Y-%m-%d")

    with TestClient(main.app) as client:
        # ── 1. New judged case (recent judgment → projected in the future, not stale) ──
        client.post("/api/cases", json={"case_number":"TX-J-1","judgment_date":d(-40),"filed_date":d(-220),"complexity":"medium","city":"dallas"})
        rows = ledger("TX-J-1")
        check("new judged case logs exactly 1 row", len(rows) == 1)
        check("  basis='judged'", rows and rows[0]["prediction_basis"] == "judged")
        check("  confidence recorded as the forward-prediction value (55), not zeroed", rows and rows[0]["confidence_pct"] == 55)
        check("  model_version + input_hash + used_joos_days stored", rows and rows[0]["model_version"]==main.MODEL_VERSION and rows[0]["input_hash"] and rows[0]["used_joos_days"] is not None)

        # ── 2. Re-POST identical state → NO new row (input_hash dedup) ──
        client.post("/api/cases", json={"case_number":"TX-J-1","judgment_date":d(-40),"filed_date":d(-220),"complexity":"medium","city":"dallas"})
        check("re-POST unchanged logs NO new row (dedup)", len(ledger("TX-J-1")) == 1)

        # ── 2b. A non-driver change (stage) also must NOT log a new row ──
        client.post("/api/cases", json={"case_number":"TX-J-1","stage":"judgment_entered"})
        check("non-driver change (stage) logs NO new row", len(ledger("TX-J-1")) == 1)

        # ── 3. Meaningful change (new judgment_date → new projected_oos) → a new row ──
        client.post("/api/cases", json={"case_number":"TX-J-1","judgment_date":d(-20)})
        check("meaningful change appends a new row", len(ledger("TX-J-1")) == 2)

        # ── 4. BPP case → NOT logged (na_bpp is not a real-estate prediction) ──
        client.post("/api/cases", json={"case_number":"TX-BPP-1","property_type":"personal","judgment_date":d(-40),"filed_date":d(-220)})
        check("BPP case logs NO prediction row", len(ledger("TX-BPP-1")) == 0)

        # ── 5. Reconcile: a judged forward prediction resolves when a real oos_date lands ──
        client.post("/api/cases", json={"case_number":"TX-R-1","judgment_date":d(-40),"filed_date":d(-220),"complexity":"medium","city":"dallas"})
        projected = ledger("TX-R-1")[0]["projected_oos"]
        actual_oos = d(10)   # real Order of Sale lands
        client.post("/api/cases", json={"case_number":"TX-R-1","oos_date":actual_oos,"oos_issued":True})
        rows = ledger("TX-R-1")
        judged_row = [r for r in rows if r["prediction_basis"] == "judged"][0]
        check("reconcile resolves the forward prediction (outcome_type=oos_issued)", judged_row["outcome_type"] == "oos_issued")
        check("  outcome_date = the real oos_date", judged_row["outcome_date"] == actual_oos)
        exp_err = (datetime.strptime(actual_oos,"%Y-%m-%d") - datetime.strptime(projected,"%Y-%m-%d")).days
        check("  error_days = actual - projected (signed)", judged_row["error_days"] == exp_err)
        check("  a 'confirmed' row was also logged after the OOS", any(r["prediction_basis"]=="confirmed" for r in rows))

        # ── 6. Reconcile idempotent (re-POST the resolved case doesn't re-touch it) ──
        client.post("/api/cases", json={"case_number":"TX-R-1","property_address":"9 Elm"})
        after = [r for r in ledger("TX-R-1") if r["prediction_basis"]=="judged"][0]
        check("reconcile idempotent (resolved row unchanged on re-POST)", after["resolved_at"]==judged_row["resolved_at"] and after["error_days"]==exp_err)

        # ── 7. sweep_expired: an old forward prediction with no outcome → expired_no_oos ──
        client.post("/api/cases", json={"case_number":"TX-E-1","judgment_date":d(-400),"filed_date":d(-700),"complexity":"medium","city":"dallas"})
        check("TX-E-1 logged as judged, unresolved", ledger("TX-E-1")[0]["prediction_basis"]=="judged" and ledger("TX-E-1")[0]["outcome_type"] is None)
        with main.get_db() as db:
            n = main.sweep_expired(db)
        check("sweep_expired marks the stale forward prediction expired_no_oos", ledger("TX-E-1")[0]["outcome_type"] == "expired_no_oos")
        check("  sweep returned a positive count", n >= 1)
        check("  sweep did NOT touch the still-valid future prediction (TX-J-1)", all(r["outcome_type"] is None for r in ledger("TX-J-1")))

    print(f"\n{sum(_results)}/{len(_results)} checks passed")
    return all(_results)

if __name__ == "__main__":
    sys.exit(0 if run() else 1)
