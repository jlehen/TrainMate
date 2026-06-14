# Design: Calendar-Sourced Daily Context

**Status:** Implemented · **Date:** 2026-06-14 · **Branch:** `richer-analysis-evidence-claude`

This document captures the agreed design for letting TrainMate ingest **external
daily context signals** (alcohol intake, sleep quality, stress, big meals, …)
without TrainMate understanding any of those domains specifically. Signals arrive
on the **existing Google Calendar** as tagged all-day events; TrainMate reads
them, persists them locally, and feeds them to the coach as context.

Implemented on this branch (schema, `db/dailycontext.py`, calendar `sync_context`,
the `garmin` bridge, and the coach analysis wiring). This doc is the spec; see
ARCHITECTURE.md §13 for the as-built summary.

---

## 1. Motivation

The user already tracks things outside TrainMate (alcohol in a spreadsheet) that
demonstrably move Garmin recovery metrics — e.g. 2 drinks after a hard day tanks
the next morning's numbers. The coach currently can't see this, so it
misattributes the dip to training load.

We do **not** want TrainMate to learn about alcohol, spreadsheets, or any
specific signal: that's too narrow and would repeat for sleep, stress, meals,
etc. Instead we want **one generic channel** through which any external source
can hand TrainMate a dated, optionally-quantified signal.

Google Calendar is already wired in (`google_calendar.py`, service account) and
is human-visible and editable from a phone, which makes it a natural generic
inbox. An external syncer (a separate repo, mirroring how `GarminScraper` feeds
Garmin data — see `DESIGN_garmin_direct_pull.md`) drops one event per
signal-day; TrainMate ingests them.

---

## 2. Goals / Non-Goals

**Goals**
- A **single** calendar carries both TrainMate's workouts and external context
  events; the two are unambiguously distinguishable.
- TrainMate ingests context events into its **own DB** so the coach reads
  locally and doesn't hit Google on every run.
- Ingestion is a **sync, not an append**: edits and deletions of a context event
  are reflected (a corrected "2 drinks" → "3 drinks" updates in place; a deleted
  event removes the row).
- TrainMate stays **domain-agnostic**: it knows "there is a signal of category X
  on day D, optionally with numeric value V and some free text." It never
  hardcodes what any category *means*.
- The coach uses signals **qualitatively today** (the LLM connects "alcohol: 2"
  to a poor recovery morning from its own world knowledge).
- The schema **leaves the door open** to future quantitative use (generic,
  category-agnostic correlation) without a migration.

**Non-Goals**
- The external syncer itself is **out of scope** — separate repo. This doc only
  fixes the *contract* (the calendar tag) it must honor.
- No per-signal logic in TrainMate (no "alcohol is bad" rule).
- No correlation / quantitative analysis built now — only the storage that would
  permit it later (§7).
- `lifeevents` is not replaced; daily context is a distinct, finer-grained thing
  (§5).
- No new calendar; we reuse the configured `google_calendar_id`.

---

## 3. Why a single calendar + a positive tag

With one shared calendar there are **three** classes of events, not two:

| Class | Origin | TrainMate action |
|-------|--------|------------------|
| Workouts | TrainMate (`extendedProperties.private.source = "TrainMate"`) | written by us; ignore on read |
| Context signals | external syncer (tagged, see §4) | **ingest** |
| Ordinary life events | the user, by hand (dentist, a flight) | ignore |

Reading "everything that isn't ours" is wrong — it would ingest the dentist
appointment as a recovery signal. So context events need a **positive** marker.
We use `extendedProperties.private` rather than a title convention (e.g.
`[ctx]`) because:

- **Unambiguous three-way split** — no text heuristics.
- **Invisible to the human view** — the event title still just reads "2 drinks".
- **Server-side filterable** — `events().list(privateExtendedProperty=
  "source=trainmate-context")` returns *only* signal events, so TrainMate never
  even fetches the user's private appointments (a nice privacy property).
- **Survives edits** — editing the title leaves the tag intact.

---

## 4. The calendar contract (what the external syncer must write)

One **all-day** event per signal-day per metric, on the configured calendar:

- `start.date` / `end.date`: the day the signal applies to (all-day, end
  exclusive = start + 1 day, matching how TrainMate writes workouts).
- `summary` / `description`: human-readable text, shown to the user and fed to
  the LLM verbatim (e.g. summary `"Alcohol: 2 drinks"`).
- `extendedProperties.private`:
  - `source = "trainmate-context"` — **required** positive marker.
  - `metric = "<category>"` — **required** free-form category string, e.g.
    `"alcohol"`, `"sleep_quality"`, `"stress"`. TrainMate treats it as opaque.
  - `value = "<number>"` — **optional** numeric magnitude as a string, e.g.
    `"2"`. Best-effort parsed to a float; absent/unparseable → `NULL`.

Everything else about the syncer (auth, how it reads the spreadsheet, dedup) is
its own concern.

---

## 5. Storage: a new `daily_context` table

Daily context is **not** a `lifeevents` row. `lifeevents` are coarse multi-day
spans with a type and an impact paragraph, consumed at *plan/meso* granularity.
Daily context is fine-grained (one row per day per metric), carries an optional
number, and must be reconciled against a calendar event id. Different shape,
different cardinality, different lifecycle → its own table.

```sql
CREATE TABLE IF NOT EXISTS daily_context (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,           -- YYYY-MM-DD (event start.date)
    metric          TEXT NOT NULL,           -- opaque category, e.g. 'alcohol'
    value           REAL,                    -- optional numeric magnitude
    text            TEXT,                    -- summary/description for the LLM
    google_event_id TEXT NOT NULL UNIQUE,    -- reconciliation key
    updated         TEXT                     -- event 'updated' RFC3339 (debug)
);
CREATE INDEX IF NOT EXISTS idx_daily_context_date ON daily_context(date);
```

- `google_event_id` is the natural key (same approach as `workouts`), making
  ingestion an **upsert** and giving us change/delete detection for free.
- We deliberately do **not** enforce `UNIQUE(date, metric)` — multiple events
  reconcile by id; the syncer owns its own dedup.
- `value` is nullable now and unused by logic; it exists so the quantitative
  path (§7) needs no migration.

`db/lifeevents.py` is the model for the new `db/dailycontext.py` mixin
(`upsert_daily_context_by_event`, `get_daily_context(start, end)`,
`delete_daily_context_by_event`, `list_context_event_ids(...)`). Table creation
goes in `db/base.py` alongside the others.

---

## 6. The pull path (sync, not append)

Folded into the existing `data pull` **and** the auto-ensure-before-read path
(`garmin.ensure_data`), so context refreshes whenever metrics do — no separate
command to remember. The two directions have different cadences:

- **Forward / incremental** (steady state): rides along with `data pull` and
  `ensure_data` via `syncToken` — only changed/new/cancelled events come back.
  Cheap, always current.
- **Full pull** (first run, or token expiry): fetches **all** tagged events with
  **no date horizon** — see below for why a horizon is unnecessary here.

**Incremental via `syncToken`.** Google Calendar's `events().list` returns a
`nextSyncToken`; passing it back next time yields only events
created/changed/**cancelled** since. This gives edit- and delete-detection
natively:

1. Load the stored context sync token (see §6.1).
2. `events().list(calendarId=..., privateExtendedProperty="source=trainmate-context",
   syncToken=<stored>)`, paging through results.
   - No stored token (first run) or `410 GONE` (token expired) → fall back to a
     **full pull of all tagged events** (no date window), then resume
     incremental. Unlike Garmin (one API call *per day*, so it needs a horizon),
     Calendar `list` is bulk + paginated + server-side filtered, and tagged
     events are at most one-per-day-per-metric — so pulling everything is a
     couple of cheap calls. Old events the coach's window doesn't cover just sit
     harmlessly in the DB.
3. For each returned event:
   - `status == "cancelled"` → `delete_daily_context_by_event(event_id)`.
   - otherwise → parse tag (`metric`, `value`), date, text →
     `upsert_daily_context_by_event(...)`.
4. Persist `nextSyncToken`.

Because the API filters by `privateExtendedProperty` server-side, the token's
change stream only covers context events — workouts and private appointments
never enter the loop.

### 6.1 Sync-state storage

`sync_state` is **already a keyed table** (`db/activities.py`,
`get_sync_state(key="garmin")`):

```sql
sync_state(key TEXT PRIMARY KEY, through_date TEXT, last_pull_utc TEXT)
```

Add a row `key="calendar_context"` rather than a new table, and add a **nullable
`sync_token TEXT` column** for the opaque `nextSyncToken` (neither existing
column fits an opaque token — `through_date` is a forward *date* high-water
mark). The column is populated only by the context row; `through_date` stays
`NULL` for it; `last_pull_utc` records when context last synced. Chosen over a
separate `settings` kv so there's a single "sync progress" concept; `wipes.py`
already clears `sync_state` in one place. `get_sync_state`/`set_sync_state` gain
a `sync_token` field.

---

## 7. Coach consumption — qualitative now, quantitative later

**Now (qualitative).** Where the analysis/coach already pulls per-window
`metrics` and `lifeevents` (`coach/service.py` analysis path, ~L1316–1327), also
pull `get_daily_context(from, until)` and render it into the prompt next to the
daily metrics. The LLM reads "alcohol: 2 on 2026-06-13" beside the trashed
2026-06-14 HRV/Body Battery and attributes the dip correctly. The meaning lives
in the model, not in TrainMate. Include daily context in the analysis
**evidence fingerprint** (`_get_evidence_fingerprint`) so a changed/added signal
invalidates the cached reconstruction.

**Later (quantitative, optional — not built now).** Because each row carries a
category and an optional number, TrainMate could one day ask a **category-
agnostic** question — "for metric X, does a higher `value` on day D precede worse
recovery on D+1?" — and run it identically for `alcohol`, `sleep`, `stress`.
Still zero domain logic: "alcohol hurts HRV" would be a *result*, never a coded
rule. This could feed the evidence-based confidence machinery
(`DESIGN_evidence_based_confidence.md`). The only thing that keeps this option
alive is the `value` column existing today.

---

## 8. Resolved decisions & remaining questions

**Resolved:**
- **Sync-state storage** → reuse `sync_state` with `key="calendar_context"` + a
  nullable `sync_token` column (§6.1).
- **Full-pull horizon** → none; full pull fetches *all* tagged events. The
  horizon question dissolves because Calendar `list` is bulk/paginated and tagged
  events are sparse (§6).
- **CLI surface** → silent inside `data pull` / `ensure_data`; no dedicated
  command. (Add a thin `context list` only if debugging later warrants it.)

**Doc-mandated implementation task:**
- **Update command help.** `d_pull`'s description (`trainmate_cli.py`, currently
  "...directly from Garmin Connect ... 2 days ending today") must state it also
  syncs tagged calendar context. Touch any other help text that describes the
  pull/ensure path.

**Resolved:**
- **Multiple metrics per day:** hand the LLM **all rows** — no collapsing.

---

## 9. Summary

External sources drop **tagged all-day events** (`source=trainmate-context`,
`metric`, optional `value`) on the **single existing calendar**. TrainMate
**incrementally syncs** them (via `syncToken`, server-side filtered) into a new
**`daily_context`** table, reconciling edits and deletions by event id. The
coach reads them **qualitatively** today; the optional `value` column keeps a
**generic quantitative** path open with no future migration — and TrainMate never
learns what any single signal *means*.
