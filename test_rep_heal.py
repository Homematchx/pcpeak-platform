#!/usr/bin/env python3
"""Browser test for the rep silent-save gap fix. Reproduces the exact TX-26-01192 state — a rep set in
local state but NULL on prod — and asserts that syncFromPlatform HEALS it (re-POSTs the rep to
/api/cases) and the rebuilt object shows the rep, instead of the rebuild reverting it to null.
Also asserts assignRep always POSTs on a real change (no no-op-skip of persistence)."""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
HTML = Path("frontend/index.html").resolve()

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

# The stuck state: localStorage has TX-26-01192 with rep=Tim Summers; prod returns it with rep=null.
SEED = [{"id": "TX-26-01192_platform", "city": "dallas", "rep_assigned": "Tim Summers",
         "extracted": {"caseNumber": "TX-26-01192", "propertyAddress": "1909 Leroy Rd.", "defendant": "Debra Myers"}}]
PLATFORM = [{"case_number": "TX-26-01192", "rep_assigned": None, "property_address": "1909 Leroy Rd.",
             "defendant": "Debra Myers", "city": "dallas"}]

STUB = """
localStorage.setItem('tfi_v5', JSON.stringify(%s));
window.__posts = [];
window.__platform = %s;
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/cases') && opts && opts.method==='POST'){
    const b = JSON.parse(opts.body); window.__posts.push(b);
    // simulate prod persisting it (so a follow-up read would reflect it)
    window.__platform.forEach(p => { if(p.case_number===b.case_number && 'rep_assigned' in b) p.rep_assigned=b.rep_assigned; });
    return J({status:'ok'});
  }
  if (url.endsWith('/api/cases')) return J(window.__platform);
  if (url.indexOf('/api/events/')>=0) return J([]);
  if (url.endsWith('/api/stats')) return J({total_cases:1});
  if (url.endsWith('/api/reps')) return J([{id:1,name:'Tim Summers',active:1}]);
  return J([]);
};
""" % (json.dumps(SEED), json.dumps(PLATFORM))

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(STUB)
    pg.goto(HTML.as_uri())
    pg.wait_for_timeout(700)   # auto-sync on load runs the heal

    posts = pg.evaluate("() => window.__posts")
    check("sync HEALED the stuck rep — re-POSTed rep to /api/cases",
          any(p.get("case_number")=="TX-26-01192" and p.get("rep_assigned")=="Tim Summers" for p in posts))
    obj = pg.evaluate("""() => {
        const c = cases.find(x => x.extracted && x.extracted.caseNumber==='TX-26-01192');
        return c ? c.rep_assigned : '(missing)';
    }""")
    check("rebuilt object shows the rep (NOT reverted to null)", obj == "Tim Summers")
    card = pg.evaluate("""() => {
        const chip = Array.from(document.querySelectorAll('.cchip')).find(el => el.textContent.includes('TX-26-01192'));
        return chip ? chip.textContent : '';
    }""")
    check("sidebar card shows Tim Summers after heal", "Tim Summers" in card)

    # A SECOND sync must NOT re-heal (prod now has it) — no duplicate heal POST.
    pg.evaluate("() => { window.__posts = []; }")
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(600)
    posts2 = pg.evaluate("() => window.__posts")
    check("idempotent: second sync does NOT re-heal (prod already has it)", len(posts2) == 0)

    # assignRep always persists on a real change (even repicking after clearing).
    pg.evaluate("() => { window.__posts = []; }")
    pg.evaluate("""() => {
        const c = cases.find(x => x.extracted && x.extracted.caseNumber==='TX-26-01192');
        assignRep(c.id, '');            // clear
        assignRep(c.id, 'Tim Summers'); // re-set
    }""")
    pg.wait_for_timeout(200)
    posts3 = pg.evaluate("() => window.__posts.map(p=>p.rep_assigned)")
    check("assignRep persists BOTH a clear and a re-set (always POSTs on change)",
          posts3 == ["", "Tim Summers"])

    check("ZERO uncaught pageerror", len(errors) == 0)
    if errors:
        for e in errors: print("   pageerror:", e)
    b.close()

print("-"*56)
t, ok = len(_res), sum(_res)
print(f"{ok}/{t} passed")
sys.exit(0 if ok == t else 1)
