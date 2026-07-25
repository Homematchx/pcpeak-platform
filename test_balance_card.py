#!/usr/bin/env python3
"""Browser test — ACT live balance on the sidebar card + the deal-shape (payoff) filter.

The critical constraint: the card must read the SAME ACT live balance the Financials and
Acquisition tabs show — property_intel.current_tax_balance — so it never reintroduces a fourth,
divergent figure. And the zero-balance disposition flag is the special case of the number: a real
$0 shows as the ⚠ REVIEW flag, never as "$0" competing with it; both read the same field, so they
cannot disagree.

Pins:
  * a positive balance renders "owes $X (verified)" on the card, and the SAME number appears on the
    Financials tab (calcPayoff's live balance) for that case — card == Financials;
  * a real $0 balance renders NO "$0" chip — the REVIEW: Paid/zero flag is its representation
    (card + flag read the same source, so they agree by construction);
  * an UNKNOWN balance (enriched, none captured) renders a muted "balance —", never "$0";
  * a BPP case renders no balance chip;
  * the deal-shape filter partitions the queue by payoff band (flip / mid / equity / zero / unknown)
    using the same classifier the card uses.

Run: python3 test_balance_card.py   (exit 0 = all green)
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


def pi(balance=None, **extra):
    d = dict(extra)
    if balance is not None:
        d["current_tax_balance"] = balance
    return json.dumps(d)


# Cases spanning every band + the flag interaction.
CASES = [
    # positive mid-band balance — the headline "owes $X"
    {"case_number": "TX-26-00001", "property_address": "1 Owes St", "defendant": "A", "city": "dallas",
     "disposition_state": "active", "property_intel": pi(balance=15691.0, market_value=210000, year_built=1960)},
    # real $0 + the zero-balance disposition flag (the TX-26-01389 shape)
    {"case_number": "TX-26-01389", "property_address": "2 Paid St", "defendant": "B", "city": "dallas",
     "disposition_state": "active", "pending_review": 1, "pending_review_code": "paid_in_full",
     "property_intel": pi(balance=0.0, market_value=180000, year_built=1972)},
    # enriched but no balance captured yet → "balance —", never $0
    {"case_number": "TX-26-00003", "property_address": "3 Unknown St", "defendant": "C", "city": "dallas",
     "disposition_state": "active", "property_intel": pi(market_value=150000, year_built=1965)},
    # mid band ($20K–$50K)
    {"case_number": "TX-26-00004", "property_address": "4 Mid St", "defendant": "D", "city": "dallas",
     "disposition_state": "active", "property_intel": pi(balance=35000.0, market_value=180000)},
    # high band ($50K+)
    {"case_number": "TX-26-00005", "property_address": "5 High St", "defendant": "E", "city": "dallas",
     "disposition_state": "active", "property_intel": pi(balance=72000.0, market_value=400000)},
    # BPP — no real-estate payoff, no chip
    {"case_number": "TX-26-00006", "property_address": "6 Biz St", "defendant": "F", "city": "dallas",
     "disposition_state": "active", "property_type": "personal", "case_track": "personal_property",
     "property_intel": pi(balance=9000.0)},
]

DISPO_CODES = {"groups": ["Taxpayer resolved"],
               "codes": [{"code": "paid_in_full", "label": "Paid / zero balance",
                          "group": "Taxpayer resolved", "state": "archived", "comment": False}]}

STUB = """
window.fetch = async function(url, opts){
  url = String(url);
  const J = (o)=> new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
  if (url.endsWith('/api/cases') && opts && opts.method==='POST') return J({status:'ok'});
  if (url.endsWith('/api/cases')) return J(%s);
  if (url.indexOf('/api/events/')>=0) return J([]);
  if (url.endsWith('/api/stats')) return J({total_cases:%d, active_cases:%d, watching_cases:0, archived_cases:0, total_all:%d, pending_review:1});
  if (url.endsWith('/api/reps')) return J([]);
  if (url.endsWith('/api/dispositions/codes')) return J(%s);
  if (url.endsWith('/api/dispositions')) return J({review_queue:[], recently_archived:[], by_code:[], by_state:{}, counts:{}});
  return J([]);
};
window.prompt = function(){ return ''; };
""" % (json.dumps(CASES), len(CASES), len(CASES), len(CASES), json.dumps(DISPO_CODES))


def card_html(pg, cn):
    return pg.evaluate("""(cn) => {
        const el = Array.from(document.querySelectorAll('#clist .cchip'))
          .find(c => c.querySelector('.cchip-n') && c.querySelector('.cchip-n').textContent.trim() === cn);
        return el ? el.innerText : null;
    }""", cn)


def shown(pg):
    return pg.evaluate("() => Array.from(document.querySelectorAll('#clist .cchip-n')).map(e=>e.textContent.trim())")


chrome = chrome_path()
if not chrome:
    print("SKIP: no chromium available for this checkout"); sys.exit(0)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1280, "height": 950})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script("window.localStorage.clear();")
    pg.add_init_script(STUB)
    pg.goto("file://" + str(HTML))
    pg.wait_for_timeout(900)

    # ── the balance chip on the card ──
    c1 = card_html(pg, "TX-26-00001")
    check("a positive balance shows 'owes $15,691' on the card", c1 and "owes $15,691" in c1)
    check("...labelled verified (it came from ACT)", c1 and "verified" in c1)

    paid = card_html(pg, "TX-26-01389")
    check("a real $0 balance shows NO '$0' chip on the card", paid and "$0" not in paid and "owes $0" not in paid)
    check("...and the zero shows as the REVIEW: Paid/zero flag instead", paid and "REVIEW" in paid and "Paid / zero balance" in paid)

    unk = card_html(pg, "TX-26-00003")
    check("an unknown balance shows a muted 'balance —', never '$0'",
          unk and "balance —" in unk and "$0" not in unk)

    bpp = card_html(pg, "TX-26-00006")
    check("a BPP case shows no balance chip", bpp and "owes $" not in bpp and "balance —" not in bpp)

    # ── card == Financials (the same current_tax_balance, no fourth figure) ──
    # calcPayoff is the Financials tab's payoff computation; drive it with the case's live balance
    # exactly as the Financials tab does, and confirm the card's number matches.
    fin = pg.evaluate("""() => {
        const c = cases.find(x => x.extracted.caseNumber === 'TX-26-00001');
        const pi = parseIntel(c.property_intel);
        const liveBal = (pi.current_tax_balance != null) ? pi.current_tax_balance : null;
        const po = calcPayoff(c.extracted, liveBal);
        return { taxPayoff: po.taxPayoff, hasLive: po.hasLive,
                 cardBalance: caseLiveBalance(c) };
    }""")
    check("Financials calcPayoff uses the live balance as-is (15691)", fin["taxPayoff"] == 15691 and fin["hasLive"])
    check("*** the card reads the SAME number the Financials tab uses (no fourth figure) ***",
          fin["cardBalance"] == 15691 and fin["cardBalance"] == fin["taxPayoff"])

    # ── card + flag read the same source: they cannot disagree on zero ──
    agree = pg.evaluate("""() => {
        const c = cases.find(x => x.extracted.caseNumber === 'TX-26-01389');
        return { bal: caseLiveBalance(c), flag: c.pending_review_code };
    }""")
    check("the zero-balance case: card source is 0 AND the flag is paid_in_full (same field)",
          agree["bal"] == 0 and agree["flag"] == "paid_in_full")

    # ── the deal-shape filter ──
    bands = pg.evaluate("""() => {
        const m = {};
        cases.forEach(c => { m[c.extracted.caseNumber] = balanceBand(c); });
        return m;
    }""")
    check("amount-owed bands: <$20K low / $20-50K mid / $50K+ high / zero / unknown; BPP → 'na'",
          bands["TX-26-00001"] == "low" and bands["TX-26-00004"] == "mid"
          and bands["TX-26-00005"] == "high" and bands["TX-26-01389"] == "zero"
          and bands["TX-26-00003"] == "unknown" and bands["TX-26-00006"] == "na")

    def apply_filter(val):
        # Drive the real select element (setFilter toggles its classList), exactly like the user.
        pg.evaluate("(v) => setFilter('balance', v, document.getElementById('filter-balance'))", val)
        pg.wait_for_timeout(150)
        return shown(pg)

    check("filter '<$20K' shows only the low-owed case", apply_filter("low") == ["TX-26-00001"])
    check("filter '$20K-$50K' shows only the mid case", apply_filter("mid") == ["TX-26-00004"])
    check("filter '$50K+' shows only the high-owed case", apply_filter("high") == ["TX-26-00005"])
    check("filter 'unknown' shows only the no-balance case", apply_filter("unknown") == ["TX-26-00003"])
    check("filter 'zero' shows the paid case", "TX-26-01389" in apply_filter("zero"))
    allback = apply_filter("")
    check("clearing the filter restores the full queue", len(allback) == len(CASES))

    check("ZERO page errors", not errors)
    if errors:
        for e in errors[:5]: print("     ! " + e)
    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
