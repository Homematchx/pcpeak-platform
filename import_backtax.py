"""
PC Peak — Backtax CSV Importer
Reads the backtax application CSV from dallasact.com,
classifies every owner, searches courts portal for tax suits,
and populates the platform database with full AI analysis.

Usage:
  python3 import_backtax.py --file Backtax_application_test_file.csv
  python3 import_backtax.py --file myfile.csv --min-balance 5000
  python3 import_backtax.py --file myfile.csv --individuals-only
"""

import asyncio, csv, json, os, re, sys, sqlite3, argparse, httpx
from pathlib import Path
from datetime import datetime, date

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "data" / "db" / "pcpeak.db"
PDF_DIR  = BASE_DIR / "data" / "pdfs"
PORTAL   = "https://courtsportal.dallascounty.org/DALLASPROD/Home/Dashboard/29"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY","")

# ── BUSINESS / OWNER DETECTION ────────────────────────────────
BUSINESS_WORDS = [
    "LLC","INC","CORP","LTD","LP","LLP","PARTNERSHIP","TRUST",
    "PROPERTIES","HOLDINGS","INVESTMENTS","ENTERPRISES","GROUP",
    "REALTY","ASSOCIATES","MANAGEMENT","DEVELOPMENT","SERVICES",
    "COMPANY","DBA","PRES","ATTN","FUND","CITY","COMMUNITY",
    "CHURCH","MINISTRY","FOUNDATION"
]
ESTATE_WORDS = ["EST OF","ESTATE OF","ESTATE","EST","HEIR"]
SUFFIX_WORDS = ["JR","SR","II","III","IV","&"]

def parse_owner(raw_name: str) -> dict:
    """Parse dallasact owner name and classify."""
    name = raw_name.strip().upper()
    if not name:
        return {"type":"unknown","priority":"low","courts_search":"",
                "display_name":raw_name,"raw":raw_name}

    is_estate  = any(w in name for w in ESTATE_WORDS)
    is_business = any(w in name for w in BUSINESS_WORDS) and not is_estate

    # Clean suffixes for parsing
    clean = name
    for s in SUFFIX_WORDS + ESTATE_WORDS:
        clean = clean.replace(s,"").strip()
    clean = re.sub(r'\s+', ' ', clean).strip()

    # Parse Last First format
    parts = clean.split()
    if len(parts) >= 2 and not is_business:
        last  = parts[0]
        first = parts[1]
        courts_search = f"{last}, {first}"
    elif is_business:
        courts_search = name.split()[0]  # Search by first word of company
    else:
        courts_search = parts[0] if parts else name

    # Classification
    if is_estate:
        owner_type = "estate"
        priority   = "high"
        reason     = "Estate/heir — title complications, highly motivated to sell"
        contact    = "Contact estate administrator or known heirs"
        note       = "Higher complexity but often desperate to resolve"
    elif is_business:
        owner_type = "business"
        priority   = "medium"
        reason     = "Business entity — investment/commercial property"
        contact    = "Formal letter to registered agent, then call"
        note       = "Negotiate on ROI and numbers"
    else:
        owner_type = "individual"
        priority   = "high"
        reason     = "Individual homeowner — likely primary residence, highly motivated"
        contact    = "Door knock first, then direct mail, then phone"
        note       = "Lead with empathy — help them resolve situation"

    return {
        "type":          owner_type,
        "priority":      priority,
        "reason":        reason,
        "contact":       contact,
        "note":          note,
        "courts_search": courts_search,
        "display_name":  raw_name.strip(),
        "raw":           raw_name
    }

def parse_amount(val: str) -> float:
    try: return float(str(val).replace(',','').replace('$','').strip())
    except: return 0.0

def extract_account(url: str) -> str:
    try: return url.split('can=')[1].split('&')[0]
    except: return ""

def load_csv(filepath: str) -> list:
    """Load and parse the backtax CSV."""
    rows = []
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            owner_raw = row.get('Owner','').strip()
            if not owner_raw: continue
            
            total_due = parse_amount(row.get('Total amount due','0'))
            curr_due  = parse_amount(row.get('Current amount due','0'))
            prior_due = parse_amount(row.get('Prior amount due','0'))
            account   = extract_account(row.get('Property page',''))
            
            # Build address
            addr_parts = [row.get(f'Address {i}','').strip() for i in range(2,5)]
            addr = next((a for a in addr_parts if a), '')
            city  = row.get('City','').strip()
            state = row.get('State','TX').strip()
            zip_  = row.get('Zip','').strip()
            full_addr = f"{addr}, {city}, {state} {zip_}".strip(', ')

            rows.append({
                "owner_raw":    owner_raw,
                "owner":        parse_owner(owner_raw),
                "total_due":    total_due,
                "current_due":  curr_due,
                "prior_due":    prior_due,
                "account":      account,
                "address":      full_addr,
                "property_url": row.get('Property page',''),
                "appraisal_url":row.get('Appraisal page',''),
                "homestead":    bool(row.get('Homestead','').strip()),
                "over65":       bool(row.get('Over 65','').strip()),
                "veteran":      bool(row.get('Veteran','').strip()),
                "disabled":     bool(row.get('Disabled','').strip()),
                "bankruptcy_no":row.get('Bankruptcy number','').strip(),
                "bankruptcy_dt":row.get('Bankruptcy filed date','').strip(),
                "year_built":   row.get('Year built','').strip(),
                "appraised_val":parse_amount(row.get('Appraised value','0')),
                "sq_ft":        row.get('Square foot','').strip(),
                "legal_desc":   row.get('Legal description','').strip(),
            })
    return rows

def save_prospect_to_db(prospect: dict, tax_suit: str = "", memo: str = ""):
    """Save delinquent account to prospects table (pre-suit pipeline)."""
    if not DB_PATH.exists(): return
    with sqlite3.connect(DB_PATH) as db:
        # Create prospects table if not exists
        db.execute("""CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_number TEXT UNIQUE,
            owner_name TEXT,
            owner_type TEXT,
            owner_priority TEXT,
            property_address TEXT,
            total_due REAL,
            current_due REAL,
            prior_due REAL,
            appraised_value REAL,
            homestead INTEGER DEFAULT 0,
            over65 INTEGER DEFAULT 0,
            bankruptcy_number TEXT,
            tax_suit_number TEXT,
            has_suit INTEGER DEFAULT 0,
            property_url TEXT,
            appraisal_url TEXT,
            ai_memo TEXT,
            courts_search_name TEXT,
            added_date TEXT,
            last_checked TEXT
        )""")
        
        owner = prospect['owner']
        now   = datetime.now().isoformat()
        
        db.execute("""INSERT INTO prospects 
            (account_number,owner_name,owner_type,owner_priority,property_address,
             total_due,current_due,prior_due,appraised_value,homestead,over65,
             bankruptcy_number,tax_suit_number,has_suit,property_url,appraisal_url,
             ai_memo,courts_search_name,added_date,last_checked)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_number) DO UPDATE SET
            total_due=excluded.total_due, current_due=excluded.current_due,
            tax_suit_number=excluded.tax_suit_number, has_suit=excluded.has_suit,
            ai_memo=excluded.ai_memo, last_checked=excluded.last_checked""",
            [prospect['account'], owner['display_name'], owner['type'],
             owner['priority'], prospect['address'],
             prospect['total_due'], prospect['current_due'], prospect['prior_due'],
             prospect['appraised_val'], 1 if prospect['homestead'] else 0,
             1 if prospect['over65'] else 0, prospect['bankruptcy_no'],
             tax_suit, 1 if tax_suit else 0,
             prospect['property_url'], prospect['appraisal_url'],
             memo, owner['courts_search'], now, now])
        db.commit()

# ── CLAUDE AI ─────────────────────────────────────────────────
EXTRACTION_PROMPT = """You are a Texas tax foreclosure analyst for PC Peak Development.
Extract ALL case data. Return ONLY valid JSON:
{"caseNumber":"","court":"","judicialOfficer":"","filedDate":"YYYY-MM-DD",
"caseStatus":"open","judgmentDate":"","judgmentType":"none",
"defendant":"","allDefendants":[],"propertyAddress":"","accountNumber":"",
"lawFirm":"","plaintiffAttorney":"","totalDueAtFiling":0,
"delinquencyYears":[],"oldestDelinquencyYear":0,
"taxBreakdown":[{"entity":"","taxAmt":0,"penaltyInterest":0,"total":0}],
"defCount":1,"citationByPostingRequested":false,"rule106SubstituteService":false,
"priorRelatedSuits":[],"estateHeirSituation":false,
"continuanceCount":0,"trialResetCount":0,"serviceIssues":"",
"nextHearingDate":"","orderOfSaleIssued":false,
"complexity":"low","complexityReason":"",
"keyDocketEvents":[{"date":"YYYY-MM-DD","event":"","type":"filing"}]}"""

MEMO_PROMPT = """Acquisition memo for PC Peak Development.
Owner type: {owner_type} — {owner_reason}
Contact approach: {contact}
Delinquent balance from dallasact.com: ${total_due:,.2f} (current: ${current_due:,.2f}, prior: ${prior_due:,.2f})
Appraised value: ${appraised_val:,.2f}
Homestead exemption: {homestead} | Over 65: {over65}

{suit_context}

Write 3 paragraphs:
1. PROPERTY & OWNER: Situation, debt load vs appraised value, owner type significance, equity position
2. TIMELINE & URGENCY: {timeline_context}
3. ACQUISITION STRATEGY: Specific approach for THIS owner type — offer range based on debt/value ratio, exact contact method, urgency level

Be direct. Specific dollar ranges. No fluff."""

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

async def claude_memo_prospect(prospect: dict, suit_case: str = "") -> str:
    if not ANTHROPIC_KEY: return ""
    owner = prospect['owner']
    
    if suit_case:
        suit_context = f"TAX SUIT FILED: {suit_case} — case is active in Dallas County courts."
        timeline = "Tax suit is filed. Reference benchmarks: TX-23-00042 (HIGH: 37mo→J, 89d→OOS), TX-25-00492 (LOW: 14mo→J). Estimate time to judgment and OOS."
    else:
        suit_context = "NO TAX SUIT YET — owner is delinquent but LGBS has not filed yet. This is an early-stage opportunity."
        timeline = "No suit filed yet. With this balance level, LGBS typically files within 6-18 months. This is the BEST time to approach — before legal pressure escalates."

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0,connect=30.0)) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                json={"model":"claude-sonnet-4-5","max_tokens":700,
                      "messages":[{"role":"user","content":MEMO_PROMPT.format(
                          owner_type=owner['type'],
                          owner_reason=owner['reason'],
                          contact=owner['contact'],
                          total_due=prospect['total_due'],
                          current_due=prospect['current_due'],
                          prior_due=prospect['prior_due'],
                          appraised_val=prospect['appraised_val'],
                          homestead="YES — PRIMARY RESIDENCE" if prospect['homestead'] else "No",
                          over65="YES — SENIOR CITIZEN" if prospect['over65'] else "No",
                          suit_context=suit_context,
                          timeline_context=timeline)}]})
            return next((b["text"] for b in r.json().get("content",[]) if b["type"]=="text"),"")
    except: return ""

async def save_case_to_cases_db(extracted: dict, memo: str, owner: dict):
    """Save to main cases table."""
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
    with sqlite3.connect(DB_PATH) as db:
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
        db.commit()

# ── BROWSER AGENT ─────────────────────────────────────────────
class BacktaxImporter:
    def __init__(self):
        self.page = None
        self.browser = None
        self.captcha_solved = False
        self.stats = {
            "total":0,"individuals":0,"businesses":0,"estates":0,
            "suits_found":0,"no_suit":0,"processed":0,"errors":0,
            "skipped_balance":0,"skipped_bankruptcy":0
        }

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

    async def handle_captcha(self):
        has = await self.page.evaluate(
            "()=>!!document.querySelector('iframe[src*=recaptcha],.g-recaptcha,[data-sitekey]')")
        if not has: return
        if not self.captcha_solved:
            self.log("CAPTCHA — solve in browser then press ENTER")
            input("Press ENTER after solving CAPTCHA...")
            self.captcha_solved = True
            await asyncio.sleep(1)

    async def search_courts(self, query: str) -> list:
        """Search courts portal for a name or case number."""
        await self.page.goto(PORTAL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        await self.handle_captcha()

        await self.page.evaluate(f"""() => {{
            const inputs = [...document.querySelectorAll('input[type=text],input:not([type])')];
            const inp = inputs.find(i=>i.offsetParent!==null)||inputs[0];
            if(inp){{
                inp.value=`{query.replace('`',"'")}`;
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
        await asyncio.sleep(4)

        results = await self.page.evaluate("""()=>{
            const out=[];
            document.querySelectorAll('table tbody tr').forEach(row=>{
                const a=row.querySelector('a');
                const text=row.innerText||'';
                const m=text.match(/TX-\\d{2}-\\d{5}/);
                if(m) out.push({caseNumber:m[0],href:a?a.href:'',rowText:text.trim()});
            });
            if(out.length===0){
                document.querySelectorAll('a').forEach(a=>{
                    const m=(a.innerText||'').match(/TX-\\d{2}-\\d{5}/);
                    if(m&&!out.find(x=>x.caseNumber===m[0]))
                        out.push({caseNumber:m[0],href:a.href,rowText:a.innerText});
                });
            }
            return out;
        }""")
        return results

    async def process_suit(self, case_number: str, href: str, prospect: dict) -> bool:
        """Navigate to case detail and process it."""
        try:
            if href and "CaseDetail" in href:
                await self.page.goto(href, wait_until="domcontentloaded", timeout=20000)
            else:
                results = await self.search_courts(case_number)
                if not results: return False
                href = results[0].get("href","")
                if href: await self.page.goto(href, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            docket_text = await self.page.inner_text("body")
            if len(docket_text) < 300: return False

            # Save docket
            pdf_dir = PDF_DIR / case_number
            pdf_dir.mkdir(parents=True, exist_ok=True)
            (pdf_dir/"docket.txt").write_text(docket_text, encoding="utf-8")

            # Extract
            extracted = await claude_extract(docket_text)
            if not extracted: return False
            extracted["caseNumber"] = case_number

            # Generate memo with both dallasact balance data AND suit data
            memo = await claude_memo_prospect(prospect, case_number)

            # Save to cases table
            await save_case_to_cases_db(extracted, memo, prospect['owner'])
            return True
        except Exception as e:
            self.log(f"  Error processing {case_number}: {e}")
            return False

    async def run(self, args):
        # Load CSV
        self.log(f"Loading CSV: {args.file}")
        all_prospects = load_csv(args.file)
        self.log(f"Loaded {len(all_prospects)} accounts")

        # Filter
        prospects = []
        for p in all_prospects:
            # Balance filter
            if p['total_due'] < args.min_balance:
                self.stats["skipped_balance"] += 1
                continue
            # Bankruptcy filter
            if p['bankruptcy_no'] and not args.include_bankruptcy:
                self.stats["skipped_bankruptcy"] += 1
                continue
            # Individuals only filter
            if args.individuals_only and p['owner']['type'] == 'business':
                continue
            prospects.append(p)

        # Dedup by owner name
        seen_owners = set()
        unique_prospects = []
        for p in prospects:
            key = p['owner']['courts_search']
            if key not in seen_owners:
                seen_owners.add(key)
                unique_prospects.append(p)

        self.stats["total"] = len(unique_prospects)
        self.log(f"After filters: {len(unique_prospects)} unique owners to process")
        self.log(f"  (skipped {self.stats['skipped_balance']} below ${args.min_balance:,.0f} minimum)")
        self.log(f"  (skipped {self.stats['skipped_bankruptcy']} bankruptcy cases)")

        # Count by type
        for p in unique_prospects:
            t = p['owner']['type']
            if t == 'individual': self.stats["individuals"] += 1
            elif t == 'business': self.stats["businesses"] += 1
            elif t == 'estate':   self.stats["estates"] += 1

        self.log(f"\nOwner breakdown:")
        self.log(f"  🔴 Individuals: {self.stats['individuals']} (high priority — door knock)")
        self.log(f"  🟡 Businesses:  {self.stats['businesses']} (medium priority — formal letter)")
        self.log(f"  ⚠️  Estates:     {self.stats['estates']} (high priority — contact administrator)")

        if not args.dry_run:
            await self.start()

        # Process each prospect
        for i, prospect in enumerate(unique_prospects):
            owner = prospect['owner']
            self.log(f"\n[{i+1}/{len(unique_prospects)}] {owner['display_name']} | {owner['type'].upper()} | ${prospect['total_due']:,.2f}")
            self.log(f"  Address: {prospect['address']}")
            self.log(f"  Courts search: '{owner['courts_search']}'")

            if args.dry_run:
                self.log(f"  [DRY RUN — would search courts portal and process]")
                # Still save to prospects table without browser
                memo = f"[Dry run — {owner['type']} owner, ${prospect['total_due']:,.2f} delinquent, address: {prospect['address']}]"
                save_prospect_to_db(prospect, "", memo)
                self.stats["no_suit"] += 1
                continue

            # Search courts portal
            search_results = await self.search_courts(owner['courts_search'])

            if search_results:
                self.log(f"  ✓ TAX SUIT FOUND: {[r['caseNumber'] for r in search_results]}")
                self.stats["suits_found"] += 1

                for result in search_results:
                    cn = result['caseNumber']
                    self.log(f"  Processing suit {cn}...")
                    success = await self.process_suit(cn, result.get('href',''), prospect)
                    if success:
                        save_prospect_to_db(prospect, cn, "")
                        self.stats["processed"] += 1
                    else:
                        self.stats["errors"] += 1
                    await asyncio.sleep(2)
            else:
                self.log(f"  → No suit filed yet — adding to pre-suit prospect list")
                self.stats["no_suit"] += 1
                # Generate pre-suit memo
                memo = await claude_memo_prospect(prospect, "")
                save_prospect_to_db(prospect, "", memo)

            # Brief delay between owners
            if i < len(unique_prospects)-1:
                await asyncio.sleep(2)

        # Final report
        self.log(f"\n{'='*60}")
        self.log(f"IMPORT COMPLETE")
        self.log(f"  Total processed:    {self.stats['total']}")
        self.log(f"  Tax suits found:    {self.stats['suits_found']} → in your cases database")
        self.log(f"  Pre-suit prospects: {self.stats['no_suit']} → in your prospects pipeline")
        self.log(f"  Errors:             {self.stats['errors']}")
        self.log(f"{'='*60}")
        self.log(f"Go to taxforeclosureanalyzer.com → click Sync to see updated cases")
        self.log(f"Pre-suit prospects saved to database prospects table")

        if self.browser: await self.browser.close()

async def main():
    parser = argparse.ArgumentParser(
        description="PC Peak Backtax CSV Importer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Full import (searches every owner on courts portal):
    python3 import_backtax.py --file Backtax_application_test_file.csv

  Only accounts over $15,000:
    python3 import_backtax.py --file myfile.csv --min-balance 15000

  Individuals only (skip businesses):
    python3 import_backtax.py --file myfile.csv --individuals-only

  Dry run (loads CSV, classifies owners, saves to prospects — no browser):
    python3 import_backtax.py --file myfile.csv --dry-run

  Preview what's in the CSV without processing:
    python3 import_backtax.py --file myfile.csv --dry-run --min-balance 0
        """)
    parser.add_argument("--file",               required=True, help="Path to backtax CSV file")
    parser.add_argument("--min-balance",         type=float, default=5000, help="Minimum total due (default: $5,000)")
    parser.add_argument("--individuals-only",    action="store_true", help="Skip business entities")
    parser.add_argument("--include-bankruptcy",  action="store_true", help="Include bankruptcy cases")
    parser.add_argument("--dry-run",             action="store_true", help="Classify and save prospects without browser scraping")
    args = parser.parse_args()

    if not Path(args.file).exists():
        print(f"ERROR: File not found: {args.file}")
        return

    if not ANTHROPIC_KEY and not args.dry_run:
        print("ERROR: Set ANTHROPIC_API_KEY")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        return

    importer = BacktaxImporter()
    await importer.run(args)

if __name__ == "__main__":
    asyncio.run(main())
