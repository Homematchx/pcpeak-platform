#!/usr/bin/env python3
"""Browser test for the CASE DISPOSITION UI (docs/case-disposition-design.md §13).

THE CENTRAL PIN — the exact failure the old Remove button had:

    delCase() filtered the in-memory array and wrote localStorage. It made NO network call, so
    syncFromPlatform's authoritative rebuild (cases = platformV3.concat(drafts)) put every
    platform case straight back within ~30 seconds. Remove looked like it worked and didn't.

    So it is not enough that Dispose POSTs and the card disappears. This asserts the card is
    STILL GONE after a full sync tick, against a platform payload that (like the real API) no
    longer returns the archived case.

Also pinned: the taxonomy renders grouped from the SERVER (no drifting second copy); a
comment-required code is blocked client-side; the confirm line NAMES the resulting state, with
`watching` distinguished from `archived`; watching cases leave the working queue but are reachable
in their own view; the review chip renders without changing state; and ZERO page errors.

Run: python3 test_disposition_browser.py   (exit 0 = all green)
"""
import json
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

HTML = Path("frontend/index.html").resolve()

# Resolve chromium portably: the pinned sandbox path first, then playwright's own download cache,
# then whatever playwright's default resolution finds. The other browser suites in this repo
# hardcode a Linux path and can't run on a Mac checkout.
_CANDIDATES = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    os.path.expanduser("~/Library/Caches/ms-playwright/chromium-1223/chrome-mac-x64/"
                       "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
]


def _chrome():
    for c in _CANDIDATES:
        if os.path.exists(c):
            return c
    for base in (Path.home() / "Library/Caches/ms-playwright",
                 Path("/opt/pw-browsers")):
        if base.exists():
            for exe in list(base.glob("chromium-*/chrome-*/chrome")) + \
                       list(base.glob("chromium-*/chrome-*/*.app/Contents/MacOS/*")):
                if exe.is_file():
                    return str(exe)
    return None


_res = []


def check(name, cond):
    _res.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)


CODES = {
    "groups": ["Taxpayer resolved", "Out of scope", "Court outcome", "PC Peak outcome", "Data quality"],
    "codes": [
        {"code": "paid_in_full", "label": "Paid / zero balance", "group": "Taxpayer resolved",
         "state": "archived", "comment": False},
        {"code": "payment_plan_33_02", "label": "§33.02 payment plan", "group": "Taxpayer resolved",
         "state": "watching", "comment": True},
        {"code": "acquired", "label": "Acquired by PC Peak", "group": "PC Peak outcome",
         "state": "archived", "comment": True},
        {"code": "duplicate", "label": "Duplicate of another case", "group": "Data quality",
         "state": "archived", "comment": True},
    ],
}

# A (active) · B (archived by the test) · C (watching) · D (flagged for review)
FULL = [
    {"case_number": "TX-26-00801", "property_address": "1 Active St", "defendant": "A", "city": "dallas",
     "disposition_state": "active"},
    {"case_number": "TX-26-00802", "property_address": "2 Gone St", "defendant": "B", "city": "dallas",
     "disposition_state": "active"},
    {"case_number": "TX-26-00803", "property_address": "3 Watch St", "defendant": "C", "city": "dallas",
     "disposition_state": "watching", "disposition_code": "payment_plan_33_02"},
    {"case_number": "TX-26-00804", "property_address": "4 Flag St", "defendant": "D", "city": "dallas",
     "disposition_state": "active", "pending_review": 1, "pending_review_code": "paid_in_full"},
]

STUB = """
window.__disposed = [];
window.__archived = new Set();
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  const m = url.match(/\\/api\\/cases\\/([^/]+)\\/disposition$/);
  if (m && opts && opts.method === 'POST') {
    const body = JSON.parse(opts.body);
    window.__disposed.push({cn: decodeURIComponent(m[1]), code: body.code,
                            comment: body.comment, decided_by: body.decided_by});
    const spec = %s.codes.find(s => s.code === body.code) || {};
    // Mirror the server: an ARCHIVED case stops being returned by /api/cases entirely.
    if (spec.state === 'archived') window.__archived.add(decodeURIComponent(m[1]));
    return J({status:'ok', state: spec.state, code: body.code, label: spec.label});
  }
  if (url.endsWith('/api/dispositions/codes')) return J(%s);
  if (url.endsWith('/api/dispositions')) return J({review_queue:[], recently_archived:[], by_code:[],
                                                   by_state:{}, counts:{}});
  if (url.endsWith('/api/cases') && opts && opts.method==='POST') return J({status:'ok'});
  if (url.endsWith('/api/cases'))
    return J(%s.filter(c => !window.__archived.has(c.case_number)));
  if (url.indexOf('/api/events/')>=0) return J([]);
  if (url.endsWith('/api/stats')) {
    const n = %s.length, a = window.__archived.size;
    return J({total_cases:n-a, active_cases:n-a-1, watching_cases:1, archived_cases:a,
              total_all:n, pending_review:1});
  }
  if (url.endsWith('/api/reps')) return J([{id:1,name:'Jay Lewis',active:1,case_count:0}]);
  return J([]);
};
""" % (json.dumps(CODES), json.dumps(CODES), json.dumps(FULL), json.dumps(FULL))


def shown(pg):
    return pg.evaluate("() => Array.from(document.querySelectorAll('.cchip-n')).map(e=>e.textContent.trim())")


chrome = _chrome()
if not chrome:
    print("SKIP: no chromium available for this checkout")
    sys.exit(0)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script("window.localStorage.clear();")
    pg.add_init_script(STUB)
    pg.goto("file://" + str(HTML))
    pg.wait_for_timeout(900)

    # ── the dead Clear All footgun is gone ──
    check("Clear All button is GONE (meaningless under archive-never-delete)",
          pg.evaluate("() => document.body.innerHTML.indexOf('Clear All') < 0"))
    check("delCase/clearAll are no longer defined",
          pg.evaluate("() => typeof delCase==='undefined' && typeof clearAll==='undefined'"))

    # ── queue composition: watching leaves the working queue ──
    lst = shown(pg)
    check("active + flagged cases are in the working queue",
          "TX-26-00801" in lst and "TX-26-00804" in lst)
    check("a WATCHING case leaves the working queue", "TX-26-00803" not in lst)
    pg.evaluate("() => setView('watching')")
    pg.wait_for_timeout(250)
    check("...and is reachable in its own Watching view", "TX-26-00803" in shown(pg))
    pg.evaluate("() => setView('active')")
    pg.wait_for_timeout(250)

    # ── review flag renders WITHOUT changing state ──
    check("a flagged case shows a REVIEW chip",
          pg.evaluate("() => document.body.innerHTML.indexOf('REVIEW') >= 0"))
    check("...and is still in the working queue (a flag never changes what the case IS)",
          "TX-26-00804" in shown(pg))

    # ── counts reconcile on their face ──
    txt = pg.evaluate("() => document.getElementById('dcounts').textContent")
    check("the count line states all four numbers (denominator never implicit)",
          "active" in txt and "watching" in txt and "archived" in txt and "total" in txt)

    # ── the modal: server-served taxonomy, grouped ──
    pg.evaluate("() => openDispose(cases.find(c=>c.extracted.caseNumber==='TX-26-00802').id)")
    pg.wait_for_timeout(350)
    check("the Dispose modal opens", pg.evaluate("() => !!document.getElementById('dispo-modal')"))
    check("the taxonomy renders GROUPED from the server (no second copy in the UI)",
          pg.evaluate("() => document.querySelectorAll('#dispo-code optgroup').length === 5"))
    check("a watching code is labelled as such in the picker",
          pg.evaluate("() => document.getElementById('dispo-code').innerHTML.indexOf('→ Watching') >= 0"))
    check("decided_by is labelled SELF-ATTESTED (no auth on this platform)",
          pg.evaluate("() => document.getElementById('dispo-modal').innerHTML.indexOf('self-attested') >= 0"))

    # ── the confirm line NAMES the resulting state ──
    pg.select_option("#dispo-code", "payment_plan_33_02")
    pg.wait_for_timeout(150)
    pv = pg.evaluate("() => document.getElementById('dispo-preview').textContent")
    check("a watching code previews as 'Watching', not archive",
          "Watching" in pv and "Archives" not in pv)
    check("...and marks the comment required",
          pg.evaluate("() => document.getElementById('dispo-req').textContent.indexOf('required') >= 0"))
    pg.select_option("#dispo-code", "paid_in_full")
    pg.wait_for_timeout(150)
    pv = pg.evaluate("() => document.getElementById('dispo-preview').textContent")
    check("an archiving code previews as 'Archives' + permanently searchable",
          "Archives" in pv and "searchable" in pv)

    # ── comment-required is blocked client-side ──
    pg.select_option("#dispo-code", "duplicate")
    pg.wait_for_timeout(120)
    pg.evaluate("() => { window.__alert=null; window.alert = m => window.__alert = m; }")
    pg.evaluate("() => submitDispose(cases.find(c=>c.extracted.caseNumber==='TX-26-00802').id)")
    pg.wait_for_timeout(250)
    check("a comment-required code is refused without a comment",
          pg.evaluate("() => (window.__alert||'').indexOf('requires a comment') >= 0"))
    check("...and nothing was POSTed", pg.evaluate("() => window.__disposed.length === 0"))

    # ══════════════════════════════════════════════════════════════════
    # THE CENTRAL PIN — archive, then survive a full sync tick
    # ══════════════════════════════════════════════════════════════════
    pg.fill("#dispo-comment", "same property as TX-26-00801")
    pg.evaluate("() => submitDispose(cases.find(c=>c.extracted.caseNumber==='TX-26-00802').id)")
    pg.wait_for_timeout(700)
    check("Dispose POSTs the code + comment to the server (not a localStorage edit)",
          pg.evaluate("() => window.__disposed.length===1 && window.__disposed[0].code==='duplicate'"
                      " && window.__disposed[0].cn==='TX-26-00802'"))
    check("the archived card disappears immediately", "TX-26-00802" not in shown(pg))

    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(800)
    check("*** the archived case is STILL GONE after a full sync tick "
          "(the exact failure the old Remove had) ***", "TX-26-00802" not in shown(pg))
    pg.evaluate("() => syncFromPlatform()")
    pg.wait_for_timeout(800)
    check("...still gone after a second sync", "TX-26-00802" not in shown(pg))
    check("...and the other cases were NOT collateral damage",
          "TX-26-00801" in shown(pg) and "TX-26-00804" in shown(pg))
    check("nothing was deleted locally — the case simply stopped being served",
          pg.evaluate("() => !cases.some(c => c.extracted.caseNumber==='TX-26-00802')"))

    check("ZERO page errors", not errors)
    if errors:
        for e in errors[:5]:
            print("     ! " + e)
    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
