"""
test_comps.py — offline unit tests for the NTREIS/Bridge comp engine (comps.py).

Runs OFFLINE with SYNTHETIC fixtures — NO live API, and deliberately NO committed MLS records
(licensing: we hotlink photos and do not store feed data). Pins the pure math: close-price
reconstruction, Haversine, qualification (GLA band / recency / distance / min price), itemized
adjustments, MatchScore ordering, and the provisional-ARV aggregation + labeling.

The live CMA validation (engine ARV vs the human CMAs) is a separate, live-API step recorded in the
Stage-2 report; it is not run here (non-deterministic, and must not persist MLS data).

Run: python3 test_comps.py  →  "N/N".
"""
import datetime

import comps

AS_OF = datetime.date(2026, 7, 19)
_p = _f = 0


def check(name, cond, got=None):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  ✗ FAIL: {name}" + (f"  (got {got!r})" if got is not None else ""))


def rec(**kw):
    """Build a synthetic RESO-shaped record (only the fields the engine reads)."""
    d = {"LotSizeAcres": 0.2, "NTREIS2_RATIO_ClosePrice_By_LotSizeAcres": None}
    d.update(kw)
    return d


# ── reconstruction (Phase-0 verified mechanism) ──────────────────────────────────────────────────
def test_reconstruct_close_price():
    r = rec(LotSizeAcres=0.2, NTREIS2_RATIO_ClosePrice_By_LotSizeAcres=1_000_000)
    check("recon = ratio × acres", comps.reconstruct_close_price(r) == 200000, comps.reconstruct_close_price(r))
    check("recon None when ratio missing", comps.reconstruct_close_price(rec(LotSizeAcres=0.2)) is None)
    check("recon None when acres missing",
          comps.reconstruct_close_price(rec(LotSizeAcres=None, NTREIS2_RATIO_ClosePrice_By_LotSizeAcres=5)) is None)


def test_land_value_shared_path():
    # §G land/teardown uses the SAME field × the SUBJECT's acreage.
    r = rec(NTREIS2_RATIO_ClosePrice_By_LotSizeAcres=300000)
    check("land value = ratio × subject acres", comps.land_value_from_comp(r, 0.15) == 45000, comps.land_value_from_comp(r, 0.15))


def test_normalize_media_and_flags():
    r = rec(ListingId="X1", LivingArea=1000, NTREIS2_RATIO_ClosePrice_By_LotSizeAcres=1_000_000,
            Media=[{"MediaURL": "https://cdn/x1.jpg"}, {"MediaURL": "https://cdn/x2.jpg"}],
            PublicRemarks="Sold AS-IS, investor special, foreclosure")
    n = comps.normalize(r)
    check("normalize close price reconstructed", n["close_price"] == 200000)
    check("normalize hotlinks media urls", n["media_urls"] == ["https://cdn/x1.jpg", "https://cdn/x2.jpg"])
    check("normalize flags distressed remarks", any("distressed" in f for f in n["arms_length_flags"]))


# ── qualification ────────────────────────────────────────────────────────────────────────────────
SUBJ = dict(gla=1000, beds=3, baths=2, year_built=1980, lot_acres=0.2, subdivision="Oak Cliff",
            lat=32.75, lng=-96.83)


def _comp(gla=1000, close=200000, days_ago=30, dist=None, beds=3, baths=2, year=1980, sub="Oak Cliff",
          lat=None, lng=None, flags=None):
    close_date = (AS_OF - datetime.timedelta(days=days_ago)).isoformat()
    return {"mls_id": "C", "gla": gla, "close_price": close, "close_date": close_date, "beds": beds,
            "baths": baths, "year_built": year, "subdivision": sub, "lat": lat, "lng": lng,
            "lot_acres": 0.2, "arms_length_flags": flags or []}


def test_qualify_gla_band():
    check("in-band GLA qualifies", comps.qualify(SUBJ, _comp(gla=1100), AS_OF)["qualified"] is True)   # +10%
    q = comps.qualify(SUBJ, _comp(gla=1300), AS_OF)                                                     # +30% out
    check("out-of-band GLA fails", q["qualified"] is False and q["gla_in_band"] is False)


def test_qualify_recency_and_price():
    check("stale comp (>365d) fails", comps.qualify(SUBJ, _comp(days_ago=400), AS_OF)["qualified"] is False)
    check("low price fails", comps.qualify(SUBJ, _comp(close=1000), AS_OF)["qualified"] is False)
    q = comps.qualify(SUBJ, _comp(days_ago=30), AS_OF)
    check("recent comp recency tier 1", q["recency_tier"] == 1, q["recency_tier"])


def test_qualify_distance_tiers():
    near = _comp(lat=32.751, lng=-96.831)      # ~0.1 mi
    far = _comp(lat=32.9, lng=-96.83)          # ~10 mi, beyond 2.0mi tier
    check("near comp distance tier 1", comps.qualify(SUBJ, near, AS_OF)["distance_tier"] == 1)
    check("far comp fails distance", comps.qualify(SUBJ, far, AS_OF)["qualified"] is False)
    check("no coords → distance not a hard fail", comps.qualify(SUBJ, _comp(), AS_OF)["qualified"] is True)


def test_same_subdivision():
    check("same subdivision detected", comps.qualify(SUBJ, _comp(sub="oak cliff"), AS_OF)["same_subdivision"] is True)
    check("diff subdivision", comps.qualify(SUBJ, _comp(sub="Pleasant Grove"), AS_OF)["same_subdivision"] is False)


# ── subdivision parsing (must reproduce DCAD-verified known answers before trust) ─────────────────
def test_parse_subdivision_known():
    cases = {
        "1: FOREST GROVE NO 5 2: BLK 10/6688 LOT 22 3:": "FOREST GROVE",       # Tryon
        "1: MOUNTAIN LAKEVIEW 3 2: BLK 19 LOT 16 3:": "MOUNTAIN LAKEVIEW",     # Grant
        "1: WALLS H G 2: BLK B/4773 LT 8 3:": "WALLS H G",                     # Ruby
    }
    for legal, expected in cases.items():
        p = comps.parse_subdivision(legal)
        check(f"parse → {expected}", p and p["normalized"] == expected, p and p["normalized"])
    check("parse None on empty", comps.parse_subdivision(None) is None)


def test_normalize_subdivision():
    n = comps.normalize_subdivision
    check("section number dropped", n("Mountain Lakeview 03") == "MOUNTAIN LAKEVIEW")
    check("NO n dropped", n("Forest Grove No 5") == "FOREST GROVE")
    check("directional kept (distinct)", n("Mountain Lakeview South 02") == "MOUNTAIN LAKEVIEW SOUTH")
    check("addition dropped", n("Turner Heights Add") == "TURNER HEIGHTS")
    check("initials kept", n("Walls H G") == "WALLS H G")
    # and the matcher treats '03'/plain as the SAME subdivision, 'South' as DIFFERENT
    subj = dict(SUBJ, subdivision="Mountain Lakeview")
    check("ML == ML 03", comps.qualify(subj, _comp(sub="Mountain Lakeview 03"), AS_OF)["same_subdivision"] is True)
    check("ML != ML South", comps.qualify(subj, _comp(sub="Mountain Lakeview South 02"), AS_OF)["same_subdivision"] is False)


def test_prefer_same_subdivision_selection():
    subj = dict(SUBJ, subdivision="Mountain Lakeview")
    pool = [_comp(gla=1055, close=250000, sub="Mountain Lakeview"),      # same subdivision, higher value
            _comp(gla=1000, close=180000, sub="Turner Heights"),        # area, cheaper
            _comp(gla=1010, close=175000, sub="Tyre Estates"),
            _comp(gla=1020, close=185000, sub="Glen Grove")]
    r = comps.provisional_arv(subj, pool, AS_OF)
    check("prefers same subdivision when present", r["selection_mode"] == "same_subdivision", r["selection_mode"])
    check("n_same_subdivision counted", r["n_same_subdivision"] == 1)
    check("ARV from same-subdivision comp, not zip median", r["provisional_arv"] > r["area_sanity_band_arv"])
    check("area sanity band still reported", r["area_sanity_band_arv"] is not None)
    # no same-subdivision comps → area fallback
    r2 = comps.provisional_arv(subj, pool[1:], AS_OF)
    check("falls back to area when none same-subdivision", r2["selection_mode"] == "area" and r2["n_same_subdivision"] == 0)


# ── adjustments ──────────────────────────────────────────────────────────────────────────────────
def test_adjustments_itemized():
    # subject 1000sqft/3bd/2ba/1980 vs comp 900sqft/2bd/1ba/1970
    adj = comps.adjust(SUBJ, _comp(gla=900, beds=2, baths=1, year=1970))
    a = comps.COMP_CONFIG["adjustments"]
    check("GLA adj = (1000−900)×psf", adj["adjustment_lines"]["gla"] == 100 * a["per_sqft_gla"])
    check("beds adj = (3−2)×per_bed", adj["adjustment_lines"]["beds"] == a["per_bed"])
    check("baths adj = (2−1)×per_bath", adj["adjustment_lines"]["baths"] == a["per_full_bath"])
    check("age adj = (1980−1970)×per_year", adj["adjustment_lines"]["age"] == 10 * a["per_year_age"])
    expected = 200000 + 100 * a["per_sqft_gla"] + a["per_bed"] + a["per_full_bath"] + 10 * a["per_year_age"]
    check("adjusted value sums lines", adj["adjusted_value"] == expected, adj["adjusted_value"])


def test_match_score_prefers_closer():
    q = comps.qualify(SUBJ, _comp(gla=1000), AS_OF)
    close = comps.match_score(SUBJ, _comp(gla=1000), q)
    far = comps.match_score(SUBJ, _comp(gla=1180), comps.qualify(SUBJ, _comp(gla=1180), AS_OF))
    check("closer GLA scores higher", close > far, (close, far))
    penalized = comps.match_score(SUBJ, _comp(flags=["distressed"]),
                                  comps.qualify(SUBJ, _comp(flags=["distressed"]), AS_OF))
    check("arm's-length flag penalizes score", penalized < close)


# ── provisional ARV ──────────────────────────────────────────────────────────────────────────────
def test_provisional_arv():
    pool = [_comp(gla=1000, close=200000), _comp(gla=1050, close=210000), _comp(gla=980, close=195000),
            _comp(gla=1020, close=205000), _comp(gla=1010, close=202000)]
    r = comps.provisional_arv(SUBJ, pool, AS_OF)
    check("provisional ARV produced", r["provisional_arv"] is not None)
    check("ARV labeled estimated/provisional", r["label"] == "estimated" and r["valuation_state"] == "provisional")
    check("ARV in comp value range", 195000 <= r["provisional_arv"] <= 215000, r["provisional_arv"])
    check("n_used capped at top_n", r["n_used"] <= comps.COMP_CONFIG["arv"]["top_n"])


def test_provisional_arv_no_qualified():
    r = comps.provisional_arv(SUBJ, [_comp(gla=1400), _comp(days_ago=500)], AS_OF)   # all disqualified
    check("no qualified → unavailable", r["provisional_arv"] is None and r["label"] == "unavailable")


# ── locality fallback: city when the case address has no zip (real gap — TX-23-00553) ────────────
def test_city_from_address():
    f = comps.city_from_address
    check("Dallas + county", f("2515 West Brooklyn Avenue, Dallas, Dallas County, Texas") == "Dallas")
    check("strips 'City of' + normalizes case", f("1561 Dent Street, City of Garland, Texas") == "Garland")
    check("uppercase → title", f("2412 ARCADY DRIVE, GARLAND, TX") == "Garland")
    check("Rowlett w/ county", f("8305 Concord Drive, City of Rowlett, Dallas County, TX") == "Rowlett")
    check("multi-comma addr", f("4548 Chaha Road, Unit 102, Building K, City of Garland, TX") == "Garland")
    check("two-word city", f("2457 Grant St., Grand Prairie, TX 75051-5534") == "Grand Prairie")
    check("no city → None", comps.city_from_address("") is None and comps.city_from_address(None) is None)


def test_subject_from_case_locality_fallback():
    # TX-23-00553: case address has NO zip, but property_intel is enriched (has GLA + legal).
    case = {"case_number": "TX-23-00553",
            "property_address": "2515 West Brooklyn Avenue, Dallas, Dallas County, Texas",
            "property_intel": '{"living_area_sqft": 1174, "market_value": 120000, '
                              '"legal_description": "1: SUNSET SUMMIT 2: BLK G/3483 LOT 16 3:"}'}
    s = comps.subject_from_case(case)
    check("no zip → postal_code None", s["postal_code"] is None)
    check("city fallback = Dallas", s["city"] == "Dallas")
    check("GLA still from DCAD", s["gla"] == 1174)
    check("subdivision still parsed", s["subdivision"] == "SUNSET SUMMIT")


def test_locality_clause():
    check("zip → PostalCode filter", comps._locality_clause("75217", "Dallas") == "PostalCode eq '75217'")
    check("no zip → City filter", comps._locality_clause(None, "Garland") == "City eq 'Garland'")
    raised = False
    try:
        comps._locality_clause(None, None)
    except ValueError:
        raised = True
    check("no locality → raises (caller fails closed)", raised)


class _FakeClient:
    def __init__(self): self.calls = []
    def _row(self):
        return {"ListingId": "L1", "LivingArea": 1150, "LotSizeAcres": 0.15, "CloseDate": "2026-06-01",
                "NTREIS2_RATIO_ClosePrice_By_LotSizeAcres": 1_500_000, "BedroomsTotal": 3,
                "BathroomsTotalInteger": 2, "YearBuilt": 1970, "SubdivisionName": "Sunset Summit"}
    def closed_sales(self, postal_code=None, city=None, gla_min=None, gla_max=None, since=None, top=100):
        self.calls.append(("closed", postal_code, city)); return [self._row()]
    def pending_listings(self, postal_code=None, city=None, gla_min=None, gla_max=None, top=40):
        self.calls.append(("pending", postal_code, city)); return []


def test_fetch_candidates_uses_city_when_no_zip():
    subj = dict(postal_code=None, city="Dallas", gla=1174)
    fc = _FakeClient()
    res = comps.fetch_candidates(subj, client=fc)
    check("query used city (not zip) as locality", ("closed", None, "Dallas") in fc.calls)
    check("returns a normalized closed comp via city fallback", len(res["closed"]) == 1 and res["closed"][0]["gla"] == 1150)
    # no locality at all → nothing queried (upstream fails closed)
    fc2 = _FakeClient()
    res2 = comps.fetch_candidates(dict(postal_code=None, city=None, gla=1000), client=fc2)
    check("no locality → no query, empty result", res2["closed"] == [] and fc2.calls == [])


# ── §G LAND FLOOR (design §16) ────────────────────────────────────────────────────────────────────
LAND_SUBJ = dict(lot_acres=0.166, gla=484, postal_code="75241", city="Dallas")


def _land(acres, close, days_ago=60, flags=None):
    return {"lot_acres": acres, "close_price": close, "arms_length_flags": flags or [],
            "close_date": (AS_OF - datetime.timedelta(days=days_ago)).isoformat()}


def test_land_recency_default_and_widen_config():
    cfg = comps.COMP_CONFIG["land"]
    check("land recency default 12mo (not 24 — stale prices understate in a rising market)",
          cfg["recency_months"] == 12)
    check("widen target 24mo, only when thin", cfg["widen_recency_months"] == 24 and cfg["min_comps_before_widen"] > 0)
    check("land_since honors an explicit window",
          comps.land_since(datetime.date(2026, 7, 23), 12) == "2025-07-23")
    check("land_since widened window", comps.land_since(datetime.date(2026, 7, 23), 24) == "2024-07-23")


def test_qualify_land_bands_by_lot_not_gla():
    q = comps.qualify_land(LAND_SUBJ, _land(0.17, 90000), AS_OF)
    check("in-band lot qualifies", q["qualified"] is True and q["lot_in_band"] is True)
    q2 = comps.qualify_land(LAND_SUBJ, _land(0.90, 400000), AS_OF)
    check("out-of-band lot fails", q2["qualified"] is False and q2["lot_in_band"] is False)
    check("stale land comp fails", comps.qualify_land(LAND_SUBJ, _land(0.17, 90000, days_ago=1000), AS_OF)["qualified"] is False)
    check("low price fails", comps.qualify_land(LAND_SUBJ, _land(0.17, 100), AS_OF)["qualified"] is False)
    # THE GUARD: land has no GLA — a comp with no GLA must still qualify (the improved band must not apply)
    check("no GLA on a land comp is NOT a disqualifier",
          comps.qualify_land(LAND_SUBJ, _land(0.17, 90000), AS_OF)["qualified"] is True)


def test_land_floor_is_median_of_reconstructed_closes():
    pool = [_land(0.15, 80000), _land(0.18, 103000), _land(0.166, 90000), _land(0.90, 400000)]
    r = comps.land_floor(LAND_SUBJ, pool, AS_OF)
    check("floor = median of banded closes (90000)", r["land_floor"] == 90000, r["land_floor"])
    check("out-of-band comp excluded from n", r["n"] == 3, r["n"])
    check("labeled estimated", r["label"] == "estimated")
    check("range + spread reported", r["range"] == [80000, 103000] and r["spread"] == 23000)
    check("$/acre reported as a separate SECONDARY field", r["median_price_per_acre"] is not None)


def test_land_floor_uses_median_close_not_price_per_acre():
    """The method is median-of-reconstructed-CLOSES, not a $/acre extrapolation (design §16.2 — the
    naive $/acre understated Kemrock by 67%). Inside a tight band the two nearly coincide (that's why
    banding works); this pool is built so they DIVERGE, pinning which one the engine actually uses."""
    pool = [_land(0.13, 60000), _land(0.20, 120000), _land(0.166, 70000)]
    r = comps.land_floor(LAND_SUBJ, pool, AS_OF)
    ppa_extrapolation = round(r["median_price_per_acre"] * LAND_SUBJ["lot_acres"])
    check("floor == median of closes (70000)", r["land_floor"] == 70000, r["land_floor"])
    check("floor != the $/acre extrapolation on a size-spread set",
          r["land_floor"] != ppa_extrapolation, (r["land_floor"], ppa_extrapolation))


def test_land_floor_unavailable_without_lot_size():
    r = comps.land_floor({"lot_acres": None}, [_land(0.15, 80000)], AS_OF)
    check("no subject lot size → unavailable", r["land_floor"] is None and r["label"] == "unavailable")
    r2 = comps.land_floor(LAND_SUBJ, [_land(0.90, 400000)], AS_OF)
    check("no in-band comps → unavailable", r2["land_floor"] is None and r2["n"] == 0)


def test_net_of_demolition_is_a_separate_line():
    r = comps.land_floor(LAND_SUBJ, [_land(0.15, 80000), _land(0.18, 103000), _land(0.166, 90000)], AS_OF)
    cfg = comps.COMP_CONFIG["land"]
    demo = max(cfg["demolition_minimum"], round(LAND_SUBJ["gla"] * cfg["demolition_cost_per_sqft"]))
    check("gross floor is NOT reduced by demolition", r["land_floor"] == 90000)
    check("net-of-demo reported separately", r["net_of_demolition"] == 90000 - demo, r["net_of_demolition"])
    check("demolition cost surfaced", r["demolition_cost"] == demo)
    vac = comps.land_floor({**LAND_SUBJ, "gla": None}, [_land(0.15, 80000)], AS_OF)
    check("vacant lot → no demo deduction", vac["net_of_demolition"] is None and "no structure" in vac["demolition_note"])


if __name__ == "__main__":
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)):
        fn()
    total = _p + _f
    print(f"{_p}/{total} checks passed" + (f"  ({_f} FAILED)" if _f else "  ✓ all green"))
