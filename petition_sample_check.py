#!/usr/bin/env python3
"""Measure petition_href recovery after a --force re-scrape sample. LOCAL read-only, no network.

Pass the case numbers you re-scraped; it reports how many now have a petition_href (the empirical
(a)-ordering-bug-recovered rate) and lists any still-null cases individually — NOT averaged — so each
genuine miss (candidate (b)) can be traced on its own. Reads the local pcpeak.db that discover.py writes.

    python3 petition_sample_check.py TX-22-01443 TX-23-00423 ...
    SYNC_DB=/path/to/pcpeak.db python3 petition_sample_check.py <cases...>

For each still-null case, re-run `python3 discover.py --case <cn> --force` and note the
"petition select: ..." log line — it says whether the selector found NO petition-type document
(genuine (b)) or found one but the fetch failed (a different, fixable problem). Paste those back.
"""
import os
import sqlite3
import sys
from pathlib import Path

DB = Path(os.environ.get("SYNC_DB", Path(__file__).parent / "data" / "db" / "pcpeak.db"))


def check(case_numbers, db_path=None):
    path = Path(db_path or DB)
    if not path.exists():
        print(f"ERROR: local DB not found at {path}")
        return 2
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    recovered, still_null, absent = [], [], []
    for cn in case_numbers:
        row = conn.execute(
            "SELECT case_number, petition_href, property_address, "
            "COALESCE(last_agent_run, created_at) AS scraped FROM cases WHERE case_number=?",
            [cn]).fetchone()
        if not row:
            absent.append(cn)
        elif row["petition_href"] and str(row["petition_href"]).strip():
            recovered.append(dict(row))
        else:
            still_null.append(dict(row))
    conn.close()

    n = len(case_numbers)
    print("=" * 68)
    print("  PETITION_HREF RECOVERY — --force re-scrape sample")
    print("=" * 68)
    print(f"  sample size: {n}")
    print(f"  RECOVERED (now have petition_href): {len(recovered)} / {n}")
    print(f"  still NULL (candidate (b) — genuine selector miss): {len(still_null)}")
    if absent:
        print(f"  not in local DB (re-scrape may have failed to save): {len(absent)} — {', '.join(absent)}")

    if recovered:
        print("\n  ✓ recovered:")
        for r in recovered:
            print(f"    {r['case_number']}   last scraped {str(r['scraped'])[:16]}")
    if still_null:
        print("\n  ⚠ STILL NULL — trace each individually (do NOT average). For each, re-run")
        print("    `python3 discover.py --case <cn> --force` and capture the 'petition select:' line:")
        for r in still_null:
            print(f"    {r['case_number']}   {(r['property_address'] or '(no address)')[:44]}")

    rate = round(100 * len(recovered) / n) if n else 0
    print("\n" + "=" * 68)
    print(f"  RECOVERY RATE: {len(recovered)}/{n} ({rate}%).  "
          + ("→ run the full 81-case backfill." if not still_null and recovered
             else "→ trace the still-null cases before the full backfill." if still_null
             else "→ nothing recovered; investigate the re-scrape itself."))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 petition_sample_check.py <case_number> [<case_number> ...]")
        sys.exit(2)
    sys.exit(check(sys.argv[1:]))
