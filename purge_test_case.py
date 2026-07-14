#!/usr/bin/env python3
"""Guarded deletion of a stub/test case from the LOCAL DB.

Test scrapes (e.g. the scrape-trigger stub, or a throwaway TX-99-xxxxx) leave a fake case
sitting in the held queue. This removes ONE entirely (its `cases` row + any `docket_events`),
but only if it is unmistakably a throwaway — the same "constraint a real row fails" discipline
as the BPP delete guard (backend `DELETE /api/cases/{cn}` gated on property_type='personal'):

    deletable ONLY if the case is  (a) NOT live on prod,
                                   (b) held (prod_ready != 1),
                                   (c) contentless (no property_address / ai_memo).

A real case — even a genuinely-held local lead — is on prod and/or carries real content, so it
is REFUSED. The constraint is also carried in the DELETE's WHERE clause (DB-level, not caller-
trusted). This is a LOCAL cleanup only — it never touches prod.

    python3 purge_test_case.py TX-99-00001            # delete the stub
    python3 purge_test_case.py TX-99-00001 --dry-run  # show the decision, change nothing

Verified by test_purge_test_case.py (pure-DB, no network).
"""
import argparse
import json
import os
import sqlite3
import ssl
import sys
import urllib.request
from pathlib import Path

import certifi

PROD = os.environ.get("PROD_URL", "https://taxforeclosureanalyzer.com").rstrip("/")
DB_PATH = Path(os.environ.get("SYNC_DB", Path(__file__).parent / "data" / "db" / "pcpeak.db"))
CTX = ssl.create_default_context(cafile=certifi.where())


def is_throwaway(row, on_prod):
    """Pure decision — returns (ok, reason). A real case fails at least one clause."""
    if row is None:
        return (False, "not found")
    if on_prod:
        return (False, "REFUSED: live on prod")
    if (row["prod_ready"] or 0) == 1:
        return (False, "REFUSED: approved (prod_ready=1)")
    if (row["property_address"] or "").strip() or (row["ai_memo"] or "").strip():
        return (False, "REFUSED: has real content (address/memo) — not a throwaway")
    return (True, "deletable throwaway")


def _row(db, cn):
    return db.execute(
        "SELECT case_number, property_address, ai_memo, prod_ready FROM cases WHERE case_number=?",
        [cn]).fetchone()


def purge(db, case_number, prod_nums, dry=False):
    """Guarded delete against an open DB connection + the set of prod case numbers. No network
    here (prod_nums is injected), so the whole decision + delete is testable in isolation."""
    row = _row(db, case_number)
    ok, reason = is_throwaway(row, case_number in prod_nums)
    before = db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    result = {"case_number": case_number, "ok": ok, "reason": reason, "dry": dry,
              "before": before, "after": before, "deleted_cases": 0, "deleted_events": 0,
              "gone": row is None}
    if not ok or dry:
        return result
    # Constraint ALSO in the WHERE clause (DB-level, not caller-trusted) — an approved row
    # (prod_ready=1) matches 0 rows here even if the guard above were somehow bypassed.
    c = db.execute("DELETE FROM cases WHERE case_number=? AND prod_ready IS NOT 1", [case_number])
    ev = db.execute("DELETE FROM docket_events WHERE case_number=?", [case_number])
    db.commit()
    result["deleted_cases"] = c.rowcount
    result["deleted_events"] = ev.rowcount
    result["after"] = db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    result["gone"] = _row(db, case_number) is None
    return result


def fetch_prod_nums():
    with urllib.request.urlopen(PROD + "/api/cases", timeout=60, context=CTX) as r:
        return {c["case_number"] for c in json.load(r)}


def main():
    ap = argparse.ArgumentParser(description="Guarded delete of a stub/test case from the local DB.")
    ap.add_argument("case_number")
    ap.add_argument("--dry-run", action="store_true", help="Show the decision; change nothing.")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: local DB not found at {DB_PATH}"); sys.exit(1)
    try:
        prod_nums = fetch_prod_nums()
    except Exception as e:
        print(f"ERROR: can't reach {PROD} to confirm the case isn't live: {e}"); sys.exit(1)

    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    row = _row(db, args.case_number)
    print("row:", dict(row) if row else None, "| on_prod:", args.case_number in prod_nums)
    res = purge(db, args.case_number, prod_nums, dry=args.dry_run)
    db.close()

    if not res["ok"]:
        print(res["reason"])
        sys.exit(0 if res["reason"] == "not found" else 1)
    if res["dry"]:
        print(f"DRY RUN — would delete {args.case_number} (throwaway); change nothing.")
        return
    print(f"deleted: {res['deleted_cases']} case row, {res['deleted_events']} event row(s)")
    print(f"verify: cases {res['before']} -> {res['after']} (Δ{res['before'] - res['after']}); "
          f"{args.case_number} gone: {res['gone']}")


if __name__ == "__main__":
    main()
