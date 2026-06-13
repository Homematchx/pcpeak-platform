# PC Peak Tax Foreclosure Intelligence Platform
## Full-Stack AI Agent System

This is a complete platform — not a local HTML file. An AI Agent that automatically 
scrapes the Dallas County portal, extracts case data with Claude, and updates a 
persistent database that powers a live dashboard.

---

## WHAT'S INCLUDED

```
pcpeak_platform/
├── backend/
│   └── main.py          ← FastAPI backend + SQLite database
├── agent/
│   └── agent.py         ← AI Agent (scrapes portal + calls Claude)
├── frontend/
│   └── index.html       ← Live dashboard (auto-updates from database)
├── data/
│   ├── db/              ← SQLite database (auto-created)
│   └── pdfs/            ← Downloaded petition PDFs (auto-created)
├── requirements.txt
├── .env.example
└── start.sh             ← One-command startup
```

---

## SETUP (one time, ~5 minutes)

### Step 1: Install Python 3.10+
Download from https://python.org

### Step 2: Install dependencies
```bash
cd pcpeak_platform
pip install -r requirements.txt
playwright install chromium
```

### Step 3: Configure API keys
```bash
cp .env.example .env
# Edit .env and add your keys:
nano .env
```

Required:
- `ANTHROPIC_API_KEY` → get at console.anthropic.com

Optional:
- `TWO_CAPTCHA_KEY` → get at 2captcha.com (~$3/1000 CAPTCHAs for full automation)

### Step 4: Start the platform
```bash
chmod +x start.sh
./start.sh
```

This starts:
- Backend API at http://localhost:8000
- Dashboard at http://localhost:8080

---

## RUNNING THE AGENT

### Manual run (all watched cases):
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 agent/agent.py
```

### Single case:
```bash
python3 agent/agent.py --case TX-26-00009
```

### Multiple specific cases:
```bash
python3 agent/agent.py --case TX-26-00009 TX-25-01777 TX-25-00492
```

### From a file:
```bash
python3 agent/agent.py --file my_cases.txt
```

### Discover new cases automatically:
```bash
python3 agent/agent.py --discover
```

### Scheduled (runs every 24 hours, fully automated):
```bash
python3 agent/agent.py --schedule
```

---

## WHAT THE AGENT DOES

1. Opens a browser (you can watch it work)
2. Navigates to Dallas County Courts Portal
3. Solves CAPTCHA (manually once, or automatically with 2Captcha key)
4. For each case:
   - Searches and navigates to the case detail page
   - Copies the full docket text
   - Detects NEW events since last run
   - Downloads the Original Petition PDF
   - Calls Claude API for structured data extraction
   - Generates an intelligence memo with acquisition analysis
   - Saves everything to the database
5. Dashboard auto-refreshes with new data

---

## ADDING CASES

**Via dashboard:** Type case number in the Watch List input and click Add.

**Via API:**
```bash
curl -X POST http://localhost:8000/api/watchlist \
  -H "Content-Type: application/json" \
  -d '{"case_number": "TX-26-00009"}'
```

**Via file:** Create cases.txt, run:
```bash
python3 agent/agent.py --file cases.txt
```

---

## DATABASE

SQLite database at `data/db/pcpeak.db`. View with any SQLite browser, or via API:

- GET /api/cases — all cases
- GET /api/cases/{case_number} — single case with events
- GET /api/stats — portfolio overview
- GET /api/benchmarks — calibration benchmarks
- GET /api/agent/runs — agent run history
- Full API docs: http://localhost:8000/docs

---

## DEPLOYING TO CLOUD (optional, for team access)

### Option A: Railway.app (~$10/month)
1. Push to GitHub
2. Connect at railway.app
3. Add environment variables in Railway dashboard
4. Platform accessible from any device, any browser

### Option B: Render.com
1. Similar process, free tier available for API
2. Dashboard deploys as static site (free)

### Option C: Your own server (Mac Mini, AWS, DigitalOcean)
1. Install dependencies
2. Run `./start.sh` 
3. Set up nginx to expose ports

---

## BENCHMARKS ALREADY LOADED

The system ships with 5 confirmed Dallas County cases as benchmarks:
- TX-23-00042 (Williams/Motley) — HIGH complexity, 37mo→J, 89 days→OOS ✓ CONFIRMED
- TX-25-00492 (Hedge) — LOW complexity, 14mo→J, OOS ~Aug 2026
- TX-25-01777 (Stewart) — LOW, trial 07/20/2026
- TX-23-00569 (Paula Williams) — HIGH, sale PULLED 05/12/2026
- TX-26-00009 (Rogers) — LOW, trial 09/30/2026

---

## TROUBLESHOOTING

**"Cannot connect to API"** → Run `python3 backend/main.py` first

**"playwright not found"** → Run `pip install playwright && playwright install chromium`

**Agent can't find case** → Check case number format: TX-26-00009 (not TX26-00009)

**CAPTCHA keeps appearing** → Add 2Captcha key to .env for automated solving

**PDF download fails** → Very new cases may not have publicly viewable PDFs yet. Docket text still extracts.
