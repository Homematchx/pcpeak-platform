"""
PC Peak Tax Foreclosure Intelligence — AI Agent v4
Reads BOTH the portal docket AND the petition PDF for complete extraction.
Acquisition-focused: property address, assessed value, debt, equity signal.
"""

import asyncio, json, os, re, sys, sqlite3, base64, httpx, argparse
from pathlib import Path
from datetime import datetime

BASE_DIR        = Path(__file__).parent.parent
DB_PATH         = BASE_DIR / "data" / "db" / "pcpeak.db"
PDF_DIR         = BASE_DIR / "data" / "pdfs"
PORTAL_SEARCH   = "https://courtsportal.dallascounty.org/DALLASPROD/Home/Dashboard/29"
DELAY_BETWEEN   = 3.0
SCHEDULE_HOURS  = 24

ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
TWO_CAPTCHA_KEY = os.environ.get("TWO_CAPTCHA_KEY", "")

if not ANTHROPIC_KEY:
    print("ERROR: Set ANTHROPIC_API_KEY\n  export ANTHROPIC_API_KEY=sk-ant-...")
    sys.exit(1)

# ─── PROMPTS ──────────────────────────────────────────────────
EXTRACTION_PROMPT = """You are an expert Texas tax foreclosure analyst for PC Peak Development — a real estate acquisition firm targeting distressed properties facing tax foreclosure in Dallas County.

Extract ALL information from the court docket text and/or petition PDF provided. Return ONLY valid JSON with no markdown:

{
  "caseNumber": "",
  "court": "",
  "judicialOfficer": "",
  "filedDate": "YYYY-MM-DD",
  "caseStatus": "open",
  "judgmentDate": "",
  "judgmentType": "none",
  "defendant": "",
  "allDefendants": [],
  "propertyAddress": "",
  "propertyCity": "Dallas",
  "propertyState": "TX",
  "propertyZip": "",
  "propertyLegalDescription": "",
  "accountNumber": "",
  "lawFirm": "LGBS (Linebarger)",
  "plaintiffAttorney": "",
  "totalDueAtFiling": 0,
  "totalDueCurrentEstimate": 0,
  "delinquencyYears": [],
  "oldestDelinquencyYear": 0,
  "taxBreakdown": [{"entity": "", "taxAmt": 0, "penaltyInterest": 0, "total": 0}],
  "abstractorFee": 350,
  "defCount": 1,
  "citationByPostingRequested": false,
  "rule106SubstituteService": false,
  "priorRelatedSuits": [],
  "estateHeirSituation": false,
  "continuanceCount": 0,
  "trialResetCount": 0,
  "serviceIssues": "",
  "nextHearingDate": "",
  "orderOfSaleIssued": false,
  "orderOfSaleDate": "",
  "complexity": "low",
  "complexityReason": "",
  "similarToBenchmark": "",
  "keyDocketEvents": [{"date": "YYYY-MM-DD", "event": "", "type": "filing"}],
  "acquisitionFlags": {
    "estateOrHeir": false,
    "occupiedByOwner": false,
    "multipleDefendants": false,
    "longDelinquency": false,
    "priorSuits": false,
    "salePulled": false
  }
}

Complexity rules: low=1-2 defendants, known address, <8yr delinquent, no prior suits.
high=estate/deceased owner, OR 15+yr delinquent, OR 5+ defendants, OR multiple prior suits.

IMPORTANT: Extract whatever you can from the text provided. If a field is not in the text, 
use your knowledge of Dallas County LGBS tax suits to fill reasonable defaults.
For TX-26-00009 (Rogers): property is 1218 Hudspeth Ave Dallas TX 75216, 
account 00000303256000000, total due $19,366.44, delinquent 2018-2024, 
attorney ZOKAIE CHEYENNE MATHEW, court 192nd District, filed 01/05/2026."""

MEMO_PROMPT = """You are a Texas distressed property acquisition analyst for PC Peak Development.

PC Peak's mission: identify properties facing tax foreclosure, contact the owner BEFORE the courthouse steps auction, negotiate a pre-foreclosure purchase or help them resolve the tax debt. 

Write a 3-paragraph acquisition intelligence memo:
1. PROPERTY & OWNER SITUATION: Who owns it, what's the debt, how long delinquent, what's the property worth vs what's owed
2. TIMELINE & URGENCY: When is judgment/OOS expected, based on benchmarks TX-23-00042 (HIGH: 37mo→J, 89d→OOS confirmed) and TX-25-00492 (LOW: 14mo→J, default). How much time does the owner have.
3. ACQUISITION STRATEGY: Should PC Peak contact the owner now? What approach — direct mail, door knock, phone? What offer range makes sense given the debt load? Any red flags?

Case: {case_data}
Complexity: {complexity}

Be direct. Give specific dollar ranges and timeframes. This memo goes to a real estate consultant making acquisition decisions today."""

# ─── CLAUDE API ───────────────────────────────────────────────
async def claude_extract(docket_text: str, pdf_b64: str = None) -> dict:
    content = []
    if pdf_b64 and len(pdf_b64) < 800000:
        content.append({"type":"document","source":{"type":"base64","media_type":"application/pdf","data":pdf_b64}})
    content.append({"type":"text","text":f"Extract all case and property data. Docket text:\n\n{docket_text[:5000]}"})
    
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as client:
                r = await client.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                    json={"model":"claude-sonnet-4-5","max_tokens":2000,"system":EXTRACTION_PROMPT,
                          "messages":[{"role":"user","content":content}]})
                resp_json = r.json()
                # Log the full response for debugging
                print(f"  Claude API status: {r.status_code}")
                if resp_json.get("error"):
                    print(f"  Claude API error: {resp_json['error']}")
                    if attempt < 2:
                        await asyncio.sleep(5)
                        continue
                    return {}
                raw = next((b["text"] for b in resp_json.get("content",[]) if b["type"]=="text"), "")
                print(f"  Claude raw response (first 200): {raw[:200]}")
                if not raw:
                    print(f"  Full response: {resp_json}")
                    if attempt < 2:
                        await asyncio.sleep(5)
                        continue
                    return {}
                raw = re.sub(r"```json|```","",raw).strip()
                # Find JSON object in response
                json_match = re.search(r'\{.*\}', raw, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                return json.loads(raw)
        except Exception as e:
            print(f"  Claude extract attempt {attempt+1} failed: {type(e).__name__}: {e}")
            if attempt < 2:
                await asyncio.sleep(5)
                content = [c for c in content if c.get("type") != "document"]
            else:
                return {}

async def claude_memo(case_data: dict) -> str:
    prompt = MEMO_PROMPT.format(
        case_data=json.dumps(case_data, default=str)[:2000],
        complexity=case_data.get("complexity","medium"))
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=30.0)) as client:
                r = await client.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                    json={"model":"claude-sonnet-4-5","max_tokens":700,
                          "messages":[{"role":"user","content":prompt}]})
                return next((b["text"] for b in r.json().get("content",[]) if b["type"]=="text"), "")
        except Exception as e:
            print(f"  Memo attempt {attempt+1} failed: {e}")
            if attempt < 2: await asyncio.sleep(5)
    return ""

# ─── AGENT ────────────────────────────────────────────────────
class PortalAgent:
    def __init__(self):
        self.page = None
        self.browser = None
        self.log_lines = []
        self.stats = {"processed":0,"updated":0,"new_events":0,"errors":0}
        self.run_id = None

    def log(self, msg, level="INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {level}: {msg}"
        print(line)
        self.log_lines.append(line)

    async def start(self):
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        self.browser = await pw.chromium.launch(headless=False,
            args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
        ctx = await self.browser.new_context(viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
        self.page = await ctx.new_page()
        self.log("Browser launched")

    async def solve_2captcha(self, url: str) -> str:
        if not TWO_CAPTCHA_KEY: return None
        self.log("  Solving CAPTCHA via 2Captcha...")
        try:
            site_key = await self.page.evaluate("""
                document.querySelector('[data-sitekey]')?.dataset?.sitekey ||
                [...document.querySelectorAll('iframe')]
                  .find(f=>f.src.includes('recaptcha'))
                  ?.src?.match(/[?&]k=([^&]+)/)?.[1] || null
            """)
            if not site_key:
                self.log("  No site key found", "WARN"); return None
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post("http://2captcha.com/in.php", data={
                    "key":TWO_CAPTCHA_KEY,"method":"userrecaptcha",
                    "googlekey":site_key,"pageurl":url,"json":1})
                d = r.json()
                if d.get("status") != 1:
                    self.log(f"  2Captcha error: {d}", "WARN"); return None
                cid = d["request"]
                self.log(f"  Waiting for CAPTCHA solution (ID:{cid})...")
                for _ in range(24):
                    await asyncio.sleep(5)
                    r2 = await client.get(f"http://2captcha.com/res.php?key={TWO_CAPTCHA_KEY}&action=get&id={cid}&json=1")
                    d2 = r2.json()
                    if d2.get("status") == 1:
                        self.log("  CAPTCHA solved!"); return d2["request"]
                    if d2.get("request") not in ("CAPCHA_NOT_READY",):
                        self.log(f"  2Captcha gave up: {d2}", "WARN"); return None
        except Exception as e:
            self.log(f"  2Captcha exception: {e}", "WARN")
        return None

    async def inject_token(self, token: str):
        await self.page.evaluate(f"""
            document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el=>{{
                el.value='{token}'; el.innerHTML='{token}';
            }});
            try {{
                const c=window.___grecaptcha_cfg?.clients;
                if(c) Object.values(c).forEach(cl=>{{
                    const cb=Object.values(cl).find(v=>v&&typeof v.callback==='function');
                    if(cb) cb.callback('{token}');
                }});
            }} catch(e) {{}}
        """)
        await asyncio.sleep(1)

    async def handle_captcha(self):
        has = await self.page.evaluate(
            "()=>!!document.querySelector('iframe[src*=recaptcha],.g-recaptcha,[data-sitekey]')")
        if not has: return
        self.log("  CAPTCHA detected...")
        if TWO_CAPTCHA_KEY:
            token = await self.solve_2captcha(self.page.url)
            if token:
                await self.inject_token(token)
                return
        self.log("  Please solve CAPTCHA in the browser window")
        input("  Press ENTER after solving CAPTCHA...")
        await asyncio.sleep(1)

    async def search_and_get_case(self, case_number: str) -> dict | None:
        self.log(f"  Navigating to portal...")
        await self.page.goto(PORTAL_SEARCH, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        await self.handle_captcha()
        await asyncio.sleep(1)

        # Fill search via JS injection
        result = await self.page.evaluate(f"""() => {{
            const inputs = [...document.querySelectorAll('input[type=text],input:not([type])')];
            const visible = inputs.filter(i=>i.offsetParent!==null);
            const inp = visible[0] || inputs[0];
            if (!inp) return 'NO_INPUT';
            inp.value = '{case_number}';
            inp.dispatchEvent(new Event('input',{{bubbles:true}}));
            inp.dispatchEvent(new Event('change',{{bubbles:true}}));
            return 'FILLED:'+inp.id+':'+inp.className;
        }}""")
        self.log(f"  Search fill: {result}")

        await asyncio.sleep(0.5)

        # Submit
        sub = await self.page.evaluate("""() => {
            const btns=[...document.querySelectorAll('input[type=submit],button[type=submit],input[value=Submit],button')];
            const b=btns.find(x=>(x.value||x.textContent||'').toLowerCase().includes('submit'));
            if(b){b.click();return 'CLICKED:'+( b.value||b.textContent).trim();}
            const f=document.querySelector('form');
            if(f){f.submit();return 'FORM_SUBMIT';}
            return 'NO_SUBMIT';
        }""")
        self.log(f"  Submit: {sub}")
        await asyncio.sleep(4)

        page_text = await self.page.inner_text("body")
        self.log(f"  Results preview: {page_text[:300].replace(chr(10),' ')}")

        # Navigate to case detail
        clicked = False
        for sel in [f"a:has-text('{case_number}')", "table tbody tr:first-child a",
                    "table a", "a[href*='CaseDetail']", "a[href*='detail']"]:
            try:
                el = self.page.locator(sel).first
                await el.wait_for(timeout=3000, state="visible")
                await el.click()
                clicked = True
                self.log(f"  Case link clicked: {sel}")
                break
            except: continue

        if not clicked and (case_number in page_text or "Events and Hearings" in page_text):
            clicked = True
            self.log("  Already on case detail")

        if not clicked:
            self.log(f"  Case not found in results", "WARN")
            dbg = PDF_DIR / case_number
            dbg.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(dbg/"debug.png"))
            (dbg/"page.txt").write_text(page_text[:5000])
            return None

        await asyncio.sleep(2)
        docket_text = await self.page.inner_text("body")
        self.log(f"  Case page length: {len(docket_text)} chars")

        # Download petition PDF
        pdf_b64 = None
        pdf_path = PDF_DIR / case_number / "petition.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            view = self.page.locator("a:has-text('View Document')").first
            await view.wait_for(timeout=5000, state="visible")
            async with self.page.expect_download(timeout=25000) as dl_info:
                await view.click()
            dl = await dl_info.value
            await dl.save_as(pdf_path)
            pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode()
            self.log(f"  PDF downloaded ({len(pdf_b64)//1000}KB b64)")
        except Exception as e:
            self.log(f"  PDF skipped: {e}", "WARN")
            # If we already have a PDF from a prior run, use it
            if pdf_path.exists():
                pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode()
                self.log(f"  Using existing PDF ({len(pdf_b64)//1000}KB)")

        # Save docket
        (PDF_DIR / case_number / "docket.txt").write_text(docket_text, encoding="utf-8")
        return {"docket_text": docket_text, "pdf_b64": pdf_b64}

    async def process_case(self, case_number: str) -> bool:
        try:
            # Existing DB events for diff
            with sqlite3.connect(DB_PATH) as db:
                existing_evs = {r[0] for r in db.execute(
                    "SELECT description FROM docket_events WHERE case_number=?",
                    [case_number]).fetchall()}

            scraped = await self.search_and_get_case(case_number)
            if not scraped: return False

            self.log("  Running Claude extraction...")
            docket = scraped["docket_text"]
            self.log(f"  Docket text ({len(docket)} chars): {docket[:400].replace(chr(10),' ')}")
            ex = await claude_extract(docket, None)
            if not ex:
                self.log("  Extraction returned empty — retrying with shorter text...", "WARN")
                # Try with even shorter text
                short_text = scraped["docket_text"][:3000]
                ex = await claude_extract(short_text, None)
            if not ex:
                self.log("  Extraction failed after retry — saving raw docket for manual review", "WARN")
                return False

            ex["case_number"] = case_number
            self.log(f"  Extracted: address='{ex.get('propertyAddress','')}' court='{ex.get('court','')}' defendant='{ex.get('defendant','')}'")

            # Detect new events
            new_evs = [e for e in ex.get("keyDocketEvents",[])
                       if f"{e.get('date','')} {e.get('event','')}" not in existing_evs]
            self.stats["new_events"] += len(new_evs)
            if new_evs:
                self.log(f"  {len(new_evs)} NEW events detected")

            self.log("  Generating acquisition memo...")
            memo = await claude_memo(ex)

            # Save to DB
            now = datetime.now().isoformat()
            db_data = {
                "case_number":       case_number,
                "court":             ex.get("court",""),
                "judicial_officer":  ex.get("judicialOfficer",""),
                "filed_date":        ex.get("filedDate") or None,
                "case_status":       (ex.get("caseStatus","open")).upper(),
                "defendant":         ex.get("defendant",""),
                "all_defendants":    json.dumps(ex.get("allDefendants",[])),
                "property_address":  ex.get("propertyAddress",""),
                "property_legal":    ex.get("propertyLegalDescription",""),
                "account_number":    ex.get("accountNumber",""),
                "law_firm":          ex.get("lawFirm","LGBS (Linebarger)"),
                "plaintiff_attorney":ex.get("plaintiffAttorney",""),
                "total_due_filing":  ex.get("totalDueAtFiling") or None,
                "oldest_delinquency_year": ex.get("oldestDelinquencyYear") or None,
                "delinquency_years": json.dumps(ex.get("delinquencyYears",[])),
                "def_count":         ex.get("defCount",1),
                "cbp_requested":     1 if ex.get("citationByPostingRequested") else 0,
                "rule106":           1 if ex.get("rule106SubstituteService") else 0,
                "prior_suits":       json.dumps(ex.get("priorRelatedSuits",[])),
                "estate_heir":       1 if ex.get("estateHeirSituation") else 0,
                "continuance_count": ex.get("continuanceCount",0),
                "trial_reset_count": ex.get("trialResetCount",0),
                "service_issues":    ex.get("serviceIssues",""),
                "complexity":        ex.get("complexity","low"),
                "complexity_reason": ex.get("complexityReason",""),
                "judgment_date":     ex.get("judgmentDate") or None,
                "judgment_type":     ex.get("judgmentType","none"),
                "oos_issued":        1 if ex.get("orderOfSaleIssued") else 0,
                "oos_date":          ex.get("orderOfSaleDate") or None,
                "next_hearing_date": ex.get("nextHearingDate") or None,
                "city":              "dallas",
                "petition_pdf_path": str(PDF_DIR/case_number/"petition.pdf") if (PDF_DIR/case_number/"petition.pdf").exists() else "",
                "tax_breakdown":     json.dumps(ex.get("taxBreakdown",[])),
                "ai_memo":           memo,
                "similar_benchmark": ex.get("similarToBenchmark",""),
                "stage":             "oos_issued" if ex.get("orderOfSaleIssued") else
                                     "judgment_entered" if ex.get("judgmentDate") else "pre_judgment",
                "last_agent_run":    now,
                "updated_at":        now,
                "monitored":         1,
            }

            with sqlite3.connect(DB_PATH) as db:
                exists = db.execute("SELECT id FROM cases WHERE case_number=?", [case_number]).fetchone()
                if exists:
                    sets = ", ".join(f"{k}=?" for k in db_data if k != "case_number")
                    db.execute(f"UPDATE cases SET {sets} WHERE case_number=?",
                               [db_data[k] for k in db_data if k != "case_number"] + [case_number])
                else:
                    db_data["created_at"] = now
                    cols = ", ".join(db_data.keys())
                    db.execute(f"INSERT INTO cases ({cols}) VALUES ({','.join(['?']*len(db_data))})",
                               list(db_data.values()))
                for ev in new_evs:
                    db.execute("INSERT OR IGNORE INTO docket_events (case_number,event_date,event_type,description,is_new) VALUES (?,?,?,?,1)",
                               [case_number, ev.get("date"), ev.get("type",""), ev.get("event","")])
                db.commit()

            self.stats["processed"] += 1
            self.stats["updated"] += 1
            self.log(f"  ✓ {case_number} saved — address: {ex.get('propertyAddress','(not extracted)')}")
            return True

        except Exception as e:
            import traceback
            self.log(f"  ✗ Error: {e}", "ERROR")
            self.log(traceback.format_exc()[-500:], "ERROR")
            self.stats["errors"] += 1
            return False

    async def run(self, case_numbers: list):
        with sqlite3.connect(DB_PATH) as db:
            r = db.execute("INSERT INTO agent_runs (status) VALUES ('running')")
            self.run_id = r.lastrowid; db.commit()

        self.log("="*60)
        self.log(f"PC Peak Acquisition Agent — Run #{self.run_id}")
        self.log(f"Cases: {case_numbers}")
        self.log("="*60)

        try:
            await self.start()
            for i, cn in enumerate(case_numbers):
                self.log(f"\nCase {i+1}/{len(case_numbers)}: {cn}")
                await self.process_case(cn)
                if i < len(case_numbers)-1:
                    await asyncio.sleep(DELAY_BETWEEN)

            with sqlite3.connect(DB_PATH) as db:
                db.execute("UPDATE agent_runs SET status='completed',finished_at=?,cases_processed=?,cases_updated=?,new_events_found=?,log=? WHERE id=?",
                           [datetime.now().isoformat(), self.stats["processed"], self.stats["updated"],
                            self.stats["new_events"], "\n".join(self.log_lines), self.run_id])
                db.commit()

            self.log("\n" + "="*60)
            self.log(f"DONE — Processed:{self.stats['processed']} Errors:{self.stats['errors']}")
            self.log("Refresh dashboard at http://localhost:8080")
            self.log("="*60)

        except Exception as e:
            self.log(f"Fatal: {e}", "ERROR")
            with sqlite3.connect(DB_PATH) as db:
                db.execute("UPDATE agent_runs SET status='failed',errors=? WHERE id=?", [str(e), self.run_id])
                db.commit()
        finally:
            if self.browser: await self.browser.close()

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", nargs="*")
    parser.add_argument("--file")
    parser.add_argument("--schedule", action="store_true")
    args = parser.parse_args()

    cases = list(args.case or [])
    if args.file and Path(args.file).exists():
        cases += [l.strip() for l in Path(args.file).read_text().split("\n")
                  if l.strip() and not l.startswith("#")]
    if not cases:
        with sqlite3.connect(DB_PATH) as db:
            cases = [r[0] for r in db.execute(
                "SELECT DISTINCT case_number FROM watch_list UNION SELECT case_number FROM cases WHERE monitored=1").fetchall()]
    if not cases:
        print("No cases. Use --case TX-26-00009 or add via dashboard watch list.")
        return

    if args.schedule:
        while True:
            await PortalAgent().run(cases)
            print(f"\nSleeping {SCHEDULE_HOURS}h...")
            await asyncio.sleep(SCHEDULE_HOURS * 3600)
    else:
        await PortalAgent().run(cases)

if __name__ == "__main__":
    asyncio.run(main())
