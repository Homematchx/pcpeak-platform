"""
PC Peak — Backtax CSV Importer v2
Two modes:
  1. --prospect-only: Instantly saves all accounts as prospects (no browser)
  2. --check NAME: Checks ONE specific owner on courts portal

Usage:
  # Import entire CSV as prospects instantly (no browser needed):
  python3 import_backtax.py --file "Backtax application test file.csv" --prospect-only

  # Then check specific high-priority owners one at a time:
  python3 import_backtax.py --file "Backtax application test file.csv" --check "BOGGESS, TRESSIE"
  python3 import_backtax.py --file "Backtax application test file.csv" --check "TAYLOR, JANETTA"
  python3 import_backtax.py --file "Backtax application test file.csv" --check "JONES, PEARLY"
"""

import asyncio, csv, json, os, re, sys, sqlite3, argparse, httpx
from pathlib import Path
from datetime import datetime, date

BASE_DIR = Path(__file__).parent.resolve()
DB_PATH  = BASE_DIR / "data" / "db" / "pcpeak.db"
PDF_DIR  = BASE_DIR / "data" / "pdfs"
PORTAL   = "https://courtsportal.dallascounty.org/DALLASPROD/Home/Dashboard/29"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY","")
TWO_CAPTCHA_KEY = "e6b154c8fad025b44a18d395ba6b1180"

# ── OWNER CLASSIFICATION ──────────────────────────────────────
BUSINESS_WORDS = [
    "LLC","INC","CORP","LTD","LP","LLP","PARTNERSHIP","TRUST",
    "PROPERTIES","HOLDINGS","INVESTMENTS","ENTERPRISES","GROUP",
    "REALTY","ASSOCIATES","MANAGEMENT","DEVELOPMENT","SERVICES",
    "COMPANY","DBA","PRES","ATTN","FUND","CITY","COMMUNITY",
    "CHURCH","MINISTRY","FOUNDATION","PROGRAM","FUNERAL","HOME"
]
ESTATE_WORDS = ["EST OF","ESTATE OF","ESTATE","EST","HEIR","LIFE ESTATE"]

def parse_owner(raw: str) -> dict:
    name = raw.strip().upper()
    if not name:
        return {"type":"unknown","priority":"low","courts_search":"",
                "display_name":raw,"contact":"Direct mail","note":"Research further"}

    is_estate   = any(w in name for w in ESTATE_WORDS)
    is_business = any(w in name for w in BUSINESS_WORDS) and not is_estate

    # Clean for parsing
    clean = name
    for w in ESTATE_WORDS + ["JR","SR","II","III","IV","&","ET AL","ETAL"]:
        clean = clean.replace(w,"").strip()
    clean = re.sub(r'\s+',' ',clean).strip()
    parts = clean.split()

    if len(parts) >= 2 and not is_business:
        courts_search = f"{parts[0]}, {parts[1]}"
    elif is_business and parts:
        courts_search = parts[0]
    else:
        courts_search = parts[0] if parts else name

    if is_estate:
        return {"type":"estate","priority":"high",
                "reason":"Estate/heir — title complications, highly motivated",
                "contact":"Contact estate administrator or heirs",
                "note":"Higher complexity, often desperate to resolve",
                "courts_search":courts_search,"display_name":raw.strip()}
    elif is_business:
        return {"type":"business","priority":"medium",
                "reason":"Business entity — investment property",
                "contact":"Formal letter to registered agent",
                "note":"Negotiate on ROI and numbers",
                "courts_search":courts_search,"display_name":raw.strip()}
    else:
        return {"type":"individual","priority":"high",
                "reason":"Individual homeowner — likely primary residence",
                "contact":"Door knock first, then direct mail, then phone",
                "note":"Lead with empathy — help them resolve situation",
                "courts_search":courts_search,"display_name":raw.strip()}

def parse_amount(val):
    try: return float(str(val).replace(',','').replace('$','').strip())
    except: return 0.0

def extract_account(url):
    try: return url.split('can=')[1].split('&')[0]
    except: return ""

def load_csv(filepath: str, min_balance: float = 0) -> list:
    rows = []
    seen_owners = set()
    with open(filepath,'r',encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            owner_raw = row.get('Owner','').strip()
            if not owner_raw: continue
            total = parse_amount(row.get('Total amount due','0'))
            if total < min_balance: continue

            owner = parse_owner(owner_raw)
            key = owner['courts_search']
            if key in seen_owners: continue
            seen_owners.add(key)

            addr_parts = [row.get(f'Address {i}','').strip() for i in range(2,5)]
            addr = next((a for a in addr_parts if a and not a.startswith('%')),'')
            city  = row.get('City','Dallas').strip()
            state = row.get('State','TX').strip()
            zip_  = row.get('Zip','').strip()

            rows.append({
                "owner_raw":    owner_raw,
                "owner":        owner,
                "total_due":    total,
                "current_due":  parse_amount(row.get('Current amount due','0')),
                "prior_due":    parse_amount(row.get('Prior amount due','0')),
                "account":      extract_account(row.get('Property page','')),
                "address":      f"{addr}, {city}, {state} {zip_}".strip(', '),
                "property_url": row.get('Property page',''),
                "appraisal_url":row.get('Appraisal page',''),
                "homestead":    bool(row.get('Homestead','').strip()),
                "over65":       bool(row.get('Over 65','').strip()),
                "veteran":      bool(row.get('Veteran','').strip()),
                "bankruptcy_no":row.get('Bankruptcy number','').strip(),
                "appraised_val":parse_amount(row.get('Appraised value','0')),
                "year_built":   row.get('Year built','').strip(),
            })
    return rows

def ensure_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_number TEXT UNIQUE,
            owner_name TEXT,
            owner_type TEXT,
            owner_priority TEXT,
            courts_search_name TEXT,
            property_address TEXT,
            total_due REAL,
            current_due REAL,
            prior_due REAL,
            appraised_value REAL,
            homestead INTEGER DEFAULT 0,
            over65 INTEGER DEFAULT 0,
            veteran INTEGER DEFAULT 0,
            bankruptcy_number TEXT,
            tax_suit_number TEXT,
            has_suit INTEGER DEFAULT 0,
            property_url TEXT,
            appraisal_url TEXT,
            contact_approach TEXT,
            acquisition_note TEXT,
            added_date TEXT,
            last_checked TEXT
        )""")
        db.commit()

def save_prospect(p: dict, suit: str = ""):
    ensure_db()
    owner = p['owner']
    now   = datetime.now().isoformat()
    with sqlite3.connect(str(DB_PATH)) as db:
        acct = p['account'] or f"NOACCT_{owner['courts_search'].replace(' ','_')}"
        db.execute("""INSERT INTO prospects
            (account_number,owner_name,owner_type,owner_priority,courts_search_name,
             property_address,total_due,current_due,prior_due,appraised_value,
             homestead,over65,veteran,bankruptcy_number,tax_suit_number,has_suit,
             property_url,appraisal_url,contact_approach,acquisition_note,
             added_date,last_checked)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_number) DO UPDATE SET
            total_due=excluded.total_due,tax_suit_number=excluded.tax_suit_number,
            has_suit=excluded.has_suit,last_checked=excluded.last_checked""",
            [acct, owner['display_name'], owner['type'], owner['priority'],
             owner['courts_search'], p['address'],
             p['total_due'], p['current_due'], p['prior_due'], p['appraised_val'],
             1 if p['homestead'] else 0, 1 if p['over65'] else 0,
             1 if p['veteran'] else 0, p['bankruptcy_no'],
             suit, 1 if suit else 0,
             p['property_url'], p['appraisal_url'],
             owner['contact'], owner['note'], now, now])
        db.commit()

# ── CLAUDE AI ─────────────────────────────────────────────────
EXTRACTION_PROMPT = """You are a Texas tax foreclosure analyst for PC Peak Development.
Extract ALL case data from this Dallas County docket. Return ONLY valid JSON:
{"caseNumber":"","court":"","judicialOfficer":"","filedDate":"YYYY-MM-DD",
"caseStatus":"open","judgmentDate":"","judgmentType":"none",
"defendant":"","allDefendants":[],"propertyAddress":"","accountNumber":"",
"lawFirm":"LGBS (Linebarger)","plaintiffAttorney":"","totalDueAtFiling":0,
"delinquencyYears":[],"oldestDelinquencyYear":0,
"taxBreakdown":[{"entity":"","taxAmt":0,"penaltyInterest":0,"total":0}],
"defCount":1,"citationByPostingRequested":false,"rule106SubstituteService":false,
"priorRelatedSuits":[],"estateHeirSituation":false,
"continuanceCount":0,"trialResetCount":0,"serviceIssues":"",
"nextHearingDate":"","orderOfSaleIssued":false,"orderOfSaleDate":"",
"complexity":"low","complexityReason":"",
"keyDocketEvents":[{"date":"YYYY-MM-DD","event":"","type":"filing"}]}"""

MEMO_PROMPT = """Acquisition memo for PC Peak Development.
Owner: {owner_type} — {owner_reason}
Contact: {contact}
Dallasact balance: ${total_due:,.2f} | Current: ${current_due:,.2f} | Homestead: {homestead} | Over 65: {over65}
{suit_info}

Write 3 paragraphs:
1. OWNER SITUATION: Who they are, debt vs appraised value, urgency
2. TIMELINE: {timeline}
3. STRATEGY: Exact contact method for this owner type, offer range, urgency
Be direct. Specific numbers."""

async def claude_extract(text: str) -> dict:
    if not ANTHROPIC_KEY: return {}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0,connect=30.0)) as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                    json={"model":"claude-sonnet-4-5","max_tokens":2000,"system":EXTRACTION_PROMPT,
                          "messages":[{"role":"user","content":f"Extract:\n\n{text[:5000]}"}]})
                d = r.json()
                if d.get("error"): await asyncio.sleep(5); continue
                raw = next((b["text"] for b in d.get("content",[]) if b["type"]=="text"),"")
                raw = re.sub(r"```json|```","",raw).strip()
                m = re.search(r'\{.*\}',raw,re.DOTALL)
                if m: return json.loads(m.group())
        except Exception as e:
            if attempt < 2: await asyncio.sleep(5)
    return {}

async def claude_memo(prospect: dict, suit: str = "") -> str:
    if not ANTHROPIC_KEY: return ""
    owner = prospect['owner']
    suit_info = f"TAX SUIT FILED: {suit}" if suit else "NO SUIT YET — early outreach opportunity"
    timeline = ("Suit filed — estimate judgment timeline using benchmarks: TX-23-00042 HIGH 37mo→J, TX-25-00492 LOW 14mo→J"
                if suit else "No suit. LGBS files typically 1-2 years after delinquency. BEST time to approach is NOW before legal pressure.")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0,connect=30.0)) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                json={"model":"claude-sonnet-4-5","max_tokens":600,
                      "messages":[{"role":"user","content":MEMO_PROMPT.format(
                          owner_type=owner['type'], owner_reason=owner['reason'],
                          contact=owner['contact'], total_due=prospect['total_due'],
                          current_due=prospect['current_due'],
                          homestead="YES — PRIMARY RESIDENCE" if prospect['homestead'] else "No",
                          over65="YES — SENIOR" if prospect['over65'] else "No",
                          suit_info=suit_info, timeline=timeline)}]})
            return next((b["text"] for b in r.json().get("content",[]) if b["type"]=="text"),"")
    except: return ""

def save_case_to_db(extracted: dict, memo: str, owner: dict):
    cn = extracted.get("caseNumber","")
    if not cn: return
    now = datetime.now().isoformat()
    data = {
        "case_number":        cn,
        "court":              extracted.get("court",""),
        "judicial_officer":   extracted.get("judicialOfficer",""),
        "filed_date":         extracted.get("filedDate") or None,
        "case_status":        extracted.get("caseStatus","OPEN").upper(),
        "defendant":          extracted.get("defendant",""),
        "all_defendants":     json.dumps(extracted.get("allDefendants",[])),
        "property_address":   extracted.get("propertyAddress",""),
        "account_number":     extracted.get("accountNumber",""),
        "law_firm":           extracted.get("lawFirm","LGBS (Linebarger)"),
        "plaintiff_attorney": extracted.get("plaintiffAttorney",""),
        "total_due_filing":   extracted.get("totalDueAtFiling") or None,
        "oldest_delinquency_year": extracted.get("oldestDelinquencyYear") or None,
        "delinquency_years":  json.dumps(extracted.get("delinquencyYears",[])),
        "def_count":          extracted.get("defCount",1),
        "cbp_requested":      1 if extracted.get("citationByPostingRequested") else 0,
        "rule106":            1 if extracted.get("rule106SubstituteService") else 0,
        "prior_suits":        json.dumps(extracted.get("priorRelatedSuits",[])),
        "estate_heir":        1 if extracted.get("estateHeirSituation") else 0,
        "continuance_count":  extracted.get("continuanceCount",0),
        "service_issues":     extracted.get("serviceIssues",""),
        "complexity":         extracted.get("complexity","low"),
        "judgment_date":      extracted.get("judgmentDate") or None,
        "judgment_type":      extracted.get("judgmentType","none"),
        "oos_issued":         1 if extracted.get("orderOfSaleIssued") else 0,
        "city":               "dallas",
        "tax_breakdown":      json.dumps(extracted.get("taxBreakdown",[])),
        "ai_memo":            memo,
        "stage":              "oos_issued" if extracted.get("orderOfSaleIssued") else
                              "judgment_entered" if extracted.get("judgmentDate") else "pre_judgment",
        "last_agent_run":     now,
        "updated_at":         now,
        "monitored":          1,
    }
    ensure_db()
    with sqlite3.connect(str(DB_PATH)) as db:
        for col in ["owner_type TEXT","owner_priority TEXT"]:
            try: db.execute(f"ALTER TABLE cases ADD COLUMN {col}")
            except: pass
        data["owner_type"]     = owner.get("type","unknown")
        data["owner_priority"] = owner.get("priority","medium")
        exists = db.execute("SELECT id FROM cases WHERE case_number=?",[cn]).fetchone()
        if exists:
            sets = ", ".join(f"{k}=?" for k in data if k != "case_number")
            db.execute(f"UPDATE cases SET {sets} WHERE case_number=?",
                      [data[k] for k in data if k != "case_number"]+[cn])
        else:
            data["created_at"] = now
            cols = ", ".join(data.keys())
            db.execute(f"INSERT INTO cases ({cols}) VALUES ({','.join(['?']*len(data))})",
                      list(data.values()))
        for ev in extracted.get("keyDocketEvents",[]):
            db.execute("INSERT OR IGNORE INTO docket_events (case_number,event_date,event_type,description,is_new) VALUES (?,?,?,?,1)",
                      [cn,ev.get("date"),ev.get("type","filing"),ev.get("event","")])
        db.commit()

# ── BROWSER (single name check) ───────────────────────────────
class PortalChecker:
    def __init__(self):
        self.page = None
        self.browser = None

    def log(self, msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    async def start(self):
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        self.browser = await pw.chromium.launch(headless=False,
            args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
        ctx = await self.browser.new_context(viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
        self.page = await ctx.new_page()

    async def check_one(self, name: str, prospect: dict) -> list:
        """Search portal for ONE name and process any suits found."""
        self.log(f"Navigating to portal...")
        await self.page.goto(PORTAL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)

        # Handle CAPTCHA
        has_cap = await self.page.evaluate(
            "()=>!!document.querySelector('iframe[src*=recaptcha],.g-recaptcha,[data-sitekey]')")
        if has_cap:
            solved = False
            if TWO_CAPTCHA_KEY:
                self.log("Solving CAPTCHA via 2Captcha...")
                try:
                    site_key = await self.page.evaluate("""
                        document.querySelector('[data-sitekey]')?.dataset?.sitekey ||
                        [...document.querySelectorAll('iframe')]
                          .find(f=>f.src.includes('recaptcha'))
                          ?.src?.match(/[?&]k=([^&]+)/)?.[1] || null
                    """)
                    if site_key:
                        self.log(f"  Site key found: {site_key[:20]}...")
                        async with httpx.AsyncClient(timeout=30) as client:
                            r = await client.post("http://2captcha.com/in.php", data={
                                "key": TWO_CAPTCHA_KEY, "method": "userrecaptcha",
                                "googlekey": site_key, "pageurl": self.page.url, "json": 1})
                            d = r.json()
                            if d.get("status") == 1:
                                cid = d["request"]
                                self.log(f"  Submitted (ID:{cid}), waiting for solution...")
                                for _ in range(24):
                                    await asyncio.sleep(5)
                                    r2 = await client.get(f"http://2captcha.com/res.php?key={TWO_CAPTCHA_KEY}&action=get&id={cid}&json=1")
                                    d2 = r2.json()
                                    if d2.get("status") == 1:
                                        token = d2["request"]
                                        await self.page.evaluate(f"""
                                            document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el=>{{
                                                el.value='{token}';el.innerHTML='{token}';
                                            }});
                                            try{{
                                                const c=window.___grecaptcha_cfg?.clients;
                                                if(c)Object.values(c).forEach(cl=>{{
                                                    const cb=Object.values(cl).find(v=>v&&typeof v.callback==='function');
                                                    if(cb)cb.callback('{token}');
                                                }});
                                            }}catch(e){{}}
                                        """)
                                        self.log("  ✓ CAPTCHA solved automatically!")
                                        solved = True
                                        await asyncio.sleep(1)
                                        break
                                    if d2.get("request") not in ("CAPCHA_NOT_READY",):
                                        self.log(f"  2Captcha error: {d2}")
                                        break
                            else:
                                self.log(f"  2Captcha submit failed: {d}")
                except Exception as e:
                    self.log(f"  2Captcha exception: {e}")
            if not solved:
                self.log("CAPTCHA — solve in browser then press ENTER")
                input("Press ENTER after solving...")
            await asyncio.sleep(1)

        # Fill search
        safe = name.replace("'","").replace("`","")
        await self.page.evaluate(f"""() => {{
            const inputs = [...document.querySelectorAll('input')];
            const inp = inputs.find(i=>(i.type==='text'||i.type==='')&&i.offsetParent!==null);
            if(inp){{
                inp.value='{safe}';
                inp.dispatchEvent(new Event('input',{{bubbles:true}}));
                inp.dispatchEvent(new Event('change',{{bubbles:true}}));
            }}
        }}""")
        await asyncio.sleep(0.5)
        await self.page.evaluate("""()=>{
            const b=[...document.querySelectorAll('input[type=submit],button[type=submit],button')]
                .find(x=>(x.value||x.textContent||'').toLowerCase().includes('submit'));
            if(b)b.click();else{const f=document.querySelector('form');if(f)f.submit();}
        }""")

        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except:
            await asyncio.sleep(4)

        # Check if CAPTCHA appeared again after submit
        has_cap2 = await self.page.evaluate(
            "()=>!!document.querySelector('iframe[src*=recaptcha],.g-recaptcha,[data-sitekey]')")
        if has_cap2:
            self.log("CAPTCHA appeared again — solving...")
            solved2 = False
            if TWO_CAPTCHA_KEY:
                try:
                    site_key2 = await self.page.evaluate("""
                        document.querySelector('[data-sitekey]')?.dataset?.sitekey ||
                        [...document.querySelectorAll('iframe')]
                          .find(f=>f.src.includes('recaptcha'))
                          ?.src?.match(/[?&]k=([^&]+)/)?.[1] || null
                    """)
                    if site_key2:
                        async with httpx.AsyncClient(timeout=30) as client:
                            r = await client.post("http://2captcha.com/in.php", data={
                                "key":TWO_CAPTCHA_KEY,"method":"userrecaptcha",
                                "googlekey":site_key2,"pageurl":self.page.url,"json":1})
                            d = r.json()
                            if d.get("status")==1:
                                cid = d["request"]
                                for _ in range(24):
                                    await asyncio.sleep(5)
                                    r2 = await client.get(f"http://2captcha.com/res.php?key={TWO_CAPTCHA_KEY}&action=get&id={cid}&json=1")
                                    d2 = r2.json()
                                    if d2.get("status")==1:
                                        token = d2["request"]
                                        await self.page.evaluate(f"""
                                            document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el=>{{
                                                el.value='{token}';el.innerHTML='{token}';
                                            }});
                                        """)
                                        solved2 = True
                                        await asyncio.sleep(1)
                                        break
                                    if d2.get("request") not in ("CAPCHA_NOT_READY",): break
                except Exception as e:
                    self.log(f"  2Captcha error: {e}")
            if not solved2:
                self.log("CAPTCHA — solve manually and press ENTER")
                input("Press ENTER after solving...")
            await asyncio.sleep(1)
            await self.page.evaluate("""()=>{
                const b=[...document.querySelectorAll('input[type=submit],button[type=submit],button')]
                    .find(x=>(x.value||x.textContent||'').toLowerCase().includes('submit'));
                if(b)b.click();
            }""")
            await asyncio.sleep(4)

        page_text = await self.page.inner_text("body")

        # Extract TX- case numbers only
        all_cases = re.findall(r'TX-\d{2}-\d{5}', page_text)
        if not all_cases:
            self.log(f"  No TX- suits found for '{name}'")
            return []

        # Dedup — most recent per year
        case_dict = {}
        for cn in all_cases:
            parts = cn.split('-')
            if len(parts) == 3:
                yr, seq = parts[1], parts[2]
                if yr not in case_dict or seq > case_dict[yr]['seq']:
                    case_dict[yr] = {'caseNumber': cn, 'seq': seq}

        deduped = sorted([v['caseNumber'] for v in case_dict.values()], reverse=True)
        self.log(f"  Found {len(deduped)} TX- suit(s): {deduped}")

        # Process each suit
        results = []
        for cn in deduped:
            self.log(f"  Processing {cn}...")
            # Click into case
            try:
                link = self.page.locator(f"a:has-text('{cn}')").first
                await link.wait_for(timeout=5000, state="visible")
                await link.click()
                await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
                await asyncio.sleep(2)

                docket_text = await self.page.inner_text("body")
                pdf_dir = PDF_DIR / cn
                pdf_dir.mkdir(parents=True, exist_ok=True)
                (pdf_dir/"docket.txt").write_text(docket_text, encoding="utf-8")

                self.log(f"  Running AI extraction...")
                extracted = await claude_extract(docket_text)
                if extracted:
                    extracted["caseNumber"] = cn
                    memo = await claude_memo(prospect, cn)
                    save_case_to_db(extracted, memo, prospect['owner'])
                    save_prospect(prospect, cn)
                    self.log(f"  ✓ {cn} saved to database")
                    results.append(cn)
                else:
                    self.log(f"  Extraction failed for {cn}")

                # Go back for next case
                if len(deduped) > 1:
                    await self.page.go_back()
                    await asyncio.sleep(2)

            except Exception as e:
                self.log(f"  Error on {cn}: {e}")

        return results

    async def close(self):
        if self.browser: await self.browser.close()

# ── MAIN ──────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(
        description="PC Peak Backtax CSV Importer v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
MODES:

  1. Import all as prospects instantly (no browser):
     python3 import_backtax.py --file "file.csv" --prospect-only

  2. Check ONE specific owner on courts portal:
     python3 import_backtax.py --file "file.csv" --check "BOGGESS, TRESSIE"
     python3 import_backtax.py --file "file.csv" --check "TAYLOR, JANETTA"
     python3 import_backtax.py --file "file.csv" --check "JONES, PEARLY"

  RECOMMENDED WORKFLOW:
  Step 1 - Import all as prospects:
    python3 import_backtax.py --file "file.csv" --prospect-only --min-balance 5000

  Step 2 - Check top targets one at a time:
    python3 import_backtax.py --file "file.csv" --check "TAYLOR, JANETTA"
    python3 import_backtax.py --file "file.csv" --check "BOGGESS, TRESSIE"
        """)
    parser.add_argument("--file",          required=True)
    parser.add_argument("--prospect-only", action="store_true",
                       help="Import all as prospects without browser")
    parser.add_argument("--check",         help="Check ONE owner name on courts portal")
    parser.add_argument("--check-list",    nargs="+", help="Check MULTIPLE names: --check-list 'BOGGESS, TRESSIE' 'TAYLOR, JANETTA' 'JONES, PEARLY'")
    parser.add_argument("--min-balance",   type=float, default=5000)
    parser.add_argument("--individuals-only", action="store_true")
    args = parser.parse_args()

    if not Path(args.file).exists():
        print(f"File not found: {args.file}"); return

    prospects = load_csv(args.file, args.min_balance)
    if args.individuals_only:
        prospects = [p for p in prospects if p['owner']['type'] == 'individual']

    print(f"Loaded {len(prospects)} unique owners")

    # ── MODE 1: Prospect-only (no browser) ──
    if args.prospect_only:
        print(f"\nSaving all as prospects to database...")
        ind = est = biz = 0
        for p in prospects:
            save_prospect(p)
            t = p['owner']['type']
            if t=='individual': ind+=1
            elif t=='estate': est+=1
            else: biz+=1

        print(f"\n{'='*50}")
        print(f"IMPORT COMPLETE — {len(prospects)} prospects saved")
        print(f"  🔴 Individuals: {ind} — door knock")
        print(f"  ⚠️  Estates:    {est} — contact administrator")
        print(f"  🟡 Businesses:  {biz} — formal letter")
        print(f"{'='*50}")
        print(f"\nTop 10 by balance:")
        top = sorted(prospects, key=lambda x: x['total_due'], reverse=True)[:10]
        for p in top:
            print(f"  ${p['total_due']:>10,.2f} | {p['owner']['type'].upper()[:3]} | {p['owner']['display_name']} | {p['address']}")
        print(f"\nNext step — check specific owners:")
        print(f"  python3 import_backtax.py --file \"{args.file}\" --check \"TAYLOR, JANETTA\"")
        return

    # ── MODE 2: Check multiple names ──
    if args.check_list:
        names = args.check_list
        print(f"\nChecking {len(names)} names in one session...")
        print("Solve CAPTCHA once — all names will be searched automatically\n")

        if not ANTHROPIC_KEY:
            print("ERROR: Set ANTHROPIC_API_KEY"); return

        checker = PortalChecker()
        try:
            await checker.start()
            results_summary = []

            for i, name in enumerate(names):
                print(f"\n[{i+1}/{len(names)}] Searching: {name}")
                # Find matching prospect
                search_name = name.upper().strip()
                match = None
                for p in prospects:
                    if (p['owner']['courts_search'].upper() == search_name or
                        p['owner']['display_name'].upper().startswith(search_name.split(',')[0].strip())):
                        match = p
                        break
                if not match:
                    match = {
                        "owner": parse_owner(name.replace(',',' ')),
                        "total_due":0,"current_due":0,"prior_due":0,
                        "account":"","address":"","property_url":"",
                        "appraisal_url":"","homestead":False,"over65":False,
                        "veteran":False,"bankruptcy_no":"","appraised_val":0
                    }
                    match['owner']['courts_search'] = name

                if match.get('total_due'):
                    print(f"  Balance: ${match['total_due']:,.2f} | {match['owner']['type'].upper()} | {match.get('address','')}")

                suits = await checker.check_one(name, match)
                if suits:
                    results_summary.append(f"  ✓ SUIT FOUND: {name} → {suits}")
                else:
                    results_summary.append(f"  → No suit: {name} (pre-suit prospect)")

            print(f"\n{'='*60}")
            print(f"BATCH CHECK COMPLETE")
            for r in results_summary:
                print(r)
            print(f"{'='*60}")
            print(f"Go to taxforeclosureanalyzer.com → Sync")
        finally:
            await checker.close()
        return

    # ── MODE 3: Check one name ──
    if args.check:
        # Find matching prospect from CSV
        search_name = args.check.upper().strip()
        match = None
        for p in prospects:
            if (p['owner']['courts_search'].upper() == search_name or
                p['owner']['display_name'].upper().startswith(search_name.split(',')[0].strip())):
                match = p
                break

        if not match:
            # Create a minimal prospect for the search
            parts = search_name.split(',')
            match = {
                "owner": parse_owner(search_name.replace(',',' ')),
                "total_due": 0, "current_due": 0, "prior_due": 0,
                "account": "", "address": "", "property_url": "",
                "appraisal_url": "", "homestead": False, "over65": False,
                "veteran": False, "bankruptcy_no": "", "appraised_val": 0
            }
            match['owner']['courts_search'] = args.check

        print(f"\nChecking: '{args.check}'")
        if match.get('total_due'):
            print(f"  Balance: ${match['total_due']:,.2f} | {match['owner']['type'].upper()} | {match.get('address','')}")

        if not ANTHROPIC_KEY:
            print("ERROR: Set ANTHROPIC_API_KEY"); return

        checker = PortalChecker()
        try:
            await checker.start()
            suits = await checker.check_one(args.check, match)
            if suits:
                print(f"\n✓ SUIT(S) FOUND: {suits}")
                print(f"Go to taxforeclosureanalyzer.com → Sync to see the case")
            else:
                print(f"\n→ No suit found — saved as pre-suit prospect")
                save_prospect(match)
        finally:
            await checker.close()
        return

    print("Please specify --prospect-only or --check 'NAME'")
    print("Run with --help for usage examples")

if __name__ == "__main__":
    asyncio.run(main())
