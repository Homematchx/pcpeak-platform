#!/usr/bin/env python3
"""Test that a sync NEVER changes which case is displayed under the user. Reproduces the reported
hazard: a sync starts while viewing case A, its slow event-fetch loop runs, the user navigates to
case B mid-sync, and the sync completes — it must show B (the user's CURRENT case), not snap back to A.
Also: a plain sync while sitting on one case keeps that case; and it still refreshes the open detail."""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
HTML = Path("frontend/index.html").resolve()

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

PLATFORM = [
    {"case_number": "TX-26-00779", "rep_assigned": "", "property_address": "A St", "defendant": "A", "city": "dallas"},
    {"case_number": "TX-26-00782", "rep_assigned": "", "property_address": "B St", "defendant": "B", "city": "dallas"},
]
# Event fetches are DELAYED so we can navigate mid-sync (mirrors the real ~238 slow sequential GETs).
STUB = """
window.__eventDelay = 0;
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/cases') && opts && opts.method==='POST') return J({status:'ok'});
  if (url.endsWith('/api/cases')) return J(%s);
  if (url.indexOf('/api/events/')>=0) { if(window.__eventDelay) await new Promise(r=>setTimeout(r, window.__eventDelay)); return J([]); }
  if (url.endsWith('/api/stats')) return J({total_cases:2});
  if (url.endsWith('/api/reps')) return J([]);
  return J([]);
};
"""% json.dumps(PLATFORM)

def open_cn(pg):
    return pg.evaluate("""() => {
        const c = cases.find(x => x.id === activeId);
        return c && c.extracted ? c.extracted.caseNumber : '(none)';
    }""")

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(STUB)
    pg.goto(HTML.as_uri())
    pg.wait_for_timeout(500)

    # Sit on case A; a plain sync must keep A.
    pg.evaluate("() => { const c = cases.find(x=>x.extracted.caseNumber==='TX-26-00779'); selectCase(c.id); }")
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(400)
    check("plain sync while viewing A keeps A open", open_cn(pg) == "TX-26-00779")

    # THE HAZARD: start a sync while on A (slow events), navigate to B mid-sync, let it finish.
    pg.evaluate("() => { const c = cases.find(x=>x.extracted.caseNumber==='TX-26-00779'); selectCase(c.id); }")
    pg.evaluate("() => { window.__eventDelay = 250; }")   # each event fetch is slow now
    pg.evaluate("() => { window.__syncP = syncFromPlatform(); }")  # start, don't await
    pg.wait_for_timeout(100)                                # sync is mid event-fetch loop
    pg.evaluate("() => { const c = cases.find(x=>x.extracted.caseNumber==='TX-26-00782'); selectCase(c.id); }")  # user navigates to B
    check("user is on B right after navigating mid-sync", open_cn(pg) == "TX-26-00782")
    pg.evaluate("() => window.__syncP")                    # ensure the promise exists
    pg.wait_for_timeout(900)                               # let the slow sync fully complete

    check("after the sync completes, the view is STILL on B (not snapped back to A)",
          open_cn(pg) == "TX-26-00782")

    # And a sync while genuinely idle-on-B still refreshes B (detail present), no switch.
    pg.evaluate("() => { window.__eventDelay = 0; }")
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(400)
    check("sync while on B keeps B (detail refresh, no switch)", open_cn(pg) == "TX-26-00782")

    check("ZERO uncaught pageerror", len(errors) == 0)
    if errors:
        for e in errors: print("   pageerror:", e)
    b.close()

print("-"*56)
t, ok = len(_res), sum(_res)
print(f"{ok}/{t} passed")
sys.exit(0 if ok == t else 1)
