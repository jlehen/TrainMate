# Design: Plan Versioning & Rollback

**Status:** Implemented · **Date:** 2026-06-19 (§9 added 2026-07-31) · **Branch:** main

## 1. Problem

Regenerating a periodization plan used to be **irreversible and asymmetric**:

- `save_macrocycle` hard-`DELETE`d the prior macrocycle (cascade-dropping its
  mesocycles) before inserting the new one. Once you accepted "Apply this new
  strategy? [y/N]", the previous plan was gone for good — and since the LLM is
  non-deterministic, regenerating could not reproduce it.
- `workout generate` eagerly **deleted** the old plan's future Calendar events but
  saved the new workouts to the DB only (`google_event_id = NULL`), leaving the
  calendar half-updated until a manual `workout push`.

So the "review before push" gate gave no real safety: the irreversible commitment
(replacing the plan) had already happened upstream, and the push step only chose
between *push-as-is* and *edit-then-push*. The athlete could end up stuck with a plan
they regretted and no way back.

## 2. Decision

Make plan regeneration **reversible** and make the workout sync **fully eager and
symmetric**:

1. **Version, don't delete.** A regeneration *supersedes* the prior macrocycle (kept,
   marked `superseded`) instead of deleting it. Exactly one version per objective is
   `active` at a time.
2. **Eager forward sync.** `workout generate` archives the previous plan's future
   workouts (tearing down their Calendar events) and pushes the new ones to Calendar
   immediately. The calendar always mirrors the active plan.
3. **`plan rollback` is the symmetric inverse.** It restores an earlier plan version
   *and* the workouts that were live under it, archiving the current plan's workouts
   and reconciling Calendar the same way generation does.

Two product decisions (asked and locked with the athlete):

- **Forward sync = fully eager auto-push** (not DB-only-then-`workout push`).
- **History = full** — every superseded version is kept; `plan rollback` undoes the
  last regeneration by default and walks further back via repeated calls or
  `--version`.

## 3. Data model

### macrocycles (plan version)
- `status TEXT DEFAULT 'active'` — `active` | `superseded`.
- `superseded_at TEXT` — ISO timestamp a version stopped being active; NULL while
  active.

Multiple rows per objective are now allowed; **readers filter on
`COALESCE(status,'active') = 'active'`**. Legacy rows default to `active`, so existing
single-version plans behave unchanged.

### workouts (plan-version axis)
- `macrocycle_id INTEGER` — the plan version the row was created under, **fixed at
  creation** and never overwritten. Generation passes it explicitly; other writers
  derive it from the macrocycle governing the workout's date
  (`get_periodization_ids_for_date`).
- `archived_at TEXT` — non-NULL ⟺ the row was **displaced by a regeneration or a
  rollback**, whatever plan version tags it (§9): one archive call stamps every live
  future row it displaces, including rows of the *currently active* version. This
  is a **fourth, orthogonal axis** alongside *modified / calendar-state / removed*
  (ARCHITECTURE.md §5). Archived rows are hidden from every read by default
  (`get_workouts`/`get_workout` exclude them, `save_workout`'s dedupe ignores them)
  and have their Calendar handle torn down — but are **kept** so a rollback can
  resurrect them. Distinct from `removed` (a deliberate athlete cancellation that
  still surfaces to the coach).

## 4. The anchor: tag workouts to the plan version that created them

The plan *is* the macrocycle, so the macrocycle is the version anchor. Every workout
carries the `macrocycle_id` it was created under. This makes rollback well-defined
even though plan and workout generation are **separate commands**:

```
plan generate  (apply)  → supersede v1, create v2 active   (workouts untouched, still v1-tagged & live)
workout generate         → archive live future (v1-tagged) workouts, create v2-tagged workouts, push
plan rollback            → activate v1, archive v2's live workouts, restore v1's archived workouts
```

A workout created under v1 stays tagged v1 even after it is archived. Rolling back to
v1 restores **v1's most-recently-archived batch** — the set that was live when v1 was
last superseded — keyed by `MAX(archived_at)` among v1-tagged archived rows. This
handles intra-version regenerations (an earlier v1 batch archived while v1 was still
active is *not* restored) and manual sessions added under v1 (archived together with
the generated batch, restored together).

## 5. Mechanics

### `save_macrocycle` (db/periodization.py)
Marks the objective's current `active` version `superseded` (stamping `superseded_at`),
then inserts the new version as `active`. No deletion.

### `workout_generate` (coach/service/workouts.py) — eager
0. If live upcoming workouts exist, the CLI confirms first (`_confirm_regeneration`,
   `cli/workouts/generate.py`) — it names how many are at stake, how many were added by
   hand, and that `workout rollback` brings them back; `-f/--force/-y` skips it.
1. `archive_future_workouts(<generation start>)` → soft-archives every live row from the
   generation start onward (sets `archived_at`, clears
   `google_event_id`/`pushed_signature`) and **returns the pre-archive rows** so their
   Calendar events can be deleted. The generation start is today, or **tomorrow** when
   today's planned session is already completed — that row and its Calendar event are
   preserved as history. (`plan rollback` and `workout rollback` always archive from
   today; the asymmetry is deliberate.)
2. Delete those events.
3. Save new workouts tagged `macrocycle_id = <active macro>`.
4. `sync_multiple(new)` — push immediately.

### `plan_rollback(objective_id, target_macrocycle_id)` (coach/service/planning.py)
1. Resolve target: `get_previous_macrocycle` (chronologically prior) by default, or a
   specific version id.
2. `archive_future_workouts(today)` + delete their events.
3. `set_active_macrocycle(target)` — flip status **before** re-pushing so date→plan
   lookups (event stamping) resolve to the restored plan.
4. `restore_macrocycle_workouts(target)` — un-archive the target's latest batch.
5. `sync_multiple(restored)` — re-push (their handles were cleared at archive time, so
   fresh events are created).

Navigation is **monotonic by creation id**: default rollback restores the newest
version older than the active one, so repeated calls walk steadily backward.
`--version <id>` jumps to any version (forward or backward), since rollback is just an
active↔superseded swap and is therefore itself reversible.

## 6. Reader audit (active-version filtering)

These were updated to ignore superseded versions:

- `get_macrocycle_for_objective` — active only.
- `get_active_mesocycle` — added `mac.status='active'` to all three fallback queries.
- `get_periodization_ids_for_date` — active macros only (event traceability stamping).
- `get_mesocycle_ranges` — joins macrocycles and filters active versions, so an old
  version's ranges don't double-count the same dates as the active one. (Objective
  *status* is still intentionally unfiltered — a since-completed objective still
  planned its dates.)

## 7. CLI & Web

`plan rollback [--goal ID] [--version PLAN_ID] [-y]` (registered alias `rb`). Interactive
confirmation by default (shows the target version's generation date). Reports how many
workouts were restored/archived. `workout generate`'s help and output now reflect the
eager push and point at `workout rollback` first (the same-version undo, §9), then at
`plan rollback` to step the strategy back with it.

Three read-only companions make versions discoverable (so rollback in either direction
doesn't require guessing ids):

- **`plan versions [--goal ID]`** — lists every kept version (active + superseded) with
  id, generated-on date, status, and a one-line strategy excerpt. `plan v` works, but as
  an unambiguous **prefix** (DESIGN_cli_noargs.md §d), not a registered alias: it would
  break silently if another `plan v…` sub-command were added.
- **`plan show --version <PLAN_ID>`** — renders a specific version in full (strategy +
  mesocycle timeline) under a "superseded" header when it isn't the active one.
- **`plan diff [PLAN_ID_A] [PLAN_ID_B] [-g ID] [--full]`** (registered alias `df`) —
  compares two versions field by field (strategy/feedback prose, mesocycles added,
  removed, renamed or re-dated, snapshotted inputs), defaulting to previous-vs-active.
  The comparison itself lives in `trainmate/plan_diff.py`; `plan versions`' footer points
  at it.

**Redo** is just a rollback to a *newer* version id: `set_active_macrocycle` swaps
active↔superseded in either direction, and the target version's workouts were archived
as one batch when it was last superseded, so restoring its `MAX(archived_at)` batch
resurrects exactly that set. `plan versions` surfaces the ids; the default (no
`--version`) always steps to the chronologically previous version.

**Web** shows the version axis but no longer drives it: the dashboard has since become
read-only (ARCHITECTURE.md §8) and 405s every mutating verb, so the `POST /api/plan/rollback`
this design originally shipped is gone — rolling back is a CLI action. What remains is
`GET /api/plan/versions` and `GET /api/plan/diff`, behind a "Plan versions & compare" panel
on the strategy card: each kept version with a per-version **Compare** button, and a footer
pointing at `tm plan rollback --version <id>`.

## 8. Limitations / non-goals

- **Single-active-goal centric.** Like the prior behavior, `workout generate` and
  rollback operate over *all* future workouts (not per-goal), matching how the tool is
  used in practice. A workout outside any mesocycle's coverage gets a NULL
  `macrocycle_id` and so is archived-but-not-restored by a plan rollback.
- **No "redo" verb.** Rolling forward again is done with `--version`, not a dedicated
  command.
- **`plan rm` / `plan wipe`** delete *all* versions for the objective — rollback is
  for undoing regenerations, not for resurrecting a deleted plan.

## 9. `workout rollback` — the same undo on the workout axis

`plan rollback` can only express "restore the workouts of *another plan version*". Two
regenerations under the **same** version — the common case, since `workout generate` is
its own command — are indistinguishable to it: both batches carry the same
`macrocycle_id`, and only the newest survives its `MAX(archived_at)` selector. So
regenerating workouts twice without touching the strategy had no undo.

### Batch = `archived_at`

No new state was needed. One `archive_future_workouts` call stamps every row it displaces
with a single `archived_at` value, so that timestamp already *is* a batch identity: the
set of sessions that were live at that moment, whatever plan version tagged them and
however they got there (generated, manually added, adapted). `get_archived_batches()`
groups on it; `restore_workout_batch(archived_at, from_date)` restores one.
`restore_macrocycle_workouts` is now a thin wrapper — resolve the version's newest stamp,
delegate — so both commands share one restore path.

### Mechanics (`workout_rollback(batch=None)`)
1. Resolve the target batch **before** archiving anything: the newest stamp by default, or
   an explicit one. The archive in step 2 creates a newer batch that would otherwise
   become the default and restore what it just displaced.
2. `_archive_and_teardown(today)` — archive the live upcoming sessions, delete their
   events (the shared helper `workout generate` and `plan rollback` also use).
3. `restore_workout_batch(target, today)` + `sync_multiple` to re-push.

The active macrocycle is **not** touched: this is an undo of a workout generation, not of
a plan decision. A restored row keeps the `macrocycle_id` of the version that created it,
so it can be older than the active plan — harmless, since nothing reads workouts through
that tag except `plan show`'s per-version listing. Like `plan rollback`, it is its own
inverse: the batch it archives becomes the newest, so calling it again steps forward.

### The date floor (bug fix)

Restore is now bounded below by `from_date` (today), because archive always was: 
`archive_future_workouts` only touches rows from a given date onward. A batch archived a
week ago still contains rows for days that have since passed, and those slots are held by
live rows the archive step deliberately left alone. Restoring them wholesale put **two
live workouts on the same `(date, sport_type)`** — breaking the uniqueness
`get_workout`/`save_workout`'s upsert assume — and re-created Calendar events in the past.
`plan rollback` had the same defect; the floor lives in the shared
`restore_workout_batch`, so both are fixed. Rows below the floor stay archived.

`get_archived_batches(from_date)` reports `restorable` (rows at or after the floor)
alongside the batch total, so the CLI and web can show what a restore would actually
revive and refuse a batch that is wholly in the past instead of "restoring" nothing.

### CLI & Web

- **`workout batches`** — lists batches newest first with a positional `#N`, archive time,
  counts, date span, and plan version. Numbering is positional and shifts after a rollback;
  the underlying key is the timestamp. Like `plan versions`, `workout b` resolves as an
  unambiguous **prefix**, not a registered alias.
- **`workout rollback [--batch N] [-y]`** (registered alias `rb`) — restores batch `#N`,
  default `#1`. Confirms interactively, naming both what comes back and what gets archived.
- Not to be confused with **`workout restore <id>`**, which un-cancels a single
  soft-removed session (the `removed` axis, ARCHITECTURE.md §5). Both help texts say so.
- Web (read-only, like the plan panel above): `GET /api/workouts/batches` feeds an
  "Archived workout batches" panel under the schedule; restoring one is `tm workout
  rollback`, which the panel's footer names.
