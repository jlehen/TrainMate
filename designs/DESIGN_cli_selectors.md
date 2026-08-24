# CLI selectors: one range grammar for every filter

## The problem

Nearly every command that reads history or plans forward needs the same answer: *which
slice?* Each one had grown its own way of asking.

`workout list` alone took eight flags for it — `-d/--days`, `-w/--weeks`, `--from`,
`--until`, `--from-mesocycle`, `--until-mesocycle [ID]`, `--mesocycle [ID]`,
`-g/--goal [ID]` — and they were not composable: the resolver was a precedence chain, so
`--mesocycle 3 --from 2026-06-10` silently dropped the `--from`. Four resolvers
(`_resolve_workout_date_range`, `_resolve_workout_end_date`, `_resolve_historical_date_range`,
`resolve_cleanup_range`) each read those flags with slightly different rules, and disagreed:
`--goal ID` ended at the goal's **target date** in one and at the **last mesocycle's end**
in another. Each command then re-implemented its own "no filter means…" default in its
handler, in prose in its `description=`, and nowhere else.

The same `-d` meant "N days" on eight commands and nothing on the rest; `-m` meant
`--message` under `workout adapt` and `--metric` under `signal`; `--date` was a single day
on `workout adapt`, a stored field on `goal edit`, and absent everywhere else.

## §1 — The grammar

One spelling, used by four dimensions:

```
SELECTOR := ATOM | ATOM ".." | ".." ATOM | ATOM ".." ATOM
```

| Flag | ATOM | Bare flag |
| --- | --- | --- |
| `-d`/`--date` | `2026-06-01`, `today`, a signed offset `-7d` / `+2w` | — |
| `-m`/`--mesocycle` | a mesocycle ID | the current block |
| `-M`/`--macrocycle` | a macrocycle ID (`plan versions` lists them) | the active plan |
| `-g`/`--goal` | a goal ID | the active goal |

```
workout list -d 2026-06-01..2026-06-30    workout list -m 3..5
workout list -d 2026-06-01..              workout list -m 3..
workout list -d ..2026-06-30              workout list -m ..5
workout list -d -7d..+2w                  workout list -m
```

`..` rather than `-` as the separator, for two reasons. A date is full of dashes, so
`2026-06-01-2026-06-07` has to be *parsed* to be read; and `-2026-06-01` is unparseable by
argparse, which sees a leading dash and reports a missing value rather than reading it as
one (§6 is the narrow exception that survives). `..` also makes signed offsets composable:
`-1w..+1w` is a fortnight around today, which `-` cannot spell at all.

The `-m` atom has since grown past "a mesocycle ID": `plan feedback -m` also takes a
date (the block covering that day) and a case-insensitive infix of a block *name*
(DESIGN_plan_feedback.md §5, `resolve_meso_atom` in trainmate/cli/selectors.py). It is
defined there as a **single-target** resolver, beside this range machinery rather than
inside it — the day a filtering command wants `workout list -m climb`, the range grammar
lifts it rather than reinventing it.

**A bare number is never a date.** `7` is a row ID (§4), so a span must carry its unit:
`-d 7d`, `-d 2w`. That is also what lets `10d` be a *span* while `-10d` is an *endpoint* —
an unsigned offset with a `..` beside it (`7d..`) is refused by name, because it cannot say
which way it runs.

## §2 — Dimensions intersect, and each resolves to a window

`resolve_window` (trainmate/cli/selectors.py) turns every selector given into a
(start, end) pair and **intersects** them: `-m 3 -d 2026-06-10..` is the part of block 3
from the 10th onward. Nothing is silently dropped, which the old precedence chain did.

Each ID dimension has one definition of its span, shared by every command:

* **mesocycle** — the block's own start and end;
* **macrocycle** — the first start and last end of its mesocycles;
* **goal** — the start of its plan through the **goal's target date**, which is past the
  last mesocycle whenever the plan does not yet reach the event. The two old resolvers
  disagreed here; this is the reading that answers "show me this goal's span".

An empty intersection is reported (`the filters do not overlap: X is after Y`), not
returned as a silently empty listing.

## §3 — The default and the direction are declared, not coded

`add_selector_args(parser, …, direction=…, default=…)` registers the flags *and* records
the command's policy; `resolve_window` reads it back. So "a bare `workout compare` looks
back 14 days" is one string on the parser rather than an `if start_date is None` branch in
the handler, a sentence in the `description=`, and a third rule for what `--days` means
there.

| direction | Missing start | Missing end | Bare span `7d` | Commands |
| --- | --- | --- | --- | --- |
| `forward` | today | stays open | today → +6d | `workout list`, `workout push`, `constraint list` |
| `backward` | `default_span` before the end | today | −6d → today | `workout compare`, `data show-*`, `data pull`, `signal list` |
| `none` | stays open | stays open | −6d → today | `data wipe`, `workout prune-calendar`, `data backfill-tss`, `data bootstrap`/`reflect` |

`default` (a selector string like `7d`, `14d`, `today..`) applies **only when no dimension
is given at all**. Once the athlete names one side, the other is the direction's business —
it is never quietly widened back to the default.

`none` is for the commands where an unbounded side is the whole point: a cleanup sweeps
everything it is not told to spare, and `data bootstrap` auto-detects the window it was
never given.

## §4 — Positional targets: `workout list 12 15`

`workout list` takes any number of targets, each an ID or a date selector — `wo li 12 15 -v`
is how you read two sessions in full without inventing a window that happens to contain
them. `signal rm` takes the same shape, with a metric name in place of a date.

A bare integer is an ID, anything else is a date selector (`parse_target`); the same
classification `workout swap` has always made between its two targets. IDs are looked up
directly rather than folded into the window — an ID the athlete typed is not a range, and
demanding it fall inside the default 7 days would defeat the point. Date targets narrow
like any other selector, and named IDs bypass the window but still respect `--type` and
`--removed`.

## §5 — The reserved vocabulary

**`-d`, `-m`, `-M`, `-g` and `-t` mean the same thing at every level of the tree.** That is
the whole value of the change, so it is pinned by a tree-walking invariant
(`TestSelectorVocabularyInvariants`, tests/test_cli_selectors.py) rather than by discipline:
a violation would fail silently, with the wrong flag simply resolving.

The renames that rule forced:

* `learnings list --sport` → `-t/--type` (also `--sport`, `--sport-type`);
* `signal list`/`rm`: `--metric` loses `-m`, and the metric becomes the positional —
  which is what §a2 of DESIGN_cli_noargs.md says a command's subject should be anyway;
* `goal edit --date` → `--target-date`.

Two deliberate exceptions, each because the command cannot mean the reserved thing:

* **`workout adapt -m`** stays `--message`, and **`signal add`** takes no `-m`/`-M`: both
  act on days, not on blocks, so a mesocycle is not a slice they could take. `-d` there is
  a single date (`parse_single_date`), as it is on `benchmark record`. Work that *does* need
  a block selector takes a verb that already reads the reserved vocabulary — `workout
  generate -m 7` — rather than retiring this exception.
* **`goal edit --target-date`, `constraint add/edit --start/--end`** name a *stored field*,
  not a filter. The line: a command that acts over a span of days takes `-d` (`signal add`
  writes one row per day); a command that writes one row whose own columns are a start and
  an end keeps those columns as named flags.

There used to be a third: `workout generate --until-goal [ID]`, kept because `-g` there
meant the *target goal* rather than a window. §8 retired both the exception and the flag —
`-g` on `generate` now means what it means everywhere else.

`progress -w/--weeks N` is untouched: it means "N weeks either side of today", a symmetric
zoom rather than a range, and `--weeks` no longer exists anywhere else to collide with.

## §6 — A leading dash, and the one place argparse needs help

`-d -7d..+2w` cannot reach argparse as two tokens: `-7d..+2w` starts with a dash, does not
match argparse's negative-number pattern, and is therefore read as an unknown option.
`translate_dashless_argv` (trainmate/cli/argparse_ext.py) — which already rewrites the
dashless bot syntax against the same tree — glues the pair into `-d=-7d..+2w`, the one form
argparse takes verbatim. The rewrite is deliberately narrow: it fires only for a value
matching `-N[dw]` optionally followed by `..`, so a mistyped flag after another flag is
still the error it looks like. Both syntaxes go through it, so `workout list date -7d..`
works in chat too.

## §7 — What this replaced

Four resolvers became one (`resolve_window`); `_resolve_workout_date_range`,
`_resolve_historical_date_range`, `resolve_cleanup_range` and (with §8)
`_resolve_workout_end_date` are all gone. The `basic_date_parser` / `plan_date_parser` /
`sport_type_parser` parent
parsers in trainmate_cli.py are gone too: a parent parser cannot carry a per-command
default, which is exactly what each command needed.

`coach_service.data_bootstrap`/`data_reflect` lost their `days=`/`weeks=` parameters. A
relative span is a CLI spelling, and resolving it twice — once in the selector, once in the
service — is how the two ends drift apart.

## §8 — `workout generate`: the plan follows the dates, not a goal

The last exception in §5 was `-g` on `workout generate` meaning "the goal to plan for".
Tracing what that goal was actually *used* for closed the exception rather than defending
it.

### The goal was an indirection, not an input

In `workout_generate`, the resolved goal fed exactly one call —
`get_macrocycle_for_objective(goal['id'])` — and was never read again. Everything
downstream keyed off the macrocycle: its `strategy` and mesocycle list built the prompt,
its `id` tagged every saved workout and drove the boundary-benchmark check, and where the
goal's *target date* was needed later it was fetched back **through** the macrocycle
(`_goal_date_for_macrocycle`), walking the same 1:1 link in reverse.

Nor did `-g` narrow what the coach saw about the athlete's goals: the prompt is built from
`upcoming_objectives()` regardless. Its whole effect was swapping which strategy string
got attached.

This was history, not design. `macrocycles.objective_id` points at the anchor table, and
when generation was written there was no macrocycle-facing selector — `-M` and plan
versions arrived later with DESIGN_plan_rollback.md. `-g` was the pre-`-M` spelling of
"which plan", left in place after the concept got its own flag.

### Even the macrocycle is the wrong key

What shapes a generated week is the **mesocycle covering those dates**. The rest of the
app already knew this: `get_active_mesocycle(date)`, `get_next_mesocycle(date)` and
`get_mesocycle_ranges(start, end)` are all date-keyed, and `workout adapt`, the block
progress context, the block-boundary hint and `workout compare` reach their blocks that
way without naming a goal. Generation was the one command routing through an objective to
reach blocks a date lookup finds directly.

The symptom was in the prompt. `_get_active_strategy_and_meso_text` listed **every**
mesocycle in the macrocycle, including blocks that ended months ago, and the task text
asked the model to work out "the active mesocycle block(s) the athlete is in during this
period" for itself.

### The rule

`db.get_governing_mesocycles(start, end, prefer_macro_id=None)` answers "which blocks
govern these days", and both the CLI's staleness check and `workout_generate` read through
it. The blocks reach the prompt through the shared assembler: `_coach_context(constraints,
blocks=…)` takes its strategy text from them instead of from a goal's macrocycle, so the
date-keyed path is a parameter of the one context builder rather than a second way to
assemble a prompt. The rule itself:

* **Sequential plans both survive.** A long horizon legitimately runs out of one goal's
  last block into the next goal's first; dropping either would leave those weeks
  unplanned.
* **Plans covering the same dates cannot both be followed**, so the most recently
  generated one wins — the same tiebreak `get_periodization_ids_for_date` already made per
  day. The loser is named on stdout rather than silently discarded.
* **`-M ID` settles that contest by hand.** It is the tiebreaker, not a filter: a bare
  `-M` (or a range) names no single winner and leaves the recency rule alone.
* **Nothing overlapping falls back** to `get_active_mesocycle`'s own chain, so a plan that
  ended before the window — or starts after it — still answers. "No strategy" now means
  there genuinely is none.

Two things follow that the goal-keyed version could not express. `macrocycle_id` is
stamped **per workout** from the block covering its date, so a span crossing a plan
boundary tags each session with the plan it belongs to (`plan rollback` accounting keys off
that column). And a horizon reaching past the last block is *visible*: generation says the
plan runs out on X, where before it just produced weeks with no block behind them.

### What `-g` means now

A span, like every other selector: generation takes **both ends** of whatever
`-d`/`-m`/`-M`/`-g` resolves to. `-g` is a goal's plan-start-through-target-date span (§2),
so `workout generate -g` generates the whole plan and `--until-goal` is retired into it.
`-d 4w` still asks for four weeks, and `-g 7 -d 4w` cannot be spelled at all: the flags
share a mutually exclusive group, because a span is one choice.

The staleness warning moved with it. It used to check the plan of the earliest upcoming
goal; it now checks every plan governing the span, so one crossing two of them warns about
both.

### The span has two ends, not one

Generation used to start today whatever the selectors said, and take only their **end** as
a horizon. So `workout generate -m 5` meant "today through the end of block 5", not "block
5", and the sessions it wrote from today onward had no far edge at all: the apply pass
voided every day from the start onward, so a horizon that stopped short cancelled
everything beyond it.

Both halves are now read off the same window `resolve_window` already builds:

* **The start is the window's start**, clamped to today — yesterday is history, not a day
  to re-plan. So `-m 5` on a block three weeks out opens there, and the days between are
  left exactly as they are. A selection that *ends* before today is refused outright: a
  block that has already run is history, and quietly regenerating today in its place is
  not what was asked for.
* **The end is the window's end**, and it bounds the *write* as well as the prompt.
  `GenerateProposal` carries `gen_end` beside `gen_start`; `displaced` is read between
  them, and the void sweep in `workout_generate_apply` stops there. A bounded regeneration
  rebuilds the days it was given and leaves the rest of the plan alone — which is what
  makes `workout generate -g 1` a way to re-plan the near goal without wiping the far one.

With no selector at all the span is still bounded at both ends: today through
`config.workout_generation_span_days`. There is no second rule for the default case.

The change is not backwards compatible, so a run says which days it no longer touches:
when the span opens later than today, or when live sessions sit past its end,
`_warn_span_change` names both readings and points at `-d today..END` for the old one. It
is transitional and fires only when the two would actually differ — which includes a bare
`workout generate` whose plan already reaches past the default horizon.

### The cost this leaves standing

Generating a whole macrocycle in one call is now the easy thing to ask for, and
DESIGN_block_boundary.md §1 names the price: sessions laid down months ahead are planned
against today's metrics, never re-read against the athlete's present state, and `adapt` is
firewalled inside the current block and cannot say so. That was already true of
`--until-goal`; making it the natural reading of `-g` does not make it safer. The plan-end
warning above is a nudge in the other direction, not a fix.

`workout adapt` still resolves its strategy text the old way, through
`_get_active_strategy_and_meso_text`. It is already date-scoped to one block, so the
indirection costs it less; converting it is a separate change.

## §9 — `plan generate -g`: the same grammar, the same reading

`plan generate` took `-g` as a bare `type=int` goal ID — the one place `-g` was not the
shared grammar. It now takes the same `A..B` range, and reads it the way §2 does: **a
range is a slice of the goal timeline, and every goal in the slice gets planned.**

Two things follow, one per end of the range.

### A named goal is bounded to its own span

`plan_generate` derives a plan start by walking back to the most recent preceding goal
*that already has a plan* and opening the day after it, clamped to today. When the
preceding goal has no plan yet that derivation falls through to today — and the new plan
quietly swallows the days belonging to a goal nobody has planned for. `-g N` now bounds it
to N's own span: the day after the goal before it, whether or not that goal has a plan.

The CLI resolves the goal and the start (`_plan_targets`/`_goal_span_start` in
trainmate/cli/plans.py) and the service takes `start_date` as a parameter, so the selector
policy stays on the CLI side (§3). The service owns the notice, because only it holds both
readings: it prints one when the caller's bound and its own derivation disagree, which is
exactly when the days before the goal were about to be absorbed.

### A range plans every goal it covers

```
plan generate -g 2      # goal 2 alone, opening after goal 1's target date
plan generate -g ..2    # every upcoming goal through goal 2 — goals 1 AND 2
plan generate -g 1..2   # the same set, spelled from both ends
plan generate -g 2..    # goal 2 and everything after it
plan generate -g        # the active goal alone
plan generate           # unchanged: the next goal, the derivation above
```

The bounds are **target dates, not row IDs**: `..2` is every upcoming goal falling on or
before goal 2's target date. Row IDs usually run in date order but nothing enforces it, and
the thing being sliced is a season.

Goals are planned oldest-first, which is also the order the chain needs: each goal's window
opens after the one before it, and once goal 1 is applied goal 2's prompt sees it as the
preceding plan. Each goal keeps its own staleness gate, its own preview and its own `y` —
declining one does not stop the next, because the windows are bounded by the goal *dates*
either way. `force` is a local per goal for that reason: raising it for the goal that was
asked about must not raise it for the rest.

One ID (and a bare `-g`) resolves the goal directly rather than through the upcoming list,
so a completed or past goal stays reachable exactly as it was. Only a range is restricted
to what is still ahead — there is no window to plan in behind us.

The cost is real: `-g ..2` is one strategy call per goal. The run says so up front
(`_announce_targets`) rather than gating it, since a range is an explicit request for
exactly that, and each goal still previews before anything is written.
