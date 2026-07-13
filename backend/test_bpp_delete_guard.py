#!/usr/bin/env python3
"""Guard test for DELETE /api/cases/{cn} — it must be STRUCTURALLY incapable of deleting a
real-property (or NULL/'unknown') case, because the BPP constraint lives in the DELETE's WHERE
clause (property_type='personal'), not an application-level check that trusts the caller.

Proves (same standard as the sync --only test): passing a REAL case number is refused and the
real case + its docket events are untouched; only a genuine BPP case is deletable.

Run: python3 backend/test_bpp_delete_guard.py   (exit 0 = all green)
"""
import sys, tempfile
from pathlib import Path

_d = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent))
import main
main.DB_PATH = _d / "pcpeak.db"
main.LEDGER_DB_PATH = _d / "ledger.db"
from fastapi.testclient import TestClient

_results = []
def check(name, cond):
    _results.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

def exists(client, cn):
    return client.get(f"/api/cases/{cn}").status_code == 200

def run():
    main.init_db()
    with TestClient(main.app) as client:
        # seed a real, a BPP, and an undetermined case (each with a docket event)
        client.post("/api/cases", json={"case_number":"TX-REAL-1","property_type":"real","defendant":"Real Owner","filed_date":"2025-01-01"})
        client.post("/api/cases", json={"case_number":"TX-BPP-1","property_type":"personal","defendant":"Biz Inc","filed_date":"2025-01-01"})
        client.post("/api/cases", json={"case_number":"TX-UNK-1","property_type":"unknown","defendant":"Maybe","filed_date":"2025-01-01"})
        for cn in ("TX-REAL-1","TX-BPP-1","TX-UNK-1"):
            client.post(f"/api/events/{cn}", json=[{"event_date":"2025-02-01","event_type":"X","description":"e"}])

        # ── 1. REAL case number is REFUSED (the core structural guarantee) ──
        r = client.request("DELETE", "/api/cases/TX-REAL-1")
        check("DELETE on a real-property case is refused (409)", r.status_code == 409)
        check("  ...and the real case still exists", exists(client, "TX-REAL-1"))
        ev = client.get("/api/events/TX-REAL-1").json()
        check("  ...and the real case's docket events are untouched", isinstance(ev, list) and len(ev) >= 1)

        # ── 2. 'unknown' (undetermined) is also refused — only 'personal' is deletable ──
        r = client.request("DELETE", "/api/cases/TX-UNK-1")
        check("DELETE on an 'unknown' case is refused (409)", r.status_code == 409)
        check("  ...and the unknown case still exists", exists(client, "TX-UNK-1"))

        # ── 3. Non-existent case → 404 (not a silent success) ──
        r = client.request("DELETE", "/api/cases/TX-NOPE-9")
        check("DELETE on a non-existent case is 404", r.status_code == 404)

        # ── 4. A genuine BPP case IS deletable, and its events go too ──
        r = client.request("DELETE", "/api/cases/TX-BPP-1")
        check("DELETE on a real BPP case succeeds (200)", r.status_code == 200)
        check("  ...and the BPP case is gone", not exists(client, "TX-BPP-1"))
        ev = client.get("/api/events/TX-BPP-1").json()
        check("  ...and its docket events are gone", isinstance(ev, list) and len(ev) == 0)

        # ── 5. Belt-and-suspenders: even a direct DB DELETE with the guard clause can't hit real ──
        with main.get_db() as db:
            cur = db.execute("DELETE FROM cases WHERE case_number=? AND property_type='personal'", ["TX-REAL-1"])
            check("raw guarded DELETE matches 0 rows for a real case", cur.rowcount == 0)
        check("  ...real case survives the raw attempt", exists(client, "TX-REAL-1"))

    print(f"\n{sum(_results)}/{len(_results)} checks passed")
    return all(_results)

if __name__ == "__main__":
    sys.exit(0 if run() else 1)
