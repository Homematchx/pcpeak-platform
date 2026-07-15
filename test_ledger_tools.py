#!/usr/bin/env python3
"""Tests for evidence_gaps.py + ledger_scorecard.py against a synthetic export fixture. No network.

Both tools read the ledger export (case_snapshots + prediction_ledger). We build a fixture that
exercises every branch, run each tool with --file, and assert the reported numbers — so the
derivation-gap classification (baseline/initial excluded, non-evidence fields excluded, only
NULL-evidence status UPDATES flagged) and the accuracy math are pinned.

Run: python3 test_ledger_tools.py   (exit 0 = all green)
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
_results = []
def check(name, cond):
    _results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)


FIXTURE = {
    "case_snapshots": [
        # genesis rows — excluded (NULL by nature, not bugs)
        {"case_number": "TX-A", "source": "baseline", "field": "oos_date",
         "old_value": None, "new_value": "2026-01-01", "evidence_event_id": None, "changed_at": "t0", "batch_id": "b0"},
        {"case_number": "TX-A", "source": "initial", "field": "total_due_filing",
         "old_value": None, "new_value": "5000", "evidence_event_id": None, "changed_at": "t0", "batch_id": "b0"},
        # status UPDATE with a docket line → linked, not a gap
        {"case_number": "TX-B", "source": "update", "field": "oos_date",
         "old_value": None, "new_value": "2026-06-16", "evidence_event_id": 42,
         "evidence_desc": "ISSUE ORDER OF SALE", "changed_at": "t1", "batch_id": "b1"},
        # status UPDATE with NO docket line → GAP (derivation-bug candidate)
        {"case_number": "TX-C", "source": "update", "field": "oos_date",
         "old_value": "2026-01-01", "new_value": "2026-05-01", "evidence_event_id": None,
         "evidence_desc": None, "changed_at": "t2", "batch_id": "b2"},
        # non-evidence field update with null link → excluded (no docket evidence BY DESIGN)
        {"case_number": "TX-C", "source": "update", "field": "stage",
         "old_value": "pre_judgment", "new_value": "oos_issued", "evidence_event_id": None,
         "changed_at": "t2", "batch_id": "b2"},
        # another GAP on a different evidence field/case
        {"case_number": "TX-D", "source": "update", "field": "judgment_date",
         "old_value": None, "new_value": "2026-02-02", "evidence_event_id": None,
         "changed_at": "t3", "batch_id": "b3"},
        {"case_number": "TX-D", "source": "update", "field": "total_due_filing",
         "old_value": "100", "new_value": "200", "evidence_event_id": None,
         "changed_at": "t3", "batch_id": "b3"},
    ],
    "prediction_ledger": [
        {"case_number": "TX-1", "prediction_basis": "judged", "model_version": "v1",
         "outcome_type": "oos_issued", "error_days": 10, "projected_oos": "2026-06-01"},
        {"case_number": "TX-2", "prediction_basis": "judged", "model_version": "v1",
         "outcome_type": "oos_issued", "error_days": -20},
        {"case_number": "TX-3", "prediction_basis": "filed", "model_version": "v1",
         "outcome_type": "oos_issued", "error_days": 100},
        {"case_number": "TX-4", "prediction_basis": "judged", "model_version": "v2",
         "outcome_type": "oos_issued", "error_days": 5},
        {"case_number": "TX-5", "prediction_basis": "judged", "model_version": "v1",
         "outcome_type": "dismissed", "error_days": None},
        {"case_number": "TX-6", "prediction_basis": "filed", "model_version": "v1",
         "outcome_type": "expired_no_oos", "error_days": None},
        {"case_number": "TX-7", "prediction_basis": "judged", "model_version": "v1",
         "outcome_type": None, "error_days": None},   # still open
    ],
    "rep_actions": [],
    "counts": {"case_snapshots": 7, "prediction_ledger": 7, "rep_actions": 0},
}


def run_tool(script, fixture_path, *extra):
    p = subprocess.run([sys.executable, str(HERE / script), "--file", str(fixture_path), *extra],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def run():
    d = Path(tempfile.mkdtemp())
    fx = d / "export.json"
    fx.write_text(json.dumps(FIXTURE))

    # ── evidence_gaps.py ──
    rc, out = run_tool("evidence_gaps.py", fx)
    check("evidence_gaps exits 0", rc == 0)
    check("counts 3 evidence-eligible status updates", "status-field updates (evidence-eligible): 3" in out)
    check("counts 1 linked", "linked to a docket line              : 1" in out)
    check("counts 2 NULL-evidence gaps", "NULL evidence (derivation-bug candidates): 2" in out)
    check("names the two gap cases (TX-C oos_date, TX-D judgment_date)",
          "TX-C" in out and "TX-D" in out and "oos_date" in out and "judgment_date" in out)
    check("does NOT flag the linked case TX-B as a gap",
          "TX-B" not in out.split("NO TRACEABLE DOCKET EVENT")[-1])
    check("does NOT flag baseline/initial or non-evidence fields (stage/total_due_filing)",
          "stage" not in out.split("NO TRACEABLE")[-1] and
          "TX-A" not in out.split("NO TRACEABLE")[-1])
    check("summary line: 2 candidates across 2 cases",
          "2 derivation-bug candidate(s) across 2 case(s)" in out)

    # empty snapshots → clean message, exit 0
    empty = d / "empty.json"; empty.write_text(json.dumps({"case_snapshots": [], "prediction_ledger": []}))
    rc, out = run_tool("evidence_gaps.py", empty)
    check("evidence_gaps handles empty snapshots cleanly", rc == 0 and "No snapshots yet" in out)

    # ── ledger_scorecard.py ──
    rc, out = run_tool("ledger_scorecard.py", fx)
    check("ledger_scorecard exits 0", rc == 0)
    check("reports 7 ledger rows, 6 resolved / 1 open",
          "ledger rows: 7" in out and "resolved: 6" in out and "still open (awaiting an outcome): 1" in out)
    check("outcome mix shows oos_issued=4, dismissed=1, expired_no_oos=1",
          "oos_issued      : 4" in out and "dismissed       : 1" in out and "expired_no_oos  : 1" in out)
    # errors [10,-20,100,5]: mean abs = 33.75→34, median abs of [5,10,20,100]=15, bias mean=23.75→24
    check("all-bases accuracy n=4", "OOS-ISSUED ACCURACY (all bases) — n=4" in out)
    check("mean abs error 34 days", "mean abs error : 34 days" in out)
    check("median abs err 15 days", "median abs err : 15 days" in out)
    check("within 30/60/90 = 75/75/75", "within 30/60/90d: 75% / 75% / 75%" in out)
    check("bias +24 days (predicted early)", "bias           : +24 days" in out and "predicted early" in out)
    check("per-basis breakdown present (judged + filed)",
          "by basis: judged" in out and "by basis: filed" in out)
    check("multi model_version note shown", "multiple model_versions" in out)
    check("small-n frozen note shown", "too few to recalibrate CITY_DATA" in out)

    empty2 = d / "empty2.json"; empty2.write_text(json.dumps({"prediction_ledger": []}))
    rc, out = run_tool("ledger_scorecard.py", empty2)
    check("ledger_scorecard handles empty ledger cleanly", rc == 0 and "Ledger is empty" in out)

    print("-" * 56)
    total, passed = len(_results), sum(_results)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
