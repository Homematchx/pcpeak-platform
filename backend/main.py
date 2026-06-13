"""
PC Peak Tax Foreclosure Intelligence Platform
Backend API — FastAPI + SQLite

Run: uvicorn main:app --reload --port 8000
"""

import sqlite3
import json
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ─── CONFIG ───────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DB_PATH  = BASE_DIR / "data" / "db" / "pcpeak.db"
PDF_DIR  = BASE_DIR / "data" / "pdfs"
PDF_DIR.mkdir(parents=True, exist_ok=True)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app_: FastAPI):
    init_db()
    yield

app = FastAPI(title="PC Peak Tax Foreclosure Intelligence", version="1.0.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── DATABASE ─────────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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

        CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(case_status);
        CREATE INDEX IF NOT EXISTS idx_cases_stage ON cases(stage);
        CREATE INDEX IF NOT EXISTS idx_events_case ON docket_events(case_number);
        """)
    
    # Seed known benchmarks
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

# ─── CITY BENCHMARKS ──────────────────────────────────────────
CITY_DATA = {
    "dallas":     {"ftj":{"low":[12,18],"medium":[18,30],"high":[30,48]}, "joos":{"low":60,"medium":75,"high":89}},
    "fort_worth": {"ftj":{"low":[10,15],"medium":[15,24],"high":[24,36]}, "joos":{"low":45,"medium":60,"high":75}},
    "houston":    {"ftj":{"low":[18,28],"medium":[28,42],"high":[42,60]}, "joos":{"low":75,"medium":90,"high":120}},
    "austin":     {"ftj":{"low":[10,16],"medium":[16,26],"high":[26,38]}, "joos":{"low":45,"medium":65,"high":80}},
}

def compute_projection(case: dict) -> dict:
    city = CITY_DATA.get(case.get("city","dallas"), CITY_DATA["dallas"])
    complexity = case.get("complexity","medium")
    ranges = city["ftj"][complexity]
    oos_days = city["joos"][complexity]
    
    filed = case.get("filed_date")
    judged = case.get("judgment_date")
    next_hearing = case.get("next_hearing_date")
    
    from datetime import datetime, timedelta
    now = datetime.now()
    
    proj_oos = None
    confidence = 0
    fj_months = None
    
    if judged:
        j_date = datetime.strptime(judged, "%Y-%m-%d")
        proj_oos = j_date + timedelta(days=oos_days)
        fj_months = round((j_date - datetime.strptime(filed, "%Y-%m-%d")).days / 30) if filed else None
        confidence = 85
    elif next_hearing:
        nh = datetime.strptime(next_hearing, "%Y-%m-%d")
        if nh > now:
            est_judgment = nh + timedelta(days=7)
            proj_oos = est_judgment + timedelta(days=oos_days)
            fj_months = round((est_judgment - datetime.strptime(filed, "%Y-%m-%d")).days / 30) if filed else None
            confidence = 75
    elif filed:
        mid = round((ranges[0]+ranges[1])/2)
        est_judgment = datetime.strptime(filed, "%Y-%m-%d") + timedelta(days=mid*30)
        proj_oos = est_judgment + timedelta(days=oos_days)
        fj_months = mid
        confidence = {"low":65,"medium":50,"high":35}[complexity]
    
    return {
        "projected_oos": proj_oos.strftime("%Y-%m-%d") if proj_oos else None,
        "confidence_pct": confidence,
        "filing_to_judgment_months": fj_months,
        "days_to_oos": (proj_oos - now).days if proj_oos else None,
    }

# ─── API ROUTES ───────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status":"ok","platform":"PC Peak Tax Foreclosure Intelligence","version":"1.0.0"}

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
    """Upsert a case (from agent or manual entry)."""
    cn = data.get("case_number")
    if not cn:
        raise HTTPException(400, "case_number required")
    
    # Compute projection
    proj = compute_projection(data)
    data.update(proj)
    
    # Serialize JSON fields
    for field in ["all_defendants","delinquency_years","prior_suits","tax_breakdown"]:
        if isinstance(data.get(field), list):
            data[field] = json.dumps(data[field])
    
    data["updated_at"] = datetime.now().isoformat()
    
    with get_db() as db:
        existing = db.execute("SELECT id FROM cases WHERE case_number=?", [cn]).fetchone()
        if existing:
            sets = ", ".join([f"{k}=?" for k in data if k != "case_number"])
            vals = [data[k] for k in data if k != "case_number"] + [cn]
            db.execute(f"UPDATE cases SET {sets} WHERE case_number=?", vals)
        else:
            data["created_at"] = datetime.now().isoformat()
            cols = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])
            db.execute(f"INSERT INTO cases ({cols}) VALUES ({placeholders})", list(data.values()))
    
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
    """Trigger the agent to run now."""
    with get_db() as db:
        result = db.execute(
            "INSERT INTO agent_runs (status) VALUES ('queued')"
        )
        run_id = result.lastrowid
    
    # In production this would trigger the agent process
    # For now return the run ID so the frontend can poll status
    return {"status":"queued","run_id":run_id,
            "message":"Agent run queued. Run `python agent/agent.py` to execute."}

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
