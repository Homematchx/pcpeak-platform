"""
PC Peak — Smart Case Discovery Engine v3
Correct search strategies for Dallas County Courts Portal.
Detects duplicates, individual vs business ownership.

Usage:
  python3 discover.py --name "WILLIAMS"        # Search by last name
  python3 discover.py --name "ROGERS, LUCY"    # Search by last, first
  python3 discover.py --address "Hudspeth"     # Search by street name
  python3 discover.py --case "TX-26-00009"     # Single case
  python3 discover.py --bulk cases.txt         # Bulk list of case numbers
"""

import asyncio, json, os, re, sys, sqlite3, argparse, httpx
from pathlib import Path
from datetime import datetime, date

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "data" / "db" / "pcpeak.db"
PDF_DIR  = BASE_DIR / "data" / "pdfs"
PORTAL   = "https://courtsportal.dallascounty.org/DALLASPROD/Home/Dashboard/29"

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY","")

# ── BUSINESS DETECTION ────────────────────────────────────────
BUSINESS_KEYWORDS = [
    "LLC","INC","CORP","LTD","LP","LLP","PARTNERSHIP","TRUST",
    "PROPERTIES","HOLDINGS","INVESTMENTS","ENTERPRISES","GROUP",
    "REALTY","ASSOCIATES","MANAGEMENT","DEVELOPMENT","SERVICES",
    "COMPANY","CO.","FUND","ESTATE OF","ET AL"
]

def classify_owner(defendant: str) -> dict:
    """Classify owner as individual or business and assess priority."""
    if not defendant:
        return {"type":"unknown","priority":"medium","reason":"No defendant name"}
    
    name = defendant.upper()
    
    # Check for business indicators
    is_business = any(kw in name for kw in BUSINESS_KEYWORDS)
    
    # Check for estate/heir situation
    is_estate = "ESTATE" in name or "HEIR" in name or "ET AL" in name
    
    # Check if it looks like a person (Last, First format)
    is_person = bool(re.match(r'^[A-Z]+,\s+[A-Z]', name)) and not is_business
    
    # Multiple defendants (comma separated names)
    parts = [p.strip() for p in defendant.split('/')]
    multi_defendant = len(parts) > 1
    
    if is_estate:
        return {
            "type": "estate",
            "priority": "high",
            "reason": "Estate/heir situation — title complications, motivated to resolve",
            "contact_approach": "Contact estate administrator or known heirs directly",
            "acquisition_note": "Higher complexity but often highly motivated sellers"
        }
    elif is_business:
        return {
            "type": "business", 
            "priority": "medium",
            "reason": "Business entity — investment property, less emotional attachment",
            "contact_approach": "Formal letter to registered agent, then follow up call",
            "acquisition_note": "Negotiate on numbers — business owners respond to ROI arguments"
        }
    elif is_person:
        return {
            "type": "individual",
            "priority": "high",
            "reason": "Individual homeowner — likely primary residence, highly motivated",
            "contact_approach": "Door knock first, then direct mail, then phone",
            "acquisition_note": "Lead with empathy — offer to help resolve situation, not just buy"
        }
    else:
        return {
            "type": "unknown",
            "priority": "medium", 
            "reason": "Owner type unclear",
            "contact_approach": "Direct mail to property address",
            "acquisition_note": "Research further before contact"
        }

def is_duplicate(case_number: str) -> bool:
    """Check if case already exists in database."""
    if not DB_PATH.exists(): return False
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("SELECT id, last_agent_run FROM cases WHERE case_number=?", 
                        [case_number]).fetchone()
        if not row: return False
        # Consider it a duplicate if processed today
        if row[1] and row[1][:10] == date.today().isoformat():
            return True
        return False  # Exists but not updated today — re-process

# ── CLAUDE AI ─────────────────────────────────────────────────
EXTRACTION_PROMPT = """You are a Texas tax foreclosure analyst for PC Peak Development.
Extract ALL case data from this Dallas County court docket. Return ONLY valid JSON:
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
"keyDocketEvents":[{"date":"YYYY-MM-DD","event":"","type":"filing"}]}
Complexity: low=1-2 defendants, no estate, <8yr delinquent. high=estate/deceased, 15+yr, 5+ defendants."""

MEMO_PROMPT = """You are a distressed property acquisition analyst for PC Peak Development.
Owner classification: {owner_type} — {owner_reason}

Write a 3-paragraph acquisition memo:
1. PROPERTY & OWNER: Who owns it, debt amount, delinquency history, owner type significance
2. TIMELINE: When is judgment/OOS expected based on benchmarks (TX-23-00042: HIGH 37mo→J 89d→OOS, TX-25-00492: LOW 14mo→J)
3. ACQUISITION STRATEGY: Contact approach for this owner type, offer range, urgency level

Case: {case_data}
Be specific. Dollar ranges. Contact method. Urgency."""

async def claude_extract(text: str) -> dict:
    if not ANTHROPIC_KEY: return {}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as client:
                r = await client.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                    json={"model":"claude-sonnet-4-5","max_tokens":2000,"system":EXTRACTION_PROMPT,
                          "messages":[{"role":"user","content":f"Extract:\n\n{text[:5000]}"}]})
                d = r.json()
                if d.get("error"): await asyncio.sleep(5); continue
                raw = next((b["text"] for b in d.get("content",[]) if b["type"]=="text"), "")
                raw = re.sub(r"```json|```","",raw).strip()
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if m: return json.loads(m.group())
        except Exception as e:
            print(f"  Extract attempt {attempt+1}: {e}")
            if attempt < 2: await asyncio.sleep(5)
    return {}

async def claude_memo(extracted: dict, owner_info: dict) -> str:
    if not ANTHROPIC_KEY: return ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=30.0)) as client:
            r = await client.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                json={"model":"claude-sonnet-4-5","max_tokens":700,
                      "messages":[{"role":"user","content":MEMO_PROMPT.format(
                          owner_type=owner_info.get("type","unknown"),
                          owner_reason=owner_info.get("reason",""),
                          case_data=json.dumps(extracted,default=str)[:1500])}]})
            return next((b["text"] for b in r.json().get("content",[]) if b["type"]=="text"), "")
    except: return ""

def save_to_db(extracted: dict, memo: str, owner_info: dict):
    cn = extracted.get("caseNumber","")
    if not cn: return False
    now = datetime.now().isoformat()
    db_data = {
        "case_number":        cn,
        "court":              extracted.get("court",""),
        "judicial_officer":   extracted.get("judicialOfficer",""),
        "filed_date":         extracted.get("filedDate") or None,
        "case_status":        extracted.get("caseStatus","OPEN").upper(),
        "defendant":          extracted.get("defendant",""),
        "all_defendants":     json.dumps(extracted.get("allDefendants",[])),
        "property_address":   extracted.get("propertyAddress",""),
        "property_legal":     extracted.get("propertyLegalDescription",""),
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
        "trial_reset_count":  extracted.get("trialResetCount",0),
        "service_issues":     extracted.get("serviceIssues",""),
        "complexity":         extracted.get("complexity","low"),
        "complexity_reason":  extracted.get("complexityReason",""),
        "judgment_date":      extracted.get("judgmentDate") or None,
        "judgment_type":      extracted.get("judgmentType","none"),
        "oos_issued":         1 if extracted.get("orderOfSaleIssued") else 0,
        "oos_date":           extracted.get("orderOfSaleDate") or None,
        "next_hearing_date":  extracted.get("nextHearingDate") or None,
        "city":               "dallas",
        "tax_breakdown":      json.dumps(extracted.get("taxBreakdown",[])),
        "ai_memo":            memo,
        "owner_type":         owner_info.get("type","unknown"),
        "owner_priority":     owner_info.get("priority","medium"),
        "stage":              "oos_issued" if extracted.get("orderOfSaleIssued") else
                              "judgment_entered" if extracted.get("judgmentDate") else "pre_judgment",
        "last_agent_run":     now,
        "updated_at":         now,
        "monitored":          1,
    }
    with sqlite3.connect(DB_PATH) as db:
        # Add owner columns if they don't exist
        try:
            db.execute("ALTER TABLE cases ADD COLUMN owner_type TEXT")
            db.execute("ALTER TABLE cases ADD COLUMN owner_priority TEXT")
        except: pass
        
        exists = db.execute("SELECT id FROM cases WHERE case_number=?", [cn]).fetchone()
        if exists:
            sets = ", ".join(f"{k}=?" for k in db_data if k != "case_number")
            db.execute(f"UPDATE cases SET {sets} WHERE case_number=?",
                      [db_data[k] for k in db_data if k != "case_number"] + [cn])
        else:
            db_data["created_at"] = now
            cols = ", ".join(db_data.keys())
            db.execute(f"INSERT INTO cases ({cols}) VALUES ({','.join(['?']*len(db_data))})",
                      list(db_data.values()))
        for ev in extracted.get("keyDocketEvents",[]):
            db.execute("INSERT OR IGNORE INTO docket_events (case_number,event_date,event_type,description,is_new) VALUES (?,?,?,?,1)",
                      [cn, ev.get("date"), ev.get("type","filing"), ev.get("event","")])
        db.commit()
    return True

# ── AGENT ─────────────────────────────────────────────────────
class Discoverer:
    def __init__(self):
        self.page = None
        self.browser = None
        self.captcha_solved = False
        self.stats = {"found":0,"processed":0,"skipped_dup":0,"skipped_biz":0,"errors":0}

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

    async def search_portal(self, query: str) -> list:
        """Search and return list of (case_number, href) tuples."""
        await self.page.goto(PORTAL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        await self.handle_captcha()

        # Fill search
        await self.page.evaluate(f"""() => {{
            const inputs = [...document.querySelectorAll('input[type=text],input:not([type])')];
            const inp = inputs.find(i=>i.offsetParent!==null) || inputs[0];
            if (inp) {{
                inp.value = '{query}';
                inp.dispatchEvent(new Event('input',{{bubbles:true}}));
                inp.dispatchEvent(new Event('change',{{bubbles:true}}));
            }}
        }}""")
        await asyncio.sleep(0.5)
        await self.page.evaluate("""() => {
            const b=[...document.querySelectorAll('input[type=submit],button[type=submit],input[value=Submit],button')]
                .find(x=>(x.value||x.textContent||'').toLowerCase().includes('submit'));
            if(b) b.click(); else { const f=document.querySelector('form'); if(f) f.submit(); }
        }""")
        await asyncio.sleep(4)

        # Extract results
        results = await self.page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('table tbody tr').forEach(row => {
                const link = row.querySelector('a');
                const text = row.innerText || '';
                const m = text.match(/TX-\\d{2}-\\d{5}/);
                if (m) out.push({caseNumber: m[0], href: link ? link.href : ''});
            });
            if (out.length === 0) {
                document.querySelectorAll('a').forEach(a => {
                    const m = (a.innerText||'').match(/TX-\\d{2}-\\d{5}/);
                    if (m) out.push({caseNumber: m[0], href: a.href});
                });
            }
            return out;
        }""")
        
        # Deduplicate
        seen = set()
        unique = []
        for r in results:
            if r['caseNumber'] not in seen:
                seen.add(r['caseNumber'])
                unique.append((r['caseNumber'], r['href']))
        
        self.log(f"  Search '{query}' → {len(unique)} cases found")
        return unique

    async def process_case(self, case_number: str, href: str = "") -> str:
        """Navigate to case, extract data, save. Returns status."""
        # Duplicate check
        if is_duplicate(case_number):
            self.log(f"  ⟳ {case_number} — already processed today, skipping")
            self.stats["skipped_dup"] += 1
            return "duplicate"

        # Navigate to case
        try:
            if href and href.startswith("http") and "CaseDetail" in href:
                await self.page.goto(href, wait_until="domcontentloaded", timeout=20000)
            else:
                # Search for exact case number and click
                results = await self.search_portal(case_number)
                if not results:
                    self.log(f"  ✗ {case_number} — not found in portal")
                    self.stats["errors"] += 1
                    return "not_found"
                _, case_href = results[0]
                if case_href:
                    await self.page.goto(case_href, wait_until="domcontentloaded", timeout=20000)
                else:
                    link = self.page.locator(f"a:has-text('{case_number}')").first
                    await link.click()
            await asyncio.sleep(2)
        except Exception as e:
            self.log(f"  ✗ Navigation failed for {case_number}: {e}")
            self.stats["errors"] += 1
            return "error"

        # Get docket text
        docket_text = await self.page.inner_text("body")
        if len(docket_text) < 200:
            self.log(f"  ✗ {case_number} — page too short, likely navigation error")
            self.stats["errors"] += 1
            return "error"

        # Save docket
        pdf_dir = PDF_DIR / case_number
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "docket.txt").write_text(docket_text, encoding="utf-8")

        # Extract with Claude
        self.log(f"  → Extracting {case_number}...")
        extracted = await claude_extract(docket_text)
        if not extracted:
            self.stats["errors"] += 1
            return "extract_failed"

        extracted["caseNumber"] = case_number

        # Classify owner
        owner_info = classify_owner(extracted.get("defendant",""))
        
        self.log(f"  → {case_number} | {owner_info['type'].upper()} | {extracted.get('defendant','')} | ${extracted.get('totalDueAtFiling',0):,.0f} | {owner_info['priority'].upper()} priority")

        # Generate memo
        memo = await claude_memo(extracted, owner_info)

        # Save to DB
        save_to_db(extracted, memo, owner_info)
        self.stats["processed"] += 1
        return "success"

    async def run_search(self, query: str, skip_business: bool = False):
        """Search portal and process all results."""
        self.log(f"\nSearching: '{query}'")
        results = await self.search_portal(query)
        
        if not results:
            self.log("No cases found")
            return
        
        self.stats["found"] += len(results)
        self.log(f"Processing {len(results)} cases...")
        
        for i, (cn, href) in enumerate(results):
            self.log(f"\nCase {i+1}/{len(results)}: {cn}")
            
            # Quick business check from results page text if possible
            status = await self.process_case(cn, href)
            
            if i < len(results) - 1:
                await asyncio.sleep(2)

    async def run(self, args):
        await self.start()
        
        try:
            if args.case:
                for cn in args.case:
                    await self.process_case(cn.upper())
            elif args.bulk:
                cases = [l.strip().upper() for l in open(args.bulk) 
                        if l.strip() and not l.startswith("#")]
                self.log(f"Bulk processing {len(cases)} cases from {args.bulk}")
                for i, cn in enumerate(cases):
                    self.log(f"\nCase {i+1}/{len(cases)}: {cn}")
                    await self.process_case(cn)
                    if i < len(cases)-1: await asyncio.sleep(2)
            elif args.name:
                await self.run_search(args.name)
            elif args.address:
                await self.run_search(args.address)
            else:
                self.log("Please provide a search query. Run with --help for options.")
        finally:
            self.log(f"\n{'='*60}")
            self.log(f"COMPLETE")
            self.log(f"  Found:          {self.stats['found']}")
            self.log(f"  Processed:      {self.stats['processed']}")
            self.log(f"  Duplicates:     {self.stats['skipped_dup']}")
            self.log(f"  Errors:         {self.stats['errors']}")
            self.log(f"{'='*60}")
            self.log(f"Refresh taxforeclosureanalyzer.com → click Sync")
            if self.browser: await self.browser.close()

async def main():
    parser = argparse.ArgumentParser(
        description="PC Peak Discovery Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Search by last name:
    python3 discover.py --name "WILLIAMS"
    python3 discover.py --name "ROGERS"

  Search by address:  
    python3 discover.py --address "Atlanta Street"
    python3 discover.py --address "Hudspeth"

  Single case:
    python3 discover.py --case TX-26-00009

  Multiple cases:
    python3 discover.py --case TX-26-00009 TX-25-00492 TX-23-00042

  Bulk from file (cases.txt with one case per line):
    python3 discover.py --bulk cases.txt
        """)
    parser.add_argument("--name",    help="Search by defendant last name (e.g. WILLIAMS)")
    parser.add_argument("--address", help="Search by street name (e.g. 'Harbor Road')")
    parser.add_argument("--case",    nargs="+", help="One or more exact case numbers")
    parser.add_argument("--bulk",    help="Text file with one case number per line")
    args = parser.parse_args()

    if not any([args.name, args.address, args.case, args.bulk]):
        parser.print_help()
        return

    if not ANTHROPIC_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        return

    await Discoverer().run(args)

if __name__ == "__main__":
    asyncio.run(main())
