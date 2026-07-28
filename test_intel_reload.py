#!/usr/bin/env python3
"""Browser test — property_intel survives a slim-cache boot (the 'Not Yet Loaded' trace).

Traced bug: the full localStorage mirror (~6.6 MB) exceeds the ~5 MB per-origin quota, so save()
degrades to a SLIM cache with property_intel stripped. A hard reload boots from that slim cache, so
the open case's Property Intel panel renders "Not Yet Loaded" for data the server IS returning.
syncFromPlatform re-fetches the full intel and rebuilds `cases` — but it deliberately does NOT
re-render the open detail (the anti-tab-reset rule), so the panel stayed stale until a manual click.

Neither suspect strips the in-memory object: save() strips a COPY, and the rebuild REPLACES from the
fresh fetch. The fix (#1) re-renders the open detail ONLY when the open case's property_intel goes
absent→present, preserving the active tab. (#2) the slim cache retains the OPEN case's intel.

This pins:
  * the pre-sync boot state (slim) genuinely renders Not-Yet-Loaded;
  * after a sync, the open panel AUTO-populates with no click (fix #1);
  * the re-render fires on materialization only — a second sync with intel already present does
    NOT re-render (so the 30s tick never resets the tab);
  * the active tab is preserved across the materialization re-render;
  * (#2) a forced-quota slim save keeps the OPEN case's property_intel and drops the others.

Run: python3 test_intel_reload.py   (exit 0 = all green)
"""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
from browser_env import chrome_path

HTML = Path("frontend/index.html").resolve()

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

FULL_PI = json.dumps({"account_number": "1234", "market_value": 231730,
                      "current_tax_balance": 17551, "year_built": 1962})

# Two platform cases, each WITH full property_intel (what the server returns).
CASES = [
    {"case_number": "TX-26-01298", "property_address": "1 Moon Dr", "defendant": "M", "city": "dallas",
     "property_intel": FULL_PI, "account_number": "1234", "disposition_state": "active"},
    {"case_number": "TX-26-01299", "property_address": "2 Sun Dr", "defendant": "S", "city": "dallas",
     "property_intel": FULL_PI, "account_number": "5678", "disposition_state": "active"},
]

# A SLIM boot cache (property_intel stripped) — exactly what save() writes at quota. Seeded into
# localStorage before load so the page boots from it, reproducing the hard-reload state.
def slim_boot(cn_open):
    return [{"id": c["case_number"] + "_platform", "city": "dallas",
             "rep_assigned": "", "uploadedAt": "2026-07-01T00:00:00Z",
             "account_number": c["account_number"],
             "extracted": {"caseNumber": c["case_number"], "propertyAddress": c["property_address"],
                           "defendant": c["defendant"], "accountNumber": c["account_number"]}}
            for c in CASES]

# The event fetch BLOCKS on window.__holdEvents so the sync cannot complete — hence cannot rebuild
# `cases` — until the test releases it. This makes the pre-sync (slim boot) window deterministic
# regardless of how long the large HTML takes to parse/load (a fixed delay races page-load).
STUB = """
window.__holdEvents = true;
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/cases') && opts && opts.method==='POST') return J({status:'ok'});
  if (url.endsWith('/api/cases')) return J(%s);
  if (url.indexOf('/api/events')>=0) { while(window.__holdEvents){ await new Promise(r=>setTimeout(r,20)); } return url.endsWith('/api/events') ? J({}) : J([]); }
  if (url.endsWith('/api/stats')) return J({total_cases:2, active_cases:2, watching_cases:0, archived_cases:0, total_all:2, pending_review:0});
  if (url.endsWith('/api/reps')) return J([]);
  if (url.endsWith('/api/dispositions/codes')) return J({groups:[], codes:[]});
  if (url.endsWith('/api/dispositions')) return J({review_queue:[], recently_archived:[], by_code:[], by_state:{}, counts:{}});
  return J([]);
};
""" % json.dumps(CASES)

chrome = chrome_path()
if not chrome:
    print("SKIP: no chromium available for this checkout"); sys.exit(0)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    # Seed the SLIM boot cache + install the fetch stub BEFORE any script runs.
    pg.add_init_script("window.localStorage.setItem('tfi_v5', JSON.stringify(%s));" % json.dumps(slim_boot("TX-26-01298")))
    pg.add_init_script(STUB)
    pg.goto("file://" + str(HTML))
    pg.wait_for_timeout(200)   # init()'s auto-sync is now BLOCKED on the held event fetch

    # Select the open case (pre-sync), from the slim boot object — sync is still held.
    pg.evaluate("() => { const c = cases.find(x=>x.extracted.caseNumber==='TX-26-01298'); selectCase(c.id); }")
    pg.wait_for_timeout(50)
    # Move to the Property Intel tab so we're watching the exact panel.
    pg.evaluate("""() => { const b=Array.from(document.querySelectorAll('#cdet .tb')).find(x=>(x.getAttribute('onclick')||'').indexOf("'pi'")>=0); swTab({target:b},'pi'); }""")
    pg.wait_for_timeout(50)

    boot_pi = pg.evaluate("() => { const c=cases.find(x=>x.extracted.caseNumber==='TX-26-01298'); return c.property_intel? c.property_intel.length:0; }")
    check("PRE-SYNC: booted from slim cache — open case has NO property_intel in memory", boot_pi == 0)
    panel_pre = pg.evaluate("() => document.getElementById('prop-intel-panel').textContent")
    check("PRE-SYNC: the Property Intel panel shows 'Not Yet Loaded'", "Not Yet Loaded" in panel_pre)
    check("PRE-SYNC: the rep is on the Property Intel tab",
          pg.evaluate("() => activeDetailTabId()") == "pi")

    # Release the sync — it fetches full intel + rebuilds. The fix must AUTO-populate the panel.
    pg.evaluate("() => { window.__holdEvents = false; }")
    pg.wait_for_timeout(700)

    mem_pi = pg.evaluate("() => { const c=cases.find(x=>x.extracted.caseNumber==='TX-26-01298'); return c.property_intel? c.property_intel.length:0; }")
    check("POST-SYNC: in-memory open case now carries full property_intel", mem_pi > 0)
    panel_post = pg.evaluate("() => document.getElementById('prop-intel-panel').textContent")
    check("*** POST-SYNC: the panel AUTO-populated with NO click (fix #1) ***",
          "Not Yet Loaded" not in panel_post and "231,730" in panel_post.replace(" ", "").replace("\\u00a0","") or "231730" in panel_post)
    check("POST-SYNC: the active tab was PRESERVED (still Property Intel, not snapped to Overview)",
          pg.evaluate("() => activeDetailTabId()") == "pi")

    # A SECOND sync with intel already present must NOT re-render (no tab reset on the 30s tick).
    pg.evaluate("""() => { const b=Array.from(document.querySelectorAll('#cdet .tb')).find(x=>(x.getAttribute('onclick')||'').indexOf("'fin'")>=0); if(b) swTab({target:b},'fin'); }""")
    pg.wait_for_timeout(30)
    tab_before2 = pg.evaluate("() => activeDetailTabId()")
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(500)
    tab_after2 = pg.evaluate("() => activeDetailTabId()")
    check("STEADY STATE: a second sync (intel already present) does NOT re-render — tab unchanged",
          tab_before2 == tab_after2 and tab_after2 == "fin")

    # ── fix #2: a forced-quota slim save retains the OPEN case's intel, drops the others ──
    res = pg.evaluate("""() => {
      const c = cases.find(x=>x.extracted.caseNumber==='TX-26-01298'); activeId = c.id;
      const realSet = Storage.prototype.setItem;
      let firstFull = true;
      Storage.prototype.setItem = function(k, v){
        if (k==='tfi_v5' && firstFull && v.length > 50) { firstFull = false; throw new DOMException('QuotaExceededError'); }
        return realSet.call(this, k, v);
      };
      save();
      Storage.prototype.setItem = realSet;
      const cached = JSON.parse(localStorage.getItem('tfi_v5'));
      const openC = cached.find(x=>x.extracted.caseNumber==='TX-26-01298');
      const otherC = cached.find(x=>x.extracted.caseNumber==='TX-26-01299');
      return { openHasPi: !!(openC && openC.property_intel), otherHasPi: !!(otherC && otherC.property_intel) };
    }""")
    check("SLIM CACHE (#2): the OPEN case's property_intel is RETAINED", res["openHasPi"] is True)
    check("SLIM CACHE (#2): other cases' property_intel is dropped (mirror still slims)", res["otherHasPi"] is False)

    check("ZERO page errors", not errors)
    if errors:
        for e in errors[:5]: print("     ! " + e)
    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
