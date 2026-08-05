# Block progress: regenerating the remainder of a block already under way

## 1. The problem

`workout generate` archives every workout from today onward and writes a fresh span
(`coach/service/workouts.py::workout_generate`). Run mid-block — the ordinary case, since the
default span is `workout_generation_span_days` (28) from today while a mesocycle is typically
four weeks — it is therefore writing the **remainder** of a block whose first weeks are
already trained.

It was given nothing about those weeks. Its whole view of the athlete was
`coach.metrics_lookback_days` (15) of raw activities and metrics, the CTL ramp line, the
baseline, and `meso_text`: a flat list of `name (start to end): focus` built by
`coach/service/prompt.py::_get_active_strategy_and_meso_text`. No planned workouts at all,
so no adherence signal; nothing block-relative, so no sense of where in the block it stood.

This made it the only one of the three coach prompts blind to the block's elapsed part:

| prompt | block-progress context |
| --- | --- |
| `plan generate` | `_build_prior_training_context` — per-elapsed-block planned-vs-actual: volume, load, per-sport per-zone distribution, block-over-block deltas (DESIGN_backward_evaluation.md §6) |
| `workout adapt` | `_intensity_block_context` — the active block to date as a per-week rate beside its focus, plus the current week raw (DESIGN_intensity_distribution.md §9.3) |
| `workout generate` | none |

Four consequences, in the order they bite:

- **The deload is duplicated or dropped.** The TASK instructs the model to "incorporate any
  deload weeks … in accordance with the science guidelines". There is no structural deload
  concept anywhere in TrainMate — it is prompt prose only — so nothing said whether the
  block's easy week had already been taken. A second one in week 4 was a compliant answer.
- **The progression restarts.** With only a 15-day activity average to anchor on, the
  block's remaining weeks get re-derived as though they were its opening weeks. CTL is a
  single scalar and does not carry per-week volume.
- **The boundary benchmark is re-placed.** BENCHMARK PLACEMENT says to schedule one fitness
  test in each mesocycle-boundary week the span covers, unconditionally. Regenerate *inside*
  the boundary week after the test was run and the model places a second one; the only
  counter-signal was a quietly-updated threshold in the profile, which the prompt never
  connected to "the test already happened".
- **A missed block reads as a completed one.** With no planned rows, generate cannot form
  planned-vs-actual at all: an athlete who trained four of eight planned sessions looks
  identical to one who trained four of four, and the next weeks ramp from a number the
  athlete never reached.

## 2. Scope, and the §9.2 line

Two things ride in this one section, and it is worth keeping them apart:

- **Volume, adherence and test history** (§3, §4) — facts about a job
  `DESIGN_intensity_distribution.md` §9.2 already assigns to `generate`. No design line moves.
- **The measured intensity distribution** (§5) — §9.4's handoff, which tells `adapt` that an
  over-hard block "belongs to the next `workout generate`". Landing that needed a deliberate
  amendment, recorded as §9.2a of the intensity design.

§9.3's warning is respected throughout: nothing here travels via `meso_text`, which plan
generation also reads. Both halves are threaded as arguments of their own.

## 3. The context (data)

`coach/service/context.py::_block_progress_context(as_of, gen_start)` renders the block's
elapsed part, threaded exactly as `pmc_context` is: computed in the service layer beside
`_pmc_prompt_context(...)`, passed as one new named argument into
`engine._workout_generate_logic(...)`, rendered as its own user-content section.

```
Build 1 — focus "threshold development" (2 completed weeks of 4, plus 2 days)
  Volume and load (2026-07-06..2026-07-22): 5 sessions, 6h23, 545 TSS
  Intensity distribution, per week over 2 completed weeks (2026-07-06..2026-07-19)
    cycling  [HR]   Z1 recovery 13m (9%)    Z2 aerobic 50m (32%)   Z3 tempo 1h00 (39%)
                    Z4 threshold 30m (19%)  Z5 VO2max+ 2m (1%)
    Coverage: cycling 100% HR
  What the plan PRESCRIBED over the same weeks, per week
    cycling  [HR]   Z1 recovery 25m (14%)   Z2 aerobic 1h30 (51%)  Z3 tempo 12m (7%)
                    Z4 threshold 48m (27%)  Z5 VO2max+ 2m (1%)
  Change vs Base 3 (4 completed weeks), per week
    cycling  [HR]   Z3 tempo +40m (+200%)   Z4 threshold +26m (+650%)  ...
  Current week so far (2026-07-20..2026-07-22) — day 3 of 7 (43% elapsed) ...
  Structural work (2026-07-06..2026-07-22)
    cycling                           5 sessions, 6h23, avg RPE 7.0, 447 sRPE load
    Functional Threshold Power (FTP)  271 W (first on record)
  Weeks already trained (load the plan asked -> load produced):
    - week of 2026-07-06: planned 200, actual 200 (100%)
    - week of 2026-07-13: planned 200, actual 100 (50%)
    - week of 2026-07-20 (in progress, 2 of 7 days): planned 60 so far, actual 60 (100%)
  Fitness tests this block has already run:
    - 2026-07-15: ftp_20min (cycling) — Functional Threshold Power (FTP) 271 W
```

The intensity half is `intensity.block_report`, unchanged except for §5's added table. It
opens with the same `format_header` line the section would print for itself, so it **stands in
for** that header rather than being stacked under a second copy.

**Anchored on `gen_start`, not on today.** The elapsed part ends the day before the first day
being written. On the run that preserves an already-completed session and starts tomorrow,
today is history and must be counted as such — otherwise the header's completed-week count and
the week lines below disagree by a day. The header itself is `intensity.format_header`,
unchanged, so it states what divided the numbers (§4 of the intensity design).

**Returns None** — and the prompt is then byte-identical to before — when there is no
fulfilled part to report: `as_of` outside every block (`get_active_mesocycle` falls back to a
future or first block, which would describe training that never happened), `gen_start` on or
before the block's first day (generate is writing the whole block, with nothing to continue),
or an elapsed part holding no rows at all.

### 3.1 Reusing the weekly maths

The week lines come from `progression.weekly_aggregates` — the same planned-vs-actual weekly
computation `tm progress` renders — so the coach and the athlete can never read different
numbers for the same week. `progression.week_plan_denom` supplies the comparable denominator,
which for the in-progress week is its elapsed slice.

The in-progress week is stated as raw load beside its elapsed day count and is **never
extrapolated**, following DESIGN_intensity_distribution.md §9.3: turning two days into a
week's projection would be a fabrication, and a model given the raw figure reasons about it
fine.

**Partiality is judged against the block, not `partial_plan`.** That flag compares a week
against the workout rows handed in, so a Monday the athlete simply had no session on reads as
"the plan starts mid-week" when it does not. Only a week the block itself straddles is
genuinely incomparable, and that is what gets flagged.

### 3.2 No deload label

A week is reported with its planned and actual load; nothing labels one "the deload". There is
no deload field to read, and deriving the label would mean inventing a threshold (and a config
knob) to decide how far below its neighbours a week must dip. The precedent is §9.3's: state
the figures and what divided them, and let the model read the dip. The TASK below says
explicitly that a clear dip in an elapsed week *was* the deload.

## 4. The prompt

`coach/engine/workouts.py::_block_progress_task` appends a `CONTINUING A BLOCK ALREADY UNDER
WAY` section, gated on the data being present so a clean block start produces the prompt it
always did. It names the data section, states that those days are history, and asks for three
things: carry the ramp on rather than restarting it; treat a clear dip in an elapsed week as
the deload already taken; build from the load the athlete actually produced rather than from an
unfulfilled plan.

### 4.1 Bounding BENCHMARK PLACEMENT

The same section ends by bounding the benchmark rule above it: a boundary week whose test
already appears in the progress section has had its test. This is the fix at the source —
BENCHMARK PLACEMENT's "one per boundary week" is unconditional on its own, and conditioning it
where it is stated beats dropping a duplicate after the fact.

De-duplication keys on the **planned benchmark session**, not the logbook: a test the athlete
performed but never recorded still must not be scheduled twice. A logbook row is matched to its
session by `workout_id`, falling back to a same-date reading for a result recorded without the
link; an in-block row matching no session is reported as an ad-hoc test. The window stops at
`gen_start`, so a benchmark in the part being re-planned is never reported as run — claiming it
had would suppress the very test generate must place.

`_warn_missing_boundary_benchmarks` gets the symmetric fix: a test already run earlier in the
boundary week silences it, in the same spirit as the existing "rest wins" silence. Without
this, regenerating mid-boundary-week advises regenerating again to recover a benchmark the
athlete has already done. The stored-workout lookup is bounded below `gen_start` because the
displaced plan's future rows are still live when the check runs (archival happens further
down) and must not answer for sessions this run just replaced.

## 5. The composition half

`_block_progress_context` also calls `intensity.block_report` for the block it is reporting,
with two arguments `adapt` never passes (`DESIGN_intensity_distribution.md` §9.2a):
`previous=` for the block-over-block delta, and a new `fetch_workouts=` for what the plan
prescribed over the same rate window. `coach/engine/workouts.py::_block_composition_task` then
appends a `JUDGING THE BLOCK'S COMPOSITION` section.

Its core is an **attribution rule**, not a licence to cut. A block measuring off its focus has
two opposite causes: if measured tracks the prescription, the plan is mis-designed and
re-shaping the remaining weeks is generate's; if measured diverges from the prescription, the
athlete is mis-executing, which is adapt's, and re-shaping the block around it would reward the
drift — the athlete gets an easier block for ignoring the plan. §9.2a tabulates the cases.

The section also names adapt's half explicitly, so generate does not start writing HR ceilings
into descriptions, and repeats §7's coverage caveat: an HR-only table under-reads a hard
session, so a block must not be judged too soft on heart rate alone.

### 5.1 Gate discipline

`_block_progress_context` returns `(text, has_intensity)` — a pair, like
`_pmc_prompt_context`'s. Every paragraph of the composition section quotes the zone tables, so
it is gated on those tables *having rows*, not on the block-progress section merely existing:
an athlete with no HR or power recordings gets the volume half and none of the composition
instructions. Otherwise the prompt would point at a table reading "no zone data recorded".

The flag asks `intensity.measured_window` — factored out of `block_report` for this — so the
gate and the table are decided from the same window. Asking `rate_window` directly instead
would disagree with the table on a block too young to average, whose data all sits in the
partial tail the rate window excludes.

The section as a whole is gated on **banked evidence** (week lines or tests), not on
`block_report` returning something: a started block with nothing recorded still yields a report
("no completed activities in …"), and pairing that with instructions about carrying a ramp on
from the last completed week describes a week that does not exist.

## 6. Deliberately not done

- **Giving `adapt` the prescribed table.** Measured diverging from the prescription is the
  execution question adapt already owns via §9.4, and it has the sharper instrument: a guard
  rail on the next session (§9.2a).
- **Dropping a model-proposed duplicate benchmark deterministically.** The rest-window
  pre-pass has that shape, but a date-window heuristic here would also suppress legitimate
  re-tests — a short block whose boundary test falls close behind the previous block's above
  all. The prompt names the completed tests, and the existing same-day collision guard still
  runs.
- **Per-week session counts.** `weekly_aggregates` returns loads, not counts, and the
  planned-vs-actual load pair already carries the adherence gap. Sessions-missed detail is
  adapt's, which computes real discrepancies.
- **Showing the elapsed part's planned *sessions*.** Only weekly loads cross into the prompt.
  The microcycle's weekday rhythm is visible in the completed-activity list generate already
  receives, and a second read-only-but-prompt-visible workout list is the cost
  DESIGN_block_boundary.md §5 declined for the same modest gain.
