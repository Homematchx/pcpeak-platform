#!/usr/bin/env python3
"""
Resolver audit — measure how often the account we have on file disagrees with what
DCAD's own address search independently returns.

Method: take N cases with a KNOWN-GOOD account (account_status='resolved', a single
17-digit account, and enrichment that actually returned a market value — so DCAD
demonstrably has that parcel). For each, run resolve_dcad_account() from the property
ADDRESS ONLY (the account is never fed in), then compare the independently-found
account to the one on file.

Verdicts:
  MATCH          found == on-file            (extraction/resolution agrees with DCAD)
  MISMATCH*      found != on-file, both set  (* if found via an ADDRESS search, DCAD's
                                              own address lookup disagrees with the
                                              on-file account — strong garble evidence)
  RESOLVER-NULL  found == ""                 (resolver couldn't confirm; NOT a garble)

Reports the mismatch rate with the sample size shown. Read-only: writes nothing to the DB.

    python3 resolver_audit.py [N]        # default N=15
"""
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "db" / "pcpeak.db"


def known_good_sample(n):
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT case_number, account_number, property_address, defendant, property_intel
           FROM cases
           WHERE account_status='resolved'
             AND account_number GLOB '[0-9]*' AND account_number NOT LIKE '%,%'
             AND account_number NOT LIKE '%;%'
             AND property_address IS NOT NULL AND TRIM(property_address) != ''
           ORDER BY case_number"""
    ).fetchall()
    good = []
    for r in rows:
        if not re.fullmatch(r"\d{17}", (r["account_number"] or "").strip()):
            continue
        try:
            pi = json.loads(r["property_intel"]) if r["property_intel"] else None
        except Exception:
            pi = None
        if pi and pi.get("market_value"):
            good.append(r)
    c.close()
    # Even spread across the set so we don't only test the first N alphabetically.
    if len(good) <= n:
        return good
    step = len(good) / n
    return [good[int(i * step)] for i in range(n)]


async def run(n):
    from playwright.async_api import async_playwright
    from property_intel import resolve_dcad_account

    sample = known_good_sample(n)
    if not sample:
        print("No known-good cases found.")
        return
    print("Auditing %d known-good accounts (independent DCAD address re-resolution)\n" % len(sample))

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    results = []
    try:
        for r in sample:
            cn = r["case_number"]
            truth = (r["account_number"] or "").strip()
            addr = r["property_address"] or ""
            try:
                found, how = await resolve_dcad_account(addr, r["defendant"] or "", browser)
            except Exception as e:
                found, how = "", "error:" + str(e)[:40]
            found = (found or "").strip()
            if not found:
                verdict = "RESOLVER-NULL"
            elif found == truth:
                verdict = "MATCH"
            else:
                verdict = "MISMATCH" + ("*" if how.startswith("address") else "")
            results.append({"case": cn, "truth": truth, "found": found,
                            "how": how, "verdict": verdict, "address": addr})
            print("  %-13s on-file=%s  found=%-19s [%-16s] %s"
                  % (cn, truth, found or "(none)", how, verdict))
    finally:
        await browser.close()
        await pw.stop()

    n = len(results)
    match = sum(1 for x in results if x["verdict"] == "MATCH")
    nulls = sum(1 for x in results if x["verdict"] == "RESOLVER-NULL")
    mism = [x for x in results if x["verdict"].startswith("MISMATCH")]
    addr_mism = [x for x in mism if x["verdict"].endswith("*")]
    confirmed = n - nulls  # cases where the resolver returned an account to compare

    print("\n" + "=" * 68)
    print("RESULTS  (n=%d known-good cases)" % n)
    print("  MATCH (agrees with DCAD)      : %d" % match)
    print("  MISMATCH                      : %d  (of which %d via address search*)" % (len(mism), len(addr_mism)))
    print("  RESOLVER-NULL (no confirm)    : %d  (excluded from the rate)" % nulls)
    if confirmed:
        print("  --")
        print("  Match rate (of %d confirmable): %.0f%%" % (confirmed, 100.0 * match / confirmed))
        print("  Mismatch rate (of %d)         : %.0f%%" % (confirmed, 100.0 * len(mism) / confirmed))
    if addr_mism:
        print("\n  ADDRESS-SEARCH MISMATCHES (DCAD's own address lookup disagrees with on-file —")
        print("  strong evidence the on-file/extracted account is garbled; verify by hand):")
        for x in addr_mism:
            print("    %-13s on-file=%s  DCAD-address=%s  | %s"
                  % (x["case"], x["truth"], x["found"], x["address"][:40]))
    out = Path("/private/tmp/claude-501/-Users-stephenlewis-Downloads-pcpeak-platform") \
        / "51b110a4-bc11-4185-b923-46ef40c96863" / "scratchpad" / "resolver_audit_result.json"
    try:
        out.write_text(json.dumps(results, indent=2))
        print("\n  Raw results: %s" % out)
    except Exception:
        pass


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    asyncio.run(run(n))
