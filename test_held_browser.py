#!/usr/bin/env python3
"""Browser test for the Held-for-Review frontend flow. Stubs window.fetch so no network/worker is
needed: it serves a held list (worker online), asserts the view renders + the worker-online dot,
then clicks Approve and asserts the approve POST + job-poll → the case drops off + syncFromPlatform
runs. Also asserts ZERO uncaught pageerror (the syncPlatform-typo class of bug)."""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
HTML = Path("frontend/index.html").resolve()

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

STUB = r"""
// Pretend a token is already present so loadHeld() runs silently (no prompt).
sessionStorage.setItem('scrape_token', 'test-token');
window.__calls = [];
window.__approveState = 'queued';   // flips to 'done' after the approve POST
const realFetch = window.fetch;
window.fetch = async function(url, opts){
  url = String(url);
  window.__calls.push({url, method: (opts&&opts.method)||'GET'});
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/held')) {
    const approving = window.__approveState === 'inflight';
    return J({worker:{online:true, last_seen:'now', age_secs:2},
              held:[{case_number:'TX-26-00010', property_address:'200 Oak Ave',
                     defendant:'ROE, JANE', total_due:4200, account_status:'resolved',
                     approving: approving}]});
  }
  if (url.match(/\/api\/held\/.*\/approve$/)) {
    window.__approveState = 'inflight';
    return J({job_id: 77, status:'queued', approve:'TX-26-00010'});
  }
  if (url.match(/\/api\/scrape-jobs\/77$/)) {
    // job completes on the first poll (one 3s cycle)
    window.__approveState = 'done-drop';
    return J({status:'done', result:{approved:'TX-26-00010'}});
  }
  if (url.endsWith('/api/cases')) return J([]);           // syncFromPlatform
  if (url.match(/\/api\/(stats|reps|agent\/runs)/)) return J([]);
  return new Response('[]', {status:200, headers:{'Content-Type':'application/json'}});
};
// After the approve job is done, the next /api/held must NOT list the case (it published).
const _origHeld = window.fetch;
"""

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(STUB)
    pg.goto(HTML.as_uri())
    pg.wait_for_timeout(800)

    # held view rendered with the case + worker-online
    worker_txt = pg.eval_on_selector("#held-worker", "el => el.textContent") or ""
    check("worker shows ONLINE", "online" in worker_txt.lower())
    count_txt = pg.eval_on_selector("#held-count", "el => el.textContent") or ""
    check("held count shows 1", count_txt.strip() == "1")
    list_html = pg.eval_on_selector("#held-list", "el => el.innerHTML") or ""
    check("held list shows the case number", "TX-26-00010" in list_html)
    check("held list shows an Approve button", "Approve" in list_html and "approveHeld" in list_html)

    # Override /api/held to drop the case once it's published, so the post-approve refresh empties it.
    pg.evaluate("""() => {
      const f = window.fetch;
      window.fetch = async function(url, opts){
        url = String(url);
        if (url.endsWith('/api/held') && window.__approveState === 'done-drop') {
          return new Response(JSON.stringify({worker:{online:true,age_secs:1}, held:[]}),
                              {status:200, headers:{'Content-Type':'application/json'}});
        }
        return f(url, opts);
      };
    }""")

    # click Approve
    pg.eval_on_selector("#held-list button", "el => el.click()")
    pg.wait_for_timeout(4200)   # allow the 3s job poll + refresh

    calls = pg.evaluate("() => window.__calls.map(c => c.method + ' ' + c.url)")
    check("approve POST was sent",
          any("POST" in c and "/approve" in c for c in calls))
    check("approve job was polled (scrape-jobs/77)",
          any("/api/scrape-jobs/77" in c for c in calls))
    check("syncFromPlatform ran after approve (GET /api/cases)",
          any(c.endswith("/api/cases") for c in calls))

    final_list = pg.eval_on_selector("#held-list", "el => el.textContent") or ""
    check("case dropped off the held list after publish", "TX-26-00010" not in final_list)
    check("ZERO uncaught pageerror (no syncPlatform-typo class bug)", len(errors) == 0)
    if errors:
        for e in errors: print("   pageerror:", e)
    b.close()

print("-"*56)
t, ok = len(_res), sum(_res)
print(f"{ok}/{t} passed")
sys.exit(0 if ok == t else 1)
