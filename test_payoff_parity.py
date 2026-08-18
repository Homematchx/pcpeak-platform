#!/usr/bin/env python3
"""§28 — PAYOFF PARITY: the client and the engine must never state different numbers.

WHY THIS EXISTS. The tax payoff is computed TWICE — once in `acquisition.tax_payoff()` for the
Acquisition tab and the whole decision stack, once in `calcPayoff()` in the browser for the Financials
tab. It has drifted **twice**:

  · before `f8188d5` — the client fee-loaded the payoff and passed a null live balance;
  · at §26 — the engine gained collector balances and the client did not, so 31 cases showed
    **$307,863 less** on one tab than the other. The platform quoted two payoffs for one parcel, which
    is worse than the uniform understatement it replaced: a rep cannot tell which is real.

Both were "did you update every consumer of this value?" failures — the §19 set-integrity family
pointed at DISPLAY consumers instead of data. A comment saying "the two must agree" sat above
`calcPayoff` the whole time and stopped nothing.

THIS TEST RUNS BOTH IMPLEMENTATIONS OVER THE SAME CASE MATRIX AND FAILS ON ANY DIVERGENCE — amount or
label. Change one, change the other, or this stops you.
"""
import asyncio
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import acquisition as A
from acquisition import CaseInput
from browser_env import chrome_path

ROOT = Path(__file__).parent
HTML = ROOT / "frontend" / "index.html"
_passed, _failed = 0, 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  → {detail}" if detail else ""))


def _fn(name, src):
    i = src.index(f"function {name}(")
    d, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            d += 1
            started = True
        elif src[j] == "}":
            d -= 1
            if started and d == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError(name)


GISD = "GARLAND INDEPENDENT SCHOOL DISTRICT"
COG = "CITY OF GARLAND"
RISD = "RICHARDSON INDEPENDENT SCHOOL DISTRICT"

# Every branch of the payoff rule, plus the real cases that exposed the drift.
MATRIX = [
    ("3909 Cambridge — ACT + both collectors (ground truth $25,750)",
     dict(owed=5974.81, total_due_filing=20000, tax_breakdown=[{"entity": GISD, "total": 12108.43},
                                                               {"entity": COG, "total": 7666.63}],
          collector_balances={"GARLAND ISD": 12108.43, "CITY OF GARLAND": 7666.63})),
    ("TX-26-00774 — ACT + Richardson + City of Garland",
     dict(owed=6812.16, total_due_filing=7000, tax_breakdown=[{"entity": RISD, "total": 14082.76},
                                                              {"entity": COG, "total": 8154.64}],
          collector_balances={"RICHARDSON ISD": 14082.76, "CITY OF GARLAND": 8154.64})),
    ("incomplete — one collector unretrieved (must be an ESTIMATED floor)",
     dict(owed=5974.81, total_due_filing=20000, tax_breakdown=[{"entity": GISD, "total": 12108.43},
                                                               {"entity": COG, "total": 7666.63}],
          collector_balances={"GARLAND ISD": 12108.43})),
    ("ACT $0, collectors fetched — real figure, not the filing estimate",
     dict(owed=0.0, total_due_filing=11329.20, filed_date="2026-01-01",
          tax_breakdown=[{"entity": GISD, "total": 6991.30}, {"entity": COG, "total": 4337.90}],
          collector_balances={"GARLAND ISD": 6991.30, "CITY OF GARLAND": 4337.90})),
    ("ACT $0, nothing fetched — fallback ONLY (double-count guard)",
     dict(owed=0.0, total_due_filing=11329.20, filed_date="2026-01-01",
          tax_breakdown=[{"entity": GISD, "total": 6991.30}, {"entity": COG, "total": 4337.90}])),
    ("plain Dallas parcel — no external collectors at all",
     dict(owed=19366.44, total_due_filing=19366.44,
          tax_breakdown=[{"entity": "DALLAS INDEPENDENT SCHOOL DISTRICT", "total": 8923.52},
                         {"entity": "CITY OF DALLAS", "total": 5889.98}])),
    ("no live balance, no breakdown, no filing amount",
     dict(owed=None, total_due_filing=None)),
    ("no live balance, filing amount only",
     dict(owed=None, total_due_filing=9000.0, filed_date="2025-06-01")),
    ("verified $0 everywhere — a fetched zero, not an assumed one",
     dict(owed=0.0, total_due_filing=0, tax_breakdown=[{"entity": GISD, "total": 100.0}],
          collector_balances={"GARLAND ISD": 0.0})),
    ("multi-tract petition rows for ONE collector (must not fragment)",
     dict(owed=1000.0, total_due_filing=5000,
          tax_breakdown=[{"entity": GISD + " - TRACT 1 (2022)", "total": 500.0},
                         {"entity": GISD + " - TRACT 2 (2021)", "total": 700.0}],
          collector_balances={"GARLAND ISD": 1200.0})),
]


async def main():
    from playwright.async_api import async_playwright
    src = HTML.read_text()
    # the collector list the client uses — extracted from the artifact, never re-typed here, so the
    # test cannot pass against a copy that has drifted from what ships
    i = src.index("var ACT_COLLECTED_UI")
    act_const = src[i:src.index("];", i) + 2]
    harness = "\n".join([act_const, _fn("canonCollector", src), _fn("collectorsNamedInSuit", src),
                         _fn("calcPayoff", src),
                         "window.__pay = function(ex, bal, cb, un){ return calcPayoff(ex, bal, cb, un); };",
                         "window.__named = function(c){ return collectorsNamedInSuit(c); };"])
    # Freeze "now" so the fallback's month-accrual matches the Python side exactly.
    as_of = datetime.date(2026, 8, 17)
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=chrome_path())
        pg = await b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto("about:blank")
        await pg.add_init_script(
            "Date = class extends Date { constructor(...a){ super(...(a.length?a:['2026-08-17T12:00:00'])); } };")
        await pg.add_script_tag(content=harness)

        for label, kw in MATRIX:
            case = CaseInput("X", property_type="real", **kw)
            py = A.tax_payoff(case, as_of)

            tb = kw.get("tax_breakdown") or []
            named = await pg.evaluate("c => window.__named(c)", {"tax_breakdown": tb})
            cb = kw.get("collector_balances") or {}
            unavail = [n for n in named if n not in cb]
            ex = {"totalDueAtFiling": kw.get("total_due_filing") or 0,
                  "filedDate": kw.get("filed_date"), "judgmentDate": None}
            js = await pg.evaluate("a => window.__pay(a[0], a[1], a[2], a[3])",
                                   [ex, kw.get("owed"), cb, unavail])

            check(f"AMOUNT parity — {label}", py["amount"] == js["taxPayoff"],
                  f'engine {py["amount"]!r} vs client {js["taxPayoff"]!r}')
            check(f"LABEL parity  — {label}", py["label"] == js["payoffLabel"],
                  f'engine {py["label"]} vs client {js["payoffLabel"]}')

        # The client must AGREE about which collectors are external, or parity is luck.
        named = await pg.evaluate("c => window.__named(c)",
                                  {"tax_breakdown": [{"entity": GISD}, {"entity": COG},
                                                     {"entity": "DALLAS COUNTY"},
                                                     {"entity": "PARKLAND HOSPITAL DISTRICT"}]})
        import jurisdictions as J
        py_ext = sorted(c["collector"] for c in J.petition_collectors(
            [{"entity": GISD}, {"entity": COG}, {"entity": "DALLAS COUNTY"},
             {"entity": "PARKLAND HOSPITAL DISTRICT"}])
            if J.resolve_collector(c["collector"])["scope"] == "external")
        check("client and engine agree on WHICH collectors are external",
              sorted(named) == py_ext, f"client={sorted(named)} engine={py_ext}")
        check("zero pageerror", not errs, "; ".join(errs))
        await b.close()

    print("-" * 78)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    print("=" * 78)
    print("§28 — CLIENT / ENGINE PAYOFF PARITY")
    print("=" * 78)
    sys.exit(0 if asyncio.run(main()) else 1)
