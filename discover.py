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
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
TWO_CAPTCHA_KEY = os.environ.get("TWO_CAPTCHA_KEY", "e6b154c8fad025b44a18d395ba6b1180")

BUSINESS_WORDS = ["LLC","INC","CORP","LTD","TRUST","PROPERTIES","HOLDINGS",
    "INVESTMENTS","GROUP","REALTY","ASSOCIATES","MANAGEMENT","DEVELOPMENT",
    "SERVICES","COMPANY","DBA","FUND","FUNERAL","CHURCH","FOUNDATION"]
ESTATE_WORDS = ["EST OF","ESTATE OF","ESTATE","HEIR","LIFE ESTATE"]

def classify(name):
    n = name.upper()
    if any(w in n for w in ESTATE_WORDS):
        return {"type":"estate","priority":"high","contact":"Contact estate administrator"}
    if any(w in n for w in BUSINESS_WORDS):
        return {"type":"business","priority":"medium","contact":"Formal letter to registered agent"}
    return {"type":"individual","priority":"high","contact":"Door knock first, then direct mail"}

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
Return ONLY valid JSON with NO markdown, NO explanation:
{
"caseNumber":"",
"court":"",
"judicialOfficer":"",
"filedDate":"YYYY-MM-DD",
"caseStatus":"open",
"judgmentDate":"",
"judgmentType":"none",
"defendant":"",
"allDefendants":[
  {
    "name":"",
    "address":"",
    "serviceStatus":"unserved",
    "serviceMethod":"",
    "isInRemOnly":false,
    "notes":""
  }
],
"propertyAddress":"",
"legalDescription":"",
"accountNumber":"",
"lawFirm":"LGBS (Linebarger)",
"plaintiffAttorney":"",
"totalDueAtFiling":0,
"delinquencyYears":[],
"oldestDelinquencyYear":0,
"taxBreakdown":[{"entity":"","taxAmt":0,"penaltyInterest":0,"total":0}],
"defCount":1,
"citationByPostingRequested":false,
"rule106SubstituteService":false,
"priorRelatedSuits":[],
"estateHeirSituation":false,
"continuanceCount":0,
"trialResetCount":0,
"serviceIssues":"",
"nextHearingDate":"",
"orderOfSaleIssued":false,
"orderOfSaleDate":"",
"complexity":"low",
"complexityReason":"",
"keyDocketEvents":[{"date":"YYYY-MM-DD","event":"","type":"filing"}]
}

CRITICAL EXTRACTION RULES:
1. totalDueAtFiling: Find "TOTAL DUE AS OF" on the LAST PAGE of the petition. It is a dollar amount like $17,653.39. Extract the TOTAL number only.
2. allDefendants: List EVERY defendant named in the petition with their address. Find addresses in bold like "Amy Ortega, 1549 Harris Ct., Dallas, TX 75223". Mark IN REM ONLY defendants. Check docket Events for service status (Served/Unserved).
3. propertyAddress: The physical property address from Exhibit A (e.g. "1549 HARRIS CT, DALLAS, TX 75223-3326").
4. accountNumber: The DCAD account number from Exhibit A (e.g. "00000153766000000").
5. defendant: Primary defendant name (first non-IN-REM defendant).
6. complexity: "low" if single defendant served, "medium" if multiple defendants or service issues, "high" if CBP/Rule106/estate/heir situation."""

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
                    json={"model": "claude-sonnet-4-5", "max_tokens": 2000,
                          "system": EXTRACTION_PROMPT,
                          "messages": [{"role": "user",
                                        "content": "Extract:\n\n" + text[:5000]}]})
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

async def claude_memo(extracted, owner):
    if not ANTHROPIC_KEY:
        return ""
    try:
        prompt = (
            "Acquisition memo for PC Peak Development.\n"
            "Owner: " + owner["type"] + " -- " + owner["contact"] + "\n"
            "Write 3 paragraphs: (1) owner situation and debt, "
            "(2) timeline vs benchmarks TX-23-00042 HIGH 37mo->J 89d->OOS "
            "and TX-25-00492 LOW 14mo->J, "
            "(3) acquisition strategy with specific offer range and contact method.\n"
            "Case: " + json.dumps(extracted, default=str)[:1000]
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=30.0)) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY,
                         "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"},
                json={"model": "claude-sonnet-4-5", "max_tokens": 600,
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
        for col in ["owner_type TEXT", "owner_priority TEXT"]:
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
            "legal_description": extracted.get("legalDescription", ""),
            "property_address": extracted.get("propertyAddress", ""),
            "account_number": extracted.get("accountNumber", ""),
            "law_firm": extracted.get("lawFirm", "LGBS"),
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
    def __init__(self, open_only=True, skip_biz=False):
        self.page = None
        self.browser = None
        self.open_only = open_only
        self.skip_biz = skip_biz
        self.stats = {"found": 0, "processed": 0, "skipped": 0, "errors": 0}

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
                        return d2["request"]
                    if d2.get("request") not in ("CAPCHA_NOT_READY",):
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
        """Navigate to portal, handle CAPTCHA, fill search, submit."""
        self.log("Searching portal: '" + query + "'")
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
        self.log("Submit: " + str(sub))
        await asyncio.sleep(4)

        # Wait for results table
        for sel in ["table tbody tr", "tr", "td"]:
            try:
                await self.page.wait_for_selector(sel, timeout=12000)
                self.log("Table ready")
                break
            except Exception:
                continue
        await asyncio.sleep(2)

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
            # Debug: show first 2 hrefs
            for cn, href in list(hrefs.items())[:2]:
                self.log("  HREF[" + cn + "]: " + str(href)[:80])
            for row in rows:
                row["href"] = hrefs.get(row["caseNumber"], "")
        except Exception:
            pass

        return rows

    async def click_into_case(self, case_number, href=""):
        """
        Navigate to case detail page (Tab 3).
        
        CORRECT FLOW (confirmed from session):
          href → intermediate party page (~1373 chars)
          click case number link → real docket (~2128+ chars with case number in text)
        
        KEY FIX: After PDF download corrupts browser state, we always re-navigate
        from scratch using page.goto(href) before attempting to find the docket.
        We verify success by checking that case_number appears in page text.
        """
        # Navigate to search results first, then click the case number
        # hrefs are all "#" (SPA), so goto(href) doesn't work
        # Instead: click Search Results breadcrumb to return to Tab 2
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

        # Now click through to the actual case detail page
        # We know we're on the right page when case_number appears in body text
        for attempt in range(5):
            try:
                text = await self.page.inner_text("body")
                self.log("  Attempt " + str(attempt+1) + ": " + str(len(text)) + " chars")

                # SUCCESS: case number is visible AND page has case detail content
                if case_number in text and "Events and Hearings" in text:
                    self.log("  Correct docket confirmed: " + case_number)
                    return True

                # On intermediate page — click the case number link
                js = (
                    "(function(){"
                    "var links=document.querySelectorAll('a');"
                    "for(var i=0;i<links.length;i++){"
                    "var t=(links[i].innerText||links[i].textContent||'').trim();"
                    "if(t==='" + case_number + "'){links[i].click();return 'clicked-exact';}"
                    "}"
                    "var cd=document.querySelector('a[href*=CaseDetail]');"
                    "if(cd){cd.click();return 'clicked-detail';}"
                    "return null;"
                    "})()"
                )
                result = await self.page.evaluate(js)
                self.log("  Click result: " + str(result))

                if not result:
                    # Nothing to click — try re-navigating via href
                    if href and href.startswith("http"):
                        self.log("  Re-navigating via href...")
                        await self.page.goto(href, wait_until="domcontentloaded", timeout=20000)
                    else:
                        break
                await asyncio.sleep(3)

            except Exception as e:
                self.log("  Attempt " + str(attempt+1) + " error: " + str(e))
                await asyncio.sleep(2)

        # Final check
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

        # Skip only if processed today AND data is complete
        if DB_PATH.exists():
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

        # Download Original Petition PDF via fresh context
        pdf_text = ""
        href = case_info.get("href", "")
        try:
            petition_href = await self.page.evaluate(
                "(function(){"
                "var links=document.querySelectorAll('a');"
                "for(var i=0;i<links.length;i++){"
                "if((links[i].innerText||'').trim()!=='View Document')continue;"
                "var p=links[i].parentElement||links[i];"
                "var ctx=(p.innerText||p.textContent||'')+'';"
                "if(ctx.indexOf('ORIGINAL PETITION')>=0)return links[i].href;"
                "}"
                "var first=document.querySelector('a[href*=Document]');"
                "return first?first.href:null;"
                "})()")
            if petition_href:
                self.log("  Downloading Original Petition...")
                dl_ctx = await self.browser.new_context(accept_downloads=True)
                dl_page = await dl_ctx.new_page()
                try:
                    async with dl_page.expect_download(timeout=30000) as dl_info:
                        try:
                            await dl_page.goto(petition_href)
                        except Exception:
                            pass  # Download starting error expected
                    dl = await dl_info.value
                    pdf_path = case_dir / "petition.pdf"
                    await dl.save_as(pdf_path)
                    self.log("  PDF saved")
                    import pypdf
                    reader = pypdf.PdfReader(str(pdf_path))
                    pages = [p.extract_text() for p in reader.pages if p.extract_text()]
                    pdf_text = "\n".join(pages)
                    self.log("  PDF text: " + str(len(pdf_text)) + " chars")
                except Exception as pe:
                    self.log("  PDF error: " + str(pe))
                finally:
                    await dl_page.close()
                    await dl_ctx.close()
                # Re-navigate main page back to case docket after PDF download
                if href and href.startswith("http"):
                    try:
                        await self.page.goto(href,
                            wait_until="domcontentloaded", timeout=20000)
                        await asyncio.sleep(2)
                        rejs = (
                            "(function(){"
                            "var links=document.querySelectorAll('a');"
                            "for(var i=0;i<links.length;i++){"
                            "var t=(links[i].innerText||links[i].textContent||'').trim();"
                            "if(t==='" + cn + "'){links[i].click();return true;}"
                            "}return false;"
                            "})()"
                        )
                        await self.page.evaluate(rejs)
                        await asyncio.sleep(2)
                    except Exception:
                        pass
        except Exception as e:
            self.log("  PDF error: " + str(e))

        # Combine docket + petition PDF for Claude
        full_text = docket_text
        if pdf_text:
            full_text = docket_text + "\n\nORIGINAL PETITION PDF:\n" + pdf_text

        # Claude extraction
        self.log("  Running Claude extraction...")
        extracted = await claude_extract(full_text)
        if not extracted:
            self.log("  Extraction failed for " + cn)
            self.stats["errors"] += 1
            return False

        extracted["caseNumber"] = cn

        # Generate acquisition memo
        self.log("  Generating memo...")
        memo = await claude_memo(extracted, owner)

        # Save to database
        save_to_db(extracted, memo, owner)

        addr = extracted.get("propertyAddress", "no address extracted")
        debt = extracted.get("totalDueAtFiling", 0)
        self.log("  Saved " + cn + " | " + addr + " | $" + "{:,.0f}".format(debt))
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
                targets = ([r for r in rows
                            if "OPEN" in r.get("status", "").upper()]
                           if self.open_only else rows)
                self.log("Found " + str(len(rows)) + " | Processing " +
                         str(len(targets)) + " OPEN")
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

                    targets = [r for r in page_rows
                               if "OPEN" in r.get("status","").upper()
                               or r.get("status","") == ""]
                    if self.skip_biz:
                        targets = [r for r in targets
                                   if classify(r["partyName"])["type"] != "business"]

                    self.log("")
                    self.log("Page " + str(page_num) + ": " +
                             str(len(page_rows)) + " cases | " +
                             str(len(targets)) + " to process")

                    for i, case in enumerate(targets):
                        o = classify(case["partyName"])
                        tag = ("[IND]" if o["type"]=="individual"
                               else "[EST]" if o["type"]=="estate" else "[BIZ]")
                        self.log("[pg" + str(page_num) + " " +
                                 str(i+1) + "/" + str(len(targets)) + "] " +
                                 tag + " " + case["caseNumber"] +
                                 " | " + case.get("partyName",""))
                        await self.process_one_case(case)
                        await asyncio.sleep(3)

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
            self.log("  Found:     " + str(self.stats["found"]))
            self.log("  Processed: " + str(self.stats["processed"]))
            self.log("  Skipped:   " + str(self.stats["skipped"]))
            self.log("  Errors:    " + str(self.stats["errors"]))
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
    parser.add_argument("--include-closed",
                        action="store_true",
                        help="Also process CLOSED cases")
    parser.add_argument("--individuals-only",
                        action="store_true",
                        help="Skip business entities")
    args = parser.parse_args()

    if not any([args.pattern, args.name, args.case]):
        parser.print_help()
        return
    if not ANTHROPIC_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        return

    await Discoverer(
        open_only=not args.include_closed,
        skip_biz=args.individuals_only
    ).run(args)


if __name__ == "__main__":
    asyncio.run(main())
