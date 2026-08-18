#!/usr/bin/env python3
"""§29 — an UNKNOWN payoff must read "unknown" EVERYWHERE, never $0.00.

WHY A DEDICATED SUITE. The assumed-zero bug appeared FOUR times inside a single gate (§28):

  · the client returned `$0 / estimated` where the engine said `unavailable`;
  · the engine lost a VERIFIED $0 by testing truthiness on the external sum;
  · `null * rate` is `0` in JS, so attorney fees became $0;
  · `null + 0` is `0`, so total-to-clear, minimum offer and suggested offer all became $0.00.

That is not carelessness, it is structural: **the bug reappears wherever a payoff can be unknown and
arithmetic touches it.** JS coerces null to 0 silently, so every derived field is a fresh opportunity,
and spot-checking real cases cannot catch it — real cases have data. Only a NO-DATA case exposes it.

THE INVARIANT: with nothing to compute from, every money field is null and renders "—". A REAL zero
(fetched, verified) must still render "$0.00" — losing that distinction is the same bug inverted.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import acquisition as A
from acquisition import CaseInput
from browser_env import chrome_path

ROOT = Path(__file__).parent
HTML = ROOT / "frontend" / "index.html"
MONEY_FIELDS = ["taxPayoff", "attyFees", "totalToClear", "minOffer", "suggestedOffer"]
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


async def main():
    from playwright.async_api import async_playwright
    src = HTML.read_text()
    harness = "\n".join([_fn("fmtC", src), _fn("calcPayoff", src),
                         "window.__pay=(e,b,c,u)=>calcPayoff(e,b,c,u);",
                         "window.__fmt=v=>fmtC(v);"])
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=chrome_path())
        pg = await b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto("about:blank")
        await pg.add_script_tag(content=harness)

        # ── THE NO-DATA CASE: no live balance, nothing fetched, no filing amount ────────────────
        print("\nno data at all — the only shape that exposes this")
        empty = {"totalDueAtFiling": 0, "filedDate": None, "judgmentDate": None}
        po = await pg.evaluate("a => window.__pay(a,null,{},[])", empty)
        check("label is 'unavailable'", po["payoffLabel"] == "unavailable", po["payoffLabel"])
        for f in MONEY_FIELDS:
            check(f"{f} is null (not 0)", po[f] is None, repr(po[f]))
        rendered = {f: await pg.evaluate("v => window.__fmt(v)", po[f]) for f in MONEY_FIELDS}
        for f, r in rendered.items():
            check(f"{f} RENDERS as '—', never a dollar figure", r == "—", repr(r))
        check("no field renders $0.00", "$0.00" not in rendered.values(), str(rendered))
        # the engine must agree
        py = A.tax_payoff(CaseInput("X", owed=None, total_due_filing=None))
        check("engine agrees: amount None", py["amount"] is None, repr(py["amount"]))
        check("engine agrees: label unavailable", py["label"] == A.UNAVAILABLE, py["label"])

        # ── THE INVERSE: a REAL zero must still read $0.00 ──────────────────────────────────────
        print("\na FETCHED zero is a fact and must NOT be hidden as unknown")
        z = await pg.evaluate("a => window.__pay(a,0.0,{'GARLAND ISD':0.0},[])",
                              {"totalDueAtFiling": 5000, "filedDate": "2026-01-01", "judgmentDate": None})
        check("a verified $0 payoff is 0, not null", z["taxPayoff"] == 0, repr(z["taxPayoff"]))
        check("…labelled verified, not unavailable", z["payoffLabel"] == "verified", z["payoffLabel"])
        check("…and RENDERS as '$0.00'", await pg.evaluate("v => window.__fmt(v)", 0) == "$0.00")
        pyz = A.tax_payoff(CaseInput("X", owed=0.0, total_due_filing=5000,
                                     tax_breakdown=[{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT"}],
                                     collector_balances={"GARLAND ISD": 0.0}))
        check("engine agrees a fetched zero is $0 verified",
              pyz["amount"] == 0 and pyz["label"] == A.VERIFIED, f'{pyz["amount"]} {pyz["label"]}')

        # ── PARTIAL DATA still produces numbers (the fix must not over-blank) ───────────────────
        print("\npartial data must still compute — the fix must not blank real figures")
        pd = await pg.evaluate("a => window.__pay(a,5000.0,{},[])",
                               {"totalDueAtFiling": 0, "filedDate": None, "judgmentDate": None})
        check("a live balance alone still yields a payoff", pd["taxPayoff"] == 5000)
        for f in ("attyFees", "totalToClear", "minOffer", "suggestedOffer"):
            check(f"{f} still computed from a known payoff", isinstance(pd[f], (int, float)))

        # ── SOURCE GUARD: a future money field cannot bypass fmtC ───────────────────────────────
        print("\nsource guard — every money field must render through fmtC")
        import re
        offenders = []
        for f in MONEY_FIELDS:
            for m in re.finditer(r"po\." + f + r"\b", src):
                seg = src[max(0, m.start() - 60):m.start()]
                if "fmtC(" not in seg.split("${")[-1]:
                    offenders.append(f"{f} @{src[:m.start()].count(chr(10))+1}")
        check("no money field is interpolated without fmtC()", not offenders, "; ".join(offenders))
        check("fmtC(null) is '—'", await pg.evaluate("() => window.__fmt(null)") == "—")
        check("fmtC(undefined) is '—'", await pg.evaluate("() => window.__fmt(undefined)") == "—")
        check("zero pageerror", not errs, "; ".join(errs))
        await b.close()

    print("-" * 78)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    print("=" * 78)
    print("§29 — UNKNOWN PAYOFF RENDERS UNKNOWN, NEVER $0.00")
    print("=" * 78)
    sys.exit(0 if asyncio.run(main()) else 1)
