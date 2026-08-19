#!/usr/bin/env python3
"""§34 — `act_units`: positive completeness, and the $0 blind spot that must stay UNKNOWN.

§33 made completeness conservative because `act_units` had no source. This gives it one: ACT's own
per-parcel jurisdiction report — an INDEPENDENT authority, not the petition.

THE SHARP EDGE THIS SUITE EXISTS FOR. ACT renders **no unit list at all** when a parcel's balance is
$0 (§17.5). The tempting reading — "ACT returned no extra units, so what we hold is complete" — is
absence-treated-as-a-value and would re-open the exact false-complete §33 closed. Confirmed live on
account 26485500040430000 (TX-26-00991): headers render, body reads "No taxes due.", zero rows.

The fixtures below are VERBATIM captures of the live report for both shapes, so this suite pins the
parser against the real page rather than against an idea of it. The first draft of the parser passed
an all-caps-line test and still returned `act_unit_list` for the $0 parcel, because the page footer
carries "DALLAS COUNTY TAX OFFICE" — a phantom unit that would have manufactured false completeness.

THREE OUTCOMES, NEVER TWO — and `no_unit_list_at_zero_balance` is kept distinct from `fetch_failed`
because one is a permanent property of the source and the other is retryable.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jurisdictions as J
import acquisition as A
from acquisition import CaseInput
from property_intel import parse_act_units

PASS = FAIL = 0
def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label} {extra}")


# ── VERBATIM live captures (text-extracted), trimmed to the report body ─────────────────────────
WITH_UNITS = """
Dallas County Web Site
Taxes Due Detail by Jurisdiction
Taxes Due as of Wed Aug 19 15:45:41 CDT 2026
Account No.: 26238500070260000
* Additional Collection Costs
Year
Jurisdiction
Base
Tax Due
Total
Due
2024
DALLAS COLLEGE
$280.00
$160.16
$440.16
DALLAS COUNTY
$1,100.00
$629.20
$1,729.20
PARKLAND HOSPITAL
$500.00
$286.00
$786.00
SCHOOL EQUALIZATION
$20.00
$11.44
$31.44
DALLAS COUNTY TAX OFFICE
"""

ZERO_BALANCE = """
Dallas County Web Site
Taxes Due Detail by Jurisdiction
Taxes Due as of Wed Aug 19 15:46:02 CDT 2026
Account No.: 26485500040430000
* Additional Collection Costs
Year
Jurisdiction
Base
Tax Due
Total
Due
No
taxes due.
DALLAS COUNTY TAX OFFICE
"""


def test_parser_three_outcomes():
    print("\nthe parser has THREE outcomes, and the $0 one is not a list")
    units, reason = parse_act_units(WITH_UNITS)
    check("a rendered unit list parses", reason == "act_unit_list", reason)
    check("…to exactly the units §23 read by hand, independently",
          units == ["DALLAS COLLEGE", "DALLAS COUNTY", "PARKLAND HOSPITAL", "SCHOOL EQUALIZATION"],
          str(units))
    check("…and page chrome is NOT mistaken for a jurisdiction",
          "DALLAS COUNTY TAX OFFICE" not in (units or []))

    units0, reason0 = parse_act_units(ZERO_BALANCE)
    check("a $0 parcel yields NO units", units0 is None, str(units0))
    check("…with the reason naming it, not a generic failure",
          reason0 == "no_unit_list_at_zero_balance", reason0)
    check("…and the footer alone never constitutes a unit list",
          reason0 != "act_unit_list")

    check("an unrecognised page is its own (retryable) outcome",
          parse_act_units("<html>maintenance</html>") == (None, "unrecognized_page"))
    check("empty input is unrecognised, never an empty list",
          parse_act_units("") == (None, "unrecognized_page"))


def test_invariant_1_blank_list_never_completes():
    print("\nINVARIANT 1 — a blank list at $0 must never read COMPLETE")
    tb = [{"entity": "DALLAS COUNTY", "total": 100.0}]
    units, reason = parse_act_units(ZERO_BALANCE)
    c = CaseInput("TX-SYNTH-ZEROLIST", owed=0.0, total_due_filing=11679.20,
                  property_type="real", tax_breakdown=tb, act_units=units)
    comp = A.tax_payoff_lines(c)["completeness"]
    check("completeness is UNKNOWN, not True", comp["complete"] is None, str(comp["complete"]))
    check("…membership_verified is False", comp["membership_verified"] is False)
    check("…and payoff_is_complete refuses it", J.payoff_is_complete(comp) is False)
    check("…so the payoff label still says estimated", A.tax_payoff(c)["label"] == A.ESTIMATED)

    # …while a REAL list on the same case does establish coverage.
    real, _ = parse_act_units(WITH_UNITS)
    c2 = CaseInput("TX-SYNTH-REALLIST", owed=100.0, total_due_filing=11679.20,
                   property_type="real", tax_breakdown=tb, act_units=real)
    comp2 = A.tax_payoff_lines(c2)["completeness"]
    check("a REAL unit list DOES prove completeness", comp2["complete"] is True, str(comp2))
    check("…and the payoff may finally say verified", A.tax_payoff(c2)["label"] == A.VERIFIED)


def test_invariant_2_empty_list_is_not_an_empty_set():
    print("\nINVARIANT 2 — [] is UNKNOWN COVERAGE, never a known-empty SET")
    # This is the safety that used to live implicitly in `{…} or None`. A refactor that dropped the
    # `or None` would turn [] into an empty set, `act_units_known` would flip True, and a parcel
    # nobody established coverage for could read COMPLETE. Pinned as behaviour, not as an idiom.
    check("None → None (unknown)", J.normalize_act_units(None) is None)
    check("[] → None (unknown), NOT set()", J.normalize_act_units([]) is None)
    check("a list of empties → None", J.normalize_act_units(["", None]) is None)
    check("a real list → a canonical set",
          J.normalize_act_units(["DALLAS COUNTY"]) == {"DALLAS COUNTY"})

    tb = [{"entity": "DALLAS COUNTY", "total": 100.0}]
    for label, au in (("None", None), ("[]", [])):
        c = CaseInput(f"TX-SYNTH-{label}", owed=100.0, property_type="real",
                      tax_breakdown=tb, act_units=au)
        lines = J.collector_lines(c.tax_breakdown, act_balance=c.owed, act_units=au)
        comp = J.payoff_completeness(lines)
        check(f"act_units={label} ⇒ act_units_known is False", lines["act_units_known"] is False)
        check(f"act_units={label} ⇒ complete is None, never True", comp["complete"] is None)

    # The teeth: an empty SET is the shape the deleted safety would produce, and it must be
    # unreachable — `is not set()` would be identity against a fresh object and always true, so this
    # asserts the TYPE instead. Under the defect this returns set(); under the fix, None.
    check("normalize can never return a set for an empty input",
          not isinstance(J.normalize_act_units([]), set))
    check("…and the defect's own value would have read as KNOWN coverage",
          J.collector_lines([{"entity": "DALLAS COUNTY"}], act_balance=1.0,
                            act_units=[])["act_units_known"] is False)


def test_zero_balance_band_is_untouched():
    print("\nthe 11 band-flip cases are NOT reachable here — by construction (§34.3)")
    # act_units cannot resolve a $0 parcel, so §33's UNCONFIRMED verdict on those must stand. This
    # pins that the increment did not quietly acquire a band path it was scoped NOT to have.
    tb = [{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 6991.30}]
    units, _ = parse_act_units(ZERO_BALANCE)
    c = CaseInput("TX-SYNTH-BANDFLIP", owed=0.0, total_due_filing=11679.20,
                  property_type="real", tax_breakdown=tb, act_units=units,
                  collector_balances={"GARLAND ISD": 0.0})
    comp = A.tax_payoff_lines(c)["completeness"]
    check("every named collector retrieved AND ACT $0…", not comp["unavailable_collectors"])
    check("…still does NOT prove completeness", J.payoff_is_complete(comp) is False)
    check("…because the $0 page states no coverage at all",
          parse_act_units(ZERO_BALANCE)[1] == "no_unit_list_at_zero_balance")


if __name__ == "__main__":
    test_parser_three_outcomes()
    test_invariant_1_blank_list_never_completes()
    test_invariant_2_empty_list_is_not_an_empty_set()
    test_zero_balance_band_is_untouched()
    print(f"\n{PASS}/{PASS+FAIL} checks passed")
    sys.exit(1 if FAIL else 0)
