#!/usr/bin/env python3
"""Browser test for the rep-assignment mismatch fix (syncFromPlatform dedup-by-case_number).

Repro: seed localStorage with TWO entries for the same case (a legacy id carrying a stale rep, and a
_platform id) — the drift that made the sidebar card and the detail panel show different reps and
inflated the case count. Stub the platform to return ONE authoritative case (rep = the correct one),
run syncFromPlatform, and assert: exactly one entry per case_number survives, its rep is the platform's,
the count is deduped, and the card chip and detail dropdown agree. Zero pageerror.
"""
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

# Two duplicate localStorage entries for TX-24-00073: a stale legacy one (Jay Lewis) and a _platform
# one (Jocelyn) — plus one unique case, to prove non-duplicates are untouched.
def case_obj(cid, cn, rep, addr="1 St"):
    return {"id": cid, "city": "dallas", "rep_assigned": rep,
            "extracted": {"caseNumber": cn, "propertyAddress": addr, "defendant": "DOE", "defCount": 1}}

SEED = [
    case_obj("leg_998877", "TX-24-00073", "Jay Lewis"),        # stale duplicate
    case_obj("TX-24-00073_platform", "TX-24-00073", "Jocelyn Carter"),  # fresher duplicate
    case_obj("TX-24-00099_platform", "TX-24-00099", "Amy Ng"),  # unique
]

# Platform returns ONE authoritative row per case (the correct reps).
PLATFORM = [
    {"case_number": "TX-24-00073", "rep_assigned": "Jocelyn Carter", "property_address": "2707 Brigham Ln.",
     "defendant": "ROBERTSON, PEARL", "city": "dallas"},
    {"case_number": "TX-24-00099", "rep_assigned": "Amy Ng", "property_address": "9 Rd",
     "defendant": "X", "city": "dallas"},
]

STUB = """
localStorage.setItem('tfi_v5', JSON.stringify(%s));
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/cases') && (!opts || (opts.method||'GET')==='GET')) return J(%s);
  if (url.indexOf('/api/events/') >= 0) return J([]);
  if (url.endsWith('/api/stats')) return J({total_cases: 2});
  if (url.endsWith('/api/reps')) return J([{id:1,name:'Jocelyn Carter',active:1},{id:2,name:'Amy Ng',active:1},{id:3,name:'Jay Lewis',active:1}]);
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
    pg.wait_for_timeout(300)

    # repro precondition (asserted on the seed itself — the auto-sync on page load may already have
    # deduped by the time we could read localStorage, which is itself the fix working):
    _dupnums = [c["extracted"]["caseNumber"] for c in SEED]
    check("seed contains a duplicate case_number (the repro)",
          len(SEED) == 3 and _dupnums.count("TX-24-00073") == 2)

    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(600)

    after = pg.evaluate("""() => {
        const cs = JSON.parse(localStorage.getItem('tfi_v5')||'[]');
        const dup = cs.filter(c => c.extracted && c.extracted.caseNumber === 'TX-24-00073');
        return { total: cs.length, dupCount: dup.length, dupRep: dup.map(c=>c.rep_assigned) };
    }""")
    check("after: exactly ONE entry for the duplicated case (collapsed)", after["dupCount"] == 1)
    check("after: surviving entry carries the PLATFORM rep (Jocelyn Carter)",
          after["dupRep"] == ["Jocelyn Carter"])
    check("after: total is 2 (deduped from 3 — no duplicate, unique case kept)", after["total"] == 2)

    # Now open the case and assert the card chip and the detail dropdown AGREE.
    pg.evaluate("""() => {
        const c = cases.find(x => x.extracted && x.extracted.caseNumber==='TX-24-00073');
        selectCase(c.id);
    }""")
    pg.wait_for_timeout(300)
    card_rep = pg.evaluate("""() => {
        // find the sidebar chip for the case and read its rep pill text
        const chip = Array.from(document.querySelectorAll('.cchip')).find(el => el.textContent.includes('TX-24-00073'));
        return chip ? chip.textContent : '';
    }""")
    detail_sel = pg.evaluate("""() => {
        const sel = document.querySelector('select[id^=\"rep-sel-\"]');
        return sel ? sel.value : '(no select)';
    }""")
    check("card chip shows Jocelyn Carter (not the stale Jay Lewis)",
          "Jocelyn Carter" in card_rep and "Jay Lewis" not in card_rep)
    check("detail dropdown shows Jocelyn Carter", detail_sel == "Jocelyn Carter")
    check("card and detail AGREE (mismatch fixed)",
          ("Jocelyn Carter" in card_rep) and detail_sel == "Jocelyn Carter")

    # Idempotent: a second sync doesn't reintroduce duplicates.
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(500)
    again = pg.evaluate("() => JSON.parse(localStorage.getItem('tfi_v5')||'[]').length")
    check("second sync stays deduped (idempotent, total 2)", again == 2)

    check("ZERO uncaught pageerror", len(errors) == 0)
    if errors:
        for e in errors: print("   pageerror:", e)
    b.close()

print("-"*56)
t, ok = len(_res), sum(_res)
print(f"{ok}/{t} passed")
sys.exit(0 if ok == t else 1)
