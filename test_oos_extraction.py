#!/usr/bin/env python3
"""Tests for discover.operative_oos_date — the deterministic operative Order-of-Sale date. No network.

A sale can be pulled and the Order of Sale RE-ISSUED. TX-23-00569: OOS 2026-04-20, sale pulled
2026-05-12, then re-issued 2026-07-24 under the combined judgment. The Claude extraction returns one
orderOfSaleDate and reasonably picked the earlier case-specific order (the 7/24 entry reads as
"judgment copies for [the combined case]"), so oos_date came back 2026-04-20. We now DERIVE the
operative date from the captured docket events as the LATEST issuance — deterministic, so it STICKS
through every re-scrape (no hand-set value to clobber).

Pins:
  * multiple issuances → the LATEST wins (TX-23-00569: 04-20 + 07-24 → 07-24);
  * ISSUANCE events count; ABSTRACT / REQUEST / "authorized" / NOTICE mentions do NOT;
  * a single issuance returns that one date; no issuance returns None (never a fabricated date);
  * malformed / non-ISO dates are ignored (can't be ordered);
  * the distinct-issuance list is returned so a re-issue can be surfaced.

Run: python3 test_oos_extraction.py   (exit 0 = all green)
"""
import sys

sys.path.insert(0, ".")
import discover

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)


def ev(date, desc):
    return {"date": date, "event": desc, "type": "outcome"}


# TX-23-00569's REAL docket events (from prod), the case that motivated this fix.
TX_00569 = [
    ev("2026-01-07", "Judgment Non-Jury entered; total due $148,369.52; order of sale authorized"),
    ev("2026-01-07", "Notice of Judgment mailed; Abstract of Judgment and Order of Sale - combined with TX-00-31060"),
    ev("2026-04-20", "Order of Sale issued"),
    ev("2026-04-20", "Issue Order of Sale; Order of Sale (TX-23-00569 only)"),
    ev("2026-05-12", "Property PULLED from sheriff sale"),
    ev("2026-07-24", "Issue Order of Sale - judgment copies for TX-00-31060-T-C"),
]


def run():
    op, alld = discover.operative_oos_date(TX_00569)
    check("TX-23-00569: operative OOS = the LATEST issuance (2026-07-24, not the 04-20 order)",
          op == "2026-07-24")
    check("TX-23-00569: both issuance dates surfaced (a re-issue)", alld == ["2026-04-20", "2026-07-24"])
    check("TX-23-00569: the 01-07 'order of sale AUTHORIZED' (a judgment line) is NOT an issuance",
          "2026-01-07" not in alld)
    check("TX-23-00569: the 01-07 'Abstract of Judgment and Order of Sale' is NOT an issuance",
          "2026-01-07" not in alld)

    # single issuance → that date
    op, alld = discover.operative_oos_date([ev("2026-05-15", "Order of Sale issued")])
    check("single issuance → that date", op == "2026-05-15" and alld == ["2026-05-15"])

    # no issuance → None (never a fabricated date)
    op, alld = discover.operative_oos_date([
        ev("2026-01-07", "Judgment Non-Jury entered"),
        ev("2026-02-01", "Request for Abstract of Judgment and Order of Sale"),
        ev("2026-03-01", "Notice of Judgment mailed"),
    ])
    check("no issuance event → None (a request/abstract/notice is not an issuance)",
          op is None and alld == [])

    # 'REQUEST FOR ... ORDER OF SALE' must not count as an issuance
    op, _ = discover.operative_oos_date([ev("2026-04-01", "REQUEST FOR ABSTRACT OF JUDGMENT AND ORDER OF SALE")])
    check("a REQUEST for an order of sale is not an issuance", op is None)

    # a later ABSTRACT must not override an earlier real issuance
    op, alld = discover.operative_oos_date([
        ev("2026-04-20", "Order of Sale issued"),
        ev("2026-06-01", "Abstract of Judgment and Order of Sale filed"),
    ])
    check("a later ABSTRACT does not override the real issuance (stays 04-20)",
          op == "2026-04-20" and alld == ["2026-04-20"])

    # malformed / missing dates are ignored (can't order by them)
    op, alld = discover.operative_oos_date([
        ev("", "Issue Order of Sale"),
        ev("2026/07/24", "Issue Order of Sale"),   # wrong separators
        ev("2026-07-24", "Issue Order of Sale"),
    ])
    check("only the valid ISO issuance date is used (malformed dates ignored)",
          op == "2026-07-24" and alld == ["2026-07-24"])

    # empty / None input
    check("no events → None", discover.operative_oos_date([]) == (None, []))
    check("None events → None", discover.operative_oos_date(None) == (None, []))

    # accepts the stored-event shape too (event_date/description keys), not just extraction shape
    op, _ = discover.operative_oos_date([
        {"event_date": "2026-07-24", "description": "Issue Order of Sale - re-drive"}])
    check("works on stored docket_events shape (event_date/description)", op == "2026-07-24")

    print("-" * 60)
    total, passed = len(_res), sum(_res)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
