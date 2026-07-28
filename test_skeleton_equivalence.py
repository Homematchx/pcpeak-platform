#!/usr/bin/env python3
"""Phase 2 equivalence proof — the refactored list helpers read the promoted column with a blob
fallback, and return BYTE-IDENTICAL results to the pre-refactor blob-parse on the same case. No network.

This is the safety proof before Phase 3 flips off the blob: Phase 2 changes NOTHING for the current
data shape. We compare the LIVE helpers (caseLiveBalance / balanceBand / caseTrack, column-first) to a
faithful copy of the PRE-refactor helpers (blob-only) across a matrix of cases:

  * column + blob present and in lockstep (the normal post-Phase-1 case) → identical;
  * blob only, no column (a local draft) → identical (the fallback path);
  * a real 0.0 vs unknown (None) → identical, no falsy conflation;
  * dismissed cases where the track depends on the balance → identical track;
  * column ONLY, no blob (the Phase-3 skeleton shape) → the NEW helper works where the OLD one
    could not — that's the whole point of the phase, verified as the improvement, not a regression.

Run: python3 test_skeleton_equivalence.py   (exit 0 = all green)
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


def blob(balance=None, mv=None):
    d = {}
    if balance is not None: d["current_tax_balance"] = balance
    if mv is not None: d["market_value"] = mv
    return json.dumps(d)


# The PRE-refactor helpers (blob-only), copied verbatim, defined INSIDE the comparison evaluate
# (Playwright doesn't reliably persist window functions across separate evaluate calls). They use
# the page's parseIntel + the live caseTrack, both in scope inside the evaluate.
COMPARE_JS = """(matrix) => {
  const oldBal = (c) => {
    var pi = (c && c.property_intel) ? parseIntel(c.property_intel) : null;
    var b = pi ? pi.current_tax_balance : null;
    return (typeof b === "number") ? b : null;
  };
  const oldBand = (c) => {
    if (caseTrack(c) === "personal_property") return "na";
    var b = oldBal(c);
    if (b === null) return "unknown";
    if (b <= 0) return "zero";
    if (b < 20000) return "low";
    if (b < 50000) return "mid";
    return "high";
  };
  const oldTrack = (c) => {
    if(c.case_track) return c.case_track;
    var ex=c.extracted||{};
    var pi=parseIntel(c.property_intel);
    if(c.property_type==="personal") return "personal_property";
    var oos=ex.orderOfSaleDate||c.oos_date;
    if(oos && (""+oos).trim()) return "oos_timing";
    var jt=((ex.judgmentType||c.judgment_type||"")+"").toUpperCase();
    if(/DISMISS|NON-?SUIT/.test(jt)){
      var bal=pi.current_tax_balance;
      if(bal==null) return "dismissed_owing";
      return bal>0 ? "dismissed_owing" : "dismissed_paid";
    }
    var jd=ex.judgmentDate||c.judgment_date;
    var hasJ=(jd&&(""+jd).trim())||(jt&&jt!=="NONE"&&jt!=="");
    return hasJ ? "judged_pending" : "active";
  };
  return matrix.map(c => ({
    cn: (c.extracted && c.extracted.caseNumber) || c.id,
    newBal: caseLiveBalance(c),  oldBal: oldBal(c),
    newBand: balanceBand(c),     oldBand: oldBand(c),
    newTrack: caseTrack(c),      oldTrack: oldTrack(c)
  }));
}"""

# The equivalence matrix — every case here carries the BLOB (the current world), so old==new must hold.
def case(cn, **kw):
    d = {"id": cn + "_platform", "extracted": {"caseNumber": cn}, "property_type": ""}
    d.update(kw); return d

MATRIX = [
    # column + blob in lockstep, a normal owing case
    case("A", property_intel=blob(15691.0, 210000), current_tax_balance=15691.0, market_value=210000),
    # dismissed + owing (track depends on balance)
    case("B", property_intel=blob(7200.0), current_tax_balance=7200.0,
         extracted={"caseNumber": "B", "judgmentType": "NON-SUIT/DISMISSAL"}),
    # dismissed + real $0 (dismissed_paid — the falsy-conflation guard)
    case("C", property_intel=blob(0.0), current_tax_balance=0.0,
         extracted={"caseNumber": "C", "judgmentType": "NON-SUIT/DISMISSAL"}),
    # dismissed + unknown balance (no balance anywhere)
    case("D", property_intel=blob(), current_tax_balance=None,
         extracted={"caseNumber": "D", "judgmentType": "NON-SUIT/DISMISSAL"}),
    # high band
    case("E", property_intel=blob(82000.0), current_tax_balance=82000.0),
    # a LOCAL DRAFT: blob present, NO column (undefined) → fallback path must match old
    {"id": "F", "inputMethod": "quick", "property_type": "",
     "extracted": {"caseNumber": "F"}, "property_intel": blob(11000.0)},
    # BPP
    case("G", property_type="personal", property_intel=blob(9000.0), current_tax_balance=9000.0),
]

chrome = chrome_path()
if not chrome:
    print("SKIP: no chromium available for this checkout"); sys.exit(0)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
    pg = b.new_page()
    pg.add_init_script("window.fetch=async()=>new Response('[]',{status:200,headers:{'Content-Type':'application/json'}});"
                       "window.prompt=()=>'';window.localStorage.clear();")
    pg.goto("file://" + str(HTML))
    pg.wait_for_timeout(500)

    r = pg.evaluate(COMPARE_JS, MATRIX)

    for row in r:
        cn = row["cn"]
        check(f"{cn}: caseLiveBalance identical (new {row['newBal']} == old {row['oldBal']})",
              row["newBal"] == row["oldBal"])
        check(f"{cn}: balanceBand identical (new {row['newBand']} == old {row['oldBand']})",
              row["newBand"] == row["oldBand"])
        check(f"{cn}: caseTrack identical (new {row['newTrack']} == old {row['oldTrack']})",
              row["newTrack"] == row["oldTrack"])

    # specific value pins (not just equality — the actual right answers)
    by = {x["cn"]: x for x in r}
    check("A owing $15,691 → low band (< $20K), balance 15691",
          by["A"]["newBand"] == "low" and by["A"]["newBal"] == 15691.0)
    check("B dismissed+owing → dismissed_owing", by["B"]["newTrack"] == "dismissed_owing")
    check("C dismissed+$0 → dismissed_paid (real 0, not unknown)", by["C"]["newTrack"] == "dismissed_paid")
    check("D dismissed+unknown → dismissed_owing (surface the lead)", by["D"]["newTrack"] == "dismissed_owing")
    check("F local draft (blob, no column) → fallback gives 11000/low", by["F"]["newBal"] == 11000.0 and by["F"]["newBand"] == "low")

    # THE PHASE-3 SHAPE: column present, blob ABSENT. New works; old could not — the improvement.
    skel = pg.evaluate("""() => {
        const c = { id:'SKEL_platform', extracted:{caseNumber:'SKEL'}, property_type:'',
                    current_tax_balance: 15691.0 };   // no property_intel at all
        const oldBal = (x) => { var pi=(x&&x.property_intel)?parseIntel(x.property_intel):null;
                                var b=pi?pi.current_tax_balance:null; return (typeof b==='number')?b:null; };
        return { newBal: caseLiveBalance(c), oldBal: oldBal(c), newBand: balanceBand(c) };
    }""")
    check("SKELETON (column, no blob): NEW reads 15691 where OLD returns null",
          skel["newBal"] == 15691.0 and skel["oldBal"] is None)
    check("SKELETON: balanceBand works off the column alone", skel["newBand"] == "low")

    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
