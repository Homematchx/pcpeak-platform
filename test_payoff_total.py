#!/usr/bin/env python3
"""§26 — verified collector balances enter the PAYOFF TOTAL.

THE DEFECT THIS CLOSES. §23 built per-collector lines and deliberately left the scalar `tax_payoff`
alone, so nothing shifted underneath while every external line was `unavailable`. That was right then
and WRONG the moment real amounts existed: `known_total` was computed and consumed by nothing, so the
fetched balances were DISPLAYED BUT NOT COUNTED and the 4x understatement survived in the decision
math behind honest-looking lines.

THE RULE
    ACT live + verified external                       (both present)
    verified external alone                            (ACT $0/absent but a collector was fetched)
    filing-derived fallback                            (neither — and NEVER fallback + external)

⚠ THE DOUBLE-COUNT TRAP, pinned below: `total_due_filing` is the PETITION total, which already
includes every plaintiff collector's filed amount. Adding fetched external balances on top of the
fallback estimate would count them twice — measured at 9 of 63 backfilled cases.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import acquisition as A
from acquisition import AcquisitionInputs, CaseInput

_passed, _failed = 0, 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  → {detail}" if detail else ""))


TB = [{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 12108.43},
      {"entity": "CITY OF GARLAND", "total": 7666.63}]
BOTH = {"GARLAND ISD": 12108.43, "CITY OF GARLAND": 7666.63}


def test_cambridge_ground_truth():
    print("\n3909 Cambridge Dr — measured by hand, to the cent")
    c = CaseInput("CAMB", owed=5974.81, total_due_filing=20000, property_type="real",
                  tax_breakdown=TB, collector_balances=BOTH)
    p = A.tax_payoff(c)
    check("payoff is ACT + both collectors = $25,750", p["amount"] == 25750, str(p["amount"]))
    check("basis says so", p["basis"] == "act_plus_collectors", p["basis"])
    check("complete ⇒ VERIFIED", p["label"] == A.VERIFIED)
    old = A.tax_payoff(CaseInput("CAMB", owed=5974.81, total_due_filing=20000, property_type="real"))
    check("the OLD ACT-only payoff was $5,975 — 23% of the truth", old["amount"] == 5975)


def test_incomplete_is_a_labelled_floor():
    print("\nincomplete — a floor, never a confident total")
    c = CaseInput("X", owed=5974.81, total_due_filing=20000, property_type="real",
                  tax_breakdown=TB, collector_balances={"GARLAND ISD": 12108.43})
    p = A.tax_payoff(c)
    check("sums only what was retrieved ($18,083)", p["amount"] == 18083, str(p["amount"]))
    check("an unretrieved collector contributes NOTHING (not $0 silently)",
          p["amount"] == round(5974.81 + 12108.43))
    check("label drops to ESTIMATED while incomplete", p["label"] == A.ESTIMATED, p["label"])
    check("the note names it a FLOOR and counts what is missing", "FLOOR" in p["note"])


def test_double_count_guard():
    print("\nthe double-count trap — fallback and external can NEVER combine")
    # ACT $0, nothing fetched → the filing-derived estimate, exactly as before.
    c1 = CaseInput("X", owed=0.0, total_due_filing=11329.20, filed_date="2026-01-01",
                   property_type="real", tax_breakdown=TB)
    p1 = A.tax_payoff(c1, __import__("datetime").date(2026, 8, 17))
    check("no fetched balance → fallback estimate", p1["basis"] == "fallback_estimate")
    # ACT $0 but collectors fetched → the REAL figure, not the estimate, and not both.
    c2 = CaseInput("X", owed=0.0, total_due_filing=11329.20, filed_date="2026-01-01",
                   property_type="real", tax_breakdown=TB,
                   collector_balances={"GARLAND ISD": 6991.30, "CITY OF GARLAND": 4337.90})
    p2 = A.tax_payoff(c2)
    check("fetched balances REPLACE the estimate", p2["basis"] == "collectors_outside_act", p2["basis"])
    check("…and equal the collectors' sum, not sum+estimate", p2["amount"] == 11329, str(p2["amount"]))
    check("no branch can add the fallback to external",
          p2["amount"] < p1["amount"] + 11329)


def test_unchanged_where_it_should_be():
    print("\nno collateral movement")
    d = CaseInput("X", owed=19366.44, total_due_filing=19366.44, property_type="real")
    p = A.tax_payoff(d)
    check("a parcel with no external collectors is untouched",
          p["amount"] == 19366 and p["basis"] == "act_live_balance")
    check("…and stays VERIFIED", p["label"] == A.VERIFIED)
    # An all-ACT petition (Dallas) must not create external lines or change the number.
    dallas_tb = [{"entity": "DALLAS INDEPENDENT SCHOOL DISTRICT", "total": 8923.52},
                 {"entity": "CITY OF DALLAS", "total": 5889.98}]
    p2 = A.tax_payoff(CaseInput("X", owed=19366.44, total_due_filing=19366.44,
                                property_type="real", tax_breakdown=dallas_tb))
    check("an all-ACT petition does not inflate the payoff", p2["amount"] == 19366, str(p2["amount"]))


def test_the_grant_st_shape():
    print("\nthe money mechanism — newly-visible debt can kill a deal (TX-26-00774, real numbers)")
    base = dict(case_number="TX-26-00774", owed=6812.16, total_due_filing=7000,
                market_value=263790, living_area_sqft=1400, property_type="real",
                tax_breakdown=[{"entity": "RICHARDSON INDEPENDENT SCHOOL DISTRICT", "total": 14082.76},
                               {"entity": "CITY OF GARLAND", "total": 8154.64}])
    fetched = {"RICHARDSON ISD": 14082.76, "CITY OF GARLAND": 8154.64}
    acq = AcquisitionInputs(arv=95000, arv_label=A.VERIFIED, agreed_price=32000, lien_status="verified")
    r_old = A.analyze(CaseInput(**base), acq, None)
    r_new = A.analyze(CaseInput(**base, collector_balances=fetched), acq, None)
    check("old payoff was the ACT scalar alone ($6,812)", r_old["tax_payoff"]["amount"] == 6812)
    check("new payoff includes both collectors ($29,050)", r_new["tax_payoff"]["amount"] == 29050,
          str(r_new["tax_payoff"]["amount"]))
    check("the seller netted POSITIVE on the understated payoff",
          r_old["seller_net_sheet"]["seller_net"]["value"] > 0)
    check("…and nets NEGATIVE once the real debt is visible",
          r_new["seller_net_sheet"]["seller_net"]["value"] < 0)
    check("closability flips to False", r_new["seller_net_sheet"]["closable"] is False)
    check("verdict becomes NO-GO — the deal died on debt we could not previously see",
          r_new["decision"] == "NO-GO", r_new["decision"])


def run():
    print("=" * 78)
    print("§26 — VERIFIED COLLECTOR BALANCES IN THE PAYOFF TOTAL")
    print("=" * 78)
    test_cambridge_ground_truth()
    test_incomplete_is_a_labelled_floor()
    test_double_count_guard()
    test_unchanged_where_it_should_be()
    test_the_grant_st_shape()
    print("-" * 78)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
