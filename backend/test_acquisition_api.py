#!/usr/bin/env python3
"""Tests for the Acquisition Intelligence API (Stage 2 workbench). No network — the NTREIS fetch is
replaced by main._COMP_SOURCE (a stub returning verified CMA-equivalent comps for each subject).

Proves the acceptance criteria: confirming CMA-equivalent comps through the workbench produces
confirmed ARVs and the pinned verdicts —
  • Grant St (TX-25-00249) → NO-GO (structurally unclosable at the confirmed ARV),
  • Tryon (TX-23-00423)   → GO (closable, clean title),
  • Ruby (TX-26-01379)    → GO-WITH-CONDITIONS, held INDETERMINATE by the identified-but-unquantified
                             LLC lien REGARDLESS of valuation state (confirmed) — never a plain GO.
Plus: fail-closed auth, confirmed comps FROZEN at confirmation, pendings directional-only.

Run: python3 backend/test_acquisition_api.py   (exit 0 = all green)
"""
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

_d = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent))
os.environ["ACQUISITION_TOKEN"] = "test-acq-token"
import main
main.DB_PATH = _d / "pcpeak.db"
main.LEDGER_DB_PATH = _d / "ledger.db"
from fastapi.testclient import TestClient

TOK = {"x-acquisition-token": "test-acq-token"}
RECENT = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()

_res = []
def check(name, cond):
    _res.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)


def _comp(mls, gla, close, sub, beds, baths, year, photos=20):
    return {"mls_id": mls, "gla": gla, "close_price": close, "close_date": RECENT, "list_price": close,
            "beds": beds, "baths": baths, "year_built": year, "subdivision": sub, "lat": None, "lng": None,
            "lot_acres": 0.15, "arms_length_flags": [], "media_urls": [f"https://cdn/{mls}.jpg"],
            "photos_count": photos, "address": f"{mls} Comp St", "listing_status": "closed"}


def _stub_source(subject):
    cn = subject["case_number"]
    if cn == "TX-23-00423":   # Tryon — Forest Grove, ~$225K
        closed = [_comp("T1", 1014, 225000, "Forest Grove", 3, 2, 1983),
                  _comp("T2", 1000, 220000, "Forest Grove 07", 3, 2, 1985),
                  _comp("T3", 1030, 230000, "Forest Grove", 3, 2, 1980)]
        pending = [{"mls_id": "TP1", "gla": 1020, "list_price_directional": 240000, "close_price": None,
                    "close_date": None, "beds": 3, "baths": 2, "year_built": 1982, "subdivision": "Forest Grove",
                    "media_urls": [], "photos_count": 5, "address": "Pending Ct", "listing_status": "pending"}]
        return {"closed": closed, "pending": pending}
    if cn == "TX-25-00249":   # Grant St — Mountain Lakeview $237/sf
        return {"closed": [_comp("G1", 1055, 250000, "Mountain Lakeview", 3, 2, 1971)], "pending": []}
    if cn == "TX-26-01379":   # Ruby — as-is ~$133K
        return {"closed": [_comp("R1", 1000, 138500, "Lagow School", 3, 2, 1953),
                           _comp("R2", 1050, 130000, "Sunrise Heights", 2, 1, 1945)], "pending": []}
    return {"closed": [], "pending": []}


def _pi(**kw):
    base = dict(distress={"level": "high", "signals": [{"type": "no_homestead"}]}, no_homestead=True)
    base.update(kw)
    return json.dumps(base)


def seed():
    c = TestClient(main.app)
    # Tryon
    c.post("/api/cases", json={"case_number": "TX-23-00423", "property_address": "10842 Addie Road, Dallas, TX 75217-3536",
        "total_due_filing": 40443.06, "filed_date": "2023-03-09", "judgment_date": "2026-03-18", "property_type": "real",
        "property_intel": _pi(market_value=217800, current_tax_balance=71938.09, living_area_sqft=1014, bedrooms=3,
            bathrooms="", year_built=1983, depreciation_pct=40, actual_age=43, lot_area_sqft=7676,
            legal_description="1: FOREST GROVE NO 5 2: BLK 10/6688 LOT 22 3:", owners=[{"name": "TRYON CHARLIE B"}])})
    # Grant St
    c.post("/api/cases", json={"case_number": "TX-25-00249", "property_address": "2457 Grant St., Grand Prairie, TX 75051-5534",
        "total_due_filing": 80583.24, "filed_date": "2025-02-13", "judgment_date": "2026-07-07", "property_type": "real",
        "property_intel": _pi(market_value=232800, current_tax_balance=152224.40, living_area_sqft=1125, bedrooms=3,
            bathrooms="1/1", year_built=1978, depreciation_pct=45, actual_age=48, lot_area_sqft=4878,
            legal_description="1: MOUNTAIN LAKEVIEW 3 2: BLK 19 LOT 16 3:", owners=[{"name": "MIDDLETON MICHAEL"}])})
    # Ruby (DCAD owner ≠ defendant; absentee)
    c.post("/api/cases", json={"case_number": "TX-26-01379", "property_address": "4227 York St., Dallas, TX 75210-1741",
        "total_due_filing": 11437.29, "filed_date": "2026-07-06", "property_type": "real",
        "property_intel": _pi(market_value=143320, current_tax_balance=11437.29, living_area_sqft=1077, bedrooms=2,
            bathrooms="1/0", year_built=1947, depreciation_pct=60, actual_age=79, lot_area_sqft=5310, is_absentee=True,
            legal_description="1: WALLS H G 2: BLK B/4773 LT 8 3:", owners=[{"name": "TAYLOR FELICIA D"}])})
    return c


def run():
    main.init_db()
    main._COMP_SOURCE = _stub_source
    c = seed()

    # ── fail-closed auth ──
    check("GET acquisition without token → 401", c.get("/api/cases/TX-23-00423/acquisition").status_code == 401)
    check("propose without token → 401", c.post("/api/cases/TX-23-00423/comps/propose").status_code == 401)
    os.environ.pop("ACQUISITION_TOKEN")
    check("token unset → 503 (fail-closed)", c.get("/api/cases/TX-23-00423/acquisition", headers=TOK).status_code == 503)
    os.environ["ACQUISITION_TOKEN"] = "test-acq-token"

    # ── propose: closed + pending, pendings directional-only ──
    p = c.post("/api/cases/TX-23-00423/comps/propose", headers=TOK).json()
    check("propose returns closed comps", any(x["listing_status"] == "closed" for x in p["comps"]))
    check("propose returns pending (directional)", any(x["listing_status"] == "pending" for x in p["comps"]))
    check("pending has no close_price (never in ARV)",
          all(x["close_price"] is None for x in p["comps"] if x["listing_status"] == "pending"))
    check("provisional ARV present pre-confirmation", p["provisional_arv"] is not None)
    pre = c.get("/api/cases/TX-23-00423/acquisition", headers=TOK).json()
    check("pre-confirmation valuation is PROVISIONAL", pre["valuation_state"] == "provisional")

    # ── pending cannot be confirmed into the ARV ──
    check("confirming a pending → 400",
          c.post("/api/cases/TX-23-00423/comps/TP1/confirm", headers=TOK).status_code == 400)

    # ── TRYON → GO: confirm the 3 Forest Grove comps + set clean title / agreed price ──
    for m in ("T1", "T2", "T3"):
        c.post(f"/api/cases/TX-23-00423/comps/{m}/confirm", headers=TOK)
    c.post("/api/cases/TX-23-00423/acquisition", headers=TOK,
           json={"lien_status": "verified", "lien_stack": [], "agreed_price": 108000})
    t = c.get("/api/cases/TX-23-00423/acquisition", headers=TOK).json()
    check("Tryon valuation flips PROVISIONAL→CONFIRMED on human confirmation", t["valuation_state"] == "confirmed")
    check("Tryon confirmed ARV ≈ $225K CMA", 218000 <= t["confirmed_arv"] <= 232000)
    check("Tryon ARV label is VERIFIED (§5.4)", t["analysis"]["arv"]["label"] == "verified")
    check("Tryon verdict GO", t["decision"] == "GO")

    # ── GRANT ST → NO-GO: confirm the Mountain Lakeview comp; structurally unclosable at confirmed ARV ──
    c.post("/api/cases/TX-25-00249/comps/propose", headers=TOK)
    c.post("/api/cases/TX-25-00249/comps/G1/confirm", headers=TOK)
    g = c.get("/api/cases/TX-25-00249/acquisition", headers=TOK).json()
    check("Grant confirmed ARV ≈ $253-257K (same-subdivision)", 250000 <= g["confirmed_arv"] <= 260000)
    check("Grant valuation confirmed", g["valuation_state"] == "confirmed")
    check("Grant structurally_unclosable gate fires",
          any(x["gate"] == "structurally_unclosable" for x in g["analysis"]["gates"]))
    check("Grant verdict NO-GO", g["decision"] == "NO-GO")

    # ── RUBY → GO-WITH-CONDITIONS, held INDETERMINATE by the unquantified lien, regardless of valuation ──
    c.post("/api/cases/TX-26-01379/comps/propose", headers=TOK)
    for m in ("R1", "R2"):
        c.post(f"/api/cases/TX-26-01379/comps/{m}/confirm", headers=TOK)
    # as-is/assignment exit (repairs 0) + the identified-but-UNQUANTIFIED LLC interest
    c.post("/api/cases/TX-26-01379/acquisition", headers=TOK, json={"repair_estimate": 0, "lien_status": "partial",
        "lien_stack": [{"type": "llc_interest", "amount": None,
                        "holder": "Unknown Shareholders/Successors/Assigns of Mesquite NF SNF, LLC"}]})
    r = c.get("/api/cases/TX-26-01379/acquisition", headers=TOK).json()
    check("Ruby valuation CONFIRMED (comps confirmed)", r["valuation_state"] == "confirmed")
    check("Ruby held by identified_unquantified_lien gate",
          any(x["gate"] == "identified_unquantified_lien" for x in r["analysis"]["gates"]))
    check("Ruby closability INDETERMINATE", r["analysis"]["seller_net_sheet"]["closable"] is None)
    check("Ruby verdict GO-WITH-CONDITIONS", r["decision"] == "GO-WITH-CONDITIONS")
    check("Ruby NEVER a plain GO despite confirmed valuation", r["decision"] != "GO")

    # ── confirmed comps are FROZEN: a later re-propose with a changed price does not move a confirmed ARV ──
    def _cheaper(subject):
        return {"closed": [_comp("G1", 1055, 150000, "Mountain Lakeview", 3, 2, 1971)], "pending": []}
    main._COMP_SOURCE = _cheaper
    c.post("/api/cases/TX-25-00249/comps/propose", headers=TOK)   # G1 re-proposed cheaper
    g2 = c.get("/api/cases/TX-25-00249/acquisition", headers=TOK).json()
    check("confirmed ARV unchanged after re-propose (comp frozen at confirmation)",
          g2["confirmed_arv"] == g["confirmed_arv"])
    main._COMP_SOURCE = _stub_source

    # ── restore guard: DELETE on a prod-owned acquisition table is denied at the engine ──
    denied = False
    try:
        with main.get_db() as db:
            db.execute("DELETE FROM ledger.comp_confirmations WHERE case_number='TX-23-00423'")
    except Exception:
        denied = True
    check("restore guard denies DELETE on comp_confirmations", denied)

    print(f"\n{sum(_res)}/{len(_res)} checks passed" + ("" if all(_res) else "  — FAILURES ABOVE"))
    return all(_res)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
