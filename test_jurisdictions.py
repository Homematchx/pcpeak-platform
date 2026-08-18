#!/usr/bin/env python3
"""§23 — per-collector payoff schema + petition membership.

THE DEFECT THIS CLOSES: `current_tax_balance` is one collector's number. On 3909 Cambridge Dr it was
$5,974.81 against a true payoff of $25,749.87 — **23%** — because Garland ISD ($12,108.43) and City of
Garland ($7,666.63) bill separately. A payoff that wrong is not a rounding problem, and it was labelled
`verified` the whole time.

WHAT IS PINNED
  · membership comes from the PETITION, never the address (§19: city→district is wrong both ways)
  · a named-but-unreached collector is `unavailable` → INDETERMINATE, and contributes NOTHING
  · agency ids are DATA (roster file), not literals — the same discipline the DALLAS|PARKLAND regex broke
  · an UNKNOWN taxing unit is never assumed to be inside ACT
  · the gate does NOT lift a case out of HOLD (measured: as `substantive` it lifted 95/334)
"""
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import acquisition as A
import jurisdictions as J
from acquisition import AcquisitionInputs, CaseInput

ROOT = Path(__file__).parent
_passed, _failed = 0, 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  → {detail}" if detail else ""))


# Real Exhibit-A rows from the live book.
GARLAND_TB = [{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 6991.30},
              {"entity": "CITY OF GARLAND", "total": 4337.90}]
DALLAS_TB = [{"entity": "DALLAS COUNTY", "total": 1753.46},
             {"entity": "PARKLAND HOSPITAL DISTRICT", "total": 1849.64},
             {"entity": "DALLAS COLLEGE", "total": 907.67},
             {"entity": "DALLAS COUNTY SCHOOL EQUALIZATION FUND", "total": 42.17},
             {"entity": "DALLAS INDEPENDENT SCHOOL DISTRICT", "total": 8923.52},
             {"entity": "CITY OF DALLAS", "total": 5889.98}]


def test_canonical():
    print("\ncanonical names — petitions, ACT and the portals each spell units differently")
    for raw, want in [
        ("GARLAND INDEPENDENT SCHOOL DISTRICT", "GARLAND ISD"),
        ("GARLAND INDEPENDENT SCHOOL DISTRICT - TRACT 1 (2022)", "GARLAND ISD"),
        ("GARLAND INDEPENDENT SCHOOL DISTRICT - TRACT 3 (2020-2022)", "GARLAND ISD"),
        ("CITY OF GARLAND - TRACT 2 (2020)", "CITY OF GARLAND"),
        ("DALLAS COUNTY (TRACT 1)", "DALLAS COUNTY"),
        ("DALLAS COUNTY COMMUNITY COLLEGE DISTRICT N/K/A DALLAS COLLEGE", "DALLAS COLLEGE"),
        ("PARKLAND HOSPITAL DISTRICT", "PARKLAND HOSPITAL"),
    ]:
        check(f"{raw[:46]!r} → {want}", J.canonical(raw) == want, J.canonical(raw))
    # A utility lien is NOT the city's ad valorem line — must stay distinct.
    check("'CITY OF GARLAND UTILITY LIEN' stays distinct from 'CITY OF GARLAND'",
          J.canonical("CITY OF GARLAND UTILITY LIEN") != "CITY OF GARLAND")


def test_membership_from_petition():
    print("\nmembership — the petition, never the address")
    cols = [c["collector"] for c in J.petition_collectors(GARLAND_TB)]
    check("Garland petition names GARLAND ISD + CITY OF GARLAND",
          cols == ["CITY OF GARLAND", "GARLAND ISD"], str(cols))
    # multi-tract rows are the SAME collector and must sum, not fragment
    tracts = [{"entity": "GARLAND ISD - TRACT 1 (2022)", "total": 100.0},
              {"entity": "GARLAND ISD - TRACT 2 (2020)", "total": 250.0}]
    got = J.petition_collectors(tracts)
    check("multi-tract rows collapse to ONE collector, amounts summed",
          len(got) == 1 and got[0]["filed_amount"] == 350.0, str(got))
    check("a JSON string breakdown parses", len(J.petition_collectors(json.dumps(GARLAND_TB))) == 2)
    check("garbage in → no collectors, no exception", J.petition_collectors("not json") == [])
    check("None → no collectors", J.petition_collectors(None) == [])


def test_resolution_and_roster():
    print("\nresolution — agency ids are DATA, and unknown is never assumed to be ACT")
    r = J.resolve_collector("GARLAND INDEPENDENT SCHOOL DISTRICT")
    check("Garland ISD resolves external/gds", r["scope"] == "external" and r["platform"] == "gds")
    check("…and its agency comes from the roster file", r["agency"] == "057909", str(r["agency"]))
    check("City of Garland is a SEPARATE collector, own agency",
          J.resolve_collector("CITY OF GARLAND")["agency"] == "057120")
    check("Irving ISD is a different PLATFORM, not a gds agency",
          J.resolve_collector("IRVING ISD")["platform"] == "irving_act")
    check("Dallas ISD is inside ACT", J.resolve_collector("DALLAS ISD")["scope"] == "act")
    # THE FAIL-SAFE DIRECTION: an unrecognised unit must never be assumed covered by ACT.
    u = J.resolve_collector("MIDTOWN PREMIUM PUBLIC IMPROVEMENT DISTRICT")
    check("an UNKNOWN taxing unit is 'unknown', never 'act'", u["scope"] == "unknown", str(u))
    check("…and is not reachable", u["reachable"] is False)
    # UPDATED §25: the gds adapter now exists, so gds collectors ARE reachable. Reachability is
    # derived from the adapter registry, so it can never over-claim — irving_act has no adapter and
    # still reports unreachable, which is what keeps its lines honestly `unavailable`.
    gds = [n for n, s in J.EXTERNAL_COLLECTORS.items() if s["platform"] == "gds"]
    check("gds collectors are reachable now that the adapter exists",
          all(J.resolve_collector(n)["reachable"] for n in gds), str(gds))
    # UPDATED §30: irving_act now has an adapter too. Reachability stays DERIVED from the registry,
    # so it can never over-claim — a collector with no mapped platform is still unreachable, which is
    # what keeps the unmapped tail honestly `unavailable`.
    check("irving_act is reachable now that its adapter exists",
          J.resolve_collector("IRVING ISD")["reachable"] is True)
    check("an UNMAPPED collector still claims no reachability",
          J.resolve_collector("CITY OF BALCH SPRINGS")["reachable"] is False)
    # §19 Q2 discipline: agency ids must not be literals in the module.
    src = (ROOT / "jurisdictions.py").read_text()
    tree = ast.parse(src)
    lits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.isdigit() and len(n.value) == 6]
    check("no hardcoded 6-digit agency id anywhere in jurisdictions.py (AST)", not lits, str(lits))


def test_lines_and_completeness():
    print("\nper-collector lines — absence is `unavailable`, never a value")
    L = J.collector_lines(GARLAND_TB, act_balance=0.0)
    ext = [l for l in L["collectors"] if l["scope"] == "external"]
    check("both Garland collectors get their own line", len(ext) == 2)
    check("each external line is `unavailable`", all(l["label"] == J.UNAVAILABLE for l in ext))
    check("an unavailable line carries NO amount", all(l["amount"] is None for l in ext))
    check("…but DOES carry the filed amount as context",
          all(l.get("filed_amount") for l in ext))
    comp = J.payoff_completeness(L)
    check("payoff is INCOMPLETE", comp["complete"] is False)
    check("…and names which collectors are missing",
          set(comp["unavailable_collectors"]) == {"GARLAND ISD", "CITY OF GARLAND"})

    # An all-ACT parcel is complete only once ACT's own coverage is known.
    LD = J.collector_lines(DALLAS_TB, act_balance=19366.44,
                           act_units=["CITY OF DALLAS", "DALLAS ISD", "DALLAS COUNTY",
                                      "DALLAS COLLEGE", "PARKLAND HOSPITAL", "SCHOOL EQUALIZATION"])
    check("an all-ACT parcel has no unavailable collector",
          J.payoff_completeness(LD)["complete"] is True)
    LD2 = J.collector_lines(DALLAS_TB, act_balance=19366.44)          # act_units NOT captured
    check("without ACT unit coverage, completeness is UNKNOWN not True",
          J.payoff_completeness(LD2)["complete"] is None)

    # A fetched balance promotes exactly one line and nothing else.
    LF = J.collector_lines(GARLAND_TB, act_balance=0.0, fetched={"GARLAND ISD": 12108.43})
    g = [l for l in LF["collectors"] if l["collector"] == "GARLAND ISD"][0]
    check("a fetched collector line is VERIFIED with its amount",
          g["label"] == J.VERIFIED and g["amount"] == 12108.43)
    check("…the other collector is still unavailable",
          J.payoff_completeness(LF)["unavailable_collectors"] == ["CITY OF GARLAND"])


def test_3909_cambridge_reconstruction():
    print("\nthe measured case — 3909 Cambridge Dr, the deal already closed")
    tb = [{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 12108.43},
          {"entity": "CITY OF GARLAND", "total": 7666.63}]
    c = CaseInput("TX-CAMBRIDGE", owed=5974.81, property_type="real", tax_breakdown=tb)
    out = A.tax_payoff_lines(c)
    check("ACT alone is what the OLD model would have offered on",
          A.tax_payoff(c)["amount"] == 5975, A.tax_payoff(c)["amount"])
    check("the new model says that payoff is INCOMPLETE", out["completeness"]["complete"] is False)
    # With both collectors fetched the true total appears — and it is 4x the ACT number.
    c2 = CaseInput("TX-CAMBRIDGE", owed=5974.81, property_type="real", tax_breakdown=tb,
                   collector_balances={"GARLAND ISD": 12108.43, "CITY OF GARLAND": 7666.63})
    out2 = A.tax_payoff_lines(c2)
    # §33 — this assertion used to read `is not False`, which PASSES FOR None and so could never
    # tell "verified complete" apart from "completeness unknown". That is the exact bug the tri-state
    # exists to prevent, sitting in the test meant to pin it. Retrieval is complete here; MEMBERSHIP
    # is not, because no act_units were supplied — so the honest verdict is None, and it is now
    # asserted exactly.
    check("every NAMED collector retrieved → retrieval complete",
          not out2["completeness"]["unavailable_collectors"])
    check("…but with no ACT unit coverage the SET is unverified → complete is None, never True",
          out2["completeness"]["complete"] is None)
    check("…and membership_verified says so in its own right",
          out2["completeness"]["membership_verified"] is False)
    check("known total is $25,750 (ACT + ISD + city)", out2["known_total"] == 25750, out2["known_total"])
    check("ACT alone was 23% of it", round(5974.81 / 25749.87, 3) == 0.232)


def test_gate_and_closability():
    print("\nthe gate — surfaced loud, INDETERMINATE, but never lifts a case")
    acq = AcquisitionInputs(agreed_price=100000, lien_status="verified")
    base = dict(case_number="X", owed=0.0, total_due_filing=11329.20, property_type="real",
                market_value=200000, living_area_sqft=1200)
    r_no = A.analyze(CaseInput(**base), acq, None)
    r_yes = A.analyze(CaseInput(**base, tax_breakdown=GARLAND_TB), acq, None)
    check("gate fires when a named collector is unreachable",
          any(g["gate"] == "collector_balance_unavailable" for g in r_yes["gates"]))
    g = [x for x in r_yes["gates"] if x["gate"] == "collector_balance_unavailable"][0]
    check("severity is GENERIC — it must not lift out of HOLD", g["severity"] == "generic", g["severity"])
    check("the verdict is UNCHANGED by the discovery",
          r_yes["decision"] == r_no["decision"], f'{r_no["decision"]} → {r_yes["decision"]}')
    check("closability was confident BEFORE", r_no["seller_net_sheet"]["closable"] is not None)
    check("closability is INDETERMINATE AFTER", r_yes["seller_net_sheet"]["closable"] is None)
    check("…and says why", r_yes["seller_net_sheet"]["gate"] == "indeterminate_payoff_incomplete",
          r_yes["seller_net_sheet"]["gate"])
    # A Dallas parcel (everything inside ACT) must be untouched — no false alarm.
    r_dal = A.analyze(CaseInput(**{**base, "owed": 19366.44}, tax_breakdown=DALLAS_TB), acq, None)
    check("an all-ACT parcel raises NO collector gate",
          not any(g["gate"] == "collector_balance_unavailable" for g in r_dal["gates"]))
    check("…and keeps a confident closability", r_dal["seller_net_sheet"]["closable"] is not None)
    # Absence of a breakdown must not manufacture a finding.
    check("no petition breakdown → no gate (absence is not evidence)",
          not any(g["gate"] == "collector_balance_unavailable" for g in r_no["gates"]))


def run():
    print("=" * 78)
    print("§23 — PER-COLLECTOR PAYOFF SCHEMA + PETITION MEMBERSHIP")
    print("=" * 78)
    test_canonical()
    test_membership_from_petition()
    test_resolution_and_roster()
    test_lines_and_completeness()
    test_3909_cambridge_reconstruction()
    test_gate_and_closability()
    print("-" * 78)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
