# Design: Continuity when a plan change reaches the sessions

**Status:** Draft · **Date:** 2026-09-09 (rev. 4) · **Branch:** worktree-plan-change-continuity

Revision 4 follows a third review against the code and a discussion of its findings. The
largest change is a removal: rev. 3 had `workout generate` reconstruct *what changed since
these sessions were written* from profile snapshots saved on every change row, and that
machinery is gone. The coach is shown the present — today's profile, the plan, the
constraints, the sessions standing — and keeps a session unless it can name the line
that session now contradicts (§6.1). Alongside it: a dropped session becomes a rest day
on its own lineage the way `workout adapt` already does, so a decision leaves one event
and not two (§4.5); the Calendar reads a void from its own lineage, so a marker written
and then covered in the same slot is not torn down (§5.2); a session the coach does not
mention is kept, not dropped (§4.5); manual sessions are held across the whole span
(§4.2); past constraints of the current block reach the prompt (§6.1). Revisions 1 to 3
are in the branch history.

Throughout, "regeneration" is never used bare: `plan generate` rebuilds the periodization,
`workout generate` writes the scheduled sessions.

## 1. The problem

An athlete profile is not written once and left alone. It gets corrected as whoever set
the instance up learns what the athlete actually wants, and as the athlete says it better
the second time. "Three sessions a week" becomes "four, but never two hard days in a row".

A correction to `preferences` is plan-shaping (DESIGN_plan_staleness.md §4), so it starts
a chain. The chain is right. What comes out the other end of it is not.

### 1.1 The chain

1. The macrocycle flags stale. `plan show` names `preferences` as the field that moved and
   shows the edit as a diff (DESIGN_plan_staleness.md §10).
2. The operator decides whether the edit re-shapes the plan. On "re-shaping" they run
   `plan generate`. On "keep" they run `plan keep`. **Either way the athlete's sessions
   are untouched at this point.**
3. To make the change reach the sessions that already exist, the operator passes a
   selector to `workout generate` — `-d today..`, or `-m` for the current block. A bare
   `workout generate` opens the day after the schedule's coverage ends
   (`cli/workouts/generate.py::_resolve_span`), so without a selector the change does not
   reach the athlete for up to `workout_generation_span_days`.
4. That selected run rewrites the span wholesale.
   `coach/service/workouts.py::workout_generate_apply` voids every live session in the
   span the new proposal does not re-propose, then writes the new ones.

Step 4 is where the surprise is made.

### 1.2 What the athlete actually sees

Take a concrete week. It is Wednesday. The calendar holds a 90-minute ride on Thursday, a
rest day on Friday and a long run on Saturday. The operator corrects `preferences` from
three sessions a week to four, and runs `workout generate -m`.

**The rewrite is blind.** The `workout generate` prompt is shown nothing about the
sessions currently standing in the horizon, except the handful an adaptation already
eased (`adaptation_count > 0`, `coach/service/workouts.py::workout_generate`). So the
model rewrites the span with no idea what the athlete was told last week. Days the
correction has nothing to do with are re-rolled along with the rest, because the model
has nothing to hold them steady against.

**A dropped ride turns into an unexplained rest day.** The prompt requires every date to
carry a row, and `_fill_coverage_gaps` adds a "Rest Day" for any the model leaves out
(DESIGN_runway_nudge.md §2.1). So when the model drops Thursday's ride, Thursday gets a
rest row. That row is a session in a different slot — slots are `(date, sport)`, and
`rest` is a sport — so it starts its own lineage and gets its own Calendar event. The
ride's void carries the change kind `generate`, which is not in `ATHLETE_VOID_KINDS`
(`db/workouts.py`), so `calendar_reconcile.py::_plan` tears the ride's event down.
Thursday now reads "Rest Day", with nothing to say a ride was there yesterday or why it
went. The "Rest Day" body says "no session planned for this day", which is not true: a
session was planned, and a decision removed it.

**A sport change is a new session with no history.** Had the correction been "no
running", Thursday's run would become a ride. Same reasoning: different sport, different
slot, new lineage. The run's event is deleted and a ride event appears with an empty
History block. Compare `workout adapt`, which hands the displaced session's lineage to
its new-sport replacement (`coach/service/adaptation.py::workout_adapt_apply`,
`lineage_id=displaced['id']`), so the event updates in place, retitled, with the run
underneath it in History.

**A session the athlete added by hand vanishes without even a void.** When
`workout generate` writes over a manual session, `_lineage_for` starts a new lineage and
records the replacement for DESIGN_workout_revisions.md §12's notice — but no void row is
written for the manual lineage. It simply has no live revision any more, `_plan` sees
`head is None`, and the event is deleted.

The record is intact in the append-only log, and `workout batches` can show exactly what
happened, but the athlete is not in a terminal. The Calendar is where the athlete meets
the plan (DESIGN_calendar_lineage.md §1), and there the day changed with no trace of a
decision. This bites hardest on a companion-mode instance, where the athlete has no shell
(DESIGN_bot_simple_frontend.md), but an expert-mode athlete gets the same unexplained
Thursday.

### 1.3 What already works

**A rewritten day in the same slot explains itself.** A day `workout generate`
re-prescribes in the same `(date, sport)` keeps its `lineage_id`, so its event is updated
in place and the History block shows the earlier form, its load, its target and its
reason (DESIGN_calendar_lineage.md §3). That reads as a change to something the athlete
recognises.

**`workout adapt` already leaves the trace this design wants.** Its vacate rule requires a
replacement on any date it empties (`coach/engine/workouts.py::_vacate_task`), the
replacement carries the displaced session's lineage, and the event is retitled
"[Adapted] Rest Day" with the reason and the old session in History. Nothing in this
design changes adapt's behaviour except what §4.6 shows it about a session's earlier form.
The design is about making `workout generate` reach the same standard — the same
standard, and not a second one beside it.

**A bare `workout generate` does not volunteer to rewrite.** `_resolve_span` opening after
the covered days means the destructive form has to be asked for. That is already a
continuity property; this design keeps it.

## 2. What "continuity" means here

Three things get conflated under the word.

1. **The near days should not move.** Whatever the athlete has already read and planned
   around — tomorrow, the rest of this week.
2. **A change should be proportionate.** A correction about how sessions are described
   should not re-roll a month of structure.
3. **A change should be explained where the athlete meets it.** Not "your Thursday is
   different now" but "your profile asks for four sessions a week, so Friday is now a
   session".

§4 answers the first inside a window. The second is answered inside the window and, on
purpose, only there: outside it the plan is the plan, and `workout generate` is free to
rebuild (§7). §5 makes every removal inside the window visible, and §6 answers the third.

## 3. Decision

**Four parts.**

- **§4 — A commitment window.** A new setting, `workout_commitment_days`. The sessions
  standing inside it reach the `workout generate` prompt, together with every session
  the athlete added by hand anywhere in the span, and the model has to account for each
  one: keep it, revise its load, move it, change its sport, or drop it — each with a
  reason written for the athlete to read. Every answer but `keep` lands on the session's
  own lineage, exactly as adapt does, so the day keeps one event. A session the model
  does not mention is kept. The preview lists what was written to each of them, before
  the operator accepts.
- **§5 — A removal leaves a trace.** A session the coach removes inside the window keeps
  its Calendar event, retitled `[Cancelled]` with the reason. A session the athlete added
  by hand keeps its event whenever the coach removes it, inside the window or not.
  `[Deleted]` stays the word for a removal the athlete asked for.
- **§6 — The coach reads the present and writes what it did.** `workout generate` is
  shown today's profile, the plan, the constraints including the ones that ended earlier
  in this block, and the sessions standing. It keeps a session unless it can name what
  that session now contradicts, and when it changes one it writes one sentence for the
  athlete about that day, and one for the week. No history is reconstructed.
- **§8 — Tests**, named up front.

## 4. The commitment window

### 4.1 The setting

`workout_commitment_days`, default 7. `coach.workout_commitment_days` in `config.yaml`,
beside `workout_generation_span_days`, and a `Setting(...)` row in
`trainmate/settings.py` under a new `Schedule` group, so the athlete-facing name is
`commitment-days`. `parse` is a non-negative integer — the first integer setting in the
registry, whose other rows are models, zones, switches and times.

**Not routable.** `cli/settings.py::ROUTABLE_SETTINGS` is a three-name allowlist, and
`routable_setting` refuses anything off it by design. A registry row does not reach the
companion's `change_setting` intent, and this one should not be added: `0` silently turns
off every trace in §5, `60` freezes the plan, and neither is explicable to an athlete in
one sentence. It is an operator knob that happens to live in the registry so it can be
read and listed like every other.

**Counting.** The window is the `N` days starting today. `7` covers today through the
sixth day after it. `1` covers today only. `0` covers nothing: today's session may change
too. `window_end = today + (N - 1)`; with `N = 0` the window is empty. One thing never
changes whatever `N` is: a session already completed today is preserved by the existing
rule in `workout_generate`.

Nothing else in this design is conditional on the value: an empty window simply contains
no sessions. There is no clamp against `workout_generation_span_days`: that setting
bounds only a bare `workout generate`, a `-d` selector can reach past it, and §4.2's
intersection already ignores every window day the run does not rewrite.

### 4.2 Where it bites: the window *and* the span

**Only where the generated span overlaps sessions that already exist.** The operative set
is the standing sessions that are in the window **and** in the range being rewritten:

```
committed = standing sessions where today <= date <= window_end
                                 and gen_start <= date <= gen_end
manual    = standing sessions where source == "manual"
                                 and gen_start <= date <= gen_end
standing block = committed ∪ manual
```

Both bounds of `committed` are load-bearing:

- A bare `workout generate` extends into empty days, so the intersection is empty and the
  prompt is the one it always was. The companion's runway button
  (DESIGN_runway_nudge.md §6) is a bare `workout generate`, so its no-preview flow is
  unaffected by the window.
- A run selected *backwards over the near days* — `-d today..`, `-m` — is the case the
  window is for.
- A run selected *forwards* — `-d 2026-09-25..`, or `-g` on a later goal — has a
  non-empty window that the span cannot touch. Without the second bound those sessions
  would enter the prompt, the model would be made to account for them, the preview would
  report a drop as `→ cancelled`, and apply — bounded to `gen_start..gen_end` — would do
  nothing. The preview would lie.
- When today's session is already completed, `gen_start` moves to tomorrow while the
  window still opens today. The second bound drops today's session from the set, which is
  right: it is preserved, not re-decided.

**Manual sessions are held across the whole span.** The argument for the window is that
past it the athlete has not seen the days. That is false for a session they typed in
themselves. So every manual session in the span is in the standing block whatever the
window, tagged as the athlete's (§4.6), and the model answers for it like any other.

**Benchmarks are not.** A scheduled fitness test inside the window is in the committed
set and carries its tag. Past the window the coach re-places tests itself, from the
record it is already given: every anchor's latest value and last test date, every test on
the calendar in the ninety days before the span, and the tests planned in the elapsed
part of the block (`coach/service/context.py::_anchor_history_text`,
`_block_benchmark_lines`). The plan is the plan out there, and that includes its tests.

One set, computed once, used by the prompt block (§4.6), the preview (§4.5) and the trace
rule (§5.2).

### 4.3 Shape, load and wording

The distinction the window turns on has three levels, not two:

| | examples | how the athlete meets it |
|---|---|---|
| **Shape** | whether the day has a session, which sport, which day | visible at a glance on a calendar |
| **Load** | duration, intensity target, the interval structure | visible by opening it, and felt on the day |
| **Wording** | the prose, the title | visible by opening it |

Inside the standing block the rule is:

- **Wording alone is never a reason to touch a session.** The default for every standing
  session is `keep`. A rewritten description with the same load is churn, and the point of
  DESIGN_workout_revisions.md §9 was to stop producing it.
- **Load may change when the profile, the plan or a constraint calls for it**, in the same
  slot, with a reason. "Never two hard days in a row" may turn Saturday's 90-minute
  threshold run into 60 minutes easy. That is a revision of the same session; its History
  shows the earlier form.
- **Shape may change only when they demand it**, with a reason. "No running" must be
  allowed to turn Thursday's run into a ride. A fourth session must be allowed to land
  on a rest day.

A planned rest day is a standing session like any other. Friday's "Rest Day" is a day the
athlete was told about, and turning it into a session is a shape change that must be
accounted for and explained (§4.5).

### 4.4 Why advisory, and not enforced

The codebase already holds both answers to "the model was asked, but did it?":

- `coach/service/workouts.py::_enforce_rest_windows_generate` — deterministic. A `rest`
  constraint bypasses the model for those dates entirely.
- `coach/service/workouts.py::_warn_missing_boundary_benchmarks` — "warn, don't
  auto-insert".

The window belongs in the second tier, because breaking it is sometimes correct. There is
no `--ignore-commitment` flag; the rule is advisory by construction, and §4.5 makes the
model's judgement visible instead of constraining it. `--force` skips the accept question
but still prints §4.5's report, so a forced run says what it did.

### 4.5 The model accounts for every standing session

Today the model is shown the eased sessions and may answer `keep` for each
(DESIGN_workout_revisions.md §7.1). The window widens that to every session in §4.2's
standing block and adds three more answers. For each session listed in the prompt's
"SESSIONS ALREADY STANDING" block, the response holds at most one of:

| answer | response entry | what apply does |
|---|---|---|
| **keep** | `{"date", "sport_type", "keep": true}` | nothing; the session and its event stand |
| **revise** | a full entry in the same `(date, sport)`, with `change_reason` | a revision on the same lineage; History shows the old load |
| **replace** | a full entry in another slot, with `"replaces": {"date", "sport_type"}` and `change_reason` | a void where it left and the new entry **on the same lineage** — a move or a sport change, exactly as adapt's new-sport rule and `workout swap` already do |
| **drop** | `{"date", "sport_type", "drop": true, "change_reason"}` | a rest day on the same date, **on the same lineage**, carrying the reason — adapt's vacate shape (§5.4) |

**`drop` is `replace` with a rest day.** The resolver rewrites a `drop` into a rest entry
on the same date whose `replaces` names the dropped session. So there is one path through
apply for every answer but `keep`, and a dropped ride leaves exactly what an adapted one
does: one event on Thursday, now titled "Rest Day", with the ride underneath it in History
and the coach's sentence as its reason. Rev. 3 wrote a void and let the coverage backstop
add a fresh rest row beside it — two all-day events on one day saying the same thing, and
a day that alternated ride, rest, ride across three runs collected a marker per run.

**A session the model does not mention is kept**, and the preview says so in a notice.
Silence is the ordinary way a JSON-mode model fails — the response format is prose in the
system prompt, not a schema the provider enforces — and a cancellation has to be said. The
coverage backstop and the void loop both stand aside for the standing block (§7): a
kept session is a row, so its date is covered, and its slot is spoken for.

**`change_reason` is one sentence, written for the athlete.** Not coach shorthand. "Your
long run stays 90 minutes but goes easy — Friday is now a hard day", not "deload,
polarised week". It is required on revise, replace and drop, and only for sessions in the
standing block: outside it the athlete has never seen the day, so there is nothing for a
change to be *from*, and the model writes those sessions as it always has. A 28-day
`workout generate` therefore produces a handful of sentences, not twenty-five. The
sentence is written in the language the athlete's profile is written in; the bracketed
Calendar labels stay English, as DESIGN_bot_simple_frontend.md decided for every fixed
string.

`workout generate`'s apply passes it into the row's `reason` column, which it does not do
today. That is what puts it on the Calendar `Reason:` line and on the `[Cancelled]` marker
(§5).

**The lineage rules.** `_lineage_for` already answers explicit-lineage appends (swap,
adapt's sport change) and appends over a void (new session). `replaces` maps onto the
explicit case: apply voids the named slot first, then appends the new entry with
`lineage_id` of the session it replaces. Nothing new in `db/workouts.py`.

**Resolving the answers.** `_resolve_kept` already carries the conflict rules for one
answer — a KEEP naming an unoccupied slot is dropped, an explicit session for a slot beats
a KEEP of it. `_resolve_standing` needs the rest of them, and they are as flat:

- A slot named by two entries — two `replaces` on one target, or a `keep` and a
  `replaces` — keeps the first in response order and drops the rest with a notice.
- A `replaces` whose destination falls outside `gen_start..gen_end` is refused outright
  and the source is left standing. Honouring only its half would void the session where it
  left and drop the entry where it landed, and the session would vanish.
- A `replaces` whose destination slot is occupied by a standing session that is kept —
  explicitly, or by silence — is refused, both sessions stay where they were, and the
  preview says so. The occupant's own answer is resolved first; only when it was itself
  revised away, moved or dropped is the destination free. A second call to the coach was
  considered and rejected: it costs a call, answers differently each time, and would run
  inside apply, where nothing else is nondeterministic. Refusing loses nothing.
- A `replaces` naming a source outside the standing block is refused: the run is only
  answering for the days it was shown.
- A full entry on a date whose only standing session is a rest day is treated as replacing
  that rest day, on its lineage, whether or not the model said `replaces`. A rest day and
  a session on the same date cannot both be true, so the model is not offered the choice.
  Friday's tempo run therefore retitles Friday's event in place, with the rest day in its
  History and no `[Cancelled] Rest Day` beside it.

**The preview.** Built from what apply *will* write, not from what the model said —
because DESIGN_workout_revisions.md §9's no-op rule silently suppresses a revision whose
prescription is unchanged, and because §5.5's deterministic passes remove sessions the
model never spoke about. The no-op comparison lives in the write path today
(`db/workouts.py::WorkoutChange._write`, `_same_prescription` against the live row), and
apply calls `append` unconditionally, so the proposal step runs the same comparison
against the standing rows it already loads and marks the equal ones `kept` before the
table is drawn. Reporting the answers instead would tell the operator a wording-only
revision changed the day, and would stay silent about a rest constraint that wiped one.

```
Sessions you were already told about (next 7 days):
  Thu 11 Sep  Long ride   90m   kept
  Fri 12 Sep  Rest Day          → Tempo run 45m       fourth session of the week
  Sat 13 Sep  Long run    90m   → 60m easy            no hard day after Friday's tempo
  Sun 14 Sep  Rest Day          kept
```

and, for the "no running" correction:

```
  Thu 11 Sep  Run         60m   → Ride 60m            no running while the knee settles
```

A dropped session prints as `→ Rest Day` with its reason; a moved one as `→ Sun 14 Sep`;
a session the model did not mention as `kept (not mentioned by the coach)`.

### 4.6 The prompt block

One block, "SESSIONS ALREADY STANDING". Each session carries tags rather than sitting in a
separate section, so the model sees it once:

- `[COMMITTED]` — in §4.2's committed set, so the §4.3 rule applies. A manual session
  past the window carries the next tag and not this one.
- `[BENCHMARK: ftp_20min]` — a scheduled fitness test. The strongest commitment on the
  calendar, since the athlete arranges to be fresh for it. Moving or dropping one needs a
  reason that says why the test can wait.
- `[ADDED BY THE ATHLETE]` — a manual session (§5.3).
- `[REST DAY]` — a planned rest day, so the model knows it is committing to a shape
  change when it fills it.

**The block holds the standing block only, and the easing rule now lives inside it.**
Rev. 2 kept `workout generate`'s carry-over rule span-wide, which is where the eased
sessions are read from today (`adaptation_count > 0` over the whole span). Past the
window the plan is the plan, and that now includes a session `workout adapt` eased there:
`workout generate` rewrites it, and because a `generate` revision is where
`db/workouts.py::_adaptation_tally` stops its walk, the easing count resets with it. What
the coach loses in the flag it gets back in the record: the block's past constraints
(§6.1) say *why* a week went quiet, and the block-progress section already says how much
was missed. Inside the window the eased session is listed with its tag and its earlier
form, and the model is told to keep it unless the moment the easing answered has passed.

`workout adapt` is unchanged here: it keeps its tag over its whole reach, because there
the tag is not about continuity but about not easing a session twice from an
already-reduced baseline.

**Both commands are shown what a session used to be, not just that it changed.** Today the
line reads `[ALREADY EASED 2x, most recently 3 days ago — do not compound]`
(`coach/formatting.py::_planned_summary`). The model is told *that* the numbers are
reduced and *ordered* not to reduce them again, but never shown from what — so it cannot
tell a 60-minute easy run that started as a 90-minute threshold session from one that
started as a 65-minute steady run, and the instruction has to be a flat rule. The first
form is already hydrated onto every session (`original_duration_minutes`, `original_tss`,
`original_rpe`, `original_date` — `db/workouts.py`), read off the lineage's first
revision at no extra cost. So the count and the recency stay, and "do not compound" is
replaced by the numbers:

```
- 2026-09-13 (RUN): Long run | Expected duration: 60m, RPE: 4, TSS: 45
  [first prescribed as 90m, RPE 7, TSS 95 — eased 2x, most recently 3 days ago: short on time]
```

"First prescribed" is the honest label: the numbers are the session's first form, not the
one just before the easing.

**A constraint outranks a fresh easing.** Inside the window both rules are live, and they
can disagree: a "no running" line in the profile over a run adapt eased two days ago. The
easing answers *how is the athlete today*; a constraint answers *what may this athlete do
at all*. The second wins, or the calendar keeps a run under a no-running instruction.

**Each session is shown** with its date, sport, title, duration, target, first form and —
for committed sessions — its description, so a revision can be minimal rather than
re-invented. The block replaces the current one, whose closing line — "every other day in
this window is yours to write from scratch, and a date this list does not name is not
spoken for" — now means the opposite of what it says and is rewritten with it.

**The instruction is about the present.** Rev. 3 rendered a "what changed since these
sessions were written" section here, reconstructed from snapshots, so that "avoid visible
changes" could be decided. §6.1 explains why that is gone. The instruction that replaces
it is decidable without any history:

> Keep each standing session unless it contradicts the athlete's profile, the plan, or a
> constraint as they stand today. If it does, name the line it contradicts in the
> athlete's sentence, and make the smallest change that resolves it. Wording is never a
> contradiction.

## 5. A removal leaves a trace

### 5.1 One label per change kind

The `workouts` table is append-only (DESIGN_workout_revisions.md §2), so "there is no
session on Thursday any more" is said by appending a **void revision**. Every void
carries the kind of the change that made it:

| kind | who decided | Calendar word |
|---|---|---|
| `rm` | the athlete cancelled it | `[Deleted]` |
| `stand-down` | the athlete called the goal off | `[Deleted]` |
| `generate` | the coach did not keep it | `[Cancelled]` |
| `adapt` | the coach dropped it | `[Cancelled]` |
| `swap` | it moved (void where it left, copy where it landed) | none: the event moves |

`[Deleted]` is what `google_calendar.py::sync_workout` draws today for the `rm` case, and
it keeps meaning "you did this". `[Cancelled]` is new and means "your coach did this".
Both carry the void's `reason` as a `Reason:` block, which §4.5 now fills with the coach's
own words instead of "Not in the regenerated plan".

With §4.5's `drop` landing a rest day on the session's own lineage, a coach void with no
replacement is the exception, not the rule. It happens in three places: a manual session
the coach writes over in its own slot (§5.3), a standing session displaced from a slot a
moved session lands in (§4.5), and the second and later sessions on a date a `rest`
constraint clears (§5.5). Each is one event beside another, and each is explicable in a
sentence: "the session you were told about is marked cancelled; the one that replaced the
day is next to it".

**The label comes off the change kind, for a surviving session too.** `sync_workout`
decides today with one line: `is_modified = bool(mod_reason)`, and a session with a reason
is titled `[Adapted]`. `workout generate` writes no reason today, which is why its
sessions render plainly — and §4.5 changes that. Left alone, every session it revises
would read `[Adapted]`, the word that means "your coach eased this because of how you were
doing". So the prefix is read off the hydrated row's `change_kind`, the way the void words
above already are:

| change kind | prefix |
|---|---|
| `adapt` | `[Adapted]` |
| `generate` | none |
| `add` | `[Manual]` (unchanged) |

### 5.2 Which voids keep their event

`ATHLETE_VOID_KINDS = ("rm", "stand-down")` has two consumers asking different questions.
`coach/service/adaptation.py::workout_adapt` asks **who asked for this?** — so the coach
is not told the athlete cancelled a day the plan merely stopped scheduling. That is about
authorship, and the constant stays as it is for that job. `calendar_reconcile.py::_plan`
asks **should this day leave a trace?**, and borrowed the authorship set because the two
happened to coincide. They no longer do:

```python
def leaves_trace(void, change) -> bool:
    """Whether a void keeps its Calendar event, retitled, rather than the event being
    torn down (DESIGN_plan_change_continuity.md §5.2)."""
    return (
        void["change_kind"] in ATHLETE_VOID_KINDS   # the athlete's own: always
        or void["source"] == "manual"                # a session they added: always
        or (change["commitment_end"] is not None     # committed when it was written
            and void["date"] <= change["commitment_end"])
    )
```

In one sentence: **a day you were counting on does not disappear — it is marked, with a
reason.** Outside the window, a coach-removed session the athlete never looked at is torn
down as today, so a 28-day rewrite does not litter the calendar.

**A void is read from its own lineage, a session from its slot.** `_plan` today asks for
the slot's live row and, finding the lineage no longer owns one, tears the event down.
That is right for a session superseded in place, and wrong for a void: `live_workouts` is
"highest id in the slot", so a void written and then covered by a new session in the same
slot — §5.3's manual case, §4.5's displaced occupant — is never the slot's live row, and
rev. 3's marker would have been torn down the instant it was written. So `_plan` reads
the lineage's newest revision. When that row is a void, `leaves_trace` decides. When it
is a session but not the slot's live row, the lineage was superseded and the event is
torn down as today. `workout add` supersedes a same-sport session the same way
`workout generate` does, and gets the same void-first order (§5.3) so that a session the
coach wrote and the athlete typed over is marked rather than erased.

**The window is stamped on the change, not re-read at sync time.** Rev. 2 had
`window_end` reach `_plan()` as a settings read. That makes the answer depend on *when the
sync runs*: write with `--no-sync` (`calendar_reconcile.no_calendar_sync`), push a
fortnight later, and a void that was outside the window when it was written is inside it
by the time it is reconciled, because the window has moved forward. So each
`workout_change` records the `window_end` in force when it ran, and the rule reads it
back. The decision is a property of the removal, not of the clock. No lower bound is
needed: no batch writes into the past, so a void's date is never before the day its change
ran.

**A rollback reads the original's stamp.** `workout rollback` restores an earlier row by
copying it forward with `restored_from` pointing at the original, and `_resolve_kind`
already follows that pointer so the copy reports the original change's kind. The window
stamp is resolved the same way, in the same place: follow `restored_from` to the original
row, then to its change, and read that change's `commitment_end`. A re-instated
`[Cancelled]` is judged exactly as it was the first time. A void the rollback writes
itself (kind `rollback`, a slot that held nothing before the undone change) is torn down
as today.

**A mark with no event gets one.** `_plan` re-pushes a trace-keeping void only when an
event already exists (`if event_id and ...`). A session written and dropped between two
syncs never had one, and would leave no trace at all. When a void leaves a trace and has
no event, the push creates it.

### 5.3 The manual session needs a void to keep

The `source == "manual"` clause has nothing to fire on today: when `workout generate`
writes over a manual session, `_lineage_for` starts a new lineage and no void is written
for the old one (§1.2). So `workout_generate_apply` voids a manual session it does not
keep *before* appending over its slot, the same order it already uses for every other
displaced session. The new session then starts its own lineage by the append-over-void
rule; the manual lineage's newest row is a void, `source` hydrates as `manual` because
`source` is derived from the lineage's first change kind (`db/workouts.py::_hydrated`),
and §5.2's lineage read finds it. DESIGN_workout_revisions.md §12's "Replaced the session
you added" notice stays.

Manual sessions are in the standing block across the whole span (§4.2), so the coach
answers for each one and the same-slot case arises only when it revises one — which, by
the append rule above, is a replacement rather than a revision. The coach's session does
not continue the athlete's lineage, or it would render `[Manual]`.

### 5.4 A dropped session is a rest day on its own lineage

Thursday's dropped ride becomes Thursday's "Rest Day" in the ride's lineage, the reason
on it, the ride in its History. That is what `workout adapt`'s vacate rule already
produces (`coach/engine/workouts.py::_vacate_task`), and the same event, retitled, is
what the athlete sees in both cases. The `[Cancelled]` word does not appear: nothing was
left without a replacement. The rest row's body carries the coach's sentence — "Long ride
cancelled — never two hard days in a row" — and not the coverage backstop's "no session
planned for this day", because the backstop never runs for the date: the rest entry is a
row, and the date is covered.

### 5.5 Removals no model answer explains get a real reason too

Several paths remove a standing session without passing through the model's answers, and
apply's void loop stamps all of them "Not in the regenerated plan" — the string §5.1
exists to replace, now promoted from a database column to the athlete's phone:

| pass | what it writes |
|---|---|
| `_enforce_rest_windows_generate` | the rest row takes the lineage of the first standing session on that date, reason the constraint's own title; any further session on the date gets a void with the same reason and a `[Cancelled]` marker |
| `_drop_benchmark_collisions` | nothing: it drops a *proposed* row, and the standing session it protects is the test |
| a `replaces` displacing an occupant the model dropped | the occupant's void carries the occupant's own `change_reason` |

The fallback for anything else becomes "your coach replaced this day", which at least
names who did it.

### 5.6 Markers stay

The first draft swept markers dated before today. That is dropped: a marker is the
athlete's record that a decision was made on that day, and a past `[Deleted]` already
stays forever. `workout prune-calendar` continues to keep every event a row still claims.

Repeated `workout generate` runs on the same day do not stack markers: every answer but
`keep` lands on the session's own lineage (§4.5), so a day re-decided in a later run is
the same event, retitled again.

## 6. The coach reads the present and writes what it did

### 6.1 What the coach reads

Rev. 2 and rev. 3 both had `workout generate` told *what changed since these sessions
were written*: rev. 2 by copying `plan show`'s diff onto the macrocycle as a pending
change, rev. 3 by saving a profile snapshot on every `generate` change row and diffing
today's against the newest one. Both were answers to one worry — that "avoid visible
changes if possible" cannot be decided without knowing what changed. Rev. 3's own worked
example shows the worry is misplaced.

The two cases it used were "describe her sessions with more detail on form cues" and
"she's had knee pain — no running for now". Look at what the coach sees in each without
any history. In the knee case the profile says no running and Thursday holds a run: the
session contradicts the profile *as it stands today*, and the coach can see that
directly. In the form-cues case Thursday's ride matches the profile in sport, duration,
intensity and day: nothing contradicts anything, so the coach keeps it, and a rewrite of
the wording is thrown away by the no-op rule anyway. The question the coach has to answer
is not "what changed" but "does this standing session contradict the profile, the plan or
a constraint as they are today". That question needs no history, and asking it of the
present is more truthful than asking it of an edit: it holds whether the profile was
edited once or three times since Thursday was written.

The reconstruction also failed on its own terms. The newest `generate` change is not the
one that wrote Thursday: the companion's runway button is a bare `workout generate` that
extends the far end of the schedule and, under rev. 3, records today's profile as it
does, so the `-d today..` run that follows diffs today against today and finds nothing. A
span straddling days written by three earlier runs would need three diffs, each attached
to some of the sessions. And a session last touched by `workout adapt`, `workout swap` or
the athlete's own `workout add` had no snapshot at all. Removing the diff removes the
snapshots on change rows, the walk back along each lineage, the manual-session special
case, the first-run-after-migration fallback and the rule about which profile fields join
which diff. That is most of what rev. 3's §6 was.

So `workout generate` reads what it reads today, with two additions and a rule:

- **Today's profile, the plan and the current constraints**, as now. The plan's strategy
  text and block list already reach the prompt (`coach/engine/workouts.py`,
  `strategy`, `meso_text`).
- **The standing block** (§4.6).
- **The current block's past constraints.** Today the prompt is given only the
  constraints still active on or after the span's start
  (`coach/service/workouts.py`, `get_constraints(gen_start)`). A constraint that ended
  last week — "ill 1 to 5 September" — is not shown, so the coach sees three missed
  sessions in the block-progress section and not why. The lower bound becomes the
  current mesocycle's start date, and constraints that ended before the span are rendered
  under their own heading, marked past. There is no "debilitating" flag to filter on —
  a constraint row has a hard-rest switch and free text — and a travel week explains a
  quiet week as well as an illness does, so all of them are listed; a block holds a
  handful. This is what makes §4.6's reset safe: the coach that rewrites the week after
  an easing knows what the easing answered.
- **The rule** in §4.6: keep unless it contradicts a line the coach can name.

What the coach is *not* given more of is health data. The prompt already carries fifteen
days of daily metrics (`coach.metrics_lookback_days`), the completed activities in the
same window, the baseline they are judged against, and the block's planned-versus-actual
progress week by week. For a week more than fifteen days old the coach needs why it went
quiet, which the past constraint gives, and how much was missed, which block progress
gives; the day-by-day HRV from four weeks ago adds size and no decision. The setting is
there if fifteen proves short.

### 6.2 What `plan generate` may write

There is one place a "what changed" sentence is cheap, true and useful: the plan itself.
`plan generate` runs with the staleness diff and the re-shaping verdict in hand
(`cli/staleness.py::explain`), and it writes the strategy text fresh from today's
profile. It may open that text with one paragraph saying what moved since the previous
version and why the plan is now shaped as it is. `plan show` displays it because it
displays the strategy; `workout generate` sees it because it is handed the strategy. No
new column, no new plumbing.

Two limits, stated so they are not rediscovered. The paragraph exists only on the
`plan generate` route: on `plan keep` no plan is written, and the operator has just said
the plan did not need re-shaping, so there is nothing to summarise at plan level. And
nothing in §4 depends on it. The coach's rule is about the present; the paragraph is
background for its judgement and for the athlete's sentence, not a mechanism.

### 6.3 What the coach writes: one sentence per session, one for the week

Rev. 2 asked the re-shaping verdict for one sentence covering the whole edit. That was
wrong twice over. The verdict is handed the reason, the diff, the strategy prose and the
block list and nothing else (`coach/engine/planning.py::_plan_reshape_verdict`) — it has
never seen the calendar, so "starting this Friday" was a guess about a day it does not
know exists. And the sentence was written before anything was decided, then stamped onto
every session the run wrote, so it could contradict the outcome.

The call that decides is the call that writes. Both sentences come out of the
`workout generate` response, grounded in what it just did:

- **Per session** — `change_reason` on every standing session it revises, moves or drops
  (§4.5). One sentence, for the athlete, about that day, naming the line it answers:
  "your profile asks for four sessions a week, so Friday becomes a session".
- **For the week** — one `athlete_note` member on the response: one line about the change
  as a whole, for the morning push. Omitted when nothing the athlete would notice changed.

One field per session, not two: rev. 2 had `Reason:` for the session and `Change:` for the
edit, which was a real distinction only while `Change:` was about the whole edit. Once
both are per-session they are the same sentence written twice, so `Change:` goes.

The re-shaping verdict keeps `{"reshaping", "why"}` and gains nothing. It is advice to the
operator about rebuilding the periodization, and nothing the athlete ever sees depends on
it having run.

### 6.4 Where the sentences go

**Into the preview.** The per-session sentences are the right-hand column of §4.5's table;
the week line prints above it. The operator can reject the proposal.

**Onto the Calendar, as `Reason:`, once.** Today `calendar_lineage.py::_entry` prints a
revision's own `reason` as `Reason:` and the change's `summary` as `Change:` when the two
differ. For an adaptation the summary is the batch's overall rationale — "HRV suppressed
three mornings running" — which is a second *why*, not a *what*, so the label misleads
(DESIGN_calendar_lineage.md §3's example shows exactly this). For a `workout generate`
the summary is the coach's four-sentence microcycle reasoning, which would then appear in
the History of every session it wrote.

So: **one label, one meaning.** `_entry` prints `Reason:` from the revision's own reason,
and falls back to the change summary under that same label only when the revision has
none. `Change:` disappears from the lineage renderer. The full batch text stays where it
belongs, in `workout batches`. DESIGN_calendar_lineage.md §3's example is corrected with
this change.

The current revision is rendered by `sync_workout`, which today prints `Reason:` for
removed and adapted sessions only, so the newest change is never described on the event
the athlete opens. It prints the reason for a `generate` revision too — under the label
rule in §5.1, so the session is not also re-titled `[Adapted]`.

**Into the morning push.** DESIGN_plan_staleness.md §9 keeps the companion silent about
the staleness *flag*, and that stays. A finished, applied change is different: it has
already happened to the athlete's week. The bot has no path for an unprompted message
(it replies, or it runs `bot morning`), and none is added. Instead `bot morning` opens with
the week line of the newest `generate` change **that carries one** and is newer than the
last change delivered:

> *"Your coach changed your week: four sessions a week now, never two hard days in a
> row."*

A bare `workout generate` that extended the schedule carries no note, so it neither
speaks nor silences an older note behind it. A `rollback` that undid a change whose line
was delivered prints *"The change to your week was undone."* — no "yesterday", since the
change may be older, and one line however many batches the rollback covered. A rollback
before the line was ever sent prints nothing: the athlete never heard of the change.

**The push tracks the change it delivered, not the day.** `MORNING_MARKER` holds a date
(`cli/bot.py`), which is enough for per-day idempotency and not enough here: a forced
re-run would repeat the line, and the push's one silent early return — an exhausted
schedule with nothing on today (`run_bot_morning`) — stamps the marker without sending. A
second marker holds the id of the last change whose line was delivered, and is written
only when something was actually sent.

### 6.5 The staleness chain, repaired on its own account

These stood in rev. 3 as inputs to the diff. They stand now because `plan show` is wrong
without them, and they are useful whatever `workout generate` does.

**A profile edit currently swallows a concurrent threshold move.** `config_changed`
(`coach/service/prompt.py`) checks the profile hash first and **returns on the first
thing it finds**; thresholds are only examined when the profile is unchanged, and the
threshold loop itself returns on the first key in alphabetical order rather than the
largest move. So: the athlete retests, FTP 250 → 265, the plan flags it correctly. Before
anyone looks, the operator also corrects `preferences`. Now `plan show` says "athlete
profile changed: preferences" and shows that diff alone — the FTP line is gone from the
reason, the coach's verdict never learns the threshold moved 6%, and `plan keep` stamps
**both** snapshots, dismissing it for good. The fix is control flow: collect every reason
instead of returning on the first, in both loops, and report "athlete profile changed:
preferences; ftp changed 250 → 265 (+6.0%)".

**Goals and the plan-shaping constraints are in neither.** `goals_hash` and
`constraints_hash` are read in exactly one place — `coach/service/planning.py`, deciding
whether `plan generate` may reuse an existing strategy. They are not part of
`config_changed`, so a moved race date does not flag the plan stale. A moved goal is the
largest reshaper there is. The snapshots to diff against are already written on every
plan save: `goals_snapshot`, and `constraints_snapshot`, which holds the `replan = 1`
subset the hash fingerprints. Both join the staleness reason and the diff.

`all_constraints_snapshot` does not. It holds every active constraint, tactical ones
included, and exists so `plan show` can list what the coach saw rather than print
"constraints considered: none" while a small constraint plainly shaped the strategy. It is
a display aid. Joining it to staleness would flag the operator's plan every time the
companion athlete says "not Wednesday next week", and this design does not touch it.

**The stamp must clear what it flags.** `update_macrocycle_config_hash`
(`db/periodization.py`) writes only `config_hash`, `config_snapshot` and
`profile_snapshot`. Once goals and constraints can flag the plan, `plan keep` and the
proceed-anyway path in `workout generate` must re-stamp `goals_hash`, `goals_snapshot`,
`constraints_hash` and `constraints_snapshot` too, or a kept plan flags again tomorrow.

**Guidance names the right command.** `cli/staleness.py::guidance` stops saying a wording
change is picked up by "your next `workout generate`" — a bare `workout generate` does
not reach the days already scheduled (§1.1) — and becomes *"…keep the plan —
`workout generate -d today..` applies it to the days already scheduled, and a bare
`workout generate` to the days after them."*

## 7. Implementation notes

**One block, one resolver.** `_resolve_kept` becomes `_resolve_standing`: it maps
`keep`, `revise`, `replaces` and `drop` entries onto the standing block under §4.5's
conflict rules, turns each `drop` into a rest entry that replaces its session, treats a
full entry on a rest-only date as replacing the rest day, adds a `keep` for every
standing session left unmentioned, and hands every pass after it a uniform list of full
sessions. The void loop then has nothing to void in the standing block — every slot is
either kept or explicitly replaced — and `_fill_coverage_gaps` finds every standing date
covered.

**The window is a shared revision layer, not a second `adapt`.** Every continuity step
hands `workout generate` something `workout adapt` already had: a view of the standing
sessions, `keep`, lineage carried across a sport change or onto a rest day, a required
`change_reason`. That is the same machinery and it should be built once: the standing
block, `_resolve_standing`, the §4.5 report, and the lineage-carrying apply. What stays
separate is the policy each command runs it under: **adapt answers "how is the athlete",
generate answers "what does the plan say".** Adapt's cause is the athlete's state, it may
not reshape the block (DESIGN_block_boundary.md §2), and it holds its easing tag over its
whole reach. Generate's cause is the plan and the profile as they stand, it may reshape
freely outside the window, and it reads the easing tag only inside it (§4.6).
`replaces` across dates is new to both; adapt may adopt it later so that a move keeps its
history there too.

**`workout generate` still resets the easing tally** wherever it revises, inside the
window or out. A `keep` appends nothing, so a kept session's tally stands; inside the
window that is information beside the session's first form (§4.6), not a stale flag.

**`plan show` asks the verdict, and it is cached.** DESIGN_plan_staleness.md §10 has
`plan show` print the diff and ask nothing, "a read-only command makes no network call".
It is also the command whose whole job is to help the operator decide, which is what the
verdict is for, so it asks. But §10 declined to cache the verdict for a reason that stops
holding the moment it does — "both questions stamp on 'keep', so the same change is asked
about once" — and `plan show` stamps nothing. So the verdict is cached on the macrocycle
against the snapshot it was asked about, and §10's "Where it is not asked" and
"Deliberately not done" paragraphs are amended. The call fails open as it does today
(`coach/service/planning.py::plan_reshape_verdict`): the plan prints whatever the network
does, with an aside where the verdict would have been.

**Already landed.** `workout_generate_apply` carried a test's `benchmark_type` onto
whatever it wrote over the slot, because the append carries the field forward and
`workout generate` never passed `clear_benchmark` the way `workout adapt` does. Fixed in
`c2d11f9`, with a test: an easy run written over a scheduled test is not a test.

**Migration.** Two nullable columns on `workout_changes`: `note` (the week line, §6.3)
and `commitment_end` (§5.2); the cached verdict on `macrocycles`; a second bot marker in
`settings`. `schema_version` bumped. No snapshots on `workout_changes`: §6.1.

**ARCHITECTURE.md** needs `leaves_trace`, the lineage read in `_plan`, the settings row,
the past-constraints section and the `[Cancelled]` word.

## 8. Tests

`tests/test_prompt_gates.py` — the "SESSIONS ALREADY STANDING" block, the `replaces` and
`drop` schema members, the per-session `change_reason` and the keep-unless-contradicted
instruction are one gated region: present together when the standing block is non-empty,
absent together when it is not. The past-constraints heading appears when the current
block has a constraint that ended before the span, and not otherwise.

`tests/test_workout_generate_window.py` (new) —
- `keep`, `revise`, `replaces` and `drop` each produce the expected rows, all on the
  session's own lineage; a `drop` is a rest row on that lineage with the reason, and the
  date has exactly one live row.
- A standing session the model does not mention is kept and flagged in the preview.
- A full entry on a rest-only date replaces the rest day on its lineage.
- `change_reason` reaches the row's `reason` column.
- Window counting: `N=0` protects nothing, `N=1` today only, `N=7` today through day six.
- The committed set is the intersection: a forward-selected span leaves it empty even
  when the window holds sessions; a completed session today is excluded and preserved.
- A manual session past the window is in the standing block; a benchmark past the window
  is not.
- A wording-only revision (same load, new prose) is reported as `kept` and appends nothing
  (DESIGN_workout_revisions.md §9) — the preview reads the comparison, not the response.
- The `replaces` conflict rules: duplicate targets, a destination outside the span, a
  source outside the standing block, a destination whose occupant is kept (refused, both
  stand), a destination whose occupant was dropped (allowed, occupant's void carries its
  reason).
- The easing tag applies inside the window and not outside it; outside, a rewrite resets
  the tally.
- A constraint-shaped profile line outranks a two-day-old easing.
- A rest constraint over a date with two standing sessions: the rest row carries the first
  lineage and the constraint's title; the second gets a void with the same reason.
- The block's past constraints are fetched from the mesocycle start; one that ended
  before the span renders under the past heading; one still active does not.

`tests/test_calendar.py` — `leaves_trace` for each clause: athlete kind outside the
window, manual outside the window, coach kind inside, coach kind outside (torn down),
`N=0`. The window is read from the change, so a void reconciled a fortnight later gets the
answer it had when written; a rollback's restored copy reads the original change's stamp.
`_plan` reads a void from its lineage: a manual session `workout generate` writes over in
the same slot keeps its event, retitled `[Cancelled]`; a session superseded in place by
a non-void is still torn down. A moved or sport-changed session updates its event in
place; a dropped session's event is retitled "Rest Day" with the session in History and
no second event on the day. A trace-keeping void with no event gets one. `[Deleted]` for
`rm`/`stand-down`, `[Cancelled]` for `generate`/`adapt`; a `generate` revision with a
reason is **not** titled `[Adapted]`, an `adapt` revision is.

`tests/test_calendar_lineage.py` — a History entry prints `Reason:` from the revision, the
batch summary under the same label only when the revision has none, and `Change:` never.

`tests/test_cli_plan_staleness.py` — `config_changed` reports a profile edit and a
concurrent threshold move together, and two threshold moves together; goals and the
plan-shaping constraints flag the plan and render a diff; a tactical constraint does not;
`plan keep` clears a goal-flagged plan; the verdict is cached against the snapshot, so
`plan show` asks once for an unchanged edit and prints the plan when the call fails.

`tests/test_cli_bot.py` — the push opens with the week line once, the day after a
`workout generate` that carried one; a bare `workout generate` in between does not
silence it; a forced second push the same day does not repeat it; a push that returns
silently does not consume it; it says "undone" after a rollback; it says nothing when the
rollback preceded the first push.

`tests/test_periodization.py` — an easy run written over a scheduled test is not a test
(landed, `c2d11f9`).

## 9. Worked example

`workout_commitment_days` is 7. It is Wednesday 10 September. The calendar holds a
90-minute ride on Thursday, a rest day on Friday and a 90-minute long run on Saturday. The
operator corrects `preferences` from "three sessions a week" to "four sessions a week,
never two hard days in a row".

**Today.** `plan show` shows the diff and the coach says "keep". The operator runs
`plan keep`, then `workout generate -m`. Thursday's ride event is deleted and a "Rest
Day" appears in its place with "no session planned for this day"; Saturday becomes
something the athlete has never seen; the whole block is re-rolled including the parts
the correction never touched. The athlete finds out by opening their phone.

**With this design.** `plan keep` stamps as before — it does not matter. `workout
generate -m` reads today's profile, which asks for four sessions and no two hard days in
a row, and sees Thursday, Friday and Saturday standing in the committed set. Thursday's
ride contradicts nothing: it keeps it. Friday's rest day contradicts the four-session
line: it puts a tempo run there, replacing the rest day. Saturday's threshold run now sits
the day after a hard day: it revises it down to easy. It writes a sentence for each, and
one for the week.

```
Your coach's note to the athlete: Four sessions a week now, never two hard days in a row.

Sessions you were already told about (next 7 days):
  Thu 11 Sep  Long ride   90m   kept
  Fri 12 Sep  Rest Day          → Tempo run 45m   your profile asks for four sessions a week
  Sat 13 Sep  Long run    90m   → 90m easy        your long run goes easy — Friday is hard now
```

On the calendar, Thursday is untouched. Friday's event is retitled in place from "Rest
Day" to "Tempo run" — no `[Adapted]` prefix, since `workout generate` wrote it — with the
rest day in its History and `Reason: your profile asks for four sessions a week` in its
body. Saturday's event updates in place, its History showing the threshold version above
the easy one, each with its own `Reason:` and no second one from the batch. On Thursday
morning the push opens with the week line, once.

Had the correction instead been "no running", the preview would have said
`Thu 11 Sep  Run 60m → Ride 60m   no running while the knee settles`, and Thursday's event
would have been retitled in place with the run in its History — the same thing adapt does
today. Had the coach dropped Thursday outright, Thursday's event would read "Rest Day"
with the ride in its History and the coach's sentence as its reason, and nothing else on
the day. Had a rest constraint covered Friday, the tempo run would have been replaced
deterministically, on the rest day's lineage, and the reason would read the constraint's
title.

## 10. Deliberately not done

**No profile diff for `workout generate`.** See §6.1. The coach is asked about the
present, and the reconstruction rev. 3 built to ask about the past was both unnecessary
and wrong on the common route.

**`preferences` is not split.** The cheapest way to reduce this whole cascade is to stop
firing it so often: split the field into a plan-shaping half and a session-flavour half.
DESIGN_plan_staleness.md §4 rejected having a model judge whether a prose diff is
plan-relevant, but a *structural* split is deterministic and cheap. It is a separate
change and should be done on its own merits.

**The whole horizon is not carried forward.** Showing the model every standing session
across the full span would partly reverse DESIGN_workout_revisions.md §7.1's choice —
"every other day stays the model's to write, which is what keeps `generate` a
regeneration rather than a second `adapt`". The standing block is the window plus the
athlete's own sessions, and nothing more. Requirement 2 of §2 is therefore met inside
the window only, and that is the intended trade.

**Benchmarks past the window are not held.** See §4.2: the coach re-places them from the
record it already has.

**The metrics window is not widened.** See §6.1.

**No override flag.** See §4.4.

**`commitment-days` is not routable.** See §4.1.

**No unprompted bot message.** See §6.4: the morning push is the channel.

**`plan keep` is unchanged** except for what it stamps (§6.5). It asks nothing, which is
right: by the time it runs the operator has decided, and `plan show` is where the coach
speaks. Nothing the athlete sees depends on it any more.

**`plan diff` still does not render profile changes.** DESIGN_plan_staleness.md §8 left
this as a follow-up and it stays one.

**Two things found in review, out of scope here.** The companion's runway button runs
`_confirm_out_of_date_plans` before its bare `workout generate`, so a companion athlete
already sees the expert staleness question, the unified diff and command names as
buttons — DESIGN_plan_staleness.md §10's "the bot has no path to this question" is no
longer true, and §6.5's reworded guidance will reach them through the same path. That
belongs to DESIGN_bot_simple_frontend.md. And `plan rollback` prints a key the service
stopped returning (`cli/plans.py`, `archived_workouts`), so every successful rollback
raises after the write; a one-line fix with no test at the command layer.
