#!/usr/bin/env python3
"""Tests for the scrape-trigger queue endpoints (backend/main.py). No network.

Covers the async state machine end-to-end at the API layer — the exact place the earlier
"Run button" bugs lived (a completed_at/finished_at column mismatch; a status string the poller
never matched). We drive a job queued -> claimed -> running -> done and ASSERT the real columns
(status, claimed_at, finished_at) are set, so a column/spelling drift fails here instead of
silently in a worker. Also: fail-closed auth, both token roles, dedup, queue cap, and an atomic
claim that can't double-grant.

Run: python3 backend/test_scrape_jobs.py   (exit 0 = all green)
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


def run():
    main.init_db()
    # Shrink the cap so the queue-full test doesn't need 20 inserts.
    main.MAX_QUEUED_SCRAPES = 3
    with TestClient(main.app) as c:
        # ── fail-closed: no server token configured → 503, never open ──
        os.environ.pop("SCRAPE_TRIGGER_TOKEN", None)
        os.environ.pop("SCRAPE_WORKER_TOKEN", None)
        r = c.post("/api/scrape-jobs", json={"case_number": "TX-26-00009"}, headers=TRIG)
        check("enqueue is 503 when SCRAPE_TRIGGER_TOKEN unset (fail-closed)", r.status_code == 503)
        r = c.post("/api/scrape-jobs/claim", json={}, headers=WORK)
        check("claim is 503 when SCRAPE_WORKER_TOKEN unset (fail-closed)", r.status_code == 503)

        os.environ["SCRAPE_TRIGGER_TOKEN"] = "trig-secret"
        os.environ["SCRAPE_WORKER_TOKEN"] = "work-secret"

        # ── auth ──
        check("enqueue with NO token → 401",
              c.post("/api/scrape-jobs", json={"case_number": "TX-26-00009"}).status_code == 401)
        check("enqueue with WRONG token → 401",
              c.post("/api/scrape-jobs", json={"case_number": "TX-26-00009"},
                     headers={"X-Scrape-Token": "nope"}).status_code == 401)
        check("worker token cannot enqueue (needs trigger token) → 401",
              c.post("/api/scrape-jobs", json={"case_number": "TX-26-00009"},
                     headers={"X-Scrape-Token": "work-secret"}).status_code == 401)

        # ── validation ──
        check("both case_number and pattern → 400",
              c.post("/api/scrape-jobs", json={"case_number": "TX-26-00009", "pattern": "TX-26"},
                     headers=TRIG).status_code == 400)
        check("neither case_number nor pattern → 400",
              c.post("/api/scrape-jobs", json={}, headers=TRIG).status_code == 400)
        check("malformed case_number → 400",
              c.post("/api/scrape-jobs", json={"case_number": "26-9"}, headers=TRIG).status_code == 400)

        # ── enqueue OK + case number normalized/uppercased ──
        r = c.post("/api/scrape-jobs", json={"case_number": "tx-26-00009"}, headers=TRIG)
        check("valid enqueue → 200 queued", r.status_code == 200 and r.json()["status"] == "queued")
        jid = r.json()["job_id"]
        check("enqueue label uppercased", r.json()["label"] == "TX-26-00009")

        # ── dedup: identical in-flight request rejected ──
        check("duplicate in-flight enqueue → 409",
              c.post("/api/scrape-jobs", json={"case_number": "TX-26-00009"},
                     headers=TRIG).status_code == 409)
        # a DIFFERENT case is fine
        check("different case enqueues fine",
              c.post("/api/scrape-jobs", json={"case_number": "TX-26-00010"},
                     headers=TRIG).status_code == 200)

        # ── queue cap (MAX_QUEUED_SCRAPES=3): 2 queued already, add 1 → 3, next → 429 ──
        c.post("/api/scrape-jobs", json={"case_number": "TX-26-00011"}, headers=TRIG)
        check("enqueue past the cap → 429",
              c.post("/api/scrape-jobs", json={"case_number": "TX-26-00012"},
                     headers=TRIG).status_code == 429)

        # ── GET status auth + shape ──
        check("get status with no token → 401", c.get(f"/api/scrape-jobs/{jid}").status_code == 401)
        r = c.get(f"/api/scrape-jobs/{jid}", headers=TRIG)
        check("get status → 200 with parsed request",
              r.status_code == 200 and r.json()["request"] == {"case_number": "TX-26-00009"})
        check("get missing job → 404", c.get("/api/scrape-jobs/99999", headers=TRIG).status_code == 404)

        # ── claim: worker-only, atomic, oldest-first ──
        check("claim with trigger token (not worker) → 401",
              c.post("/api/scrape-jobs/claim", json={}, headers=TRIG).status_code == 401)
        r = c.post("/api/scrape-jobs/claim", json={"worker_id": "w1"}, headers=WORK)
        first = r.json()["job"]
        check("claim returns the OLDEST queued job (TX-26-00009)",
              first and first["label"] == "TX-26-00009" and first["status"] == "claimed")
        check("claimed job stamped claimed_at + worker_id",
              first["claimed_at"] and first["worker_id"] == "w1")

        # drain the rest; every claim returns a distinct id; then empty
        seen = {first["id"]}
        for _ in range(5):
            j = c.post("/api/scrape-jobs/claim", json={"worker_id": "w1"}, headers=WORK).json()["job"]
            if j is None:
                break
            check(f"atomic claim: id {j['id']} not double-granted", j["id"] not in seen)
            seen.add(j["id"])
        check("claim on empty queue → {job: null} (no error)",
              c.post("/api/scrape-jobs/claim", json={}, headers=WORK).json()["job"] is None)

        # ── PATCH: state machine + the finished_at column (regression guard) ──
        check("patch with trigger token (not worker) → 401",
              c.patch(f"/api/scrape-jobs/{jid}", json={"status": "running"}, headers=TRIG).status_code == 401)
        check("patch invalid status → 400",
              c.patch(f"/api/scrape-jobs/{jid}", json={"status": "bogus"}, headers=WORK).status_code == 400)
        rr = c.patch(f"/api/scrape-jobs/{jid}",
                     json={"status": "running", "progress": "scraping…"}, headers=WORK).json()
        check("patch → running persists, finished_at still empty",
              rr["status"] == "running" and not rr.get("finished_at"))
        done = c.patch(f"/api/scrape-jobs/{jid}",
                       json={"status": "done", "result": {"found": 1, "cases": [{"case_number": "TX-26-00009", "prod_ready": 0}]}},
                       headers=WORK).json()
        check("patch → done STAMPS finished_at (the completed_at/finished_at bug class)",
              done["status"] == "done" and bool(done["finished_at"]))
        check("done result stored + parsed back, shows prod_ready=0 (held)",
              done["result"]["found"] == 1 and done["result"]["cases"][0]["prod_ready"] == 0)
        check("patch missing job → 404",
              c.patch("/api/scrape-jobs/99999", json={"status": "done"}, headers=WORK).status_code == 404)

        # ── after terminal, an identical request can be re-enqueued (dedup only blocks in-flight) ──
        check("re-enqueue after done is allowed (dedup is in-flight-only)",
              c.post("/api/scrape-jobs", json={"case_number": "TX-26-00009"},
                     headers=TRIG).status_code == 200)

        # ── include_closed: default INCLUDES closed (the moat); pattern request stores the choice ──
        check("ScrapeJobIn.include_closed defaults True", main.ScrapeJobIn().include_closed is True)
        rp = c.post("/api/scrape-jobs", json={"pattern": "TX-23-9"}, headers=TRIG).json()
        gp = c.get(f"/api/scrape-jobs/{rp['job_id']}", headers=TRIG).json()
        check("pattern enqueue defaults include_closed=True in the stored request",
              gp["request"]["include_closed"] is True and gp["request"]["individuals_only"] is True)
        rp2 = c.post("/api/scrape-jobs",
                     json={"pattern": "TX-23-8", "include_closed": False, "individuals_only": False},
                     headers=TRIG).json()
        gp2 = c.get(f"/api/scrape-jobs/{rp2['job_id']}", headers=TRIG).json()
        check("pattern enqueue with include_closed=False is stored (narrow mode)",
              gp2["request"]["include_closed"] is False and gp2["request"]["individuals_only"] is False)

    print("-" * 56)
    total, passed = len(_results), sum(_results)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
