#!/usr/bin/env python3
"""Faithful test of the REAL assign flow: open a case, change the detail rep <select> (dispatch the
actual 'change' event that fires onchange=assignRep), and assert the SIDEBAR card shows the rep chip
IMMEDIATELY — no extra sync, no re-click. This is the "see who's assigned at a glance" path."""
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
    {"case_number": "TX-26-01192", "rep_assigned": "", "property_address": "1909 Leroy Rd.", "defendant": "Debra Myers", "city": "dallas"},
    {"case_number": "TX-26-01191", "rep_assigned": "", "property_address": "5728 Johnson Ln", "defendant": "Ernest Weaver", "city": "dallas"},
    {"case_number": "TX-26-01190", "rep_assigned": "", "property_address": "6406 Kemrock Dr.", "defendant": "Luden Osorto", "city": "dallas"},
]
STUB = """
window.__posts = [];
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/cases') && opts && opts.method==='POST'){ window.__posts.push(JSON.parse(opts.body)); return J({status:'ok'}); }
  if (url.endsWith('/api/cases')) return J(%s);
  if (url.indexOf('/api/events/')>=0) return J([]);
  if (url.endsWith('/api/stats')) return J({total_cases:3});
  if (url.endsWith('/api/reps')) return J([{id:1,name:'Tim Summers',active:1}]);
  return J([]);
};
""" % json.dumps(PLATFORM)

def card_text(pg, cn):
    return pg.evaluate("""(cn) => {
        const chip = Array.from(document.querySelectorAll('.cchip')).find(el => el.textContent.includes(cn));
        return chip ? chip.textContent : '(no card)';
    }""", cn)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(STUB)
    pg.goto(HTML.as_uri())
    pg.wait_for_timeout(600)   # auto-sync settles: 3 clean cases, no reps

    check("precondition: sidebar card has NO rep chip before assignment",
          "Tim Summers" not in card_text(pg, "TX-26-01192"))

    # Open the case detail so the rep <select> is rendered.
    pg.evaluate("""() => {
        const c = cases.find(x => x.extracted && x.extracted.caseNumber==='TX-26-01192');
        selectCase(c.id);
    }""")
    pg.wait_for_timeout(200)

    # Drive the REAL dropdown change (the exact user action): set value + dispatch 'change'.
    changed = pg.evaluate("""() => {
        const sel = document.querySelector('select[id^="rep-sel-"]');
        if (!sel) return {ok:false, why:'no select'};
        sel.value = 'Tim Summers';
        sel.dispatchEvent(new Event('change', {bubbles:true}));
        return {ok:true};
    }""")
    check("detail rep <select> exists and change dispatched", changed.get("ok"))
    pg.wait_for_timeout(200)

    # THE ASK: sidebar chip appears immediately, no sync, no re-click.
    check("sidebar card shows 'Tim Summers' IMMEDIATELY after the dropdown change",
          "Tim Summers" in card_text(pg, "TX-26-01192"))
    check("the POST fired (persisted to prod)",
          any(p.get("case_number")=="TX-26-01192" and p.get("rep_assigned")=="Tim Summers"
              for p in pg.evaluate("() => window.__posts")))
    check("other cards are unaffected (no chip)",
          "Tim Summers" not in card_text(pg, "TX-26-01191"))

    # And it survives a subsequent sync (prod now has it / heal preserves it).
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(500)
    check("chip persists after a follow-up sync", "Tim Summers" in card_text(pg, "TX-26-01192"))

    # HARDENING: a case with MALFORMED property_intel must NOT freeze renderList (caseTrack parses it
    # per card). Inject one, re-render, and assign a rep to ANOTHER case — the sidebar must still update.
    pg.evaluate("""() => {
        const bad = { id:'TX-BAD_platform', city:'dallas', rep_assigned:'',
                      property_intel:'{truncated json...',   // malformed on purpose
                      extracted:{ caseNumber:'TX-BAD', propertyAddress:'9 Broken St', defendant:'X' } };
        cases.push(bad);
        renderList();
    }""")
    pg.wait_for_timeout(150)
    check("renderList survives a malformed-property_intel case (list still renders both)",
          "TX-BAD" in card_text(pg, "TX-BAD") and "TX-26-01191" in card_text(pg, "TX-26-01191"))
    pg.evaluate("""() => {
        const c = cases.find(x => x.extracted && x.extracted.caseNumber==='TX-26-01191');
        assignRep(c.id, 'Tim Summers');
    }""")
    pg.wait_for_timeout(150)
    check("assignment still updates the sidebar despite a malformed record present",
          "Tim Summers" in card_text(pg, "TX-26-01191"))

    check("ZERO uncaught pageerror", len(errors) == 0)
    if errors:
        for e in errors: print("   pageerror:", e)
    b.close()

print("-"*56)
t, ok = len(_res), sum(_res)
print(f"{ok}/{t} passed")
sys.exit(0 if ok == t else 1)
