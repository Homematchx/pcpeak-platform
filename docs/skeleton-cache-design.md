# Skeleton cache + on-demand detail — design

**Status:** DESIGN ONLY — no code until approved.
**Date:** 2026-07-28
**Branch:** `claude/remove-analyze-with-ai-0vu5i9`
**Motivation:** the Elon "delete the requirement" move — the mirror-everything client cache is at
end of life. This deletes it rather than optimizing around it.

---

## 1. The requirement that shouldn't exist

Today the client mirrors **every case in full** to `localStorage` (`tfi_v5`) and re-fetches the whole
book every 30s. Measured facts from this session:

- The full mirror is **6.66 MB** and grows with fleet size. It already exceeds the ~5 MB per-origin
  quota, so `save()` degrades to a **slim cache** (strips `property_intel`).
- The dominant weight is `property_intel` — **~15 KB × 251 cases ≈ 3.7 MB** of blobs the sidebar
  list never reads.
- At 400+ cases the slim path becomes the **permanent** boot state for every rep: `property_intel`
  is never cached, so every case waits out a sync before its detail can render.

Everything we've patched around this — the slim-cache degradation, the stale-intel-panel re-render
(`hasUsableIntel` + the absent→present detection), the open-case intel retention, the quota-safe
`save()` — are **bridges over a seam that shouldn't exist**. The seam is "mirror the whole detail to
the client." Delete it.

## 2. Principle

**The client caches a lightweight SKELETON (what the list needs); it fetches DETAIL on demand (what a
case needs when opened).** The skeleton is tiny and always fits in quota. Detail is fetched fresh
when a case is opened, so it is never stale and never cached to bloat.

This single change:
- **deletes the quota problem** (skeleton for 251 cases is ~200–400 KB, an order of magnitude under
  the ceiling, and scales to thousands of cases);
- **deletes the slim-cache machinery** and the stale-intel-panel class *at the root* (detail is
  always fetched fresh, so there is no "stripped intel" state to recover from);
- **shrinks the sync payload** from 6.66 MB to the skeleton;
- keeps the always-fresh-on-open guarantee the mirror never actually gave (it gave you stale-cached
  intel, or none).

## 3. The field split — the crux of the whole design

Everything hinges on this line: **what does the sidebar list actually read, vs what only the detail
reads?** Grounded by auditing `getFilteredCases`, the card render, and the filter helpers.

### 3.1 SKELETON — what the list + filters need (cached, synced)

Per-card render and every filter read only these (the case row **minus the heavy blobs**):

`case_number, property_address, defendant, all_defendants, city, rep_assigned, complexity,
def_count, estate_heir, stage, oos_issued, oos_date, sale_pulled_date, judgment_date, judgment_type,
account_number, account_status, account_note, case_track, property_type, disposition_state,
disposition_code, disposition_at, pending_review, pending_review_code` — **plus two values extracted
from `property_intel`:** `current_tax_balance` and `market_value`.

Those two are the catch: `caseLiveBalance` / `balanceBand` (the balance chip + the amount-owed
filter) and `caseTrack` (dismissed_owing vs dismissed_paid) currently `parseIntel(c.property_intel)`
for every card. Under the skeleton there IS no `property_intel` on a non-open case, so **the balance
must be a first-class skeleton field**, and those helpers must read it from the skeleton, not parse a
blob. This is the central refactor (§5.2).

### 3.2 DETAIL — what only an opened case needs (fetched on demand, never cached full)

`property_intel` (the full ~15 KB blob — DCAD/ACT enrichment, ownership, payment history, tax tables),
`ai_memo`, `tax_breakdown`, `delinquency_years`, `prior_suits`, and the case's `events`. These feed
the detail tabs (Property Intel, Acquisition/land workbench, Financials, Timeline, Defendants) — none
of which the sidebar list renders.

## 4. Backend

Two endpoints — one new, one already exists.

- **`GET /api/cases` gains a skeleton shape.** It returns every case **without `property_intel` /
  `ai_memo` / the heavy JSON columns**, but WITH `current_tax_balance` and `market_value` extracted
  as top-level fields (one cheap `json.loads` per row server-side, or — better — persist them as real
  columns so no parse is needed; see open decision §8.1). Chosen over a separate endpoint so the sync
  path stays one request. `sync_to_prod` reads `/api/cases` only for case numbers + dedupe, so a
  skeleton payload doesn't affect it (verify in §7).
- **`GET /api/cases/{cn}` — already returns full detail + events.** No change. This is the on-demand
  detail fetch the client already has available.
- The bulk **`GET /api/events`** (shipped today) stays — the list needs no events, but the Timeline
  tab does; open decision §8.3 is whether events move to on-demand too or stay bulk.

## 5. Frontend — the four touch points

### 5.1 Boot + sync (the hot path)
- Boot: `cases = JSON.parse(localStorage.tfi_v5)` — now a skeleton array. Always fits.
- `syncFromPlatform`: fetch the skeleton `/api/cases` (+ stats + reps), rebuild `cases` from
  skeletons, `save()` the skeleton. The dedup/heal/activeId logic is **unchanged** — it operates on
  case identity, not on `property_intel`.

### 5.2 The list-level helpers — read the skeleton, not the blob (the central refactor)
`caseLiveBalance`, `balanceBand`, and `caseTrack` change from `parseIntel(c.property_intel)
.current_tax_balance` to reading `c.current_tax_balance` (the skeleton field). Behavior is identical
when the balance is present; the difference is it no longer requires the full blob. This is the one
change with real blast radius — it's covered by `test_balance_card` and the disposition suites.

### 5.3 Open a case → fetch detail on demand (the new behavior)
`selectCase(id)`: if the case's detail (`property_intel`) isn't loaded yet, `GET /api/cases/{cn}`,
merge the detail onto the in-memory case, THEN `renderDetail`. Show a one-line "loading detail…"
while it fetches (a sub-200 ms call). Cache the fetched detail on the in-memory object for the
session (so re-opening is instant) — but NEVER write detail back to `localStorage` (that's the bloat
we're deleting). A lightweight in-memory LRU cap (e.g. keep detail for the last ~20 opened cases) if
we want to bound session memory; open decision §8.2.

### 5.4 Delete the slim-cache machinery
`save()` collapses to a single `localStorage.setItem` (skeleton always fits; keep the try/catch as a
belt-and-suspenders no-throw). **Deleted:** the slim-copy fallback, `hasUsableIntel`, the
absent→present intel re-render, the open-case intel retention, `openIntelBefore` — the entire
stale-intel-panel apparatus, because detail is now always fetched fresh on open. `test_intel_reload`
and `test_quota_save` are retired/rewritten (§6).

## 6. What this DELETES (the point)

- The 6.66 MB → quota ceiling. Gone.
- `save()`'s slim degradation + the "property_intel never cached at scale" failure.
- The stale-intel-panel re-render logic (fix #1/#2 from the 2026-07-23 work) — moot at root.
- ~3.7 MB off every sync payload.
- Two tests become obsolete (`test_intel_reload`, `test_quota_save`) — replaced by simpler
  on-demand-detail tests. Deleting tests for deleted machinery is correct, not a coverage loss.

## 7. Rollout — incremental, because this is the most-patched path

A big-bang rewrite of boot+sync+selectCase risks regressing a dozen hard-won fixes (selection
stability, rep dedup, events-batching). Phase it so each step is independently shippable and verified:

- **Phase 1 (backend, additive, invisible):** add `current_tax_balance` + `market_value` to the
  `/api/cases` payload (extracted or as columns). No frontend change. Verify sync_to_prod unaffected.
- **Phase 2 (frontend, no behavior change):** refactor `caseLiveBalance`/`balanceBand`/`caseTrack` to
  read the new top-level fields, with a fallback to `parseIntel(property_intel)` while both exist.
  Full mirror still in place — pure equivalence refactor, green on the existing suites.
- **Phase 3 (the switch):** `/api/cases` stops sending `property_intel`; boot/sync/save use the
  skeleton; `selectCase` fetches detail on demand. This is the real change — gated behind its own
  verification (all hot-path browser suites + new on-demand tests).
- **Phase 4 (delete):** remove the slim-cache/stale-intel machinery and the obsolete tests.

Each phase is a separate commit through the normal deploy gate. Phase 3 is the one to scrutinize.

## 8. OPEN DECISIONS — need a call before build

1. **`current_tax_balance` / `market_value`: extract-per-request or persist as columns?** Extracting
   (`json.loads` per row in `/api/cases`) is zero-migration but adds CPU per sync. Persisting them as
   real `cases` columns (written when `property_intel` is saved) is cleaner and lets the balance
   filter/sort run in SQL later, but is a migration + a write-path change. **Recommend: persist as
   columns** — it's the durable answer and pays off for server-side filtering. Which?
2. **In-memory detail cache: unbounded for the session, or an LRU cap (~20 cases)?** Unbounded is
   simplest; a rep opening 200 cases in one session re-accumulates the blobs in memory (not
   localStorage). An LRU cap bounds it. **Recommend: LRU ~20** — trivial and bounds memory.
3. **Do `events` also go on-demand, or stay bulk?** We just shipped bulk `/api/events` (one call,
   4,583 events / 121 ms). The Timeline tab is detail, so events *could* move to the on-demand
   `/api/cases/{cn}` fetch (which already returns events) and the bulk endpoint retire. **Recommend:
   move events on-demand too** — it further shrinks the sync and the detail fetch already carries
   them; the bulk endpoint becomes unused. But this reverts today's batching change, so confirm.
4. **Loading affordance on open:** a spinner line vs rendering the skeleton fields immediately then
   filling detail. **Recommend: render what the skeleton has instantly (address, balance, stage),
   then fill the tabs when detail lands** — no blank flash, and the header/overview is useful before
   the blob arrives.

## 9. Validation plan

- **Skeleton completeness:** a test asserting every field the list/filters read is present in the
  skeleton payload (so a card never renders blank because a field moved to detail).
- **Equivalence (Phase 2):** the refactored `caseLiveBalance`/`caseTrack`/`balanceBand` return
  identical results reading the skeleton field vs parsing `property_intel`, across the
  `test_balance_card` and disposition fixtures.
- **On-demand detail:** open a case whose detail isn't loaded → asserts a single `GET /api/cases/{cn}`
  fires, the panel fills, and re-opening fires NO second fetch (session cache). Pins that the list
  never triggers detail fetches (only opening does).
- **Quota is gone:** boot + sync + save with 500 synthetic skeleton cases stays well under quota and
  `save()` never degrades.
- **No regression on the hot path:** the full browser suite (selection-stability, rep-*, disposition,
  land, balance, events-batch) green after Phase 3.
- **sync_to_prod unaffected:** a dry-run against the skeleton `/api/cases` still reconciles.

## 10. Explicitly out of scope

- Server-side filtering/pagination of the case list (the skeleton makes it *possible* later; not now).
- Changing the local-scrape / cloud-serve architecture (the deeper seam) — this deletes the client
  mirror, not the sync model.
