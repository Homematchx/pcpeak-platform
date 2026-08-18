#!/usr/bin/env python3
"""§30 — the ACT-instance collector adapter (Irving ISD), offline.

Irving ISD runs its OWN copy of the same software Dallas County uses, so the detail page is a plain
`showdetail2.jsp?can=<CAD>&ownerno=0` GET — no session, no widget, no browser. Same contract as the
GDS adapter: CAD-keyed, membership before balance, identity guard, retry once, fail-soft.

Fixtures are REAL captured page text from the live Irving instance.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collectors_act as C
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


# REAL page text — TX-26-00041, 302 W 16th St. Current $3,382.91 + prior $84,821.05 = $88,203.96.
IRVING_OWING = """
<td>Account Number:&nbsp;</td><td>32057500060100000</td>
<td>Property Site Address:</td><td>302 W 16TH ST, CI</td>
<td>Current Tax Levy: &nbsp;</td><td>$2,368.98</td>
<td>Current Amount Due:&nbsp;</td><td>$3,382.91</td>
<td>Prior Year Amount Due:&nbsp;</td><td>$84,821.05</td>
<td>Total Amount Due:&nbsp;</td><td>$88,203.96</td>
<td>Active Lawsuits:</td><td>&nbsp; Yes</td>
"""
# REAL page text — TX-23-01994, a genuinely paid parcel.
IRVING_PAID = """
<td>Account Number:&nbsp;</td><td>32238500100060000</td>
<td>Property Site Address:</td><td>1618 MEADOWBROOK LN, CI</td>
<td>Current Tax Levy: &nbsp;</td><td>$2,537.62</td>
<td>Current Amount Due:&nbsp;</td><td>$0.00</td>
<td>Prior Year Amount Due:&nbsp;</td><td>$0.00</td>
<td>Total Amount Due:&nbsp;</td><td>$0.00</td>
<td>Active Lawsuits:</td><td>&nbsp; None</td>
"""


def test_parse():
    print("\nparsing a real ACT-instance detail page")
    d = C.parse_act_detail(IRVING_OWING)
    check("account (the CAD) parsed", d["account"] == "32057500060100000", str(d.get("account")))
    check("total amount due parsed", d["amount_due"] == 88203.96, str(d.get("amount_due")))
    check("…and it is the TOTAL, not just the current year", d["amount_due"] != 3382.91)
    check("current + prior reconcile to the total", abs(3382.91 + 84821.05 - d["amount_due"]) < 0.01)
    check("levy parsed", d["levy"] == 2368.98)
    check("site address captured (independent identity evidence)",
          d["site_address"] == "302 W 16TH ST, CI", str(d.get("site_address")))
    check("active lawsuit read as True", d["lawsuits"] is True)


def test_verified_zero():
    print("\na fetched zero is a fact, not an absence")
    d = C.parse_act_detail(IRVING_PAID)
    check("a paid parcel parses", bool(d))
    check("amount is 0.0, a real value", d["amount_due"] == 0.0)
    check("…not None", d["amount_due"] is not None)
    check("no active lawsuit", d["lawsuits"] is False)
    lines = J.collector_lines([{"entity": "IRVING INDEPENDENT SCHOOL DISTRICT", "total": 500.0}],
                              act_balance=0.0, fetched={"IRVING ISD": 0.0})
    g = [l for l in lines["collectors"] if l["collector"] == "IRVING ISD"][0]
    check("a fetched $0 renders VERIFIED in the payoff schema", g["label"] == J.VERIFIED)


def test_fails_closed():
    print("\nfails closed — never invents a balance")
    for label, txt in [("empty", ""), ("None", None), ("junk", "<html>unrelated</html>"),
                       ("account but no total", "<td>Account Number:</td><td>123</td>"),
                       ("total but no account", "<td>Total Amount Due:</td><td>$5.00</td>")]:
        check(f"{label} → {{}} (→ unavailable, never $0)", C.parse_act_detail(txt) == {})


def test_registry_and_guards():
    print("\nregistry, guards and the shared contract")
    info = J.resolve_collector("IRVING INDEPENDENT SCHOOL DISTRICT")
    check("Irving ISD resolves to the irving_act platform", info["platform"] == "irving_act")
    check("…and is now reachable (adapter registered)", info["reachable"] is True)
    check("the ACT instance path is registry DATA", J.act_path_for("IRVING ISD") == "irving")
    src = (Path(__file__).parent / "collectors_act.py").read_text()
    check("the fetcher hardcodes no instance path", not re.search(r'["\']irving["\']', src))
    check("identity guard present (returned account vs requested CAD)",
          'got.get("account")' in src and "requested_cad" in src)
    check("retries once before giving up", "attempts: int = 2" in src)
    check("gds collectors are NOT routed to this adapter",
          J.resolve_collector("GARLAND ISD")["platform"] != "irving_act")
    # membership gating: a petition that never named Irving must not query it
    got = C.fetch_for_case([c["collector"] for c in J.petition_collectors(
        [{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 1.0}])], "32057500060100000")
    check("a collector the petition did not name is never queried", got == {})


def run():
    print("=" * 78)
    print("§30 — ACT-INSTANCE COLLECTOR ADAPTER (IRVING ISD)")
    print("=" * 78)
    test_parse()
    test_verified_zero()
    test_fails_closed()
    test_registry_and_guards()
    print("-" * 78)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
