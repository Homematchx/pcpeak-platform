#!/usr/bin/env python3
"""Browser test — the §G LAND COMP WORKBENCH (design §16.9).

WHY: the land floor and the ARV comps do the SAME evidentiary job — "why should I believe this
number?" — so they are held to ONE standard. On a land-dominant subject like Kemrock (TX-26-01190)
the ARV is unavailable and the MAO ladder cannot compute, so the land floor is the ONLY valuation
on the screen; a real capital decision rests on it. A telemetry value an operator cannot verify is
a defect, not a backlog item.

Pins the workbench standard:
  * its own card with the same weight as the ARV comp workbench — NOT a footnote in the Valuation card;
  * EXPANDED by default when the land floor is the only valuation (no ARV);
  * a photo per comp, hotlinked from Media, with an EXPLICIT "no photo" where a Land listing has none;
  * a relevance score (MS) shown like MatchScore, and the set ordered by it, not by price;
  * arm's-length flags spelled out IN WORDS, plus an explicit statement that flagged sales ARE in
    the median, with a sensitivity median excluding them that does NOT replace the floor;
  * a SUBJECT row and a MEDIAN marker so the operator sees where the subject and the floor sit;
  * a REAL USER CLICK toggles it (the earlier suite forced .open and asserted markup — it passed
    against a control no user could reach, which is how the toggle bug shipped);
  * the open/closed state SURVIVES a panel rebuild (a background sync used to collapse it silently);
  * read-only — no confirm/reject until the land/teardown exit mode exists.

Run: python3 test_land_evidence_browser.py   (exit 0 = all green)
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


def land_comp(addr, acres, price, date, flags=None, media=None, score=70):
    sqft = round(acres * 43560)
    return {"address": addr, "lot_acres": acres, "lot_sqft": sqft, "close_price": price,
            "close_date": date, "price_per_lot_sqft": round(price / sqft, 2),
            "match_score": score, "media_urls": media if media is not None else [],
            "qualification": {"arms_length_flags": flags or []}}


# Kemrock-shaped: land-dominant, NO ARV, MAO cannot compute — the floor is the only valuation.
# closes sorted = 72000, 83000, 88000, 99000 → median = (83000+88000)/2 = 85500, an even-n midpoint
# that NO single sale equals — which is why the engine names the bracketing pair.
LAND = {
    "land_floor": 85500, "median": 85500, "label": "estimated", "n": 4,
    "range": [72000, 99000], "spread": 27000,
    "median_bracket": [83000, 88000],
    "subject_lot_acres": 0.166, "lot_band_acres": [0.1162, 0.2158],
    "median_price_per_acre": 373362, "median_price_per_lot_sqft": 8.57,
    "recency_months_used": 12, "recency_widened": False,
    "net_of_demolition": 71500, "demolition_cost": 14000,
    "n_arms_length_flagged": 2, "flagged_in_median": True,
    "median_excluding_flagged": 80000, "n_unflagged": 2,
    "comps": [   # already ranked by relevance (MS desc), as the engine returns them
        land_comp("6402 Kemrock Dr", 0.170, 88000, "2026-05-19", score=95,
                  media=["https://example.invalid/land1.jpg"]),
        land_comp("6501 Bonnie View Rd", 0.150, 72000, "2026-02-11", score=78,
                  flags=["distressed/REO/auction language in remarks"]),
        land_comp("1207 Lansing Ave", 0.195, 99000, "2026-06-30", score=61,
                  flags=["possible family/non-arm's-length"],
                  media=["https://example.invalid/land3.jpg"]),
        land_comp("6610 Kemrock Dr", 0.205, 83000, "2026-04-02", score=54, media=[]),
    ],
}

DATA = {
    "valuation_state": "provisional",
    "land_floor": LAND,
    "proposed_comps": [],
    "n_confirmed_comps": 0,
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

LAND_CARD = "#acq-panel .acq-land"      # own class — positional selectors are fragile
SUMMARY = LAND_CARD + " details summary"

chrome = chrome_path()
if not chrome:
    print("SKIP: no chromium available for this checkout"); sys.exit(0)


def render(pg, data):
    pg.evaluate("(d) => { document.getElementById('acq-panel').innerHTML = _acqHtml('TX-26-01190', d); }", data)
    pg.wait_for_timeout(120)


def card_text(pg):
    return pg.evaluate("() => document.querySelector('%s').innerText" % LAND_CARD)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script("window.localStorage.clear();")
    pg.add_init_script("""
      window.fetch = async function(){ return new Response('[]', {status:200, headers:{'Content-Type':'application/json'}}); };
      window.prompt = function(){ return ''; };
    """)
    pg.goto("file://" + str(HTML))
    pg.wait_for_timeout(400)

    pg.evaluate("""() => {
      cases = [{ id:'TX-26-01190_platform', city:'dallas', rep_assigned:'', uploadedAt:'2026-07-01T00:00:00Z',
                 disposition_state:'active',
                 extracted:{ caseNumber:'TX-26-01190', propertyAddress:'6406 Kemrock Dr', defendant:'K',
                             accountNumber:'1234' } }];
      selectCase('TX-26-01190_platform');
    }""")
    pg.wait_for_timeout(200)
    # Open the Acquisition TAB like a user: #acq-panel sits inside <div class="tp" id="tp-acq">,
    # display:none until the tab carries 'on'. Asserting markup in a hidden panel is how the
    # unusable-toggle bug shipped.
    pg.evaluate("""() => { const b = Array.from(document.querySelectorAll('#cdet .tb'))
                             .find(x => (x.getAttribute('onclick')||'').indexOf("'acq'") >= 0);
                           swTab({target: b}, 'acq'); }""")
    pg.wait_for_timeout(250)
    check("the Acquisition tab panel is VISIBLE (not a hidden tab)",
          pg.evaluate("() => document.getElementById('tp-acq').classList.contains('on')"))
    render(pg, DATA)
    panel = pg.evaluate("() => document.getElementById('acq-panel').innerText")

    # ── (4) its own workbench section, same weight as the ARV workbench ──
    check("a 'Land comp workbench' card exists", "Land comp workbench" in panel)
    check("it is its OWN card, not buried in the Valuation card",
          pg.evaluate("""() => { const h = Array.from(document.querySelectorAll('#acq-panel .acq-card'))
                                   .find(c => c.innerText.indexOf('Land comp workbench') >= 0);
                                 return !!h && h.innerText.indexOf('DCAD market') < 0; }"""))
    check("it uses the SAME comp row markup as the ARV workbench (.acq-comp)",
          pg.evaluate("() => document.querySelectorAll('%s .acq-comp').length" % LAND_CARD) == 5)
    check("the Valuation card no longer carries the evidence table",
          pg.evaluate("""() => { const v = Array.from(document.querySelectorAll('#acq-panel .acq-card'))
                                   .find(c => c.innerText.indexOf('DCAD market') >= 0);
                                 return !!v && !v.querySelector('details'); }"""))
    check("the Land floor headline still shows in the Valuation card", "85,500" in panel)

    # ── expanded by default when the floor is the only valuation ──
    check("*** EXPANDED by default because there is no ARV (only valuation on screen) ***",
          pg.evaluate("() => document.querySelector('%s details').open" % LAND_CARD) is True)
    card = card_text(pg)
    check("...and it says so explicitly", "only valuation on the" in card)
    check("MAO still cannot compute — the floor NEVER feeds it", "Needs a confirmed ARV" in panel)

    # ── (5) subject row + median marker ──
    check("a SUBJECT row is rendered", "SUBJECT" in card)
    check("the subject's lot size is shown in acres AND sqft", "0.166 ac" in card and "7,231 sf" in card)
    # .acq-lbl is uppercased by CSS (same treatment as the ARV workbench labels) — compare
    # case-insensitively so the test pins the WORDS, not the styling.
    check("the subject row is marked as not a sale", "not a sale" in card.lower())
    check("the MEDIAN is marked on the two sales that define it (even-n bracket)",
          pg.evaluate("() => document.querySelectorAll('%s .acq-comp-on').length" % LAND_CARD) == 2)
    check("...labelled as the median bracket", "median bracket" in card.lower())

    # ── (1) photos, with an explicit no-photo label ──
    check("comps WITH media render an inline photo",
          pg.evaluate("() => document.querySelectorAll('%s .acq-comp-ph img').length" % LAND_CARD) == 2)
    check("photos are MLS hotlinks (src is the Media url)",
          pg.evaluate("""() => { const i = document.querySelector('%s .acq-comp-ph img');
                                 return !!i && i.getAttribute('src').indexOf('http') === 0; }""" % LAND_CARD))
    check("a comp with NO media says 'no photo' explicitly (never a blank square)",
          card.count("no photo") == 2)

    # ── (2) relevance score, sorted by it ──
    check("a relevance score (MS) is shown per comp", "MS 95" in card and "MS 54" in card)
    scores = pg.evaluate("""() => Array.from(document.querySelectorAll('%s .acq-comp .acq-note'))
        .map(e => { const m = e.innerText.match(/MS (\\d+)/); return m ? +m[1] : null; })
        .filter(v => v !== null);""" % LAND_CARD)
    check("the set is ordered by RELEVANCE, descending", scores == sorted(scores, reverse=True))
    check("...and that ordering is by MS, not price", scores == [95, 78, 61, 54])

    # ── (3) flags in words + whether they are in the median ──
    check("flags are spelled out IN WORDS, not a bare symbol",
          "distressed/reo/auction language in remarks" in card.lower()
          and "possible family/non-arm's-length" in card.lower())
    check("no bare warning glyph is used as the flag signal", "⚠" not in card)
    check("it STATES that flagged sales are included in the median", "included in the median" in card)
    check("it names how many are flagged", "2 of 4 sales carry an arm" in card)
    check("it gives the sensitivity median excluding flagged sales", "$80,000" in card)
    check("...and says that figure does NOT replace the floor", "does NOT replace the floor" in card)
    check("...and that excluding them is a human decision", "valuation decision for a human" in card)

    # ── per-comp evidence fields ──
    for addr in ("6402 Kemrock Dr", "6501 Bonnie View Rd", "1207 Lansing Ave", "6610 Kemrock Dr"):
        check("comp listed: " + addr, addr in card)
    check("lot size per comp in acres AND sqft", "0.17 ac" in card and "7,405 sf" in card)
    check("close date per comp", "2026-05-19" in card)
    check("reconstructed close price per comp", "$88,000" in card and "$99,000" in card)
    check("$/lot-sqft per comp", "/lot sf" in card)

    # ── reconciliation ──
    check("RANGE is stated", "$72,000" in card and "Range" in card)
    check("MEDIAN is stated and equals the floor", "median $85,500" in card and "= the floor" in card)
    check("method stated — median of actual closes, not a per-unit extrapolation",
          "median of these actual closes" in card and "never a per-unit rate" in card)
    check("close prices labelled RECONSTRUCTED", "reconstructed" in card.lower())
    check("display-only caveat restated", "never feeds MAO" in card)

    # ── read-only ──
    check("READ-ONLY — no confirm/reject controls in the land workbench",
          pg.evaluate("() => document.querySelectorAll('%s button, %s input').length" % (LAND_CARD, LAND_CARD)) == 0)

    # ── a REAL USER CLICK toggles it (the assertion whose absence let the toggle bug ship) ──
    audit = pg.evaluate("""() => {
      const s = document.querySelector('%s');
      s.scrollIntoView({block:'center'});
      const r = s.getBoundingClientRect();
      const top = document.elementFromPoint(r.left + Math.min(40, r.width/2), r.top + r.height/2);
      const cs = getComputedStyle(s);
      const anc = [];
      for (let e = s.parentElement; e && e !== document.body; e = e.parentElement) {
        if (e.getAttribute && e.getAttribute('onclick')) anc.push((e.id||e.tagName)+'[onclick]');
        if (getComputedStyle(e).pointerEvents === 'none') anc.push((e.id||e.tagName)+'[pe:none]');
      }
      return { hitIsSummary: !!(top && (top === s || s.contains(top))),
               hitEl: top ? top.tagName : null, pe: cs.pointerEvents,
               firstChild: s.parentElement.firstElementChild === s,
               zero: r.width === 0 || r.height === 0, anc: anc };
    }""" % SUMMARY)
    check("summary is the element at its own click point (nothing overlays it)", audit["hitIsSummary"] is True)
    check("summary has a non-zero hit area", audit["zero"] is False)
    check("summary pointer-events not disabled", audit["pe"] != "none")
    check("summary is the FIRST child of <details> (required for the native toggle)",
          audit["firstChild"] is True)
    check("no ancestor carries an inline click handler or pointer-events:none", audit["anc"] == [])
    if not audit["hitIsSummary"]:
        print("     ! click point actually hits: " + str(audit["hitEl"]))

    pg.click(SUMMARY); pg.wait_for_timeout(200)
    check("*** a REAL USER CLICK on the summary CLOSES it (it starts open here) ***",
          pg.evaluate("() => document.querySelector('%s details').open" % LAND_CARD) is False)
    pg.click(SUMMARY); pg.wait_for_timeout(200)
    check("clicking again RE-OPENS it (a real toggle)",
          pg.evaluate("() => document.querySelector('%s details').open" % LAND_CARD) is True)
    check("the comp rows are actually on screen after that click",
          pg.evaluate("""() => { const c = document.querySelector('%s .acq-comp');
                                 return !!c && c.getBoundingClientRect().height > 0; }""" % LAND_CARD) is True)

    # ── the shipped bug: state must survive a panel rebuild (background sync / tab switch) ──
    check("the user's intent is recorded OUTSIDE the DOM", pg.evaluate("() => window._landEvidenceOpen") is True)
    render(pg, DATA)
    check("*** the drill-down STAYS OPEN across a panel rebuild (no silent collapse) ***",
          pg.evaluate("() => document.querySelector('%s details').open" % LAND_CARD) is True)
    pg.click(SUMMARY); pg.wait_for_timeout(150)
    render(pg, DATA)
    check("a CLOSED drill-down also stays closed across a rebuild",
          pg.evaluate("() => document.querySelector('%s details').open" % LAND_CARD) is False)

    # ── with an ARV present the floor is NOT the only valuation → collapsed by default ──
    pg.evaluate("() => { window._landEvidenceOpen = undefined; }")
    with_arv = json.loads(json.dumps(DATA))
    with_arv["analysis"]["arv"] = {"value": 219000, "label": "verified"}
    render(pg, with_arv)
    check("with an ARV available it is COLLAPSED by default (evidence on demand)",
          pg.evaluate("() => document.querySelector('%s details').open" % LAND_CARD) is False)
    check("...and the 'only valuation' banner is NOT shown", "only valuation on the" not in card_text(pg))

    # ── no comps ⇒ no workbench at all (never claim a set it lacks) ──
    pg.evaluate("(d) => { const c = JSON.parse(JSON.stringify(d)); c.land_floor.comps = [];"
                "  document.getElementById('acq-panel').innerHTML = _acqHtml('TX-26-01190', c); }", DATA)
    pg.wait_for_timeout(120)
    check("no comps ⇒ no land workbench rendered",
          "Land comp workbench" not in pg.evaluate("() => document.getElementById('acq-panel').innerText"))

    check("ZERO page errors", not errors)
    if errors:
        for e in errors[:5]: print("     ! " + e)
    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
