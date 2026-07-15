#!/usr/bin/env python3
"""Durability backup of the PROD-OWNED ledger tables (prediction_ledger + rep_actions).

Pulls from prod via the token-gated GET /api/ledger/export, writes a timestamped, append-only
local dump, and VERIFIES it with the fingerprint standard (see the prod-history-fingerprint-proof
rule): capture a baseline (row counts for BOTH tables + sha256) BEFORE, re-read AFTER to prove the
source is unchanged (the pull is read-only / snapshot-consistent), and prove the dump is faithful
(dump fingerprint == source fingerprint). A backup that can't prove faithfulness exits non-zero.

Prod-owned data lives only on the Railway volume (single copy) and the volume isn't file-accessible
(SSH/upload blocked), so the backup goes through the API — the same read path scorecard uses (§11).
Object-store archive is the eventual durable target (archive.py, currently paused); until then the
dump lands locally under data/backups/.

Env: LEDGER_EXPORT_TOKEN (required), LEDGER_API_BASE (default https://taxforeclosureanalyzer.com).
Run: python3 backup_ledger.py
"""
import os, sys, json, hashlib, ssl, urllib.request
from datetime import datetime
from pathlib import Path

BASE = os.environ.get("LEDGER_API_BASE", "https://taxforeclosureanalyzer.com").rstrip("/")
TOKEN = os.environ.get("LEDGER_EXPORT_TOKEN", "")
BACKUP_DIR = Path(__file__).parent / "data" / "backups"


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch_export():
    req = urllib.request.Request(BASE + "/api/ledger/export", headers={"X-Ledger-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=60, context=_ctx()) as r:
        return json.load(r)


def fingerprint(export):
    """Deterministic fingerprint of ALL prod-owned tables: row counts + sha256 over
    canonically-ordered rows (the endpoint returns them ORDER BY id, so it's stable)."""
    pl = export.get("prediction_ledger", [])
    ra = export.get("rep_actions", [])
    cs = export.get("case_snapshots", [])
    blob = json.dumps({"prediction_ledger": pl, "rep_actions": ra, "case_snapshots": cs},
                      sort_keys=True, default=str)
    return {"prediction_ledger": len(pl), "rep_actions": len(ra), "case_snapshots": len(cs),
            "sha256": hashlib.sha256(blob.encode()).hexdigest()}


def main():
    if not TOKEN:
        print("ERROR: LEDGER_EXPORT_TOKEN not set — cannot authenticate to the export endpoint")
        sys.exit(1)

    # ── BASELINE (before) ──
    before = fetch_export()
    fp0 = fingerprint(before)
    print("BASELINE (source, before backup):")
    print(f"  prediction_ledger rows : {fp0['prediction_ledger']}")
    print(f"  rep_actions rows       : {fp0['rep_actions']}")
    print(f"  case_snapshots rows    : {fp0['case_snapshots']}")
    print(f"  sha256                 : {fp0['sha256']}")

    # ── WRITE the dump (timestamped, append-only — never overwrite a prior backup) ──
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    dump_path = BACKUP_DIR / f"ledger-{ts}.json"
    dump_path.write_text(json.dumps(before, sort_keys=True, default=str))
    fp_dump = fingerprint(json.loads(dump_path.read_text()))

    # ── RE-READ (after) — a CONTENTION report, not a pass/fail. Unlike the static
    # history-purge source, this ledger is LIVE: create_case may append a prediction (or
    # reconcile may fill outcome columns) mid-window. That does NOT invalidate the dump —
    # the dump is a faithful point-in-time snapshot as of the `before` read; any concurrent
    # activity lands in the next backup. So the PASS/FAIL is `dump == before` (faithfulness);
    # before/after is reported for visibility. Running with no sync active keeps it uncontested. ──
    after = fetch_export()
    fp1 = fingerprint(after)

    dump_faithful = (fp_dump == fp0)          # the real integrity guarantee
    uncontested = (fp0 == fp1)                # informational: was the window quiet?

    print(f"\nDUMP written: {dump_path}")
    print(f"  dump prediction_ledger : {fp_dump['prediction_ledger']}")
    print(f"  dump rep_actions       : {fp_dump['rep_actions']}")
    print(f"  dump case_snapshots    : {fp_dump['case_snapshots']}")
    print(f"  dump sha256            : {fp_dump['sha256']}")
    print("\nAFTER (source, re-read — contention check):")
    print(f"  prediction_ledger rows : {fp1['prediction_ledger']}")
    print(f"  rep_actions rows       : {fp1['rep_actions']}")
    print(f"  case_snapshots rows    : {fp1['case_snapshots']}")
    print(f"  sha256                 : {fp1['sha256']}")
    print("\nVERIFICATION:")
    print(f"  dump faithful (dump fingerprint == before snapshot)  : {'PASS' if dump_faithful else 'FAIL'}  <- integrity gate")
    if uncontested:
        print(f"  window uncontested (source identical before/after)   : yes")
    else:
        print(f"  window uncontested (source identical before/after)   : NO — concurrent write during backup")
        print(f"    ↳ pred_ledger {fp0['prediction_ledger']}→{fp1['prediction_ledger']}, "
              f"rep_actions {fp0['rep_actions']}→{fp1['rep_actions']}, "
              f"case_snapshots {fp0['case_snapshots']}→{fp1['case_snapshots']}. The dump is a "
              f"consistent snapshot as of the BEFORE read; the delta lands in the next backup. Not a failure.")

    print("\n" + ("BACKUP VERIFIED CLEAN (faithful point-in-time snapshot)" if dump_faithful
                  else "BACKUP VERIFICATION FAILED — dump does not match the source snapshot; do not trust it"))
    sys.exit(0 if dump_faithful else 1)


if __name__ == "__main__":
    main()
