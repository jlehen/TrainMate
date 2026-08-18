# Block boundary: adapting near the end of a mesocycle

## 1. The problem

`workout adapt` reads a backward window of recovery metrics (`config.metrics_lookback_days`)
and adapts forward from the evaluation date to the end of the mesocycle containing it
(`coach/service/adaptation.py::workout_adapt`). The backward window has a fixed size. The
forward range does not — it shrinks toward nothing as the evaluation date approaches the
block's end.

Two consequences follow:

- The coach keeps full evidence of fatigue but loses the runway to act on it. On the
  penultimate day the only adaptable sessions are today's and tomorrow's, and today's is
  locked if a matching activity was already recorded.
- The next block is invisible. `get_workouts` is bounded at the mesocycle end, so an
  already-generated next block is neither read nor writable. Both halves are enforced
  separately: the read bound keeps post-boundary sessions out of the prompt, and a
  write-side filter drops any proposal dated past the range end, so a hallucinated date
  cannot slip through (the apply range is derived from the surviving proposals, so it
  cannot stretch past the block either).

When no mesocycle covers the evaluation date at all, `workout_adapt` **refuses**, raising
the same "run `plan generate` first" error `workout_generate` already raises (§6). There is
no synthetic range: every judgement adapt makes is relative to the block — its focus, the
days it has left, whether a cut can still rebound before it ends — so without one there is
nothing to adapt *towards*.

The fatigue signal is not actually lost, though — it travels a different path.
`workout_generate` reads the same `metrics_lookback_days` window (metrics, completed
activities, baseline), so regenerating the next block against current metrics closes the loop.
The real failure mode is **staleness**: a block generated far ahead (e.g. `workout generate
-g` laying down a whole macrocycle — the natural reading of that flag since
DESIGN_cli_selectors.md §8, so this is now the easy thing to ask for) is never re-read against the athlete's present
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
`coach/engine/workouts.py::_workout_adapt_logic` appends a `THIS BLOCK IS ENDING` section to
the task. It states the two consequences from §1 and biases the model toward holding planned
load: an easing has no runway left to rebound, and a cut must not be deepened to "carry" the
athlete into a block that will be planned against its own metrics when it is generated.

"Prefer rescheduling over cutting" used to be restated here too. It is now standing rule 1 of
the TASK (`DESIGN_adapt_task_prompt.md` §2), which holds over every section, so this one
carries only what is specific to the terminal window — the lost runway and the next block
being out of reach. The rationale for both is §1 and §2 above, not the prompt.

The section is appended conditionally, so runs outside the window produce a byte-identical
prompt to before.

## 4. Regeneration nudge (CLI)

`cli/workouts/generate.py::_print_block_boundary_hint` (called by `run_workout_adapt`) prints
a hint whenever the evaluation date is in the terminal window and a next block exists: which
block is ending, when, and the exact `workout generate -m ..<id>` invocation that
re-plans the next block against current metrics.

`-m ..<id>` sets only the end date (DESIGN_cli_selectors.md §5); generate starts from today,
so that command also
rewrites the ending block's remaining sessions. That is the intent — inside the terminal
window the tail is a few days, and they are re-planned against the same current metrics — but
it is a wider rewrite than the phrasing suggests.

It fires on **every** run inside the window, not only when adaptations are proposed. The next
block is equally stale on a green day, and gating the hint on detected fatigue would surface
it only once it was too late to act on.

## 5. Deliberately not done

- **Extending the adaptation range past `meso_end`.** See §2.
- **Feeding the next block's concrete sessions to the model as read-only context.** The system
  prompt already lists every mesocycle's name, date range and focus
  (`coach/service/prompt.py::_get_active_strategy_and_meso_text`), which is enough to support
  the "is easing cheap here?" judgement. Adding the sessions would introduce a new data path
  and a new class of prompt-visible-but-immutable workout for modest gain.

## 6. Known asymmetry

`get_active_mesocycle` has two fallbacks when no block contains the evaluation date: first
the next *future* block, then — if every block is already over — the absolute first
mesocycle. On a calendar gap between blocks the adaptation range therefore snaps from one day
(the last day of a block) to the whole upcoming block, rather than tapering.

Blocks are contiguous in practice. Both features here gate on `0 <= days_left <= N` against
the returned block's end date, so neither misfires in either fallback: the future block's end
is far away (`days_left` large), and a wholly-past block gives a negative `days_left`.
Recorded rather than fixed.

**No mesocycle at all — `adapt` refuses.** This case used to synthesize a range end of
evaluation date + 6 days, which meant the prompt gate compared `days_left` against an
invented boundary: an `adapt_terminal_window_days` of 6 or more would have announced
`THIS BLOCK IS ENDING` for a block that does not exist. Rather than special-case the gate,
adapt now requires a block, matching `workout_generate`, which has always refused without a
periodization strategy. The phantom-boundary case stops existing instead of being guarded.

The cost is deliberate and worth naming: adapt used to degrade all the way down — it
tolerates a missing *goal* too (`objective_id` is `None` when there is no active
objective), so it kept answering "you slept badly, should today change?" in the gap between
one goal ending and the next being set. It no longer does. Two consequences follow: a
manually added workout (`workout add` needs no plan) cannot be adapted, and the engine's
"no active block" branch — which dropped the intensity table and the drift instructions
that reference it — is no longer reachable through this path.
