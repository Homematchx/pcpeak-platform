#!/usr/bin/env python3
"""Test for the Petition-link fix (Option 1). The button must NEVER download the app page.

Mode B was: with no captured petition_href, the Download link sat on href="#" and clicking it saved
the app's own index.html (~163KB). Fix: a single "View in court portal" link that (a) only ever points
at a real http(s) court URL, opened in a new tab with NO download attribute, and (b) is hidden (with a
"not captured" note) when there's no URL.

Two parts:
  A. STATIC — assert the real markup in index.html (the panel lives inside a JS template literal, so
     it's not in the initial DOM): #petition-view has target=_blank + NO download attr; the old
     #petition-download / #petition-fullscreen IDs are gone; #petition-none exists.
  B. RUNTIME (Chromium) — inject the link elements, stub fetch, drive openPetitionPDF() for a case
     WITH a court URL and one WITHOUT (404), and assert href/visibility + that a click in the no-URL
     state triggers ZERO downloads and the JS never adds a download attribute.
"""
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

from browser_env import chrome_path
CHROME = chrome_path()   # pinned sandbox path first, else the local playwright cache
HTML_PATH = Path("frontend/index.html").resolve()
HTML = HTML_PATH.read_text()

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)


# ── Part A: static markup ──
m = re.search(r'<a id="petition-view".*?</a>', HTML, re.S)
view_tag = m.group(0) if m else ""
check("A: #petition-view link exists in markup", bool(view_tag))
check("A: #petition-view opens a new tab (target=_blank)", 'target="_blank"' in view_tag)
check("A: #petition-view has NO download attribute", not re.search(r'\bdownload\b', view_tag))
check("A: #petition-view uses rel=noopener", "noopener" in view_tag)
check("A: 'not captured' note (#petition-none) exists", 'id="petition-none"' in HTML)
check("A: old #petition-download is GONE", 'id="petition-download"' not in HTML)
check("A: old #petition-fullscreen is GONE", 'id="petition-fullscreen"' not in HTML)
check("A: no download attr is set in openPetitionPDF JS", 'setAttribute("download"' not in HTML)


# ── Part B: runtime behavior ──
STUB = r"""
window.__downloads = [];
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o,s)=> new Response(JSON.stringify(o), {status:s||200, headers:{'Content-Type':'application/json'}});
  if (url.indexOf('/api/petition/TX-HAS') >= 0)
    return J({url:'https://courtsportal.dallascounty.org/DALLASPROD/Home/Dashboard/29?doc=999'});
  if (url.indexOf('/api/petition/TX-NONE') >= 0)
    return J({detail:'not found'}, 404);
  return J([]);
};
// Inject the link elements the real detail-panel template renders (not present until a case renders).
document.body.insertAdjacentHTML('beforeend',
  '<span id="petition-none" style="display:none">note</span>' +
  '<a id="petition-view" href="#" target="_blank" rel="noopener" style="display:none">view</a>' +
  '<div id="petition-brief"></div>');
"""

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("download", lambda d: pg.evaluate("fn => window.__downloads.push(fn)", d.suggested_filename))
    pg.goto(HTML_PATH.as_uri())
    pg.wait_for_timeout(300)
    pg.evaluate(STUB)

    # WITH a court URL
    pg.evaluate("openPetitionPDF({ id:'TX-HAS_platform', extracted:{}, property_intel:'' })")
    pg.wait_for_timeout(300)
    v = pg.eval_on_selector("#petition-view", """el => ({
        href: el.getAttribute('href'), display: getComputedStyle(el).display,
        hasDownload: el.hasAttribute('download') })""")
    none1 = pg.eval_on_selector("#petition-none", "el => getComputedStyle(el).display")
    check("B: WITH url → link visible", v["display"] != "none")
    check("B: WITH url → href is the real https court URL",
          (v["href"] or "").startswith("https://courtsportal.dallascounty.org"))
    check("B: WITH url → JS did NOT add a download attribute", v["hasDownload"] is False)
    check("B: WITH url → note hidden", none1 == "none")

    # NO url (404)
    pg.evaluate("openPetitionPDF({ id:'TX-NONE_platform', extracted:{}, property_intel:'' })")
    pg.wait_for_timeout(300)
    v2 = pg.eval_on_selector("#petition-view", """el => ({
        href: el.getAttribute('href'), display: getComputedStyle(el).display })""")
    none2 = pg.eval_on_selector("#petition-none", "el => getComputedStyle(el).display")
    check("B: NO url → link hidden", v2["display"] == "none")
    check("B: NO url → href is NOT '#' (never saves the app page)", v2["href"] != "#")
    check("B: NO url → note shown", none2 != "none")

    # click in the NO-url state → zero downloads
    pg.eval_on_selector("#petition-view", "el => el.click()")
    pg.wait_for_timeout(300)
    check("B: NO url → clicking triggers ZERO downloads",
          len(pg.evaluate("() => window.__downloads")) == 0)
    check("B: ZERO uncaught pageerror", len(errors) == 0)
    if errors:
        for e in errors: print("   pageerror:", e)
    b.close()

print("-"*56)
t, ok = len(_res), sum(_res)
print(f"{ok}/{t} passed")
sys.exit(0 if ok == t else 1)
