"""
PC Peak Tax Foreclosure Intelligence Platform
Backend API — FastAPI + SQLite

Run: uvicorn main:app --reload --port 8000
"""

import sqlite3
import json
import os
import re
import hashlib
import hmac
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel

# ─── CONFIG ───────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DB_PATH  = BASE_DIR / "data" / "db" / "pcpeak.db"
# PROD-OWNED data lives in a SEPARATE database file (see docs/prediction-ledger-design.md
# §11-12). It is ATTACHed to every connection as schema `ledger`. Because it is a distinct
# file, the local→prod disaster-recovery path — a raw `sqlite3 pcpeak.db .dump` / restore —
# physically cannot reference, drop, or overwrite it: the structural guarantee that a
# procedural guard can't give for a path run under pressure from a runbook.
LEDGER_DB_PATH = BASE_DIR / "data" / "db" / "ledger.db"
PDF_DIR  = BASE_DIR / "data" / "pdfs"
PDF_DIR.mkdir(parents=True, exist_ok=True)

# Acquisition Intelligence engine (pure calculators) + comp engine live at the repo root.
import sys as _sys
if str(BASE_DIR) not in _sys.path:
    _sys.path.insert(0, str(BASE_DIR))
import acquisition          # noqa: E402  — Stage-1 calculators/gates (pure, no I/O)
import comps                # noqa: E402  — Stage-2 NTREIS comp engine

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app_: FastAPI):
    init_db()
    yield

app = FastAPI(title="PC Peak Tax Foreclosure Intelligence", version="1.0.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"])

# ─── ACCOUNT-RESOLUTION STATE ─────────────────────────────────
# One rule, shared by discover.py (valid_dcad_accounts), this backfill, and the
# frontend (caseAccountStatus) so the three always agree. A Dallas DCAD account is
# exactly 17 digits; a multi-tract petition lists several (comma/semicolon).
_PLACEHOLDER_ACCTS = {"tbd", "n/a", "na", "none", "unknown", "null"}

def case_track_of(property_type, oos_date, judgment_type, judgment_date, tax_balance):
    """Which track/dataset a case belongs to. One shared rule (mirrored in discover.py
    and the frontend) so the tag stays consistent and filterable:
      personal_property — business personal-property suit (different instrument; EXCLUDED
                          from real-estate timing/equity + calibration). Checked FIRST so a
                          BPP case can never land in oos_timing even if it gets an oos_date.
      oos_timing      — reached an Order of Sale (feeds the timing model)
      dismissed_owing — dismissed BUT still owes tax (the real lead pipeline; unknown
                        balance also lands here so a rep looks — dismissal != dead)
      dismissed_paid  — dismissed and $0 owed (resolved, low value)
      judged_pending  — has a real judgment, no OOS yet
      active          — open / pre-judgment
    `tax_balance` is the live ACT balance (None = unknown, 0.0 = real zero — distinct)."""
    if property_type == "personal":
        return "personal_property"          # FIRST — never let BPP reach the timing track
    if oos_date and str(oos_date).strip():
        return "oos_timing"
    jt = (judgment_type or "").upper()
    if "DISMISS" in jt or "NON-SUIT" in jt or "NONSUIT" in jt:
        if tax_balance is None:
            return "dismissed_owing"          # unknown → surface it, don't hide a lead
        return "dismissed_owing" if tax_balance > 0 else "dismissed_paid"
    has_judgment = bool(judgment_date and str(judgment_date).strip()) or (jt and jt not in ("", "NONE"))
    return "judged_pending" if has_judgment else "active"


def account_status_of(acct):
    a = (acct or "").strip()
    if not a or a.lower() in _PLACEHOLDER_ACCTS:
        return "needs_lookup"                      # no account to work with
    parts = [p for p in re.split(r"[,;]\s*", a) if p]
    # A DCAD account is 17 chars — usually digits, but some parcels use 17-char
    # alphanumeric IDs (condos/townhomes/special), which are equally valid.
    if any(re.fullmatch(r"[0-9A-Za-z]{17}", p) for p in parts):
        return "resolved"                          # at least one usable 17-char account
    return "invalid"                               # something was extracted, but it's malformed

# ─── PROD-OWNED DATA GUARD (see docs/prediction-ledger-design.md §11-12) ────────
# prediction_ledger + rep_actions + case_snapshots are generated and OWNED by prod (ledger via
# create_case, rep_actions via the rep UI, case_snapshots via create_case's diff-on-write). They
# cannot be regenerated from local scraping — case_snapshots specifically IS the history of what a
# case used to say, which a re-scrape overwrites and can never reconstruct — so the local→prod
# restore/push path must NEVER delete, drop, or wholesale-overwrite them. All three are append-only
# (case_snapshots is strictly insert-only; prediction_ledger also has a one-time reconcile column-
# fill), so a DELETE or DROP against them is ALWAYS wrong — restore, bug, or otherwise. Enforced at
# the SQLite engine level by an authorizer that denies DELETE/DROP on these tables while allowing the
# legitimate INSERT (logging) and UPDATE (reconcile). Installed on EVERY connection, so the tables
# are protected the instant they exist — before they ever hold real data.
PROD_OWNED_TABLES = ("prediction_ledger", "rep_actions", "case_snapshots",
                     "comps", "comp_confirmations", "acquisition_analysis")

def _restore_guard_authorizer(action, arg1, arg2, db_name, trigger_name):
    if action in (sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE) and arg1 in PROD_OWNED_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK

def assert_restore_safe(target_tables):
    """Tripwire for any FUTURE bulk/restore/reset code that clears or replaces tables:
    refuse if it targets a prod-owned table. Restore is scraped-data-only. (There is
    intentionally no such bulk path today; this keeps it that way if one is ever added.)"""
    hit = [t for t in target_tables if t in PROD_OWNED_TABLES]
    if hit:
        raise RuntimeError(f"restore guard: refusing to touch prod-owned table(s) {hit} "
                           f"— not restorable from local (design doc §11-12)")

# ─── DATABASE ─────────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(_restore_guard_authorizer)   # deny DELETE/DROP on prod-owned tables
    # Attach the prod-owned data file as schema `ledger` (created on first use). Both files
    # run WAL for concurrency; cross-file crash atomicity is intentionally NOT relied on —
    # authoritative prod-owned writes are single-DB (atomic), main-DB columns are derived
    # caches that self-heal on next write (design doc §12).
    conn.execute("ATTACH DATABASE ? AS ledger", (str(LEDGER_DB_PATH),))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA ledger.journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS cases (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number     TEXT UNIQUE NOT NULL,
            court           TEXT,
            judicial_officer TEXT,
            filed_date      TEXT,
            case_status     TEXT DEFAULT 'OPEN',
            case_type       TEXT DEFAULT 'TAX DELINQUENCY',
            defendant       TEXT,
            all_defendants  TEXT,  -- JSON array
            property_address TEXT,
            property_legal  TEXT,
            account_number  TEXT,
            law_firm        TEXT,
            plaintiff_attorney TEXT,
            total_due_filing REAL,
            oldest_delinquency_year INTEGER,
            delinquency_years TEXT, -- JSON array
            def_count       INTEGER DEFAULT 1,
            cbp_requested   INTEGER DEFAULT 0,
            rule106         INTEGER DEFAULT 0,
            prior_suits     TEXT,  -- JSON array
            estate_heir     INTEGER DEFAULT 0,
            continuance_count INTEGER DEFAULT 0,
            trial_reset_count INTEGER DEFAULT 0,
            service_issues  TEXT,
            complexity      TEXT DEFAULT 'medium',
            complexity_reason TEXT,
            judgment_date   TEXT,
            judgment_type   TEXT,
            oos_date        TEXT,
            oos_issued      INTEGER DEFAULT 0,
            next_hearing_date TEXT,
            notice_judgment_date TEXT,
            projected_oos   TEXT,
            confidence_pct  INTEGER,
            city            TEXT DEFAULT 'dallas',
            petition_pdf_path TEXT,
            petition_href       TEXT,
            tax_breakdown   TEXT,  -- JSON array
            ai_memo         TEXT,
            similar_benchmark TEXT,
            stage           TEXT DEFAULT 'pre_judgment',
            assessed_value  REAL,
            minimum_bid     REAL,
            sale_scheduled_date TEXT,
            sale_pulled_date TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now')),
            last_agent_run  TEXT,
            monitored       INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS docket_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number TEXT NOT NULL,
            event_date  TEXT,
            event_type  TEXT,
            description TEXT,
            detail      TEXT,
            is_new      INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (case_number) REFERENCES cases(case_number)
        );

        CREATE TABLE IF NOT EXISTS agent_runs (
            output TEXT,
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT DEFAULT (datetime('now')),
            finished_at     TEXT,
            status          TEXT DEFAULT 'running',
            cases_processed INTEGER DEFAULT 0,
            cases_updated   INTEGER DEFAULT 0,
            new_events_found INTEGER DEFAULT 0,
            errors          TEXT,
            log             TEXT
        );

        CREATE TABLE IF NOT EXISTS watch_list (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number TEXT UNIQUE NOT NULL,
            added_at    TEXT DEFAULT (datetime('now')),
            added_by    TEXT DEFAULT 'manual',
            notes       TEXT
        );

        -- Front-end scrape TRIGGER queue. The live site can't scrape (no browser in cloud;
        -- scraping is deliberately local). Instead the browser enqueues a job here (token-gated),
        -- a local worker on the Mac (scrape_worker.py) polls this queue OUTBOUND, claims a job,
        -- runs the real discover.py CLI locally, and PATCHes status/result back. Nothing ever
        -- connects INTO the Mac. State machine: queued -> claimed -> running -> done | failed
        -- (done/failed are terminal). `request` is the normalized input JSON ({case_number} or
        -- {pattern, individuals_only}); `result` is the worker's snapshot (cases found + each
        -- one's guardrail outcome + prod_ready, which is 0/held — the trigger scrapes, it does
        -- NOT publish). Column names are authoritative here; every reader uses them verbatim.
        CREATE TABLE IF NOT EXISTS scrape_jobs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            request      TEXT NOT NULL,                    -- JSON: {"case_number":...} | {"pattern":...,"individuals_only":true}
            label        TEXT,                             -- human label (the case# / pattern) for display
            status       TEXT NOT NULL DEFAULT 'queued',   -- queued|claimed|running|done|failed
            progress     TEXT,                             -- short worker-updated status line
            result       TEXT,                             -- JSON snapshot on success
            error        TEXT,                             -- failure detail
            worker_id    TEXT,                             -- which worker claimed it
            requested_by TEXT,                             -- token identity / 'operator'
            requested_at TEXT NOT NULL DEFAULT (datetime('now')),
            claimed_at   TEXT,
            finished_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_scrape_jobs_status ON scrape_jobs(status, id);

        -- HELD-FOR-REVIEW mirror. Held cases (prod_ready=0) live on the MAC, never on prod — so
        -- this is a PREVIEW-only mirror the Mac worker pushes up (POST /api/held/sync, full replace)
        -- so the browser can list what's awaiting approval. Approving enqueues an approve-job that
        -- the worker runs as the real sync_to_prod.py --approve locally; the case then publishes into
        -- `cases` and drops off this list on the next held-sync. No full case data or events here.
        CREATE TABLE IF NOT EXISTS held_cases (
            case_number      TEXT PRIMARY KEY,
            property_address TEXT,
            defendant        TEXT,
            total_due        REAL,
            property_type    TEXT,
            case_track       TEXT,
            account_status   TEXT,
            synced_at        TEXT DEFAULT (datetime('now'))
        );

        -- Worker liveness. The Mac worker heartbeats here each poll; the browser shows online/offline
        -- so a rep knows whether triggering / approving will actually be picked up (the whole feature
        -- depends on the worker running). One row per worker_id.
        CREATE TABLE IF NOT EXISTS worker_state (
            worker_id  TEXT PRIMARY KEY,
            last_seen  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS benchmarks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number     TEXT UNIQUE NOT NULL,
            description     TEXT,
            city            TEXT,
            complexity      TEXT,
            def_count       INTEGER,
            filed_date      TEXT,
            judgment_date   TEXT,
            judgment_type   TEXT,
            oos_date        TEXT,
            filing_to_judgment_months INTEGER,
            judgment_to_oos_days INTEGER,
            total_due       REAL,
            delinquency_years INTEGER,
            stage           TEXT,
            key_factors     TEXT,  -- JSON array
            outcome         TEXT,
            is_confirmed    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS reps (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            active     INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(case_status);
        CREATE INDEX IF NOT EXISTS idx_cases_stage ON cases(stage);
        CREATE INDEX IF NOT EXISTS idx_events_case ON docket_events(case_number);

        -- ── PROD-OWNED schema in the attached `ledger` file (design doc §4/§5/§13). Both
        -- tables are append-only (+ a one-time reconcile column-fill on prediction_ledger);
        -- the get_db() authorizer denies DELETE/DROP on them, and they live in a separate
        -- file so a raw pcpeak.db restore can't touch them. ──
        CREATE TABLE IF NOT EXISTS ledger.prediction_ledger (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number          TEXT NOT NULL,
            predicted_at         TEXT NOT NULL DEFAULT (datetime('now')),
            model_version        TEXT NOT NULL,
            prediction_basis     TEXT NOT NULL,          -- confirmed|judged|next_hearing|filed|none|na_bpp
            projected_oos        TEXT,
            confidence_pct       INTEGER,
            days_to_oos          INTEGER,
            filing_to_judgment_months INTEGER,
            in_city              TEXT,
            in_complexity        TEXT,
            in_stage             TEXT,
            in_filed_date        TEXT,
            in_judgment_date     TEXT,
            in_next_hearing_date TEXT,
            in_oos_issued        INTEGER,
            used_joos_days       INTEGER,
            used_ftj_low         INTEGER,
            used_ftj_high        INTEGER,
            input_hash           TEXT NOT NULL,          -- change-detection (new row only on meaningful change)
            outcome_type         TEXT,                   -- NULL until resolved; oos_issued|dismissed|sale|expired_no_oos
            outcome_date         TEXT,
            error_days           INTEGER,                -- signed: actual_oos - projected_oos
            resolved_at          TEXT
        );
        CREATE INDEX IF NOT EXISTS ledger.idx_pl_case  ON prediction_ledger(case_number, predicted_at);
        CREATE INDEX IF NOT EXISTS ledger.idx_pl_open  ON prediction_ledger(case_number) WHERE outcome_type IS NULL;
        CREATE INDEX IF NOT EXISTS ledger.idx_pl_model ON prediction_ledger(model_version);

        CREATE TABLE IF NOT EXISTS ledger.rep_actions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number  TEXT NOT NULL,
            rep          TEXT,
            action_at    TEXT NOT NULL DEFAULT (datetime('now')),
            action_type  TEXT NOT NULL,          -- contact_attempted|contact_made|response|offer|result
            channel      TEXT,                   -- call|email|door|mail
            response     TEXT,                   -- no_answer|callback|interested|not_interested|hostile
            offer_amount REAL,
            result       TEXT,                   -- deal|dead|pending|redeemed|lost
            note         TEXT,
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS ledger.idx_ra_case ON rep_actions(case_number, action_at);

        -- CASE-FIELD HISTORY (design: the case_snapshots foundation). A re-scrape/re-sync overwrites
        -- a case's fields in place (create_case does merge→UPDATE); this append-only table captures
        -- the DIFF — old→new, ONE ROW PER CHANGED FIELD, timestamped and grouped by batch_id (one
        -- batch per create_case write) — so we never lose the history of what a case used to say.
        -- Lives in ledger.db (restore-guarded, authorizer denies DELETE/DROP) exactly like
        -- prediction_ledger. evidence_event_id/evidence_desc link a status-field change back to the
        -- specific docket_events line that produced it (best-effort, by date+keyword); a NULL evidence
        -- link on a status change is itself a signal — the value changed with no new docket evidence,
        -- i.e. a derivation change on unchanged raw data rather than a real new event.
        CREATE TABLE IF NOT EXISTS ledger.case_snapshots (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number       TEXT NOT NULL,
            batch_id          TEXT NOT NULL,          -- groups all field-changes from one write
            changed_at        TEXT NOT NULL DEFAULT (datetime('now')),
            source            TEXT NOT NULL,          -- baseline | initial | update
            model_version     TEXT NOT NULL,
            field             TEXT NOT NULL,
            old_value         TEXT,                   -- NULL on baseline/initial (genesis)
            new_value         TEXT,
            evidence_event_id INTEGER,                -- docket_events.id (best-effort; NULL if none)
            evidence_desc     TEXT                    -- durable copy of that docket line (survives event re-sync)
        );
        CREATE INDEX IF NOT EXISTS ledger.idx_cs_case  ON case_snapshots(case_number, changed_at);
        CREATE INDEX IF NOT EXISTS ledger.idx_cs_field ON case_snapshots(case_number, field);
        CREATE INDEX IF NOT EXISTS ledger.idx_cs_batch ON case_snapshots(batch_id);

        -- ── ACQUISITION INTELLIGENCE (Stage 2) — prod-owned human decisions in ledger.db ──
        -- comps: proposed NTREIS/Bridge candidates for a subject (refreshable snapshot of a fetch).
        -- listing_status 'closed' drives ARV; 'pending' is DIRECTIONAL-ONLY (never in ARV math).
        CREATE TABLE IF NOT EXISTS ledger.comps (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number    TEXT NOT NULL,
            mls_id         TEXT,
            fetch_batch    TEXT NOT NULL,           -- one uuid per propose; latest batch = current set
            fetched_at     TEXT NOT NULL DEFAULT (datetime('now')),
            listing_status TEXT,                    -- 'closed' | 'pending'
            address        TEXT,
            subdivision    TEXT,
            same_subdivision INTEGER DEFAULT 0,
            close_date     TEXT,
            close_price    INTEGER,                 -- reconstructed (ratio × acres); NULL for pending
            list_price     INTEGER,
            gla            INTEGER,
            beds           INTEGER,
            baths          REAL,
            year_built     INTEGER,
            distance_mi    REAL,
            match_score    INTEGER,
            adjusted_value INTEGER,
            photos_count   INTEGER,
            media_urls     TEXT,                    -- JSON array of hotlink URLs (Q2 — never stored blobs)
            arms_length_flags TEXT,                 -- JSON array
            comp_json      TEXT                     -- JSON of the full normalized+scored comp (audit)
        );
        CREATE INDEX IF NOT EXISTS ledger.idx_comps_case ON comps(case_number, fetched_at);

        -- comp_confirmations: append-only human decisions. A confirmation FREEZES the comp's data +
        -- adjusted value at decision time (frozen_comp) — a re-query can never silently move a
        -- confirmed ARV (Q1, same principle as case_snapshots). Latest decision per mls_id wins.
        CREATE TABLE IF NOT EXISTS ledger.comp_confirmations (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number    TEXT NOT NULL,
            mls_id         TEXT NOT NULL,
            decided_at     TEXT NOT NULL DEFAULT (datetime('now')),
            decided_by     TEXT,
            decision       TEXT NOT NULL,           -- 'confirmed' | 'rejected'
            adjusted_value INTEGER,                 -- FROZEN at confirmation
            frozen_comp    TEXT NOT NULL,           -- JSON snapshot of the comp at decision time
            note           TEXT
        );
        CREATE INDEX IF NOT EXISTS ledger.idx_cc_case ON comp_confirmations(case_number, decided_at);

        -- acquisition_analysis: append-only versions of the human inputs (repairs override, agreed
        -- price, lien stack) + the computed analysis snapshot. Latest version per case is current.
        CREATE TABLE IF NOT EXISTS ledger.acquisition_analysis (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            case_number     TEXT NOT NULL,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_by      TEXT,
            model_version   TEXT,
            repair_estimate INTEGER,
            agreed_price    INTEGER,
            lien_stack      TEXT,                   -- JSON [{type,amount|null,holder,...}]
            lien_status     TEXT,                   -- unavailable | partial | verified
            rule_pct_override REAL,
            confirmed_arv   INTEGER,
            valuation_state TEXT,                   -- provisional | confirmed
            decision        TEXT,
            analysis_json   TEXT                    -- full analyze() output at this version
        );
        CREATE INDEX IF NOT EXISTS ledger.idx_aa_case ON acquisition_analysis(case_number, updated_at);
        """)
    
    # Seed known benchmarks
    # Migration: add columns that may not exist in older DBs
    with get_db() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(cases)").fetchall()]
        for col, typedef in [
            ("petition_href", "TEXT"),
            ("property_intel", "TEXT"),
            ("legal_description", "TEXT"),
            ("petition_pdf", "TEXT"),
            ("owner_type", "TEXT"),
            ("owner_priority", "TEXT"),
            ("rep_assigned", "TEXT"),
            # Account-resolution state: 'resolved' (valid 17-digit account on file),
            # 'needs_lookup' (no account extracted, none resolvable — manual DCAD
            # lookup owed), 'invalid' (an account was extracted but malformed and
            # not resolvable). Written by discover.py; surfaced as a sidebar filter.
            ("account_status", "TEXT"),
            # Human-readable reason for the current account_status — e.g. why a case is
            # still in the backlog (uncorroborated candidate, out-of-county), or how an
            # account was auto-resolved. Written by discover.py / resolve_backlog.py.
            ("account_note", "TEXT"),
            # Which dataset/track a case belongs to (keeps OOS-timing vs lead pipelines
            # from blending): oos_timing | dismissed_owing | dismissed_paid | judged_pending
            # | active | personal_property. Stored + queryable (sidebar filter); see case_track_of().
            ("case_track", "TEXT"),
            # 'real' | 'personal' — from the Tyler docket's Comment field (REAL PROPERTY /
            # PERSONAL PROPERTY). BPP (personal) suits are a different instrument and are
            # excluded from real-estate timing/equity + the CITY_DATA calibration. Set by
            # discover.py at scrape time (cloud can't read dockets, so it's stored not derived).
            ("property_type", "TEXT"),
            # DERIVED caches of the prod-owned rep_actions log (which lives in ledger.db).
            # Recomputable from rep_actions at any time — see design §12 (WAL self-heal:
            # authoritative writes are single-DB, these caches self-heal on next write).
            ("deal_status", "TEXT"),      # not_contacted|contacted|in_conversation|offer_out|won|dead
            ("last_action_at", "TEXT"),
            # PROD-APPROVAL GATE (default 0 = held). sync_to_prod.py pushes ONLY prod_ready=1
            # cases, so a routine local→prod sync can never silently promote work-in-progress
            # leads — the structural fix for the 2026-07-13 36-case premature-sync incident.
            # Scraping (discover.py) never sets it, so every new scrape lands held; approval is
            # a deliberate, explicit act (sync_to_prod.py --approve). A case already live on
            # prod is implicitly approved (sync_to_prod reconciles it to 1). Local-only signal —
            # never sent up (SKIP_CASE_FIELDS); prod carries the column but doesn't use it.
            ("prod_ready", "INTEGER DEFAULT 0"),
        ]:
            if col not in cols:
                db.execute(f"ALTER TABLE cases ADD COLUMN {col} {typedef}")
                print(f"Migration: added {col}")
        # Seed the rep roster from any rep already assigned to a case, so the
        # roster starts as the real source of truth (no hardcoded placeholders).
        db.execute("INSERT OR IGNORE INTO reps (name) SELECT DISTINCT rep_assigned FROM cases "
                   "WHERE rep_assigned IS NOT NULL AND TRIM(rep_assigned) != ''")
        # Backfill account_status for rows migrated in before the column existed, so
        # none stay NULL (indistinguishable from a real state). Uses the SAME rule as
        # discover.py's valid_dcad_accounts() and the frontend's caseAccountStatus(),
        # so all three paths agree exactly (the counts must reconcile).
        for cn, acct in db.execute(
                "SELECT case_number, account_number FROM cases "
                "WHERE account_status IS NULL OR TRIM(account_status)=''").fetchall():
            db.execute("UPDATE cases SET account_status=? WHERE case_number=?",
                       [account_status_of(acct), cn])
        # Backfill case_track — parse property_intel for the live balance (None=unknown).
        # Re-derive whenever case_track is empty OR property_type is set but the track
        # doesn't yet reflect it (so a BPP case flips to personal_property once tagged).
        for cn, pt, od, jt, jd, tr, pij in db.execute(
                "SELECT case_number, property_type, oos_date, judgment_type, judgment_date, "
                "case_track, property_intel FROM cases "
                "WHERE case_track IS NULL OR TRIM(case_track)='' "
                "   OR (property_type='personal' AND case_track!='personal_property')").fetchall():
            bal = None
            if pij:
                try: bal = json.loads(pij).get("current_tax_balance")
                except Exception: bal = None
            db.execute("UPDATE cases SET case_track=? WHERE case_number=?",
                       [case_track_of(pt, od, jt, jd, bal), cn])
        # One-time case_snapshots BASELINE: every case already live at migration time gets a genesis
        # snapshot of its current field values (source='baseline', old=NULL), so history doesn't start
        # blank for the existing book — new changes then diff against a real baseline instead of a void.
        # Idempotent: only cases with ZERO snapshots are seeded, so re-running init_db never duplicates.
        seeded = {r[0] for r in db.execute(
            "SELECT DISTINCT case_number FROM ledger.case_snapshots").fetchall()}
        n_baseline = 0
        for row in db.execute("SELECT * FROM cases").fetchall():
            cn = row["case_number"]
            if not cn or cn in seeded:
                continue
            if snapshot_case(db, cn, None, dict(row), source="baseline"):
                n_baseline += 1
        if n_baseline:
            print(f"case_snapshots: seeded baseline history for {n_baseline} existing case(s)")
        db.commit()
    _seed_benchmarks()
    print(f"Database initialized: {DB_PATH}")

def _seed_benchmarks():
    benchmarks = [
        ("TX-23-00042","Williams/Motley — 3928 Atlanta St, Dallas","dallas","high",8,
         "2023-01-13","2026-02-16","nonjury","2026-05-15",37,89,25889.70,20,"oos_issued",
         json.dumps(["8 defendants","CBP at filing","estate/heir","3 continuances","2 prior suits"]),
         "CONFIRMED: 37mo filing→judgment, 89 days judgment→OOS",1),
        ("TX-25-00492","Hedge — 2827 E. Overton Rd, Dallas","dallas","low",1,
         "2025-03-14","2026-05-18","default",None,14,None,13704.32,4,"judgment_entered",
         json.dumps(["1 defendant","Rule 106","1 continuance","default judgment"]),
         "14 months filing→judgment. OOS projected ~Aug 2026",1),
        ("TX-25-01777","Stewart — Dallas","dallas","low",1,
         "2025-10-28",None,"none",None,None,None,None,None,"pre_judgment",
         json.dumps(["1 defendant","served 43 days","trial 07/20/2026","no complications"]),
         "PENDING: Trial 07/20/2026",0),
        ("TX-23-00569","Paula Williams / Chester Est — 1506 Harbor Rd, Dallas","dallas","high",5,
         "2023-04-03","2026-01-07","nonjury","2026-04-20",33,103,112449.25,35,"sale_pulled",
         json.dumps(["5 defendants","estate deceased","35yr delinquency","3 prior suits","sale PULLED 05/12/2026"]),
         "Sale pulled May 12 2026. Will resurface. Monitor docket.",1),
        ("TX-26-00009","Rogers — 1218 Hudspeth Ave, Dallas","dallas","low",1,
         "2026-01-05",None,"none",None,None,None,19366.44,7,"pre_judgment",
         json.dumps(["1 defendant","known address","unexecuted citation 02/11/2026","trial 09/30/2026"]),
         "PENDING: Trial 09/30/2026. Low complexity.",0),
    ]
    with get_db() as db:
        for b in benchmarks:
            db.execute("""
                INSERT OR IGNORE INTO benchmarks
                (case_number,description,city,complexity,def_count,filed_date,judgment_date,
                judgment_type,oos_date,filing_to_judgment_months,judgment_to_oos_days,
                total_due,delinquency_years,stage,key_factors,outcome,is_confirmed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, b)

# ─── PYDANTIC MODELS ──────────────────────────────────────────
class CaseCreate(BaseModel):
    case_number: str
    notes: Optional[str] = None

class WatchListAdd(BaseModel):
    case_number: str
    notes: Optional[str] = None

class CaseUpdate(BaseModel):
    case_status: Optional[str] = None
    judgment_date: Optional[str] = None
    judgment_type: Optional[str] = None
    oos_date: Optional[str] = None
    oos_issued: Optional[bool] = None
    next_hearing_date: Optional[str] = None
    stage: Optional[str] = None
    ai_memo: Optional[str] = None
    complexity: Optional[str] = None
    notes: Optional[str] = None

# ─── PREDICTION LEDGER CONSTANTS (see docs/prediction-ledger-design.md §3) ────
# Bump on ANY change to compute_projection() logic OR the CITY_DATA constants. Every ledger
# row stores the version it was made under, so calibration can compare accuracy across models
# ("did the recalibration improve error?"). Format: date + short descriptor.
MODEL_VERSION = "2026-07-13-ftj-frozen-joos-bimodal"

# A forward prediction whose projected_oos has passed by more than this with no real outcome is
# marked expired_no_oos (a measured miss, not a silent gap). Named, not inline — the ledger
# should eventually tell us whether 90 is the right threshold (§12).
PREDICTION_EXPIRY_DAYS = 90

# ─── CITY BENCHMARKS ──────────────────────────────────────────
CITY_DATA = {
    # FROZEN (ftj). Do NOT recalibrate from scorecard data without explicit sign-off.
    # A 2026-07-09 recalibration to ftj [7,11]/[10,16]/[15,28] on n=8 OOS cases was
    # reverted — sample too small. scorecard.py may REPORT observed windows (always
    # tagged with n=), but must not write these constants. Revisit threshold: >=40
    # closed cases with a real oos_date, and only with an explicit go-ahead.
    #
    # joos (judgment->OOS) is a POINT over BIMODAL data — do not trust it as precise.
    # Observed n=10: [0,0,40,41,41,70,119 | 360,379,557] — a fast cluster (~40-120d) and a
    # contested tail (~360-557d). These constants (60/75/89) capture only the fast path, so
    # the projected OOS DATE carries wide uncertainty even post-judgment. That's why
    # post-judgment confidence is moderate (~55%), NOT high — a judgment tells us OOS will
    # likely happen, not WHEN. Same >=40-case, explicit-sign-off bar before recalibrating.
    "dallas":     {"ftj":{"low":[12,18],"medium":[18,30],"high":[30,48]}, "joos":{"low":60,"medium":75,"high":89}},
    "fort_worth": {"ftj":{"low":[10,15],"medium":[15,24],"high":[24,36]}, "joos":{"low":45,"medium":60,"high":75}},
    "houston":    {"ftj":{"low":[18,28],"medium":[28,42],"high":[42,60]}, "joos":{"low":75,"medium":90,"high":120}},
    "austin":     {"ftj":{"low":[10,16],"medium":[16,26],"high":[26,38]}, "joos":{"low":45,"medium":65,"high":80}},
}

def compute_projection(case: dict) -> dict:
    # Business personal-property suits are a different legal instrument — no house, no
    # real equity, no real-estate Order of Sale. They must be EXCLUDED from real-estate
    # timing/equity math, not just labeled. Short-circuit to N/A.
    if case.get("property_type") == "personal" or case.get("case_track") == "personal_property":
        return {
            "projected_oos": None, "confidence_pct": 0,
            "filing_to_judgment_months": None, "days_to_oos": None,
            "projection_na": True, "projection_na_reason": "business personal property",
            "basis": "na_bpp",
        }
    city = CITY_DATA.get(case.get("city","dallas"), CITY_DATA["dallas"])
    complexity = case.get("complexity","medium")
    complexity = complexity if complexity in city["ftj"] else "low"
    ranges = city["ftj"][complexity]
    oos_days = city["joos"][complexity]
    
    filed = case.get("filed_date")
    judged = case.get("judgment_date")
    next_hearing = case.get("next_hearing_date")
    
    from datetime import datetime, timedelta
    now = datetime.now()

    # A REAL Order of Sale trumps any projection. If oos_date is on record, show THAT
    # confirmed date — never a computed estimate over a confirmed fact. (Was: all 10
    # confirmed-OOS cases showed a fabricated projected date at 85%.)
    real_oos = (case.get("oos_date") or "").strip()
    if real_oos:
        fjm = None
        if filed and judged:
            try: fjm = round((datetime.strptime(judged, "%Y-%m-%d") - datetime.strptime(filed, "%Y-%m-%d")).days / 30)
            except Exception: fjm = None
        try: dto = (datetime.strptime(real_oos, "%Y-%m-%d") - now).days
        except Exception: dto = None
        return {
            "projected_oos": real_oos, "confidence_pct": 100, "oos_confirmed": True,
            "filing_to_judgment_months": fjm, "days_to_oos": dto, "basis": "confirmed",
        }

    proj_oos = None
    confidence = 0
    fj_months = None
    basis = "none"

    if judged:
        j_date = datetime.strptime(judged, "%Y-%m-%d")
        proj_oos = j_date + timedelta(days=oos_days)
        fj_months = round((j_date - datetime.strptime(filed, "%Y-%m-%d")).days / 30) if filed else None
        confidence = 55   # joos is bimodal (fast ~40-120d vs contested ~360-557d) — date is uncertain even post-judgment
        basis = "judged"
    elif next_hearing:
        nh = datetime.strptime(next_hearing, "%Y-%m-%d")
        if nh > now:
            est_judgment = nh + timedelta(days=7)
            proj_oos = est_judgment + timedelta(days=oos_days)
            fj_months = round((est_judgment - datetime.strptime(filed, "%Y-%m-%d")).days / 30) if filed else None
            confidence = 45
            basis = "next_hearing"
    elif filed:
        mid = round((ranges[0]+ranges[1])/2)
        est_judgment = datetime.strptime(filed, "%Y-%m-%d") + timedelta(days=mid*30)
        proj_oos = est_judgment + timedelta(days=oos_days)
        fj_months = mid
        confidence = {"low":40,"medium":30,"high":22}[complexity]
        basis = "filed"

    result = {
        "projected_oos": proj_oos.strftime("%Y-%m-%d") if proj_oos else None,
        "confidence_pct": confidence,
        "filing_to_judgment_months": fj_months,
        "days_to_oos": (proj_oos - now).days if proj_oos else None,
        "basis": basis,
    }
    # STALENESS: the projected OOS date has passed and NO real OOS is on record — the
    # prediction failed. Don't keep flashing stale confidence; say so explicitly.
    if proj_oos and proj_oos < now:
        result["projection_stale"] = True
        result["projection_failed_reason"] = "no OOS as of %s (predicted %s)" % (
            now.strftime("%Y-%m-%d"), proj_oos.strftime("%Y-%m-%d"))
        result["confidence_pct"] = 0
        # NB: `basis` stays the underlying branch (judged/filed/next_hearing) — staleness is a
        # DISPLAY status (projection_stale), not how the prediction was made. For the ledger, a
        # stale forward prediction keeps basis='judged' and is resolved to expired_no_oos by the
        # sweep — so it's still counted as the (failed) forward prediction it was.
    return result

# ─── PREDICTION LEDGER (design doc §6) — logged on the WRITE path only, never reads ──
def _prediction_snapshot(case: dict, proj: dict) -> dict:
    """The immutable inputs + outputs that define one prediction (for input_hash + the row)."""
    city = CITY_DATA.get(case.get("city", "dallas"), CITY_DATA["dallas"])
    complexity = case.get("complexity", "medium")
    complexity = complexity if complexity in city["ftj"] else "low"
    ftj = city["ftj"][complexity]; joos = city["joos"][complexity]
    return {
        "case_number": case.get("case_number"),
        "model_version": MODEL_VERSION,
        "prediction_basis": proj.get("basis", "none"),
        "projected_oos": proj.get("projected_oos"),
        "confidence_pct": proj.get("confidence_pct"),
        "days_to_oos": proj.get("days_to_oos"),
        "filing_to_judgment_months": proj.get("filing_to_judgment_months"),
        "in_city": case.get("city", "dallas"),
        "in_complexity": complexity,
        "in_stage": case.get("stage"),
        "in_filed_date": case.get("filed_date"),
        "in_judgment_date": case.get("judgment_date"),
        "in_next_hearing_date": case.get("next_hearing_date"),
        "in_oos_issued": 1 if case.get("oos_issued") else 0,
        "used_joos_days": joos, "used_ftj_low": ftj[0], "used_ftj_high": ftj[1],
    }

def _prediction_input_hash(snap: dict) -> str:
    # Hash the PREDICTION DRIVERS (NOT predicted_at, and NOT confidence_pct/in_stage which are
    # derived or display-mutated — confidence gets zeroed when a projection goes stale, and
    # stage is contextual metadata; including either would log a spurious new row when nothing
    # about the prediction actually changed). basis + projected_oos + the driving inputs +
    # model_version fully identify a prediction; a MODEL_VERSION bump forces a fresh row.
    keys = ["model_version", "prediction_basis", "projected_oos", "in_city",
            "in_complexity", "in_filed_date", "in_judgment_date",
            "in_next_hearing_date", "in_oos_issued"]
    return hashlib.sha256("|".join(str(snap.get(k)) for k in keys).encode()).hexdigest()

def log_prediction(db, case: dict, proj: dict):
    """Append a prediction_ledger row IF it meaningfully differs from this case's most-recent
    row (input_hash change) — new row per meaningful change, never version-in-place. Called
    from create_case (the WRITE path) only. BPP (na_bpp) is not a real-estate prediction and is
    not logged. Authoritative write to ledger.db is single-file; a crash-split just means the
    row re-logs next write (machine-derived, self-heals — design §12)."""
    snap = _prediction_snapshot(case, proj)
    if not snap["case_number"] or snap["prediction_basis"] == "na_bpp":
        return
    h = _prediction_input_hash(snap)
    last = db.execute("SELECT input_hash FROM ledger.prediction_ledger WHERE case_number=? "
                      "ORDER BY id DESC LIMIT 1", [snap["case_number"]]).fetchone()
    if last and last["input_hash"] == h:
        return  # nothing meaningful changed since the last recorded prediction
    cols = list(snap.keys()) + ["input_hash"]
    db.execute(f"INSERT INTO ledger.prediction_ledger ({','.join(cols)}) "
               f"VALUES ({','.join(['?']*len(cols))})", [snap[k] for k in snap] + [h])

def reconcile(db, case_number: str):
    """When a real outcome lands (oos_date / dismissal / sale), resolve this case's unresolved
    FORWARD-prediction rows against it — one-time pending->resolved, prediction fields never
    touched. Idempotent (only outcome_type IS NULL rows). Called from create_case after the
    case row is written."""
    row = db.execute("SELECT oos_date, case_track, sale_scheduled_date FROM cases "
                     "WHERE case_number=?", [case_number]).fetchone()
    if not row:
        return
    oos = (row["oos_date"] or "").strip()
    track = row["case_track"] or ""
    outcome_type = outcome_date = None
    if oos:
        outcome_type, outcome_date = "oos_issued", oos
    elif track in ("dismissed_owing", "dismissed_paid"):
        outcome_type = "dismissed"          # a dismissal has no single 'outcome date' we trust
    elif row["sale_scheduled_date"]:
        outcome_type, outcome_date = "sale", row["sale_scheduled_date"]
    if not outcome_type:
        return                              # no resolving signal yet
    now_iso = datetime.now().isoformat()
    for r in db.execute(
            "SELECT id, projected_oos FROM ledger.prediction_ledger WHERE case_number=? "
            "AND outcome_type IS NULL AND prediction_basis IN ('judged','next_hearing','filed')",
            [case_number]).fetchall():
        err = None
        if outcome_type == "oos_issued" and r["projected_oos"] and outcome_date:
            try:
                err = (datetime.strptime(outcome_date, "%Y-%m-%d")
                       - datetime.strptime(r["projected_oos"], "%Y-%m-%d")).days
            except Exception:
                err = None
        db.execute("UPDATE ledger.prediction_ledger SET outcome_type=?, outcome_date=?, "
                   "error_days=?, resolved_at=? WHERE id=?",
                   [outcome_type, outcome_date, err, now_iso, r["id"]])

def sweep_expired(db) -> int:
    """Batch: mark forward predictions whose projected_oos passed by > PREDICTION_EXPIRY_DAYS
    with no outcome as expired_no_oos (a measured miss). Batch, not per-case — a case that goes
    quiet is exactly when it needs this. Returns count marked."""
    now = datetime.now(); n = 0
    for r in db.execute(
            "SELECT id, projected_oos FROM ledger.prediction_ledger WHERE outcome_type IS NULL "
            "AND prediction_basis IN ('judged','next_hearing','filed') "
            "AND projected_oos IS NOT NULL").fetchall():
        try:
            if (now - datetime.strptime(r["projected_oos"], "%Y-%m-%d")).days > PREDICTION_EXPIRY_DAYS:
                db.execute("UPDATE ledger.prediction_ledger SET outcome_type='expired_no_oos', "
                           "resolved_at=? WHERE id=?", [now.isoformat(), r["id"]])
                n += 1
        except Exception:
            pass
    return n

# ─── CASE-FIELD HISTORY (case_snapshots) — diff-on-write, append-only, on the WRITE path only ──
#
# Material case FACTS to snapshot (allowlist). Deliberately EXCLUDES churn/display fields
# (updated_at, last_agent_run, confidence_pct, projected_oos) that are recomputed on every write and
# would drown the signal — same "log DRIVERS, not derived-display noise" discipline as the
# prediction ledger's input_hash. property_intel (a big JSON blob) is NOT snapshotted raw: its two
# material sub-values (market_value, current_tax_balance = the live balance) are tracked as their own
# fields, plus a content hash so "enrichment changed" is recorded without storing the whole blob.
SNAPSHOT_DIRECT_FIELDS = [
    "total_due_filing", "property_address", "account_number", "account_status",
    "case_track", "stage", "oos_issued", "oos_date", "judgment_date", "judgment_type",
    "sale_scheduled_date", "sale_pulled_date", "def_count", "complexity",
]
SNAPSHOT_PI_FIELDS = ["pi_market_value", "pi_tax_balance", "property_intel_hash"]
SNAPSHOT_FIELDS = SNAPSHOT_DIRECT_FIELDS + SNAPSHOT_PI_FIELDS

# Fields whose change is expected to be caused by a specific docket line. A change here WITHOUT a
# matching docket event (evidence NULL) means the derived value moved on unchanged raw data — the
# derivation-bug signal, distinct from a real new event (evidence present).
EVIDENCE_KEYWORDS = {
    "oos_date":            ["ORDER OF SALE"],
    "oos_issued":         ["ORDER OF SALE"],
    "judgment_date":       ["JUDGMENT"],
    "judgment_type":       ["JUDGMENT"],
    "sale_scheduled_date": ["SALE"],
    "sale_pulled_date":    ["WITHDRAW", "PULL", "CANCEL", "PASSED", "RESET", "VACAT"],
}


def _pi_subvalues(pi_raw):
    """From a property_intel JSON string, extract (market_value, tax_balance, content_hash).
    The hash excludes volatile timestamp/error fields so it only changes on real content change."""
    if not pi_raw:
        return None, None, None
    try:
        pi = json.loads(pi_raw)
    except (ValueError, TypeError):
        return None, None, None
    if not isinstance(pi, dict):
        return None, None, None
    mv = pi.get("market_value")
    bal = pi.get("current_tax_balance")
    stable = {k: v for k, v in pi.items() if k not in ("enriched_at", "errors", "street_view_url")}
    h = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return mv, bal, h


def _snapshot_view(row):
    """The tracked-field view of a case row (dict). Direct columns + the three property_intel-derived
    fields. Returns {field: value} over SNAPSHOT_FIELDS; missing columns default to None."""
    row = dict(row) if row else {}
    view = {f: row.get(f) for f in SNAPSHOT_DIRECT_FIELDS}
    mv, bal, h = _pi_subvalues(row.get("property_intel"))
    view["pi_market_value"] = mv
    view["pi_tax_balance"] = bal
    view["property_intel_hash"] = h
    return view


def _norm_date(v):
    """Normalize a date-ish value to 'YYYY-MM-DD' for comparison, or None."""
    if not v:
        return None
    s = str(v).strip()
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else None


def _snap_str(v):
    """Canonical string form for a tracked value. Normalizes numerics so a change-detection compare
    doesn't fire on REAL-vs-int noise (5000.0 == 5000 == "5000") when a value round-trips through the
    SQLite REAL column and back — an integral float and its int must read as unchanged."""
    if v is None:
        return None
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f.is_integer() else repr(f)
    s = str(v)
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else s
    except (ValueError, TypeError):
        return s


def _resolve_evidence(db, case_number, field, new_value):
    """Best-effort link from a status-field change to the docket_events line that produced it.
    Returns (event_id, description) or (None, None). Prefers an exact date match among events whose
    description matches the field's keywords; falls back to the most-recent keyword match. Requires
    the causing event to already be present (sync pushes events BEFORE the case for exactly this)."""
    kws = EVIDENCE_KEYWORDS.get(field)
    if not kws or new_value in (None, "", 0, "0"):
        return None, None
    rows = db.execute("SELECT id, event_date, description FROM docket_events WHERE case_number=?",
                      [case_number]).fetchall()
    cand = [r for r in rows if any(k in (r["description"] or "").upper() for k in kws)]
    if not cand:
        return None, None
    nd = _norm_date(new_value)
    if nd:
        exact = [r for r in cand if _norm_date(r["event_date"]) == nd]
        if exact:
            return exact[0]["id"], exact[0]["description"]
    cand.sort(key=lambda r: (_norm_date(r["event_date"]) or ""), reverse=True)
    return cand[0]["id"], cand[0]["description"]


def snapshot_case(db, case_number, old_row, new_row, source):
    """Append case_snapshots rows for each tracked field that changed old→new. old_row None = genesis
    (only non-empty new values are recorded, to avoid an all-NULL baseline). Returns the batch dict
    {batch_id, changed:[fields]} or None if nothing changed. Insert-only; never updates a prior row."""
    old_view = _snapshot_view(old_row) if old_row is not None else {}
    new_view = _snapshot_view(new_row)
    batch = uuid.uuid4().hex
    changed = []
    for f in SNAPSHOT_FIELDS:
        ov = old_view.get(f) if old_row is not None else None
        nv = new_view.get(f)
        if _snap_str(ov) == _snap_str(nv):
            continue
        if old_row is None and (nv is None or nv == ""):
            continue   # don't seed a genesis row for a field that has no value yet
        eid, edesc = _resolve_evidence(db, case_number, f, nv)
        db.execute(
            "INSERT INTO ledger.case_snapshots (case_number, batch_id, source, model_version, "
            "field, old_value, new_value, evidence_event_id, evidence_desc) VALUES (?,?,?,?,?,?,?,?,?)",
            [case_number, batch, source, MODEL_VERSION, f, _snap_str(ov), _snap_str(nv), eid, edesc])
        changed.append(f)
    return {"batch_id": batch, "changed": changed} if changed else None


# ─── API ROUTES ───────────────────────────────────────────────

@app.get("/")
async def root():
    # Serve the frontend dashboard
    for path in [
        BASE_DIR / "frontend" / "index.html",
        Path("/app/frontend/index.html"),
        Path("frontend/index.html"),
    ]:
        if path.exists():
            return HTMLResponse(path.read_text())
    return HTMLResponse("""<!DOCTYPE html>
<html><head><title>PC Peak Platform</title></head>
<body style="font-family:sans-serif;padding:40px;background:#f4f2ed">
<h1 style="color:#1e3a5f">PC Peak Tax Foreclosure Intelligence</h1>
<p>Platform API is running. Frontend loading...</p>
<p><a href="/api/stats">View API Stats</a></p>
</body></html>""")

@app.get("/favicon.ico")
async def favicon():
    return HTMLResponse("")

@app.get("/api/cases")
async def get_cases(status: str = None, stage: str = None, city: str = None):
    with get_db() as db:
        q = "SELECT * FROM cases WHERE 1=1"
        params = []
        if status: q += " AND case_status=?"; params.append(status)
        if stage:  q += " AND stage=?"; params.append(stage)
        if city:   q += " AND city=?"; params.append(city)
        q += " ORDER BY updated_at DESC"
        rows = db.execute(q, params).fetchall()
        cases = []
        for r in rows:
            c = dict(r)
            c["projection"] = compute_projection(c)
            for field in ["all_defendants","delinquency_years","prior_suits","tax_breakdown"]:
                if c.get(field):
                    try: c[field] = json.loads(c[field])
                    except: pass
            cases.append(c)
        return cases

@app.get("/api/cases/{case_number}")
async def get_case(case_number: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM cases WHERE case_number=?", [case_number]).fetchone()
        if not row:
            raise HTTPException(404, f"Case {case_number} not found")
        c = dict(row)
        c["projection"] = compute_projection(c)
        c["events"] = [dict(e) for e in db.execute(
            "SELECT * FROM docket_events WHERE case_number=? ORDER BY event_date DESC", [case_number]
        ).fetchall()]
        for field in ["all_defendants","delinquency_years","prior_suits","tax_breakdown"]:
            if c.get(field):
                try: c[field] = json.loads(c[field])
                except: pass
        return c

@app.post("/api/cases")
async def create_case(data: dict):
    """Upsert a case. A partial payload (e.g. just property_intel or
    rep_assigned) is merged onto the existing row BEFORE the projection is
    recomputed, so partial updates never clobber stored fields like
    projected_oos/confidence_pct with nulls computed from missing dates."""
    cn = data.get("case_number")
    if not cn:
        raise HTTPException(400, "case_number required")

    # Serialize incoming JSON list fields
    for field in ["all_defendants","delinquency_years","prior_suits","tax_breakdown"]:
        if isinstance(data.get(field), list):
            data[field] = json.dumps(data[field])

    with get_db() as db:
        # compute_projection() also emits transient fields (filing_to_judgment_months,
        # days_to_oos) that aren't columns; filtering to real columns avoids
        # "table cases has no column named ..." on write.
        valid_cols = {r[1] for r in db.execute("PRAGMA table_info(cases)").fetchall()}
        existing = db.execute("SELECT * FROM cases WHERE case_number=?", [cn]).fetchone()

        # Merge the incoming payload onto the existing row, then recompute the
        # projection from that complete picture.
        merged = dict(existing) if existing else {}
        merged.update({k: v for k, v in data.items() if k in valid_cols})
        merged.update(compute_projection(merged))

        write = {k: v for k, v in merged.items() if k in valid_cols and k not in ("case_number", "id")}
        write["updated_at"] = datetime.now().isoformat()

        if existing:
            sets = ", ".join([f"{k}=?" for k in write])
            db.execute(f"UPDATE cases SET {sets} WHERE case_number=?", list(write.values()) + [cn])
        else:
            write["case_number"] = cn
            write["created_at"] = datetime.now().isoformat()
            cols = ", ".join(write.keys())
            placeholders = ", ".join(["?" for _ in write])
            db.execute(f"INSERT INTO cases ({cols}) VALUES ({placeholders})", list(write.values()))

        # Invariant: any rep assigned to a case exists in the roster.
        rep = (write.get("rep_assigned") or "").strip()
        if rep:
            db.execute("INSERT OR IGNORE INTO reps (name) VALUES (?)", [rep])

        # Prediction ledger (design §6): record this prediction if it meaningfully changed,
        # then reconcile any unresolved forward predictions against a real outcome. This is the
        # sanctioned WRITE-path log point (never the read paths). `merged` carries both the case
        # inputs and the compute_projection outputs (incl. basis). Wrapped so ledger logging can
        # never break a case write.
        try:
            merged["case_number"] = cn
            log_prediction(db, merged, merged)
            reconcile(db, cn)
        except Exception:
            pass

        # Case-field history (case_snapshots): diff the pre-write row against the just-written state
        # and append one row per changed material field. 'initial' = a case's first-ever write
        # (genesis, old=NULL); 'update' = a change to an existing case. Evidence resolves against
        # docket_events, which sync pushes BEFORE the case so the causing line is already present.
        # Wrapped so snapshot logging can never break a case write (same discipline as log_prediction).
        try:
            snapshot_case(db, cn, dict(existing) if existing else None, merged,
                          source="update" if existing else "initial")
        except Exception:
            pass

    return {"status":"ok","case_number":cn}

@app.patch("/api/cases/{case_number}")
async def update_case(case_number: str, data: CaseUpdate):
    with get_db() as db:
        updates = {k:v for k,v in data.dict().items() if v is not None}
        updates["updated_at"] = datetime.now().isoformat()
        sets = ", ".join([f"{k}=?" for k in updates])
        db.execute(f"UPDATE cases SET {sets} WHERE case_number=?",
                   list(updates.values()) + [case_number])
    return {"status":"ok"}

@app.delete("/api/cases/{case_number}")
async def delete_bpp_case(case_number: str):
    """Delete a case ONLY if it is business personal property (property_type='personal').

    This platform does not store personal-property data. The BPP constraint is enforced IN THE
    DELETE's WHERE CLAUSE — the query is structurally incapable of removing a real-property (or
    NULL / 'unknown') row, no matter what case_number is passed. It is NOT an application-level
    check that trusts the caller: a real case number simply matches 0 rows and is refused. Same
    "physically impossible, not procedurally discouraged" standard as the ledger.db guard."""
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM cases WHERE case_number=? AND property_type='personal'", [case_number])
        if cur.rowcount == 0:
            row = db.execute("SELECT property_type FROM cases WHERE case_number=?", [case_number]).fetchone()
            if row is None:
                raise HTTPException(404, f"{case_number} not found")
            raise HTTPException(409, f"refused: {case_number} is not a personal-property case "
                                     f"(property_type={row['property_type']!r}); this endpoint can "
                                     f"only delete BPP")
        # It WAS a BPP case and is now gone — remove its docket events too.
        db.execute("DELETE FROM docket_events WHERE case_number=?", [case_number])
    return {"status": "deleted", "case_number": case_number, "was": "personal_property"}

@app.get("/api/ledger/export")
async def ledger_export(x_ledger_token: str = Header(default="")):
    """Read-only export of the PROD-OWNED tables (prediction_ledger + rep_actions) — the access
    path for the durability backup (Step 4) and scorecard analysis (Step 6, design §11). No local
    write-back; prod is the source of truth for these.

    TOKEN-GATED (unlike the otherwise-open API): rep_actions holds rep contact/offer data that must
    not be world-readable. Fail-CLOSED — if LEDGER_EXPORT_TOKEN isn't configured in the env, the
    endpoint is unavailable (503), never open by accident. Constant-time token compare."""
    want = os.environ.get("LEDGER_EXPORT_TOKEN", "")
    if not want:
        raise HTTPException(503, "ledger export not configured (LEDGER_EXPORT_TOKEN unset)")
    if not x_ledger_token or not hmac.compare_digest(x_ledger_token, want):
        raise HTTPException(401, "unauthorized")
    with get_db() as db:
        pl = [dict(r) for r in db.execute("SELECT * FROM ledger.prediction_ledger ORDER BY id")]
        ra = [dict(r) for r in db.execute("SELECT * FROM ledger.rep_actions ORDER BY id")]
        cs = [dict(r) for r in db.execute("SELECT * FROM ledger.case_snapshots ORDER BY id")]
    return {"prediction_ledger": pl, "rep_actions": ra, "case_snapshots": cs,
            "counts": {"prediction_ledger": len(pl), "rep_actions": len(ra),
                       "case_snapshots": len(cs)}}

# ─── REP ROSTER ───────────────────────────────────────────────
# The reps table is the source of truth for the assignable-rep list. Cases
# store the rep NAME (denormalized) for display; rename cascades to cases, and
# removal is a soft-delete (active=0) that preserves history — a separate
# reassign moves a rep's cases to someone else.

class RepIn(BaseModel):
    name: str

class RepPatch(BaseModel):
    name: Optional[str] = None
    active: Optional[int] = None

class Reassign(BaseModel):
    from_rep: str
    to_rep: str = ""   # "" = unassign

@app.get("/api/reps")
async def list_reps():
    with get_db() as db:
        rows = db.execute(
            "SELECT r.id, r.name, r.active, "
            "(SELECT COUNT(*) FROM cases c WHERE c.rep_assigned = r.name) AS case_count "
            "FROM reps r ORDER BY r.active DESC, r.name COLLATE NOCASE"
        ).fetchall()
        return [dict(x) for x in rows]

@app.post("/api/reps")
async def add_rep(r: RepIn):
    name = (r.name or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    with get_db() as db:
        existing = db.execute("SELECT id FROM reps WHERE name=? COLLATE NOCASE", [name]).fetchone()
        if existing:
            db.execute("UPDATE reps SET active=1 WHERE id=?", [existing["id"]])  # reactivate
            return {"status":"ok", "id": existing["id"], "name": name}
        cur = db.execute("INSERT INTO reps (name) VALUES (?)", [name])
        return {"status":"ok", "id": cur.lastrowid, "name": name}

@app.patch("/api/reps/{rep_id}")
async def update_rep(rep_id: int, u: RepPatch):
    with get_db() as db:
        row = db.execute("SELECT name FROM reps WHERE id=?", [rep_id]).fetchone()
        if not row:
            raise HTTPException(404, "rep not found")
        old = row["name"]
        if u.name is not None and u.name.strip() and u.name.strip() != old:
            new = u.name.strip()
            clash = db.execute("SELECT 1 FROM reps WHERE name=? COLLATE NOCASE AND id!=?", [new, rep_id]).fetchone()
            if clash:
                raise HTTPException(409, "a rep with that name already exists")
            db.execute("UPDATE reps SET name=? WHERE id=?", [new, rep_id])
            # cascade the rename to every case that rep owns
            db.execute("UPDATE cases SET rep_assigned=?, updated_at=datetime('now') WHERE rep_assigned=?", [new, old])
        if u.active is not None:
            db.execute("UPDATE reps SET active=? WHERE id=?", [1 if u.active else 0, rep_id])
    return {"status":"ok"}

@app.delete("/api/reps/{rep_id}")
async def deactivate_rep(rep_id: int):
    # Soft-delete: keep the rep row + their case history; just remove them from
    # the assignable roster. Reassign separately if their cases need a new owner.
    with get_db() as db:
        if not db.execute("SELECT 1 FROM reps WHERE id=?", [rep_id]).fetchone():
            raise HTTPException(404, "rep not found")
        db.execute("UPDATE reps SET active=0 WHERE id=?", [rep_id])
    return {"status":"ok"}

@app.post("/api/reps/reassign")
async def reassign_cases(r: Reassign):
    src = (r.from_rep or "").strip()
    dst = (r.to_rep or "").strip()
    if not src:
        raise HTTPException(400, "from_rep required")
    with get_db() as db:
        cur = db.execute("UPDATE cases SET rep_assigned=?, updated_at=datetime('now') WHERE rep_assigned=?", [dst, src])
        moved = cur.rowcount
        if dst:
            db.execute("INSERT OR IGNORE INTO reps (name) VALUES (?)", [dst])
            db.execute("UPDATE reps SET active=1 WHERE name=? COLLATE NOCASE", [dst])
    return {"status":"ok", "moved": moved, "from": src, "to": dst}

@app.get("/api/events/{case_number}")
async def get_events(case_number: str):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM docket_events WHERE case_number=? ORDER BY event_date DESC, id DESC",
            [case_number]
        ).fetchall()
        return [dict(r) for r in rows]

@app.post("/api/events/{case_number}")
async def add_events(case_number: str, events: List[dict]):
    with get_db() as db:
        for ev in events:
            db.execute(
                "INSERT OR IGNORE INTO docket_events (case_number,event_date,event_type,description,detail,is_new) VALUES (?,?,?,?,?,?)",
                [case_number, ev.get("date"), ev.get("type"), ev.get("description"), ev.get("detail",""), ev.get("is_new",0)]
            )
    return {"status":"ok","added":len(events)}

@app.get("/api/cases/{case_number}/snapshots")
async def get_case_snapshots(case_number: str):
    """Case-field history: every recorded old→new change for this case, newest first, plus the most
    recent change-batch grouped for convenience (what a 'Refresh' would show as 'what changed'). Open
    read like the rest of the case API — case_snapshots holds case facts, not rep PII."""
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM ledger.case_snapshots WHERE case_number=? ORDER BY id DESC",
            [case_number]).fetchall()]
    latest = None
    if rows:
        top = rows[0]["batch_id"]
        changes = [r for r in rows if r["batch_id"] == top]
        latest = {
            "batch_id": top,
            "changed_at": changes[0]["changed_at"],
            "source": changes[0]["source"],
            "fields": [{"field": r["field"], "old": r["old_value"], "new": r["new_value"],
                        "evidence_event_id": r["evidence_event_id"],
                        "evidence_desc": r["evidence_desc"]} for r in changes],
        }
    return {"case_number": case_number, "count": len(rows),
            "latest_batch": latest, "snapshots": rows}


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# ACQUISITION INTELLIGENCE (Stage 2) — comp propose/confirm workbench + confirmed-ARV analysis.
# Token-gated (ACQUISITION_TOKEN, fail-closed) — the analysis carries rep negotiation inputs (agreed
# price) and the propose step spends an external NTREIS call. §5.4 enforced end-to-end: a confirmed
# ARV (verified) comes ONLY from human-confirmed comps; anything else stays provisional.
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

# Test seam: when set, propose() draws candidates from this instead of the live NTREIS feed. It takes
# the subject dict and returns {"closed":[...], "pending":[...]} of normalized comps.
_COMP_SOURCE = None


class CompDecision(BaseModel):
    decided_by: str = ""
    note: str = ""

class AcqInputs(BaseModel):
    updated_by: str = ""
    repair_estimate: Optional[int] = None
    agreed_price: Optional[int] = None
    lien_stack: list = []
    lien_status: str = "unavailable"
    rule_pct_override: Optional[float] = None

class ProposeIn(BaseModel):
    include_pending: bool = True


def _parse_pi(case: dict) -> dict:
    pi = case.get("property_intel")
    if isinstance(pi, str):
        try: pi = json.loads(pi)
        except Exception: pi = {}
    return pi or {}


def _case_input(case: dict, pi: dict):
    """Build an acquisition.CaseInput from a case row + parsed property_intel (design §3.4).
    'owed' is the live current_tax_balance, NOT total_due_filing."""
    distress = pi.get("distress") or {}
    owners = pi.get("owners") or []
    return acquisition.CaseInput(
        case_number=case.get("case_number"),
        market_value=pi.get("market_value"),
        owed=pi.get("current_tax_balance"),
        total_due_filing=case.get("total_due_filing"),
        filed_date=case.get("filed_date"),
        judgment_date=case.get("judgment_date"),
        living_area_sqft=pi.get("living_area_sqft"),
        depreciation_pct=pi.get("depreciation_pct"),
        actual_age=pi.get("actual_age"),
        year_built=pi.get("year_built") or pi.get("effective_year_built"),
        distress_level=distress.get("level"),
        distress_signals=[s.get("type") for s in distress.get("signals", [])],
        estate=bool(case.get("estate_heir") or case.get("owner_type") == "estate" or pi.get("estate_flag")),
        is_absentee=bool(pi.get("is_absentee")),
        no_homestead=bool(pi.get("no_homestead")),
        property_type=case.get("property_type") or "real",
        case_track=case.get("case_track"),
        oos_date=case.get("oos_date"),
        sale_scheduled_date=case.get("sale_scheduled_date"),
        owner_of_record=(owners[0].get("name") if owners else None),
    )


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else round((vals[n // 2 - 1] + vals[n // 2]) / 2)


def _latest_confirmations(db, cn):
    """Latest decision per mls_id (append-only log; last write wins)."""
    latest = {}
    for r in db.execute("SELECT * FROM ledger.comp_confirmations WHERE case_number=? ORDER BY id", [cn]).fetchall():
        latest[r["mls_id"]] = dict(r)
    return latest


def _build_acquisition(case_number: str) -> dict:
    """Assemble the full analysis: confirmed comps (frozen) → confirmed ARV (verified) → analyze().
    Falls back to a provisional ARV from the latest proposed closed comps when nothing is confirmed."""
    with get_db() as db:
        row = db.execute("SELECT * FROM cases WHERE case_number=?", [case_number]).fetchone()
        if not row:
            raise HTTPException(404, f"Case {case_number} not found")
        case = dict(row)
        aa = db.execute("SELECT * FROM ledger.acquisition_analysis WHERE case_number=? ORDER BY id DESC LIMIT 1",
                        [case_number]).fetchone()
        confirmations = _latest_confirmations(db, case_number)
        batch = db.execute("SELECT fetch_batch FROM ledger.comps WHERE case_number=? ORDER BY id DESC LIMIT 1",
                           [case_number]).fetchone()
        proposed = []
        if batch:
            proposed = [dict(r) for r in db.execute(
                "SELECT * FROM ledger.comps WHERE case_number=? AND fetch_batch=? ORDER BY match_score DESC",
                [case_number, batch["fetch_batch"]]).fetchall()]

    pi = _parse_pi(case)
    ci = _case_input(case, pi)

    confirmed = [c for c in confirmations.values() if c["decision"] == "confirmed"]
    rejected_ids = {m for m, c in confirmations.items() if c["decision"] == "rejected"}

    # CONFIRMED ARV — from human-confirmed comps' FROZEN adjusted values only (§5.4).
    conf_arv = _median([c["adjusted_value"] for c in confirmed])
    if conf_arv is not None:
        arv, arv_label = conf_arv, acquisition.VERIFIED
    else:
        # provisional: median adjusted of the top proposed CLOSED comps (never trusted for an offer)
        prop_closed = [c for c in proposed if c["listing_status"] == "closed" and c.get("adjusted_value")]
        arv, arv_label = _median([c["adjusted_value"] for c in prop_closed[:5]]), acquisition.ESTIMATED

    if aa:
        acq = acquisition.AcquisitionInputs(
            arv=arv, arv_label=arv_label, repair_estimate=aa["repair_estimate"],
            agreed_price=aa["agreed_price"],
            lien_stack=json.loads(aa["lien_stack"]) if aa["lien_stack"] else [],
            lien_status=aa["lien_status"] or "unavailable", rule_pct_override=aa["rule_pct_override"])
    else:
        acq = acquisition.AcquisitionInputs(arv=arv, arv_label=arv_label)

    analysis = acquisition.analyze(ci, acq)
    for c in proposed:
        c["confirmation"] = confirmations.get(c["mls_id"], {}).get("decision")
    return {
        "case_number": case_number,
        "analysis": analysis,
        "valuation_state": analysis["valuation_state"],
        "decision": analysis["decision"],
        "confirmed_arv": conf_arv,
        "n_confirmed_comps": len(confirmed),
        "proposed_comps": proposed,
        "confirmed_comps": [json.loads(c["frozen_comp"]) for c in confirmed],
        "arv_sanity_band": acquisition.arv_sanity_band(arv, pi.get("market_value")),
    }


@app.post("/api/cases/{case_number}/comps/propose")
async def propose_comps(case_number: str, body: ProposeIn = ProposeIn(),
                        x_acquisition_token: str = Header(default="")):
    """Fetch + rank NTREIS comps for the subject, store them as the current proposal batch, and return
    them (with photos) for human confirmation. Closed drive ARV; pendings are directional-only."""
    _require_token("ACQUISITION_TOKEN", x_acquisition_token)
    with get_db() as db:
        row = db.execute("SELECT * FROM cases WHERE case_number=?", [case_number]).fetchone()
        if not row:
            raise HTTPException(404, f"Case {case_number} not found")
        case = dict(row)
    subject = comps.subject_from_case({**case, "property_intel": case.get("property_intel")})
    # Locality = postal code (precise) OR city (fallback for case addresses with no zip, e.g.
    # TX-23-00553). Fail closed only when neither a locality nor a living area is available.
    if not (subject.get("postal_code") or subject.get("city")) or not subject.get("gla"):
        raise HTTPException(422, "subject lacks a locality (postal code or city) or living area — cannot propose comps")

    if _COMP_SOURCE is not None:
        cand = _COMP_SOURCE(subject)
    else:
        if not comps.BridgeClient().configured():
            raise HTTPException(503, "comp engine not configured (NTREIS_BASE_URL / NTREIS_SERVER_TOKEN unset)")
        since = (date.today().replace(year=date.today().year - 1)).isoformat()
        cand = comps.fetch_candidates(subject, since=since, include_pending=body.include_pending)

    ranked = comps.provisional_arv(subject, cand.get("closed", []))
    batch = uuid.uuid4().hex
    rows_out = []
    with get_db() as db:
        for c in ranked.get("comps_ranked", []):
            q, adj = c["qualification"], c["adjustment"]
            db.execute(
                "INSERT INTO ledger.comps (case_number, mls_id, fetch_batch, listing_status, address, "
                "subdivision, same_subdivision, close_date, close_price, list_price, gla, beds, baths, "
                "year_built, distance_mi, match_score, adjusted_value, photos_count, media_urls, "
                "arms_length_flags, comp_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [case_number, c["mls_id"], batch, "closed", c.get("address"), c.get("subdivision"),
                 1 if q["same_subdivision"] else 0, c.get("close_date"), c.get("close_price"),
                 c.get("list_price"), c.get("gla"), c.get("beds"), c.get("baths"), c.get("year_built"),
                 q.get("distance_mi"), c["match_score"], adj["adjusted_value"], c.get("photos_count"),
                 json.dumps(c.get("media_urls", [])), json.dumps(q.get("arms_length_flags", [])),
                 json.dumps({k: v for k, v in c.items() if k != "qualification"})])
        for p in cand.get("pending", []):
            db.execute(
                "INSERT INTO ledger.comps (case_number, mls_id, fetch_batch, listing_status, address, "
                "subdivision, close_date, close_price, list_price, gla, beds, baths, year_built, "
                "photos_count, media_urls, comp_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [case_number, p.get("mls_id"), batch, "pending", p.get("address"), p.get("subdivision"),
                 p.get("close_date"), None, p.get("list_price_directional"), p.get("gla"), p.get("beds"),
                 p.get("baths"), p.get("year_built"), p.get("photos_count"),
                 json.dumps(p.get("media_urls", [])), json.dumps(p)])
        rows_out = [dict(r) for r in db.execute(
            "SELECT * FROM ledger.comps WHERE case_number=? AND fetch_batch=? ORDER BY match_score DESC",
            [case_number, batch]).fetchall()]
    return {"case_number": case_number, "fetch_batch": batch,
            "provisional_arv": ranked.get("provisional_arv"),
            "n_closed": len(cand.get("closed", [])), "n_pending": len(cand.get("pending", [])),
            "comps": rows_out}


@app.post("/api/cases/{case_number}/comps/{mls_id}/confirm")
async def confirm_comp(case_number: str, mls_id: str, body: CompDecision = CompDecision(),
                       x_acquisition_token: str = Header(default="")):
    """Confirm a proposed CLOSED comp — FREEZES its data + adjusted value at this moment (Q1)."""
    _require_token("ACQUISITION_TOKEN", x_acquisition_token)
    with get_db() as db:
        c = db.execute("SELECT * FROM ledger.comps WHERE case_number=? AND mls_id=? ORDER BY id DESC LIMIT 1",
                       [case_number, mls_id]).fetchone()
        if not c:
            raise HTTPException(404, f"comp {mls_id} not proposed for {case_number}")
        c = dict(c)
        if c["listing_status"] != "closed":
            raise HTTPException(400, "pending listings are directional-only and cannot be confirmed into the ARV")
        db.execute("INSERT INTO ledger.comp_confirmations (case_number, mls_id, decided_by, decision, "
                   "adjusted_value, frozen_comp, note) VALUES (?,?,?,?,?,?,?)",
                   [case_number, mls_id, body.decided_by, "confirmed", c["adjusted_value"],
                    json.dumps(c), body.note])
    return _build_acquisition(case_number)


@app.post("/api/cases/{case_number}/comps/{mls_id}/reject")
async def reject_comp(case_number: str, mls_id: str, body: CompDecision = CompDecision(),
                      x_acquisition_token: str = Header(default="")):
    _require_token("ACQUISITION_TOKEN", x_acquisition_token)
    with get_db() as db:
        c = db.execute("SELECT * FROM ledger.comps WHERE case_number=? AND mls_id=? ORDER BY id DESC LIMIT 1",
                       [case_number, mls_id]).fetchone()
        if not c:
            raise HTTPException(404, f"comp {mls_id} not proposed for {case_number}")
        db.execute("INSERT INTO ledger.comp_confirmations (case_number, mls_id, decided_by, decision, "
                   "adjusted_value, frozen_comp, note) VALUES (?,?,?,?,?,?,?)",
                   [case_number, mls_id, body.decided_by, "rejected", None, json.dumps(dict(c)), body.note])
    return _build_acquisition(case_number)


@app.post("/api/cases/{case_number}/acquisition")
async def upsert_acquisition(case_number: str, body: AcqInputs,
                             x_acquisition_token: str = Header(default="")):
    """Save human acquisition inputs (repairs, agreed price, lien stack) + recompute; append a version."""
    _require_token("ACQUISITION_TOKEN", x_acquisition_token)
    built = _build_acquisition(case_number)   # 404s if the case is missing
    with get_db() as db:
        db.execute(
            "INSERT INTO ledger.acquisition_analysis (case_number, updated_by, model_version, "
            "repair_estimate, agreed_price, lien_stack, lien_status, rule_pct_override, confirmed_arv, "
            "valuation_state, decision, analysis_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [case_number, body.updated_by, MODEL_VERSION, body.repair_estimate, body.agreed_price,
             json.dumps(body.lien_stack), body.lien_status, body.rule_pct_override,
             built["confirmed_arv"], built["valuation_state"], built["decision"], json.dumps(built["analysis"])])
    return _build_acquisition(case_number)


@app.get("/api/cases/{case_number}/acquisition")
async def get_acquisition(case_number: str, x_acquisition_token: str = Header(default="")):
    _require_token("ACQUISITION_TOKEN", x_acquisition_token)
    return _build_acquisition(case_number)

@app.get("/api/watchlist")
async def get_watchlist():
    with get_db() as db:
        rows = db.execute("SELECT * FROM watch_list ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]

@app.post("/api/watchlist")
async def add_to_watchlist(item: WatchListAdd):
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO watch_list (case_number,notes) VALUES (?,?)",
                   [item.case_number, item.notes])
    return {"status":"ok"}

@app.delete("/api/watchlist/{case_number}")
async def remove_from_watchlist(case_number: str):
    with get_db() as db:
        db.execute("DELETE FROM watch_list WHERE case_number=?", [case_number])
    return {"status":"ok"}

@app.get("/api/benchmarks")
async def get_benchmarks():
    with get_db() as db:
        rows = db.execute("SELECT * FROM benchmarks ORDER BY filed_date DESC").fetchall()
        result = []
        for r in rows:
            b = dict(r)
            if b.get("key_factors"):
                try: b["key_factors"] = json.loads(b["key_factors"])
                except: pass
            result.append(b)
        return result

@app.get("/api/agent/runs")
async def get_agent_runs(limit: int = 20):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?", [limit]
        ).fetchall()
        return [dict(r) for r in rows]

@app.post("/api/agent/run")
async def trigger_agent_run(background_tasks: BackgroundTasks):
    with get_db() as db:
        result = db.execute("INSERT INTO agent_runs (status) VALUES ('queued')")
        run_id = result.lastrowid
    return {"status":"queued","run_id":run_id,
            "message":"Agent run queued. Run `python agent/agent.py` to execute."}

# ─── SCRAPE TRIGGER QUEUE ─────────────────────────────────────
# The browser can't scrape (no cloud browser; scraping is local by design). It ENQUEUES a job
# here; the Mac's scrape_worker.py polls, claims, runs the real discover.py CLI, and reports back.
# Two fail-CLOSED tokens (same pattern as ledger export): SCRAPE_TRIGGER_TOKEN gates enqueue (who
# may spend API/CAPTCHA credits + hit the live portal); SCRAPE_WORKER_TOKEN gates claim/patch (who
# may drain the queue + write results). If a token env is unset the route is 503 — never open by
# accident. Enqueue itself spends NO credits; only the worker running a job does.
_SCRAPE_STATES = {"queued", "claimed", "running", "done", "failed"}
_SCRAPE_TERMINAL = {"done", "failed"}
MAX_QUEUED_SCRAPES = 20
_CASE_RE = re.compile(r"^TX-\d{2}-\d{5}$")


def _require_token(env_name: str, provided: str):
    want = os.environ.get(env_name, "")
    if not want:
        raise HTTPException(503, f"scrape queue not configured ({env_name} unset)")
    if not provided or not hmac.compare_digest(provided, want):
        raise HTTPException(401, "unauthorized")


def _job_out(row):
    """Row -> JSON-friendly dict with request/result parsed back to objects."""
    d = dict(row)
    for k in ("request", "result"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                pass
    return d


class ScrapeJobIn(BaseModel):
    case_number: str = ""
    pattern: str = ""
    individuals_only: bool = True
    # Discovery captures the full picture by DEFAULT (closed cases = the OOS / dismissed-owing
    # moat). include_closed=False is the deliberate narrow-mode opt-in (worker → discover
    # --open-only). Only meaningful for pattern searches; a --case scrape hits that case regardless.
    include_closed: bool = True


class ScrapeJobClaim(BaseModel):
    worker_id: str = "worker"


class ScrapeJobPatch(BaseModel):
    status: Optional[str] = None
    progress: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None


@app.post("/api/scrape-jobs")
async def create_scrape_job(job: ScrapeJobIn, x_scrape_token: str = Header(default="")):
    """Enqueue a scrape (token-gated). Exactly one of case_number / pattern. Deduped against any
    already-in-flight identical request; capped so a burst can't pile up credit-spending runs."""
    _require_token("SCRAPE_TRIGGER_TOKEN", x_scrape_token)
    cn = (job.case_number or "").strip().upper()
    pat = (job.pattern or "").strip().upper()
    if bool(cn) == bool(pat):
        raise HTTPException(400, "provide exactly one of case_number or pattern")
    if cn:
        if not _CASE_RE.match(cn):
            raise HTTPException(400, "case_number must look like TX-26-00009")
        request = {"case_number": cn}
        label = cn
    else:
        request = {"pattern": pat, "individuals_only": bool(job.individuals_only),
                   "include_closed": bool(job.include_closed)}
        label = pat
    request_json = json.dumps(request, sort_keys=True)
    with get_db() as db:
        dup = db.execute(
            "SELECT id FROM scrape_jobs WHERE request=? AND status IN ('queued','claimed','running')",
            [request_json]).fetchone()
        if dup:
            raise HTTPException(409, f"an identical job is already in flight (job {dup[0]})")
        queued = db.execute("SELECT COUNT(*) FROM scrape_jobs WHERE status='queued'").fetchone()[0]
        if queued >= MAX_QUEUED_SCRAPES:
            raise HTTPException(429, f"queue full ({queued} queued) — wait for the worker to drain it")
        cur = db.execute(
            "INSERT INTO scrape_jobs (request, label, status, requested_by) VALUES (?,?,'queued','operator')",
            [request_json, label])
        job_id = cur.lastrowid
    return {"job_id": job_id, "status": "queued", "label": label}


@app.get("/api/scrape-jobs")
async def list_scrape_jobs(limit: int = 20, x_scrape_token: str = Header(default="")):
    _require_token("SCRAPE_TRIGGER_TOKEN", x_scrape_token)
    with get_db() as db:
        rows = db.execute("SELECT * FROM scrape_jobs ORDER BY id DESC LIMIT ?", [limit]).fetchall()
    return [_job_out(r) for r in rows]


@app.get("/api/scrape-jobs/{job_id}")
async def get_scrape_job(job_id: int, x_scrape_token: str = Header(default="")):
    _require_token("SCRAPE_TRIGGER_TOKEN", x_scrape_token)
    with get_db() as db:
        row = db.execute("SELECT * FROM scrape_jobs WHERE id=?", [job_id]).fetchone()
    if not row:
        raise HTTPException(404, "job not found")
    return _job_out(row)


@app.post("/api/scrape-jobs/claim")
async def claim_scrape_job(claim: ScrapeJobClaim, x_worker_token: str = Header(default="")):
    """Worker-only. Atomically claim the oldest queued job (single UPDATE ... RETURNING, so two
    workers can never grab the same one). Returns {"job": null} when the queue is empty."""
    _require_token("SCRAPE_WORKER_TOKEN", x_worker_token)
    with get_db() as db:
        row = db.execute(
            "UPDATE scrape_jobs SET status='claimed', worker_id=?, claimed_at=datetime('now') "
            "WHERE id=(SELECT id FROM scrape_jobs WHERE status='queued' ORDER BY id LIMIT 1) "
            "RETURNING *", [claim.worker_id]).fetchone()
    return {"job": _job_out(row) if row else None}


@app.patch("/api/scrape-jobs/{job_id}")
async def patch_scrape_job(job_id: int, patch: ScrapeJobPatch, x_worker_token: str = Header(default="")):
    """Worker-only status/progress/result updates. A terminal status stamps finished_at."""
    _require_token("SCRAPE_WORKER_TOKEN", x_worker_token)
    sets, vals = [], []
    if patch.status is not None:
        if patch.status not in _SCRAPE_STATES:
            raise HTTPException(400, f"invalid status '{patch.status}'")
        sets.append("status=?"); vals.append(patch.status)
        if patch.status in _SCRAPE_TERMINAL:
            sets.append("finished_at=datetime('now')")
    if patch.progress is not None:
        sets.append("progress=?"); vals.append(patch.progress)
    if patch.result is not None:
        sets.append("result=?"); vals.append(json.dumps(patch.result))
    if patch.error is not None:
        sets.append("error=?"); vals.append(patch.error)
    if not sets:
        raise HTTPException(400, "nothing to update")
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM scrape_jobs WHERE id=?", [job_id]).fetchone()
        if not exists:
            raise HTTPException(404, "job not found")
        db.execute(f"UPDATE scrape_jobs SET {', '.join(sets)} WHERE id=?", vals + [job_id])
        row = db.execute("SELECT * FROM scrape_jobs WHERE id=?", [job_id]).fetchone()
    return _job_out(row)


# ─── HELD-FOR-REVIEW (close the loop: browser approves a held case → worker publishes it) ─────
# Held cases live on the MAC (prod_ready=0), never on prod. The worker mirrors a PREVIEW up here so
# the browser can list them; approving enqueues an approve-job the worker runs as the real
# sync_to_prod.py --approve. Same two fail-closed tokens as the scrape trigger.
WORKER_ONLINE_SECS = 30


class HeldPreview(BaseModel):
    case_number: str
    property_address: str = ""
    defendant: str = ""
    total_due: Optional[float] = None
    property_type: str = ""
    case_track: str = ""
    account_status: str = ""


class HeldSyncIn(BaseModel):
    held: List[HeldPreview] = []


class HeartbeatIn(BaseModel):
    worker_id: str = "worker"


def _worker_liveness(db):
    row = db.execute(
        "SELECT last_seen, (strftime('%s','now') - strftime('%s', last_seen)) AS age "
        "FROM worker_state ORDER BY last_seen DESC LIMIT 1").fetchone()
    if not row:
        return {"online": False, "last_seen": None, "age_secs": None}
    age = row["age"]
    return {"online": age is not None and age < WORKER_ONLINE_SECS,
            "last_seen": row["last_seen"], "age_secs": age}


@app.post("/api/worker/heartbeat")
async def worker_heartbeat(hb: HeartbeatIn, x_worker_token: str = Header(default="")):
    _require_token("SCRAPE_WORKER_TOKEN", x_worker_token)
    with get_db() as db:
        db.execute("INSERT INTO worker_state (worker_id, last_seen) VALUES (?, datetime('now')) "
                   "ON CONFLICT(worker_id) DO UPDATE SET last_seen=datetime('now')", [hb.worker_id])
    return {"ok": True}


@app.post("/api/held/sync")
async def held_sync(payload: HeldSyncIn, x_worker_token: str = Header(default="")):
    """Worker-only. FULL REPLACE of the held-review mirror with the Mac's current held set — so a
    just-approved case (no longer held locally) drops off, and new held cases appear."""
    _require_token("SCRAPE_WORKER_TOKEN", x_worker_token)
    with get_db() as db:
        db.execute("DELETE FROM held_cases")
        for h in payload.held:
            cn = (h.case_number or "").strip().upper()
            if not cn:
                continue
            db.execute(
                "INSERT OR REPLACE INTO held_cases (case_number, property_address, defendant, "
                "total_due, property_type, case_track, account_status, synced_at) "
                "VALUES (?,?,?,?,?,?,?, datetime('now'))",
                [cn, h.property_address, h.defendant, h.total_due, h.property_type,
                 h.case_track, h.account_status])
        n = db.execute("SELECT COUNT(*) FROM held_cases").fetchone()[0]
    return {"held": n}


@app.get("/api/held")
async def list_held(x_scrape_token: str = Header(default="")):
    """Browser view of cases awaiting approval + whether the Mac worker is online (approvals only
    process when it is)."""
    _require_token("SCRAPE_TRIGGER_TOKEN", x_scrape_token)
    with get_db() as db:
        rows = db.execute("SELECT * FROM held_cases ORDER BY case_number").fetchall()
        worker = _worker_liveness(db)
        inflight = set()
        for r in db.execute("SELECT request FROM scrape_jobs WHERE status IN "
                            "('queued','claimed','running') AND request LIKE '%approve%'"):
            try:
                a = json.loads(r["request"]).get("approve")
                if a:
                    inflight.add(a)
            except (ValueError, TypeError):
                pass
    held = []
    for r in rows:
        d = dict(r)
        d["approving"] = d["case_number"] in inflight
        held.append(d)
    return {"held": held, "worker": worker}


@app.post("/api/held/{case_number}/approve")
async def approve_held(case_number: str, x_scrape_token: str = Header(default="")):
    """Enqueue an approve-job — the worker runs the real sync_to_prod.py --approve locally. Only a
    case currently in the held mirror can be approved (never creates data; only publishes an existing
    held case). Deduped against an in-flight approve for the same case; capped like scrape enqueue."""
    _require_token("SCRAPE_TRIGGER_TOKEN", x_scrape_token)
    cn = (case_number or "").strip().upper()
    request_json = json.dumps({"approve": cn}, sort_keys=True)
    with get_db() as db:
        if not db.execute("SELECT 1 FROM held_cases WHERE case_number=?", [cn]).fetchone():
            raise HTTPException(404, f"{cn} is not in the held-review list")
        dup = db.execute("SELECT id FROM scrape_jobs WHERE request=? AND status IN "
                         "('queued','claimed','running')", [request_json]).fetchone()
        if dup:
            raise HTTPException(409, f"{cn} is already being approved (job {dup[0]})")
        queued = db.execute("SELECT COUNT(*) FROM scrape_jobs WHERE status='queued'").fetchone()[0]
        if queued >= MAX_QUEUED_SCRAPES:
            raise HTTPException(429, "queue full — wait for the worker to drain it")
        cur = db.execute("INSERT INTO scrape_jobs (request, label, status, requested_by) "
                         "VALUES (?,?,'queued','operator')", [request_json, "approve " + cn])
        job_id = cur.lastrowid
    return {"job_id": job_id, "status": "queued", "approve": cn}

@app.get("/api/petition/{case_number}")
async def get_petition_pdf(case_number: str):
    """Return petition URL for a case."""
    with get_db() as db:
        try:
            row = db.execute(
                "SELECT petition_href FROM cases WHERE case_number=?",
                [case_number]
            ).fetchone()
            if row and row[0]:
                return {"url": row[0], "case_number": case_number}
        except Exception:
            pass
    # Fallback to local file
    from fastapi.responses import FileResponse
    pdf_path = BASE_DIR / "data" / "pdfs" / case_number / "petition.pdf"
    if pdf_path.exists():
        return FileResponse(str(pdf_path), media_type="application/pdf",
                          headers={"Content-Disposition": "inline"})
    raise HTTPException(status_code=404, detail="Petition URL not found. Run discover.py to capture.")

@app.get("/api/agent/runs/{run_id}")
async def get_run_status(run_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM agent_runs WHERE id=?", [run_id]).fetchone()
        if not row:
            raise HTTPException(404, "Run not found")
        return dict(row)

@app.get("/api/stats")
async def get_stats():
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        pre_j = db.execute("SELECT COUNT(*) FROM cases WHERE stage='pre_judgment'").fetchone()[0]
        judged = db.execute("SELECT COUNT(*) FROM cases WHERE judgment_date IS NOT NULL AND oos_issued=0").fetchone()[0]
        # A pulled sale is its own (latest) stage — count it as sale_pulled, not oos_issued, so
        # the portfolio buckets stay mutually exclusive AND the "Sale Pulled" stat matches the
        # card badge. Count sale_pulled by the DATE field (not just the stage column), since a
        # re-scrape recomputes stage from orderOfSaleIssued and would otherwise revert it to
        # oos_issued (discover.py doesn't yet capture sale-pulled events — see known gaps).
        _pulled_where = "(stage='sale_pulled' OR (sale_pulled_date IS NOT NULL AND TRIM(sale_pulled_date)!=''))"
        oos = db.execute(f"SELECT COUNT(*) FROM cases WHERE oos_issued=1 AND NOT {_pulled_where}").fetchone()[0]
        pulled = db.execute(f"SELECT COUNT(*) FROM cases WHERE {_pulled_where}").fetchone()[0]
        last_run = db.execute("SELECT started_at FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "total_cases": total,
            "pre_judgment": pre_j,
            "judgment_entered": judged,
            "oos_issued": oos,
            "sale_pulled": pulled,
            "last_agent_run": last_run[0] if last_run else None
        }

@app.get("/api/pdf/{case_number}")
async def get_pdf(case_number: str):
    pdf_path = PDF_DIR / case_number / "petition.pdf"
    if not pdf_path.exists():
        raise HTTPException(404, "PDF not found")
    return FileResponse(pdf_path, media_type="application/pdf")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, app_dir="backend")
