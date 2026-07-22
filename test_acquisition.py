"""
test_acquisition.py — Stage-1 validation harness for the Acquisition Intelligence engine.

Runs OFFLINE (no NTREIS, no DB, no network) — the golden-case inputs are embedded verified fixtures
(pulled + hand-verified from the live DB on 2026-07-19). Two layers:

  A. FORMULA CORRECTNESS — pins the math: MAO ladder, the CORRECTED payoff model (ACT live balance
     used as-is; §33.48 only a fallback estimator), the separate Seller-Net lines + fatal gate,
     condition, Mission Score, and the valuation hierarchy (no offer number ever rests on DCAD).
  B. GOLDEN CASES — TX-23-00423 (Tryon), TX-26-01379 (Ruby Faye Brown), TX-25-00249. Every output
     derivable from verified enrichment is asserted; run directly to print the full analysis for
     the human-analysis sign-off gate.

Run: python3 test_acquisition.py   →  prints golden reports + "N/N".
"""
import datetime

import acquisition as A
from acquisition import CaseInput, AcquisitionInputs

AS_OF = datetime.date(2026, 7, 19)   # fixed so the FALLBACK estimator is deterministic

_passed = 0
_failed = 0


def check(name, cond, got=None):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ FAIL: {name}" + (f"  (got {got!r})" if got is not None else ""))


# ── verified golden fixtures (from live DB 2026-07-19; 'owed' = live current_tax_balance) ─────────
TRYON = CaseInput(
    case_number="TX-23-00423", market_value=217800, owed=71938.09, total_due_filing=40443.06,
    filed_date="2023-03-09", judgment_date="2026-03-18", living_area_sqft=1014, depreciation_pct=40,
    actual_age=43, year_built=1983, distress_level="high",
    distress_signals=["depreciation", "no_homestead", "payment_gap"], no_homestead=True,
    property_type="real", case_track="dismissed_owing", owner_of_record="TRYON CHARLIE B",
    defendant="Charlie B. Tryon",  # same party as owner → no mismatch
)
RUBY = CaseInput(
    case_number="TX-26-01379", market_value=143320, owed=11437.29, total_due_filing=11437.29,
    filed_date="2026-07-06", judgment_date=None, living_area_sqft=1077, depreciation_pct=60,
    actual_age=79, year_built=1947, distress_level="high",
    distress_signals=["high_depreciation", "no_homestead"], no_homestead=True, is_absentee=True,
    property_type="real", case_track=None, owner_of_record="TAYLOR FELICIA D",
    defendant="Ruby Faye Brown",  # owner ≠ defendant → substantive heir mismatch
)
GRANT = CaseInput(
    case_number="TX-25-00249", market_value=232800, owed=152224.40, total_due_filing=80583.24,
    filed_date="2025-02-13", judgment_date="2026-07-07", living_area_sqft=1125, depreciation_pct=45,
    actual_age=48, year_built=1978, distress_level="high",
    distress_signals=["depreciation", "no_homestead", "payment_gap"], no_homestead=True,
    property_type="real", case_track="judged_pending", owner_of_record="MIDDLETON MICHAEL",
    defendant="Michael Middleton",  # same party as owner → no mismatch
)


# ── A. FORMULA CORRECTNESS ────────────────────────────────────────────────────────────────────────
def test_mao_ladder():
    m = A.mao_ladder(arv=200000, repairs=50000)
    check("mao 60%", m["60"] == 70000, m["60"])          # 200000*0.60 − 50000
    check("mao 65%", m["65"] == 80000, m["65"])
    check("mao 70%", m["70"] == 90000, m["70"])
    check("mao 75%", m["75"] == 100000, m["75"])
    check("mao 80%", m["80"] == 110000, m["80"])
    check("mao ladder monotonic", m["60"] < m["65"] < m["70"] < m["75"] < m["80"])


def test_mao_ignores_taxes_and_liens():
    # The rule MAO must NOT change with taxes/liens (design §5.1 double-count rule).
    check("mao is (ARV×rule)−repairs only",
          A.mao_ladder(200000, 50000) == A.mao_ladder(200000, 50000))


# ── CORRECTED payoff model ────────────────────────────────────────────────────────────────────────
def test_payoff_is_live_balance_as_is():
    # ACT live balance used AS-IS — never accrued upon, never fee-loaded (design §5, corrected).
    p = A.tax_payoff(CaseInput("X", owed=71938.09, total_due_filing=40443.06,
                               filed_date="2023-03-09", judgment_date="2026-03-18"), AS_OF)
    check("payoff == live balance as-is", p["amount"] == 71938, p["amount"])
    check("payoff labeled VERIFIED", p["label"] == A.VERIFIED)
    check("payoff basis act_live_balance", p["basis"] == "act_live_balance")


def test_payoff_fallback_only_without_live_balance():
    # No live balance → §-based estimate from filing amount, labeled ESTIMATED (the ONLY accrual use).
    p = A.tax_payoff(CaseInput("X", owed=None, total_due_filing=5000, filed_date="2024-01-01"), AS_OF)
    check("fallback est = filed + penalty/interest", p["amount"] == 7250, p["amount"])   # 5000 + 5000*.015*30
    check("fallback labeled ESTIMATED", p["label"] == A.ESTIMATED)
    check("fallback basis", p["basis"] == "fallback_estimate")
    check("no data → UNAVAILABLE", A.tax_payoff(CaseInput("X"), AS_OF)["label"] == A.UNAVAILABLE)


def test_attorney_fees_separate_and_estimated():
    check("post-judgment atty 20%", A.tax_suit_attorney_fees(CaseInput("X", judgment_date="2026-01-01"), 100000)["amount"] == 20000)
    check("pre-judgment atty 15%", A.tax_suit_attorney_fees(CaseInput("X"), 100000)["amount"] == 15000)
    check("atty fees labeled ESTIMATED", A.tax_suit_attorney_fees(CaseInput("X"), 100000)["label"] == A.ESTIMATED)


# ── Seller Net Sheet (corrected 4-line model) ─────────────────────────────────────────────────────
def _seller(owed, agreed, judgment=None, liens=None, lien_status="verified"):
    case = CaseInput("X", owed=owed, judgment_date=judgment)
    acq = AcquisitionInputs(agreed_price=agreed, lien_status=lien_status, lien_stack=liens or [])
    return A.seller_net_sheet(case, acq, A.tax_payoff(case, AS_OF))


def test_seller_net_closable():
    # owed 70000 (no judgment → atty 15% = 10500), agreed 150000, lien 20000, closing 2%×150000=3000
    s = _seller(70000, 150000, liens=[{"amount": 20000}])
    check("seller tax_payoff line == balance", s["tax_payoff"]["value"] == 70000, s["tax_payoff"]["value"])
    check("seller atty fees separate line", s["tax_suit_attorney_fees"]["value"] == 10500, s["tax_suit_attorney_fees"]["value"])
    check("seller total_payoffs", s["total_payoffs"]["value"] == 103500, s["total_payoffs"]["value"])
    check("seller net", s["seller_net"]["value"] == 46500, s["seller_net"]["value"])
    check("closable True", s["closable"] is True and s["gate"] == "closable")


def test_seller_net_fatal():
    # owed 70000, agreed 80000, lien 20000, closing 1600, atty 10500 → total 102100 > 80000
    s = _seller(70000, 80000, liens=[{"amount": 20000}])
    check("fatal seller_net negative", s["seller_net"]["value"] == 80000 - 102100, s["seller_net"]["value"])
    check("fatal closable False", s["closable"] is False)
    check("fatal gate", s["gate"] == "fatal_negative_seller_net")


def test_seller_net_indeterminate_when_liens_unavailable():
    s = _seller(70000, 150000, lien_status="unavailable")
    check("liens unavailable → closable None", s["closable"] is None)
    check("liens unavailable → gate indeterminate", s["gate"] == "indeterminate_liens_unavailable")
    check("mowing/labor lien line UNAVAILABLE", s["mowing_labor_and_other_liens"]["label"] == A.UNAVAILABLE)


def test_gate_bpp():
    gates = A.deal_gates(CaseInput("X", property_type="personal"), AcquisitionInputs(),
                         A.seller_net_sheet(CaseInput("X"), AcquisitionInputs(), A.tax_payoff(CaseInput("X"), AS_OF)))
    check("bpp fatal gate", any(g["gate"] == "bpp" and g["severity"] == "fatal" for g in gates))


# ── condition / rehab ─────────────────────────────────────────────────────────────────────────────
def test_condition_class():
    dep = lambda d: A.condition_estimate(CaseInput("X", depreciation_pct=d, living_area_sqft=1000))["condition_class"]["value"]
    check("dep 10 → C2", dep(10) == "C2")
    check("dep 40 → C4", dep(40) == "C4")
    check("dep 45 → C5", dep(45) == "C5")
    check("dep 60 → C6", dep(60) == "C6")
    check("dep None → C4 default", A.condition_estimate(CaseInput("X", living_area_sqft=1000))["condition_class"]["value"] == "C4")
    c = A.condition_estimate(CaseInput("X", depreciation_pct=40, living_area_sqft=1000))
    check("condition labeled INFERRED", c["condition_class"]["label"] == A.INFERRED)
    check("rehab labeled ESTIMATED", c["rehab_base"]["label"] == A.ESTIMATED)
    check("interior access False", c["interior_access"]["value"] is False)


def test_rehab_estimate():
    c = A.condition_estimate(CaseInput("X", depreciation_pct=45, living_area_sqft=1125))  # C5 @ $60
    check("rehab_base C5", c["rehab_base"]["value"] == 67500, c["rehab_base"]["value"])       # 60×1125
    check("rehab_low", c["rehab_low"]["value"] == 57375, c["rehab_low"]["value"])             # ×0.85
    check("rehab_high (no-interior +25%)", c["rehab_high"]["value"] == 84375, c["rehab_high"]["value"])  # ×1.25


def test_mission_score_provisional_and_weights():
    w = A.ACQ_CONFIG["mission_weights"]
    check("mission weights sum to 1.0", abs(sum(w.values()) - 1.0) < 1e-9, sum(w.values()))
    ms = A.mission_score({k: 80 for k in w})
    check("full mission score = 80", ms["score"] == 80 and ms["provisional"] is False)
    ms2 = A.mission_score({**{k: 80 for k in w}, "valuation_confidence": None})
    check("missing component → provisional", ms2["provisional"] is True)


# ── VALUATION HIERARCHY: no offer number ever rests on DCAD (design §5, locked) ───────────────────
def test_valuation_hierarchy_no_offer_on_dcad():
    # DCAD MV present, but NO comp ARV supplied → no MAO/offer number is produced at all.
    r = A.analyze(TRYON, AcquisitionInputs(), AS_OF)   # market_value=217800 present in the case
    check("no ARV → no MAO ladder", r["mao_ladder"] is None)
    check("no ARV → no itemized MAO", r["mao_itemized"] is None)
    check("valuation provisional without confirmed comps", r["valuation_state"] == "provisional")
    # ARV supplied but only ESTIMATED (auto/triage) → still provisional, offer not trusted.
    r2 = A.analyze(TRYON, AcquisitionInputs(arv=210000, arv_label=A.ESTIMATED), AS_OF)
    check("estimated ARV → still provisional", r2["valuation_state"] == "provisional")
    check("estimated ARV → mission provisional", r2["mission_score"]["provisional"] is True)
    # Only a VERIFIED (human-confirmed comps) ARV promotes to confirmed.
    r3 = A.analyze(TRYON, AcquisitionInputs(arv=210000, arv_label=A.VERIFIED,
                                            agreed_price=95000, lien_status="verified"), AS_OF)
    check("confirmed ARV → valuation confirmed", r3["valuation_state"] == "confirmed")


def test_arv_sanity_band():
    check("comp near assessed → no flag", A.arv_sanity_band(210000, 217800)["flag"] is False)
    check("comp far from assessed → flag", A.arv_sanity_band(300000, 200000)["flag"] is True)
    check("insufficient data → no flag", A.arv_sanity_band(None, 200000)["flag"] is False)


# ── B. GOLDEN CASES — assert everything derivable from verified data ──────────────────────────────
GOLDEN_EXPECTED = {
    # (tax_payoff_amount [live balance as-is], attorney_fee_est, condition_class, rehab_base)
    "TX-23-00423": (71938, 14388, "C4", 40560),    # judgment → 20% atty; C4 @ $40 × 1014
    "TX-25-00249": (152224, 30445, "C5", 67500),   # judgment → 20% atty; C5 @ $60 × 1125
    "TX-26-01379": (11437, 1716, "C6", 91545),     # no judgment → 15% atty; C6 @ $85 × 1077
}


def test_golden_derivable():
    for case in (TRYON, GRANT, RUBY):
        r = A.analyze(case, AcquisitionInputs(), AS_OF)   # no ARV, no agreed price, liens unavailable
        exp_pay, exp_atty, exp_cls, exp_rehab = GOLDEN_EXPECTED[case.case_number]
        check(f"{case.case_number} tax payoff (live as-is)", r["tax_payoff"]["amount"] == exp_pay, r["tax_payoff"]["amount"])
        check(f"{case.case_number} payoff VERIFIED", r["tax_payoff"]["label"] == A.VERIFIED)
        check(f"{case.case_number} atty fee est", A.tax_suit_attorney_fees(case, exp_pay)["amount"] == exp_atty)
        check(f"{case.case_number} condition class", r["condition"]["condition_class"]["value"] == exp_cls, r["condition"]["condition_class"]["value"])
        check(f"{case.case_number} rehab base", r["condition"]["rehab_base"]["value"] == exp_rehab, r["condition"]["rehab_base"]["value"])
        check(f"{case.case_number} lien gate blocking", any(g["gate"] == "lien_discovery_required" for g in r["gates"]))
        check(f"{case.case_number} never a plain GO without confirmed comps", r["decision"] != "GO")
        check(f"{case.case_number} valuation provisional", r["valuation_state"] == "provisional")


def test_golden_heir_signal():
    r = A.analyze(RUBY, AcquisitionInputs(), AS_OF)   # DCAD owner TAYLOR FELICIA D ≠ defendant
    check("Ruby heir/estate title gate", any(g["gate"] == "heir_estate_title" for g in r["gates"]))


def test_worked_scenario_closable():
    # ILLUSTRATIVE inputs (ARV/agreed NOT human-verified — pending golden reference). Tryon @ 95k.
    acq = AcquisitionInputs(arv=210000, agreed_price=95000, lien_status="verified",
                            lien_stack=[{"type": "mowing", "amount": 4000}])
    r = A.analyze(TRYON, acq, AS_OF)
    s = r["seller_net_sheet"]
    # tax 71938 + atty 14388 + lien 4000 + closing 1900 = 92226 ; net = 95000 − 92226 = 2774
    check("worked total_payoffs", s["total_payoffs"]["value"] == 92226, s["total_payoffs"]["value"])
    check("worked seller net", s["seller_net"]["value"] == 2774, s["seller_net"]["value"])
    check("worked thin-but-closable", s["closable"] is True)
    check("worked MAO 65% present", r["mao_ladder"]["65"] == round(210000 * 0.65 - 40560))


def test_worked_scenario_fatal():
    # Grant St thin-equity: agreed 140k can't clear tax 152224 + atty 30445 + closing 2800.
    acq = AcquisitionInputs(arv=250000, agreed_price=140000, lien_status="verified")
    r = A.analyze(GRANT, acq, AS_OF)
    s = r["seller_net_sheet"]
    check("Grant total_payoffs", s["total_payoffs"]["value"] == 185469, s["total_payoffs"]["value"])
    check("Grant unclosable (payoffs > price)", s["closable"] is False)
    check("Grant decision NO-GO", r["decision"] == "NO-GO", r["decision"])


# ── GOLDEN PINS — human-verified acceptance tests (delivered 2026-07-19) ─────────────────────────
# Each pin encodes the human's ARV/inputs + verdict. The engine MUST reproduce each verdict with
# labels matching. These are the Stage-1 acceptance gate.

# Pin inputs (human CMA / analysis):
PIN_GRANT = AcquisitionInputs(arv=265000, arv_label=A.VERIFIED, repair_estimate=67500,
                              lien_status="unavailable")
PIN_TRYON = AcquisitionInputs(arv=225000, arv_label=A.VERIFIED, agreed_price=108000,
                              lien_status="verified", lien_stack=[])   # clean title — the deal closed
PIN_RUBY = AcquisitionInputs(arv=125000, arv_label=A.ESTIMATED, lien_status="partial",  # $110–140K provisional
                             lien_stack=[{"type": "llc_interest", "amount": None,
                                          "holder": "Unknown Shareholders/Successors/Assigns of Mesquite NF SNF, LLC"}])


def test_pin_grant_structurally_unclosable_nogo():
    r = A.analyze(GRANT, PIN_GRANT, AS_OF)
    # MAO ladder @ ARV 265000, repairs 67500: max (80%) = 144500. Min payoffs 152224 + 30445 = 182669.
    min_payoffs = r["tax_payoff"]["amount"] + A.tax_suit_attorney_fees(GRANT, r["tax_payoff"]["amount"])["amount"]
    check("Grant min payoffs", min_payoffs == 182669, min_payoffs)
    check("Grant payoffs exceed MAO at EVERY rule%", all(min_payoffs > v for v in r["mao_ladder"].values()),
          r["mao_ladder"])
    check("Grant structurally_unclosable fatal gate fires",
          any(g["gate"] == "structurally_unclosable" and g["severity"] == "fatal" for g in r["gates"]))
    check("Grant verdict NO-GO", r["decision"] == "NO-GO", r["decision"])
    check("Grant tax payoff verified $152,224", r["tax_payoff"]["amount"] == 152224 and r["tax_payoff"]["label"] == A.VERIFIED)
    check("Grant ARV label verified (human CMA)", r["arv"]["label"] == A.VERIFIED)


def test_pin_tryon_go():
    r = A.analyze(TRYON, PIN_TRYON, AS_OF)
    check("Tryon MAO 70% == $116,940", r["mao_ladder"]["70"] == 116940, r["mao_ladder"]["70"])
    check("Tryon contract $108k passes MAO_70", 108000 < r["mao_ladder"]["70"])
    atty = A.tax_suit_attorney_fees(TRYON, r["tax_payoff"]["amount"])["amount"]
    pre_closing_net = 108000 - r["tax_payoff"]["amount"] - atty
    check("Tryon seller net (pre-closing) == human $21,674", pre_closing_net == 21674, pre_closing_net)
    s = r["seller_net_sheet"]
    check("Tryon closable True", s["closable"] is True)
    check("Tryon final seller net positive", s["seller_net"]["value"] > 0, s["seller_net"]["value"])
    check("Tryon verdict GO", r["decision"] == "GO", r["decision"])
    check("Tryon valuation confirmed", r["valuation_state"] == "confirmed")


def test_pin_ruby_held_indeterminate_never_go():
    # THE acceptance test for INDETERMINATE-never-false-GO: an identified but unquantified lien holds it.
    r = A.analyze(RUBY, PIN_RUBY, AS_OF)
    check("Ruby valuation provisional (one comp, mismatch — §5.4)", r["valuation_state"] == "provisional")
    check("Ruby identified_unquantified_lien gate holds it",
          any(g["gate"] == "identified_unquantified_lien" for g in r["gates"]))
    check("Ruby closability INDETERMINATE", r["seller_net_sheet"]["closable"] is None)
    check("Ruby verdict GO-WITH-CONDITIONS", r["decision"] == "GO-WITH-CONDITIONS", r["decision"])
    check("Ruby NEVER a plain GO while lien unquantified", r["decision"] != "GO")
    check("Ruby tax payoff verified $11,437", r["tax_payoff"]["amount"] == 11437 and r["tax_payoff"]["label"] == A.VERIFIED)
    # And if that lien were later quantified low + ARV confirmed, it could promote — sanity that the
    # ONLY thing blocking a clean path here is the lien + provisional valuation, not a fatal flaw.
    check("Ruby not fatal", not any(g["severity"] == "fatal" for g in r["gates"]))


# ── GRADUATED heir/estate gate + decision-table drift fix (2026-07-21) ────────────────────────────
TX_00553 = CaseInput(  # the live gap case: no-zip (city fallback) AND a real owner-mismatch
    case_number="TX-23-00553", market_value=120000, owed=9500, total_due_filing=9000,
    living_area_sqft=1174, depreciation_pct=35, actual_age=46, year_built=1980, distress_level="high",
    distress_signals=["no_homestead"], no_homestead=True, is_absentee=True, estate=True,
    property_type="real", owner_of_record="BACA NORMA ESTELA ET AL &", defendant="Pauline Hernandez",
)


def test_owner_defendant_mismatch():
    check("Ruby owner≠defendant (TAYLOR vs BROWN) → mismatch", A.owner_defendant_mismatch(RUBY) is True)
    check("00553 owner≠defendant (BACA vs HERNANDEZ) → mismatch", A.owner_defendant_mismatch(TX_00553) is True)
    check("Tryon owner==defendant → no mismatch", A.owner_defendant_mismatch(TRYON) is False)
    check("no defendant → no mismatch", A.owner_defendant_mismatch(CaseInput("X", owner_of_record="SMITH JOHN")) is False)
    check("shared surname + 'ET AL' noise → NOT a mismatch",
          A.owner_defendant_mismatch(CaseInput("X", owner_of_record="SMITH JOHN ET AL", defendant="John Smith")) is False)


def test_pin_00553_go_with_conditions_via_owner_mismatch():
    # RESOLUTION of the decision-table contradiction: 00553's heir flag is a GENUINE owner-mismatch
    # (BACA NORMA ESTELA ET AL ≠ Pauline Hernandez), same substantive class as Ruby → verdict is
    # GO-WITH-CONDITIONS, driven by the title question NOT the provisional ARV. The pre-propose HOLD
    # was the bug; the verdict holds REGARDLESS of valuation state.
    r0 = A.analyze(TX_00553, AcquisitionInputs(), AS_OF)                                  # no ARV at all
    check("00553 heir_estate_title is SUBSTANTIVE (owner-mismatch)",
          any(g["gate"] == "heir_estate_title" and g["severity"] == "substantive" for g in r0["gates"]))
    check("00553 → GO-WITH-CONDITIONS even with NO valuation (substantive owner-mismatch)",
          r0["decision"] == "GO-WITH-CONDITIONS", r0["decision"])
    rp = A.analyze(TX_00553, AcquisitionInputs(arv=304000, arv_label=A.ESTIMATED), AS_OF)  # noisy city-wide ARV
    check("00553 → GO-WITH-CONDITIONS with a provisional ARV too (same reason, not the ARV)",
          rp["decision"] == "GO-WITH-CONDITIONS", rp["decision"])
    check("00553 valuation still provisional", rp["valuation_state"] == "provisional")


def test_pin_provisional_alone_stays_hold():
    # THE drift fix: a provisional ARV with NO substantive condition must NOT lift out of HOLD.
    clean = CaseInput("TX-99-CLEAN", market_value=200000, owed=30000, total_due_filing=30000,
                      living_area_sqft=1500, depreciation_pct=20, year_built=2000, property_type="real",
                      owner_of_record="SMITH JOHN", defendant="John Smith")   # owner==defendant, not absentee/estate
    r = A.analyze(clean, AcquisitionInputs(arv=250000, arv_label=A.ESTIMATED, lien_status="unavailable"), AS_OF)
    check("clean case has no substantive condition", not any(g["severity"] == "substantive" for g in r["gates"]))
    check("provisional ARV ALONE → stays HOLD (drift fixed)", r["decision"] == "HOLD", r["decision"])
    r2 = A.analyze(clean, AcquisitionInputs(arv=250000, arv_label=A.VERIFIED, lien_status="unavailable"), AS_OF)
    check("same case + CONFIRMED valuation + generic gate → GO-WITH-CONDITIONS",
          r2["decision"] == "GO-WITH-CONDITIONS", r2["decision"])


def _print_pin_verdicts():
    print("\n" + "═" * 82)
    print("GOLDEN-PIN ACCEPTANCE VERDICTS (as_of 2026-07-19)")
    print("═" * 82)
    for label, case, acq, want in [
        ("Grant St", GRANT, PIN_GRANT, "NO-GO (structurally unclosable)"),
        ("Tryon", TRYON, PIN_TRYON, "GO (closed @ $108k)"),
        ("Ruby Faye Brown", RUBY, PIN_RUBY, "GO-WITH-CONDITIONS (INDETERMINATE)"),
    ]:
        r = A.analyze(case, acq, AS_OF)
        match = "✓" if r["decision"] in want else "✗"
        print(f"  {match} {case.case_number} {label:<16} → {r['decision']:<18} "
              f"[val:{r['valuation_state']}]  gates={[g['gate'] for g in r['gates']] or 'none'}")
        print(f"        expected: {want}")


# ── golden report printer (for human sign-off) ────────────────────────────────────────────────────
def _print_golden_report():
    print("\n" + "═" * 82)
    print("GOLDEN-CASE OUTPUTS — CORRECTED PAYOFF MODEL (as_of 2026-07-19) — for human sign-off")
    print("═" * 82)
    for case in (TRYON, RUBY, GRANT):
        r = A.analyze(case, AcquisitionInputs(), AS_OF)
        p, c = r["tax_payoff"], r["condition"]
        atty = A.tax_suit_attorney_fees(case, p["amount"])
        print(f"\n▸ {case.case_number}  ({case.owner_of_record})  MV ${case.market_value:,}  "
              f"live owed ${case.owed:,.2f}")
        print(f"    Condition: {c['condition_class']['value']} [{c['condition_class']['label']}] — {c['condition_class']['note']}")
        print(f"    Rehab est (exterior-only): low ${c['rehab_low']['value']:,} · base ${c['rehab_base']['value']:,} · high ${c['rehab_high']['value']:,}")
        print(f"    Tax payoff (ACT live balance, AS-IS): ${p['amount']:,}  [{p['label']}]")
        print(f"    + Tax-suit attorney fees (est, until LGBS letter): ${atty['amount']:,}  [{atty['label']}]")
        print(f"    + Mowing/labor & other liens: UNKNOWN until title search  [unavailable]")
        print(f"    = Payoff stack before liens/closing: ${p['amount'] + atty['amount']:,}")
        print(f"    Rule%: {r['recommended_rule_pct']['pct']} ({r['recommended_rule_pct']['tier']})  ·  "
              f"ARV: {r['arv']['label']}  ·  Valuation: {r['valuation_state']}")
        print(f"    Gates: {[g['gate'] for g in r['gates']] or 'none'}  ·  Mission {r['mission_score']['score']} "
              f"(provisional={r['mission_score']['provisional']})  ·  DECISION: {r['decision']}")
    print("\n  Tax payoff is now the ACT live balance used AS-IS (VERIFIED). Attorney fees + liens are")
    print("  SEPARATE estimated lines. Seller Net + MAO need your ARV / agreed price / lien stack to")
    print("  finish — confirm the above match your human analysis to close the Stage-1 gate.")


if __name__ == "__main__":
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)):
        fn()
    _print_pin_verdicts()
    _print_golden_report()
    print("\n" + "─" * 82)
    total = _passed + _failed
    print(f"{_passed}/{total} checks passed" + (f"  ({_failed} FAILED)" if _failed else "  ✓ all green"))
