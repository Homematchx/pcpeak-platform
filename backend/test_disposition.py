#!/usr/bin/env python3
"""Tests for the CASE DISPOSITION system (docs/case-disposition-design.md §13). No network.

Archive-never-delete: a case never leaves the platform; a disposition is a STATE written through
an append-only prod-owned log. Every pin in the design's validation plan is asserted here:

  * APPEND-ONLY DERIVATION — current state and the open-review flag are derived purely from row
    ORDER (decision / proposal / dismissal), including reversal sequences, with no row ever updated;
  * GUARDS — dismissed_resolved refused when the balance is >0 OR UNKNOWN (the dismissed-owing
    pipeline can never be one-clicked into the archive); required-comment codes refused without
    one; unknown codes refused;
  * deal_status ISOLATION — posting a comment leaves the funnel cache byte-identical;
  * COUNT RECONCILIATION — active + watching + archived == total_all in /api/stats; /api/cases
    excludes ONLY archived by default and ?include_archived=1 restores exactly the archived set;
  * NON-INTERFERENCE — disposing a case changes nothing about its projection or prediction ledger;
  * INVALIDATION PRECISION — an `acquired` case with a zero balance produces NO flag, while a
    `paid_in_full` case whose balance goes positive produces exactly one;
  * PROPOSAL PRECISION — a dismissed_owing case is never proposed for archive, and an unknown
    balance never proposes paid_in_full;
  * SYNC ISOLATION — the disposition columns are skip-listed so a local sync can't clobber them;
  * APPEND-ONLY at the engine level — the restore guard denies DELETE on both new tables.

Run: python3 backend/test_disposition.py   (exit 0 = all green)
"""
import json
import sys
import tempfile
from pathlib import Path

_d = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent))
import main
main.DB_PATH = _d / "pcpeak.db"
main.LEDGER_DB_PATH = _d / "ledger.db"
from fastapi.testclient import TestClient

_results = []


def check(name, cond):
    _results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)


def pi(balance=None, **extra):
    """A property_intel blob. balance=None means UNKNOWN (key absent) — distinct from a real 0.0."""
    d = dict(extra)
    if balance is not None:
        d["current_tax_balance"] = balance
    return json.dumps(d)


def state_of(cn):
    with main.get_db() as db:
        r = db.execute("SELECT disposition_state, disposition_code, pending_review, "
                       "pending_review_code FROM cases WHERE case_number=?", [cn]).fetchone()
        return dict(r) if r else None


def log_of(cn):
    with main.get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM ledger.case_dispositions WHERE case_number=? ORDER BY id", [cn])]


def run():
    main.init_db()
    with TestClient(main.app) as c:

        # ════════════════════════════════════════════════════════════════════
        # PURE DERIVATION (§3.2) — state and flag read off row ORDER alone.
        # Kept pure so reversal sequences pin without a database.
        # ════════════════════════════════════════════════════════════════════
        d = main.derive_disposition([])
        check("derive: empty log → active, no code, no flag",
              d["state"] == "active" and d["code"] is None and d["pending_review"] == 0)

        d = main.derive_disposition([{"id": 1, "kind": "proposal", "code": "paid_in_full"}])
        check("derive: a proposal alone flags but NEVER changes state",
              d["state"] == "active" and d["pending_review"] == 1
              and d["pending_review_code"] == "paid_in_full")

        d = main.derive_disposition([
            {"id": 1, "kind": "proposal", "code": "paid_in_full"},
            {"id": 2, "kind": "decision", "state": "archived", "code": "paid_in_full"}])
        check("derive: a decision closes the open proposal",
              d["state"] == "archived" and d["pending_review"] == 0)

        d = main.derive_disposition([
            {"id": 1, "kind": "proposal", "code": "paid_in_full"},
            {"id": 2, "kind": "dismissal", "code": "paid_in_full"}])
        check("derive: a dismissal closes the flag and leaves the case UNCHANGED",
              d["state"] == "active" and d["code"] is None and d["pending_review"] == 0)

        # Reversal sequence — archive → reopen → archive again, all appended.
        d = main.derive_disposition([
            {"id": 1, "kind": "decision", "state": "archived", "code": "paid_in_full"},
            {"id": 2, "kind": "decision", "state": "active", "code": "reopened"},
            {"id": 3, "kind": "decision", "state": "watching", "code": "payment_plan_33_02"}])
        check("derive: reversal sequence resolves to the LATEST decision (watching)",
              d["state"] == "watching" and d["code"] == "payment_plan_33_02")

        # An invalidation fires AFTER a decision → flag reopens without touching state.
        d = main.derive_disposition([
            {"id": 1, "kind": "decision", "state": "archived", "code": "paid_in_full"},
            {"id": 2, "kind": "proposal", "code": "paid_in_full"}])
        check("derive: a proposal AFTER a decision flags an ARCHIVED case (flag ⟂ state)",
              d["state"] == "archived" and d["pending_review"] == 1)

        # ════════════════════════════════════════════════════════════════════
        # TAXONOMY — 15 codes, and every declared predicate actually exists.
        # ════════════════════════════════════════════════════════════════════
        check("taxonomy has exactly 15 codes", len(main.DISPOSITION_CODES) == 15)
        groups = {}
        for spec in main.DISPOSITION_CODES.values():
            groups[spec["group"]] = groups.get(spec["group"], 0) + 1
        check("taxonomy groups are 3+4+1+5+2",
              [groups[g] for g in main.DISPOSITION_GROUP_ORDER] == [3, 4, 1, 5, 2])
        check("every declared invalidation predicate is bound",
              all(s["invalidation"] in main.INVALIDATION_PREDICATES
                  for s in main.DISPOSITION_CODES.values() if s.get("invalidation")))
        check("the four watching codes are the warm-lead ones",
              sorted(k for k, v in main.DISPOSITION_CODES.items() if v["state"] == "watching")
              == ["owner_declined", "payment_plan_33_02", "tax_loan_32_06", "unable_to_contact"])
        check("there is NO plain `dismissed` code (the dismissed-owing pipeline is protected)",
              "dismissed" not in main.DISPOSITION_CODES)

        # ════════════════════════════════════════════════════════════════════
        # GUARDS (§4.3)
        # ════════════════════════════════════════════════════════════════════
        c.post("/api/cases", json={"case_number": "TX-26-00100", "property_address": "1 Owing St",
                                   "judgment_type": "NON-SUIT/DISMISSAL",
                                   "property_intel": pi(balance=7200.0)})
        r = c.post("/api/cases/TX-26-00100/disposition", json={"code": "dismissed_resolved"})
        check("dismissed_resolved REFUSED on a case that still owes (409)", r.status_code == 409)
        check("...and the refusal says the case is an active lead",
              "ACTIVE LEAD" in r.json().get("detail", ""))
        check("...and the case is untouched (still active)", state_of("TX-26-00100")["disposition_state"] == "active")

        c.post("/api/cases", json={"case_number": "TX-26-00101", "property_address": "2 Unknown St",
                                   "judgment_type": "NON-SUIT/DISMISSAL"})   # NO property_intel
        r = c.post("/api/cases/TX-26-00101/disposition", json={"code": "dismissed_resolved"})
        check("dismissed_resolved REFUSED when the balance is UNKNOWN (never reads as paid)",
              r.status_code == 409 and "UNKNOWN" in r.json().get("detail", ""))

        r = c.post("/api/cases/TX-26-00101/disposition", json={"code": "acquired"})
        check("a comment-required code is refused without a comment (400)", r.status_code == 400)
        r = c.post("/api/cases/TX-26-00101/disposition", json={"code": "not_a_real_code"})
        check("an unknown code is refused (400)", r.status_code == 400)
        r = c.post("/api/cases/TX-99-99999/disposition", json={"code": "duplicate", "comment": "x"})
        check("disposing a nonexistent case 404s", r.status_code == 404)

        # ════════════════════════════════════════════════════════════════════
        # COMMIT + STATE DERIVED FROM CODE (§4) — a caller never picks the state
        # ════════════════════════════════════════════════════════════════════
        c.post("/api/cases", json={"case_number": "TX-26-00102", "property_address": "3 Plan St",
                                   "property_intel": pi(balance=4100.0)})
        r = c.post("/api/cases/TX-26-00102/disposition",
                   json={"code": "payment_plan_33_02", "comment": "36-mo plan signed 2026-07-01",
                         "decided_by": "Jay Lewis"})
        check("payment_plan_33_02 commits to WATCHING (state derived from the code)",
              r.status_code == 200 and r.json()["state"] == "watching")
        check("...and the cache reflects it", state_of("TX-26-00102")["disposition_state"] == "watching")

        c.post("/api/cases", json={"case_number": "TX-26-00103", "property_address": "4 Paid St",
                                   "property_intel": pi(balance=0.0)})
        r = c.post("/api/cases/TX-26-00103/disposition",
                   json={"code": "paid_in_full", "decided_by": "Jay Lewis"})
        check("paid_in_full on a REAL 0.0 balance commits to ARCHIVED",
              r.status_code == 200 and r.json()["state"] == "archived")

        # ════════════════════════════════════════════════════════════════════
        # VISIBILITY + COUNT RECONCILIATION (§8)
        # ════════════════════════════════════════════════════════════════════
        allc = {x["case_number"] for x in c.get("/api/cases").json()}
        check("/api/cases DEFAULT excludes the archived case", "TX-26-00103" not in allc)
        check("/api/cases DEFAULT still includes the WATCHING case (a warm lead is a real lead)",
              "TX-26-00102" in allc)
        inc = {x["case_number"] for x in c.get("/api/cases?include_archived=1").json()}
        check("?include_archived=1 restores exactly the archived set",
              inc - allc == {"TX-26-00103"})
        only = c.get("/api/cases?state=archived").json()
        check("?state=archived returns only archived", [x["case_number"] for x in only] == ["TX-26-00103"])

        s = c.get("/api/stats").json()
        check("stats reconcile: active + watching + archived == total_all",
              s["active_cases"] + s["watching_cases"] + s["archived_cases"] == s["total_all"])
        check("stats total_cases is the DEFAULT-VIEW denominator (archived excluded)",
              s["total_cases"] == s["active_cases"] + s["watching_cases"]
              and s["total_cases"] == len(allc))
        check("stats reports archived separately (the gap is never invisible)", s["archived_cases"] == 1)

        # An archived case stays PERMANENTLY QUERYABLE by case number.
        r = c.get("/api/cases/TX-26-00103")
        check("an archived case is still returned by /api/cases/{cn} (permanently queryable)",
              r.status_code == 200 and r.json()["case_number"] == "TX-26-00103")

        # ════════════════════════════════════════════════════════════════════
        # NON-INTERFERENCE (the §G land-floor pattern) — disposing changes nothing
        # ════════════════════════════════════════════════════════════════════
        c.post("/api/cases", json={"case_number": "TX-26-00104", "property_address": "5 Same St",
                                   "judgment_date": "2026-01-15", "stage": "judgment",
                                   "city": "Dallas", "property_intel": pi(balance=9000.0)})
        before = c.get("/api/cases/TX-26-00104").json()
        with main.get_db() as db:
            pl_before = db.execute("SELECT COUNT(*) FROM ledger.prediction_ledger "
                                   "WHERE case_number='TX-26-00104'").fetchone()[0]
        c.post("/api/cases/TX-26-00104/disposition",
               json={"code": "no_go_underwriting", "comment": "payoffs exceed MAO at every rung"})
        after = c.get("/api/cases/TX-26-00104").json()
        check("disposing leaves the PROJECTION byte-identical",
              json.dumps(before["projection"], sort_keys=True)
              == json.dumps(after["projection"], sort_keys=True))
        with main.get_db() as db:
            pl_after = db.execute("SELECT COUNT(*) FROM ledger.prediction_ledger "
                                  "WHERE case_number='TX-26-00104'").fetchone()[0]
        check("disposing writes NOTHING to the prediction ledger "
              "(outcomes come from docket evidence, never human filing decisions)",
              pl_before == pl_after)

        # ════════════════════════════════════════════════════════════════════
        # deal_status ISOLATION (§3.3) — a note must NEVER advance the funnel
        # ════════════════════════════════════════════════════════════════════
        with main.get_db() as db:
            ds_before = db.execute("SELECT deal_status, last_action_at FROM cases "
                                   "WHERE case_number='TX-26-00104'").fetchone()
            ds_before = (ds_before[0], ds_before[1])
            ra_before = db.execute("SELECT COUNT(*) FROM ledger.rep_actions").fetchone()[0]
        r = c.post("/api/cases/TX-26-00104/comments",
                   json={"body": "drove by, looks vacant", "author": "Jay Lewis"})
        with main.get_db() as db:
            ds_after = db.execute("SELECT deal_status, last_action_at FROM cases "
                                  "WHERE case_number='TX-26-00104'").fetchone()
            ds_after = (ds_after[0], ds_after[1])
            ra_after = db.execute("SELECT COUNT(*) FROM ledger.rep_actions").fetchone()[0]
        check("posting a comment succeeds", r.status_code == 200)
        check("...leaves deal_status BYTE-IDENTICAL (no fabricated funnel state)", ds_before == ds_after)
        check("...and writes NO rep_actions row", ra_before == ra_after)
        check("comment is readable back",
              c.get("/api/cases/TX-26-00104/comments").json()["count"] >= 1)

        # ════════════════════════════════════════════════════════════════════
        # PROPOSAL PRECISION (§5) — the auto-flag proposes, never archives
        # ════════════════════════════════════════════════════════════════════
        # dismissed AND owing → the CORE LEAD PIPELINE. Must never be proposed for archive.
        st = state_of("TX-26-00100")
        check("a dismissed_owing case is NEVER proposed for archive",
              st["pending_review"] == 0 and st["disposition_state"] == "active")
        # unknown balance → never proposes paid_in_full
        st = state_of("TX-26-00101")
        check("an UNKNOWN balance never proposes paid_in_full", st["pending_review"] == 0)
        # real 0.0 balance on a fresh case → proposes paid_in_full, and does NOT archive it
        c.post("/api/cases", json={"case_number": "TX-26-00105", "property_address": "6 Zero St",
                                   "property_intel": pi(balance=0.0)})
        st = state_of("TX-26-00105")
        check("a real 0.0 balance PROPOSES paid_in_full",
              st["pending_review"] == 1 and st["pending_review_code"] == "paid_in_full")
        check("...but the case stays ACTIVE — nothing auto-archives",
              st["disposition_state"] == "active")
        listed = {x["case_number"] for x in c.get("/api/cases").json()}
        check("...and a flagged case is still in the default view", "TX-26-00105" in listed)
        # dismissed + real zero balance → case_track='dismissed_paid' → dismissed_resolved
        c.post("/api/cases", json={"case_number": "TX-26-00106", "property_address": "7 Done St",
                                   "judgment_type": "NON-SUIT/DISMISSAL",
                                   "property_intel": pi(balance=0.0)})
        st = state_of("TX-26-00106")
        check("dismissed + a real zero balance proposes dismissed_resolved",
              st["pending_review_code"] == "dismissed_resolved")
        # idempotent: a second identical write does not append a second proposal
        n = len([r for r in log_of("TX-26-00105") if r["kind"] == "proposal"])
        c.post("/api/cases", json={"case_number": "TX-26-00105", "property_intel": pi(balance=0.0)})
        check("re-writing does NOT append a duplicate proposal (idempotent)",
              len([r for r in log_of("TX-26-00105") if r["kind"] == "proposal"]) == n)

        # Dismissing the flag leaves the case exactly as it was.
        r = c.post("/api/cases/TX-26-00105/disposition/dismiss",
                   json={"code": "paid_in_full", "comment": "balance is stale, re-enriching"})
        st = state_of("TX-26-00105")
        check("dismissing a flag clears it and leaves the case ACTIVE",
              r.status_code == 200 and st["pending_review"] == 0
              and st["disposition_state"] == "active")
        r = c.post("/api/cases/TX-26-00105/disposition/dismiss", json={"code": "paid_in_full"})
        check("dismissing with no open flag 409s", r.status_code == 409)

        # ════════════════════════════════════════════════════════════════════
        # INVALIDATION PRECISION (§6) — premise checks, not re-proposals
        # ════════════════════════════════════════════════════════════════════
        # `acquired` has NO predicate: a zero balance is EXPECTED forever and must never re-flag.
        c.post("/api/cases", json={"case_number": "TX-26-00107", "property_address": "8 Bought St",
                                   "property_intel": pi(balance=0.0)})
        c.post("/api/cases/TX-26-00107/disposition",
               json={"code": "acquired", "comment": "closed 2026-07-20 at $108k",
                     "decided_by": "Jay Lewis"})
        for _ in range(3):
            c.post("/api/cases", json={"case_number": "TX-26-00107", "property_intel": pi(balance=0.0)})
        st = state_of("TX-26-00107")
        check("an ACQUIRED case with a zero balance produces NO flag, ever",
              st["pending_review"] == 0 and st["disposition_state"] == "archived")

        # `paid_in_full` whose balance goes positive → EXACTLY one flag.
        c.post("/api/cases", json={"case_number": "TX-26-00108", "property_address": "9 Back St",
                                   "property_intel": pi(balance=0.0)})
        c.post("/api/cases/TX-26-00108/disposition", json={"code": "paid_in_full"})
        # Count proposals raised AFTER the decision — the creation-time proposal that the decision
        # already closed is legitimate history and must stay in the log.
        decision_id = max(r["id"] for r in log_of("TX-26-00108") if r["kind"] == "decision")
        after_decision = lambda: [r for r in log_of("TX-26-00108")
                                  if r["kind"] == "proposal" and r["id"] > decision_id]
        c.post("/api/cases", json={"case_number": "TX-26-00108", "property_intel": pi(balance=3300.0)})
        st = state_of("TX-26-00108")
        check("a PAID_IN_FULL case whose balance goes positive flags exactly once",
              st["pending_review"] == 1 and len(after_decision()) == 1)
        check("...the flag does NOT change its state (still archived)",
              st["disposition_state"] == "archived")
        c.post("/api/cases", json={"case_number": "TX-26-00108", "property_intel": pi(balance=3400.0)})
        check("...and re-writing does not pile on duplicate flags", len(after_decision()) == 1)
        ev = json.loads(after_decision()[-1]["evidence"])
        check("...and the flag carries its evidence (old vs new balance)",
              ev.get("balance") == 3300.0 and ev.get("balance_at_decision") == 0.0)

        # A §33.02 plan default — the predicate that makes `watching` earn its keep.
        c.post("/api/cases", json={"case_number": "TX-26-00109", "property_address": "10 Plan St",
                                   "property_intel": pi(balance=2000.0)})
        c.post("/api/cases/TX-26-00109/disposition",
               json={"code": "payment_plan_33_02", "comment": "24-mo plan"})
        c.post("/api/cases", json={"case_number": "TX-26-00109", "property_intel": pi(balance=2100.0)})
        check("a §33.02 plan does NOT flag on rounding-scale noise (+$100)",
              state_of("TX-26-00109")["pending_review"] == 0)
        c.post("/api/cases", json={"case_number": "TX-26-00109", "property_intel": pi(balance=5200.0)})
        check("a §33.02 plan DOES flag when the balance climbs materially (plan default)",
              state_of("TX-26-00109")["pending_review"] == 1)

        # ════════════════════════════════════════════════════════════════════
        # REVERSIBILITY (§13) — archive → reopen → full history intact
        # ════════════════════════════════════════════════════════════════════
        r = c.post("/api/cases/TX-26-00103/disposition/reopen",
                   json={"code": "reopened", "comment": "balance was stale", "decided_by": "Jay Lewis"})
        check("reopen returns the case to ACTIVE", r.status_code == 200 and r.json()["state"] == "active")
        check("...it reappears in the default view",
              "TX-26-00103" in {x["case_number"] for x in c.get("/api/cases").json()})
        lg = log_of("TX-26-00103")
        check("...and the PRIOR disposition stays in the log permanently (append-only)",
              [r["kind"] for r in lg].count("decision") == 2
              and lg[0]["code"] == "paid_in_full" and lg[-1]["state"] == "active")
        r = c.post("/api/cases/TX-26-00103/disposition/reopen", json={"code": "reopened"})
        check("reopening an already-active case 409s", r.status_code == 409)

        # ════════════════════════════════════════════════════════════════════
        # MERGED HISTORY (§3.5) — read-time merge, case_snapshots untouched
        # ════════════════════════════════════════════════════════════════════
        h = c.get("/api/cases/TX-26-00108/history").json()
        origins = {i["origin"] for i in h["history"]}
        check("merged history carries BOTH snapshot and disposition rows",
              "snapshot" in origins and "disposition" in origins)
        check("merged history is chronological",
              [i["at"] for i in h["history"]] == sorted(i["at"] for i in h["history"]))
        with main.get_db() as db:
            bad = db.execute("SELECT COUNT(*) FROM ledger.case_snapshots "
                             "WHERE field LIKE 'disposition%' OR source='disposition'").fetchone()[0]
        check("case_snapshots is UNTOUCHED by the disposition system (no write-through)", bad == 0)

        # ════════════════════════════════════════════════════════════════════
        # ROLL-UP + REVIEW SURFACES (§9/§10.1)
        # ════════════════════════════════════════════════════════════════════
        dj = c.get("/api/dispositions").json()
        check("roll-up counts reconcile with total_all",
              dj["counts"]["active"] + dj["counts"]["watching"] + dj["counts"]["archived"]
              == dj["counts"]["total_all"])
        check("the review queue lists flagged cases",
              any(x["case_number"] == "TX-26-00108" for x in dj["review_queue"]))
        check("recently-archived carries decided_by (a wrong archive is noticed, not silent)",
              any(x["case_number"] == "TX-26-00107" and x["decided_by"] is not None
                  for x in dj["recently_archived"]))
        codes = c.get("/api/dispositions/codes").json()
        check("the taxonomy is served (the UI never keeps a second copy that can drift)",
              len(codes["codes"]) == 15 and codes["groups"] == main.DISPOSITION_GROUP_ORDER)

        # ════════════════════════════════════════════════════════════════════
        # APPEND-ONLY at the ENGINE level — the restore guard denies DELETE
        # ════════════════════════════════════════════════════════════════════
        import sqlite3
        for tbl in ("case_dispositions", "case_comments"):
            check(f"{tbl} is registered prod-owned", tbl in main.PROD_OWNED_TABLES)
            try:
                with main.get_db() as db:
                    db.execute(f"DELETE FROM ledger.{tbl}")
                denied = False
            except sqlite3.DatabaseError:
                denied = True
            check(f"DELETE on {tbl} is DENIED by the restore guard", denied)
        try:
            main.assert_restore_safe(["cases", "case_dispositions"])
            tripped = False
        except RuntimeError:
            tripped = True
        check("assert_restore_safe refuses a restore targeting case_dispositions", tripped)

    # ════════════════════════════════════════════════════════════════════
    # SYNC ISOLATION — a local sync can never push or clobber a disposition
    # ════════════════════════════════════════════════════════════════════
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import sync_to_prod
    for col in ("disposition_state", "disposition_code", "disposition_at",
                "pending_review", "pending_review_code"):
        check(f"sync_to_prod never sends {col}", col in sync_to_prod.SKIP_CASE_FIELDS)
    # Sync must read prod's TRUE inventory, not the default working view: an archived case that
    # looked absent would be re-pushed as "new" every run and would break the already-live
    # prod_ready reconcile.
    src = Path(sync_to_prod.__file__).read_text()
    check("sync_to_prod fetches prod with include_archived (true inventory, not the working view)",
          '"GET", "/api/cases?include_archived=1"' in src)
    check("...and its post-run verify uses the SAME denominator",
          src.count("/api/cases?include_archived=1") >= 2)
    check("sync_to_prod never fetches the archived-excluding default",
          '"GET", "/api/cases")' not in src)

    print("-" * 60)
    total, passed = len(_results), sum(_results)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
