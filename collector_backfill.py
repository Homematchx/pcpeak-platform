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
    # ORDER BY NEED, NOT BY ROWID. `--limit N` slices this list, so its order decides what a paced
    # batch actually accomplishes. Measured: a 20-case run fetched 29 balances and moved the census
    # by ZERO, because all 20 were already complete and NONE of the 23 cases missing a collector were
    # in the first 20 — five paced sessions would have elapsed before touching the real backlog.
    # Cases still missing an adapter-backed collector now sort first; the rest are refreshes.
    def _still_missing(t):
        cb = (t["intel"].get("collector_balances") or {})
        have = {k for k, v in cb.items()
                if isinstance(v, dict) and isinstance(v.get("amount"), (int, float))}
        return any(c not in have for c in t["collectors"])
    out.sort(key=lambda t: (not _still_missing(t), t["case"]))
    return out


# PACING. The portal re-blocked this host after an 80-case run — ~136 fetches over ~10 unbroken
# minutes from one IP. Concurrency was already 1, so volume is the variable, and the only lever was
# `--limit`. This is a DELIBERATE inter-case delay: the in-fetch sleeps are page-load waits, not
# politeness. Default is conservative because the cost of being slow is minutes and the cost of being
# fast is a multi-hour block on 4 of 5 mapped collectors.
# ⚠ WE DO NOT KNOW THE ACTUAL LIMIT. One data point (80 cases → blocked) is not a rate model, so
# treat any cadence as a hypothesis and keep batches small enough that being wrong is cheap.
DEFAULT_DELAY_S = 4.0


async def run(targets, write=True, delay=DEFAULT_DELAY_S):
    from playwright.async_api import async_playwright
    conn = sqlite3.connect(DB)
    done = fetched = failed = 0
    portal_down = False
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=chrome_path())
        try:
            for t in targets:
                # Each adapter takes only the collectors on ITS platform and ignores the rest, so a
                # case naming both a GDS office and an ACT district is served by both in one pass.
                got = await collectors_gds.fetch_for_case(t["collectors"], t["cad"], browser)
                got.update(collectors_act.fetch_for_case(t["collectors"], t["cad"]))
                # §19 — SPLIT BY SHAPE BEFORE SUMMING. `got` carries collector dicts AND sentinel
                # keys whose values are LISTS (`_portal_unavailable`, `_rejected`). Summing across
                # `.values()` crashed here the first time a portal fault occurred, and would have
                # mis-counted `fetched` even when it did not crash.
                amounts = jurisdictions.collector_amounts(got)
                sentinels = jurisdictions.collector_sentinels(got)
                # UNAVAILABLE means "we hold no balance for this collector", NOT "this pass missed
                # it". The run re-fetches everything, so a collector fetched in an EARLIER pass is
                # still held — the merge preserves it. Comparing against this fetch alone reported 5
                # unavailable when only 3 were genuinely missing, which makes a healthy run look
                # worse than it is and invites chasing misses that are not there.
                held = jurisdictions.collector_amounts(t["intel"].get("collector_balances"))
                miss = [c for c in t["collectors"] if c not in amounts and c not in held]
                total = sum(amounts.values())
                print(f"  {t['case']:<14} cad={t['cad']}  fetched {len(amounts)}/{len(t['collectors'])}"
                      f"  ${total:>12,.2f}" + (f"   UNAVAILABLE: {', '.join(miss)}" if miss else ""))
                for key, val in sentinels.items():
                    print(f"      ⚠ {key}: {val}")
                fetched += len(amounts)
                failed += len(miss)
                # Write once, whatever happened — the sentinel is PART of the record (batch_census
                # and the payoff surfaces both read it), so a fault is stored, not swallowed.
                # A stale diagnostic must clear even when THIS pass fetched nothing: the fact it
                # asserts ("the portal refused us") is disproved by having reached the portal at all,
                # and an empty result is a per-parcel miss, not a refusal. Gating the clear on `got`
                # left TX-26-01600 flagged as faulted while holding a live $18,599.03 balance.
                stale = [k for k in jurisdictions.SENTINEL_KEYS
                         if k in (t["intel"].get("collector_balances") or {}) and k not in got]
                if write and (got or stale):
                    intel = t["intel"]
                    merged = {**(intel.get("collector_balances") or {}), **got}
                    # CLEAR STALE DIAGNOSTICS. `{**stored, **got}` preserves a sentinel forever,
                    # because a SUCCESSFUL fetch simply has no sentinel key to overwrite it with.
                    # Measured: TX-26-01600 carried `_portal_unavailable` from the blocked run while
                    # also holding a freshly-fetched RICHARDSON ISD $18,599.03, so the census reported
                    # a portal fault that no longer existed. A diagnostic must describe the LATEST
                    # attempt; one that outlives its truth is worse than none, because it is believed.
                    for key in jurisdictions.SENTINEL_KEYS:
                        if key not in got:
                            merged.pop(key, None)
                    intel["collector_balances"] = merged
                    conn.execute("UPDATE cases SET property_intel=? WHERE case_number=?",
                                 (json.dumps(intel), t["case"]))
                    conn.commit()
                done += 1
                # An infrastructure fault is not a per-parcel negative. Grinding through the rest of
                # the book against a portal that is refusing us produces nothing and risks deepening
                # a rate-limit block, so stop and say so.
                if "_portal_unavailable" in sentinels:
                    portal_down = True
                    print(f"\n  ⛔ PORTAL REFUSING — {sentinels['_portal_unavailable']}")
                    print("     Stopping. Balances already written are intact; unfetched collectors")
                    print("     stay `unavailable` → INDETERMINATE, never $0. Safe to re-run later.")
                    break
                # Space the requests out. Skipped after the final target so a run does not end on a
                # pointless wait.
                if delay and t is not targets[-1]:
                    await asyncio.sleep(delay)
        finally:
            await browser.close()
    conn.close()
    print(f"\n  cases processed {done} · collector balances fetched {fetched} · "
          f"left UNAVAILABLE {failed}")
    if portal_down:
        print("  ⛔ RUN STOPPED EARLY on a portal fault — re-run when the portal is back.")
        print("     Verify first with:  python3 collectors_gds.py <CAD>   (never curl — §32.6)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--case")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_S,
                    help=f"seconds between CASES (default {DEFAULT_DELAY_S}). Raise it if the "
                         f"portal blocks; 0 disables pacing entirely (not advised).")
    a = ap.parse_args()
    conn = sqlite3.connect(DB)
    targets = eligible(conn, only=a.case)
    conn.close()
    if a.limit:
        targets = targets[:a.limit]
    print(f"eligible cases (petition names a gds-reachable collector + a CAD on file): {len(targets)}")
    # PRE-FLIGHT. One GET (~200ms) instead of launching chromium to discover the same thing 10s later
    # and once per case. Cheap enough that there is no reason not to check first.
    blocked, why = collectors_gds.portal_blocked()
    print(f"  portal pre-flight: {'⛔ BLOCKED' if blocked else '✅ reachable'} — {why}")
    if blocked and not a.dry_run:
        print("\n  Refusing to start: every fetch would fail soft and write nothing useful.")
        print("  Watch for recovery cheaply:  python3 collectors_gds.py --watch")
        print("  When it clears, CONFIRM with the real adapter before a batch:")
        print("      python3 collectors_gds.py 26238500070260000")
        return
    if a.dry_run:
        SHOWN = 20
        for t in targets[:SHOWN]:
            print(f"  {t['case']:<14} cad={t['cad']}  would query: {', '.join(t['collectors'])}")
        if len(targets) > SHOWN:
            # ⚠ Say the truncation out loud. Counting the PRINTED lines gives 20 and is wrong; the
            # header above carries the real number. That exact mistake has been made here before.
            print(f"  … and {len(targets) - SHOWN} more NOT LISTED — the count is the header line "
                  f"above ({len(targets)}), not the number of lines printed.")
        print("  (dry run — nothing fetched, nothing written)")
        return
    est = len(targets) * (a.delay + 6)
    print(f"  pacing: {a.delay}s between cases → rough ETA {est/60:.0f} min for {len(targets)} case(s)")
    asyncio.run(run(targets, delay=a.delay))


if __name__ == "__main__":
    main()
