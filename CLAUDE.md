# PC Peak Tax Foreclosure Intelligence Platform

**Live site:** taxforeclosureanalyzer.com
**Railway project:** gracious-tenderness
**GitHub:** Homematchx/pcpeak-platform
**Working directory:** `~/Downloads/pcpeak_platform`

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

### OPEN — credential rotations (tracked, NOT done)
These are still outstanding; do not assume resolved:
- [ ] Rotate the **GitHub PAT** embedded in the `origin` remote URL (`ghp_…`); switch
      to a git credential helper instead of a token-in-URL.
- [ ] Rotate the **2Captcha key** — it is hardcoded as a default in `discover.py`
      (`TWO_CAPTCHA_KEY = os.environ.get("TWO_CAPTCHA_KEY", "<literal>")`) and was
      exposed in terminal output; remove the hardcoded default too.
- [ ] Rotate the **Anthropic key** if it was ever pasted in plaintext; keep it in env
      only (it is no longer in Railway).

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

### Steps 2-5 — not started
Backfill closed 2024/2025 cases, deed/lien-index research, geocoding + legal parsing,
nearest-neighbor benchmark matching. Harden account extraction (Garland 5-digit /
Carrollton empty accounts) at the FRONT of step 2 before backfilling more.
