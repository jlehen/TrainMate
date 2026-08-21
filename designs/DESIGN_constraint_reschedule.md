# Design: honoring a constraint the daily adapt cannot reach

## 1. The problem

A constraint dated outside the current mesocycle is stored, is never lost, and is eventually
honored — but nothing acts on it now, and nothing says so.

`coach/service/adaptation.py::workout_adapt` pins its forward range to the block containing the
evaluation date:

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

The existing escape hatches cover the two extremes and leave the middle open:

| the constraint | what happens today |
| --- | --- |
| falls inside the current block | daily `adapt` honors it |
| trips the §7 magnitude heuristic | the replan proposal offers a full regen |
| anything else past `meso_end` | nothing, until adapt's window rolls onto it |

Three costs follow.

**It is honored late, silently.** Up to a block-length late. For "no run that Thursday" late is
fine; for "away the 10th to the 20th" the calendar shows sessions the athlete already knows they
will not do, for weeks, with no indication that the coach has registered the fact.

**A constraint straddling the boundary is honored in part.** `get_constraints` returns it — the
query is an overlap, not a containment — so a ten-day trip starting three days before the block
ends has its first three days rested and its remaining seven ignored. Nothing marks the seam.

**The only workaround is disproportionate.** `workout generate` always starts today
(DESIGN_cli_selectors.md §8, "generation always starts today"), so re-planning the week that
contains the constraint rewrites every session between here and there — discarding the
adaptations the intervening weeks have accumulated, and paying for a full generation to move two
sessions.

## 2. Why not simply extend adapt's horizon

DESIGN_block_boundary.md §5 lists *extending the adaptation range past `meso_end`* under
**Deliberately not done**, and that entry stands. This design does not overturn it; it goes
around it.

The firewall in §2 of that doc is not about the *range*. It is about what would ride along with
it. Adapt's entire input is a backward window of recovery metrics — HRV, RHR, sleep, PMC,
adherence — and its output is a load judgement made from them. Give that judgement a longer
reach and this morning's HRV gets authority over a session four weeks out, where recovery data
has no predictive claim at all. The guard against it would be a prompt instruction, which the
model can quietly not follow.

The distinction that lets a command cross the boundary safely:

> **Adapt reacts to something inferred. This reacts to something declared.**

A constraint is a dated fact the athlete typed in. Honoring it needs no metrics, makes no fitness
judgement, and cannot be wrong about the athlete's state because it never consults it. That is
why a *separate* command may reach past `meso_end` while `workout adapt` still may not, and why
this one deliberately does not read metrics at all (§6) — the moment it does, §2's argument
applies to it too.

## 3. Mental model — the third tier

DESIGN_constraints.md §7 gives a constraint two possible fates. This adds a third between them:

| tier | trigger | scope | cost |
| --- | --- | --- | --- |
| tactical | daily `adapt` | current block only | free, already running |
| **window** | **this design** | **the constraint's own dates** | **one small LLM call** |
| plan-shaping | `--replan` | the whole plan | `plan generate` + `workout generate` |

The middle tier is the one the athlete actually reaches for most: a directive that is real, is
dated, is too far off for adapt and too small for a replan. It reshuffles sessions around a
declared obstacle. It does not re-periodize anything, and it does not touch the strategy.

The `replan` boundary is unchanged and stays the athlete's call: the magnitude heuristic
(`config.replan_displaced_load_pct`, `config.replan_rest_span_days`) still proposes escalation at
add time, and this command never escalates on its own. A constraint can be honored in its window
today and escalated to plan-shaping later; the two are independent.

## 4. Command surface

One command. Three ways to name its subject; the window each constraint gets is the same
however it was named.

```
workout accommodate                       # every unhonored constraint from today to plan end
workout accommodate -m 7                  # those landing in block 7
workout accommodate -d 2026-09-10..09-20  # those overlapping an explicit range
workout accommodate -c 47                 # that one, honored already or not
```

**`workout accommodate`** takes the reserved selector vocabulary unchanged
(DESIGN_cli_selectors.md §5) — `-d`, `-m`, `-M`, `-g` mean here exactly what they mean
everywhere, and the tree-walking invariant in `TestSelectorVocabularyInvariants` covers it for
free. Its `_selector_policy` is `("forward", None, None)`: forward, with no span default, because
the no-argument case is not "the next 7 days" but §4.1 below.

The verb is `accommodate` and not `reschedule` because of the prefix namespace, not the prose.
Every command resolves by unambiguous prefix (DESIGN_cli_noargs.md §d), and `workout` already
holds `restore`; adding `reschedule` would make `re` and `res` ambiguous and break `w res`, the
spelling ARCHITECTURE.md §7 documents. `accommodate` collides with nothing: `a` stays `adapt`
(a registered alias, and aliases resolve before prefixes), `ad` stays ambiguous between `add`
and `adapt` exactly as today, and `ac` is free. `honor` was the other clean prefix but reads
wrong in this namespace: `workout <verb>` supplies its object from the group, so `workout honor`
parses as "honor the workout" — plausible, since a session is commitment-like, and therefore
misleading. "Accommodate a workout" is a poor enough fit that the phrase reads as incomplete and
sends the reader looking for the real object, which is the constraint. §13 records the rest.

A new verb rather than a flag on `adapt`, for two reasons. The inputs differ — a flag would make
one command mean two things depending on whether an argument was present. And `workout adapt -m`
is a *declared exception* to the reserved vocabulary (DESIGN_cli_selectors.md §5): it is
`--message`, because adapt "acts on days, not on blocks". Teaching adapt a block selector means
retiring that exception and breaking the flag on the most-used daily command.

**`-c ID…`** names the subject instead of a window to find it in, and carries the one
behavioural difference in the command: every other form filters to constraints the plan does
not yet reflect (§8), while naming one is an explicit imperative that runs even when it is
already stamped — re-honoring after hand-editing the window's sessions is a legitimate ask.
Because it answers the same question as `-d`/`-m`/`-M`/`-g` by a different route, combining
them is refused rather than resolved by a precedence rule nobody would remember.

`-c` is not part of the reserved selector vocabulary and does not take its `A..B` grammar: a
constraint ID is not a span, and the flag picks the subject rather than bounding a window.
It is registered in `tests/test_cli_selectors.py`'s letter map so `-c` cannot come to mean
anything else elsewhere.

**This was two commands until it was not.** `constraint honor 47` was the second door —
`constraint`-side, positional ID, same service call — and it earned its place on the argument
that the athlete reaching for this after typing `constraint add` looks under `constraint`.
It cost more than it returned. The two parsers had already drifted (`--show-llm-prompt-only`
was on one and not the other), which took a tree-walking parity test to hold shut; the flow
needed a module owned by neither family to break the import cycle; and the difference the
second door actually carried — run it even if stamped — is one flag's worth of behaviour, not
a command's. `-c` keeps that behaviour and deletes the surface. §13 records what the doors
were traded for.

### 4.1 The no-argument sweep

A bare `workout accommodate` finds every constraint from today to the end of the last governing
block that the plan does not yet reflect (§8), and offers them for honoring, one pass at a
time.

This is the case that matters. In the scenario this design exists for, the athlete does **not**
know which block the constraint landed in — that is the entire surprise. A command that demands
`-m 7` still requires them to have worked out that it is block 7.

Per DESIGN_cli_noargs.md §b, the defaulted target is named rather than assumed:

```
No window given — checking 2 constraint(s) your plan does not reflect, through 2026-11-30.
```

If there are none, it says so and exits without an LLM call.

Each unhonored constraint is honored in its own §5 window — one pass per constraint, run
sequentially: preview, confirm, apply, then the next, so every pass is judged against the plan
as the previous apply left it. Two constraints whose spill-widened windows overlap are merged
into a single pass, because a second pass over shared days would preview a plan the first had
not yet applied. Either way a proposal's `range_start`/`range_end` is exactly the window its
own pass evaluated, which is the contract those fields carry (§7).

Nothing caps how many passes a sweep runs, and nothing should: the cap the athlete wants is
"however many constraints I have", and each pass confirms separately anyway. What the sweep
does owe them is knowing the size of what they just asked for *before* it is spent, so a run
with more than one pass says how many — one coach call each, and under `-y` one write each.
The common case is one, and it says nothing.

### 4.2 The refusals are typed, not inferred

A constraint no block covers cannot be honored (§5), and neither can one whose remaining
window is empty because it ends today — only adapt's day is left of that one. Both are named
rather than skipped silently, so "checked through 2026-11-30" is never read as "checked
everything".

**Which of them applies is the service's answer, not the caller's guess.** One call,
`coach_service.accommodation_plan(today, constraints)`, returns an `AccommodationPlan`: the
`passes` that will run, the `ungoverned` that cannot, the `spent` that adapt owns, and the
`plan_end` all three were judged against. The CLI renders those buckets and loops over
`passes`; it decides only *which* directives are in scope, because two of the three ways of
naming them are argparse-shaped.

It was the other way round first, and the cost was a wrong message on a real case. The CLI
worked out a horizon of its own — the last day of any block covering today onward — and
called anything starting past it too far out; the service worked out a different one from
the blocks overlapping each individual window. A constraint landing in a **gap** between two
blocks passes the first test, because it starts well before the plan's end, and fails the
second. So it became a pass, spent nothing, and died mid-loop on *"No active periodization
strategy found"* — which was untrue, and pointed at the wrong fix. One bucket, filled by the
side that knows, covers the gap and the far end alike: *"1 constraint has no block in your
plan to reshuffle towards (it runs to 2026-12-31); run `plan generate` to cover their dates
first"*.

Governance is asked of the **merged** window rather than of every day in it, and it is asked
**once**: `accommodation_plan` resolves the governing blocks per pass and carries them on the
`AccommodationPass`, and both the CLI's "plan runs out" warning and `workout_accommodate`
read that list rather than asking again — two sites answering "does the plan cover this?"
for themselves is exactly the shape that produced the gap bug above. A pass with any
governed day runs, over its whole window (§5); a pass is refused only when nothing in its
window is planned against a block at all.

The per-pass `ValueError` stays, because `workout_accommodate` is callable on its own and
must still refuse a window it cannot act on. The CLI keeps catching it, now as a backstop
for a world that changed under the plan rather than as the ordinary path.

## 5. Scope — what it may touch

The window is the constraint's own dates, widened by `config.accommodate_spill_days` (default
**2**) on each side, clipped to **tomorrow** on the near side (`max(start, today + 1)`). The
past is excluded for the same reason §7 of DESIGN_constraints.md clips the displaced-load sum:
a plan cannot be reshaped around what already happened. Today is excluded on this command's own
grounds: today is the day `workout adapt` judges with the full metrics picture, and a
metric-blind command must not race it there.

The spill margin exists because displaced load has to land somewhere, and the day before and
after a three-day trip are usually where it goes. It is bounded and small on purpose. **Without a
bound this command becomes the horizon flag wearing a different name** — any window it may
rebalance freely is a window in which it is re-periodizing, which is §2's objection all over
again. Two days either side can absorb a moved session; it cannot restructure a block.

That clip is also why adapt's completed-session lock is not needed here: `completed_keys`
guards the evaluation day the athlete may already have trained, and this window never contains
it.

Every constraint overlapping the widened window — not only the one being honored — is fetched,
for the prompt and the rest pre-pass alike: a spill day may belong to a neighboring rest
window, and a pass that cannot see that constraint would move load onto it.

The blocks governing the window are resolved once, in `accommodation_plan` (§4.2), ride on
the pass, and reach the prompt through `_coach_context(constraints, blocks=…)`. Two
consequences, both wanted: a constraint spanning two
blocks is one pass rather than two, which is the straddling case from §1 fixed by construction —
with both blocks' name and focus in context, so the model knows which side of the seam displaced
load belongs on; and there is still exactly one context assembler, not a second way to build a
coach prompt.

If no block governs any of the window, the command refuses with the same message
`workout_adapt` and `workout_generate` raise. Ungoverned dates can still hold sessions —
`workout add` needs no plan, and generate writes past the plan's end under a warning — but a
window that overlaps no block at all was never planned against one, and there is nothing to
reshuffle it *towards*. There is a trap in the reader, and the fix belongs in the reader
rather than here: `get_governing_mesocycles` falls back to a block that does *not* cover the
window when nothing overlaps — right for generate, which uses it to lay sessions near a
plan's edges and to make an empty result mean "no plan at all", wrong here. So this asks the
strict reader, `get_covering_mesocycles`, rather than re-checking the answer afterwards
(§11).

A *partially* governed window (a constraint straddling the plan's end) is evaluated **in
full**. The days past the plan's end have no block to guide what lands on them, but a
session already sitting there in conflict still has to be cleared or capped, and the
constraint's own dates are authority enough for that — §2's warrant is the declared fact,
not the block. The CLI says so before the call is spent, off the pass's blocks ("the plan
runs out on X, before this window ends"), and the pass then covers the whole window, so per
§8 the constraint **is** stamped. An earlier draft clipped `range_end` to the plan's end and
withheld the stamp so the sweep would re-offer the constraint once the plan was extended —
but the re-offer arrived long before any extension: the governed days still held sessions,
so every bare sweep re-ran a pass that could propose nothing new and could never stamp, one
LLM call per run, indefinitely. And the clip protected against nothing: `workout generate`
builds around every stored constraint its span overlaps, stamped or not, so the days a
`plan generate` later grows the plan into are reshaped around the directive by the command
that writes their sessions.

## 6. Inputs — no metrics, on purpose

`workout accommodate` does not read `get_metrics_cache`, the PMC context, the baseline, or the
intensity distribution. Its trigger is a dated fact, not a reading, and §2 is only true of it for
as long as that holds.

The cost is real and worth naming: **it cannot tell you the reshuffle it just made is unwise
given how you are actually recovering.** Moving Saturday's long ride to Sunday to clear a
constraint may stack it against a hard Monday. Nothing here will notice.

That is acceptable because of where the sessions are. A window inside the current block is
adapt's, and adapt runs daily with the full metrics picture — so the load-aware second opinion
arrives on its own. A window three weeks out has no meaningful metrics to consult: today's HRV
says nothing about a Tuesday in October, which is §2's whole point. The command is metric-blind
precisely where metrics are blind too.

## 7. The write model — an adapt sibling, not a generate sibling

This is the mechanical fact that settles where the code lives.

`workout_generate` is archive-and-rebuild: `_archive_and_teardown(from_date)` →
`db.archive_future_workouts(from_date)`, with `restore_workout_batch(archived_at, from_date)`
symmetric to it. Both are **"from a date onward"**, and DESIGN_plan_rollback.md §9 keeps them
that way deliberately — the date floor is what stops a rollback resurrecting rows onto days that
have since passed. A window-scoped rewrite has no representation in that model, and giving
archival a start *and* an end would re-open a case that doc closed on purpose.

`workout_adapt_apply` has no such problem: it edits rows in place, stamps `modification_reason`
per session and `adaptation_summary` per batch, deletes only sessions its own range overrides,
and re-syncs Calendar per event. A reschedule is exactly that shape.

So `workout accommodate` **reuses `workout_adapt_apply` unchanged**, handing it a proposal
whose `range_start`/`range_end` are the §5 window. That inherits, for free, the reason those
proposal objects exist at all (`coach/proposals.py`): preview and apply agree about what
disappears because they are handed the same range, rather than one of them re-deriving it from
the span of the returned proposals.

There is one filter adapt does not have and this needs. `workout_adapt` clamps only the *top* of
its range (`w['date'] <= meso_end_date_str`), because its window starts at the evaluation date
and nothing can land below it. This window starts *tomorrow*, so a proposal dated today — the day
§5 excludes precisely so a metric-blind pass cannot race adapt — would sail through. The mixin
clamps both sides: `range_start <= date <= range_end`. §5's clip is a statement of intent; this
filter is what enforces it.

Two adjustments to the shared path:

**It never stamps `adapted_at`.** That column drives the DO NOT COMPOUND guard — how recently and
how often a session was *eased*, so repeated cuts do not stack. DESIGN_intensity_distribution.md
§9.5 already established the principle for drift corrections: a rewrite that does not cut load is
not an easing, and stamping it would raise the compounding bar for a session that was never cut.
A constraint-driven move is the same case, and more strongly so — the trigger is not fatigue at
all, so a reschedule must never make tomorrow's genuine easing look like a compounded one. Note
the existing `eased` test would get this wrong if inherited unchanged: a session moved to a new
date has no same-sport predecessor there, and `eased = not existing` would stamp it. The cost is
accepted knowingly: a reschedule that genuinely cuts a session — a "45 minutes only" constraint
shrinking a 90-minute ride — leaves no [ALREADY EASED] tag for a later adapt to hold or restore
against. `modification_reason` still names the cause, and a cut that fatigue did not drive is
exactly the one the compounding guard has no business protecting.

The opt-out rides on the **proposal**, not on the apply signature: `RevisionProposal` carries
`stamp_adapted_at: bool = True`, and the two commands differ only in what they construct. The
reason is that the producer knows and the consumer does not: `workout_revision_apply` is called
from two places, and an argument there is one each of them can forget, silently stamping. On the
proposal it is decided once, where the command that built it is in scope, and no call site can get
it wrong. The DB side needs nothing — `save_workout` already does
`adapted_at = COALESCE(?, adapted_at)`, so passing `None` is a clean no-op.

**One rule keeps that from turning the record into a union of two commands: every field
either producer does not fill has a default, so a producer names only what it has.** A
per-command flag is fine — `stamp_adapted_at` is a fact about the pass that built the
proposal, and it is the producer that knows it. What is not fine is a field a producer has
to explicitly opt out of: `new_constraints` (candidate directives extracted from the
athlete's note) belongs to adapt alone, and a metric-blind window pass with no note to read
should say nothing about it rather than declare an empty list. Once a record has two or
three fields that one side only ever zeroes, it has stopped being a shared shape and become
a discriminated union with the tag left off. A field that has to be defaulted rather than
zeroed is the same field with the question answered where it is known.

**The rest pre-pass is reused as a backstop, not as a fast path.**
`_enforce_rest_windows_adapt(adapted, planned, constraints, completed_keys, from_date)` is
already a classmethod bounded by the constraints handed to it, not by the caller's block.
Pointed at the §5 window it needs no change: every `rest = 1` day in range becomes a rest
session deterministically, whatever the model returned. The LLM is still always called, though —
a rest window *displaces* load, and deciding what fits into the spill days is the model's
judgement (standing rule 1); the pre-pass guarantees the rest days regardless of what comes
back, it does not decide where the displaced work goes. An earlier draft skipped the call for
purely `rest = 1` windows, which quietly deleted the displaced load — the exact outcome the
spill margin exists to avoid. A trip too long for anything meaningful to be re-placed has
usually already tripped the rest-window floor of DESIGN_constraints.md §7
(`replan_rest_span_days`, default 3) at add time, so its escape is the replan proposal, not a
silent fast path.

Sharing the path also renames it. Once two commands produce and apply the same in-place
proposal, the shared pieces stop carrying adapt's name: `AdaptProposal` → `RevisionProposal`,
`workout_adapt_apply` → `workout_revision_apply`, `pair_adaptations` → `pair_revisions`,
`_enforce_rest_windows_adapt` → `_enforce_rest_windows_revision`. "Revision" is this section's
own distinction given a name — the in-place sibling of a *generation* — which is why the
`_generate` twin keeps its suffix. `workout_adapt` itself keeps its name: it is a command, not
the shared machinery.

## 8. `honored_at` — the signal the sweep reads

The no-argument sweep needs to answer "does the plan reflect this constraint yet?". The workouts
table carries no per-row `updated_at`, so the answer cannot be inferred from timestamps, and
inferring it from content only works for `rest = 1`.

One new column, `constraints.honored_at` (UTC ISO, nullable) — a one-off migration, no backward
compatibility, per AGENTS.md.

**Set** on successful apply by any command that wrote sessions across every remaining day of
the constraint's window — the window clipped below to the first day the pass itself had
authority over: the evaluation date for adapt, tomorrow for a reschedule (§5), `gen_start` for
generate. No command has authority over days already behind it, and demanding the literal whole
window would leave any constraint already under way flagged forever, since nothing could ever
cover its past again. The writers: `workout accommodate`,
`workout_generate_apply` for constraints whose clipped window sits inside `gen_start..gen_end`
(generate's constraint fetch is open-ended, so coverage is checked against the written range,
not the fetched set), and the adapt path for constraints whose clipped window sits inside the
adaptation range. The last of those is what keeps the sweep quiet about constraints daily adapt
is already handling, and it splits across the preview/apply seam: a run that proposes changes
stamps only on the athlete's `y` (`workout_revision_apply`) — a declined proposal reflects
nothing — while a run that proposes *no* change never reaches apply at all. Requiring a *change*
would leave "no adaptation needed" flagged forever, so the no-change branch stamps too, through
`workout_revision_apply`'s sibling `workout_revision_record_no_change`.

**That stamp is a write, so it is on the write side.** An earlier draft put it inside
`workout_adapt`, which builds a proposal and is supposed to write nothing — and the cost was
immediate: the accommodate flow has the same branch, nobody noticed it needed the same call, and
a pass that answered "the plan already works around this" left the constraint unstamped, so the
sweep re-offered it forever at one LLM call a run. Both no-change branches now call the one
recorder, and `tests/test_service_invariants.py` fails any method returning a `*Proposal` that
touches the database, so the shape cannot come back.

The stamping rule itself — the predicate, the ids, the write — lives in `coach/honoring.py`
rather than being restated at each of the four sites that claim it. Nothing else owns the
column's meaning, and the bug above is what a rule with no owner looks like.

Which constraints a pass covered is decided once, at proposal time, and carried on the proposal
(`covered_constraint_ids`, on both proposal types) — apply stamps that list rather than
re-deriving it, the discipline `coach/proposals.py` exists for: a constraint edited between
preview and `y` is a new window no pass has covered, and a re-fetch at apply time would stamp
it anyway.

Deciding it at proposal time also settles a question that looks like a missing field. Coverage
for generate is checked against the *written* range, and `GenerateProposal` carries no `gen_end`
— but it does not need one: `workout_generate` has both `gen_start_str` and `gen_end_str` in
scope at the point the proposal is built, so the comparison happens there and the proposal
carries only the resulting id list. Adding a range field to reach it later would be re-deriving
what this paragraph exists to stop. Both dataclasses are frozen, so the new field is appended
with a default and populated at construction.

**Cleared** by `constraint edit` whenever `start_date`, `end_date`, `rest`, `title` or
`description` changes. The window moved, or the directive changed — and for an advisory
constraint the prose *is* the enforcement mechanism (DESIGN_constraints.md §5), so new words
are a new directive a previous honoring says nothing about.

**Cleared, too, by a rollback that resurrects a plan older than the honoring.** Both rollbacks
restore a batch keyed by its `archived_at` stamp, and a constraint with
`honored_at > archived_at` was honored into a plan *newer* than the one coming back, so the
restored rows cannot reflect it. One timestamp comparison, in `db.clear_honored_after` — the
constraints mixin's SQL, called from inside `restore_workout_batch`'s transaction so the two
halves of a restore cannot diverge, and returning what it cleared so the caller can name it.

It is **not** announced before the `y`. The cost of the flag being cleared is one nudge and a
cheap re-pass, which is not worth a reader, a lifted batch-stamp query and two threaded confirm
prompts to pre-empt; the rollback simply reports what it un-honored once it has. `cli/common.py:
report_unhonored` renders that line for both rollbacks.

Rollback is its own inverse, but the clear survives rolling forward again — deliberately
unrestored, because a false "unhonored" costs a nudge and a cheap re-pass that re-stamps, while
a false "honored" hides a real gap. The converse case needs nothing: an honoring older than the
batch was already part of the plan being restored.

**Read** through one predicate, `honoring.needs_a_pass` — *should the window tier be
offered for this directive?* — by the sweep, by `constraint list`/`show`, by the add-time
nudge and by the `status` screen. Four terms:

| term | why |
| --- | --- |
| `honored_at IS NULL` | a pass has already had it in scope |
| `replan = 0` | the plan-shaping tier owns it (below) |
| the §5 window is non-empty | only adapt's own day is left of it |
| **the window holds at least one session** | **nothing to reshuffle** |

The last term is the one that keeps the nudge honest. A window with no sessions in it has
nothing to move, so naming it is a prompt the athlete can act on in no way at all: running
the command would spend an LLM call to be told the obvious. It also silently covers the
case that used to nag forever — a constraint past the plan's end has no sessions in its
window, because nothing has been generated there yet.

The `replan = 0` term is the third tier staying in its lane. A constraint escalated to
plan-shaping belongs to `plan generate` + `workout generate`, which stamp it when their horizon
reaches it; until then it is unstamped but it is not *unhandled*.

**That these five surfaces ask one function is the whole point, and the first pass did not
do it.** The rule was written out at each site — a SQL `WHERE` in the sweep's reader, an
early return in the add-time nudge, a date comparison in each of the two renderers — and
the two renderers were written without the `replan` term, so `constraint show` recommended
`workout accommodate -c 47` for exactly the directives the sweep refuses to offer. The
predicate has one owner in `coach/honoring.py`; there is no SQL half-copy of it in
`db/constraints.py`, deliberately, since that layer cannot see `coach` and a partial copy
is what went wrong. `tests/test_constraints.py` drives all four surfaces over the same
three constraints and asserts they agree — a rule that spans files, tested across them.

What the column means, stated plainly so nobody reads more into it: *a coach pass had this
constraint in scope, with authority over every day of it still ahead.* Not *the plan definitely
changed*.
That is the same warrant `workout generate` gives for every other constraint it built around, and
it is a nudge's worth of precision, which is all the sweep needs.

## 9. Prompt

A new TASK in `coach/engine/workouts.py`, not a branch inside `_workout_adapt_logic` — that
function already carries one always-on body plus five conditional sections, and
DESIGN_adapt_task_prompt.md §1 is the record of what happens when sections accumulate there.

It is small: the sessions in the window, the constraint text (every constraint overlapping the
window, §5), the governing blocks' names and focus, and standing rules 1 and 2 from
DESIGN_adapt_task_prompt.md §2 — *move before you ease, ease before you delete*, and *the block
is not yours to reshape*. Both are load-bearing here. Rule 1 is the entire job. Rule 2 is what
stops a reshuffle becoming a re-periodization, and it is the same firewall §2 above describes
from the other side.

**Rule 2's evidence clause is parametrized, not copied and not dropped.** Its first and last
sentences ("you adapt the sessions inside it", "when you do believe the block itself is wrong,
say so in `reason` and leave it alone") transfer verbatim. Its middle names the signals that are
*not* evidence a block is too hard — "a depressed morning, a note, a drift reading" — and a
metric-blind command sees none of the three. Copied as-is it would argue against evidence this
prompt never receives; dropped, rule 2 loses the half that does the work. So the clause takes its
signal list as an argument, the way `_terminal_window_task` already switches its "ends today" /
"ends in N days" phrasing: adapt keeps its three, and this command names its own — *a
constraint, however disruptive*. That is the more honest wording here anyway, because the
failure mode this rule guards against is exactly "the athlete is away for ten days, so let me
rebuild the block around it".

Only those two rules are hoisted into constants, because only those two are *shared*. Adapt's
other three go to one prompt and stay written where they are read, at its own call site: a
constant per rule buys drift protection between two copies, and there is only one. An earlier
pass hoisted all five, which left three module-level constants whose only remaining reader was
a test asserting their absence from the other prompt.

Two mechanics ride along, because a preference order says nothing about how to encode a move
(DESIGN_adapt_task_prompt.md §2, "a rule does not displace a mechanic"):

**A vacated date is re-filled, never left empty.** Apply removes a displaced session only on
dates the response covers, so a move that emits only the destination leaves the original in
place — the session twice, on both days, and silently: the preview renders only what the
proposal targets, so the leftover never appears in the before/after table. The old date gets an
explicit replacement, normally rest, whose `change_reason` names the move.
Replacement-with-rest rather than a first-class delete is deliberate, and it is adapt's
existing mechanic: an empty date and a planned rest day mean different things to adherence
(DESIGN_constraints.md §6), so a hole is never the right encoding inside a governed span (§13).
This instruction is shared into adapt's TASK too, from the same helper: adapt has the same
latent gap today — only its benchmark section states the encoding, and an ordinary moved
session relies on the model inferring the pattern by analogy.

It is its own named `###` section in both prompts, not a clause tucked inside the benchmark
text where the encoding currently lives. Two reasons. DESIGN_prompt_structure.md §4 leaves no
room for an unlabelled trailing paragraph under a `###` scheme, so it needs a name either way.
And the case it governs is *central* here rather than incidental: adapt's commonest action is
easing a session in place, which vacates nothing, while for this command moving is the entire
job. A rule that fires on almost every pass should not be discoverable only by reading the
benchmark section.

**The window can contain a benchmark**, and a constraint over a test day is an ordinary
reschedule ask. The PROTECTING A BENCHMARK section is extracted from the adapt TASK into a
shared helper — the treatment `_planned_zone_task` already gets — with its scope phrasing
parametrized, so the two prompts cannot drift apart. Without it, the shared apply path's
flag-clearing (DESIGN_benchmark_workouts.md §4.2) would blank a moved test the model forgot to
re-emit, or file its replacement as the test.

Three phrases are scope-bound, not one, and all three take the `scope` argument together —
the helper holds two wordings and picks by argument rather than accepting three separate
strings:

| adapt | here |
| --- | --- |
| "to a later day within THIS block" | "to a later day within this window" |
| "if it already sits on the block's LAST day and no later in-block day exists" | "if it already sits on the window's LAST day and no later day in the window exists" |
| "the next generated block re-places the test when it is due" | "the daily adapt or the next generated block re-places it" |

The third matters most and is the easiest to miss: adapt's postponement escape is honest
because a block boundary really does bring a `workout generate` that re-places the test. A
window sitting mid-block has no such guarantee, so promising one would tell the model a
postponement is cheaper than it is.

The section's closing line — *"a benchmark you are NOT changing need not be returned at all"* —
travels with it, and must. `workout_revision_apply` infers `clear_benchmark` from a returned
change that drops the flag, and that inference is only sound because the prompt has told the
model an unchanged test may be omitted; without the line, an omission is ambiguous and the
clearing becomes a guess. It already sits inside the extracted text, so this costs nothing —
it just must not be trimmed on the way out.

No metrics section, no PMC, no intensity table, no adherence — §6. Section hierarchy and naming
follow DESIGN_prompt_structure.md.

## 10. Display, and closing the silence at add time

The preview is adapt's machinery with one difference: it renders **the whole window**, not only
the rows the proposal changes.

The block that draws adapt's table and runs its confirm (`cli/workouts/generate.py`, inline
inside `run_workout_adapt` today) is extracted into a shared helper, and whole-window versus
changed-rows is an argument to it rather than a fork. That is what makes this affordable: §5
bounds the window to a constraint's dates plus two spill days either side, so "all of it" is a
handful of rows. Adapt's range runs to `meso_end` — three or four weeks early in a block — and
it runs *daily*, so a full render there would bury the one or two rows that matter. Adapt keeps
its change-only table; `workout adapt --full` can opt into the other mode later, spelled after
`plan diff --full`, and costs nothing to add once the argument exists.

What the full window buys is not a guard on the model — §14 says why there is none — but an
honest confirm. The vacate failure in §9 is invisible in a changed-rows table by construction:
the leftover sits on a date the proposal never mentions, so it appears in neither `pairs` nor
`removals`. Rendered in full it is plainly a session listed twice, on the constrained day and
its destination, with the constrained day marked unchanged. The athlete sees it before the `y`.

Independently of the commands, `cli/constraints.py` gains the branch that removes the original
surprise. It prints today only when the magnitude heuristic fires; below that threshold it says
nothing about *when* the constraint takes effect. When the constraint's window *ends* after the
active block's end — landing wholly beyond it, or straddling the boundary, which §1 shows adapt
honors only in part:

```
Lands in Build 2 (2026-09-14 — 2026-10-04), outside daily adapt's reach.
Honor it now with `workout accommodate -c 47`, or leave it — adapt reaches it on 2026-09-14.
```

**The block named is the one holding the constraint's LAST day, and the three cases are not one
message.** Which days are out of reach is decided by where the window *ends* — that is the test
this branch fires on — so asking which block the window *starts* in answers a different question,
and answers it wrongly for exactly the case §1 names: a straddling constraint starts in the
current block, so it names the block adapt reaches *today* and then offers that block's first day
— already past — as the day adapt will get to it. Both lines contradict themselves.

So the straddle gets its own wording, and it is a better pitch than the corrected date would have
been. Adapt does not reach a straddling constraint *later*; it reaches it in two halves and never
as one, which is the §1 complaint stated to the athlete:

```
Straddles the end of Build 1 (2026-09-14): daily adapt honors the days up to there,
Build 2 holds the rest, and no one run sees both.
Honor the whole of it in one pass with `workout accommodate -c 47`.
```

And a window whose last day no block holds is two cases, not one. If its *first* day is governed,
the plan covers part of it and §5 honors that part — so it is offered, with `plan generate` named
for the rest. Only a window governed nowhere gets the refusal. Reading the start date for that
split is right here for the same reason it is wrong above: the question is what a pass can still
act on, which is a fact about where the window begins.

**It is not a branch appended to `_maybe_replan`, and that is not a detail.** That function
returns early four times before it reaches its own heuristic — on `--no-replan`, on `--replan`,
on a missing constraint, and on a magnitude *below* the threshold. The last of those is the
common case and is precisely the case this message exists for: the athlete adds "away the 14th
to the 24th", it does not trip the replan bar, `_maybe_replan` returns, and anything written at
the bottom of that function never runs. In the scenario the whole design is for, the branch
would be dead code.

So it is its own function, called by `run_constraint_add` and `run_constraint_edit` immediately
*after* `_maybe_replan`, and it re-reads the constraint first and asks `honoring.needs_a_pass`
of it — the same question the sweep asks. If the replan flow ended with `replan = 1` it says
nothing, because the constraint is being built into the plan and "honor it now" would point at
the wrong tier; likewise if nothing is scheduled in the window, because there would be nothing
to reshuffle. Otherwise, if the window ends past the active block's end, it prints the lines
above. After `_maybe_replan`, not before, because the replan proposal may change the answer.

That is worth shipping even if neither command is built: it converts a silent gap into a stated
one. `status` gains a matching line when the sweep is non-empty.

## 11. Where it lives

Not a file-by-file inventory: that is what `git log` and ARCHITECTURE.md §"Command
reference" are for, and a hand-written list of touched files is an inventory, which rots.
What is worth writing down is which module *owns* which rule, because that is the thing a
later change can get wrong without any test noticing.

| the rule | its owner |
| --- | --- |
| what `honored_at` means, who may stamp it, and whether the window tier should be offered at all (§8) | `coach/honoring.py` |
| the §5 window arithmetic — a constraint's dates ± spill, clipped to tomorrow | `coach/honoring.py`, because the predicate above needs it too |
| how a returned change becomes a workout row, for both revision commands (§7) | `coach/revisions.py::structure_revision` |
| what a proposal carries, so preview and apply cannot disagree | `coach/proposals.py` |
| the window resolution, the constraint fetch and the two-sided date clamp | `coach/service/accommodate.py` |
| which passes can run, and the typed reason each refusal is a refusal (§4.2) | `coach/service/accommodate.py::accommodation_plan` |
| where the plan's horizon is, asked once | `coach/service/accommodate.py::plan_horizon` |
| the shared write path both revision commands apply through (§7) | `coach/service/adaptation.py::workout_revision_apply` |
| the TASK, and the three sections shared with adapt (§9) | `coach/engine/workouts.py` |
| which constraints are in scope (the three ways of naming them), and rendering (§4, §10) | `cli/workouts/accommodate.py`, `cli/workouts/revisions.py` |

Three things elsewhere are worth naming because they are easy to miss:

- **`db/base.py` needs a `SCHEMA_VERSION` bump alongside the new column**, not just the
  `_add_column` call: `_init_db` returns early on an up-to-date stamp, so without the bump
  an existing database skips the ALTER and fails on the first read.
- **`types.py`'s `Constraint` gains `honored_at` declared last**, because the column is
  appended by an ALTER and `tests/test_types.py` pins declaration order against
  `PRAGMA table_info`.
- **`db/periodization.py` splits its readers by the question, not by a flag.** The
  governing readers (`get_governing_mesocycles`, `get_active_mesocycle`) fall back to a
  *neighbouring* block when nothing overlaps — right for `generate`, which lays sessions
  near a plan's edges — and the covering readers (`get_covering_mesocycles`,
  `get_covering_mesocycle`) answer strictly or not at all, for every caller that treats
  the answer as covering the days it asked about. The first pass of this design re-checked
  the fallback's answer by hand at each new call site, which made five copies of one
  workaround; the second made strictness a `fallback=False` argument, which made every
  call site (and every review of one) re-derive what the boolean meant there. A name each
  site reads as what it means is the third shape, and the one that stays.

## 12. Tests

The behaviour is covered in `tests/test_accommodate.py` (the window, the scope, the write
model, the passes, the preview, the honoring) and `tests/test_constraints.py` (the column
across add / honor / edit / generate / adapt / rollback). Rather than list them, here are
the four that are load-bearing — the ones whose absence would let a known bug back in:

- **A proposal dated *today* is dropped by the two-sided clamp.** §5's "adapt owns today"
  is a statement of intent; that filter is the only thing enforcing it.
- **The preview renders every day of the window**, so a move that emits only its
  destination shows the session on both dates. This is the §10 claim, and the one that
  makes the confirm honest.
- **All four surfaces agree on which tier owns a directive** (§8), driven over the same
  constraints in one test. A rule that spans files needs a test that spans them; this one
  had already shipped broken once.
- **Structural, in `tests/test_service_invariants.py`:** a method *returning* a `*Proposal`
  never writes to the database, and a method *taking* one always records the coach pass.
  Both were prose, and both were violated. They key on the annotations rather than on a
  list of names, so a command written tomorrow is covered tomorrow.

`tests/test_prompt_gates.py` asserts which rules each of the two TASKs is given, against
the constants they are built from rather than against quoted prose — rewording a rule
should not break a test about which rules are present.
## 13. Alternatives considered

**A `--horizon` / `--through` flag on `workout adapt`.** §2. It carries the metrics-driven load
judgement across the boundary, which is the thing DESIGN_block_boundary.md §2 forbids, and the
only guard would be a prompt instruction.

**Window-scoped `workout generate`.** Let `-m 7` regenerate just block 7. More general, and
"re-plan that block" is a reasonable ask on its own — but it contradicts DESIGN_cli_selectors.md
§8's declared "generation always starts today", needs generate's metrics window to mean something
other than "ends today", and re-opens the archival question in §7. A bigger change than this
problem justifies; worth doing on its own merits, if ever.

**No column — infer "unhonored".** Compare the constraint against the sessions in its window (a
`rest = 1` day with a non-rest session is plainly unhonored). Works only for `rest = 1`, and for
`rest = 0` there is nothing to compare against, since honoring an advisory constraint is a
judgement rather than a state.

**A first-class delete in the response schema.** Schedule-plus-delete would compose a move
without §9's replacement rule — but an empty date and a planned rest day diverge in adherence
(a missing row reads as an unplanned gap, DESIGN_constraints.md §6), so a hole is never the
right result inside a governed span, and the rest-replacement adapt already uses *is* the
delete. A delete member would add a schema field, an apply branch and a new class of model
error, to produce a state the app does not want.

**No adapt-side stamp — a read-side sweep filter instead.** Skip constraints wholly inside
the current block when sweeping, on the grounds that they are adapt's territory by definition
(§3), and drop adapt's `honored_at` writes entirely — less machinery, no no-change stamping
path. Rejected: it assumes adapt actually runs. Nothing guarantees the daily command is run
daily, and a sweep that hides an in-block constraint nothing has acted on defeats its own
question. The stamp asserts a pass *happened*; the filter would assert only that one was
expected.

**No sweep — require a selector.** Cheaper, and wrong for the motivating case: the athlete does
not know which block to name (§4.1).

**A `moved_from` field in the response schema.** The narrower cousin of the delete member above:
not "remove this date" but "this session came from that one", letting apply place the rest
replacement itself when the model emits only the destination. It is genuinely less objectionable
than a delete — it produces the state the app wants rather than a hole — and it was still
rejected, for two reasons. It only works when the model fills it in, and a model that forgot the
vacating row is exactly the one that will forget the field, so it narrows the failure without
closing it. And the guarantee it buys is already there for the case that matters: a `rest = 1`
window is vacated *deterministically* by the pre-pass (§7), whatever comes back. What remains is
advisory constraints where a move loses its replacement — which §9's named rule instructs and
§10's whole-window preview makes visible.

**Naming.** `workout replan` collides head-on with `--replan`, which means the opposite (escalate
to plan-shaping). `reconcile` already means remote-vs-local matching here (`garmin/sync.py`,
`google_calendar.py`, `db/activities.py`). `apply` is the second half of every preview-then-apply
flow and would blur it. `constraint enforce` matches `_enforce_rest_windows_*` but describes only
the deterministic half. `plan reschedule` was rejected on namespace grounds: every `plan`
subcommand takes a plan *version* as its subject, and even `plan rollback`, which does write
workout rows, does so as a consequence of restoring a version. This changes no version.

`workout reschedule` was the working name through four drafts and lost on prefixes rather than
on meaning (§4): it would have made `re`/`res` ambiguous with `restore`. Two ways out were
available and both were declined. Registering `res` as a winner-picking alias on `restore` is
sanctioned by DESIGN_cli_noargs.md §d and passes every invariant — but it spends the tie-break
mechanism to keep a name that has an unencumbered alternative, and §d's whole point is that
aliases earn their place. Collapsing to `constraint honor` alone, with the selectors on it
(`constraint list` already carries them), was the closer call: it removes a command instead of
adding one and puts the verb on the noun it acts on. It was declined because the athlete
reaching for this after looking at their schedule looks under `workout` — and because the
sweep, which is the case this design exists for, has no single constraint to hang a
`constraint`-side verb on.

**The collapse happened in the other direction.** `constraint honor` was cut and its one real
behaviour became `workout accommodate -c` (§4). What settled it was the maintenance the second
door was actually charging: two parsers that had already drifted, a parity test to hold them
level, a module placed to break an import cycle between the two command families, and every
message in the codebase having to pick which of two names to print. None of that bought
anything the athlete could not get from a flag. The `workout`-side home was the one to keep,
because the sweep only fits there.

## 14. Deliberately not done

- **Rebalancing outside the spill margin.** §5. The margin is the whole difference between a
  reschedule and a re-periodization.
- **Reading metrics.** §6, with its cost stated there.
- **Escalating to `replan` on its own.** DESIGN_constraints.md §7's posture holds: nothing sets
  `replan = 1` without a human `y`.
- **Authoring learnings.** Adapt is read-only w.r.t. coach learnings
  (DESIGN_evidence_based_confidence.md §2/§11); this is further from the evidence than adapt is.
- **Running it automatically after `constraint add`.** The §10 line points at it; the athlete
  runs it. Same confirm-before-acting posture as everything else that writes sessions.
- **Warning about un-honored constraints before a rollback's `y`.** §8. The flag being cleared
  costs a nudge; pre-announcing it cost a reader, a lifted query and two threaded confirm
  prompts. The rollback reports it afterwards instead.
- **A code backstop for the vacate rule.** §9's rule is prompt-enforced, and stays that way.
  DESIGN_benchmark_workouts.md §4.2 settled this class of question for the same apply path: a
  deterministic guard would have to reverse-engineer intent from a proposal batch — is this pair
  of changes a move, a displacement, or a softening? — which is the judgement the model already
  has in front of it. The exposure is also smaller than it first looks, because the one case with
  real teeth is already deterministic: a `rest = 1` window is vacated by the pre-pass whatever the
  model returns (§7). That leaves advisory constraints, where §9 states the rule explicitly —
  better than adapt states it today — and §10 renders the result so a slip is visible before the
  `y`. The whole-window preview is a display choice, not a guard: it constrains nothing the model
  may do, it only stops the athlete confirming something they cannot see.

## 15. Amendments to existing docs

All three are **applied**, and all three carry the command's name, so a verb change is a
cross-doc edit rather than a local one — which is itself an argument for settling the name
before implementation rather than during it (§4, §13).

- `DESIGN_block_boundary.md` §5 — the "extending the adaptation range" entry gains a pointer
  here, so a reader meets the answer rather than re-deriving the firewall argument and concluding
  it was overlooked.
- `DESIGN_constraints.md` §7 — the two-fate model gains the third tier (§3).
- `DESIGN_cli_selectors.md` §5 — `workout accommodate` joins the commands taking the reserved
  vocabulary; the `workout adapt -m` exception is unchanged and §4 records why.

Two more land at implementation time rather than now, because they describe code that does not
exist yet:

- `DESIGN_benchmark_workouts.md` §4.2 — PROTECTING A BENCHMARK stops being adapt's section and
  becomes a shared, scope-parametrized one (§9). §4.2's argument is unchanged; what changes is
  that two commands now rely on it.
- `DESIGN_adapt_task_prompt.md` §2 — rule 2's evidence clause becomes a parameter, and the vacate
  mechanic it names in passing ("`PROTECTING A BENCHMARK` still spells out that moving a test
  means emitting it on its new date plus a replacement on the old one") becomes a section of its
  own, stated for ordinary moves rather than only for tests.
