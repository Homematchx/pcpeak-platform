#!/usr/bin/env python3
"""§25 — the GDS collector adapter (texaspayments.com), offline.

Parsing and the guards are pure functions tested against REAL captured page text; the network flow is
exercised separately by `collector_backfill.py` against the live portal.

WHAT MATTERS HERE
  · the account balance is the SUM ACROSS YEARS, not the one expanded detail block (3909 Cambridge:
    the block showed $4,086.97, the account owed $12,108.43 — reading the block understates by 3x)
  · a page we cannot confidently parse yields {} → `unavailable`, NEVER $0
  · a FETCHED $0 is a real, verified zero and must stay distinct from an absent balance
  · agency ids are resolved from the roster, never written here
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collectors_gds as G
import jurisdictions as J

_passed, _failed = 0, 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  → {detail}" if detail else ""))


# REAL captured text — 3909 Cambridge Dr, Garland ISD (agency 057909, account 0000089040).
CAMBRIDGE_GISD = (
    "Account Number:\n0000089040\nOwner Name:\nMELKA GEORGE F\n"
    "Address:\n3909 CAMBRIDGE DR GARLAND, TX 75043-0000\n"
    "CAD Number:\n26341500100280000\nMortgage Company:\nProperty Type:\nR\n"
    "Property Address:\n3909 CAMBRIDGE DR\nLegal Description:\nMEADOWCREEK PARK 9TH SEC\n"
    "Deferral :\nNo\nBankruptcy :\nNo\nQuarterly :\nNo\nLawsuit :\nYes\nPayment Plan :\nNo\n"
    "Show Detail\n\t\nYear\n\t\nCurrent Levy\n\t\nAmount Due\n"
    "\t2025\t$2,862.03\t$4,086.97\n\t2024\t$2,568.71\t$4,038.01\n\t2023\t$2,321.36\t$3,983.45\n"
    "\t2022\t$2,584.31\t$0.00\n\t2021\t$2,124.40\t$0.00\n\t2020\t$2,124.40\t$0.00\n Show All\n"
    "2025 - GARLAND INDEPENDENT SCHOOL DISTRICT\nTaxes Due:\n$2,862.03\n")

# REAL captured text — 413 W Carolyn Dr, Garland ISD: a genuine, verified ZERO.
CAROLYN_GISD = (
    "Account Number:\n0000135135\nOwner Name:\nMACIAS JUAN CARLOS ESTRADA &\n"
    "CAD Number:\n26485500040430000\nProperty Type:\nR\nLawsuit :\nNo\n"
    "Show Detail\n\t\nYear\n\t\nCurrent Levy\n\t\nAmount Due\n"
    "\t2025\t$2,145.00\t$0.00\n\t2024\t$2,050.00\t$0.00\n\t2023\t$1,980.00\t$0.00\n")


def test_sum_across_years():
    print("\nthe balance is the SUM across years, not the expanded block")
    d = G.parse_account_detail(CAMBRIDGE_GISD)
    check("account number parsed", d["account"] == "0000089040")
    check("CAD parsed (used for the identity guard)", d["cad"] == "26341500100280000")
    check("all six year rows parsed", len(d["years"]) == 6, str(len(d.get("years", []))))
    check("amount_due sums the years → $12,108.43", d["amount_due"] == 12108.43, str(d["amount_due"]))
    check("…and is NOT the single expanded block ($4,086.97)", d["amount_due"] != 4086.97)
    check("lawsuit flag read", d["lawsuit"] is True)
    check("owner captured", d["owner"].startswith("MELKA"))


def test_fetched_zero_is_real():
    print("\na FETCHED zero is a verified zero — distinct from an absent balance")
    d = G.parse_account_detail(CAROLYN_GISD)
    check("a genuinely paid account parses", bool(d))
    check("amount_due is 0.0, a real value", d["amount_due"] == 0.0)
    check("…and it is a float zero, not None", d["amount_due"] is not None)
    check("lawsuit=No is read as False", d["lawsuit"] is False)
    # The schema must treat this as VERIFIED, not unavailable.
    lines = J.collector_lines([{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 6991.30}],
                              act_balance=0.0, fetched={"GARLAND ISD": 0.0})
    g = [l for l in lines["collectors"] if l["collector"] == "GARLAND ISD"][0]
    check("a fetched $0 renders VERIFIED, not unavailable", g["label"] == J.VERIFIED, g["label"])
    check("…and the payoff is then COMPLETE",
          not J.payoff_completeness(lines)["unavailable_collectors"])


def test_fails_closed():
    print("\nfails closed — a page we cannot read yields nothing, never a number")
    for label, txt in [("empty page", ""), ("None", None), ("junk", "some unrelated page"),
                       ("no-match result", "0 No Matches\nAccount Number:\n"),
                       ("detail with no year rows", "Account Number:\n123\nCAD Number:\n456\n")]:
        check(f"{label} → {{}} (→ unavailable, never $0)", G.parse_account_detail(txt) == {})


def test_identity_guard_and_no_literals():
    print("\nguards")
    src = (Path(__file__).parent / "collectors_gds.py").read_text()
    check("the adapter contains NO hardcoded agency id",
          not re.search(r'["\']0\d{5}["\']', src))
    check("the adapter resolves agencies through the roster",
          "load_gds_roster" in src or "resolve_collector" in src)
    check("the identity guard compares returned CAD to requested CAD",
          'got.get("cad")' in src and "requested_cad" in src)
    # membership gate: a collector the petition did not name is never queried
    named = [c["collector"] for c in J.petition_collectors(
        [{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 1.0}])]
    check("petition membership yields ONLY the named collector", named == ["GARLAND ISD"])
    check("…and City of Garland is absent when the petition did not name it",
          "CITY OF GARLAND" not in named)
    # roster-driven agency resolution
    check("Garland ISD → 057909 from the roster",
          J.resolve_collector("GARLAND ISD")["agency"] == "057909")
    check("a non-gds collector is not routed to this adapter",
          J.resolve_collector("IRVING ISD")["platform"] != "gds")


def test_adapter_is_registered():
    print("\nregistry")
    check("gds is registered as an available adapter", "gds" in J.ADAPTERS, str(list(J.ADAPTERS)))
    check("a gds collector now reports reachable", J.resolve_collector("GARLAND ISD")["reachable"] is True)
    check("irving_act still has no adapter (honest)",
          J.resolve_collector("IRVING ISD")["reachable"] is False)


def run():
    print("=" * 78)
    print("§25 — GDS COLLECTOR ADAPTER")
    print("=" * 78)
    test_sum_across_years()
    test_fetched_zero_is_real()
    test_fails_closed()
    test_identity_guard_and_no_literals()
    test_adapter_is_registered()
    print("-" * 78)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
