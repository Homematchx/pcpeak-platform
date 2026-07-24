#!/usr/bin/env python3
"""Browser test — the §G land floor ships its EVIDENCE (design §16.9).

WHY THIS MATTERS (the priority argument, pinned as a test): on a land-dominant subject like Kemrock
(TX-26-01190) the ARV is unavailable and the MAO ladder cannot compute, so the land floor is the ONLY
valuation on the screen — a real capital decision rests on it. A telemetry value an operator cannot
verify is a defect, not a backlog item. This asserts the drill-down actually reaches the screen.

Pins: the collapsible renders every banded land sale with address / lot acres / lot sqft / close date
/ reconstructed close price / $-per-lot-sqft; the reconciliation line shows range + median and the
median equals the floor printed above; the section is READ-ONLY (no confirm control); the floor still
never feeds MAO; and zero page errors.

Run: python3 test_land_evidence_browser.py   (exit 0 = all green)
"""
import json
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
from browser_env import chrome_path

HTML = Path("frontend/index.html").resolve()

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)


def land_comp(addr, acres, price, date):
    sqft = round(acres * 43560)
    return {"address": addr, "lot_acres": acres, "lot_sqft": sqft, "close_price": price,
            "close_date": date, "price_per_lot_sqft": round(price / sqft, 2),
            "qualification": {"arms_length_flags": []}}


# Kemrock-shaped: land-dominant, NO ARV, MAO cannot compute — the floor is the only valuation.
LAND = {
    "land_floor": 85500, "median": 85500, "label": "estimated", "n": 4,
    "range": [72000, 99000], "spread": 27000,
    "subject_lot_acres": 0.229, "lot_band_acres": [0.1603, 0.2977],
    "median_price_per_acre": 373362, "median_price_per_lot_sqft": 8.57,
    "recency_months_used": 12, "recency_widened": False,
    "net_of_demolition": 71500, "demolition_cost": 14000,
    "comps": [
        land_comp("6402 Kemrock Dr", 0.180, 72000, "2026-02-11"),
        land_comp("6501 Bonnie View Rd", 0.210, 83000, "2026-04-02"),
        land_comp("1207 Lansing Ave", 0.240, 88000, "2026-05-19"),
        land_comp("6610 Kemrock Dr", 0.275, 99000, "2026-06-30"),
    ],
}

DATA = {
    "valuation_state": "provisional",
    "land_floor": LAND,
    "proposed_comps": [],
    "analysis": {
        "decision": "HOLD",
        "mission_score": {"score": 41, "provisional": True},
        "recommended_rule_pct": {"pct": 70, "tier": "standard"},
        "gates": [],
        "arv": {"value": None, "label": "unavailable"},          # no ARV — the whole point
        "market_value": {"value": 120340, "label": "estimated"},
        "condition": {"condition_class": {"value": "unknown", "label": "inferred"},
                      "rehab_low": {"value": None, "label": "unavailable"},
                      "rehab_high": {"value": None, "label": "unavailable"}},
        "mao_ladder": None,                                       # cannot compute without an ARV
        "tax_payoff": {"value": 18450, "label": "verified"},
        "seller_net_sheet": {
            "agreed_price": {"value": None, "label": "unavailable"},
            "tax_payoff": {"value": 18450, "label": "verified"},
            "tax_suit_attorney_fees": {"value": 3690, "label": "estimated"},
            "mowing_labor_and_other_liens": {"value": None, "label": "unavailable"},
            "seller_closing_costs": {"value": None, "label": "unavailable"},
            "seller_net": {"value": None, "label": "unavailable"},
            "closable": None,
        },
    },
}

chrome = chrome_path()
if not chrome:
    print("SKIP: no chromium available for this checkout"); sys.exit(0)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
    pg = b.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script("window.localStorage.clear();")
    # Keep the page inert — we drive the pure renderer directly, no network and no token prompt.
    pg.add_init_script("""
      window.fetch = async function(){ return new Response('[]', {status:200, headers:{'Content-Type':'application/json'}}); };
      window.prompt = function(){ return ''; };
    """)
    pg.goto("file://" + str(HTML))
    pg.wait_for_timeout(400)

    # #acq-panel lives inside the detail card, which only exists once a case is open — select one.
    pg.evaluate("""() => {
      cases = [{ id:'TX-26-01190_platform', city:'dallas', rep_assigned:'', uploadedAt:'2026-07-01T00:00:00Z',
                 disposition_state:'active',
                 extracted:{ caseNumber:'TX-26-01190', propertyAddress:'6406 Kemrock Dr', defendant:'K',
                             accountNumber:'1234' } }];
      selectCase('TX-26-01190_platform');
    }""")
    pg.wait_for_timeout(200)
    check("the acquisition panel exists once a case is open",
          pg.evaluate("() => !!document.getElementById('acq-panel')"))

    # Open the Acquisition TAB the way a user does. This is NOT optional set-up: #acq-panel sits
    # inside <div class="tp" id="tp-acq">, which is display:none until the tab carries class 'on'.
    # The first version of this test injected markup into that HIDDEN panel and asserted strings —
    # so it passed against a control no user could reach. Activate the tab, THEN render (swTab
    # itself calls renderAcquisition, which would overwrite whatever we injected first).
    pg.evaluate("""() => { const b = Array.from(document.querySelectorAll('#cdet .tb'))
                             .find(x => (x.getAttribute('onclick')||'').indexOf("'acq'") >= 0);
                           swTab({target: b}, 'acq'); }""")
    pg.wait_for_timeout(250)
    check("the Acquisition tab panel is actually VISIBLE (not a hidden tab)",
          pg.evaluate("() => document.getElementById('tp-acq').classList.contains('on')"))

    # Render the acquisition panel from the pure function with our Kemrock-shaped payload.
    pg.evaluate("(d) => { document.getElementById('acq-panel').innerHTML = _acqHtml('TX-26-01190', d); }",
                DATA)
    pg.wait_for_timeout(150)
    check("the evidence section is visible to a user (non-zero box on screen)",
          pg.evaluate("""() => { const d = document.querySelector('#acq-panel details');
                                 return !!d && d.getBoundingClientRect().height > 0; }"""))

    panel = pg.evaluate("() => document.getElementById('acq-panel').innerText")
    html = pg.evaluate("() => document.getElementById('acq-panel').innerHTML")

    # ── the floor line itself still reads as before ──
    check("the Land floor line shows the floor", "85,500" in panel)
    check("the floor is labeled estimated", "estimated" in panel)
    check("MAO still cannot compute — the floor NEVER feeds it", "Needs a confirmed ARV" in panel)
    check("ARV is unavailable (this is the land-dominant case)",
          re.search(r"ARV\s*\n?\s*unavailable", panel) is not None or "unavailable" in panel)

    # ── the collapsible evidence section ──
    check("a <details> collapsible is rendered", pg.evaluate("() => !!document.querySelector('#acq-panel details')"))
    summary = pg.evaluate("() => { const s=document.querySelector('#acq-panel details summary'); return s?s.textContent:''; }")
    check("the summary names how many land sales back the floor", "4 land sales" in summary)
    check("it is COLLAPSED by default (evidence on demand, not noise)",
          pg.evaluate("() => !document.querySelector('#acq-panel details').open"))

    # ══════════════════════════════════════════════════════════════════════════════
    # A REAL USER CLICK must toggle it open. The first version of this test forced
    # `.open = true` and asserted the markup — which passes even when the summary is
    # unclickable, and that is exactly the class of bug that shipped. Assert the
    # BEHAVIOUR: click the summary, then assert open === true.
    # ══════════════════════════════════════════════════════════════════════════════
    # First audit every mechanism that can swallow the toggle, so a failure NAMES the cause
    # instead of just saying "still closed".
    audit = pg.evaluate("""() => {
      const s = document.querySelector('#acq-panel details summary');
      // Scroll it into view before probing: elementFromPoint is viewport-relative and returns null
      // for a point below the fold, which would make the overlay check meaningless (Playwright's
      // own click auto-scrolls, so without this the probe and the click disagree).
      s.scrollIntoView({block: 'center'});
      const r = s.getBoundingClientRect();
      const cx = r.left + Math.min(40, r.width / 2), cy = r.top + r.height / 2;
      const top = document.elementFromPoint(cx, cy);
      const cs = getComputedStyle(s);
      // Walk ancestors for inline handlers / pointer-events / overflow traps.
      const anc = [];
      for (let e = s.parentElement; e && e !== document.body; e = e.parentElement) {
        const ecs = getComputedStyle(e);
        if (e.getAttribute && e.getAttribute('onclick')) anc.push((e.id||e.tagName)+'[onclick]');
        if (ecs.pointerEvents === 'none') anc.push((e.id||e.tagName)+'[pointer-events:none]');
      }
      return {
        hitIsSummary: !!(top && (top === s || s.contains(top))),
        hitEl: top ? (top.tagName + (top.className ? '.' + String(top.className).split(' ')[0] : '')) : null,
        display: cs.display, pointerEvents: cs.pointerEvents, visibility: cs.visibility,
        summaryIsFirstChild: s.parentElement.firstElementChild === s,
        ancestorsWithHandlers: anc,
        zeroSize: r.width === 0 || r.height === 0
      };
    }""")
    check("summary is the element at its own click point (nothing overlays it)",
          audit["hitIsSummary"] is True)
    check("summary has a non-zero hit area", audit["zeroSize"] is False)
    check("summary pointer-events is not disabled", audit["pointerEvents"] != "none")
    check("summary is visible", audit["visibility"] == "visible")
    check("summary is the FIRST child of <details> (required for the native toggle)",
          audit["summaryIsFirstChild"] is True)
    check("no ancestor carries an inline click handler or pointer-events:none",
          audit["ancestorsWithHandlers"] == [])
    if not audit["hitIsSummary"]:
        print("     ! click point actually hits: " + str(audit["hitEl"]))

    # THE PIN: a real click, not a forced property set.
    pg.click("#acq-panel details summary")
    pg.wait_for_timeout(200)
    check("*** a REAL USER CLICK on the summary toggles it OPEN ***",
          pg.evaluate("() => document.querySelector('#acq-panel details').open") is True)
    check("the table is actually visible after that click",
          pg.evaluate("""() => { const t = document.querySelector('#acq-panel details table');
                                 return !!t && t.getBoundingClientRect().height > 0; }""") is True)
    # And clicking again closes it (the control is a real toggle, not one-way).
    pg.click("#acq-panel details summary")
    pg.wait_for_timeout(200)
    check("clicking again CLOSES it (a real toggle)",
          pg.evaluate("() => document.querySelector('#acq-panel details').open") is False)

    # Re-open for the content assertions below.
    pg.click("#acq-panel details summary")
    pg.wait_for_timeout(150)
    tbl = pg.evaluate("() => { const t=document.querySelector('#acq-panel details table'); return t?t.innerText:''; }")

    check("every banded land sale is listed (4 rows)",
          pg.evaluate("() => document.querySelectorAll('#acq-panel details tbody tr').length") == 4)
    for addr in ("6402 Kemrock Dr", "6501 Bonnie View Rd", "1207 Lansing Ave", "6610 Kemrock Dr"):
        check("comp listed: " + addr, addr in tbl)
    check("lot size shown in ACRES", "0.18 ac" in tbl or "0.18 ac" in tbl.replace("0.180", "0.18"))
    check("lot size ALSO shown in SQFT", "7,841 sf" in tbl or "7841" in tbl.replace(",", ""))
    check("close DATE shown", "2026-02-11" in tbl)
    check("reconstructed close PRICE shown", "$72,000" in tbl and "$99,000" in tbl)
    check("$/lot-sqft shown per comp", "$9.18" in tbl or re.search(r"\$\d+\.\d\d", tbl) is not None)

    # ── the reconciliation line — the whole point: the number is checkable on screen ──
    details_txt = pg.evaluate("() => document.querySelector('#acq-panel details').innerText")
    check("RANGE is stated", "$72,000" in details_txt and "$99,000" in details_txt and "Range" in details_txt)
    check("MEDIAN is stated and equals the floor printed above",
          "median $85,500" in details_txt and "= the floor above" in details_txt)
    check("the method is stated — median of actual closes, NOT a per-unit extrapolation",
          "median of these actual closes" in details_txt and "never a per-unit rate" in details_txt)
    check("close prices are labeled RECONSTRUCTED (NTREIS omits ClosePrice)",
          "reconstructed" in details_txt.lower())
    check("display-only caveat is restated in the evidence section",
          "never feeds MAO" in details_txt)

    # ── read-only: no confirm flow until the land/teardown exit mode exists ──
    check("the evidence section is READ-ONLY (no buttons/inputs inside)",
          pg.evaluate("() => document.querySelectorAll('#acq-panel details button, #acq-panel details input').length") == 0)

    # ── a floor with no comps must not claim a set ──
    pg.evaluate("(d) => { const c = JSON.parse(JSON.stringify(d)); c.land_floor.comps = [];"
                "  document.getElementById('acq-panel').innerHTML = _acqHtml('TX-26-01190', c); }", DATA)
    pg.wait_for_timeout(100)
    check("no comps ⇒ no evidence section rendered (never claims a set it lacks)",
          pg.evaluate("() => !document.querySelector('#acq-panel details')"))

    # ══════════════════════════════════════════════════════════════════════════════
    # THE SHIPPED BUG: a background re-render closed the drill-down under a reviewer.
    # Any rebuild of #acq-panel (renderAcquisition on a tab switch/retry, or the sync-time
    # renderDetail that repopulates materialized property_intel) creates a NEW <details>,
    # which defaults to closed — so an open evidence table silently collapsed and it read
    # as "clicking the summary does nothing". The open state must survive a rebuild.
    # ══════════════════════════════════════════════════════════════════════════════
    pg.evaluate("() => { window._landEvidenceOpen = false; }")
    pg.evaluate("(d) => { document.getElementById('acq-panel').innerHTML = _acqHtml('TX-26-01190', d); }", DATA)
    pg.wait_for_timeout(100)
    pg.click("#acq-panel details summary")
    pg.wait_for_timeout(150)
    check("user opens the evidence → open", pg.evaluate("() => document.querySelector('#acq-panel details').open") is True)
    check("...and the intent is recorded outside the DOM (survives a rebuild by construction)",
          pg.evaluate("() => window._landEvidenceOpen") is True)
    # Rebuild the panel exactly as a background sync / tab switch does.
    pg.evaluate("(d) => { document.getElementById('acq-panel').innerHTML = _acqHtml('TX-26-01190', d); }", DATA)
    pg.wait_for_timeout(150)
    check("*** the drill-down STAYS OPEN across a panel rebuild (no silent collapse) ***",
          pg.evaluate("() => document.querySelector('#acq-panel details').open") is True)
    check("...and the table is still on screen after the rebuild",
          pg.evaluate("""() => { const t = document.querySelector('#acq-panel details table');
                                 return !!t && t.getBoundingClientRect().height > 0; }""") is True)
    # Closing it must also stick across a rebuild (the flag tracks the user, not one direction).
    pg.click("#acq-panel details summary")
    pg.wait_for_timeout(150)
    pg.evaluate("(d) => { document.getElementById('acq-panel').innerHTML = _acqHtml('TX-26-01190', d); }", DATA)
    pg.wait_for_timeout(150)
    check("a CLOSED drill-down also stays closed across a rebuild",
          pg.evaluate("() => document.querySelector('#acq-panel details').open") is False)

    check("ZERO page errors", not errors)
    if errors:
        for e in errors[:5]: print("     ! " + e)
    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
