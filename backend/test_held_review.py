#!/usr/bin/env python3
"""Tests for the held-for-review endpoints (backend/main.py). No network.

The held-review feature lets the browser LIST cases awaiting approval and PUBLISH one by asking the
Mac worker to run the real `sync_to_prod.py --approve`. The browser never touches prod case data —
these endpoints only (a) mirror the Mac's held set for display, (b) report worker liveness, and
(c) enqueue an approve-JOB. We assert the invariants that matter:

  * fail-closed auth on every endpoint, and the RIGHT token role (trigger vs worker);
  * /api/held/sync is a FULL REPLACE (a just-approved case drops off; new held cases appear);
  * approve NEVER creates a case — it 404s for anything not in the held mirror, and only ever
    enqueues an {"approve": CN} job (deduped in-flight, capped) for the worker to run;
  * worker liveness flips online→offline purely from the heartbeat age (the whole feature depends
    on the worker running, so the UI must be able to say so).

Run: python3 backend/test_held_review.py   (exit 0 = all green)
"""
import os
import sys
import tempfile
from pathlib import Path

_d = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent))
import main
main.DB_PATH = _d / "pcpeak.db"
main.LEDGER_DB_PATH = _d / "ledger.db"
from fastapi.testclient import TestClient

TRIG = {"X-Scrape-Token": "trig-secret"}
WORK = {"X-Worker-Token": "work-secret"}

_results = []
def check(name, cond):
    _results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)


def _held_payload(cases):
    return {"held": [{"case_number": cn, "property_address": addr, "defendant": dfn,
                      "total_due": due, "property_type": "real", "case_track": "oos_timing",
                      "account_status": acct}
                     for (cn, addr, dfn, due, acct) in cases]}


def run():
    main.init_db()
    main.MAX_QUEUED_SCRAPES = 3
    with TestClient(main.app) as c:
        # ── fail-closed: no server tokens configured → 503 on every held endpoint ──
        os.environ.pop("SCRAPE_TRIGGER_TOKEN", None)
        os.environ.pop("SCRAPE_WORKER_TOKEN", None)
        check("GET /api/held is 503 when trigger token unset (fail-closed)",
              c.get("/api/held", headers=TRIG).status_code == 503)
        check("POST /api/held/sync is 503 when worker token unset (fail-closed)",
              c.post("/api/held/sync", json={"held": []}, headers=WORK).status_code == 503)
        check("POST /api/worker/heartbeat is 503 when worker token unset (fail-closed)",
              c.post("/api/worker/heartbeat", json={"worker_id": "w1"}, headers=WORK).status_code == 503)
        check("POST approve is 503 when trigger token unset (fail-closed)",
              c.post("/api/held/TX-26-00009/approve", headers=TRIG).status_code == 503)

        os.environ["SCRAPE_TRIGGER_TOKEN"] = "trig-secret"
        os.environ["SCRAPE_WORKER_TOKEN"] = "work-secret"

        # ── token ROLES: held list is trigger-only; sync/heartbeat are worker-only ──
        check("GET /api/held with NO token → 401", c.get("/api/held").status_code == 401)
        check("GET /api/held with worker token (wrong role) → 401",
              c.get("/api/held", headers={"X-Scrape-Token": "work-secret"}).status_code == 401)
        check("held/sync with trigger token (wrong role) → 401",
              c.post("/api/held/sync", json={"held": []},
                     headers={"X-Worker-Token": "trig-secret"}).status_code == 401)
        check("heartbeat with trigger token (wrong role) → 401",
              c.post("/api/worker/heartbeat", json={"worker_id": "w1"},
                     headers={"X-Worker-Token": "trig-secret"}).status_code == 401)

        # ── liveness: no heartbeat yet → offline; after a heartbeat → online ──
        j = c.get("/api/held", headers=TRIG).json()
        check("no heartbeat → worker offline, empty held list",
              j["worker"]["online"] is False and j["held"] == [])
        check("heartbeat → 200", c.post("/api/worker/heartbeat", json={"worker_id": "mac"},
                                        headers=WORK).status_code == 200)
        j = c.get("/api/held", headers=TRIG).json()
        check("after heartbeat → worker online (age < WORKER_ONLINE_SECS)",
              j["worker"]["online"] is True and j["worker"]["age_secs"] is not None)

        # ── held/sync full-replace: push a set, then a smaller set drops the missing one ──
        r = c.post("/api/held/sync", json=_held_payload([
            ("TX-26-00009", "100 Main St", "DOE, JOHN", 12818.78, "resolved"),
            ("TX-26-00010", "200 Oak Ave", "ROE, JANE", 4200.0, "needs_lookup"),
        ]), headers=WORK)
        check("held/sync → reports 2 held", r.status_code == 200 and r.json()["held"] == 2)
        j = c.get("/api/held", headers=TRIG).json()
        check("GET /api/held lists both, sorted, with preview fields",
              [h["case_number"] for h in j["held"]] == ["TX-26-00009", "TX-26-00010"]
              and j["held"][0]["property_address"] == "100 Main St"
              and abs(j["held"][0]["total_due"] - 12818.78) < 1e-6)
        check("held rows carry approving=False by default", all(h["approving"] is False for h in j["held"]))

        # full replace: syncing only ONE case drops the other (mirrors an approve dropping it locally)
        c.post("/api/held/sync", json=_held_payload([
            ("TX-26-00010", "200 Oak Ave", "ROE, JANE", 4200.0, "needs_lookup")]), headers=WORK)
        j = c.get("/api/held", headers=TRIG).json()
        check("held/sync is a FULL REPLACE (00009 dropped, only 00010 remains)",
              [h["case_number"] for h in j["held"]] == ["TX-26-00010"])
        # empty sync clears everything
        c.post("/api/held/sync", json={"held": []}, headers=WORK)
        check("empty held/sync clears the mirror", c.get("/api/held", headers=TRIG).json()["held"] == [])

        # ── approve: only a case in the held mirror can be approved (never creates data) ──
        check("approve a case NOT in held → 404 (never creates)",
              c.post("/api/held/TX-26-99999/approve", headers=TRIG).status_code == 404)
        check("approve with worker token (wrong role) → 401",
              c.post("/api/held/TX-26-00010/approve",
                     headers={"X-Scrape-Token": "work-secret"}).status_code == 401)

        # put a case back, then approve it → enqueues an {"approve": CN} job
        c.post("/api/held/sync", json=_held_payload([
            ("TX-26-00010", "200 Oak Ave", "ROE, JANE", 4200.0, "needs_lookup")]), headers=WORK)
        r = c.post("/api/held/tx-26-00010/approve", headers=TRIG)   # lower-case in → normalized
        check("approve a held case → 200 queued, normalized cn",
              r.status_code == 200 and r.json()["status"] == "queued" and r.json()["approve"] == "TX-26-00010")
        job_id = r.json()["job_id"]
        gp = c.get(f"/api/scrape-jobs/{job_id}", headers=TRIG).json()
        check("approve enqueues ONLY an {approve: CN} request (no case data)",
              gp["request"] == {"approve": "TX-26-00010"} and gp["label"] == "approve TX-26-00010")

        # ── inflight flag: the held list shows the case as approving while its job is open ──
        j = c.get("/api/held", headers=TRIG).json()
        row = next(h for h in j["held"] if h["case_number"] == "TX-26-00010")
        check("held row shows approving=True while an approve job is in-flight", row["approving"] is True)

        # ── dedup: a second approve for the same in-flight case → 409 ──
        check("duplicate approve while in-flight → 409",
              c.post("/api/held/TX-26-00010/approve", headers=TRIG).status_code == 409)

        # ── cap: fill the queue, then approve → 429 (cap is shared with scrape enqueue) ──
        c.post("/api/held/sync", json=_held_payload([
            ("TX-26-00021", "1 A St", "A", 1.0, "resolved"),
            ("TX-26-00022", "2 B St", "B", 2.0, "resolved"),
            ("TX-26-00023", "3 C St", "C", 3.0, "resolved")]), headers=WORK)
        # 1 approve job already queued (00010). Add until cap (3), then next → 429.
        c.post("/api/held/TX-26-00021/approve", headers=TRIG)
        c.post("/api/held/TX-26-00022/approve", headers=TRIG)
        check("approve past the shared queue cap → 429",
              c.post("/api/held/TX-26-00023/approve", headers=TRIG).status_code == 429)

        # ── the worker CLAIMS the approve job and it flows through the same state machine ──
        # drain to find the approve job for 00010
        claimed = None
        for _ in range(6):
            jj = c.post("/api/scrape-jobs/claim", json={"worker_id": "mac"}, headers=WORK).json()["job"]
            if jj is None:
                break
            if jj["request"] == {"approve": "TX-26-00010"}:
                claimed = jj
        check("worker can claim the approve job (same queue)", claimed is not None)
        # once claimed, it's no longer 'queued' → inflight stays True (claimed is in-flight), and a
        # done patch stamps finished_at like any job.
        done = c.patch(f"/api/scrape-jobs/{claimed['id']}",
                       json={"status": "done", "result": {"approved": "TX-26-00010"}},
                       headers=WORK).json()
        check("approve job → done stamps finished_at",
              done["status"] == "done" and bool(done["finished_at"]))

    print("-" * 56)
    total, passed = len(_results), sum(_results)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
