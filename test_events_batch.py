#!/usr/bin/env python3
"""Events regression pin — syncFromPlatform fetches ZERO events (Phase 3: detail-on-open). No network.

Updated for skeleton-cache Phase 3: the interim "one bulk /api/events call" is superseded — the
sync now fetches no events at all (events ride the /api/cases/{cn} detail fetch on open). This is
the stronger form of the same guarantee: the third-strike per-case events storm can never return.

The third-strike root cause: syncFromPlatform did 1 GET /api/cases + ~244 SEQUENTIAL
GET /api/events/{cn}. That loop was behind the 502 bursts, the mid-sync navigation race, and the
stale-intel-panel window. Phase 3 removed events from the sync entirely. This pins that:

  * the sync fires ZERO events requests — no bulk /api/events, no per-case /api/events/{cn};
  * repeated syncs still fetch zero events;
  * the case list is intact across syncs.

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

    # Phase 3 SUPERSEDES the interim batching: the sync now fetches NO events at all (detail —
    # including events — is fetched on OPEN via /api/cases/{cn}; see test_detail_on_demand). This
    # is a STRONGER guarantee than "one bulk call" — a dedicated regression pin so the third-strike
    # events storm (N sequential per-case GETs on every sync) can never return in any form.
    calls = pg.evaluate("() => window.__calls")
    check("*** the sync fetches ZERO events — no bulk, no per-case (the storm is gone) ***",
          calls["bulk"] == 0 and calls["percase"] == 0)
    check("6 cases loaded from one /api/cases call",
          pg.evaluate("() => cases.length") == 6 and calls["cases"] == 1)

    # Repeated syncs never fetch events.
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(500)
    calls2 = pg.evaluate("() => window.__calls")
    check("a second sync still fetches ZERO events (no per-case, no bulk)",
          calls2["bulk"] == 0 and calls2["percase"] == 0)
    check("...and the case list is intact after repeated syncs", pg.evaluate("() => cases.length") == 6)

    check("ZERO page errors", not errors)
    if errors:
        for e in errors[:5]: print("     ! " + e)
    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
