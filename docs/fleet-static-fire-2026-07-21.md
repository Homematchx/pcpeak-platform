# Acquisition Engine — Fleet Static-Fire (2026-07-21)

**Purpose.** Run the full acquisition analysis across all live cases and observe the engine's
behavior *at scale* before Stage 3 decides what to build — the "static-fire of the whole fleet."
**This was READ-ONLY: no engine changes, no writes to prod, no confirmations. Nothing was changed.**

**Scope.** 220 live cases (`GET /api/cases`, open read). Analyzed in the **cold** state — the way the
engine sees a case with **zero human input**: no confirmed comps, no entered liens, no agreed price.
This is deliberate: it isolates the engine's *default* behavior. Because a provisional/unconfirmed
valuation no longer lifts a case out of HOLD (the 2026-07-21 decision-table fix, commit `be66c5e`), the
cold verdict is **gate-driven** and does not depend on the (provisional) ARV — so the verdict
distribution needs no comp fetch. The **ARV-mode breakdown** does run the comp engine (one NTREIS
closed-sales query per proposable case).

Engine at time of run: `acquisition.py` `f060600c`, `comps.py` `7e4ca32f`, deployed `be66c5e`.

---

## 1. Verdict distribution (cold)

| Verdict | Count | % |
|---|---:|---:|
| HOLD | 188 | 85% |
| GO-WITH-CONDITIONS | 32 | 15% |
| GO | 0 | 0% |
| NO-GO | 0 | 0% |

- **GO = 0** and **NO-GO = 0** are *correct* cold outcomes, not gaps. GO requires a **confirmed**
  valuation (human-confirmed comps); `structurally_unclosable` (the main cold NO-GO path) also requires
  a confirmed ARV. Neither exists cold, so neither verdict can fire. This is the decision-table fix
  working: **the engine cannot promote a case to GO on cold or provisional data.**
- The 32 GO-WITH-CONDITIONS are exactly the cases with a **substantive** gate cold — here, the
  graduated heir/estate owner-mismatch (no liens are entered cold, so the unquantified-lien gate
  can't fire either).

## 2. Gate frequency

| Gate | Count | % | Class | Note |
|---|---:|---:|---|---|
| `lien_discovery_required` | 220 | 100% | generic (non-lifting) | Every un-worked case has liens undiscovered — expected and benign; does not move a verdict. |
| `estate_absentee_signal` | 51 | 23% | generic (non-lifting) | Absentee/estate language *without* a confirmed owner-mismatch — a soft note. |
| `heir_estate_title` | 32 | 15% | **substantive (lifting)** | The graduated gate: DCAD owner-of-record is a differently-named party than the defendant. |

## 3. Graduated heir gate — calibration check

The heir gate is the **only verdict-lifting gate that can fire cold**, so its rate is the key
calibration signal for the 2026-07-21 graduation.

- **Owner ≠ defendant mismatch: 32 / 220 = 15%** fleet-wide.
- Of the **131** cases that actually have DCAD owner data (60% coverage — see §6), the mismatch rate
  is **32 / 131 = 24%**.
- **Verdict: well-calibrated.** 15–24% is a plausible estate/heir/prior-sale share for a tax-foreclosure
  book — it is *not* a suspicious majority. The graduation (substantive only on a real named-party
  mismatch; generic absentee/estate stays non-lifting) is doing its job of keeping GO-WITH-CONDITIONS
  selective rather than universal.
- Sample mismatch cases: `TX-26-01190, TX-23-00792, TX-23-00789, TX-25-00497, TX-25-00397, TX-26-00023,
  TX-26-01196, TX-26-01374`.

## 4. ARV-mode breakdown (comp engine, closed-only)

One NTREIS `Property` closed-sales query per proposable case (0 errors, ~98s, throttled). Modes are
mutually exclusive, in precedence order.

| ARV mode | Count | % | Meaning |
|---|---:|---:|---|
| same-subdivision | 41 | 19% | zip locality + the subject's own subdivision had ≥1 qualified comp (highest precision) |
| area | 83 | 38% | zip locality + area-mode selection (no same-subdivision comp) |
| city (fallback) | 20 | 9% | no zip in the case address → `City eq …` locality (broad, lowest precision) |
| none | 76 | 35% | no locality/GLA (61) or a locality but 0 qualified comps (15) |

**Locality used:** zip 139 · city-fallback 20 · none (no locality/GLA) 61.

## 5. Anomaly review

Two checks were requested — *any GO with thin support*, and *any gate firing on a suspicious majority*
— plus the engine's own low-confidence tiers.

- **No GO with thin support** — structurally impossible cold (GO = 0; requires confirmed comps). The
  engine can't emit a thin/false GO by construction.
- **No suspicious-majority *lifting* gate** — the 100% `lien_discovery_required` is a **generic,
  non-lifting** gate (present on every un-worked case; it does not affect the verdict). The one
  verdict-lifting gate (`heir_estate_title`) is a selective 15% (§3).
- **Two real low-confidence patterns surfaced — both are already-logged Stage-3 items, now quantified
  at fleet scale.** All ARVs below are **provisional/triage** (0 confirmed comps) and feed **no**
  verdict; they are informational, and this is exactly the material the "confirmed output as an
  appraisal report" Stage-3 deliverable (reconciliation line, bracketing, n= display) is meant to make
  legible.

  **(a) Single-comp SNAP — 17 of 41 same-subdivision ARVs (41%) rest on n = 1 comp.** When exactly one
  same-subdivision comp qualifies, the median-of-one snaps to that single comp's adjusted value. Sample:

  | Case | Provisional ARV | n same-subdiv | GLA |
  |---|---:|---:|---:|
  | TX-26-01092 | $129,030 | 1 | 1060 |
  | TX-26-00031 | $222,740 | 1 | 1098 |
  | TX-23-00749 | $286,400 | 1 | 1944 |
  | TX-23-00770 | $147,960 | 1 | 1204 |
  | TX-25-00581 | $381,580 | 1 | 2038 |
  | TX-25-00497 | $533,000 | 1 | 2852 |
  | TX-25-00249 | $260,500 | 1 | 1125 |
  | TX-26-01388 | $230,000 | 1 | 2004 |

  High-value single-comp ARVs (e.g. $533k on n=1) are the sharpest illustration of the fragility.

  **(b) City-fallback (broad, low-precision) — 20 cases (9%)** draw from a city-wide pool (qualified
  ≈ 97–100) because the case address had no zip. Sample:

  | Case | Provisional ARV | qualified |
  |---|---:|---:|
  | TX-26-01390 | $252,695 | 100 |
  | TX-26-00036 | $444,410 | 100 |
  | TX-25-00478 | $324,150 | 97 |
  | TX-26-00799 | $199,880 | 100 |
  | TX-26-00039 | $319,250 | 100 |
  | TX-23-00553 | $287,430 | 97 |

## 6. Data coverage

- **Defendant** populated: 220 / 220 (100%) — the heir gate always has the defendant to compare.
- **DCAD owner-of-record** populated: 131 / 220 (60%) — the heir gate can only fire on these 131; the
  89 without owner data can't produce an owner-mismatch (they fall to HOLD unless another gate fires).
- **No locality/GLA** (can't propose): 61 cases — 60 lack `living_area_sqft` (the logged 60-no-GLA
  measure-then-decide follow-up) + 1 with no locality at all.

## 7. Takeaways for Stage 3

1. **The decision-table fix behaves at scale.** No false GO is possible cold; verdicts sit at 85% HOLD
   / 15% GO-WITH-CONDITIONS, the latter entirely from the substantive heir gate.
2. **The graduated heir gate is well-calibrated** (15% fleet / 24% of owner-data cases) — it is not
   over-firing.
3. **The single-comp snap is material** — 41% of same-subdivision ARVs — and directly motivates the
   reconciled-set + `n=`/spread reconciliation line in the appraisal-report deliverable.
4. **City-fallback (9%) is the deliberate low-precision tier** — it unblocks no-zip cases as
   provisional/triage; the bracketing + reconciliation display should make its breadth visible.
5. **35% of the fleet can't produce an ARV** at all (mostly the 60 no-GLA cases) — sequenced under the
   separate 60-no-GLA measure-then-decide follow-up (enrichment backfill vs. land/teardown routing).
6. **NO-GO=0 cold is correct but leaves a triage blind spot.** `structurally_unclosable` is
   confirmed-valuation-gated (so it never false-kills a deal on a noisy provisional ARV) — but that means
   cold triage can't surface an *arithmetic-dead* deal (provisional ARV failing every MAO rung).
   Stage-3 candidate: a **"provisionally unclosable" advisory flag** — a triage marker only, never a
   verdict, never NO-GO — so the fleet view can prioritize while the actual NO-GO stays confirmed-gated.
7. **Owner-of-record coverage is a third enrichment gap.** The heir gate only fires where DCAD owner data
   exists — **131/220 (60%)** — so **89/220 (40%) of the fleet is blind to the substantive owner-mismatch
   check** and defaults to non-lifting (HOLD). The three enrichment-gap figures from this run:
   **no-GLA 60 · no-locality 61 · no-DCAD-owner-of-record 89 (40%)**. The first is under the 60-no-GLA
   session; the other two are candidates for the same measure-then-decide backfill.

## Reproduce

Read-only; hits `GET /api/cases` (open) and, for the ARV modes, one NTREIS `Property` closed-sales
query per proposable case (throttled). Build each subject with `comps.subject_from_case`, the CaseInput
with the same field mapping as `backend/main.py:_case_input`, then `acquisition.analyze(case,
AcquisitionInputs())` for the cold verdict and `comps.provisional_arv` for the ARV mode. Provisional
ARVs drift slightly with the recency window (the 1-year `CloseDate` floor moves with the run date), so
individual ARV figures are as-of 2026-07-21; the distribution is stable.
