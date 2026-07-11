#!/usr/bin/env python3
"""
Resolve the account backlog (account_status in needs_lookup / invalid) via CORROBORATED
DCAD lookup, then optionally enrich the recovered cases.

Safety rule (from the n=92 audit): DCAD's address search can return a confidently WRONG
parcel. So an account is auto-assigned ONLY when a second, independent signal agrees
(property_intel.resolve_account_corroborated). An uncorroborated candidate is recorded
as a note but NEVER written — the case stays in the backlog for a human. A wrong-but-
confident account is worse than a delayed one.

    python3 resolve_backlog.py                 # resolve + enrich corroborated cases (writes DB)
    python3 resolve_backlog.py --dry-run       # show what WOULD happen, write nothing
    python3 resolve_backlog.py --no-enrich     # resolve accounts only, skip enrichment
    python3 resolve_backlog.py --limit 5
"""
import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "db" / "pcpeak.db"


def backlog(limit=None):
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT case_number, account_number, account_status, property_address,
                  defendant, all_defendants
           FROM cases
           WHERE account_status IN ('needs_lookup','invalid')
           ORDER BY case_number"""
    ).fetchall()
    c.close()
    return rows[:limit] if limit else rows


def _all_def_text(row):
    try:
        ad = json.loads(row["all_defendants"]) if row["all_defendants"] else []
        return " ".join(x.get("name", "") for x in ad)
    except Exception:
        return ""


async def main(args):
    from playwright.async_api import async_playwright
    from property_intel import resolve_account_corroborated, enrich_property, corroboration_strength

    cases = backlog(args.limit)
    if not cases:
        print("Backlog is empty — nothing to resolve.")
        return
    print("Backlog: %d case(s) (needs_lookup / invalid)%s\n"
          % (len(cases), "   [DRY RUN — no writes]" if args.dry_run else ""))

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    stats = {"resolved": 0, "uncorroborated": 0, "unresolved": 0, "enriched": 0}
    try:
        for r in cases:
            cn = r["case_number"]
            addr = r["property_address"] or ""
            deff = r["defendant"] or ""
            try:
                acct, conf, why = await resolve_account_corroborated(
                    addr, deff, browser, extra_names=_all_def_text(r))
            except Exception as e:
                acct, conf, why = "", "unresolved", "error: " + str(e)[:50]

            if conf == "corroborated" and acct:
                stats["resolved"] += 1
                note = "auto-resolved [" + corroboration_strength(why) + "]: " + why
                print("  ✓ %-13s RESOLVED %s  (%s)" % (cn, acct, why))
                if not args.dry_run:
                    with sqlite3.connect(str(DB_PATH)) as db:
                        db.execute("UPDATE cases SET account_number=?, account_status='resolved', "
                                   "account_note=? WHERE case_number=?", [acct, note, cn])
                        db.commit()
                    if not args.no_enrich:
                        try:
                            intel = await enrich_property(acct, addr, browser)
                            if intel and (intel.get("market_value") or intel.get("current_tax_balance")):
                                with sqlite3.connect(str(DB_PATH)) as db:
                                    db.execute("UPDATE cases SET property_intel=? WHERE case_number=?",
                                               [json.dumps(intel), cn]); db.commit()
                                stats["enriched"] += 1
                                print("      enriched: MV $%s | balance $%s"
                                      % (intel.get("market_value"), intel.get("current_tax_balance")))
                        except Exception as e:
                            print("      enrich error: " + str(e)[:60])
            elif conf == "uncorroborated":
                stats["uncorroborated"] += 1
                note = "still needs manual lookup — " + why
                print("  … %-13s KEPT in backlog (%s)  candidate=%s" % (cn, why, acct))
                if not args.dry_run:
                    with sqlite3.connect(str(DB_PATH)) as db:
                        db.execute("UPDATE cases SET account_note=? WHERE case_number=?", [note, cn])
                        db.commit()
            else:
                stats["unresolved"] += 1
                note = "no Dallas DCAD match (likely out-of-county / other appraisal district)"
                print("  ✗ %-13s UNRESOLVED (%s)  %s" % (cn, why, addr[:34]))
                if not args.dry_run:
                    with sqlite3.connect(str(DB_PATH)) as db:
                        db.execute("UPDATE cases SET account_note=? WHERE case_number=?", [note, cn])
                        db.commit()
    finally:
        await browser.close()
        await pw.stop()

    n = len(cases)
    print("\n" + "=" * 60)
    print("BACKLOG RESOLUTION  (n=%d)" % n)
    print("  auto-resolved (corroborated) : %d%s" % (stats["resolved"],
          "  (%d enriched)" % stats["enriched"] if stats["enriched"] else ""))
    print("  kept — uncorroborated        : %d  (candidate found, not trusted)" % stats["uncorroborated"])
    print("  unresolved — no DCAD match   : %d  (out-of-county / other district)" % stats["unresolved"])
    tot = stats["resolved"] + stats["uncorroborated"] + stats["unresolved"]
    print("  reconcile: %d + %d + %d = %d == %d  -> %s"
          % (stats["resolved"], stats["uncorroborated"], stats["unresolved"], tot, n, tot == n))
    if args.dry_run:
        print("\n  DRY RUN — no database changes were made.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    ap.add_argument("--no-enrich", action="store_true", help="resolve accounts only, skip DCAD enrichment")
    ap.add_argument("--limit", type=int, default=None)
    asyncio.run(main(ap.parse_args()))
