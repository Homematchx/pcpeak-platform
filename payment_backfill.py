#!/usr/bin/env python3
"""One-time backfill: re-scrape ACT payment history for enriched cases with the fixed
line-pairing parser, and update payment_history in each case's property_intel.
Disk-light (ACT HTML only, no PDFs). Skips BPP (personal_property) cases."""
import asyncio, json, sqlite3, re
from pathlib import Path

DB = Path(__file__).parent / "data" / "db" / "pcpeak.db"

async def main():
    from playwright.async_api import async_playwright
    from property_intel import scrape_dallas_act
    c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT case_number, account_number, property_intel FROM cases "
        "WHERE property_intel IS NOT NULL AND (case_track IS NULL OR case_track!='personal_property')"
    ).fetchall()
    targets = []
    for r in rows:
        try: pi = json.loads(r["property_intel"])
        except Exception: continue
        acct = (r["account_number"] or "").split(",")[0].strip()
        if re.fullmatch(r"[0-9A-Za-z]{17}", acct):
            targets.append((r["case_number"], acct, pi))
    print("payment backfill: %d enriched cases" % len(targets), flush=True)
    pw = await async_playwright().start(); b = await pw.chromium.launch(headless=True)
    fixed = 0; grew = 0
    try:
        for cn, acct, pi in targets:
            before = len(pi.get("payment_history") or [])
            try:
                fresh = await scrape_dallas_act(acct, b)
            except Exception as e:
                print("  %s ERR %s" % (cn, str(e)[:40]), flush=True); continue
            ph = fresh.get("payment_history") or []
            pi["payment_history"] = ph
            with sqlite3.connect(str(DB)) as db:
                db.execute("UPDATE cases SET property_intel=? WHERE case_number=?", [json.dumps(pi), cn]); db.commit()
            fixed += 1
            if len(ph) > before: grew += 1
            print("  %-13s payments %d -> %d" % (cn, before, len(ph)), flush=True)
    finally:
        await b.close(); await pw.stop()
    print("DONE: %d cases re-scraped, %d gained records" % (fixed, grew), flush=True)

if __name__ == "__main__":
    asyncio.run(main())
