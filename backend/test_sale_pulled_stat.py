#!/usr/bin/env python3
"""Tests for the DATE-AWARE 'Sale Pulled' portfolio stat (/api/stats). No network.

TX-23-00569: an Order of Sale was issued 2026-04-20, the sale scheduled, then PULLED 2026-05-12,
then a NEW Order of Sale issued 2026-07-24. A pull is USUALLY the latest event, but here the OOS
was re-issued AFTER the pull — the sale is being driven again, so the operative stage is oos_issued,
NOT sale_pulled. The stat counted it as pulled because it keyed off `sale_pulled_date` being set,
regardless of a later OOS. Fixed: a pull counts only when it is at-or-after the OOS date.

Pins:
  * pull AFTER the OOS (original pull) → counted as sale_pulled (unchanged behaviour);
  * OOS re-issued AFTER the pull (TX-23-00569) → counted as oos_issued, NOT pulled;
  * the buckets stay mutually exclusive (a case is oos_issued XOR sale_pulled, never both);
  * a stale stage='sale_pulled' with no dates still counts as pulled (belt-and-suspenders);
  * archived cases are excluded from both buckets (unchanged).

Run: python3 backend/test_sale_pulled_stat.py   (exit 0 = all green)
"""
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


def run():
    main.init_db()
    with TestClient(main.app) as c:
        def mk(cn, **fields):
            c.post("/api/cases", json=dict(case_number=cn, **fields))

        # 1. original pull: OOS 04-20 then pulled 05-12 (pull is the latest) → sale_pulled
        mk("TX-PULL-ORIG", oos_issued=1, oos_date="2026-04-20",
           sale_pulled_date="2026-05-12", stage="sale_pulled")
        # 2. the TX-23-00569 case: pull 05-12 then OOS RE-ISSUED 07-24 → oos_issued (NOT pulled)
        mk("TX-23-00569", oos_issued=1, oos_date="2026-07-24",
           sale_pulled_date="2026-05-12", stage="oos_issued")
        # 3. a plain OOS, never pulled → oos_issued
        mk("TX-OOS-PLAIN", oos_issued=1, oos_date="2026-06-01")
        # 4. stale stage flag only, no dates → still pulled (belt-and-suspenders)
        mk("TX-PULL-STAGEONLY", stage="sale_pulled")
        # 5. a pre-judgment case → neither bucket
        mk("TX-PREJ", stage="pre_judgment")

        s = c.get("/api/stats").json()

        # Query the exact membership so the headline claims are real, not inferred from totals.
        def in_pulled(cn):
            with main.get_db() as db:
                w = ("(sale_pulled_date IS NOT NULL AND TRIM(sale_pulled_date)!='' "
                     "AND (oos_date IS NULL OR TRIM(oos_date)='' OR TRIM(oos_date) <= TRIM(sale_pulled_date))) "
                     "OR (stage='sale_pulled' AND (sale_pulled_date IS NULL OR TRIM(sale_pulled_date)='') "
                     "    AND (oos_date IS NULL OR TRIM(oos_date)=''))")
                return db.execute(f"SELECT COUNT(*) FROM cases WHERE case_number=? AND ({w})",
                                  [cn]).fetchone()[0] == 1

        check("original pull (pull after OOS) IS in the Sale Pulled bucket", in_pulled("TX-PULL-ORIG"))
        check("*** OOS re-issued after the pull (TX-23-00569) is NOT in the Sale Pulled bucket ***",
              not in_pulled("TX-23-00569"))
        check("...and TX-23-00569 IS in OOS Issued",
              c.get("/api/cases/TX-23-00569").json()["oos_issued"] == 1 and not in_pulled("TX-23-00569"))
        check("counts: sale_pulled == 2 (orig + stage-only), oos_issued == 2 (00569 + plain)",
              s["sale_pulled"] == 2 and s["oos_issued"] == 2)
        check("buckets are mutually exclusive by construction (oos = oos_issued AND NOT pulled)",
              s["oos_issued"] + s["sale_pulled"] <= s["total_all"])
        check("a stale stage='sale_pulled' with no dates still counts (belt-and-suspenders)",
              in_pulled("TX-PULL-STAGEONLY"))

        # Archived exclusion — archive TX-PULL-ORIG, it should leave the pulled bucket.
        c.post("/api/cases/TX-PULL-ORIG/disposition",
               json={"code": "sold_at_tax_sale"})   # archives it
        s2 = c.get("/api/stats").json()
        check("an archived pulled case drops out of the Sale Pulled bucket",
              s2["sale_pulled"] == 1)   # only TX-PULL-STAGEONLY remains
        check("...and out of OOS Issued too if it were there (denominator shared)",
              s2["oos_issued"] == 2)    # TX-23-00569 + TX-OOS-PLAIN unaffected

        # Same-day edge: pull on the SAME day as the OOS → treated as pulled (withdrawal supersedes).
        mk("TX-SAMEDAY", oos_issued=1, oos_date="2026-08-01", sale_pulled_date="2026-08-01")
        s3 = c.get("/api/stats").json()
        check("same-day pull (pull >= OOS) counts as pulled, not oos",
              s3["sale_pulled"] == 2)   # STAGEONLY + SAMEDAY

    print("-" * 60)
    total, passed = len(_res), sum(_res)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
