#!/usr/bin/env python3
"""Step-1 restore guard test (design doc §12). Proves that prediction_ledger + rep_actions
(prod-owned, non-regenerable) are never deleted/dropped/overwritten by the local→prod restore
path, and that the SQLite authorizer denies DELETE/DROP on them while still allowing the
legitimate append (logging) + one-time column-fill (reconcile).

Run: python3 backend/test_restore_guard.py   (exit 0 = all green)

This MUST be green before the schema migration (step 2) or create_case logging (step 3) put
any real data in the prod-owned tables. It uses a throwaway DB and minimal fixture versions of
the two tables (real schema lands in step 2); the guard is table-NAME based, so it already
protects those names.
"""
import sys, tempfile, sqlite3, hashlib, json
from pathlib import Path

# Point the backend at a throwaway DB BEFORE using it. get_db() reads main.DB_PATH per call.
_tmpdb = Path(tempfile.mkdtemp()) / "guard_test.db"
sys.path.insert(0, str(Path(__file__).parent))
import main
main.DB_PATH = _tmpdb
from fastapi.testclient import TestClient

_results = []
def check(name, cond):
    _results.append(cond)
    print(("  PASS  " if cond else "  FAIL  ") + name)

def snapshot(table):
    with main.get_db() as db:
        rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
    return len(rows), hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()

def setup_prod_owned_fixtures():
    # Minimal, forward-compatible fixtures (case_number + outcome_type both exist in the real
    # step-2 schema, so this test stays valid once init_db creates the real tables).
    with main.get_db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS prediction_ledger "
                   "(id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT, projected_oos TEXT, "
                   " confidence_pct INTEGER, outcome_type TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS rep_actions "
                   "(id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT, rep TEXT, action_type TEXT)")
        db.execute("INSERT INTO prediction_ledger (case_number,projected_oos,confidence_pct) VALUES ('TX-99-00001','2026-09-01',55)")
        db.execute("INSERT INTO prediction_ledger (case_number,projected_oos,confidence_pct) VALUES ('TX-99-00002','2026-10-01',40)")
        db.execute("INSERT INTO rep_actions (case_number,rep,action_type) VALUES ('TX-99-00001','alice','contact_made')")

def run():
    main.init_db()                     # builds scraped-data schema (cases/events/reps/...)
    setup_prod_owned_fixtures()

    pl0, ra0 = snapshot("prediction_ledger"), snapshot("rep_actions")

    # ── 1. Run the ACTUAL restore path (exactly what sync_to_prod POSTs) ──
    with TestClient(main.app) as client:
        r1 = client.post("/api/cases", json={"case_number":"TX-99-00001","defendant":"New Guy",
             "filed_date":"2025-01-01","judgment_date":"2025-06-01","complexity":"medium","city":"dallas"})  # INSERT
        r2 = client.post("/api/cases", json={"case_number":"TX-99-00001","property_address":"1 Main St"})     # UPDATE existing
        r3 = client.post("/api/events/TX-99-00001", json=[{"event_date":"2025-06-01","event_type":"JUDGMENT","description":"x"}])
    check("restore POSTs succeeded (200)", r1.status_code==200 and r2.status_code==200 and r3.status_code==200)
    check("restore leaves prediction_ledger byte-identical", snapshot("prediction_ledger")==pl0)
    check("restore leaves rep_actions byte-identical",       snapshot("rep_actions")==ra0)

    # ── 2. Authorizer teeth: DELETE/DROP on prod-owned tables are DENIED ──
    for tbl in main.PROD_OWNED_TABLES:
        for op in (f"DELETE FROM {tbl}", f"DELETE FROM {tbl} WHERE id=1", f"DROP TABLE {tbl}", f"DROP TABLE IF EXISTS {tbl}"):
            denied = False
            try:
                with main.get_db() as db:
                    db.execute(op)
            except sqlite3.DatabaseError:
                denied = True
            check(f"denied: {op}", denied)

    # ── 3. Legit append (logging) + one-time column-fill (reconcile) still WORK ──
    ok = False
    try:
        with main.get_db() as db:
            db.execute("INSERT INTO prediction_ledger (case_number,projected_oos,confidence_pct) VALUES ('TX-99-00003','2026-11-01',30)")
            db.execute("UPDATE prediction_ledger SET outcome_type='oos_issued' WHERE case_number='TX-99-00003'")
            db.execute("INSERT INTO rep_actions (case_number,rep,action_type) VALUES ('TX-99-00003','bob','offer')")
        ok = True
    except Exception as e:
        print("   legit-op error:", e)
    check("legit INSERT (log) + UPDATE (reconcile) still allowed", ok)

    # ── 4. assert_restore_safe() tripwire for any FUTURE bulk path ──
    tripped = False
    try: main.assert_restore_safe(["cases", "prediction_ledger"])
    except RuntimeError: tripped = True
    check("assert_restore_safe rejects a prod-owned target", tripped)
    allowed = True
    try: main.assert_restore_safe(["cases", "docket_events", "reps"])
    except RuntimeError: allowed = False
    check("assert_restore_safe allows scraped tables", allowed)

    # ── 5. Re-running init_db (boot migration) is non-destructive to prod-owned data ──
    n_before = snapshot("prediction_ledger")[0]
    main.init_db()
    check("re-running init_db preserves prod-owned rows", snapshot("prediction_ledger")[0]==n_before)

    print(f"\n{sum(_results)}/{len(_results)} checks passed")
    return all(_results)

if __name__ == "__main__":
    sys.exit(0 if run() else 1)
