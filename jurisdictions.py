#!/usr/bin/env python3
"""Collector identity and per-collector payoff plumbing (design §17.3, §22, §23).

WHY THIS EXISTS. `property_intel.current_tax_balance` is ONE scalar from ONE collector (Dallas
County / ACT). On a parcel whose school district or city collects its own taxes, that scalar is not
slightly short — measured on 3909 Cambridge Dr it was **23% of the true payoff** ($5,974.81 of
$25,749.87). A payoff that wrong in a target market is a broken instrument, so the payoff model has
to carry ONE LINE PER COLLECTOR, each labeled independently.

THE THREE RULES THIS MODULE ENFORCES

  1. MEMBERSHIP COMES FROM THE PETITION, NEVER FROM THE ADDRESS. The suit names its plaintiff taxing
     districts, and a district that sued is definitionally owed. City→district mapping is wrong in
     BOTH directions (Rowlett reads no-ISD in ACT but is Garland ISD territory; Garland city contains
     Richardson and Dallas ISD parcels), which is the §19 "local truth applied fleet-wide" shape.
     `tax_breakdown[].entity` — already captured on 321/334 cases — IS that list.

  2. ABSENCE IS `unavailable`, NEVER $0. A named collector we cannot reach yet is a KNOWN debt of
     UNKNOWN size. It reads `unavailable` and pushes closability to INDETERMINATE exactly as an
     unquantified lien does (§5.3). It must never contribute 0 to a total.

  3. COVERAGE IS DEFINED BY WHAT THE PARCEL OWES, NOT BY WHAT WE HAVE INTEGRATED. A collector with no
     adapter is still surfaced as a named, unreachable line. Building "one more portal each time we
     trip over one" would make coverage a function of our discovery history — the §19 shape again.

PLATFORMS ARE ADAPTERS BEHIND ONE INTERFACE. `texaspayments.com` (GDS — agency-code parameterised,
roster-driven) and Irving's own ACT instance are two adapters over one registry. Adding a platform
means adding an adapter, never touching this flow. No adapter is implemented in this increment: every
external line is `unavailable` until one lands, which is the honest state and is already correct.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
GDS_ROSTER_PATH = ROOT / "data" / "gds_agency_roster.json"

VERIFIED, ESTIMATED, UNAVAILABLE = "verified", "estimated", "unavailable"


# ── canonical names ──────────────────────────────────────────────────────────────────────────────
# Petitions suffix a unit with the tract and years it applies to — "GARLAND ISD - TRACT 1 (2022)",
# "CITY OF GARLAND - TRACT 2 (2020)", "DALLAS COUNTY (TRACT 1)". Those are the SAME collector billing
# different tracts of one parcel, and treating them as distinct units would fragment the roster and
# hide real coverage. Strip the qualifier; `petition_collectors` sums the rows back together.
_NOISE = re.compile(
    r"\s*[-–]?\s*\(?(?:TRACT|TR)\s*\d+\)?(?:\s*\(\s*\d{4}(?:\s*[-–]\s*\d{4})?\s*\))?\s*$"
    r"|\s*\(\s*\d{4}(?:\s*[-–]\s*\d{4})?\s*\)\s*$"
    r"|\s+N/?K/?A\s+.*$|\s+F/?K/?A\s+.*$", re.I)
_ALIAS = {
    "DALLAS COUNTY COMMUNITY COLLEGE DISTRICT": "DALLAS COLLEGE",
    "PARKLAND HOSPITAL DISTRICT": "PARKLAND HOSPITAL",
    "DALLAS COUNTY SCHOOL EQUALIZATION FUND": "SCHOOL EQUALIZATION",
    "DALLAS INDEPENDENT SCHOOL DISTRICT": "DALLAS ISD",
    "GARLAND INDEPENDENT SCHOOL DISTRICT": "GARLAND ISD",
    "MESQUITE INDEPENDENT SCHOOL DISTRICT": "MESQUITE ISD",
    "RICHARDSON INDEPENDENT SCHOOL DISTRICT": "RICHARDSON ISD",
    "IRVING INDEPENDENT SCHOOL DISTRICT": "IRVING ISD",
    "CARROLLTON-FARMERS BRANCH INDEPENDENT SCHOOL DISTRICT": "CARROLLTON-FARMERS BRANCH ISD",
    "CEDAR HILL INDEPENDENT SCHOOL DISTRICT": "CEDAR HILL ISD",
    "GRAND PRAIRIE INDEPENDENT SCHOOL DISTRICT": "GRAND PRAIRIE ISD",
    "DUNCANVILLE INDEPENDENT SCHOOL DISTRICT": "DUNCANVILLE ISD",
    "LANCASTER INDEPENDENT SCHOOL DISTRICT": "LANCASTER ISD",
    "DESOTO INDEPENDENT SCHOOL DISTRICT": "DESOTO ISD",
    "SUNNYVALE INDEPENDENT SCHOOL DISTRICT": "SUNNYVALE ISD",
    "COPPELL INDEPENDENT SCHOOL DISTRICT": "COPPELL ISD",
    "HIGHLAND PARK INDEPENDENT SCHOOL DISTRICT": "HIGHLAND PARK ISD",
}


def canonical(entity):
    """One spelling per taxing unit. Petitions, ACT and the portals each write them differently."""
    if not entity:
        return ""
    n = _NOISE.sub("", str(entity)).upper().strip(" .,")
    n = re.sub(r"\s+", " ", n)
    return _ALIAS.get(n, n)


# ── who collects what ────────────────────────────────────────────────────────────────────────────
# Units observed being BILLED BY ACT on real parcels (dallasact.com taxbyyearbyunit, measured
# 2026-08-15/16 across every city code in the book). ACT billing them is what makes
# `current_tax_balance` complete for those units — and ONLY those.
ACT_COLLECTED = {
    "DALLAS COUNTY", "DALLAS COLLEGE", "PARKLAND HOSPITAL", "SCHOOL EQUALIZATION",
    "DALLAS COUNTY TAX OFFICE", "TAX CERTIFICATES",
    "CITY OF DALLAS", "DALLAS ISD",
    "CITY OF MESQUITE", "MESQUITE ISD",
    "CITY OF CEDAR HILL", "CEDAR HILL ISD",
    "CITY OF GRAND PRAIRIE", "GRAND PRAIRIE ISD",
    "CITY OF CARROLLTON", "CITY OF FARMERS BRANCH", "CITY OF IRVING", "CITY OF RICHARDSON",
    "CITY OF ROWLETT", "CITY OF SEAGOVILLE", "CITY OF WILMER", "CITY OF HUTCHINS",
    "TOWN OF ADDISON",
}

# Self-collecting offices. `platform` selects the ADAPTER; `agency` is looked up from the GDS roster
# BY NAME at runtime — deliberately not a hardcoded id, the same discipline that the DALLAS|PARKLAND
# regex violated. `adapter` is False everywhere until an adapter increment ships.
EXTERNAL_COLLECTORS = {
    "GARLAND ISD":                   {"platform": "gds",        "roster_name": "Garland ISD Tax Office"},
    "CITY OF GARLAND":               {"platform": "gds",        "roster_name": "City of Garland Tax Office"},
    "RICHARDSON ISD":                {"platform": "gds",        "roster_name": "Richardson ISD Tax Office"},
    "CARROLLTON-FARMERS BRANCH ISD": {"platform": "gds",        "roster_name": "Carrollton-Farmers Branch ISD Tax Office"},
    # `act_path` is the district's own ACT instance segment — registry DATA, so a second
    # self-hosted ACT district is an entry here rather than a literal in the fetcher.
    "IRVING ISD":                    {"platform": "irving_act", "roster_name": None,
                                      "act_path": "irving"},
}


def act_path_for(collector):
    """The ACT instance segment for a self-hosted district, or None."""
    spec = EXTERNAL_COLLECTORS.get(canonical(collector)) or {}
    return spec.get("act_path")

# Adapter registry — which platforms we can actually reach. `reachable` is derived from THIS, so a
# collector is only ever claimed as fetchable when an adapter really exists. The fetchers themselves
# live in local scraping modules (the cloud never scrapes) and are not imported here: this is a
# capability declaration, so the served engine can reason about reachability without pulling in
# Playwright.
ADAPTERS = {"gds": "collectors_gds", "irving_act": "collectors_act"}


def load_gds_roster(path=None):
    """The GDS agency roster (office name → agency code), captured from texaspayments.com. Read from
    disk so agency ids are DATA, not literals in the code."""
    p = Path(path) if path else GDS_ROSTER_PATH
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def resolve_collector(entity, roster=None):
    """Where is this taxing unit collected, and can we reach it?

    Returns {collector, scope, platform, agency, reachable}. `scope` is 'act' (inside the ACT live
    balance), 'external' (a self-collecting office) or 'unknown' (never observed — treated as
    external, because assuming ACT covers it is the very mistake this module exists to prevent)."""
    name = canonical(entity)
    if not name:
        return None
    if name in ACT_COLLECTED:
        return {"collector": name, "scope": "act", "platform": None, "agency": None, "reachable": True}
    spec = EXTERNAL_COLLECTORS.get(name)
    if spec is None:
        # UNKNOWN unit. Fail toward "we cannot account for this", never toward "ACT has it".
        return {"collector": name, "scope": "unknown", "platform": None, "agency": None, "reachable": False}
    roster = load_gds_roster() if roster is None else roster
    agency = roster.get(spec["roster_name"]) if spec["roster_name"] else None
    return {"collector": name, "scope": "external", "platform": spec["platform"],
            "agency": agency, "reachable": spec["platform"] in ADAPTERS}


def petition_collectors(tax_breakdown):
    """The plaintiff taxing districts, from the petition's own Exhibit-A breakdown. THIS IS THE
    MEMBERSHIP ORACLE — a district that sued is owed, no inference from the address involved.
    Returns [{collector, filed_amount}] deduped and canonicalised (multi-tract rows are summed)."""
    if isinstance(tax_breakdown, str):
        try:
            tax_breakdown = json.loads(tax_breakdown)
        except ValueError:
            return []
    out = {}
    for row in (tax_breakdown or []):
        if not isinstance(row, dict):
            continue
        name = canonical(row.get("entity"))
        if not name:
            continue
        amt = row.get("total")
        amt = float(amt) if isinstance(amt, (int, float)) else None
        cur = out.setdefault(name, {"collector": name, "filed_amount": None})
        if amt is not None:
            cur["filed_amount"] = (cur["filed_amount"] or 0) + amt
    return sorted(out.values(), key=lambda r: r["collector"])


def collector_lines(tax_breakdown, act_balance=None, act_units=None, fetched=None, roster=None):
    """ONE LINE PER COLLECTOR — the §17.3 schema.

    `act_units`   the units ACT bills for THIS parcel (its own jurisdiction report), or None if not
                  captured. NOTE ACT renders no unit list at all when the balance is $0, so None
                  means UNKNOWN COVERAGE — never "ACT collects nothing".
    `fetched`     {collector: amount} from an adapter. Empty until an adapter ships.

    Labels: `verified` only where a live balance actually covers the line; `estimated` for a
    petition-filing amount standing in for a live one; `unavailable` for a named collector we cannot
    currently reach. An `unavailable` line NEVER contributes a number."""
    fetched = fetched or {}
    act_units_c = normalize_act_units(act_units)
    lines, seen = [], set()

    for row in petition_collectors(tax_breakdown):
        name = row["collector"]
        seen.add(name)
        info = resolve_collector(name, roster=roster)
        in_act = (name in act_units_c) if act_units_c is not None else (info["scope"] == "act")
        if name in fetched and fetched[name] is not None:
            lines.append({"collector": name, "scope": info["scope"], "amount": round(float(fetched[name]), 2),
                          "label": VERIFIED, "basis": "collector_portal",
                          "note": f"live balance from {info['platform']}"})
        elif in_act:
            lines.append({"collector": name, "scope": "act", "amount": None, "label": VERIFIED,
                          "basis": "act_live_balance",
                          "note": "inside the ACT live balance (not billed separately)"})
        else:
            lines.append({"collector": name, "scope": info["scope"], "amount": None, "label": UNAVAILABLE,
                          "basis": "no_adapter" if info["platform"] else "collector_unmapped",
                          "note": f"NAMED IN THE SUIT, collected outside ACT"
                                  f"{' via ' + info['platform'] if info['platform'] else ''} — "
                                  f"balance not retrieved; filed amount was "
                                  f"${row['filed_amount']:,.2f}" if row["filed_amount"] else
                                  "NAMED IN THE SUIT, collected outside ACT — balance not retrieved",
                          "filed_amount": row["filed_amount"]})

    # THE NEGATIVE SIGNAL. ACT billing a unit the petition never named is fine (the petition lists
    # plaintiffs, not everyone owed). The reverse is what matters and is handled above. Here we only
    # record ACT's own coverage so the total can say whether it is complete.
    act_line = {"collector": "ACT (Dallas County) live balance", "scope": "act",
                "amount": round(float(act_balance), 2) if isinstance(act_balance, (int, float)) else None,
                "label": VERIFIED if isinstance(act_balance, (int, float)) else UNAVAILABLE,
                "basis": "act_live_balance", "note": "county-side units, used as-is"}
    return {"act": act_line, "collectors": lines,
            "act_units_known": act_units_c is not None}


def payoff_completeness(lines):
    """Is the tax payoff COMPLETE, and if not, why? This is the label defect §17.1 named: a live
    balance labelled `verified` asserts *correct* while silently implying *complete*. Only the first
    was ever checked. Returns {complete, membership_verified, unavailable_collectors, reason}.

    §33 — THE VERDICT IS TRI-STATE AND THE THIRD STATE IS LOAD-BEARING. It answers TWO questions
    that must never be collapsed into one:

      RETRIEVAL   did we reach every collector we KNOW about?   -> `unavailable_collectors`
      MEMBERSHIP  do we know the full set of levying units?     -> `membership_verified`

    `complete is True` requires BOTH. `complete is None` means retrieval succeeded but membership is
    unproven — an answer that is NOT yes. `petition_collectors` is a LOWER BOUND: the petition names
    PLAINTIFFS, not everyone who levies, so a district that levies but did not sue is invisible to
    it. Only ACT's own per-parcel unit report (`act_units`) can close that, which is why absence of
    that report yields None rather than True."""
    missing = [l["collector"] for l in lines["collectors"] if l["label"] == UNAVAILABLE]
    verified = bool(lines["act_units_known"])
    if lines["act"]["label"] == UNAVAILABLE:
        return {"complete": False, "membership_verified": verified,
                "unavailable_collectors": missing, "reason": "no live ACT balance"}
    if missing:
        return {"complete": False, "membership_verified": verified,
                "unavailable_collectors": missing,
                "reason": f"{len(missing)} named collector(s) outside ACT not retrieved: "
                          + ", ".join(missing)}
    if not verified:
        return {"complete": None, "membership_verified": False, "unavailable_collectors": [],
                "reason": "every named collector was read, but the petition names plaintiffs, not "
                          "everyone who levies — ACT per-parcel unit coverage not captured, so the "
                          "collector set is UNVERIFIED"}
    return {"complete": True, "membership_verified": True, "unavailable_collectors": [],
            "reason": "every named collector accounted for, against a verified collector set"}


# ── collector-payload shape (§19: is it a dict or a list?) ──────────────────────────────────────
# An adapter returns {collector: {amount, account, …}} PLUS, conditionally, these two keys whose
# values are LISTS, not collector entries:
#   _rejected            identity-guard discards (returned CAD ≠ requested CAD)
#   _portal_unavailable  infrastructure fault; the adapter stops rather than hammer a refusing portal
# They are deliberately stored alongside the balances so the fault survives into the record — but any
# consumer that walks `.values()` expecting a collector dict WILL break on them. That is not
# hypothetical: `collector_backfill` summed `v["amount"]` across every value and died with
# "list indices must be integers or slices, not str" the first time a portal fault occurred.
SENTINEL_KEYS = frozenset({"_rejected", "_portal_unavailable"})


def collector_amounts(payload) -> dict:
    """{collector: amount} — REAL collector entries only, sentinels and malformed rows excluded.

    The one reading of a collector payload, so no caller invents its own. Requires a dict WITH a
    numeric `amount`: a fetched $0.00 is a fact and is kept; a sentinel list is not a collector and
    is dropped."""
    out = {}
    for k, v in (payload or {}).items():
        if k in SENTINEL_KEYS or not isinstance(v, dict):
            continue
        amt = v.get("amount")
        if isinstance(amt, (int, float)):
            out[k] = amt
    return out


def collector_sentinels(payload) -> dict:
    """The non-collector diagnostics carried in the same mapping — surfaced, never silently dropped."""
    return {k: v for k, v in (payload or {}).items() if k in SENTINEL_KEYS and v}


def normalize_act_units(act_units):
    """ACT's per-parcel unit coverage as a canonical SET, or None for UNKNOWN COVERAGE (§34.2).

    ⚠ AN EMPTY LIST IS UNKNOWN, NOT "ACT COVERS NOTHING". ACT renders **no unit list at all** when a
    parcel's balance is $0 (§17.5), so `[]` is the shape absence arrives in — and reading it as a
    known-empty SET is `absence-treated-as-a-value`, §33's exact failure mode wearing the last costume
    available to it. Downstream, `act_units_known` would flip True on an empty set and
    `payoff_completeness` could then return `complete is True` for a parcel nobody ever established
    coverage for.

    This used to be `{canonical(u) for u in (act_units or [])} or None` inline — correct, but by
    accident of `or None`, unpinned, and one refactor away from silently becoming false-complete.
    It is a named function with its own test for that reason, not for tidiness."""
    if act_units is None:
        return None
    canon = {c for c in (canonical(u) for u in act_units) if c}
    return canon or None


def payoff_is_complete(completeness) -> bool:
    """§33 — the ONE reading of the tri-state, so no caller can invent its own.

    TRUE only when completeness is affirmatively established. `None` is NOT complete: the third
    state exists precisely so that UNKNOWN cannot be read as YES, and `is not False` — which passes
    for None — is the exact bug this replaces. Any surface that CLAIMS completeness (the `verified`
    label, a corroborated-zero band) must gate on this. Surfaces that govern MONEY gate on
    `unavailable_collectors` instead; see acquisition.seller_net_sheet for why."""
    return completeness.get("complete") is True
