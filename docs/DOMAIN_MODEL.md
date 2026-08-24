# The TrainMate domain model

**Goals, macrocycles, mesocycles, microcycles and workouts — what each one is, what
must always be true about it, and what you can do to it.**

This document describes the current state of the code. Where a rule has a rationale
that is written down elsewhere, the pointer is given (`ARCHITECTURE.md §N`,
`designs/DESIGN_*.md §N`) rather than restated.

---

## 1. The five levels in one page

TrainMate organises training time as nested containers. Each level answers exactly one
question, and each level is written by a different command.

| Level | The question it answers | Where it is stored | Who writes it |
|---|---|---|---|
| **Goal** | What are we training for, and by when? | `objectives` table | The athlete (`goal add/edit`) |
| **Macrocycle** | What is the overall strategy from now until that goal? | `macrocycles` table | `plan generate` (one LLM call) |
| **Mesocycle** | What is this block of weeks *for*? | `mesocycles` table | `plan generate`, in the same call |
| **Microcycle** | What does a typical week look like? | **Nothing. It is not stored.** | Implied by the workouts |
| **Workout** | What do I actually do on Tuesday? | `workouts` table | `workout generate`, then `adapt` / hand edits |

Two things in that table are worth pausing on, because they are the two facts that
explain most of the design.

**The microcycle has no table.** It is a vocabulary word, not an entity. Section 6
explains why, and what stands in for it.

**"Plan" means the top two rows only.** In TrainMate, *the plan* is the periodization —
one macrocycle plus its mesocycle blocks. The scheduled sessions are *workouts*, never
"the plan". The `plan` and `workout` command families are split on exactly that line
(`ARCHITECTURE.md §11`).

### The shape of the data

```
  objectives (goal)
      │  1 goal ──► N macrocycles, but only ONE is 'active'
      ▼
  macrocycles (one row = one PLAN VERSION)
      │  ON DELETE CASCADE
      ├──────────────► mesocycles (the blocks)      ON DELETE CASCADE
      └──────────────► plan_feedback (notes)        ON DELETE CASCADE

  workouts
      │  macrocycle_id  ── a plain INTEGER tag, NOT a foreign key
      └╌╌╌╌╌╌╌╌╌╌╌╌╌╌► macrocycles
```

The dashed line matters. `workouts.macrocycle_id` records which plan version produced a
session, but there is no foreign key and therefore no cascade. Deleting a goal removes
its plan versions, blocks and feedback, and leaves its sessions behind — which is why
`goal rm` counts them and warns before it runs (§9.7).

---

## 2. Goal (`objectives`)

### What it is

A dated target. It is the only object in the system the athlete authors from nothing;
everything else is generated from it.

### The fields that carry meaning

| Field | Notes |
|---|---|
| `title` | Free text. Reaches every coach prompt verbatim. |
| `target_date` | `YYYY-MM-DD`. |
| `date_type` | `event` (default) or `horizon` — see below. |
| `sport_type` | One sport, or a comma-separated list (`running,cycling`). |
| `status` | `active` or `archived` — **only those two**. |
| `description` | Free text, read by the LLM. |

**`date_type` is the field that says what the date *means*.** An `event` is a day
something happens on, so the periodization peaks and tapers into it. A `horizon` is
just how far out the athlete wants to train: the plan still ends around that date,
but with an ordinary training block — no peak, no taper pinned to it — and the rule
that forbids a fitness test in the goal's own week does not apply, because there is no
event for a test to compete with.

This exists as a *field* rather than as prose in `description` for a concrete reason:
the event framing is structural. It is restated in the planning task, in the response
format, and in the user message. A `description` saying "this date is indicative, not a
race" loses that argument every time (`ARCHITECTURE.md §15 "Goal dates"`).

**There is no priority field, and deliberately so.** One existed: it was stored,
displayed and fingerprinted, but no code ever branched on it and
`_render_goal_lines` never emitted it, so the coach never saw the number. All it did
was change the `goals_hash` — marking the plan stale for a regeneration from a
byte-identical prompt. Goals are ordered by date, and the system prompt says so:
*"Focus scheduling on the NEXT CHRONOLOGICAL GOAL only."* A goal that matters more
than its neighbours says so in `description`, which the model does read.

### Invariants

1. `title`, `target_date` and `sport_type` are `NOT NULL`.
2. `status` is `active` or `archived`. Nothing else. A one-off migration rewrote every
   legacy `completed` row to `active`.
3. **Completion is never stored — it is derived.** A goal that is not archived and whose
   `target_date` has passed *is* completed, by definition of the date. One function,
   `db.objectives.goal_state()`, makes that call and returns
   `upcoming | completed | archived`. Every surface — `goal list`, `status`, the web view
   — renders that one function's answer, so no two of them can disagree
   (`DESIGN_backward_evaluation.md §12`).
4. A goal may own many macrocycles (plan versions), but at most one is `active`.
5. Deleting a goal cascades to every plan version, every block, and every feedback note.
   It does **not** cascade to workouts.

### Three named ways to read goals

The accessors are named after the question they ask, so a caller cannot pick the wrong
one by accident:

- **`upcoming_objectives()`** — not archived, date not yet passed. This is what "the
  goals that matter" means at every planning and picker site.
- **`get_active_objective()`** — with no ID, the *next* goal still ahead. With an ID, any
  goal that is not archived, past or future.
- **`get_preceding_objectives(date)`** — goals strictly before a date, **completed ones
  very much included**, since they are the whole point of the lookup. Bounded by
  `coach.goals_lookback_days` (default 90).

### Operations

| Command | What it does |
|---|---|
| `goal add TITLE DATE SPORT…` | Create. `--desc`, `--date-type`. |
| `goal edit ID` | Change any field. `--status archived` / `--status active` — see below. |
| `goal list` | List all. |
| `goal rm ID` | **Destructive.** Deletes the goal and cascades to every plan version, block and feedback note. Prints that inventory plus the number of upcoming sessions it would strand, then asks. |
| `goal wipe` | Delete all goals. |

**Archiving is not deleting, and the difference is the point.**

`goal edit ID --status archived` *calls the goal off*. It stands down every upcoming
session that the goal's plan versions generated (archives them, tears down their Calendar
events) and keeps the plan, its versions and its feedback log completely intact. Past
sessions stay put — a cancelled race does not un-train the work already done.

`goal edit ID --status active` reinstates it: the stood-down sessions come back, floored
at today, and are re-pushed to Calendar. A goal reinstated months later recovers only the
sessions still ahead.

The sweep is scoped by `macrocycle_id`, not by date. A date-only sweep would also displace
a *neighbouring* goal's sessions in the same window. Sessions with no `macrocycle_id`
(legacy rows) belong to no version, so they are reported rather than swept
(`DESIGN_backward_evaluation.md §14`).

---

## 3. Macrocycle (`macrocycles`) — one row is one plan version

### What it is

The overall periodization strategy for one goal: a prose `strategy` field plus the set
of mesocycle blocks hanging off it. **Every regeneration creates a new row.** The
previous row is marked `superseded` and kept, never deleted.

So "macrocycle" and "plan version" are the same thing. `plan versions` lists them;
`plan show --macrocycle <id>` renders a specific one; `plan rollback` makes an old one
active again.

### The fields, in three groups

**The content:**

| Field | Notes |
|---|---|
| `objective_id` | FK → `objectives`, cascade delete. |
| `strategy` | The LLM's prose explanation of the whole approach. |
| `created_at`, `status`, `superseded_at` | `status` is `active` or `superseded`. |

**The fingerprints** — SHA-256 of the inputs the strategy was generated from:

| Field | Covers |
|---|---|
| `goals_hash` | Every upcoming goal, cleaned and stably ordered. |
| `constraints_hash` | Only the **plan-shaping** (`replan = 1`) constraints. |
| `config_hash` | The plan-shaping `user_profile` fields (thresholds, `name` and equipment excluded). |

**The snapshots** — the inputs themselves, kept as JSON so a stale plan can say *what*
changed rather than only *that* something did: `goals_snapshot`, `constraints_snapshot`,
`all_constraints_snapshot`, `config_snapshot`, `profile_snapshot`.

The hash detects the change; the snapshot names the field that moved.

### Invariants

1. **Exactly one active version per goal.** `save_macrocycle` supersedes the current
   active row before inserting; `set_active_macrocycle` supersedes every other active row
   before promoting its target. Readers filter on `COALESCE(status,'active') = 'active'`.
2. **Regenerating never deletes.** The old version and its blocks survive so
   `plan rollback` can restore them (`DESIGN_plan_rollback.md`).
3. **The plan window is `plan_start … target_date`, and it must be non-empty.**
   `plan_start` is the day after the most recent preceding goal that has a plan, floored
   to today; today if there is no such goal. If `target_date <= plan_start`, generation
   refuses: *"there is no window to plan in."*
4. **No minimum or maximum plan length.** How to periodize a three-week run-in or a
   two-year horizon is a question for the science guidelines, not for a threshold in the
   app. The only check is that the window exists.
5. **TrainMate never invents intermediate goals.** An athlete who wants a tune-up race as
   a milestone adds it as a goal; `plan generate` then plans to whichever goal comes first.
6. **Fingerprints are computed when the strategy is generated and carried verbatim to
   apply.** They are never recomputed at accept time — a goal edited between generating
   and accepting would otherwise be recorded as though the strategy had seen it, silently
   defeating the staleness detector.
7. **Only `replan = 1` constraints fingerprint the plan.** All active constraints reach
   the prompt; a tactical "no run Thursday" must not trip the reuse-vs-regenerate decision
   (`DESIGN_constraints.md §7`).

### Derived state: is the plan stale?

`plan generate` reuses the existing macrocycle — no LLM call — when **all** of these hold:

- `goals_hash` matches, **and**
- `constraints_hash` matches, **and**
- `config_changed()` reports no drift (thresholds only past `coach.threshold_replan_pct`),
  **and**
- no plan feedback is pending, **and**
- `--force` was not passed.

Pending feedback opens the gate on its own: notes are a plan input, so they apply without
`--force` (`DESIGN_plan_feedback.md §7`).

**Pending** has no flag of its own. A note is pending when its `macrocycle_id` is the
goal's *currently active* version. Supersession *is* the consumption event — which gives
two behaviours for free: a regeneration previewed and declined leaves the notes pending,
and a `plan rollback` makes an earlier version's notes pending again.

### Operations

| Command | What it does |
|---|---|
| `plan generate` | Generate or reuse. `-f` forces. `--fresh` withholds the plan in place from the prompt (a clean slate, not a revision — implies `-f`). `-g ID` targets a goal. |
| `plan show` | Strategy, snapshotted inputs, block timeline with each block's session count / duration / load. `-M ID` for a superseded version, `-a` for every goal, `-w` to list each block's sessions. |
| `plan versions` | Every kept version for a goal, active and superseded, with IDs and dates. |
| `plan diff [A] [B]` | Compare two versions: strategy prose, attached feedback, blocks added/removed/renamed/re-dated, snapshot deltas. |
| `plan rollback` | Make a superseded version active again, and reconcile the workouts with it. |
| `plan feedback` | Append a note to the append-only log for the next version. `-m` files it to one block. `--rm ID` deletes one. `--replan` regenerates immediately. |
| `plan rm` / `plan wipe` | Delete. |

What `plan generate` reads before it calls the model: the science guidelines, the athlete
profile, every upcoming goal, every active constraint, a 15-day training and metrics
summary (including the current CTL / ATL / TSB block), a planned-vs-actual review of the
prior plan, the active coach learnings, and the pending feedback log — which it **must**
address note by note.

What it does *not* do: touch coach learnings (read-only), or write any workouts.
Generating a new strategy leaves the existing sessions exactly where they are until
`workout generate` runs.

---

## 4. Mesocycle (`mesocycles`) — the block

### What it is

A phase of training with one job: *"Base Building, 2026-09-01 to 2026-10-05, Zone 2
aerobic base, high volume."*

That is genuinely all it is. The table has four meaningful columns:

| Field | Notes |
|---|---|
| `macrocycle_id` | FK → `macrocycles`, cascade delete. |
| `name` | e.g. "Base Building", "Peak & Taper". |
| `start_date`, `end_date` | Inclusive `YYYY-MM-DD` bounds. |
| `focus` | Prose: what this block is for. |

There is **no** stored load target, no weekly hours, no intensity distribution, no deload
flag. A block states what it is for, and the workout generator derives the rest from that
sentence plus the science guidelines.

### Invariants

**Enforced by the code:**

1. A mesocycle belongs to exactly one macrocycle, and dies with it.
2. Blocks are always read in `start_date` order.
3. Blocks are only reachable through an **active** macrocycle. Most readers additionally
   require the owning goal to be non-archived. The one deliberate exception is
   `get_mesocycle_ranges`, which ignores goal status — a since-completed goal still
   planned its dates — while still excluding superseded *versions*, so an old version's
   dates cannot double-count the active one's.
4. Within one plan, blocks are contiguous: no gap and no overlap between one block's
   `end_date` and the next's `start_date`. The model is asked for this, and
   `save_macrocycle` repairs what comes back rather than trusting it
   (`repair_block_contiguity`): end dates are authoritative, a start that disagrees with
   its predecessor's end is re-dated to the day after it, and a block that ends inside
   its predecessor is dropped. `plan apply` runs the same repair first and prints a note
   per fix, so a repaired plan is never silently different from the one shown.

**Asked of the model, but not enforced anywhere:**

5. The first block should start on the plan start date, and the last should end on or
   around the goal date.
6. Plans of *different* goals should not overlap. `plan generate` pins a new plan's
   start to the day after the latest preceding goal that has a plan — but this is
   best-effort: planning goals out of chronological order still produces two active
   plans over the same dates. That state is legitimate and transient — adding the
   earlier goal flags the later plan stale (its `goals_hash` covers every goal), and
   regenerating it re-pins its start — and the readers settle it by recency in the
   meantime (below).

Point 5 lives in the planning prompt only; the readers absorb a plan that starts late or
ends early by falling back (below).


### Reading blocks for a window: one reader

`get_governing_mesocycles(start, end, prefer_macro_id=None)` is the only window reader:
"which blocks govern these days". `end=None` means "to the plan's end", so no caller has
to invent a far-future date. It returns a **tuple**, not a list:
`(blocks, dropped_macrocycle_ids)` — the second half is a receipt, so the caller can say
which plan it ignored and offer the override:

```
Plan ID 4 also covers part of this span; following the more recently generated plan
instead. Pass -M 4 to follow that one.
```

Internally it does three things, in order (the first two are
`_get_covering_mesocycles`, its private strict half):

1. **Overlap query.** Every block whose span touches the window, belonging to an active
   plan version of a non-archived goal, in start-date order.
2. **Plan arbitration.** The query can hand back blocks from two different plans, and
   two plans cannot both be followed on the same day. Each plan gets a **footprint** —
   the earliest start and latest end among *its blocks that came back*, not its full
   span — and plans are walked in priority order (`prefer_macro_id` first, then most
   recently created first), dropping any plan whose footprint overlaps one already kept.
   Whole plans are dropped, never individual blocks, so a surviving plan is never
   half-followed. Sequential plans both survive — their footprints do not overlap —
   which is what a long horizon needs when it runs out of one goal's last block into the
   next goal's first.
3. **Fallback.** Only when nothing overlaps at all does it answer with
   `get_active_mesocycle`'s nearest block instead. `workout generate` depends on this:
   it lays sessions near a plan's edges and reads an empty answer as "there is no plan
   at all — run `plan generate`", which must not happen for a plan that merely starts
   next week.

The behaviour on one window:

| The window has… | It returns |
|---|---|
| one plan covering it | that plan's blocks, `dropped = []` |
| two plans, **sequential** | every block of both, `dropped = []` |
| two plans, **same days** | the newer plan's blocks, `dropped = [older]` |
| no block touching it, but a plan elsewhere in time | the nearest block, `dropped = []` |
| no plan at all | `([], [])` |


### The single-date readers

This is where the strict-vs-lenient fork lives: a strict reader answers only with a
block that CONTAINS the date, a lenient one falls back to the nearest. The add-time
constraint message is the strict caller (`cli/constraints.py`): it asks which block
holds a constraint's last day, and a block that does not cover that date would name the
wrong block and offer a date already behind the athlete — being handed `None` is what
lets it say "past the end of your plan" instead. Strict and lenient are two named
readers rather than one boolean flag, because a flag's meaning has to be re-derived at
every call site and a name does not. Pinned by
`test_the_strict_readers_answer_only_with_blocks_that_cover_their_target`.

All of them settle overlapping plans the same way the window reader does — the most
recently created plan wins — so no two commands name different blocks for one day.

| Reader | Answers |
|---|---|
| `get_covering_mesocycle(date)` | **Strict.** The active block containing that date, or `None`. |
| `get_active_mesocycle(date)` | **Lenient.** Covering block → first block ending in the future → the absolute first block. |
| `get_next_mesocycle(after)` | The earliest block starting strictly after a date. **Never falls back**: no block ahead means `None`. |
| `get_periodization_ids_for_date(date)` | `(objective_id, macrocycle_id, mesocycle_id)`, for stamping a session with its provenance. |


**The governing plan.** `get_governing_macrocycle()` is "the plan the current workouts
implement": the earliest goal still ahead that has a plan, falling back to the most recent
*completed* goal's plan when nothing ahead has one. The day after a race, the months of
training behind the athlete still belong to that plan, and dropping the labels then would
blank the progress timeline exactly when it is being looked at.

### The block boundary is a firewall

`workout adapt` may only rewrite sessions **up to the end of the block containing the
evaluation date**. It may not reach into the next block. Its runway therefore shrinks to
nothing as a block ends.

That is on purpose, and it is enforced on both sides:

- **Read side:** workouts are fetched with the block end as the upper bound, so
  post-boundary sessions never enter the prompt.
- **Write side:** any proposal dated past the range end is dropped, so a hallucinated date
  cannot be written. The apply range is derived from the *surviving* proposals, so it
  cannot stretch past the block either.

The reason is not the range, it is what would ride along with it. Adapt's whole input is a
backward window of recovery metrics. A longer reach would give this morning's HRV authority
over a session four weeks out, where it has no predictive claim. Periodization is authored
by `plan generate` and `workout generate`; a daily readiness check must not rewrite it
(`DESIGN_block_boundary.md §2`).

Rather than widening the firewall, both sides are made aware of it. Inside
`config.adapt_terminal_window_days` of a block's end, the adapt prompt gains a
`THIS BLOCK IS ENDING` section, and the CLI prints the exact
`workout generate -m <id>` invocation that re-plans the next block against current
metrics.

Nothing crosses that boundary. A constraint dated past it is built in by the next
`workout generate` whose horizon reaches it — which re-plans those days against the blocks
that govern them, rather than carrying today's readings across to them. What the athlete
gets in the meantime is *notice*: `constraints.honored_at` records whether any pass has had
the directive in scope, so `status`, `constraint list`/`show` and the message printed at add
time can say the plan does not reflect it yet and name the run that would
(`DESIGN_constraint_honoring.md`).

### Operations

**There is no mesocycle CRUD.** Blocks are created only as a side effect of
`plan generate`, and destroyed only by cascade. You cannot add, rename, re-date or delete
one directly.

What you *can* do with a block:

| Operation | How |
|---|---|
| Select a window by block | `-m <id>` on any command taking range selectors: `workout list -m 5`, `workout generate -m 7`, `constraint list -m 5`. |
| File feedback against a block | `plan feedback -m [ATOM] "text"` — the atom is a block ID, a date it covers, or an infix of its name. Bare `-m` means the current block. |
| Reshape a block | Change the goal, the constraints or the feedback, then `plan generate`. That is the only path. |
| Re-plan a block's sessions | `workout generate -m <id>` — the selector names the whole span to rebuild, so only that block's days are rewritten. A span never opens before today. |

A note filed to a block records the block's **name**, not just its ID, when it is later
rendered — names survive version churn, IDs do not.

---

## 5. Microcycle — the level with no table

There is no `microcycles` table, no `Microcycle` type, and no microcycle ID. Searching the
codebase for the word turns up prompt text, CLI help strings, and the science
vocabulary document — nothing else.

### What a microcycle is, conceptually

From `trainmate/science/periodization.md`, which is the vocabulary authority:

> The repeating work/rest unit from which daily workouts are allocated. **Where a plan
> document expresses doses per week ("2/week", "75% of the previous week"), the microcycle
> is 7 days** unless that plan says otherwise.

Three programming defaults come with it, and they apply unless a science document
deliberately overrides them:

- **The Fatigue Buffer** — high-intensity sessions are not scheduled on consecutive days.
- **The Aerobic Anchor** — the week's *longest* session should be a low-intensity one, so
  the greatest duration and the greatest intensity do not land on the same session.
- **The Rest Mandate** — 1 to 2 complete rest or active-recovery days in any rolling
  7-day equivalent.

### Why it is not stored

Because storing it would add nothing. A microcycle is fully described by the workouts that
implement it: seven dated rows already say which days are hard, which are long, and which
are rest. A `microcycles` row would either duplicate that or contradict it.

So the concept lives in exactly three places:

1. **The workout-generation prompt**, which asks for it by name: *"Ensure the weekly
   schedules/microcycles are designed specifically to match the focus, target volume, and
   intensity of the active mesocycle block(s)."* The model's `reasoning` field is asked to
   describe *"the shape of the week and why"* — that is the microcycle design, returned as
   prose rather than as data.
2. **The science guidelines**, as the defaults above.
3. **`progression.weekly_aggregates`**, the one place the app makes weeks concrete. It
   aggregates planned-vs-actual load into **Monday-commencing** weeks and labels each week
   with the mesocycle that has the majority overlap. `tm progress` and the block-progress
   prompt section both read it, so the coach and the athlete can never read different
   numbers.

### The practical consequence

You cannot query "the current microcycle". You query a date range. Every command that
looks like it operates on a week — `workout list -d 7d`, `progress -w 8` — is operating on
dates that happen to be a week long. The `-m` selector reaches a *block*; there is no
selector that reaches a week, because there is no week object to reach.

---

## 6. Workout (`workouts`) — the session

### What it is

One prescribed session, on one date, for one sport. Rest days are workouts too, with
`duration_minutes`, `rpe` and `tss` all zero.

### One row is one *revision*, not one session

`workouts` is an **append-only revision log** (`DESIGN_workout_revisions.md`). Nothing is
ever updated or deleted; a session that changes gains a new row. Two facts follow, and
everything else in this section follows from them:

- **The live session in a slot is the highest `id` in that `(date, sport_canonical)`.**
  That is the `live_workouts` view, and it is what every reader goes through.
- **A session's identity is its `lineage_id`**, carried across every revision of it,
  including a move to another date. That is the id `workout list` prints and the id
  `workout rm` / `swap` / benchmark results address — never the row id.

### The fields, by what kind of fact they carry

**The prescription** — `date`, `sport_canonical`, `sport_type`, `title`, `description`,
`duration_minutes`, `rpe`, `tss`, `benchmark_type`, and the seven `planned_zone*_sec`
slots with their `planned_zone_currency` (`hr` or `power`).

**What this revision is:**

| Field | Meaning |
|---|---|
| `lineage_id` | The session this revision is a form of. Equal to `id` on a first revision. |
| `change_id` | The command invocation that wrote it — a row in `workout_changes`, carrying its `kind`, its `summary` and its timestamp. |
| `void` | 1 ⇺ this slot holds **no session** as of this revision. The way a cancellation, a departure and a dropped day are all recorded. |
| `reason` | Why *this session* changed — or, on a void, why it went. |
| `restored_from` | Set when the revision is a copy of an earlier one, put back by `workout restore` or a rollback. |
| `macrocycle_id` | The plan version that wrote this revision. |
| `created_at` | When the *session* first entered the plan, carried across its lineage. Distinct from `date` and from the change's own timestamp. |

**Nothing else is stored, because nothing else needs to be.** `original_*`, `source`,
`adapted_at`, `adaptation_count` and the modification kind are all **derived from the
lineage** at read time and handed to callers on the hydrated row (§6 "Reading"). Sixteen
columns went with them, including `archived_at`, `original_description` and
`adaptation_count`.

**Calendar state lives in its own table**, `workout_calendar_state`, keyed by
`lineage_id`: `google_event_id`, `pushed_signature`, `adherence_pushed_signature`. It is
sync bookkeeping, not prescription — on the row it would make a push have to append a
revision.

### Reading — the hydrated row

`get_workouts` and friends return the live revision **plus the lineage-derived fields**, so
every caller above `db/workouts.py` sees the same dict shape it always did: `id` (the
lineage), `revision_id` (the physical row), `original_*`, `source`, `adapted_at`,
`adaptation_count`, `change_kind`, and the Calendar columns joined in. The revision model
stops at that boundary.

Two derivations are worth naming:

- **`adaptation_count` / `adapted_at`** walk the lineage backwards, jumping over any span
  an undo took back, stopping at the first `generate`, and counting only the `adapt`
  revisions whose duration or TSS actually *fell*. Because the walk follows the lineage it
  survives a date move — which is why `lineage_id` exists at all.
- **`original_*`** is simply the lineage's first revision.

### Invariants

1. **The log is append-only.** Two SQL triggers refuse every `UPDATE` and every `DELETE`
   on `workouts`, with one exception: seeding a first revision's `lineage_id` to its own
   id, which cannot be known until the insert assigns it. There is no other way in.
2. **At most one *live* session per (date, canonical sport)** — by construction, not by a
   lookup: the live row is the highest `id` in the slot, and there is exactly one. The
   slot key is the **canonical** sport, so a revision spelled `strength` and one spelled
   `strength_training` are the same slot; the stored row keeps its own spelling.
3. **Creation-time facts carry forward.** A revision is the slot's live one merged with
   what the writer supplied, so a partial re-save cannot read an omission as a deletion.
   `benchmark_type` has one escape hatch — `clear_benchmark=True` — for an adaptation that
   replaces a test with something that is no longer that test. The flag belongs to the
   *test*, not to the date. A revision that starts a **new** lineage — a manual `add`, a
   session appended over a void, a swap's destination — deliberately inherits nothing.
4. **A no-op is not written.** A revision prescribing exactly what the live one already
   does is dropped, so `workout adapt` on a settled day leaves no trace and re-running a
   generation does not double the table.
5. **Calendar freshness falls out rather than being arranged.** The live row after any
   change is a *different row*, so its hash differs from the stored `pushed_signature` and
   it reads stale without any writer remembering to reset a flag.
6. **`macrocycle_id` is a tag, not a foreign key.** No cascade. See §9.7.
7. **Nothing is written by a `propose` method.** `workout_generate` returns a
   `GenerateProposal`; only `workout_generate_apply` writes. This is enforced structurally:
   `tests/test_service_invariants.py` parses the source and fails any method returning a
   `*Proposal` that calls a database write, any proposal-taking method that fails to record
   the pass, and any revision preview that reads the database instead of drawing the
   proposal it was handed.

### Workout state is three orthogonal axes, not one enum

There is no `status` column. Three independent facts, all derived:

| Axis | How you ask it |
|---|---|
| **Modified?** | The `kind` of the change that wrote the live revision — `generate` / `adapt` / `swap` / `add` / … There is no stored flag and no precedence rule: the kind says what last happened, and the derived tally says how often the session has been eased, so one walked down twice and then moved reads `[SWAPPED, ADAPTED ×2]`. |
| **Removed?** | The live revision is a **void**. `workout rm` appends one carrying the athlete's reason; nothing is deleted, and every revision before the void is still in the log. Hidden from listings, comparisons, adaptation inputs and the Calendar push, but still shown to the coach as a deliberate *cancellation* — which is not the same thing as a miss. |
| **Pushed / fresh?** | Comparing the live hash of the Calendar-relevant fields against `pushed_signature`. Never stored as a boolean. |

**"Archived" is no longer an axis.** It was the old model's way of saying "this row is not
the current one", and in a revision log that is not a property of a row at all — it is a
*position in a chain*. A revision is live because it is the newest in its slot, and
superseded because something newer exists. Nothing has to be stamped for that to be true.

The two things `archived_at` did are now done separately and better: undo keys on
`change_id` (the batch that *wrote* rows, not the one that killed them, so an `adapt` is
undoable on its own), and a day the plan emptied is stated by a void rather than left
implicit in an absence.

Voids carry the same distinction `removed` and `archived_at` used to: a void from `rm` or
a goal stand-down is the athlete cancelling, and its Calendar event is kept, retitled
`[Deleted]`; a void from a `generate` or an `adapt` is the app dropping a day, and the
event goes with it.

The rationale for deriving rather than storing is in `ARCHITECTURE.md §15 "Workout
state"`. The short version: a stored enum has to be updated by every writer, and the day
one writer forgets, the enum is lying with no way to tell.

### Operations

| Command | What it does |
|---|---|
| `workout list` | Show planned sessions. Default 7-day forward window. |
| `workout compare` | Planned vs completed, with misses, rest violations and unplanned high load. Today's untrained sessions read *"(not yet — still ahead today)"* and are **not** misses. |
| `workout generate` | Write the sessions for a horizon, from the blocks governing those days. |
| `workout adapt` | Daily readiness adjustment, within the current block only. |
| `workout add` | Manually schedule one session. No LLM. Replaces the same-sport session that day (or, with `--replace-day`, every session that day), recording what it overwrote. Deliberately does **not** re-balance surrounding days — that is `adapt`'s job. |
| `workout swap` | Exchange two sessions' dates, or move one onto a rest day. Mandatory reason. Validated first: warns about new >2-day hard streaks, weekly load spikes, and block-boundary crossings. Returning a session to its `original_date` clears `modification_reason` — it is no longer modified. |
| `workout rm` / `restore` | Soft-delete one session and its Calendar event, and undo that. |
| `workout rollback` / `batches` | Undo a whole change and everything after it, point-in-time. `batches` lists the changes, most recent first. Any change qualifies — an `adapt` is undoable on its own, which the old batch key could not express. |
| `workout push` | Sync to Google Calendar. Only unsynced rows unless `-f`. |
| `workout prune-calendar` | Delete Calendar events no local row references. |
| `workout wipe` | Delete all. |

---

## 7. Who is allowed to write what

This is the central discipline of the system. Each command owns one level, and a lower
level may never rewrite a higher one.

| Command | Writes | Horizon | Reads recovery metrics? | LLM calls |
|---|---|---|---|---|
| `plan generate` | macrocycle + mesocycles | plan start → goal date (`-g <id>` opens it at the goal's own span) | 15-day summary + PMC block | 1 |
| `workout generate` | workout revisions | the span `-d`/`-m`/`-M`/`-g` names, default today → 28 days | Yes — full `metrics_lookback_days` window | 1 |
| `workout adapt` | workout revisions | evaluation date → **end of the current block** | Yes — full window, plus daily signals | 1 |
| `workout add` / `swap` / `rm` | one or two workout revisions | a single date | No | 0 |

Read that table top to bottom as an authority ladder:

- `plan generate` may reshape everything, and costs the most to run.
- `workout generate` may rewrite sessions freely, but only within the blocks it was
  handed. It cannot move a block boundary.
- `workout adapt` may not cross a block boundary at all.
- `workout add` / `swap` / `rm` touch exactly what you name and nothing else.

**There is one write model, and every command uses it.** There used to be two —
archive-and-rebuild for `workout generate`, edit-in-place for `adapt` —
and they disagreed about what a change *was*, which is why undo could reach one and not
the other. Under the revision log both are the same act: append. A command that empties a
day appends a void; a command that changes a session appends its new form; a command that
moves one appends the new form at the destination carrying the same lineage, and a void
where it left.

Writing goes through **one handle** and there is no other way in:

```python
with db.workout_change(kind="adapt", summary=why) as change:
    change.append(date=..., sport_type=..., title=..., ...)   # a new form
    change.void(date=..., sport_type=...)                     # this slot is now empty
    change.restore(revision)                                  # put an earlier form back
# on exit: commit, then reconcile Calendar over the lineages touched
```

The handle opens the `workout_changes` row that every revision points at, applies the
lineage rules, drops a revision that changes nothing, and — because the Calendar has to
agree with the log and no command should have to remember that — runs the reconcile pass
when it closes.

---

## 8. A worked example

An athlete has a marathon on 2026-11-15.

**1. The goal.**

```
./tm goal add "Autumn Marathon" 2026-11-15 running
```

One row in `objectives`. `date_type` defaults to `event`, so the plan will peak and taper
into that date. `goal_state()` reports `upcoming`. Nothing else exists yet.

**2. The plan.**

```
./tm plan generate
```

The plan window runs from today to 2026-11-15. TrainMate assembles the science guidelines,
the athlete profile, every upcoming goal, every active constraint, a 15-day training and
metrics summary with the current CTL/ATL/TSB, a planned-vs-actual review of any earlier
plan, and the coach learnings — one LLM call — and gets back a `strategy` plus a list of
blocks:

```
- Base Building        (2026-08-22 → 2026-09-26)  Zone 2 aerobic base, high volume
- Specific Preparation (2026-09-27 → 2026-10-24)  Threshold work, marathon pace
- Build                (2026-10-25 → 2026-11-07)  Peak long runs, race simulation
- Peak & Taper         (2026-11-08 → 2026-11-15)  Volume down, intensity held
```

One `macrocycles` row (`status = 'active'`), four `mesocycles` rows. **No workouts yet.**

**3. The sessions.**

```
./tm workout generate
```

TrainMate resolves which blocks govern today → today+27 (that lands entirely in Base
Building), reads the metrics window, builds the block-progress context, and asks the model
for a schedule. It prints the proposed sessions and asks. On `y`, it archives any existing
future sessions, writes the new rows tagged with the active `macrocycle_id`, and pushes
them to Calendar.

The weekly shape those 28 rows fall into *is* the microcycle. It is visible in the rows and
described in the model's `reasoning` prose. It is stored as neither.

**4. A bad morning.**

```
./tm workout adapt
```

HRV is down, sleep was poor. Adapt reads the backward metrics window and may rewrite
sessions from today to **2026-09-26** — the end of Base Building — and no further. Each
row it eases gets a `modification_reason`, the shared `adaptation_summary`, and an
`adapted_at` stamp (the last only if the load actually moved, so a pure drift correction
does not raise the "already eased" bar for next time).

**5. A trip in October.**

```
./tm constraint add "Work trip, no bike" --start 2026-10-12 --end 2026-10-16
./tm workout generate -m 7
```

The constraint is dated inside Specific Preparation — too far off for adapt to reach, too
small to trip the replan heuristic. So `constraint add` says so, naming the block it lands
in and the run that would cover it; until then `status` and `constraint list` both mark it
*not yet in the plan*. The generate re-plans from today through that block's end, building
around the trip like any other stored directive, and stamps the constraint's `honored_at`
because its whole remaining window sat inside what was written.

**6. Second thoughts about the plan.**

```
./tm plan feedback -m "Base Building" "This block is too long — I plateau after four weeks"
./tm plan generate
```

The note goes into the append-only log against the active version. Because a note is
pending, `plan generate` regenerates without `--force`, and the prompt requires the model
to address every note. The result is macrocycle **version 2**, active; version 1 is now
`superseded`, kept, and its feedback log is consumed by that supersession.

The existing workouts are still tagged with version 1 and still sitting on the calendar.
They only change when `workout generate` runs again.

**7. Regret.**

```
./tm plan rollback
```

Version 1 becomes active again. The current future sessions are archived and their
Calendar events torn down; version 1's archived sessions are resurrected and re-pushed.
Version 1's feedback notes are pending once more.

---

## 9. What happens when something changes

### 9.1 The goal's date, title, description or sport moves

`goals_hash` changes → the plan reads stale → the next `plan generate` regenerates instead
of reusing, and the staleness message names the field that moved (from `goals_snapshot`).
Nothing happens automatically.

### 9.2 `date_type` flips between `event` and `horizon`

`_clean_goals` includes the field **only when it is `horizon`**. So an event goal — every
goal that predates the field — hashes exactly as it always did, and shipping the field did
not flag existing plans stale. Flipping a goal either way adds or removes the key, changes
`goals_hash`, and prompts the replan that change genuinely warrants.

Removing `priority` (§2) was the opposite case, and deliberately so. Dropping the key from
`_clean_goals` changed `goals_hash` for every plan generated before it, so every existing
plan read stale once on upgrade. The alternative — keeping a vestigial `'priority': 1` in
the fingerprint forever to preserve old hashes — buys one avoided replan at the cost of a
constant nobody would dare delete later. One replan prompt was the cheaper side.
`plan diff` across that boundary also reports `priority` disappearing from the goals
snapshot; that is the same one-time artifact, not a bug.

### 9.3 A constraint is added

- With `--replan`: `constraints_hash` changes → the plan is stale → `plan generate`
  rebuilds around it.
- Without: no staleness at all. The constraint still reaches every coach prompt, and is
  honoured by `workout adapt` or the next `workout generate`, whichever gets there first.
  Until one of them has, `honored_at` is NULL and every surface that lists the constraint
  says so.

At add time, a magnitude heuristic *proposes* escalation — when the constraint displaces at
least `replan_displaced_load_pct` (default 50%) of a typical week's planned load, or is a
rest window of at least `replan_rest_span_days` (default 3) days. It only ever proposes;
escalation is the athlete's call.

`honored_at` records when a coach pass last had the constraint in scope **with authority
over every day of it still ahead**. `NULL` means the plan does not reflect it yet. It is
deliberately *not* a claim that the plan changed. Editing a constraint's window or text
clears it, and so does a rollback restoring a plan older than the honouring.

### 9.4 A threshold or profile field changes

`config_hash` changes, but thresholds are compared against `coach.threshold_replan_pct`
drift rather than exact equality — a one-watt FTP change is not a reason to re-plan.
Profile fields that cannot reshape a plan (`name`, equipment) are excluded from the hash
entirely.

### 9.5 The plan is regenerated

The old macrocycle is superseded and kept, with its blocks and its feedback. A new active
macrocycle is inserted. **Workouts are untouched** — they still carry the old version's
`macrocycle_id` — until `workout generate` runs, which archives from today forward and
rebuilds.

### 9.6 A goal is archived and later reinstated

Archiving stands down the upcoming sessions of *that goal's* plan versions and clears their
Calendar events. The plan, its versions and its feedback survive intact. Past sessions stay.
Reinstating restores the stood-down batch, floored at today, and re-pushes it.

### 9.7 A goal is deleted

The cascade takes every plan version, every block and every feedback note. It does **not**
take the workouts, because `workouts.macrocycle_id` is a plain integer with no foreign key.
Those sessions are left with no plan to explain them, which is why `goal rm` counts them
first and says so:

```
Removing goal 'Autumn Marathon' (ID 3) also deletes:
  - 2 periodization plan version(s)
  - 8 mesocycle block(s)
  - 3 plan feedback note(s)
  and leaves 26 upcoming session(s) with no plan to explain them.
```

`goal edit --status archived` is the reversible alternative, and the one to reach for
unless the goal was entered by mistake.

---

## 10. Invariant summary

| # | Invariant | Enforced where |
|---|---|---|
| 1 | A goal's `status` is only `active` or `archived`; completion is derived from the date | `db/objectives.goal_state()`, one-off migration |
| 2 | Exactly one active macrocycle per goal | `save_macrocycle`, `set_active_macrocycle`, every reader's `status` filter |
| 3 | Regeneration supersedes, never deletes | `save_macrocycle` |
| 4 | The plan window must be non-empty (`goal date > plan start`) | `plan_generate` raises |
| 5 | Plan fingerprints are computed at generate time and carried to apply | `PlanFingerprints` passed through `plan_apply` |
| 6 | Only `replan = 1` constraints fingerprint the plan | `plan_generate` filters before hashing |
| 7 | A mesocycle belongs to one macrocycle and dies with it | FK `ON DELETE CASCADE` |
| 8 | Adapt may not write past the end of the current block | Read bound + write-side filter |
| 9 | A pass may stamp `honored_at` only for a constraint whose whole remaining window it wrote | `honoring.covers`, checked at proposal time |
| 10 | At most one live workout per (date, canonical sport) | By construction: the live row is the highest `id` in the slot (`live_workouts`) |
| 11 | `workouts` is append-only — no row is ever updated or deleted | Two SQL triggers, `RAISE(ABORT)` |
| 12 | A session's identity is its `lineage_id`, and it survives both edits and date moves | `WorkoutChange._lineage_for`; the adaptation tally walks it |
| 13 | A `propose` method never writes; an apply method always records the pass | `tests/test_service_invariants.py` (source-level) |
| 14 | Workout state is derived from orthogonal axes, never a stored enum | The change `kind`, the lineage tally, `calendar_state` |
| 15 | Weeks are Monday-commencing everywhere | `progression.weekly_aggregates` |
| 16 | Within one plan, blocks are contiguous — no gaps, no overlaps | `save_macrocycle` via `repair_block_contiguity` |

## 11. Deliberately not enforced

Worth knowing, because each of these is a decision rather than an oversight:

- **Cross-plan non-overlap and full coverage.** Within one plan, contiguity is repaired
  on save (invariant 16); across plans and at a plan's edges the readers absorb the
  consequences instead: the lenient readers fall back, and overlapping plans are settled
  by recency. Enforcing it on save would mean rejecting the new plan or destructively
  editing another goal's already-saved one; read-time recency keeps both intact and lets
  regeneration heal the overlap.
- **The relationship between a session's planned zone seconds and its duration.** Stored
  exactly as the model emitted them. A session whose zone seconds do not sum to
  `duration_minutes` is a prescription, not an accounting identity — silently scaling it
  would put the app back in the business of correcting the model rather than aligning for it.
- **A block having any load target at all.** A block states a name, a span and a focus.
  Everything quantitative is derived downstream from the sessions.
- **Workouts pointing at a real macrocycle.** No foreign key, so no cascade, so a deleted
  goal strands its sessions — reported at `goal rm` time rather than prevented.
- **The gap between plans.** `get_active_mesocycle` snaps from "one day left" to "the whole
  next block" across a calendar gap rather than tapering. Within one plan such a gap no
  longer exists (invariant 16); one can still open between two goals' plans. Both features
  that read it gate on `0 <= days_left <= N`, so neither misfires. Recorded in
  `DESIGN_block_boundary.md §6` rather than fixed.

---

## Further reading

| Topic | Document |
|---|---|
| Periodization vocabulary and defaults | `trainmate/science/periodization.md` |
| Full schema and module map | `ARCHITECTURE.md` §5, §2 |
| Plan vs workout terminology | `ARCHITECTURE.md` §11 |
| Plan versioning and rollback | `designs/DESIGN_plan_rollback.md` |
| The block firewall | `designs/DESIGN_block_boundary.md` |
| Whether the plan reflects a constraint | `designs/DESIGN_constraint_honoring.md` |
| Constraints and the replan escalation | `designs/DESIGN_constraints.md` |
| Plan feedback log | `designs/DESIGN_plan_feedback.md` |
| Goal states and archiving | `designs/DESIGN_backward_evaluation.md` §12, §14 |
| Why `workouts` is an append-only log | `designs/DESIGN_workout_revisions.md` |
| What a Calendar event shows of a session's history | `designs/DESIGN_calendar_lineage.md` |
