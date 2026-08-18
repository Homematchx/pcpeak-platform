#!/usr/bin/env python3
"""Populate `property_intel.collector_balances` for cases whose PETITION names a collector outside ACT.

    python3 collector_backfill.py --dry-run          # show what would be fetched, hit nothing
    python3 collector_backfill.py --limit 5          # fetch a small sample
    python3 collector_backfill.py --case TX-26-00991 # one case
    python3 collector_backfill.py                    # the whole eligible set

LOCAL ONLY — scraping never runs in the cloud. This writes to the local DB; `sync_to_prod.py` pushes
it afterwards, under the usual prod_ready gate.

TWO RULES IT WILL NOT BREAK
  · MEMBERSHIP BEFORE BALANCE — a collector is queried only if the petition NAMED it. Never geography.
  · FAIL-SOFT — a fetch that fails leaves the collector ABSENT, which the payoff schema renders
    `unavailable` → INDETERMINATE. It never writes $0 and never disturbs enrichment already stored.
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collectors_act
import collectors_gds
import jurisdictions
from browser_env import chrome_path

# platform → the adapters that can reach it. Adding a platform is an entry here; the runner below is
# platform-agnostic.
REACHABLE = set(jurisdictions.ADAPTERS)

DB = Path(__file__).parent / "data" / "db" / "pcpeak.db"


def eligible(conn, only=None):
    """Cases whose petition names a collector an adapter can actually reach, and that have a CAD."""
    rows = conn.execute("SELECT case_number, tax_breakdown, property_intel FROM cases").fetchall()
    out = []
    roster = jurisdictions.load_gds_roster()
    for cn, tb, pi in rows:
        if only and cn != only:
            continue
        try:
            intel = json.loads(pi or "{}")
        except ValueError:
            continue
        cad = (intel.get("account_number") or "").split(",")[0].strip()
        if not cad:
            continue
        named = [c["collector"] for c in jurisdictions.petition_collectors(tb)]
        reach = [n for n in named
                 if (jurisdictions.resolve_collector(n, roster=roster) or {}).get("platform") in REACHABLE]
        if reach:
            out.append({"case": cn, "cad": cad, "collectors": reach, "intel": intel})
    return out


async def run(targets, write=True):
    from playwright.async_api import async_playwright
    conn = sqlite3.connect(DB)
    done = fetched = failed = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=chrome_path())
        try:
            for t in targets:
                # Each adapter takes only the collectors on ITS platform and ignores the rest, so a
                # case naming both a GDS office and an ACT district is served by both in one pass.
                got = await collectors_gds.fetch_for_case(t["collectors"], t["cad"], browser)
                got.update(collectors_act.fetch_for_case(t["collectors"], t["cad"]))
                miss = [c for c in t["collectors"] if c not in got]
                total = sum(v["amount"] for v in got.values())
                print(f"  {t['case']:<14} cad={t['cad']}  fetched {len(got)}/{len(t['collectors'])}"
                      f"  ${total:>12,.2f}" + (f"   UNAVAILABLE: {', '.join(miss)}" if miss else ""))
                fetched += len(got)
                failed += len(miss)
                if write and got:
                    intel = t["intel"]
                    intel["collector_balances"] = {**(intel.get("collector_balances") or {}), **got}
                    conn.execute("UPDATE cases SET property_intel=? WHERE case_number=?",
                                 (json.dumps(intel), t["case"]))
                    conn.commit()
                done += 1
        finally:
            await browser.close()
    conn.close()
    print(f"\n  cases processed {done} · collector balances fetched {fetched} · "
          f"left UNAVAILABLE {failed}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--case")
    a = ap.parse_args()
    conn = sqlite3.connect(DB)
    targets = eligible(conn, only=a.case)
    conn.close()
    if a.limit:
        targets = targets[:a.limit]
    print(f"eligible cases (petition names a gds-reachable collector + a CAD on file): {len(targets)}")
    if a.dry_run:
        for t in targets[:20]:
            print(f"  {t['case']:<14} cad={t['cad']}  would query: {', '.join(t['collectors'])}")
        print("  (dry run — nothing fetched, nothing written)")
        return
    asyncio.run(run(targets))


if __name__ == "__main__":
    main()
