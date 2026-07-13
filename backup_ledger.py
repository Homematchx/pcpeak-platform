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
    """Deterministic fingerprint of both prod-owned tables: row counts + sha256 over
    canonically-ordered rows (the endpoint returns them ORDER BY id, so it's stable)."""
    pl = export.get("prediction_ledger", [])
    ra = export.get("rep_actions", [])
    blob = json.dumps({"prediction_ledger": pl, "rep_actions": ra}, sort_keys=True, default=str)
    return {"prediction_ledger": len(pl), "rep_actions": len(ra),
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
    print(f"  sha256                 : {fp0['sha256']}")

    # ── WRITE the dump (timestamped, append-only — never overwrite a prior backup) ──
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    dump_path = BACKUP_DIR / f"ledger-{ts}.json"
    dump_path.write_text(json.dumps(before, sort_keys=True, default=str))
    fp_dump = fingerprint(json.loads(dump_path.read_text()))

    # ── RE-READ (after) to prove the source is unchanged / snapshot-consistent ──
    after = fetch_export()
    fp1 = fingerprint(after)

    source_unchanged = (fp0 == fp1)
    dump_faithful = (fp_dump == fp0)

    print(f"\nDUMP written: {dump_path}")
    print(f"  dump prediction_ledger : {fp_dump['prediction_ledger']}")
    print(f"  dump rep_actions       : {fp_dump['rep_actions']}")
    print(f"  dump sha256            : {fp_dump['sha256']}")
    print("\nAFTER (source, re-read):")
    print(f"  prediction_ledger rows : {fp1['prediction_ledger']}")
    print(f"  rep_actions rows       : {fp1['rep_actions']}")
    print(f"  sha256                 : {fp1['sha256']}")
    print("\nVERIFICATION:")
    print(f"  source unchanged during backup (read-only / consistent) : {'PASS' if source_unchanged else 'FAIL'}")
    print(f"  dump faithful (fingerprint == source)                   : {'PASS' if dump_faithful else 'FAIL'}")

    ok = source_unchanged and dump_faithful
    print("\n" + ("BACKUP VERIFIED CLEAN" if ok else "BACKUP VERIFICATION FAILED — do not trust this dump"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
