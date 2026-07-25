#!/usr/bin/env python3
"""Backfill DCAD property intel — re-scrape the DCAD detail page and merge the result into each
case's property_intel. Same shape as payment_backfill.py (disk-light, DCAD HTML only, no PDFs).

TWO recoveries, both traced from TX-26-00777 (5807 Morningside Ave):

  --silent  (DEFAULT)  Cases where the DCAD scrape produced NOTHING but recorded NO error. Those
                       stored every DCAD field empty, which is indistinguishable from a property
                       that genuinely has no data — so propose 422'd for "no living area" with no
                       way to tell a scrape failure from a real gap. Measured 14 of 247, ALL with
                       valid resolved accounts (case numbers cluster, consistent with one scrape
                       session where DCAD stopped responding mid-run).

  --baths              Cases missing bathrooms. DCAD renders "# Baths (Full/Half)  1/ 1" with a
                       SPACE after the slash; the old pattern required the digits adjacent, so it
                       never matched and 0 of 247 cases had baths (while 164 had bedrooms). Baths
                       feed the comp MatchScore, so every ARV match was quietly degraded. The
                       capture is fixed, but existing rows only pick it up on a re-scrape.

MERGE IS ADDITIVE AND NON-DESTRUCTIVE: a field is overwritten only when the re-scrape produced a
real value. A failed re-scrape can never blank data that is already good — and if the new scrape
also comes back empty, the explicit error is stored so the failure stays visible.

  --retry-errors       Every case carrying a recorded DCAD error — the pass to run AFTER a parser
                       fix (e.g. the 'no such group' crash on TX-23-00777 / TX-26-00782, whose
                       non-standard land-table layout hit a group-less regex). A genuine thin
                       account just fails again and keeps its error; a case that failed only
                       because the parser threw recovers; a stale error on a case that already has
                       data (parsed before the throw) is cleared. The reconciling stats show which.

  python3 intel_backfill.py --dry-run          # show what would be re-scraped
  python3 intel_backfill.py                    # the silent failures (DCAD empty, no error)
  python3 intel_backfill.py --retry-errors     # every case with a recorded DCAD error
  python3 intel_backfill.py --baths            # every case missing baths
  python3 intel_backfill.py --only TX-26-00782 # one case
"""
import argparse
import asyncio
import json
import re
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "data" / "db" / "pcpeak.db"

# A DCAD result carrying none of these parsed nothing usable (mirrors the guard in property_intel).
CORE = ("market_value", "living_area_sqft", "owners", "land_value", "legal_description")
# Never overwrite a good value with an empty one — only these are merged, and only when truthy.
MERGE_KEYS = [
    "market_value", "land_value", "improvement_value", "living_area_sqft", "total_area_sqft",
    "lot_area_sqft", "bedrooms", "bathrooms", "year_built", "effective_year_built", "actual_age",
    "building_class", "construction_type", "foundation", "roof_type", "roof_material",
    "exterior_wall", "heating", "air_condition", "stories", "depreciation_pct", "desirability",
    "legal_description", "owners", "ownership_history", "market_value_history",
    "taxable_value_history", "exemptions_history", "exemptions", "deed_transfer_date",
    "garage_sqft", "land_unit_price", "lot_depth_ft", "lot_frontage_ft", "zoning",
    "estimated_annual_taxes", "tax_rates", "owner_changes", "is_absentee",
    "mailing_differs_from_property", "no_homestead",
]


def first_account(raw):
    """First syntactically valid 17-char DCAD account (multi-tract rows are comma-joined)."""
    for part in re.split(r"[,;]\s*", (raw or "").strip()):
        if re.fullmatch(r"[0-9A-Za-z]{17}", part):
            return part
    return None


def select_targets(conn, mode, only):
    rows = conn.execute(
        "SELECT case_number, account_number, property_intel FROM cases "
        "WHERE property_intel IS NOT NULL "
        "  AND (case_track IS NULL OR case_track!='personal_property')"
    ).fetchall()
    out = []
    for r in rows:
        if only and r["case_number"] not in only:
            continue
        try:
            pi = json.loads(r["property_intel"])
        except Exception:
            continue
        acct = first_account(r["account_number"])
        if not acct:
            continue                      # no usable account — resolve_backlog.py's job, not ours
        if only:
            reason = "explicit --only"
        elif mode == "baths":
            if pi.get("bathrooms"):
                continue
            reason = "no baths"
        elif mode == "retry_errors":
            # Every case carrying a recorded DCAD error — the right pass to run AFTER a parser fix
            # (e.g. the 'no such group' crash). A genuine thin account will just fail again and keep
            # its error; a case that failed only because the parser threw will recover. A stale
            # error on a case that DID recover data (parsed before the throw) is cleared by the
            # re-scrape too. Reconciling stats make the split visible.
            err = (pi.get("errors") or {}).get("dcad")
            if not err:
                continue
            reason = f"recorded DCAD error: {str(err)[:40]}"
        else:
            err = (pi.get("errors") or {}).get("dcad")
            if any(pi.get(k) for k in CORE) or err:
                continue                  # has data, or already carries a real error
            reason = "DCAD empty with NO error (silent failure)"
        out.append((r["case_number"], acct, pi, reason))
    return out


def merge(pi, dcad):
    """Additive merge — only real values land. Returns (updated_pi, [changed field names])."""
    changed = []
    for k in MERGE_KEYS:
        v = dcad.get(k)
        if v in (None, "", [], {}):
            continue                      # never blank an existing value with an empty scrape
        if pi.get(k) != v:
            pi[k] = v
            changed.append(k)
    errs = pi.get("errors") or {}
    errs["dcad"] = dcad.get("error")      # keep the failure visible when it is still failing
    pi["errors"] = errs
    return pi, changed


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baths", action="store_true", help="target cases missing bathrooms")
    ap.add_argument("--retry-errors", action="store_true", dest="retry_errors",
                    help="re-scrape every case with a recorded DCAD error (run after a parser fix)")
    ap.add_argument("--only", help="comma-separated case numbers")
    ap.add_argument("--dry-run", action="store_true", help="show targets, scrape nothing")
    ap.add_argument("--limit", type=int, help="cap how many cases are re-scraped")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None
    mode = "baths" if args.baths else "retry_errors" if args.retry_errors else "silent"

    conn = sqlite3.connect(str(DB)); conn.row_factory = sqlite3.Row
    targets = select_targets(conn, mode, only)
    if args.limit:
        targets = targets[:args.limit]

    print(f"intel backfill [{mode}]: {len(targets)} case(s) to re-scrape")
    for cn, acct, _pi, reason in targets[:20]:
        print(f"  {cn}  {acct}  ({reason})")
    if len(targets) > 20:
        print(f"  … and {len(targets)-20} more")
    if args.dry_run:
        print("\n--dry-run: nothing scraped, nothing written.")
        return
    if not targets:
        return

    from playwright.async_api import async_playwright
    from property_intel import scrape_dcad

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    recovered = still_failing = unchanged = 0
    try:
        for cn, acct, pi, _reason in targets:
            try:
                dcad = await scrape_dcad(acct, browser)
            except Exception as e:
                print(f"  {cn}: scrape raised {e}")
                still_failing += 1
                continue
            pi, changed = merge(pi, dcad)
            conn.execute("UPDATE cases SET property_intel=? WHERE case_number=?",
                         [json.dumps(pi), cn])
            conn.commit()
            if dcad.get("error"):
                still_failing += 1
                print(f"  {cn}: STILL FAILING — {dcad['error'][:90]}")
            elif changed:
                recovered += 1
                gla, baths = pi.get("living_area_sqft"), pi.get("bathrooms")
                print(f"  {cn}: recovered {len(changed)} field(s)"
                      f"  gla={gla or '—'}  baths={baths or '—'}")
            else:
                unchanged += 1
                print(f"  {cn}: no change")
    finally:
        await browser.close(); await pw.stop(); conn.close()

    # Stats must reconcile (project standard) — every target lands in exactly one bucket.
    total = recovered + still_failing + unchanged
    print(f"\nrecovered {recovered} · still failing {still_failing} · unchanged {unchanged}"
          f"  → {total} of {len(targets)} "
          f"{'OK' if total == len(targets) else 'MISMATCH — some target was dropped'}")
    print("These are LOCAL DB writes. Push with: python3 sync_to_prod.py --update-existing "
          "--only \"<case,…>\"")


if __name__ == "__main__":
    asyncio.run(main())
