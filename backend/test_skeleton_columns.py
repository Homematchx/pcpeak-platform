#!/usr/bin/env python3
"""Phase 1 of the skeleton-cache design: current_tax_balance + market_value promoted to columns.

The whole point of promoting these is so the sidebar list + amount-owed filter render without the
15KB property_intel blob. The INVARIANT that must never break: the column is the SAME value as
property_intel.current_tax_balance — promoted, never re-derived — so the card/filter figure stays
identical to what the Financials and Acquisition tabs show (the standing same-number-everywhere rule).

Pins:
  * migration backfills the columns from property_intel;
  * the column EXACTLY equals property_intel.current_tax_balance (no drift, incl. None→None);
  * create_case keeps them in lockstep on every write, and a partial update (no property_intel in
    the payload) preserves them from the existing blob;
  * a None/absent balance yields a None column, never a fabricated 0;
  * /api/cases carries them.

Run: python3 backend/test_skeleton_columns.py   (exit 0 = all green)
"""
import json
import sys
import tempfile
from pathlib import Path

_d = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent))
import main
main.DB_PATH = _d / "pcpeak.db"
main.LEDGER_DB_PATH = _d / "ledger.db"
from fastapi.testclient import TestClient

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)


def col(cn, field):
    with main.get_db() as db:
        r = db.execute(f"SELECT {field} FROM cases WHERE case_number=?", [cn]).fetchone()
        return r[0] if r else "MISSING"


def run():
    main.init_db()
    with TestClient(main.app) as c:
        # a real balance promotes to the column, identical to the blob
        c.post("/api/cases", json={"case_number": "TX-01",
               "property_intel": json.dumps({"current_tax_balance": 15691.0, "market_value": 210000})})
        check("current_tax_balance column == blob value (same number, promoted)", col("TX-01", "current_tax_balance") == 15691.0)
        check("market_value column == blob value", col("TX-01", "market_value") == 210000)
        row = c.get("/api/cases/TX-01").json()
        check("the column equals property_intel.current_tax_balance exactly",
              row["current_tax_balance"] == json.loads(row["property_intel"])["current_tax_balance"])
        check("/api/cases carries the promoted columns",
              any(x["case_number"] == "TX-01" and x.get("current_tax_balance") == 15691.0
                  for x in c.get("/api/cases").json()))

        # a real 0.0 balance is a real value, distinct from unknown
        c.post("/api/cases", json={"case_number": "TX-ZERO",
               "property_intel": json.dumps({"current_tax_balance": 0.0})})
        check("a real 0.0 balance promotes as 0.0 (not None)", col("TX-ZERO", "current_tax_balance") == 0.0)

        # unknown balance → None column, never a fabricated 0
        c.post("/api/cases", json={"case_number": "TX-UNK",
               "property_intel": json.dumps({"market_value": 99000})})
        check("absent balance → None column (never a fabricated 0)", col("TX-UNK", "current_tax_balance") is None)
        check("...market_value still promotes", col("TX-UNK", "market_value") == 99000)

        # no property_intel at all → both None
        c.post("/api/cases", json={"case_number": "TX-BARE", "property_address": "1 Bare St"})
        check("no property_intel → both columns None",
              col("TX-BARE", "current_tax_balance") is None and col("TX-BARE", "market_value") is None)

        # lockstep on update: change the blob's balance → column follows
        c.post("/api/cases", json={"case_number": "TX-01",
               "property_intel": json.dumps({"current_tax_balance": 18000.0, "market_value": 210000})})
        check("re-writing property_intel updates the column in lockstep", col("TX-01", "current_tax_balance") == 18000.0)

        # partial update (no property_intel) preserves the column from the existing blob
        c.post("/api/cases", json={"case_number": "TX-01", "rep_assigned": "Jay Lewis"})
        check("a partial update (no property_intel) preserves the balance column",
              col("TX-01", "current_tax_balance") == 18000.0)

        # the promoted value must match _pi_subvalues (the one extractor) exactly
        mv, bal, _ = main._pi_subvalues(json.dumps({"current_tax_balance": 4483.61, "market_value": 286730}))
        c.post("/api/cases", json={"case_number": "TX-EXTRACT",
               "property_intel": json.dumps({"current_tax_balance": 4483.61, "market_value": 286730})})
        check("column uses the same _pi_subvalues extractor (no second parser to drift)",
              col("TX-EXTRACT", "current_tax_balance") == bal and col("TX-EXTRACT", "market_value") == mv)

    print("-" * 60)
    total, passed = len(_res), sum(_res)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
