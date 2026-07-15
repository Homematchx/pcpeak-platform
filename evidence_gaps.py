#!/usr/bin/env python3
"""Derivation-bug detector — surface every case_snapshots status change with NO docket line behind it.

case_snapshots (Phase 0) links a status-field change (oos_date, oos_issued, judgment_*, sale_*) back to
the docket_events row that caused it. When that link is NULL on an UPDATE, the derived value moved on
unchanged raw data — a derivation bug, distinct from a real new event. This is exactly the diagnostic
case_snapshots was built to enable: "status changed with no traceable docket event behind it."

It reads the token-gated ledger export (the same read path backup_ledger.py / scorecard use), so it
runs against LIVE prod with no deploy. Or point it at a saved dump for offline use:

    export LEDGER_EXPORT_TOKEN=...                 # required for the live endpoint
    python3 evidence_gaps.py                       # query live prod
    python3 evidence_gaps.py --file data/backups/ledger-XX.json   # a backup_ledger.py dump
    python3 evidence_gaps.py --all-fields          # also list non-evidence field changes (context)

Only 'update' rows on EVIDENCE-eligible fields count as gaps — 'baseline'/'initial' rows are genesis
(NULL by nature, not a bug), and non-status fields (total_due_filing, etc.) have no docket evidence by
design, so neither is flagged. Exit 0 always (a report, not a gate).
"""
import argparse
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path
import importlib.util

BASE = os.environ.get("LEDGER_API_BASE", "https://taxforeclosureanalyzer.com").rstrip("/")

# The fields whose change is expected to be evidenced by a docket line — the SINGLE SOURCE OF TRUTH is
# the backend's EVIDENCE_KEYWORDS. Import it so this detector can never drift from what create_case
# actually resolves; fall back to a literal copy only if the backend import fails.
def _evidence_fields():
    try:
        p = Path(__file__).parent / "backend" / "main.py"
        spec = importlib.util.spec_from_file_location("bmain_ev", str(p))
        m = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(m)
        except SystemExit:
            pass
        return set(m.EVIDENCE_KEYWORDS.keys())
    except Exception:
        return {"oos_date", "oos_issued", "judgment_date", "judgment_type",
                "sale_scheduled_date", "sale_pulled_date"}

EVIDENCE_FIELDS = _evidence_fields()


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch_export(file=None):
    if file:
        return json.loads(Path(file).read_text())
    token = os.environ.get("LEDGER_EXPORT_TOKEN", "")
    if not token:
        print("ERROR: LEDGER_EXPORT_TOKEN not set (needed for the live endpoint). "
              "Or pass --file <dump.json>.")
        sys.exit(2)
    req = urllib.request.Request(BASE + "/api/ledger/export", headers={"X-Ledger-Token": token})
    with urllib.request.urlopen(req, timeout=60, context=_ctx()) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser(description="Surface case_snapshots status changes with NULL evidence.")
    ap.add_argument("--file", help="Read a saved ledger export JSON instead of the live endpoint.")
    ap.add_argument("--all-fields", action="store_true",
                    help="Also list non-evidence field changes lacking a link (context, not bugs).")
    args = ap.parse_args()

    export = fetch_export(args.file)
    snaps = export.get("case_snapshots", [])

    src = args.file or (BASE + "/api/ledger/export")
    print("=" * 72)
    print("  DERIVATION-BUG DETECTOR — status changes with no docket line behind them")
    print("=" * 72)
    print(f"  source: {src}")
    print(f"  case_snapshots rows total: {len(snaps)}")
    if not snaps:
        print("\n  No snapshots yet — nothing to check. (After deploy, init_db seeds baselines; a")
        print("  real gap only appears once a case is re-synced and a status field moves.)")
        return

    # A GAP = an 'update' to an EVIDENCE-eligible field with no resolved docket line. Baseline/initial
    # rows are genesis (NULL by nature); non-evidence fields never carry a link. Both are excluded.
    status_updates = [s for s in snaps if s.get("source") == "update" and s.get("field") in EVIDENCE_FIELDS]
    gaps = [s for s in status_updates if not s.get("evidence_event_id")]
    linked = len(status_updates) - len(gaps)

    print(f"  status-field updates (evidence-eligible): {len(status_updates)}")
    print(f"    ├─ linked to a docket line              : {linked}")
    print(f"    └─ NULL evidence (derivation-bug candidates): {len(gaps)}")

    if not gaps:
        print("\n  ✓ No derivation gaps — every status change traces to a docket line.")
    else:
        print("\n  ⚠ STATUS CHANGES WITH NO TRACEABLE DOCKET EVENT (investigate each — the derived")
        print("    value moved on unchanged raw data, or the evidence resolver missed a real line):\n")
        by_case = {}
        for g in gaps:
            by_case.setdefault(g.get("case_number", "?"), []).append(g)
        for cn in sorted(by_case):
            print(f"    {cn}")
            for g in sorted(by_case[cn], key=lambda r: (r.get("field",""), r.get("changed_at",""))):
                print(f"      · {g.get('field'):<20} {str(g.get('old_value')):>12} → "
                      f"{str(g.get('new_value')):<12}  @ {g.get('changed_at','?')}  (batch {str(g.get('batch_id'))[:8]})")

    if args.all_fields:
        other_null = [s for s in snaps if s.get("source") == "update"
                      and s.get("field") not in EVIDENCE_FIELDS and not s.get("evidence_event_id")]
        print(f"\n  (context) non-evidence field updates (no docket link BY DESIGN): {len(other_null)}")

    print("\n" + "=" * 72)
    print(f"  {len(gaps)} derivation-bug candidate(s) across "
          f"{len({g.get('case_number') for g in gaps})} case(s).")


if __name__ == "__main__":
    main()
