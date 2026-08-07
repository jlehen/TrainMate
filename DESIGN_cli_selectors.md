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
`--message` under `workout adapt` and `--metric` under `context`; `--date` was a single day
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
| `backward` | `default_span` before the end | today | −6d → today | `workout compare`, `data show-*`, `data pull`, `context list` |
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
them. `context rm` takes the same shape, with a metric name in place of a date.

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
* `context list`/`rm`: `--metric` loses `-m`, and the metric becomes the positional —
  which is what §a2 of DESIGN_cli_noargs.md says a command's subject should be anyway;
* `goal edit --date` → `--target-date`.

Two deliberate exceptions, each because the command cannot mean the reserved thing:

* **`workout adapt -m`** stays `--message`, and **`context add`** takes no `-m`/`-M`: both
  act on days, not on blocks, so a mesocycle is not a slice they could take. `-d` there is
  a single date (`parse_single_date`), as it is on `benchmark record`.
* **`goal edit --target-date`, `constraint add/edit --start/--end`** name a *stored field*,
  not a filter. The line: a command that acts over a span of days takes `-d` (`context add`
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

The horizon, like every other selector: generation always starts today, so it takes the
**end** of whatever `-d`/`-m`/`-M`/`-g` resolves to. `-g` is a goal's plan-start-through-
target-date span (§2), whose end is race day — so `workout generate -g` generates the whole
plan, and `--until-goal` is retired into it. `-d 4w` still asks for four weeks, and `-g 7
-d 4w` cannot be spelled at all: the flags share a mutually exclusive group, because a
horizon is one choice.

The staleness warning moved with it. It used to check the plan of the earliest upcoming
goal; it now checks every plan governing the horizon, so a span crossing two of them warns
about both.

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
