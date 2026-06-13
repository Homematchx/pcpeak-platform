"""
PC Peak — Smart Case Discovery Engine
Searches Dallas County portal using multiple strategies:
  1. Year/series batch:  python3 discover.py --year 2026
  2. Defendant name:     python3 discover.py --name "ROGERS, LUCY"
  3. Address:            python3 discover.py --address "1218 Hudspeth"
  4. Account number:     python3 discover.py --account 00000303256000000
  5. Full year sweep:    python3 discover.py --sweep 2026
  6. Custom pattern:     python3 discover.py --pattern "TX-26-00*"

Results are saved to the platform database automatically.
"""

import asyncio, json, os, re, sys, sqlite3, argparse
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "data" / "db" / "pcpeak.db"
PORTAL   = "https://courtsportal.dallascounty.org/DALLASPROD/Home/Dashboard/29"

ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY","")
TWO_CAPTCHA_KEY = os.environ.get("TWO_CAPTCHA_KEY","")

class Discoverer:
    def __init__(self):
        self.page = None
        self.browser = None
        self.found_cases = []

    def log(self, msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    async def start(self):
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        self.browser = await pw.chromium.launch(headless=False,
            args=["--no-sandbox"])
        ctx = await self.browser.new_context(viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
        self.page = await ctx.new_page()

    async def handle_captcha(self):
        has = await self.page.evaluate(
            "()=>!!document.querySelector('iframe[src*=recaptcha],.g-recaptcha,[data-sitekey]')")
        if not has: return
        self.log("CAPTCHA detected — please solve it in the browser")
        input("Press ENTER after solving CAPTCHA...")
        await asyncio.sleep(1)

    async def search(self, query: str, search_type: str = "case") -> list:
        """Search portal and return list of case numbers found."""
        self.log(f"Searching: '{query}' (type: {search_type})")
        
        await self.page.goto(PORTAL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        await self.handle_captcha()

        # Fill search input
        result = await self.page.evaluate(f"""() => {{
            const inputs = [...document.querySelectorAll('input[type=text],input:not([type])')];
            const visible = inputs.filter(i=>i.offsetParent!==null);
            const inp = visible[0] || inputs[0];
            if (!inp) return 'NO_INPUT';
            inp.value = '{query}';
            inp.dispatchEvent(new Event('input',{{bubbles:true}}));
            inp.dispatchEvent(new Event('change',{{bubbles:true}}));
            return 'FILLED:'+inp.id;
        }}""")
        self.log(f"  Fill result: {result}")
        await asyncio.sleep(0.5)

        # Submit
        await self.page.evaluate("""() => {
            const btns=[...document.querySelectorAll('input[type=submit],button[type=submit],input[value=Submit],button')];
            const b=btns.find(x=>(x.value||x.textContent||'').toLowerCase().includes('submit'));
            if(b) b.click();
            else { const f=document.querySelector('form'); if(f) f.submit(); }
        }""")
        await asyncio.sleep(4)

        # Extract all case numbers from results
        page_text = await self.page.inner_text("body")
        
        # Find all TX-XX-XXXXX patterns
        cases_found = list(set(re.findall(r'TX-\d{2}-\d{5}', page_text)))
        
        # Also try to get more details from the results table
        try:
            rows = await self.page.locator("table tbody tr").all()
            for row in rows:
                text = await row.inner_text()
                case_matches = re.findall(r'TX-\d{2}-\d{5}', text)
                cases_found.extend(case_matches)
        except:
            pass
        
        cases_found = list(set(cases_found))
        self.log(f"  Found {len(cases_found)} cases: {cases_found[:10]}")
        return cases_found

    async def sweep_year(self, year: int) -> list:
        """Sweep all cases for a given year using multiple prefix searches."""
        yr = str(year)[2:]  # "2026" -> "26"
        all_cases = []
        
        # Search multiple ranges to get complete coverage
        prefixes = [
            f"TX-{yr}-00",   # 00001-00999
            f"TX-{yr}-01",   # 01000-01999
            f"TX-{yr}-02",   # 02000-02999
            f"TX-{yr}-03",   # 03000-03999
            f"TX-{yr}-04",   # 04000-04999
            f"TX-{yr}-05",   # etc
        ]
        
        self.log(f"Sweeping year {year} with {len(prefixes)} prefix searches...")
        
        for i, prefix in enumerate(prefixes):
            self.log(f"  Search {i+1}/{len(prefixes)}: {prefix}*")
            found = await self.search(prefix + "*")
            new = [c for c in found if c not in all_cases]
            all_cases.extend(new)
            self.log(f"  Running total: {len(all_cases)} unique cases")
            
            if i < len(prefixes) - 1:
                await asyncio.sleep(2)  # polite delay
        
        return list(set(all_cases))

    async def save_to_watchlist(self, case_numbers: list):
        """Save discovered cases to platform database watch list."""
        if not DB_PATH.exists():
            self.log("Database not found — start the backend first")
            return
        
        with sqlite3.connect(DB_PATH) as db:
            # Get existing cases
            existing = {r[0] for r in db.execute("SELECT case_number FROM cases").fetchall()}
            existing_watch = {r[0] for r in db.execute("SELECT case_number FROM watch_list").fetchall()}
            
            new_cases = [c for c in case_numbers if c not in existing and c not in existing_watch]
            
            for cn in new_cases:
                db.execute("INSERT OR IGNORE INTO watch_list (case_number, added_by, notes) VALUES (?,?,?)",
                          [cn, "discovery", f"Auto-discovered {datetime.now().strftime('%Y-%m-%d')}"])
            db.commit()
        
        self.log(f"Saved {len(new_cases)} new cases to watch list")
        self.log(f"({len(case_numbers) - len(new_cases)} already in database)")
        return new_cases

    async def run(self, args):
        await self.start()
        all_found = []

        if args.year:
            all_found = await self.sweep_year(args.year)
        elif args.pattern:
            all_found = await self.search(args.pattern)
        elif args.name:
            # Search by defendant name (Last, First format)
            all_found = await self.search(args.name)
        elif args.address:
            all_found = await self.search(args.address)
        elif args.account:
            all_found = await self.search(args.account)

        if all_found:
            self.log(f"\n{'='*50}")
            self.log(f"DISCOVERY COMPLETE — {len(all_found)} cases found")
            self.log(f"{'='*50}")
            for cn in sorted(all_found):
                self.log(f"  {cn}")
            
            # Save to watch list
            new = await self.save_to_watchlist(all_found)
            
            # Optionally run agent on new cases
            if new and args.analyze:
                self.log(f"\nRunning agent on {len(new)} new cases...")
                if self.browser: await self.browser.close()
                
                # Run agent
                import subprocess
                env = os.environ.copy()
                case_args = " ".join([f"--case"] + new[:20])  # max 20 at once
                subprocess.run(
                    f"python3 agent/agent.py {case_args}",
                    shell=True, env=env, cwd=BASE_DIR
                )
            
            # Save results to file
            results_file = BASE_DIR / f"discovery_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(results_file, 'w') as f:
                json.dump({
                    "discovered_at": datetime.now().isoformat(),
                    "query": vars(args),
                    "total_found": len(all_found),
                    "case_numbers": sorted(all_found)
                }, f, indent=2)
            self.log(f"\nResults saved to: {results_file}")
        else:
            self.log("No cases found matching that search")

        if self.browser:
            await self.browser.close()

async def main():
    parser = argparse.ArgumentParser(
        description="PC Peak — Smart Case Discovery Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Sweep all 2026 cases:
    python3 discover.py --year 2026

  Search by defendant name:
    python3 discover.py --name "ROGERS, LUCY"
    python3 discover.py --name "WILLIAMS"

  Search by address:
    python3 discover.py --address "1218 Hudspeth"
    python3 discover.py --address "Harbor Road"

  Search by case pattern:
    python3 discover.py --pattern "TX-26-00*"
    python3 discover.py --pattern "TX-25-01*"

  Search by DCAD account number:
    python3 discover.py --account 00000303256000000

  Discover AND immediately analyze with AI:
    python3 discover.py --year 2026 --analyze
    python3 discover.py --name "WILLIAMS" --analyze
        """
    )
    parser.add_argument("--year",    type=int, help="Sweep all cases for a year (e.g. 2026)")
    parser.add_argument("--pattern", help="Case number pattern (e.g. TX-26-00*)")
    parser.add_argument("--name",    help="Defendant name search (e.g. 'ROGERS, LUCY')")
    parser.add_argument("--address", help="Property address search")
    parser.add_argument("--account", help="DCAD account number")
    parser.add_argument("--analyze", action="store_true", 
                       help="Auto-run AI agent on newly discovered cases")
    args = parser.parse_args()

    if not any([args.year, args.pattern, args.name, args.address, args.account]):
        parser.print_help()
        return

    d = Discoverer()
    await d.run(args)

if __name__ == "__main__":
    asyncio.run(main())
