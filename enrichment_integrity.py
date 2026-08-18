#!/usr/bin/env python3
"""Enrichment-integrity increment (design §32) — resolve the accounts that block every adapter.

Two populations, one operation:
  A. the 18 cases whose PETITION names a reachable collector but that carry NO DCAD account, so no
     adapter can be queried at all (upstream enrichment gap, not an adapter limit);
  B. the 4 cases carrying a WRONG account — the enrichment-contamination residue (§17.2), where a
     re-sync would only re-push bad data because LOCAL is wrong too.

    python3 enrichment_integrity.py --dry-run     # list both populations, touch nothing
    python3 enrichment_integrity.py               # resolve + verify, write only what is proven

DISCIPLINE, because this is the lowest-certainty work in the arc:
  · resolution goes through `resolve_account_corroborated` — TWO independent signals, never a naive
    address hit. The 2026-07-11 audit measured naive address search returning a confidently-wrong
    parcel ~2% of the time, which is exactly how population B was created.
  · EVERY candidate is then verified against ACT's own `Property Site Address` for that account —
    an independent authority, street NUMBER as the discriminator (differing street WORDING is
    formatting, not contamination).
  · an account that fails either step is NOT written. The case stays `needs_lookup` and its payoff
    stays INDETERMINATE, which is the honest state.
"""
import argparse
import asyncio
import json
import re
import sqlite3
import ssl
import sys
import urllib.request
from pathlib import Path

import certifi

sys.path.insert(0, str(Path(__file__).parent))
import jurisdictions
import property_intel
from browser_env import chrome_path

DB = Path(__file__).parent / "data" / "db" / "pcpeak.db"
_CTX = ssl.create_default_context(cafile=certifi.where())

# §17.2 — wrong parcel enriched, wrong in LOCAL as well as prod, so never a plain re-sync.
CONTAMINATED = ["TX-24-00080", "TX-26-00086", "TX-26-00990", "TX-26-01093"]


def act_site_address(account):
    """ACT's own site address for an account — the independent authority we verify against."""
    url = f"https://www.dallasact.com/act_webdev/dallas/showdetail2.jsp?can={account}&ownerno=0"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                    context=_CTX, timeout=25) as f:
            h = f.read().decode("utf8", "ignore")
    except Exception:
        return None
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))
    m = re.search(r"Property Site Address:\s*(.*?)\s*Legal Description", t)
    return m.group(1).strip() if m else None


def _num(s):
    m = re.match(r"\s*(\d+)", (s or "").strip())
    return m.group(1) if m else None


def verify(account, case_address):
    """(ok, act_address). Street NUMBER is the discriminator — 'LYNNACRE' vs 'Lynna Cre' is
    formatting, a different house number is a different property."""
    site = act_site_address(account)
    if not site:
        return False, None
    a, b = _num(case_address), _num(site)
    return bool(a and b and a == b), site


def populations(conn):
    a, b = [], []
    roster = jurisdictions.load_gds_roster()
    for cn, addr, tb, pi, dfd in conn.execute(
            "SELECT case_number, property_address, tax_breakdown, property_intel, defendant FROM cases"):
        try:
            intel = json.loads(pi or "{}")
        except ValueError:
            intel = {}
        cad = (intel.get("account_number") or "").split(",")[0].strip()
        named = [c["collector"] for c in jurisdictions.petition_collectors(tb)]
        reach = [n for n in named
                 if (jurisdictions.resolve_collector(n, roster=roster) or {}).get("platform")
                 in jurisdictions.ADAPTERS]
        row = {"case": cn, "address": addr, "defendant": dfd, "cad": cad, "collectors": reach}
        if cn in CONTAMINATED:
            b.append(row)
        elif reach and not cad:
            a.append(row)
    return a, b


async def resolve_all(rows, write=True):
    from playwright.async_api import async_playwright
    conn = sqlite3.connect(DB)
    stats = {"corroborated": 0, "verified": 0, "written": 0,
             "uncorroborated": 0, "unresolved": 0, "failed_act": 0}
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=chrome_path())
        try:
            for r in rows:
                acct, conf, reason = await property_intel.resolve_account_corroborated(
                    r["address"], r["defendant"], browser)
                if not acct:
                    stats["unresolved"] += 1
                    print(f"  {r['case']:<14} UNRESOLVED      {reason[:64]}")
                    continue
                if conf == "corroborated":
                    stats["corroborated"] += 1
                # ACT SITE-ADDRESS VERIFICATION IS ITSELF THE SECOND SIGNAL — and a stronger one than
                # owner-name matching. The resolver grades a candidate `uncorroborated` when the DCAD
                # owner does not match the defendant, which is routine and often correct: 5221 Robin
                # Road resolved to the RIGHT account (ACT-confirmed) yet graded uncorroborated,
                # because the owner reads "VILLANUEVA LUIS A C & GELISTA HERLINDA M" against a
                # defendant of "HERLINDA M. GELISTA". Rejecting that would discard a verified-correct
                # answer. What the guard exists to stop is the ~2% CONFIDENTLY-WRONG parcel — and
                # that is exactly the case where ACT's own site address DISAGREES. So a candidate is
                # accepted on ACT agreement, never on the address search alone.
                ok, site = verify(acct, r["address"])
                if not ok:
                    stats["failed_act"] += 1
                    print(f"  {r['case']:<14} REJECTED        {acct} ({conf}) but ACT says "
                          f"{site!r} ≠ {(r['address'] or '')[:30]!r}")
                    continue
                stats["verified"] += 1
                print(f"  {r['case']:<14} RESOLVED        {acct}  [{conf}+ACT]  {site}")
                if write:
                    cur = conn.execute("SELECT property_intel FROM cases WHERE case_number=?",
                                       (r["case"],)).fetchone()
                    intel = json.loads(cur[0]) if cur and cur[0] else {}
                    intel["account_number"] = acct
                    conn.execute("UPDATE cases SET account_number=?, account_status='resolved', "
                                 "account_note=?, property_intel=? WHERE case_number=?",
                                 (acct, f"corroborated + ACT-verified {site}", json.dumps(intel), r["case"]))
                    conn.commit()
                    stats["written"] += 1
        finally:
            await browser.close()
    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=["nocad", "contaminated"])
    a = ap.parse_args()
    conn = sqlite3.connect(DB)
    no_cad, contaminated = populations(conn)
    conn.close()
    print(f"A. NO ACCOUNT — blocks every adapter : {len(no_cad)}")
    print(f"B. WRONG ACCOUNT — §17.2 residue     : {len(contaminated)}")
    rows = (no_cad if a.only == "nocad" else contaminated if a.only == "contaminated"
            else no_cad + contaminated)
    if a.dry_run:
        for r in rows:
            print(f"   {r['case']:<14} {(r['address'] or '')[:44]:<46} {r['collectors']}")
        print("   (dry run — nothing resolved, nothing written)")
        return
    print(f"\nresolving {len(rows)} …")
    s = asyncio.run(resolve_all(rows))
    print(f"\n  corroborated {s['corroborated']} · ACT-verified {s['verified']} · WRITTEN {s['written']}")
    print(f"  uncorroborated {s['uncorroborated']} · unresolved {s['unresolved']} · "
          f"rejected-by-ACT {s['failed_act']}")
    print("  everything not written stays needs_lookup → payoff INDETERMINATE (the honest state)")


if __name__ == "__main__":
    main()
