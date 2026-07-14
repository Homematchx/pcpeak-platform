#!/usr/bin/env python3
"""Guard test for purge_test_case.py. No network (prod set is injected).

Same standard as the BPP delete guard (test_bpp_delete_guard.py): prove the delete is
STRUCTURALLY incapable of removing a real case. Only a contentless, local-only, held throwaway
is deletable; a case that is on prod, approved, or carries real content — including a genuinely-
held local lead — is refused, and its row + events survive untouched. Also checks isolation
(only the target is affected) and --dry-run (no write).

Run: python3 test_purge_test_case.py   (exit 0 = all green)
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import purge_test_case as P

_results = []
def check(name, cond):
    _results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE cases (case_number TEXT UNIQUE, property_address TEXT, ai_memo TEXT, prod_ready INTEGER DEFAULT 0)")
    db.execute("CREATE TABLE docket_events (case_number TEXT, description TEXT)")
    return db


def seed(db):
    # (case_number, property_address, ai_memo, prod_ready)
    rows = [
        ("TX-99-00001", None, None, 0),                       # the stub — throwaway
        ("TX-23-00379", "3928 Atlanta St, Dallas", "memo", 0),# real, will be on prod (pre-reconcile prod_ready=0)
        ("TX-26-99999", "123 Real Lead Rd", None, 0),         # genuinely-held local lead WITH content
        ("TX-25-00492", "2827 E Overton", "memo", 1),         # approved, NOT yet on prod (tests the prod_ready=1 branch)
    ]
    db.executemany("INSERT INTO cases VALUES (?,?,?,?)", rows)
    # events for the stub AND a real case — deleting the stub must not touch the real one's events
    db.executemany("INSERT INTO docket_events VALUES (?,?)",
                   [("TX-99-00001", "stub event"), ("TX-23-00379", "real event")])
    db.commit()

# Live on prod: the real published case. TX-25-00492 is approved locally but NOT yet synced
# (a real state after --approve, before sync), so it exercises the prod_ready=1 refusal branch
# rather than being short-circuited by the on-prod check.
PROD = {"TX-23-00379"}


def has(db, cn):
    return db.execute("SELECT 1 FROM cases WHERE case_number=?", [cn]).fetchone() is not None

def events(db, cn):
    return db.execute("SELECT COUNT(*) FROM docket_events WHERE case_number=?", [cn]).fetchone()[0]


def run():
    # ── is_throwaway unit decisions ──
    def dec(pa, memo, pr, on_prod):
        return P.is_throwaway({"property_address": pa, "ai_memo": memo, "prod_ready": pr}, on_prod)
    check("stub (no content, held, not on prod) → deletable", dec(None, None, 0, False)[0] is True)
    check("on prod → refused", dec(None, None, 0, True)[0] is False)
    check("approved (prod_ready=1) → refused", dec(None, None, 1, False)[0] is False)
    check("has property_address → refused", dec("123 Main", None, 0, False)[0] is False)
    check("has ai_memo → refused", dec(None, "some memo", 0, False)[0] is False)
    check("None row → not found", P.is_throwaway(None, False) == (False, "not found"))

    # ── purge integration: deletes ONLY the stub, entirely ──
    db = fresh_db(); seed(db)
    res = P.purge(db, "TX-99-00001", PROD)
    check("purge stub → ok", res["ok"] is True)
    check("purge stub → 1 case row deleted", res["deleted_cases"] == 1)
    check("purge stub → its event deleted", res["deleted_events"] == 1)
    check("purge stub → count dropped by exactly 1", res["before"] - res["after"] == 1)
    check("purge stub → stub gone", res["gone"] is True and not has(db, "TX-99-00001"))
    check("purge stub → real case UNTOUCHED", has(db, "TX-23-00379"))
    check("purge stub → real case's events UNTOUCHED", events(db, "TX-23-00379") == 1)

    # ── refusals leave everything intact ──
    db = fresh_db(); seed(db)
    r_prod = P.purge(db, "TX-23-00379", PROD)
    check("purge on-prod case → refused (live on prod)", r_prod["ok"] is False and "prod" in r_prod["reason"])
    check("purge on-prod case → survives + keeps events", has(db, "TX-23-00379") and events(db, "TX-23-00379") == 1)

    r_lead = P.purge(db, "TX-26-99999", PROD)
    check("purge held local lead WITH content → refused", r_lead["ok"] is False and "content" in r_lead["reason"])
    check("purge held local lead → survives", has(db, "TX-26-99999"))

    r_appr = P.purge(db, "TX-25-00492", PROD)
    check("purge approved case → refused", r_appr["ok"] is False and "approved" in r_appr["reason"])
    check("purge approved case → survives", has(db, "TX-25-00492"))

    r_absent = P.purge(db, "TX-00-00000", PROD)
    check("purge absent case → not found, nothing deleted", r_absent["reason"] == "not found" and r_absent["deleted_cases"] == 0)
    check("all 4 seeded cases still present after refusals",
          all(has(db, c) for c in ("TX-99-00001", "TX-23-00379", "TX-26-99999", "TX-25-00492")))

    # ── --dry-run does not write ──
    db = fresh_db(); seed(db)
    r_dry = P.purge(db, "TX-99-00001", PROD, dry=True)
    check("dry-run → ok decision but 0 deleted", r_dry["ok"] is True and r_dry["deleted_cases"] == 0)
    check("dry-run → stub still present", has(db, "TX-99-00001"))

    print("-" * 56)
    total, passed = len(_results), sum(_results)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
