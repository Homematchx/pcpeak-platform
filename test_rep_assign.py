#!/usr/bin/env python3
"""Browser test for the assignRep hardening: assigning a rep updates EVERY local copy of the case, so
the sidebar card and the detail panel agree instantly — even in the window before a sync collapses a
duplicate. (Complements test_rep_dedup.py, which covers the sync-time dedup.)"""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

from browser_env import chrome_path
CHROME = chrome_path()   # pinned sandbox path first, else the local playwright cache
HTML = Path("frontend/index.html").resolve()

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

PLATFORM = [{"case_number": "TX-26-01192", "rep_assigned": "", "property_address": "1909 Leroy Rd.",
             "defendant": "Debra Myers", "city": "dallas"}]

STUB = """
window.__posts = [];
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/cases') && opts && opts.method==='POST'){ window.__posts.push(JSON.parse(opts.body)); return J({status:'ok'}); }
  if (url.endsWith('/api/cases')) return J(%s);
  if (url.indexOf('/api/events/')>=0) return J([]);
  if (url.endsWith('/api/stats')) return J({total_cases:1});
  if (url.endsWith('/api/reps')) return J([{id:1,name:'Tim Summers',active:1}]);
  return J([]);
};
""" % json.dumps(PLATFORM)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(STUB)
    pg.goto(HTML.as_uri())
    pg.wait_for_timeout(500)   # auto-sync settles → one platform copy of the case

    # Inject a SECOND (duplicate) copy with a different id and NO rep — the drifted-duplicate state.
    pg.evaluate("""() => {
        const base = cases.find(x => x.extracted && x.extracted.caseNumber==='TX-26-01192');
        const dup = JSON.parse(JSON.stringify(base));
        dup.id = 'legacy_1192'; dup.rep_assigned = '';
        cases.push(dup);
    }""")
    n_copies = pg.evaluate("() => cases.filter(x=>x.extracted&&x.extracted.caseNumber==='TX-26-01192').length")
    check("two copies of the case exist (drifted-duplicate repro)", n_copies == 2)

    # Assign via the platform copy's id.
    pg.evaluate("""() => {
        const c = cases.find(x => x.id==='TX-26-01192_platform');
        assignRep(c.id, 'Tim Summers');
    }""")
    pg.wait_for_timeout(300)

    reps = pg.evaluate("() => cases.filter(x=>x.extracted&&x.extracted.caseNumber==='TX-26-01192').map(x=>x.rep_assigned)")
    check("assignRep updated ALL copies to Tim Summers", reps == ["Tim Summers", "Tim Summers"])
    posted = pg.evaluate("() => window.__posts")
    check("assignRep POSTed the rep to prod (persisted)",
          any(p.get("case_number")=="TX-26-01192" and p.get("rep_assigned")=="Tim Summers" for p in posted))

    # The sidebar card now shows the rep chip (the reported symptom: card was blank).
    card = pg.evaluate("""() => {
        const chip = Array.from(document.querySelectorAll('.cchip')).find(el => el.textContent.includes('TX-26-01192'));
        return chip ? chip.textContent : '';
    }""")
    check("sidebar card shows 'Tim Summers' (was blank before the fix)", "Tim Summers" in card)

    check("ZERO uncaught pageerror", len(errors) == 0)
    if errors:
        for e in errors: print("   pageerror:", e)
    b.close()

print("-"*56)
t, ok = len(_res), sum(_res)
print(f"{ok}/{t} passed")
sys.exit(0 if ok == t else 1)
