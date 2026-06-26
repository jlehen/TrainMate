# Design: Plan Versioning & Rollback

**Status:** Implemented · **Date:** 2026-06-19 · **Branch:** main

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
- `archived_at TEXT` — non-NULL ⟺ the row belongs to a superseded plan version. This
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

### `workout_generate` (coach/service.py) — eager
1. `archive_future_workouts(today)` → soft-archives every live future row (sets
   `archived_at`, clears `google_event_id`/`pushed_signature`) and **returns the
   pre-archive rows** so their Calendar events can be deleted.
2. Delete those events.
3. Save new workouts tagged `macrocycle_id = <active macro>`.
4. `sync_multiple(new)` — push immediately.

### `plan_rollback(objective_id, target_macrocycle_id)` (coach/service.py)
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

`plan rollback [--goal ID] [--version PLAN_ID] [-y]` (alias `rb`). Interactive
confirmation by default (shows the target version's generation date). Reports how many
workouts were restored/archived. `workout generate`'s help and output now reflect the
eager push and point at `plan rollback` to undo.

Two read-only companions make versions discoverable (so rollback in either direction
doesn't require guessing ids):

- **`plan versions [--goal ID]`** (alias `v`) — lists every kept version (active +
  superseded) with id, generated-on date, status, and a one-line strategy excerpt.
- **`plan show --version <PLAN_ID>`** — renders a specific version in full (strategy +
  mesocycle timeline) under a "superseded" header when it isn't the active one.

**Redo** is just a rollback to a *newer* version id: `set_active_macrocycle` swaps
active↔superseded in either direction, and the target version's workouts were archived
as one batch when it was last superseded, so restoring its `MAX(archived_at)` batch
resurrects exactly that set. `plan versions` surfaces the ids; the default (no
`--version`) always steps to the chronologically previous version.

**Web** mirrors the same surface (ARCHITECTURE.md §8): `GET /api/plan/versions` lists
versions and `POST /api/plan/rollback` (`{goal_id?, version?}`) performs the swap. The
strategy card gains a "Plan versions & rollback" panel that lists each kept version with
a per-version **Restore** button (picking a newer id is the redo path). `workout generate`
in the web also pushes to Calendar eagerly, like the CLI.

## 8. Limitations / non-goals

- **Single-active-goal centric.** Like the prior behavior, `workout generate` and
  rollback operate over *all* future workouts (not per-goal), matching how the tool is
  used in practice. A workout outside any mesocycle's coverage gets a NULL
  `macrocycle_id` and so is archived-but-not-restored by a plan rollback.
- **No "redo" verb.** Rolling forward again is done with `--version`, not a dedicated
  command.
- **`plan rm` / `plan wipe`** delete *all* versions for the objective — rollback is
  for undoing regenerations, not for resurrecting a deleted plan.
