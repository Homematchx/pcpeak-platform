"""
PC Peak -- Case Discovery Engine v7
Correct flow:
1. Search TX-26-00*** -> get results list (case numbers + party names)
2. For each case: click the case number link -> lands on case detail page
3. Read full docket from case detail page (address, debt, attorney, events)
4. Run Claude extraction + memo
5. Save to database
6. Click 'Search Results' breadcrumb -> back to results list
7. Click next case
"""
import asyncio, json, os, re, sqlite3, argparse, httpx
from pathlib import Path
from datetime import datetime, date

DB_PATH = Path("/Users/stephenlewis/Downloads/pcpeak_platform/data/db/pcpeak.db")
PDF_DIR = Path("/Users/stephenlewis/Downloads/pcpeak_platform/data/pdfs")
PORTAL  = "https://courtsportal.dallascounty.org/DALLASPROD/Home/Dashboard/29"


def _load_local_env():
    """Load KEY=VALUE lines from a local, gitignored env file into os.environ (never
    overwriting an already-set var — a real shell export always wins). Zero-dependency;
    the project's secrets live in a gitignored env file the code wouldn't otherwise read.
    Checked in order; the first that exists wins."""
    here = Path(__file__).parent
    for name in (".env", "Anthropic_API_KEY.env"):
        f = here / name
        if not f.exists():
            continue
        for raw in f.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
        return name
    return None


_ENV_FILE = _load_local_env()
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
TWO_CAPTCHA_KEY = os.environ.get("TWO_CAPTCHA_KEY", "")

BUSINESS_WORDS = ["LLC","INC","CORP","LTD","TRUST","PROPERTIES","HOLDINGS",
    "INVESTMENTS","GROUP","REALTY","ASSOCIATES","MANAGEMENT","DEVELOPMENT",
    "SERVICES","COMPANY","CO","DBA","FUND","FUNERAL","CHURCH","FOUNDATION",
    "ENTERPRISES","PARTNERS","PARTNERSHIP","LP","LLP","BANK","CONSTRUCTION",
    "BUILDERS","TITLE","TILE","GRANITE","AUTO","MOTORS"]
ESTATE_WORDS = ["EST OF","ESTATE OF","ESTATE","HEIR","LIFE ESTATE"]

def classify(name):
    n = name.upper()
    if any(w in n for w in ESTATE_WORDS):
        return {"type":"estate","priority":"high","contact":"Contact estate administrator"}
    # Word-boundary match so short abbreviations (CO, LP) don't false-positive
    # on substrings inside individual names (e.g. "COOK", "SCOTT", "NICOLE").
    if any(re.search(r'(?<![A-Z])' + re.escape(w) + r'(?![A-Z])', n) for w in BUSINESS_WORDS):
        return {"type":"business","priority":"medium","contact":"Formal letter to registered agent"}
    return {"type":"individual","priority":"high","contact":"Door knock first, then direct mail"}


def partition_page(rows, open_only, skip_biz):
    """Split a page of result rows into what we process vs. what each filter drops — COUNTING
    every drop so the Found total always reconciles (Found = targets + closed + business, and
    downstream Processed+Skipped+Errors over the targets). Pure (only depends on classify), so
    it's unit-tested without the portal. open_only keeps OPEN or blank-status rows; skip_biz
    drops business-type parties. Returns {"targets", "closed", "business"}."""
    if open_only:
        kept = [r for r in rows
                if "OPEN" in (r.get("status", "") or "").upper() or (r.get("status", "") or "") == ""]
    else:
        kept = list(rows)
    closed = len(rows) - len(kept)
    business = 0
    if skip_biz:
        before = len(kept)
        kept = [r for r in kept if classify(r.get("partyName", ""))["type"] != "business"]
        business = before - len(kept)
    return {"targets": kept, "closed": closed, "business": business}

def valid_dcad_accounts(acct):
    """A Dallas DCAD account is exactly 17 CHARACTERS — usually all digits, but some
    parcels (condos/townhomes/special) use 17-char ALPHANUMERIC IDs (e.g.
    '0067850D0010A0000', '382020500K0150000') that are equally valid. A multi-tract
    petition can list several (comma/semicolon separated). Returns the list of valid
    17-char accounts, uppercased (empty if none). Anything else — a 5-digit Garland
    number, a lot number — is rejected rather than passed to enrichment as garbage."""
    if not acct:
        return []
    return [a.upper() for a in re.split(r"[,;]\s*", str(acct).strip())
            if re.fullmatch(r"[0-9A-Za-z]{17}", a)]

def property_type_from_docket(docket_text):
    """Tyler docket has a structured 'Comment' field = REAL PROPERTY | PERSONAL PROPERTY.
    This is the authoritative Real-vs-BPP discriminator (verified 123 REAL / 20 PERSONAL,
    clean). Returns 'personal' | 'real' | None (no Comment field found)."""
    if not docket_text:
        return None
    m = re.search(r'Comment\s*\n\s*-?\s*([^\n]+)', docket_text, re.IGNORECASE)
    if not m:
        return None
    v = m.group(1).upper()
    if "PERSONAL PROPERTY" in v:
        return "personal"
    if "REAL PROPERTY" in v:
        return "real"
    return None

def case_track_of(property_type, oos_date, judgment_type, judgment_date, tax_balance):
    """Which dataset/track a case belongs to (mirror of backend.case_track_of — keep in
    sync). personal_property | oos_timing | dismissed_owing | dismissed_paid |
    judged_pending | active. personal_property is checked FIRST so a BPP suit never
    reaches the timing track even if it somehow gets an oos_date."""
    if property_type == "personal":
        return "personal_property"
    if oos_date and str(oos_date).strip():
        return "oos_timing"
    jt = (judgment_type or "").upper()
    if "DISMISS" in jt or "NON-SUIT" in jt or "NONSUIT" in jt:
        if tax_balance is None:
            return "dismissed_owing"
        return "dismissed_owing" if tax_balance > 0 else "dismissed_paid"
    has_judgment = bool(judgment_date and str(judgment_date).strip()) or (jt and jt not in ("", "NONE"))
    return "judged_pending" if has_judgment else "active"

def pad_pattern(raw):
    r = raw.rstrip("*").upper()
    parts = r.split("-")
    digits = len(parts[2]) if len(parts) == 3 else 0
    return r + ("*" * (5 - digits))

def parse_rows_from_text(text):
    rows = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r'(TX-\d{2}-\d{5})', line)
        if not m:
            continue
        cn = m.group(1)
        if cn in seen:
            continue
        seen.add(cn)
        parts = [p.strip() for p in re.split(r'\t|  +', line) if p.strip()]
        idx = next((i for i, p in enumerate(parts) if cn in p), 0)
        rows.append({
            "caseNumber": cn,
            "fileDate":   parts[idx+1] if len(parts) > idx+1 else "",
            "status":     parts[idx+3] if len(parts) > idx+3 else "",
            "court":      parts[idx+4] if len(parts) > idx+4 else "",
            "partyName":  parts[idx+5] if len(parts) > idx+5 else "",
            "href":       ""
        })
    return rows

EXTRACTION_PROMPT = """You are a Texas tax foreclosure analyst for PC Peak Development.
Extract ALL case data from this Dallas County court docket AND Original Petition PDF.
Return ONLY valid JSON with NO markdown:
{"caseNumber":"","court":"","judicialOfficer":"","filedDate":"YYYY-MM-DD",
"caseStatus":"open","judgmentDate":"","judgmentType":"none",
"defendant":"","allDefendants":[{"name":"","address":"","serviceStatus":"unserved","serviceMethod":"","isInRemOnly":false,"notes":""}],
"propertyAddress":"","legalDescription":"","accountNumber":"",
"lawFirm":"","plaintiffAttorney":"","totalDueAtFiling":0,
"delinquencyYears":[],"oldestDelinquencyYear":0,
"taxBreakdown":[{"entity":"","taxAmt":0,"penaltyInterest":0,"total":0}],
"defCount":1,"citationByPostingRequested":false,"rule106SubstituteService":false,
"priorRelatedSuits":[],"estateHeirSituation":false,
"continuanceCount":0,"trialResetCount":0,"serviceIssues":"",
"nextHearingDate":"","orderOfSaleIssued":false,"orderOfSaleDate":"","saleScheduledDate":"",
"complexity":"low","complexityReason":"",
"keyDocketEvents":[{"date":"YYYY-MM-DD","event":"","type":"filing"}]}

CRITICAL EXTRACTION RULES:
1. totalDueAtFiling: Find "TOTAL DUE AS OF" on the LAST PAGE of the petition PDF. It shows as "TOTAL $X,XXX.XX" after all tax entity breakdowns. Extract the GRAND TOTAL dollar amount only.
2. allDefendants: Extract EVERY defendant from the ORIGINAL PETITION PDF section "DEFENDANT(S)". Each defendant is listed in bold with their address like: "Henry Alexander Borg, Jr., 1116 Hidden Meadow Dr., Burleson, TX 76025". Extract the FULL individual address for each defendant — NOT the property address. Mark defendants with "(In Rem Only)" or "(IN REM ONLY)". Use docket Events for service status.
3. propertyAddress: The physical property address from Exhibit A (e.g. "1530 E. Overton Rd., Dallas, TX 75216-5506").
4. accountNumber: The DCAD property account number from Exhibit A. A real Dallas DCAD account is EXACTLY 17 digits (e.g. "00000302749000000"). Copy only a genuine 17-digit DCAD account. If Exhibit A has no 17-digit DCAD account, or the property is outside Dallas County (e.g. Garland/Carrollton parcels in another appraisal district), return an empty string "". Do NOT substitute a lot number, cause number, city/utility account, or any other identifier, and never guess. For a multi-tract property listing several 17-digit accounts, return them comma-separated.
5. defendant: The PRIMARY defendant — find who is listed as "In Personam". Never use a Life Estate holder or In Rem Only defendant as primary.
6. complexity: "low" if single defendant served, "medium" if multiple or service issues, "high" if CBP/Rule106/estate/heir.
7. judgmentType: copy the docket's exact judgment/disposition wording — e.g. "JUDGMENT NON-JURY" (a real foreclosure judgment), "NON-SUIT/DISMISSAL BY PLAINTIFF" (a dismissal — NOT a foreclosure), "DEFAULT JUDGMENT". This distinction matters: a dismissal is not a path to sale. judgmentDate = the date of the FINAL judgment/disposition event (not a "PROPOSED ORDER/JUDGMENT").
8. orderOfSaleIssued / orderOfSaleDate: set orderOfSaleIssued=true ONLY if the docket has an actual "ISSUE ORDER OF SALE" or "ORDER OF SALE" ISSUED event (not merely a "REQUEST FOR ABSTRACT OF JUDGMENT AND ORDER OF SALE"). orderOfSaleDate = that event's date (YYYY-MM-DD). These outcome events appear in the "KEY DOCKET OUTCOME EVENTS" section and at the END of the docket — look there.
9. saleScheduledDate: if the docket or a notice states a foreclosure/constable/sheriff SALE or AUCTION date (e.g. "sale date 08/04/2026"), return it as YYYY-MM-DD; else "".
10. IMPORTANT: The petition PDF text comes after "ORIGINAL PETITION PDF:" in the input. Read it carefully for all defendant addresses and total due."""

async def claude_extract(text):
    if not ANTHROPIC_KEY:
        return {}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_KEY,
                             "anthropic-version": "2023-06-01",
                             "Content-Type": "application/json"},
                    json={"model": "claude-sonnet-4-6", "max_tokens": 4000,
                          "system": EXTRACTION_PROMPT,
                          "messages": [{"role": "user",
                                        "content": "Extract:\n\n" + text[:16000]}]})
                d = r.json()
                if d.get("error"):
                    await asyncio.sleep(5)
                    continue
                raw = next((b["text"] for b in d.get("content", [])
                            if b["type"] == "text"), "")
                raw = re.sub(r"```json|```", "", raw).strip()
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if m:
                    return json.loads(m.group())
        except Exception:
            if attempt < 2:
                await asyncio.sleep(5)
    return {}

async def claude_memo(extracted, owner, intel=None):
    if not ANTHROPIC_KEY:
        return ""
    try:
        # Build property intel summary for memo
        intel_summary = ""
        if intel and isinstance(intel, dict):
            # honest strings for the memo — 'unknown' (None) is not the same as a real $0
            _mvv = intel.get("market_value"); _balv = intel.get("current_tax_balance")
            mv = "unknown" if _mvv is None else f"${_mvv:,.0f}"
            bal = "unknown" if _balv is None else f"${_balv:,.2f}"
            yr = intel.get("year_built") or ""
            age = intel.get("actual_age") or ""
            dep = intel.get("depreciation_pct") or ""
            sqft = intel.get("living_area_sqft") or ""
            ext_wall = intel.get("exterior_wall") or ""
            no_hmstd = intel.get("no_homestead", False)
            deed = intel.get("deed_transfer_date") or ""
            owners = intel.get("owners", [])
            payments = intel.get("payment_history", [])
            distress = intel.get("distress", {})

            # Payment story
            payer_story = ""
            if payments:
                payers = {}
                for p in payments:
                    name = p.get("payer","").strip()
                    yr_p = p.get("tax_year","")
                    if name and name not in payers:
                        payers[name] = yr_p
                payer_story = "Payment history: " + "; ".join(
                    name + " (paid " + yr_p + ")" for name, yr_p in list(payers.items())[:5]
                )

            intel_summary = (
                f"\n\nPROPERTY INTELLIGENCE (DCAD + Dallas ACT live data):\n"
                f"- Market Value: {mv} | Live Tax Balance: {bal}\n"
                f"- Year Built: {yr} ({age} yrs old) | Depreciation: {dep}% | Size: {sqft} sqft\n"
                f"- Exterior: {ext_wall} | Deed Transfer: {deed}\n"
                f"- Homestead Exemption: {'NONE - likely absentee/non-owner-occupied' if no_hmstd else 'YES - owner occupied'}\n"
                f"- Ownership: {', '.join(o['name'] + ' ' + str(o['pct']) + '%' for o in owners[:3])}\n"
                f"- {payer_story}\n"
                f"- Distress Level: {distress.get('level','').upper()} (Score: {distress.get('score',0)})\n"
                f"- Distress Signals: {'; '.join(s['label'] for s in distress.get('signals',[])[:4])}\n"
            )

        prompt = (
            "You are a senior acquisition analyst for PC Peak Development, a Texas real estate investment firm.\n"
            "Write a deep, specific acquisition intelligence memo for this tax foreclosure case.\n"
            "Owner type: " + owner["type"] + " | Contact: " + owner["contact"] + "\n\n"
            "Write 4 paragraphs:\n"
            "1. PROPERTY & OWNER SITUATION: Physical condition analysis using DCAD data "
            "(age, depreciation, structure type, lot vs improvement value ratio). "
            "Is this a land play or structure play? Who are the prior owners and what does payment history reveal?\n"
            "2. FINANCIAL PICTURE: Current live tax balance vs filing amount, penalty acceleration, "
            "debt-to-value ratio, what payoff actually costs today.\n"
            "3. TIMELINE & URGENCY: When does judgment likely hit based on benchmarks "
            "(TX-23-00042 HIGH 37mo->J 89d->OOS, TX-25-00492 LOW 14mo->J). "
            "Service status impact. Trial date if any.\n"
            "4. ACQUISITION STRATEGY: Specific offer range based on market value and debt, "
            "contact method (door knock/mail/skip trace), key risks, and one actionable next step.\n"
            "Be specific with dollar amounts. Reference actual data. No generic language.\n\n"
            "CASE DATA:\n" + json.dumps(extracted, default=str)[:2000] +
            intel_summary
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=30.0)) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY,
                         "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 600,
                      "messages": [{"role": "user", "content": prompt}]})
            return next((b["text"] for b in r.json().get("content", [])
                         if b["type"] == "text"), "")
    except Exception:
        return ""

def save_to_db(extracted, memo, owner):
    cn = extracted.get("caseNumber", "")
    if not cn:
        return
    now = datetime.now().isoformat()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as db:
        for col in ["owner_type TEXT", "owner_priority TEXT", "property_intel TEXT", "legal_description TEXT", "account_status TEXT", "account_note TEXT", "case_track TEXT", "property_type TEXT"]:
            try:
                db.execute("ALTER TABLE cases ADD COLUMN " + col)
            except Exception:
                pass
        data = {
            "case_number": cn,
            "court": extracted.get("court", ""),
            "judicial_officer": extracted.get("judicialOfficer", ""),
            "filed_date": extracted.get("filedDate") or None,
            "case_status": extracted.get("caseStatus", "OPEN").upper(),
            "defendant": extracted.get("defendant", ""),
            "all_defendants": json.dumps(extracted.get("allDefendants", [])),
            "property_address": extracted.get("propertyAddress", ""),
            "account_number": extracted.get("accountNumber", ""),
            "petition_href": extracted.get("petitionHref") or None,
            # Default to "" (unknown), matching plaintiff_attorney/judicial_officer — never
            # fabricate a firm. LGBS is the common Dallas answer but must come from the document,
            # not a hardcoded default silently written onto a case that may be another firm.
            "law_firm": extracted.get("lawFirm", ""),
            "plaintiff_attorney": extracted.get("plaintiffAttorney", ""),
            "total_due_filing": extracted.get("totalDueAtFiling") or None,
            "oldest_delinquency_year": extracted.get("oldestDelinquencyYear") or None,
            "delinquency_years": json.dumps(extracted.get("delinquencyYears", [])),
            "def_count": extracted.get("defCount", 1),
            "cbp_requested": 1 if extracted.get("citationByPostingRequested") else 0,
            "rule106": 1 if extracted.get("rule106SubstituteService") else 0,
            "prior_suits": json.dumps(extracted.get("priorRelatedSuits", [])),
            "estate_heir": 1 if extracted.get("estateHeirSituation") else 0,
            "continuance_count": extracted.get("continuanceCount", 0),
            "service_issues": extracted.get("serviceIssues", ""),
            "complexity": extracted.get("complexity", "low"),
            "judgment_date": extracted.get("judgmentDate") or None,
            "judgment_type": extracted.get("judgmentType", "none"),
            "oos_issued": 1 if extracted.get("orderOfSaleIssued") else 0,
            "oos_date": extracted.get("orderOfSaleDate") or None,
            "sale_scheduled_date": extracted.get("saleScheduledDate") or None,
            "next_hearing_date": extracted.get("nextHearingDate") or None,
            "city": "dallas",
            "tax_breakdown": json.dumps(extracted.get("taxBreakdown", [])),
            "ai_memo": memo,
            "owner_type": owner.get("type", "individual"),
            "owner_priority": owner.get("priority", "high"),
            "stage": ("oos_issued" if extracted.get("orderOfSaleIssued") else
                      "judgment_entered" if extracted.get("judgmentDate")
                      else "pre_judgment"),
            "last_agent_run": now,
            "updated_at": now,
            "monitored": 1,
            "property_intel": extracted.get("_property_intel", ""),
            "legal_description": extracted.get("legalDescription", ""),
        }
        exists = db.execute(
            "SELECT id FROM cases WHERE case_number=?", [cn]).fetchone()
        if exists:
            sets = ", ".join(k + "=?" for k in data if k != "case_number")
            db.execute("UPDATE cases SET " + sets + " WHERE case_number=?",
                       [data[k] for k in data if k != "case_number"] + [cn])
        else:
            data["created_at"] = now
            cols = ", ".join(data.keys())
            vals = ", ".join(["?"] * len(data))
            db.execute("INSERT INTO cases (" + cols + ") VALUES (" + vals + ")",
                       list(data.values()))
        for ev in extracted.get("keyDocketEvents", []):
            db.execute(
                "INSERT OR IGNORE INTO docket_events "
                "(case_number,event_date,event_type,description,is_new) "
                "VALUES (?,?,?,?,1)",
                [cn, ev.get("date"), ev.get("type", "filing"), ev.get("event", "")])
        db.commit()


class Discoverer:
    def __init__(self, open_only=False, skip_biz=False, force=False):
        self.page = None
        self.browser = None
        self.open_only = open_only
        self.skip_biz = skip_biz
        self.force = force   # re-scrape even cases marked "complete today" (the completeness
                             # heuristic can be fooled — e.g. addr+debt present but sourced from
                             # the WRONG document; --force re-downloads + re-extracts)
        self.stats = {"found": 0, "processed": 0, "skipped": 0, "closed_skipped": 0,
                      "errors": 0, "invalid_account": 0, "no_account": 0}

    def log(self, msg):
        print("[" + datetime.now().strftime("%H:%M:%S") + "] " + str(msg))

    async def start(self):
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        import shutil
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if not Path(chrome).exists():
            chrome = shutil.which("google-chrome") or ""
        try:
            if chrome:
                self.browser = await pw.chromium.launch(
                    headless=False,
                    executable_path=chrome,
                    args=["--no-sandbox",
                          "--disable-blink-features=AutomationControlled"])
            else:
                raise Exception("No Chrome")
        except Exception:
            self.browser = await pw.chromium.launch(
                headless=False,
                args=["--no-sandbox",
                      "--disable-blink-features=AutomationControlled"])
        ctx = await self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"))
        self.page = await ctx.new_page()

    async def solve_2captcha(self, url):
        if not TWO_CAPTCHA_KEY:
            return None
        try:
            sk = await self.page.evaluate(
                "document.querySelector('[data-sitekey]')?.dataset?.sitekey || "
                "[...document.querySelectorAll('iframe')]"
                ".find(f=>f.src.includes('recaptcha'))"
                "?.src?.match(/[?&]k=([^&]+)/)?.[1] || null")
            if not sk:
                return None
            self.log("  Solving CAPTCHA via 2Captcha...")
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post("http://2captcha.com/in.php",
                    data={"key": TWO_CAPTCHA_KEY, "method": "userrecaptcha",
                          "googlekey": sk, "pageurl": url, "json": 1})
                d = r.json()
                if d.get("status") != 1:
                    # Surface the real reason (ERROR_WRONG_USER_KEY, ERROR_ZERO_BALANCE,
                    # etc.) instead of failing silently.
                    self.log("  2Captcha rejected submit: " + str(d.get("request", d)))
                    return None
                cid = d["request"]
                self.log("  Waiting (ID:" + str(cid) + ")...")
                for _ in range(24):
                    await asyncio.sleep(5)
                    r2 = await c.get(
                        "http://2captcha.com/res.php?key=" + TWO_CAPTCHA_KEY +
                        "&action=get&id=" + str(cid) + "&json=1")
                    d2 = r2.json()
                    if d2.get("status") == 1:
                        self.log("  ✓ CAPTCHA solved (ID:" + str(cid) + ")")
                        return d2["request"]
                    if d2.get("request") not in ("CAPCHA_NOT_READY",):
                        self.log("  2Captcha solve failed: " + str(d2.get("request")))
                        break
        except Exception as e:
            self.log("  2Captcha error: " + str(e))
        return None

    async def inject_token(self, token):
        script = (
            "document.querySelectorAll('[name=\"g-recaptcha-response\"]')"
            ".forEach(el=>{el.value='" + token + "';"
            "el.innerHTML='" + token + "';});"
            "try{"
            "var cfg=window.___grecaptcha_cfg;"
            "if(cfg&&cfg.clients){"
            "Object.values(cfg.clients).forEach(function(cl){"
            "var cb=Object.values(cl).find(function(v){"
            "return v&&typeof v.callback==='function';});"
            "if(cb)cb.callback('" + token + "');});}"
            "}catch(e){}")
        await self.page.evaluate(script)
        await asyncio.sleep(1)

    async def handle_captcha(self):
        has = await self.page.evaluate(
            "!!document.querySelector("
            "'iframe[src*=recaptcha],.g-recaptcha,[data-sitekey]')")
        if not has:
            return
        self.log("  CAPTCHA detected...")
        token = await self.solve_2captcha(self.page.url)
        if token:
            await self.inject_token(token)
            self.log("  CAPTCHA solved!")
            await asyncio.sleep(2)
            return
        self.log("  Solve CAPTCHA manually then press ENTER")
        input("  Press ENTER after solving...")
        await asyncio.sleep(1)

    async def go_to_portal_and_search(self, query):
        """Navigate to portal, handle CAPTCHA, fill search, submit.

        The Tyler Smart Search intermittently returns an empty results grid when
        the reCAPTCHA token hasn't been validated by the time the search fires.
        Retry the whole navigate -> solve -> submit cycle (fresh CAPTCHA each
        time) until case rows actually appear."""
        self.log("Searching portal: '" + query + "'")
        for attempt in range(4):
            await self.page.goto(PORTAL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            await self.handle_captcha()
            await asyncio.sleep(1)

            # Fill search input
            await self.page.evaluate(
                "(function(){"
                "var inputs=document.querySelectorAll('input[type=text],input:not([type])');"
                "var visible=[];"
                "for(var i=0;i<inputs.length;i++){"
                "if(inputs[i].offsetParent!==null)visible.push(inputs[i]);}"
                "var inp=visible[0]||inputs[0];"
                "if(inp){"
                "inp.value='" + query.replace("'", "") + "';"
                "inp.dispatchEvent(new Event('input',{bubbles:true}));"
                "inp.dispatchEvent(new Event('change',{bubbles:true}));}"
                "})()")
            await asyncio.sleep(0.5)

            # Click Submit
            sub = await self.page.evaluate(
                "(function(){"
                "var btns=document.querySelectorAll("
                "'input[type=submit],button[type=submit],input[value=Submit],button');"
                "for(var i=0;i<btns.length;i++){"
                "var t=(btns[i].value||btns[i].textContent||'').toLowerCase();"
                "if(t.indexOf('submit')>=0){btns[i].click();return 'CLICKED';}"
                "}"
                "var f=document.querySelector('form');"
                "if(f){f.submit();return 'SUBMIT';}"
                "return 'NONE';"
                "})()")
            self.log("Submit: " + str(sub) + (" (attempt " + str(attempt + 1) + ")" if attempt else ""))
            await asyncio.sleep(4)

            # Wait for results table
            for sel in ["table tbody tr", "tr", "td"]:
                try:
                    await self.page.wait_for_selector(sel, timeout=12000)
                    break
                except Exception:
                    continue
            await asyncio.sleep(2)

            # Did case rows actually load? If not, retry with a fresh CAPTCHA.
            body = await self.page.inner_text("body")
            if re.search(r'TX-\d{2}-\d{5}', body):
                self.log("Results ready" + (" (attempt " + str(attempt + 1) + ")" if attempt else ""))
                break
            if attempt < 3:
                self.log("  Empty results (reCAPTCHA likely not validated) — retrying search...")
                await asyncio.sleep(2)

        if os.environ.get("SCRAPE_DEBUG"):
            try:
                await self.page.screenshot(path="/tmp/portal_debug.png", full_page=True)
                body = await self.page.inner_text("body")
                open("/tmp/portal_debug.txt", "w").write(body)
                nrows = len(re.findall(r'TX-\d{2}-\d{5}', body))
                self.log("  DEBUG url=" + self.page.url)
                self.log("  DEBUG TX-rows in body=" + str(nrows) + " body_len=" + str(len(body)) +
                         " -> /tmp/portal_debug.{png,txt}")
            except Exception as de:
                self.log("  DEBUG dump failed: " + str(de))

    async def get_case_list_from_current_page(self):
        """Read all TX- case rows from current results page using Python parsing."""
        text = await self.page.inner_text("body")
        rows = parse_rows_from_text(text)

        # Get hrefs -- check both link text and surrounding context
        try:
            hrefs = await self.page.evaluate(
                "(function(){"
                "var out={};"
                "var links=document.querySelectorAll('a');"
                "for(var i=0;i<links.length;i++){"
                "var t=(links[i].innerText||links[i].textContent||'').trim();"
                "if(/^TX-[0-9][0-9]-[0-9][0-9][0-9][0-9][0-9]$/.test(t)){"
                "out[t]=links[i].href;}"
                "}"
                "return out;"
                "})()")
            found_hrefs = sum(1 for r in rows if hrefs.get(r["caseNumber"]))
            self.log("  hrefs captured: " + str(found_hrefs) + "/" + str(len(rows)))
            for row in rows:
                row["href"] = hrefs.get(row["caseNumber"], "")
        except Exception:
            pass

        return rows

    async def click_into_case(self, case_number, href=""):
        """
        Navigate to case detail page. Confirms by checking case_number AND
        "Events and Hearings" both appear in page text.
        Uses breadcrumb navigation back to search results between cases.
        """
        # Navigate back to search results first via breadcrumb
        self.log("  Going to Search Results...")
        back_clicked = await self.page.evaluate(
            "(function(){"
            "var els=document.querySelectorAll('a,li,span,div,ol');"
            "for(var i=0;i<els.length;i++){"
            "var t=(els[i].innerText||els[i].textContent||'').trim();"
            "if(t==='Search Results'||t==='2'||t==='2 Search Results'){"
            "els[i].click();return true;}"
            "}"
            "return false;"
            "})()"
        )
        self.log("  Back to results: " + str(back_clicked))
        await asyncio.sleep(3)

        # Click through to case detail — verify BOTH case_number AND Events in text.
        # The CaseDetail page loads its docket via AJAX, so poll for the content
        # to render, and RELOAD a blank detail page instead of re-clicking a
        # results link that no longer exists once we've navigated in.
        for attempt in range(5):
            try:
                text = ""
                for _ in range(7):                       # poll up to ~14s for docket to render
                    text = await self.page.inner_text("body")
                    if case_number in text and "Events and Hearings" in text:
                        break
                    await asyncio.sleep(2)
                self.log("  Attempt " + str(attempt+1) + ": " + str(len(text)) + " chars")

                if case_number in text and "Events and Hearings" in text:
                    self.log("  Correct docket confirmed: " + case_number)
                    return True

                # If we're already on a (blank) detail page, reload it to force
                # the docket to re-fetch; otherwise click the case link in results.
                if "CaseDetail" in (self.page.url or ""):
                    self.log("  Detail page not populated — reloading")
                    try:
                        await self.page.reload(wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    continue

                result = await self.page.evaluate(
                    "(function(){"
                    "var links=document.querySelectorAll('a');"
                    "for(var i=0;i<links.length;i++){"
                    "var t=(links[i].innerText||links[i].textContent||'').trim();"
                    "if(t==='" + case_number + "')"
                    "{links[i].click();return 'clicked-exact';}"
                    "}"
                    "var cd=document.querySelector('a[href*=CaseDetail]');"
                    "if(cd){cd.click();return 'clicked-detail';}"
                    "return null;"
                    "})()"
                )
                self.log("  Click result: " + str(result))
                if not result:
                    break
                await asyncio.sleep(2)
            except Exception as e:
                self.log("  Attempt " + str(attempt+1) + " error: " + str(e))
                await asyncio.sleep(2)

        try:
            text = await self.page.inner_text("body")
            if case_number in text and len(text) > 1600:
                self.log("  Docket confirmed on final check")
                return True
            self.log("  FAILED: " + case_number + " not found in page")
        except Exception:
            pass
        return False
    async def back_to_search_results(self):
        """
        Click 'Search Results' breadcrumb to return to Tab 2.
        This avoids triggering a new CAPTCHA.
        """
        try:
            clicked = await self.page.evaluate(
                "(function(){"
                "var links=document.querySelectorAll('a,li,span,div');"
                "for(var i=0;i<links.length;i++){"
                "var t=(links[i].innerText||links[i].textContent||'').trim();"
                "if(t==='Search Results'||t==='2'||t==='2 Search Results'){"
                "links[i].click();return true;}"
                "}"
                "return false;"
                "})()")
            if clicked:
                await asyncio.sleep(2)
                return True
        except Exception:
            pass
        # Fallback: browser back
        try:
            await self.page.go_back()
            await asyncio.sleep(2)
            return True
        except Exception:
            return False

    def _case_exists(self, cn):
        """Is this case already in the local DB? (for --skip-existing)"""
        try:
            with sqlite3.connect(str(DB_PATH)) as db:
                return db.execute("SELECT 1 FROM cases WHERE case_number=?", [cn]).fetchone() is not None
        except Exception:
            return False

    async def process_one_case(self, case_info):
        """
        The core flow:
        1. Click into the case detail page
        2. Read full docket text (address, debt, court, attorney, events)
        3. Save docket to file
        4. Run Claude extraction
        5. Generate acquisition memo
        6. Save to database
        """
        cn = case_info["caseNumber"]
        party = case_info.get("partyName", "")
        owner = classify(party)

        # Skip only if processed today AND data is complete (bypassed by --force)
        if DB_PATH.exists() and not self.force:
            with sqlite3.connect(str(DB_PATH)) as db:
                row = db.execute(
                    "SELECT last_agent_run, property_address, total_due_filing "
                    "FROM cases WHERE case_number=?",
                    [cn]).fetchone()
                if row and row[0] and row[0][:10] == date.today().isoformat():
                    has_addr = bool(row[1] and str(row[1]).strip())
                    has_debt = bool(row[2] and float(row[2] or 0) > 0)
                    if has_addr and has_debt:
                        self.log("  Already complete today: " + cn)
                        self.stats["skipped"] += 1
                        return False
                    else:
                        self.log("  Re-processing (incomplete): " + cn)

        # Skip businesses if flag set
        if self.skip_biz and owner["type"] == "business":
            self.log("  Skip business: " + cn)
            self.stats["skipped"] += 1
            return False

        self.log("  -> Clicking into " + cn + " | " + owner["type"].upper() +
                 " | " + party)

        # CLICK INTO THE CASE DETAIL PAGE
        clicked = await self.click_into_case(cn, case_info.get("href", ""))
        if not clicked:
            self.log("  Could not open case detail for " + cn)
            self.stats["errors"] += 1
            return False

        # READ FULL DOCKET from case detail page
        docket_text = await self.page.inner_text("body")
        self.log("  Docket: " + str(len(docket_text)) + " chars")

        if len(docket_text) < 500:
            self.log("  Docket too short -- navigation failed")
            self.stats["errors"] += 1
            return False

        # Save docket to file
        case_dir = PDF_DIR / cn
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "docket.txt").write_text(docket_text, encoding="utf-8")

        # BUSINESS PERSONAL PROPERTY suits are out of scope for real-estate acquisition.
        # Detect from the docket Comment field HERE — BEFORE the expensive Claude
        # extraction + PDF download + DCAD/ACT enrichment + memo — and save only a minimal
        # skip-record so future BPP cases never cost API calls we'd only discard.
        if property_type_from_docket(docket_text) == "personal":
            self.log("  ⚑ BUSINESS PERSONAL PROPERTY suit — skip-record only (out of scope; no extraction/enrichment)")
            save_to_db({
                "caseNumber": cn,
                "court": case_info.get("court", ""),
                "filedDate": case_info.get("fileDate") or None,
                "caseStatus": (case_info.get("status") or "OPEN").upper(),
                "defendant": party,
            }, "", owner)
            with sqlite3.connect(str(DB_PATH)) as _db:
                _db.execute(
                    "UPDATE cases SET property_type='personal', case_track='personal_property', "
                    "account_status='n/a', account_note='business personal property — out of scope (skip-record)', "
                    "oos_date=NULL, oos_issued=0 WHERE case_number=?", [cn])
                _db.commit()
            self.stats["processed"] = self.stats.get("processed", 0) + 1
            self.stats["bpp_skipped"] = self.stats.get("bpp_skipped", 0) + 1
            return True

        # Download Original Petition PDF via fresh context
        pdf_text = ""
        petition_href = ""          # persisted via save_to_db below (needs the row to exist first)
        href = case_info.get("href", "")
        try:
            # Identify the ORIGINAL PETITION document. Its link TITLE is often the property type
            # ("- REAL PROPERTY" / "- PERSONAL PROPERTY"), NOT the word "petition" (older Dallas
            # docket format), so matching only 'ORIGINAL PETITION' missed it and the code fell
            # through to "the first document" — which grabbed the JUDGMENT and stored it as the
            # petition (verified on 4 TX-23 cases). Now: match petition markers in the link's
            # event context, EXCLUDE other instruments, and if none qualifies record NONE —
            # never fall through to the wrong document.
            _pet = await self.page.evaluate("""
(function(){
  var BAD=/JUDGMENT|ORDER OF SALE|\\bORDER\\b|ABSTRACT|WRIT|CITATION|AFFIDAVIT|MOTION|REQUEST FOR SERVICE|NON.?JURY|TRIAL|APPOINT|COST OF|CBP/i;
  var PET=/ORIGINAL PETITION|REAL PROPERTY|PERSONAL PROPERTY/i;
  function ctx(el){var t='';var n=el;for(var u=0;u<4&&n;u++){t+=' '+(n.innerText||n.textContent||'');n=n.parentElement;}return t.replace(/\\s+/g,' ');}
  var links=document.querySelectorAll('a');
  for(var i=0;i<links.length;i++){
    if((links[i].innerText||'').trim()!=='View Document')continue;
    var c=ctx(links[i]);
    if(BAD.test(c))continue;
    if(PET.test(c))return {href:links[i].href, why:c.slice(0,120)};
  }
  return {href:null, why:'NO petition-type document found — recording none (not falling through)'};
})()
""")
            petition_href = (_pet or {}).get("href") or ""
            self.log("  petition select: " + ((_pet or {}).get("why") or "none"))
            if petition_href:
                # persisted by save_to_db (via extracted below), not here — the row doesn't
                # exist yet, so an UPDATE would be a no-op (that lost petition_href for
                # ~81/112 first-scrape cases; fixed by routing through save_to_db).
                self.log("  Downloading Original Petition...")
                # Fetch the PDF bytes directly over the authenticated session
                # instead of waiting for a browser 'download' event. The portal
                # serves many petitions INLINE (viewer), so no download fires and
                # expect_download() just times out after 30s. A raw GET returns
                # the bytes regardless of Content-Disposition.
                pdf_path = case_dir / "petition.pdf"
                for pdf_try in range(2):
                    try:
                        resp = await self.page.context.request.get(petition_href, timeout=45000)
                        if not resp.ok:
                            self.log("  PDF fetch HTTP " + str(resp.status) +
                                     (" — retrying" if pdf_try == 0 else ""))
                            continue
                        body = await resp.body()
                        if body[:4] != b"%PDF":
                            self.log("  PDF fetch returned non-PDF (" + str(len(body)) +
                                     " bytes, likely a viewer page)" +
                                     (" — retrying" if pdf_try == 0 else ""))
                            continue
                        pdf_path.write_bytes(body)
                        import pypdf
                        reader = pypdf.PdfReader(str(pdf_path))
                        pages = [p.extract_text() for p in reader.pages if p.extract_text()]
                        pdf_text = "\n".join(pages)
                        self.log("  PDF saved (" + str(len(body)) + " bytes) | text " +
                                 str(len(pdf_text)) + " chars")
                        break
                    except Exception as pe:
                        self.log("  PDF error: " + str(pe) +
                                 (" — retrying" if pdf_try == 0 else ""))
        except Exception as e:
            self.log("  PDF error: " + str(e))

        # The docket is chronological (oldest first), so the OUTCOME events we
        # need — judgment, Order of Sale, sale/auction date, dismissal — sit at
        # the END of a long docket and were being dropped by front-truncation.
        # Always surface the outcome-signal lines (with a little context) so they
        # survive regardless of docket length, in addition to the head + PDF.
        outcome_re = re.compile(
            r"ORDER OF SALE|ORDER OF SALE ISSUED|SALE DATE|FORECLOSURE SALE|"
            r"JUDGMENT|NON-SUIT|DISMISS|ABSTRACT OF JUDGMENT|WRIT|SHERIFF|"
            r"CONSTABLE|SET FOR SALE|NOTICE OF SALE", re.I)
        dlines = docket_text.splitlines()
        key_lines = []
        for i, l in enumerate(dlines):
            if outcome_re.search(l):
                # keep the line and its immediate neighbours (dates live adjacent)
                key_lines.extend(dlines[max(0, i - 1):i + 2])
        # de-dup preserving order
        seen = set(); key_block = []
        for l in key_lines:
            if l not in seen:
                seen.add(l); key_block.append(l)
        key_block = "\n".join(key_block)[:3500]

        full_text = docket_text[:4000]
        if key_block:
            full_text += "\n\nKEY DOCKET OUTCOME EVENTS (judgment / order of sale / sale date / dismissal):\n" + key_block
        if pdf_text:
            full_text = full_text + "\n\nORIGINAL PETITION PDF:\n" + pdf_text[:12000]

        # Claude extraction
        self.log("  Running Claude extraction...")
        extracted = await claude_extract(full_text)
        if not extracted:
            self.log("  Extraction failed for " + cn)
            self.stats["errors"] += 1
            return False

        extracted["caseNumber"] = cn

        # Fallback: if Claude missed the debt, parse PDF text directly
        # pypdf removes spaces so "TOTAL DUE AS OF" becomes "TOTALDUEASOF"
        pdf_text_nospace = pdf_text.replace(" ", "").upper() if pdf_text else ""
        if not extracted.get("totalDueAtFiling") and pdf_text:
            import re as _re
            # Pattern: "TOTAL DUE AS OF [MONTH], [YEAR]\nTOTAL\t$X,XXX.XX\t$X,XXX.XX\t$XX,XXX.XX"
            # or "TOTAL\s+\$X,XXX.XX\s+\$X,XXX.XX\s+\$XX,XXX.XX"
            # Try normal spaced text first
            total_m = _re.search(
                r'TOTAL DUE AS OF[^\$]+\$([\d,]+\.\d{2})\s*$',
                pdf_text, _re.IGNORECASE | _re.MULTILINE
            )
            if not total_m:
                # pypdf strips spaces: "TOTALDUEASOFMAY,2026TOTAL $11,405.88$6,247.51$17,653.39"
                # Find the LAST dollar amount after TOTALDUEASOF
                nospace = pdf_text.replace(" ", "")
                ns_idx = nospace.upper().find("TOTALDUEASOF")
                if ns_idx >= 0:
                    segment = nospace[ns_idx:]
                    amounts = _re.findall(r'\$([\d,]+\.\d{2})', segment)
                    if amounts:
                        # Last amount is the grand total
                        extracted["totalDueAtFiling"] = float(amounts[-1].replace(",",""))
                        self.log("  Debt from PDF (no-space): $" + "{:,.2f}".format(extracted["totalDueAtFiling"]))
            if not extracted.get("totalDueAtFiling") and total_m:
                extracted["totalDueAtFiling"] = float(total_m.group(1).replace(",",""))
                self.log("  Debt from PDF fallback: $" + "{:,.2f}".format(extracted["totalDueAtFiling"]))

        # Save to database first (without memo). Persist the petition URL captured at
        # download time so the "Petition PDF" button works (it was being lost to a
        # pre-insert UPDATE that no-op'd for first-scrape cases).
        if petition_href:
            extracted["petitionHref"] = petition_href
        save_to_db(extracted, "", owner)

        addr = extracted.get("propertyAddress", "no address extracted")
        debt = extracted.get("totalDueAtFiling", 0)

        # Property Intel enrichment — DCAD + Dallas ACT FIRST
        intel = None
        raw_acct = extracted.get("accountNumber", "")
        valid_accts = valid_dcad_accounts(raw_acct)
        acct = ", ".join(valid_accts)
        acct_status = "resolved" if valid_accts else None   # set by the resolver branches below
        acct_note = None
        if not valid_accts:
            # The petition account was missing or garbled. Try to RESOLVE the real
            # DCAD account from the address/owner — but ONLY auto-assign when a second
            # independent signal corroborates it (the n=92 audit showed address search
            # alone returns a confidently-wrong parcel ~2% of the time). An
            # uncorroborated candidate is noted but NEVER written.
            extra = ""
            try:
                extra = " ".join(x.get("name", "") for x in (extracted.get("allDefendants") or []))
            except Exception:
                extra = ""
            try:
                from property_intel import resolve_account_corroborated, corroboration_strength
                resolved, conf, why = await resolve_account_corroborated(
                    extracted.get("propertyAddress", ""),
                    extracted.get("defendant", ""), self.browser, extra_names=extra)
            except Exception as re_err:
                resolved, conf, why = "", "unresolved", "error:" + str(re_err)[:40]
            cand = (" (uncorroborated candidate " + resolved + ")") if (resolved and conf != "corroborated") else ""
            if resolved and conf == "corroborated":
                self.log("  ↳ Resolved DCAD account (" + why + "): " + resolved +
                         " (petition account was " + (repr(str(raw_acct).strip()) or "empty") + ")")
                extracted["accountNumber"] = resolved
                valid_accts = [resolved]
                acct = resolved
                acct_status = "resolved"
                acct_note = "auto-resolved [" + corroboration_strength(why) + "]: " + why
                self.stats["resolved_account"] = self.stats.get("resolved_account", 0) + 1
                try:
                    with sqlite3.connect(str(DB_PATH)) as _db:
                        _db.execute("UPDATE cases SET account_number=? WHERE case_number=?", [resolved, cn]); _db.commit()
                except Exception:
                    pass
            elif str(raw_acct).strip():
                self.log("  ⚠ Account '" + str(raw_acct).strip() + "' invalid; no corroborated DCAD match"
                         + cand + " — needs manual lookup.")
                self.stats["invalid_account"] += 1
                acct_status = "invalid"
                acct_note = "still needs manual lookup — " + why + cand
            else:
                self.log("  ⚠ No account extracted; no corroborated DCAD match" + cand + " — needs manual lookup.")
                self.stats["no_account"] += 1
                acct_status = "needs_lookup"
                acct_note = "no corroborated DCAD match — " + why + cand

        # Persist the account-resolution state + reason so a flagged case becomes a
        # VISIBLE, queryable backlog item (sidebar filter) instead of a lost log line.
        try:
            with sqlite3.connect(str(DB_PATH)) as _db:
                _db.execute("UPDATE cases SET account_status=?, account_note=? WHERE case_number=?",
                            [acct_status, acct_note, cn]); _db.commit()
        except Exception:
            pass
        if valid_accts:
            try:
                from property_intel import enrich_property
                self.log("  Enriching property intel: " + acct)
                intel = await enrich_property(acct, addr, self.browser)
                # A real $0 balance (taxes paid) is DATA, not "no data" — gate on
                # `is not None`, not falsy, so a paid-up property isn't dropped.
                if intel and (intel.get("market_value") is not None or intel.get("current_tax_balance") is not None):
                    with sqlite3.connect(str(DB_PATH)) as db:
                        db.execute(
                            "UPDATE cases SET property_intel=? WHERE case_number=?",
                            [json.dumps(intel), cn]
                        )
                        db.commit()
                    mvv = intel.get("market_value"); balv = intel.get("current_tax_balance")
                    mv_s = "unknown" if mvv is None else "${:,.0f}".format(mvv)
                    bal_s = "unknown" if balv is None else "${:,.2f}".format(balv)
                    self.log("  Intel saved: MV " + mv_s + " | Balance " + bal_s)
                    extracted["_property_intel"] = json.dumps(intel)
                else:
                    self.log("  Intel: no data returned")
                    intel = None
            except Exception as ie:
                self.log("  Intel error: " + str(ie))
                intel = None

        # Property type (real vs business-personal-property) from the docket Comment field.
        # BPP suits are a different instrument — detect at scrape time so they never
        # accumulate into the real-estate timing/equity math again.
        _ptype = property_type_from_docket(docket_text)
        # Belt-and-suspenders: a BPP suit must never carry a real-estate oos_date. Clear
        # it BEFORE it can reach the timing track (writ-of-execution != Order of Sale).
        if _ptype == "personal":
            try:
                with sqlite3.connect(str(DB_PATH)) as _db:
                    _db.execute("UPDATE cases SET oos_date=NULL, oos_issued=0 WHERE case_number=?", [cn]); _db.commit()
            except Exception:
                pass

        # Tag the dataset/track (keeps OOS-timing vs dismissed-owing leads from blending;
        # personal_property is classified FIRST).
        try:
            _bal = intel.get("current_tax_balance") if intel else None
            _oos = None if _ptype == "personal" else extracted.get("orderOfSaleDate")
            _track = case_track_of(_ptype, _oos,
                                   extracted.get("judgmentType"),
                                   extracted.get("judgmentDate"), _bal)
            # UNDETERMINED property type (no docket Comment field — the 'no docket' edge
            # cases): do NOT silently default to real property. Tag 'unknown' so it is a
            # visible, queryable manual-review state; sync_to_prod holds 'unknown' back from
            # prod (same as 'personal') until a human confirms real vs personal.
            _ptype_store = _ptype if _ptype else "unknown"
            with sqlite3.connect(str(DB_PATH)) as _db:
                _db.execute("UPDATE cases SET case_track=?, property_type=? WHERE case_number=?",
                            [_track, _ptype_store, cn]); _db.commit()
            if _ptype is None:
                self.log("  ⚑ property type UNDETERMINED (no docket Comment) — tagged 'unknown' for "
                         "manual review; held from prod sync (not silently treated as real)")
            else:
                self.log("  Track: " + _track + " [property_type=" + _ptype_store + "]")
        except Exception:
            pass

        # Fix tax breakdown: parse entity totals directly from PDF (no-space format)
        if pdf_text and (not extracted.get("taxBreakdown") or
                         all(e.get("taxAmt",0)==0 for e in extracted.get("taxBreakdown",[]))):
            import re as _re2
            nospace_pdf = pdf_text.replace(" ","")
            entities = [
                ("DALLASCOUNTY", "Dallas County"),
                ("PARKLANDHOSPITALDISTRICT", "Parkland Hospital District"),
                ("DALLASCOLLEGE", "Dallas College"),
                ("DALLASCOUNTYSCHOOLEQUALIZATIONFUND", "Dallas County School Equalization Fund"),
                ("DALLASINDEPENDENTSCHOOLDISTRICT", "Dallas Independent School District"),
                ("CITYOFDALLAS", "City of Dallas"),
            ]
            breakdown = []
            for key, label in entities:
                idx = nospace_pdf.upper().find(key + "ACCT")
                if idx < 0:
                    idx = nospace_pdf.upper().find(key)
                if idx < 0:
                    continue
                segment = nospace_pdf[idx:idx+500]
                # Find TOTAL $X$Y$Z pattern
                total_m = _re2.search(r'TOTAL\s*\$([\d,]+\.\d{2})\$([\d,]+\.\d{2})\$([\d,]+\.\d{2})', segment)
                if total_m:
                    breakdown.append({
                        "entity": label,
                        "taxAmt": float(total_m.group(1).replace(",","")),
                        "penaltyInterest": float(total_m.group(2).replace(",","")),
                        "total": float(total_m.group(3).replace(",",""))
                    })
            if breakdown:
                extracted["taxBreakdown"] = breakdown
                self.log("  Tax breakdown: " + str(len(breakdown)) + " entities parsed from PDF")
                with sqlite3.connect(str(DB_PATH)) as db:
                    db.execute("UPDATE cases SET tax_breakdown=? WHERE case_number=?",
                               [json.dumps(breakdown), cn])
                    db.commit()

        # Generate acquisition memo AFTER intel — so it uses real values
        self.log("  Generating memo...")
        memo = await claude_memo(extracted, owner, intel)

        # Update DB with memo
        with sqlite3.connect(str(DB_PATH)) as db:
            db.execute("UPDATE cases SET ai_memo=? WHERE case_number=?", [memo, cn])
            db.commit()

        self.log("  Saved " + cn + " | " + addr + " | $" + "{:,.0f}".format(debt))

        # Append-only raw-capture archive → durable object storage. No-op unless
        # the ARCHIVE_* env vars are set (then the raw source corpus is preserved
        # off-laptop and the big local PDF is pruned). See archive.py.
        try:
            import archive
            if archive.archiving_enabled():
                from datetime import datetime as _dt
                ts = _dt.now().strftime("%Y%m%dT%H%M%SZ")
                files = {"petition.pdf": str(case_dir / "petition.pdf"),
                         "docket.txt": str(case_dir / "docket.txt")}
                blobs = {"extracted.json": (json.dumps(extracted), "application/json")}
                if intel:
                    blobs["property_intel.json"] = (json.dumps(intel), "application/json")
                keys = archive.archive_case(cn, ts, files=files, blobs=blobs)
                self.log("  ↑ archived " + str(len(keys)) + " raw artifacts (durable storage)")
                pdf = case_dir / "petition.pdf"           # prune only after a successful upload
                if keys and pdf.exists():
                    pdf.unlink()
        except Exception as ae:
            self.log("  archive note (kept local): " + str(ae)[:80])

        self.stats["processed"] += 1
        return True

    async def run(self, args):
        await self.start()
        try:
            if args.case:
                # Single case numbers -- search directly
                for i, cn in enumerate(args.case):
                    self.log("Case " + str(i+1) + "/" + str(len(args.case)) +
                             ": " + cn)
                    await self.go_to_portal_and_search(cn)
                    rows = await self.get_case_list_from_current_page()
                    match = next((r for r in rows if r["caseNumber"] == cn), None)
                    if match:
                        await self.process_one_case(match)
                    else:
                        self.log("Case not found: " + cn)
                    await asyncio.sleep(2)

            elif args.name:
                await self.go_to_portal_and_search(args.name)
                rows = await self.get_case_list_from_current_page()
                self.stats["found"] = len(rows)
                # Same root cause as the pattern path: count closed-excluded so Found reconciles.
                # skip_biz=False here — businesses are filtered (and counted) per-case in
                # process_one_case on this path, not at the page level.
                part = partition_page(rows, self.open_only, skip_biz=False)
                targets = part["targets"]
                if part["closed"]:
                    self.stats["closed_skipped"] += part["closed"]
                    self.log("  (" + str(part["closed"]) +
                             " CLOSED cases excluded (--open-only mode))")
                self.log("Found " + str(len(rows)) + " | Processing " + str(len(targets)))
                for i, case in enumerate(targets):
                    self.log("[" + str(i+1) + "/" + str(len(targets)) + "] " +
                             case["caseNumber"] + " | " + case.get("partyName", ""))
                    await self.process_one_case(case)
                    if i < len(targets) - 1:
                        await self.back_to_search_results()
                        await asyncio.sleep(1)

            elif args.pattern:
                pattern = pad_pattern(args.pattern)
                self.log("Pattern: '" + args.pattern + "' -> '" + pattern + "'")

                # Process page by page — saves immediately, stoppable anytime
                await self.go_to_portal_and_search(pattern)
                total_found = 0
                page_num = 1
                prev_first_cn = ""
                hit_limit = False

                while True:
                    page_rows = await self.get_case_list_from_current_page()
                    if not page_rows:
                        self.log("Page " + str(page_num) + ": no rows")
                        break
                    first_cn = page_rows[0]["caseNumber"]
                    if first_cn == prev_first_cn:
                        self.log("Pagination stalled — done")
                        break
                    prev_first_cn = first_cn
                    total_found += len(page_rows)
                    self.stats["found"] = total_found

                    # DEFAULT takes every row (judgment/OOS/dismissed CLOSED cases included — the
                    # moat). --open-only narrows to OPEN (or blank-status) rows for fast lead-gen.
                    # Both filters COUNT their drops (partition_page) so the Found total reconciles
                    # instead of silently shedding closed cases.
                    part = partition_page(page_rows, self.open_only, self.skip_biz)
                    targets = part["targets"]
                    if part["closed"]:
                        self.stats["closed_skipped"] += part["closed"]
                        self.log("  (" + str(part["closed"]) +
                                 " CLOSED cases excluded (--open-only mode))")
                    if part["business"]:
                        self.stats["skipped"] += part["business"]
                        self.log("  (" + str(part["business"]) +
                                 " business entities excluded by --individuals-only)")

                    self.log("")
                    self.log("Page " + str(page_num) + ": " +
                             str(len(page_rows)) + " cases | " +
                             str(len(targets)) + " to process")

                    if os.environ.get("SCRAPE_DEBUG"):
                        try:
                            pager = await self.page.evaluate(
                                "(function(){var o=[];"
                                "var els=document.querySelectorAll('a,button,span,li');"
                                "for(var i=0;i<els.length;i++){var el=els[i];"
                                "var c=(el.className||'')+'';var a=(el.getAttribute('aria-label')||'');"
                                "var t=(el.getAttribute('title')||'');"
                                "if(/pager|k-i-|k-link|next|last|arrow|page/i.test(c+' '+a+' '+t)){"
                                "o.push({tag:el.tagName,cls:c,aria:a,title:t,"
                                "txt:(el.innerText||'').trim().slice(0,15),vis:el.offsetParent!==null});}}"
                                "return JSON.stringify(o);})()")
                            open("/tmp/pager_debug.json", "w").write(pager or "[]")
                            self.log("  DEBUG pager elements -> /tmp/pager_debug.json (" +
                                     str(len(pager or "")) + " chars)")
                        except Exception as pe:
                            self.log("  DEBUG pager dump failed: " + str(pe))

                    for i, case in enumerate(targets):
                        cn = case["caseNumber"]
                        if args.limit and self.stats["processed"] >= args.limit:
                            self.log("Reached --limit " + str(args.limit) + " — stopping.")
                            hit_limit = True
                            break
                        if args.skip_existing and self._case_exists(cn):
                            self.stats["skipped"] += 1
                            self.log("  skip (already in DB): " + cn)
                            continue
                        o = classify(case["partyName"])
                        tag = ("[IND]" if o["type"]=="individual"
                               else "[EST]" if o["type"]=="estate" else "[BIZ]")
                        self.log("[pg" + str(page_num) + " " +
                                 str(i+1) + "/" + str(len(targets)) + "] " +
                                 tag + " " + cn +
                                 " | " + case.get("partyName",""))
                        await self.process_one_case(case)
                        await asyncio.sleep(3)

                    if hit_limit:
                        break

                    # Return to the results grid before paginating: processing a
                    # page's cases navigates into case-detail pages, so the pager
                    # isn't present unless we go back to the Search Results tab
                    # first. (A repeated first case number trips the stall guard
                    # above, so this can't loop even if the grid reset a page.)
                    await self.back_to_search_results()
                    await asyncio.sleep(1)

                    next_js = (
                        "(function(){"
                        "var els=document.querySelectorAll('a,button,span,li');"
                        "for(var i=0;i<els.length;i++){"
                        "var el=els[i];"
                        "if(el.offsetParent===null)continue;"
                        "var aria=(el.getAttribute('aria-label')||'').toLowerCase();"
                        "var title=(el.getAttribute('title')||'').toLowerCase();"
                        "var cls=(el.className||'').toLowerCase();"
                        "var isNext=(aria.indexOf('next page')>=0||"
                        "title.indexOf('next page')>=0||"
                        "cls.indexOf('k-i-arrow-60-right')>=0||"
                        "(cls.indexOf('k-next')>=0&&cls.indexOf('k-last')<0));"
                        "var isLast=(aria.indexOf('last')>=0||"
                        "title.indexOf('last')>=0||cls.indexOf('k-last')>=0);"
                        "if(isNext&&!isLast){el.click();return true;}"
                        "}return false;"
                        "})()")
                    try:
                        moved = await self.page.evaluate(next_js)
                        if not moved:
                            self.log("No Next button — all pages done")
                            break
                        await asyncio.sleep(3)
                        page_num += 1
                        if page_num > 30:
                            break
                    except Exception as ne:
                        self.log("Next page error: " + str(ne))
                        break

                self.stats["found"] = total_found


        finally:
            self.log("")
            self.log("=" * 55)
            self.log("COMPLETE")
            found = self.stats["found"]
            proc = self.stats["processed"]
            skip = self.stats["skipped"]
            closed = self.stats["closed_skipped"]
            errs = self.stats["errors"]
            self.log("  Found:     " + str(found))
            self.log("  Processed: " + str(proc) +
                     (" (of which BPP skip-records: " + str(self.stats["bpp_skipped"]) + ")"
                      if self.stats.get("bpp_skipped") else ""))
            self.log("  Skipped:   " + str(skip) + " (business / already-complete)")
            self.log("  Closed:    " + str(closed) + " (excluded — default open-only)")
            self.log("  Errors:    " + str(errs))
            # Stats must add up (project standard): Found = Processed + Skipped + Closed + Errors.
            # Only meaningful for list searches (pattern/name) where `found` is the grid total.
            accounted = proc + skip + closed + errs
            if found:
                ok = "OK" if accounted == found else "MISMATCH — investigate"
                self.log("  reconcile: " + str(proc) + "+" + str(skip) + "+" + str(closed) +
                         "+" + str(errs) + " = " + str(accounted) + " vs Found " + str(found) +
                         "  " + ok)
            # Machine-readable line the worker/UI parse (decoupled from the human log format).
            print("SCRAPE_SUMMARY " + json.dumps({
                "found": found, "processed": proc, "skipped": skip,
                "closed": closed, "errors": errs,
                "bpp": self.stats.get("bpp_skipped", 0)}))
            if self.stats.get("resolved_account"):
                self.log("  ✔ DCAD account resolved from address/owner: " + str(self.stats["resolved_account"]))
            flagged = self.stats["invalid_account"] + self.stats["no_account"]
            if flagged:
                self.log("  ⚠ Account needs manual lookup: " + str(flagged) +
                         " (" + str(self.stats["invalid_account"]) + " invalid, " +
                         str(self.stats["no_account"]) + " missing)")
            self.log("=" * 55)
            self.log("Go to taxforeclosureanalyzer.com and click Sync")
            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass


async def main():
    parser = argparse.ArgumentParser(description="PC Peak Discovery Engine v7")
    parser.add_argument("--pattern",
                        help="Case pattern: TX-26-00 searches TX-26-00***")
    parser.add_argument("--name",
                        help="Defendant name: 'JONES, PEARLY'")
    parser.add_argument("--case",
                        nargs="+",
                        help="Exact case number(s): TX-26-00009")
    parser.add_argument("--open-only",
                        action="store_true",
                        help="Lead-gen mode: scrape OPEN cases only, EXCLUDING closed. Faster/"
                             "cheaper, but SKIPS the moat (OOS-issued + dismissed-owing are CLOSED). "
                             "Default now INCLUDES closed — narrowing is a deliberate opt-in.")
    parser.add_argument("--include-closed",
                        action="store_true",
                        help="(Deprecated no-op — closed cases are INCLUDED by default now; "
                             "kept for backward compatibility.)")
    parser.add_argument("--individuals-only",
                        action="store_true",
                        help="Skip business entities (individual owners only)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after processing this many cases (0 = no limit)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip cases already present in the local DB")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape even cases marked 'complete today' (re-download + "
                             "re-extract; use when stored data may be from the wrong document)")
    args = parser.parse_args()

    if not any([args.pattern, args.name, args.case]):
        parser.print_help()
        return
    if not ANTHROPIC_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        return
    if not TWO_CAPTCHA_KEY:
        print("⚠  WARNING: TWO_CAPTCHA_KEY not set — reCAPTCHA auto-solve is DISABLED; the "
              "portal search will likely fail. Set it in Anthropic_API_KEY.env / .env or export it.")
    else:
        print("✓ TWO_CAPTCHA_KEY loaded (len %d, src %s)" % (len(TWO_CAPTCHA_KEY), _ENV_FILE or "shell env"))

    # DISCOVERY DEFAULT (design principle): capture the full picture — INCLUDE closed cases —
    # unless the caller deliberately narrows with --open-only. Closed cases are the mission
    # (OOS-timing ledger + dismissed-owing inventory); the gate must never silently exclude them.
    await Discoverer(
        open_only=args.open_only,
        skip_biz=args.individuals_only,
        force=args.force
    ).run(args)


if __name__ == "__main__":
    asyncio.run(main())
