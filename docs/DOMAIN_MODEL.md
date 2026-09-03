# The TrainMate domain model

**Goals, macrocycles, mesocycles, microcycles and workouts — what each one is, how they
fit together, what must always be true about them, and how each one is born, changes and
ends.**

This document describes the current state of the code. Where a rule has a rationale that
is written down elsewhere, the pointer is given (`ARCHITECTURE.md §N`,
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

**The microcycle has no table.** It is a vocabulary word, not an entity. Section 5
explains why, and what stands in for it.

**"Plan" means the top two rows only.** In TrainMate, *the plan* is the periodization —
one macrocycle plus its mesocycle blocks. The scheduled sessions are *workouts*, never
"the plan". The `plan` and `workout` command families are split on exactly that line
(`ARCHITECTURE.md §11`).

### How they relate — the shape of the data

```
  objectives (goal)
      │  1 goal ──► N macrocycles, but only ONE is 'active'
      ▼
  macrocycles (one row = one PLAN VERSION)
      │  ON DELETE CASCADE
      ├──────────────► mesocycles (the blocks)      ON DELETE CASCADE
      └──────────────► plan_feedback (notes)        ON DELETE CASCADE

  workouts (one row = one REVISION of one session)
      │  macrocycle_id  ── a plain INTEGER tag, NOT a foreign key
      └╌╌╌╌╌╌╌╌╌╌╌╌╌╌► macrocycles
```

Read it top to bottom:

- A **goal** owns its plan versions. There can be many, because every regeneration
  makes a new one, but exactly one is active.
- A **macrocycle** owns its blocks and its feedback notes. They are created with it and
  deleted with it. Nothing else creates or deletes a block.
- A **workout** points at the plan version that wrote it, but only as a label. The
  dashed line matters: there is no foreign key and therefore no cascade. Deleting a goal
  removes its plan versions, blocks and feedback, and leaves its sessions behind. That
  is why deleting is not what `goal rm` does — it calls the goal off instead, which
  stands those sessions down properly. The delete survives as `goal rm --purge`, and it
  counts the sessions it would strand and warns before it runs (§9.7).

### How they relate — the shape of time

The same five things, laid out along the calendar:

```
  today                                                            goal date
    │                                                                  │
    ▼                                                                  ▼
    ├───────── Base ─────────┼──── Build ────┼──── Peak ────┼── Taper ──┤   ← mesocycles
    │                        │               │              │           │
    ├──────────────────────── one macrocycle (the plan) ────────────────┤
    │
    ├─ wk ─┼─ wk ─┼─ wk ─┼─ wk ─┼ …                                        ← microcycles
    │
    ▪ ▪ ▪ ▪ ▪ ▪ ▪ ▪ ▪ ▪ ▪ ▪ ▪ ▪ ▪ ▪ …                                     ← workouts
```

- The **macrocycle** spans from the plan start (usually today) to the goal date.
- The **mesocycles** tile that span with no gaps and no overlaps (§4, invariant 4).
- The **microcycles** are the weeks. They are not drawn in the database; they are what
  seven consecutive workout rows look like when you read them together (§5).
- The **workouts** are the dots. Each one sits on one date, in one sport, and is
  labelled with the plan version that produced it.

### The lifecycles side by side

Each level has its own notion of "alive", and they are deliberately different:

| Level | Born when | Alive means | Ends when | Can it come back? |
|---|---|---|---|---|
| Goal | `goal add` | not archived, date not yet passed (*upcoming*) | the date passes (*completed*), or the athlete calls it off (*archived*) | Yes — `goal edit --status active` reinstates a called-off goal |
| Macrocycle | `plan generate` accepted | `status = 'active'` — the one version the workouts follow | a newer version supersedes it | Yes — `plan rollback` makes a superseded version active again |
| Mesocycle | with its macrocycle | its macrocycle is active and its goal is not archived | with its macrocycle | Only with its macrocycle |
| Microcycle | — | — | — | — (not an entity) |
| Workout | first revision of a lineage (`generate`, `add`, a swap or a cross-sport substitution landing) | its newest revision is the newest thing in its `(date, sport)` slot, and is not a void | a void revision is appended (`rm`, `generate`, `adapt`, a goal stood down) | Yes — `restore`, `rollback`, `reinstate` append a copy of an earlier revision |

Sections 2 to 6 take each level in turn. Section 7 says who may write what. Section 8 is
a worked example. Section 9 lists what happens when something upstream changes.

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
displayed and fingerprinted, but no code ever branched on it and the goal renderer never
emitted it, so the coach never saw the number. All it did was change the `goals_hash` —
marking the plan stale for a regeneration from a byte-identical prompt. Goals are
ordered by date, and the system prompt says so: *"Focus scheduling on the NEXT
CHRONOLOGICAL GOAL only."* A goal that matters more than its neighbours says so in
`description`, which the model does read.

### Lifecycle

A goal has three states, but only one of them is stored:

```
                goal add
                   │
                   ▼
   ┌───────────────────────────┐   the date passes    ┌───────────────────────────┐
   │         UPCOMING          │ ───────────────────► │         COMPLETED         │
   │ status = active           │   (nothing written)  │ status = active           │
   │ target_date >= today      │                      │ target_date <  today      │
   └───────────────────────────┘                      └───────────────────────────┘
          │            ▲                                       │            ▲
   goal rm │            │ goal edit                     goal rm │            │ goal edit
   (calls  │            │ --status active               (calls  │            │ --status active
    off)   ▼            │                                off)   ▼            │
   ┌────────────────────────────────────────────────────────────────────────────┐
   │                               ARCHIVED                                     │
   │                          status = archived                                 │
   └────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        │ goal rm --purge  (also from UPCOMING / COMPLETED)
                                        ▼
                                     deleted
```

**Completion is never stored — it is derived.** A goal that is not archived and whose
`target_date` has passed *is* completed, by definition of the date. One function,
`db.objectives.goal_state()`, makes that call and returns
`upcoming | completed | archived`. Every surface — `goal list`, `status`, the web view
— renders that one function's answer, so no two of them can disagree
(`DESIGN_backward_evaluation.md §12`). A goal whose date is *today* is still upcoming.

**Archiving is not deleting, and the difference is the point.**

`goal rm ID` and `goal edit ID --status archived` are two names for one action: they
*call the goal off*. That stands down every upcoming session the goal's plan versions
generated — one void revision per session (§6), under a single `stand-down` change — and
keeps the plan, its versions and its feedback log completely intact. Past sessions stay
put: a cancelled race does not un-train the work already done. The stood-down sessions
keep their Calendar events, retitled `[Deleted]`, because a stand-down is the athlete's
decision and the Calendar says so.

`goal edit ID --status active` reinstates it: every lineage whose live revision is still
that stand-down void gets a copy of the session the void ended, floored at today, and
the copies are re-pushed. A goal reinstated months later recovers only the sessions still
ahead. A slot that was regenerated or edited in the meantime no longer shows the
stand-down void, so it is left alone — something else owns it now.

The sweep is scoped by `macrocycle_id`, not by date, and it covers **every** plan version
the goal owns, superseded ones included, because a superseded version can still own live
sessions. A date-only sweep would also displace a *neighbouring* goal's sessions in the
same window. Sessions with no `macrocycle_id` (rows that predate the tag) belong to no
version, so they are reported rather than swept (`DESIGN_backward_evaluation.md §14`).

### Invariants

1. `title`, `target_date` and `sport_type` are `NOT NULL`.
2. `status` is `active` or `archived`. Nothing else. A one-off migration rewrote every
   legacy `completed` row to `active`.
3. Completion is derived from the date, never written (above).
4. A goal may own many macrocycles (plan versions), but at most one is `active`.
5. Deleting a goal cascades to every plan version, every block, and every feedback note.
   It does **not** cascade to workouts.

### Named ways to read goals

The accessors are named after the question they ask, so a caller cannot pick the wrong
one by accident:

- **`upcoming_objectives()`** — not archived, date not yet passed. This is what "the
  goals that matter" means at every planning and picker site.
- **`get_active_objective()`** — with no ID, the *next* goal still ahead. With an ID, any
  goal that is not archived, past or future.
- **`get_preceding_objectives(date)`** — goals strictly before a date, **completed ones
  very much included**, since they are the whole point of the lookup. Bounded by
  `coach.goals_lookback_days` (default 90).
- **`get_objectives()`** — everything, by date. The history surfaces use it.

### Operations

| Command | What it does |
|---|---|
| `goal add TITLE DATE SPORT…` | Create. `--desc`, `--date-type`. |
| `goal edit ID` | Change any field: `--title`, `--target-date`, `--sport`, `--desc`, `--date-type`, `--status`. |
| `goal list` | The goals that matter — upcoming and completed. `-a/--all` adds the ones called off. |
| `goal rm ID` | Calls the goal off — the same thing as `--status archived`, under the verb people reach for. Reversible, so it does not ask. |
| `goal rm ID --purge` | **Destructive.** Deletes the goal and cascades to every plan version, block and feedback note. Prints that inventory plus the number of upcoming sessions it would strand, then asks (`-y` skips). For a goal entered by mistake. |
| `goal wipe` | Delete all goals. |

---

## 3. Macrocycle (`macrocycles`) — one row is one plan version

### What it is

The overall periodization strategy for one goal: a prose `strategy` field plus the set
of mesocycle blocks hanging off it. **Every regeneration creates a new row.** The
previous row is marked `superseded` and kept, never deleted.

So "macrocycle" and "plan version" are the same thing. `plan versions` lists them;
`plan show -M <id>` renders a specific one; `plan rollback` makes an old one active
again.

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

### Lifecycle

```
   plan generate ──► proposal shown ──► athlete says y ──► plan_apply
                                            │
                     (declined: nothing is written, the old version stays active)
                                            ▼
                    ┌───────────────────────────────────────┐
                    │                ACTIVE                 │
                    │  the one version the workouts follow  │
                    └───────────────────────────────────────┘
                          │                        ▲
        a newer version   │                        │  plan rollback
        is saved          ▼                        │  (set_active_macrocycle)
                    ┌───────────────────────────────────────┐
                    │              SUPERSEDED               │
                    │  kept, with its blocks and its notes  │
                    └───────────────────────────────────────┘
                                        │
                                        │ plan rm / goal rm --purge / plan wipe
                                        ▼
                                     deleted
```

Three things about that diagram:

**Saving a version is what retires the previous one.** `save_macrocycle` marks the
goal's current active version `superseded` and inserts the new one in the same
transaction. There is no moment with zero or two active versions.

**Rollback is a flip, not a copy.** `plan rollback` calls `set_active_macrocycle` on
the older row: every other active version of that goal is superseded and the target is
promoted. Then it undoes every workout change made after that version's newest write,
so the sessions match the plan again (§6 "Undo"). The version that was active a moment
ago is now superseded, and can itself be rolled back to.

**Deletion is rare and total.** `plan rm` deletes *every* version the goal has, active
and superseded, and their blocks and notes go with them by cascade. Regeneration never
deletes anything.

### The plan window

The window a macrocycle is generated for is `plan_start … target_date`:

- `plan_start` is the day after the most recent *preceding* goal that has a plan
  (looking back `goals_lookback_days`), floored to today. With no such goal it is today.
- `plan generate -g ID` bounds the window to that goal's own span — the same reading,
  made explicit for a goal that is not the next one (`DESIGN_cli_selectors.md §9`).
- If `target_date <= plan_start`, generation refuses: *"there is no window to plan in."*

There is no minimum or maximum plan length. How to periodize a three-week run-in or a
two-year horizon is a question for the science guidelines, not for a threshold in the
app. The only check is that the window exists.

### Derived state: is the plan stale?

`plan generate` reuses the existing macrocycle — no LLM call — when **all** of these hold:

- `goals_hash` matches, **and**
- `constraints_hash` matches, **and**
- `config_changed()` reports no drift (thresholds only past `coach.threshold_replan_pct`),
  **and**
- no plan feedback is pending, **and**
- neither `--force` nor `--fresh` was passed.

Pending feedback opens the gate on its own: notes are a plan input, so they apply without
`--force` (`DESIGN_plan_feedback.md §7`).

**Pending** has no flag of its own. A note is pending when its `macrocycle_id` is the
goal's *currently active* version. Supersession *is* the consumption event — which gives
two behaviours for free: a regeneration previewed and declined leaves the notes pending,
and a `plan rollback` makes an earlier version's notes pending again.

### Invariants

1. **Exactly one active version per goal.** `save_macrocycle` supersedes the current
   active row before inserting; `set_active_macrocycle` supersedes every other active row
   before promoting its target. Readers filter on `COALESCE(status,'active') = 'active'`.
2. **Regenerating never deletes.** The old version and its blocks survive so
   `plan rollback` can restore them (`DESIGN_plan_rollback.md`).
3. **The plan window must be non-empty** (above).
4. **TrainMate never invents intermediate goals.** An athlete who wants a tune-up race as
   a milestone adds it as a goal; `plan generate` then plans to whichever goal comes first.
5. **Fingerprints are computed when the strategy is generated and carried verbatim to
   apply.** They are never recomputed at accept time — a goal edited between generating
   and accepting would otherwise be recorded as though the strategy had seen it, silently
   defeating the staleness detector.
6. **Only `replan = 1` constraints fingerprint the plan.** All active constraints reach
   the prompt; a tactical "no run Thursday" must not trip the reuse-vs-regenerate decision
   (`DESIGN_constraints.md §7`).

### Three readers, three different "previous plans"

These are easy to confuse, so they are named apart:

| Reader | Answers |
|---|---|
| `get_macrocycle_for_objective(goal)` | This goal's **active** version. |
| `get_previous_macrocycle_version(goal)` | An older **version** of this same goal's plan — superseded, never trained, sitting on the same dates. What `plan rollback` walks. |
| `get_preceding_macrocycle(goal)` | The active plan of the goal whose date comes **before** this one — the plan that governed the calendar *before* this plan did. What a retrospective view wants. |
| `get_governing_macrocycle()` | "The plan the current workouts implement": the earliest goal still ahead that has a plan, falling back to the most recent *completed* goal's plan when nothing ahead has one. The day after a race, the months of training behind the athlete still belong to that plan, and dropping its labels then would blank the progress timeline exactly when it is being looked at. |

### Operations

| Command | What it does |
|---|---|
| `plan generate` | Generate or reuse. `-f` forces. `--fresh` withholds the plan in place from the prompt (a clean slate, not a revision — implies `-f`). `-g` targets a goal. `-y` applies without the preview. `--show-llm-context` also prints the planned-vs-actual review it feeds the model. |
| `plan show` | Strategy, snapshotted inputs, block timeline with each block's session count / duration / load. `-M ID` for a superseded version, `-a` for every goal, `-w` to list each block's sessions. |
| `plan versions` | Every kept version for a goal, active and superseded, with IDs and dates. |
| `plan diff [A] [B]` | Compare two versions: strategy prose, attached feedback, blocks added/removed/renamed/re-dated, snapshot deltas. |
| `plan rollback` | Make a superseded version active again, and put the workouts back the way they were when it last wrote. `-M` names a version other than the previous one. |
| `plan feedback` | Append a note to the append-only log for the next version. `-m` files it to one block. `--rm ID` deletes one. `--replan` regenerates immediately. Bare `plan feedback` lists what is pending. |
| `plan rm` / `plan wipe` | Delete every version of one goal's plan / every plan. |

What `plan generate` reads before it calls the model: the science guidelines, the athlete
profile, every upcoming goal, every active constraint, a 15-day training and metrics
summary (including the current CTL / ATL / TSB block), a planned-vs-actual review of the
plans the athlete trained through, the active coach learnings, the block the athlete is
mid-way through (offered so the new plan may let it finish rather than cut it at today),
and the pending feedback log — which it **must** address note by note.

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

### Lifecycle

A block has no lifecycle of its own. It is born when its macrocycle is saved, it is
reachable while its macrocycle is active (and its goal is not archived), it becomes
unreachable when its macrocycle is superseded, reachable again on rollback, and it is
deleted only by cascade. **There is no mesocycle CRUD.** You cannot add, rename, re-date
or delete one directly; the only way to reshape a block is to change what the plan is
generated from — the goal, the constraints, the feedback — and run `plan generate`.

What *does* change over time is which block is **current**, and that is a function of
today's date, not of anything stored. A bare `-m` on any selector-taking command means
"the block covering today".

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
`THIS BLOCK IS ENDING` section. On the CLI side, the runway detector watches for the
schedule running out: inside `config.runway_warning_days` of the last scheduled session,
**every** daily surface prints the exact `workout generate` invocation that carries the
schedule on — `-m ..<id>` at a block boundary, a bare `workout generate` otherwise
(`DESIGN_runway_nudge.md §3`). When every block of the plan has already ended,
`workout adapt` refuses outright: there is nothing to adapt towards.

Nothing crosses that boundary. A constraint dated past it is built in by the next
`workout generate` whose span reaches it — which re-plans those days against the blocks
that govern them, rather than carrying today's readings across to them. What the athlete
gets in the meantime is *notice*: `constraints.honored_at` records whether any pass has had
the directive in scope, so `status`, `constraint list`/`show` and the message printed at add
time can say the plan does not reflect it yet and name the run that would
(`DESIGN_constraint_honoring.md`).

### Operations

There is no mesocycle CRUD (above). What you *can* do with a block:

| Operation | How |
|---|---|
| Select a window by block | `-m <id>` on any command taking range selectors: `workout list -m 5`, `workout generate -m 7`, `constraint list -m 5`. Bare `-m` is the block covering today. |
| File feedback against a block | `plan feedback -m [ATOM] "text"` — the atom is a block ID, a date it covers, or an infix of its name. |
| Reshape a block | Change the goal, the constraints or the feedback, then `plan generate`. That is the only path. |
| Re-plan a block's sessions | `workout generate -m <id>` — the selector names the whole span to rebuild, so only that block's days are rewritten. `-m ..<id>` runs from today to that block's end. A span never opens before today. |

A note filed to a block records the block's **name**, not just its ID, when it is later
rendered — names survive version churn, IDs do not.

---

## 5. Microcycle — the level with no table

There is no `microcycles` table, no `Microcycle` type, and no microcycle ID. Searching the
codebase for the word turns up prompt text, CLI help strings (`workout` is described as
"Manage workouts (microcycles)"), and the science vocabulary document — nothing else.

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
   numbers. `workout swap`'s weekly-load-spike check uses the same Monday week.

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

So there are two words for two things. A **slot** is a `(date, sport)` cell on the
calendar; it holds whatever revision is newest there. A **lineage** is one session's
story across every slot it has occupied. Most of the time they line up one-to-one. They
part company on a swap (one lineage, two slots: a void where it left, a copy where it
landed) and on an `add` over a generated session (one slot, two lineages: the old one
ended, the new one begun).

### The fields, by what kind of fact they carry

**The prescription** — `date`, `sport_canonical`, `sport_type`, `title`, `description`,
`duration_minutes`, `rpe`, `tss`, `benchmark_type`, and the seven `planned_zone*_sec`
slots with their `planned_zone_currency` (`hr` or `power`).

**What this revision is:**

| Field | Meaning |
|---|---|
| `lineage_id` | The session this revision is a form of. Equal to `id` on a first revision. |
| `change_id` | The command invocation that wrote it — a row in `workout_changes`, carrying its `kind`, its `summary` and its timestamp. |
| `void` | 1 ⇔ this slot holds **no session** as of this revision. The way a cancellation, a departure and a dropped day are all recorded. |
| `reason` | Why *this session* changed — or, on a void, why it went. |
| `restored_from` | Set when the revision is a copy of an earlier one, put back by `restore`, `rollback` or `reinstate`. |
| `macrocycle_id` | The plan version that wrote this revision. |
| `created_at` | When the *session* first entered the plan, carried across its lineage. Distinct from `date` and from the change's own timestamp. |

The change `kind` is one of nine words, fixed at write time: `generate`, `adapt`, `swap`,
`add`, `rm`, `restore`, `rollback`, `stand-down`, `reinstate`.

**Nothing else is stored, because nothing else needs to be.** `original_*`, `source`,
`adapted_at`, `adaptation_count` and the modification kind are all **derived from the
lineage** at read time and handed to callers on the hydrated row (below).

**Calendar state lives in its own table**, `workout_calendar_state`, keyed by
`lineage_id`: `google_event_id`, `pushed_signature`, `adherence_pushed_signature`. It is
sync bookkeeping, not prescription — on the row it would make a push have to append a
revision.

### Lifecycle of one session

```
              generate / add / swap-landing / cross-sport substitution
                                     │
                                     ▼  first revision: lineage_id = id
   ┌────────────────────────────────────────────────────────────────────┐
   │                               LIVE                                 │
   │   the newest revision in its slot, and void = 0                    │
   │                                                                    │
   │      adapt / swap / add / generate ──► a new revision, same        │
   │      lineage (a swap lands it in another slot)                     │
   └────────────────────────────────────────────────────────────────────┘
          │                                              ▲
          │ rm, stand-down                               │ restore, rollback,
          │ (the athlete's voids: event kept, retitled)  │ reinstate:
          │                                              │ a COPY of an earlier
          │ generate, adapt                              │ revision, stamped
          │ (the plan's voids: event deleted)            │ restored_from
          ▼                                              │
   ┌────────────────────────────────────────────────────────────────────┐
   │                               VOID                                 │
   │   the newest revision in its slot is a void: no session this day   │
   └────────────────────────────────────────────────────────────────────┘
          │
          │ something newer takes the slot (a generate, an add)
          ▼
        SUPERSEDED — the lineage is still in the log, but a different
        lineage now speaks for that slot
```

Nothing in that diagram is a flag being set. A session is live because its revision is
the newest in its slot; it is void because that newest revision says so; it is superseded
because something newer exists. Every earlier revision is still there, which is what makes
`workout list -v` able to show a session's history and what makes undo a copy rather than
an un-delete.

A few transitions deserve a sentence each:

- **A `generate` over a manually added session starts a new lineage.** The plan owns the
  horizon, so the generate proceeds, but under a new lineage — which is also what keeps
  `source` honest. The command names each replaced session and the change to undo.
- **Appending over a void is a new session, not a resurrection.** It must not inherit the
  removed session's Calendar event, its originals or its tally.
- **A swap carries the lineage to the destination.** That is why `lineage_id` exists at
  all: the adaptation tally has to follow the session across the move, or the coach
  would cut an already-cut session again.
- **A void is always the last chapter of the lineage it ends**, never a first revision.
- **A no-op is not written.** A revision prescribing exactly what the live one already
  does is dropped, so an `adapt` on a settled day leaves no trace and re-running a
  generation does not double the table. The change row is still written, flagged `held`.

### Reading — the hydrated row

`get_workouts` and friends return the live revision **plus the lineage-derived fields**, so
every caller above `db/workouts.py` sees one dict shape: `id` (the lineage),
`revision_id` (the physical row), `original_*`, `source`, `adapted_at`,
`adaptation_count`, `change_kind`, `removed`, and the Calendar columns joined in. The
revision model stops at that boundary.

Two derivations are worth naming:

- **`adaptation_count` / `adapted_at`** walk the lineage backwards, jumping over any span
  an undo took back, stopping at the first `generate`, and counting only the `adapt`
  revisions whose duration or TSS actually *fell*. Because the walk follows the lineage it
  survives a date move.
- **`original_*`** is simply the lineage's first revision.

### Workout state is three orthogonal axes, not one enum

There is no `status` column. Three independent facts, all derived:

| Axis | How you ask it |
|---|---|
| **Modified?** | The `kind` of the change that wrote the live revision — `generate` / `adapt` / `swap` / `add` / … There is no stored flag and no precedence rule: the kind says what last happened, and the derived tally says how often the session has been eased, so one walked down twice and then moved reads `[SWAPPED, ADAPTED ×2]`. A restored copy reads as whatever the revision it copied was. |
| **Removed?** | The live revision is a **void**. `workout rm` appends one carrying the athlete's reason; nothing is deleted, and every revision before the void is still in the log. Hidden from listings, comparisons, adaptation inputs and the Calendar push, but still shown to the coach as a deliberate *cancellation* — which is not the same thing as a miss. Only the athlete's voids (`rm`, `stand-down`) are shown that way; a day the plan simply stopped scheduling is not a cancellation. |
| **Pushed / fresh?** | Comparing the live hash of the Calendar-relevant fields against `pushed_signature`. Never stored as a boolean. |

**"Archived" is not an axis.** It was the old model's way of saying "this row is not the
current one", and in a revision log that is not a property of a row at all — it is a
*position in a chain*. Nothing has to be stamped for that to be true. Undo keys on
`change_id` (the batch that *wrote* rows, not the one that killed them, so an `adapt` is
undoable on its own), and a day the plan emptied is stated by a void rather than left
implicit in an absence.

Voids come in two kinds, and the Calendar tells them apart: a void from `rm` or a goal
stand-down is the athlete cancelling, and its Calendar event is kept, retitled
`[Deleted]`; a void from a `generate` or an `adapt` is the app dropping a day, and the
event goes with it.

The rationale for deriving rather than storing is in `ARCHITECTURE.md §15 "Workout
state"`. The short version: a stored enum has to be updated by every writer, and the day
one writer forgets, the enum is lying with no way to tell.

### Invariants

1. **The log is append-only.** Two SQL triggers refuse every `UPDATE` and every `DELETE`
   on `workouts`, with one exception: seeding a first revision's `lineage_id` to its own
   id, which cannot be known until the insert assigns it. There is no other way in.
2. **At most one *live* session per (date, canonical sport)** — by construction, not by a
   lookup: the live row is the highest `id` in the slot, and there is exactly one. The
   slot key is the **canonical** sport, so a revision spelled `strength` and one spelled
   `strength_training` are the same slot; the stored row keeps its own spelling.
3. **Creation-time facts carry forward.** A revision that continues a session is the
   slot's live one merged with what the writer supplied, so a partial re-save cannot read
   an omission as a deletion. `benchmark_type` has one escape hatch —
   `clear_benchmark=True` — for an adaptation that replaces a test with something that is
   no longer that test. A revision that starts a **new** lineage deliberately inherits
   nothing.
4. **A no-op is not written** (above).
5. **Calendar freshness falls out rather than being arranged.** The live row after any
   change is a *different row*, so its hash differs from the stored `pushed_signature` and
   it reads stale without any writer remembering to reset a flag.
6. **`macrocycle_id` is a tag, not a foreign key.** No cascade. See §9.7.
7. **Nothing is written by a `propose` method.** `workout_generate` returns a
   `GenerateProposal`, `workout_adapt` a `RevisionProposal`; only the matching `*_apply`
   writes. This is enforced structurally: `tests/test_service_invariants.py` parses the
   source and fails any method returning a `*Proposal` that calls a database write, any
   proposal-taking method that fails to record the pass, and any revision preview that
   reads the database instead of drawing the proposal it was handed.

### Undo

Every write is a change, and every change is undoable. `workout batches` lists them,
newest first; `workout rollback [--batch N]` reverts change N *and every change after
it*, point-in-time. With no target it undoes the newest change — which is what makes an
`adapt` undoable on its own.

Undo works per **slot**, not per lineage: for every slot that change N or a later change
wrote, the revision that was live there just before N is copied forward (stamped
`restored_from`); a slot that held nothing before N gets a void. It never reaches into
the past: appending a copy into a slot already trained would silently make it the live
session for a day that is done. `plan rollback` is the same primitive, run from the
change after the restored version's newest write.

### Operations

| Command | What it does |
|---|---|
| `workout list` | Show planned sessions. Default 7-day forward window. `-v` shows each session's lifecycle. |
| `workout compare` | Planned vs completed, with misses, rest violations and unplanned high load. Today's untrained sessions read *"not yet"* and are **not** misses. |
| `workout generate` | Write the sessions for a span, from the blocks governing those days (details in §7). |
| `workout adapt` | Daily readiness adjustment, within the current block only. `-m "note"` passes a free-text note in the same call. |
| `workout add` | Manually schedule one session. No LLM. Replaces the same-sport session that day (or, with `--replace-day`, every session that day), recording what it overwrote on the new session's note. Starts a new lineage. Deliberately does **not** re-balance surrounding days — that is `adapt`'s job. |
| `workout swap` | Exchange two sessions' dates, or move one onto a rest day. Mandatory reason. Validated first: warns about new >2-day hard streaks, weekly load spikes, and block-boundary crossings. |
| `workout rm` / `restore` | Append a void with the athlete's reason / append a copy of the revision that void ended. |
| `workout rollback` / `batches` | Undo (above). |
| `workout push` | Sync to Google Calendar. Only stale rows unless `-f`. |
| `workout prune-calendar` | Delete Calendar events no local row references. |
| `workout wipe` | Delete all. |

---

## 7. Who is allowed to write what

This is the central discipline of the system. Each command owns one level, and a lower
level may never rewrite a higher one.

| Command | Writes | Span | Reads recovery metrics? | LLM calls |
|---|---|---|---|---|
| `plan generate` | macrocycle + mesocycles | plan start → goal date (`-g <id>` opens it at the goal's own span) | 15-day summary + PMC block | 1 |
| `workout generate` | workout revisions | the span `-d`/`-m`/`-M`/`-g` names; by default, the day after the schedule stops, for 28 days | Yes — full `metrics_lookback_days` window | 1 |
| `workout adapt` | workout revisions | evaluation date → **end of the current block** | Yes — full window, plus daily signals | 1 |
| `workout add` / `swap` / `rm` / `restore` | one or two workout revisions | a single date | No | 0 |
| `goal rm` / `goal edit --status` | `stand-down` / `reinstate` revisions | today → the goal's last session | No | 0 |

Read that table top to bottom as an authority ladder:

- `plan generate` may reshape everything, and costs the most to run.
- `workout generate` may rewrite sessions freely, but only within the blocks it was
  handed. It cannot move a block boundary.
- `workout adapt` may not cross a block boundary at all.
- `workout add` / `swap` / `rm` touch exactly what you name and nothing else.

### What `workout generate` actually does

Because it is the command that turns a plan into a calendar, its rules are worth
spelling out:

- **The span has two ends, and the dates pick the plan.** `-d`, `-m`, `-M` and `-g` each
  name a whole span (a block's own days, a goal's whole plan span, …), and the blocks
  governing those days are what shape the sessions. The goal is never an input; it was
  only ever an indirection to the blocks (`DESIGN_cli_selectors.md §8`).
- **With no selector, it carries the schedule on.** The span opens the day after the
  last scheduled session (today, once the schedule has run out) and runs for
  `config.workout_generation_span_days` (default 28). A bare run therefore *adds* days
  rather than rewriting the ones already covered. When the schedule already reaches the
  plan's last day, the run is refused: that is the plan running out, and the fix is a new
  goal, not another span.
- **A span never opens before today.** If today's session is already completed, the
  span opens tomorrow and today's row is left alone.
- **Within the span, the proposal is the plan.** Every live session in the span that the
  proposal does not name is voided ("Not in the regenerated plan"); every proposed
  session is appended; a session the model marks `keep` is neither. Days outside the span
  are untouched. Sessions a prior `adapt` already eased are shown to the model so it does
  not hand back the load adapt took off.
- **Every date in the span gets a row**, so a hole means the schedule ended, not that a
  rest day was skipped — which is what lets the runway detector tell the two apart.
- **Each session is tagged with the plan version governing its date**, so a span that
  runs from one goal's last block into the next goal's first produces sessions from two
  macrocycles, and rollback accounting keys off that tag.
- **It warns when the plan runs out before the span does**, naming `plan generate` as the
  fix.

### There is one write model, and every command uses it

Under the revision log every change is the same act: append. A command that empties a
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
plan, and the coach learnings — one LLM call — and shows a `strategy` plus a list of
blocks:

```
- Base Building        (2026-08-22 → 2026-09-26)  Zone 2 aerobic base, high volume
- Specific Preparation (2026-09-27 → 2026-10-24)  Threshold work, marathon pace
- Build                (2026-10-25 → 2026-11-07)  Peak long runs, race simulation
- Peak & Taper         (2026-11-08 → 2026-11-15)  Volume down, intensity held
```

On `y`: one `macrocycles` row (`status = 'active'`), four `mesocycles` rows, block dates
repaired for contiguity if the model left a gap. **No workouts yet.**

**3. The sessions.**

```
./tm workout generate
```

Nothing is scheduled yet, so the span opens today and runs 28 days — that lands entirely
in Base Building. TrainMate resolves the governing blocks, reads the metrics window,
builds the block-progress context, and asks the model for a schedule. It prints the
proposed sessions and asks. On `y`, it opens one `generate` change, appends one revision
per session (each a first revision, `lineage_id = id`, tagged with the active
`macrocycle_id`), and the change handle's reconcile pass pushes them to Calendar.

The weekly shape those 28 rows fall into *is* the microcycle. It is visible in the rows and
described in the model's `reasoning` prose. It is stored as neither.

**4. A bad morning.**

```
./tm workout adapt
```

HRV is down, sleep was poor. Adapt reads the backward metrics window and may rewrite
sessions from today to **2026-09-26** — the end of Base Building — and no further. It
shows the changes and asks. On `y`, one `adapt` change appends a new revision for each
session it eases, carrying the session's lineage and a per-session `reason`; the batch
rationale is the change's `summary`. Reading those sessions back, `adaptation_count` is
1 and `adapted_at` is set — but only for the ones whose duration or TSS actually fell, so
a pure rewording does not raise the "already eased" bar for next time. If the coach
decides nothing needs to change, the change row is still written, flagged `held`.

**5. Three weeks later, the schedule runs out.**

```
./tm workout generate
```

The last scheduled session is 2026-09-18, so this run opens on 2026-09-19 and runs 28
days — through 2026-10-16, crossing from Base Building into Specific Preparation. The
sessions already on the calendar are left alone; each new one is tagged with the block
governing its date. (Had the athlete run this at the block boundary, every daily surface
would already have printed `workout generate -m ..<id>` — the same command, bounded to
the block.)

**6. A trip in October.**

```
./tm constraint add "Work trip, no bike" --start 2026-10-12 --end 2026-10-16
./tm workout generate -m 7
```

The constraint is dated inside Specific Preparation — too far off for adapt to reach, too
small to trip the replan heuristic. So `constraint add` says so, naming the block it lands
in and the run that would cover it; until then `status` and `constraint list` both mark it
*not yet in the plan*. The generate re-plans that block's remaining days, building around
the trip like any other stored directive: the sessions already there are voided ("Not in
the regenerated plan"), the new ones appended, and the constraint's `honored_at` is stamped
because its whole remaining window sat inside what was written.

**7. Second thoughts about the plan.**

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

**8. Regret.**

```
./tm plan rollback
```

Version 1 becomes active again. Every workout change made after version 1's newest write
is undone: for each slot those changes touched, the revision that was live before them is
copied forward (stamped `restored_from`), never earlier than today, and the Calendar
follows. Version 1's feedback notes are pending once more.

**9. The race is cancelled.**

```
./tm goal rm 3
```

The goal is called off. One `stand-down` change voids every upcoming session tagged with
any of its plan versions; their Calendar events stay, retitled `[Deleted]`. The plan, both
its versions and the notes are untouched. If the race is back on next month,
`goal edit 3 --status active` copies the stood-down sessions back — the ones still ahead —
and re-pushes them.

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
clears it, and so does any undo that restores sessions older than the honouring —
`workout rollback`, `plan rollback`, or reinstating a goal.

### 9.4 A threshold or profile field changes

`config_hash` changes, but thresholds are compared against `coach.threshold_replan_pct`
drift rather than exact equality — a one-watt FTP change is not a reason to re-plan.
Profile fields that cannot reshape a plan (`name`, equipment) are excluded from the hash
entirely.

### 9.5 The plan is regenerated

The old macrocycle is superseded and kept, with its blocks and its feedback. A new active
macrocycle is inserted. **Workouts are untouched** — they still carry the old version's
`macrocycle_id` — until `workout generate` runs.

### 9.6 A goal is called off and later reinstated

`goal rm ID` and `goal edit ID --status archived` are two names for this one action.
It stands down the upcoming sessions of *that goal's* plan versions (one `stand-down`
change, one void per session) and retitles their Calendar events. The plan, its versions
and its feedback survive intact. Past sessions stay. Reinstating copies the stood-down
sessions back, floored at today, and re-pushes them.

### 9.7 A goal is purged

Only `goal rm --purge` gets here; plain `goal rm` calls the goal off (§9.6). The cascade
takes every plan version, every block and every feedback note. It does **not**
take the workouts, because `workouts.macrocycle_id` is a plain integer with no foreign key.
Those sessions are left with no plan to explain them, which is why the purge counts them
first and says so:

```
Purging goal 'Autumn Marathon' (ID 3) also deletes:
  - 2 periodization plan version(s)
  - 8 mesocycle block(s)
  - 3 plan feedback note(s)
  and leaves 26 upcoming session(s) with no plan to explain them.
```

Calling the goal off is the reversible alternative, and the one to reach for unless the
goal was entered by mistake.

### 9.8 The goal's date passes

Nothing is written. `goal_state()` starts answering `completed`; `upcoming_objectives()`
stops returning the goal, so `plan generate` and `workout generate` move on to the next
one; `get_governing_macrocycle()` keeps returning its plan until a later goal has one, so
the progress timeline still labels the weeks just trained.

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
| 9 | A pass may stamp `honored_at` only for a constraint whose whole remaining window it wrote | `honoring.covered_ids`, decided at proposal time, stamped at apply |
| 10 | At most one live workout per (date, canonical sport) | By construction: the live row is the highest `id` in the slot (`live_workouts`) |
| 11 | `workouts` is append-only — no row is ever updated or deleted | Two SQL triggers, `RAISE(ABORT)` |
| 12 | A session's identity is its `lineage_id`, and it survives both edits and date moves | `WorkoutChange._lineage_for`; the adaptation tally walks it |
| 13 | A `propose` method never writes; an apply method always records the pass | `tests/test_service_invariants.py` (source-level) |
| 14 | Workout state is derived from orthogonal axes, never a stored enum | The change `kind`, the lineage tally, `workout_calendar_state` |
| 15 | Weeks are Monday-commencing everywhere | `progression.weekly_aggregates`, `workout_swap_validate` |
| 16 | Within one plan, blocks are contiguous — no gaps, no overlaps | `save_macrocycle` via `repair_block_contiguity` |
| 17 | Every date of a generated span carries a row | `_fill_coverage_gaps` in `workout_generate` |

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
- **Workouts pointing at a real macrocycle.** No foreign key, so no cascade, so a purged
  goal strands its sessions — reported at `goal rm --purge` time rather than prevented.
  Calling a goal off does not have this problem: it sweeps the sessions by plan version
  before archiving anything.
- **The gap between plans.** `get_active_mesocycle` snaps from "one day left" to "the whole
  next block" across a calendar gap rather than tapering. Within one plan such a gap no
  longer exists (invariant 16); one can still open between two goals' plans. Both features
  that read it gate on `0 <= days_left <= N`, so neither misfires. Recorded in
  `DESIGN_block_boundary.md §6` rather than fixed.
- **A generated span stopping at a block boundary.** A bare `workout generate` runs 28
  days from where the schedule stops, whatever block that lands in. A clamp to the block
  end existed briefly and was removed: it left two-day slivers when a block was slightly
  longer than the cap, and the block-scoped `-m ..<id>` selector already covers the case
  where stopping at the boundary is what the athlete wants.

---

## Further reading

| Topic | Document |
|---|---|
| Periodization vocabulary and defaults | `trainmate/science/periodization.md` |
| Full schema and module map | `ARCHITECTURE.md` §5, §2 |
| Plan vs workout terminology | `ARCHITECTURE.md` §11 |
| Plan versioning and rollback | `designs/DESIGN_plan_rollback.md` |
| The block firewall | `designs/DESIGN_block_boundary.md` |
| How `workout generate` picks its span | `designs/DESIGN_cli_selectors.md` §8, §9 |
| The schedule running out | `designs/DESIGN_runway_nudge.md` |
| Whether the plan reflects a constraint | `designs/DESIGN_constraint_honoring.md` |
| Constraints and the replan escalation | `designs/DESIGN_constraints.md` |
| Plan feedback log | `designs/DESIGN_plan_feedback.md` |
| Goal states, calling a goal off | `designs/DESIGN_backward_evaluation.md` §12, §14 |
| Why `workouts` is an append-only log | `designs/DESIGN_workout_revisions.md` |
| What a Calendar event shows of a session's history | `designs/DESIGN_calendar_lineage.md` |
