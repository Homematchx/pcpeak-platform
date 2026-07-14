# PC Peak Tax Foreclosure Intelligence Platform

**Live site:** taxforeclosureanalyzer.com
**Railway project:** gracious-tenderness
**GitHub:** Homematchx/pcpeak-platform
**Working directory:** `~/Downloads/pcpeak_platform`

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
