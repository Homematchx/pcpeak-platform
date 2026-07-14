#!/usr/bin/env python3
"""Test discover.partition_page — the page-filter counting that makes the Found total reconcile.

Pure, no portal. The 2026-07-14 finding: the open-only filter silently dropped CLOSED rows
without counting them (Found 110, Processed 0, Skipped 2 → 108 unaccounted). partition_page now
counts every drop; this proves closed + business + kept == total on every branch.

Run: python3 test_discover_filter.py   (exit 0 = all green)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import discover as D

_results = []
def check(name, cond):
    _results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)

ROWS = [
    {"caseNumber": "TX-23-00401", "status": "OPEN",   "partyName": "SMITH, JOHN"},
    {"caseNumber": "TX-23-00402", "status": "CLOSED", "partyName": "DOE, JANE"},
    {"caseNumber": "TX-23-00403", "status": "CLOSED", "partyName": "ACME LLC"},
    {"caseNumber": "TX-23-00404", "status": "OPEN",   "partyName": "ACME PROPERTIES INC"},
    {"caseNumber": "TX-23-00405", "status": "",       "partyName": "JONES, PEARLY"},  # blank = open
]


def run():
    # DEFAULT SEMANTICS (design principle): discovery INCLUDES closed cases by default —
    # narrowing to open-only is a deliberate opt-in. A regression here re-blinds the moat.
    check("Discoverer() default open_only is False (closed INCLUDED by default)",
          D.Discoverer().open_only is False)

    # open + individuals (the trigger's narrow mode)
    p = D.partition_page(ROWS, open_only=True, skip_biz=True)
    check("open+indiv: 2 CLOSED counted", p["closed"] == 2)
    check("open+indiv: 1 business counted", p["business"] == 1)          # 404 (403 already closed)
    check("open+indiv: 2 kept (open individuals, incl blank-status)", len(p["targets"]) == 2)
    check("open+indiv: blank status treated as open (405 kept)",
          any(r["caseNumber"] == "TX-23-00405" for r in p["targets"]))
    check("open+indiv: RECONCILES closed+business+kept == total",
          p["closed"] + p["business"] + len(p["targets"]) == len(ROWS))

    # include-closed (open_only=False): nothing dropped as closed; businesses still counted
    p2 = D.partition_page(ROWS, open_only=False, skip_biz=True)
    check("include-closed: 0 closed", p2["closed"] == 0)
    check("include-closed: 2 businesses counted (ACME LLC + ACME PROPERTIES INC)", p2["business"] == 2)
    check("include-closed: RECONCILES", p2["closed"] + p2["business"] + len(p2["targets"]) == len(ROWS))

    # no filters at all → everything kept, nothing counted as dropped
    p3 = D.partition_page(ROWS, open_only=False, skip_biz=False)
    check("no filters: all kept, 0 dropped",
          len(p3["targets"]) == len(ROWS) and p3["closed"] == 0 and p3["business"] == 0)

    # name-path mode (open filter only, biz handled per-case) → biz NOT counted here
    p4 = D.partition_page(ROWS, open_only=True, skip_biz=False)
    check("open-only (name path): 2 closed, 0 business, 3 kept",
          p4["closed"] == 2 and p4["business"] == 0 and len(p4["targets"]) == 3)

    # empty page
    p5 = D.partition_page([], True, True)
    check("empty page: all zeros", p5["targets"] == [] and p5["closed"] == 0 and p5["business"] == 0)

    print("-" * 56)
    total, passed = len(_results), sum(_results)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
