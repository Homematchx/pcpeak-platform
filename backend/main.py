"""
PC Peak Tax Foreclosure Intelligence Platform
Backend API — FastAPI + SQLite

Run: uvicorn main:app --reload --port 8000
"""

import sqlite3
import json
import os
import re
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks
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
# prediction_ledger + rep_actions are generated and OWNED by prod (ledger via create_case,
# rep_actions via the rep UI). They cannot be regenerated from local scraping, so the
# local→prod restore/push path must NEVER delete, drop, or wholesale-overwrite them.
# Both are append-only (+ a one-time reconcile column-fill), so a DELETE or DROP against
# them is ALWAYS wrong — restore, bug, or otherwise. Enforced at the SQLite engine level by
# an authorizer that denies DELETE/DROP on these tables while allowing the legitimate INSERT
# (logging) and UPDATE (reconcile). Installed on EVERY connection, so the tables are
# protected the instant they exist — before they ever hold real data.
PROD_OWNED_TABLES = ("prediction_ledger", "rep_actions")

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
            "filing_to_judgment_months": fjm, "days_to_oos": dto,
        }

    proj_oos = None
    confidence = 0
    fj_months = None

    if judged:
        j_date = datetime.strptime(judged, "%Y-%m-%d")
        proj_oos = j_date + timedelta(days=oos_days)
        fj_months = round((j_date - datetime.strptime(filed, "%Y-%m-%d")).days / 30) if filed else None
        confidence = 55   # joos is bimodal (fast ~40-120d vs contested ~360-557d) — date is uncertain even post-judgment
    elif next_hearing:
        nh = datetime.strptime(next_hearing, "%Y-%m-%d")
        if nh > now:
            est_judgment = nh + timedelta(days=7)
            proj_oos = est_judgment + timedelta(days=oos_days)
            fj_months = round((est_judgment - datetime.strptime(filed, "%Y-%m-%d")).days / 30) if filed else None
            confidence = 45
    elif filed:
        mid = round((ranges[0]+ranges[1])/2)
        est_judgment = datetime.strptime(filed, "%Y-%m-%d") + timedelta(days=mid*30)
        proj_oos = est_judgment + timedelta(days=oos_days)
        fj_months = mid
        confidence = {"low":40,"medium":30,"high":22}[complexity]
    
    result = {
        "projected_oos": proj_oos.strftime("%Y-%m-%d") if proj_oos else None,
        "confidence_pct": confidence,
        "filing_to_judgment_months": fj_months,
        "days_to_oos": (proj_oos - now).days if proj_oos else None,
    }
    # STALENESS: the projected OOS date has passed and NO real OOS is on record — the
    # prediction failed. Don't keep flashing stale confidence; say so explicitly.
    if proj_oos and proj_oos < now:
        result["projection_stale"] = True
        result["projection_failed_reason"] = "no OOS as of %s (predicted %s)" % (
            now.strftime("%Y-%m-%d"), proj_oos.strftime("%Y-%m-%d"))
        result["confidence_pct"] = 0
    return result

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

class CaseRunRequest(BaseModel):
    case_number: str
    pattern: str = ""

@app.post("/api/agent/run-case")
async def run_single_case(req: CaseRunRequest, background_tasks: BackgroundTasks):
    """Run discover.py on a single case number or pattern in the background."""
    import subprocess, os, threading

    with get_db() as db:
        result = db.execute(
            "INSERT INTO agent_runs (status, started_at) VALUES ('running', datetime('now'))"
        )
        run_id = result.lastrowid

    def run_discover():
        try:
            env = os.environ.copy()
            if req.case_number:
                args = ["python3", "discover.py", "--case", req.case_number]
            else:
                args = ["python3", "discover.py", "--pattern", req.pattern, "--individuals-only"]
            
            proc = subprocess.run(
                args,
                capture_output=True, text=True,
                cwd=BASE_DIR, env=env, timeout=600
            )
            status = "completed" if proc.returncode == 0 else "failed"
            output = (proc.stdout or "") + (proc.stderr or "")
            
            with get_db() as db2:
                db2.execute(
                    "UPDATE agent_runs SET status=?, finished_at=datetime('now'), output=? WHERE id=?",
                    [status, output[:2000], run_id]
                )
        except Exception as e:
            with get_db() as db2:
                db2.execute(
                    "UPDATE agent_runs SET status='failed', finished_at=datetime('now'), output=? WHERE id=?",
                    [str(e), run_id]
                )

    thread = threading.Thread(target=run_discover, daemon=True)
    thread.start()

    return {"status": "running", "run_id": run_id}

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
        oos = db.execute("SELECT COUNT(*) FROM cases WHERE oos_issued=1").fetchone()[0]
        pulled = db.execute("SELECT COUNT(*) FROM cases WHERE stage='sale_pulled'").fetchone()[0]
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
