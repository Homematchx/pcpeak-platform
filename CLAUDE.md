# PC Peak Tax Foreclosure Intelligence Platform

**Live site:** taxforeclosureanalyzer.com
**Railway project:** gracious-tenderness
**GitHub:** Homematchx/pcpeak-platform
**Working directory:** `~/Downloads/pcpeak_platform`

## SESSION HANDOFF — 2026-07-23 (case disposition system + stale-intel-panel fix — SESSION DONE)

**Two initiatives shipped this session, both deployed + fingerprint-verified + live-verified.**

**1. CASE DISPOSITION SYSTEM (design-first, approved) — archive-never-delete. LIVE.**
Design: [`docs/case-disposition-design.md`](docs/case-disposition-design.md) (all 7 open decisions
settled, §12). TRACE FIRST established the old `🗑 Remove` made NO network call — a localStorage
edit `syncFromPlatform` silently undid ~30s later (nothing hard-deleted; a dead affordance that
looked like it worked). Replaced with a real, durable, reversible disposition STATE written to a
prod-owned append-only log.
- **Schema (ledger.db, restore-guarded):** `case_dispositions` (append-only; three row kinds
  proposal|decision|dismissal; state + code + comment + decided_by + evidence) + `case_comments`
  (separate from rep_actions so a note NEVER advances the deal_status funnel). Both in
  PROD_OWNED_TABLES. Derived cache cols on `cases` (disposition_state/code/at, pending_review*),
  skip-listed in sync_to_prod. `test_restore_guard` auto-rose to 47.
- **Three states:** active | **watching** | archived. watching = warm leads that legitimately come
  back (§33.02 plan, §32.06 loan, owner_declined, unable_to_contact) — out of the working queue,
  never filed next to `duplicate`. Review-flagging is ORTHOGONAL to state (a flag never changes what
  a case IS), which lets §6 invalidation predicates fire against watching/archived cases.
- **15-code taxonomy** served via `/api/dispositions/codes` (UI keeps no drifting copy): 3+4+1+5+2.
  **NO plain `dismissed` code** — dismissed-owing is the core pipeline; `dismissed_resolved` is
  guarded server-side on a REAL 0.0 balance (>0 or unknown REFUSED).
- **Derivation is PURE row-order** (current state = latest decision's state else active; open flag =
  a proposal newer than the latest decision-or-dismissal). No resolution column, no in-place update;
  reversal is a new decision row.
- **Auto-flag (§5) proposes, human commits; invalidation predicates (§6) ask "does the premise still
  hold?"** — never auto-archive, never auto-reopen. The §33.02 plan-default detector is what makes
  `watching` earn its keep.
- **`/api/cases` DEFAULT-EXCLUDES archived** (?include_archived=1 / ?state= restore); this is the one
  behavior change reaching existing consumers, chosen over an opt-in flag deliberately (an opt-in
  leaves archived cases in every denominator forever). `/api/stats` returns four reconciling numbers.
  **Denominator-printing requirement (§8)** applied to sync_to_prod (reads include_archived=1 for
  TRUE inventory so archived cases aren't re-pushed as new) and scorecard.py.
- **PROVENANCE PRINCIPLE recorded:** the prediction ledger's outcomes come from DOCKET EVIDENCE,
  never human filing decisions — a `sold_at_tax_sale` disposition is a rep's judgment, the docket's
  sale entry is the fact. Dispositions write NOTHING to prediction_ledger.outcome_type. Joinable-but-
  separate. Calibration labels captured (§7) but nothing consumes them yet (out of scope).
- **Tests:** `backend/test_disposition.py` 75/75 (every §13 pin incl. append-only reversal
  sequences, guards, deal_status isolation, count reconciliation, non-interference, invalidation
  precision), `test_disposition_browser.py` 24/24 (incl. the archived-case-stays-gone-across-a-sync
  pin). Verified against a copy of the real 247-case prod DB: migration clean+idempotent, NO inferred
  backfill (every case starts active). Live curl round-trip: guard 409, archive hides+stays-queryable,
  reopen restores.

**2. STALE PROPERTY-INTEL PANEL fix (traced from a live TX-26-01298 report).** Panel showed "Not Yet
Loaded" for property_intel the server WAS returning (15,405 chars). TRACE (real data, in-browser):
NEITHER suspect strips the in-memory object — `save()` slims a COPY, the sync rebuild REPLACES from
the fresh fetch. Root cause = the **full localStorage mirror is 6.66 MB**, over the ~5 MB per-origin
quota → save() degrades to a SLIM cache (property_intel stripped: 6.66MB→1.70MB measured). A hard
reload BOOTS from that slim cache (in-memory intel absent), and `syncFromPlatform` re-fetches the
full intel but DELIBERATELY didn't re-render the open detail (the 2026-07-18 anti-tab-reset rule) —
so the panel stayed stale until a manual click. **Pre-existing, NOT caused by the disposition work.**
- **Fix #1 (targeted):** after the rebuild, re-render the open detail ONLY when the open case's
  property_intel goes absent→present (`hasUsableIntel` mirrors the panel gate exactly), preserving
  the active tab (`activeDetailTabId`/`reactivateDetailTab`, petition tab skipped). Fires on material
  change ONLY — a steady-state 30s tick with intel already present does NOT re-render, so the
  anti-tab-reset rule holds by construction.
- **Fix #2 (complement):** the slim cache RETAINS the currently-open case's property_intel (one
  ~15KB blob is trivial against the freed megabytes) so the case a rep is mid-review on survives a
  hard reload without the flash.
- **Tests:** `test_intel_reload.py` 10/10 (deterministic hold-gate: pre-sync slim boot renders Not-
  Yet-Loaded, post-sync AUTO-populates with no click + tab preserved, steady-state second sync does
  NOT re-render, #2 open-case retention). `test_quota_save.py` updated for #2 (heavy case is now
  non-open so general slimming still drops it) → 5/5.

**Also this session:** `browser_env.py` — the 8 Playwright suites hardcoded a Linux chromium path
(`/opt/pw-browsers/chromium-1194/...`) and had SILENTLY STOPPED being checks on a Mac checkout;
`chrome_path()` resolves the pinned path first, else the local playwright cache. All browser suites
run again on this Mac.

**QUEUED FOR NEXT INCREMENT:**
1. **⬆ EVENTS-BATCHING — TOP OF QUEUE (elevated; THIRD STRIKE).** `syncFromPlatform` does 1
   `/api/cases` GET + ~244 SEQUENTIAL `/api/events/{cn}` GETs. One root cause, three independent
   symptoms now: (a) the 502 bursts (2026-07-18), (b) the widened navigation-race window
   (selection-stability bugs), (c) it determines how long THIS session's stale-intel-panel window
   lasts. Past "logged optimization." Fix: batch events into the `/api/cases` payload (one response).
2. **Land comp display — evidence behind the §G floor** (collapsible banded land-sale set; unchanged
   from prior handoff).
3. **Fleet-wide standing land floors** (batch-compute, not propose-triggered; unchanged).

**LOGGED — ARCHITECTURAL, NO ACTION (design when raised):** the mirror-everything client cache is
approaching end of life. The full localStorage mirror is **6.66 MB and grows with fleet size** — at
400+ cases the SLIM path becomes the PERMANENT boot state for every rep (property_intel never cached,
every case waits out the sync). Fixes #1/#2 mitigate the symptom; they don't move the ceiling. The
eventual fix is caching the case-list SKELETON and fetching detail ON DEMAND — which also DELETES the
quota problem entirely. Stage-3-adjacent.

**ALSO PENDING (unchanged, gated on the user raising them):**
- **Disposition follow-ups (Stage-3-adjacent):** calibration FROM disposition labels (v1 captures
  only); platform AUTHENTICATION as its own named initiative (triggered when reps beyond the owner
  are daily users — `decided_by` is self-attested today, the append-only reversible attributed log is
  the control, NOT a gate); bulk disposition.
- **2 pre-existing `test_petition_link` failures on HEAD** (`B: WITH url → link visible` / `href is
  the real https court URL`) — surfaced now that browser_env.py made the suites runnable again. NOT
  from this work (confirmed on unmodified HEAD). No action; someone should eventually look. See
  memory `petition-link-browser-failures`.
- Everything from the §G-land-floor handoff below (case disposition was the "queued next initiative"
  there — now DONE; the rest stands).

**SESSION DONE (2026-07-23, second session).** Both initiatives built, tested, deployed, live-
verified. Every remaining item logged and gated.

## SESSION HANDOFF — 2026-07-23 (§G land floor + batch legibility — SESSION DONE)

**Platform LIVE at `9c4a389`** (feature/main/production all FF, no force). Served artifacts:
`acquisition.py` `1ef778ff`, `comps.py` `d73aeeaf`, `backend/main.py` `dd752b37`,
`frontend/index.html` `bdfcf1fa`. Suites: `test_acquisition` 126/126, `test_comps` 84/84,
`backend/test_acquisition_api` 46/46, `test_restore_guard` 39/39, all regressions green.

**SHIPPED — §G land floor + propose-batch legibility** (design
[`docs/acquisition-intelligence-design.md`](docs/acquisition-intelligence-design.md) §16).
Triggered by a live finding on **TX-26-01190** (6406 Kemrock, 75241): propose returned 0 comps. Funnel:
1,529 Residential+Closed in zip → 282 after recency → **0 at the GLA band**, because the subject is
**484 sqft** against a local market floor of **870** — a sub-minimum structure on a land-dominant parcel
(DCAD improvement $50,340 vs land $70,000). The band was correct; the subject was outside the improved
market. **Zero qualified comps must never mean zero valuation information.**
- **`comps.py` §G engine:** `land_sales()` (`PropertyType='Land'` ONLY — teardown-intent is Stage 3),
  `qualify_land()` (LOT-SIZE band, **explicit NO-GLA-band guard**), `land_floor()` (**median of
  RECONSTRUCTED CLOSES**, never a $/acre extrapolation), `land_floor_for_subject()` (12mo default,
  widens to 24mo only when thin and says so), plus a **separate net-of-demolition line** (the gross
  floor is never reduced). All magnitudes tunable in `COMP_CONFIG['land']`.
- **HARD RULE — the floor NEVER feeds MAO.** `analyze()` takes it as a pure display passthrough read by
  nothing; tests pin that the MAO ladder, itemized MAO, decision, Mission Score, gates and seller-net
  sheet are byte-identical with and without it, that it never becomes the ARV, and that it never lifts
  a case out of HOLD.
- **Batch legibility:** new append-only `ledger.comp_batches` (restore-guarded) records EVERY propose
  including 0-comp ones — funnel counts, locality, GLA band, the **named zeroing stage**, and the land
  floor. Previously an empty propose stored no rows and was indistinguishable from never-proposed. The
  workbench now shows the batch result + Land floor with provenance instead of a dead end.
- **Acceptance:** Kemrock **$85,500 gross** (n=14, 12mo) — reproduced exactly. **Ruby's pin RESTATED**
  in §16.8: $42.5K → **gross ≈ $72,500 / net ≈ $63,900**. The original $42.5K was a single-comp $/sqft
  extrapolation from a ~36%-larger lot — the same size-dependence error §16.2 guards against; the engine
  caught a human land comp the way it caught naive $/acre. **Ruby's verdict is untouched** (it never
  rested on the floor); the deal reads stronger.

**STANDING RULE recorded (it bit twice in one increment):** *never extrapolate a per-unit rate
($/acre, $/sqft) across dissimilar sizes — band first, then take the median of actual closes.* Applies
to the GLA band (improved comps) and the lot band (land comps). Measured: naive $/acre understated
Kemrock by 67% ($51,130 vs $85,500); a 24-month window understated it again ($70,000) by importing 2024
sales at ~half 2026 levels — hence the 12mo default with widen-only-if-thin.

**QUEUED FOR THE NEXT INCREMENT (logged, do NOT start until raised):**
1. **Land comp display — evidence behind the floor.** Persist and render the banded land-sale set behind
   every floor (address, lot size, close date, reconstructed price) as a **collapsible section** in the
   workbench. Read-only until the land/teardown *exit mode* adds its confirm step. The floor currently
   shows a **conclusion without its evidence** — same **audit-not-trust** principle as the per-comp
   adjustment grid.
2. **Fleet-wide standing floors.** Batch-compute land floors across **all** cases that have a lot size,
   not only on propose — so the floor is genuinely standing rather than propose-triggered.

**QUEUED NEXT INITIATIVE — CASE DISPOSITION SYSTEM (fresh session, DESIGN-FIRST).** Same discipline as
the ledger / scrape-trigger / acquisition builds: no code until the design is approved.
- **Step one is a TRACE, not a design:** establish what the existing **Remove** button actually does
  today (frontend detail-card header → which endpoint/local path; note `DELETE /api/cases/{cn}` is the
  BPP-only guarded delete, so Remove may be doing something else entirely). Do not assume.
- Then design **archive-never-delete**: a disposition taxonomy, **auto-flag / human-decide** (the engine
  proposes a disposition, a human commits it — same propose→confirm shape), and **rep comments**.

**ALSO PENDING (unchanged, gated on the user raising them):**
- **60-no-GLA measurement report** — running in its OWN session; fold its result in when it lands. Carry
  the Kemrock framing: the land-routing bucket must catch **sub-minimum structures on land-dominant
  parcels**, not just vacant lots (cluster on land-dominance OR sub-minimum GLA, not improvement≈0).
- **Stage 3** — the confirmed-output-as-an-appraisal-report reframe (tiered pool, reconciled 3–6 comp
  set with range/spread/median, bracketing + can't-bracket flag, adjustment grid at confirm), plus exit
  matrix, sensitivity, the reweighted Mission Score, and the two logged Stage-3 candidates
  (provisionally-unclosable advisory flag; owner-of-record coverage backfill).
- **August Tryon closing — fee verification** (TX-23-00423): capture the title company's ACTUAL payoff
  demands; if the real §33.48 attorney fee is materially off the 20% estimate, recalibrate
  `tax_suit_atty_fee_rate` and RE-RUN Grant St's fee-sensitive NO-GO.
- **Branch:** all work on `claude/remove-analyze-with-ai-0vu5i9`; feature == main == production at
  `9c4a389`.

**SESSION DONE (2026-07-23).** Nothing mid-flight here; every remaining item is logged and gated.

## SESSION HANDOFF — 2026-07-21 (continued — post-Stage-2 hardening + fleet static-fire)

Everything here is AFTER the Stage-2-close handoff (immediately below, which covers the full
Acquisition Intelligence build). The layer is LIVE and was hardened through additional deploys; a
fleet-wide static-fire then validated its behavior at scale. **Current deployed state:
feature/main/production all FF to `be66c5e`** — `acquisition.py` `f060600c`, `backend/main.py`
`7919141a`, `comps.py` `7e4ca32f`, `frontend/index.html` `c9adeb10`. All acquisition suites green
(`test_acquisition` 116/116, `test_comps` 62/62, `backend/test_acquisition_api` 31/31, regressions green).

**Shipped + deployed after Stage-2 close:**
- **calcPayoff fix (`f8188d5`).** Financials-tab tax payoff = ACT live balance AS-IS (was fee-loaded AND
  passed null for the live balance) — now matches `acquisition.py`; the Acquisition and Financials tabs
  agree. Live-verified (Tryon $71,938 / Grant St $152,224).
- **City locality fallback (`5f5e2ca`).** propose 422'd on 37% of cases (81/220). `property_intel` stores
  NO DCAD situs/zip anywhere, so the subject-builder now derives the CITY from the case address
  (normalized Title Case; NTREIS `City` is case-SENSITIVE) and the comp query uses PostalCode else City.
  Recovered ~20 no-zip-but-enriched cases (TX-23-00553 → 'Dallas'); the 60 no-GLA cases correctly still
  fail closed. City-wide = broader/less precise (provisional/triage). Live-verified.
- **Decision-table drift fix + GRADUATED HEIR GATE + 0-photo block (`be66c5e`) — LIVE VERDICT SEMANTICS.**
  A provisional/unconfirmed valuation NO LONGER lifts a case out of HOLD (§5.4 — provisional is
  triage-only). Gate severities regrouped `fatal|substantive|generic`; table = fatal→NO-GO,
  substantive→GO-WITH-CONDITIONS (regardless of valuation), confirmed valuation→GO/GO-WITH-CONDITIONS,
  else→HOLD. The graduated heir gate (a logged Stage-3 item, DELIVERED EARLY): `heir_estate_title` is
  substantive only on a real owner-mismatch (`owner_defendant_mismatch` — DCAD owner-of-record a
  differently-named party than the defendant; name-token overlap, noise-word aware); absentee/estate
  without a mismatch = generic non-lifting `estate_absentee_signal`. Added `defendant` to CaseInput +
  backend `_case_input`. Resolved the TX-23-00553 contradiction as case (b): its heir flag is a GENUINE
  owner-mismatch (BACA NORMA ESTELA ET AL ≠ Pauline Hernandez) → GO-WITH-CONDITIONS is its correct verdict
  regardless of valuation; the pre-propose HOLD was the bug. 0-photo comps BLOCKED on confirm (422,
  mandatory-photo rule §6.3). Golden pins UNCHANGED (Tryon GO / Grant NO-GO / Ruby GO-WITH-CONDITIONS);
  00553 + a provisional-alone→HOLD case explicitly pinned.

**FLEET STATIC-FIRE (read-only, changed NOTHING) — full report:**
[`docs/fleet-static-fire-2026-07-21.md`](docs/fleet-static-fire-2026-07-21.md). Cold analysis over all
220 cases: verdicts **85% HOLD / 15% GO-WITH-CONDITIONS / GO=0 / NO-GO=0** (decision-table fix behaving —
no false GO is structurally possible cold); graduated heir gate fires **15%** (well-calibrated, not a
suspicious majority); ARV modes same-subdiv 19% / area 38% / city-fallback 9% / none 35%. Two quantified
anomalies (both = logged Stage-3 items): single-comp SNAP = **17/41 (41%)** of same-subdiv provisional
ARVs (incl. $533k @ n=1); city-fallback broad = 20 (9%).

**PENDING (do NOT start without the user raising them):**
- **60-no-GLA measurement — RUNNING IN ITS OWN SESSION** (task chip started). 60 cases lack DCAD
  `living_area_sqft` so still 422 on propose after the city fallback. Measure-then-decide: failed
  enrichment (guarded re-scrape backfill, `payment_backfill.py`/`resolve_backlog.py` pattern) vs
  legit no-GLA land/teardown (→ §G land valuation). Fold that session's result in when it lands.
  **⚠ FRAMING TO CARRY IN (from the TX-26-01190 Kemrock finding, 2026-07-23):** Kemrock is the
  BOUNDARY CASE that measurement is clustering on — a **484 sqft improvement** valued at $50,340
  against **$70,000 of land** is exactly the "near-zero improvement value" bucket, EXCEPT the structure
  is not zero and the parcel is not vacant land. It proves the **land-routing bucket must catch
  SUB-MINIMUM STRUCTURES on land-dominant parcels, not just vacant lots.** A cluster rule that only
  tests `improvement_value ≈ 0` will misfile these as "has an improvement → failed enrichment" when the
  correct routing is land valuation. Recommended cluster test: **land-dominant**
  (`land_value / market_value` over a threshold) **OR sub-minimum GLA** (below the local market's
  smallest recent sale — Kemrock 484 sf vs a 75241 floor of 870 sf), not improvement≈0 alone. Route to
  land valuation should be a FIRST-CLASS outcome of that session, not merely backfill-vs-legit. See
  `docs/acquisition-intelligence-design.md` §16.7.
- **Stage 3 — UNBUILT, its own cycle when raised.** Reframed around the **CONFIRMED OUTPUT AS AN
  APPRAISAL REPORT** (supersedes the four appraiser-grade items): (1) tiered/capped propose pool by
  MatchScore (weak collapsed) = selection not a dump; (2) reconciled 3–6 comp set → range/spread/median
  reconciliation line; (3) bracketing check + explicit can't-bracket flag; (4) per-comp adjustment grid
  at confirm = audit not trust. Time-adjustment folds in. Plus exit matrix, sensitivity, full reweighted
  Mission Score. The fleet static-fire quantified why the reconciliation line + `n=` display matter (41%
  single-comp snap).
- **August Tryon closing — fee verification.** At TX-23-00423's Aug 2026 closing, capture the title
  company's ACTUAL payoff demands; if the real §33.48 attorney fee is materially off the 20% estimate,
  recalibrate `tax_suit_atty_fee_rate` in `ACQ_CONFIG` and RE-RUN Grant St's verdict (its NO-GO is
  fee-sensitive on the margin).
- **LOGGED (Stage-3 candidate, no action) — a 'provisionally unclosable' ADVISORY flag.** The fleet
  static-fire's NO-GO=0 cold is CORRECT — `structurally_unclosable` is confirmed-valuation-gated so it
  never false-kills a deal on a noisy provisional ARV — but it means cold triage cannot surface an
  arithmetic-dead deal (provisional ARV fails every MAO rung). Candidate: a TRIAGE MARKER (never a
  verdict, never NO-GO) that flags "provisionally unclosable" when the provisional ARV can't clear the
  payoffs at any rule%, so the fleet view can prioritize; the actual NO-GO stays confirmed-valuation-
  gated. Decide during Stage 3.
- **LOGGED (enrichment gap, no action) — owner-of-record coverage is the THIRD enrichment-gap figure.**
  The heir gate can only fire where DCAD owner data exists: **131/220 (60%)**, so **89/220 (40%) of the
  fleet is BLIND to the substantive owner-mismatch check** (defaults to non-lifting → HOLD). Track it
  alongside the other two enrichment gaps surfaced by the fleet static-fire: **no-GLA 60**, **no-locality
  61**, **no-DCAD-owner-of-record 89 (40%)**. The 60-no-GLA measurement session covers the first;
  owner-of-record + locality coverage are candidates for the same measure-then-decide backfill treatment.
- **Branch:** all work on `claude/remove-analyze-with-ai-0vu5i9`; feature is ahead of main/production by
  DOC-ONLY commits (fleet report + the Stage-3/60-GLA/coverage logs) — they ride to prod on the next
  code deploy.

**SESSION DONE (2026-07-21).** Acquisition Intelligence is built, deployed (`be66c5e`), live-verified,
and fleet-validated; the corrections above are all shipped; every remaining item is LOGGED and
explicitly gated on the user raising it. Nothing is mid-flight in this session (the 60-no-GLA
measurement runs in its own session).

## SESSION HANDOFF — 2026-07-21

**ACQUISITION INTELLIGENCE layer — BUILT, DEPLOYED, and LIVE-VERIFIED end to end.** The whole
initiative (design → Stage 1 → Stage 2 engine → Stage 2 workbench → deploy) shipped this cycle under
the design-doc-first / one-proven-stage-at-a-time discipline. It is a downstream READ-mostly analysis
layer over the enrichment `property_intel` already captures — NO new court scraping. Design:
[`docs/acquisition-intelligence-design.md`](docs/acquisition-intelligence-design.md); NTREIS Phase-0
findings: [`docs/acquisition-ntreis-phase0.md`](docs/acquisition-ntreis-phase0.md). Files:
`acquisition.py` (Stage-1 pure calculators/gates) + `test_acquisition.py` (104/104), `comps.py` (NTREIS
comp engine) + `test_comps.py` (45/45 offline), `backend/main.py` (schema + endpoints) +
`backend/test_acquisition_api.py` (24/24), the frontend `Acquisition` tab. Transaction model =
pre-foreclosure, direct-from-owner: no §34.21 redemption (auction-only); countdown = existing
`oos_date`/`sale_scheduled_date`; we inherit the full lien stack (title/lien discovery is the primary
gate); heirs/estates are core pipeline.
- **Two calculators kept STRICTLY separate.** MAO = (ARV × Rule%) − Repairs (our ceiling; taxes/liens
  NOT deducted). Seller Net = Agreed Price − ACT live balance − tax-suit attorney fees − mowing/labor
  liens − seller closing (rep-facing). Fatal gate: Total Payoffs > Agreed Price → cannot close. Plus a
  `structurally_unclosable` gate (payoffs exceed MAO at every rule% → NO-GO pre-negotiation, confirmed
  valuation only) and `identified_unquantified_lien` (a known-but-unpriced lien holds a case
  INDETERMINATE — never a false GO).
- **PAYOFF MODEL (corrected + deployed).** Tax payoff = the ACT live balance (`current_tax_balance`),
  used AS-IS (already includes penalties+interest to date) — never accrued upon or fee-loaded; labeled
  verified. §33.48 accrual is a FALLBACK estimator only (no live balance). Attorney fees a SEPARATE
  estimated line. The frontend `calcPayoff` (Financials tab) was fixed to match (commit `f8188d5`) — it
  had been double-counting AND passing null for the live balance. Live-verified on prod: Tryon $71,938 /
  Grant St $152,224 as "ACT live balance (as-is) [verified]"; the Acquisition and Financials tabs now agree.
- **VALUATION HIERARCHY (locked, §5.4).** ARV from confirmed NTREIS comps ONLY. DCAD market value is
  NEVER a valuation source — only a sanity band + labeled-estimated triage placeholder. `valuation_state`
  is `confirmed` only when the ARV came from human-confirmed comps; that flips the Mission Score
  provisional→confirmed. No offer number ever rests on DCAD.
- **NTREIS = Bridge Interactive** (`api.bridgedataoutput.com/api/v2/OData/ntreis2`, bearer token). Phase-0
  live-verified: sold data available but `ClosePrice` ABSENT → reconstructed as
  `NTREIS2_RATIO_ClosePrice_By_LotSizeAcres × LotSizeAcres` (exact on ListPrice; same field is the §G
  land/teardown basis). Filter `PropertyType='Residential'` (bare Closed = leases). Photos via the `Media`
  field (hotlinked, never stored). Propose→confirm workbench: pendings are directional-only (never in ARV).
- **VALIDATION (proof before trust).** Golden pins reproduce the human verdicts on all three cases:
  TX-23-00423 Tryon → GO (closed @ $108k, seller net ~$21,674), TX-25-00249 Grant St → NO-GO
  (structurally unclosable), TX-26-01379 Ruby Faye Brown → GO-WITH-CONDITIONS held INDETERMINATE by the
  Mesquite NF SNF LLC unquantified lien. Subdivision-aware comp selection (parse_subdivision reproduces
  DCAD-verified Mountain Lakeview/Forest Grove/Walls H G) lands confirmed-comp ARVs near the human CMAs
  ($219k/$253.5k/$146k vs $225k/$265k/$110-140k) — a METHOD fix, zero adjustment-knob tuning to target.
- **DEPLOY.** feature/main/production all FF to `f8188d5`; frontend `c9adeb10…`, backend `7ef551b0…`.
  Railway env now has ACQUISITION_TOKEN + NTREIS_BASE_URL + NTREIS_SERVER_TOKEN. Endpoints live (401 not
  503). **Bridge-from-Railway CONFIRMED via a live authenticated propose on TX-26-01379** (real comps +
  arm's-length flags returned through the deployed workbench). Stage 2 is CLOSED end to end.

**OPEN / NEXT (do NOT start without the user raising them):**
- **August Tryon closing — fee verification.** At TX-23-00423's August 2026 closing, capture the title
  company's ACTUAL payoff demands. If the real §33.48 attorney fee is materially off the 20% estimate,
  recalibrate `tax_suit_atty_fee_rate` in `ACQ_CONFIG` and RE-RUN Grant St's verdict (its NO-GO is
  fee-sensitive on the margin).
- **Stage 3 — UNBUILT, its own cycle when the user raises it.** Exit matrix, sensitivity/stress testing,
  the full reweighted Mission Score (weights are tunable config with framework defaults; need sign-off +
  `n=` display before treated as authoritative).
- **LOGGED (investigation, no action taken): provisional ARV can SNAP to a single comp.**
  `comps.provisional_arv` synthesizes = median of a selected set, BUT with `prefer_same_subdivision` +
  `min_same_subdivision=1`, if exactly ONE same-subdivision comp qualifies the set is that one comp and
  median-of-one snaps to its adjusted value (**Grant St was the live n=1 case → $253,500 = a single
  Mountain Lakeview comp**). The **TX-26-01379 batch does NOT snap** — 24 qualified, 0 same-subdivision
  (WALLS H G absent in NTREIS naming) → area mode → synthesized median of 5. Mitigated already
  (provisional/triage only; confirmed ARV needs human comp confirmation; the area sanity band is always
  shown). A single-comp ARV is inherently less robust — a Stage-3/tuning candidate (e.g. require min-N
  for the same-subdivision path, or flag low-n ARVs). NOT changed.
- **LOGGED (Stage-3 design question, no action): graduate the heir/estate title gate.** Today
  `heir_estate_title` fires uniformly on estate/absentee. Consider making it GRADUATED — a soft
  *condition* when a conveyance path is identified (a named record owner exists, the Brown pattern:
  DCAD owner Taylor Felicia D is a concrete party to negotiate/quiet-title through), and STRICTER
  (blocking) only when NO record owner / no path exists (true unknown-heirs). Decide during Stage 3.
- **LOGGED (measure-then-decide, no action — same treatment as the petition_href gap): 60 cases lack
  DCAD living area.** The propose city-fallback (commit `5f5e2ca`, DEPLOYED) recovered ~20 no-zip cases
  but 60 of the 81 that would 422 have NO `property_intel.living_area_sqft` (and `total_area_sqft` does
  NOT rescue any — 0 have it), so they still fail closed (a subject needs a GLA). INVESTIGATE whether
  that's FAILED enrichment worth a guarded re-scrape backfill (like `payment_backfill.py` /
  `resolve_backlog.py`) or LEGITIMATELY no-GLA properties (vacant land, teardowns — where a §G land/
  teardown valuation, not GLA-based ARV, is the right path anyway). MEASURE first (cluster the 60 by
  city/account_status/improvement_value: near-zero improvement_value ⇒ likely land/legit; suburban/
  out-of-county ⇒ likely the known enrichment gaps), THEN decide. No action until raised.
- **LOGGED (Stage-3 scope, no action) — the CONFIRMED OUTPUT AS AN APPRAISAL REPORT** (supersedes /
  reframes the earlier four appraiser-grade items into ONE report-shaped deliverable). The confirmed
  acquisition output must read like an appraisal report whose purpose is minimizing PC Peak's RISK, the
  way an appraisal minimizes a lender's. Four concrete parts: (1) **Tiered/capped propose pool** — the
  proposed comps sectioned by MatchScore into strong/weak, weak collapsed by default, so it reads as comp
  SELECTION not a dump. (2) **Reconciled 3–6 comp set** — confirming builds toward a 3–6 comp set;
  display the confirmed set's RANGE, SPREAD, and MEDIAN as a reconciliation line. (3) **Bracketing check**
  on the confirmed set with an explicit `can't-bracket` flag (subject bracketed above AND below on
  price/GLA, or flag when not). (4) **Per-comp adjustment GRID at confirm time** — show the itemized line
  items on confirm so the click is an AUDIT, not trust (the data already exists: `comps.adjust()` returns
  `adjustment_lines`). Market-conditions time-adjustment for older comps folds in here. Build during the
  Stage-3 cycle. FLEET STATIC-FIRE (2026-07-21, read-only, changed nothing) quantified two motivations:
  the single-comp SNAP hits **17/41 (41%) of same-subdivision provisional ARVs** (incl. high-value ones
  — TX-25-00497 $533k @ n=1), and **20 cases (9%) rest on the broad city-fallback pool** — both are why
  the reconciliation-line + bracketing + n= display matter. (Cold fleet verdicts: 85% HOLD, 15%
  GO-WITH-CONDITIONS via the substantive heir gate, GO=0/NO-GO=0 — the decision-table fix behaving.)

## SESSION HANDOFF — 2026-07-18

**REP-ASSIGNMENT correctness + SELECTION-STABILITY: a chain of frontend root causes traced (not
guessed), each fixed with a regression test, all DEPLOYED fingerprint-verified.** Latest prod
`frontend/index.html` sha `dccd482b23a06ddf`; `backend/main.py` UNCHANGED at `2fe7f3c7…` (all fixes were
frontend-only); `origin/main == origin/production == origin/claude/remove-analyze-with-ai-0vu5i9` at
commit `fe00fda`. Backend `create_case` already persists `rep_assigned` correctly (proven via TestClient
+ live curl) — every bug this session was on the BROWSER side of the write/rebuild loop. Traced one at a
time under the user's "trace it, don't guess" standard:
- **Card/detail rep DRIFT + inflated count — dedup rebuild.** `syncFromPlatform` was concatenating
  platform + local copies so a case appeared twice with divergent reps (e.g. sidebar "Jay Lewis" vs
  detail "Jocelyn Cart"). Rebuild is now authoritative: `cases = platformV3.concat(drafts)` — ONE object
  per platform case_number + only genuine local drafts, deduped. Fixes the count and the drift.
- **assignRep silently not persisting — three real causes, all fixed.** (1) a no-op guard skipped the
  POST when local==target; removed. (2) POST now fires BEFORE `save()` and updates ALL copies by
  case_number, not just find-by-id. (3) a swallowed `.catch(){}` hid POST failures; now surfaced via
  `console.warn`. `test_rep_assign.py` 5/5.
- **QuotaExceededError from save() aborting the POST (a SECOND silent-persistence cause).** The full
  238-case mirror (each with a property_intel blob) exceeds the ~5MB localStorage quota; `save()` threw
  BEFORE assignRep's POST ran. `save()` rewritten crash-proof: full → slim copy (drop property_intel +
  memo) → disable cache; NEVER throws. `test_quota_save.py` 5/5.
- **Rep HEAL on sync.** For the stuck TX-26-01192 state (rep set locally, NULL on prod), sync now
  re-POSTs the local rep to fill an EMPTY prod rep (idempotent — only fills empty, doesn't re-heal once
  prod has it). `test_rep_heal.py` 6/6.
- **Sidebar chip didn't appear until you opened the detail — renderList after assign.** assignRep now
  re-renders the card list so the rep chip shows immediately at a glance. `test_rep_sidebar.py` 9/9.
- **Malformed property_intel could FREEZE renderList.** `caseTrack` + 4 siblings did unguarded
  `JSON.parse(property_intel)`; one truncated record threw and killed the whole render. New `parseIntel()`
  helper returns `{}` on bad JSON, never throws; all 5 sites routed through it.
- **Sync silently SWITCHING the viewed case out from under the user.** During the slow ~238-request
  event-fetch window the user would navigate case A→B, then the completing sync re-selected the STALE
  captured case. Fix: after rebuild, re-point `activeId` at the still-valid deduped object for the case
  the user is CURRENTLY on (prefer current `activeId`, fall back to `selectedCn`) — never snap back.
- **Sync RESETTING the active tab/scroll (Timeline→Overview every ~30s).** The case-switch fix still
  called `selectCase()` on sync, and `renderDetail` rebuilds the panel with Overview as default. Fix:
  REMOVED the sync-time detail re-render entirely — the sidebar refreshes via `renderList`, the open
  detail is left untouched by syncs. `test_selection_stability.py` 7/7 (asserts B stays open across a
  mid-sync navigation AND the Timeline tab survives a sync).
- **Underlying load cause noted, NOT yet built:** `syncFromPlatform` does 1 `/api/cases` GET + ~238
  sequential `/api/events/{cn}` GETs — that burst is what produces the 502 spikes and the multi-second
  navigation window the selection bugs exploited. **Recommended future optimization: batch events into
  `/api/cases`** (one payload) to kill the burst. Deferred, not urgent.
- All frontend suites green: rep_dedup 9/9, rep_assign 5/5, rep_heal 6/6, quota_save 5/5, rep_sidebar
  9/9, selection_stability 7/7, plus the earlier petition_link 17/17 / petition_backfill 10/10 and the
  Phase-0 backend/held/worker/ledger suites. Every fix `node --check`-clean, zero pageerror.

**NEXT INITIATIVE — ACQUISITION INTELLIGENCE layer (DESIGN-DOC-FIRST; full brief arrives in a FRESH
session; NO code until the design is approved).** A major new downstream subsystem — same process
discipline as the prediction-ledger and scrape-trigger builds (design doc → explicit approval → build).
Scope, as briefed: a downstream ANALYSIS layer that READS the enrichment property_intel.py already
captures and produces acquisition decisions — Mission Score, MAO, exit-scenario modeling, deal-killer
gates — surfaced as a NEW case-detail tab. **It is NOT a patch to discover.py and involves NO new
scraping.** Transaction model: buy pre-foreclosure directly from owners/heirs BEFORE any sheriff's sale,
so no §34.21 redemption applies to us; countdown is the existing `oos_date`/`sale_scheduled_date`; we
inherit the seller's FULL lien stack (no tax-sale lien wipe); heirs/estates are core pipeline. Two
calculators kept STRICTLY separate: MAO = (ARV × Rule%) − Repairs (taxes/liens NOT deducted); Seller Net
Sheet = Agreed Price − tax payoff − lien payoffs − seller closing costs; fatal gate: Total Payoffs >
Agreed Price → cannot close. Comp engine on MLS/NTREIS (key already in `Anthropic_API_KEY.env`, zero
marginal cost, first-class/always-available) with appraiser-grade qualification; ALL adjustment figures
as named TUNABLE config with Dallas defaults (never hardcoded); MLS photos render inline with every comp
(mandatory); flow is propose-confirm; Mission-Score valuation confidence provisional vs confirmed;
condition-evidence + verified/estimated/inferred labels everywhere. VALIDATION is non-negotiable: run the
full analysis against known cases (TX-23-00423 Tryon — $71,938 owed / $217,800 MV; Ruby Faye Brown's
case; TX-25-00249) and confirm outputs match human analysis. Design-doc deliverable must cover
architecture, the NTREIS integration approach (CONFIRM what the API actually returns before designing
around it), comp storage/confirmation-state schema, the UI surface, and the validation plan — with every
open decision flagged explicitly.
- **EXPLICITLY OUT OF SCOPE — tracked as a SEPARATE future initiative, NOT part of Acquisition
  Intelligence:** non-suit back-tax lead discovery (DCAD-only discovery, no court case number). Do not
  fold it into the Acquisition Intelligence design.

## SESSION HANDOFF — 2026-07-15 (later still)

**Phase 0 DEPLOYED (fingerprint-verified) + two READER tools built to actually query the live ledger
data — the "prove it's useful before more architecture" step.** Deploy: feature→main (FF)→production
(merge `3e1d317`); prod `backend/main.py` sha `2fe7f3c7…`, `index.html` unchanged `2cc38058…`,
`origin/main == origin/production`. INERT except the one intended effect: init_db's one-time baseline
backfill seeds ~127 `source='baseline'` case_snapshots rows on first boot (append-only, additive; no
case data or display changes). Diff-on-write only fires on future syncs (still gated by approval + the
offline worker). Live click-through is the USER's check (sandbox egress 403-blocks Railway).
- **`evidence_gaps.py` (LOCAL, reads the token-gated export; no deploy).** The derivation-bug detector
  case_snapshots was built to enable: surfaces every status change with a NULL evidence link. Counts
  ONLY `source='update'` rows on EVIDENCE-eligible fields (oos_date/oos_issued/judgment_*/sale_*) with
  no docket line — baseline/initial genesis (NULL by nature) and non-evidence fields (total_due, etc.)
  are excluded, so it's signal not noise. Imports `EVIDENCE_KEYWORDS` from backend (single source, no
  drift). `--file <dump.json>` for offline use against a backup_ledger dump.
- **`ledger_scorecard.py` (LOCAL, reads the export; no deploy).** REAL predicted-vs-actual from the
  prod prediction_ledger AS LOGGED+RESOLVED (not scorecard.py's local-DB recompute): resolved/open,
  outcome mix, OOS-issued accuracy (mean/median abs err, within 30/60/90, signed bias) overall + per
  basis + per model_version, with honest n= and the frozen-until-≥40 caveat. `--file` too. NOTE:
  scorecard.py (local recompute of the CURRENT model) is KEPT — different, complementary purpose.
- Both accept `--file` so they're testable offline (sandbox can't reach prod). **`test_ledger_tools.py`
  21/21** (gap classification: genesis/non-evidence excluded, only NULL-evidence status-updates flagged;
  accuracy math pinned on a fixture). All 12 suites green. Tools are LOCAL (pull to Mac); need
  `LEDGER_EXPORT_TOKEN` set to hit live prod. **Sequencing:** user drives — after using these for real,
  they'll raise Phase 1 (per-case Refresh) as its own decision; Phase 2 (scheduled sweep) stays parked
  until Phase 1 is proven under manual use. Deploy vs architecture = separate, explicitly-labeled asks.

## SESSION HANDOFF — 2026-07-15 (later)

**CASE-FIELD HISTORY — Phase 0 (`case_snapshots`) BUILT + tested, on the feature branch, DEPLOYED
2026-07-15 (`3e1d317`, fingerprint-verified).** Confirmed the telemetry gap
first by tracing code: a re-scrape/re-sync overwrites a case's fields IN PLACE with no history —
`discover.save_to_db` (SELECT id → blind `UPDATE cases SET <all fields>`), the enrichment UPDATEs
(incl. `property_intel` wholesale, which nests the DCAD year-tables), and `create_case` (merge →
UPDATE). Sweep for the pattern: the ONLY other wholesale-replace is `held_cases` (intentional display
mirror, benign); `docket_events` is append (INSERT OR IGNORE); `prediction_ledger`/`rep_actions` are
the append-only template. So the gap is real and isolated to the case-row update.
- **`case_snapshots` in `ledger.db`** (append-only, added to `PROD_OWNED_TABLES` so the get_db
  authorizer denies DELETE/DROP — same restore-guard as prediction_ledger; a raw pcpeak.db restore
  can't touch it). NARROW shape: one row per changed field, `old_value→new_value`, grouped by a
  per-write `batch_id`, timestamped, `source` = baseline|initial|update, `model_version` stamped.
- **Diff-on-write in `create_case` only** (the WRITE path, like log_prediction) — allowlist of material
  fields (excludes churn/display: updated_at, confidence_pct, projected_oos). `property_intel` is NOT
  snapshotted raw: captured as sub-values `pi_market_value` + `pi_tax_balance` (the live balance) + a
  content `property_intel_hash` (excludes enriched_at/errors so a re-enrich with no real change is a
  no-op). Wrapped defensively — snapshot logging can never break a case write.
- **Three requirements folded in (as asked):**
  1. **Genesis baseline for the existing book** — `init_db` one-time seeds a `source='baseline'` genesis
     for every already-live case (idempotent: only cases with 0 snapshots), so history doesn't start
     blank for the 127 live cases.
  2. **Capture boundary stated precisely** — history is at **create_case (sync) granularity**, NOT every
     local re-scrape. A status change made and reverted locally between syncs, or an intermediate value
     never synced, is never seen (A→C, never A→B→C). This is inherent to capturing at the prod write
     path (the approved design); local pre-sync churn was never "said" to anyone.
  3. **Evidence link to raw docket line** — a status-field change (oos_date/oos_issued/judgment_*/sale_*)
     resolves to the `docket_events` row that caused it (by date + keyword), storing `evidence_event_id`
     + a durable `evidence_desc` copy. To make this resolvable at WRITE time, **sync_to_prod now pushes
     events BEFORE the case** (FK enforcement is off; additive/idempotent — safe). KEY: a **NULL evidence
     link on a status change is itself the signal** that the value moved on unchanged raw data — a
     derivation bug, distinct from a real new event — exactly the two failure modes we've hit before.
- **Also:** `GET /api/cases/{cn}/snapshots` (open read — case facts, not rep PII; returns rows + a
  grouped `latest_batch` = "what changed", ready for Phase 1's Refresh diff); `/api/ledger/export` +
  `backup_ledger.py` extended to carry case_snapshots under the same fingerprint standard.
- **Tests: `backend/test_case_snapshots.py` 22/22** (genesis/initial + baseline backfill + idempotent,
  narrow shape + batch grouping, no-op write, REAL-vs-int normalization, property_intel sub-values+hash
  with enriched_at no-op, evidence present vs NULL, the A→C boundary, append-only DELETE denied, export).
  `test_restore_guard` auto-rose 19→23 (it iterates PROD_OWNED_TABLES → now guards case_snapshots too).
  All 11 suites green. **NOT DEPLOYED** — Phase 0 is on `claude/remove-analyze-with-ai-0vu5i9` for review;
  deploy + Phase 1 (per-case Refresh) / Phase 2 (scheduled sweep) are separate, explicitly-gated steps.

## SESSION HANDOFF — 2026-07-15

**HELD-FOR-REVIEW browser view BUILT (Option A) — approve + publish a scraped case from the site,
without ever putting held/unpublished data on prod.** The scrape trigger fills a held queue
(prod_ready=0) but there was no in-browser way to approve. Built the missing piece with the SAME
architecture as the trigger — the browser NEVER touches prod case data; it only asks the Mac worker
to publish an already-held case via the real `sync_to_prod.py --approve`. **Why Option A (route
through the worker), not "list cloud prod_ready=0":** held cases live ONLY on the Mac — a held case
is never synced up, and prod's `prod_ready` is LOCAL-only (in SKIP_CASE_FIELDS, never sent), so it's
uniformly 0/meaningless on cloud. The literal "show prod_ready=0 on cloud" spec structurally can't
work; Option B's "remember to filter prod_ready=0 everywhere" is the exact fragile pattern the
prod_ready gate was built to kill. So the worker MIRRORS its held set up for display only.
- **Backend (`backend/main.py`, served):** two preview/liveness tables — `held_cases` (a display-only
  mirror the worker full-replaces; PK case_number + a few preview fields, NO full case data/events)
  and `worker_state` (heartbeat liveness). Endpoints: `POST /api/worker/heartbeat` (worker token) +
  `POST /api/held/sync` (worker token, FULL REPLACE so a just-approved case drops off) + `GET /api/held`
  (trigger token → held rows + worker online/offline + per-case `approving` inflight flag) +
  `POST /api/held/{cn}/approve` (trigger token). **Approve ONLY ever flips prod_ready + publishes,
  never creates data:** it 404s if the case isn't in the held mirror, then just enqueues an
  `{"approve": CN}` job (deduped in-flight, capped by the shared MAX_QUEUED). Both roles fail-closed
  (503 unset / 401 wrong role), same two tokens as the trigger.
- **Worker (`scrape_worker.py`, LOCAL):** ONE queue, dispatched by request shape — `{"approve": CN}`
  runs `run_approve` = the real `sync_to_prod.py --approve CN --only CN` (flip prod_ready=1 + push ONLY
  that case; no data created); everything else scrapes via discover.py. Every poll POSTs a heartbeat;
  on startup + after every successful job it POSTs the current LOCAL held set (`local_held_cases`, a
  predicate that mirrors `sync_to_prod.pending_cases` EXACTLY) to `/api/held/sync`, so the browser list
  IS precisely what `--approve` would consider. Held-mirror refresh is best-effort (never flips a job's
  real outcome — same `terminal_sent` discipline). Overridable via `SCRAPE_SYNC_CMD` for tests.
- **Frontend (`frontend/index.html`, served):** a "Held for Review" sidebar section — lists held cases
  with address/defendant/due, a worker ●online/●offline indicator (offline tells the rep to start the
  worker + that approvals queue), and an "Approve → publish" button per case. Approve POSTs, reflects
  `approving…`, polls the job to done, then refreshes both the held view (case drops off) and the case
  list (now live). Silent auto-load every 15s (never prompts for the token on page load; only explicit
  actions prompt). Reuses the trigger's sessionStorage token.
- **Worker-liveness concern factored in (as asked):** the whole feature depends on the worker running,
  so the UI surfaces it — heartbeat drives an online/offline dot, and an approve while offline still
  queues (processed when the worker returns), it doesn't silently fail.
- **Tests (all isolated, no network): `backend/test_held_review.py` 25/25** (fail-closed auth + both
  roles, full-replace sync, liveness online/offline, approve-404-when-not-held / enqueue / dedup / cap /
  claim+done), **`test_scrape_worker.py` 39/39** (+approve dispatch to approve_fn not discover, run_approve
  cmd shape, held-mirror refresh on success only, local_held_cases predicate excludes approved/BPP/unknown,
  sync_held payload), **`test_held_browser.py` 9/9** (Chromium: renders held + online dot, Approve →
  POST + poll + drop-off + syncFromPlatform, ZERO pageerror). All 10 suites green (219 checks).
- backend/main.py is a SERVED artifact (deploys on the next `production` merge — inert until the 2
  Railway tokens are set, already-set from the trigger). scrape_worker.py/sync_to_prod.py are LOCAL
  (pull); the "Held for Review" UI is a FRONTEND change (next deploy). ⚠ **Live end-to-end is the
  USER's check** (browser→approve→Mac worker→sync_to_prod→case live): needs the deploy + `scrape_worker.py`
  running on the Mac. Sandbox egress 403-blocks Railway, so no live click-through from here.

## SESSION HANDOFF — 2026-07-14

**DESIGN PRINCIPLE (stated, alongside fingerprint-proof + prod_ready) — DISCOVERY CAPTURES THE FULL
PICTURE BY DEFAULT; NARROWING IS A DELIBERATE, VISIBLE OPT-IN.** The discovery/search gate must never
silently exclude the mission data. Every OOS-issued case and every dismissed-owing lead is CLOSED, so
`discover.py`'s old `open_only=True` default (exclude closed unless `--include-closed`) structurally
blinded routine scrapes to BOTH core moats — and the scrape trigger had NO way to override it at all
(frontend hardcoded `individuals_only:true`, no `include_closed` field on `ScrapeJobIn`, worker never
passed `--include-closed`), so once Step 5 opens the site to reps, nobody could ever capture an OOS or
dismissed-owing case through the button. Same class as the prod_ready gate: a default that quietly worked
AGAINST the mission, now made structurally correct instead of patched. **INVERTED (Option 1):**
- `discover.py` default is now `open_only=False` (**include closed**). `--open-only` is the deliberate
  narrow-mode opt-in (fast lead-gen, skips the moat); `--include-closed` kept as an accepted no-op
  (backward-compat + scorecard's printed hint). `__init__` default flipped to `False` too.
- Trigger exposes BOTH filters as VISIBLE checkboxes (Load New Deals), each **checked by default**:
  "Include closed cases (OOS · dismissed-owing)" and "Individuals only (skip businesses)". No invisible
  default is acceptable even when the default VALUE is reasonable — individuals-only stays default-on but
  is now visible/overridable. `ScrapeJobIn.include_closed: bool = True`; worker adds `--open-only` only
  when a request opts out. A `--case` scrape hits that case regardless of status (unaffected).
- **Cost tradeoff (accepted):** default-include-closed makes a broad pattern scrape heavier (more portal
  hits + Claude + enrichment). Mitigated by skip-existing / already-complete-today, the MAX_QUEUED cap,
  and the visible "open-only" checkbox for when speed matters — mission-completeness is not something a
  rep has to remember to opt into. **Pre-flight: `discover.py --pattern X --count-only`** searches +
  paginates + COUNTS only (no docket/Claude/enrichment; doesn't even need ANTHROPIC_API_KEY) → reports
  "Found N | would process W (with current flags)" so you know the scale before committing to a run.
- **Sweep:** this silent-exclusion pattern lived at the discovery gate in TWO spots (`open_only` +
  `skip_biz`/`individuals_only`); both are now visible opt-ins. Clean elsewhere — the case-list API's
  `case_status` filter is opt-in (returns all by default), and sync's `SYNCABLE_WHERE` is the deliberate
  documented prod_ready/BPP gate. The capture layer (`oos_date`/`sale_pulled_date`/`case_track`) was NOT
  touched — it was already proven correct; the gap was purely the discovery gate one level upstream.
- **Tests: `test_discover_filter.py` 12/12** (+ default open_only False), **`test_scrape_worker.py` 25/25**
  (+ build_discover_args: default no `--open-only`, opt-out adds it), **`test_scrape_jobs.py` 32/32**
  (+ include_closed default True, pattern request stores the choice), browser check (both checkboxes
  render default-checked; default body `include_closed:true`, unchecked `false`; zero pageerror). All 9
  suites green. discover.py/scrape_worker.py are LOCAL (pull); the trigger checkboxes are a FRONTEND
  change (next deploy). ⚠ Do NOT let this regress to open-only-by-default — it re-blinds the moat.

**Scrape SUMMARY skip-buckets split (honest labels) + worker double-terminal bug fixed.** A `--case
TX-25-00249` run showed "0 found → 1 business/skip · 0 saved (held)" over a card showing the case as
held — a contradiction. Root cause (same class as partition_page): the `--case` path never set
`stats["found"]`, and the ALREADY-COMPLETE-TODAY skip was lumped into the same generic `stats["skipped"]`
bucket as business exclusions, so a REUSED case read as "business/skip". Fixed at the root: split
`skipped` into distinct buckets — `business` (excluded), `reused` (already captured today, not
re-scraped), `skip_existing` (--skip-existing) — everywhere (process_one_case + pattern partition +
--case now sets found); reconcile is now `found = processed + reused + skip_existing + business + closed
+ errors`; SCRAPE_SUMMARY carries the distinct fields; the trigger UI shows "1 found → 1 already captured
· 0 new (held)" + a "already captured and reused" note so a held card never contradicts a "0 new" line.
**Bonus bug found + fixed:** renaming the summary field crashed `scrape_worker.process_one`'s done-log on
`summary['skipped']` KeyError AFTER the done patch was sent → the except caught it and wrongly re-reported
the job `failed` (done→failed flip). Fixed with defensive `.get()` on all summary reads + a `terminal_sent`
guard so a post-terminal exception can never overwrite the real outcome (a logging detail must never flip a
succeeded job). **Tests: `test_scrape_worker.py` 27/27** (+parse_summary distinct buckets, +post-terminal
error keeps DONE), browser check (reused renders honestly, no contradiction, zero pageerror), all 9 green.
discover.py/scrape_worker.py LOCAL (pull); the renderScrapeResult relabel is a FRONTEND change (next deploy).

**Scrape-filter STATS now RECONCILE + the breakdown is surfaced (silent-number-gap fixed).** A
`--pattern TX-23-004 --individuals-only` trigger run reported `Found: 110, Processed: 0, Skipped: 2`
— 108 CLOSED cases were dropped by the default open-only filter WITHOUT being counted (the business
filter counted its drops; the closed filter didn't), so Found never reconciled. NOT a scrape gap
(pagination walked all 11 pages; Found 110 == manual portal count) — purely the filter shedding rows
uncounted, which violated the "stats must add up" standard. Fixes: (1) extracted a pure
`discover.partition_page(rows, open_only, skip_biz)` that COUNTS every drop → `{targets, closed,
business}`, used in BOTH the pattern and `--name` paths (same root cause swept), with a new
`closed_skipped` stat; the COMPLETE summary now prints a `Closed:` line + a `reconcile:
Processed+Skipped+Closed+Errors == Found` check, plus a machine-readable `SCRAPE_SUMMARY {json}` line.
(2) `scrape_worker.parse_summary()` reads that line into the job `result.summary`; the trigger UI's
`renderScrapeResult` shows the breakdown — "**110 found → 108 closed · 2 business/skip · 0 saved
(held)**" — so a rep sees where the other 107 went instead of a bare "3 cases". Also aligned the
`--name` open-filter predicate to keep blank-status rows as open (matching the pattern path).
**Tests: `test_discover_filter.py` 11/11** (partition_page reconciles on every branch: open+indiv,
include-closed, no-filter, name-mode, empty), **`test_scrape_worker.py` 22/22** (+parse_summary +
result carries the breakdown), browser check (breakdown renders incl. the 0-saved explanatory line,
zero pageerror). All 9 suites green. discover.py/scrape_worker.py are LOCAL tools (pull, no deploy);
the `renderScrapeResult` breakdown is a FRONTEND change — shows on the live site after the next deploy
(degrades gracefully: no `summary` → old behavior).

**`purge_test_case.py` — the STANDARD guarded cleanup for stub/test cases (use it, don't hand-roll a DELETE).**
Test scrapes (the scrape-trigger stub, any throwaway `TX-99-xxxxx`) leave a fake case in the held queue.
This deletes ONE entirely (its `cases` row + `docket_events`) but ONLY if it is a true throwaway — same
"constraint a real row fails" discipline as the BPP delete guard: REFUSES if the case is (a) live on prod,
(b) approved (`prod_ready=1`), or (c) has real content (`property_address`/`ai_memo`) — so a real case, even a
genuinely-held local lead, can't be nuked; the constraint is also carried in the DELETE's WHERE clause
(`prod_ready IS NOT 1`, DB-level not caller-trusted). LOCAL cleanup only — it fetches prod's case list purely
to refuse on-prod cases, never writes to prod. `python3 purge_test_case.py TX-99-00001` (`--dry-run` to preview
the decision). Testable pure core (`is_throwaway`/`purge` with an injected prod set); **`test_purge_test_case.py`
23/23** (no network: only the contentless local-only held stub deletes; on-prod / approved / has-content /
absent all refused; isolation — a real case's row+events survive; `--dry-run` writes nothing).

**'Add Case Manually' (quick-add) REMOVED — governed Scrape trigger makes it redundant + it FABRICATED data.**
Same ungoverned-localStorage anti-pattern as Analyze-with-AI (never POSTs; bypasses the pipeline/prod_ready
gate; `Date.now()` ids), AND worse: it stamped hardcoded plausible-looking IDENTITY defaults onto every
manual case — `judicialOfficer:"GINSBERG, CARL"`, `lawFirm:"LGBS (Linebarger)"`, `plaintiffAttorney:"ATKINS,
ASHLY STEELE"`, a guessed benchmark, `abstractorFee:350`. Stripped the whole section + `doQuickAdd`/`checkQuickReady`
+ orphaned `// INIT` header; updated the prune comment; fixed the detail-card `Input: ${inputMethod}` →
`Source:` (would now render "undefined"). Verified in Chromium: quick-add gone, Scrape trigger + case list
intact, zero pageerrors. **⚠ AUDIT (the fabricated-default pattern is NOT isolated — a real recurring shape,
mostly around `law_firm`/LGBS; decision pending, NOT yet fixed):**
- `discover.py:301` — `extracted.get("lawFirm","LGBS")` **PERSISTS "LGBS" to the DB** (→ prod on sync) when
  extraction returns no firm. Its siblings do it RIGHT (`judicial_officer`/`plaintiff_attorney` default `""`).
  Highest severity — a fabricated firm is written as if real. **FIXED → `""`.**
- `discover.py:149` — the EXTRACTION_PROMPT JSON scaffold pre-fills `"lawFirm":"LGBS (Linebarger)"` (other
  fields `""`) → primes Claude toward LGBS. Reinforces the save-time default.
- `frontend platformToV3:503` — `pc.law_firm || "LGBS (Linebarger)"` **display-fabricates** the firm when the
  backend value is null (siblings correctly `|| ""`).
- Fee-constant class (separate category — payoff MODEL assumptions, not identity): `abstractorFee 350`,
  `courtCosts 450`, `postJudgment 500` feed `totalPayoff` uniformly for every case.
- NOT the bug (verified, legitimate): `CITY_DATA.firm` (per-jurisdiction reference metadata), the `KNOWN`
  benchmarks (real values for real confirmed cases), the `§33.48`/"Dallas County LGBS" display labels.
- **FIXED (2026-07-14): all 3 law_firm sites default to `""` now, matching `plaintiff_attorney`/`judicial_officer`.**
  No-regression on real LGBS cases verified: the save + display sites are pass-through by construction (a present
  "LGBS (Linebarger)" is preserved; only ABSENCE becomes ""), and a Chromium spot-check of platformToV3 fed
  TX-23-00379 (real LGBS) + a null case confirmed LGBS preserved / null→"" (no fabrication), zero pageerrors.
  ⚠ The extraction-PROMPT site can't be live-verified from the sandbox (no API key + portal egress blocked) —
  but the sibling `plaintiffAttorney` is the proof-of-mechanism (identical empty-scaffold treatment, no rule,
  yet the KNOWN benchmarks show it extracting DIFFERENT real values ATKINS vs ZOKAIE = genuinely read from the
  doc, not defaulted). **USER's live check after deploy: re-scrape TX-23-00379 or TX-23-00423 and confirm
  law_firm still shows LGBS** (it's a real, common Dallas answer — must come from the doc, and now does).
- **TRACKED FOLLOW-UP (LOW priority — do NOT build now; decided 2026-07-14):** the fee constants
  `abstractorFee 350` / `courtCosts 450` / `postJudgment 500` (frontend `equity()`/payoff calc ~894, rendered in
  the payoff table ~1293–1306) feed `totalPayoff` uniformly. This is a MODELING-ASSUMPTION-WITHOUT-A-LABEL
  problem, NOT identity fabrication — so the fix is a UI LABEL, not a data change. **Do NOT remove or zero them**
  (that makes the payoff estimate worse). Scope: label them clearly in the payoff UI as estimates — e.g.
  "Estimated fees — typical Dallas range" — so they read as an estimate, not case-specific fact. Small, cosmetic,
  no calc change. Sequenced AFTER: law_firm fix (DONE) + the user's live scrape-trigger verification.

**FRONT-END SCRAPE TRIGGER — BUILT + isolated-tested (integration is the USER's live check).** A
button on the live site to scrape a new case/pattern, calling the REAL `discover.py` CLI (never a
reimplementation) so it inherits every guardrail — document selector, corroboration guard, BPP detection,
`prod_ready` default-held — automatically. Analyze-with-AI's lesson applied in reverse: a governed trigger,
not a competing tool. **Architecture (cloud can't scrape; the Mac can't be reached inbound):** inversion of
control — the browser ENQUEUES a job in a cloud `scrape_jobs` queue; `scrape_worker.py` on the Mac POLLS it
OUTBOUND, claims a job (atomic `UPDATE…RETURNING`, no double-grant), runs `python3 discover.py --case/--pattern`
locally, and PATCHes status + a result snapshot back. Nothing connects INTO the Mac. State machine
queued→claimed→running→done|failed; the worker shells out to the CLI so ZERO logic is duplicated. **Scraped
cases land HELD (prod_ready=0)** — the trigger fills the review queue, never auto-publishes (approve+sync to
publish). **Access control:** two fail-closed tokens (ledger-export pattern) — `SCRAPE_TRIGGER_TOKEN` (enqueue
= who spends credits/hits the portal) + `SCRAPE_WORKER_TOKEN` (claim/patch = who drains the queue); 503 if
unset; enqueue deduped (in-flight-only) + capped (MAX_QUEUED_SCRAPES=20). Trigger token lives in the browser's
`sessionStorage` (cleared on tab close — the Analyze-with-AI key-exposure lesson). **Replaced the dead
`/api/agent/run-case`** (it ran `subprocess discover.py` INSIDE the Railway container — no browser there, could
never work) and removed its orphaned `triggerCaseRun`/`triggerAgent` frontend JS. **The two Run-button bugs
were designed against + regression-tested, not assumed-fixed:** the `finished_at`/`completed_at` column class
(backend test drives every transition and asserts the real columns are stamped) and the `syncPlatform()` typo
(browser test asserts job-completion refreshes via the real `syncFromPlatform()` with ZERO pageerror); plus the
worker is a separate process that reports failures explicitly instead of dying silently in a daemon thread.
**Tests (all isolated, no network/portal/credits): `backend/test_scrape_jobs.py` 29/29** (fail-closed auth,
both token roles, validation, dedup, cap, atomic claim, full state machine + column assertions), **`test_scrape_worker.py`
18/18** (process_one running→done/failed, exception-never-crashes, real subprocess round-trip via a stub discover
cmd + snapshot), **browser async-flow** (queued→running→done→preview→refresh, no JS error). All 4 prior backend
suites + prod_ready gate still green. **NOT deployed / NOT live-verified from here** — the user runs the real
end-to-end (browser→queue→Mac worker→real scrape→held result): see `docs/scrape-trigger-runbook.md` (local-first
option needs no deploy; live needs the `git merge main` deploy + the 2 Railway env tokens + `python3 scrape_worker.py`).
The endpoints ship fail-closed so deploying is inert until tokens are set. Code on `main` + the feature branch.

**#3 RESOLVED — 'Analyze with AI' client-side path REMOVED + deployed. Step 5 is UNBLOCKED.**
The ungoverned in-browser Analyze-with-AI path (user pastes docket + enters their OWN Anthropic key
in localStorage → direct api.anthropic.com calls from the browser → localStorage-only draft, never
POSTs to prod, bypasses BPP detection / document-selection / enrichment / corroboration guard; its
Date.now-id drafts were the root cause of the earlier stale-count bug) was **removed, not gated** —
inappropriate to leave running as Step 5 opens the platform to real human input. Scoped exhaustively
first, confirmed nothing else depended on it, then stripped end-to-end from `frontend/index.html`:
API-key input UI + saveKey/clearKey/loadKey; PDF/Paste/Both input modes + upload zone + file chips +
the input-method toggle (existed only to feed the analyze call); `doAnalyze()` and its two direct
api.anthropic.com fetches; the loading overlay (`#lov`) + its helpers (only doAnalyze used them); all
path-exclusive CSS. **Kept** the independent Quick-add manual-entry path (no key, no Anthropic call) —
now the sole entry under the section (relabeled "Add Case Manually", always visible). Diff: 1 file,
+4/−358. Verified in Chromium (pinned to the pre-installed browser): page loads with zero JS errors,
all removed markers gone, quick-add adds a case end-to-end (localStorage 0→1, `inputMethod:"quick"`).
**Deployed via cherry-pick** (commit `f1dbaac` on `production`, matching every prior "Deploy …"
cherry-pick) — clean, frontend-only, zero backend blast radius. ⚠ **LIVE click-through NOT done from
this session** — the sandbox's egress policy 403-blocks outbound to the Railway host, so
taxforeclosureanalyzer.com couldn't be reached to confirm Railway's build/serve. The deployed
artifact is byte-identical to the verified local file; still, a human should load the live site once
to confirm it serves clean (no console errors, quick-add works).

**`main`↔`production` DIVERGENCE — RECONCILED (verified lossless + runtime-neutral).** The scary
framing (37 commits / 902 insertions / 3 conflicts) was real at the HISTORY level but the AUDIT showed
the divergence was entirely NON-RUNTIME: `git diff origin/production origin/main` for **`backend/main.py`
is EMPTY** (the deployed server was already in sync via prod's bundled "Deploy …" cherry-picks) and
**`frontend/index.html` was byte-identical** (this session's cherry-pick synced it). The only real deltas
were docs (`CLAUDE.md`, design doc, `.gitignore`), tests, LOCAL-only tools (`discover.py`/`property_intel.py`/
`sync_to_prod.py`/`backup_ledger.py`/`payment_backfill.py`), and `.DS_Store`/`.pyc` cruft — none served by
Railway. Reconciled with ONE deliberate merge (commit `03915ad` on `production`): the 2 remaining conflicts
(`CLAUDE.md`, `discover.py`) resolved by taking main (verified a strict superset — prod's BPP funcs + doc
sections all already in main), everything else auto-merged. **Fingerprint-verified (the ledger.db/git-purge
standard):** served artifacts unchanged before→after — `backend/main.py` `b702bbb8…`, `frontend/index.html`
`003f951d…` on prod == main == reconciled; and **reconciled tree == origin/main exactly** (nothing lost,
nothing extra). Result: `git diff origin/main origin/production` is now EMPTY — the documented `git merge
main` deploy gate works cleanly again; future deploys carry doc/tool catch-up automatically, no more
cherry-pick gymnastics. (Railway redeployed the identical runtime; a human should still glance at the live
site — this sandbox's egress policy 403-blocks the Railway host so no click-through was possible here.)

**`prod_ready` GATE BUILT — the structural fix for the 36-case premature-sync incident ([[sync-approved-for-prod-gate]]).**
`sync_to_prod.py` now pushes a case ONLY if `cases.prod_ready=1`. New scrapes default to `0` (held —
`discover.py` never sets it, and its SELECT-then-UPDATE preserves the flag across re-scrapes), so a routine
sync can NEVER silently promote work-in-progress leads — not "remember to hold back," but structurally
impossible. Approval is explicit: `--approve "<case,…>"` (revoke `--unapprove`, inspect `--pending`); a case
already LIVE on prod is implicitly approved (reconciled to 1 at run start, so `--update-existing` keeps
working for public cases). ONE source-of-truth predicate `SYNCABLE_WHERE` (prod_ready=1 + the existing
BPP/unknown exclusions) that every push path (default / `--update-existing` / `--only`) runs through — no
bypass. `prod_ready` is LOCAL-only (added to `SKIP_CASE_FIELDS`, never sent up); `ensure_schema()` self-heals
the column on a pre-migration local DB. Migration added to `backend/main.py` init_db (`INTEGER DEFAULT 0`,
verified idempotent + new-row default 0 against a real DB). **`test_prod_ready_gate.py` 21/21** (pure-DB, no
network: held-never-syncable, default-held, BPP-still-excluded-when-approved, reconcile, approve/revoke,
pending, + a 34-held/2-approved incident simulation). All 4 existing backend suites still green (11/19/17/9).
The gate works locally immediately; the `backend/main.py` column is dormant on prod (prod doesn't use it) and
will reach prod harmlessly on the next `git merge main` deploy — no separate deploy needed.

**PRE-STEP-5 SEQUENCING (per user, 2026-07-14): both blockers now DONE — divergence reconciled, prod_ready
gate built. Step 5 (rep_actions API+UI) is next.** Reminder: Step 5 opens the platform to real,
non-regenerable HUMAN input — start it fresh with the same checkpoint discipline (ledger.db / git-purge).

## SESSION HANDOFF — 2026-07-13

**Prediction-ledger build (design [`docs/prediction-ledger-design.md`](docs/prediction-ledger-design.md),
6-step order §12): Steps 1–4 DONE, tested, and LIVE ON PROD. NEXT is Step 5 (rep_actions API+UI) —
gated and ready to start FRESH.** Step 5 opens the platform to real, non-regenerable HUMAN input for
the first time; start it in a clean session (same checkpoint discipline as ledger.db / the git purge).

**Ledger build shipped this session (Steps 1–4):**
- **Step 1 — restore guard (structural).** prod-owned tables (`prediction_ledger`, `rep_actions`)
  live in a SEPARATE file `data/db/ledger.db`, ATTACHed as schema `ledger`; a raw `pcpeak.db`
  dump/restore physically can't touch them, and a get_db() authorizer denies DELETE/DROP (append-only).
  `backend/test_restore_guard.py` 19/19.
- **Step 2 — schema + constants.** `prediction_ledger` (24 col) + `rep_actions` (11 col) in ledger.db
  + indexes; `deal_status`/`last_action_at` cache cols on `cases`; `MODEL_VERSION` +
  `PREDICTION_EXPIRY_DAYS=90` next to CITY_DATA.
- **Step 3 — log_prediction + reconcile in create_case (WRITE path only, never reads).**
  compute_projection returns `basis`; log-on-meaningful-change (input_hash over DRIVERS, not
  confidence/stage); reconcile → signed error_days; sweep_expired → expired_no_oos. test_ledger.py 17/17.
- **Step 4 — backup + LEDGER BACKEND DEPLOYED LIVE.** Token-gated `GET /api/ledger/export`
  (`X-Ledger-Token` vs `LEDGER_EXPORT_TOKEN`, constant-time, **fail-closed** 503 if unset — rep_actions
  is sensitive). `backup_ledger.py` pulls both tables → timestamped dump in `data/backups/` (gitignored;
  archive is eventual durable target, paused). **Contention-assumption fix:** the before/after check is
  now a CONTENTION REPORT, not a false-fail — PASS/FAIL is strictly dump==before (faithful point-in-time
  snapshot); a live append during the window is benign. test_ledger_backup.py 9/9. **Deployed to prod**
  (backend/main.py → production); `LEDGER_EXPORT_TOKEN` set in Railway (stored in Anthropic_API_KEY.env);
  seeded + backup verified live: before/dump/after all sha 71b56f5f, dump-faithful PASS. §8/§11
  contradiction resolved: NO rep_actions sync-back; scorecard reads prod directly; backup is a separate
  prod→local dump. **The prod ledger is LIVE and logging predictions on every sync.**

**Bug report fixed this session (TX-23-00379, pre-Step-5 data-integrity pass):**
- **#1 wrong-source-document (data integrity).** discover.py's petition selector matched only
  'ORIGINAL PETITION' then fell through to the FIRST document — grabbing the JUDGMENT and storing it as
  the petition on **4 TX-23 cases** (older docket format: the petition link title is the property type
  '- REAL PROPERTY', not "petition"), so no defendant addresses were captured. FIXED: match petition
  markers (ORIGINAL PETITION | REAL/PERSONAL PROPERTY), EXCLUDE other instruments, record NONE if no
  petition qualifies (never fall through); logs chosen context. Added `--force` (the "complete today"
  guard was fooled by addr+debt present-but-from-the-wrong-doc). **Re-scraped all 4; full 127-case
  content re-audit CLEAN (0 wrong docs on any live case — no blind spot).** TX-23-00379: 14 defendants +
  real Exhibit A total 46,463.65 (was judgment 83,750.45). TX-23-00423 (live lead) verified: petition,
  10842 Addie Road populated, owner+lienholder set sane. 4 synced to prod.
- **#2 date off-by-one (display).** `fmt()` parsed date-only "YYYY-MM-DD" as UTC → a day early in local
  time. Backend DB values were CORRECT (confirmed both DBs). Fixed to parse date-only as local; deployed.
- **#3 OOS event/document-title mislabel — SCOPED + TRACKED (background task), low priority.** Only
  **1 case affected (TX-23-00379)** — the only case with 2+ same-named "ISSUE ORDER OF SALE" events (the
  trigger). ⚠ **"1 case" is based on CURRENT data — re-check as more older-vintage (TX-23/earlier) cases
  get backfilled**, since older dockets are where duplicate same-named entries appear.

**Live-site review (2026-07-13) — 4 findings; Step 5 now HELD pending #3:**
- **#1 stale localStorage / count mismatch (FIXED + DEPLOYED).** "Analyzed Cases 148" vs "Total 127":
  the localStorage prune only removed `_platform`-id cases, missing legacy-synced cases with older
  `Date.now()` ids — so purged BPP held the count at the pre-purge 148 (real, browsable, not cosmetic).
  Prune is now id-agnostic: keep only genuine local drafts (`inputMethod`/`uploadedAt`) or on-platform
  case numbers; guarded vs an empty platform list. Reload drops to 127.
- **#2 sale-pulled stat/badge contradiction (FIXED + DEPLOYED).** "0 SALE PULLED" stat vs a card badged
  SALE PULLED (TX-23-00569): stat counted `stage='sale_pulled'` but the derivation checks
  `orderOfSaleIssued` first → a pulled sale reads oos_issued. Stat now counts sale-pulled by the DATE
  field OR stage (excluded from oos bucket); `caseStage` checks sale-pulled first; stored stage corrected.
  Live: sale_pulled=1. ⚠ **FOLLOW-UP: `discover.py` does NOT capture sale-pulled docket events at all** —
  TX-23-00569's sale_pulled_date is seeded/benchmark data, not scraped. Capturing pulls (a real distress
  signal — sale scheduled then withdrawn = bankruptcy/payoff/challenge, property resurfaces) is unbuilt.
- **#3 'Analyze with AI' — PRE-EXISTING UNGOVERNED CLIENT-SIDE PATH, PENDING DECISION (not acted on).**
  Not built this session. User pastes a docket, enters their OWN Anthropic key (stored in localStorage),
  and it calls api.anthropic.com DIRECT from the browser to extract + memo, saving a case to localStorage
  only. It **never POSTs to prod** (doesn't reach reps/ledger/sync guards) BUT bypasses the extraction
  pipeline entirely — no BPP detection, no document-selection, no enrichment — and its Date.now-id drafts
  are what caused #1. Plus the key sits in localStorage (XSS-readable). **DECISION NEEDED before Step 5:
  keep as a power-user local tool, or remove/gate it.** Step 5 is HELD until resolved.
- **#4 export ANTHROPIC_API_KEY=sk-ant-… — NON-ISSUE.** `&hellip;` (HTML ellipsis) — instructional
  placeholder in the Load-New-Deals command snippet, not a real key.

**NEXT — Step 5 (rep_actions API+UI) is HELD pending the #3 decision.** When unblocked:
`POST /api/cases/{cn}/actions` + `GET .../actions`, cached `deal_status`/`last_action_at`, logging UI
(gate green — Step 4 backup verified). Then Step 6 (scorecard.py → read the ledger via prod API, §11).
**Open follow-ups:** the #3 keep-or-remove decision; sale-pulled event capture in discover.py (from #2);
structural `prod_ready` gate on sync_to_prod ([[sync-approved-for-prod-gate]]); the OOS-event docket-parse
fix (task chip). Apply [[prod-history-fingerprint-proof]] to anything touching prod/ledger data.

**Standard reinforced this session (apply to anything touching prod or history):** capture a
baseline fingerprint (size + sha256 + row count) BEFORE, and re-verify it AFTER — prove data is
intact, never assume. The pcpeak.db purge verification is the template.

## INCIDENT (2026-07-13) — 36 cases synced to prod prematurely: a PROCESS gap, not a data bug

**What happened.** The held batch-1/2 cases (21 BPP + 16 dismissed-owing leads) went live on prod
via a `sync_to_prod.py` run when only one case was meant to be pushed — prod jumped **112 → 148**.
It was NOT assessed when it happened and stayed open through a full architecture session (ledger
design + restore guard) until directly asked about. Recording it as a **process** failure, not a
data one.

**Why it happened.** `sync_to_prod.py` has **no distinction between "exists locally" and "approved
for prod."** Its default / `--update-existing` modes push every local case not already on prod, so
a routine sync silently promotes everything in the local DB — including work-in-progress leads
deliberately held back. There is no "ready for prod" gate; the only safeguard is remembering to
pass `--only`/`--dry-run`.

**Resolution — fix-forward, not rollback.** Rollback would mean deleting rows from the prod DB via
the raw `railway run` path — the exact unguarded DR vector this session structurally eliminated
(ledger.db separation) — to fix a state that already displays correctly. Not worth it. Severity
assessment: the fixes shipped this session (BPP→N/A, `case_track` classification, projection
staleness, balance-as-distress) mean the live cases display reasonably; the one real gap was the
**dismissed-owing UI label**, which was then BUILT (`dismissedBadge`: "⚠ DISMISSED · OWES $X",
paid = muted) and deployed to prod. No data corruption, no fabricated values.

**FOLLOW-UP (not urgent, but real): give `sync_to_prod.py` a structural "approved for prod" gate.**
The same way the restore path was made structurally safe, this class of mistake should be made
structurally hard to repeat — not guarded by memory. Options: a per-case `prod_ready` column (only
`prod_ready=1` cases sync), or an explicit allowlist the sync must be handed. Until then, ALWAYS
run `--only <case>` or `--dry-run` first; never a bare / `--update-existing` sync. See
[[sync-approved-for-prod-gate]].

## Engineering standard for this project

Every claim about the data has to be checked against the actual source before it's
treated as true — not inferred, not assumed, not defaulted. Specifically:

- No field is allowed to silently become `0`, `""`, or `None` and get displayed as if
  that were a real value. If a scrape genuinely can't determine a number, that has to
  be visible as "unknown," not indistinguishable from a real zero.
- Any fix to extraction, enrichment, or calculation logic gets verified against a real
  case's real numbers before it's considered done — not just "the code looks right."
  The property_intel.py multi-tract fix wasn't trusted until its output was checked
  by hand against the actual DCAD balances for TX-26-01196 ($6,409.75 + $6,409.03 =
  $12,818.78).
- Stats and logs have to add up. If "Found: 10 / Processed: 6" doesn't sum with
  "Skipped" + "Errors," something is being silently dropped and needs to be surfaced,
  not left as an unexplained gap.
- When a bug is fixed in one place, check for the same root cause elsewhere in the
  pipeline. Today's session found the same "comma-separated account number treated
  as one ID" bug in three separate places (property_intel.py's scrapers, the frontend
  action-bar links) from a single root cause — fix the root cause, then sweep for
  every place it leaked.

## Current known-good state (as of this session)

Four confirmed, verified, fixed bugs — patched files are in this directory and need
to be merged into your working tree, diffed, and deployed:

1. **`main.py`** — `agent_runs` table has `finished_at`, not `completed_at`. The
   in-browser "Run" button's background thread was crashing silently on every
   single-case run, leaving the status stuck at `running` forever. Fixed.
2. **`index.html`** — case-run success handler called `syncPlatform()`, which doesn't
   exist. Real function is `syncFromPlatform()`. Fixed.
3. **`discover.py`** — `classify()` was doing plain substring matching, so business
   names without an exact-match keyword (e.g. "Texas Granite & Tile Co.") slipped
   through `--individuals-only` unflagged, and the skip counter never incremented for
   filtered-out businesses even when the filter did work. Rewrote as word-boundary
   regex, expanded the keyword list, added skip counting. Verified against 11 real
   case names with no false positives.
4. **`property_intel.py`** — `enrich_property()` had no handling for petitions listing
   multiple DCAD account numbers (e.g. a lot split into tracts 4A/4C). A
   comma-joined account string was passed straight through as a single malformed ID,
   producing a garbage market value coincidence and an `unknown` tax balance. Rewrote
   to detect multi-account strings, scrape each tract in parallel, and sum the
   financial fields. Also fixed the same root issue in `index.html`'s DCAD/Tax
   Balance/Tax by Year/Payment History buttons, which built one malformed link the
   same way.

**Verification still needed on your end (Claude Code should do this before moving on):**
- Merge all four patches into the live repo, `git diff` to confirm nothing else in
  your working tree gets clobbered.
- Re-run `discover.py --case TX-26-01196` after deploying `property_intel.py` and
  confirm the live equity card shows $114,000 MV / $12,818.78 balance, not the old
  $57,000 / unknown.
- Test the in-browser "Run" button live on taxforeclosureanalyzer.com — this feature
  has never been confirmed working end-to-end through the UI, only via terminal.

## Known data gaps (lower priority, not yet fixed)

- `discover.py`'s `EXTRACTION_PROMPT` schema has `"accountNumber": ""` as a single
  string. Multi-account petitions apparently get comma-joined into that one field
  reliably enough that today's fix worked — but that's inferred from one case, not
  confirmed as consistent behavior. Worth deciding whether to harden this to an
  explicit `"accountNumbers": []` array in the schema, which would touch more of the
  pipeline (discover.py's save logic, main.py's schema, index.html's rendering).
- `discover.py`'s `claude_extract()` truncates docket text to 4000 chars / PDF text
  to 12000 chars (raised today from a flat 5000-char front-truncation that was
  silently cutting off property addresses on ~60% of complex cases). Watch for any
  petition where combined docket+PDF text meaningfully exceeds that — TX-23-00569
  (35yr delinquency, 3 prior suits) is the most complex case on file and is worth an
  eyeball check if it's ever re-run.

## Credentials

Never hardcode API keys in source files or paste them into commit messages/logs.
Use environment variables (`ANTHROPIC_API_KEY`, `TWO_CAPTCHA_KEY`) exported per
shell session. If either key has ever been pasted in plaintext anywhere (chat,
terminal output shared elsewhere, screenshots), rotate it — don't assume it's fine
because nothing bad has happened yet.

## Working rhythm

1. State the bug/task precisely before touching code.
2. Find the actual root cause in the actual file — not the first plausible guess.
3. Fix it, then check whether the same root cause exists anywhere else in the
   pipeline (extraction → enrichment → storage → display).
4. Verify against real data, not just "the code compiles" or "the logic looks right."
5. Confirm stats/logs are internally consistent (numbers add up) before calling
   something done.
6. Then, and only then, move to the next item.

## Deployment & operations state (as of 2026-07-08 session)

### Architecture: scraping is LOCAL, cloud only serves data
- **Scraping runs locally only.** `discover.py` / `property_intel.py` are run from a
  terminal on the Mac; the Railway service (`taxforeclosureanalyzer.com`, project
  `gracious-tenderness`, service `pcpeak-platform`) only serves/reads the DB and the
  frontend. Do **not** add `playwright` to `requirements.txt` or the Railway build
  unless this decision is reversed — the in-browser "Run" button therefore cannot
  scrape in-cloud (no playwright/Chromium in the image) and that is by design.
- `ANTHROPIC_API_KEY` and `TWO_CAPTCHA_KEY` were **removed from Railway env** — the
  server never references them (only the local CLIs do, via raw httpx HTTP, not the
  `anthropic`/`twocaptcha` SDKs, so those libs are not required).
- **Local interpreter:** the default `python3` is the 3.14 framework build and has
  `httpx`, `bs4`, `playwright` + chromium installed. `python3 discover.py` works as-is.
  (`.python-version` says 3.11 but pyenv isn't installed, so it's inert. `/usr/bin/python3`
  is 3.9.6 and lacks playwright — don't use it. `playwright.__version__` doesn't exist
  in modern playwright; test with `from playwright.async_api import async_playwright`.)

### Persistence: Railway volume (fixed 2026-07-08)
- Before: no volume → every deploy wiped the SQLite DB (ephemeral container FS).
- Now: volume `pcpeak-platform-volume` mounted at `/app/data/db` (= where `DB_PATH`
  resolves). DB survives deploys — verified by forced redeploy keeping 48 cases.

### DB restore procedure (repopulate prod from local)
- ⚠️ **`data/db/ledger.db` is a SEPARATE, PROD-OWNED file — never restore over it.** It holds
  `prediction_ledger` + `rep_actions` (prod-generated, non-regenerable; see
  `docs/prediction-ledger-design.md` §13). The restore below covers `pcpeak.db` ONLY. A raw
  `pcpeak.db .dump`/restore physically can't touch `ledger.db` (different file — the structural
  guard), so the procedure is safe as-is; just don't dump/restore `ledger.db` from local (local's
  copy is empty). Back `ledger.db` up separately (Step 4 of the ledger build).
- Source of truth: local `data/db/pcpeak.db` (git-ignored; can be bloated by dead
  free pages — `VACUUM INTO` gives a ~1.5MB clean copy). `data/db/dump.sql` is a
  tracked `.dump` snapshot; regenerate with
  `sqlite3 data/db/pcpeak.db .dump > data/db/dump.sql`.
- `railway volume files upload` is **blocked** (needs SSH keys, none registered).
- Working method: push through the app API (column-name-based, order-safe) —
  `POST /api/cases` per case (drop `id`), `POST /api/events/{case_number}` for docket
  rows. Use a `certifi` SSL context (bare urllib fails CERT_VERIFY on this Mac).
- Restored 2026-07-08: 48 cases + 838 events, verified live count == 48.

### Multi-tract audit (2026-07-08)
- Only **one** case has a comma-joined `account_number`: **TX-26-01196**. It was
  re-scraped with the fixed `property_intel.py` and now reads MV **$114,000** /
  balance **$12,818.78** (2 tracts @ $57k), verified live. Raw-data sweep found no
  other hidden multi-account cases. Re-run the audit
  (`SELECT ... WHERE account_number LIKE '%,%'`) after any bulk import.
- Fixed same session: `scrape_dcad` silently null-ed `market_value` under a fixed 2s
  read and under concurrent multi-tract loads. Now polls for the valuation to render,
  uses a whitespace-tolerant regex, and enriches tracts sequentially.

### Ownership-history parser rebuilt (2026-07-09)
`scrape_dcad_history` was splitting the whole AcctHistory.aspx page on any
`YEAR\t` boundary, merging its FOUR stacked tables (Owner/Legal, Market Value,
Taxable Value, Exemptions) into `ownership_history` — ~80 junk "owners" ($0,
No Exemptions) per case across 39 cases. Now slices the page by table header
first; parses ownership from its section only; the other three tables go to
`market_value_history` / `taxable_value_history` / `exemptions_history`. Also
fixed: mailing-address off-by-one, `owner_changes` reversed direction/year
(now chronological via `_derive_owner_signals`), `is_absentee` hard-coded
"HARRIS" (now compares owner mailing city vs property city), and BPP (99-prefix)
accounts whose header is `Year\tLegal Owner\tDoing Business As (DBA)`. All 45
enriched cases backfilled + verified live (0 contaminated).

### OPEN — 4 un-enrichable cases (data-source issues, NOT code)
These have no usable DCAD data; the account or county is the problem:
- `TX-26-00899` — empty account, property in **Carrollton** (likely Denton
  County, outside Dallas DCAD).
- `TX-26-00995` (`29323`), `TX-26-00992` (`43270`) — malformed **5-digit**
  accounts, properties in **Garland**. Extraction likely captured a wrong ID;
  Dallas DCAD uses 17-digit accounts.
- `TX-25-01777` (`00008496024000000`) — valid-format account but DCAD returns
  "No Owner History / No Market History" (retired/merged/invalid account).
Fixing these means correcting the source account numbers, not the scraper.

### Credentials — status 2026-07-11 (mostly rotated; 2 user follow-ups left)
- **Secrets live in `Anthropic_API_KEY.env`** (gitignored via `*.env`, untracked — verified).
  Holds ANTHROPIC_API_KEY, TWO_CAPTCHA_KEY, NTREIS/ATTOM/GOOGLE_STREET_VIEW/PORT.
  discover.py's `_load_local_env()` loads it into os.environ at startup (zero-dep; a real
  shell export still wins). Nothing loaded it before — the key was silently empty.
- [x] **2Captcha key** — ROTATED + verified working (new key len 32; getbalance $2.61; a
      live one-case run solved the CAPTCHA end-to-end). Hardcoded default removed from
      source. NOTE: the OLD key is still in git HISTORY — optional filter-repo/BFG purge
      (now harmless since rotated).
- [x] **Anthropic key** — ROTATED + verified (Claude extraction ran on it). Not in source,
      not in Railway.
- [~] **GitHub PAT** — remote switched to token-less URL + `credential.helper osxkeychain`
      (helper verified functional). USER TODO: (1) first authenticated push enters the new
      fine-grained PAT (keychain stores it), (2) **REVOKE the old PAT on GitHub** — still
      valid until revoked.

### Deploy gate — production branch is ACTIVE (Railway watches `production`)
Work lands on `main`; release with `git checkout production && git merge main && git push
origin production && git checkout main`. Railway deploys `production`. DB is on a volume (no
data loss), scraping is local (prod only serves), so a bad deploy's blast radius is a broken
site, not lost data — recoverable via revert + redeploy.

**STANDING RULES (2026-07-23) — the feature branch must ALWAYS be the complete record:**
1. **Every commit is preceded by a branch check** (`git branch --show-current`). Feature-first
   authoring order is NOT ceremony — it is what guarantees the feature branch originates every
   change, so it is never behind main/production. (Bit once: a fix was committed on `main`
   directly because a prior deploy left the checkout there; the feature branch had to be
   FF-reconciled after the fact. Net result was correct, but the branch briefly wasn't the
   record.) Commit on the FEATURE branch, then run the FF chain feature → main → production.
2. **Every deploy sequence ENDS by checking out the feature branch** (`git checkout
   claude/remove-analyze-with-ai-0vu5i9`), never leaving the tree on `main` or `production`.
   This is what prevents rule 1 from being violated on the next commit.
3. FF chain is **no-force**, always. Verify FF-safety (`git merge-base --is-ancestor`) before
   each hop, and fingerprint both served artifacts (`backend/main.py`, `frontend/index.html`)
   before AND after — same standard as any prod/history op ([[prod-history-fingerprint-proof]]).
   End state: all three origin refs at ONE SHA.

> **CORRECTION (verified live 2026-07-11): `f7a3003` is actually SAFE to deploy.** My
> earlier concern — that untracking `data/pdfs/` would 404 the "Petition PDF" button — was
> WRONG. The frontend `openPetitionPDF()` does `fetch('/api/petition/'+cn).then(r=>r.json())`
> and uses `data.url` = the stored `petition_href` (a courtsportal.dallascounty.org
> DocumentViewer URL). It NEVER uses the local PDF file: a `FileResponse` fallback would
> break `r.json()` and just hide the button, and there are **0 frontend references to
> `/api/pdf`**. So removing `data/pdfs/` breaks nothing user-facing. This deploy still
> cherry-picked the 4 code commits (harmless over-caution); a future straight `git merge
> main` carrying `f7a3003` is FINE and would slim the ~237MB corpus out of the container.
> (The petition button works only for the 31/112 cases that have a `petition_href`; that's a
> separate coverage gap, unrelated to local files.)

## Roadmap progress

### Step 1 — Sidebar UI — DONE & verified (2026-07-09)
Built in `frontend/index.html`, directly above the case list:
- Search box (case #, defendant, address — client-side substring).
- Filter dropdowns: complexity, stage, rep + the city pills (the city filter was
  previously a no-op — `filterCity` set a var `renderList` never read; now wired).
- Sort: days to OOS, total due, filed date.
- Pagination, 15/page; count shows "N of M" when filtered.
- `renderList()` now runs filter → sort → paginate; helpers `getFilteredCases`,
  `setSearch/setFilter/setSort/gotoPage`, `caseStage`.
- **Rep assignment**: standardized on `rep_assigned` (new DB column, added to the boot
  migration), persisted to the platform via `POST /api/cases` (was localStorage-only),
  shown as a chip on each card and as a filter option.
Verified in a real browser against the 48 live cases (search/filter/sort/paginate/rep
assign all work) and on the live site (rep persists).

Bug found & fixed along the way: `POST /api/cases` ran `compute_projection` on the raw
payload, so partial updates (property_intel-only, rep-only) nulled stored
`projected_oos`/`confidence_pct`. Now merges the payload onto the existing row before
projecting. Restored projections for all 48 cases. (Note: the frontend recomputes
projection client-side via `project()`, so this was silent, not user-visible.)

Deferred from step 1: virtualized rendering (pagination is enough at this scale);
server-side filtering (client-side is fine while all cases load in one `/api/cases`).

**Rep roster (upgraded from free-text to a managed entity):** the first rep control
was free-text with an implied roster (no remove, hardcoded undeletable defaults, typo
fragmentation). Rebuilt as a server-side `reps` table (the single source of truth):
- Endpoints: `GET /api/reps` (with per-rep `case_count`), `POST` (add/reactivate),
  `PATCH /api/reps/{id}` (rename → cascades to every case that rep owns, or toggle
  `active`), `DELETE` (soft-delete = deactivate, keeps history), `POST /api/reps/reassign`
  ({from_rep,to_rep} — move a rep's cases or unassign). `create_case` registers any
  assigned rep so the roster ⊇ all assignments. Seeded from existing assignments.
- Remove semantics (per decision): **deactivate (keep history) + separate reassign** to
  move the book. One owner per case.
- Frontend: "Manage Reps" modal (gear by the rep filter) — add, inline rename,
  remove/restore, reassign, live case-counts. `allReps()` (assignment picker) sources
  from the **active roster ONLY**, so a removed rep can't reappear even if a case still
  carries their name; the sidebar filter unions active-roster + anyone-with-cases so a
  removed rep's remaining cases stay findable/reassignable. Handlers are id-based (names
  never inlined into HTML). Verified: backend lifecycle via TestClient; frontend wiring
  + deactivate-exclusion in-browser.

### Scrape→sync pipeline — flown & hardened (2026-07-09)
Ran the first real end-to-end flight (scrape locally → sync to live) on small
batches and fixed every reliability bug it surfaced. discover.py now:
- **Search retry** — the Tyler Smart Search intermittently returned an empty grid
  (reCAPTCHA token not validated before submit); retry navigate→solve→submit up to
  4x until TX- rows appear.
- **PDF via session GET** — `expect_download()` timed out on inline-served petitions;
  now fetch bytes over the authenticated context (`page.context.request.get`), verify
  `%PDF`, retry once. Recovered cases that were saving empty.
- **Case-detail nav** — poll ~14s for the docket to render and reload a blank
  CaseDetail page instead of re-clicking a stale results link.
- **Pagination** — was capped at page 1 (after processing a page, the scraper was on a
  case-detail page with no pager). Now `back_to_search_results()` before the next-page
  click; verified paging 1→2→3 processing cases with 0 errors.
- **Account validation** — only 17-digit DCAD accounts (single or comma-multi) go to
  enrichment; Garland 5-digit / out-of-county / wrong-field accounts are flagged
  ("needs manual lookup", counted in the summary) instead of stored as garbage.
- **Safety flags** — `--limit N`, `--skip-existing`; `SCRAPE_DEBUG` dumps.

**`sync_to_prod.py`** is the local→prod push: `--dry-run` / default (new cases) /
`--update-existing`. Never sends `rep_assigned` (live-owned), additive-only, dedupes
docket events client-side, idempotent, reconciles its tally, verifies count after.

**Loaded this session:** 58 cases live (was 48) — 10 new TX-26 deals through the full
loop. **OPEN gap:** the Mesquite/Garland/out-of-county cases with no Dallas DCAD account
enrich to nothing (flagged). Fix = resolve the DCAD account by property address (a DCAD
address search) instead of relying on the petition's account field. **Also OPEN:** the
in-browser "Run" button was deleted (cloud can't scrape) — scraping is local + sync.

### Timing-model engine + DCAD account resolver (2026-07-09)
Turned "backfill closed cases" into a self-measuring learn loop:
- **Outcome capture fixed** — the docket is chronological (oldest first) and
  extraction front-truncated to 4000 chars, silently dropping the OUTCOME events
  (judgment / ISSUE ORDER OF SALE / sale / dismissal) that sit at the end. Now
  always surfaces outcome-signal lines regardless of docket length; captures
  `oos_date`/`oos_issued`, `saleScheduledDate`, and distinguishes a real judgment
  from a NON-SUIT/DISMISSAL. Verified on TX-23-02230 (OOS 2026-06-16 captured).
- **`scorecard.py`** — backtests `compute_projection` vs cases with a real
  `oos_date`: prediction error (post-judgment & at-filing), observed vs assumed
  filing→judgment / judgment→OOS windows, data-quality flags. Early read (n=4):
  post-judgment model is decent (median err ~48d, 75% within 90d) but
  **filing→judgment is ~9mo observed vs the model's assumed 12–48mo** — the big
  recalibration target; judgment→OOS ~56d median with a long contested tail.
- **`resolve_dcad_account(address, owner, browser)`** in property_intel.py —
  DCAD address search (exact parcel) then owner-name fallback (sole/address-match
  only; never guesses). Recovers the many cases extraction garbles (Tryon got a
  wrong 15-digit account). Wired into the scraper's enrich gate: bad/missing
  account → resolve from address/owner → persist → enrich. Recovered 9/11
  no-account TX-23 cases; account coverage now 87%, 62 cases with a real balance.
- **Funnel truth:** ~38% dismissed, ~38% judged-pending, ~23% reach OOS. Dismissed
  ≠ resolved (see [[dismissal-not-resolved]] memory) — tax balance is the signal.
  OOS outcomes live in OLD case numbers (TX-23-0*, TX-24-000*), not fresh ones.

**OPEN next:** accumulate more OOS cases → recalibrate CITY_DATA from measured
reality; ACT (tax-office) balance scrape shows some spurious $0 (may need the
retry treatment DCAD got); surface dismissed-but-delinquent as a lead view.

### Account backlog + corroboration guard (2026-07-11)
- **CITY_DATA FROZEN.** The n=8 Dallas ftj recalibration ([7,11]/[10,16]/[15,28]) was
  reverted to baseline [12,18]/[18,30]/[30,48] in both backend and frontend. Do NOT
  recalibrate without explicit sign-off; revisit only at ≥40 closed OOS cases. Always
  show sample size (n=) next to any stat. See [[city-data-frozen-sample-size]].
- **`account_status` column** (resolved | needs_lookup | invalid) — flagged/malformed
  accounts were vanishing into a log line + counter; now a persisted, queryable state
  written by discover.py, surfaced as a sidebar filter + per-card badge (⚠ NEEDS ACCT /
  BAD ACCT). Plus `account_note` (the reason, shown in the badge tooltip). One shared
  rule across discover.py `valid_dcad_accounts`, backend `account_status_of`, frontend
  `caseAccountStatus` (≥1 part of exactly 17 digits = resolved; empty/placeholder =
  needs_lookup; else invalid). Verified: all 3 paths agree, counts reconcile.
- **Resolver audit (`resolver_audit.py`, n=92):** independently re-resolved known-good
  accounts from address only vs on-file. 85 match / 5 mismatch / 2 null. Verified the 5:
  2 were resolver FALSE POSITIVES (address search returned a wrong parcel; on-file owner
  matched the defendant → on-file correct), 3 inconclusive (owner≠defendant, but could be
  estate/heir). **Confirmed extraction garble on valid-17-digit accounts: 0/90.** The real
  finding: address search alone returns a confidently-wrong parcel ~2% of the time.
- **Corroboration guard** (`property_intel.resolve_account_corroborated`): auto-assign an
  account ONLY when a 2nd independent signal agrees (address+owner searches converge, or
  the result's DCAD owner matches the defendant, or an owner result's address matches the
  petition). Estate/heir cases handled: defendant≠DCAD-owner is expected there, not a
  mismatch. Uncorroborated candidate is NOTED but NEVER written. Wired into BOTH the
  backlog tool and the live scraper (discover.py).
- **`resolve_backlog.py`** consumes the needs_lookup/invalid backlog. First run (n=14):
  6 auto-resolved+enriched (all corroborated — recovered the Garland 5-digit stubs +
  an estate/heir case), 1 held back (owner-only, not trusted), 7 unresolved (Mesquite/
  Rowlett/out-of-county, no Dallas DCAD match). Local backlog 14→8. **These 6 resolutions
  are LOCAL only — NOT yet synced to prod.**
- **OPEN:** enrich_property's ownership parser returns empty `owners` for some suburban
  (Garland/Carrollton/Mesquite) accounts even when market_value/balance parse fine — a
  display gap (account still correct). This is now the main remaining enrichment gap.

### Alphanumeric DCAD accounts fixed (2026-07-11) — backlog 8→3
Root cause of most of the backlog: `valid_dcad_accounts` required 17 DIGITS, but DCAD
uses 17-char ALPHANUMERIC IDs for some parcels (condos/townhomes/special — e.g.
`0067850D0010A0000`, `382020500K0150000`). Those valid accounts were flagged 'invalid'
and never enriched. Verified all 5 affected accounts load real DCAD properties (3/5
confirm owner==defendant: Diaz/Quinlan/Richey). Widened the account rule to 17-char
alphanumeric across ALL paths that must agree: `valid_dcad_accounts` (discover.py),
`_dcad_results` (property_intel.py), `account_status_of` (backend), `caseAccountStatus`
(frontend) — unit-tested in-browser (5-digit stubs / non-17 still rejected). Reprocessed
the 5 (resolved+enriched), synced to prod; local+prod backlog now **3**, all structural:
- `TX-23-02234` (SALZMAN) — petition extracted NO property address; needs re-extraction.
- `TX-24-00079` — Las Colinas HOA, dual "12 Erling / 13 Claiborne" common-area parcel.
- `TX-24-00099` — 4210 Pecan Grove Ln, Rowlett (spans Dallas/Rockwall county).
Code is on `main`; deploys to prod on the next `production` merge. Prod DATA + display are
already correct (synced account_status='resolved'; the frontend prefers the stored value).

### Repo hygiene — untracked the scraped corpus from git (2026-07-11)
`git rm --cached` (files kept on disk) untracked ~108 stale files that predated
`.gitignore`: 3 `.pyc`, all of `data/pdfs/` (51 docket.txt + 46 petition.pdf), and 3
stray root PDFs. `.gitignore` now covers `*.pyc`, `__pycache__/`, `data/pdfs/`, `*.pdf`.
- **Deferred (optional):** the ~237MB corpus is still in git HISTORY. A `git filter-repo`
  / BFG purge would reclaim it, but rewrites all hashes + needs a force-push — do it
  deliberately, not mid-session. New clones won't grow further regardless.
- **⚠ BACKUP GAP (raised priority):** now that `data/pdfs/` is git-untracked, git is no
  longer even an incidental backup of the raw corpus. The ONLY copy is local disk until
  the [[archive-paused]] object-store archive is turned on — a laptop failure loses the
  moat. This is the strongest argument yet for un-pausing archive.py once storage exists.

### Raw-capture archive — built & inert, awaiting storage creds (2026-07-10) — PAUSED
(Deliberately paused by the user 2026-07-11; keep inert until they resume — see
[[archive-paused]]. Original notes below.)
`archive.py` — append-only raw-source archive to durable S3-compatible object
storage (Cloudflare R2 / Backblaze B2 / AWS S3). The raw corpus (petition PDFs,
docket, extraction + enrichment snapshots) is the moat: capture it raw + timestamped
so it can be re-mined forever as models improve, and get it off the laptop. Wired
into `discover.py` right after a case saves: uploads `raw/{case}/{ts}/` (petition.pdf,
docket.txt, extracted.json, property_intel.json — a NEW snapshot per scrape, never
overwritten) then prunes the local PDF **only** after a confirmed upload.
- **Fully env-gated.** `archiving_enabled()` = all 4 `ARCHIVE_*` vars set. Unset =
  no-op, scraper unchanged (verified: `archive_case()` returns `[]`, PDF kept local).
- **To turn on:** set `ARCHIVE_ENDPOINT/BUCKET/ACCESS_KEY_ID/SECRET_ACCESS_KEY`
  (+ optional `ARCHIVE_REGION`, default `auto`) — see `.env.example`. Validate with
  `python3 archive.py` (write→read→delete probe).
- **boto3 1.43.46** installed locally (only import site is `archive._client()`; not a
  scraper hard-dep — `archiving_enabled()` never imports boto3). NOT added to Railway
  (cloud doesn't scrape or archive).
- **OPEN follow-ups (once storage is provisioned):** (1) one-time backfill-archive of
  the ~236MB of petition PDFs already on the laptop, then prune; (2) v2 = capture raw
  DCAD/ACT HTML (property_intel currently saves parsed values only, not source HTML);
  (3) build the retrieval/re-extract path (pull raw from archive to re-mine).

### Business Personal Property (BPP) suits — detected + excluded (2026-07-11)
BPP tax suits (business equipment/inventory, not real estate) were running through
real-estate compute_projection/equity as if they were houses. Audit: **21 of 147 cases
are BPP.** Contamination of the CITY_DATA calibration was **coincidental, not structural**
— BPP suits DO reach a sale stage ("WRIT OF EXECUTION", the personal-property analog of
an Order of Sale; 3 already have it), and scorecard.py selected calibration cases by raw
`oos_date`, so any BPP case that ever got an oos_date would have leaked. Fixed structurally:
- **Signal:** the Tyler docket's `Comment` field (REAL PROPERTY | PERSONAL PROPERTY) — the
  authoritative discriminator (123 REAL / 21 PERSONAL, clean). NOT the 99-account prefix
  (only 7/21 have it). `discover.property_type_from_docket()` parses it at scrape time.
- **`property_type` column** ('real'|'personal'), stored (cloud can't read dockets), set by
  discover.py. **`personal_property` is a case_track value, classified FIRST** — a BPP case
  can never reach oos_timing even if it carries an oos_date.
- **Belt-and-suspenders:** discover.py + the backfill NULL out `oos_date`/`oos_issued` for
  BPP, so the raw field stays real-estate-only.
- **scorecard.py excludes BPP** (filters property_type/case_track), so calibration never sees
  one — the actual leak-close. compute_projection + frontend project()/equity short-circuit
  to "N/A · business personal property". Verified: 21 tagged, 0 in oos_timing, calibration
  still 10 real-property OOS cases, scorecard excludes 21.
- **OPEN:** the 3 prod-facing BPP cases (TX-26-01188/01373/01377) need a data sync +
  production merge to take effect live; batch-1/2 BPP cases are local-only (held).

### BPP: escalated from detect-and-skip to NEVER STORE + PURGED (2026-07-13)
Direction changed — the platform must not store personal-property data at all, not just
exclude it from math. Three structural guards ("physically impossible, not procedurally
discouraged" — same standard as ledger.db), then a purge:
- **Search-level filter — NOT feasible (verified live).** `case_type` is uniformly
  "TAX DELINQUENCY" (real + personal + null); the portal Smart Search has zero filters; the
  real/personal split lives ONLY in the docket Comment field, unknowable until the docket is
  fetched. So "zero portal contact" is impossible — the Comment check (already before Claude
  extraction) is the earliest detection the data allows.
- **`sync_to_prod.py` structural filter** — `local_cases()` selects `WHERE property_type IS NOT
  'personal' AND property_type IS NOT 'unknown' AND case_track IS NOT 'personal_property'`, so
  NO path (default / --update-existing / a forgotten --only) can push a BPP (or undetermined)
  case. Direct structural fix for the 36-case incident. Verified: --only on a BPP case refused.
- **`property_type='unknown'` review state** — a new scrape with no docket Comment is tagged
  'unknown' (not silently 'real') and held from sync until a human confirms. The 3 existing
  null cases (TX-25-00492/22-01443/23-00553) are already-live likely-real closed foreclosures —
  deliberately NOT backfilled (would mislabel real cases).
- **Guarded delete** — `DELETE /api/cases/{cn}` deletes ONLY when the row is
  property_type='personal', enforced IN THE WHERE CLAUSE (`DELETE FROM cases WHERE case_number=?
  AND property_type='personal'`) — a real case number matches 0 rows and is refused (409), DB-
  level not caller-trusted. test_bpp_delete_guard.py 11/11; verified live on prod (real case
  refused + survives).
- **PURGED both sides (2026-07-13):** 21 BPP deleted from prod (via the guarded endpoint) and
  local. Both now 148→**127 cases, 0 BPP**, non-BPP unchanged at 127, 0 orphan events. The
  platform no longer stores personal-property data anywhere.

### Pre-launch review — 3 live-data defects fixed + deployed (2026-07-11)
1. **OOS projection self-check + staleness.** compute_projection/project() ignored a real
   oos_date and showed a fabricated projected date at 85% (all 10 confirmed cases), and 53
   cases flashed stale confidence past a failed projection. Now: real OOS → confirmed date
   @100% ("Order of Sale ISSUED"); passed-with-no-OOS → "Projection failed — no OOS as of
   <today>" @0%. Verified live (TX-23-00244 confirmed, TX-22-01443 stale).
2. **joos confidence calibration.** joos never got ftj's freeze/scrutiny; the n=10 data is
   bimodal (fast ~40-120d vs contested ~360-557d), so 85% "judgment→high confidence"
   misrepresented it. Max non-confirmed confidence now 55% ([22,30,45,55]) + a bimodal UI
   caveat + CITY_DATA doc. Constants NOT changed (same don't-recalibrate-on-small-n rule).
3. **payment_history parser** (same class as the DCAD ownership-history fix): ACT page is
   a vertical layout, old regex required a single-line row + a payer + capped at 15 →
   dropped the majority. Rewrote as date→amount line-pairing. Backfill recovered **2457
   dropped records across 110/122 cases** (1448→3905 local; prod synced to 3391 for its 112).
   `payment_backfill.py` is the one-time re-scrape tool.
Deployed 1&2 to prod via cherry-pick (f7a3003 still held out); synced 3's data to the 112
prod cases only (held batch-1/2 leads NOT pushed — they still need the dismissed-owing UI label).

### DONE — git history purge (2026-07-13): .git 316M → 1.1M
Executed with `git-filter-repo` (single script; git 2.39.2). Purged from ALL history:
`data/pdfs/` (~160M) + **`data/db/pcpeak.db` (192M — the DOMINANT bloat, a SQLite DB committed
to history early, discovered mid-rewrite)** + 8 stray root `*.pdf` (petition PDFs committed to
root early). LEFT `data/db/dump.sql` intact (intentionally-tracked restore snapshot, 28 commits).
- **On-disk data UNTOUCHED (verified byte-for-byte):** live `data/db/pcpeak.db` same size
  (3698688) / sha256 (e9310b29…) / 127 cases before & after; 146 `data/pdfs` PDFs still on disk.
  The purge only rewrote git HISTORY — untracked/gitignored working files are invisible to it.
- Local `production` branch created from origin/production so BOTH refs rewrote in one pass;
  single `git push --force origin main production` (main 7330f3a, production 7bce569). Railway
  (watches production HEAD, not SHA lineage) deployed cleanly: /api/cases,stats,reps → 200,
  127 cases, frontend 200. fsck clean. Freed ~315M; disk 3.2GB.
- New clones are now tiny. If future scrapes ever re-commit a DB/PDF, re-sweep the same way.

### Future enhancement (not urgent) — capture the real judgment amount
The docket's "Total Judgment: of $0.00" is a Tyler source quirk (the real award is in the
judgment PDF, which we don't download — we only pull the petition PDF). We do NOT store a
judgment-amount field, so there is no our-side conflation bug here (verified 2026-07-11:
real debt is captured in `total_due_filing`; the 4 judged batch-1 cases carry $8,982–$43,474).
The actual judgment/payoff amount is valuable (payoff at judgment) but lives in the judgment
document — capturing it would mean downloading + parsing that PDF. Future enhancement, low
priority. This is a data-CAPTURE gap, NOT the falsy-conflation bug shape (that was DCAD
1/1/1900 and ACT $0-vs-unknown, both fixed).

### Steps 2-5 — not started
Backfill closed 2024/2025 cases, deed/lien-index research, geocoding + legal parsing,
nearest-neighbor benchmark matching. Harden account extraction (Garland 5-digit /
Carrollton empty accounts) at the FRONT of step 2 before backfilling more.

**Deed/lien data path — BUSINESS DECISION, not an engineering one (researched 2026-07-11).**
`dallas.tx.publicsearch.us` (the County Clerk's deed/lien records) must NOT be scraped:
its robots.txt is `Allow: /$` / `Disallow: /` (homepage only), there's no open API, and
Tyler-style records ToS prohibit automated aggregation. Legitimate paths: (1) a Texas
Public Information Act (PIA) request to the County Clerk for bulk data, or (2) a licensed
provider (e.g. TexasFile covers Dallas OPR). Decide the path before any Step-2 deed work.
See [[publicsearch-tos-research]].
