# Prediction Ledger + Outcome-Capture Layer — Design

**Status:** Approved design, not yet built (design-only; approved 2026-07-12).
**Scope of this doc:** the two new tables, the reconciliation mechanism, the resolved
decisions, and the implementation hooks — enough for a fresh session to build from without
re-litigating the design. **No constant is changed and no recalibration happens as part of
this work** (see Scope Boundary).

---

## 1. Why this exists

Three pre-launch defects (fixed 2026-07-11, see CLAUDE.md "Pre-launch review") all traced to
the same structural gap: **the platform makes predictions but never records them at the moment
they're made**, so it cannot reconcile prediction vs. actual outcome over time.

- `compute_projection()` (`backend/main.py`) recomputes live on every request — no history.
- `scorecard.py` backtests only against the **current** `oos_date`, so it can't measure
  calibration *drift* (did a model change actually improve accuracy?).
- Defect ② had to argue confidence down from 85%→55% on the *assumption* that joos is bimodal;
  the ledger is what turns that from an asserted claim into a measured one.

This layer records every prediction immutably, captures what actually happened, and reconciles
the two — building the evidence that would eventually justify un-freezing `CITY_DATA` at ≥40
closed cases (per the `city-data-frozen-sample-size` rule; recalibration stays manual + signed-off).

## 2. Framing — three distinct concepts

Conflating these is the trap. Keep them separate:

| Concept | What it is | Where it lives |
|---|---|---|
| **Prediction** | What `compute_projection()` said (date + confidence + the inputs behind it). An estimate. | **NEW** `prediction_ledger` |
| **Court outcome** | What actually happened: `oos_date`, dismissal (`case_track`), sale. Objective, scraped. | **Existing** `cases` |
| **Rep action** | What a human did: contacted, offer, result. Business workflow. | **NEW** `rep_actions` |

- **Item (1)** = the prediction ledger.
- **Item (2)** = rep-action capture.
- **Item (3)** = reconciliation, which *joins* prediction ↔ court outcome (both already exist —
  it computes the error and pins it to the prediction that called it).

So: **two new tables + a reconciliation mechanism** (not a third table).

## 3. Constants (new, in `backend/main.py` next to `CITY_DATA`)

```python
# Bump on ANY change to compute_projection() logic OR the CITY_DATA constants. Every ledger
# row stores the version it was made under, so calibration can compare accuracy across models
# (e.g. "did the recalibration improve error?"). Format: date + short descriptor.
MODEL_VERSION = "2026-07-12-ftj-frozen-joos-bimodal"

# A forward prediction whose projected_oos has passed by more than this with no real outcome
# is marked expired_no_oos (a measured miss, not a silent gap). Named, not inline, because the
# ledger should eventually tell us whether 90 is the right threshold.
PREDICTION_EXPIRY_DAYS = 90
```

## 4. Table 1 — `prediction_ledger` (append-only, immutable predictions)

```sql
CREATE TABLE prediction_ledger (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number          TEXT NOT NULL,
    predicted_at         TEXT NOT NULL DEFAULT (datetime('now')),
    model_version        TEXT NOT NULL,        -- MODEL_VERSION at prediction time

    -- ── prediction outputs (IMMUTABLE) ──
    prediction_basis     TEXT NOT NULL,        -- confirmed | judged | next_hearing | filed | none | na_bpp
    projected_oos        TEXT,                 -- predicted date (NULL if unprojectable)
    confidence_pct       INTEGER,
    days_to_oos          INTEGER,              -- predicted horizon at predicted_at
    filing_to_judgment_months INTEGER,

    -- ── model inputs snapshot (IMMUTABLE — makes the row reproducible/auditable) ──
    in_city              TEXT,
    in_complexity        TEXT,
    in_stage             TEXT,
    in_filed_date        TEXT,
    in_judgment_date     TEXT,
    in_next_hearing_date TEXT,
    in_oos_issued        INTEGER,
    used_joos_days       INTEGER,              -- the CITY_DATA value actually applied
    used_ftj_low         INTEGER,
    used_ftj_high        INTEGER,
    input_hash           TEXT NOT NULL,        -- hash(inputs+outputs+basis) — change-detection

    -- ── reconciliation (NULL until outcome lands; written ONCE; prediction fields never touched) ──
    outcome_type         TEXT,                 -- oos_issued | dismissed | sale | expired_no_oos
    outcome_date         TEXT,
    error_days           INTEGER,              -- signed: actual_oos − projected_oos
    resolved_at          TEXT
);
CREATE INDEX idx_pl_case  ON prediction_ledger(case_number, predicted_at);
CREATE INDEX idx_pl_open  ON prediction_ledger(case_number) WHERE outcome_type IS NULL;
CREATE INDEX idx_pl_model ON prediction_ledger(model_version);
```

**Key field rationale:**

- **`prediction_basis`** is the linchpin. It records which branch of `compute_projection()`
  produced the row:
  - `confirmed` — a real `oos_date` was already on record (records a *fact*, not a prediction).
  - `judged` / `next_hearing` / `filed` — genuine **forward predictions** (the only rows
    calibration scores).
  - `none` — unprojectable (no filed/judgment/hearing date).
  - `na_bpp` — business personal property (short-circuited; never a real-estate prediction).

  Calibration filters to `prediction_basis IN ('judged','next_hearing','filed')`. This is the
  same prediction-vs-fact distinction defect ① drew, now persisted.

- **`used_joos_days` / `used_ftj_low` / `used_ftj_high`** are stored on the row because
  `model_version` alone can't reconstruct historical constants once `CITY_DATA` changes. Storing
  the values actually used makes each row self-contained and reproducible even after a future
  recalibration — which is the entire point of gathering un-freeze evidence.

- **Reconciliation columns live on this table, filled once.** Append-only applies to the
  *prediction* fields; the outcome resolution is a one-time `pending → resolved` transition.
  A pure reconciliation *view* was rejected: the outcome must be **pinned to the prediction that
  called it**, not recomputed against ever-changing `cases` data. A materialized `error_days` is
  a stable historical fact; a view would silently rewrite history on every case update.

## 5. Table 2 — `rep_actions` (outcome-capture) + cached columns

Built **in this phase** (not deferred): the ledger alone only tells us whether the *timing model*
is accurate. `rep_actions` is what eventually answers whether **accurate predictions actually
convert to deals** — the real point of the system (`prediction_ledger ⋈ rep_actions`).

```sql
CREATE TABLE rep_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number  TEXT NOT NULL,
    rep          TEXT,                 -- ties to reps.name
    action_at    TEXT NOT NULL DEFAULT (datetime('now')),
    action_type  TEXT NOT NULL,        -- contact_attempted | contact_made | response | offer | result
    channel      TEXT,                 -- call | email | door | mail
    response     TEXT,                 -- no_answer | callback | interested | not_interested | hostile
    offer_amount REAL,                 -- when action_type='offer'
    result       TEXT,                 -- deal | dead | pending | redeemed | lost
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_ra_case ON rep_actions(case_number, action_at);
```

**Cached columns on `cases`** (for fast list display/filtering; mirrors the existing
`stage`/`projected_oos` denormalization):

```sql
ALTER TABLE cases ADD COLUMN deal_status   TEXT;   -- not_contacted | contacted | in_conversation | offer_out | won | dead
ALTER TABLE cases ADD COLUMN last_action_at TEXT;
```

**Why a table, not just columns:** rep work is a *sequence* (attempt → made → response → offer →
result); one column set can't hold the trajectory. The table is the log (source of truth); the
two `cases` columns are the cached "where does this deal stand," updated on each new action.

## 6. Reconciliation mechanism (item 3)

Not a table — a function run at the **write choke point**: `create_case` in `backend/main.py`
(~line 526, the single place a case is written *with* a freshly computed projection —
`merged.update(compute_projection(merged))`). **Read paths (`GET /api/cases`, lines ~475/490)
must NOT log** — they recompute for display, not as prediction events.

On each `create_case` (fires on every scrape→sync POST):

1. **Log (if changed).** Compute the projection on the new merged state, derive `input_hash`,
   and append a ledger row **only if `input_hash` differs from the case's most-recent ledger row**
   — OR if `prediction_basis` transitioned (e.g. `filed → judged`) even when dates coincide.
   First-ever prediction for a case always logs. (See §7 for cadence.)

2. **Reconcile.** Run `reconcile(case_number)`: if the case now carries a resolving signal
   (`oos_date` present, or `case_track` dismissed, or a sale date), fill the reconciliation
   columns on that case's **unresolved forward-prediction rows**
   (`outcome_type IS NULL AND prediction_basis IN ('judged','next_hearing','filed')`):
   - `outcome_type` ← `oos_issued` | `dismissed` | `sale`
   - `outcome_date` ← the real date
   - `error_days` ← `(actual_oos − projected_oos)` in days (NULL when the outcome isn't an OOS)
   - `resolved_at` ← now

   Every prediction in the case's history resolves against the **same** actual outcome — that's
   how "at filing we were off 200d, post-judgment off 40d" becomes measurable. Idempotent: once a
   row is resolved it is never touched again.

3. **Expiry sweep (batch, not per-case).** A prediction whose `projected_oos` passed by
   `> PREDICTION_EXPIRY_DAYS` with no outcome → `outcome_type = 'expired_no_oos'`. This turns
   defect-①'s silent stale projections into **measured misses**. Runs as a cheap batch over all
   unresolved forward-prediction rows (a case that goes quiet is exactly when it needs expiry
   marking, so this can't be per-case-on-write) — trigger at the end of a sync run or as a
   standalone maintenance call.

**Idempotency note:** `sync_to_prod --update-existing` re-POSTs every case → `create_case` fires
for each. The `input_hash` dedup (step 1) prevents duplicate ledger rows on unchanged cases, so a
full re-sync does not bloat the ledger.

## 7. Resolved decisions

| # | Question | Decision |
|---|---|---|
| a | Where predictions are logged | **Prod-only, via `create_case`.** Local scrapes log when they *publish* (sync). The prediction that matters is the one tied to published state. Ledger lives in the prod DB (Railway volume — already persistent + covered by the `POST`-based DB-restore/sync tooling). |
| b | `rep_actions` shape | **Full table + 2 cached `cases` columns** (`deal_status`, `last_action_at`). |
| c | Expiry threshold | **90 days**, as the named constant `PREDICTION_EXPIRY_DAYS` (not inline) — the ledger should eventually tell us if 90 is right. |
| d | Is `rep_actions` in this phase? | **Yes, in-phase.** Small independent table; it's the half that answers the ROI question, so it ships alongside — not as a fast-follow that risks never happening. |

**Cadence (Q2):** log on **meaningful change only** — never on read (would be thousands of
identical rows/day), and at write time only when `input_hash` differs from the case's latest row
(or on a `prediction_basis` transition). Every row then represents a real change in what we
believed — clean drift signal, no noise.

**Re-predictions (Q3):** **always a new row**, never version-in-place. The "current" prediction
is the latest row by `predicted_at` (or the denormalized `projected_oos` on `cases`). The per-case
**trajectory** (how accuracy sharpens filing→judgment→OOS) is the most valuable output and only
exists with new-row-per-change.

## 8. Implementation hooks (for the build session)

- **Schema:** add the two `CREATE TABLE`s + the two `ALTER TABLE cases` to `init_db()` in
  `backend/main.py` (boot migration pattern already used there).
- **Constants:** `MODEL_VERSION` + `PREDICTION_EXPIRY_DAYS` next to `CITY_DATA`.
- **Log + reconcile:** hook into `create_case` after the case row is written. Factor a
  `log_prediction(case, projection)` and `reconcile(case_number)`; keep them in `main.py` (same
  module as `compute_projection`, so basis derivation stays in sync with the predictor).
- **Do NOT touch** read paths (~475/490) — display only.
- **Expiry:** a `sweep_expired()` batch, called at end of sync (or a small endpoint / CLI).
- **`scorecard.py` migration:** point it at `prediction_ledger` (resolved forward-prediction rows)
  instead of recomputing against the current `oos_date`. It can then report calibration **by
  `model_version`** — the drift measurement that doesn't exist today.
- **API (rep_actions):** `POST /api/cases/{cn}/actions` (append), `GET /api/cases/{cn}/actions`
  (history); update the cached `deal_status`/`last_action_at` on append. Frontend UI for logging
  actions is a follow-up within this phase.
- **Sync:** `prediction_ledger` and `rep_actions` should ride the existing local↔prod sync path;
  since logging is prod-side via `create_case`, the ledger is generated on prod — confirm whether
  `rep_actions` (entered by reps on prod) needs to sync *back* to local (likely yes, so local
  analysis/scorecard sees them).

## 9. Scope boundary (what this is NOT, this phase)

- **No auto-recalibration.** The ledger is evidence-gathering; `CITY_DATA` stays frozen, changed
  only manually with sign-off at ≥40 closed cases. Building this changes **no constant**.
- **No calibration dashboard UI yet.** v1 = capture + reconciliation + `scorecard.py` reading the
  ledger. The visual predicted-vs-actual view (sliced by stage/complexity/city, always showing n=)
  is a follow-up once rows accrue.
- **Rep-action UI** beyond basic logging endpoints is a follow-up within the phase.

## 10. What the ledger enables (illustrative queries)

```sql
-- Calibration by model version (the drift measurement that doesn't exist today)
SELECT model_version, in_complexity,
       COUNT(*) AS n,
       ROUND(AVG(ABS(error_days))) AS mae_days,
       ROUND(AVG(error_days))      AS bias_days      -- +ve = we predicted too early
FROM prediction_ledger
WHERE prediction_basis = 'judged' AND outcome_type = 'oos_issued'
GROUP BY model_version, in_complexity;               -- always report n=

-- The real ROI question: do accurate, high-confidence predictions convert to deals?
SELECT pl.confidence_pct,
       COUNT(DISTINCT pl.case_number)                         AS predicted,
       COUNT(DISTINCT CASE WHEN ra.result='deal' THEN ra.case_number END) AS won
FROM prediction_ledger pl
LEFT JOIN rep_actions ra ON ra.case_number = pl.case_number AND ra.action_type='result'
WHERE pl.prediction_basis IN ('judged','next_hearing','filed')
GROUP BY pl.confidence_pct;
```
