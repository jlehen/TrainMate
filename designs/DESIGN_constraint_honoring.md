# Design: knowing whether the plan reflects a constraint

## 1. The problem

A constraint dated outside the current mesocycle is stored, is never lost, and is eventually
honored — but nothing acts on it now, and **nothing says so**.

`coach/service/adaptation.py::workout_adapt` pins its forward range to the block containing
the evaluation date:

```python
active_meso = self._db.get_active_mesocycle(target_date_str)
meso_end_date_str = active_meso['end_date']
constraints = self._db.get_constraints(target_date_str, meso_end_date_str)
```

Two bounds follow, enforced separately (DESIGN_block_boundary.md §1): the read bound keeps
post-boundary sessions out of the prompt, and a write-side filter drops any proposal dated past
`meso_end`. So even when the sessions already exist — `workout generate -g` lays down a whole
macrocycle, and since DESIGN_cli_selectors.md §8 that is the natural thing to ask for — a
constraint landing three weeks out changes nothing today.

The existing tiers cover it eventually:

| the constraint | what honors it |
| --- | --- |
| falls inside the current block | daily `adapt` |
| trips the §7 magnitude heuristic (DESIGN_constraints.md) | the replan proposal, then `plan generate` |
| anything else past `meso_end` | the next `workout generate` whose horizon reaches it |

The third row is the gap, and the cost is not that it is honored late. It is that the athlete
**cannot tell**. For "no run that Thursday" late is fine; for "away the 10th to the 20th" the
calendar shows sessions the athlete already knows they will not do, for weeks, with no
indication that the coach has registered the fact.

A constraint straddling the block boundary is worse: `get_constraints` returns it — the query
is an overlap, not a containment — so a ten-day trip starting three days before the block ends
has its first three days rested and its remaining seven ignored, and nothing marks the seam.

**This design closes the silence, not the gap.** §5 records why.

## 2. `honored_at` — the signal

One column, `constraints.honored_at` (UTC ISO, nullable) — a one-off migration, no backward
compatibility, per AGENTS.md. The workouts table carries no per-row `updated_at`, so the answer
cannot be inferred from timestamps, and inferring it from content only works for `rest = 1`:
honoring an advisory constraint is a judgement, not a state.

What the column means, stated plainly so nobody reads more into it: *a coach pass had this
constraint in scope, with authority over every day of it still ahead.* **Not** *the plan
definitely changed*. That is the same warrant `workout generate` gives for every other
constraint it built around, and it is a nudge's worth of precision, which is all this needs.

**Set** on successful apply by any command that wrote sessions across every remaining day of
the constraint's window — the window clipped below to the first day the pass itself had
authority over: the evaluation date for `adapt`, `gen_start` for `generate`. No command has
authority over days already behind it, and demanding the literal whole window would leave any
constraint already under way flagged forever, since nothing could ever cover its past again.

The writers are `workout_generate_apply`, for constraints whose clipped window sits inside
`gen_start..gen_end` (generate's constraint fetch is open-ended, so coverage is checked against
the *written* range, not the fetched set), and the adapt path, for constraints whose clipped
window sits inside the adaptation range. The adapt stamp is what keeps the nudge quiet about
constraints the daily command is already handling, and it splits across the preview/apply seam:
a run that proposes changes stamps only on the athlete's `y` (`workout_revision_apply`) — a
declined proposal reflects nothing — while a run that proposes *no* change never reaches apply
at all. Requiring a *change* would leave "no adaptation needed" flagged forever, so the
no-change branch stamps too, through `workout_revision_record_no_change`.

**That stamp is a write, so it is on the write side.** An earlier draft put it inside
`workout_adapt`, which builds a proposal and is supposed to write nothing. The cost was
immediate: a second producer of the same proposal shape appeared, nobody noticed it needed the
same call, and a pass that answered "the plan already works around this" left the constraint
unstamped — so the nudge re-offered it forever. `tests/test_service_invariants.py` now fails any
method returning a `*Proposal` that touches the database, so the shape cannot come back.

**Cleared** by `constraint edit` whenever `start_date`, `end_date`, `rest`, `title` or
`description` changes. The window moved, or the directive changed — and for an advisory
constraint the prose *is* the enforcement mechanism (DESIGN_constraints.md §5), so new words are
a new directive a previous honoring says nothing about.

**Cleared, too, by a rollback that resurrects a plan older than the honoring.** Both rollbacks
restore a batch keyed by its stamp, and a constraint honored into a plan *newer* than the one
coming back cannot be reflected by the restored rows. One timestamp comparison, in
`db.clear_honored_after` — called from inside `restore_workout_batch`'s transaction so the two
halves of a restore cannot diverge, and returning what it cleared so the caller can name it.

It is **not** announced before the `y`. The cost of the flag being cleared is one nudge, which
is not worth a reader, a lifted batch-stamp query and two threaded confirm prompts to pre-empt;
the rollback simply reports what it un-honored once it has. `cli/common.py: report_unhonored`
renders that line for both rollbacks.

Rollback is its own inverse, but the clear survives rolling forward again — deliberately
unrestored, because a false "unhonored" costs a nudge and a cheap re-pass that re-stamps, while
a false "honored" hides a real gap. The converse case needs nothing: an honoring older than the
batch was already part of the plan being restored.

## 3. Coverage is decided at proposal time

Which constraints a pass covered is decided once, when the proposal is built, and carried on it
(`covered_constraint_ids`, on both proposal types). Apply stamps that list rather than
re-deriving it — the discipline `coach/proposals.py` exists for: a constraint edited between
preview and `y` is a new window no pass has covered, and a re-fetch at apply time would stamp it
anyway.

Deciding it there also settles a question that looks like a missing field. Coverage for generate
is checked against the *written* range, and `GenerateProposal` carries no `gen_end` — but it does
not need one: `workout_generate` has both `gen_start_str` and `gen_end_str` in scope where the
proposal is built, so the comparison happens there and the proposal carries only the resulting id
list. Adding a range field to reach it later would be re-deriving what this paragraph exists to
stop.

## 4. One predicate, four surfaces

*Does the plan reflect this directive yet, and is there anything to be done about it?* —
`honoring.needs_a_pass`, read by the `status` line, `constraint list`, `constraint show` and
the add-time nudge. Four terms, cheapest first:

| term | why |
| --- | --- |
| `honored_at IS NULL` | a pass has already had it in scope |
| `replan = 0` | the plan-shaping tier owns it — `plan generate` builds it in |
| the window is non-empty | only adapt's own day is left of it |
| **the window holds at least one session** | **nothing to reshuffle** |

The window is the constraint's own dates clipped to **tomorrow**: today is the day
`workout adapt` judges with the full metrics picture, and a directive whose last day is today
belongs to that run, not to a nudge about the future.

The last term is what keeps the nudge honest. A window with no sessions in it has nothing to
move, so naming it is a prompt the athlete can act on in no way at all. It also silently covers
the case that used to nag forever — a constraint past the plan's end has no sessions in its
window, because nothing has been generated there yet.

**That these surfaces ask one function is the whole point, and the first pass did not do it.**
The rule was written out at each site — a SQL `WHERE` in one reader, an early return in the
nudge, a date comparison in each of the two renderers — and the renderers were written without
the `replan` term, so `constraint show` flagged exactly the directives the others deliberately
skip. The predicate has one owner in `coach/honoring.py`; there is deliberately no SQL half-copy
of it in `db/constraints.py`, since that layer cannot see `coach` and a partial copy is what went
wrong. `tests/test_constraints.py` drives all four surfaces over the same three constraints and
asserts they agree — a rule that spans files, tested across them.

### 4.1 The add-time message

`cli/constraints.py` prints today only when the magnitude heuristic fires; below that threshold
it says nothing about *when* the constraint takes effect. When the constraint's window *ends*
after the active block's end — landing wholly beyond it, or straddling the boundary:

```
Lands in Build 2 (2026-09-14 — 2026-10-04), outside daily adapt's reach.
Leave it — adapt reaches it on 2026-09-14 — or build it in now with `workout generate -m 8`,
which rebuilds the plan from today through that block's end.
```

**The block named is the one holding the constraint's LAST day, and the three cases are not one
message.** Which days are out of reach is decided by where the window *ends* — that is the test
this branch fires on — so asking which block the window *starts* in answers a different question,
and answers it wrongly for the straddling case: such a constraint starts in the current block, so
it would name the block adapt reaches *today* and then offer that block's first day — already
past — as the day adapt will get to it. Both lines contradict themselves.

So the straddle gets its own wording, and it is a better pitch than the corrected date would have
been. Adapt does not reach a straddling constraint *later*; it reaches it in two halves and never
as one:

```
Straddles the end of Build 1 (2026-09-14): daily adapt honors the days up to there,
Build 2 holds the rest, and no one run sees both.
Build the whole of it in with `workout generate -m 8` — that rebuilds the plan from today
through 2026-10-04.
```

Naming the block that holds the **last** day is also what makes one run enough: generation always
starts today (DESIGN_cli_selectors.md §8), so `-m` on the later block covers every block before
it too.

**It is not a branch appended to `_maybe_replan`, and that is not a detail.** That function
returns early four times before it reaches its own heuristic — on `--no-replan`, on `--replan`,
on a missing constraint, and on a magnitude *below* the threshold. The last of those is the common
case and is precisely the case this message exists for: the athlete adds "away the 14th to the
24th", it does not trip the replan bar, `_maybe_replan` returns, and anything written at the
bottom of that function never runs. It would be dead code exactly where it is needed.

So it is its own function, called by `run_constraint_add` and `run_constraint_edit` immediately
*after* `_maybe_replan` — after, because the replan proposal may change the answer.

## 5. Why there is no command of its own

There was one: `workout accommodate`, a third tier between `adapt` and `--replan` that
re-arranged the sessions in a constraint's own window (its dates ± a small spill margin), read no
metrics, and reused adapt's write path. It shipped, and it was removed. What follows is why, so
the case is not re-argued from scratch.

**`workout generate` already honors constraints, by the same two mechanisms.** Rest windows are
forced deterministically by `_enforce_rest_windows_generate`; advisory constraints reach the model
through the shared ACTIVE CONSTRAINTS prompt section; and `get_constraints(gen_start)` is
open-ended, so every stored directive from today onward is in scope. That is not a weaker
honoring than a dedicated tier's — it is the same one.

**On plan quality it is the stronger tool.** A window pass was metric-blind on purpose and could
only rebalance ±3 days, so displaced load had to fit in the margin or be dropped. Generate reads
metrics, PMC, baseline, intensity distribution and the block's focus, and re-lays whole
microcycles around the constraint.

**The tier's exclusive domain was narrow.** To be its case a constraint had to be past adapt's
reach, *and* below the replan bar (`replan_rest_span_days`, `replan_displaced_load_pct`), *and*
have sessions in its window: a one-to-two-day rest window or a light advisory cap, more than a
block out. Anything more disruptive escalates at add time; anything nearer is adapt's.

**What it bought was not rewriting the intervening weeks.** Generation always starts today, so
building a constraint in three weeks out rebuilds the plan from here to there, discarding
accumulated adaptations and replacing any hand-added sessions (`workout_generate_apply` names
them, and `workout rollback` restores them). That cost is real. It is an argument for letting
`workout generate` take a start bound — a general capability, cheap now that
DESIGN_workout_revisions.md has made every write an append with a lineage — and not an argument
for a second command with its own prompt, its own window arithmetic, its own proposal types and
its own judgement model.

The visibility half is what actually closed the complaint in §1, and it is what remains.

## 6. Deliberately not done

- **A window-scoped `workout generate`.** The right shape for the cost named above, and a change
  worth making on its own merits rather than folded into this one. It no longer conflicts with
  the write model: `archive_future_workouts` is gone, and `workout_generate_apply` bounds its
  void pass with a single `get_workouts(start_date=…)` call that an end date would join.
- **Escalating to `replan` automatically.** DESIGN_constraints.md §7's posture holds: nothing
  sets `replan = 1` without a human `y`.
- **Running anything automatically after `constraint add`.** §4.1 points at the command; the
  athlete runs it. Same confirm-before-acting posture as everything else that writes sessions.
- **Warning about un-honored constraints before a rollback's `y`.** §2. The flag being cleared
  costs a nudge; pre-announcing it cost a reader, a lifted query and two threaded confirm
  prompts. The rollback reports it afterwards instead.
