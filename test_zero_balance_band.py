#!/usr/bin/env python3
"""§17.4/§17.5 — a $0 collector balance must never read as PAID when it contradicts an active
collection posture.

THE DEFECT: `balanceBand()` mapped `b <= 0` to "zero" (paid). That inference is sound only where the
collector we read bills EVERY taxing unit on the parcel. Where an ISD collects separately, ACT's $0
means "nothing owed HERE", and a live foreclosure silently left amount-owed triage — the worst failure
shape, because a rep never sees it. Proven on TX-26-01455 (ACT "No taxes due" vs Garland ISD $4,896.59
with `Lawsuit: Yes`).

THE DISCRIMINATOR IS THE CONTRADICTION, NOT THE ADDRESS — pinned below with a Dallas case that must
flag on identical facts, and a Garland case that must NOT flag because its docket shows a dismissal.

Runs the REAL functions out of frontend/index.html in Chromium — no reimplementation.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
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


def _fn(name):
    """Extract one top-level function from the served artifact, verbatim."""
    src = HTML.read_text()
    i = src.index(f"function {name}(")
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError(name)


# Cases are shaped like the /api/cases SKELETON (property_intel dropped; the promoted
# current_tax_balance column carries the figure) — the exact objects renderList sees.
CASES = [
    # ── THE PROVEN CASE ──────────────────────────────────────────────────────────────────────────
    dict(id="TX-26-01455", case_number="TX-26-01455", current_tax_balance=0.0,
         total_due_filing=11306.35, case_track="active", case_status="OPEN",
         property_address="5221 Robin Road, Garland, TX 75043", property_type="real"),
    # ── SAME CONTRADICTION, DALLAS ADDRESS: geography must not be the discriminator ───────────────
    dict(id="dallas-contra", case_number="TX-26-00879", current_tax_balance=0.0,
         total_due_filing=19569.63, case_track="active", case_status="OPEN",
         property_address="1234 Anywhere St, Dallas, TX 75216", property_type="real"),
    # ── A GARLAND CASE THAT MUST NOT FLAG: the docket dismissed it ────────────────────────────────
    dict(id="garland-dismissed", case_number="TX-24-00067", current_tax_balance=0.0,
         total_due_filing=7377.80, case_track="dismissed_paid", case_status="CLOSED",
         property_address="x, Garland, TX 75040", property_type="real"),
    # ── GENUINELY PAID, DALLAS: dismissed on the docket → stays paid ──────────────────────────────
    dict(id="dallas-paid", case_number="TX-25-00093", current_tax_balance=0.0,
         total_due_filing=13196.13, case_track="dismissed_paid", case_status="CLOSED",
         property_address="y, Dallas, TX 75210", property_type="real"),
    # ── ORDINARY BANDS MUST BE UNTOUCHED ──────────────────────────────────────────────────────────
    dict(id="low", case_number="A1", current_tax_balance=11437.29, total_due_filing=11437.29,
         case_track="active", property_type="real"),
    dict(id="mid", case_number="A2", current_tax_balance=25000.0, total_due_filing=25000.0,
         case_track="active", property_type="real"),
    dict(id="high", case_number="A3", current_tax_balance=152224.40, total_due_filing=80583.24,
         case_track="judged_pending", property_type="real"),
    dict(id="null-bal", case_number="A4", current_tax_balance=None, total_due_filing=9000.0,
         case_track="active", property_type="real"),
    # ── $0 with NO suit amount: no contradiction evidence, so not flagged ─────────────────────────
    dict(id="zero-nofiling", case_number="A5", current_tax_balance=0.0, total_due_filing=None,
         case_track="active", property_type="real"),
    # ── BPP is its own track and never lands in a payoff band ─────────────────────────────────────
    dict(id="bpp", case_number="A6", current_tax_balance=0.0, total_due_filing=5000.0,
         case_track="personal_property", property_type="personal"),
]


async def main():
    from playwright.async_api import async_playwright

    src = HTML.read_text()
    # §31 added a collector-corroboration branch to zeroIsContradicted, so its dependencies come too.
    _i = src.index("var ACT_COLLECTED_UI")
    harness = "\n".join([
        src[_i:src.index("];", _i) + 2],
        _fn("parseIntel"), _fn("caseTrack"), _fn("caseLiveBalance"),
        _fn("canonCollector"), _fn("collectorsNamedInSuit"),
        _fn("zeroIsContradicted"), _fn("balanceBand"),
        "window.__band = function(c){ return balanceBand(c); };",
        "window.__contra = function(c){ return zeroIsContradicted(c); };",
    ])

    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=chrome_path())
        pg = await b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto("about:blank")
        await pg.add_script_tag(content=harness)

        bands = {}
        for c in CASES:
            bands[c["id"]] = await pg.evaluate("c => window.__band(c)", c)

        # ── THE DEFECT IS FIXED ──────────────────────────────────────────────────────────────────
        check("TX-26-01455 ($0 ACT, $11,306 suit, active) is NOT classified paid",
              bands["TX-26-01455"] != "zero", bands["TX-26-01455"])
        check("TX-26-01455 surfaces as 'unknown' — visible in triage",
              bands["TX-26-01455"] == "unknown", bands["TX-26-01455"])

        # ── THE DISCRIMINATOR IS THE CONTRADICTION, NOT THE ADDRESS ──────────────────────────────
        check("a DALLAS case with identical facts flags too (not geography-gated)",
              bands["dallas-contra"] == "unknown", bands["dallas-contra"])
        check("a GARLAND case dismissed on the docket stays paid (not address-gated either)",
              bands["garland-dismissed"] == "zero", bands["garland-dismissed"])

        # ── NO BLANKET zero→unknown: genuinely-paid cases are untouched ──────────────────────────
        check("dismissed_paid Dallas case stays 'zero'", bands["dallas-paid"] == "zero", bands["dallas-paid"])
        check("$0 with no suit amount is not flagged (no contradiction evidence)",
              bands["zero-nofiling"] == "zero", bands["zero-nofiling"])

        # ── EVERY OTHER BAND UNCHANGED ───────────────────────────────────────────────────────────
        check("low band unchanged", bands["low"] == "low", bands["low"])
        check("mid band unchanged", bands["mid"] == "mid", bands["mid"])
        check("high band unchanged", bands["high"] == "high", bands["high"])
        check("null balance still 'unknown'", bands["null-bal"] == "unknown", bands["null-bal"])
        check("BPP still 'na'", bands["bpp"] == "na", bands["bpp"])

        # ── THE PREDICATE ITSELF ─────────────────────────────────────────────────────────────────
        contra = {c["id"]: await pg.evaluate("c => window.__contra(c)", c) for c in CASES}
        check("predicate true on the proven case", contra["TX-26-01455"] is True)
        check("predicate false on a dismissal", contra["garland-dismissed"] is False)
        check("predicate false with no suit amount", contra["zero-nofiling"] is False)
        check("predicate needs a POSITIVE suit amount (0 filed ⇒ no contradiction)",
              await pg.evaluate("c => window.__contra(c)",
                                dict(case_number="Z", current_tax_balance=0.0,
                                     total_due_filing=0, case_track="active")) is False)

        # ── THE FILTER OPTION VALUES STILL MATCH THE CLASSIFIER ──────────────────────────────────
        opts = set(re.findall(r'<option value="(low|mid|high|zero|unknown)">', src))
        check("filter offers every band the classifier can return",
              {"low", "mid", "high", "zero", "unknown"} <= opts, str(sorted(opts)))
        check("the 'zero' option no longer claims a bare '$0' means paid",
              'value="zero">Paid ($0, dismissed)' in src)

        check("zero pageerror", not errs, "; ".join(errs))
        await b.close()

    print("-" * 60)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
