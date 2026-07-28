#!/usr/bin/env python3
"""Frontend caseStage() — the DATE-AWARE stage derivation. No network.

A pull is USUALLY the latest event and supersedes an OOS, but a sale can be pulled and then the
Order of Sale RE-ISSUED — TX-23-00569: pull 2026-05-12, then OOS 2026-07-24. When both dates are
known the LATEST event wins, so a re-issued OOS reads as oos_issued again. Pins that this does NOT
regress the original 'pull is latest' behaviour, and that live cases (platformToV3 does not map the
pulled fields) still fall through to oos_issued as before — a safe superset.

Run: python3 test_case_stage.py   (exit 0 = all green)
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
from browser_env import chrome_path

HTML = Path("frontend/index.html").resolve()

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)

CASES = {
    # OOS re-issued AFTER the pull → oos_issued (the TX-23-00569 fix)
    "reissued":     ({"orderOfSaleIssued": True, "orderOfSaleDate": "2026-07-24", "sale_pulled_date": "2026-05-12"}, "oos_issued"),
    # original pull AFTER the OOS → sale_pulled (unchanged)
    "origPull":     ({"orderOfSaleIssued": True, "orderOfSaleDate": "2026-04-20", "sale_pulled_date": "2026-05-12"}, "sale_pulled"),
    # stale stage flag, no dates → sale_pulled (belt-and-suspenders)
    "stageOnly":    ({"stage": "sale_pulled"}, "sale_pulled"),
    # plain OOS, never pulled → oos_issued
    "plainOOS":     ({"orderOfSaleIssued": True, "orderOfSaleDate": "2026-06-01"}, "oos_issued"),
    # a live case: platformToV3 doesn't map pulled fields → oos_issued (safe superset, unchanged)
    "liveNoFields": ({"orderOfSaleIssued": True, "orderOfSaleDate": "2026-07-24"}, "oos_issued"),
    # same-day pull (pull >= OOS) → sale_pulled (withdrawal supersedes)
    "sameDay":      ({"orderOfSaleIssued": True, "orderOfSaleDate": "2026-08-01", "sale_pulled_date": "2026-08-01"}, "sale_pulled"),
    "judged":       ({"judgmentDate": "2026-01-07"}, "judgment_entered"),
    "prej":         ({}, "pre_judgment"),
}

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
    for name, (ex, want) in CASES.items():
        got = pg.evaluate("(ex) => caseStage(ex)", ex)
        check(f"caseStage {name} → {want}", got == want)
    b.close()

print("-" * 60)
print(f"{sum(_res)}/{len(_res)} passed")
sys.exit(0 if all(_res) else 1)
