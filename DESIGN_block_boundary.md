# Block boundary: adapting near the end of a mesocycle

## 1. The problem

`workout adapt` reads a backward window of recovery metrics (`config.metrics_lookback_days`)
and adapts forward from the evaluation date to the end of the mesocycle containing it
(`coach/service.py::workout_adapt`). The backward window has a fixed size. The forward range
does not — it shrinks toward nothing as the evaluation date approaches the block's end.

Two consequences follow:

- The coach keeps full evidence of fatigue but loses the runway to act on it. On the
  penultimate day the only adaptable sessions are today's and tomorrow's, and today's is
  locked if a matching activity was already recorded.
- The next block is invisible. `get_workouts` is bounded at the mesocycle end, so an
  already-generated next block is neither read nor writable.

The fatigue signal is not actually lost, though — it travels a different path.
`workout_generate` reads the same `metrics_lookback_days` window (metrics, completed
activities, baseline), so regenerating the next block against current metrics closes the loop.
The real failure mode is **staleness**: a block generated far ahead (e.g. `workout generate
--until-goal` laying down a whole macrocycle) is never re-read against the athlete's present
state, and `adapt` cannot reach it to say so.

## 2. Why adapt does not reach across the boundary

Extending the adaptation range into the next block would be a small code change. We
deliberately do not.

`adapt` is tactical and within-block. It is read-only with respect to coach learnings
(DESIGN_evidence_based_confidence.md §2/§11), it is instructed not to permanently reshape the
mesocycle, and it carries a compounding guard precisely because its cuts are meant to be
transient and to rebound within the block. Periodization is authored elsewhere, by `plan` and
`workout generate`.

Letting a daily check rewrite a multi-week block would collapse that separation, and it would
do so at the worst possible moment: recovery metrics lag, so a depressed morning at a block's
end is disproportionately likely to reflect fatigue a prior adaptation already acted on.

The boundary therefore stays a firewall. Rather than crossing it, we make both sides aware
of it.

## 3. Terminal-window guidance (prompt)

When the evaluation date falls within `config.adapt_terminal_window_days` of the block's end,
`engine.py::_workout_adapt_logic` appends a `THIS BLOCK IS ENDING` section to the task. It
states the two consequences from §1 and biases the model toward holding planned load:
preserve or reschedule rather than cut, and do not deepen a cut to "carry" the athlete into a
block that will be planned against its own metrics when it is generated.

The section is appended conditionally, so runs outside the window produce a byte-identical
prompt to before.

## 4. Regeneration nudge (CLI)

`cli/workouts.py::run_workout_adapt` prints a hint whenever the evaluation date is in the
terminal window and a next block exists: which block is ending, when, and the exact
`workout generate --until-mesocycle <id>` invocation that re-plans the next one against
current metrics.

It fires on **every** run inside the window, not only when adaptations are proposed. The next
block is equally stale on a green day, and gating the hint on detected fatigue would surface
it only once it was too late to act on.

## 5. Deliberately not done

- **Extending the adaptation range past `meso_end`.** See §2.
- **Feeding the next block's concrete sessions to the model as read-only context.** The system
  prompt already lists every mesocycle's name, date range and focus
  (`coach/service.py::_get_active_strategy_and_meso_text`), which is enough to support the "is
  easing cheap here?" judgement. Adding the sessions would introduce a new data path and a new
  class of prompt-visible-but-immutable workout for modest gain.

## 6. Known asymmetry

`get_active_mesocycle` falls back to the next *future* block when no block contains the
evaluation date. On a calendar gap between blocks the adaptation range therefore snaps from
one day (the last day of a block) to the whole upcoming block, rather than tapering.

Blocks are contiguous in practice. Both features here gate on a small day-delta against the
returned block's end date, so neither misfires in the gap case: the returned block is the
upcoming one, whose end is far away. Recorded rather than fixed.
