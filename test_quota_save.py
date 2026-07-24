#!/usr/bin/env python3
"""Browser test for the localStorage quota fix. save() must NEVER throw (it slims, then degrades), and
assignRep must persist the rep POST even when the cache write is under quota pressure — because a
throwing save() used to abort assignRep before its POST (a second silent-persistence cause)."""
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

# Force localStorage.setItem to throw QuotaExceededError for the FULL payload (contains property_intel),
# but succeed for a slim one — so we exercise the real fallback path deterministically.
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
// Simulate a full quota: reject any payload that still carries the heavy property_intel marker.
const _set = Storage.prototype.setItem;
Storage.prototype.setItem = function(k, v){
  if (k === 'tfi_v5' && v.indexOf('QUOTA_MARK') !== -1) {
    const err = new Error('quota'); err.name = 'QuotaExceededError'; throw err;
  }
  return _set.call(this, k, v);
};
""" % json.dumps(PLATFORM)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(STUB)
    pg.goto(HTML.as_uri())
    pg.wait_for_timeout(500)

    # Put a case with a heavy property_intel blob into memory, then save().
    setup = pg.evaluate("""() => {
        // valid-JSON property_intel (caseTrack parses it) carrying the heavy QUOTA_MARK padding
        const pi = JSON.stringify({ current_tax_balance: 5000, _pad: 'QUOTA_MARK' + 'x'.repeat(5000) });
        cases = [{ id:'TX-26-01192_platform', city:'dallas', rep_assigned:'Tim Summers', memo:'m',
                   property_intel: pi,
                   extracted:{ caseNumber:'TX-26-01192', propertyAddress:'1909 Leroy Rd.', defendant:'Debra Myers' } }];
        // No open case — so the slim path drops this heavy blob (it retains ONLY the OPEN case's
        // property_intel; that retention is covered by test_intel_reload). Here we exercise the
        // general slimming: the heavy blob must be dropped so the mirror fits.
        activeId = null;
        try { save(); return {threw:false}; } catch(e){ return {threw:true, err:String(e)}; }
    }""")
    check("save() does NOT throw when the full payload exceeds quota", setup.get("threw") is False)

    stored = pg.evaluate("""() => {
        const raw = localStorage.getItem('tfi_v5') || '';
        return { has: !!raw, hasBlob: raw.indexOf('QUOTA_MARK') !== -1,
                 hasCase: raw.indexOf('TX-26-01192') !== -1, hasRep: raw.indexOf('Tim Summers') !== -1 };
    }""")
    check("a SLIM cache was written (case + rep kept, heavy property_intel dropped)",
          stored["has"] and stored["hasCase"] and stored["hasRep"] and not stored["hasBlob"])

    # assignRep must still POST when save() is under quota pressure (heavy blob present).
    pg.evaluate("() => { window.__posts = []; }")
    assign = pg.evaluate("""() => {
        try { assignRep('TX-26-01192_platform', 'Jay Lewis'); return {threw:false}; }
        catch(e){ return {threw:true, err:String(e)}; }
    }""")
    check("assignRep does NOT throw under quota pressure", assign.get("threw") is False)
    posts = pg.evaluate("() => window.__posts")
    check("assignRep STILL POSTs the rep to /api/cases despite the full cache",
          any(p.get("case_number")=="TX-26-01192" and p.get("rep_assigned")=="Jay Lewis" for p in posts))

    check("ZERO uncaught pageerror (QuotaExceededError no longer escapes)", len(errors) == 0)
    if errors:
        for e in errors: print("   pageerror:", e)
    b.close()

print("-"*56)
t, ok = len(_res), sum(_res)
print(f"{ok}/{t} passed")
sys.exit(0 if ok == t else 1)
