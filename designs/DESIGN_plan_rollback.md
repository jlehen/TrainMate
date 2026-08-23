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
  `-M/--macrocycle` (a plan version IS a macrocycle, so it is
  named after the thing it identifies — DESIGN_cli_selectors.md §5).

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

### `workout_generate` (coach/service/workouts.py) — eager, once accepted
0. If live upcoming workouts exist, the CLI confirms the LLM call first
   (`_confirm_regeneration`, `cli/workouts/generate.py`) — it names how many are at
   stake, how many were added by hand, and that `workout rollback` brings them back;
   `-f/--force/-y` skips it.
0b. `workout_generate` writes nothing: it returns a `GenerateProposal`, which the CLI
   lists (as `workout list` renders it) and gates behind a second confirmation. Steps 1-4
   below are `workout_generate_apply`, reached only on a `y` (or `-f`). Declining leaves
   the live rows and their Calendar events exactly as they were.
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
1. Resolve target: `get_previous_macrocycle_version` (chronologically prior) by default, or a
   specific version id.
2. `archive_future_workouts(today)` + delete their events.
3. `set_active_macrocycle(target)` — flip status **before** re-pushing so date→plan
   lookups (event stamping) resolve to the restored plan.
4. `restore_macrocycle_workouts(target)` — un-archive the target's latest batch.
5. `sync_multiple(restored)` — re-push (their handles were cleared at archive time, so
   fresh events are created).

Navigation is **monotonic by creation id**: default rollback restores the newest
version older than the active one, so repeated calls walk steadily backward.
`--macrocycle <id>` jumps to any version (forward or backward), since rollback is just an
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

### 6.1 Walking blocks: by macrocycle id, never by date

The filtering above covers every accessor that answers *"what is the plan?"*. It does not
cover the retrospective views, which ask a different question — *"which blocks did the
athlete train through, in order?"* — and those have two traps, one on each side of the
same filter:

- **Keep the `mac.status = 'active'` filter and query mesocycles by date**, and the
  cross-plan case disappears: a window reaching back before the current plan's first block
  finds nothing there, because the earlier goal's plan is a different macrocycle and a
  date query has no reason to prefer it.
- **Drop the filter**, and superseded versions come flooding back. They are not history:
  they sit on *the same calendar dates* as the active version and describe training that
  was planned and then replaced. Reporting them double-counts every activity in the
  overlap and corrupts the block-over-block delta chain, whose baseline is simply the
  previous entry in the list.

So the rule for any view that walks the plan retrospectively: **fix the lineage by
macrocycle id first, then compare dates.** `trainmate/plan_lineage.py`'s `plan_lineage()`
is that walk, shared by `tm progress --blocks` (`cli/progress.py`) and the strategy
prompt's planned-vs-actual review (`coach/service/context.py`).

**`tm progress --blocks` stops the delta at the plan boundary; the strategy prompt does
not.** Each block reports its change against the block before it, which within one plan is
the periodization signal proper (DESIGN_intensity_distribution.md §4.1). Across a boundary
the block before is the *previous goal's* last one, so the comparison spans a taper, a race
and whatever off-season followed.

The two consumers want opposite things there, and the split is deliberate:

- **`--blocks` suppresses it** (`plan_lineage.delta_baseline()`). The athlete is asking
  how the current training is going; a "change" that is really a season transition reads
  as a collapse in load and says nothing about intensity creep. The block is still
  *reported* — the coverage is what a long window asked for — it just reports no change.
- **The strategy prompt keeps it.** Reviewing one season against the last is the whole
  point of the planned-vs-actual review (DESIGN_backward_evaluation.md §6.1), which is why
  its blocks are ordered by when they were trained rather than by argument position. Pinned
  by `test_blocks_are_ordered_by_when_they_were_trained`.

So this is one case where the athlete's view and the coach's prompt legitimately differ,
against the usual rule that they must not (§3.1): they are answering different questions.

Which makes the name of the "previous plan" accessor load-bearing, because there are two
of them and they mean opposite things:

| Accessor | Returns | For |
|---|---|---|
| `get_previous_macrocycle_version(objective_id)` | an earlier **version** of *this* goal's plan — superseded, never trained, overlapping dates | `plan rollback`, `plan diff` |
| `get_preceding_macrocycle(objective_id)` | the **active** plan of the *previous goal* — what actually governed the earlier dates | retrospective views |

`_blocks_in_window` originally called the first while its own docstring warned against
exactly what the first returns, so `tm progress --blocks` reported every block twice after
any plan regeneration. The rename is the fix that keeps it fixed.

## 7. CLI & Web

`plan rollback [-g/--goal ID] [-M/--macrocycle ID] [-y]` (registered alias `rb`). Interactive
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
- **`plan show -M/--macrocycle <ID>`** — renders a specific version in full (strategy +
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
`--macrocycle`) always steps to the chronologically previous version.

**Web** shows the version axis but no longer drives it: the dashboard has since become
read-only (ARCHITECTURE.md §8) and 405s every mutating verb, so the `POST /api/plan/rollback`
this design originally shipped is gone — rolling back is a CLI action. What remains is
`GET /api/plan/versions` and `GET /api/plan/diff`, behind a "Plan versions & compare" panel
on the strategy card: each kept version with a per-version **Compare** button, and a footer
pointing at `tm plan rollback --macrocycle <id>`.

## 8. Limitations / non-goals

- **Single-active-goal centric.** Like the prior behavior, `workout generate` and
  rollback operate over *all* future workouts (not per-goal), matching how the tool is
  used in practice. A workout outside any mesocycle's coverage gets a NULL
  `macrocycle_id` and so is archived-but-not-restored by a plan rollback.
- **No "redo" verb.** Rolling forward again is done with `--macrocycle`, not a dedicated
  command.
- **`plan rm` / `plan wipe`** delete *all* versions for the objective — rollback is
  for undoing regenerations, not for resurrecting a deleted plan.

## 9. `workout rollback` — the same undo on the workout axis

`plan rollback` can only express "restore the workouts of *another plan version*". Two
regenerations under the **same** version — the common case, since `workout generate` is
its own command — are indistinguishable to it: both batches carry the same
`macrocycle_id`, and only the newest survives its `MAX(archived_at)` selector. So
regenerating workouts twice without touching the strategy had no undo.

### Batch = `archived_at` → superseded by `change_id`

**This section is superseded by DESIGN_workout_revisions.md §10.** It is kept because the
reasoning below is what led there, and because the date floor it introduced still holds.

The original design keyed a batch on `archived_at`: one `archive_future_workouts` call
stamped every row it displaced with a single timestamp, so that timestamp *was* a batch
identity. It cost no new state, and it had one blind spot that turned out to be decisive:
it is stamped on the rows that **died**. `workout adapt` killed no rows, so it created no
batch, so there was no "undo just the adapt" — rolling back after an adapt also reverted
the generation beneath it.

Under revisions the batch is `change_id`, carried by the rows a command **created**. Every
write is therefore a batch and every batch is undoable by one primitive. See
DESIGN_workout_revisions.md §10 for the mechanics; the rest of this section describes what
carried over.

### Mechanics (`workout_rollback(change_id=None)`)

`rollback_to_change(change_id, today)` is point-in-time: it reverts the target change and
every change made after it, putting each affected slot back to the revision that was live
just before the target ran. With no target it undoes the newest change, which is what
makes an adapt undoable on its own.

The active macrocycle is **not** touched: this is an undo of a workout write, not of a
plan decision. A restored session keeps the `macrocycle_id` of the version that created
it, so it can be older than the active plan — harmless, since nothing reads workouts
through that tag except `plan show`'s per-version listing. It stays its own inverse: the
rollback is itself a change, so calling it again undoes the undo.

### The date floor (bug fix)

Restore is now bounded below by `from_date` (today), because archive always was: 
`archive_future_workouts` only touches rows from a given date onward. A batch archived a
week ago still contains rows for days that have since passed, and those slots are held by
live rows the archive step deliberately left alone. Restoring them wholesale put **two
live workouts on the same `(date, sport_type)`** — breaking the uniqueness
`get_workout`/`save_workout`'s upsert assume — and re-created Calendar events in the past.
`plan rollback` had the same defect; the floor now lives in `rollback_to_change`, which
both commands share, so both are fixed. Slots below the floor are left alone.

`get_workout_changes(from_date)` reports `restorable` (revisions at or after the floor)
alongside the change's total, so the CLI and web can show what an undo would actually
reach and refuse a change that is wholly in the past instead of "restoring" nothing.

### CLI & Web

- **`workout batches`** — lists changes newest first with a positional `#N`, when, kind,
  revision count, date span, and plan version. Numbering is positional and shifts after
  each change; the underlying key is the change id. Like `plan versions`, `workout b`
  resolves as an unambiguous **prefix**, not a registered alias.
  The unnumbered `live` row this section used to describe is gone, and its reason with it:
  it existed because the plan in force carried no `archived_at` and so appeared nowhere in
  a listing keyed on death. Keyed on birth, the change that wrote the plan in force is an
  ordinary row at `#1` — and it is undoable like any other, which is precisely what the
  `live` row had to explain was not the case.
- **`workout rollback [--batch N] [-y]`** (registered alias `rb`) — undoes change `#N` and
  every change after it, default `#1`. Confirms interactively, naming what it undoes.
- Not to be confused with **`workout restore <id>`**, which un-cancels a single session
  (the void axis, ARCHITECTURE.md §5). Both help texts say so.
- Web (read-only, like the plan panel above): `GET /api/workouts/batches` feeds a
  "Workout changes" panel under the schedule; undoing one is `tm workout rollback`, which
  the panel's footer names.
