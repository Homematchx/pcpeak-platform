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
