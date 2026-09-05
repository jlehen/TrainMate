# Design: Continuity when a plan change reaches the sessions

**Status:** Draft · **Date:** 2026-09-05 · **Branch:** worktree-plan-change-continuity

## 1. The problem

An athlete profile is not written once and left alone. It gets corrected as whoever set
the instance up learns what the athlete actually wants, and as the athlete says it better
the second time. "Three sessions a week" becomes "four, but never two hard days in a row".

A correction to `preferences` is plan-shaping (DESIGN_plan_staleness.md §4), so it starts
a chain. The chain is right. What comes out the other end of it is not.

### 1.1 The chain

1. The macrocycle flags stale. `plan show` names `preferences` as the field that moved.
2. `plan generate` builds a new periodization. **The athlete's sessions are untouched at
   this point.**
3. To make the new plan reach the sessions that already exist, the operator passes a
   selector — `workout generate -d today..`, or `-m 1`. A bare `workout generate` opens
   the day after the schedule's coverage ends (`cli/workouts/generate.py::_resolve_span`),
   so without a selector the new plan does not reach the athlete for up to
   `workout_generation_span_days`.
4. That selected run rewrites the span wholesale.
   `coach/service/workouts.py::workout_generate_apply` voids every live session in the
   span the new proposal does not re-propose, then writes the new ones.

Step 4 is where the surprise is made.

### 1.2 Two sharp edges

**The rewrite is blind.** The generation prompt is shown nothing about the sessions
currently standing in the horizon, except the handful an adaptation already eased
(`adaptation_count > 0`, `coach/service/workouts.py::workout_generate`). So the model
rewrites the span with no idea what the athlete was told last week. Days the correction
has nothing to do with are re-rolled along with the rest, because the model has nothing
to hold them steady against.

**Dropped days vanish.** A void produced by a regeneration carries the change kind
`generate`, which is not in `ATHLETE_VOID_KINDS` (`db/workouts.py`), so
`calendar_reconcile.py::_plan` tears its Google Calendar event down. The event is deleted.

Not retitled, not marked — deleted. Thursday held a 90-minute ride yesterday; today
Thursday is blank. There is nothing to tap and nothing that says a decision was made.
The record is intact in the append-only log, and `workout batches` can show exactly what
happened, but the athlete is not in a terminal. The Calendar is where the athlete meets
the plan (DESIGN_calendar_lineage.md §1), and there the day simply emptied.

This bites hardest on a companion-mode instance, where the athlete has no shell and no
way to ask what changed (DESIGN_bot_simple_frontend.md). It is not exclusive to it: an
expert-mode athlete gets the same blank Thursday, and only finds out why by going and
looking for it.

### 1.3 What already works

Two things, and they are the model for the rest.

**A rewritten day explains itself.** A day the regeneration re-prescribes keeps its
`lineage_id`, so its event is updated in place and the History block underneath shows the
earlier form, its load, its target and its reason (DESIGN_calendar_lineage.md §3). That
reads as a change to something the athlete recognises. A deleted event reads as nothing
at all — which is why dropping is worse than rewriting, even though rewriting sounds like
the bigger edit.

**A regeneration does not volunteer to rewrite.** `_resolve_span` opening after the
covered days means the destructive form has to be asked for. The operator has to type a
selector. That is already a continuity property; this design keeps it and adds a second
line of defence behind it, because the selector is exactly what the operator types when
they want the correction to take effect now.

## 2. What "continuity" means here

Three things get conflated under the word. They need different answers, so they are
separated up front.

1. **The near days should not move.** Whatever the athlete has already read and planned
   around — tomorrow, the rest of this week.
2. **A change should be proportionate.** A correction about how sessions are described
   should not re-roll a month of structure.
3. **A change should be explained where the athlete meets it.** Not "your Thursday is
   different now" but "you asked for four sessions a week, so from Monday there are four".

The first two are about what the regeneration is allowed to do. The third is about what
is said afterwards. §4 and §5 answer the first two; §6 answers the third.

## 3. Decision

**Three parts.**

- **§4 — A commitment window.** A new setting, `workout_commitment_days`. The sessions
  standing inside it reach the generation prompt, and the model is asked to change their
  *description* rather than their *shape* unless the change that triggered the run
  demands otherwise. A deterministic post-check reports every shape change it made, in
  the preview, before the operator accepts.
- **§5 — A void inside the window leaves a trace.** Any void dated inside the commitment
  window keeps its Calendar event, retitled `[Deleted]` with its reason, whatever change
  kind produced it. Outside the window, teardown as today.
- **§6 — The plan says what changed, in one sentence.** `plan generate` returns an
  `athlete_note` alongside the strategy. It rides onto the regeneration's change summary
  and into the companion message.

## 4. The commitment window

### 4.1 The setting

`workout_commitment_days`, default 7. `coach.workout_commitment_days` in `config.yaml`,
beside `workout_generation_span_days`, and a `Setting(...)` row in
`trainmate/settings.py` under a new `Schedule` group, so the athlete-facing name is
`commitment-days`.

It earns a registry row on DESIGN_settings.md §1's test — "a knob the athlete might
reasonably change from a phone". How far ahead the week feels settled is a comfort
question, not a coaching threshold. An athlete who wants nothing touched inside three days
can say so without a shell, and an athlete who would rather always have the freshest
prescription can set it to `0` and get today's behaviour back exactly.

`parse` is a non-negative integer. `0` means the window is off; nothing about the rest of
this design is conditional on it, because an empty window simply contains no sessions.

### 4.2 Where it bites

**Only where the generated span overlaps sessions that already exist.**

This needs no notion of *why* the run is happening, which is what makes it cheap and
predictable. A bare `workout generate` extends into empty days, so the window contains
nothing and the prompt is the one it always was. A selected run that reaches back over
scheduled days is, by construction, proposing a diff to something the athlete has already
read — and that is the case the window is for.

The window is measured from today, not from the span start: it is about what the athlete
has already committed to, and that is anchored to the present.

### 4.3 Shape and content

The distinction the window turns on:

| | examples | how the athlete meets it |
|---|---|---|
| **Shape** | the day, the sport, roughly the duration | visible at a glance on a calendar |
| **Content** | the description, the interval structure, the prose | only visible by opening it |

A shape change cannot explain itself. A dropped day deletes an event; a moved day makes
two events change at once; a sport change makes the day unrecognisable. None of those can
carry their own reason, because the surface they would carry it on is the thing that
moved.

A content change explains itself for free. Same day, same sport, same event — and the
History block already renders the earlier form directly beneath the new one. The athlete
who opens it sees exactly what changed and why.

So inside the window the instruction is: **change the content freely, and change the shape
only when the change that triggered the run demands it.**

### 4.4 Why advisory, and not enforced

The codebase already holds both answers to "the model was asked, but did it?", side by
side in one file:

- `coach/service/workouts.py::_enforce_rest_windows_generate` — deterministic. A `rest`
  constraint bypasses the model for those dates entirely, because a rest day is not a
  judgement call.
- `coach/service/workouts.py::_warn_missing_boundary_benchmarks` — "warn, don't
  auto-insert… a surfaced warning the athlete can act on, not a silent fix".

The commitment window belongs in the second tier, because breaking it is sometimes
correct. A correction that says "no running, her knee is bad" *must* be allowed to change
the shape of a run scheduled for tomorrow. A hard rule would either forbid that or need an
override flag, and an override flag that is right to use is a rule that was stated wrongly.

So there is no `--ignore-commitment` flag. The rule is advisory by construction, and §4.6
makes the model's judgement visible instead of constraining it.

### 4.5 The model must be told what changed

"Avoid visible changes if possible" is not usable on its own. The model needs to know what
would make it *not* possible, and without that it either always obeys or obeys
arbitrarily.

Consider two corrections, and what the model actually has to work with in each:

> **A.** "Describe her sessions with more detail on form cues."
>
> Nothing about that requires Thursday's ride to move. The right answer is to reword the
> days in the window and hold every shape.

> **B.** "She's had knee pain — no running for now."
>
> Thursday's run cannot stay a run. The shape has to change, and the right answer is to
> swap the sport and say why.

If the prompt supplies only the *new* profile and the standing sessions, the model sees
the same thing in both cases: a profile, and a horizon to write. The fact that separates A
from B is *what moved*, and it was never shown.

So §6's `WHAT CHANGED SINCE THE LAST PLAN` section is not only for the athlete note — it
is what makes the window's instruction decidable. With it, "if possible" becomes a
question with an answer:

> Does this day's shape change follow from what changed? If it does, make it and give a
> reason. If it does not, hold the day and change only its description.

### 4.6 The post-check

After the proposal comes back and before the preview is drawn, compare it against the live
sessions inside the window and report every shape change:

```
Commitment window (next 7 days):
  Thu 11 Sep  ride 90m   → dropped
  Sat 13 Sep  run 60m    → moved to Sun 14 Sep
  Tue 09 Sep  swim 45m   → reworded (same shape)
```

Deterministic, and it does two jobs. It tells the operator, before they accept, that a
correction is about to re-roll the athlete's week — which is the thing they cannot see
today. And it is the enforcement the advisory instruction does not have: a model that
misjudges the window is visible to the operator rather than to the athlete.

**Any session whose shape changes inside the window must carry a `change_reason`.** It
costs nothing to require, and it flows straight into the Calendar History `Reason:` line
and into §6's message. The one thing the athlete does notice is then also the one thing
that arrives explained.

## 5. A void inside the window leaves a trace

### 5.1 What a void is

The `workouts` table is append-only (DESIGN_workout_revisions.md §2), so "there is no
session on Thursday any more" cannot be said by deleting a row. It is said by appending
one: a **void revision**, a row meaning *this slot now holds no session*. When it is the
newest revision in the slot, the day is empty.

Every void carries the kind of the change that made it, and the reasons a day can empty
are not alike:

| kind | what happened |
|---|---|
| `rm` | the athlete cancelled it |
| `stand-down` | its goal was archived |
| `swap` | it moved to another day (void where it left, copy where it landed) |
| `generate` | a regeneration did not re-propose it |
| `adapt` | an adaptation dropped it |

### 5.2 The two questions `ATHLETE_VOID_KINDS` answers

`ATHLETE_VOID_KINDS = ("rm", "stand-down")` has two consumers asking different questions.
They agree today by coincidence, not by construction.

- `coach/service/adaptation.py::workout_adapt` asks **who asked for this?** — so the coach
  is not told the athlete cancelled a day the plan merely stopped scheduling. That is
  genuinely about authorship, and the set is exactly right for it.
- `calendar_reconcile.py::_plan` asks **should this day leave a trace?** That is about
  visibility. It borrowed the authorship set because the two happened to coincide.

The second question has a different answer, and it is date-dependent.

### 5.3 The predicate

Leave the constant alone for its authorship job. Add a predicate for the calendar's:

```python
def leaves_tombstone(void, commitment_end: str) -> bool:
    """Whether a void keeps its Calendar event, retitled '[Deleted]', rather than the
    event being torn down (DESIGN_plan_change_continuity.md §5.3)."""
    return (
        void["change_kind"] in ATHLETE_VOID_KINDS
        or void["date"] <= commitment_end
    )
```

Inside the window, any void leaves a tombstone whatever ended it. Outside it, only the
athlete-asked ones, exactly as today. One sentence states it: **a day you were counting on
does not disappear — it is marked cancelled, with a reason.**

Nothing needs building to render it. The void row already carries `removed_reason`
(`db/workouts.py::get_workout_by_id`), and `google_calendar.py::sync_workout` already
draws `[Deleted] {title}` with a `Reason:` block for the `rm` case. Only the routing
changes.

`commitment_end` has to reach `_plan()`, which today takes `(db, lineage_ids)`. It is a
settings read, so read it there — the same way the bot re-reads settings on each tick of
its push loop (DESIGN_settings.md §5).

Outside the window the teardown stays. Retitling every day a 28-day rewrite happens to
drop would litter the calendar with entries for days the athlete never looked at.

### 5.4 `adapt` gets this too, and that matters more

`workout adapt` runs daily and works forward from today, so its drops land inside any
commitment window by definition. Today they are torn down in silence, exactly like a
regeneration's.

That makes this not an occasional replan edge case but a hole in the daily loop:
"cancelled — you have been under-recovered three mornings running" is precisely what the
athlete should see, and today they see an empty Thursday. The date-based predicate catches
it with no extra rule.

### 5.5 Tombstones need a sweep

`workout prune-calendar` deletes **orphans** — events no local row claims
(`cli/workouts/edit.py::run_workout_prune_calendar`). A tombstone is claimed by its removed
row, so prune deliberately keeps it. Today that is fine, because `rm` is rare.

Under this change tombstones arrive from every regeneration and every adapt drop inside
the window, and nothing ever clears them.

So: **each reconcile pass also tears down tombstones dated before today.** One extra query
per pass, and every write path already ends in a reconcile
(`calendar_reconcile.py::reconcile`). It is self-limiting — at most a window's worth of
tombstones can exist at once, and a cancellation stops being news the day after the day it
was for.

## 6. The plan says what changed, in one sentence

### 6.1 Where the sentence comes from

Three options were considered.

**The operator types it** — `plan generate --because "you're on four sessions a week
now"`. Zero risk and honest, but it is a thing to remember, and the operator is the person
least likely to be surprised by the change, so they are the worst placed to judge what
needs saying.

**Derive it from the snapshot.** Both sides of the diff are already stored: the macrocycle
carries `profile_snapshot`, the plan-shaping profile as JSON at generation time
(DESIGN_plan_staleness.md §5), and `config.py::changed_plan_profile_fields` already
compares it against live config to name what moved. So a deterministic sentence is free —
but `preferences` is free prose, and a diff of two paragraphs is not something anyone
wants to read on a phone. This yields "your preferences changed", which is what is already
said.

**Ask for it in the call already being made.** Chosen. `plan generate` sends a strategy
call and gets back a strategy and its reasoning. One more output field costs no extra call
and no extra latency, and the model writing it is the one that just decided what to do
about the correction — so it can state the *consequence* rather than the input. Not "your
preferences were updated" but "four sessions a week now instead of three, and never two
hard days in a row".

### 6.2 The two prompt additions

Both gated on the profile having actually moved, so a run where nothing changed produces
the prompt it always did — the same gating idiom as
`coach/engine/workouts.py::_carried_adaptations_task`.

**A schema member**, carrying its own instruction. The precedent is
`coach/engine/workouts.py::_carried_keep_field`, which puts the whole rule for `keep` in
the member description rather than in a TASK block:

```
"athlete_note": "One sentence to the athlete: what changed for them, and from when.
  Their words, not the plan's vocabulary. OMIT when nothing moved."
```

**A short data section**, built from `profile_snapshot` and `changed_plan_profile_fields`:

```
## WHAT CHANGED SINCE THE LAST PLAN
preferences: was "3 sessions a week, evenings only"
             now "4 sessions a week, never two hard days in a row"
```

Four lines of prompt and one schema line. No TASK block: there is nothing to explain that
the field description does not already say.

This section is load-bearing twice over — §4.5 needs it for the window instruction to be
decidable at all.

### 6.3 A note, not a fact

`athlete_note` is prose from a model, so nothing branches on it. It is stored on the
macrocycle, shown to the operator in the `plan generate` preview before they accept, and
only then carried onto the regeneration's change summary and into the companion message.
The operator reads it before the athlete does and can reject the plan or overwrite the
note.

Carrying it onto the change summary is what puts it in front of an athlete who never opens
Telegram: the summary is the `Change:` line of the Calendar History block
(DESIGN_calendar_lineage.md §3), so every rewritten session in the span carries the
sentence.

### 6.4 The companion hears about the outcome, not the flag

DESIGN_plan_staleness.md §9 keeps the companion silent about the staleness flag:
regenerating is operator work, and the companion athlete has no shell to do it in. That
stays right.

A *finished, applied* change is the opposite case. It is not a decision the athlete has to
make; it is a thing that has already happened to their week, and they are about to meet it
whether or not anyone says so. So the companion gets one message after the regeneration
lands: the `athlete_note`, when the window's shape changes start, and the fact that the
days before then are unchanged.

## 7. Worked example

`workout_commitment_days` is 7. It is Wednesday. The athlete's calendar holds a 90-minute
ride on Thursday and a long run on Saturday. The operator corrects `preferences` from
"three sessions a week" to "four sessions a week, never two hard days in a row".

**Today.** The plan regenerates. The operator runs `workout generate -m 1`. Thursday's
ride disappears from the calendar with no trace, Saturday becomes something the athlete
has never seen, and the whole month is re-rolled including the parts the correction never
touched. The athlete finds out by opening their phone.

**With this design.** The regeneration sees Thursday and Saturday standing in the window,
and it sees that what changed is session frequency and hard-day spacing. Thursday's ride
keeps its day, its sport and its duration; only the description changes, to sit better
beside the new Friday session. Saturday is untouched. The new fourth session lands on
Friday, which was empty and so commits to nothing.

The preview says so before the operator accepts:

```
Commitment window (next 7 days):
  Thu 11 Sep  ride 90m   → reworded (same shape)
  Sat 13 Sep  run 90m    → unchanged
```

And the athlete gets one message: *"You're on four sessions a week now, and never two hard
days in a row. That starts Friday — this week's ride and long run stay as they are."*

Had the correction instead been "no running", Thursday's shape would have had to change.
The preview would have said `Thu 11 Sep run 60m → sport changed to ride`, the operator
would have seen it, and the athlete's calendar event would have carried the reason in
place.

## 8. Implementation notes

**The window list and the eased list must be one section.** The sessions already eased by
an adaptation (`adaptation_count > 0`) reach the prompt today under their own rules — KEEP
by default, replace only when the moment the easing answered has passed
(DESIGN_workout_revisions.md §7.1). Those sessions can also sit inside the commitment
window. Two independent sections would show the model the same session twice under two
different instructions. Merge them into one "sessions already standing" block, with window
membership and easing history as per-session tags.

**No new response action.** `keep` already means "leave it exactly as it stands", which
covers the no-change case. A description-only rewrite is an ordinary workout that happens
to carry the same date, sport and duration — §4.6's post-check reads the shape and sees
that. A third `reword` action would be one more thing to get wrong, and the post-check has
to exist either way.

**`generate` still resets the easing tally.** A reworded session inside the window is a
new `generate` revision, so DESIGN_workout_revisions.md §7's backward walk stops there and
`adaptation_count` returns to 0. That is correct and unchanged: an easing described the old
prescription, not the new one. It does mean a session can be reworded out of its
`[ALREADY EASED]` tag, which is another reason the merged block above must show the model
the easing history it is about to clear.

**The window is a shared revision layer, not a second `adapt`.** Every continuity step
hands `generate` something `adapt` already had: a view of the standing sessions and a `keep`
action (DESIGN_workout_revisions.md §7.1), and now hold-by-default, a required
`change_reason` on shape changes, and a "what changed" section doing the job adapt's
attribution block does. That is the same machinery, and it should be built once — the merged
"sessions already standing" block above, `_resolve_kept`, §4.6's post-check and the
change-reason requirement — and called from both services as one operation: *revise a span
against standing sessions, with a stated cause.* What stays separate is the policy each
command runs it under, and the difference is one sentence: **adapt answers "how is the
athlete", generate answers "what does the plan say".** Adapt's cause is the athlete's state,
it may not reshape the block (DESIGN_block_boundary.md §2, DESIGN_adapt_task_prompt.md rule
2), and its revisions count toward the compounding guard. Generate's cause is the plan diff,
it may reshape freely outside the window, and its revisions reset the easing tally. Merging
the commands would put both causes in one call and gate block-reshaping behind a flag —
§4.4's misstated-rule problem at a larger scale — so the commands stay, and the layer beneath
them is what merges.

**ARCHITECTURE.md** needs the new predicate and the settings row once this is built.

## 9. Deliberately not done

**`preferences` is not split.** The cheapest way to reduce this whole cascade is to stop
firing it so often: split the field into a plan-shaping half and a session-flavour half, so
a correction to how sessions are described never flags the plan at all.
DESIGN_plan_staleness.md §4 rejected having a model judge whether a prose diff is
plan-relevant, and rightly — but a *structural* split is deterministic and cheap. It is a
separate change, it reduces how often this design is needed rather than improving it, and
it should be done on its own merits.

**The whole horizon is not carried forward.** An earlier version of this design showed the
model every standing session across the full span, not just the window. That partly
reverses DESIGN_workout_revisions.md §7.1's deliberate choice — "every other day stays the
model's to write, which is what keeps `generate` a regeneration rather than a second
`adapt`". Bounding the carry-forward to the commitment window keeps that choice intact
everywhere except a narrow span where there is a stated reason to suspend it, and costs
roughly a tenth of the prompt.

**No override flag.** See §4.4: the rule is advisory, so there is nothing to override.

**`plan diff` still does not render profile changes.** DESIGN_plan_staleness.md §8 left
this as a natural follow-up and it stays one. §6's `athlete_note` answers the athlete's
question, not the operator's; an operator who wants the field-level diff is still reading
`config.yaml`.
