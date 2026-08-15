# Acquisition Intelligence Layer — Design

**Status:** DESIGN ONLY — not approved, not built. No code until sign-off (same gate as the
prediction ledger and scrape trigger).
**Scope of this doc:** module boundaries and data flow from `property_intel`, the NTREIS comp-engine
approach, the comp storage/confirmation schema, the UI surface (a new case-detail tab), and the
validation plan. Every unresolved choice is flagged inline **[OPEN #n]** and collected in §14.
**What this doc deliberately does NOT do:** change any scraping, touch `discover.py`'s capture logic,
recalibrate `CITY_DATA`, or add a new court-data source. It is a *downstream, read-mostly* analysis
layer over enrichment data that already exists.

---

## 1. Why this exists

The platform today answers *"when will this case reach an Order of Sale, and what is owed?"* It does
not answer the acquisition question: **can we buy this property directly from the owner/heir, clear
the lien stack, and exit at a risk-adjusted profit before the sale date wipes the opportunity?**

The attached PC Peak framework is written for **auction underwriting**. Our transaction model is
different, and that difference reframes the entire framework (§2). This layer adapts the framework's
disciplines — multiple independent valuations, explicit deal-killer gates, a hard ceiling, stress
testing, evidence-over-assumption labeling — to a **pre-foreclosure, direct-from-owner** purchase.

It is built as a separate analytical stage precisely so it inherits the project's standing rule:
**no field silently becomes a real-looking number.** Every output carries a `verified | estimated |
inferred | unavailable` label (framework §XXI), and no offer number is trusted until comps are
human-confirmed.

## 2. Transaction-model reframe (this governs every downstream choice)

We buy **pre-foreclosure, directly from owners/heirs, before any sheriff's sale.** Consequences that
override the attached framework wherever they conflict:

| Framework assumption (auction) | Our reality (pre-foreclosure direct) |
|---|---|
| §34.21 redemption period governs the countdown | **No redemption applies to us** — it attaches only to auction buyers. Our countdown is the existing `oos_date` / `sale_scheduled_date`. |
| Tax sale wipes junior liens | **We inherit the seller's full lien stack** — no wipe. Title/lien discovery is the *primary* gate, not a secondary check. |
| Possession = post-sale eviction of former owner | Possession is negotiated *into* the purchase from the owner/heir. Occupancy still matters, but the mechanism is a deal term, not a writ. |
| Heirs/estates = edge case | **Heirs/estates are core pipeline.** The existing `estate_heir` / `owner_type='estate'` / `property_intel.estate_flag` signals feed this directly. |
| Winning bid is the acquisition cost | Our cost is the **Agreed Price** negotiated with the seller, and closability is set by whether payoffs fit under that price (§5). |

**The countdown is a closing deadline, not a redemption clock.** `oos_date` (Order of Sale issued)
and `sale_scheduled_date` (auction on the calendar) are the dates by which a deal must *close* or the
property goes to auction and the direct-purchase opportunity is lost. The layer surfaces "days to
close-or-lose," anchored on `sale_scheduled_date` when present, else the confirmed `oos_date`, else
`compute_projection().projected_oos` (labeled *estimated*, carrying its existing confidence).

## 3. Architecture — module boundaries and data flow

### 3.1 One-line data flow

```
        (existing, unchanged)                    (NEW — this layer)
discover.py ──► cases.property_intel  ──►  acquisition_engine  ──►  acquisition_analysis (stored)
   (DCAD/ACT enrichment blob)          ▲         │                        │
cases.{oos_date, sale_scheduled_date,  │         ├──► NTREIS comp engine ──► comps + comp_confirmations
  total_due_filing, estate_heir,       │         │        (NEW external source)
  all_defendants, case_track, ...}  ───┘         └──► reads liens (SOURCE UNRESOLVED — §5.3, [OPEN #1])
                                                          │
                                              GET /api/cases/{cn}/acquisition ──► "Acquisition" tab
```

### 3.2 Modules (proposed)

- **`acquisition.py`** (NEW, local + importable by backend) — pure functions, no I/O:
  `mission_score()`, `mao_rule()`, `mao_itemized()`, `seller_net_sheet()`, `exit_matrix()`,
  `sensitivity()`, `deal_gates()`, `subject_condition_estimate()`. Takes a normalized case +
  confirmed comps + human inputs; returns the analysis dict. **All tunable numbers live in a named
  `ACQ_CONFIG` constant** (§6.4), never inline literals — the fee-constant lesson, applied up front.
- **`comps.py`** (NEW, local) — the NTREIS client + qualification/adjustment engine. Fetches
  candidate comps, qualifies (§6.2), ranks (MatchScore), attaches Media URLs, returns *ranked
  candidates* for human confirmation. Never auto-selects the comp set that drives a trusted ARV.
- **`backend/main.py`** (existing) — new read endpoint(s) that assemble the analysis from stored
  data, plus write endpoints for human confirmations (comp accept/reject, ARV/repair/lien inputs).
- **`frontend/index.html`** (existing) — a new `Acquisition` tab (§10), same `swTab` lazy-render
  pattern as Property Intel / Defendants.

### 3.3 Data-ownership decision (mirrors the ledger)

The acquisition layer produces two kinds of data:

1. **Derivable / recomputable** — Mission Score, MAO, exit math. Pure functions of inputs; can be
   recomputed on read like `compute_projection()`. Cache only.
2. **Human, non-regenerable** — which comps a rep confirmed/rejected, the confirmed ARV, the entered
   repair estimate, the entered lien stack, the agreed price. **This is exactly the class of data
   `ledger.db` was built to protect** (like `rep_actions`): a raw `pcpeak.db` restore must not be
   able to wipe a rep's confirmed comp set or their negotiated numbers.

**Recommendation:** the human-input tables (`comps`, `comp_confirmations`, `acquisition_analysis`)
live in `ledger.db` (the prod-owned, restore-guarded file), added to `PROD_OWNED_TABLES` so the
get_db authorizer denies DELETE/DROP, and carried by `/api/ledger/export` + `backup_ledger.py` under
the existing fingerprint standard. Comp *photos* are the exception (§6.3 — likely not stored at all).
**[OPEN #2]** confirm ledger.db placement vs a new file, and confirm append-only semantics fit a
workflow where a rep legitimately *changes* their mind on a comp (append a new confirmation row
rather than mutate — same shape as rep_actions).

### 3.4 What this layer reads (no new capture)

From `cases.property_intel` (the JSON blob — confirmed field names): `market_value`, `land_value`,
`improvement_value`, `year_built`/`effective_year_built`/`actual_age`, `living_area_sqft`,
`total_area_sqft`, `bedrooms`, `bathrooms`, `stories`, `construction_type`, `foundation`,
`roof_type`/`roof_material`, `exterior_wall`, `zoning`, `lot_area_sqft`, `current_tax_balance`,
`tax_by_year`, `distress{score,level,signals[]}`, `owners[]`, `is_absentee`, `estate_flag`,
`no_homestead`, `street_view_url`, `ownership_history[]`, `owner_changes[]`.

From `cases` columns: `oos_date`, `oos_issued`, `sale_scheduled_date`, `sale_pulled_date`,
`total_due_filing`, `estate_heir`, `owner_type`, `all_defendants` (JSON), `case_track`,
`property_type`, `property_address`, `account_number`, `assessed_value`, `minimum_bid`, plus
`compute_projection()` output for the countdown fallback.

**Gap to note:** the live tax balance lives *inside* the `property_intel` blob as
`current_tax_balance`, not as a case column — the engine must parse the blob (the frontend already
does this defensively: `typeof pi==="string" ? JSON.parse(pi) : pi`).

## 4. Subject-condition problem (the asymmetry that must be labeled everywhere)

Comps are **interior-verified** (MLS photos). The subject is **exterior-only**. We have no interior
access to a pre-foreclosure property we haven't bought. The layer must never present a subject
condition as if it were verified. Evidence available for the subject:

- **DCAD physical characteristics** — `year_built`, `depreciation_pct`, `construction_type`,
  `exterior_wall` (e.g. ASBESTOS → hazmat), `actual_age`. *Labeled: inferred from assessor record.*
- **Exterior imagery** — Street View (currently a dead placeholder URL, `property_intel.py:879`;
  wiring the real `GOOGLE_STREET_VIEW_KEY` + capturing the image *date* is a prerequisite, §6.3).
  *Labeled: exterior-only, as of <capture date>.*
- **Distress / vacancy / occupancy signals** — `distress.signals[]` (`vacant`, `distressed`,
  `high_depreciation`), `no_homestead`, `is_absentee`, `estate_flag`, docket estate signals.
  *Labeled: inferred.*

**Rule:** subject condition is always `estimated`, with a **default 20–30% rehab contingency when no
interior access** (framework §VII; the exact default is `ACQ_CONFIG`, **[OPEN #3]** 20 vs 25 vs 30
default). Map DCAD depreciation + distress to a condition class (C1–C6) as an *estimate*, and show the
evidence chain behind it. This directly implements the user's condition-evidence requirement.

## 5. The two calculators — STRICTLY SEPARATE

This is the single most important correctness boundary in the layer. They answer different questions,
use different inputs, and must never be blended into one number.

### 5.1 MAO — our acquisition ceiling ("what can we pay?")

```
MAO_rule = (ARV × Rule%) − Repairs
```

- **Taxes and liens are NOT deducted from MAO.** (Framework §XII double-count rule: the `(1 − Rule%)`
  margin already absorbs profit/carry/selling costs; subtracting taxes again would double-count.)
- Compute the full rule ladder (60/65/70/75/80) plus an **itemized MAO** (framework §XIII, Mode B) as
  an independent cross-check. Recommend the rule% by risk tier (§7), never a fixed 70%.
- ARV comes **only from human-confirmed comps** (§6.5). Until confirmed, MAO is rendered *provisional*
  and no offer number is trusted.

### 5.2 Seller Net Sheet — rep-facing negotiation output ("what does the seller walk away with?")

```
Seller Net = Agreed Price
             − ACT live balance            (the tax payoff — used AS-IS)
             − Tax-suit attorney fees       (SEPARATE line, estimated until confirmed)
             − Mowing/labor & other liens   (SEPARATE line, estimated until title search)
             − Seller closing costs
```

- This is what a rep shows a seller who asks "what do I get?" It is **not** our ceiling and is computed
  from the **Agreed Price** (a negotiated input), not from ARV.
- **PAYOFF MODEL (corrected 2026-07-19).** The tax payoff **IS the ACT live balance**
  (`property_intel.current_tax_balance`), used **AS-IS** — never re-derived, never accrued upon (it
  already reflects taxes + penalties + interest to date). Labeled **verified**. The old §33.48 accrual
  formula is demoted to a **fallback estimator, used only when no live balance exists**, labeled
  **estimated**. Implemented in `acquisition.tax_payoff()`.
- **Tax-suit attorney fees are a SEPARATE line** (`tax_suit_attorney_fees()`), estimated (~15% pre- /
  20% post-judgment of the balance) **until the LGBS payoff letter confirms** — never folded into the
  tax payoff.
- **Mowing/labor & other liens are a SEPARATE line**, estimated **until a title search** (part of the
  lien stack, §5.3). Unknown → the line is `UNAVAILABLE` and closability is INDETERMINATE.
- `Seller Closing Costs` = `ACQ_CONFIG` default (Dallas), labeled estimated.
- **[OPEN #4] — follow-up, tracked.** The deployed frontend `calcPayoff()` still uses the OLD accrual
  model. It must be fixed to match (live balance as-is; attorney fees a separate line) so the UI and
  engine agree. This is a deployed-UI change, done deliberately and separately — NOT part of Stage 1.

### 5.3 The fatal gate — closability is set by the lien stack, not by our ceiling

```
FATAL if:  Total Payoffs (tax + all liens + seller closing costs)  >  Agreed Price
           → seller nets negative → the deal cannot close.
```

A deal can be well under MAO and still be unclosable because the payoffs exceed what the seller can
accept. **This gate is load-bearing and it depends on lien data we do not currently have a source
for.** The deed/lien portal (`dallas.tx.publicsearch.us`) is robots- and ToS-blocked
([[publicsearch-tos-research]]): no scraping. So:

**[OPEN #1] — the primary open decision. Where does the lien stack come from?** Options:
  1. **Manual rep entry** — a rep enters known liens (from a title company prelim, a PIA response, or
     their own research) into the Acquisition tab. Ships first; zero new data source; honest about
     what's unknown. Until entered, the lien stack is `UNAVAILABLE` and the fatal gate returns
     `INDETERMINATE — lien discovery required` (never a false GO).
  2. **Licensed provider** (TexasFile covers Dallas OPR) — a real integration, a business/cost
     decision, later phase.
  3. **PIA request to the County Clerk** — bulk, slow, not per-deal.

Recommendation: **Phase-1 = manual entry with an explicit `UNAVAILABLE` state**; treat automated lien
discovery as its own initiative behind the same business decision already documented for deed data.
The design must make "we don't know the liens yet" a *visible mission-blocking state*, not a silent
zero — this is the §33.48-style falsy-conflation trap in a new place.

### 5.4 Valuation hierarchy (LOCKED — no offer number ever rests on DCAD)

- **ARV comes from confirmed NTREIS comps ONLY.** It is the sole basis for any MAO/offer number, and
  only after human confirmation (§6.5). No offer figure is ever computed from DCAD.
- **DCAD market value is NEVER a valuation source.** Its only two roles:
  1. A **sanity-check band** — flag when a confirmed comp ARV diverges sharply from assessed
     (`acquisition.arv_sanity_band()`, default ±30%, `ACQ_CONFIG`). A flag prompts review; it never
     sets or caps an offer.
  2. A **labeled-estimated provisional placeholder** for triage ranking only (so a case has *a*
     number before comps exist), never trusted for an offer.
- **Stage-1 enforces this structurally (verified):** `mao_ladder`/`mao_itemized` are computed *only*
  from a supplied ARV — DCAD `market_value` is passed through as a labeled `verified`-assessor display
  value and is read by no offer calculation. `valuation_state` is `confirmed` **only** when the ARV
  label is `verified` (human-confirmed comps); an `estimated` (auto/triage) ARV keeps the whole
  analysis `provisional` and the Mission Score provisional. Pinned by `test_valuation_hierarchy_no_offer_on_dcad`.

## 6. Comp engine (NTREIS / NTREIS RESO Web API)

### 6.1 Confirm the API before designing around it (hard pre-build gate)

**The comp engine is fully greenfield.** `NTREIS_BASE_URL` and `NTREIS_SERVER_TOKEN` exist in
`Anthropic_API_KEY.env` but **no code references them** — there is nothing in the repo to read the
return shape from. The `server token` naming and the RESO Web API pattern point to a **Bearer-token
OData feed** (Bridge Interactive `bridgedataoutput.com` or CoreLogic Trestle), i.e. RESO
Data-Dictionary resources `Property`, `Member`, `Office`, `Media` with `$filter/$select/$top/$skip/
$orderby/$expand`, a `$metadata` document, and Media (photos) either as its own resource or via
`$expand` on `Property`. Auth: `Authorization: Bearer {NTREIS_SERVER_TOKEN}`.

I **could not confirm the live return shape** — the sandbox cannot reach the NTREIS host, and I will
not design final field mappings against an assumed schema. **Before any comp code is written, confirm
against the live endpoint (user runs, or we run once egress allows):**

1. **Which platform / base URL** — hit `{NTREIS_BASE_URL}/$metadata` with the bearer token; capture
   the dataset id and the resource list.
2. **Sold data availability — THE critical question.** Appraiser-grade comps need **closed sales with
   close prices**. Many feeds are IDX-only (active listings, no sold prices). Confirm `Property`
   exposes `StandardStatus=Closed`, `ClosePrice`, `CloseDate`. **If sold data is not licensed, the
   comp engine as specified cannot be built on NTREIS** and this becomes a licensing decision. [OPEN #5]
3. **Media/photos** — confirm the `Media` resource (or `Property.Media` via `$expand`) returns photo
   URLs, `PhotosCount`, and the MLS's **photo display/usage terms** (hotlink vs cache; watermark;
   attribution). [OPEN #6]
4. **Field coverage** — confirm the RESO fields the engine needs (below) are actually populated for
   Dallas: `LivingArea`, `LotSizeSquareFeet`/`LotSizeAcres`, `BedroomsTotal`, `BathroomsTotalInteger`,
   `YearBuilt`, `PropertySubType`, `Latitude`/`Longitude`, `StandardStatus`, `ListPrice`, `ClosePrice`,
   `CloseDate`, `DaysOnMarket`, `SubdivisionName`, `PublicRemarks` (for condition/flip signals).
5. **Rate limits / replication** — per-request `$top` cap, daily quota, whether we page candidates
   live per case or maintain a local replicated slice. [OPEN #7]

Everything below is written to the RESO standard and will be reconciled to the real `$metadata` in
the build session. Design it as a **first-class, always-available layer** (the key is free/zero
marginal cost per the user) — not an optional enrichment.

### 6.2 Appraiser-grade qualification

Candidate → qualified comp requires (all tunable in `ACQ_CONFIG`, Dallas defaults, [OPEN #8] for the
exact default values):

- **Distance tiers** — e.g. ≤0.5 mi (tier 1), ≤1.0 mi (tier 2), ≤2.0 mi (tier 3); same subdivision
  is a bonus. Wider tiers only when tier 1 is thin, and the tier used is labeled.
- **Recency windows** — e.g. ≤90 days (tier 1), ≤180, ≤365; older only when necessary, labeled.
- **GLA band** — subject `living_area_sqft` **±15–20%** (default which end? [OPEN #8]).
- **Property-type / structure match** — `PropertySubType`, stories, year-built band.
- **Arm's-length verification** — flag and *exclude by default* (rep can override): flips (short hold +
  large price jump), family transfers (surname match), foreclosure resales / REO / auction
  (`PublicRemarks` + status heuristics). Mirrors `property_intel.py`'s existing arm's-length flag
  vocabulary so the labels are consistent.
- **MatchScore** — the framework §XII weighting (size 25 / distance 15 / time 10 / beds-baths 10 /
  stories 10 / year 10 / features 10 / lot 5, ± subdivision/condition), tunable, used only to *rank*
  candidates for human review — never to auto-finalize.

### 6.3 MLS photos render inline — mandatory

Every candidate comp shows its MLS photos inline in the confirmation UI (the user's hard requirement —
a human can't confirm a comp they can't see). Design questions:

- **Storage / CSP** — MLS licenses usually restrict photo caching. Default to **hotlinking the MLS
  media URLs** at render time (no local storage of copyrighted photos), which also sidesteps the
  Artifact-style CSP concern. Confirm the MLS's terms (6.1 #3). [OPEN #6]
- **Subject imagery** — Street View must be wired to the real key and the **capture date** captured
  and shown (exterior-only label). Fix the `property_intel.py:879` placeholder as a prerequisite.
  [OPEN #9] does subject Street View belong to this layer or is it a `property_intel` fix pulled
  forward? Recommend: small `property_intel` fix, done first.

### 6.4 Adjustments as named config (never hardcoded)

All adjustment magnitudes are named, tunable, Dallas-defaulted — the fee-constant lesson applied from
day one:

```
ACQ_CONFIG = {
  "adjustments": { "price_per_sqft_gla": <$>, "per_bath": <$>, "per_bed": <$>,
                   "condition_delta_per_class": <$>, "per_garage_stall": <$>,
                   "lot_sqft": <$>, "per_year_age": <$>, ... },      # Dallas defaults, [OPEN #8]
  "qualification": { "distance_tiers_mi": [...], "recency_tiers_days": [...],
                     "gla_band_pct": 0.15..0.20 },
  "rehab": { "no_interior_contingency_pct": 0.20..0.30, "condition_class_cost_psf": {...} },
  "rule_pct_by_risk": { "clean": 0.75, "standard": 0.70, "tax_foreclosure": 0.65, "severe": 0.60 },
  "seller_closing_costs": { ... }, "required_profit": { "mode": "pct_of_arv"|"fixed", "value": ... },
}
```

Every adjustment applied to a comp is stored *itemized* (which knob, what $, why) so an ARV is fully
auditable — "show the numbers" (framework §XXI). Changing a default is a signed-off action, like
`CITY_DATA` ([[city-data-frozen-sample-size]]).

### 6.5 Propose → confirm → compute (valuation confidence has two states)

```
engine pre-qualifies + ranks candidates (with photos)   →   PROVISIONAL ARV (triage only)
        │                                                      (auto-comps; never trusted for an offer)
        ▼
human confirms / rejects EACH comp                       →   CONFIRMED ARV
        │                                                      (from confirmed comps only)
        ▼
MAO / offer numbers unlocked                                  no offer number is "trusted" before this
```

- **Provisional** — auto-selected comps, shown for triage, watermarked provisional. Mission Score's
  valuation-confidence component is capped low.
- **Confirmed** — ARV computed from the human-confirmed comp set only. Required before any MAO/offer
  number is presented as trusted. This is the valuation analog of the confirmed-vs-projected OOS
  distinction the UI already draws.

## 7. Mission Score, gates, decision (adapted to pre-foreclosure)

- **Mission Score** — keep the framework's 0–100 structure but **drop redemption certainty** (N/A for
  us) and **reframe possession** (negotiated, not evicted). Proposed reweight: Title/Lien certainty
  25 · Valuation confidence 20 · Margin strength 15 · Condition confidence 15 · Exit liquidity 10 ·
  Timeline-to-close reliability 10 · Occupancy/possession clarity 5. **[OPEN #10]** weights need
  sign-off and should show `n=`/confidence like every other stat — do NOT ship as authoritative
  without validation (§13).
- **Deal-killer gates (fatal overrides, framework §XVIII/§XXVIII, pre-foreclosure subset):**
  `Total Payoffs > Agreed Price` (§5.3) · lien stack `UNAVAILABLE` → `INDETERMINATE` · no path to
  clear/insure title · legal-description/parcel mismatch (identity gate) · `property_type='personal'`
  (BPP — already excluded upstream) · combined-stress loss beyond tolerance · sale date too close to
  close in time.
- **Decision states** — GO / GO-WITH-CONDITIONS / HOLD (due diligence) / NO-GO, with a fatal gate
  overriding the numeric score. Mirror the existing projection-basis honesty: a GO on provisional
  comps is impossible by construction.

## 8. Exit matrix & sensitivity (framework §XIV / §XVI, condensed)

- **Exits modeled** (each independent, with basis + confidence): wholesale/assignment, vacant as-is
  resale, light-rehab resale, full flip, buy-and-hold (NTREIS rental comps → NOI/cap/DSCR/CoC), and
  land/redevelopment where zoning supports it. Primary + backup exit are required outputs.
- **Sensitivity** — base / rehab overrun (+10/20/30%) / market decline (−5/10/15%) / timeline & close
  delay / combined stress / severe. Report whether the deal survives the **combined stress case** —
  the framework's "don't win the auction and lose the investment" is our "don't close and lose the
  capital." **[OPEN #11]** how deep does Phase 1 go — full exit matrix + sensitivity, or ship
  MAO + Seller Net + gates first and add exits/sensitivity in Phase 2? Recommend phased (§15).

## 9. Schema (comp storage + confirmation state + analysis)

All in `ledger.db` (§3.3), append-only, restore-guarded. Field lists are **provisional pending the
NTREIS `$metadata` confirmation** (§6.1).

```
comps                          # one row per candidate comp fetched for a subject case
  id, case_number, mls_number, fetched_at, model_version,
  address, latitude, longitude, distance_mi,
  close_price, close_date, list_price, days_on_market, standard_status,
  gla_sqft, lot_sqft, beds, baths, year_built, property_subtype, subdivision,
  match_score, arms_length_flags (JSON), photo_urls (JSON — hotlink refs, not blobs),
  raw (JSON — the full RESO record as returned, for audit)

comp_confirmations             # append-only human decisions (a rep can change their mind → new row)
  id, case_number, comp_id, rep, decided_at, decision ('confirmed'|'rejected'),
  adjustments_applied (JSON — itemized knob→$→reason), adjusted_value, note

acquisition_analysis           # one current analysis per case (human inputs + computed snapshot)
  id, case_number, updated_at, updated_by, model_version,
  # human inputs:
  confirmed_arv, repair_estimate, repair_basis ('estimated_no_interior'|'contractor'|...),
  rule_pct_used, lien_stack (JSON — each lien: type/amount/holder/source/verified),
  lien_status ('unavailable'|'partial'|'verified'), agreed_price,
  # computed snapshot (recomputable; stored for the ledger/history):
  mao_rule_ladder (JSON), mao_itemized, seller_net, total_payoffs, closable (bool|null),
  mission_score, decision, gates_triggered (JSON), exits (JSON), sensitivity (JSON),
  valuation_state ('provisional'|'confirmed')
```

**[OPEN #12]** `acquisition_analysis` — one mutable current row per case (with history via
`case_snapshots`-style diff-on-write) vs append-only versions? Recommend append-only versions keyed by
`updated_at`, with a "latest" read — consistent with the ledger philosophy and gives free history of
how a deal's numbers moved.

## 10. UI surface — the `Acquisition` tab

Follows the exact existing pattern (`frontend/index.html`, ~2,312 lines, single-file): add one
`<button class="tb" onclick="swTab(event,'acq')">Acquisition</button>` to the `.tbar`, one
`<div class="tp" id="tp-acq">` panel, and an `if (id === "acq") { renderAcquisition(...) }` branch in
`swTab` (lazy render, like Property Intel / Defendants). Reuse existing conventions: the
confirmed-vs-projected box styles, the `.proj-cf` confidence bar, the per-event `verified/estimated/
inferred` source labels, and the distress banner.

Panel layout (framework §XX executive-decision-first):
1. **Mission banner** — decision + Mission Score + days-to-close-or-lose + fatal flags.
2. **Valuation** — provisional/confirmed state; ARV with the confirmed comp set; MAO rule ladder +
   itemized; subject condition estimate with its evidence chain (labeled).
3. **Comp workbench** — ranked candidates with **inline MLS photos**, MatchScore, arm's-length flags,
   per-comp confirm/reject, itemized adjustments; subject Street View (exterior, dated).
4. **Seller Net Sheet** — Agreed Price input → tax payoff / lien payoffs / seller closing → seller net;
   the fatal-gate banner (`closable` / `INDETERMINATE — lien discovery required`).
5. **Exits & sensitivity** (Phase 2, [OPEN #11]).
6. **Every number labeled** `verified | estimated | inferred | unavailable`.

**[OPEN #13]** does the existing frontend `calcPayoff`/`project` stay client-side, or does the
Acquisition tab read a fully server-computed analysis from `GET /api/cases/{cn}/acquisition`?
Recommend server-computed for the acquisition math (single source, auditable, stored), with the tab as
a thin renderer + the confirm/input POSTs.

## 11. API surface (proposed)

- `GET  /api/cases/{cn}/acquisition` — assembled analysis (stored analysis + latest confirmations +
  recomputed snapshot). Open read (case facts, like `/snapshots`) **[OPEN #14]** — but rep-entered
  agreed price / negotiation numbers may be sensitive; consider token-gating writes and possibly reads.
- `POST /api/cases/{cn}/comps/refresh` — trigger an NTREIS candidate fetch (token-gated — it spends an
  external call; reuse the scrape-trigger token discipline). Returns ranked candidates.
- `POST /api/cases/{cn}/comps/{comp_id}/confirm` — append a `comp_confirmations` row.
- `POST /api/cases/{cn}/acquisition` — upsert human inputs (confirmed ARV, repairs, lien stack, agreed
  price); recompute + store.

## 12. Assumption labeling (framework §XXI — non-negotiable, pervasive)

Every value in every output carries one of: **verified** (from a controlling record — court, DCAD
assessor value as assessor-verified, confirmed comp close price), **estimated** (subject condition,
rehab, ARV-from-adjustments), **inferred** (distress/occupancy signals, condition class from
depreciation), **unavailable** (lien stack before entry — rendered as a blocking state, never 0/blank).
This is enforced structurally: the engine returns `{value, label, evidence}` tuples, and the renderer
cannot display a value without its label.

## 13. Validation plan (proof before trust — non-negotiable, gates first trust)

Before any score/number is trusted on an unknown case, run the **full analysis against known cases and
confirm the outputs match human analysis** — same discipline as `scorecard.py` and the fingerprint
standard.

**Golden reference cases** (in the data today):
- **TX-23-00423 (Tryon)** — user-supplied truth: **$71,938 owed / $217,800 MV.** Full pre-foreclosure
  walk-through: does the layer produce a sane MAO ladder, a Seller Net Sheet, and a closability verdict
  matching a human's read?
- **Ruby Faye Brown's case** — present in `seed_cases.py` / the dump; **[OPEN #15] the exact case number
  and the human-analysis numbers must be supplied** to validate against (I found the name in the data
  but not a confirmed golden-number set).
- **TX-25-00249** — in the data.
- **Suggested add: TX-23-00569 (1506 Harbor Rd)** — a rich estate/heir, occupied, sale-*pulled* case
  with assessed $286,730, total-at-OOS $216,554, minimum bid $226,910 (thin equity). An ideal stress
  test for the fatal gate and the occupancy/estate path.

**Method:** for each case, produce the full Acquisition output and diff every material number
(condition class, ARV band, MAO ladder, tax payoff, seller net, closability, Mission Score, decision)
against a human analysis. **Discrepancies block trust** — the layer ships in "provisional/triage"
framing until the golden set passes, exactly as `CITY_DATA` stays frozen until n≥40. The validation
harness is a test file (`test_acquisition.py`) pinning the golden numbers on a fixture, runnable
offline (no NTREIS/portal), like `test_ledger_tools.py`.

**Confirming NTREIS accuracy separately:** comp-engine ARV can only be validated once the live feed is
confirmed (§6.1) — until then the golden-case validation runs against *manually supplied* comp sets so
the *math* is proven independent of the *feed*.

## 14. Open decisions (consolidated — every one needs a call before/at build)

| # | Decision | Recommendation (not yet approved) |
|---|---|---|
| **1** | **Lien-stack source** (load-bearing — sets the fatal gate) | Phase-1 manual rep entry + explicit `UNAVAILABLE`→`INDETERMINATE` state; automated (TexasFile/PIA) is a separate business decision |
| 2 | Human-input tables in `ledger.db` vs new file; append-only fit | `ledger.db`, `PROD_OWNED_TABLES`, append-only versions |
| 3 | Default no-interior rehab contingency % | 25% default (band 20–30), in `ACQ_CONFIG` |
| 4 | `calcPayoff` port-to-Python vs duplicate | Port to backend; frontend calls it |
| **5** | **NTREIS sold-data (ClosePrice/CloseDate) licensed?** | MUST confirm live; if not, comp engine can't be built on NTREIS as specified |
| 6 | MLS photo storage/usage terms (hotlink vs cache) | Hotlink, no local copy; confirm MLS terms |
| 7 | NTREIS live-per-case fetch vs local replication + rate limits | Confirm quota; likely live-per-case with a short cache |
| 8 | Dallas default adjustment $ + qualification tiers | Seed from a Dallas appraiser/market source; all in `ACQ_CONFIG`, signed-off |
| 9 | Street View fix owned by this layer or `property_intel` | Small `property_intel` fix first (real key + capture date) |
| 10 | Mission Score weights (redemption dropped, possession reframed) | Proposed reweight in §7; needs sign-off + `n=` display |
| 11 | Phase-1 depth: include exit matrix + sensitivity or defer | Defer full exits/sensitivity to Phase 2 |
| 12 | `acquisition_analysis` mutable-row vs append-only versions | Append-only versions, "latest" read |
| 13 | Acquisition math client-side vs server-computed | Server-computed, tab is a thin renderer |
| 14 | Read/write auth on acquisition endpoints (agreed price sensitive?) | Token-gate writes + the comp-refresh; decide read gating |
| 15 | Ruby Faye Brown case number + golden human-analysis numbers | User to supply the golden set |

## 15. Build sequencing (order is load-bearing, if approved)

Not a commitment — the shape of a phased build so each piece is provable before the next, mirroring the
ledger's Step 1–6 discipline. **Nothing is built until this design is approved.**

- **Phase 0 — confirm reality.** Hit NTREIS `$metadata` + a sample Property/Media query with the token
  (resolves [OPEN #5/#6/#7]); fix the Street View placeholder + capture date. No product code.
- **Phase 1 — the calculators + gate + validation harness.** `acquisition.py` (MAO ladder, itemized,
  Seller Net Sheet, fatal gate, subject condition estimate), `ACQ_CONFIG`, `test_acquisition.py`
  pinning the golden cases against *manual* comp sets. Lien stack = manual entry / `UNAVAILABLE`.
  Read endpoint + a minimal Acquisition tab (valuation + seller net + gate). Everything labeled.
- **Phase 2 — the comp engine.** `comps.py` (NTREIS client, qualification, MatchScore, Media), the
  propose→confirm workbench with inline photos, `comps`/`comp_confirmations` schema, provisional→
  confirmed ARV wiring. Validate comp ARV against the golden set once the feed is confirmed.
- **Phase 3 — exits, sensitivity, Mission Score.** Exit matrix, stress testing, the reweighted Mission
  Score (with sign-off), decision states. Held until Phases 1–2 are proven under real use.

Each phase is a separately-gated, explicitly-labeled ask — deploy vs. architecture stay separate, as
always on this project.

---

**Bottom line for approval:** this layer is a downstream, read-mostly analysis stage over enrichment
that already exists; it introduces no new court scraping; it keeps MAO and Seller Net Sheet strictly
separate with the lien-stack fatal gate as the primary closability test; and it makes the comp engine a
first-class NTREIS layer with human-confirmed valuation. **The two decisions that most shape the build
are [OPEN #1] (lien-stack source) and [OPEN #5] (does NTREIS actually return sold data).** Both should
be resolved before Phase 1/Phase 2 respectively. No code until you approve.

---

## 16. §G LAND FLOOR + propose-batch legibility (approved 2026-07-23 — build authorized)

**Why.** Live finding on **TX-26-01190** (6406 Kemrock Dr, Dallas 75241): propose returns 200 with a
valid batch and **0 closed + 0 pending**, while comparable cases return 20+. The funnel:

| Stage | Survivors |
|---|---:|
| `PropertyType='Residential' and StandardStatus='Closed'` + zip 75241 | 1,529 |
| + recency (`CloseDate ge` 1yr) | 282 |
| **+ GLA band [387, 580]** | **0**  ← the stage that zeroes it |

The subject is **484 sqft GLA** (built 1935); recent closed sales in 75241 run **870 – 3,550 (median
1,527)** — the subject is *below the market's smallest recent sale*, so nothing can fall in a ±20% band.
The band is behaving correctly (an appraiser would not comp 484 sf against 1,527 sf). The subject is a
**sub-minimum structure on a land-dominant parcel**: DCAD $120,340 = improvement $50,340 + **land
$70,000** (58% land). **Zero qualified comps must never mean zero valuation information — there is
always land under the house.**

### 16.1 Data availability — VERIFIED live (Phase-0 discipline, 2026-07-23)
- `PropertyType eq 'Land'` closed: **60,400** records. Zip 75241: **30** closed land sales in the last
  year (vs 0 improved in-band). Neighbors: 75216 → 30, 75215 → 53. Depth is there.
- **30/30 carry BOTH `LotSizeAcres` and `NTREIS2_RATIO_ClosePrice_By_LotSizeAcres`** — the Phase-0
  verified reconstruction works identically for land. **ONE reconstruction path serves improved + land**,
  exactly as specified (§6.1 / `comps.land_value_from_comp`).

### 16.2 METHOD — lot-size band, NOT a $/acre extrapolation (measured, not assumed)

| Method (subject 0.166 ac, 75241) | Land floor |
|---|---:|
| Naive: median $/acre across ALL lot sizes × subject acreage | $51,130 |
| **Lot-size band ±30% (n=14): median reconstructed close** | **$85,500** |
| DCAD assessor land (sanity band only) | $70,000 |

A **67% understatement** by the naive method — $/acre is strongly size-dependent (small lots carry far
higher $/acre: 0.141 ac → $780k/acre vs 0.298 ac → $302k/acre). The banded result is **stable** (±30%
and ±40% both n=14 → $85,500; ±50% → $81,000), so the band is not knife-edge.
**LOCKED:** band land comps by **LOT SIZE** and take the **median of RECONSTRUCTED CLOSES**. $/acre may
be displayed as a secondary figure, **never as the basis**. (Same lesson as the GLA band for improved.)

### 16.3 Algorithm
1. **Subject acreage** — `property_intel.lot_area_sqft / 43560`; fallback NTREIS LotSize fields. No
   acreage → no land floor (fail closed, labeled `unavailable`).
2. **Fetch land comps** — `PropertyType eq 'Land' and StandardStatus eq 'Closed'` + locality
   (`_locality_clause`: PostalCode else City) + `CloseDate ge` recency floor + `LotSizeAcres` in band.
3. **Reconstruct each close** — `NTREIS2_RATIO_ClosePrice_By_LotSizeAcres × that comp's own LotSizeAcres`.
4. **Qualify** — recency, arm's-length flags, min price. **NO GLA band** — land has no GLA; applying the
   improved-comp band would zero the set (explicit guard).
5. **Land floor = median of the qualified banded comps' reconstructed closes**, labeled `estimated`,
   carrying `n=`, range and spread (feeds the appraisal-report reconciliation line, §Stage-3).
6. **Display** as **"Land floor"** in the valuation block; DCAD `land_value` shown beside it as a
   **sanity band only** (§5.4 — DCAD is never a valuation source).

### 16.4 Approved decisions (2026-07-23)
1. **Lot-size band ±30% default** — tunable config like every other adjustment.
2. **Land-comp definition: `PropertyType='Land'` ONLY**, first. Teardown-intent improved sales are a
   **Stage-3 refinement** — do not blur the set now.
3. **The land floor NEVER feeds MAO** — hard rule, **test-pinned like the DCAD lock** (§5.4).
4. **Land recency: tunable config, default 24 months** (land moves slower than improved).

### 16.5 Guardrails (all approved as stated)
- A **floor, not an ARV** — never silently becomes the ARV or drives MAO (see 16.4.3).
- **Display + triage only** initially; a rep-selected land/teardown **exit mode** (with its own confirm
  step) is Stage 3.
- **A land floor alone never lifts a case out of HOLD** — consistent with the 2026-07-21 decision table
  (a derived/unconfirmed valuation is information, not a verdict).
- **COMPUTE ALWAYS** — a standing floor line on **every case that has a lot size**, not only when comps
  come back empty. One extra query per propose; doubles as a permanent sanity band.

### 16.6 Propose-batch legibility (rides this increment — not a bundle of convenience)
**Root cause:** an empty propose stores **no comps rows**, so **no batch record exists** — the UI
literally cannot distinguish "never proposed" from "proposed, 0 qualified." It therefore needs a
persisted batch record, which is why it is one coherent unit with §G rather than a UI tweak.
- **Schema:** append-only **`comp_batches`** in `ledger.db` (added to `PROD_OWNED_TABLES`):
  `case_number, fetch_batch, fetched_at, locality_used, gla_band, n_raw, n_stored_closed,
  n_stored_pending, n_qualified, zero_reason`.
- **Backend:** propose **always** writes a batch row (including at 0); `GET /acquisition` returns
  `latest_batch`.
- **UI:** replace the ambiguous "No comps proposed yet" with the actual outcome — e.g. *"Proposed
  2026-07-23 — 0 of 282 candidates qualified · locality zip 75241 · GLA band [387,580] · no closed sale
  in band (zip GLA range 870–3,550)"* — surfacing the funnel instead of a dead end.

### 16.7 Priority — the land path is load-bearing for THREE populations
With a land path the propose gate becomes **locality + (GLA OR lot acreage)**: GLA drives the
improved-comp path, lot acreage drives the land path. That makes §G load-bearing for:
(a) the **60 no-GLA** cases; (b) **zero-comp pockets** like TX-26-01190; (c) **teardown checks**.

**→ Framing to carry into the pending 60-no-GLA measurement session.** Kemrock is the **boundary case
that measurement is already clustering on**: a **484 sqft improvement** valued at $50,340 against
$70,000 of land is exactly the "near-zero improvement value" bucket — except the structure is *not*
zero, and the case is *not* vacant land. It proves the **land-routing bucket must catch sub-minimum
structures on land-dominant parcels, not just vacant lots.** A clustering rule that only tests
`improvement_value ≈ 0` will misfile these as "has an improvement → failed enrichment" when the correct
routing is land valuation. Recommended cluster test: **land-dominant** (`land_value / market_value`
above a threshold) **OR sub-minimum GLA** (below the local market's smallest recent sale), not
improvement≈0 alone. The 60-no-GLA session should fold **"route to land valuation"** in as a
first-class outcome, not merely backfill-vs-legit.

### 16.8 Acceptance — MEASURED (built + live-verified 2026-07-23)
- **TX-26-01190 (Kemrock)** — 0 improved comps qualify (GLA band); **gross land floor $85,500**
  (n=14, 12mo, no widening), net-of-demolition $77,500. DCAD $70,000 shown **sanity-only**.
  Reproduces the pinned target exactly. ✓
- **TX-26-01379 (Ruby) — PIN RESTATED to the market figure.** **Gross land floor ≈ $72,500** (n=12,
  12mo), **net-of-demolition ≈ $63,900** at the config demo rate. DCAD $70,000 sanity-only.
  **Why the original $42.5K was superseded:** it was a **single-comp $/sqft extrapolation from a lot
  ~36% larger than the subject** — the *same size-dependence error* §16.2 was written to guard against.
  The banded market set (n=12, $45K–$125K) and the DCAD land line ($70K) independently agree at ~$70K.
  The engine caught a human land comp the same way it caught the naive $/acre method. **Ruby's verdict
  is untouched** — GO-WITH-CONDITIONS never rested on the floor (§16.4.3: the floor feeds nothing);
  the deal simply reads stronger with more land under it.
- **HARD PIN (enforced + tested):** the land floor appears in **no** MAO rung and **no** verdict input.
  `analyze()` takes it as a pure passthrough; tests assert the MAO ladder, itemized MAO, decision,
  Mission Score, gates and seller-net sheet are byte-identical with and without it, that it never
  becomes the ARV, and that it never lifts a case out of HOLD.
- **Batch legibility:** a 0-comp propose writes a `comp_batches` row and renders
  "N of M candidates qualified" with the zeroing stage named (Kemrock: "no closed sale in zip 75241
  within GLA band [387,580] — recent sales run 870–3,550 sqft; subject is 484 sqft").

**Lesson recorded:** two independent valuation errors in this increment shared one root cause —
extrapolating a per-unit rate ($/acre, $/sqft) across dissimilar sizes. Band first, then take the
median of actual closes. That rule now applies to improved comps (GLA band) and land comps (lot band).

**Build sequencing:** design committed → build (config + land engine + `comp_batches` + backend + UI +
acceptance pins) → deploy through its own gate, same staged rhythm.

## 17. LOGGED — per-jurisdiction payoff lines (multi-jurisdiction tax collection). NO ACTION YET.

**Filed 2026-08-15. Sequenced AFTER the Stage-2 gate work (below the `heir_estate_title` block).**
Trigger: the Garland ISD Tax Office runs its **own** payment portal
(`texaspayments.com/057909` — session-based, search by name / account / address / CAD number, no API,
no stable per-parcel URL). That is a tax-collection surface the payoff model has never seen.

### 17.1 The schema question comes FIRST — and it is already answered by the code (traced, not assumed)

**The payoff model carries ONE BLENDED TAX BALANCE. There are no per-jurisdiction lines anywhere in
the payoff path.** Traced end to end:

| Layer | What exists today |
|---|---|
| Capture | `property_intel.enrich_property()` → `current_tax_balance = act["total_amount_due"]` — a single scalar scraped from **dallasact.com** (the Dallas County tax office / ACT). |
| Multi-tract | `current_tax_balance` is in the tract `SUM_FIELDS` — that sums across **TRACTS of one parcel**, NOT across **taxing jurisdictions**. Different axis; it does not close this gap. |
| Normalize | `CaseInput.owed: Optional[float]` — one number. |
| Compute | `acquisition.tax_payoff()` → one `{amount, label, basis, note}`. |
| Consume | `seller_net_sheet()` one `tax_payoff` line · `mao_itemized(tax_payoff_total=…)` · `structural_unclosability()` one `payoff["amount"]`. |

The only per-jurisdiction data in the system today feeds **nothing** in the payoff path:
`property_intel.tax_rates` is DCAD's **estimated annual tax** by entity (not a delinquent balance),
and `taxBreakdownSummary` exists only in the hardcoded `KNOWN` benchmark seeds (display-only).

**So the answer is: one blended balance — and the exposure is a LABEL defect before it is an
arithmetic one.** When a live balance exists, `tax_payoff()` labels it `VERIFIED` with the note
"ACT current amount due — used as-is". That label asserts *this number is correct*; it silently also
implies *this number is complete*. Those are different claims, and only the first one was ever
checked. A blended scalar cannot express "verified for the jurisdictions ACT collects, unknown for
any that collect separately."

Note the fallback path has the same shape: with no live balance, `tax_payoff()` estimates from
`total_due_filing` — the petition's Exhibit A, i.e. **the jurisdictions that sued**. A jurisdiction
collecting separately and not joined to the suit is outside that set too.

### 17.2 MEASURED 2026-08-15 — GISD is ADDITIVE. The branch is REAL; §17 does not close.

The one-parcel comparison was run before any build, and it resolved on a better instrument than a
total-vs-total diff: **ACT publishes its own per-parcel jurisdiction breakdown**
(`reports/taxbyyearbyunit.jsp?can=<account>`), which enumerates exactly which taxing units ACT
collects for. That converts the question from inference to enumeration.

**Parcel A — TX-23-02251, 729 Woodcastle Dr, Garland 75040, CAD `26238500070260000`**

| Source | Units collected | Annual levy | Balance due |
|---|---|---|---|
| ACT (dallasact.com) | DALLAS COLLEGE · DALLAS COUNTY · PARKLAND HOSPITAL · SCHOOL EQUALIZATION | $1,430.16 | **$6,847.91** (2022–24) |
| GISD portal (texaspayments.com/057909) | GARLAND ISD — **separate account `0000056331`** | **$2,280.09** | $0.00 (last paid 03/24/2026) |

- **ACT's total does NOT include the ISD.** ACT's own jurisdiction detail names four county-side units
  and no school district and no city.
- **The ISD is the LARGER share.** GISD's annual levy ($2,280.09) is **1.59× ACT's entire annual levy**
  ($1,430.16) on the same parcel — **~61% of the annual tax burden sits outside the number the payoff
  model calls the verified tax payoff.**
- On *this* parcel the ISD balance happens to be **$0.00**, so its payoff is not understated. That is
  parcel-specific (this owner is current on ISD, delinquent county-side) — **it is not evidence the gap
  is closed.** The structural separation is what generalizes; the dollar amount is per-parcel.

**Contrast — TX-23-00569, 1506 Harbor Rd, Dallas, CAD `00000503698000000`** (same ACT report):
units = **CITY OF DALLAS · DALLAS COLLEGE · DALLAS COUNTY · DALLAS ISD · PARKLAND HOSPITAL · SCHOOL
EQUALIZATION**, total $196,930.17. In Dallas, ACT collects the ISD **and** the city.

**This is the finding.** `current_tax_balance` means **"every taxing unit"** on a Dallas parcel and
**"county-side units only"** on a Garland parcel — and it carries the **same `VERIFIED` label in both
cases**. The label defect §17.1 predicted is now measured, not hypothesized. The number is not wrong;
its *scope silently varies by locality* and nothing in the schema records which scope applied.

**Second Garland parcel (TX-26-00774, 4110 Hillsdale Ln, 75042, CAD `26545500120260000`):** ACT total
$6,864.17, same four county-side units — the ACT pattern replicates. Its CAD returned **0 matches on
the GISD portal**, which is NOT yet explained: a Garland *mailing address* does not imply Garland ISD
(parts of the city sit in Richardson ISD and Dallas ISD). Do not read it as "no ISD debt."

**Consequences (both confirmed live, sequencing unchanged — after the Stage-2 gate):**
1. **Schema per §17.3 (a/b/c)** — authorized as a design.
2. **Recompute pass on the already-analyzed Garland cases** — but it has a PREREQUISITE that this
   measurement exposed: **we cannot currently identify which ISD a parcel belongs to.** ACT omits the
   ISD line entirely for these parcels, and our stored DCAD `tax_rates` does not supply it either — for
   parcel A it holds a single malformed row (`"DALLAS COUNTY\tDALLAS COLLEGE\tPARKLAND HOSPITAL\t
   UNASSIGNED\nHOMESTEAD EXEMPTION"`, `estimated_tax: 41539.0`) and for parcels B and C it is empty.
   So the pass cannot be keyed on "Garland appears in the address" — it needs a
   **jurisdiction-identification step first**, and the malformed `tax_rates` parse is a separate defect
   to look at when that step is designed. **Absent that step, the correct state for those cases is
   `unavailable` (§17.3 item 3), NOT a recomputed number and NOT $0.**

### 17.2.1 Original pre-build gate (superseded by the measurement above, kept for the record)

The screenshot proves GISD operates its own collection portal. It does **not** prove that ACT's
`total_amount_due` for a Garland parcel *excludes* GISD. That is the load-bearing question and it is
unverified. **Pre-build gate (§6.1 discipline — confirm the source before designing around it):**
take a Garland case with a resolved 17-char DCAD account, pull the ACT balance and the GISD-portal
balance for the same parcel, and compare.
- If GISD is **already inside** the ACT total → no gap; close this section and record the finding.
- If GISD is **additive** → ACT is not fleet-complete, the `VERIFIED` label is wrong for those cases,
  and §17.3 is authorized.

**Exposure if it is additive (measured against the local 334-case DB, 2026-08-15):**
**43 cases carry a Garland address; 22 of those already carry an ACT balance that IS their payoff
today, labeled verified.** The broader multi-jurisdiction surface is **127 non-Dallas-city addresses**
(Garland, Mesquite, Rowlett, Carrollton, Irving, Lancaster, Duncanville, Wilmer…). This is not one
odd case — it is roughly a third of the book.

### 17.3 Proposed shape (approved for DESIGN only when §17.2 measures additive)

1. **Payoff schema represents per-jurisdiction LINES**, each carrying its **own independent**
   `verified | estimated | unavailable` label — not one label over a blended sum. The case-level tax
   payoff becomes the sum of its lines, and it is only `verified` when **every** line is verified.
2. **GISD portal as a LABELED ISD-line source** — a named source like ACT and DCAD, never an
   unattributed number folded into the total.
3. **On any multi-jurisdiction county, an absent ISD balance is `unavailable` — NEVER assumed $0.**
   This is the project's standing rule (a field may not silently become `0` and display as a real
   value) applied to the payoff. Consequence to design deliberately: an `unavailable` line makes the
   *total* payoff incomplete, which should push closability toward INDETERMINATE the same way an
   unquantified lien does (§5.3) rather than produce a confident, low, wrong payoff.

**Carry into that design:** the GISD portal is session-based with no API and no stable per-parcel URL,
so an ISD line is a scraping increment with its own cost — sequence it as its own phase, and do not
let the absence of a scraper turn into an assumed $0 in the meantime (that is exactly what item 3
prevents).

**Sequencing:** below the `heir_estate_title` block; after the Stage-2 gate work. No code until
§17.2 is measured and this section is approved.

## 18. THE THIRD TITLE STATE — fatal `heir_no_conveyance_path` (built 2026-08-15)

Approved as the revised Stage-1 exit criterion: **the owner-mismatch branch blocks; estate/absentee
stays graduated.** `heir_estate_title` was NOT promoted to fatal — that would have reversed the
2026-07-21 graduation. Instead the gate now has **three** states.

### 18.1 The three states

| Condition (all read the COUNTY RECORD, never petition language) | Gate | Severity | Verdict effect |
|---|---|---|---|
| No party to the suit is, or is related to, any owner of record | `heir_no_conveyance_path` | **fatal** | **NO-GO** |
| Owner of record ≠ defendant, but a related party is on title or in the suit | `heir_estate_title` | substantive | GO-WITH-CONDITIONS |
| Estate/absentee signal with no confirmed mismatch | `estate_absentee_signal` | generic | does not lift HOLD |

The fatal branch is the only one that can kill a deal, and it fires on a single verifiable question:
**is there anybody we can actually sign a purchase contract with?** Pre-foreclosure acquisition is a
contract with the owner. If no party to the suit holds record title and none is related to whoever
does, there is no seller — and no amount of valuation work changes that.

### 18.2 BOTH SIDES OF THE COMPARISON MUST BE COMPLETE (this is the whole difficulty)

The naive predicate — `owner_of_record` vs `defendant` — is wrong on **both** sides, and each error
was live in the code:

- **Owners.** `_case_input` passed only `owners[0]`. TX-23-00553's owners are BACA NORMA ESTELA (50%)
  and **HERNANDEZ NORMA (50%)** — the co-owner who shares the defendant's family name, i.e. the
  conveyance path itself, was being discarded before the engine saw it.
- **Defendants.** `CaseInput.defendant` is the **lead defendant only**, but tax suits name 2–21
  parties and the record owner is very often one of them. **Measured on the live 334-case book:
  matching the lead alone produced 38 fatal verdicts, of which 13 were FALSE** — the record owner or a
  relative was a non-lead defendant. The false set was concentrated in exactly the heir cases this
  pipeline exists to work.

Both sides are now complete: `CaseInput.owners` (full DCAD list) and `CaseInput.all_defendants` (full
roster, parsed from the `cases.all_defendants` JSON column by `_defendant_names()`).

### 18.3 Fixture correction — Ruby is the COUNTER-fixture, not the blocking one

TX-26-01379 was nominated as the blocking fixture on the reading "DCAD owner TAYLOR FELICIA D ≠
Brown". That is true of the *lead* defendant and false of the case: **the suit names three defendants
and the second is "Felicia Denise Taylor"** — the record owner is already a party. DCAD confirms she
bought it in a 2020 arm's-length sale (BROWN RUBY FAYE → TAYLOR FELICIA D, deed 6/22/2020), so title
departed, but **the buyer is joined to the suit**. That is a *condition*, not a dead end, and it is
precisely the "identified counterpart to negotiate/quiet-title through" the 2026-07-21 graduation
described. **Ruby's golden GO-WITH-CONDITIONS verdict stands unchanged** and is now pinned against the
fatal branch as a must-not-escalate case.

**The blocking fixture is TX-26-01196**: sole DCAD owner ANDERSON BETTY, sole defendant Gayla
Jefferson. Nobody in that suit can convey.

**Standing lesson (third time this project has hit the shape):** the earlier version of this rule read
one element of a list and treated it as the whole — the same root cause as the comma-joined DCAD
account and the `owners[0]` ownership parse. Before comparing two parties, confirm both sides are the
FULL set.

### 18.4 Guardrails

- **Fail-soft by construction.** `no_conveyance_path()` returns False whenever the fact cannot be
  verified — no owner list (**11% of the book**), or no defendant. Missing data must never manufacture
  a NO-GO; absence of evidence is not evidence of departed title.
- **Never reads petition language.** Estate/heir/absentee wording cannot reach this branch; pinned by
  a test that an estate+absentee case with no owner record does not block.
- **Fleet impact measured, not assumed:** **25/334 (7.5%) fatal**, 28/334 (8.4%) substantive.
- **Other findings still report** — the fatal title gate does not suppress the lien or valuation gates.

### 18.5 Tests

`test_acquisition.py` **146/146** (predicate incl. both hiding-places, the Williams/Motley 10-defendant
case and an explicit pin that it *would* have blocked on the lead alone, fail-soft set, three-state
separation, Ruby held at GO-WITH-CONDITIONS, 00553 held substantive) · `backend/test_acquisition_api.py`
**52/52** (fatal branch end-to-end on TX-26-01196; Ruby and 00553 pinned non-fatal through the real
endpoint, which is what guards the backend against reverting to `owners[0]` / lead-defendant).
Regressions green: `test_comps` 111/111, `test_skeleton_equivalence` 28/28, `test_balance_card` 47/47.
