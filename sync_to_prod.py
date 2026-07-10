#!/usr/bin/env python3
"""
Push locally-scraped cases up to the live platform.

Scraping is LOCAL (discover.py writes data/db/pcpeak.db); the cloud only serves
data. This is the missing local -> prod step: it diffs the local DB against the
live site and pushes what's new.

    python3 sync_to_prod.py --dry-run        # preview, change nothing
    python3 sync_to_prod.py                  # push NEW cases + their events
    python3 sync_to_prod.py --update-existing # also refresh scraped fields on
                                              # cases already live (still never
                                              # touches rep_assigned)

Safety invariants (deliberate):
  * rep_assigned is NEVER sent. You assign reps on the live site; local doesn't
    know those, and create_case merges absent fields, so live assignments are
    preserved. New cases arrive Unassigned.
  * Additive only. A case that is on prod but not local is left alone (you can
    add cases via the live UI; those are prod-owned).
  * Events are de-duplicated client-side by (event_date, description) because the
    server's INSERT OR IGNORE can't dedupe (docket_events has no unique key).
  * Every case is isolated in try/except; one failure never aborts the run, and
    the final tally must reconcile (new + updated + skipped + failed == total).
"""
import argparse
import json
import sqlite3
import ssl
import sys
import urllib.request
import urllib.error
from pathlib import Path

import certifi

import os
PROD = os.environ.get("PROD_URL", "https://taxforeclosureanalyzer.com").rstrip("/")
DB_PATH = Path(os.environ.get("SYNC_DB", Path(__file__).parent / "data" / "db" / "pcpeak.db"))
CTX = ssl.create_default_context(cafile=certifi.where())

# Fields never pushed: id is prod's own autoincrement; rep_assigned is owned by
# the live site (see invariants above).
SKIP_CASE_FIELDS = {"id", "rep_assigned"}


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        PROD + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)


def local_cases(db):
    rows = db.execute("SELECT * FROM cases").fetchall()
    return {r["case_number"]: dict(r) for r in rows if r["case_number"]}


def local_events(db, case_number):
    rows = db.execute(
        "SELECT event_date, event_type, description, detail, is_new "
        "FROM docket_events WHERE case_number=? ORDER BY id", [case_number]
    ).fetchall()
    return [dict(r) for r in rows]


def case_payload(row):
    return {k: v for k, v in row.items() if k not in SKIP_CASE_FIELDS and v is not None}


def event_key(ev):
    # Content identity for dedupe. GET returns event_date/description; local rows
    # use the same names, so this key is stable across both sides.
    return ((ev.get("event_date") or "").strip(), (ev.get("description") or "").strip())


def push_events(case_number, evs, dry):
    """Push only local events prod doesn't already have. Returns count pushed."""
    if not evs:
        return 0
    try:
        _, prod_evs = api("GET", f"/api/events/{case_number}")
    except Exception:
        prod_evs = []
    have = {event_key(e) for e in (prod_evs or [])}
    missing = [e for e in evs if event_key(e) not in have]
    if not missing or dry:
        return len(missing)
    body = [{
        "date": e.get("event_date"), "type": e.get("event_type"),
        "description": e.get("description"), "detail": e.get("detail", ""),
        "is_new": e.get("is_new", 0),
    } for e in missing]
    api("POST", f"/api/events/{case_number}", body)
    return len(missing)


def main():
    ap = argparse.ArgumentParser(description="Sync locally-scraped cases to the live platform.")
    ap.add_argument("--dry-run", action="store_true", help="Preview only; change nothing.")
    ap.add_argument("--update-existing", action="store_true",
                    help="Also refresh scraped fields on cases already live (never rep_assigned).")
    args = ap.parse_args()
    dry = args.dry_run

    if not DB_PATH.exists():
        print(f"ERROR: local DB not found at {DB_PATH}"); sys.exit(1)

    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    local = local_cases(db)

    try:
        status, prod_list = api("GET", "/api/cases")
    except Exception as e:
        print(f"ERROR: can't reach {PROD}: {e}"); sys.exit(1)
    prod = {c["case_number"]: c for c in prod_list}

    new_nums = [cn for cn in local if cn not in prod]
    existing_nums = [cn for cn in local if cn in prod]

    mode = "DRY RUN — no changes will be made" if dry else "LIVE — pushing to prod"
    print(f"── sync_to_prod ── {mode}")
    print(f"local cases: {len(local)}   prod cases: {len(prod)}")
    print(f"new (local only): {len(new_nums)}   already on prod: {len(existing_nums)}")
    if args.update_existing:
        print("existing cases WILL be refreshed (scraped fields only; rep_assigned preserved)")
    print("-" * 60)

    created = updated = skipped = failed = events_pushed = 0
    errors = []

    # 1) New cases
    for cn in sorted(new_nums):
        try:
            if not dry:
                api("POST", "/api/cases", case_payload(local[cn]))
            ev = push_events(cn, local_events(db, cn), dry)
            events_pushed += ev
            created += 1
            print(f"  + NEW  {cn}  (+{ev} events)")
        except Exception as e:
            failed += 1; errors.append((cn, str(e)))
            print(f"  ! FAIL {cn}: {e}")

    # 2) Existing cases
    for cn in sorted(existing_nums):
        if not args.update_existing:
            skipped += 1
            continue
        try:
            if not dry:
                api("POST", "/api/cases", case_payload(local[cn]))
            ev = push_events(cn, local_events(db, cn), dry)
            events_pushed += ev
            updated += 1
            tag = "would refresh" if dry else "refreshed"
            if ev or not dry:
                print(f"  ~ {tag} {cn}  (+{ev} events)")
        except Exception as e:
            failed += 1; errors.append((cn, str(e)))
            print(f"  ! FAIL {cn}: {e}")

    print("-" * 60)
    verb = "would create" if dry else "created"
    print(f"{verb}: {created}   updated: {updated}   skipped(existing): {skipped}   failed: {failed}")
    print(f"events {'would push' if dry else 'pushed'}: {events_pushed}")

    # Tally must reconcile against the local set we considered.
    accounted = created + updated + skipped + failed
    print(f"reconcile: {created}+{updated}+{skipped}+{failed} = {accounted}  (local total {len(local)})")
    if accounted != len(local):
        print("WARNING: tally does not reconcile — investigate.")
    if errors:
        print("\nErrors:")
        for cn, e in errors:
            print(f"  {cn}: {e}")

    # 3) Verify live count after a real run.
    if not dry and created:
        try:
            _, after = api("GET", "/api/cases")
            expected = len(prod) + created
            print(f"\nverify: prod now {len(after)} cases (expected {expected}) "
                  f"{'OK' if len(after) == expected else 'MISMATCH — check errors above'}")
        except Exception as e:
            print(f"verify failed: {e}")


if __name__ == "__main__":
    main()
