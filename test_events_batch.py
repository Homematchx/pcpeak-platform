#!/usr/bin/env python3
"""Events-batching regression pin — syncFromPlatform makes ONE bulk events call, not N. No network.

The third-strike root cause: syncFromPlatform did 1 GET /api/cases + ~244 SEQUENTIAL
GET /api/events/{cn}. That loop was behind the 502 bursts, the mid-sync navigation race, and the
stale-intel-panel window. Fixed by fetching all events in one GET /api/events (grouped by
case_number) and joining locally. This pins the fix so the loop can't quietly return:

  * the sync fires the BULK /api/events exactly once,
  * it fires ZERO per-case /api/events/{cn} requests (regardless of case count),
  * events are still correctly joined onto each case (the Timeline is populated),
  * a bulk-events failure degrades gracefully (cases still render).

Run: python3 test_events_batch.py   (exit 0 = all green)
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

# 6 cases — enough that an N-per-case loop would be unmistakable in the call counts.
CASES = [{"case_number": f"TX-26-{i:05d}", "property_address": f"{i} St", "defendant": chr(65+i),
          "city": "dallas", "disposition_state": "active"} for i in range(1, 7)]

# Bulk events grouped by case_number — one case gets a real docket event to prove the join.
EVENTS = {"TX-26-00001": [{"event_date": "2026-07-24", "description": "Order of Sale issued",
                           "event_type": "outcome", "is_new": 1}]}

STUB = """
window.__calls = {bulk: 0, percase: 0, cases: 0};
window.__failBulk = false;
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/cases') && opts && opts.method==='POST') return J({status:'ok'});
  if (url.endsWith('/api/cases')) { window.__calls.cases++; return J(%s); }
  if (url.endsWith('/api/events')) { window.__calls.bulk++;
      if (window.__failBulk) return new Response('x', {status:500});
      return J(%s); }
  if (url.indexOf('/api/events/')>=0) { window.__calls.percase++; return J([]); }   // must stay 0
  if (url.endsWith('/api/stats')) return J({total_cases:6, active_cases:6, watching_cases:0, archived_cases:0, total_all:6, pending_review:0});
  if (url.endsWith('/api/reps')) return J([]);
  if (url.endsWith('/api/dispositions/codes')) return J({groups:[], codes:[]});
  if (url.endsWith('/api/dispositions')) return J({review_queue:[], recently_archived:[], by_code:[], by_state:{}, counts:{}});
  return J([]);
};
window.prompt = function(){ return ''; };
""" % (json.dumps(CASES), json.dumps(EVENTS))

chrome = chrome_path()
if not chrome:
    print("SKIP: no chromium available for this checkout"); sys.exit(0)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script("window.localStorage.clear();")
    pg.add_init_script(STUB)
    pg.goto("file://" + str(HTML))
    pg.wait_for_timeout(900)   # initial auto-sync runs

    calls = pg.evaluate("() => window.__calls")
    check("the sync fetched the BULK /api/events exactly once", calls["bulk"] == 1)
    check("*** ZERO per-case /api/events/{cn} requests (the N-fetch loop is gone) ***",
          calls["percase"] == 0)
    check("6 cases loaded from one /api/cases call",
          pg.evaluate("() => cases.length") == 6 and calls["cases"] == 1)

    # The join still works — the case with a bulk event carries it into its timeline.
    joined = pg.evaluate("""() => {
        const c = cases.find(x => x.extracted.caseNumber === 'TX-26-00001');
        return c && c.extracted.keyDocketEvents ? c.extracted.keyDocketEvents.length : 0;
    }""")
    check("events are joined locally onto the right case (Timeline populated)", joined == 1)
    check("a case with no events joins to an empty list (no crash)",
          pg.evaluate("""() => { const c = cases.find(x=>x.extracted.caseNumber==='TX-26-00002');
                                 return Array.isArray(c.extracted.keyDocketEvents) && c.extracted.keyDocketEvents.length===0; }"""))

    # Do a second manual sync — still one bulk call per sync, never per-case.
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(500)
    calls2 = pg.evaluate("() => window.__calls")
    check("a second sync adds exactly one more bulk call, still zero per-case",
          calls2["bulk"] == 2 and calls2["percase"] == 0)

    # Graceful degradation: bulk events fails → cases still render (timeline just waits for next sync).
    pg.evaluate("() => { window.__failBulk = true; }")
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(500)
    check("a bulk-events failure does NOT break the sync (cases still present)",
          pg.evaluate("() => cases.length") == 6)
    check("...and still fires no per-case fallback requests",
          pg.evaluate("() => window.__calls.percase") == 0)

    check("ZERO page errors", not errors)
    if errors:
        for e in errors[:5]: print("     ! " + e)
    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
