#!/usr/bin/env python3
"""Post-batch census — verified-complete vs honest-INDETERMINATE, BY CAUSE.

    python3 batch_census.py                 # whole local book
    python3 batch_census.py --since TX-26-015   # only cases matching a prefix (a batch)
    python3 batch_census.py --prod           # census the LIVE book through the real endpoint

WHY BY CAUSE. "57 INDETERMINATE" is not actionable; the four causes have different remedies and three
of them are not defects at all:

  gds_unfetched      a NAMED adapter-backed collector we did not retrieve  -> re-run collector_backfill
  zero_blind_spot    ACT publishes no unit list at $0 balance (§17.5/§34)  -> NOT FIXABLE, by design
  membership_gap     petition membership empty (second-template extraction) -> Claude re-extraction
  no_cad             no resolvable DCAD account                            -> resolve_backlog.py

Also reports IDENTITY-GUARD DISCARDS (`_rejected`) and whether NONZERO collector balances actually
landed — the caveat left open when the gds block cleared: the fetch path was confirmed on parcels that
all returned $0.00, so "nonzero under load" stays unproven until a real batch shows one.
"""
import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import acquisition as A
import jurisdictions as J
from acquisition import CaseInput

DB = Path(__file__).parent / "data" / "db" / "pcpeak.db"
BASE = "property_type IS NOT 'personal' AND case_track IS NOT 'personal_property'"


def classify(pi, tb, comp):
    """Why is this case not verified-complete? First matching cause wins, most-actionable first."""
    if not (pi.get("account_number") or "").strip():
        return "no_cad"
    if not J.petition_collectors(tb):
        return "membership_gap"
    if comp["unavailable_collectors"]:
        reach = [c for c in comp["unavailable_collectors"]
                 if (J.resolve_collector(c) or {}).get("platform") in set(J.ADAPTERS)]
        return "gds_unfetched" if reach else "no_adapter_by_design"
    if pi.get("act_units_reason") == "no_unit_list_at_zero_balance":
        return "zero_blind_spot"
    if not pi.get("act_units"):
        return "act_units_missing"
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="case-number prefix, e.g. TX-26-015")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    q = f"SELECT * FROM cases WHERE {BASE}"
    params = ()
    if args.since:
        q += " AND case_number LIKE ?"
        params = (args.since + "%",)
    rows = con.execute(q, params).fetchall()

    verified, causes = 0, collections.Counter()
    rejects, nonzero, zero_fetched, portal_faults = [], [], 0, []
    for r in rows:
        d = dict(r)
        try:
            pi = json.loads(d.get("property_intel") or "{}")
        except ValueError:
            pi = {}
        cbraw = pi.get("collector_balances") or {}
        for rj in (cbraw.get("_rejected") or []):
            rejects.append((d["case_number"], rj))
        if cbraw.get("_portal_unavailable"):
            portal_faults.append(d["case_number"])
        cb = {k: v.get("amount") for k, v in cbraw.items()
              if isinstance(v, dict) and isinstance(v.get("amount"), (int, float))}
        for k, v in cb.items():
            if v > 0:
                nonzero.append((d["case_number"], k, v))
            else:
                zero_fetched += 1
        c = CaseInput(d["case_number"], owed=pi.get("current_tax_balance"),
                      total_due_filing=d.get("total_due_filing"), property_type="real",
                      case_track=d.get("case_track"), tax_breakdown=d.get("tax_breakdown"),
                      collector_balances=cb, act_units=pi.get("act_units"))
        comp = A.tax_payoff_lines(c)["completeness"]
        if J.payoff_is_complete(comp):
            verified += 1
        else:
            causes[classify(pi, d.get("tax_breakdown"), comp)] += 1

    n = len(rows)
    print(f"CENSUS — {n} case(s){' matching ' + args.since if args.since else ''}\n")
    print(f"  VERIFIED-COMPLETE (proven on evidence) : {verified:4d}  ({100*verified/n:.1f}%)" if n else "")
    print(f"  honest INDETERMINATE                   : {sum(causes.values()):4d}\n")
    print("  by cause:")
    LABEL = {"gds_unfetched": "adapter-backed collector NOT fetched  -> re-run collector_backfill",
             "zero_blind_spot": "$0 balance, ACT prints no unit list    -> NOT FIXABLE (by design)",
             "membership_gap": "petition membership empty              -> Claude re-extraction",
             "no_cad": "no resolvable DCAD account            -> resolve_backlog.py",
             "no_adapter_by_design": "collector has no adapter              -> by design",
             "act_units_missing": "act_units absent                      -> act_units_backfill.py",
             "other": "unclassified                          -> investigate"}
    for k, v in causes.most_common():
        print(f"     {v:4d}  {LABEL.get(k, k)}")

    print(f"\n  IDENTITY-GUARD DISCARDS (_rejected): {len(rejects)}")
    for cn, rj in rejects[:8]:
        print(f"     {cn}  {rj.get('collector')}  requested={rj.get('requested_cad')} "
              f"returned={rj.get('returned_cad')}")
    print(f"  PORTAL FAULTS (_portal_unavailable): {len(portal_faults)} {portal_faults[:6]}")

    print(f"\n  COLLECTOR BALANCES LANDED — nonzero: {len(nonzero)}   fetched-zero: {zero_fetched}")
    if nonzero:
        print("     (this closes the 'nonzero under load' caveat left open when gds cleared)")
        for cn, k, v in sorted(nonzero, key=lambda x: -x[2])[:10]:
            print(f"     {cn:14s} {k:26s} ${v:,.2f}")
    else:
        print("     ⚠ NO nonzero collector balance anywhere — the caveat is STILL OPEN.")
        print("       Not necessarily a fault: every fetched parcel may genuinely owe $0.")


if __name__ == "__main__":
    main()
