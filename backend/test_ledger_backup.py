#!/usr/bin/env python3
"""Step-4 tests: the token-gated GET /api/ledger/export endpoint + backup_ledger.py's fingerprint
logic. Proves: fail-CLOSED when the token env is unset (503, never open); 401 on a bad token; 200
+ correct rows on the right token; and the fingerprint is deterministic + detects any change (the
basis of the before/after + dump-faithful backup verification).

Run: python3 backend/test_ledger_backup.py   (exit 0 = all green)
"""
import sys, tempfile, os, json
from pathlib import Path

_d = Path(tempfile.mkdtemp())
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))                    # repo root (backup_ledger) — lower priority
sys.path.insert(0, str(Path(__file__).parent))   # backend/ (main) — inserted last → index 0, wins
import main
main.DB_PATH = _d / "pcpeak.db"
main.LEDGER_DB_PATH = _d / "ledger.db"
from fastapi.testclient import TestClient
import backup_ledger

_results = []
def check(name, cond):
    _results.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

TOKEN = "test-secret-token-abc123"

def seed():
    with main.get_db() as db:
        db.execute("INSERT INTO ledger.prediction_ledger (case_number,model_version,prediction_basis,projected_oos,confidence_pct,input_hash) VALUES ('TX-1','v1','judged','2026-09-01',55,'h1')")
        db.execute("INSERT INTO ledger.prediction_ledger (case_number,model_version,prediction_basis,projected_oos,confidence_pct,input_hash) VALUES ('TX-2','v1','filed','2026-10-01',40,'h2')")
        db.execute("INSERT INTO ledger.rep_actions (case_number,rep,action_type) VALUES ('TX-1','alice','contact_made')")

def run():
    main.init_db()
    seed()
    client = TestClient(main.app)

    # ── 1. Fail-CLOSED: no LEDGER_EXPORT_TOKEN configured → 503 (never open by accident) ──
    os.environ.pop("LEDGER_EXPORT_TOKEN", None)
    r = client.get("/api/ledger/export", headers={"X-Ledger-Token": "anything"})
    check("unconfigured token env → 503 (fail-closed, not open)", r.status_code == 503)

    # ── 2. Configured, but wrong / missing token → 401 ──
    os.environ["LEDGER_EXPORT_TOKEN"] = TOKEN
    check("wrong token → 401", client.get("/api/ledger/export", headers={"X-Ledger-Token": "nope"}).status_code == 401)
    check("missing token → 401", client.get("/api/ledger/export").status_code == 401)

    # ── 3. Correct token → 200 + both tables with correct counts ──
    r = client.get("/api/ledger/export", headers={"X-Ledger-Token": TOKEN})
    check("correct token → 200", r.status_code == 200)
    body = r.json() if r.status_code == 200 else {}
    check("export has both tables with correct row counts",
          len(body.get("prediction_ledger", [])) == 2 and len(body.get("rep_actions", [])) == 1)

    # ── 4. Fingerprint is deterministic + detects any change (backup-verification basis) ──
    fp = backup_ledger.fingerprint(body)
    check("fingerprint counts match (2 / 1)", fp["prediction_ledger"] == 2 and fp["rep_actions"] == 1)
    check("fingerprint deterministic (same input → same sha256)", backup_ledger.fingerprint(body)["sha256"] == fp["sha256"])
    mutated = json.loads(json.dumps(body))
    mutated["prediction_ledger"][0]["confidence_pct"] = 99
    check("fingerprint changes when a row changes", backup_ledger.fingerprint(mutated)["sha256"] != fp["sha256"])

    # ── 5. Dump round-trip is faithful (write → re-read → identical fingerprint) ──
    dump = _d / "dump.json"
    dump.write_text(json.dumps(body, sort_keys=True, default=str))
    check("dump written from export re-reads to an identical fingerprint",
          backup_ledger.fingerprint(json.loads(dump.read_text()))["sha256"] == fp["sha256"])

    os.environ.pop("LEDGER_EXPORT_TOKEN", None)
    print(f"\n{sum(_results)}/{len(_results)} checks passed")
    return all(_results)

if __name__ == "__main__":
    sys.exit(0 if run() else 1)
