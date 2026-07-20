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


if __name__ == "__main__":
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)):
        fn()
    total = _p + _f
    print(f"{_p}/{total} checks passed" + (f"  ({_f} FAILED)" if _f else "  ✓ all green"))
