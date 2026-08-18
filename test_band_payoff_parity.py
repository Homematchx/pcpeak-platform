#!/usr/bin/env python3
"""§31 — BAND ↔ PAYOFF PARITY: the two consumers of collector data must agree on paid-vs-unconfirmed.

THE PATTERN, THIRD SURFACE. `balanceBand()` and `tax_payoff()` now both read collector coverage. When
only one of them learned about collectors, they disagreed on **11 cases** — the band saying
"Unconfirmed — needs check" while the payoff on the same parcel said a **verified $0**. That is the
§28 two-consumer defect at a new surface, and nothing but a test stops it recurring:

  §19  is this a set?              (data)
  §28  did every consumer update?  (client vs engine payoff)
  §31  …and this consumer too?     (band vs payoff, on paid-vs-unconfirmed)

THE INVARIANT (restated by §33 — it used to read "a VERIFIED $0")
  band == "zero"  (confirmed paid)  ⟺  payoff is $0 AND completeness is affirmatively established
  band == "unknown" (unconfirmed)   ⟺  anything else
Anything else means one surface learned something the other did not.

⚠ §33 changed what "confirmed paid" can mean, and the parity predicate had to move with it. The old
form compared against `label == VERIFIED`; §33 pins that label to ESTIMATED fleet-wide (the collector
set is petition-derived and therefore a lower bound), so the comparison became a constant-False vs
constant-False and passed VACUOUSLY. It now reads `payoff_is_complete(completeness)`, and every row
additionally asserts its expected band DIRECTLY — a parity check alone cannot notice that both sides
went false together.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import acquisition as A
import jurisdictions as J
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

# (label, ACT balance, petition rows, fetched balances, filing amount, track, expected_band)
#
# ⚠ §33 REWROTE THE FIRST TWO. They used to read "every named collector fetched at $0 ⇒ CONFIRMED
# PAID". That inference is unsound: `collectors_named` comes from the petition, which names
# PLAINTIFFS — a LOWER BOUND on who levies — so fetching it to completion proves nothing about the
# SET. Corroborating a zero needs ACT's per-parcel unit report, which exists for no parcel today.
#
# `expected_band` is asserted DIRECTLY, not only through parity. Parity alone went VACUOUS under
# §33: it compares `band=="zero"` against `label==VERIFIED`, and §33 makes the label never VERIFIED,
# so both sides go false together and the row passes while its description claims the opposite.
# Parity still catches a revert of the band flip — but a guard whose fixtures can drift out of
# agreement with their own names is one bad rename from meaning nothing.
CASES = [
    ("named collectors all fetched at $0 — still UNCONFIRMED: the SET is a lower bound (§33)",
     0.0, [{"entity": GISD, "total": 6991.30}], {"GARLAND ISD": 0.0}, 11329.20, "active", "unknown"),
    ("…same with TWO collectors both fetched at $0 — fetching a lower bound to completion "
     "corroborates nothing",
     0.0, [{"entity": GISD}, {"entity": COG}], {"GARLAND ISD": 0.0, "CITY OF GARLAND": 0.0},
     11329.20, "active", "unknown"),
    ("PARTIALLY checked — one of two fetched ⇒ still UNCONFIRMED",
     0.0, [{"entity": GISD}, {"entity": COG}], {"GARLAND ISD": 0.0}, 11329.20, "active", "unknown"),
    ("never checked — collectors named, none fetched ⇒ UNCONFIRMED (the §17.4 case)",
     0.0, [{"entity": GISD}, {"entity": COG}], {}, 11329.20, "active", "unknown"),
    ("collector fetched with a REAL balance ⇒ not paid, not a zero band",
     0.0, [{"entity": GISD}], {"GARLAND ISD": 6991.30}, 11329.20, "active", "unknown"),
    ("all-ACT parcel, ACT $0, active suit ⇒ UNCONFIRMED (no collector can corroborate)",
     0.0, [{"entity": "DALLAS INDEPENDENT SCHOOL DISTRICT"}], {}, 19366.44, "active", "unknown"),
    ("dismissed_paid ⇒ zero band regardless (the DOCKET stopped collection — independent evidence, "
     "not a balance corroborating itself)",
     0.0, [{"entity": GISD}], {}, 11329.20, "dismissed_paid", "zero"),
    ("no suit amount ⇒ nothing to contradict",
     0.0, [{"entity": GISD}], {}, 0, "active", "zero"),
]


async def main():
    from playwright.async_api import async_playwright
    src = HTML.read_text()
    i = src.index("var ACT_COLLECTED_UI")
    harness = "\n".join([
        src[i:src.index("];", i) + 2],
        _fn("parseIntel", src), _fn("caseTrack", src), _fn("caseLiveBalance", src),
        _fn("canonCollector", src), _fn("collectorsNamedInSuit", src),
        _fn("zeroIsContradicted", src), _fn("balanceBand", src),
        "window.__band = c => balanceBand(c);"])

    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=chrome_path())
        pg = await b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto("about:blank")
        await pg.add_script_tag(content=harness)

        for label, bal, tb, fetched, filed, track, expected_band in CASES:
            # SKELETON shape: the promoted columns, exactly what the sidebar receives.
            named = sum(1 for c in J.petition_collectors(tb)
                        if J.resolve_collector(c["collector"])["scope"] != "act")
            skel = {"case_number": "X", "current_tax_balance": bal, "total_due_filing": filed,
                    "case_track": track, "property_type": "real",
                    "collectors_named": named,
                    "collectors_fetched": len(fetched) or None,
                    "collector_fetched_total": (round(sum(fetched.values()), 2) if fetched else None)}
            band = await pg.evaluate("c => window.__band(c)", skel)
            payoff = A.tax_payoff(CaseInput("X", owed=bal, total_due_filing=filed,
                                            tax_breakdown=tb, collector_balances=fetched))
            # DIRECT assertion of the band — states the intent even if the parity predicate below
            # ever goes vacuous again.
            check(f"{label}", band == expected_band,
                  f'band={band} expected={expected_band} payoff=${payoff["amount"]} [{payoff["label"]}]')
            # PARITY: the band and the engine must not disagree about a confirmed-paid parcel. Read
            # completeness, not the label word — §33 pinned the label to ESTIMATED fleet-wide, so
            # `label == VERIFIED` is now a constant False and comparing against it proves nothing.
            comp = A.tax_payoff_lines(CaseInput("X", owed=bal, total_due_filing=filed,
                                                tax_breakdown=tb,
                                                collector_balances=fetched))["completeness"]
            confirmed_paid = payoff["amount"] == 0 and J.payoff_is_complete(comp)
            agree = (band == "zero") == confirmed_paid if track != "dismissed_paid" and filed else True
            check(f"  ↳ band/payoff parity", agree,
                  f'band={band} amount=${payoff["amount"]} complete={comp["complete"]}')

        # ── the 11 real cases that exposed the disagreement ──────────────────────────────────────
        print("\nthe 11 live cases that disagreed before this gate")
        import sqlite3
        db = ROOT / "data" / "db" / "pcpeak.db"
        if db.exists():
            con = sqlite3.connect(db)
            bad = []
            for cn, tb_raw, pi_raw, filed, track in con.execute(
                    "SELECT case_number,tax_breakdown,property_intel,total_due_filing,case_track FROM cases"):
                try:
                    pi = json.loads(pi_raw or "{}")
                except ValueError:
                    continue
                bal = pi.get("current_tax_balance")
                fetched = {k: v.get("amount") for k, v in (pi.get("collector_balances") or {}).items()
                           if isinstance(v, dict) and isinstance(v.get("amount"), (int, float))}
                if bal is None or not fetched:
                    continue
                named = sum(1 for c in J.petition_collectors(tb_raw)
                            if J.resolve_collector(c["collector"])["scope"] != "act")
                skel = {"case_number": cn, "current_tax_balance": bal, "total_due_filing": filed,
                        "case_track": track, "property_type": "real", "collectors_named": named,
                        "collectors_fetched": len(fetched),
                        "collector_fetched_total": round(sum(fetched.values()), 2)}
                band = await pg.evaluate("c => window.__band(c)", skel)
                po = A.tax_payoff(CaseInput(cn, owed=bal, total_due_filing=filed,
                                            tax_breakdown=tb_raw, collector_balances=fetched))
                _c = A.tax_payoff_lines(CaseInput(cn, owed=bal, total_due_filing=filed, tax_breakdown=tb_raw, collector_balances=fetched))["completeness"]
                vz = po["amount"] == 0 and J.payoff_is_complete(_c)
                if (band == "zero") != vz and track != "dismissed_paid" and (filed or 0) > 0:
                    bad.append((cn, band, po["amount"], po["label"]))
            check(f"no case on the real book disagrees (was 11)", not bad, str(bad[:6]))
            con.close()
        check("zero pageerror", not errs, "; ".join(errs))
        await b.close()

    print("-" * 78)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    print("=" * 78)
    print("§31 — BAND ↔ PAYOFF PARITY")
    print("=" * 78)
    sys.exit(0 if asyncio.run(main()) else 1)
