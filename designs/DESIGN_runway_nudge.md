# Design: End-of-runway nudges — suggesting the right action when the schedule runs out

**Status:** Proposed · **Date:** 2026-08-30

## 1. The problem

The scheduled workouts end somewhere. When that end is a few days away, the athlete should
be told — and told *what to do about it*, because "the schedule runs out" has three
different causes with three different right actions:

| Cliff | Situation | Right action |
|---|---|---|
| **Span cliff** | Sessions were generated for a bounded span (`workout generate` defaults to `config.workout_generation_span_days` = 28 days from today) and the periodization runs on past the last one | `workout generate` — extend the sessions; nothing to rethink |
| **Block cliff** | The current mesocycle ends; the next block exists but its sessions were generated against stale metrics | `workout generate -m ..<id>` — already solved (DESIGN_block_boundary.md §4) |
| **Plan cliff** | The periodization itself ends — the macrocycle's last block closes, usually on the goal | If a later goal exists: `workout generate -g`. If not: `goal add`, then `plan generate` — a conversation, not a button |

Parts of this already exist, scattered by surface rather than by fact:

- The **block cliff** is fully handled: `workout adapt` prints a two-line hint on every run
  inside `config.adapt_terminal_window_days` (`cli/workouts/generate.py::
  _print_block_boundary_hint`).
- `progress` renders a **plan-gap banner** ("plan generated through X, N weeks before the
  next objective") from `progression.plan_gap`, and a **plan-lapsed** empty state — but only
  when the athlete happens to run `progress` (DESIGN_progress_timeline.md §3).
- The daily touchpoints — `workout adapt`, `status`, the bot's morning push — say nothing.
  The morning push after the last scheduled session reads "rest day", which is the schedule
  being empty, not the athlete being given rest.

The failure mode is the same one DESIGN_constraint_honoring.md §1 names for constraints:
the fact is on record, the athlete is left to notice it. The fix is the same shape too —
compute the fact once, surface it where the athlete already is, name the exact command.

## 2. One detector, many wordings

`progression.runway()` becomes the single source, beside `plan_gap` and following its
contract: pure over rows the caller fetched, returning a small structured result each
surface words itself (DESIGN_progress_timeline.md §6.0 — computing once keeps surfaces
from diverging on *when* it fires or *by how much*).

Inputs: today, the scheduled workouts from today forward, the mesocycles of the active
macrocycle, the active objectives, and `config.runway_warning_days` (§7). Output: `None`
when nothing fires, else:

- `last_workout_date` — the last scheduled session on the books, and `days_left` from
  today (negative once the cliff is behind the athlete);
- `kind` — `span` | `plan_end_next_goal` | `plan_end_no_goal`;
- for `span`: the periodization end date, so the wording can say how much plan is left;
- for `plan_end_next_goal`: the objective, reusing `plan_gap`'s derivation verbatim.

**Classification.** Compare `last_workout_date` to the last mesocycle's end date. If
periodization extends beyond the last session by more than a rest-tail margin, it is a
span cliff; otherwise a plan cliff, split on whether `plan_gap` finds a live objective
beyond the plan. The margin exists because a closing block may legitimately end on
scheduled rest (a taper's last days), and "your schedule runs out" two days before a goal
the athlete is tapering for would be noise. `adapt_terminal_window_days` (default 3) is the
right size for that tail and already means "the end-of-block zone", so it is reused rather
than minting a third window.

**When it fires.** `0 <= days_left <= runway_warning_days`, plus the passed state
(`days_left < 0` with a plan still active) — the morning push needs the latter to stop
calling an exhausted schedule a rest day (§6). Like the block-boundary hint it fires on
**every** run in the window, not only when something else is wrong: the cliff is equally
real on a green day, and acting on the nudge is what makes it stop, because the fact
changes (DESIGN_block_boundary.md §4).

## 3. Precedence

Inside the terminal window the block hint and a span-cliff runway hint can both be true —
an ending block whose successor exists but holds no sessions is *both* a block cliff and
the reason the runway ends. One fact, one nudge: when `_print_block_boundary_hint` fires,
the runway hint stays silent. The block hint's command (`workout generate -m ..<id>`)
re-plans exactly the stretch the runway hint would have pointed at, and it is the more
specific wording. A plan cliff cannot coincide with a block cliff — the block hint requires
a next mesocycle, the plan cliff requires there to be none.

## 4. CLI

The idiom is the block-boundary hint's, verbatim: two yellow lines, the fact plus the
exact command, printed from the daily touchpoints.

`workout adapt` gains `_print_runway_hint` beside `_print_block_boundary_hint` (same
call site, §3 precedence). `status` prints the same fact beside its existing
staleness/constraints/feedback nags — all of which are already "an input on record that
nothing has acted on", which is exactly what an expiring runway is.

Span cliff:

    Scheduled workouts run out in 4 day(s), on Thu Sep 3.
    Your plan covers 6 more weeks — run `workout generate` to schedule the next span.

Plan cliff, next goal on record (command matches the `progress` gap banner):

    Your plan ends with its last session on Sun Sep 6.
    Next up: Klausenpass (Oct 11) — run `workout generate -g` to build toward it.

Plan cliff, nothing after:

    Your plan ends with its last session on Sun Sep 6 — nothing is planned beyond it.
    Set what's next with `goal add`, then `plan generate`.

`workout list` additionally marks the cliff in-line whenever the listed range crosses it —
one gray line after the last session, unconditional (it is a fact of the listing, not a
warning): `— end of scheduled workouts (plan continues to Oct 18) —`, or `(end of plan)`.
An empty tail then reads as a fact rather than a rendering gap.

`progress` is unchanged: its banners already exist, already derive from `plan_gap`, and
already name the same fixes. The runway hint complements them at session granularity; it
does not replace them.

## 5. Telegram, expert mode

Free. The expert bot executes the real CLI and returns its output in `<pre>` blocks, so
the §4 hints arrive the moment the CLI prints them. Byte-parity preserved, nothing built.

## 6. Telegram, simple mode

The athlete never types commands, so the suggestion must become a button or prose — and
per the standing split, the CLI owns *what* to offer, the bot only renders
(`cli/bot.py::MORNING_BUTTONS`). `bot morning` grows one conditional line, inside its
existing per-day idempotent push:

**Span cliff** — a line and a one-tap fix. `workout generate` is safe to offer because the
button feeds it through the normal command pipeline like every `send` entry, and the
structured-prompt protocol (`TRAINMATE_FRONTEND=json`) renders its preview/confirm as
tappable buttons — she sees the proposed week and confirms with taps, as with any decision.

> 🏃 Today: 45 min easy run …
>
> Heads up — your schedule runs out on Thursday. Want me to plan the next few weeks?
>
> `[📅 Plan my next weeks]` `[👍 Got it]` `[😴 Feeling tired]` …

**Plan cliff** — prose only, both variants. Periodization decisions are operator territory
in simple mode, the same scoping the bot design applies to rendering ("simple mode renders
the commands worth rendering"; DESIGN_bot_simple_frontend.md §2):

> 🎉 Your plan wraps up this Sunday — that's the goal you've been training toward!
> When you know what you'd like to work toward next, just tell me and we'll set it up.

**The rest-day lie.** `simple_day_lines` renders any empty day as the one-line rest
message, whatever the reason it is empty (DESIGN_bot_simple_frontend.md §10). Once the
cliff has *passed* — no session today and none ahead, plan still active — that is wrong:
"rest day 🌴" describes an exhausted schedule as a coaching decision. The passed state
(§2) takes precedence over the rest line in the morning push: "You've finished everything
on the schedule 🎉", followed by the span-cliff button or the plan-cliff prose as
appropriate. Inside the window but before the cliff, the rest line stays and the runway
line is appended after it. `simple_week_lines` gets the matching one-liner when its window
crosses the cliff ("that's the end of the current schedule"), mirroring §4's list marker.

The push repeats each morning inside the window until acted on. That is the §2 unconditional
rule, not an oversight: no new state, and generating the next span is what silences it.

**Free text about the next goal.** The router gains a `new_goal` intent ("the athlete says
what they want to train for next — a race, an event, a new target"). In this design it is
reply-only, like `help` and `unclear`: a warm echo ("Exciting — I'll pass that on to your
coach 🎯") and nothing persisted. Every durable inbox on hand is wrong for it — `workout
adapt -m` classifies constraint-shaped notes, `plan feedback` files against the plan being
superseded, `journal` is the run log — and goal authoring needs a date, an event-vs-horizon
choice and a priority, which is a conversation the simple surface cannot yet hold. In the
companion-mode setup the operator reads the same chat, so the message reaches a human who
can run `goal add`. A goal-capture conversation is named future work (§8), not smuggled in.

## 7. Config

`coach.runway_warning_days`, default **7**, sibling to `adapt_terminal_window_days` in
`config.py` and the config templates. Wider than the terminal window (3) deliberately:
the block window gates prompt behaviour and must stay tight, while generating the next
span is a deliberate act the athlete may sit on for a few days — a week gives the nudge
room to be seen twice before the schedule actually dries up.

## 8. Deliberately not done

- **Auto-generation.** Every path ends at the athlete confirming a preview. Generation
  costs an LLM call and rewrites the calendar; it stays behind an explicit yes (tap or
  `y`), on both surfaces.
- **A separate nag channel or scheduler.** The nudge rides the daily touchpoints and the
  existing morning push with its settings-marker idempotence. No new push, no new state.
- **Gating on fatigue or proposals.** Fires every run in the window, per the
  block-boundary precedent — the schedule is equally finite on a green day.
- **Simple-mode plan generation.** A `plan generate` preview is long, and the choice of
  what to periodize toward is the operator's in companion mode. Span extension is the only
  one-tap offer.
- **Goal capture in chat.** The `new_goal` intent stays reply-only until a real
  goal-authoring conversation (date, event-vs-horizon, priority) is designed; a half-goal
  written by a router would be worse than a message the operator reads.
- **Predicting the cliff from the generation span.** The detector reads the workouts
  actually on the books, never `workout_generation_span_days` arithmetic — the athlete may
  have generated with any selector, and the rows are the truth.
