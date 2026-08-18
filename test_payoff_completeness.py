"""§33 — COMPLETENESS INVARIANT. The fifth guard family.

THE DEFECT THIS PINS (seventh instance of the §19 "is this a set?" class):
`petition_collectors` reads the petition's Exhibit-A breakdown, which names PLAINTIFFS. A district
that levies on the parcel and did not join the suit is invisible to it. So petition membership is a
LOWER BOUND on who is owed, and "we retrieved every collector we know about" is NOT "we know about
every collector". Fetching a lower bound to completion proves nothing about the set.

The engine already computed the honest tri-state — and every production caller discarded it, reading
`unavailable_collectors` (petition-derived) instead. On the real book that read empty on 221 of 329
cases, each one asserting `verified` — a word that means *correct* but is heard as *complete*.

THE INVARIANT: no surface may CLAIM completeness unless it is affirmatively established.
  · `complete is True`  ⇒ retrieval complete AND membership verified
  · `complete is None`  ⇒ NOT complete (the state `is not False` used to swallow)
  · `complete is False` ⇒ a NAMED collector was not retrieved

DELIBERATE SCOPE (asserted here so it cannot be "fixed" by accident): the MONEY gate in
`seller_net_sheet` reads RETRIEVAL, not membership. Membership is unverified fleet-wide, so gating
closability on it would force every priced deal to INDETERMINATE and retire the seller-net gate
entirely — and a cold sweep would show ZERO flips, because no case carries an agreed_price until a
rep enters one. The damage would be invisible in the sweep and live at the moment money is decided.

Checks marked [teeth] re-run the assertion against the DEFECT's own logic and require that it gives
the wrong answer. A guard that would pass with the bug reinstated is not a guard.
"""
import sys
sys.path.insert(0, ".")
import jurisdictions as J
import acquisition as A
from acquisition import CaseInput

PASS = FAIL = 0
def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label} {extra}")

def teeth(label, defect_answer, correct_answer):
    """Pin that the DEFECT and the FIX disagree. If they agree, the guard proves nothing."""
    global PASS, FAIL
    if defect_answer != correct_answer:
        PASS += 1; print(f"  ok   [teeth] {label} — defect gives {defect_answer!r}, fix gives {correct_answer!r}")
    else:
        FAIL += 1; print(f"  FAIL [teeth] {label} — defect and fix AGREE ({correct_answer!r}); guard is inert")

GARLAND_TB = [{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 12108.43},
              {"entity": "CITY OF GARLAND", "total": 7666.63}]
DALLAS_TB = [{"entity": "DALLAS COUNTY", "total": 5000.0},
             {"entity": "CITY OF DALLAS", "total": 4000.0}]
ACT_UNITS = ["DALLAS COUNTY", "CITY OF DALLAS", "DALLAS COLLEGE",
             "PARKLAND HOSPITAL", "DALLAS ISD", "SCHOOL EQUALIZATION"]


def test_tristate_is_exact():
    print("\nthe tri-state is exact — None is NOT True")
    c_missing = J.payoff_completeness(J.collector_lines(GARLAND_TB, act_balance=0.0))
    check("a NAMED-but-unretrieved collector ⇒ complete is False", c_missing["complete"] is False)

    # Retrieval complete, membership unknown — THE case the old `is not False` swallowed.
    L_unknown = J.collector_lines(GARLAND_TB, act_balance=0.0,
                                  fetched={"GARLAND ISD": 0.0, "CITY OF GARLAND": 0.0})
    c_unknown = J.payoff_completeness(L_unknown)
    check("every named collector read, but no ACT unit report ⇒ complete is None",
          c_unknown["complete"] is None)
    check("…retrieval really is complete (the None is about the SET, not the fetch)",
          not c_unknown["unavailable_collectors"])
    check("…membership_verified reports False in its own right",
          c_unknown["membership_verified"] is False)
    teeth("`is not False` vs the correct reading on an UNKNOWN verdict",
          c_unknown["complete"] is not False,          # what the old assertion computed
          J.payoff_is_complete(c_unknown))             # what completeness actually is

    L_true = J.collector_lines(DALLAS_TB, act_balance=19366.44, act_units=ACT_UNITS)
    c_true = J.payoff_completeness(L_true)
    check("retrieval complete AND ACT unit coverage known ⇒ complete is True",
          c_true["complete"] is True)
    check("…and membership_verified is True", c_true["membership_verified"] is True)


def test_payoff_is_complete_is_the_only_reading():
    print("\npayoff_is_complete() — one reading of the tri-state, no caller invents its own")
    check("True  ⇒ complete", J.payoff_is_complete({"complete": True}) is True)
    check("None  ⇒ NOT complete", J.payoff_is_complete({"complete": None}) is False)
    check("False ⇒ NOT complete", J.payoff_is_complete({"complete": False}) is False)
    check("a missing key is NOT complete (absence is never a yes)",
          J.payoff_is_complete({}) is False)
    # NOTE: no [teeth] check for truthiness here — `bool(None)` and the correct reading AGREE on
    # every state, so truthiness was never the defect and a teeth check on it would be theatre.
    # The defect was specifically `is not False`, pinned in test_tristate_is_exact.


def test_label_never_claims_unverified_completeness():
    print("\nthe `verified` label requires MEMBERSHIP, not just retrieval")
    # Petition membership NONEMPTY but INCOMPLETE — the synthetic case §33 was asked to pin.
    c = CaseInput("TX-SYNTH-LOWERBOUND", owed=100.0, property_type="real",
                  tax_breakdown=GARLAND_TB,
                  collector_balances={"GARLAND ISD": 12108.43, "CITY OF GARLAND": 7666.63})
    payoff = A.tax_payoff(c)
    comp = A.tax_payoff_lines(c)["completeness"]
    check("retrieval is complete — nothing named is missing", not comp["unavailable_collectors"])
    check("…yet the payoff is NOT labelled verified", payoff["label"] != A.VERIFIED)
    check("…it is labelled estimated", payoff["label"] == A.ESTIMATED)
    check("…and the note names the SET as unverified, not a missing collector",
          "UNVERIFIED" in payoff["note"], payoff["note"])
    check("…the note does NOT claim a collector went unretrieved",
          "unretrieved collector(s)" not in payoff["note"])
    # The defect's own rule, recomputed here verbatim: retrieval-only.
    old_label = A.ESTIMATED if (bool(comp["unavailable_collectors"]) or
                                not isinstance(c.owed, (int, float))) else A.VERIFIED
    teeth("retrieval-only labelling vs membership-aware labelling", old_label, payoff["label"])
    check("the payoff AMOUNT is unchanged by the label rule — this is a LABEL fix, not arithmetic",
          payoff["amount"] == round(100.0 + 12108.43 + 7666.63), payoff["amount"])


def test_unverified_set_is_disclosed_but_is_not_a_gate():
    print("\nthe unverified set is DISCLOSED on the payoff — and is deliberately NOT a gate")
    acq = A.AcquisitionInputs()
    c = CaseInput("TX-SYNTH-DISCLOSE", owed=100.0, property_type="real",
                  tax_breakdown=GARLAND_TB,
                  collector_balances={"GARLAND ISD": 1.0, "CITY OF GARLAND": 1.0})
    gates = A.deal_gates(c, acq, A.seller_net_sheet(c, acq, A.tax_payoff(c)))
    # An unverified set is the state of EVERY case today. `deal_gates` feeds a table where any
    # `generic` gate demotes GO → GO-WITH-CONDITIONS, so a gate firing at 100% would retire the GO
    # verdict entirely — the identical over-fire §23 measured at 95/334 for this same finding.
    check("NO collector_set_unverified gate is emitted",
          not [x for x in gates if x["gate"] == "collector_set_unverified"])
    check("…so a universal condition cannot silently retire the GO verdict",
          not any(x["severity"] == "generic" and "collector set" in x.get("detail", "").lower()
                  for x in gates))
    # …but the finding is NOT lost: it is stated on the payoff and in the completeness verdict.
    payoff = A.tax_payoff(c)
    comp = A.tax_payoff_lines(c)["completeness"]
    check("the payoff note discloses the unverified set", "UNVERIFIED" in payoff["note"])
    check("…the label refuses to say verified", payoff["label"] == A.ESTIMATED)
    check("…and membership_verified states it as a fact for any consumer",
          comp["membership_verified"] is False)

    # The RETRIEVAL gate is unaffected — it discriminates, so it stays a gate.
    c2 = CaseInput("TX-SYNTH-BOTH", owed=100.0, property_type="real", tax_breakdown=GARLAND_TB)
    names2 = {x["gate"] for x in A.deal_gates(c2, acq, A.seller_net_sheet(c2, acq, A.tax_payoff(c2)))}
    check("an UNRETRIEVED named collector still raises the retrieval gate",
          "collector_balance_unavailable" in names2)


def test_money_gate_scope_is_retrieval_deliberately():
    print("\nSCOPE PIN — the money gate reads RETRIEVAL, so it can still fire")
    # lien_status must be known, or closability is INDETERMINATE for an unrelated reason (§5.3) and
    # the pin would prove nothing about the collector question.
    acq = A.AcquisitionInputs(agreed_price=500000.0, lien_status="verified", lien_stack=[])
    c = CaseInput("TX-SYNTH-MONEY", owed=1000.0, property_type="real", tax_breakdown=DALLAS_TB)
    sheet = A.seller_net_sheet(c, acq, A.tax_payoff(c))
    comp = A.tax_payoff_lines(c)["completeness"]
    check("membership is unverified on this case", not J.payoff_is_complete(comp))
    check("…yet closability is still DECIDED, not forced INDETERMINATE",
          sheet["closable"] is not None, sheet.get("gate"))
    teeth("membership-gated closability vs retrieval-gated closability",
          None,                       # what gating on membership would yield, today, for every case
          sheet["closable"])

    # …and it still forces INDETERMINATE for the question it DOES govern.
    c2 = CaseInput("TX-SYNTH-MONEY2", owed=1000.0, property_type="real", tax_breakdown=GARLAND_TB)
    sheet2 = A.seller_net_sheet(c2, acq, A.tax_payoff(c2))
    check("an UNRETRIEVED named collector still forces INDETERMINATE",
          sheet2["closable"] is None and sheet2["gate"] == "indeterminate_payoff_incomplete",
          sheet2.get("gate"))


def test_verified_zero_still_reads_zero():
    print("\n§29 interop — a fetched $0 is still a verified fact, not an absence")
    c = CaseInput("TX-SYNTH-ZERO", owed=0.0, property_type="real",
                  tax_breakdown=GARLAND_TB,
                  collector_balances={"GARLAND ISD": 0.0, "CITY OF GARLAND": 0.0})
    payoff = A.tax_payoff(c)
    check("a fetched zero yields an amount of 0, never null", payoff["amount"] == 0, payoff)
    check("…but the SET being unverified still downgrades the label",
          payoff["label"] == A.ESTIMATED)


if __name__ == "__main__":
    test_tristate_is_exact()
    test_payoff_is_complete_is_the_only_reading()
    test_label_never_claims_unverified_completeness()
    test_unverified_set_is_disclosed_but_is_not_a_gate()
    test_money_gate_scope_is_retrieval_deliberately()
    test_verified_zero_still_reads_zero()
    print(f"\n{PASS}/{PASS+FAIL} checks passed")
    sys.exit(1 if FAIL else 0)
