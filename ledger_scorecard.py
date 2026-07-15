#!/usr/bin/env python3
"""Prediction accuracy from the LIVE prediction_ledger — real predicted-vs-actual, as actually logged.

scorecard.py backtests the CURRENT model against outcomes by recomputing compute_projection over the
LOCAL case DB. This is different and complementary: it reports the accuracy of predictions AS THEY WERE
ACTUALLY MADE and RESOLVED — reading the prod-owned prediction_ledger (create_case logs each meaningful
prediction; reconcile() resolves it against the real outcome and stores signed error_days). So the
numbers here are genuine accumulated telemetry, not a re-scored snapshot.

Reads the token-gated ledger export (no deploy; same path as backup_ledger.py), or a saved dump:

    export LEDGER_EXPORT_TOKEN=...
    python3 ledger_scorecard.py                                   # live prod
    python3 ledger_scorecard.py --file data/backups/ledger-XX.json

error_days is signed = actual_oos - projected_oos: POSITIVE means the actual OOS landed LATER than
predicted (model predicted EARLY); negative = predicted LATE. Sample sizes are shown everywhere — with
the model frozen at small n, these are for reading reality, not for auto-recalibrating (see
[[city-data-frozen-sample-size]]). Exit 0 always (a report).
"""
import argparse
import json
import os
import ssl
import statistics
import sys
import urllib.request
from collections import Counter
from pathlib import Path

BASE = os.environ.get("LEDGER_API_BASE", "https://taxforeclosureanalyzer.com").rstrip("/")


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
        print("ERROR: LEDGER_EXPORT_TOKEN not set (needed for the live endpoint). Or pass --file.")
        sys.exit(2)
    req = urllib.request.Request(BASE + "/api/ledger/export", headers={"X-Ledger-Token": token})
    with urllib.request.urlopen(req, timeout=60, context=_ctx()) as r:
        return json.load(r)


def _acc_block(rows, label):
    """Accuracy stats over resolved oos_issued rows that carry a signed error_days."""
    errs = [r["error_days"] for r in rows if r.get("error_days") is not None]
    print(f"\n  {label} — n={len(errs)}")
    if not errs:
        print("    (no resolved oos_issued predictions with a scoreable error yet)")
        return
    ae = [abs(e) for e in errs]
    within = lambda k: round(100 * sum(1 for x in ae if x <= k) / len(ae))
    bias = round(statistics.mean(errs))
    print(f"    mean abs error : {round(statistics.mean(ae))} days")
    print(f"    median abs err : {round(statistics.median(ae))} days")
    print(f"    within 30/60/90d: {within(30)}% / {within(60)}% / {within(90)}%")
    print(f"    bias           : {'+' if bias >= 0 else ''}{bias} days "
          f"({'actual LATER than predicted (predicted early)' if bias > 0 else 'actual EARLIER (predicted late)' if bias < 0 else 'unbiased'})")


def main():
    ap = argparse.ArgumentParser(description="Accuracy report from the live prediction_ledger.")
    ap.add_argument("--file", help="Read a saved ledger export JSON instead of the live endpoint.")
    args = ap.parse_args()

    export = fetch_export(args.file)
    pl = export.get("prediction_ledger", [])

    print("=" * 72)
    print("  PREDICTION LEDGER SCORECARD — predicted vs actual (as logged & resolved)")
    print("=" * 72)
    print(f"  source: {args.file or (BASE + '/api/ledger/export')}")
    print(f"  ledger rows: {len(pl)}   cases: {len({r.get('case_number') for r in pl})}")
    if not pl:
        print("\n  Ledger is empty — no predictions logged yet. (create_case logs on each sync with a")
        print("  meaningful change; run a sync, then re-check.)")
        return

    resolved = [r for r in pl if r.get("outcome_type")]
    open_rows = [r for r in pl if not r.get("outcome_type")]
    print(f"  resolved: {len(resolved)}   still open (awaiting an outcome): {len(open_rows)}")

    # Outcome mix — how predictions actually ended.
    oc = Counter(r["outcome_type"] for r in resolved)
    if oc:
        print("\n  RESOLVED OUTCOMES:")
        for k in ("oos_issued", "dismissed", "sale", "expired_no_oos"):
            if oc.get(k):
                print(f"    {k:<16}: {oc[k]}")
        for k, v in oc.items():
            if k not in ("oos_issued", "dismissed", "sale", "expired_no_oos"):
                print(f"    {k:<16}: {v}")

    # Accuracy is only meaningful for oos_issued outcomes (a real OOS date to score against).
    scored = [r for r in resolved if r.get("outcome_type") == "oos_issued"]
    _acc_block(scored, "OOS-ISSUED ACCURACY (all bases)")

    # By prediction basis — accuracy differs sharply (judged is tighter than filed).
    bases = sorted({r.get("prediction_basis") for r in scored if r.get("prediction_basis")})
    if len(bases) > 1:
        for b in bases:
            _acc_block([r for r in scored if r.get("prediction_basis") == b], f"  by basis: {b}")

    # By model_version — a MODEL_VERSION bump starts a fresh prediction lineage; don't blend them.
    mvs = Counter(r.get("model_version") for r in scored)
    if len(mvs) > 1:
        print("\n  (predictions span multiple model_versions — accuracy blends them; "
              "treat a version bump as a fresh baseline)")
        for mv, n in mvs.most_common():
            print(f"    {mv}: {n}")

    if len(scored) < 10:
        print(f"\n  NOTE: n={len(scored)} scoreable OOS outcomes — too few to recalibrate CITY_DATA "
              f"(frozen until ≥40). This is for reading reality, not tuning constants.")
    print("\n" + "=" * 72)


if __name__ == "__main__":
    main()
