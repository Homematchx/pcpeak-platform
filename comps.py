"""
comps.py — NTREIS/Bridge comparable-sales engine (Acquisition Intelligence, Stage 2).

Fetches closed SALE comps from the Bridge RESO Web API (dataset ntreis2), qualifies them
appraiser-style, ranks by MatchScore, applies tunable adjustments, and produces a PROVISIONAL ARV
for triage. A trusted (confirmed) ARV requires human confirmation of the comp set (design §6.5) —
this module only proposes + ranks; it never finalizes the number an offer rests on.

Grounded in the live Phase-0 verification (docs/acquisition-ntreis-phase0.md), NOT assumptions:
  • ClosePrice is ABSENT from the feed → reconstructed as
        NTREIS2_RATIO_ClosePrice_By_LotSizeAcres × LotSizeAcres        (verified 0.0% on ListPrice)
    ONE reconstruction path also serves §G land/teardown pricing.
  • Sales require PropertyType='Residential' (a bare Closed filter returns leases).
  • Photos via the Media FIELD (MediaURL on Bridge CDN) — hotlinked, never stored (Q2).
  • DaysOnMarket absent → derive from CloseDate − ListingContractDate.

Credentials come from Anthropic_API_KEY.env (NTREIS_BASE_URL / NTREIS_SERVER_TOKEN); never logged.
"""
from __future__ import annotations

import datetime
import json
import math
import re
import ssl
import urllib.parse
import urllib.request
from typing import Optional

UNAVAILABLE_LABEL = "unavailable"   # matches acquisition.UNAVAILABLE (kept literal to avoid a cycle)

# ── tunable comp config (Dallas defaults) — every magnitude named, never inline. [SIGN-OFF] ───────
# These are appraisal knobs; the defaults are reasonable-but-uncalibrated placeholders (same standard
# as ACQ_CONFIG / CITY_DATA). Changing one is a signed-off action once real Dallas calibration exists.
COMP_CONFIG = {
    "qualification": {
        "distance_tiers_mi": [0.5, 1.0, 2.0],      # tier 1/2/3; beyond tier 3 = out of range
        "recency_tiers_days": [90, 180, 365],      # tier 1/2/3
        "gla_band_pct": 0.20,                       # subject GLA ±20% (design §6.2)
        "min_close_price": 20000,                   # guard against lease/garbage rows
    },
    "adjustments": {                                # $ applied to a comp to make it like the subject
        "per_sqft_gla": 110,                        # $/sqft for GLA delta (Dallas placeholder)
        "per_bed": 5000,
        "per_full_bath": 7000,
        "per_year_age": 400,                        # per year of effective-age difference
        "condition_delta_per_class": 12000,         # per C-class step (C4 vs C5 etc.)
    },
    "match_weights": {                              # framework §XII, normalized to available signals
        "gla": 25, "distance": 15, "recency": 10, "beds_baths": 10,
        "year": 10, "subdivision": 10, "lot": 5, "condition": 15,
    },
    "arv": {
        "top_n": 5,                                 # comps that drive the ARV
        "method": "median_adjusted",                # median of adjusted comp values
        "prefer_same_subdivision": True,            # the subject's own subdivision is the best comp set
        "min_same_subdivision": 1,                  # ≥ this many same-subdivision comps → use them
    },
    # §G LAND FLOOR (design §16). Land is priced from PropertyType='Land' closed sales, banded by LOT
    # SIZE (never a $/acre extrapolation — $/acre is strongly size-dependent; see design §16.2), using
    # the SAME reconstruction path as improved comps. [SIGN-OFF on the defaults.]
    "land": {
        "property_type": "Land",
        "lot_size_band_pct": 0.30,      # subject acreage ±30%
        # Recency: 12mo DEFAULT, widened to 24 ONLY when the band is too thin (and labeled when it is).
        # Measured why: a 24mo window imports stale prices in an appreciating market — Kemrock's median
        # fell $85.5k (12mo, n=14) → $70k (24mo, n=33) purely from 2024 sales at ~half 2026 levels.
        # Widening is a last resort until the Stage-3 market-conditions time adjustment exists.
        "recency_months": 12,
        "widen_recency_months": 24,
        "min_comps_before_widen": 6,
        "min_close_price": 5000,
        "max_fetch": 100,
        # Teardown netting — a SEPARATE labeled line, NEVER baked into the gross floor. [SIGN-OFF]
        "demolition_cost_per_sqft": 8,
        "demolition_minimum": 8000,
    },

    "arv_reconstruction_field": "NTREIS2_RATIO_ClosePrice_By_LotSizeAcres",
    "select_fields": [
        "ListingId", "ListingKey", "PropertyType", "PropertySubType", "StandardStatus", "MlsStatus",
        "CloseDate", "ListingContractDate", "ListPrice", "LotSizeAcres", "LotSizeSquareFeet",
        "LivingArea", "BedroomsTotal", "BathroomsTotalInteger", "YearBuilt", "Latitude", "Longitude",
        "PostalCode", "City", "SubdivisionName", "UnparsedAddress", "PublicRemarks", "PhotosCount",
        "NTREIS2_RATIO_ClosePrice_By_LotSizeAcres", "NTREIS2_PreviousStatus", "NTREIS2_ClosedRemarks",
        "Media",
    ],
}


# ── Bridge OData client ──────────────────────────────────────────────────────────────────────────
def _load_env(path: str = "Anthropic_API_KEY.env") -> dict:
    """os.environ wins (Railway/shell export); the local .env file is the fallback (dev)."""
    import os
    env = {}
    try:
        for line in open(path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v
    except FileNotFoundError:
        pass
    for k in ("NTREIS_BASE_URL", "NTREIS_SERVER_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _parse_baths(raw) -> Optional[float]:
    """DCAD bathrooms come as 'full/half' (e.g. '2/1' → 2.5) or blank. Lenient; None if unparseable."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    m = re.match(r'^\s*(\d+)\s*/\s*(\d+)\s*$', s)
    if m:
        return int(m.group(1)) + 0.5 * int(m.group(2))
    try:
        return float(s)
    except ValueError:
        return None


def city_from_address(addr: Optional[str]) -> Optional[str]:
    """Normalize the city from a case (petition) address — the LOCALITY FALLBACK when the address has
    no postal code. NTREIS `City` is case-sensitive Title Case ('Garland', not 'garland'/'CITY OF
    GARLAND'). Returns None when no city is parseable. (NOTE: the DCAD situs address — which carries a
    zip — is NOT stored in property_intel, so the city here comes from the case address, not DCAD.)"""
    if not addr:
        return None
    m = re.search(r',\s*([A-Za-z][A-Za-z .]+?)\s*,\s*(?:Dallas County|Texas|TX)\b', addr, re.I)
    if not m:
        m = re.search(r'(?:CITY OF)\s+([A-Za-z][A-Za-z .]+?)\s*,', addr, re.I)
    if not m:
        return None
    city = re.sub(r'^\s*city of\s+', '', m.group(1), flags=re.I).strip()
    return city.title() if city else None


def subject_from_case(case: dict) -> dict:
    """Build a comp-engine subject from a case row + its property_intel blob (design §3.4).
    Locality = postal code from property_address when present, else the city (fallback). GLA is the
    DCAD living area; subdivision is parsed from the DCAD legal_description."""
    pi = case.get("property_intel")
    if isinstance(pi, str):
        try:
            pi = json.loads(pi)
        except Exception:
            pi = {}
    pi = pi or {}
    addr = case.get("property_address") or ""
    m = re.search(r'\b(\d{5})(?:-\d{4})?\b', addr)
    legal = pi.get("legal_description") or case.get("legal_description")
    sub = parse_subdivision(legal)
    lot_sqft = pi.get("lot_area_sqft")
    return {
        "case_number": case.get("case_number"),
        "postal_code": m.group(1) if m else None,
        "city": city_from_address(addr),                 # locality fallback when no zip
        "gla": pi.get("living_area_sqft"),
        "beds": pi.get("bedrooms"),
        "baths": _parse_baths(pi.get("bathrooms")),
        "year_built": pi.get("year_built") or pi.get("effective_year_built"),
        "lot_acres": round(lot_sqft / 43560, 4) if lot_sqft else None,
        "subdivision": sub["normalized"] if sub else None,
        "lat": None, "lng": None,
        "market_value": pi.get("market_value"),
    }


def _locality_clause(postal_code: Optional[str], city: Optional[str]) -> str:
    """OData locality filter: PostalCode when known (precise), else City (broader fallback for case
    addresses that carry no zip). Raises if neither — the caller must fail closed before this."""
    if postal_code:
        return f"PostalCode eq '{postal_code}'"
    if city:
        return f"City eq '{city}'"
    raise ValueError("no locality (postal_code or city) for the comp query")


class BridgeClient:
    """Minimal Bridge RESO Web API (OData v4) client. Token in the Authorization header, never logged."""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        env = _load_env()
        self.base = (base_url or env.get("NTREIS_BASE_URL", "")).rstrip("/")
        self._token = token or env.get("NTREIS_SERVER_TOKEN", "")
        self._ctx = ssl.create_default_context()
        try:
            import certifi
            self._ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass

    def configured(self) -> bool:
        return bool(self.base and self._token)

    def query(self, resource: str, params: dict) -> dict:
        url = f"{self.base}/{resource}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        with urllib.request.urlopen(req, timeout=60, context=self._ctx) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def closed_sales(self, postal_code: Optional[str] = None, city: Optional[str] = None,
                     gla_min: Optional[float] = None, gla_max: Optional[float] = None,
                     since: Optional[str] = None, top: int = 100) -> list:
        """Closed SFR SALES (PropertyType='Residential' — excludes leases, per Phase 0). Locality =
        PostalCode when known, else City (broader — fallback for case addresses with no zip)."""
        clauses = ["PropertyType eq 'Residential'", "StandardStatus eq 'Closed'", _locality_clause(postal_code, city)]
        if gla_min:
            clauses.append(f"LivingArea ge {int(gla_min)}")
        if gla_max:
            clauses.append(f"LivingArea le {int(gla_max)}")
        if since:
            clauses.append(f"CloseDate ge {since}")
        params = {
            "$filter": " and ".join(clauses),
            "$select": ",".join(COMP_CONFIG["select_fields"]),
            "$orderby": "CloseDate desc",
            "$top": str(top),
        }
        return self.query("Property", params).get("value", [])

    def land_sales(self, postal_code: Optional[str] = None, city: Optional[str] = None,
                   acres_min: Optional[float] = None, acres_max: Optional[float] = None,
                   since: Optional[str] = None, top: Optional[int] = None) -> list:
        """Closed LAND sales (§G basis) — `PropertyType='Land'` ONLY (teardown-intent improved sales are
        a Stage-3 refinement, design §16.4). Banded by LOT SIZE, never by GLA (land has no GLA)."""
        cfg = COMP_CONFIG["land"]
        clauses = [f"PropertyType eq '{cfg['property_type']}'", "StandardStatus eq 'Closed'",
                   _locality_clause(postal_code, city)]
        if acres_min:
            clauses.append(f"LotSizeAcres ge {acres_min}")
        if acres_max:
            clauses.append(f"LotSizeAcres le {acres_max}")
        if since:
            clauses.append(f"CloseDate ge {since}")
        params = {"$filter": " and ".join(clauses), "$select": ",".join(COMP_CONFIG["select_fields"]),
                  "$orderby": "CloseDate desc", "$top": str(top or cfg["max_fetch"])}
        return self.query("Property", params).get("value", [])

    def gla_profile(self, postal_code: Optional[str] = None, city: Optional[str] = None,
                    since: Optional[str] = None, top: int = 200) -> dict:
        """DIAGNOSTIC (only run when a propose returns 0): what GLA sizes actually sell in this locality,
        so an empty batch can name WHY — e.g. 'subject 484 sf vs local range 870–3,550'."""
        clauses = ["PropertyType eq 'Residential'", "StandardStatus eq 'Closed'",
                   _locality_clause(postal_code, city)]
        if since:
            clauses.append(f"CloseDate ge {since}")
        rows = self.query("Property", {"$filter": " and ".join(clauses), "$select": "LivingArea",
                                       "$orderby": "CloseDate desc", "$top": str(top)}).get("value", [])
        g = sorted(r["LivingArea"] for r in rows if r.get("LivingArea"))
        if not g:
            return {"n": 0}
        return {"n": len(g), "min": g[0], "max": g[-1], "median": g[len(g) // 2]}

    def pending_listings(self, postal_code: Optional[str] = None, city: Optional[str] = None,
                         gla_min: Optional[float] = None, gla_max: Optional[float] = None,
                         top: int = 40) -> list:
        """Pending / under-contract SALES — DIRECTIONAL ONLY (no close price; never in ARV math)."""
        clauses = ["PropertyType eq 'Residential'",
                   "(StandardStatus eq 'Pending' or StandardStatus eq 'Active Under Contract')",
                   _locality_clause(postal_code, city)]
        if gla_min:
            clauses.append(f"LivingArea ge {int(gla_min)}")
        if gla_max:
            clauses.append(f"LivingArea le {int(gla_max)}")
        params = {"$filter": " and ".join(clauses), "$select": ",".join(COMP_CONFIG["select_fields"]),
                  "$top": str(top)}
        return self.query("Property", params).get("value", [])


def fetch_candidates(subject: dict, client: Optional["BridgeClient"] = None, since: Optional[str] = None,
                     include_pending: bool = True) -> dict:
    """Live-fetch normalized closed comps (+ optional directional pendings) for a subject. Returns
    {closed:[...], pending:[...]}. Pendings carry listing_status='pending', no close_price."""
    client = client or BridgeClient()
    band = COMP_CONFIG["qualification"]["gla_band_pct"]
    gla = subject.get("gla")
    lo, hi = (gla * (1 - band), gla * (1 + band)) if gla else (None, None)
    pc = subject.get("postal_code")
    city = subject.get("city")
    if not (pc or city):                       # no locality at all → nothing to query (fail closed upstream)
        return {"closed": [], "pending": []}
    closed = [dict(normalize(r), listing_status="closed")
              for r in client.closed_sales(postal_code=pc, city=city, gla_min=lo, gla_max=hi, since=since)]
    pending = []
    if include_pending:
        for r in client.pending_listings(postal_code=pc, city=city, gla_min=lo, gla_max=hi):
            n = normalize(r)
            n["listing_status"] = "pending"
            n["close_price"] = None            # directional only — no confirmed sale price
            n["list_price_directional"] = r.get("ListPrice")
            pending.append(n)
    return {"closed": closed, "pending": pending}


# ── §G LAND FLOOR (design §16) ────────────────────────────────────────────────────────────────────
def land_since(as_of: Optional[datetime.date] = None, months: Optional[int] = None) -> str:
    """Recency floor for land comps. Default 12mo; the caller widens only when the band is thin."""
    as_of = as_of or datetime.date.today()
    months = months if months is not None else COMP_CONFIG["land"]["recency_months"]
    y, m = as_of.year - months // 12, as_of.month - months % 12
    if m <= 0:
        m += 12
        y -= 1
    return datetime.date(y, m, min(as_of.day, 28)).isoformat()


def qualify_land(subject: dict, comp: dict, as_of: Optional[datetime.date] = None) -> dict:
    """Land qualification: LOT-SIZE band + recency + min price. **NO GLA band** — land has no GLA, and
    applying the improved-comp band would zero the set (design §16.3, explicit guard)."""
    as_of = as_of or datetime.date.today()
    cfg = COMP_CONFIG["land"]
    reasons, ok = [], True
    cp = comp.get("close_price")
    if not cp or cp < cfg["min_close_price"]:
        ok = False
        reasons.append("no/low reconstructed close price")
    sa, ca = subject.get("lot_acres"), comp.get("lot_acres")
    in_band = None
    if sa and ca:
        b = cfg["lot_size_band_pct"]
        lo, hi = sa * (1 - b), sa * (1 + b)
        in_band = lo <= ca <= hi
        if not in_band:
            ok = False
            reasons.append(f"lot {ca}ac outside ±{int(b*100)}% of {sa}ac")
    elif not ca:
        ok = False
        reasons.append("comp lot size missing")
    days = None
    if comp.get("close_date"):
        try:
            days = (as_of - datetime.date.fromisoformat(comp["close_date"])).days
        except ValueError:
            days = None
    if days is not None and days > cfg["recency_months"] * 31:
        ok = False
        reasons.append(f"closed {days}d ago beyond {cfg['recency_months']}mo")
    return {"qualified": ok, "lot_in_band": in_band, "recency_days": days,
            "arms_length_flags": comp.get("arms_length_flags", []), "reasons": reasons}


def land_floor(subject: dict, land_comps: list, as_of: Optional[datetime.date] = None) -> dict:
    """§G LAND FLOOR = MEDIAN of the qualified, lot-size-banded comps' RECONSTRUCTED CLOSES.
    Deliberately NOT a $/acre extrapolation — that understated by 67% on the Kemrock check because
    $/acre is strongly size-dependent (design §16.2). $/acre is returned for SECONDARY display only.
    Labeled `estimated`. It is a FLOOR, never an ARV, and must NEVER feed MAO (design §16.4.3)."""
    as_of = as_of or datetime.date.today()
    sa = subject.get("lot_acres")
    if not sa:
        return {"land_floor": None, "label": UNAVAILABLE_LABEL, "n": 0,
                "note": "subject lot size unknown — no land floor"}
    b = COMP_CONFIG["land"]["lot_size_band_pct"]
    band = [round(sa * (1 - b), 4), round(sa * (1 + b), 4)]
    q = []
    for c in land_comps:
        r = qualify_land(subject, c, as_of)
        if r["qualified"]:
            q.append({**c, "qualification": r})
    if not q:
        return {"land_floor": None, "label": UNAVAILABLE_LABEL, "n": 0, "subject_lot_acres": sa,
                "lot_band_acres": band, "note": "no qualified land comps in the lot-size band"}
    closes = sorted(c["close_price"] for c in q)
    n = len(closes)
    med = closes[n // 2] if n % 2 else round((closes[n // 2 - 1] + closes[n // 2]) / 2)
    ppa = sorted(round(c["close_price"] / c["lot_acres"]) for c in q if c.get("lot_acres"))
    return {
        "land_floor": med, "label": "estimated", "n": n,
        "range": [closes[0], closes[-1]], "spread": closes[-1] - closes[0],
        "subject_lot_acres": sa, "lot_band_acres": band,
        "median_price_per_acre": (ppa[len(ppa) // 2] if ppa else None),   # SECONDARY display only
        "n_arms_length_flagged": sum(1 for c in q if c["qualification"]["arms_length_flags"]),
        # Self-describing defaults so every caller (including a direct land_floor() call) carries
        # provenance; land_floor_for_subject() overrides these when it widens the window.
        "recency_months_used": COMP_CONFIG["land"]["recency_months"],
        "recency_widened": False,
        **_net_of_demolition(subject, med),
        "comps": q,
        "note": f"median of {n} closed land sales in lot band {band[0]}–{band[1]} ac "
                f"(FLOOR only — never an ARV, never feeds MAO)",
    }


def fetch_land_candidates(subject: dict, client: Optional["BridgeClient"] = None,
                          as_of: Optional[datetime.date] = None, months: Optional[int] = None) -> list:
    """Live-fetch normalized closed LAND comps for the subject's locality + lot-size band."""
    client = client or BridgeClient()
    sa = subject.get("lot_acres")
    pc, city = subject.get("postal_code"), subject.get("city")
    if not sa or not (pc or city):
        return []
    b = COMP_CONFIG["land"]["lot_size_band_pct"]
    rows = client.land_sales(postal_code=pc, city=city, acres_min=round(sa * (1 - b), 4),
                             acres_max=round(sa * (1 + b), 4), since=land_since(as_of, months))
    return [dict(normalize(r), listing_status="land_closed") for r in rows]


# ── subdivision parsing (DCAD legal_description → NTREIS-matchable name) ──────────────────────────
def normalize_subdivision(name: Optional[str]) -> str:
    """Normalize a subdivision name to a matchable BASE (drop section numbers + 'Addition', keep
    directional words like SOUTH which denote a distinct addition). Conservative: 'Mountain Lakeview
    03' and 'Mountain Lakeview' → 'MOUNTAIN LAKEVIEW'; 'Mountain Lakeview South 02' stays distinct."""
    if not name:
        return ""
    s = name.upper().strip()
    s = re.sub(r'\b(ADDITION|ADDN|ADD|REVISED|REV)\b', ' ', s)
    s = re.sub(r'\b(NO|NUMBER|SEC|SECTION|PH|PHASE|UNIT|BLK|BLOCK)\s*#?\s*\d+\w*', ' ', s)  # section markers
    s = re.sub(r'\b\d+\w*\b', ' ', s)          # standalone number tokens (3, 03, 02ndse)
    s = re.sub(r'[^A-Z ]', ' ', s)             # drop punctuation/#
    return re.sub(r'\s+', ' ', s).strip()


def parse_subdivision(legal_description: Optional[str]) -> Optional[dict]:
    """Parse the subdivision from a DCAD legal_description. DCAD format is
    '1: <SUBDIVISION> 2: BLK <x> LOT <y> 3: ...'. Returns {raw, normalized} or None.
    MUST reproduce the known subdivisions before it's trusted (test_comps): Grant→MOUNTAIN LAKEVIEW,
    Tryon→FOREST GROVE, Ruby→WALLS H G."""
    if not legal_description:
        return None
    m = re.search(r'1:\s*(.*?)\s*(?:\d+:|$)', legal_description)
    raw = (m.group(1) if m else legal_description).strip()
    norm = normalize_subdivision(raw)
    return {"raw": raw, "normalized": norm} if norm else None


# ── price reconstruction (Phase-0 verified) ──────────────────────────────────────────────────────
def reconstruct_close_price(rec: dict) -> Optional[float]:
    """ClosePrice = RATIO_ClosePrice_By_LotSizeAcres × LotSizeAcres (ClosePrice is absent from the
    feed). Verified exact on ListPrice in Phase 0. Returns None if either input is missing."""
    ratio = rec.get(COMP_CONFIG["arv_reconstruction_field"])
    acres = rec.get("LotSizeAcres")
    if ratio and acres:
        return round(ratio * acres)
    return None


def land_value_from_comp(rec: dict, subject_lot_acres: float) -> Optional[float]:
    """§G land/teardown pricing — SAME reconstruction path: close-price-per-acre × subject acreage."""
    ratio = rec.get(COMP_CONFIG["arv_reconstruction_field"])
    if ratio and subject_lot_acres:
        return round(ratio * subject_lot_acres)
    return None


# ── normalization ────────────────────────────────────────────────────────────────────────────────
def _media_urls(rec: dict, cap: int = 12) -> list:
    out = []
    for m in (rec.get("Media") or [])[:cap]:
        u = m.get("MediaURL") or m.get("MediaUrl")
        if u:
            out.append(u)
    return out


def _arms_length_flags(rec: dict) -> list:
    """Flag non-arm's-length signals for human review (excluded from the confirmed set by default)."""
    flags = []
    remarks = ((rec.get("PublicRemarks") or "") + " " + (rec.get("NTREIS2_ClosedRemarks") or "")).lower()
    prev = (rec.get("NTREIS2_PreviousStatus") or "").lower()
    if any(w in remarks for w in ("foreclosure", "reo", "auction", "as-is", "as is", "investor", "estate sale")):
        flags.append("distressed/REO/auction language in remarks")
    if any(w in remarks for w in ("relative", "family", "non-arm", "not arm")):
        flags.append("possible family/non-arm's-length")
    if "foreclosure" in prev or "reo" in prev:
        flags.append("prior status foreclosure/REO")
    return flags


def normalize(rec: dict) -> dict:
    return {
        "mls_id": rec.get("ListingId"),
        "listing_key": rec.get("ListingKey"),
        "address": rec.get("UnparsedAddress"),
        "postal_code": rec.get("PostalCode"),
        "subdivision": rec.get("SubdivisionName"),
        "lat": rec.get("Latitude"),
        "lng": rec.get("Longitude"),
        "close_price": reconstruct_close_price(rec),   # RECONSTRUCTED (Phase-0 verified)
        "close_date": (rec.get("CloseDate") or "")[:10] or None,
        "list_price": rec.get("ListPrice"),
        "list_date": (rec.get("ListingContractDate") or "")[:10] or None,
        "gla": rec.get("LivingArea"),
        "lot_acres": rec.get("LotSizeAcres"),
        "beds": rec.get("BedroomsTotal"),
        "baths": rec.get("BathroomsTotalInteger"),
        "year_built": rec.get("YearBuilt"),
        "subtype": rec.get("PropertySubType"),
        "photos_count": rec.get("PhotosCount"),
        "media_urls": _media_urls(rec),                # hotlink refs only (Q2)
        "arms_length_flags": _arms_length_flags(rec),
        "price_source": "reconstructed_ratio_x_acres",
    }


# ── qualification ────────────────────────────────────────────────────────────────────────────────
def haversine_mi(lat1, lng1, lat2, lng2) -> Optional[float]:
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return round(r * 2 * math.asin(math.sqrt(a)), 3)


def _tier(value, tiers) -> Optional[int]:
    if value is None:
        return None
    for i, t in enumerate(tiers, 1):
        if value <= t:
            return i
    return None  # beyond the widest tier


def qualify(subject: dict, comp: dict, as_of: datetime.date) -> dict:
    """Appraiser-grade qualification. Returns pass/fail + the tiers + reasons. A comp fails if it's
    out of GLA band, out of distance/recency range, or below the min price. Arm's-length flags do NOT
    auto-fail (they're surfaced for human review) but drop MatchScore."""
    cfg = COMP_CONFIG["qualification"]
    reasons = []
    cp = comp.get("close_price")
    ok = True

    if not cp or cp < cfg["min_close_price"]:
        ok = False
        reasons.append("no/low reconstructed close price")

    # GLA band
    gla_ok = None
    if subject.get("gla") and comp.get("gla"):
        band = cfg["gla_band_pct"]
        lo, hi = subject["gla"] * (1 - band), subject["gla"] * (1 + band)
        gla_ok = lo <= comp["gla"] <= hi
        if not gla_ok:
            ok = False
            reasons.append(f"GLA {comp['gla']} outside ±{int(band*100)}% of {subject['gla']}")

    # distance (needs subject coords; else None → tier unknown, not a hard fail)
    dist = haversine_mi(subject.get("lat"), subject.get("lng"), comp.get("lat"), comp.get("lng"))
    dist_tier = _tier(dist, cfg["distance_tiers_mi"]) if dist is not None else None
    if dist is not None and dist_tier is None:
        ok = False
        reasons.append(f"distance {dist}mi beyond {cfg['distance_tiers_mi'][-1]}mi")

    # recency
    days = None
    if comp.get("close_date"):
        try:
            days = (as_of - datetime.date.fromisoformat(comp["close_date"])).days
        except ValueError:
            days = None
    rec_tier = _tier(days, cfg["recency_tiers_days"]) if days is not None else None
    if days is not None and rec_tier is None:
        ok = False
        reasons.append(f"closed {days}d ago beyond {cfg['recency_tiers_days'][-1]}d")

    return {
        "qualified": ok,
        "distance_mi": dist, "distance_tier": dist_tier,
        "recency_days": days, "recency_tier": rec_tier,
        "gla_in_band": gla_ok,
        "same_subdivision": bool(subject.get("subdivision") and comp.get("subdivision")
                                 and normalize_subdivision(subject["subdivision"]) != ""
                                 and normalize_subdivision(subject["subdivision"]) == normalize_subdivision(comp["subdivision"])),
        "arms_length_flags": comp.get("arms_length_flags", []),
        "reasons": reasons,
    }


# ── MatchScore + adjustments ─────────────────────────────────────────────────────────────────────
def match_score(subject: dict, comp: dict, q: dict) -> int:
    w = COMP_CONFIG["match_weights"]
    score = 0.0
    # GLA closeness
    if subject.get("gla") and comp.get("gla"):
        score += w["gla"] * max(0, 1 - abs(comp["gla"] - subject["gla"]) / subject["gla"])
    # distance / recency by tier (tier 1 best)
    if q["distance_tier"]:
        score += w["distance"] * (1 - (q["distance_tier"] - 1) / len(COMP_CONFIG["qualification"]["distance_tiers_mi"]))
    if q["recency_tier"]:
        score += w["recency"] * (1 - (q["recency_tier"] - 1) / len(COMP_CONFIG["qualification"]["recency_tiers_days"]))
    # beds/baths
    if subject.get("beds") and comp.get("beds"):
        score += w["beds_baths"] * 0.5 * max(0, 1 - abs(comp["beds"] - subject["beds"]) / max(subject["beds"], 1))
    if subject.get("baths") and comp.get("baths"):
        score += w["beds_baths"] * 0.5 * max(0, 1 - abs(comp["baths"] - subject["baths"]) / max(subject["baths"], 1))
    # year
    if subject.get("year_built") and comp.get("year_built"):
        score += w["year"] * max(0, 1 - abs(comp["year_built"] - subject["year_built"]) / 60)
    # subdivision
    if q["same_subdivision"]:
        score += w["subdivision"]
    # arm's-length penalty
    if q["arms_length_flags"]:
        score -= 8
    return max(0, round(score))


def adjust(subject: dict, comp: dict) -> dict:
    """Itemized $ adjustments to make the comp like the subject (each named, from COMP_CONFIG).
    Adjusted value = comp close price + Σ adjustments. Every line stored for auditability (§XXI)."""
    a = COMP_CONFIG["adjustments"]
    lines = {}
    base = comp.get("close_price") or 0
    if subject.get("gla") and comp.get("gla"):
        lines["gla"] = round((subject["gla"] - comp["gla"]) * a["per_sqft_gla"])
    if subject.get("beds") is not None and comp.get("beds") is not None:
        lines["beds"] = (subject["beds"] - comp["beds"]) * a["per_bed"]
    if subject.get("baths") is not None and comp.get("baths") is not None:
        lines["baths"] = int((subject["baths"] - comp["baths"]) * a["per_full_bath"])
    if subject.get("year_built") and comp.get("year_built"):
        lines["age"] = (subject["year_built"] - comp["year_built"]) * a["per_year_age"]
    adjusted = base + sum(lines.values())
    return {"base_close_price": base, "adjustment_lines": lines,
            "net_adjustment": sum(lines.values()), "adjusted_value": round(adjusted)}


# ── provisional ARV (triage only; confirmed ARV needs human comp confirmation, §6.5) ─────────────
def provisional_arv(subject: dict, comps: list, as_of: Optional[datetime.date] = None) -> dict:
    """Rank qualified comps by MatchScore, adjust the top-N, return the median adjusted value as a
    PROVISIONAL ARV (labeled — never a trusted/offer number until the comps are human-confirmed)."""
    as_of = as_of or datetime.date.today()
    scored = []
    for c in comps:
        q = qualify(subject, c, as_of)
        if not q["qualified"]:
            continue
        ms = match_score(subject, c, q)
        adj = adjust(subject, c)
        scored.append({**c, "qualification": q, "match_score": ms, "adjustment": adj})
    scored.sort(key=lambda x: x["match_score"], reverse=True)
    if not scored:
        return {"provisional_arv": None, "label": "unavailable", "n_qualified": 0,
                "comps_ranked": [], "note": "no qualified comps"}

    def _median_adj(comps_list):
        vals = sorted(c["adjustment"]["adjusted_value"] for c in comps_list)
        n = len(vals)
        return vals[n // 2] if n % 2 else round((vals[n // 2 - 1] + vals[n // 2]) / 2)

    cfg = COMP_CONFIG["arv"]
    same = [c for c in scored if c["qualification"]["same_subdivision"]]
    area_top = scored[: cfg["top_n"]]
    area_arv = _median_adj(area_top)   # zip/area sanity band (always computed)

    # Prefer the subject's OWN subdivision when it has comps (appraisal principle — homogeneity beats
    # count). This is a METHOD choice, not tuning: the same-subdivision comps are the correct set.
    if cfg["prefer_same_subdivision"] and len(same) >= cfg["min_same_subdivision"]:
        top = same[: cfg["top_n"]]
        mode = "same_subdivision"
    else:
        top = area_top
        mode = "area"

    median = _median_adj(top)
    psf = [round(c["adjustment"]["adjusted_value"] / c["gla"]) for c in top if c.get("gla")]
    divergence = round((median - area_arv) / area_arv * 100) if area_arv else 0
    return {
        "provisional_arv": median,
        "label": "estimated",   # PROVISIONAL — triage only; confirmed requires human comp confirmation
        "valuation_state": "provisional",
        "selection_mode": mode,
        "n_qualified": len(scored),
        "n_same_subdivision": len(same),
        "n_used": len(top),
        "adjusted_psf_range": [min(psf), max(psf)] if psf else None,
        "area_sanity_band_arv": area_arv,             # zip/area median for comparison
        "same_vs_area_divergence_pct": divergence,    # how far the chosen ARV sits from the area median
        "comps_ranked": scored,
        "top_comps": top,
    }


def _net_of_demolition(subject: dict, gross: int) -> dict:
    """Teardown netting — a SEPARATE, clearly-labeled line, never baked into the gross floor.
    Only meaningful when a structure still stands; for a vacant lot gross == net."""
    cfg = COMP_CONFIG["land"]
    sqft = subject.get("gla") or 0
    if not sqft:
        return {"net_of_demolition": None, "demolition_cost": None,
                "demolition_note": "no structure on record — gross land value is the net"}
    demo = max(cfg["demolition_minimum"], round(sqft * cfg["demolition_cost_per_sqft"]))
    return {"net_of_demolition": gross - demo, "demolition_cost": demo,
            "demolition_note": f"teardown: gross land − demolition ({sqft} sqft @ "
                               f"${cfg['demolition_cost_per_sqft']}/sqft, min ${cfg['demolition_minimum']:,}) "
                               f"— ESTIMATED, shown separately; the floor itself stays gross"}


def land_floor_for_subject(subject: dict, client: Optional["BridgeClient"] = None,
                           as_of: Optional[datetime.date] = None) -> dict:
    """Fetch + price the §G land floor. Uses the 12mo default and widens to 24mo ONLY when the band is
    too thin — labeled when widened, because a wide window imports stale prices in a rising market."""
    cfg = COMP_CONFIG["land"]
    client = client or BridgeClient()
    months = cfg["recency_months"]
    fl = land_floor(subject, fetch_land_candidates(subject, client, as_of, months), as_of)
    widened = False
    if fl.get("n", 0) < cfg["min_comps_before_widen"]:
        wm = cfg["widen_recency_months"]
        w = land_floor(subject, fetch_land_candidates(subject, client, as_of, wm), as_of)
        if w.get("n", 0) > fl.get("n", 0):
            fl, months, widened = w, wm, True
    fl["recency_months_used"] = months
    fl["recency_widened"] = widened
    if widened:
        fl["note"] = (fl.get("note", "") + f" · recency WIDENED to {months}mo (band was thin) — "
                      f"older comps may understate in a rising market")
    return fl
