# Case Disposition System — design (revised)

**Status:** DESIGN — all seven open decisions settled (§12). Awaiting approval of this revision.
**No code until approved.**
**Date:** 2026-07-23
**Branch:** `claude/remove-analyze-with-ai-0vu5i9`

---

## 1. Step one — TRACE: what the 🗑 Remove button actually does today

Established by reading the code, not inferred. **Finding: `Remove` makes no network call of any
kind. It is a client-side localStorage edit that a background sync silently undoes ~30 seconds
later.**

### 1.1 The call chain

| # | Surface | Line | Handler |
|---|---|---|---|
| 1 | Detail-card header `🗑 Remove` | [index.html:1363](frontend/index.html:1363) | `delCase(c.id)` |
| 2 | Sidebar card `×` | [index.html:2368](frontend/index.html:2368) | `delCase(c.id, event)` |
| 3 | Sidebar `🗑 Clear All` | [index.html:386](frontend/index.html:386) | `clearAll()` |

`delCase` in full ([index.html:2520](frontend/index.html:2520)):

```js
function delCase(id, e) {
  if (e) e.stopPropagation();
  if (!confirm("Remove this case?")) return;
  cases = cases.filter(x => x.id!==id);
  if (activeId===id) { activeId = null; /* …show empty state… */ }
  save(); renderList();
}
```

That is the entire implementation. `save()` ([index.html:2540](frontend/index.html:2540)) writes
the `tfi_v5` localStorage key. There is **no `fetch`**, no POST, no DELETE.

### 1.2 The backend delete is unreachable

`DELETE /api/cases/{case_number}` ([backend/main.py:1133](backend/main.py:1133)) is the BPP-only
guarded delete, with the constraint carried in the WHERE clause (`AND property_type='personal'` → a
real case matches 0 rows → 409). The handoff note was right to flag it — and the trace confirms
**nothing in the frontend calls it.** The only `method:"DELETE"` fetch anywhere in `index.html` is
`/api/reps/{id}` at [index.html:1976](frontend/index.html:1976) (rep soft-delete). The guarded case
delete is invoked only by operator tooling (`purge_test_case.py` path / manual curl).

### 1.3 What actually happens to a removed case

`setInterval(syncFromPlatform, 30000)` ([index.html:956](frontend/index.html:956)) → the
authoritative rebuild at [index.html:647](frontend/index.html:647):

```js
cases = platformV3.concat(drafts);   // drafts = LOCAL-ONLY (inputMethod||uploadedAt), not on platform
```

So for any case that exists on prod, the removal survives at most until the next sync tick and the
card **reappears with no explanation**. Removal only persists for genuine local drafts — and even
then only as far as the cache holds: `save()` is deliberately crash-proof and, at localStorage
quota, degrades to a slim copy or disables the cache entirely
([index.html:2549–2563](frontend/index.html:2549)), in which case a reload restores the case from
prod anyway. `clearAll()` has the same property at whole-book scale.

### 1.4 Verdict

**Nothing to flag as destructive — no real case is hard-deleted, on prod or locally.** The
`prod_ready` gate, the BPP WHERE-clause guard, and the ledger.db restore guard are all intact and
untouched by this path.

The defect is the opposite one, and it is a trust defect: **Remove is a dead affordance that
appears to work.** A rep clicks it, confirms a modal that says "Remove this case?", watches the case
vanish, and finds it back in the list half a minute later. The platform currently has *no* way to
say "this case is done, stop showing it to me" — and the button that looks like that mechanism
silently isn't one. That is the gap this system fills.

---

## 2. Principles

1. **Archive, never delete.** No case leaves the platform. Disposition is a state, not a removal.
2. **The decision is prod-owned and non-regenerable** — same class as `rep_actions`. It lives in
   `ledger.db` behind the restore guard, append-only, so a raw `pcpeak.db` restore physically cannot
   erase a human's judgment.
3. **Propose → confirm.** The engine flags; a human commits. Nothing auto-archives, and nothing
   auto-reopens. Same shape as the comp workbench.
4. **Every disposition carries who, when, why.** A code without a human and a timestamp is not a
   disposition, it's a mutation.
5. **Outcomes come from docket evidence, never from human filing decisions.** A `sold_at_tax_sale`
   disposition is a rep's judgment; the docket's sale entry is the fact. The prediction ledger reads
   the fact. (§7)
6. **Each log records only what it is authoritative for.** Unification happens at read time, never
   by writing a fact into a table that means something else. (§3.5)
7. **Counts reconcile.** Any view that hides cases states its denominator. active + watching +
   archived == total, visibly.
8. **Dismissal ≠ resolved.** The taxonomy must not let "dismissed" become a way to archive a live
   lead — dismissed-owing is the core pipeline. (§4.3)

---

## 3. Data model

### 3.1 States

Three states, on the case: **`active` → `watching` → `archived`**, freely reversible in any
direction by a human decision.

| State | Meaning | Default rep queue | Fleet analyses | Queryable |
|---|---|---|---|---|
| `active` | Normal working case. | yes | yes | yes |
| `watching` | A warm lead that legitimately comes back — plans default, loans mature, circumstances change. Out of the working queue, **never filed next to `duplicate`.** | no — own section | yes | yes |
| `archived` | Filed. Out of default views entirely. | no | no (excluded, stated) | **yes, permanently** |

**Review-flagging is orthogonal to state, not a fourth state.** An auto-flag never changes what a
case *is*; it only marks that a human should look. An active case can be flagged (a proposal), and
so can a `watching` or `archived` one (an invalidation — §6). A flagged case keeps every property of
its current state.

### 3.2 `ledger.case_dispositions` — append-only decision log (the record)

```sql
CREATE TABLE IF NOT EXISTS ledger.case_dispositions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number    TEXT NOT NULL,
    entered_at     TEXT NOT NULL DEFAULT (datetime('now')),
    kind           TEXT NOT NULL,      -- proposal | decision | dismissal
    state          TEXT,               -- decisions only: active | watching | archived
    code           TEXT NOT NULL,      -- taxonomy code (§4)
    comment        TEXT,               -- required for some codes (§4)
    decided_by     TEXT,               -- rep name — SELF-ATTESTED (§9)
    source         TEXT NOT NULL,      -- human | auto_flag
    evidence       TEXT,               -- JSON: what the auto-flag saw (balance, event id, …)
    model_version  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger.idx_cd_case ON case_dispositions(case_number, id);
```

Added to `PROD_OWNED_TABLES` ([backend/main.py:115](backend/main.py:115)) so the `get_db()`
authorizer denies DELETE/DROP.

Three row kinds, all appended, nothing ever updated:

- **`proposal`** — the engine flags (`source='auto_flag'`, `state` NULL, `evidence` populated).
- **`decision`** — a human commits a code and the state it puts the case in.
- **`dismissal`** — a human rejects a proposal; the case stays exactly as it is.

Both derived facts read off the same append-only sequence:

- **Current state** = the `state` of the latest `decision` row, else `active`.
- **Open review flag** = a `proposal` row exists with `id` greater than the latest
  `decision`-or-`dismissal` row.

So a proposal is closed by *any* subsequent decision or dismissal, with no in-place update and no
resolution column to keep consistent. Reversal is a new `decision` row (e.g. back to `active`),
never an edit.

### 3.3 `ledger.case_comments` — per-case rep thread

```sql
CREATE TABLE IF NOT EXISTS ledger.case_comments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number    TEXT NOT NULL,
    author         TEXT,
    body           TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    disposition_id INTEGER             -- set when the comment accompanies a disposition
);
CREATE INDEX IF NOT EXISTS ledger.idx_cc_case ON case_comments(case_number, id);
```

Also prod-owned and append-only.

**Why not `rep_actions` with `action_type='comment'`** — `rep_actions` drives the derived
`deal_status` cache (`not_contacted|contacted|in_conversation|offer_out|won|dead`,
[backend/main.py:518](backend/main.py:518)). A comment routed through it means every consumer of
that derivation must remember to exclude it, or a rep jotting *"drove by, looks vacant"* silently
advances the case from `not_contacted` to `contacted` — a fabricated funnel state, the same shape of
bug as the sale-pulled stat contradiction. **A note must never advance `deal_status`.** The funnel
log stays rigid and typed; comments get their own table; the UI interleaves them (§3.5).

### 3.4 Cache columns on `cases` (rebuildable, never authoritative)

Exactly the `deal_status`/`last_action_at` pattern ([backend/main.py:515](backend/main.py:515)):

| Column | Values |
|---|---|
| `disposition_state` | `active` (default) / `watching` / `archived` |
| `disposition_code` | taxonomy code, NULL while never dispositioned |
| `disposition_at` | timestamp of the current decision |
| `pending_review` | `0` / `1` — an open proposal exists |
| `pending_review_code` | the proposed code, NULL when none |

All recomputable from `case_dispositions` at any time. **Added to
`sync_to_prod.SKIP_CASE_FIELDS`** ([sync_to_prod.py:61](sync_to_prod.py:61)) alongside
`rep_assigned`/`prod_ready` — a local sync must never push a disposition up or clobber one.

### 3.5 Unified history — merged at READ, never at write

`case_snapshots` is untouched: no disposition rows, no new `source` value. Its purpose is detecting
*machine* derivation bugs on re-scrape — its signal is "this value moved with no docket evidence,"
and a disposition has no docket evidence by nature and never will. Writing human decisions into it
would make that signal mean two different things.

Instead, **`GET /api/cases/{cn}/history` merges three append-only logs into one chronological
timeline** — `case_snapshots` (field changes), `case_dispositions` (decisions), `case_comments`
(notes) — each row tagged with its origin. One read gives *"balance hit zero → dismissal entered →
archived as paid — Jay Lewis, 2026-07-23"*, while each table on disk still records only what it is
authoritative for.

The same principle gives the per-case activity thread: `rep_actions` + `case_comments` interleaved
at read, stored apart (§3.3). Both existing endpoints
(`/api/cases/{cn}/snapshots`, `/api/cases/{cn}/actions`) keep their current shape and consumers
(`evidence_gaps.py`, `backup_ledger.py`, the export) are unaffected.

---

## 4. Disposition taxonomy — 15 codes

`state` = the state a committed decision puts the case in. `comment` = free-text required at
decision time. `invalidation` = §6 predicate that can re-flag the case later.

### 4.1 Group A — taxpayer resolved the delinquency

| Code | Label | State | Comment | Invalidation |
|---|---|---|---|---|
| `paid_in_full` | Paid / zero balance | archived | no | balance > 0 |
| `payment_plan_33_02` | §33.02 payment plan | **watching** | yes | plan default |
| `tax_loan_32_06` | §32.06 tax-loan transfer | **watching** | yes | new suit activity |

`paid_in_full` requires a **real `0.0`** live balance — never `None`/unknown (standing
falsy-conflation rule). §33.02 plans default routinely and §32.06 transferees mature their liens;
both stay visible, with the counterparty and lien-stack change recorded in the comment.

### 4.2 Group B — property or owner left our scope

| Code | Label | State | Comment | Invalidation |
|---|---|---|---|---|
| `ownership_changed` | Ownership changed | archived | yes | — |
| `sold_at_tax_sale` | Sold at tax sale | archived | no | — |
| `not_distressed` | Not distressed | archived | yes | balance jump / new judgment |
| `out_of_market` | Out of market | archived | yes | — |

`out_of_market` sits here rather than under data quality: a Rowlett property in a Dallas County
suit is a real scope fact, not a defect in our record.

### 4.3 Group C — court outcome

| Code | Label | State | Comment | Invalidation |
|---|---|---|---|---|
| `dismissed_resolved` | Dismissed — nothing owed | archived | no | balance > 0 |

**Guarded server-side: selectable only on a real `0.0` live balance.**

⚠ **There is deliberately no plain `dismissed` code.** A dismissed case that still owes tax is the
*core lead pipeline* (`case_track='dismissed_owing'`, [backend/main.py:83](backend/main.py:83); the
[[dismissal-not-resolved]] finding — TX-23-00423 was dismissed still owing $72k, then closed at
$108k). Offering "dismissed" as a one-click archive would let the most valuable bucket on the
platform be filed away as finished. Balance >0 or unknown → the case stays active. Enforced in the
endpoint, not by UI convention.

### 4.4 Group D — PC Peak outcome

| Code | Label | State | Comment | Invalidation |
|---|---|---|---|---|
| `acquired` | Acquired by PC Peak | archived | yes | — |
| `lost_to_competitor` | Lost to competitor | archived | yes | — |
| `owner_declined` | Owner declined | **watching** | yes | sale date approaching |
| `unable_to_contact` | Unable to contact | **watching** | yes | new owner/mailing address |
| `no_go_underwriting` | No-go — underwriting | archived | yes | — |

Kept at five deliberately. These are three genuinely different lessons — our offer lost, our
approach lost, our own gates said no — and they are the only ground truth the acquisition verdicts
will ever be scored against. The person who lived the deal can pick correctly without thinking.

### 4.5 Group E — data quality

| Code | Label | State | Comment | Invalidation |
|---|---|---|---|---|
| `duplicate` | Duplicate of another case | archived | yes | — |
| `data_quality` | Unusable record | archived | **yes** | account resolved after being unresolvable |

Collapsed from four. Facing a garbled record, `bad_data` vs `wrong_property_type` is genuinely
ambiguous to a rep — and a confidently-labeled wrong code is fabricated data in the calibration set.
These also carry the *lowest* calibration value, describing our pipeline rather than the market. The
required comment holds the specifics. `duplicate` stays separate: it implies a distinct action and
its comment must name the surviving case number.

---

## 5. Auto-flag / human-decide

The engine **proposes**; a human **commits**. A proposal appends a `kind='proposal'` row with
`source='auto_flag'` and its `evidence` JSON. **A proposal never changes state** — the case keeps
every property it had and simply renders a review chip.

Proposal rules (v1), evaluated on write in `create_case` alongside the snapshot diff, idempotent (an
already-open proposal for the same code is not re-appended):

| Signal | Proposes | Guard |
|---|---|---|
| ACT live balance is a real `0.0` | `paid_in_full` | Never fires on `None`/unknown. Evidence records the balance and its `enriched_at`. |
| Docket dismissal event **and** balance is real `0.0` | `dismissed_resolved` | Dismissal alone never flags — §4.3. |
| Docket sale-completed event | `sold_at_tax_sale` | Evidence carries the `docket_events.id`. |
| DCAD owner-of-record changed to a non-defendant after filing | `ownership_changed` | Reuses the heir-gate name-token logic so estate/heir overlap doesn't trip it. |

A human may commit a **different** code than the one proposed, or reject it outright
(`kind='dismissal'`). The proposal is a prompt, never a default that gets rubber-stamped.

---

## 6. Invalidation predicates — re-flagging a dispositioned case

Asking *"what would we propose for this case?"* against a dispositioned case produces permanent
noise: an `acquired` case has a zero balance forever and would re-propose `paid_in_full` at every
write. The question is narrower — **does this disposition's premise still hold?**

Each code carries an optional predicate (§4 tables). When one fires, it appends a `proposal` row
against the case, surfacing it in a review queue. It **never auto-reopens and never changes state**
— a human decides, or dismisses the flag.

- `paid_in_full` / `dismissed_resolved` — balance goes positive. The premise is falsified.
- `payment_plan_33_02` — balance jumps or new docket activity after the decision: the plan
  defaulted. **This predicate is the reason `watching` earns its keep.**
- `tax_loan_32_06` — new suit activity against the property.
- `owner_declined` — `oos_date`/`sale_scheduled_date` set or inside the re-approach window. People
  change their minds as the sale closes.
- `unable_to_contact` — a new owner or mailing address appears in enrichment: a new contact path.
- `not_distressed` — material balance jump or a new judgment.
- `data_quality` — `account_status` becomes `resolved` after having been `needs_lookup`/`invalid`.

Codes with no predicate (`acquired`, `sold_at_tax_sale`, `ownership_changed`, `duplicate`,
`out_of_market`, `lost_to_competitor`, `no_go_underwriting`) never re-flag. A predicate for
`no_go_underwriting` on a materially changed payoff picture is a logged candidate, not v1.

---

## 7. Calibration — and the provenance line

Disposition codes are captured as outcome labels for future calibration. `acquired`,
`sold_at_tax_sale`, `owner_declined`, `lost_to_competitor` are precisely the ground truth the
Mission Score and the acquisition verdicts have never been scored against.

**Dispositions do NOT write `prediction_ledger.outcome_type`. No carve-out.** Recorded as a
standing principle:

> **The prediction ledger's outcomes come from docket evidence, never from human filing decisions.**
> A `sold_at_tax_sale` disposition is a rep's judgment; the docket's sale entry is the fact. If the
> ledger ever ingests sale outcomes, it reads them from `docket_events` — same provenance discipline
> as everything else on this platform.

The two logs stay joinable-but-separate on `case_number`, so *"what did our GO verdicts actually
produce"* is a query, not a schema entanglement. This is the §G land-floor discipline applied again:
a value that is displayed and recorded but feeds no calculation it hasn't earned.

Calibration itself is **out of scope for v1** — we capture labels; nothing consumes them yet.
Consistent with the ≥40-sample freeze rule ([[city-data-frozen-sample-size]]): there won't be enough
dispositions to calibrate anything for a long while, and capturing early is how we get there.

---

## 8. Visibility, queues, and counts

**Archived is the hard server-side line; `watching` is a queue-composition rule.**

- `GET /api/cases` — defaults to `disposition_state != 'archived'`. Every row carries
  `disposition_state`, so `watching` cases ship with the payload and stay in fleet analyses (a
  watching case is still a real lead). Params: `?state=<active|watching|archived>` and
  `?include_archived=1`.
- `GET /api/cases/{cn}` — **unchanged, always returns the case** regardless of state, plus its
  disposition history. Permanently queryable by case number is non-negotiable.
- `GET /api/stats` — returns `active_cases`, `watching_cases`, `archived_cases`, `total_all`. Four
  numbers that visibly reconcile, rather than one that silently shrinks.
- **Frontend queue composition:** the default sidebar list and rep working queues show `active`
  only; `watching` gets its own section with a count; archived is behind an explicit filter.
- Fleet analyses (`fleet-static-fire`, acquisition batch runs, `scorecard.py`) — exclude archived
  and **must print which denominator they used**. A report saying "220 cases" when 40 are archived
  is the silent-number-gap this project has already fixed twice.
- Held-for-review queue and `local_held_cases` — exclude archived.
- `sync_to_prod` — **unaffected.** An archived case still receives fact updates from local
  re-scrapes (a defaulted §33.02 plan and a re-scrape that fixes a `data_quality` case must both
  surface). Disposition columns are skip-listed so the sync cannot touch the decision.

Because the frontend rebuild is `cases = platformV3.concat(drafts)`, an archived case dropping out
of `/api/cases` means **Remove finally has a real, durable effect** — the card goes away and stays
away, with no change to the rebuild logic.

---

## 9. Attribution and access

`decided_by` is a pick from the rep roster and is **self-attested** — the platform has no
authentication (the API is open; only ledger export and the scrape trigger are token-gated). It is
labeled as such in the UI, using the verified/estimated/inferred vocabulary already in use.

**Archiving stays open, not token-gated.** The action is reversible, append-only, attributed, and
archived cases remain permanently queryable — **the audit trail is the control, not the gate.** One
addition closes the remaining gap: a **"recently archived" review surface** showing `decided_by`, so
a wrong archive is noticed rather than silent.

Standing up authentication inside a disposition build is how architectural commitments get made
without being decided. It is logged as its own named initiative (§11).

---

## 10. API + UI surface

### 10.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/cases/{cn}/disposition` | Current state, code, open proposal, full log. |
| `POST` | `/api/cases/{cn}/disposition` | Commit `{code, comment, decided_by}`. Derives state from the code (§4); validates code, per-code comment, and the `dismissed_resolved` balance guard. |
| `POST` | `/api/cases/{cn}/disposition/dismiss` | Reject an open proposal `{comment, decided_by}` — case unchanged. |
| `POST` | `/api/cases/{cn}/disposition/reopen` | Append a `decision` row returning the case to `active`. |
| `GET` | `/api/cases/{cn}/history` | The merged timeline — snapshots + dispositions + comments (§3.5). |
| `GET`/`POST` | `/api/cases/{cn}/comments` | Thread, chronological / append `{body, author}`. |
| `GET` | `/api/dispositions` | Fleet roll-up by code/state, the review queue, and recently-archived. |

Open on read (case facts), consistent with `/api/cases/{cn}/snapshots`.

### 10.2 UI

- **`🗑 Remove` → `Dispose…`**, opening a modal: grouped code picker, comment box (required-marked
  per code), rep selector for `decided_by`, and a plain-language confirm line that names the
  resulting state — *"Archive TX-26-01190 as Paid / zero balance. It leaves your active list and
  stays permanently searchable."* / *"Move TX-26-01379 to Watching as §33.02 payment plan. It leaves
  your queue; we'll flag it if the plan defaults."*
- **Sidebar `×`** — same modal. No path disposes a case without a code.
- **`Clear All` — deleted.** It has no meaning under archive-never-delete and is a whole-book
  footgun that already does nothing durable.
- **Review chip** on flagged cards + a "Needs disposition review" queue (covers both new proposals
  and invalidations).
- **Sidebar sections/filters:** active list, a `Watching (n)` section, an archived filter (off by
  default), and a count line — *"127 active · 8 watching · 14 archived"*.
- **Case detail:** a Disposition section (state, code, who/when, log) and the merged activity
  thread.

### 10.3 Migration

Every existing case starts `disposition_state='active'` with no disposition row. **No backfill** —
nothing infers a disposition from `case_track` or a zero balance. Same discipline as the
`property_type IS NULL` non-backfill decision: a machine-inferred disposition attributed to nobody is
the fabricated-value pattern this project has already had to strip out three times. The auto-flag
surfaces the obvious ones for a human on the next write.

---

## 11. Explicitly out of scope

- **Platform authentication & identity — its own named future initiative.** Triggered when reps
  beyond the owner are daily users; not built sideways here (§9).
- Calibrating anything from disposition labels — v1 captures only (§7).
- Bulk disposition (multi-select archive). Deliberate: each decision stays a considered one.
- Any change to the BPP guarded delete, `prod_ready`, or the sync gate.

---

## 12. Decision record

| # | Decision | Call |
|---|---|---|
| 1 | `case_snapshots` write-through | **Read-time merge** — both logs stay honest about what they record (§3.5). |
| 2 | Taxonomy granularity | **15 codes.** Group D kept at five; Group E collapsed to `duplicate` + `data_quality`-with-required-comment; `out_of_market` moved to Group B (§4). |
| 3 | Disposition → prediction ledger | **Separate, no carve-out.** Outcomes come from docket evidence, never human filing decisions (§7). |
| 4 | Comments storage | **Separate table.** A note must never advance `deal_status` (§3.3). |
| 5 | `decided_by` / access | **Self-attested + labeled; archiving open; recently-archived review surface.** Auth logged as its own initiative (§9, §11). |
| 6 | Re-flagging dispositioned cases | **Per-code invalidation predicates**, never auto-reopen, review queue only (§6). |
| 7 | Third state | **`watching` approved** — active / watching / archived (§3.1). |
| — | No plain `dismissed` code | **Approved as designed**, with the server-side `0.0`-balance guard (§4.3). |

**Count correction:** the prior revision's §9.2 said "15 codes" while §4 listed **16**. The revised
taxonomy is 15 by construction: 3 + 4 + 1 + 5 + 2.

---

## 13. Validation plan

Proof before trust, per the project standard:

- **Trace regression:** a browser test asserting `Dispose…` POSTs, and that an archived case **stays
  gone across a `syncFromPlatform` tick** — the exact failure the current Remove has.
- **Restore guard:** `test_restore_guard` auto-rises as `case_dispositions`/`case_comments` join
  `PROD_OWNED_TABLES`; pin that DELETE/DROP are denied.
- **Append-only derivation:** current state and open-flag are derived purely from row order — pin
  decision → proposal → dismissal → decision sequences, including reversals, with no row ever
  updated.
- **Guards:** `dismissed_resolved` refused when balance is >0 **or unknown**; required-comment codes
  refused without one; unknown code refused.
- **`deal_status` isolation:** posting a comment leaves `deal_status` byte-identical.
- **Count reconciliation:** `active + watching + archived == total` pinned in `/api/stats`;
  `/api/cases` default excludes archived only, and `?include_archived=1` restores exactly the
  archived set.
- **Non-interference (the §G land-floor pattern):** pin that disposing a case changes **nothing**
  about its acquisition analysis, MAO ladder, Mission Score, gates, or prediction ledger —
  byte-identical with and without a disposition.
- **Sync isolation:** a local `sync_to_prod` run against an archived case updates its facts and
  leaves `disposition_*` untouched.
- **Auto-flag precision on real data:** run the proposal rules read-only across all 220 live cases
  and hand-check every proposal — specifically that no `dismissed_owing` case is ever proposed for
  archive, and that unknown balances never propose `paid_in_full`.
- **Invalidation precision:** pin that an `acquired` case with a zero balance produces **no** flag,
  while a `paid_in_full` case whose balance goes positive produces exactly one.
- **Reversibility:** archive → reopen → the case returns to active views with its full history
  intact.
