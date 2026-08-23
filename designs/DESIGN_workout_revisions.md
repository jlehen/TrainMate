# Design: Workout Revisions — an append-only `workouts` table

**Status:** Implemented · **Date:** 2026-08-23 · **Branch:** worktree-workout-revisions-design

## 1. The problem

The `workouts` table is already mostly history, but history is second class. As of this
writing the live database holds **372 rows for a two-month span, of which 60 are live and
312 archived**, with up to 10 rows in a single `(date, sport)` slot. The table behaves like
a log. It is not modelled as one.

That mismatch shows up in four places.

**Three write paths disagree about what happens to the old version.**

| Path | What it does to the previous version |
|---|---|
| `workout generate`, `plan rollback`, goal archive | Archive and rebuild — the old row is kept |
| `workout adapt`, `workout accommodate` | Edit in place — the old values are overwritten |
| Adapt displacing a session, `workout add --replace-day` | Hard `DELETE` — the old row is gone |

The third one is unrecoverable data loss on a normal day's use
(`coach/service/adaptation.py`, `coach/service/editing.py`).

**Seven columns exist to fake a history a chain would give for free.**
`original_date`, `original_description`, `original_duration_minutes`, `original_tss`,
`original_rpe`, `adaptation_count`, `adapted_at`. They record the first version and the
current one, never anything between. And `original_date` has a permanent hole: a backfill
migration set `original_date = date` on legacy rows, erasing every swap made before the
column existed.

**`modification_state.py` reconstructs a fact nobody recorded.** It classifies a workout as
`unmodified` / `adapted` / `swapped` / `replaced` by sniffing whether `adaptation_summary`
is non-NULL, whether `modification_reason` starts with one of two magic string prefixes,
whether `date != original_date`, and whether `source == 'manual'` — with a documented
catch-all for rows that predate the split. It is a heuristic standing in for a column.

**Undo is coarse and uneven.** `workout rollback`'s batch key is `archived_at`, a timestamp
stamped on the rows that *died*. Adapt kills no rows, so adapt creates no batch, so there is
no "undo just the adapt": rolling back after an adapt also reverts the generation that
preceded it (DESIGN_plan_rollback.md §9).

## 2. Decision

**`workouts` becomes append-only. A row is a revision and is never updated or deleted.**
Every change — generate, adapt, accommodate, swap, add, remove, restore, rollback — appends
new rows. The most recent revision in a slot is the live one. Everything else is history.

Six parts:

1. **The slot chain.** `(date, sport_canonical)` may hold many revisions. The one with the
   highest `id` is live. There is no flag marking a row obsolete — being superseded *is*
   having a newer sibling.
2. **The lineage.** A `lineage_id` gives a session a stable identity that survives both
   edits and date moves. Copies inherit it; a swap carries it across dates. It is also the
   id the CLI and Calendar show, so athlete-visible ids never churn.
3. **A change is a row.** Each command invocation writes one `workout_changes` row and
   points every revision it appends at it. That row carries the kind of change and the
   batch-level rationale.
4. **Removal is a void revision.** A revision may say "this slot holds no session". When it
   is the newest revision in the slot, there is no session that day. When it is not, it is
   just history like any other row.
5. **Calendar state moves out** to a side table keyed by lineage, so the workouts table is
   literally immutable and can be enforced with a trigger.
6. **No-op revisions are suppressed.** If a proposed revision's prescription is identical to
   the live one, no row is written.

## 3. Data model

### New table: `workout_changes`

One row per command invocation that wrote workouts.

```sql
CREATE TABLE workout_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,  -- UTC ISO, when the command ran
    kind          TEXT    NOT NULL,  -- see the kind vocabulary below
    summary       TEXT,              -- batch rationale (today's `adaptation_summary`)
    macrocycle_id INTEGER            -- the plan version in force when this ran
);
```

**Kind vocabulary** — one value per command, fixed at write time:

`generate` · `adapt` · `accommodate` · `swap` · `add` · `rm` · `restore` · `rollback` ·
`stand-down` (goal archived) · `reinstate` (goal reactivated).

There is no `legacy` kind. Every pre-migration row classifies into one of today's four
modification statuses, and each of those maps onto a real kind (§13 step 3) — a special
pre-migration kind would only exempt migrated rows from every derivation that reads kinds.

One invocation has exactly one kind, so `kind` lives on the change and not on each row.
`summary` replaces `adaptation_summary`, which is currently copied onto every row of a batch
and deduplicated again at display time.

`macrocycle_id` deliberately lives at both levels, doing different jobs. The row-level
`workouts.macrocycle_id` column stays: it is the per-date tag every scoping read uses
(goal stand-down, `plan show`), a generate spanning two plans writes two values across
its rows exactly as today, and `change.append` takes it per row like any other field.
The change-level copy here is context for `workout batches`; a generate spanning two
plans stamps the version governing the span's start.

A change row is written even when the pass appends nothing. An adapt that looked at the
metrics and held is a real event, and worth being able to see. `workout batches` labels a
change that appended nothing `(held)`, so a run of no-op generates reads as what it is.

### New table: `workout_calendar_state`

```sql
CREATE TABLE workout_calendar_state (
    lineage_id                 INTEGER PRIMARY KEY,
    google_event_id            TEXT,
    pushed_signature           TEXT,
    adherence_pushed_signature TEXT
);
```

See §8.

### `workouts` — what it gains

| Column | Meaning |
|---|---|
| `change_id INTEGER NOT NULL` | The `workout_changes` row that appended this revision. |
| `lineage_id INTEGER` | Stable session identity. Equals `id` on a session's first revision. Nullable because a first revision is born NULL and seeded before commit (§4); the §14 trigger permits no other change to it, so a committed row always carries one. |
| `sport_canonical TEXT NOT NULL` | The slot key. `sport_type` keeps the spelling as written. |
| `void INTEGER NOT NULL DEFAULT 0` | 1 means this slot holds no session as of this revision. |
| `reason TEXT` | Per-revision note. Replaces `modification_reason` and `removed_reason`. |
| `restored_from INTEGER` | Only on a `rollback`/`restore`/`reinstate` copy: the id of the revision this row is a copy of. NULL everywhere else. The adaptation tally follows it to skip spans that were undone (§7). |

`reason` merges two columns that never legitimately coexisted on one *revision*: a row's
note is either why it was changed or why it was cancelled, and the change kind says which.
Today a single row can carry both, because adapting and then removing a session writes twice
to the same row. Under revisions those are two rows with one note each.

### `workouts` — what it loses

Sixteen columns:

- **Superseded by the chain:** `archived_at`.
- **Derived from the lineage (§7):** `original_date`, `original_description`,
  `original_duration_minutes`, `original_tss`, `original_rpe`, `adaptation_count`,
  `adapted_at`, `source`.
- **Renamed or re-homed:** `modification_reason` and `removed_reason` → `reason`;
  `removed` → `void`; `adaptation_summary` → `workout_changes.summary`.
- **Moved to the side table:** `google_event_id`, `pushed_signature`, `marked_signature`.

Net: **16 columns out, 6 in, 2 tables added.**

### Indexes

```sql
CREATE INDEX idx_workouts_slot    ON workouts(date, sport_canonical, id);
CREATE INDEX idx_workouts_lineage ON workouts(lineage_id, id);
```

The first serves the live view, the second serves every lineage derivation.

## 4. Identity — the slot chain and the lineage

These are two different questions and the design answers both.

```
  the slot chain           "what has happened to Tuesday's cycling session?"
  (date, sport_canonical)   → ORDER BY id

  the lineage              "what has happened to THIS long ride?"
  lineage_id                → ORDER BY id
```

The slot chain is what motivated this design. The lineage is what makes it pay for itself,
for one specific reason.

### Why the lineage is not optional

Among the columns §3 deletes are `adaptation_count` and `adapted_at`. They are deleted
because you can count the chain instead of maintaining a counter. **That deletion only works
if the chain follows the session, and a swap moves a session to a different date.**

The failure is not cosmetic. It breaks a live safety guard.
`coach/formatting.py::_adapt_recency_tag` injects this into the adapt prompt, per session:

```
[ALREADY EASED by a prior adaptation (3x, most recently 2 days ago) —
 current form is the reduced plan, not the original; do not compound]
```

Its job is to stop the coach cutting an already-cut session again while recovery metrics are
still lagging. Without it, three bad mornings walk a 90-minute ride down to nothing.

Now watch it fail with slot chains alone. Tuesday has a long ride, 90 minutes. Thursday has
an easy spin, 45. Two bad mornings, two adapts, then a swap on Wednesday:

```
Tue / cycling:   r1  long ride 90   (generate)
                 r2  long ride 75   (adapt)
                 r3  long ride 65   (adapt)
                 r4  easy spin 45   (swap)

Thu / cycling:   r1  easy spin 45   (generate)
                 r2  long ride 65   (swap)
```

Thursday morning, metrics still poor, `workout adapt` runs. It looks at the live Thursday row
— a 65-minute long ride — and asks the chain how often this has been eased.

**Thursday's chain says never.** Two revisions, one generate and one swap, zero adapts. The
guard does not fire, and the coach cuts a session that has already been cut twice.

Note *when* it fails: at a swap. The athlete swapped because life got in the way, which is
also when recovery is worst and the guard matters most.

With `lineage_id`, the Thursday long ride carries the lineage of the Tuesday rows it descends
from. Counting adapts over the lineage returns 2, and the guard fires.

The alternative is to keep `adaptation_count` and `adapted_at` as ordinary columns copied
forward by every revision and carried across by a swap. That works, and it is what happens
today. But then those columns are not deleted, a writer still has to remember to increment
one of them, and the rewrite has bought less than it cost. **One integer column, or two
hand-maintained fields forever.**

### How `lineage_id` is set

Inheriting from the slot's live revision is the common case, but it is not unconditional —
"next occupant of the slot" and "same session" are different things, and conflating them
would corrupt every lineage-derived field. The rules:

- **A revision continues the slot's lineage** when it modifies the session live there:
  `adapt`, `accommodate`, a `generate` refreshing its own generated session,
  `rollback`/`restore` copies, `stand-down`/`reinstate`.
- **A revision starts a new lineage** when it introduces a different session:
  - the slot is empty, or its live revision is a **void** — appending over a void is a new
    session, not a resurrection of the removed one (which would otherwise inherit its
    Calendar event, its `original_*` and its adaptation tally);
  - the change kind is **`add`** — a manual replace is a new session even over a live one.
    This is also what keeps `source` honest: an `add` inheriting a generated lineage would
    derive as `'generated'` (§5);
  - a **`generate` lands on a manually added session**. The generate proceeds — the plan
    owns the horizon — but starts a new lineage and prints a notice naming the manual
    session it replaced, so the athlete can undo the change (§10).
- **A void carries the lineage of the session it ends** — the removed or departing
  session, never a fresh one. A void is the last chapter of a lineage, not a first.
- **A swap** — and an adapt moving or substituting a session (§11) — appends at the
  destination a revision carrying the *moved session's* lineage, not the destination
  slot's.

Mechanically, a session's first revision is written as insert, then
`UPDATE workouts SET lineage_id = id WHERE id = ?` in the same transaction: the one write
that touches a row after insert, before commit, and the only transition the immutability
trigger exempts (§14). Every other revision knows its lineage before insert and writes it
directly. This is why the column is declared without `NOT NULL` — the id does not exist
until the insert assigns it, so a first revision is necessarily born with a NULL lineage.
The trigger, not a column constraint, is what keeps that NULL transient: the only UPDATE
it lets through is the one that fills it with the row's own id.

### Swap, in full

A same-sport swap of A (Tue/cycling) and B (Thu/cycling) appends two revisions:

```
Tue / cycling  ← copy of B, date=Tue, lineage = B's lineage
Thu / cycling  ← copy of A, date=Thu, lineage = A's lineage
```

A cross-sport swap of A (Tue/cycling) and B (Thu/running) touches four slots, because the
sessions land in different slots than they left:

```
Tue / cycling  ← void       (the ride left)
Tue / running  ← copy of B, date=Tue, lineage = B's lineage
Thu / running  ← void       (the run left)
Thu / cycling  ← copy of A, date=Thu, lineage = A's lineage
```

Four appends where today there are two date updates. The extra two are the honest cost of
saying out loud that two slots became empty — today that fact is implicit and unrecorded.

## 5. Reading — the live view and the hydrated row

### The view

```sql
CREATE VIEW live_workouts AS
SELECT w.* FROM workouts w
WHERE w.id = (
    SELECT MAX(w2.id) FROM workouts w2
    WHERE w2.date = w.date AND w2.sport_canonical = w.sport_canonical
);
```

`id` is `AUTOINCREMENT` and therefore monotonic, so the highest id in a slot is the newest
revision. This is also why *restore is a duplicate rather than an un-flag*: appending a copy
of an old revision gives it a new, higher id, and it becomes live by the same rule as
everything else. There is no second mechanism.

Every existing reader keeps its own `WHERE` clause and changes only its `FROM`:

```sql
-- before
SELECT * FROM workouts WHERE archived_at IS NULL AND COALESCE(removed,0) = 0 AND date >= ?
-- after
SELECT * FROM live_workouts WHERE void = 0 AND date >= ?
```

The view includes void revisions on purpose. A cancelled session is still a fact the coach
must see — it is a deliberate cancellation, not a miss — and `prune-calendar` needs it too,
because a soft-removed session keeps its "[Deleted]" Calendar event. Readers filter voids the
same way they filter `removed = 1` today.

### The hydrated row — the contract that keeps this contained

**`get_workouts()` returns dicts of the same shape as today, three key edits aside:**
`revision_id` appears, `archived_at` disappears, and `marked_signature` becomes
`adherence_pushed_signature` (§8). The `Workout` TypedDict makes those three edits and no
others. Fields that used to be columns are filled in from the lineage before the dict is
returned. Everything above `db/workouts.py` is untouched, except the readers of the
removed and renamed keys named below and in §8.

| Key in the returned dict | Where it now comes from |
|---|---|
| `id` | **`lineage_id`** — the stable session identity |
| `revision_id` | the physical row id (new key; only history surfaces read it) |
| `original_date`, `original_description`, `original_duration_minutes`, `original_tss`, `original_rpe` | the lineage's first revision |
| `adaptation_count`, `adapted_at` | walked over the lineage (§7) |
| `source` | `'manual'` if the lineage's first change kind is `add`, else `'generated'` |
| `modification_reason`, `removed_reason` | `reason` |
| `removed` | `void` |
| `adaptation_summary` | `workout_changes.summary` |
| `google_event_id`, `pushed_signature` | `workout_calendar_state` |
| `adherence_pushed_signature` | `workout_calendar_state` — the dict key follows the §8 column rename; no `marked_signature` key remains |
| `archived_at` | **gone** — see below |

Setting `id` to the lineage id is what makes the athlete-visible ids stable. It also fixes a
bug this design would otherwise introduce. `get_workout_by_id` runs
`SELECT * FROM workouts WHERE id = ?` with no live filter, so with raw revision ids this
would happen:

```
$ tm workout list
  [42] 2026-08-25  cycling  Long ride (90 min, TSS 110)
$ tm workout adapt            # row 42 superseded; live ride is now row 87
$ tm workout rm 42 --reason "work trip"
  Workout with ID 42 ('Long ride') removed successfully.
```

That last line would be a lie — a dead revision marked, the live session untouched, success
reported. With lineage ids the id the athlete reads is the id the command needs.

`archived_at` is the one key that disappears from the dict. Its history readers
(`workout batches`, `workout rollback`, `plan show`'s archived listing) move to
`change_id` (§10). It has one non-history reader too: `clear_honored_after`
(`db/constraints.py`) un-honors constraints by comparing their `honored_at` against the
restored batch's `archived_at`. That comparison survives, re-keyed to the target change's
`created_at` (§10).

One consequence for the type pin: `Workout` now describes the hydrated dict, not the
physical table, so `test_types.py` can no longer check it against
`PRAGMA table_info(workouts)`. The `workouts` entry leaves `TYPE_TABLES`; in its place a
test writes one session through the change handle and asserts the dict `get_workouts()`
returns has exactly the TypedDict's keys. Same honesty property, checked against the
thing the TypedDict now claims to describe.

Hydration is batched, not per-row: the lineage-derived fields come from one query over the
whole result set (window functions over `lineage_id`), plus one join each to
`workout_changes` and `workout_calendar_state`. `get_workouts()` must not turn a 60-row
listing into 200 lineage lookups.

## 6. Writing — one append per change

`save_workout` is replaced by a change handle — the only public write path onto
`workouts`:

```python
with db.workout_change(kind="adapt", summary=..., macrocycle_id=...) as change:
    change.append(date=..., sport_type=..., title=..., ...)
    change.void(date=..., sport_type=..., reason=...)
# exit: commit, then the §8 Calendar reconcile pass over the lineages touched
```

The handle is the enforcement, not a convenience. A writer cannot append a revision
without a `workout_changes` row, because `change.append` is the only door in; and it
cannot forget the Calendar reconcile, because the handle schedules it on close. Same
philosophy as the §14 trigger: the rule lives where it cannot be skipped, not in every
caller's memory.

`change.append` does, in one transaction shared by all of the handle's appends:

1. Read the current live revision for `(date, sport_canonical)`.
2. Build the new revision as `{**live_revision, **supplied_fields}`.
3. If the result's *prescription* is identical to the live revision, return without writing
   (§9).
4. Insert. Set `lineage_id` by §4's rules: inherit the live revision's lineage, or start a
   new one where §4 says the revision introduces a different session.

Step 2 is the whole of what `save_workout`'s `COALESCE` ladder does today — roughly forty
lines of SQL whose only job is "a partial re-save must not read an omission as a deletion",
plus a bolted-on `CASE WHEN ? THEN NULL ELSE COALESCE(?, benchmark_type) END` escape hatch
for `benchmark_type`.

This is a relocation, not a deletion: something still has to decide which fields carry
forward. But it is decided once, in Python, where it is one dict merge and can be read.

## 7. What becomes derived

### `original_*` — the lineage's first revision

```sql
SELECT * FROM workouts WHERE lineage_id = ? ORDER BY id LIMIT 1;
```

### `adaptation_count` / `adapted_at` — walked over the lineage

An adapt revision counts only if it actually eased load — a comparison against its own
predecessor rather than a flag threaded through the proposal. But the tally is not a blind
`COUNT(*)` over the lineage, because two kinds of revision make older adapts stop
describing the live session:

- **A `rollback`/`restore` copy undoes a span.** The copy records the revision it
  duplicates in `restored_from`; everything between that revision and the copy was undone
  and must not count. Otherwise an adapt, rolled back a minute later, would still leave
  the guard shouting `ALREADY EASED` at a session running at full prescription.
- **A `generate` re-prescribes the session.** Easings of the previous prescription do not
  describe the new one. This matches today, where a regeneration created a fresh row with
  a zero count.

So the derivation is a short backward walk, in Python, over the lineage's rows — a lineage
is a handful of rows, fetched once per §5's batching:

1. Start at the live revision.
2. On a row with `restored_from` set, jump to the revision it names and continue from
   there — the undone span is skipped.
3. Stop at the first `generate` revision, or at the lineage's first revision.
4. Count the `adapt` rows encountered whose duration or TSS fell against their
   predecessor. That is `adaptation_count`; the newest such row's change `created_at` is
   `adapted_at`.

Three chains, one rule each — `rN` is a revision, its kind from `workout_changes`,
minutes shown:

```
A swap does not reset the tally:

  r1 generate 90' ── r2 adapt 75' ── r3 adapt 65' ── r4 swap 65' (moved to Thu)
  walk: r4 not an adapt, keep going → r3 counts → r2 counts → r1 generate, stop
  adaptation_count = 2  — the §4 guard fires on Thursday

A rollback skips the span it undid:

  r1 generate 90' ── r2 adapt 75' ── r3 adapt 65' ── r4 rollback (copy of r1,
                                                        restored_from = r1)
  walk: r4 jumps straight to r1 → r1 generate, stop
  adaptation_count = 0  — the session is back at full prescription

A generate starts a fresh tally:

  r1 generate 90' ── r2 adapt 75' ── r3 generate 60' (a new plan re-prescribes the day)
  walk: r3 is a generate, stop immediately
  adaptation_count = 0  — easings of the old prescription do not describe the new one
```

The remaining kinds are walked past without effect: `accommodate` reduces load without
counting as an easing (that is its point, above), `restore` and `reinstate` copies jump
via `restored_from` exactly as rollback's do, and `swap`, `rm` voids and `stand-down`
are not easings at all. `add` starts a new lineage (§4), so it never appears mid-walk.

This deletes `AdaptProposal.stamp_adapted_at`. That flag exists so `workout accommodate` can
reduce load without its reductions counting as easings — and under this model accommodate is
excluded by its change kind, with no flag to carry or forget.

**The guard reads the tally, not the marker.** `coach/formatting.py::_adapt_recency_tag`
currently returns `""` unless the session's modification status is `adapted` — which works
today only because of the `adapted`-beats-`swapped` precedence rule §12 retires. Under this
model the marker reflects the *latest* change, so an adapted-then-swapped session reads
`swapped`, and a status gate would silence the tag in exactly the §4 scenario that
motivates the lineage. The gate becomes `adaptation_count > 0`: the tag renders whenever
the walk finds standing easings, whatever the last change kind was. §14 pins the rendered
tag, not just the count.

### Modification kind — `modification_state.py` is deleted

The kind is the change kind of the live revision:

| Live revision's change kind | Reads as |
|---|---|
| `generate` (and it is the lineage's first revision) | unmodified |
| `adapt` | adapted |
| `swap` | swapped |
| `add` | replaced |
| `accommodate` | accommodated *(new — today it is misreported as adapted)* |

No string prefixes, no `date != original_date`, no legacy catch-all. The two magic constants
`SWAP_REASON_PREFIX` and `MANUAL_REPLACE_REASON_PREFIX`, the writer-side test that pins them,
and the whole file go with it.

## 8. Calendar state moves to a side table

`google_event_id`, `pushed_signature` and `adherence_pushed_signature` are sync bookkeeping,
not prescription. Left on the row they would break immutability: a successful push would have
to append a revision, so `workout push -f` alone would double the table and fill every slot
chain with rows that changed nothing an athlete would call a change.

They move to `workout_calendar_state`, keyed by `lineage_id`. Lineage rather than revision is
right because there is one Calendar event per *session*, and it must follow that session
across both edits and date moves — which is exactly what today's `google_event_id` does when
`update_workout_date` moves a row.

**`marked_signature` is renamed to `adherence_pushed_signature`.** The old name says nothing
about which of the two pushes it belongs to, and does not even match its own writer,
`mark_workout_adherence_pushed`. The pair now reads as one group:

| Column | What it records |
|---|---|
| `pushed_signature` | what the last ordinary push sent to Calendar |
| `adherence_pushed_signature` | what the last `compare --mark` adherence push sent |

Both are skip-the-redundant-write memos. The second one earns its keep on `tm data pull`,
which stamps adherence verdicts onto past events as a ride-along on every pull; without it a
`tm data pull -d 30d` over a settled range issues thirty identical Calendar writes.

One property that is currently hand-rolled now falls out. `save_workout` deliberately never
touches `pushed_signature`, so that any content change reads as `stale`. Under revisions the
live row is a different row, its hash differs from the stored signature, and it reads stale
without anyone arranging it.

**Who keeps the Calendar true.** Today the event lifecycle piggybacks on archival:
`archive_future_workouts` clears event handles and the service tears the events down;
restore re-pushes. Append-only removes that hook, and nothing may creep into the append
primitive to replace it — a Calendar call inside the write path would put network I/O in a
transaction and fail workout writes on push errors. Instead, every command that appends
revisions ends with one **reconcile pass** over the lineages it touched, after commit:
compare each touched lineage against `workout_calendar_state` and make the Calendar
agree. This is not a convention for each command to remember — the §6 change handle runs
the pass when it closes, so the only way to write workouts already schedules the
reconcile.

The pass is keyed on **the lineage's newest revision** — highest `id` anywhere in the
lineage, not per slot. The distinction matters exactly once: a swap leaves the moved
session's lineage with a live void at its source slot *and* a live copy at its
destination (§4), and the copy must speak for the lineage or the pass would delete an
event it should move. The handle keeps that true by construction: within one change,
voids are appended before the copies that carry the same sessions elsewhere, so a moved
session's newest revision is always the copy. Per lineage, then:

- live non-void revision whose signature differs (or whose stored event id dangles) →
  push, creating or updating the event;
- live void from `rm` / `stand-down` → retitle the event "[Deleted]" and keep it, as
  today, so `prune-calendar` still works;
- live void from `generate` / `adapt` / `rollback` (the session is gone, not moved) →
  delete the event and drop the `workout_calendar_state` row. A `swap` void never lands
  here: its lineage's newest revision is the destination copy, so the first rule moves
  the event instead.

One batch-sized pass at the end of the command — an adapt touching five sessions pushes
once — and the Calendar converges after every command instead of relying on each flow to
remember its own teardown.

The event's *content* then gets the lineage too: because every form of a session is still
a row, the event can carry its whole history rather than the two forms the old columns
could hold. That is DESIGN_calendar_lineage.md, built on this section.

Beyond the syncer, two consumers change: `cli/common.py` and the `Workout` TypedDict key.
Nothing else reads these columns.

## 9. No-op suppression

**If a proposed revision's prescription is identical to the live one, no row is written.**

Without this rule the design defeats its own purpose. `workout generate` rebuilds a 28-day
horizon on every run and most days come back unchanged. The current database shows the shape:
10 archived batches, up to 10 rows in one slot, but only **10 rows in the whole table with
`adaptation_count > 0`**. Nearly all existing history is regeneration churn. Make that churn
the thing you read when you ask "what happened to Tuesday" and the answer is six identical
rows and one real change.

Storage is not the concern — 372 rows in two months is nothing and SQLite will not notice
ten thousand. Legibility is.

**The compared fields** are the prescription and nothing else:

```
date · sport_canonical · sport_type · title · description · duration_minutes · rpe · tss
benchmark_type · planned_zone_currency · planned_zone1..7_sec · void
```

`reason` is deliberately excluded. If the prescription did not move, the session was held,
and a rationale for holding is not worth a revision. The pass is still recorded — the
`workout_changes` row is written regardless, so a change that appended nothing is visible in
the history as exactly that.

## 10. Undo — the batch key moves from death to birth

Today a batch is `archived_at`: a timestamp stamped on the rows a command *displaced*. That
is why adapt has no undo — adapt displaces nothing, so it stamps nothing.

Under revisions the batch is `change_id`, carried by the rows a command *created*. Every
write is therefore a batch, and every batch is undoable by one mechanism:

**Rollback is point-in-time: undoing change N reverts N and every change after it.** For
each lineage touched by N or by any later change, find that lineage's newest revision
older than N and append a copy of it — stamped `restored_from` — under one new change of
kind `rollback`. A lineage that did not exist before N (its first revision was appended by
N — an `add`, say) gets a void instead.

Undoing one change *in isolation* is deliberately not offered. It looks finer-grained but
is unsound the moment a later change touched the same lineage: undo Tuesday's adapt after
Wednesday's swap moved the session to Thursday, and the "undo" would append the pre-adapt
copy — dated Tuesday — into the Tuesday slot while the swapped copy stays live on
Thursday. One session, live twice. Point-in-time has no such state: it restores exactly
what was live the moment before N ran.

Consequences:

- `workout adapt` becomes undoable on its own, without also reverting the generation
  beneath it — point-in-time reverts what came *after* the target, never what came before,
  and an adapt just made is the newest change.
- `workout rollback` and `plan rollback` share the same primitive instead of a separate
  archive-and-restore path.
- `workout batches` lists every change, not only the ones that happened to displace rows. The
  live set no longer needs the special `_live_batch` handling it has today, because the newest
  change is an ordinary row in the list.
- Rollback stays its own inverse, as it is today: undoing an undo is just another change.

**The date floor stays.** Restore is bounded below by today, because appending a copy into a
past slot would silently make it the live session for a day already trained. Same rule, same
reason as `restore_workout_batch` has now (DESIGN_plan_rollback.md §9).

**Rollback still un-honors constraints.** Today `restore_workout_batch` clears
`honored_at` on every constraint honored *after* the batch being restored
(`clear_honored_after`, keyed on `archived_at`), and the commands report the cleared set.
The rule survives with its key moved like everything else here: a rollback targeting
change N clears `honored_at` where it is newer than N's `created_at` (and the constraint
still has days ahead), and reports it exactly as today. Same comparison, new timestamp.

**Two commands reuse the primitive with a derived target.**

- **`plan rollback` to version V** targets the moment just after V's newest change — the
  newest change that appended any revision tagged with V (`workouts.macrocycle_id`). The
  restore is point-in-time and *unscoped*, exactly the primitive above: restoring the
  whole moment is what keeps the one-session-live-twice argument intact, and it matches
  today, where the archive sweep displaces everything live and restores V's batch.
  Flipping the active version (`set_active_macrocycle`) stays a separate plan-table
  write, exactly as now.
- **Goal stand-down / reinstate.** Stand-down appends, under one `stand-down` change, a
  void from today onward for every live session tagged with one of the goal's plan
  versions — the same row-level `macrocycle_id` scoping that spares a neighbouring goal
  today. Reinstate is its mirror, read off the live view: every lineage whose live
  revision is a stand-down void tagged with one of the goal's versions gets a copy of
  the revision that void ended, stamped `restored_from`, under one `reinstate` change,
  floored at today. A slot regenerated or edited since the stand-down no longer has that
  void live and is left alone — something else owns it now.

## 11. What each command does under this model

| Command | Change kind | What it appends |
|---|---|---|
| `workout generate` | `generate` | A revision per changed day in the horizon; a void for every live slot from the generation start onward that the new plan does not fill — open-ended past the horizon, matching today's `archive_future_workouts`, which has no end bound. |
| `workout adapt` | `adapt` | A revision per eased session; a void for a session it drops. A session it moves or substitutes cross-sport: a void at the source and a revision at the destination carrying the session's lineage — the swap shape (§4). **No more `DELETE`.** |
| `workout accommodate` | `accommodate` | Same, within the constraint window. |
| `workout swap` | `swap` | Two revisions (same sport) or four (cross-sport), per §4. |
| `workout add` | `add` | One revision; with `--replace-day`, a void per other session that day. **No more `DELETE`.** |
| `workout rm` | `rm` | One void revision carrying the athlete's reason. |
| `workout restore` | `restore` | A copy of the revision before the void, stamped `restored_from`. |
| `workout rollback` | `rollback` | A copy of each lineage's pre-target revision (§10). |
| `plan rollback` | `rollback` | The same point-in-time primitive, targeted at the moment just after the restored version's newest change (§10); flipping the active version stays a plan-table write. |
| goal archive / reinstate | `stand-down` / `reinstate` | Stand-down: one void per live session tagged with the goal's plan versions, from today on. Reinstate: one copy per lineage still live as such a void (§10). |
| `workout push` | — | Writes only `workout_calendar_state`. Appends nothing. |
| `workout compare --mark` | — | Writes only `workout_calendar_state`. Appends nothing. |

The two hard-`DELETE` call sites (`coach/service/adaptation.py`,
`coach/service/editing.py`) become voids. That is the single clearest correctness win here:
those paths currently destroy sessions with no way back.

## 12. Behaviour changes the athlete will notice

Each of these is a deliberate change, not a side effect.

**`original_*` after a cross-sport swap.** Today a cross-sport swap seeds the new session's
`original_*` from the *displaced* session, so the Calendar event shows what was planned on
that day. With lineage-derived values, `original_*` answers "what was **this session** first
prescribed as". Both questions are now answerable — the day's original prescription is the
first revision of the slot chain — but they are no longer the same field, and the Calendar
footer changes accordingly.

**Modification markers stop having a precedence rule.** Today `adapted` beats `swapped`,
because a session adapted and later swapped keeps its `adaptation_summary` and would
otherwise be misread. That precedence is a workaround for shared columns. With revisions the
marker reflects the latest change and the count comes from the lineage, so a session eased
twice and then swapped renders `[SWAPPED, ADAPTED ×2]`. More information, no rule.

**`accommodate` gets its own marker.** Today an accommodated session reads `[ADAPTED]`,
because it goes through adapt's write path. It now reads `[ACCOMMODATED]`.

**Workout ids stop churning.** They were already stable in practice; they stay stable by
construction, and `workout rm` / `swap` can no longer address a dead revision.

**`workout batches` gets longer and more useful.** It lists every change, including adapts,
swaps and manual edits, each undoable.

**A regeneration may replace a manually added session, and says so.** Today the two paths
collide silently. Under §4's lineage rules the generate proceeds, the manual session's
lineage ends, and the command prints which manual session was replaced — with the change id
to roll back if the athlete disagrees.

## 13. Migration

Single user, one-off, no backward compatibility (AGENTS.md). 372 rows. One script under
`scripts/`, run once. **Step 0 — back up the database file and verify the copy opens,
before anything else.**

This is a **table rebuild**, not `ALTER TABLE` surgery, for two reasons: SQLite cannot
`ADD COLUMN ... NOT NULL` without a default, and step 4 must control physical insertion
order. The script builds `workouts_new` alongside, fills it, then swaps names.

**Step 1 — `sport_canonical`.** Compute `canonical_sport(sport_type)` per row.
`sport_type` keeps its stored spelling, so nothing about display changes.

**Step 2 — synthetic originals.** For each row with `adaptation_count > 0`, first emit a
synthetic pre-revision built from the `original_*` columns — kind `add` if
`source = 'manual'`, else `generate`; `created_at` = the row's `created_at` — and then the
row itself, under a synthetic `adapt` change stamped `created_at = adapted_at`. Without
this, dropping `original_*` / `adaptation_count` / `adapted_at` would erase the only
record of in-place adaptations — adapt edits its row today, so there are no archived
intermediates — and the DO NOT COMPOUND guard would forget every currently-eased session
on migration day. A count of 3 collapses to 1; `adapted_at`, the recency signal the guard
weighs hardest, survives exactly. §16 records the collapse.

**Step 3 — synthetic changes.** Group the remaining rows by `(archived_at,
classified_kind)`; one `workout_changes` row per group. `created_at` = the group's
`archived_at`, or for live rows the row's own `created_at`, falling back to `date` on the
oldest rows (the `created_at` column was itself backfilled late and can be NULL).
`classified_kind` is `modification_state.modification_status()`'s answer, mapped:

| `modification_status()` | kind |
|---|---|
| `unmodified` | `generate` |
| `adapted` — including the legacy-adapt catch-all | `adapt` |
| `swapped` | `swap` |
| `replaced` | `add` |

One override runs before the table applies: **a row with `source = 'manual'` classifies
as `add`, whatever `modification_status()` says.** The classifier keys on
`modification_reason`, and a manual session added onto an empty day has none — it would
read `unmodified`, root its lineage in a `generate`, and §5 would then derive its
`source` as `'generated'`, erasing its athlete-added standing. Migration-script code
only; the classifier itself is not touched before it is deleted.

`replaced → add` is what roots a manual session's lineage in an `add`, so the §5 `source`
derivation keeps reading it as manual. The heuristic file's last act is to seed the data
that replaces it; after the migration it is deleted.

**Step 4 — insertion order.** Fill `workouts_new` slot by slot: archived rows in
`(created_at, id)` order first, **the live row last**. This is load-bearing, not cosmetic:
the live view's rule is "highest id wins", and today's data violates it — `workout
rollback` revives old rows in place (`UPDATE ... SET archived_at = NULL`,
`db/workouts.py`), so any slot ever rolled back holds a live row with a *lower* id than
its archived siblings. Migrate ids as they are and the view would resurrect a dead
generation as the live plan.

**Step 5 — lineages.** Within each slot, all rows get the earliest row's new id as
`lineage_id`.

This is exact for the common case — a slot's rows *are* one session's revisions — and wrong
wherever a pre-migration swap moved a session between slots. That is accepted. Pre-migration
history is slot history; lineages are real from the migration forward. §16 records it.

**Step 6 — column moves.** `removed` → `void`; `modification_reason` / `removed_reason` →
`reason` (removal reason wins where a row somehow carries both); `google_event_id`,
`pushed_signature`, `marked_signature` → `workout_calendar_state`, keyed by the lineage id
assigned in step 5.

**Step 7 — swap and seal.** Drop `workouts`, rename `workouts_new`, then create the view,
the indexes, and last the immutability triggers (§14) — the view after the rebuild so its
`SELECT w.*` binds the final column set, the triggers last so the migration is not blocked
by them.

## 14. Tests

**The trigger is the guarantee.**

```sql
CREATE TRIGGER workouts_no_update BEFORE UPDATE ON workouts
WHEN NOT (OLD.lineage_id IS NULL AND NEW.lineage_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'workouts is append-only: append a revision'); END;

CREATE TRIGGER workouts_no_delete BEFORE DELETE ON workouts
BEGIN SELECT RAISE(ABORT, 'workouts is append-only: append a void revision'); END;
```

The `WHEN` clause exempts one transition — the post-insert `lineage_id` seeding of §4 —
and pins the value it may write: only the row's own id, so the exemption cannot be used to
graft a row onto an arbitrary lineage. A SQL trigger cannot cheaply also assert "and no
other column moved in the same statement"; that residue is covered by the source-level
test below, which allows no `UPDATE workouts` beyond the seeding statement in the first
place.

**A rule that spans files needs a test that spans files** (AGENTS.md). The trigger stops the
write at runtime; a source-level test names the offender at review time:

- `test_workouts_table_is_append_only` — parses every module under `trainmate/db/` and fails
  on any `UPDATE workouts` or `DELETE FROM workouts` outside the migration function and the
  `data wipe` reset (below). Keyed on the shape (a glob over the directory), not on a list of
  filenames, so a module written tomorrow is covered tomorrow.
- `test_workout_reads_go_through_the_live_view` — fails any `FROM workouts` outside the
  append path and the declared history readers.

**One whole-table deletion is exempt: `data wipe`.** `wipe_workouts` (`db/wipes.py`) is a
reset, not a write path to convert. It drops the two triggers, deletes `workouts`,
`workout_changes` and `workout_calendar_state` together, and recreates the triggers —
and it is the second name `test_workouts_table_is_append_only` declares, beside the
migration function.

**Behaviour tests, each pinning something this design claims:**

- The DO NOT COMPOUND guard survives a swap **end to end**. Build the §4 example — two
  adapts on Tuesday, a swap to Thursday — and assert the *rendered prompt tag* for the
  Thursday session says `ALREADY EASED` with `2x`. Asserting only `adaptation_count == 2`
  would pass at the db layer while a status-gated `_adapt_recency_tag` still returned `""`
  (§7); the tag is the behaviour this design exists to protect. This test fails on slot
  chains alone, and it fails on a status-gated tag.
- An adapt undone by rollback stops counting: adapt, roll it back, assert
  `adaptation_count == 0` and that no tag renders (the `restored_from` jump of §7).
- Rollback is point-in-time: generate, adapt, then swap; undo the adapt's change and
  assert the swap is reverted too — the pre-adapt session is live on its original day and
  no lineage is live in two slots (§10).
- A non-void appended over a void starts a new lineage: `rm` a session, generate into the
  slot, assert the new session has a fresh lineage, a zero tally, and no inherited
  Calendar event (§4).
- A generate landing on a manual session replaces it under a new lineage, prints the
  notice, and rolling the change back brings the manual session back (§4, §12).
- Regenerating an unchanged horizon appends no revisions (§9).
- Restoring an archived revision appends a copy and makes it live, and the previously live
  revision stays in the chain.
- An adapt that drops a session leaves a void, and `workout rollback` on that change brings
  the session back. Today this case loses the row permanently.
- A cross-sport swap voids both source slots and lands both sessions with their own lineages.
- `workout rm <id>` addressed by lineage id acts on the live revision after an intervening
  adapt.

## 15. Alternatives considered

**Keep edit-in-place and add a separate `workout_history` audit table.** Rejected: two
tables that must agree, and the audit table is written by the same writers that already
forget to maintain the columns this design deletes. A log that is a side effect of the real
write is exactly the shape that goes stale.

**Slot chains with no `lineage_id`.** Rejected for the reason in §4: it silently breaks the
DO NOT COMPOUND guard at a swap, and it does not let you delete `adaptation_count` or
`adapted_at`, which were two of the reasons for doing this at all.

**Zero the adaptation tally whenever the live revision's kind is not `adapt`.** Considered
as a cheaper stand-in for `restored_from`. Rejected: the tally's whole §4 justification is
surviving a swap, and a swap makes the live kind `swap` — the rule would zero the count in
exactly the scenario the lineage exists to protect. Undone spans are excluded by the
`restored_from` jump instead (§7).

**Keep the calendar columns on the row, with an explicit carve-out.** Rejected: an
"append-only except these three columns" rule cannot be enforced by a trigger, so it is a
rule someone has to remember — which is the class of problem this design exists to remove.

**Point `benchmark_results.workout_id` at a lineage as a bug fix.** It is not a bug fix. A
benchmark result is recorded after the session is done, and prescription changes only ever
happen from today forward, so the revision it names is never superseded in normal use. It
should still be re-pointed at the lineage during the migration, because once lineages exist
it is free and strictly more correct — but as tidying, not as a repair.

## 16. Deliberately not done

- **Two sessions of the same sport on one day.** The slot key still forbids it, exactly as
  today's upsert does. Worth noting that because the *spine* is `lineage_id` and not the
  slot key, relaxing this later means adding a slot ordinal to the uniqueness rule, not
  re-keying the history model.
- **Accurate pre-migration lineages across swaps.** Step 5 assigns one lineage per slot,
  which mis-joins any session a pre-migration swap moved. Reconstructing those from
  `original_date` and a free-text prefix is guesswork over 372 rows of history that is about
  to be superseded by real data. Not worth the code.
- **Reconstructing multi-adapt counts.** §13 step 2 rebuilds one synthetic original per
  in-place-adapted row, so a legacy `adaptation_count` of 3 reads as 1 after migration —
  the intermediate forms were overwritten in place and there is nothing to rebuild them
  from. `adapted_at`, the guard's recency signal, survives exactly.
- **Pruning old revisions.** At roughly four thousand rows a year with §9's suppression in
  force, SQLite does not care. No retention policy, no vacuum job.
- **A `kind` column on `workouts`.** It would be a per-row copy of `workout_changes.kind`,
  which is exactly the denormalization this design removes elsewhere. One join.

## 17. Amendments to existing docs

To be made when this is implemented, not before:

| Document | Change |
|---|---|
| `ARCHITECTURE.md` §5 | Schema: the two new tables, the view, the sixteen dropped columns. |
| `ARCHITECTURE.md` §15 "Workout state" | The four orthogonal axes become three; *archived* is no longer an axis but a position in a chain. |
| `docs/DOMAIN_MODEL.md` §6 | Workout fields, the state axes, and invariants 10 and 11. **Done** — a row is a revision, "archived" stops being an axis. |
| `docs/DOMAIN_MODEL.md` §7 | "Two write models" collapses to one: everything appends. **Done.** |
| `docs/DOMAIN_MODEL.md` §10 | Invariant 11 is replaced by the immutability trigger; a new invariant covers the lineage. **Done.** |
| `DESIGN_plan_rollback.md` §9 | The batch key moves from `archived_at` to `change_id`. |
| `DESIGN_constraint_reschedule.md` §7 | Accommodate is no longer "adapt's write path"; both simply append. |
| `DESIGN_benchmark_workouts.md` | `benchmark_results.workout_id` names a lineage. |
| `trainmate/coach/formatting.py` | `_adapt_recency_tag` gates on the derived tally (`adaptation_count > 0`), not on the modification marker (§7). |
| `trainmate/modification_state.py` | Deleted, with its test and the two prefix constants. |
| `trainmate/db/wipes.py` | `wipe_workouts` drops the triggers, wipes the three workout tables, recreates the triggers (§14). |
| `trainmate/db/constraints.py` | `clear_honored_after` keyed on the target change's `created_at`, not `archived_at` (§10). |
| `tests/test_types.py` | `workouts` leaves `TYPE_TABLES`; a hydrated-dict key check replaces it (§5). |
