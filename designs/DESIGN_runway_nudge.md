# Design: End-of-runway nudges — suggesting the right action when the schedule runs out

**Status:** Proposed · **Date:** 2026-08-30 · **Revised:** 2026-08-31 (review pass:
coverage invariant replaces the rest-tail margin, block hint folded in, passed-state
window, honest `new_goal` reply)

## 1. The problem

The scheduled workouts end somewhere. When that end is a few days away, the athlete should
be told — and told *what to do about it*, because "the schedule runs out" has three
different causes with three different right actions:

| Cliff | Situation | Right action |
|---|---|---|
| **Span cliff** | Sessions were generated for a bounded span (`workout generate` defaults to `config.workout_generation_span_days` = 28 days from today) and the periodization runs on past the last one | `workout generate` — extend the sessions; nothing to rethink |
| **Block cliff** | The current mesocycle's sessions run out at its boundary; the next block exists but holds no fresh sessions | `workout generate -m ..<id>` — re-plan the next block against current metrics |
| **Plan cliff** | The periodization itself ends — the macrocycle's last block closes, usually on the goal | If a later goal has a plan: `workout generate -g`. If not: `goal add`, then `plan generate` — a conversation, not a button |

Parts of this already exist, scattered by surface rather than by fact:

- The **block cliff** has a hint — but only on one surface: `workout adapt` prints two
  lines inside `config.adapt_terminal_window_days`
  (`trainmate/cli/workouts/generate.py::_print_block_boundary_hint`). `status` and the
  bot never see it, so the same morning can already answer differently depending on
  which command was typed. §3 folds it into the detector rather than adding a second
  hint beside it.
- `progress` renders a **plan-gap banner** ("plan generated through X, N weeks before the
  next objective") from `progression.plan_gap`, and a **plan-lapsed** empty state — but only
  when the athlete happens to run `progress` (DESIGN_progress_timeline.md §3).
- The daily touchpoints — `workout adapt`, `status`, the bot's morning push — say nothing.
  Worse, two of them actively mislead once the schedule is exhausted: the morning push
  reads "Rest day — enjoy it 🎉" (the schedule being empty, not the athlete being given
  rest), and `workout adapt` — adapting against `get_active_mesocycle`'s wholly-past
  fallback block — closes with "All metrics are green and workout plan is on track."
  over an empty calendar (§4).

The failure mode is the same one DESIGN_constraint_honoring.md §1 names for constraints:
the fact is on record, the athlete is left to notice it. The fix is the same shape too —
compute the fact once, surface it where the athlete already is, name the exact command.

## 2. One detector, many wordings

`progression.runway()` becomes the single source, beside `plan_gap` and following its
contract: pure over rows the caller fetched, returning a small structured result each
surface words itself (DESIGN_progress_timeline.md §6.0 — computing once keeps surfaces
from diverging on *when* it fires or *by how much*).

Inputs: today, the **generated** workouts from today forward, the mesocycles of the active
macrocycle, the active objectives, and `config.runway_warning_days` (§7). Two input rules,
both following `progression.plan_end`'s precedent (progression.py):

- **Manual rows don't count.** `workout add` needs no plan, so a hand-entered event —
  the race itself, put on the calendar weeks out — must not make the detector believe
  the schedule reaches it while the generated sessions end next Thursday. The hole is
  the fact, and only generated rows testify to it.
- **Rest rows do count.** A planned rest day is a row like any other (§2.1), so the last
  covered date is read off the rows directly — no margin, no guessing whether a quiet
  tail is a taper or a hole.

Output: `None` when nothing fires, else:

- `last_covered_date` — the last date a generated row (rest included) covers, and
  `days_left` from today (negative once the cliff is behind the athlete);
- `kind` — `block` | `span` | `plan_end_next_goal` | `plan_end_no_goal`;
- for `block`: the next mesocycle's id, so every surface can name `-m ..<id>`;
- for `span`: the periodization end date, so the wording can say how much plan is left;
- for `plan_end_next_goal`: the objective — via `plan_gap`, **fed the mesocycle-derived
  plan end**, not `progression.plan_end`. The two differ exactly when the detector
  fires: `progression.plan_end` is the last generated *row* (right for the progress
  banner, which asks "do the workouts reach the goal?"), while classification here asks
  "does the *periodization* reach a goal?" — a span cliff must not read as a goal gap.

### 2.1 The coverage invariant

Classification is an exact comparison, and it can be because generation covers every
date of its span: a planned rest day is written as an explicit rest row, never a hole.
This is already the model's behavior — the response schema names "Rest Day" as a title,
`adapt` already has the rule ("an omitted date keeps the plan; an absent row and an
explicit rest day mean different things", `coach/engine/workouts.py`), and the live data
confirms it (every date of the current plan holds a row, taper rest included). This
design promotes the behavior to an invariant: one sentence in the generate task stating
it, and a deterministic backstop that fills any hole the model leaves with the existing
`_rest_workout` factory (`coach/service/workouts.py` — the same factory the rest-window
pre-pass uses), so the invariant holds by construction, not by model compliance.

The earlier draft carried a "rest-tail margin" here (reusing `adapt_terminal_window_days`
to absorb a taper's quiet tail). The invariant retires it, and with it two misfires — a
taper ending in more than three rest days would have nudged forever with a fix that
generates nothing, and sessions genuinely missing for the plan's last couple of days
would have been misread as the plan ending — plus the silent overloading of a knob
documented as gating prompt behavior.

**Classification.** Compare `last_covered_date` to the last mesocycle's end date:

- covered short of plan end, ending exactly on a non-final block's boundary with a next
  block on record → **block** cliff;
- covered short of plan end otherwise → **span** cliff;
- covered through plan end → **plan** cliff, split on whether `plan_gap` (fed the plan
  end, per above) finds a live objective beyond it.

**When it fires.** `0 <= days_left <= runway_warning_days`, plus the **passed state**:
`days_left < 0` while today is at most `runway_warning_days` past the plan's end (the
later of `last_covered_date` and the last mesocycle's end). "Plan still active" is
defined by the macrocycle's dates, deliberately not by `upcoming_objectives()` — that
filter drops a goal the day after its target date, which is exactly the morning the
wrap-up message matters most. Past the passed-state window the detector returns `None`
and the surfaces go quiet (§6 — an honest nothing beats a daily 🎉 forever).

Like the old block hint it fires on **every** run in the window, not only when something
else is wrong: the cliff is equally real on a green day, and acting on the nudge is what
makes it stop, because the fact changes (DESIGN_block_boundary.md §4).

## 3. One hint, not two

The earlier draft kept `_print_block_boundary_hint` and gave the runway hint a
precedence rule. That rule could only exist on `workout adapt` — the only surface the
block hint ever fired on — so `status` would have named a different command than `adapt`
on the same morning, and even on `adapt` alone the advice would have flipped from
`workout generate` (days 7–4, runway window) to `workout generate -m ..<id>` (days 3–0,
block window) mid-week.

Instead the detector owns the distinction: the `block` kind carries the next mesocycle's
id, and **every** surface prints the same `-m ..<id>` command from the first day of the
runway window. `_print_block_boundary_hint` retires into `_print_runway_hint` — one
printer, one fact, one command. DESIGN_block_boundary.md §4's CLI hint is subsumed by
this; its §3 prompt-side terminal window (`THIS BLOCK IS ENDING`) is untouched and stays
on `adapt_terminal_window_days` — that gate is about what the *coach model* is told, and
must stay tight for the reasons that design gives.

A plan cliff cannot coincide with a block cliff — the block kind requires a next
mesocycle, the plan cliff requires there to be none.

## 4. CLI

The idiom is the old block hint's, verbatim: two yellow lines, the fact plus the exact
command, printed from the daily touchpoints.

`workout adapt` calls `_print_runway_hint` where `_print_block_boundary_hint` used to
sit. `status` prints the same fact beside its existing staleness/constraints/feedback
nags — with one placement rule the earlier draft missed: those nags all live inside
`status`'s `if objectives:` / `if macro:` branch, and the plan-cliff-no-goal case — the
one wording that exists *because* nothing is on record — never reaches that branch. The
runway hint therefore prints **outside** it, unconditionally on detector output.

**Adapt stops contradicting the fact.** When every block of the active macrocycle is
behind today, `workout adapt` currently adapts against `get_active_mesocycle`'s
absolute-first-block fallback and closes with "All metrics are green and workout plan is
on track." — over an empty calendar. This design extends DESIGN_block_boundary.md §6's
"adapt requires a block" one step: with the plan wholly in the past, adapt **refuses**,
printing the plan-cliff wording (the same two lines) instead of a green all-clear. There
is nothing to adapt *towards*; the block-boundary design already accepted that
consequence for the no-plan case, and a finished plan is the same situation one day
later. (`bot morning` with `adapt-first` on already swallows a refusing adapt into a
terminal-side aside, so the push degrades exactly as it does today.)

Block cliff (same wording every surface, every day in the window):

    This block ends in 4 day(s), on Thu Sep 3, and the next one has no fresh sessions.
    Run `workout generate -m ..7` to plan it against current metrics.

Span cliff:

    Scheduled workouts run out in 4 day(s), on Thu Sep 3.
    Your plan covers 6 more weeks — run `workout generate` to schedule the next span.

(Day-zero gets its own phrasing — "Scheduled workouts run out today." — never
"in 0 day(s)".)

Plan cliff, next goal with a plan on record (command matches the `progress` gap banner):

    Your plan ends with its last session on Sun Sep 6.
    Next up: Klausenpass (Oct 11) — run `workout generate -g` to build toward it.

Plan cliff, nothing after:

    Your plan ends with its last session on Sun Sep 6 — nothing is planned beyond it.
    Set what's next with `goal add`, then `plan generate`.

`workout list` additionally marks the cliff in-line whenever the listed range crosses it —
one gray line after the last session, unconditional (it is a fact of the listing, not a
warning): `— end of scheduled workouts (plan continues to Oct 18) —`, or `(end of plan)`.
A listing with nothing to show renders the marker **alone** — today `run_workout_list`
prints a header and then nothing, and an empty listing is precisely the passed-state case
where the athlete most needs the gap named rather than left looking like a rendering bug.

`progress` is unchanged: its banners already exist, already derive from `plan_gap`, and
already name the same fixes. The runway hint complements them at session granularity; it
does not replace them.

## 5. Telegram, expert mode

Free. The expert bot executes the real CLI and returns its output in `<pre>` blocks, so
the §4 hints arrive the moment the CLI prints them. Byte-parity preserved, nothing built.

## 6. Telegram, simple mode

The athlete never types commands, so the suggestion must become a button or prose — and
per the standing split, the CLI owns *what* to offer, the bot only renders
(`trainmate/cli/bot.py::MORNING_BUTTONS` defines the rows; `trainmate_bot.py` renders
them and maps the taps — the offer spans both files). `bot morning` grows one conditional
line, inside its existing per-day idempotent push.

**Span and block cliffs — a line and a one-tap fix.** This **amends
DESIGN_bot_simple_frontend.md §7's guardrail**, which listed `workout generate` among
the commands requiring typed expert vocabulary (that document is edited in the same
change). The amendment is deliberately narrow: `workout generate` becomes tappable in
exactly one place — the morning push's runway button, whose argv comes from the detector
(`workout generate`, or `workout generate -m ..<id>` for a block cliff) — and never from
the free-text router. It is safe to offer because the button feeds the normal command
pipeline like every `send` entry, and the structured-prompt protocol
(`TRAINMATE_FRONTEND=json`) renders its preview/confirm as tappable buttons — she sees
the proposed weeks and confirms with taps, as with any decision.

Two mechanics the earlier draft hand-waved:

- **One gate per button row.** `emit_buttons` today fires only `if ahead:` — deliberately,
  so an all-done day earns no "Can't today" row. That gate stays exactly as it is for the
  session rows. The runway row has its own, equally simple gate: *the detector fired with
  a `block` or `span` kind*. `bot morning` emits `session_rows(if ahead) +
  runway_row(if runway and runway.kind in (block, span))` — two independent conditions,
  each answering its own question, so relaxing one cannot resurrect the other's buttons.
- **The preview must not be a `<pre>` dump.** `workout generate` joins the simple-rendering
  opt-in set for its preview: the proposed sessions render through `simple_week_lines`
  (one dated line per session — the form her week view already uses), descriptions
  staying expert detail. Rest rows get their own entry in `SPORT_EMOJI` so a taper week
  reads as intended rest, not as a generic session.

> 🏃 Today: 45 min easy run …
>
> Heads up — your schedule runs out on Thursday. Want me to plan the next few weeks?
>
> `[📅 Plan my next weeks]` `[👍 Got it]` `[😴 Feeling tired]` …

**Plan cliff — prose only, both variants.** Periodization decisions are operator
territory in simple mode, the same scoping the bot design applies to rendering ("simple
mode renders the commands worth rendering"; DESIGN_bot_simple_frontend.md §2):

> 🎉 Your plan wraps up this Sunday — that's the goal you've been training toward!
> When you know what you'd like to work toward next, tell your coach — setting up a new
> goal happens from the computer.

**The rest-day lie, and when to just say nothing.** `simple_day_lines` renders any empty
day as "Rest day — enjoy it 🎉", whatever the reason it is empty
(DESIGN_bot_simple_frontend.md §10). Once the cliff has *passed* — no session today and
none ahead, plan still active per §2's definition — that is wrong: it describes an
exhausted schedule as a coaching decision. The passed state takes precedence over the
rest line in the morning push: "You've finished everything on the schedule 🎉", followed
by the span/block-cliff button or the plan-cliff prose as appropriate. Inside the window
but before the cliff, the rest line stays and the runway line is appended after it.
`simple_week_lines` gets the matching one-liner when its window crosses the cliff
("that's the end of the current schedule"), mirroring §4's list marker.

The push repeats each morning inside the window until acted on — the §2 unconditional
rule. But the plan-cliff-no-goal passed state has no action the athlete can take (the fix
is operator work), so it must not repeat forever: past the §2 passed-state window
(`runway_warning_days` mornings after the plan's end) the detector returns `None` and the
morning push **sends nothing at all** on an empty day — not the rest-day lie, not a stale
celebration. Silence is the honest state for "no plan covers today"; the push resumes the
morning a schedule exists again.

**Dedup with `adapt-first`.** When `adapt-first` is on, the push renders `workout adapt
-y`'s output — which now carries §4's hint lines. The CLI suppresses the runway hint
under `TRAINMATE_RENDER=simple`: on the simple surface the bot words the fact itself, and
one fact gets one wording per message.

**Free text about the next goal.** The router gains a `new_goal` intent ("the athlete says
what they want to train for next — a race, an event, a new target"). In this design it is
reply-only, like `help` and `unclear` — and the reply is **honest about what happens
next**: "A new goal — exciting! 🎯 Setting that up happens from the computer (`goal add`)
— tell your coach directly so it isn't lost." No delivery is promised because none is
performed: the message is not persisted (every durable inbox on hand is wrong for it —
`workout adapt -m` classifies constraint-shaped notes, `plan feedback` files against the
plan being superseded, `journal` is the run log), and nothing forwards it — the operator
chats with the bot from their *own* chat id and never sees the athlete's messages.
Forwarding the text to an operator chat, and a real goal-capture conversation, are both
named future work (§8), not smuggled in.

## 7. Config

`runway_warning_days`, default **7** — read as `config.runway_warning_days` (§2's
spelling), stored under the `coach:` section like its sibling
`adapt_terminal_window_days`, in `config.py` and `config_template_full.yaml` (the short
template stays minimal and gains nothing — it has no `coach:` section by design). Wider than the terminal window (3)
deliberately: the terminal window gates prompt behaviour and must stay tight, while
generating the next span is a deliberate act the athlete may sit on for a few days — a
week gives the nudge room to be seen twice before the schedule actually dries up. The
same value bounds the passed-state window (§2): a week of "you've finished everything",
then quiet.

## 8. Deliberately not done

- **Auto-generation.** Every path ends at the athlete confirming a preview. Generation
  costs an LLM call and rewrites the calendar; it stays behind an explicit yes (tap or
  `y`), on both surfaces.
- **A separate nag channel or scheduler.** The nudge rides the daily touchpoints and the
  existing morning push with its settings-marker idempotence. No new push, no new state.
- **Gating on fatigue or proposals.** Fires every run in the window, per the
  block-boundary precedent — the schedule is equally finite on a green day.
- **Simple-mode plan generation.** A `plan generate` preview is long, and the choice of
  what to periodize toward is the operator's in companion mode. Span/block extension is
  the only one-tap offer.
- **Goal capture in chat.** The `new_goal` intent stays reply-only until a real
  goal-authoring conversation (date, event-vs-horizon, priority) is designed; a half-goal
  written by a router would be worse than an honest "tell your coach".
- **Forwarding `new_goal` text to the operator.** The bot could relay the athlete's
  message to a second allowlisted chat id — but nothing today marks which id is "the
  operator", and inventing that notion for one intent is out of scope. Named here so the
  honest reply above can one day become "passed on" and mean it.
- **Predicting the cliff from the generation span.** The detector reads the workouts
  actually on the books, never `workout_generation_span_days` arithmetic — the athlete may
  have generated with any selector, and the rows are the truth. (Generated rows: §2 says
  why manual ones are excluded.)
