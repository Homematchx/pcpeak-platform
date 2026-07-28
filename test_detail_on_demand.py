#!/usr/bin/env python3
"""Skeleton-cache Phase 3 — detail-on-demand. The four verification points, in the browser. No network.

Phase 3 deletes the mirror: the list caches SKELETONS (no property_intel), and a case's detail is
fetched on open via /api/cases/{cn}. This proves the switch works and deletes the bug classes at the
root:

  (1) a case opens with skeleton fields INSTANT, detail tabs fill on land, no "Not Yet Loaded" flash
      (a distinct "Loading…" state while the fetch is in flight);
  (2) the balance chip / amount-owed filter read IDENTICALLY to the Financials live balance on the
      same case — same-number-everywhere survives the blob dropping;
  (3) the stale-intel-panel class is GONE — a case with no cached intel fetches FRESH on open;
  (4) events load on detail-open; the sync makes ZERO events calls (bulk or per-case).

Run: python3 test_detail_on_demand.py   (exit 0 = all green)
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


def skel(cn, addr, bal, mv):
    return {"case_number": cn, "property_address": addr, "defendant": "X", "city": "dallas",
            "disposition_state": "active", "current_tax_balance": bal, "market_value": mv,
            "updated_at": "2026-07-28T10:00:00", "stage": "judgment_entered",
            "judgment_date": "2026-01-07", "account_number": "1234"}

def detail(cn, addr, bal, mv):
    d = skel(cn, addr, bal, mv)
    d["property_intel"] = json.dumps({"current_tax_balance": bal, "market_value": mv,
                                      "year_built": 1962, "account_number": "1234"})
    d["events"] = [{"event_date": "2026-01-07", "description": "Judgment Non-Jury entered",
                    "event_type": "outcome", "is_new": 1}]
    return d

SKEL = [skel("TX-26-00001", "1 Moon Dr, Dallas", 15691.0, 210000),
        skel("TX-26-00002", "2 Sun Dr, Dallas", 40000.0, 300000)]
DETAILS = {"TX-26-00001": detail("TX-26-00001", "1 Moon Dr, Dallas", 15691.0, 210000),
           "TX-26-00002": detail("TX-26-00002", "2 Sun Dr, Dallas", 40000.0, 300000)}

# The detail fetch for 00002 BLOCKS on __hold so we can observe the loading state deterministically.
STUB = """
window.__calls = {events: 0, detail: 0, list: 0};
window.__hold2 = false;
window.__details = %s;
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/cases') && opts && opts.method==='POST') return J({status:'ok'});
  if (url.endsWith('/api/cases')) { window.__calls.list++; return J(%s); }
  const m = url.match(/\\/api\\/cases\\/(TX-[0-9-]+)$/);
  if (m) {
    window.__calls.detail++;
    if (m[1] === 'TX-26-00002') { while(window.__hold2){ await new Promise(r=>setTimeout(r,20)); } }
    return J(window.__details[m[1]] || null);
  }
  if (url.indexOf('/api/events')>=0) { window.__calls.events++; return J({}); }   // must stay 0
  if (url.endsWith('/api/stats')) return J({total_cases:2, active_cases:2, watching_cases:0, archived_cases:0, total_all:2, pending_review:0});
  if (url.endsWith('/api/reps')) return J([]);
  if (url.endsWith('/api/dispositions/codes')) return J({groups:[], codes:[]});
  if (url.endsWith('/api/dispositions')) return J({review_queue:[], recently_archived:[], by_code:[], by_state:{}, counts:{}});
  return J([]);
};
window.prompt = function(){ return ''; };
""" % (json.dumps(DETAILS), json.dumps(SKEL))

chrome = chrome_path()
if not chrome:
    print("SKIP: no chromium available for this checkout"); sys.exit(0)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script("window.localStorage.clear();")
    pg.add_init_script(STUB)   # __hold2 defaults false — cases load normally
    pg.goto("file://" + str(HTML))
    pg.wait_for_timeout(700)   # initial skeleton sync + auto-select (loads that case's detail)

    # ── (4) sync fetches NO events; skeleton has no blob but has the balance column ──
    calls = pg.evaluate("() => window.__calls")
    check("*** the sync fetches ZERO events (bulk /api/events retired) ***", calls["events"] == 0)
    check("the list skeleton was fetched", calls["list"] >= 1)
    check("the skeleton carries NO property_intel (blob dropped from the list)",
          pg.evaluate("() => !cases.find(x=>x.extracted.caseNumber==='TX-26-00001').property_intel"))
    check("...but the balance COLUMN is on the skeleton",
          pg.evaluate("() => cases.find(x=>x.extracted.caseNumber==='TX-26-00001').current_tax_balance === 15691.0"))

    # ── (2)+(3): open 00001 normally → detail fetched fresh, cached, same-number-everywhere ──
    pg.evaluate("() => { const c = cases.find(x=>x.extracted.caseNumber==='TX-26-00001'); selectCase(c.id); }")
    pg.wait_for_timeout(300)
    check("opening a case fetches its detail via /api/cases/{cn}", pg.evaluate("() => window.__calls.detail") >= 1)
    check("detail cached fresh in-memory (stale-intel-panel class gone)",
          pg.evaluate("() => { const d=_detail['TX-26-00001']; return !!(d && d.full && d.full.property_intel); }"))
    panelA = pg.evaluate("""() => { const b=Array.from(document.querySelectorAll('#cdet .tb')).find(x=>(x.getAttribute('onclick')||'').indexOf("'pi'")>=0); if(b) swTab({target:b},'pi');
                                    const el=document.getElementById('prop-intel-panel'); return el?el.innerText:''; }""")
    check("opened panel shows intel, not Loading / Not-Yet-Loaded",
          "Loading" not in panelA and "Not Yet Loaded" not in panelA)
    same = pg.evaluate("""() => {
        const c = cases.find(x=>x.extracted.caseNumber==='TX-26-00001');
        const cardBal = caseLiveBalance(c);
        const full = _detail['TX-26-00001'].full;
        const pi = parseIntel(full.property_intel);
        const finBal = calcPayoff(full.extracted, pi.current_tax_balance).taxPayoff;
        return { cardBal, finBal, blobBal: pi.current_tax_balance };
    }""")
    check("*** card balance (column) == Financials live balance == blob (same number) ***",
          same["cardBal"] == 15691.0 and same["finBal"] == 15691 and same["blobBal"] == 15691.0)

    # ── LRU: re-open uses the cache (no second detail fetch) ──
    before = pg.evaluate("() => window.__calls.detail")
    pg.evaluate("() => { const c = cases.find(x=>x.extracted.caseNumber==='TX-26-00001'); selectCase(c.id); }")
    pg.wait_for_timeout(150)
    check("re-opening uses the session cache (no second detail fetch)",
          pg.evaluate("() => window.__calls.detail") == before)

    # ── (1) loading state: 00002 fetch is HELD → skeleton instant, panel 'Loading' not Not-Yet-Loaded ──
    pg.evaluate("() => { window.__hold2 = true; delete _detail['TX-26-00002']; }")
    pg.evaluate("() => { const c = cases.find(x=>x.extracted.caseNumber==='TX-26-00002'); selectCase(c.id); }")
    pg.wait_for_timeout(120)
    check("skeleton header renders INSTANTLY (address up before detail lands)",
          "2 Sun Dr" in pg.evaluate("() => document.getElementById('cdet').innerText"))
    pg.evaluate("""() => { const b=Array.from(document.querySelectorAll('#cdet .tb')).find(x=>(x.getAttribute('onclick')||'').indexOf("'pi'")>=0); if(b) swTab({target:b},'pi'); }""")
    pg.wait_for_timeout(60)
    panel = pg.evaluate("() => { const el=document.getElementById('prop-intel-panel'); return el?el.innerText:''; }")
    check("*** mid-fetch the panel shows 'Loading', NOT 'Not Yet Loaded' (no stale flash) ***",
          "Loading" in panel and "Not Yet Loaded" not in panel)
    pg.evaluate("() => { window.__hold2 = false; }")
    pg.wait_for_timeout(300)
    panel2 = pg.evaluate("() => { const el=document.getElementById('prop-intel-panel'); return el?el.innerText:''; }")
    check("tabs FILL on land (panel no longer Loading / Not-Yet-Loaded)",
          "Loading" not in panel2 and "Not Yet Loaded" not in panel2)

    check("ZERO page errors", not errors)
    if errors:
        for e in errors[:6]: print("     ! " + e)
    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
