#!/usr/bin/env python3
"""
Push locally-scraped cases up to the live platform.

Scraping is LOCAL (discover.py writes data/db/pcpeak.db); the cloud only serves
data. This is the missing local -> prod step: it diffs the local DB against the
live site and pushes what's new.

    python3 sync_to_prod.py --pending                 # list held (unapproved) cases
    python3 sync_to_prod.py --approve TX-26-00009      # approve, then sync (pushes it)
    python3 sync_to_prod.py --dry-run                  # preview, change nothing
    python3 sync_to_prod.py                            # push APPROVED new cases + events
    python3 sync_to_prod.py --update-existing          # also refresh approved cases
                                                       # already live (never rep_assigned)

PROD-APPROVAL GATE (the primary safety invariant):
  * A case is pushed ONLY if it is approved for prod: cases.prod_ready = 1.
    Scraping (discover.py) never sets it, so every new local case lands HELD
    (prod_ready=0) and a routine sync CANNOT promote work-in-progress leads.
    This is the structural fix for the 2026-07-13 36-case premature-sync
    incident — approval is a deliberate, explicit act, not a thing you must
    remember to hold back. Approve with --approve "<case,case>" (revoke with
    --unapprove); see the held set with --pending.
  * A case already LIVE on prod is implicitly approved — reconciled to
    prod_ready=1 at the start of every run so --update-existing keeps working
    for public cases. The gate only ever blocks NEW, unapproved local cases.

Other safety invariants (deliberate):
  * rep_assigned is NEVER sent. You assign reps on the live site; local doesn't
    know those, and create_case merges absent fields, so live assignments are
    preserved. New cases arrive Unassigned.
  * prod_ready is NEVER sent — it is a LOCAL approval signal, not prod state.
  * BPP / undetermined property types are excluded at the source (property_type
    'personal'/'unknown', case_track 'personal_property') — belt-and-suspenders
    alongside the approval gate.
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
# the live site; prod_ready is a LOCAL approval signal, not prod state (see invariants).
SKIP_CASE_FIELDS = {"id", "rep_assigned", "prod_ready"}


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        PROD + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)


# The single source-of-truth SQL predicate for "syncable to prod". Every push path
# runs through it, so no mode (default, --update-existing, --only) can bypass it.
#   - prod_ready=1 — APPROVED for prod. The primary gate (36-case-incident fix). New
#     scrapes default to 0 (held); approval is explicit (--approve) or implicit for
#     already-live cases (reconcile_prod_ready).
#   - property_type NOT 'personal'/'unknown', case_track NOT 'personal_property' —
#     BPP / undetermined held at the source. `IS NOT` (not `!=`) so NULL/legacy rows
#     are KEPT by the type filter (only explicit tags excluded); the prod_ready gate
#     still applies to them.
SYNCABLE_WHERE = (
    "prod_ready=1 "
    "AND property_type IS NOT 'personal' AND property_type IS NOT 'unknown' "
    "AND case_track IS NOT 'personal_property'")


def ensure_schema(db):
    """Self-heal the prod_ready column if this local DB predates the migration
    (discover.py can create/write the DB without the backend ever running init_db)."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(cases)").fetchall()}
    if "prod_ready" not in cols:
        db.execute("ALTER TABLE cases ADD COLUMN prod_ready INTEGER DEFAULT 0")
        db.commit()
        print("schema: added prod_ready column (default 0 = held)")


def reconcile_prod_ready(db, prod_nums):
    """A case already live on prod is, by definition, approved — mark it prod_ready=1
    so the gate never blocks refreshing a public case. Idempotent; returns count newly
    marked. (The gate's real job is blocking NEW, unapproved local cases.)"""
    prod_nums = [n for n in prod_nums if n]
    if not prod_nums:
        return 0
    qs = ",".join("?" * len(prod_nums))
    cur = db.execute(
        f"UPDATE cases SET prod_ready=1 WHERE case_number IN ({qs}) "
        f"AND (prod_ready IS NULL OR prod_ready=0)", prod_nums)
    db.commit()
    return cur.rowcount


def set_prod_ready(db, nums, value):
    """Approve (value=1) / revoke (value=0) named local cases. Returns the case numbers
    actually changed (present in local DB), so the caller can warn about unknown ones."""
    changed = []
    for cn in nums:
        cur = db.execute("UPDATE cases SET prod_ready=? WHERE case_number=?", [value, cn])
        if cur.rowcount:
            changed.append(cn)
    db.commit()
    return changed


def pending_cases(db):
    """Real-property local cases NOT yet approved for prod — the held set the gate
    is protecting. (Excludes BPP/unknown, which are held for other reasons.)"""
    rows = db.execute(
        "SELECT case_number FROM cases WHERE (prod_ready IS NULL OR prod_ready=0) "
        "AND property_type IS NOT 'personal' AND property_type IS NOT 'unknown' "
        "AND case_track IS NOT 'personal_property' AND case_number IS NOT NULL "
        "ORDER BY case_number").fetchall()
    return [r[0] for r in rows]


def local_cases(db):
    rows = db.execute("SELECT * FROM cases WHERE " + SYNCABLE_WHERE).fetchall()
    return {r["case_number"]: dict(r) for r in rows if r["case_number"]}


def excluded_counts(db):
    bpp = db.execute("SELECT COUNT(*) FROM cases WHERE property_type IS 'personal' "
                     "OR case_track IS 'personal_property'").fetchone()[0]
    unk = db.execute("SELECT COUNT(*) FROM cases WHERE property_type IS 'unknown'").fetchone()[0]
    return bpp, unk


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
    ap.add_argument("--only", default="",
                    help="Comma-separated case numbers — sync ONLY these (a precise, targeted "
                         "push; implies --update-existing for them). Everything else is untouched. "
                         "Still subject to the prod_ready gate — approve first (or in the same run).")
    ap.add_argument("--approve", default="",
                    help="Comma-separated case numbers to APPROVE for prod (set prod_ready=1), "
                         "then continue with the sync. The deliberate act that opens the gate.")
    ap.add_argument("--unapprove", default="",
                    help="Comma-separated case numbers to REVOKE approval (set prod_ready=0). "
                         "Does not remove anything already on prod; just holds it from future syncs.")
    ap.add_argument("--pending", action="store_true",
                    help="List real-property local cases still HELD (not approved for prod), then exit.")
    args = ap.parse_args()
    dry = args.dry_run

    def parse_nums(s):
        return [c.strip() for c in s.split(",") if c.strip()]

    if not DB_PATH.exists():
        print(f"ERROR: local DB not found at {DB_PATH}"); sys.exit(1)

    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    ensure_schema(db)

    # --pending: show the held set and exit (pure inspection).
    if args.pending:
        held = pending_cases(db)
        if held:
            print(f"HELD — not approved for prod ({len(held)}). Approve with "
                  f"--approve \"<case,...>\":")
            for cn in held:
                print(f"  · {cn}")
        else:
            print("No held cases — every real-property local case is approved for prod.")
        return

    # Fetch prod first — needed both for the sync diff and to reconcile already-live cases.
    try:
        status, prod_list = api("GET", "/api/cases")
    except Exception as e:
        print(f"ERROR: can't reach {PROD}: {e}"); sys.exit(1)
    prod = {c["case_number"]: c for c in prod_list}

    # Already live on prod ⇒ implicitly approved. Reconcile so --update-existing keeps working
    # for public cases; the gate then only blocks NEW, unapproved local cases.
    rec = reconcile_prod_ready(db, prod.keys())
    if rec:
        print(f"reconciled {rec} already-live case(s) → prod_ready=1")

    # --approve / --unapprove: the deliberate gate actions (honor --dry-run: show, don't write).
    for flag, val, verb in [(args.approve, 1, "approve"), (args.unapprove, 0, "unapprove")]:
        nums = parse_nums(flag)
        if not nums:
            continue
        if dry:
            print(f"DRY RUN — would {verb}: {', '.join(nums)}")
            continue
        changed = set_prod_ready(db, nums, val)
        unknown = [n for n in nums if n not in changed]
        if changed:
            print(f"{verb}d (prod_ready={val}): {', '.join(changed)}")
        if unknown:
            print(f"WARNING: {verb} case(s) not in local DB (ignored): {', '.join(unknown)}")

    # Gather the APPROVED, syncable local set (prod_ready=1 + real-property).
    local = local_cases(db)
    held = pending_cases(db)
    n_bpp, n_unk = excluded_counts(db)
    if held:
        print(f"HELD by prod-approval gate (not approved — use --approve): {len(held)}")
    if n_bpp:
        print(f"BPP structurally excluded (never synced — personal property): {n_bpp}")
    if n_unk:
        print(f"Undetermined property type held for manual review (not synced): {n_unk}")

    if args.only:
        want = set(parse_nums(args.only))
        # Distinguish "not in local at all" from "held (unapproved)" — the latter is the
        # gate doing its job, and the fix is --approve, not a silent skip.
        held_named = want & set(held)
        absent = want - set(local) - set(held)
        if held_named:
            print("NOTE: --only case(s) are HELD (not approved) — skipped. Add "
                  "--approve \"" + ",".join(sorted(held_named)) + "\" to push them: "
                  + ", ".join(sorted(held_named)))
        if absent:
            print("WARNING: --only case(s) not in local DB (ignored): " + ", ".join(sorted(absent)))
        local = {cn: v for cn, v in local.items() if cn in want}
        args.update_existing = True   # targeting existing prod cases → refresh them
        if not local:
            print("Nothing to sync — no matching approved local cases."); sys.exit(0)

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
            # Events BEFORE the case: create_case appends case_snapshots (the field-history diff) and
            # resolves a status change back to the docket line that caused it by matching docket_events
            # ALREADY PRESENT. Pushing events first makes that evidence link resolvable at write time
            # (durable), not a later recompute. FK enforcement is off, and pushes are additive/
            # idempotent, so events-before-case is safe even for a case prod hasn't seen yet.
            ev = push_events(cn, local_events(db, cn), dry)
            if not dry:
                api("POST", "/api/cases", case_payload(local[cn]))
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
            # Events before the case (see the NEW-case loop): lets create_case's snapshot diff link a
            # status change to its docket line at write time. This is the path a case Refresh takes.
            ev = push_events(cn, local_events(db, cn), dry)
            if not dry:
                api("POST", "/api/cases", case_payload(local[cn]))
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
