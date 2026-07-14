#!/usr/bin/env python3
"""Guard test for the prod-approval gate in sync_to_prod.py.

The 2026-07-13 36-case premature-sync incident happened because sync had no distinction
between "exists locally" and "approved for prod" — a routine push promoted every local
case, including work-in-progress leads. The fix is structural: cases.prod_ready (default
0 = held), and EVERY push path runs through SYNCABLE_WHERE (prod_ready=1). This proves the
gate at the DB level (no network): a held case is never syncable, approval is explicit, an
already-live case is reconciled to approved, and the BPP exclusion still holds on top.

Run: python3 test_prod_ready_gate.py   (exit 0 = all green)
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync_to_prod as S

_results = []
def check(name, cond):
    _results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)


def fresh_db(with_prod_ready=True):
    """A minimal cases table. If with_prod_ready=False, omit the column so we can
    test ensure_schema() self-healing a pre-migration DB."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    cols = ["case_number TEXT UNIQUE", "property_type TEXT", "case_track TEXT"]
    if with_prod_ready:
        cols.append("prod_ready INTEGER DEFAULT 0")
    db.execute(f"CREATE TABLE cases ({', '.join(cols)})")
    return db


def add(db, cn, prod_ready=0, property_type=None, case_track=None):
    db.execute(
        "INSERT INTO cases (case_number, prod_ready, property_type, case_track) "
        "VALUES (?,?,?,?)", [cn, prod_ready, property_type, case_track])
    db.commit()


def run():
    # ── 1. ensure_schema self-heals a pre-migration DB ──
    db = fresh_db(with_prod_ready=False)
    cols_before = {r[1] for r in db.execute("PRAGMA table_info(cases)").fetchall()}
    S.ensure_schema(db)
    cols_after = {r[1] for r in db.execute("PRAGMA table_info(cases)").fetchall()}
    check("ensure_schema adds prod_ready when missing",
          "prod_ready" not in cols_before and "prod_ready" in cols_after)
    # idempotent — second call is a no-op, no error
    S.ensure_schema(db)
    check("ensure_schema is idempotent (no error on second call)", True)

    # ── 2. the core gate: held (prod_ready=0) is NEVER syncable ──
    db = fresh_db()
    add(db, "TX-26-HELD", prod_ready=0, property_type="real")
    add(db, "TX-26-APPROVED", prod_ready=1, property_type="real")
    local = S.local_cases(db)
    check("approved real-property case IS syncable", "TX-26-APPROVED" in local)
    check("held (prod_ready=0) case is NOT syncable (the incident fix)",
          "TX-26-HELD" not in local)

    # ── 3. default is held — a freshly-scraped case (no explicit prod_ready) stays out ──
    db = fresh_db()
    db.execute("INSERT INTO cases (case_number, property_type) VALUES ('TX-26-NEW','real')")
    db.commit()
    check("a new scrape defaults to held (DEFAULT 0, not syncable)",
          "TX-26-NEW" not in S.local_cases(db))

    # ── 4. BPP / unknown excluded even when approved (belt-and-suspenders) ──
    db = fresh_db()
    add(db, "TX-26-BPP", prod_ready=1, property_type="personal")
    add(db, "TX-26-BPPTRACK", prod_ready=1, property_type="real", case_track="personal_property")
    add(db, "TX-26-UNK", prod_ready=1, property_type="unknown")
    add(db, "TX-26-OK", prod_ready=1, property_type="real")
    local = S.local_cases(db)
    check("approved BPP (property_type=personal) still excluded", "TX-26-BPP" not in local)
    check("approved case_track=personal_property still excluded", "TX-26-BPPTRACK" not in local)
    check("approved unknown property_type still excluded", "TX-26-UNK" not in local)
    check("approved real case passes both gates", "TX-26-OK" in local)

    # ── 5. reconcile_prod_ready: already-live ⇒ approved ──
    db = fresh_db()
    add(db, "TX-26-LIVE1", prod_ready=0, property_type="real")
    add(db, "TX-26-LIVE2", prod_ready=0, property_type="real")
    add(db, "TX-26-LOCALONLY", prod_ready=0, property_type="real")
    n = S.reconcile_prod_ready(db, ["TX-26-LIVE1", "TX-26-LIVE2"])
    check("reconcile marks 2 already-live cases approved", n == 2)
    local = S.local_cases(db)
    check("reconciled live cases become syncable",
          "TX-26-LIVE1" in local and "TX-26-LIVE2" in local)
    check("a local-only (not-on-prod) case stays held after reconcile",
          "TX-26-LOCALONLY" not in local)
    check("reconcile is idempotent (0 newly marked on second run)",
          S.reconcile_prod_ready(db, ["TX-26-LIVE1", "TX-26-LIVE2"]) == 0)

    # ── 6. set_prod_ready: explicit approve / revoke ──
    db = fresh_db()
    add(db, "TX-26-A", prod_ready=0, property_type="real")
    changed = S.set_prod_ready(db, ["TX-26-A", "TX-26-GHOST"], 1)
    check("set_prod_ready approves a real local case", "TX-26-A" in S.local_cases(db))
    check("set_prod_ready returns only cases that existed (ghost ignored)",
          changed == ["TX-26-A"])
    S.set_prod_ready(db, ["TX-26-A"], 0)
    check("set_prod_ready(0) revokes approval (case held again)",
          "TX-26-A" not in S.local_cases(db))

    # ── 7. pending_cases: the held real-property set (the gate's protectees) ──
    db = fresh_db()
    add(db, "TX-26-H1", prod_ready=0, property_type="real")
    add(db, "TX-26-H2", prod_ready=0, property_type=None)          # NULL type kept
    add(db, "TX-26-APP", prod_ready=1, property_type="real")       # approved → not pending
    add(db, "TX-26-BPP", prod_ready=0, property_type="personal")   # BPP → not in this set
    pend = set(S.pending_cases(db))
    check("pending_cases lists held real/NULL cases", pend == {"TX-26-H1", "TX-26-H2"})
    check("pending_cases excludes approved cases", "TX-26-APP" not in pend)
    check("pending_cases excludes BPP (held for a different reason)", "TX-26-BPP" not in pend)

    # ── 8. incident simulation: 34 held leads + 2 approved → only 2 sync ──
    db = fresh_db()
    for i in range(34):
        add(db, f"TX-26-LEAD{i:02d}", prod_ready=0, property_type="real")
    add(db, "TX-26-READY1", prod_ready=1, property_type="real")
    add(db, "TX-26-READY2", prod_ready=1, property_type="real")
    syncable = set(S.local_cases(db))
    check("36-case incident cannot recur: only the 2 approved cases are syncable",
          syncable == {"TX-26-READY1", "TX-26-READY2"})
    check("all 34 held leads are reported as pending", len(S.pending_cases(db)) == 34)

    print("-" * 56)
    total, passed = len(_results), sum(_results)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
