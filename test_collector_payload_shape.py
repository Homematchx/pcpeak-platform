#!/usr/bin/env python3
"""§19 family — COLLECTOR PAYLOAD SHAPE: is this value a collector, or a diagnostic?

THE DEFECT THIS PINS (a real crash, on a real run, 2026-08-19):

    total = sum(v["amount"] for v in got.values())
    TypeError: list indices must be integers or slices, not str

An adapter returns `{collector: {amount, account, …}}` — PLUS, conditionally, two keys whose values
are LISTS rather than collector entries:

    _rejected            identity-guard discards (returned CAD ≠ requested CAD)
    _portal_unavailable  infrastructure fault; the adapter stops rather than hammer a refusing portal

Both are deliberately stored alongside the balances so a fault survives into the record. But every
consumer that walks `.values()` expecting a collector dict breaks on them. `collector_backfill` did
exactly that and died the first time a portal fault occurred — on the 96-case run, after the 80-case
run an hour earlier had been clean, because no fault had happened yet.

WHY THIS IS THE §19 QUESTION AND NOT A TYPO. The summer assumed a SHAPE it never checked: that every
value in the mapping is the same kind of thing. It is not. The engine and the frontend both already
guarded this (`isinstance(v, dict)` / `typeof v !== "object"`); only the reporting layer assumed.
That is the "one producer, many readers, and one reader assumed" pattern.

⚠ THE DANGEROUS FIX would have been to make line 73 tolerant — e.g. `v.get("amount", 0)` — which
turns a portal fault into a silent **$0 contribution** to a payoff total. That is the assumed-zero
defect this whole arc exists to kill. The correct fix SPLITS BY SHAPE FIRST and never lets a
diagnostic reach the arithmetic.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jurisdictions as J

PASS = FAIL = 0
def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label} {extra}")


# The exact payload shape that crashed the run, verbatim from the live diagnostic.
CRASHER = {"_portal_unavailable": ["057120: portal returned https://www.texaspayments.com/Error/WrongRequest"]}
MIXED = {
    "GARLAND ISD": {"amount": 12108.43, "account": "0000056331", "cad": "26238500070260000"},
    "CITY OF GARLAND": {"amount": 0.0, "account": "0000076491", "cad": "26238500070260000"},
    "_rejected": [{"collector": "RICHARDSON ISD", "requested_cad": "A", "returned_cad": "B"}],
    "_portal_unavailable": ["057916: portal returned .../Error/WrongRequest"],
}


def test_the_exact_crash_no_longer_crashes():
    print("\nthe payload that crashed the real run")
    try:
        amounts = J.collector_amounts(CRASHER)
        crashed = False
    except Exception as e:
        amounts, crashed = None, True
        print(f"       raised {type(e).__name__}: {e}")
    check("collector_amounts does not raise on a sentinel-only payload", not crashed)
    check("…and yields NO collectors", amounts == {}, str(amounts))
    check("…so the total is 0 collectors, not a crash and not a fabricated $0 line",
          sum((amounts or {}).values()) == 0)
    # The defect, reproduced: prove the old expression really does die on this input.
    try:
        sum(v["amount"] for v in CRASHER.values())
        old_crashed = False
    except TypeError:
        old_crashed = True
    check("[teeth] the ORIGINAL expression still raises TypeError on this payload", old_crashed)


def test_sentinels_never_enter_arithmetic():
    print("\nsentinels are diagnostics, never collectors")
    amounts = J.collector_amounts(MIXED)
    check("only real collectors are returned",
          set(amounts) == {"GARLAND ISD", "CITY OF GARLAND"}, str(set(amounts)))
    check("_rejected is excluded", "_rejected" not in amounts)
    check("_portal_unavailable is excluded", "_portal_unavailable" not in amounts)
    check("the total is the real sum only", sum(amounts.values()) == 12108.43, str(sum(amounts.values())))
    check("a fetched $0.00 SURVIVES — it is a verified fact, not an absence (§29)",
          amounts["CITY OF GARLAND"] == 0.0)
    # The count that was also wrong, silently, even when it did not crash.
    check("[teeth] len(payload) over-counts collectors; collector_amounts does not",
          len(MIXED) == 4 and len(amounts) == 2)


def test_sentinels_are_surfaced_not_dropped():
    print("\n…and they are SURFACED, not swallowed")
    s = J.collector_sentinels(MIXED)
    check("both sentinels are reported", set(s) == {"_rejected", "_portal_unavailable"}, str(set(s)))
    check("the portal fault text is preserved verbatim",
          "Error/WrongRequest" in str(s["_portal_unavailable"]))
    check("an empty sentinel list is not reported as a fault",
          J.collector_sentinels({"_rejected": []}) == {})
    check("a clean payload has no sentinels",
          J.collector_sentinels({"GARLAND ISD": {"amount": 1.0}}) == {})


def test_the_tolerant_fix_would_have_been_worse():
    print("\nthe fix NOT taken: tolerating the sentinel would fabricate $0")
    # `v.get("amount", 0)` over every value turns a portal fault into a $0 payoff contribution —
    # silently short, confidently wrong. Pin that the real implementation does not do this.
    amounts = J.collector_amounts(CRASHER)
    check("a portal fault contributes NO line at all (not a $0 line)", amounts == {})
    check("…so a payoff built from it has nothing to sum, and the collector stays UNAVAILABLE",
          len(amounts) == 0)
    # And the engine renders that absence as `unavailable`, which is INDETERMINATE, never $0.
    lines = J.collector_lines([{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 6991.30}],
                              act_balance=0.0, fetched=amounts)
    gisd = [l for l in lines["collectors"] if l["collector"] == "GARLAND ISD"][0]
    check("the engine renders it `unavailable` with NO amount",
          gisd["label"] == J.UNAVAILABLE and gisd["amount"] is None, str(gisd))
    check("…and completeness reports it as a missing collector",
          J.payoff_completeness(lines)["unavailable_collectors"] == ["GARLAND ISD"])


def test_malformed_rows_are_dropped_not_guessed():
    print("\nmalformed rows are dropped, never coerced")
    weird = {"A": {"amount": None}, "B": {"amount": "12.00"}, "C": "not-a-dict",
             "D": {"no_amount": 1}, "E": {"amount": 5.5}}
    amounts = J.collector_amounts(weird)
    check("only the numerically-valid row survives", amounts == {"E": 5.5}, str(amounts))
    check("a string amount is NOT coerced to a number", "B" not in amounts)
    check("a None amount is not read as 0", "A" not in amounts)


if __name__ == "__main__":
    test_the_exact_crash_no_longer_crashes()
    test_sentinels_never_enter_arithmetic()
    test_sentinels_are_surfaced_not_dropped()
    test_the_tolerant_fix_would_have_been_worse()
    test_malformed_rows_are_dropped_not_guessed()
    print(f"\n{PASS}/{PASS+FAIL} checks passed")
    sys.exit(1 if FAIL else 0)
