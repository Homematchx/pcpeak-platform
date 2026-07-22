"""
acquisition.py — Acquisition Intelligence engine (STAGE 1: config + calculators + labeling).

Downstream, read-only analysis over property_intel enrichment. NO scraping, NO NTREIS in Stage 1.
See docs/acquisition-intelligence-design.md. Transaction model = pre-foreclosure, direct-from-owner:
no §34.21 redemption; the countdown is oos_date/sale_scheduled_date; we inherit the full lien stack.

Two calculators kept STRICTLY separate (design §5):
  MAO_rule  = (ARV × Rule%) − Repairs          our ceiling; taxes/liens are NOT deducted
  SellerNet = AgreedPrice − TaxPayoff − LienPayoffs − SellerClosingCosts   rep-facing negotiation
  FATAL GATE: TotalPayoffs > AgreedPrice → seller nets negative → the deal cannot close.
The lien stack determines closability, not our ceiling.

Every human-facing financial leaf is a Labeled value carrying one of (design §12):
  VERIFIED   — from a controlling record (assessor value, a confirmed comp close price)
  ESTIMATED  — subject condition, rehab, ARV-from-adjustments
  INFERRED   — distress/occupancy signals, condition class from depreciation
  UNAVAILABLE— e.g. the lien stack before a human enters it (a BLOCKING state, never 0/blank)

Stage-1 boundary: ARV is an INPUT (from human-supplied comps). The NTREIS comp engine that produces
ARV is Stage 2 and is gated on this stage validating against the golden cases.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Optional

# ── assumption labels ──────────────────────────────────────────────────────────────────────────
VERIFIED = "verified"
ESTIMATED = "estimated"
INFERRED = "inferred"
UNAVAILABLE = "unavailable"


def labeled(value, label, note: str = "") -> dict:
    """A human-facing leaf: a number can never be displayed without its evidence label (design §12)."""
    return {"value": value, "label": label, "note": note}


# ── tunable configuration (Dallas defaults) ──────────────────────────────────────────────────────
# EVERY magnitude below is a named, tunable default — never an inline literal (the fee-constant
# lesson). Changing a default is a signed-off action, like CITY_DATA. Values marked [SIGN-OFF] are
# placeholders that MUST be replaced with a real Dallas market source before these numbers are
# trusted for a live offer; they are internally consistent and correct in FORM, not yet calibrated.
ACQ_CONFIG = {
    # MAO rule ladder — the standard flip-screening percentages (framework §XI).
    "rule_ladder": [0.60, 0.65, 0.70, 0.75, 0.80],

    # Recommended rule% by risk tier. Our default deal is a tax-foreclosure with limited inspection
    # and quantifiable-but-unresolved title/lien risk → 0.65 baseline (framework §XI). [SIGN-OFF]
    "rule_pct_by_risk": {"clean": 0.75, "standard": 0.70, "tax_foreclosure": 0.65, "severe": 0.60},

    # Subject condition (exterior-only; NO interior access). Depreciation_pct → condition class.
    # Thresholds are [lo, hi) on DCAD depreciation_pct. [SIGN-OFF]
    "condition_class_by_depreciation": [
        ("C2", 0, 12), ("C3", 12, 25), ("C4", 25, 42), ("C5", 42, 58), ("C6", 58, 1000),
    ],
    # Rehab $/sqft by condition class (Dallas, gut-to-cosmetic ladder). [SIGN-OFF]
    "rehab_psf_by_class": {"C1": 0, "C2": 12, "C3": 25, "C4": 40, "C5": 60, "C6": 85},
    # No-interior contingency: we cannot see inside a pre-foreclosure home → widen the high estimate.
    # Design [OPEN #3] resolved to 25% default (band 20–30). [SIGN-OFF band]
    "rehab_low_haircut": 0.15,          # rehab_low = base × (1 − haircut)
    "no_interior_contingency": 0.25,    # rehab_high = base × (1 + contingency)

    # Itemized MAO (Mode B cross-check) — costs as % of exit value / flat, Dallas-typical. [SIGN-OFF]
    "selling_cost_pct": 0.07,           # agent + closing on the resale
    "carry_months": 6,                  # base hold
    "carry_cost_per_month": 350,        # taxes/insurance/utilities/interest while held
    "title_cure_default": 2500,         # routine curative (not a full quiet-title)
    "possession_cost_default": 0,       # negotiated in the purchase in our model; 0 unless known
    "legal_cost_default": 1500,
    "financing_cost_default": 0,        # cash by default
    "itemized_contingency_pct": 0.05,   # on the exit value
    "required_profit": {"mode": "pct_of_arv", "value": 0.15},   # or {"mode":"fixed","value":$}

    # Seller Net Sheet — seller-side closing costs (title policy, escrow, prorations). [SIGN-OFF]
    "seller_closing_cost_pct": 0.02,

    # TAX PAYOFF MODEL (design §5, corrected 2026-07-19):
    #   PRIMARY: the ACT live balance (property_intel.current_tax_balance) IS the tax payoff —
    #   used AS-IS, never re-derived or accrued upon (it already reflects taxes+penalties+interest
    #   to date). Labeled VERIFIED.
    #   FALLBACK (only when NO live balance exists): estimate the tax debt from the petition filing
    #   amount grown by statutory penalty/interest. Labeled ESTIMATED. This is the ONLY remaining
    #   use of the accrual formula.
    "payoff": {"fallback_monthly_interest_rate": 0.015},

    # Tax-suit attorney fees (§33.48) — a SEPARATE Seller-Net line, ESTIMATED until the LGBS payoff
    # letter confirms. NOT folded into the tax payoff. Rate applied to the tax balance. [SIGN-OFF]
    "tax_suit_atty_fee_rate": {"pre_judgment": 0.15, "post_judgment": 0.20},

    # ARV sanity band — DCAD market value is NEVER a valuation source (design §5, valuation
    # hierarchy). It is only a sanity check: flag when a CONFIRMED comp ARV diverges from assessed
    # by more than this. Stage-2 wiring. [SIGN-OFF]
    "arv_sanity_band_pct": 0.30,

    # Mission Score weights — design [OPEN #10] approved as tunable config with framework defaults.
    # Redemption certainty DROPPED (N/A pre-foreclosure); possession reframed as negotiated. Weights
    # sum to 1.0. [SIGN-OFF on the reweight before Mission Score is treated as authoritative.]
    "mission_weights": {
        "title_lien_certainty": 0.25,
        "valuation_confidence": 0.20,
        "margin_strength": 0.15,
        "condition_confidence": 0.15,
        "exit_liquidity": 0.10,
        "timeline_reliability": 0.10,
        "occupancy_clarity": 0.05,
    },
}


# ── normalized case input ────────────────────────────────────────────────────────────────────────
@dataclass
class CaseInput:
    """The read-only facts pulled from a case + its property_intel blob (design §3.4).

    `owed` is the LIVE current_tax_balance from property_intel — NOT total_due_filing (design §3.4)."""
    case_number: str
    market_value: Optional[float] = None          # DCAD assessor market value (VERIFIED-assessor)
    owed: Optional[float] = None                  # live current_tax_balance
    total_due_filing: Optional[float] = None      # petition Exhibit-A grand total
    filed_date: Optional[str] = None              # "YYYY-MM-DD"
    judgment_date: Optional[str] = None
    living_area_sqft: Optional[float] = None
    depreciation_pct: Optional[float] = None
    actual_age: Optional[int] = None
    year_built: Optional[int] = None
    distress_level: Optional[str] = None          # critical|high|moderate|low
    distress_signals: list = field(default_factory=list)   # list of signal 'type' strings
    estate: bool = False                          # estate_heir OR owner_type=='estate' OR estate_flag
    is_absentee: bool = False
    no_homestead: bool = False
    property_type: str = "real"                   # 'real' | 'personal' | 'unknown'
    case_track: Optional[str] = None
    oos_date: Optional[str] = None
    sale_scheduled_date: Optional[str] = None
    owner_of_record: Optional[str] = None         # DCAD current owner (may differ from defendant → heir)
    defendant: Optional[str] = None               # the sued party; owner_of_record ≠ defendant = title question


@dataclass
class AcquisitionInputs:
    """Human-supplied acquisition inputs. In Stage 1 ARV is passed in; Stage 2 sources it from comps.

    lien_status: 'unavailable' (no lien discovery yet) | 'partial' | 'verified'. When 'unavailable'
    the fatal gate returns INDETERMINATE — never a false GO (design §5.3, [OPEN #1] manual entry)."""
    arv: Optional[float] = None                   # after-repair value (Stage 2: from confirmed comps)
    arv_label: str = ESTIMATED
    repair_estimate: Optional[float] = None       # override; else engine estimates from condition
    agreed_price: Optional[float] = None          # negotiated with the seller (Seller Net input)
    lien_stack: list = field(default_factory=list)  # [{type,amount,holder,source,verified(bool)}]
    lien_status: str = "unavailable"
    rule_pct_override: Optional[float] = None


# ── date helper (deterministic: as_of is injectable for testing) ────────────────────────────────
def _months_between(start: Optional[str], as_of: datetime.date) -> int:
    if not start:
        return 0
    try:
        d = datetime.date.fromisoformat(start[:10])
    except (ValueError, TypeError):
        return 0
    return max(0, (as_of.year - d.year) * 12 + (as_of.month - d.month))


# ── calculator 1 (part): tax payoff = ACT live balance, used AS-IS ───────────────────────────────
def tax_payoff(case: CaseInput, as_of: Optional[datetime.date] = None) -> dict:
    """The tax payoff = the ACT live balance (current_tax_balance), used AS-IS (design §5, corrected
    2026-07-19). Never re-derived, never accrued upon — it already reflects taxes+penalties+interest
    to date. Only when NO live balance exists do we fall back to a §33.48 estimate from the filing
    amount, labeled ESTIMATED. Returns {amount, label, basis, note}."""
    as_of = as_of or datetime.date.today()
    live = case.owed
    if live and live > 0:
        return {"amount": round(live), "label": VERIFIED, "basis": "act_live_balance",
                "note": "ACT current amount due — used as-is"}
    filed = case.total_due_filing or 0.0
    if filed > 0:
        months = _months_between(case.filed_date, as_of)
        est = filed + filed * (ACQ_CONFIG["payoff"]["fallback_monthly_interest_rate"] * months)
        return {"amount": round(est), "label": ESTIMATED, "basis": "fallback_estimate",
                "note": f"NO live balance — est. from filing ${filed:,.0f} + {months}mo penalty/interest"}
    return {"amount": None, "label": UNAVAILABLE, "basis": "none", "note": "no live balance or filing amount"}


def tax_suit_attorney_fees(case: CaseInput, tax_payoff_amount: Optional[float]) -> dict:
    """§33.48 tax-suit attorney fees — a SEPARATE Seller-Net line, ESTIMATED until confirmed by the
    LGBS payoff letter. Never folded into the tax payoff. Estimated as a % of the tax balance
    (post-judgment fees run higher)."""
    if not tax_payoff_amount:
        return {"amount": 0, "label": ESTIMATED, "note": "no tax balance"}
    rate = ACQ_CONFIG["tax_suit_atty_fee_rate"]["post_judgment" if case.judgment_date else "pre_judgment"]
    return {"amount": round(tax_payoff_amount * rate), "label": ESTIMATED,
            "note": f"~{int(rate*100)}% of balance — estimated until LGBS payoff letter"}


def arv_sanity_band(comp_arv: Optional[float], market_value: Optional[float],
                    tolerance: Optional[float] = None) -> dict:
    """Sanity check ONLY (design §5, valuation hierarchy). DCAD market value is NEVER a valuation
    source — this flags when a CONFIRMED comp ARV diverges sharply from assessed. No offer number
    ever rests on this. Stage-2 wiring (needs comp ARV); here so the hierarchy is locked in code."""
    tol = tolerance if tolerance is not None else ACQ_CONFIG["arv_sanity_band_pct"]
    if not comp_arv or not market_value:
        return {"flag": False, "note": "insufficient data"}
    diff = (comp_arv - market_value) / market_value
    return {"flag": abs(diff) > tol, "divergence_pct": round(diff * 100),
            "note": f"confirmed comp ARV {'+' if diff >= 0 else ''}{round(diff*100)}% vs DCAD assessed "
                    f"(sanity band ±{int(tol*100)}%)"}


# ── subject condition estimate (exterior-only; always ESTIMATED/INFERRED) ────────────────────────
def condition_estimate(case: CaseInput) -> dict:
    """Estimate condition class + a 3-point rehab from DCAD depreciation, age, and distress signals.
    Subject is exterior-only — this is NEVER verified (design §4). Interior contingency baked in."""
    cfg = ACQ_CONFIG
    dep = case.depreciation_pct
    cls = None
    if dep is not None:
        for name, lo, hi in cfg["condition_class_by_depreciation"]:
            if lo <= dep < hi:
                cls = name
                break
    # Distress override: a vacant/teardown signal can't be better than C5.
    sig = set(case.distress_signals or [])
    if ({"vacant", "distressed"} & sig) and (cls is None or cls < "C5"):
        cls = "C5" if cls is None else max(cls, "C5")
    if cls is None:
        cls = "C4"  # default when depreciation is unknown: assume fair, needs interior confirmation

    psf = cfg["rehab_psf_by_class"].get(cls, 40)
    sqft = case.living_area_sqft or 0
    base = psf * sqft
    low = base * (1 - cfg["rehab_low_haircut"])
    high = base * (1 + cfg["no_interior_contingency"])

    note = f"DCAD depreciation {dep}%" if dep is not None else "depreciation unknown"
    if case.actual_age:
        note += f", age {case.actual_age}"
    if sig:
        note += f", distress[{','.join(sorted(sig))}]"
    return {
        "condition_class": labeled(cls, INFERRED, note),
        "rehab_low": labeled(round(low), ESTIMATED, f"{cls} @ ${psf}/sqft × {sqft} sqft, −{int(cfg['rehab_low_haircut']*100)}%"),
        "rehab_base": labeled(round(base), ESTIMATED, f"{cls} @ ${psf}/sqft × {sqft} sqft"),
        "rehab_high": labeled(round(high), ESTIMATED, f"+{int(cfg['no_interior_contingency']*100)}% no-interior contingency"),
        "interior_access": labeled(False, VERIFIED, "pre-foreclosure — no interior access"),
    }


# ── risk tier → recommended rule% ────────────────────────────────────────────────────────────────
def recommend_rule_pct(case: CaseInput, acq: AcquisitionInputs, cond_class: str) -> dict:
    cfg = ACQ_CONFIG["rule_pct_by_risk"]
    reasons = []
    tier = "tax_foreclosure"  # our baseline: limited inspection + title/lien risk (framework §XI)
    reasons.append("baseline: tax-foreclosure, limited inspection")
    if cond_class == "C6" or case.distress_level == "critical" or (acq.lien_status == "unavailable" and case.estate):
        tier = "severe"
        reasons.append("severe: C6 / critical distress / estate with liens unknown")
    if acq.rule_pct_override is not None:
        return {"pct": acq.rule_pct_override, "tier": "override", "reasons": ["manual override"]}
    return {"pct": cfg[tier], "tier": tier, "reasons": reasons}


# ── calculator 1: MAO (our ceiling; taxes/liens NOT deducted) ────────────────────────────────────
def mao_ladder(arv: float, repairs: float) -> dict:
    """MAO_rule = (ARV × Rule%) − Repairs for the full ladder. Taxes/liens deliberately NOT deducted
    (framework §XII double-count rule — the (1−rule%) margin already absorbs profit/carry/selling)."""
    return {f"{int(p*100)}": round(arv * p - repairs) for p in ACQ_CONFIG["rule_ladder"]}


def mao_itemized(arv: float, repairs: float, tax_payoff_total: float, lien_payoffs: float) -> dict:
    """Mode B cross-check (framework §XIII). Here taxes+liens ARE subtracted (it's a full cost stack,
    NOT the rule screen). If this diverges sharply from the rule ladder, the deal needs a hard look."""
    cfg = ACQ_CONFIG
    selling = arv * cfg["selling_cost_pct"]
    carry = cfg["carry_months"] * cfg["carry_cost_per_month"]
    contingency = arv * cfg["itemized_contingency_pct"]
    rp = cfg["required_profit"]
    required_profit = arv * rp["value"] if rp["mode"] == "pct_of_arv" else rp["value"]
    deductions = {
        "repairs": repairs,
        "taxes_and_liens": tax_payoff_total + lien_payoffs,
        "title_cure": cfg["title_cure_default"],
        "possession": cfg["possession_cost_default"],
        "legal": cfg["legal_cost_default"],
        "carry": carry,
        "financing": cfg["financing_cost_default"],
        "selling": selling,
        "contingency": contingency,
        "required_profit": required_profit,
    }
    mao = arv - sum(deductions.values())
    return {"mao": round(mao), "exit_value": arv, "deductions": {k: round(v) for k, v in deductions.items()}}


# ── calculator 2: Seller Net Sheet + the fatal closability gate ──────────────────────────────────
def seller_net_sheet(case: CaseInput, acq: AcquisitionInputs, tax_pay: dict) -> dict:
    """SellerNet = AgreedPrice − ACT live balance − tax-suit attorney fees − mowing/labor liens
    − seller closing costs (design §5.2, corrected 2026-07-19). Each is a SEPARATE labeled line.
    FATAL GATE: TotalPayoffs > AgreedPrice → seller nets negative → cannot close (design §5.3).
    Lien status 'unavailable' → closable is None (INDETERMINATE), never a false GO."""
    tax_amount = tax_pay["amount"] or 0
    atty = tax_suit_attorney_fees(case, tax_amount)          # separate line, estimated
    # A lien entry with amount=None is IDENTIFIED-but-UNQUANTIFIED (e.g. a recorded LLC interest with
    # no dollar figure yet). It can't be summed and it holds closability INDETERMINATE — distinct from
    # 'unavailable' (no lien data at all). Either way the total can't be trusted.
    lien_vals = [l.get("amount") for l in (acq.lien_stack or [])]
    has_unquantified = any(v is None for v in lien_vals)
    lien_amount = sum(v for v in lien_vals if v)              # quantified liens only
    liens_known = (acq.lien_status != "unavailable") and not has_unquantified

    agreed = acq.agreed_price
    seller_closing = (agreed or 0) * ACQ_CONFIG["seller_closing_cost_pct"]
    total_payoffs = tax_amount + atty["amount"] + lien_amount + seller_closing

    if agreed is None:
        seller_net, closable, gate = None, None, "no_agreed_price"
    elif not liens_known:
        seller_net, closable, gate = agreed - total_payoffs, None, "indeterminate_liens_unavailable"
    else:
        seller_net = agreed - total_payoffs
        closable = seller_net >= 0
        gate = "closable" if closable else "fatal_negative_seller_net"

    lien_note = ("identified but UNQUANTIFIED — title/instrument value required" if has_unquantified
                 else (acq.lien_status if liens_known else "estimated until title search"))
    return {
        "agreed_price": labeled(agreed, VERIFIED if agreed is not None else UNAVAILABLE, "negotiated"),
        "tax_payoff": labeled(tax_amount, tax_pay["label"], tax_pay["note"]),
        "tax_suit_attorney_fees": labeled(atty["amount"], atty["label"], atty["note"]),
        "mowing_labor_and_other_liens": labeled(
            round(lien_amount) if liens_known else None,
            (VERIFIED if lien_amount else ESTIMATED) if liens_known else UNAVAILABLE, lien_note),
        "seller_closing_costs": labeled(round(seller_closing), ESTIMATED,
                                        f"{int(ACQ_CONFIG['seller_closing_cost_pct']*100)}% of price"),
        "total_payoffs": labeled(round(total_payoffs) if agreed is not None else None,
                                 ESTIMATED if liens_known else UNAVAILABLE,
                                 "minimum agreed price for the seller to break even"),
        "seller_net": labeled(round(seller_net) if seller_net is not None else None,
                              ESTIMATED if seller_net is not None else UNAVAILABLE),
        "closable": closable,
        "gate": gate,
    }


# Gate severity model (design §7, decision-table §7):
#   fatal       → NO-GO (overrides everything)
#   substantive → an IDENTIFIED condition that shapes a real conditional verdict (a named owner-mismatch
#                 to convey through, an identified-but-unpriced lien). LIFTS to GO-WITH-CONDITIONS
#                 regardless of valuation state (the Ruby / TX-23-00553 pattern).
#   generic     → a "still-gathering / soft-signal" state (liens not yet entered, absentee/estate with
#                 NO owner-mismatch, unconfirmed property type). Does NOT by itself lift out of HOLD;
#                 with a CONFIRMED valuation it becomes a listed condition (GO-WITH-CONDITIONS).
_NAME_NOISE = {"ET", "AL", "ETAL", "ESTATE", "EST", "OF", "HEIRS", "HEIR", "DEVISEE", "DECEASED", "THE",
               "AND", "LIFE", "JR", "SR", "III", "LLC", "INC", "TRUST", "TRUSTEE", "AKA", "DBA"}


def _name_tokens(name: Optional[str]) -> set:
    if not name:
        return set()
    return {t for t in re.findall(r"[A-Za-z]{2,}", name.upper()) if t not in _NAME_NOISE and len(t) >= 3}


def owner_defendant_mismatch(case: CaseInput) -> bool:
    """SUBSTANTIVE title question (the graduated heir gate): the DCAD owner-of-record is a DIFFERENT
    named party than the sued defendant — an identified counterpart to convey / quiet-title through
    (Ruby: TAYLOR FELICIA D ≠ Ruby Faye Brown; TX-23-00553: BACA NORMA ESTELA ≠ Pauline Hernandez).
    True only when BOTH are named and share NO significant name token; a shared token = same party.
    Absentee/estate language WITHOUT such a mismatch is a generic soft signal, not this."""
    o, d = _name_tokens(case.owner_of_record), _name_tokens(case.defendant)
    if not o or not d:
        return False
    return o.isdisjoint(d)


def deal_gates(case: CaseInput, acq: AcquisitionInputs, seller: dict) -> list:
    gates = []
    if case.property_type == "personal":
        gates.append({"gate": "bpp", "severity": "fatal", "detail": "business personal property — not a real-estate deal"})
    if case.property_type == "unknown":
        gates.append({"gate": "property_type_unknown", "severity": "generic", "detail": "docket Comment missing — confirm real vs personal before analysis"})
    if seller["gate"] == "fatal_negative_seller_net":
        gates.append({"gate": "unclosable", "severity": "fatal",
                      "detail": "Total payoffs exceed agreed price — seller nets negative, deal cannot close"})
    # Identified-but-unquantified lien (e.g. a recorded LLC interest with no dollar figure): SUBSTANTIVE
    # — a specific instrument to quantify. The engine must NEVER emit GO while a known lien is unpriced.
    unquantified = [l for l in (acq.lien_stack or []) if l.get("amount") is None]
    if unquantified:
        holders = "; ".join(l.get("holder", "unknown holder") for l in unquantified)
        gates.append({"gate": "identified_unquantified_lien", "severity": "substantive",
                      "detail": f"Identified but UNQUANTIFIED lien/interest ({holders}) — quantify the instrument "
                                f"before any GO; closability INDETERMINATE until then"})
    elif acq.lien_status == "unavailable":
        gates.append({"gate": "lien_discovery_required", "severity": "generic",
                      "detail": "Lien stack unknown — title/lien discovery not yet done (design §5.3, OPEN #1)"})
    # Heir/estate title — GRADUATED (minimal Stage-3 graduated gate). A real owner-mismatch (named
    # counterpart) is SUBSTANTIVE and lifts; absentee/estate language without a mismatch is a GENERIC
    # soft signal that does not lift out of HOLD on its own.
    if owner_defendant_mismatch(case):
        gates.append({"gate": "heir_estate_title", "severity": "substantive",
                      "detail": f"DCAD owner '{case.owner_of_record}' differs from the defendant "
                                f"'{case.defendant}' — identified conveyance-path/title question; confirm who can convey"})
    elif (case.estate or case.is_absentee) and case.owner_of_record:
        gates.append({"gate": "estate_absentee_signal", "severity": "generic",
                      "detail": "Estate/absentee signal (no confirmed owner-mismatch) — soft note; verify who conveys"})
    return gates


def structural_unclosability(case: CaseInput, acq: AcquisitionInputs, mao: dict, payoff: dict) -> Optional[dict]:
    """A pre-negotiation FATAL verdict: if the minimum payoffs (tax + attorney fees + known liens)
    exceed our MAO ceiling at EVERY rule%, no price can both satisfy the seller and stay under our
    ceiling — structurally unclosable regardless of negotiation. Only asserted on a CONFIRMED
    valuation (design §5.4 — a NO-GO verdict must not rest on a provisional/triage ARV)."""
    if mao is None or acq.arv_label != VERIFIED:
        return None
    known_liens = sum(l.get("amount") or 0 for l in (acq.lien_stack or []))
    atty = tax_suit_attorney_fees(case, payoff["amount"])["amount"]
    min_payoffs = (payoff["amount"] or 0) + atty + known_liens
    mao_max = max(mao.values())
    if min_payoffs > mao_max:
        exceeded = [f"{k}%" for k, v in mao.items() if min_payoffs > v]
        return {"gate": "structurally_unclosable", "severity": "fatal",
                "detail": f"Minimum payoffs ${min_payoffs:,} exceed MAO at every rule% "
                          f"(max MAO ${mao_max:,} @80%; exceeded: {', '.join(exceeded)}) — no viable price exists"}
    return None


# ── Mission Score (weights are tunable config; components 0–100) ──────────────────────────────────
def mission_score(components: dict) -> dict:
    """Weighted 0–100. Components are 0–100 sub-scores; missing components make the score PROVISIONAL
    (design §6.5 — no confirmed valuation → not trusted). Weights: ACQ_CONFIG['mission_weights']."""
    w = ACQ_CONFIG["mission_weights"]
    provided = {k: v for k, v in components.items() if v is not None}
    provisional = set(w) - set(provided)
    # Renormalize over provided components so a provisional score is still on a 0–100 scale.
    wsum = sum(w[k] for k in provided) or 1.0
    score = sum(components[k] * w[k] for k in provided) / wsum
    return {
        "score": round(score),
        "provisional": bool(provisional),
        "missing_components": sorted(provisional),
        "weights": w,
    }


# ── orchestrator ─────────────────────────────────────────────────────────────────────────────────
def analyze(case: CaseInput, acq: AcquisitionInputs, as_of: Optional[datetime.date] = None) -> dict:
    """Full Stage-1 analysis. ARV comes from `acq` (Stage 1). Returns a structured, fully-labeled
    dict. Where ARV/agreed-price/liens are absent, the relevant outputs are UNAVAILABLE, never faked."""
    cond = condition_estimate(case)
    cond_class = cond["condition_class"]["value"]
    payoff = tax_payoff(case, as_of)          # {amount, label, basis, note} — ACT live balance as-is
    rule = recommend_rule_pct(case, acq, cond_class)

    repairs = acq.repair_estimate if acq.repair_estimate is not None else cond["rehab_base"]["value"]
    repairs_label = ESTIMATED

    mao = None
    mao_full = None
    if acq.arv is not None:
        mao = mao_ladder(acq.arv, repairs)
        lien_payoffs = sum(l.get("amount") or 0 for l in (acq.lien_stack or []))  # quantified only
        atty = tax_suit_attorney_fees(case, payoff["amount"])["amount"]
        mao_full = mao_itemized(acq.arv, repairs, (payoff["amount"] or 0) + atty, lien_payoffs)

    seller = seller_net_sheet(case, acq, payoff)
    gates = deal_gates(case, acq, seller)
    structural = structural_unclosability(case, acq, mao, payoff)   # fatal on confirmed valuation only
    if structural:
        gates.append(structural)

    # Countdown: close-or-lose deadline (design §2).
    countdown_date, countdown_basis = None, None
    if case.sale_scheduled_date:
        countdown_date, countdown_basis = case.sale_scheduled_date, VERIFIED
    elif case.oos_date:
        countdown_date, countdown_basis = case.oos_date, VERIFIED
    # (else: falls back to compute_projection in the backend integration — Stage 1 leaves it None.)

    # Mission Score components (0–100). Stage 1 fills what it can; valuation_confidence stays None
    # (provisional) until confirmed comps exist (Stage 2) → keeps the whole score provisional.
    fatal = any(g["severity"] == "fatal" for g in gates)
    components = {
        "title_lien_certainty": 20 if acq.lien_status == "unavailable" else (85 if acq.lien_status == "verified" else 55),
        "valuation_confidence": None if acq.arv_label != VERIFIED else 85,   # provisional until confirmed comps
        "margin_strength": None,          # needs ARV vs basis — filled once ARV+agreed price both present
        "condition_confidence": 45,       # exterior-only estimate — capped (design §4)
        "exit_liquidity": None,           # Stage 2 (comps/market)
        "timeline_reliability": 70 if (case.oos_date or case.sale_scheduled_date) else 40,
        "occupancy_clarity": 40 if (case.is_absentee or case.no_homestead or case.estate) else 60,
    }
    if acq.arv is not None and acq.agreed_price:
        equity = acq.arv - acq.agreed_price - repairs
        components["margin_strength"] = max(0, min(100, round(equity / acq.arv * 200))) if acq.arv else None
    ms = mission_score(components)

    # Decision table (design §7, corrected 2026-07-21). A PROVISIONAL/unconfirmed valuation never by
    # itself lifts a case out of HOLD (design §5.4 — provisional is triage-only, never trusted). The
    # ONLY things that lift out of HOLD are: a fatal gate (→ NO-GO), a SUBSTANTIVE identified condition
    # (→ GO-WITH-CONDITIONS regardless of valuation — Ruby's unquantified lien, TX-23-00553's owner-
    # mismatch), or a CONFIRMED valuation. A GENERIC gate (liens-not-yet-entered, absentee/estate with
    # no mismatch) is a listed condition only once the valuation is confirmed. GO requires a confirmed
    # valuation with nothing outstanding.
    substantive = any(g["severity"] == "substantive" for g in gates)
    generic = any(g["severity"] == "generic" for g in gates)
    confirmed_valuation = acq.arv is not None and acq.arv_label == VERIFIED
    if fatal:
        decision = "NO-GO"
    elif substantive:
        decision = "GO-WITH-CONDITIONS"      # an identified condition — lifts regardless of valuation
    elif confirmed_valuation:
        decision = "GO-WITH-CONDITIONS" if generic else "GO"
    else:
        decision = "HOLD"                    # unconfirmed valuation + only generic/no conditions

    return {
        "case_number": case.case_number,
        "as_of": (as_of or datetime.date.today()).isoformat(),
        "market_value": labeled(case.market_value, VERIFIED, "DCAD assessor"),
        "owed_live": labeled(case.owed, VERIFIED, "live ACT balance"),
        "condition": cond,
        "tax_payoff": payoff,
        "recommended_rule_pct": rule,
        "repairs_used": labeled(repairs, repairs_label,
                                "override" if acq.repair_estimate is not None else "engine condition estimate"),
        "arv": labeled(acq.arv, acq.arv_label if acq.arv is not None else UNAVAILABLE,
                       "Stage-1 input (Stage-2: confirmed comps)"),
        "mao_ladder": mao,          # None until ARV supplied
        "mao_itemized": mao_full,   # None until ARV supplied
        "seller_net_sheet": seller,
        "gates": gates,
        "countdown": {"date": countdown_date, "basis": countdown_basis},
        "mission_score": ms,
        "decision": decision,
        "valuation_state": "confirmed" if acq.arv_label == VERIFIED else "provisional",
    }
