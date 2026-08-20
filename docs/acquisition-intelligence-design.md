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

### 17.4 CONFIRMED LIVE — a Garland case where ACT reads **$0.00** while the debt is real (2026-08-15)

**TX-26-01455 · HERLINDA M. GELISTA · 5221 Robin Road, Garland 75043 · CAD `26380500080060000`**
(resolved from ACT's address search; the case row itself is unenriched — `account_status=needs_lookup`,
no account, no balance).

| Source | Reads |
|---|---|
| **ACT** (dallasact.com) | **Total Amount Due $0.00** · jurisdiction detail: **"No taxes due."** · MV $292,850 |
| **GISD portal** (account `0000103464`) | **`Lawsuit: Yes`** · 2025 Amount Due **$4,896.59** = tax $3,428.98 + P&I $651.51 + **attorney fees $816.10** · Total Paid $0.00 |
| The suit itself | `total_due_filing` **$11,306.35** |

This is the predicted case in its strongest form: for this parcel ACT is not *partially* short, it is
**entirely empty of the debt**. A live tax foreclosure with attorney fees already accruing reads as
zero at the source the payoff model calls VERIFIED.

**Note the petition does not reconcile to GISD either** ($11,306.35 vs $4,896.59, a ~$6,410 gap).
Neither source accounts for it. City of Garland appears in NEITHER ACT (no city line on Garland
parcels) NOR the GISD portal (which shows only the GARLAND ISD block) — so a **THIRD collector** is
the leading hypothesis. **Unverified — do not assume it.** It is the next measurement.

### 17.5 THE LIVE DEFECT THIS EXPOSES — zero-balance cases are classified **PAID** and leave the queue

The payoff line is not the worst of it. `frontend/index.html balanceBand()` maps `b <= 0` → `"zero"`,
commented *"paid — represented by the disposition flag, not a $"*. That inference is sound where ACT
collects everything (Dallas) and **false wherever an ISD collects separately**.

**Measured on the local 334-case book: 88 cases carry `current_tax_balance == 0` and are therefore
classified PAID. 15 of them are Garland cases — every one with a real petition amount ($5,917–$11,679)
and `account_status = resolved`, i.e. enrichment ran and ACT genuinely returned $0.**

Examples: TX-26-00991 ($11,679 filed), TX-25-00387 ($11,096), TX-26-00992 ($10,966), TX-24-00090
($9,234).

**Honest state of proof: the mechanism is CONFIRMED on one case (TX-26-01455); the other 15 are
SUSPECTED on the same pattern and are not individually verified.** The 73 non-Garland zeros are a
separate question — in Dallas, ACT $0 probably does mean paid, which is exactly why the fix must be
jurisdiction-aware and not a blanket change.

**Why this outranks the schema work in urgency:** a payoff that is understated still shows the case to
a rep, who can catch it. A case classified `zero` **silently drops out of amount-owed triage** — the
rep never sees it. That is the project's own standing rule violated at the display layer: *no field may
silently become 0 and be displayed as if that were a real value.* The correct state for a Garland
parcel with ACT $0 and no ISD reading is **`unknown`, not `zero`** — §17.3 item 3 applied to the band
classifier rather than only to the payoff line.

**NOT CHANGED — sequencing is the user's call.** `balanceBand` is a deployed served artifact and this
would alter live triage for 88 cases. Recommended as the first increment of §17 (ahead of the payoff
schema), but not started.

### 17.6 BUILT 2026-08-15 — the contradiction rule (frontend `balanceBand`)

**The discriminator is the CONTRADICTION, not the address.** Geography does not imply the ISD — parts
of Garland sit in Richardson and Dallas ISD — and a Dallas case with the same contradiction is equally
suspect. The rule tests posture:

```
a suit was filed for real money  +  the collector we read shows $0  +  collection has not stopped
        (total_due_filing > 0)          (live balance <= 0)            (case_track != dismissed_paid)
                                    ⇒ band = "unknown", never "zero"
```

`zeroIsContradicted()` in `frontend/index.html`; `balanceBand` returns `"unknown"` instead of `"zero"`
when it holds. The filter option is relabelled *"Paid ($0, dismissed)"* / *"Unconfirmed — needs check"*
so the UI stops asserting that a bare $0 means paid.

**Measured effect on the local 334-case book — NO blanket zero→unknown:**

| | count | Garland | other |
|---|---|---|---|
| was PAID → now **UNCONFIRMED** | **50** | 9 | 41 |
| stays **PAID** | 38 | 6 | 32 |

The 50 flipped are `active` (35), `judged_pending` (12), `oos_timing` (3) — cases still being
prosecuted, where a $0 collector balance contradicts the docket. 41 are non-Garland, which is the rule
working as specified: **a Dallas case with the same contradiction is equally suspect.** The 38 kept are
37 `dismissed_paid` plus one with no suit amount (no contradiction evidence to act on).

**WHY NOT A LEVY-RATIO TEST.** ACT's collected levy as a share of market value separates cleanly on the
live book (Garland median 0.518%, max 0.727%; elsewhere median 2.079%) because ACT bills only the
county-side units on a split parcel. It was rejected anyway: it is an *inference* driving triage
classification, exemptions confound the denominator, and the boundary is fuzzy. Same standard as §5.4 —
a derived number is not evidence.

**⚠ KNOWN RESIDUAL, recorded not papered over.** `case_track == "dismissed_paid"` is itself derived
from `tax_balance == 0` (`backend/main.py case_track_of`), so on a split-collector parcel it inherits
the same blind spot. **6 Garland cases sit in that state and remain classified paid.** The same
derivation also feeds the disposition auto-flag (`main.py` ~1369 proposes an ARCHIVE for
`dismissed_paid`), so the blind spot has a second, more permanent stage — proposal only, a human still
commits, but the premise is unverified. **Neither is touched here.**

**What actually resolves the residual — capture, not inference:** ACT's per-parcel **jurisdiction
coverage** as a stored fact ("which taxing units does ACT bill for this account?"). Note the obvious
source does NOT work: `taxbyyearbyunit.jsp` renders *"No taxes due."* with no unit list precisely when
the balance is $0 — empty exactly when needed. The Summary/Current Tax Statement carries the per-unit
levy but is served as a **PDF**, so this is a real capture increment, not a one-line scrape. Its own
design.

**Tests:** `test_zero_balance_band.py` **18/18** — runs the real `balanceBand`/`zeroIsContradicted` out
of the served artifact in Chromium. Pins the proven case, a Dallas case flagging on identical facts
(not geography-gated), a Garland case NOT flagging because its docket dismissed it (not address-gated
either), every other band unchanged, and zero pageerror.

## 19. STANDING RULE — the recurring meta-defect: a local truth applied fleet-wide

Four instances now, one shape. Each was a fact that is true **in a subset** — one account, one owner,
one defendant, one county's collection arrangement — encoded as if it held for **every** case:

| # | Instance | The local truth | Applied fleet-wide as |
|---|---|---|---|
| 1 | Comma-joined DCAD account | one parcel = one account | every account string is one ID |
| 2 | `owners[0]` ownership parse | one owner per parcel | the first owner is *the* owner |
| 3 | Lead defendant (§18.2) | one defendant per suit | `defendant` is *the* defendant |
| 4 | `zero → PAID` (§17.5) | ACT bills every unit *in Dallas* | ACT $0 means paid *everywhere* |

**A fifth was caught while writing §18's own counter-check** and is the reason this section exists:
`owner_defendant_mismatch` still compared `owners[0]` against the lead defendant, so TX-26-01455 —
where *both* record owners are *both* defendants — raised a false substantive title flag on a clean
case. The gate immediately below it had just been fixed for the identical reason. **The shape survives
a fix to its neighbour.**

**THE RULE.** Before a fact becomes a classification:
1. **Is this a set?** Accounts, owners, defendants, jurisdictions, tracts are all plural. If the code
   reads `[0]` or a scalar, the answer is already no.
2. **Whose truth is it?** A fact verified in one county/city/parcel class is scoped to it until
   measured elsewhere. Dallas ≠ Dallas County.
3. **Measure the blast radius before shipping.** Every instance above was quantified on the real book
   first (13 false fatals of 38; 50 of 88 zeros; 28→56 substantive) — the count is what exposes an
   over-fire, never the reasoning.
4. **Absence is `unknown`, never a value.** `0`, `""`, `owners[0]`, "the defendant" are all silent
   substitutes for a set you did not fully read.

Rule 1 alone would have caught instances 1, 2, 3 and 5. Rule 2 would have caught 4.

### 18.6 THIRD REFINEMENT — `_same_party()` (spot-check FAILED first, 2026-08-15)

Sign-off was conditioned on spot-checking the newly-substantive cases. **The first spot-check failed.**
Token-containment matching produced false title flags on cases that were **the same party written two
ways** — not owner-adjacent situations at all:

| DCAD owner | Petition defendant | Artifact |
|---|---|---|
| TREVINO JUAN F & SANJUANA | Juan Francisco Trevino | middle name |
| MOLINA FLORES ANA GABRIELA | Anna Gabriela Molina Flores | ANA / Anna |
| BECERRA MARIA DELOURDES SALCEDO | Maria De Lourdes Salcedo Becerra | DELOURDES / De Lourdes |
| DABNEYCLARK TERESA | TERESA DABNEY-CLARK | hyphen concatenated |
| GALLEGOSLARA MARIA LEONOR | Maria Leonor Gallegos-Lara | hyphen concatenated |
| KIRKWALLS PATRICIA A | Patricia Kirk Walls a/k/a … | concatenation + a/k/a |
| LALANI ASSET MANGEMENT LLC | LALANI ASSET MANAGEMENT LLC | DCAD typo |
| MONROY VELMA AND ERASTO | Velma Jean Cantu Monroy | combined couple record |
| LE KEVIN · WU QINGNONG | KEVIN LE · QINGNONG WU | two-letter surname dropped as a token |
| IKAZ S A | THE UNKNOWN SHAREHOLDERS … OF IKAZ, S.A. | single-part entity name |
| ORTEGA IDALIA | *(scalar column says "GARCIA, V, IDALIA")* | lead's a/k/a lives in the roster |

`_same_party()` replaces containment: **two significant name parts in common = the same party**,
matched as whole tokens or as substrings of the other side's separator-stripped form. One part is not
enough — a shared surname is a FAMILY relationship, which is the question `no_conveyance_path` asks,
not this one. Supporting fixes: the token floor dropped 3→2 characters (real surnames here are two
letters) with the noise set widened to absorb honorifics/suffixes/entity boilerplate; a single-part
name matches on that one part alone; and the lead defendant is expanded to its fuller roster form so
an a/k/a on title is seen. **A scoring bug was caught in the same pass** — counting each direction
separately double-counted one shared token and briefly made HERNANDEZ NORMA "the same party" as
Pauline Hernandez, collapsing exactly the family/identity distinction the function exists to keep.

**Result: substantive 56 → 36 (16.8% → 10.8%), newly-substantive 36 → 19.** Note it now lands *below*
the graduated gate's original 15% — that 15% was itself inflated by these artifacts.

**Second spot-check: 18 of 19 genuine.** Two real shapes — (a) the Ruby shape, where the lead defendant
is not the owner but the owner is a co-defendant (TX-26-01371 Quinlan, TX-25-00499 Waheed, TX-26-01398
Lalani, TX-26-01482 Hodges) — the advisory "confirm who conveys" is correct; and (b) genuine
estate/heir title questions (TX-23-00569 WILLIAMS CHESTER F EST OF, TX-25-02268 JOHNSON DORIS EST OF,
TX-25-02238 BAKER LILA LIFE ESTATE, TX-26-00034 where only Thomas Sloan is sued but Victoria Sloan and
Ivory Harris hold title).

**⚠ ONE RESIDUAL, NOT FIXED — TX-26-00875.** DCAD `PATTERSON BEN C & DIANE` vs lead
`Bennie Charles Patterson`: a **nickname**, not a formatting artifact. BEN is three characters and
substring matching requires four. **Deliberately not chased:** lowering to three characters would match
ANN inside SUSANNA and merge genuinely different parties, converting a visible false flag into an
invisible missed title question. The error direction matters — this gate is ADVISORY
(GO-WITH-CONDITIONS, "confirm who can convey"), so a false positive costs one verification click while
a false negative hides a real question. **Flagged for the sign-off decision; not resolved unilaterally.**

## 20. §19 IS ENFORCED, NOT DOCUMENTED — `test_set_invariance.py` (Stage-1 exit criterion)

Prose did not stop instance five; it appeared in the function *beside* the one just fixed. The four
questions are therefore assertions, and the Stage-1 exit criterion is **this file passing** — not the
five fixes, which are only its first five applications.

| Q | Executable form |
|---|---|
| **Q1 is this a set?** | **POSITION INVARIANCE** — the truth is walked through every slot of the owner list and the defendant roster and the verdict may not change; a 3×3 case is swept through all **36 orderings** and must yield ONE verdict; index 0 is made actively misleading to prove index 1 is read (and removing it must flip the verdict, so the test has teeth); plus an **AST** guard that no plural input is indexed at `[0]` — parsed, not grepped, because the docstrings deliberately discuss `owners[0]` and a text scan fires on the documentation written to prevent it. |
| **Q2 whose truth is it?** | classifiers may not hardcode a locality — asserted against the source of `zeroIsContradicted()` and `no_conveyance_path()`. |
| **Q3 blast radius measured?** | gate rates must sit inside a **declared** envelope on the real book (fatal 4–12%, substantive 6–18%; currently 7.5% / 10.8%, n=334). A change that moves a rate outside must be re-measured and re-signed-off — which is the point. |
| **Q4 absence is `unknown`** | six unverifiable-input shapes must never produce a fatal verdict; no facts → HOLD never GO; an empty tax payoff is `UNAVAILABLE`, never `$0`. |

**26/26.** Position invariance is the general form: every one of the five instances fails it, so a
sixth of the same shape is caught by a test nobody has to remember to write.

## 21. GISD BALANCE AUTOMATION — STEP-1 FEASIBILITY (2026-08-15). VERDICT: **BLOCKED-ON-DATA for Garland; the increment stops here.**

Step 1 asked: can a parcel's ISD be resolved authoritatively, and from what field? Measured, not
reasoned about.

### 21.1 What DOES authoritatively name a parcel's ISD

**ACT's per-parcel unit list (`reports/taxbyyearbyunit.jsp?can=…`) names the ISD by name whenever ACT
collects it.** Sampled across every city code on the real prod book (one nonzero-balance parcel each):

| ACT names an ISD | ACT names NO ISD |
|---|---|
| DA Dallas · CA Addison · CJ Seagoville · CT Wilmer · CU Hutchins → **DALLAS ISD** | **CG Garland (n=18)** |
| CM Mesquite → **MESQUITE ISD** | CI Irving (n=5) |
| CH Cedar Hill → **CEDAR HILL ISD** | CR Richardson · CF Farmers Branch |
| CP Grand Prairie → **GRAND PRAIRIE ISD** | CC Carrollton · CW Rowlett *(equalization only)* |

This is a real, per-parcel, already-fetchable authority — and it covers the large majority of the book
(DA alone is 118 of the ~180 sampled).

### 21.2 Why that does NOT unblock the GISD increment — three independent failures

1. **ACT is silent for exactly the self-collecting districts.** The right-hand column above IS the set
   of districts that collect their own taxes — Garland, Irving, Carrollton-Farmers Branch, Richardson.
   ACT cannot name what it does not bill, and **Garland is the case this increment exists for.**
2. **The source is empty precisely when the balance is $0.** `taxbyyearbyunit.jsp` renders
   *"No taxes due."* with no unit list at all (2 of the sampled city codes returned an empty list for
   this reason) — so it is unavailable for the §17.4 "Unconfirmed" population that motivated the work.
   The one place we most need jurisdiction identity is the one place this field is blank.
3. **The GISD portal is NOT usable as an automated membership oracle today.** A district's own tax roll
   *would* be authoritative membership, but a headless HTTP replay returned NO MATCH for two CADs that
   were manually confirmed to be in GISD (`26380500080060000` acct 0000103464, `26238500070260000`
   acct 0000056331). **That means my harness is wrong, not that the parcels are absent** — recorded as
   *not established*, never as a negative result. The portal is session + widget driven.

### 21.3 The finding that settles the §19 question — city ≠ ISD in BOTH directions

**CW / Rowlett shows no ISD line**, and Rowlett is largely Garland ISD territory. So GISD's footprint
extends *beyond* Garland city, while Garland city contains parcels in Richardson and Dallas ISD
(TX-26-00774 is the live example). A city→ISD mapping is wrong in both directions. **Confirmed ISD
identity is the set membership; the address is not a proxy for it** — exactly the §19 class.

### 21.4 VERDICT AND RECOMMENDATION

**For Garland ISD specifically: STOP. Keep it manual.** No field we can reach today names the ISD for a
self-collecting district, and a clean manual flag (current §17.4 behaviour) beats an automated fetch
against the wrong district. Steps 2 and 3 do not begin.

**But two spin-offs are NOT blocked and are worth their own increments:**

**(a) Capture ACT's per-parcel unit list as a stored fact.** It yields two things: the ISD *named* for
the majority, and — crucially — the **negative signal** "ACT bills no ISD on this parcel ⇒ an external
collector exists." **That negative signal alone is enough to make §17.3's schema correct.** Marking an
ISD line `unavailable` (→ INDETERMINATE, §5.3) requires only knowing that a collector exists, NOT which
one. **Identifying the district is needed only to FETCH a number.** That decoupling means step 2 can
proceed on the negative signal while step 3 stays blocked — and it is the honest-labelling outcome
§17 wanted, without any new portal integration. Caveat carried: the unit list is blank at $0 balance,
so absence of the signal is itself `unknown`, never "no ISD".

**(b) Fix `property_intel` line ~477 — a SIXTH §19 instance, found while investigating.** The
jurisdiction parser is
`re.findall(r'(DALLAS[A-Z\s]*|PARKLAND[A-Z\s]*|UNASSIGNED)\s+\$([\d,\.]+)', text)` — it can only
recognise units named `DALLAS…` or `PARKLAND…`. **A `GARLAND ISD` line is structurally invisible to
it**, which is why stored `tax_rates` is malformed or empty and could never have answered this
question. A Dallas-local truth hardcoded as a fleet-wide rule, in the exact shape §19 names. Note this
would NOT have been caught by `test_set_invariance` Q1 (it is not a `[0]` read) but IS caught by Q2
(*whose truth is it?* — a locality baked into a classifier). Worth a Q2 source-guard extension.

**UNTESTED — the one remaining candidate.** DCAD's own per-parcel page may name taxing units; it could
not be loaded this session (direct GET returns "Details for the account you requested could not be
shown" — it needs the Playwright session `property_intel` already uses). **Defined test before any
further work here:** drive `AcctDetailRes.aspx` through the existing Playwright path for
`26380500080060000` (Garland) and `26545500120260000` (the Garland-address parcel with no GISD match)
and check whether either names its school district. If DCAD names it, step 1 is unblocked and this
verdict is revisited; if not, Garland stays manual permanently.

## 22. GISD CAPTURE — THE TWO PRE-BUILD ANSWERS (2026-08-16). §21's verdict is SUPERSEDED.

§21 concluded "blocked-on-data" because it hunted for a *downstream* ISD oracle. The petition is the
oracle, and we already scrape it at step one. That collapses step 1 and both pre-build questions are
now answered against live pages (not the screenshots — portals change markup).

### 22.1 ANSWER — which address does the portal tolerate? **Don't use an address. Use the CAD number.**

Both offices expose a **Search by CAD Number** mode, and the CAD number is the exact 17-char parcel key
we ALREADY store as `account_number` from ACT/DCAD. Confirmed live on three parcels across two
agencies (`26380500080060000`, `26238500070260000`, `26341500100280000`). **The address-tolerance risk
is designed out, not mitigated** — build no address normalization, and the LYNNACRE/"Lynna Cre" class
of defect cannot reach this fetch. Address search stays a documented fallback only.

Residual, handled by the hard rule: `26545500120260000` (TX-26-00774, a Garland *address*) returns 0
GISD matches. Under petition-gated membership we would never query it. A 0-match on a
**petition-confirmed** parcel is `unavailable` → INDETERMINATE, surfaced — never $0.

### 22.2 ANSWER — is the petition the complete collector record? **Authoritative for MEMBERSHIP, incomplete for AMOUNTS.**

Decoded from the live petition (custom font encoding, every glyph shifted by 25):

> "Now come the taxing districts set out below: **CITY OF GARLAND and GARLAND INDEPENDENT SCHOOL
> DISTRICT** on behalf of themselves and all taxing districts for Whom they collect."

- **Membership: authoritative.** GISD is a named plaintiff ⇒ the parcel is definitionally in GISD. No
  proxy, no inference. It also names **TWO** collectors, not one — the petition supplies the full
  *plaintiff-side* collector list, which is exactly the agency set to query.
- **Amounts: NOT complete.** The four ACT-billed county units (Dallas County, Dallas College, Parkland,
  School Equalization) are **not plaintiffs here**, yet ACT shows **$5,974.81** owed on this parcel.
  So petition ∪ ACT is required and **neither source alone is complete**. The §17.3 negative-signal
  schema fix must still run — it is what catches a collector no source enumerated.

### 22.3 THE MEASUREMENT THAT SETTLES THE REQUIREMENT — 3909 Cambridge Dr (the deal PC Peak closed)

CAD `26341500100280000`, owner MELKA GEORGE F, `Lawsuit: Yes` on both rolls:

| collector | agency | account | balance due |
|---|---|---|---|
| ACT — Dallas county-side | — | `26341500100280000` | $5,974.81 |
| **Garland ISD** | **057909** | `0000089040` | **$12,108.43** |
| **City of Garland** | **057120** | `0000110637` | **$7,666.63** |
| **TRUE TOTAL** | | | **$25,749.87** |

**The ACT-only payoff is 23.2% of the real number — it understates by $19,775 (77%).** On the very
property the firm already closed. Rates confirm the shape: GISD $1.170900 + City $0.689746 dwarf the
county-side levy. This is the requirement restated as evidence: in the target market the instrument is
not slightly off, it is wrong by 4×.

### 22.4 THE BONUS FINDING — this is NOT a Garland special case; it is one parameterized integration

texaspayments.com publishes its agency roster as JSON on the home page. The Dallas County (`057…`)
self-collecting offices are:

| office | agency |
|---|---|
| City of Garland Tax Office | `057120` |
| Garland ISD Tax Office | `057909` |
| Carrollton-Farmers Branch ISD Tax Office | `057903` |
| Richardson ISD Tax Office | `057916` |

**That set maps EXACTLY onto the cities where §21 measured "ACT names no ISD"** (CG Garland, CC
Carrollton, CF Farmers Branch, CR Richardson). Two independent observations converging on the same
set is strong corroboration of the model: *ACT silence ⇔ a self-collecting office exists*. Step 4 is
therefore **ONE fetcher parameterized by agency code**, not a Garland one-off — and the petition's
plaintiff list is what selects the agencies to query. (Irving is NOT on this platform: CI shows no ISD
in ACT, so Irving ISD self-collects somewhere else. Unmapped — treat as `unavailable`, never $0.)

### 22.5 REVISED PLAN

1. **Petition → membership + agency set.** Parse the "Now come the taxing districts set out below: …"
   clause into a collector list; map named collectors to GDS agency codes. Ships with the schema fix.
2. **§17.3 negative-signal schema.** Per-jurisdiction lines, independent labels; ACT-silence ⇒ an
   external collector exists ⇒ line `unavailable` ⇒ INDETERMINATE. Correct even with zero portal
   integration, and it is what covers collectors no petition named.
3. **DCAD** — parcel facts only. No longer the jurisdiction oracle. **The `property_intel.py:477`
   `DALLAS|PARKLAND` regex fix is independent and still owed** (sixth §19 instance).
4. **GDS fetch, keyed on CAD number, agency-parameterized, confirmed-membership only.** Fail-soft: a
   timeout degrades that line to `unavailable` → INDETERMINATE and must never block or zero the
   ACT/DCAD/petition enrichment already landed.

**Unchanged hard rules:** membership before balance; absence is `unknown`, never a value; own
fingerprinted FF gate per served-artifact change, never batched; blast radius measured on the real
book before shipping.

## 23. BUILT 2026-08-17 — per-collector payoff schema + petition membership (gate 1 of the GISD arc)

**The finding that collapsed the increment: membership was already in the database.** §22 planned a
petition parser; none was needed. `cases.tax_breakdown` — the petition's Exhibit-A per-entity rows,
already captured by the existing extraction schema — **is populated on 321 of 334 cases (96%)** and
names the plaintiff taxing districts directly. 75 distinct entities, including GARLAND ISD (49 cases),
CITY OF GARLAND (39), RICHARDSON ISD (23), IRVING ISD (8), CARROLLTON-FARMERS BRANCH ISD (7).

*(PDF parsing was attempted first and abandoned on evidence: petitions carry multiple font encodings
per document, varying by law firm — a shift-25 decode read the Perdue Brandon petition and 0 of 12
sampled others. The stored breakdown is both more robust and already there.)*

### 23.1 The coverage backlog — measured, not hunted

**108 of 334 cases (32.3%) name at least one collector outside ACT.** Not a Garland edge case — a third
of the book.

| collector | cases | platform | agency | adapter |
|---|---|---|---|---|
| GARLAND ISD | 49 | gds | 057909 | no |
| CITY OF GARLAND | 39 | gds | 057120 | no |
| RICHARDSON ISD | 23 | gds | 057916 | no |
| IRVING ISD | 8 | irving_act | — | no |
| CARROLLTON-FARMERS BRANCH ISD | 7 | gds | 057903 | no |
| 23 further units (PIDs, utility liens, small cities, transferred tax liens) | 1–5 each | unmapped | — | no |

That last row is the point of the exercise: coverage is now a **measured set with a named tail**,
not an open-ended hunt. An unmapped unit is `scope='unknown'` and is **never assumed to be inside
ACT** — the fail-safe direction.

### 23.2 What shipped

`jurisdictions.py` (new) — canonical unit names (petitions suffix "- TRACT 1 (2022)"; the same
collector must not fragment), the petition membership oracle, a collector→platform→agency registry,
and per-collector lines with independent labels. **Agency ids are DATA** (`data/gds_agency_roster.json`,
captured from the platform's own roster) — an AST test asserts no 6-digit id is a literal in the
module, the same discipline the `DALLAS|PARKLAND` regex broke. `ADAPTERS` is **empty by design**: the
schema is correct with zero adapters because every external line reads `unavailable`, which is true.

`acquisition.py` — `CaseInput.tax_breakdown / act_units / collector_balances`; `tax_payoff_lines()`;
gate `collector_balance_unavailable`; and an incomplete payoff forces `closable=None`
(`indeterminate_payoff_incomplete`). The scalar `tax_payoff` is unchanged, so no existing consumer
shifts underneath.

### 23.3 Blast radius — and the severity the measurement corrected

Modelled first as **`substantive`**, the gate lifted **95 of 334 cases (28.4%) HOLD → GO-WITH-CONDITIONS**
— a case reading *more* positive because we had just discovered its payoff was **incomplete**.
Backwards. Severity is therefore **`generic`**:

| | |
|---|---|
| verdict changes across the book | **0** |
| `collector_balance_unavailable` fires | **108 (32.3%)** |
| closability moved confident → **INDETERMINATE** | **108** |

Those 108 previously returned a confident seller-net from a payoff missing a named collector. Fail-loud
is delivered by the mechanism that governs money (closability), not by a verdict label that would have
flattered the case.

### 23.4 Pins

`test_jurisdictions.py` **46/46** — canonicalisation incl. tract suffixes; membership from the
petition; multi-tract rows summed not fragmented; unknown unit never assumed ACT; roster-driven
agencies with the AST literal guard; `unavailable` carries no amount; completeness UNKNOWN when ACT's
own unit coverage was never captured; the 3909 Cambridge reconstruction ($25,750 with both collectors,
ACT alone 23%); gate is generic, verdict unchanged, closability INDETERMINATE, and an all-ACT Dallas
parcel raises no false alarm. Regressions green: `test_acquisition` 148/148,
`backend/test_acquisition_api` 52/52, `test_set_invariance` 26/26, `test_zero_balance_band` 18/18,
`test_comps` 111/111, `test_balance_card` 17/17.

### 23.5 Next

Adapters, behind the interface: **gds** (one fetcher, agency-parameterised, CAD-number keyed — covers
Garland ISD + City of Garland + Richardson ISD + C-FB ISD, 118 of the 126 external collector rows) then
**irving_act**. Irving is one increment behind the interface, not a special case. Standalone and
independent: the `property_intel.py:477` `DALLAS|PARKLAND` regex (sixth §19 instance) plus the §19 Q2
guard extension for hardcoded jurisdiction names in a parser.

## 24. BUILT 2026-08-17 — DCAD taxing-unit parse (sixth §19 instance) + the §19 Q2 extension

Standalone gate, independent of the adapters. Ticketed as "the `DALLAS|PARKLAND` regex fix"; the
investigation found the regex was the **symptom of a deeper defect**.

### 24.1 The defect had two layers

```python
re.findall(r'(DALLAS[A-Z\s]*|PARKLAND[A-Z\s]*|UNASSIGNED)\s+\$([\d,\.]+)', text)
```

- **Symptom (the ticket):** the alternation can only recognise units named `DALLAS…` or `PARKLAND…`.
  GARLAND ISD, CITY OF GARLAND, RICHARDSON ISD and every other non-Dallas unit were invisible **by
  construction** — a local truth applied fleet-wide.
- **Actual defect:** DCAD's table is **COLUMN-ORIENTED** — units are columns, each row is one
  attribute of every unit — so a unit's name never sits adjacent to its own amount. `[A-Z\s]*` ran
  across tabs and newlines and swallowed the whole header row into a single "entity". **No list of
  jurisdiction names would have fixed this.** The captured layout:

```
 	City	School	County	College	Hospital	Special District
Taxing Jurisdiction	GARLAND	GARLAND ISD	DALLAS COUNTY	DALLAS COLLEGE	PARKLAND HOSPITAL	UNASSIGNED
Tax Rate per $100	$0.689746	$1.1709	$0.2155	$0.106575	$0.212	N/A
Estimated Taxes	$2,019.92	$3,428.98	$631.09	$312.10	$620.84	N/A
Total Estimated Taxes:	$7,012.94
```

**MEASURED FLEET-WIDE: `tax_rates` was EMPTY on 223 and MALFORMED on 77 of 300 enriched cases —
unusable on 100% of the book.** Not a Garland problem; the field never worked anywhere.

### 24.2 The fix

`property_intel.parse_tax_jurisdictions()` reads the table **by column**, agnostic to which units
appear, how many, and in what order. Verified against **real captured DCAD pages** for a Garland and a
Dallas parcel: 5 units each, names + categories + rates + amounts, and the parsed amounts **reconcile
to the page's own Total to within a cent**. The rates independently corroborate the portals —
`$1.1709` = the GISD portal's rate, `$0.689746` = the City of Garland portal's rate.

Three anchoring bugs were found and fixed *against the live page*, each worth recording because each
would have shipped silently: the words "Estimated Taxes" appear **7×** per page (nav bar, ENS link,
disclaimer prose), `Estimated Taxes\s*\(` still matched "Notice Of Estimated Taxes (ENS*)", and an
unrelated Legal-Desc row with an empty first cell hijacked the category header. The anchor is now
`Estimated Taxes\s*\(\s*\d{4}` — verified as the single match on both pages.

### 24.3 Two upsides beyond the ticket

1. **DCAD is now an INDEPENDENT corroboration source for the petition's collector list** (§23). Pinned:
   every collector the Garland petition named appears in DCAD's unit table, DCAD independently
   identifies the same two external collectors, and a Dallas parcel yields none (no false alarm). It
   also covers the 13 of 334 cases with no petition breakdown. *(This also resolves §21's one untested
   candidate: DCAD **does** name the district — though §23's petition oracle remains primary, being
   authoritative and requiring no scrape.)*
2. `tax_rates` stops being malformed fleet-wide, on every parcel, not just Garland.

### 24.4 §19 Q2 EXTENSION — and the discrimination it needed

Q2 now guards **both** layers, in `test_set_invariance.py`:

- **AST guard:** no parser in `property_intel.py` may match on hardcoded jurisdiction NAMES. It fired
  immediately on a second site — and that one is a **false positive worth encoding**:
  `CITY OF ([A-Z][A-Z ]+?)(?:,|\s+DALLAS|\s+TX|…)` uses "DALLAS" as a *delimiter* inside a
  **non-capturing** group; it constrains nothing about which cities can be recognised. The old defect
  had its names **inside the capture**. The guard therefore strips `(?:…)` groups before scanning —
  names in a capture are the defect, names as delimiters are not. Both directions are pinned.
- **Behavioural guard:** an invented district (`NOVUSVILLE ISD`) must parse like any other, and each
  unit must take **its own column's** amount — a row-reading or name-list implementation fails both.

### 24.5 Pins

`test_dcad_jurisdictions.py` **25/25** (the units the old regex could never see; the old regex
recovering nothing from a column table; column reordering; a never-seen district; a 2-column table;
`N/A` → None while a real `$0.00` stays 0.0; UNASSIGNED is not a unit; fails closed on junk; the
nav-bar anchor trap; petition corroboration). `test_set_invariance.py` **26 → 29**. Regressions green:
`test_dcad_parse` 38/38, `test_jurisdictions` 46/46, `test_acquisition` 148/148,
`backend/test_acquisition_api` 52/52, `test_comps` 111/111, `test_zero_balance_band` 18/18,
`test_balance_card` 17/17.

**Deploy note:** `property_intel.py` is a LOCAL scraping tool — it is **not imported by the web app**,
so no served artifact changes and prod behaviour is unaffected until cases are re-enriched locally.
The fingerprint check below proves that rather than assuming it.

## 25. BUILT 2026-08-17 — the `gds` collector adapter (texaspayments.com)

One fetcher, agency-parameterised, covering the self-collecting Dallas County offices: **Garland ISD,
City of Garland, Richardson ISD, Carrollton-Farmers Branch ISD — 4 of 5 mapped collectors, 118 of 126
external collector rows.** Adding another GDS office is a roster entry, not code.

### 25.1 The confirmed shape, built as specified

- **CAD number is the key.** Both systems share the 17-char parcel id and we already store it, so
  address normalisation is designed OUT rather than solved. Address search is never used.
- **Agency ids come from the roster file**, resolved by name at call time; the AST guard asserts no
  literal appears in the adapter.
- **Membership before balance.** `fetch_for_case` only queries collectors the PETITION named.
- **Fail-soft.** Any failure — timeout, no match, changed markup, unparseable page — returns nothing
  for that collector, which renders `unavailable` → INDETERMINATE. Never $0, never blocks enrichment
  already stored.
- **Local only.** The cloud never scrapes: the adapter runs during local enrichment and writes
  `property_intel.collector_balances`, which the served engine merely reads.

### 25.2 Two findings from building it against the live portal

**1. The balance is the SUM ACROSS YEARS, not the expanded detail block.** The account page expands
one year by default. On 3909 Cambridge that block reads **$4,086.97** while the account actually owes
**$12,108.43** across three delinquent years. Parsing the block would have understated by 3× — the
same shape as the original ACT-only defect, one level down. Pinned by test.

**2. AN IDENTITY GUARD WAS MISSING AND IS NOW ENFORCED.** The first sample stored balances without
confirming the returned page was for the requested parcel. That is precisely the cross-contamination
class that put one parcel's enrichment into another case's row (2.1% of the book, §17.2). The adapter
now compares the returned CAD against the requested CAD and **discards any result it cannot tie to the
parcel**, recording it as rejected rather than storing it.

Live end-to-end on 3909 Cambridge: Garland ISD **$12,108.43** (acct 0000089040) + City of Garland
**$7,666.63** (acct 0000110637) = **$19,775.06**, matching the manual measurement to the cent.

### 25.3 A FETCHED zero is not an ASSUMED zero

TX-26-00991 (413 W Carolyn Dr) filed at $11,679 and was flagged `Unconfirmed` by §17.4. The adapter
returns **$0.00 with `Lawsuit: No`** from Garland ISD — and the identity is confirmed: CAD
`26485500040430000` is 413 W Carolyn Dr per ACT, and GISD's owner "MACIAS JUAN CARLOS ESTRADA" is the
defendant "JUAN CARLOS ESTRADA MACIAS". **The taxes really were paid.** The adapter therefore resolves
`Unconfirmed` in BOTH directions — surfacing real debt, and clearing cases that are genuinely settled.
That distinction is the whole point of the arc: a verified zero is a fact, an assumed zero was the bug.

### 25.4 Pins

`test_collectors_gds.py` **28/28** — sum-across-years vs the expanded block; a fetched $0 rendering
`verified` (and completing the payoff) while an absent balance stays `unavailable`; five fail-closed
shapes; no hardcoded agency id; the identity guard present; membership gating; roster-resolved
agencies; and the registry honestly reporting `gds` reachable while `irving_act` is not.
`test_jurisdictions` updated (the "no adapter exists yet" pin is now "gds reachable, irving_act not").

`collector_backfill.py` is the local runner (`--dry-run` / `--limit` / `--case`).

### 25.5 FLEET ACCEPTANCE RUN (2026-08-17) — 81 eligible cases

| outcome | cases | |
|---|---|---|
| **RESOLVED → REAL DEBT** | **40** | **$385,471.74 newly quantified** |
| **RESOLVED → VERIFIED-PAID ($0)** | **23** | fetched zeros, not assumed ones |
| UNAVAILABLE · no CAD on file | 18 | never queried — an ENRICHMENT gap, not a portal failure |
| UNAVAILABLE · portal miss | **0** | |
| IDENTITY-GUARD DISCARDS | **0** | every stored balance tied to the requested CAD |

**78% of eligible cases now carry a VERIFIED collector balance.** Per collector: GARLAND ISD 42 cases
/ $142,116 · RICHARDSON ISD 15 / $144,716 · CITY OF GARLAND 33 / $83,187 · CARROLLTON-FARMERS BRANCH
ISD 5 / $15,453.

**$385,471.74 of real tax debt that the platform previously could not see at all** — every dollar of it
outside ACT, on cases whose payoff the old model would have computed from the ACT scalar alone.

**A DEFECT THE RUN EXPOSED — TRANSIENT FAILURES WERE BEING ACCEPTED AS FINAL.** The first pass left 4
cases unavailable. All four hit on a straight retry, including **729 Woodcastle — a parcel already
known BY HAND to be on the GISD roll** — and TX-26-00774's Richardson ISD line at **$14,082.76**.
Fail-soft is the correct *final* state, but treating a transient failure as final silently discards
recoverable debt. `fetch_one` now retries once; after the retry the portal-miss count is **0**, and
those four cases contributed a further ~$31,700. Ground truth is what caught it: one of the four was a
parcel whose answer was already known, so "no result" was checkable rather than plausible.

**TX-26-00774 closes its own loop.** The case that first showed "Garland address, zero GISD matches"
resolves to **Richardson ISD + City of Garland = $22,237.40**. City ≠ district, proven end to end: the
petition named Richardson ISD, the adapter fetched Richardson ISD, and the parcel owes $22K nobody
could see.

**The 18 no-CAD cases are the next real gap** — an enrichment failure upstream (no account resolved),
not an adapter limitation. They correctly read `unavailable` → INDETERMINATE. Logged, not fixed here.

**NOT YET SYNCED TO PROD** — this is local data behind the `prod_ready` gate; pushing it is its own
step.

## 26. BUILT 2026-08-17 — verified collector balances enter the PAYOFF TOTAL

**Caught before the data sync shipped.** §25's fleet run quantified $385K of external debt; the sync
was ready. Measuring its blast radius first returned **0 verdict flips and no payoff movement** — and
the zero was the defect, not a clean result. `tax_payoff()` returned only the ACT scalar, every
calculator consumed that, and `known_total` was computed and consumed by nothing. **The fetched
balances were displayed but not counted.** Syncing would have landed $385K into a field the engine
reads for labels and ignores for money — the 4x understatement surviving behind honest-looking lines.

The §23 decision to leave the scalar alone was correct while every external line was `unavailable`
and there was nothing to add. It expired the moment real amounts existed.

### 26.1 The rule

```
ACT live + verified external      both present
verified external alone           ACT $0/absent but a collector was fetched
filing-derived fallback           neither — and NEVER fallback + external
```

**The double-count trap:** `total_due_filing` is the PETITION total, which already includes every
plaintiff collector's filed amount. Adding fetched external balances on top of the fallback estimate
would count them twice — **measured at 9 of 63 backfilled cases**. The fallback branch is now
reachable only when `external` is 0, so the two can never combine. Pinned.

Only `verified` lines are summed; an `unavailable` collector contributes **nothing** and drops the
total's label to `estimated` with the note naming it a **FLOOR** and counting what is missing. A
payoff that cannot be complete says so instead of reading confident and low.

### 26.2 Blast radius, measured on the real book (63 cases carrying fetched balances)

| | |
|---|---|
| payoff changes | **40 cases** · aggregate **$304,936 → $602,380** |
| verdict flips (cold) | **0** |
| closability transitions | **60 × `INDETERMINATE → closable`**, 2 unchanged |
| deals killed on the cold book | **0** |

**The 0 cold verdict flips are correct and expected, not a null result.** A fatal `NO-GO` via
`structural_unclosability` is **confirmed-valuation-gated** (§5.4) and no case in the book has
confirmed comps, so cold triage cannot produce one — that is the 2026-07-21 decision-table fix
behaving. What the data actually does cold is **resolve indeterminacy**: 60 cases move from
INDETERMINATE to a real closability answer because the collector line is no longer unavailable.

### 26.3 The Grant St shape, demonstrated on real numbers

TX-26-00774 (Richardson ISD + City of Garland), confirmed valuation, agreed price $32,000:

| | payoff | seller net | closable | verdict |
|---|---|---|---|---|
| ACT-only (old) | $6,812 | **+$23,526** | INDETERMINATE | GO-WITH-CONDITIONS |
| with both collectors | **$29,050** | **−$2,048** | **False** | **NO-GO** |

The deal dies on debt the platform previously could not see. That is the mechanism the whole arc was
built for, and it is pinned by test.

### 26.4 Pins

`test_payoff_total.py` **21/21** — the 3909 Cambridge ground truth to the cent; incomplete rendering
as a labelled FLOOR; both halves of the double-count guard; no movement on all-ACT parcels; and the
Grant St flip end to end. Regressions green: `test_acquisition` 148/148, `test_jurisdictions` 47/47,
`test_collectors_gds` 28/28, `test_set_invariance` 29/29, `test_dcad_jurisdictions` 25/25,
`test_zero_balance_band` 18/18, `backend/test_acquisition_api` 52/52.

### 26.5 DATA SYNC LANDED (2026-08-17) — 63 cases now priced on real payoffs

Ran after the code gate, never with it. `sync_to_prod.py --update-existing --only <63 cases>`:
created 0 · updated 63 · failed 0 · reconciled.

**Live on prod after the sync:** payoff basis across the 63 — `act_plus_collectors` **31** ·
`collectors_outside_act` **9** · `act_live_balance` 6 · `fallback_estimate` 17. **40 cases now priced
with collector balances, aggregate payoff $602,380** — matching the local projection exactly.

**Spot-checks against the LIVE portal, prod vs source:**

| case | collector | portal | prod | |
|---|---|---|---|---|
| TX-26-00774 | RICHARDSON ISD · CITY OF GARLAND | $14,082.76 · $8,154.64 | identical | ✓ |
| TX-26-01459 | RICHARDSON ISD | $20,089.29 | identical | ✓ |
| TX-24-00098 | GARLAND ISD | $19,639.35 | identical | ✓ |

*(The third check first read MISMATCH because the verification script used a guessed CAD rather than
the case's own — the `cad-match=False` flag caught it immediately. Tester error, not data error, and
the identity guard is exactly what made it visible in one line.)*

**Payoffs now live on prod:** TX-26-00774 **$29,050** (was $6,812) · TX-26-01459 **$33,479** (was
$13,390) · TX-24-00098 **$48,610** (was $28,970).

## 27. SEQUENCING READ (2026-08-17) — and a LIVE REGRESSION §26 INTRODUCED

Asked to weigh irving_act (8 cases) against account resolution (18 cases, all platforms). Measuring
both surfaced a third item that outranks them, and it is one this work caused.

### 27.1 ⚠ §26 RE-OPENED THE TAB-DIVERGENCE BUG THAT `f8188d5` CLOSED IN JULY

The frontend has **two** payoff paths:
- **Acquisition tab** (`index.html:2296`) renders `analysis.tax_payoff` from the API — now correct,
  collector-inclusive.
- **Financials tab** (`index.html:1701`) calls `calcPayoff(ex, _liveBal)`, which computes client-side
  from the **ACT live balance alone** — the pre-§26 model.

Commit `f8188d5` (2026-07-19) existed precisely to make those two agree. **§26 changed the engine and
not the client, so they disagree again — live, right now:**

| case | Financials tab | Acquisition tab | divergence |
|---|---|---|---|
| TX-26-00774 | $6,812 | **$29,050** | $22,238 |
| TX-26-01459 | $13,390 | **$33,479** | $20,089 |
| TX-24-00098 | $28,970 | **$48,610** | $19,640 |

**31 cases disagree; $307,863 of understatement is on screen today.** A rep on the Financials tab sees
the old wrong number for the very cases this arc just fixed. The engine being right does not help if
the interface contradicts it — and a contradiction is worse than the original understatement, because
now the platform states two different payoffs for one parcel.

**This is not "UI surfacing" and should not be scheduled as a feature.** It is a correctness fix for a
regression introduced by §26, and it ranks above any coverage work.

### 27.2 The two coverage options, measured

| option | cases unlocked | notes |
|---|---|---|
| **irving_act adapter** | **8** (all 8 have a CAD → fully fetchable) | one adapter behind the existing interface; high certainty |
| **account resolution** | **18** across 4 collectors (Garland ISD 8 · Richardson ISD 8 · City of Garland 7 · C-FB 2) | every one is `needs_lookup` — the KNOWN-HARD residue |

Account resolution unlocks more cases and helps every platform, but these 18 are the structural
backlog that prior resolution attempts already failed on (Rowlett/Farmers Branch/multi-parcel HOA,
county-spanning parcels), and naive address search has a measured **~2% confidently-wrong-parcel rate**
(2026-07-11 audit) — so each needs `resolve_account_corroborated` plus per-case verification against
ACT's site address. Higher ceiling, much lower certainty per unit of effort, and it is the same work
already queued as the 4-case re-resolution increment.

### 27.3 RECOMMENDED ORDER

1. **Fix the tab divergence + surface the collector lines** — one frontend gate. Retire `calcPayoff`'s
   independent payoff in favour of the API's `tax_payoff`, and render `tax_payoff_lines` so
   TX-26-00774 shows Richardson ISD + City of Garland and its NO-GO. Fixes a live wrong number AND
   delivers the surfacing in the same change, because they are the same code path.
2. **irving_act** — 8 cases, all fetchable, one adapter, high certainty.
3. **Account resolution** — 18 cases, all platforms; fold in the 4-case re-resolution already queued
   and treat it as one enrichment-integrity increment rather than two.

Coverage we already have but display wrongly beats coverage we do not have yet.

## 28. BUILT 2026-08-17 — payoff parity: the client and the engine can no longer disagree

Fixes the §26 regression (§27) AND surfaces the collector lines, because they are the same code path.

### 28.1 What changed

`calcPayoff()` now implements the §26 rule exactly — ACT live + verified external; external alone
when ACT is $0/absent but collectors were fetched; the filing-derived fallback only when neither, and
never fallback + external. The Financials payoff card renders a **per-collector breakdown**: the ACT
county-side line, each fetched collector with a `verified` chip, and any named-but-unretrieved
collector in amber as `unavailable` with a FLOOR warning. `collectorsNamedInSuit()` derives membership
from the petition's own breakdown, mirroring `jurisdictions.py` — never from the address.

**Divergence closed: 63/63 cases now agree; 31 disagreed before.** TX-26-00774 reads **$29,050** on
both tabs, with Richardson ISD $14,082.76 and City of Garland $8,154.64 itemised beneath it.

### 28.2 THE GUARD — `test_payoff_parity.py`, and the three bugs it found immediately

The standing rule the user asked for: **a test that fails if the two implementations ever state
different numbers.** It runs the REAL `calcPayoff` extracted from the served artifact in Chromium and
the REAL `acquisition.tax_payoff()` over a 10-shape case matrix, asserting BOTH amount and label, plus
that the two sides agree on *which* collectors are external. A comment reading "the two must agree"
sat above `calcPayoff` through both previous drifts and stopped nothing; this does.

It failed on first run and found **three genuine defects, two of them pre-existing**:

1. **Month off-by-one (pre-existing).** `new Date("2026-01-01")` parses as UTC midnight, which is the
   *previous month* in US local time, inflating the fallback accrual by a whole month. This is the
   exact defect `fmt()` was fixed for in July — **`calcPayoff` was a missed consumer of that fix**,
   which is the same "did you update every consumer" family, third instance.
2. **Rounding placement (pre-existing).** The client rounded the accrual then added, yielding a
   non-integer payoff ($12,689.20); the engine rounds the total.
3. **A fabricated $0 (pre-existing) and a lost verified $0 (introduced by §26).** With nothing to
   compute from, the client returned `$0 / estimated` where the engine correctly says `unavailable` —
   the assumed-zero bug living in the client the whole time. Conversely §26's engine used truthiness
   on the external sum, so a **complete set of fetched zeros** read as `unavailable` instead of a
   verified $0 — losing exactly the distinction §25 established. Both fixed: retrieval is now tracked
   explicitly (`fetched_any` / `fetchedAny`), and completeness requires ACT's figure to be known too.

**And a fourth, caught while fixing the third:** `null * rate` is `0` and `null + 0` is `0` in JS, so
an unknown payoff silently became a confident **$0.00 total-to-clear, minimum offer and suggested
offer** one line below the fix. Derived money fields are now null when the payoff is unknown, and
`fmtC` renders "—". The assumed-zero bug reappears wherever a null is allowed into arithmetic.

### 28.3 Pins

`test_payoff_parity.py` **22/22** (10 shapes × amount+label, plus external-collector agreement and
zero pageerror). Frontend regressions green: `test_zero_balance_band` 18/18, `test_balance_card`
17/17, `test_selection_stability` 7/7, `test_rep_sidebar` 9/9, `test_land_evidence_browser` 56/56.
Engine regressions green: `test_payoff_total` 21/21, `test_acquisition` 148/148,
`backend/test_acquisition_api` 52/52.

### 28.4 §29 — the assumed-zero bug is STRUCTURAL, and now has a dedicated guard

It appeared **four times inside §28 alone**: the client returning `$0/estimated` where the engine said
`unavailable`; the engine losing a VERIFIED $0 to truthiness on the external sum; `null * rate` → `$0`
attorney fees; and `null + 0` → `$0.00` total-to-clear, minimum offer and suggested offer.

That is not carelessness — **it is structural.** JS coerces `null` to `0` silently, so every derived
money field is a fresh opportunity for the bug, and **spot-checking real cases cannot catch it because
real cases have data.** Only a no-data case exposes it, which is exactly the case nobody clicks.

`test_unknown_payoff.py` **27/27** pins the invariant in both directions:
- **no data at all** → every one of the five money fields is `null`, renders `—`, and **no field
  renders `$0.00`**; the engine agrees (`amount None`, `label unavailable`);
- **a fetched zero** → `$0 verified`, rendering `$0.00` — losing that distinction is the same bug
  inverted, and §25/§26 exist to preserve it;
- **partial data** still computes, so the fix cannot over-blank real figures;
- **source guard** — every money field must be interpolated through `fmtC()`; a future field added
  raw is caught. Verified to have teeth against a synthetic unwrapped field.

## 30. BUILT 2026-08-17 — the `irving_act` adapter, behind the same interface

Irving ISD runs its **own copy of the same ACT software** Dallas County uses, so its detail page is a
plain `showdetail2.jsp?can=<CAD>&ownerno=0` GET — **no session, no widget, no browser**. That is a
real simplification: the adapter runs in-process, so the backfill reaches it without Playwright.

Same contract as `gds`: CAD-keyed (no address normalisation), membership before balance, identity
guard on every fetch, retry once, fail-soft to `unavailable` → INDETERMINATE. The instance path
(`irving`) is registry DATA on the collector spec (`act_path`), asserted absent from the fetcher.
`collector_backfill.py` is now platform-agnostic — each adapter takes only the collectors on its own
platform, so a case naming both a GDS office and an ACT district is served by both in one pass.

### 30.1 Live result — all 8 Irving cases, 0 discards, 0 unavailable

**$117,358.39 fetched.** Every returned site address matched its case address independently of the
account match. Payoff effect:

| case | payoff before → after |
|---|---|
| TX-26-00041 | $83,263 → **$171,467** (+$88,204) |
| TX-26-00056 | $10,765 → **$25,207** |
| TX-26-00085 | $13,360 → **$26,494** |
| TX-23-01976 | $1,428 → $2,731 |
| TX-23-00768 | $14,834 → **$274** |
| TX-23-01979 | $18,742 → **$0** |
| TX-23-00478 | $27,257 → **$0** |

**The three DECREASES are correct and worth stating.** Those cases had a known ACT $0 and were falling
back to an estimate derived from the filing amount; now every named collector has been read and
returns ~zero, so the payoff is a **verified** $0/$274 rather than a guess. The adapter resolves
uncertainty in both directions — that is the same mechanism that made TX-26-00991 verified-paid.

TX-26-00041's $88,203.96 was checked rather than accepted: current $3,382.91 + prior $84,821.05
reconciles exactly, the petition filed Irving ISD at $28,778 (since accrued), and City of Irving is
inside ACT's units so no third collector is missing.

### 30.2 ⚠ FOUND, NOT FIXED — the §17.4 band and the payoff now disagree on 11 cases

`balanceBand()` reads only the ACT scalar, so a parcel with ACT $0 + an active suit still reads
**"Unconfirmed — needs check"** even where **every named collector has now been fetched and came back
zero** — where the payoff correctly reads a **verified $0**. Two surfaces, one parcel, different
answers: the exact family §28 just closed.

**11 cases:** TX-26-00991 · TX-26-00992 · TX-26-00994 · TX-23-02248 · TX-23-02239 · TX-24-00090 ·
TX-23-00478 · TX-26-00039 · TX-25-00479 · TX-25-00591 · TX-26-01291.

This is the §17.4 residual recorded as *blocked-on-data* — and **the data now exists** for these
cases. The fix is to make the band collector-aware: a zero corroborated by every named collector is
CONFIRMED paid, not unconfirmed. `frontend/index.html` is a served artifact, so it takes **its own
gate** and is not batched here.

### 30.3 Pins

`test_collectors_act.py` **25/25** — total-vs-current-year parsing, current+prior reconciliation, a
fetched zero staying a fact, five fail-closed shapes, the instance path being registry data, identity
guard and retry present, membership gating, and gds collectors not routed here.

## 31. BUILT 2026-08-17 — the band becomes collector-aware (two gates), and BAND↔PAYOFF parity is enforced

§17.4 flagged an ACT $0 as **UNCONFIRMED** because a collector billing outside ACT might be unread.
That was the honest state when nobody had checked. The adapters have now checked — so on **11 cases**
the band still said "Unconfirmed" while the payoff on the same parcel said a **verified $0**. Two
consumers of collector data, one updated: the §28 defect at a third surface.

### 31.1 Gate 1 (backend, inert) — promoted columns

`balanceBand` runs on the SKELETON, which drops **both** `property_intel` and `tax_breakdown`, so it
could not see collector coverage at all. Three columns promoted in the same lockstep as
`current_tax_balance`: `collector_fetched_total`, `collectors_fetched`, and `collectors_named` —
the last derived by **`jurisdictions.py`, the same module the payoff engine uses**, so the two
surfaces can never disagree about *which* collectors exist.

`_collector_subvalues` counts only entries with a real numeric amount: an unreachable collector stays
out of the total, while a **fetched $0.00 IS counted** — that is exactly what lets the band say
confirmed-paid. `(None, None)` when nothing was fetched keeps "never checked" distinct from
"checked, zero".

**A gap caught by verifying prod rather than assuming:** the columns went live and came up **all
null**, because they only populate on a `create_case` write and no pre-existing row had been
rewritten. The `current_tax_balance` promotion had an `init_db` backfill for exactly this; the
collector columns needed the same. After it: **73 cases with `collectors_named` > 0, 47 with
`collectors_fetched`.**

### 31.2 Gate 2 (frontend) — the corroborated zero

`zeroIsContradicted()` now returns false when **every collector the petition named has been fetched
and they sum to zero** — the zero is corroborated, the parcel is genuinely paid. Everything else is
unchanged: partially-checked, never-checked, and all-ACT parcels all stay UNCONFIRMED.

**Blast radius: exactly 11 cases, all `unknown → zero`, nothing else moved** (bands before
`unknown 80 / zero 38` → after `unknown 69 / zero 49`).

### 31.3 THE GUARD — `test_band_payoff_parity.py`

The band and the payoff are now two consumers of collector data, and nothing but a test stops them
drifting again. The invariant:

> `band == "zero"` ⟺ the payoff is a **VERIFIED $0**

Eight synthetic shapes (corroborated / partially checked / never checked / real balance / all-ACT /
dismissed / no suit amount) **plus a sweep of the entire real book** asserting no case disagrees —
where 11 did before. The band now joins the enforced-parity family:

| guard | question |
|---|---|
| §19 | is this a set? |
| §28 | did every consumer of the payoff update? |
| §29 | does absence survive arithmetic? |
| **§31** | **do the band and the payoff agree on paid-vs-unconfirmed?** |

`test_zero_balance_band.py`'s harness was extended for the new dependency (18/18 still green) — a
reminder that extracting functions for a browser test couples the test to the artifact's call graph.

## 32. BUILT 2026-08-17 — enrichment integrity: resolving the accounts that block every adapter

The last coverage gap, and the lowest-certainty work in the arc. Two populations, one operation:
**A** the 18 cases whose petition names a reachable collector but that carry NO DCAD account (so no
adapter can be queried at all), and **B** the 4 carrying a WRONG account (§17.2 contamination
residue, wrong in LOCAL as well as prod — which is why a plain re-sync was never the fix).

### 32.1 THE FINDING THAT CHANGED THE GATE — ACT verification IS the second signal

Validated the pipeline first on a case whose answer was already known by hand (5221 Robin Road →
`26380500080060000`). The resolver returned **the correct account** but graded it **`uncorroborated`**,
because DCAD's owner reads "VILLANUEVA LUIS A C & GELISTA HERLINDA M" against a defendant of
"HERLINDA M. GELISTA" and the name heuristic did not converge.

Under the original gate (`corroborated` only) that verified-correct answer would have been **thrown
away**. So the gate was restructured: a candidate is accepted when **ACT's own site address confirms
the parcel**, whichever grade the name heuristic assigned. This is *stricter*, not looser — the ~2%
confidently-wrong-parcel rate the guard exists to stop is exactly the case where ACT **disagrees**,
and ACT is an independent authority rather than a fuzzy string compare. Nothing is accepted on an
address search alone.

### 32.2 Result — 12 of 22 resolved, every one ACT-verified

| outcome | n | |
|---|---|---|
| **RESOLVED + ACT-verified + written** | **12** | 5 also `corroborated` by name, 7 `uncorroborated`-but-ACT-confirmed |
| UNRESOLVED — no DCAD candidate | 8 | 6 of these have **no street address at all** (blank, or a legal description only) — structurally unresolvable by address search |
| REJECTED by verification | 2 | the case has an EMPTY address, so ACT had nothing to verify against — the guard correctly refused to write |

**Population B: 3 of 4 contamination residues fixed and ACT-verified** — TX-24-00080 → `00C23700000000211` (2220 CANTON ST) · TX-26-00990 → `00000626481080000` (10130 SHAYNA DR) · TX-26-01093 → `00000510373000000` (3048 BEAUCHAMP ST).

**⚠ TX-26-00086 required a deliberate act, not just a skip.** Re-resolution found no candidate, but the
case still carried the KNOWN-WRONG account marked `resolved` — so every downstream consumer would keep
trusting a parcel belonging to another case. The account, status and all values derived from it were
**cleared to `needs_lookup`**. Leaving a known-wrong value marked confident is the exact failure this
whole arc exists to eliminate; absence is the honest state.

**Adapter coverage unlocked: eligible cases 71 → 80.**

### 32.3 What remains, and why it is not a defect

The 8 unresolved stay `needs_lookup` → payoff **INDETERMINATE**. Six have no street address in the
petition extraction, so this is an EXTRACTION gap upstream of resolution, not a resolver limitation —
the correct next target, and materially different work from account resolution.

### 32.4 ⚠ A THROTTLED PORTAL READ AS A CLEAN NEGATIVE — caught by ground truth, now impossible

Backfilling the 12 newly-resolved cases, **six came back UNAVAILABLE including TX-26-01455 — a parcel
confirmed BY HAND earlier the same session to be on BOTH Garland rolls** (GISD acct `0000103464`
$4,896.59, City acct `0000110637`). A parcel cannot stop being on a roll, so the result was
impossible, and only ground truth made that visible.

**Diagnosis (CORRECTED 2026-08-18 — see §32.6).** texaspayments serves `/Error/WrongRequest` for
every path INCLUDING its own homepage, and the page states the reason itself: *"You have been
redirected to this page possibly due to your IP location."* This is an **IP-level block**, confirmed
by the site rather than inferred. The original "throttled after ~200 requests" wording was a guess at
the trigger; the mechanism is IP, and whether our fleet volume caused it is unknown. The two ACT hosts
answered 200 throughout, so it is that host refusing this IP — not a code regression.

**The design gap:** `parse_account_detail` saw an error page, found no account, returned `{}` — and
the caller recorded `unavailable`, **indistinguishable from "this parcel is not on the roll."** Safe
(nothing wrong was written) but misleading, and it would have been reported as coverage that does not
exist.

**Fixed:** `PortalUnavailable` is raised when the portal redirects to an error page or the search form
is absent, propagates past the blanket `except`, and is recorded as `_portal_unavailable` —
infrastructure to retry, never a fact about a parcel — and the run stops hammering a portal that is
refusing it. **Verified against the live block**, which was still in force.

This is the same principle as `unavailable` vs `$0`, one layer up: **absence must stay
distinguishable from a negative.** The nine Garland/C-FB cases are pending a retry once the portal
recovers; their accounts are resolved and stored, so the retry is a fetch, not a re-resolution.


### 32.6 ⚠ CORRECTION — two independent faults overlapped, and I misattributed one of them

Recorded because the diagnosis was wrong in a way worth remembering.

**Fault 1 — the local machine ran out of disk.** `No space left on device`; bash could not write temp
files, `df`/`head`/Python all failed, and a scratch probe script was silently **truncated to 0 bytes**
(which then "ran" successfully and printed nothing). Playwright could not launch. This broke every
diagnostic I was using at the moment I was using it.

**Fault 2 — texaspayments is IP-blocking this host.** Real, independent, and confirmed by the site's
own error text once there was disk to run a browser again.

**The misattribution went both ways, which is the lesson.** First I attributed the failing fetches to
the portal from a bare `curl` status code — the wrong mechanism, since the adapter drives a real
browser. Then, on discovering the disk was full, I over-corrected and told the user my portal
diagnosis was *"likely wrong"* — but it was substantially right; I had simply proven it badly. **A
correct conclusion reached by an invalid method is still worth re-deriving, but it is not
automatically false**, and revising it on the strength of a second confounder is its own error.

**Integrity after the disk event, verified not assumed:** git fsck clean · 0 uncommitted · all four
served artifacts byte-match their commits · `pcpeak.db` integrity ok at 334 cases · prod ledger intact
(330 predictions, 4,627 case_snapshots). The local `ledger.db` was truncated to 0 bytes, which is
harmless — it is the prod-owned file and the local copy is documented as empty; `init_db` recreates
the schema.

**Operational consequence, wider than the 9 cases:** while this IP is blocked, the **entire `gds`
adapter is unusable** — Garland ISD, City of Garland, Richardson ISD and Carrollton-Farmers Branch,
4 of the 5 mapped collectors. Already-fetched balances are unaffected (stored in `property_intel`,
synced to prod); only NEW fetches fail, and they fail honestly as `_portal_unavailable` →
INDETERMINATE. Remedies are operational, not code: contact `support@gdsincorporated.com`, or run
enrichment from a different network. `irving_act` and all ACT/DCAD enrichment are unaffected.

## 33. BUILT 2026-08-18 — the COMPLETENESS INVARIANT: petition membership is a LOWER BOUND

**Seventh instance of the §19 "is this a set?" class, and the §23 defect one layer upstream.** §23
closed the case where a *balance* was scoped narrower than it claimed (`current_tax_balance` meaning
"all units" in Dallas and "county-side only" in Garland). §33 closes the same defect in the
*membership* that replaced it: `petition_collectors` reads the petition's Exhibit-A breakdown, which
names **plaintiffs**. A district that levies on the parcel and did not join the suit is invisible to
it. So petition membership is a **lower bound**, and "we retrieved every collector we know about" is
not "we know about every collector". **Fetching a lower bound to completion proves nothing about the
set.**

### 33.1 What the trace found — the safeguard existed and was disconnected at both ends

`payoff_completeness` was already correct: it refuses `True` without `act_units` (ACT's own
per-parcel unit report) and returns `None` — UNKNOWN — instead. Its own comment says it outright:
*"the petition lists plaintiffs, not everyone owed."* Two facts killed it in production:

1. **`act_units` is populated nowhere.** Its only occurrences outside tests were the dataclass
   default (`None`) and the pass-through into `collector_lines`. So `complete` was **structurally
   never `True`** — only `False` or `None`.
2. **No production caller read `complete` at all.** All four read `unavailable_collectors` instead
   (`acquisition.py` 212 / 398 / 604, and `frontend/index.html` re-implementing it independently) —
   a list derived *entirely from petition membership*.

The engine computed an honest UNKNOWN and every caller discarded it. Worse, the one test asserting
the happy path used `is not False`, **which passes for `None`**, so the suite could not tell
"verified complete" from "completeness unknown" either.

**The cited counter-mechanisms were both inert, and were checked rather than assumed.** "DCAD
corroboration" is measured unusable by this project's own test (`test_dcad_jurisdictions.py`:
`tax_rates` EMPTY on 223 and MALFORMED on 77 of 300); "the negative signal" *is* `act_units`, never
populated. `property_intel.tax_by_year` — the field that would carry ACT's unit report — is present
on all 300 enriched blobs and **nonempty on zero**.

### 33.2 The invariant

No surface may CLAIM completeness unless it is affirmatively established.

| verdict | meaning |
|---|---|
| `complete is True` | retrieval complete **and** membership verified |
| `complete is None` | **NOT complete** — the state `is not False` used to swallow |
| `complete is False` | a NAMED collector was not retrieved |

`jurisdictions.payoff_is_complete()` is the single reading, so no caller invents its own.
`membership_verified` is exposed alongside it as a fact in its own right.

### 33.3 TWO QUESTIONS, NEVER COLLAPSED — and the scoping decision that follows

  · **RETRIEVAL** — did we reach every collector we KNOW about? → `unavailable_collectors`
  · **MEMBERSHIP** — do we know the full set of levying units? → `payoff_is_complete`

**Money gates on RETRIEVAL. Claims of completeness gate on MEMBERSHIP.** Gating closability on
membership would force every priced deal to INDETERMINATE and silently retire the seller-net money
gate — *and a cold fleet sweep would report ZERO verdict flips*, because no case carries an
`agreed_price` until a rep enters one. The damage would be invisible in the sweep and live at the
exact moment money is decided. That trap is pinned in `test_payoff_completeness.py` so it cannot be
"fixed" by accident.

**The unverified set is deliberately NOT a gate.** `deal_gates` feeds a table where any `generic`
gate demotes `GO → GO-WITH-CONDITIONS`; an unverified set is the state of *every* case today, so a
gate would fire at 100% and retire the `GO` verdict outright — **measured, not predicted: it flipped
the Tryon golden fixture (a signed, closing deal) on the first run.** Identical over-fire to the one
§23 recorded when this same finding was modelled `substantive` and lifted 95 of 334 cases. A
condition true of the whole book carries no discriminating information at the verdict layer.
**Revisit when ACT unit capture lands** — unverified then becomes a real minority, a gate
discriminates, and its severity should be chosen by measuring the *then-current* blast radius.

### 33.4 Measured blast radius (local book, n=329, engine errors 0)

- **281 payoff labels flip `verified → estimated`**, one single cause: membership unverified. Amounts
  are **byte-identical** — this is a LABEL fix, not an arithmetic one, pinned as such.
- **11 band flips `zero`/PAID → `unknown`/UNCONFIRMED.** All are `active` (7) or `judged_pending` (4)
  with a real filed debt — the case posture *independently* contradicts "paid", so every flip is a
  genuine "we cannot prove this is the full collector set", not an artifact. This reverts §31's
  corroborated-zero branch, which rested on the same lower bound.
- **0 verdict flips.** Golden set intact: Grant St NO-GO, Addie Rd GO, Brown GO-WITH-CONDITIONS.

### 33.5 A guard that went vacuous, caught by its own standing lesson

Changing the label broke §31's parity predicate *silently*. It compared `band=="zero"` against
`label==VERIFIED`; §33 pins that label to ESTIMATED fleet-wide, so **both sides went constant-False
together and every fixture passed while its description claimed the opposite outcome** — including
two rows literally named "⇒ CONFIRMED PAID". The suite reported 10/10.

Fixed by reading `payoff_is_complete(completeness)` instead of the label word, **and** by asserting
each row's `expected_band` DIRECTLY — a parity check alone cannot notice that both of its sides went
false at once. This is the standing "**0 flips → trace why before calling success**" lesson landing
on the guard family itself.

### 33.6 Suites

`test_payoff_completeness.py` **30/30** (fifth guard family). Every claim additionally re-run against
the DEFECT's own logic (`[teeth]`) and required to disagree with it — *a guard that would pass with
the bug reinstated is not a guard*. Rejected one teeth-check as theatre when truthiness and the
correct reading agreed on every state; the defect was specifically `is not False`.
`test_band_payoff_parity.py` 10 → **18/18**. All four prior families green.

### 33.7 GROUND TRUTH — the petition declares the lower bound in its own words

§33 is not an imposed assumption. The source document states it. Dallas tax petitions of the
delimited template read:

> **NOW COME(S) THE TAXING DISTRICTS SET OUT BELOW: GARLAND INDEPENDENT SCHOOL DISTRICT**
> **ON BEHALF OF THEMSELVES AND ALL TAXING DISTRICTS FOR WHOM THEY COLLECT.**

The named plaintiff sues *on behalf of districts it does not name*. So `tax_breakdown[].entity` — the
membership oracle — is by the petition's own construction a **lower bound** on who levies, and any
surface reading it as the complete set is contradicted by the instrument it came from. Verbatim from
TX-26-00994 (a Garland ISD-only suit); the clause is boilerplate across that template.

**Corollary, and the reason §33's conservatism is not over-caution:** a suit naming ONE district is
not evidence that one district levies. It is evidence that one district *sued*, expressly for itself
and for others unnamed.

### 33.8 What the backfill established (2026-08-19) — and the coverage correction

The union-merge backfill (`petition_union_backfill.py`) improved **0 of 329** cases; `pcpeak.db` is
byte-unchanged. Deterministic re-parse of the local corpus recovers nothing the stored Claude
extractions do not already hold. **That is the finding, not a failure of the run.**

- **Union-only was load-bearing.** Overwrite would have SHRUNK 2 cases — TX-26-00899
  (`PROPERTY RESEARCH FEE`) and TX-24-00092 (`CITY OF GARLAND UTILITY LIEN`).
- **Three parser designs were rejected, each caught by dry-run before any write**: a prose regex whose
  character class contained a space (swallowed counsel names, "improved" 327 of 329); the same plus
  stopword trimming (minted `CITY OF DALLAS ACTIVE ATTORNEYS`); and a closed vocabulary over the whole
  document (added `DALLAS COUNTY` to ~100 cases from **venue boilerplate** — `IN AND FOR DALLAS
  COUNTY, TEXAS` — verified against TX-26-00994). The survivor also needed longest-match-**masking**:
  without it `DALLAS COUNTY` matched inside `DALLAS COUNTY UTILITY & RECLAMATION DISTRICT`.
- **A FABRICATED COLLECTOR IS WORSE THAN A MISSED ONE.** It becomes a permanent `unavailable` payoff
  line for a debt that does not exist — the lower bound corrupted *upward*, the mirror of the defect
  §33 closed.
- **COVERAGE CORRECTION.** The earlier "~76% re-extractable without portal contact" was wrong. The
  corpus is on disk (367 dockets / 352 petitions, 756MB) but only **~26%** carries the delimited
  plaintiff list; the other 245 use a template where `ON BEHALF OF` belongs to the law-firm
  authorization clause and no reliable delimiter exists. That readable subset is **already complete**.
- **The 8 membership residuals are NOT resolved here.** All 8 use the second template and TX-26-00087
  has no petition PDF at all. They require **Claude re-extraction** — the path that produced the
  stored data — which is cost-gated and its own increment.

## 34. SCOPE (NOT BUILT) — `act_units`: positive completeness, and the $0 blind spot

§33 made completeness conservative: `complete` is `None` fleet-wide because `act_units` has no source.
This increment gives it one — turning UNKNOWN into **proven complete** where ACT genuinely collects
every unit. It is scoped here and **no code is written** until this is signed off.

### 34.1 The source, and precisely what it does and does not yield

`https://www.dallasact.com/act_webdev/dallas/reports/taxbyyearbyunit.jsp?can=<account>&ownerno=0`
publishes ACT's own per-parcel unit breakdown — an INDEPENDENT authority, not the petition. §23 read
it by hand for Garland parcel TX-23-02251 (CAD 26238500070260000): `DALLAS COLLEGE · DALLAS COUNTY ·
PARKLAND HOSPITAL · SCHOOL EQUALIZATION` — no ISD, no city, which is exactly how that arc proved the
ISD burden sat outside ACT.

**Nothing fetches it today.** `property_intel` hits `taxbyyear.jsp` — the YEAR-level page, a
different report — into `tax_by_year`. ⚠ Separately: `tax_by_year` is **nonempty on 0 of 300** enriched
blobs, so that existing scrape is silently broken. Pre-existing, out of scope here, logged.

### 34.2 ⚠ THE SHARP EDGE — a blank unit list at $0 is UNKNOWN, never "ACT is everything"

§17.5 already established that ACT renders **"No taxes due." with NO unit list** exactly when the
balance is $0. That is the single most dangerous input this increment can receive, because the
tempting reading — *"ACT returned no extra units, so the set we hold is complete"* — is
**absence-treated-as-a-value**, and it would re-open the precise false-complete §33 just closed. It
would also wrongly corroborate the band's zero on the 11 cases §33 just moved to UNCONFIRMED.

**The fetch therefore has THREE outcomes, never two, and they are stored distinguishably:**

| ACT response | `act_units` | `membership_verified` | stored reason |
|---|---|---|---|
| unit list renders (N units) | `[names…]` | **True** | `act_unit_list` |
| "No taxes due.", no list ($0 parcel) | `None` | **False** | `no_unit_list_at_zero_balance` |
| fetch failed / blocked / timeout | `None` | **False** | `fetch_failed` |

Rows 2 and 3 both yield UNKNOWN completeness → INDETERMINATE. **The reason is still recorded
separately**, because they are different facts: row 3 is retryable, row 2 is a permanent property of
the source at $0. Collapsing them into a bare `None` would lose that and invite someone to "fix" the
$0 case with a retry loop that can never succeed.

**Two invariants to pin as tests before any code ships:**
1. A blank list at $0 must NOT produce `complete is True`, and must NOT flip any band to `zero`.
2. An EMPTY list must never be passed through as an empty SET. Today `collector_lines` does
   `{…} or None`, which collapses `[]` to `None` and happens to behave — but it is implicit and
   unpinned. Make it explicit, or a refactor deletes the safety without anyone noticing.

### 34.3 Population — what this can and cannot reach (measured, n=329)

| ACT balance | cases | reachable by this increment? |
|---|---|---|
| **> $0** — unit list renders | **210 (63.8%)** | **YES** — completeness becomes provable |
| **== $0** — no unit list (§17.5) | **88 (26.7%)** | **NO** — stays INDETERMINATE, permanently |
| null / not captured | 31 (9.4%) | NO — no balance to query against |

308 of 329 carry a stored DCAD account, which is the fetch key.

**Ceiling: at most 210 of 329 (63.8%) can ever reach `complete is True` here** — and only those whose
named collectors were ALSO all retrieved, so the real figure is lower and gated on the gds IP block
clearing for the Garland-family parcels.

**THE 11 BAND-FLIP CASES ARE ALL IN THE 88.** They have ACT $0 by definition, so this increment
**cannot** resolve them — by construction, not by omission. They remain UNCONFIRMED until a source
exists that states unit coverage independently of the balance. §17.5 already recorded the only known
candidate — the per-unit levy — as **PDF-only**. Anyone reading this expecting `act_units` to close
the zero-balance question should stop here: it closes the opposite population.

**Checked and rejected as a fallback for the 88:** `payment_history` is nonempty on 296 of 300 blobs
but its rows are `{date, amount, tax_year, payer}` — it names **no taxing unit**, so it cannot
establish coverage.

### 34.4 Non-goals
No change to the money gate (§33.3 scope stands). No balance fetching — this is coverage only. No
re-enabling of the `collector_set_unverified` gate; that decision is re-measured *after* this lands,
when unverified is a minority (§33.3).

### 34.5 Open decisions — sign-off needed before code
1. **Where does it run?** Fleet pass (~308 fetches, one per account) vs on-demand at analysis time vs
   folded into enrichment. Same scheduling/rate-limit question the standing-land-floors job has.
2. **Staleness.** A parcel's unit set changes rarely but not never (annexation, district changes). TTL,
   or re-fetch on re-enrichment?
3. **Storage.** New `property_intel.act_units` + `act_units_reason`, or promoted columns like the §31
   collector fields (the band reads the skeleton, so a promoted column is needed if the band is ever
   to use this).
4. **Does the band get to use it?** A parcel with a VERIFIED unit set and every collector retrieved at
   $0 would be genuinely corroborated-paid — but that population is empty by construction (§34.3), so
   this may be dead code on arrival. Decide deliberately rather than building it speculatively.

### 33.9 DEPLOYED + LIVE-VERIFIED 2026-08-19 (`c997dd1`) — its own gate, §34 deliberately held back

`origin/main == origin/production == c997dd1`; feature branch ahead at `6943a7a` (backfill tool, docs,
and §34 — **not** deployed, so the gate is exactly §33). No commit reorder was needed: §33 is the
OLDEST of the new commits, so a plain FF to it ships the guard alone.

**Fingerprints before → after, verified against the LIVE SERVED BYTES:**
`frontend/index.html` `a14c1d1d75bb8a44` → **`b8f4e94682a4d084`** (the served sha matched its git blob
exactly both before and after) · `acquisition.py` `07b2d8f011c54694` → **`7c280a66888f26d6`** ·
`jurisdictions.py` `e580fa6abba347a9` → **`2bc71d28a7e02362`** · `backend/main.py`
**`0821b3408b57b8e2` UNCHANGED**. Railway took ~60s to serve the new artifact; the poll waited it out
rather than diagnosing (standing deploy-window lesson, which has now bitten twice).

**PROD COUNTS ARE NOT THE LOCAL COUNTS, and the difference was predicted before deploying.** The local
figures were 281 labels / 11 bands on 329 real-property cases *including archived and local-only held*;
prod's working book is 252. The expected prod numbers were therefore derived from a **prod pre-deploy
baseline through the real endpoint** — 186 cases labelled `verified`, 4 band candidates — and both were
then confirmed exactly. Same denominator effect as §18 (10.8%→11.2%) and §17.4 (50→21).

**FULL BEFORE/AFTER SWEEP, 252 cases through the live endpoint, 0 errors:**

| measure | result | expected |
|---|---|---|
| payoff label `verified → estimated` | **186** | 186 ✓ |
| payoff AMOUNTS moved | **0** | 0 — a LABEL fix, not arithmetic |
| decision flips | **0** | 0 |
| closability / gate flips | **0** | 0 — money gate reads RETRIEVAL |
| `membership_verified` | **False on 252/252** | False (no `act_units` source yet) |

**186 and not one more is the load-bearing number**: a higher count would have meant a consumer of
`complete` the §33 trace missed.

**BAND: 4 flips, computed by the SERVED frontend code against live prod skeletons** — TX-25-00591 ·
TX-24-00090 · TX-23-02248 · TX-23-02239, all `judged_pending`, all balance $0. The other 7 of the local
11 are **archived on prod** (verified individually via `?include_archived=1`), so the 4 are exactly the
non-archived subset — not collateral. Full census `unknown 44 · low 108 · mid 59 · high 13 · zero 28`;
**every one of the 28 still banded `zero` is `dismissed_paid` or has no suit amount**, so there was no
blanket zero→unknown. Zero pageerrors.

**Spot-checks (the three largest flipped payoffs, where a false `complete` costs most):** TX-23-00569
$196,083 · TX-26-00041 $171,467 (`ACT $83,263 + $88,204 from collectors billing outside ACT`) ·
TX-26-00076 $156,445 — each keeps its amount to the dollar, drops `verified` for `estimated`, and now
carries `— FLOOR: every NAMED collector was read, but the collector set is UNVERIFIED (the petition
names plaintiffs, not everyone who levies)`.

**§33.3's money-gate reasoning is empirically confirmed by this sweep**: every case reads
`gate=no_agreed_price` cold, because no case carries an `agreed_price` until a rep enters one. Had
closability been gated on membership, this sweep would have shown **0 flips and looked clean** while the
seller-net gate was silently retired for every priced deal.

### 33.10 ⚠ FLIP-COUNT RECONCILIATION — and a correction to my own local figure

The acceptance test on a completeness guard is not "did it flip" but "does the count reconcile
case-by-case": **more** flips than the trace predicted means a real false-complete nobody modelled;
**fewer** means a consumer that is not reading the guard, i.e. honesty not reaching those cases. Both
directions were checked as sets, not totals.

**THE PREDICTED FIGURE OF 281 WAS MY OWN MEASUREMENT ERROR.** It was produced by reconstructing the
pre-§33 label with a *formula* — `ESTIMATED if (unavailable or not act_known) else VERIFIED` — instead
of the function's *branch structure*. Three of `tax_payoff`'s branches (`fallback_estimate`, the
no-data branch) **hardcode** their label and never read the computed `label` variable at all, so §33
is a structural no-op on them. The same reconstruction flaw was spotted earlier for two
`verified→unavailable` rows and correctly discounted; it was not extended to the fallback branch, and
that omission produced the inflated number.

Counting only the three LABEL-BEARING bases (`act_live_balance`, `act_plus_collectors`,
`collectors_outside_act`), the correct local prediction is **229**, not 281.

| set | count |
|---|---|
| **L** — local predicted (branch-correct) | **229** |
| **P** — prod observed | **186** |
| `P \ L` — flipped but NOT predicted | **0** |
| `L \ P` — predicted, archived on prod | **43** |
| `L \ P` — predicted, absent from prod | **0** |
| `L \ P` — **ACTIVE on prod and did not flip** | **0** |

**229 = 186 + 43.** Closes exactly.

`P \ L = 0` is the safety result: **nothing flipped that the trace did not model**, so there is no
unmodelled consumer of `complete`. `active-and-did-not-flip = 0` is the other half: **every case
§33 should have made honest, it made honest** — no consumer is failing to read the guard.

The 32 cases that first appeared as an unexplained residual were investigated rather than assumed:
prod's `owed_live` is `0.0` on all of them, identical to local, and their payoff basis is
`fallback_estimate` — a branch whose label is hardcoded `ESTIMATED`. They were already honest before
§33 and could not flip. That was a defect in the prediction, not in the deploy.

**BAND, same treatment:** 11 predicted locally, **4 observed on prod**, and the missing 7 were each
checked individually against `?include_archived=1` — **all 7 archived**, none absent, none active-and-
unflipped. The 4 are exactly the non-archived subset, all `judged_pending` at $0, and §31 parity ties
them: all 28 cases still banded `zero` are `dismissed_paid` or carry no suit amount.

### 34.6 DEPLOYED + LIVE-VERIFIED 2026-08-19 (`73b94e1`) — reader first, then the field

Two gates in strict order, on the §33-before-`act_units` principle: **deploy the reader, then land the
field — never sync into an engine that cannot read it.**

**GATE 1 — §34 code (`c997dd1` → `73b94e1`).** Served-artifact delta: `backend/main.py`
`0821b3408b57b8e2` → **`c2036fbb10dc0d00`** · `jurisdictions.py` `2bc71d28a7e02362` →
**`fecdf9133f84c175`** · `property_intel.py` `452efe44e539d3c6` → **`7bbfa45453784d3d`** ·
`acquisition.py` **unchanged** · `frontend/index.html` **unchanged** — confirming no band path shipped,
as scoped.

**The predicted result of gate 1 was ZERO flips, stated BEFORE deploying**, because §34 is the reader
and prod carried no `act_units` data. Observed: **0 label flips, 0 completeness flips, 0 decision
flips.** Here 0 is the pass condition; a nonzero result would have meant something unmodelled. This is
the inverse of the standing "0 flips → trace why" lesson: predict the zero, then confirm it.

Because an inert deploy is externally unobservable, **a one-case CANARY was used as the proof**:
`sync_all.py TX-23-00569` pushed a single parcel's `act_units`, and the engine immediately read it —
which is the only evidence that could distinguish "§34 is live" from "§34 never deployed".

**GATE 2 — the data (`sync_to_prod.py --update-existing`).** 328 refreshed, 0 created, 0 failed, 85
events, reconciled. `prod_ready` satisfied by construction: every case was already live, so nothing was
promoted.

**RECONCILIATION — closes exactly, both directions:**

| set | count |
|---|---|
| **L** local predicted `verified` | **168** |
| **P** prod observed `verified` | **144** |
| `P \ L` verified but NOT predicted | **0** |
| `L \ P` archived on prod | **24** |
| `L \ P` absent from prod | **0** |
| `L \ P` **active and did not flip** | **0** |

**168 = 144 + 24.** All 144 flips are `estimated → verified`; **0 decision flips**.

Prod completeness after: **`True` 144 · `False` 51 · `None` 57**, labels `verified 144 · estimated 107
· unavailable 1`. Before `act_units` existed, `True` was **0 fleet-wide**.

**THE EMBLEM.** TX-23-00569 ran the whole arc: `verified` (asserted, unproven) → `estimated` (§33 made
it honest) → **`verified` on evidence** (§34 proved it), $196,083 across six named units, reason *"every
named collector accounted for, against a verified collector set"*. TX-26-00041 is the stronger case:
$171,467 = **ACT $83,263 + $88,204 billed outside ACT**, now verified across six units including CITY
OF IRVING and IRVING ISD — a payoff that the pre-§23 model would have reported as $83,263.

**COUNTER-CASE HOLDS:** TX-26-01093 (`no_unit_list_at_zero_balance`) still reads `complete=None`,
`membership_verified=False`. The $0 blind spot stayed a blind spot, which is the whole point.

**3 payoff AMOUNTS moved** (TX-26-01388 $11,174→$3,174 · TX-26-01387 $14,491→$13,391 · TX-26-01372
$26,308→$13,308). These are **data deltas from the property_intel refresh, not §34 logic** — local held
fresher balances than prod. Noted so they are not misread as a completeness effect.
