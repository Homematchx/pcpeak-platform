#!/usr/bin/env python3
"""Restore guard test (design doc §11-12). prediction_ledger + rep_actions are prod-owned,
non-regenerable, and live in a SEPARATE file (ledger.db) attached as schema `ledger`. This
proves the disaster-recovery path — a raw `sqlite3 pcpeak.db .dump` / restore — physically
cannot touch them (structural guarantee), plus the SQLite authorizer denies DELETE/DROP on
them (defense in depth) while still allowing the legitimate append (logging) + one-time
column-fill (reconcile), and cross-attached-DB joins work.

Run: python3 backend/test_restore_guard.py   (exit 0 = all green)

Must be green before the schema migration (step 2) or create_case logging (step 3) put any
real data in the prod-owned tables. Uses throwaway DBs + minimal fixture tables (real schema
lands in step 2 inside ledger.db).
"""
import sys, tempfile, sqlite3, hashlib, json
from pathlib import Path

# Point the backend at throwaway main + ledger DBs BEFORE using it.
_d = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent))
import main
main.DB_PATH = _d / "pcpeak.db"
main.LEDGER_DB_PATH = _d / "ledger.db"
from fastapi.testclient import TestClient

_results = []
def check(name, cond):
    _results.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

def snapshot(table):
    with main.get_db() as db:
        rows = [dict(r) for r in db.execute(f"SELECT * FROM ledger.{table} ORDER BY id").fetchall()]
    return len(rows), hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()

def rows_by_id(table):
    with main.get_db() as db:
        return {r["id"]: json.dumps(dict(r), sort_keys=True, default=str)
                for r in db.execute(f"SELECT * FROM ledger.{table}").fetchall()}

def setup_ledger_fixtures():
    # init_db (called first in run()) creates the real ledger schema; just seed dummy rows,
    # honoring the NOT NULL columns (model_version, prediction_basis, input_hash).
    with main.get_db() as db:
        db.execute("INSERT INTO ledger.prediction_ledger (case_number,model_version,prediction_basis,projected_oos,confidence_pct,input_hash) VALUES ('TX-99-00001','test-v1','judged','2026-09-01',55,'h1')")
        db.execute("INSERT INTO ledger.prediction_ledger (case_number,model_version,prediction_basis,projected_oos,confidence_pct,input_hash) VALUES ('TX-99-00002','test-v1','filed','2026-10-01',40,'h2')")
        db.execute("INSERT INTO ledger.rep_actions (case_number,rep,action_type) VALUES ('TX-99-00001','alice','contact_made')")

def run():
    main.init_db()                     # main schema (cases/events/reps/...); get_db attaches ledger.db
    setup_ledger_fixtures()
    pl0, ra0 = snapshot("prediction_ledger"), snapshot("rep_actions")
    pl_ids0, ra_ids0 = rows_by_id("prediction_ledger"), rows_by_id("rep_actions")

    # ── A. STRUCTURAL DR-SAFETY: a raw pcpeak.db dump/restore cannot reference ledger.db ──
    raw = sqlite3.connect(main.DB_PATH)          # main only — ledger NOT attached to this conn
    dump_sql = "\n".join(raw.iterdump()); raw.close()
    check("raw `pcpeak.db .dump` excludes prediction_ledger", "prediction_ledger" not in dump_sql)
    check("raw `pcpeak.db .dump` excludes rep_actions",       "rep_actions" not in dump_sql)
    # Apply that dump to a throwaway DB via executescript — exactly the raw DR restore, which
    # bypasses get_db()/the authorizer entirely. ledger.db is a separate file → untouched.
    fresh = _d / "restored_main.db"
    rc = sqlite3.connect(fresh); rc.executescript(dump_sql); rc.commit(); rc.close()
    check("raw restore of pcpeak.db leaves ledger.db byte-identical", snapshot("prediction_ledger")==pl0 and snapshot("rep_actions")==ra0)

    # ── B. Authorizer teeth (defense in depth): DELETE/DROP on prod-owned tables DENIED ──
    for tbl in main.PROD_OWNED_TABLES:
        for op in (f"DELETE FROM ledger.{tbl}", f"DELETE FROM {tbl}", f"DROP TABLE ledger.{tbl}", f"DROP TABLE IF EXISTS {tbl}"):
            denied = False
            try:
                with main.get_db() as db: db.execute(op)
            except sqlite3.DatabaseError:
                denied = True
            check(f"denied: {op}", denied)

    # ── C. Normal restore path (create_case + event POST — what sync_to_prod does) ──
    with TestClient(main.app) as client:
        r1 = client.post("/api/cases", json={"case_number":"TX-99-00001","defendant":"New Guy",
             "filed_date":"2025-01-01","judgment_date":"2025-06-01","complexity":"medium","city":"dallas"})
        r2 = client.post("/api/cases", json={"case_number":"TX-99-00001","property_address":"1 Main St"})
        r3 = client.post("/api/events/TX-99-00001", json=[{"event_date":"2025-06-01","event_type":"JUDGMENT","description":"x"}])
    check("restore POSTs succeeded (200)", r1.status_code==200 and r2.status_code==200 and r3.status_code==200)
    # create_case now LOGS a prediction (append-only), so the invariant is no longer
    # "byte-identical" — it's "no pre-existing prod-owned row is deleted or mutated" (append OK).
    pl_now, ra_now = rows_by_id("prediction_ledger"), rows_by_id("rep_actions")
    check("restore preserves every pre-existing prediction_ledger row unchanged (append-only)",
          all(pl_now.get(i) == v for i, v in pl_ids0.items()))
    check("restore preserves every pre-existing rep_actions row unchanged",
          all(ra_now.get(i) == v for i, v in ra_ids0.items()))
    check("restore never deleted a prod-owned row (count only grew or held)",
          len(pl_now) >= len(pl_ids0) and len(ra_now) >= len(ra_ids0))

    # ── D. Legit append (logging) + one-time column-fill (reconcile) still WORK ──
    ok = False
    try:
        with main.get_db() as db:
            db.execute("INSERT INTO ledger.prediction_ledger (case_number,model_version,prediction_basis,projected_oos,confidence_pct,input_hash) VALUES ('TX-99-00003','test-v1','judged','2026-11-01',30,'h3')")
            db.execute("UPDATE ledger.prediction_ledger SET outcome_type='oos_issued', error_days=12 WHERE case_number='TX-99-00003'")
            db.execute("INSERT INTO ledger.rep_actions (case_number,rep,action_type) VALUES ('TX-99-00003','bob','offer')")
        ok = True
    except Exception as e:
        print("   legit-op error:", e)
    check("legit INSERT (log) + UPDATE (reconcile) still allowed", ok)

    # ── E. Cross-attached-DB join (reconcile shape) works ──
    joined = None
    with main.get_db() as db:
        joined = db.execute("SELECT pl.case_number, pl.projected_oos, c.oos_date "
                            "FROM ledger.prediction_ledger pl JOIN cases c ON c.case_number=pl.case_number "
                            "WHERE pl.prediction_basis IN ('judged','filed','next_hearing')").fetchall()
    check("cross-DB join ledger⋈cases returns rows", joined is not None and len(joined) >= 1)

    # ── F. assert_restore_safe tripwire for any future bulk path ──
    tripped = False
    try: main.assert_restore_safe(["cases", "prediction_ledger"])
    except RuntimeError: tripped = True
    check("assert_restore_safe rejects a prod-owned target", tripped)

    # ── G. Re-running init_db (boot migration) is non-destructive to prod-owned data ──
    n_before = snapshot("prediction_ledger")[0]
    main.init_db()
    check("re-running init_db preserves prod-owned rows", snapshot("prediction_ledger")[0]==n_before)

    print(f"\n{sum(_results)}/{len(_results)} checks passed")
    return all(_results)

if __name__ == "__main__":
    sys.exit(0 if run() else 1)
