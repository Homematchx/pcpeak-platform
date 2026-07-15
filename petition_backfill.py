#!/usr/bin/env python3
"""Backfill petition_href for cases the pre-fix ordering bug left NULL (diagnosis: commit c0030ee).

The bug: a pre-insert `UPDATE cases SET petition_href` no-op'd on first-scrape cases, so the captured
petition URL was lost for ~everything scraped before 2026-07-11. The fixed discover.py persists it via
save_to_db. A sample of 10 recovered 10/10 with the current code — zero live selector bugs — so the
whole remaining gap is backfillable by re-running the FIXED scraper with --force.

This DERIVES the missing set from the LIVE local DB at runtime (never a hardcoded list — the count
drifts as new cases are scraped), re-scrapes each with `discover.py --case X --force`, then reconciles:
how many recovered a petition_href vs are still NULL (candidate genuine misses, listed individually for
tracing — never averaged). Resumable: a case that already has a petition_href is skipped, so re-running
only hits what's still missing.

    python3 petition_backfill.py --dry-run       # list eligible, scrape NOTHING
    python3 petition_backfill.py --limit 10      # backfill the first 10 still-missing (batch a long run)
    python3 petition_backfill.py                 # backfill ALL still-missing real-property cases

Scraping is LOCAL and credit-spending (portal + Claude + enrichment per case); --limit lets you run it
in controlled batches. Real-property only — BPP/undetermined are excluded (no real-estate petition).
"""
import argparse
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("SYNC_DB", BASE_DIR / "data" / "db" / "pcpeak.db"))
# Overridable so tests inject a stub instead of the real credit-spending scraper.
DISCOVER_CMD = shlex.split(os.environ.get("SCRAPE_DISCOVER_CMD", "python3 discover.py"))


def _has_petition(row):
    return bool(row and row[0] and str(row[0]).strip())


def eligible_missing(db_path=None):
    """Real-property cases with no petition_href, from the live DB. Column-tolerant. Returns
    (eligible_case_numbers, total_missing_including_bpp, excluded_bpp_unknown)."""
    path = Path(db_path or DB_PATH)
    if not path.exists():
        raise FileNotFoundError(f"local DB not found at {path}")
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}
        base = "(petition_href IS NULL OR TRIM(petition_href)='') AND case_number IS NOT NULL"
        total = conn.execute(f"SELECT COUNT(*) FROM cases WHERE {base}").fetchone()[0]
        where = [base]
        if "property_type" in cols:
            where.append("property_type IS NOT 'personal' AND property_type IS NOT 'unknown'")
        if "case_track" in cols:
            where.append("case_track IS NOT 'personal_property'")
        rows = conn.execute(
            f"SELECT case_number FROM cases WHERE {' AND '.join(where)} ORDER BY case_number").fetchall()
        eligible = [r[0] for r in rows]
        return eligible, total, total - len(eligible)
    finally:
        conn.close()


def _petition_href(db_path, cn):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT petition_href FROM cases WHERE case_number=?", [cn]).fetchone()
    finally:
        conn.close()


def default_runner(cn):
    """Re-scrape one case with the FIXED scraper. Returns (rc, select_line)."""
    proc = subprocess.run(DISCOVER_CMD + ["--case", cn, "--force"],
                          capture_output=True, text=True, cwd=str(BASE_DIR), timeout=1200)
    m = re.search(r"petition select:.*", proc.stdout or "")
    return proc.returncode, (m.group(0).strip() if m else "(no 'petition select' line in output)")


def backfill(cases, runner=default_runner, db_path=None, log=print):
    """Re-scrape each case, then re-read petition_href to classify recovered vs still-null."""
    path = str(db_path or DB_PATH)
    recovered, still_null, errored = [], [], []
    for i, cn in enumerate(cases, 1):
        # resume: skip if it already has one (a prior run or a concurrent scrape filled it)
        if _has_petition(_petition_href(path, cn)):
            recovered.append(cn); log(f"  [{i}/{len(cases)}] {cn} — already has petition_href (skip)")
            continue
        try:
            rc, select_line = runner(cn)
        except Exception as e:  # noqa: BLE001 — one case failing must not abort the batch
            errored.append((cn, str(e))); log(f"  [{i}/{len(cases)}] {cn} — ERROR: {e}")
            continue
        if _has_petition(_petition_href(path, cn)):
            recovered.append(cn); log(f"  [{i}/{len(cases)}] {cn} — ✓ recovered")
        else:
            still_null.append((cn, select_line))
            log(f"  [{i}/{len(cases)}] {cn} — ⚠ STILL NULL | {select_line}")
    return {"recovered": recovered, "still_null": still_null, "errored": errored}


def main():
    ap = argparse.ArgumentParser(description="Backfill petition_href via --force re-scrape.")
    ap.add_argument("--dry-run", action="store_true", help="List eligible cases; scrape nothing.")
    ap.add_argument("--limit", type=int, default=0, help="Re-scrape at most N (batch a long run).")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    try:
        eligible, total_missing, excluded = eligible_missing(args.db)
    except FileNotFoundError as e:
        print(f"ERROR: {e}"); sys.exit(1)

    print("=" * 68)
    print("  PETITION_HREF BACKFILL")
    print("=" * 68)
    print(f"  DB: {args.db}")
    print(f"  missing petition_href (total): {total_missing}")
    print(f"  excluded (BPP / undetermined — no real-estate petition): {excluded}")
    print(f"  eligible to backfill (real-property): {len(eligible)}")
    todo = eligible[:args.limit] if args.limit and args.limit > 0 else eligible
    if args.limit and args.limit > 0:
        print(f"  --limit {args.limit}: this run targets {len(todo)} of {len(eligible)} "
              f"(re-run to continue; resumable)")

    if args.dry_run:
        print("\n  DRY RUN — would re-scrape (discover.py --case X --force):")
        for cn in todo:
            print(f"    {cn}")
        print(f"\n  {len(todo)} case(s) would be re-scraped. Nothing was changed.")
        return

    if not todo:
        print("\n  Nothing to backfill — every real-property case already has a petition_href.")
        return

    print(f"\n  Re-scraping {len(todo)} case(s) with --force (LOCAL, credit-spending)…\n")
    res = backfill(todo, db_path=args.db)

    print("\n" + "-" * 68)
    n = len(todo)
    print(f"  recovered: {len(res['recovered'])}   still NULL: {len(res['still_null'])}   "
          f"errored: {len(res['errored'])}")
    print(f"  reconcile: {len(res['recovered'])}+{len(res['still_null'])}+{len(res['errored'])} "
          f"= {len(res['recovered']) + len(res['still_null']) + len(res['errored'])}  (attempted {n})")
    if res["still_null"]:
        print("\n  ⚠ STILL NULL — trace individually (the 'petition select' line says found-nothing vs")
        print("    found-but-fetch-failed):")
        for cn, line in res["still_null"]:
            print(f"    {cn}   {line}")
    if res["errored"]:
        print("\n  errored (re-scrape failed — retry these):")
        for cn, e in res["errored"]:
            print(f"    {cn}: {e}")
    remaining = len(eligible) - len(todo)
    if remaining > 0:
        print(f"\n  {remaining} eligible case(s) not attempted this run (--limit). Re-run to continue.")
    print("=" * 68)


if __name__ == "__main__":
    main()
