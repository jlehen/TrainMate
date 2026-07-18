# Design: Unified Directives (`constraint` command)

**Status:** Draft (rev 6) · **Date:** 2026-07-10 · **Supersedes:** the `lifeevent`
command · **Companion to:** `DESIGN_context_authoring.md`,
`DESIGN_backward_evaluation.md`

*Rev 6 (2026-07-10, after use): the authoring surface is collapsed to a single
deterministic flag. `binding` (`hard`/`soft`), `sport`, and `type` are removed;
a constraint is now **advisory prose the coach works around**, plus one boolean
**`rest`** — a full no-training window whose dates skip the LLM and are forced to
rest. This is the one edge rev 5 already carved out (`hard` + no sport); everything
else it modelled had leaked at the seams. The `binding` axis only ever *did*
anything in one of its four `hard`/`soft` × sport/no-sport quadrants (blanket
`hard`); `sport`'s only mechanical role was to **disable** that deterministic path
(`hard` + sport was advisory — the code delegated the substitution to the LLM
already, §6); and `type` was an opaque label **no code ever branched on** (§5,
Non-Goals). All three read as prohibitions in the help and pushed classification
back onto the author — the exact friction §1 set out to remove. So: which sport,
"only 45 min", injury nuance are now prose the LLM interprets; `--rest` is the only
structured knob. Schema drops `binding`/`sport`/`type`, adds `rest`; the migration
is pure idempotent DDL (`hard` + no sport → `rest = 1`, everything else advisory)
that runs in `_init_db`, with a one-off `constraints_hash` backfill script
(`scripts/migrate_constraints_drop_binding.py`) so existing plans aren't spuriously
invalidated (§7). The §7 hard-window floor becomes a rest-window floor
(`replan_rest_span_days`); message extraction (§8) drops the `sport`/`type` fields
and can only ever create advisory (`rest = 0`) rows. Sections below still read in
the old vocabulary where not corrected inline; this preamble governs on conflict.*

*Rev 5 (2026-07-02, after implementation): migration bindingness corrected to
**`soft`**, reversing the rev 4 decision. A life event was never code-enforced
before this refactor — it was rendered as advisory prompt context only, exactly
like a `soft` constraint today — so `soft` is the faithful carry-over; `hard`
would have made every migrated life event newly bypass the LLM and force
deterministic rest, a behavior change rather than a migration (§9, §11).*

*Rev 4 (2026-07-02, after implementation review): the remaining under-specified
edges closed so the doc reads execute-only. The §7 magnitude heuristic is now
concrete — relative displaced-load with a hard-window floor, both config knobs
(§7). `workout adapt --message` extraction is a **two-confirmation** flow: create
the constraint(s), then adapt (§8). The `generate` hard-rest pre-pass is pinned
as a **post-hoc override** of the model's returned workout list, not a prompt
instruction (§6). `constraint edit` does **not** prompt interactively — every
field is passed on the command line; only `add` prompts (§4). Migrated life
events become **`hard`**, not `soft` (§9), resolving that open question. The
migration **backfills `constraints_hash` on active macrocycles** so it does not
spuriously invalidate existing plans (§7, §9). `trainmate_web.py`'s life-event
REST surface is added to the repoint list (§6, §10). `--replan` and `--hard`
are confirmed independent — neither implies the other (§5, §7).*

*Rev 3 (2026-07-02, after review): `hard` + sport narrowed to advisory/
LLM-mediated rather than code-enforced (§5–§6); hard-blocked dates always get
an explicit `Rest` row in both `generate` and `adapt`, reusing `adapt`'s
existing displaced-session handling (§6); `get_constraints`'s window made
explicit per caller with a real overlap rule (§6); `constraints_hash`'s
cross-goal/cross-sport scope flagged as an accepted limitation (§7);
`--message` constraint extraction folded into the existing `adapt` LLM call
(no separate pass) and capped to `soft` only (§8); data migration called out
as a one-off operation, not part of `_init_db` (§9); `constraint list`'s
default window specified (§4); the hard-constraint/adherence open question
resolved — no new plumbing needed (§11).*

*Rev 2 (same day, after codebase review): `note` split into `title` /
`description` / opaque `type` (§5); analysis-path discounting feed made explicit
(§2, §6); staleness-hash policy specified (§7); migration made verbatim and
moved earlier in the rollout (§9–§10); `base.py` legacy-rename hazard flagged
(§9).*

A single first-class object — a **constraint** — for everything the athlete
*asks the coach to work around*, at any horizon: "no run Thursday", "only 45 min
Tuesday", "prefer easy this week", or "3-week injury layoff". It replaces the
`lifeevent` command and gives `workout adapt --message` a typed home to land in.
Observations (`context`, daily signals) are deliberately **not** merged in — see
§2.

---

## 1. Motivation

TrainMate accreted three ways for the world to touch the plan, and they feel
disparate because each is "special" for a *different, non-semantic* reason:

- **Life events** are special by **horizon** (they reshape the plan).
- **Daily context** is special by **storage** (it lives in Google Calendar — an
  artifact of the author's alcohol-in-a-spreadsheet history, `DESIGN_context_authoring.md`).
- **`workout adapt --message`** is special by **durability** (ephemeral by
  design, its effect smuggled into a session's `change_reason`).

Storage and durability are *implementation* accidents, not meanings. Worse,
`lifeevent` forces the athlete to **classify by consequence at authoring time**
("is a 3-day offsite a life event or not?") — but plan-invalidation is a function
of *the input against the current plan*, not a property of the input, so the
classification is a guess with an expensive, near-irreversible consequence
(a regen) riding on it.

This design keeps the one **principled** split (observation vs directive, §2),
gives directives a single typed object (§4–§5), and turns plan-invalidation from
an authoring-time category into a **derived, human-confirmed action** (§7).

---

## 2. Mental model — two axes; storage is not one of them

Every way the world touches the plan is a point in a 2-D space:

- **Observation vs Directive.** Did something *happen / is true about me* (the
  coach **interprets** it — it may even become cited evidence behind a coach
  learning), or am I *asking the coach to respect* something (it **bounds** what
  the coach may prescribe)?
- **Horizon.** Does it bind this *run*, these *few days*, or the *plan*?

Storage medium (SQLite vs Google Calendar) and entry method (CLI, calendar,
message) are an **orthogonal I/O concern**, never a property of meaning.

| | Observation (evidence) | Directive (bound) |
|---|---|---|
| **home** | `context` / `daily_context` (+ Garmin) | **`constraint`** (this doc) |
| **coach uses it to** | interpret readiness; feed learnings | bound `generate` + `adapt`; discount anomalies (§6) |
| **may become evidence?** | yes (cited weeks) | **no** — may only *discount* it (§6) |

**We keep the observation/directive split.** It is not cosmetic: observations
feed the evidence/confidence machinery, directives feed the solver, and a
directive must *never* become physiological evidence ("I couldn't train Thursday"
is not data that the block is too hard). The one sanctioned crossing runs the
*other* way: the weekly analysis may read a directive to **explain an anomaly
away** (a travel week is not a fitness-loss signal), never to *support* a
learning — this is what life events already do for the backward evaluation
today, and constraints inherit that feed unchanged (§6). Any object that blurred
the supporting direction would be wrong. So this doc unifies **only**
directives; `context` is untouched.

---

## 3. Goals / Non-Goals

**Goals**

- One directive object spanning hard availability, capacity/intensity caps, soft
  preferences, and big disruptions — at any horizon.
- Consumed uniformly by **both** `workout generate` and `workout adapt`, and fed
  to the weekly analysis as discounting context (§6).
- Plan-invalidation is *derived* (magnitude vs. plan) and *human-confirmed*,
  never an authoring-time classification.
- A typed destination for `workout adapt --message` auto-classification (§8).
- Fold in and deprecate `lifeevent` with a clean, lossless data migration (§9).

**Non-Goals**

- Merging observations (`context`) into directives (§2).
- Decoupling `daily_context` from Google Calendar storage — a separate refactor.
- Per-type intelligence. `type` is an **opaque, user-vocabulary label** (like
  `daily_context.metric` — `DESIGN_context_authoring.md` §1); no code ever
  branches on a specific value. The free-text fields carry specifics; TrainMate
  stays domain-agnostic (§5).
- Auto-triggering a regen without a human `y` (§7).

---

## 4. Command surface

New top-level **`constraint`** (alias `cons`). The standard CRUD six — no
seventh verb; the "replan" action is a *flag*, not a verb (§7). Per the
prompt-over-flags preference (`DESIGN_context_authoring.md` §4), **`add`**
prompts interactively for any field omitted on the command line. **`edit` does
not prompt** — every field it changes is passed explicitly on the command line
(prompting the full field set on an edit would be wrong, and there is no
`context edit` precedent to mirror); an `edit` with no field flags is a no-op
that prints guidance.

| Subcommand | Alias | Purpose |
|---|---|---|
| `add`  | `a` | Author a directive over a day or range |
| `list` | `l` | List active/upcoming directives (date/sport/type filtered) |
| `show` | `s` | One in detail — incl. whether it is plan-shaping (§7) |
| `edit` | `e` | Adjust scope / bindingness / text / replan |
| `rm`   | `r` | Remove by id |
| `wipe` |     | Remove all (guarded; `-y`/`--yes` for the bot) |

```
constraint add [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--sport SPORT]
               [--hard | --soft] [--type TYPE] [--desc TEXT]
               [--replan | --no-replan] [TITLE]
```

- `TITLE` (positional) is **the directive itself, stated short** — "no run
  Thursday", "only 45 min today". Mandatory (prompted if omitted on `add`); it
  doubles as the `list` display string. Quick capture stays flag-free:
  `cons a "no run Thursday"`.
- `--desc` is **optional richer context** for the coach, only when the one-liner
  isn't enough ("hotel gym only, no pool, long layover on the 14th"). Most
  constraints never set it. (Column name: `description`, §5.)
- `--type` is an **optional opaque label** ("trip", "injury", …). The `add`
  interactive prompt shows types already in use (same distinct-values query
  pattern that powers `context list-metrics`) to discourage "trip" vs "travel"
  drift. Never required — mandatory classification on the fast path is the
  friction §1 diagnoses.
- `--start` defaults to today; `--end` defaults to `--start` (single day).
- `--hard` / `--soft` bindingness (§5); default `--soft` (advisory — the
  conservative default, since `hard` can deterministically null out training).
- `--sport` scopes the directive to one sport; omitted = all sports.
- `--replan` / `--no-replan` pre-answer the plan-shaping proposal (§7); omitted =
  let the magnitude heuristic decide whether to *ask*. Independent of
  `--hard`/`--soft` (§5, §7).
- `constraint list` defaults to directives **from the start of the current
  mesocycle** (`get_active_mesocycle(today)['start_date']` — the training block
  being planned) plus everything upcoming (open-ended into the future). This
  anchors the list on the block the coach is actively reasoning over rather than
  a rolling calendar window. `--all`/`-a` drops the lower bound (past directives
  included); `--from`/`--until` override the bounds explicitly. When no active
  mesocycle exists to anchor on (no plan yet), the lower bound is dropped and
  **every** constraint is shown — a fresh user has only a handful, and there is
  no block to scope to.

### Disambiguation: `context` alias `c` → `ctx`

To keep `cons` (constraint) and context from colliding in the head and at the
prompt, the `context` command's canonical short alias becomes **`ctx`**; the
single-letter `c` is retired. No data or schema change — parser config only.

---

## 5. Data model

```sql
CREATE TABLE IF NOT EXISTS constraints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,          -- == start_date for a single day
    binding     TEXT NOT NULL,          -- 'hard' | 'soft'
    sport       TEXT,                   -- NULL = all sports
    type        TEXT,                   -- opaque user-vocabulary label ('trip', 'injury', …)
    title       TEXT NOT NULL,          -- the directive, stated short; list display string
    description TEXT,                   -- optional richer context, read by the LLM
    replan      INTEGER NOT NULL DEFAULT 0, -- 1 <=> plan-shaping (built into the plan, §7)
    source      TEXT,                   -- 'manual' | 'message' | 'lifeevent' (migration)
    created     TEXT
);
CREATE INDEX IF NOT EXISTS idx_constraints_start ON constraints(start_date);
```

(`description`, not `desc` — `DESC` is an SQL keyword, and `description` matches
the `objectives`/`workouts` columns.)

A field earns a column only when deterministic code reads it: the solver
branches on `start_date`/`end_date` (window queries), `binding` together with
`sport` (to tell a blanket hard-rest date from a single-sport hard
restriction — see below) and `replan` (snapshot/hash, §7); display renders
`title` and `type`. Everything else is prose in `title`/`description`,
interpreted by the coach. `type` is deliberately **not** an enum — the old
`event_type` vocabulary (`business_trip`/`vacation`/`party`/`other`) was never
branched on anywhere in the code and fits none of the short directives this
object now spans; it is an opaque label exactly like `daily_context.metric`,
with vocabulary owned by the user (mirrors `DESIGN_context_authoring.md` §1).
Bindingness has just enough structure to let exactly one edge skip the LLM —
the rest stays advisory, deliberately: picking a sensible substitute activity
needs judgement the code doesn't have, and the LLM has been reliable at
respecting stated constraints, so there is no reason to take that judgement
away from it:

| `binding` | `sport` | Meaning | Enforcement |
|---|---|---|---|
| `hard` | NULL | No training at all on those dates | **Deterministic** rest — `generate`/`adapt` place an explicit `Rest` entry directly, bypassing the LLM for that date entirely |
| `hard` | set  | That sport is unavailable | **Advisory** — rendered into the prompt as a hard instruction; the LLM is trusted to honor it and choose any substitute itself |
| `soft` | any  | Preference / capacity hint | **Advisory** — passed to the LLM to honor via judgement |

`replan` records whether a directive is currently escalated to plan-shaping; it
is set by the §7 flow, surfaced in `constraint show`, and read by `plan generate`
(§7). It is **not** an authoring-time category the user picks blind.

**`binding` and `replan` are independent axes.** `binding` answers *how the
directive is enforced when a plan is built* (code-enforced rest vs advisory
prose); `replan` answers *whether we rebuild the macrocycle around it now*.
Neither implies the other, and no flag couples them: "prefer easy this whole
build block" is `soft` **and** `replan = 1` (reshape the plan, no rest rows);
"no run Thursday" is `soft` and `replan = 0`; "broke my ankle, out 6 weeks" is
`hard` **and** `replan = 1`. `--replan` therefore never sets `hard`, and
`--hard` never sets `replan` (§7).

---

## 6. Consumption — one read path for `generate` and `adapt`, a discounting feed for analysis

All three consumers fetch the **active** directives overlapping their own
window through one shared function, `get_constraints(start, end)`, replacing
the old `get_lifeevents(start_after=…)` call (and, for analysis, the manual
tail-end filter it currently needs bolted on top). "Overlapping" means a real
two-sided overlap test — a constraint is returned iff
`start_date <= end AND end_date >= start` — not the old one-sided "hasn't
ended yet" check `get_lifeevents` does today. Each caller passes its own
window:

- `plan generate` → `get_constraints(today, end=None)` — open-ended, since
  planning is inherently thinking about the whole future anyway.
- `adapt` → `get_constraints(target_date, meso_end_date)` — bounded to the
  actual adaptation range. This tightens today's behavior: `get_lifeevents`
  has no upper bound at all, so `adapt` currently receives life events
  arbitrarily far in the future, well past anything it can act on.
- weekly analysis (`data reflect` / `data bootstrap`) →
  `get_constraints(window_start, window_end)` — bounded to that run's lookback
  window. The real two-sided overlap test means the manual
  `c['start_date'] <= until_str` filter the analysis path currently has to add
  on top of `get_lifeevents` becomes unnecessary and can be deleted.

For each directive in the fetched set:

- **`hard` + no sport** over a date → that date is forced to an explicit
  **`Rest`** entry, deterministically, without asking the LLM. This is a
  **post-hoc override of the model's output, not a prompt instruction** — the
  model still returns whatever it likes for that date, and the app rewrites it:
  - `generate` calls the LLM for the whole plan and then saves the returned
    list (`coach/service.py workout_generate`, the `workouts = plan_data.get(
    "workouts", [])` → `save_workout` loop). The pre-pass runs **between** those
    two steps: drop any session the model placed on a hard-rest date and splice
    in a forced `{sport_type: 'rest', duration_minutes: 0, rpe: 0, tss: 0}` row
    for that date. `generate` never leaves the date empty — an empty date and an
    explicit rest day are not the same thing elsewhere in the app (e.g.
    adherence treats a missing row differently from a planned rest day).
  - `adapt` eases whatever is already planned on that date — possibly more than
    one session, if multiple sports were scheduled that day — to the same
    explicit rest entry, with a `change_reason` naming the constraint (the
    change_reason footprint is now grounded in a durable row, not a smuggled
    ephemeral note). This reuses `adapt`'s existing displaced-session handling:
    `workout_adapt_apply` already removes every session on a date that isn't
    covered by a given day's proposal, so handing it a single synthesized `rest`
    proposal for that date correctly clears all of that day's sessions, not just
    the first one.
- **`hard` + sport** → rendered into the prompt as a hard instruction that
  this sport is unavailable for those dates. This is advisory, not
  code-enforced (§5): the LLM decides what, if anything, to substitute. Other
  sports flow normally.
- **`soft`** → rendered into the prompt as a preference the coach should honor
  but may trade off against readiness, exactly as life-event
  `impact_description` is fed today.

The rendered block replaces the current life-events section of both prompts,
field-for-field: `title | dates | binding | sport | type | description` mirrors
today's `title | dates | type | impact`. No new prompt *structure* is required,
only a relabel and the deterministic hard-rest pre-pass.

**Analysis (third consumer — discounting only).** The weekly backward
evaluation currently reads life events so the model doesn't misattribute an
anomalous week to training (illness/travel/work explain a load or recovery
anomaly; `coach/service.py` window fetch + per-week tagging, and the
life-events digest inside the analysis-window hash). Constraints **inherit this
feed unchanged**: overlapping constraints are passed into the analysis input as
context that may *explain an anomaly away*, with the prompt continuing to
forbid citing them as supporting evidence for a learning (§2). The
analysis-window hash swaps its life-events digest for a constraints digest.

**Other consumers repointed in the same change:** the `status` overview's
life-events section (`cli/status.py`), the **web REST surface**
(`trainmate_web.py` — `manage_life_events` / `single_life_event`, the
list/add/update/delete endpoints around lines 145–182: repoint them at
`get_constraints`/constraint CRUD; the exact shape is left to
implementation-time judgement), `wipe` plumbing (`db/wipes.py`), and the
`LifeEvent` TypedDict (`types.py`) get `Constraint` successors. The
`trainmate_bot.py` touch is only a one-line command label
(`("lifeevent", "Manage life events")`) and is handled by the §9 forwarder — no
special work.

---

## 7. Plan-invalidation as a derived, confirmed action

Plan-invalidation is **not** a property of a constraint the user declares. Flow:

1. On `constraint add`/`edit`, compute the directive's **magnitude against the
   active plan** (the heuristic below).
2. If magnitude crosses the threshold and neither `--replan` nor `--no-replan`
   was given, **propose**: *"This overlaps your build block and displaces a big
   chunk of planned load — replan around it? [y/N]"*. On `y`, set `replan = 1`
   and run the existing `plan generate` → `workout generate` confirm flow. On
   `n`, `replan = 0`; the directive is still honored by daily `adapt` (§6), just
   not built into the plan.
3. `--replan` / `--no-replan` **pre-answer** step 2 (the front door a big,
   obviously-plan-shaping disruption uses). `--replan` sets `replan = 1` **and**
   enters the same confirm flow as a `y` — each step of which still confirms
   before applying anything. `--replan` is what the deprecated `lifeevent add`
   becomes (§9); note this means the forwarder gains a regen *proposal* at add
   time where the old command only printed "run `plan generate`" advice —
   intended: it closes the "now remember to regenerate" gap without removing
   the human confirmation. `--replan` does **not** set `binding = 'hard'` (§5).

Nothing sets `replan = 1` or regenerates a plan without a human `y`. This is the
same confirm-before-regen posture `plan generate` / `workout generate` already
take — just constraint-triggered instead of pre-classified.

**Magnitude heuristic (concrete).** Two independent triggers; **either** one
firing proposes a replan. Both thresholds are config knobs (under `coach:`,
alongside `metrics_lookback_days`), so the policy is tunable without a code
change:

1. **Displaced-load trigger (relative).** Sum `adherence._planned_load(w)` over
   the active-plan sessions overlapping the constraint's window (the same
   per-session TSS/sRPE the adherence path already computes). Divide by the
   plan's **trailing weekly planned load** (the natural per-week rollup) to get
   a self-scaling ratio, and propose when it meets or exceeds
   `config.replan_displaced_load_pct` (default **50** — i.e. the constraint
   wipes out ≥ half a typical week of planned training). Relative, not
   absolute, so no magic TSS number rots as the athlete's fitness changes.
2. **Hard-window floor.** Independently, propose whenever the constraint is
   `hard` **and** spans at least `config.replan_hard_span_days` days (default
   **3**) — a multi-day hard window is intrinsically plan-shaping regardless of
   how much load it happens to overlap (it may land in a taper week that carries
   little load yet still reshapes everything after it).

There is deliberately **no** per-session "importance" term: TrainMate has no
per-workout importance/priority field (priority lives on *objectives*, not
sessions), so a heuristic that leaned on "key sessions" would be inventing data
the schema doesn't carry. Displaced load is the honest proxy. Start with the
defaults above (conservative — only multi-day hard windows or half-week-plus
load displacements trip it) and tune the two knobs from there.

**Snapshotting & staleness.** `plan generate` records the active `replan = 1`
constraints onto the macrocycle as a new `constraints_snapshot` (the successor
to `lifeevents_snapshot`), so "inputs this plan was built on" stays inspectable
(`plans._print_considered_inputs`). The staleness fingerprint moves with it: the
macrocycle's `lifeevents_hash` (compared on every `plan generate` to decide
reuse vs regen — `coach/service.py` reuse check) becomes `constraints_hash`,
computed over the **`replan = 1` constraints only**. Tactical directives ("no
run Thursday") must *not* flag the plan stale — hashing every constraint would
make each quick capture trip the "inputs changed" regen proposal and fight the
magnitude flow above; plan-level staleness is exactly what `replan` escalation
is for. Existing `lifeevents_snapshot` values are left untouched as legacy; the
display code already tolerates plans that predate a snapshot key.

**Migration must not spuriously invalidate existing plans.** The reuse-vs-regen
decision (`coach/service.py`) matches three fingerprints — `goals_hash`,
`lifeevents_hash`→`constraints_hash`, `config_hash`. This change only disturbs
the middle one (renamed column, new field shape, `replan = 1` filter), so goals
and config still match; the only invalidation risk is the *stored* hash no
longer equaling the freshly-computed one. The one-off migration therefore
**backfills** `constraints_hash` on every active macrocycle (§9): after copying
the rows, it recomputes the hash exactly the way the next `plan generate` will —
`get_constraints(today, end=None)`, filtered to `replan = 1`, cleaned, hashed —
and overwrites the column. Because the hash isn't scoped by objective (the known
limitation below), every active macrocycle gets the **same** value, and the next
`plan generate` recomputes that identical value → all three hashes match →
`reused = True`, no regen proposed. (This neutralizes the migration as a cause
of invalidation; it does not change the pre-existing ambient behavior that the
`start_after=today` window makes the hash drift naturally as events age out.)

**Known limitation — not scoped by goal or sport.** `constraints_hash` (like
`lifeevents_hash` before it) is computed from the *whole* `replan = 1` set,
regardless of which objective or sport a constraint's `sport` field names.
With two concurrent objectives in different sports, a plan-shaping constraint
naming only one sport still flags *every* active macrocycle stale, not just
the one it actually affects. Properly scoping this would need to match a
constraint's `sport` against an objective's `sport_type` — which can itself be
a comma-separated list of sports, not a single value — so it isn't a small
fix, and it's inherited unchanged from today's `lifeevents_hash` behavior.
Accepted as-is: a `replan = 1` constraint (an injury layoff, a multi-week
trip) is usually significant enough that re-confirming across every active
goal is a reasonable, if occasionally redundant, default.

---

## 8. `workout adapt --message` — quick capture, typed landing

`--message` stays the ergonomic one-liner, but stops being its own semantic
channel. It becomes a **fast-capture inbox** classified by the *same* `adapt`
LLM call that already evaluates the day — no separate classification pass, no
extra LLM round-trip or cost. The response schema (the same JSON object that
carries `adapted_workouts`, in `engine.py`'s `_workout_adapt_logic`) gains a
sibling field with its own formal shape, mirroring what `constraint add`
itself accepts:

```
"new_constraints": [
  // Optional. Directives extracted from the athlete's message this run. Every
  // entry is created exactly as if the athlete had run `constraint add`.
  {
    "title": "no run Thursday",   // required
    "start_date": "YYYY-MM-DD",   // required
    "end_date": "YYYY-MM-DD",     // required
    "sport": "running",           // optional — omit/null for "all sports"
    "type": "injury",             // optional — omit/null
    "description": "..."          // optional — omit/null
  }
]
```

- **Constraint-shaped** ("can't train Thursday", "only 45 min today") → the app
  **creates a `constraint` row** from each entry (`source = 'message'`,
  **always `binding = 'soft'`** — see trust boundary below), reversible and
  inspectable. Only `title` and dates are required of the classifier; every
  other field may be left NULL. It is honored this run through the existing
  athlete-message advisory text (unchanged), *and* persists for every future
  run via the durable row — so the `change_reason` smuggling hack is no longer
  needed for these.
- **Ephemeral nudge** ("felt flat, just ease today") → unchanged legacy behavior:
  a one-run hint folded into `change_reason`, no row created.

**Two-confirmation flow.** The `adapt` LLM call returns `new_constraints`
*alongside* `adapted_workouts` in the same response, but the two land through
**separate confirmations**, in order:

1. **Confirm the constraint(s) first.** If `new_constraints` is non-empty, echo
   each one ("Add constraint: *no training Thu* (2026-07-09)?") and ask
   `[y/N]`. On `y`, the row(s) are created (`source = 'message'`, `soft`). On
   `n`, the extraction is **discarded** and the run degrades to a plain
   ephemeral nudge — the message still influenced *this* run's adaptation via
   the advisory text, but nothing durable is written.
2. **Then confirm the adaptation.** The existing adapt preview + `[y/N]` apply
   prompt (`cli/workouts.py`) runs as it does today. Declining the constraint in
   step 1 does not block step 2, and vice versa — they are independent
   commits.

This makes §11's "echo what was created" fall out for free (the confirmation
*is* the echo) and means a misfiled extraction is caught before any row exists,
not after. `new_constraints` requires a new return channel from `workout_adapt`
(which today returns only `(reason, proposed_workouts)`) up to the CLI; thread
the extracted list out beside those and drive step 1 from it.

**Trust boundary (agreed).** Auto-classification may **create reversible
state** (a constraint you can `rm`) — but only ever `soft`. The LLM can never
mark an extracted constraint `hard`: `hard` is code-enforced (§5) and takes
effect the next time `generate`/`adapt` runs with **no further human
confirmation** in the loop, so it must be a deliberate human action
(`constraint edit <id> --hard`), never an auto-classification outcome.
Separately, auto-classification may **never** set `replan = 1` or trigger a
regen. If an extracted constraint is large (e.g. "broke my ankle, out 6
weeks"), it is still only *created* as a durable, soft constraint
(`replan = 0`); the §7 magnitude check then **surfaces a suggestion** — "this
looks plan-shaping; run `constraint edit <id> --replan` or `plan generate`" —
which the human acts on. The model classifies; the human authorizes both the
`hard` escalation and the expensive `replan` step. The "auto-classify short
constraints but not life events" rule thus *falls out* of the design rather
than needing separate enforcement.

---

## 9. Folding in & deprecating `lifeevent`

**Command.** `lifeevent` / `le` / `e` are retained for one release as thin
**forwarders** to `constraint … --replan` (a life event was, by definition,
plan-shaping), emitting a deprecation notice, then removed. (Behavior change vs
the old command: the forwarder can now propose a regen at add time, §7 step 3.)

**Data migration — a one-off operation, not part of `_init_db`.** Every other
migration in `base.py` is idempotent *by construction*: `CREATE TABLE IF NOT
EXISTS`, `ADD COLUMN` guarded by `except OperationalError`, and renames
guarded by column/table presence all self-terminate, because their guard
condition stops being true after the first run. A *row copy* between two
tables that both keep existing afterward (`lifeevents` stays around, read-only,
until the forwarder is removed — see below) has no such natural guard.
`_init_db()` runs on every single CLI invocation (`Database()` is constructed
fresh per process, at import time), so this migration must **not** live inside
`_init_db` — it must be an explicit, one-off script (or a command gated behind
its own flag/confirmation) that the operator runs once, by hand. Each
`lifeevents` row → one `constraints` row, **verbatim — no info loss**:

| `lifeevents` | → `constraints` |
|---|---|
| `start_date`, `end_date` | copied verbatim |
| `title` | `title`, verbatim |
| `event_type` | `type`, verbatim (opaque label now; no enum) |
| `impact_description` | `description`, verbatim |
| — | `binding = 'soft'` (life events were always advisory; see below) |
| — | `replan = 1` (life events were always plan-shaping) |
| — | `source = 'lifeevent'` |

**Migration bindingness → `soft` (corrected).** Migrated life events become
`soft`, not `hard`. A life event was never code-enforced in the old model — it
was rendered into the prompt as advisory context (`UPCOMING LIFE EVENTS`) for
the LLM to weigh, exactly like a `soft` constraint is today; nothing
deterministically forced rest on its account. Migrating it as `hard` would
therefore *strengthen* it past what it ever did, making every migrated life
event suddenly bypass the LLM and force an explicit `Rest` entry — a behavior
change, not a faithful carry-over. `soft` preserves continuity: the directive
still reaches `generate`/`adapt`/analysis exactly as before, honored by
judgement. Since `event_type` survives verbatim in `type`, any later
refinement (e.g. escalate a specific `type` to `hard`) can still be run after
the fact without re-migrating.

**Backfill `constraints_hash` on active macrocycles (must run inside the same
one-off script, after the row copy).** Per §7, recompute each active
macrocycle's `constraints_hash` the way the next `plan generate` will —
`get_constraints(today, end=None)` → filter `replan = 1` → clean → hash — and
overwrite the (renamed) column, so migrating does not make every existing plan
propose a regen. All active macrocycles receive the same value (the hash is
global, §7); superseded macrocycles are never compared and are left untouched.

**Legacy-name hazard (must land with step 1).** The name `constraints` is
already claimed by dormant migration code: the table spent its early life under
that name, and `base.py` still renames an existing `constraints` table to
`lifeevents` whenever `lifeevents` is absent (base.py ~lines 57–70), and a
`constraints_hash` column to `lifeevents_hash` (base.py ~lines 413–419). Left in
place, those renames would **hijack the new schema** on any database where
`lifeevents` doesn't exist — every fresh DB, and every DB after the final drop
(step 8). They must be deleted in the same change that creates the new table.
This drops auto-upgrade support for pre-rename databases, which is acceptable at
this project's age.

**Snapshots.** As §7: stop writing `lifeevents_snapshot`; write
`constraints_snapshot` going forward; leave old snapshots as legacy. The
`lifeevents_hash` column is renamed to `constraints_hash` and backfilled per §7.

---

## 10. Rollout

1. Schema: create `constraints`; **delete the `base.py` legacy renames** (§9
   hazard); keep `lifeevents` intact.
2. `constraint` command (CRUD, §4) + `get_constraints` reads. `add` prompts
   interactively; `edit` is command-line-only (§4).
3. **Migrate `lifeevents` rows (§9)** — as `soft` — **and backfill
   `constraints_hash` on active macrocycles (§7/§9)**, and, in the same release,
   repoint every consumer: `generate`/`adapt` (§6, incl. the deterministic
   hard-rest post-hoc override), the weekly-analysis discounting feed, `status`,
   the **web REST surface** (`trainmate_web.py`), wipes, and the
   `constraints_hash`/`constraints_snapshot` successors (§7). The migration must
   land *with* the repoint, not after it — otherwise existing life events are
   silently ignored in between.
4. §7 magnitude proposal + `--replan` flag + the two config knobs
   (`replan_displaced_load_pct`, `replan_hard_span_days`).
5. `--message` classification with the two-confirmation flow (§8).
6. `lifeevent` → forwarder + deprecation notice.
7. `context` alias `c` → `ctx` (§4).
8. Later release: remove the `lifeevent` forwarder; drop the `lifeevents` table.

Steps 1–3 are shippable on their own (new object usable, existing life events
migrated and honored, no existing plan spuriously invalidated); 4–6 complete the
vision; 8 is cleanup.

---

## 11. Open questions

- **Magnitude thresholds (§7) — resolved as config, defaults to tune.** The
  mechanism is settled (relative displaced-load ≥ `replan_displaced_load_pct`,
  OR `hard` window ≥ `replan_hard_span_days`). The only remaining work is
  tuning the two defaults (50% / 3 days) against real use; both are config
  knobs, so tuning needs no code change.
- **Migration bindingness — resolved.** Migrate life events as `soft` (§9) —
  the faithful carry-over, since a life event was never code-enforced before
  this refactor either; `event_type` survives in `type`, so a later per-`type`
  escalation to `hard` needs no re-migration.
- **Hard-constraint adherence — resolved, no new plumbing needed.** A `hard`
  date already gets an explicit `Rest` entry (§6), and a planned rest day
  never counts as a miss today — `analyze_adherence` already treats it as
  `rest_ok` unless the athlete does something significant on it, which is a
  separate, already-handled case. So once the rest entry exists, there's
  nothing left for the adherence path to special-case. The one caveat: this
  only protects a date once `adapt` has actually run on it and converted
  whatever was scheduled to rest — `constraint add`/`edit` do nothing
  synchronously. That's expected, not a gap: `adapt` is meant to run daily,
  and like everything else in TrainMate, a constraint has no effect until the
  command that consumes it runs.
- **`--message` classifier reliability.** Extraction is easy; the risk is a
  durable-looking sentence mis-filed as ephemeral (or vice versa). Mitigation:
  the two-confirmation flow (§8) echoes each extracted constraint and asks
  before creating it, so a misfile is visible and never becomes a row in the
  first place.
