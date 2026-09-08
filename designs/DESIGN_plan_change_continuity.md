# Design: Continuity when a plan change reaches the sessions

**Status:** Draft · **Date:** 2026-09-08 (rev. 3) · **Branch:** worktree-plan-change-continuity

Revision 3 follows a second review against the code. It found that rev. 2's central
mechanism — the *pending change* — never fires on the route the design itself calls the
common one, that the call it asked to write the athlete's sentence cannot know what the
sentence says, and that writing the model's reason onto a session re-labels every
regenerated day `[Adapted]`. The pending change is gone, replaced by something derived;
the sentence moved to the call that decides; and the label now follows the change kind.
Revisions 1 and 2 are in the branch history.

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
   selector — `workout generate -d today..`, or `-m` for the current block. A bare
   `workout generate` opens the day after the schedule's coverage ends
   (`cli/workouts/generate.py::_resolve_span`), so without a selector the change does not
   reach the athlete for up to `workout_generation_span_days`.
4. That selected run rewrites the span wholesale.
   `coach/service/workouts.py::workout_generate_apply` voids every live session in the
   span the new proposal does not re-propose, then writes the new ones.

Step 4 is where the surprise is made. Step 2 is where the *reason* for it is thrown away
(§6.1).

### 1.2 What the athlete actually sees

Take a concrete week. It is Wednesday. The calendar holds a 90-minute ride on Thursday, a
rest day on Friday and a long run on Saturday. The operator corrects `preferences` from
three sessions a week to four, and runs `workout generate -m`.

**The rewrite is blind.** The generation prompt is shown nothing about the sessions
currently standing in the horizon, except the handful an adaptation already eased
(`adaptation_count > 0`, `coach/service/workouts.py::workout_generate`). So the model
rewrites the span with no idea what the athlete was told last week, and no idea what the
correction was. Days the correction has nothing to do with are re-rolled along with the
rest, because the model has nothing to hold them steady against.

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

**A session the athlete added by hand vanishes without even a void.** When generate
writes over a manual session, `_lineage_for` starts a new lineage and records the
replacement for DESIGN_workout_revisions.md §12's notice — but no void row is written
for the manual lineage. It simply has no live revision any more, `_plan` sees
`head is None`, and the event is deleted.

The record is intact in the append-only log, and `workout batches` can show exactly what
happened, but the athlete is not in a terminal. The Calendar is where the athlete meets
the plan (DESIGN_calendar_lineage.md §1), and there the day changed with no trace of a
decision. This bites hardest on a companion-mode instance, where the athlete has no shell
(DESIGN_bot_simple_frontend.md), but an expert-mode athlete gets the same unexplained
Thursday.

### 1.3 What already works

**A rewritten day in the same slot explains itself.** A day the regeneration re-prescribes
in the same `(date, sport)` keeps its `lineage_id`, so its event is updated in place and
the History block shows the earlier form, its load, its target and its reason
(DESIGN_calendar_lineage.md §3). That reads as a change to something the athlete
recognises.

**`workout adapt` already leaves the trace this design wants.** Its vacate rule requires a
replacement on any date it empties (`coach/engine/workouts.py::_vacate_task`), the
replacement carries the displaced session's lineage, and the event is retitled
"[Adapted] Rest Day" with the reason and the old session in History. Nothing in this
design changes adapt's behaviour except what §4.6 shows it about a session's earlier form.
The design is about making `generate` reach the same standard.

**A regeneration does not volunteer to rewrite.** `_resolve_span` opening after the
covered days means the destructive form has to be asked for. That is already a continuity
property; this design keeps it.

## 2. What "continuity" means here

Three things get conflated under the word.

1. **The near days should not move.** Whatever the athlete has already read and planned
   around — tomorrow, the rest of this week.
2. **A change should be proportionate.** A correction about how sessions are described
   should not re-roll a month of structure.
3. **A change should be explained where the athlete meets it.** Not "your Thursday is
   different now" but "you asked for four sessions a week, so from Friday there are four".

§4 answers the first inside a window. The second is answered inside the window and, on
purpose, only there: outside it the plan is the plan, and the regeneration is free to
rebuild (§7). §5 makes every removal inside the window visible, and §6 answers the third.

## 3. Decision

**Four parts.**

- **§4 — A commitment window.** A new setting, `workout_commitment_days`. The sessions
  standing inside it reach the generation prompt, and the model has to account for each
  one: keep it, revise its load, move it, change its sport, or drop it — each with a
  reason written for the athlete to read. A move or a sport change carries the session's
  lineage, exactly as adapt does. The preview lists what was written to each of them,
  before the operator accepts.
- **§5 — A removal leaves a trace.** A session the coach removes inside the window keeps
  its Calendar event, retitled `[Cancelled]` with the reason. A session the athlete added
  by hand keeps its event whenever the coach removes it, inside the window or not.
  `[Deleted]` stays the word for a removal the athlete asked for. The day a session leaves
  says where it went.
- **§6 — The change says what it is.** The coach that decides also writes: one sentence
  per changed session for the Calendar, and one for the week for the morning push. What it
  needs to read to write them — what changed since these sessions were written — is
  derived from a snapshot each regeneration leaves behind, not carried in a side channel.
- **§8 — Tests**, named up front, since the first draft had none.

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
no sessions. A window longer than `workout_generation_span_days` is clamped to it with a
notice — a promise about days no run can rewrite is not a promise.

### 4.2 Where it bites: the window *and* the span

**Only where the generated span overlaps sessions that already exist.** The operative set
is the standing sessions that are in the window **and** in the range being rewritten:

```
committed = standing sessions where today <= date <= window_end
                                 and gen_start <= date <= gen_end
```

Both bounds are load-bearing, and rev. 2 had only the first:

- A bare `workout generate` extends into empty days, so the intersection is empty and the
  prompt is the one it always was. The companion's runway button
  (DESIGN_runway_nudge.md §6) is a bare generate, so its no-preview flow is unaffected.
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

One set, computed once, used by the prompt block (§4.6), the preview (§4.5) and the trace
rule (§5.2).

### 4.3 Shape, load and wording

The distinction the window turns on has three levels, not two:

| | examples | how the athlete meets it |
|---|---|---|
| **Shape** | whether the day has a session, which sport, which day | visible at a glance on a calendar |
| **Load** | duration, intensity target, the interval structure | visible by opening it, and felt on the day |
| **Wording** | the prose, the title | visible by opening it |

Inside the window the rule is:

- **Wording alone is never a reason to touch a session.** The default for every standing
  session is `keep`. A rewritten description with the same load is churn, and the point of
  DESIGN_workout_revisions.md §9 was to stop producing it.
- **Load may change when the change that triggered the run calls for it**, in the same
  slot, with a reason. "Never two hard days in a row" may turn Saturday's 90-minute
  threshold run into 60 minutes easy. That is a revision of the same session; its History
  shows the earlier form.
- **Shape may change only when the change demands it**, with a reason. "No running" must
  be allowed to turn Thursday's run into a ride. A fourth session must be allowed to land
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
but still prints §4.5's report, so a forced run says what it did — and it still gets the
diff and still writes the sentences, because none of that depends on the staleness
question it skipped (§6.1).

### 4.5 The model accounts for every committed session

Today the model is shown the eased sessions and may answer `keep` for each
(DESIGN_workout_revisions.md §7.1). The window widens that to every session in §4.2's
committed set and adds three more answers. For each session listed in the prompt's
"SESSIONS ALREADY STANDING" block, the response holds exactly one of:

| answer | response entry | what apply does |
|---|---|---|
| **keep** | `{"date", "sport_type", "keep": true}` | nothing; the session and its event stand |
| **revise** | a full entry in the same `(date, sport)`, with `change_reason` | a revision on the same lineage; History shows the old load |
| **replace** | a full entry in another slot, with `"replaces": {"date", "sport_type"}` and `change_reason` | a void where it left and the new entry **on the same lineage** — a move or a sport change, exactly as adapt's new-sport rule and `workout swap` already do |
| **drop** | `{"date", "sport_type", "drop": true, "change_reason"}` | a void on the lineage, reason attached; the day gets a rest row that says where the session went (§5.5) |

A committed session the model does not account for is treated as dropped with the reason
"the coach gave no reason", and the preview says so in red. The model is never asked to
half-do anything: `keep` returns nothing to adjust, and a revision is written out in full.

**`change_reason` is one sentence, written for the athlete.** Not coach shorthand. "Your
long run stays 90 minutes but goes easy — Friday is now a hard day", not "deload,
polarised week". It is required on revise, replace and drop, and only for sessions in the
committed set: outside it the athlete has never seen the day, so there is nothing for a
change to be *from*, and the model writes those sessions as it always has. A 28-day
regeneration therefore produces a handful of sentences, not twenty-five.

Generate's apply passes it into the row's `reason` column, which it does not do today.
That is what puts it on the Calendar `Reason:` line and on the `[Cancelled]` marker (§5).

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
- A `replaces` whose destination is occupied by a session in a *different* sport is
  allowed — the occupant is in the committed set too, and the model must account for it
  separately. When the destination lies outside the span, it is refused as above.
- A `replaces` naming a source outside the committed set is refused: the run is only
  answering for the days it was shown.

**The preview.** Built from what apply *wrote*, not from what the model said — because
DESIGN_workout_revisions.md §9's no-op rule silently suppresses a revision whose
prescription is unchanged, and because §5.5's deterministic passes remove sessions the
model never spoke about. Reporting
the answers would tell the operator a wording-only revision changed the day, and would
stay silent about a rest constraint that wiped one.

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

A dropped session prints as `→ cancelled` with its reason; a moved one as `→ Sun 14 Sep`.

### 4.6 The prompt block

One block, "SESSIONS ALREADY STANDING". Each session carries tags rather than sitting in a
separate section, so the model sees it once:

- `[COMMITTED]` — in §4.2's set, so the §4.3 rule applies.
- `[BENCHMARK: ftp_20min]` — a scheduled fitness test. The strongest commitment on the
  calendar, since the athlete arranges to be fresh for it. Moving or dropping one needs a
  reason that says why the test can wait.
- `[ADDED BY THE ATHLETE]` — a manual session (§5.3).
- `[REST DAY]` — a planned rest day, so the model knows it is committing to a shape
  change when it fills it.

**The block holds the committed set only, and the easing rule now lives inside it.**
Rev. 2 kept `generate`'s carry-over rule span-wide, which is where the eased sessions are
read from today (`adaptation_count > 0` over the whole span). That rule and the window are
the same rule written twice: both exist because the athlete has already been told about
the session in its current form. Past the window they have not, and the design's own
position is that the plan is the plan out there. So the protected set becomes the
committed set — the same filter with a nearer end date — and this fixes a bug it inherits:
the easing tally resets only when a regeneration *rewrites* a session
(`db/workouts.py::_adaptation_tally` stops the walk at a `generate`), so a session the
model is told to keep stays flagged eased across every future regeneration, indefinitely.

`adapt` is unchanged here: it keeps its tag over its whole reach, because there the tag is
not about continuity but about not easing a session twice from an already-reduced
baseline.

**Both commands are shown what a session used to be, not just that it changed.** Today the
line reads `[ALREADY EASED 2x, most recently 3 days ago — do not compound]`
(`coach/formatting.py::_planned_summary`). The model is told *that* the numbers are
reduced and *ordered* not to reduce them again, but never shown from what — so it cannot
tell a 60-minute easy run that started as a 90-minute threshold session from one that
started as a 65-minute steady run, and the instruction has to be a flat rule. The earlier
form is already hydrated onto every session (`original_duration_minutes`, `original_tss`,
`original_rpe`, `original_date` — `db/workouts.py`), read off the lineage's first
revision at no extra cost. So the count and the recency stay, and "do not compound" is
replaced by the numbers:

```
- 2026-09-13 (RUN): Long run | Expected duration: 60m, RPE: 4, TSS: 45
  [planned as 90m, RPE 7, TSS 95 — eased 2x, most recently 3 days ago: short on time]
```

Now "keep it unless the moment the easing answered has passed" is answerable, and so is
adapt's "do not compound": both can see how far the session has already fallen.

**A constraint outranks a fresh easing.** Inside the window both rules are live, and they
can disagree: a "no running" correction over a run adapt eased two days ago. The easing
answers *how is the athlete today*; a constraint answers *what may this athlete do at
all*. The second wins, or the calendar keeps a run under a no-running instruction.

**Each session is shown** with its date, sport, title, duration, target, earlier form and
— for committed sessions — its description, so a revision can be minimal rather than
re-invented. The block replaces the current one, whose closing line — "every other day in
this window is yours to write from scratch, and a date this list does not name is not
spoken for" — now means the opposite of what it says and is rewritten with it.

**The model must be told what changed.** "Avoid visible changes if possible" is not
usable on its own. The model needs to know what would make it *not* possible. Consider
"describe her sessions with more detail on form cues" against "she's had knee pain — no
running for now". The first holds every shape; the second cannot. If the prompt supplies
only the new profile and the standing sessions, the model sees the same thing in both
cases. So the diff §6.1 derives is rendered as a "WHAT CHANGED SINCE THESE SESSIONS WERE
WRITTEN" section whenever the committed set is non-empty, and the instruction becomes
decidable:

> Does this session's change follow from what changed? If it does, make it and write the
> athlete one sentence saying so. If it does not, keep the session.

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

**The label comes off the change kind, for a surviving session too.** `sync_workout`
decides today with one line: `is_modified = bool(mod_reason)`, and a session with a reason
is titled `[Adapted]`. Generate writes no reason today, which is why regenerated sessions
render plainly — and §4.5 changes that. Left alone, every session a regeneration revises
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

**The window is stamped on the change, not re-read at sync time.** Rev. 2 had
`window_end` reach `_plan()` as a settings read. That makes the answer depend on *when the
sync runs*: write with `--no-sync` (`calendar_reconcile.no_calendar_sync`), push a
fortnight later, and a void that was outside the window when it was written is inside it
by the time it is reconciled, because the window has moved forward. So each
`workout_change` records the `window_end` in force when it ran, and the rule reads it
back. The decision is a property of the removal, not of the clock. No lower bound is
needed: no batch writes into the past, so a void's date is never before the day its change
ran.

**A mark with no event gets one.** `_plan` re-pushes a trace-keeping void only when an
event already exists (`if event_id and ...`). A session written and dropped between two
syncs never had one, and would leave no trace at all. When a void leaves a trace and has
no event, the push creates it.

### 5.3 The manual session needs a void to keep

The `source == "manual"` clause has nothing to fire on today: when generate writes over a
manual session, `_lineage_for` starts a new lineage and no void is written for the old one
(§1.2). So `workout_generate_apply` voids a manual session it does not keep *before*
appending over its slot, the same order it already uses for every other displaced session.
The new session then starts its own lineage by the append-over-void rule, the manual
lineage's head is a void with `source == "manual"`, and the marker appears.
DESIGN_workout_revisions.md §12's "Replaced the session you added" notice stays.

### 5.4 A rest day is cancelled like any other session

Friday's "Rest Day" filled by a fourth session gets "[Cancelled] Rest Day" beside the new
session, with the reason. It is a session the athlete was told about, and the fact that
its sport is `rest` does not change what a marker is for.

### 5.5 The day a session leaves does not lie

§1.2's complaint is that a dropped ride leaves Thursday reading "Rest Day — no session
planned for this day", "which is not true: a session was planned, and a decision removed
it." Rev. 2 left the filler untouched, so Thursday would have shown the `[Cancelled]`
marker *beside* a rest day still saying nothing was planned.

The filler is `_rest_workout(date, "no session planned for this day", title='Rest Day')`,
placed by the coverage backstop (`_fill_coverage_gaps`). When the date was emptied by a
`drop` or a `replaces` in this same run, the backstop writes the change's own reason
instead: "Long ride cancelled — never two hard days in a row." That is what adapt's
vacate rule already makes its model do (`coach/engine/workouts.py::_vacate_task`), and it
is the same sentence §4.5 already required, so nothing new is asked of the model.

**Removals no deterministic pass explains get a real reason too.** Several paths remove a
committed session without passing through the model's answers, and apply's void loop
stamps all of them "Not in the regenerated plan" — the string §5.1 exists to replace, now
promoted from a database column to the athlete's phone:

| pass | reason it writes |
|---|---|
| `_enforce_rest_windows_generate` | the constraint's own title, which it already builds and discards |
| `_drop_benchmark_collisions` | "makes room for the {sport} test on this day" |
| a `replaces` displacing an unaccounted occupant | the replacing session's own `change_reason` |

The fallback for anything else becomes "your coach replaced this day", which at least
names who did it.

### 5.6 Markers stay

The first draft swept markers dated before today. That is dropped: a marker is the
athlete's record that a decision was made on that day, and a past `[Deleted]` already
stays forever. `workout prune-calendar` continues to keep every event a row still claims.

Repeated regenerations on the same day do not stack markers: a removal re-decided in a
later run is the same lineage, so it is the same event, retitled again.

## 6. The change says what it is

### 6.1 What the coach reads: the snapshot the sessions were written against

The model can only write "why is Thursday different" if it is told what changed. That
input is a diff, and today it goes missing on the ordinary route.

The plan carries a snapshot of the profile it was generated from. "Stale" means today's
profile no longer matches it, and the diff is the comparison
(`coach/service/prompt.py::profile_diff`). The problem is that several commands
**overwrite that snapshot with today's profile** — which is exactly what "stop flagging
this" means. `plan keep` does it (`cli/plans.py`), and so does proceeding past
`workout generate`'s warning (`cli/workouts/generate.py::_confirm_out_of_date_plans`).
Once overwritten, both sides of the comparison are identical and there is nothing to show.

Wednesday, in full: the operator edits `preferences`; `plan show` reports it; the coach
says keep; `plan keep` stamps; `workout generate -m` finds nothing stale, computes an
empty diff, and rewrites Thursday, Friday and Saturday blind. That is §1.2 again, and
rev. 2's *pending change* — the diff copied onto the macrocycle at verdict time, carried
forward by `plan_apply`, consumed by the next regeneration — was an attempt to smuggle the
diff past the stamp. It needed four columns on `macrocycles`, a carry-forward, a rollback
rule, and it still failed, because it depended on the re-shaping verdict having run and
`plan keep` never asks it (DESIGN_plan_staleness.md §10, "Where it is not asked").

**Derive it instead.** Those sessions were written by a regeneration, and that
regeneration read some profile. So each `workout_change` of kind `generate` records the
inputs it was built from, and the next regeneration diffs today's against them:

```
what changed since these sessions were written
  = today's inputs  vs  the inputs the newest `generate` change recorded
```

That is literally the question the prompt section asks. It is correct on every route —
keep, force, network failure, a second goal, a rolled-back plan — because it never passes
through the staleness machinery at all. It is also *more* truthful than the pending
change, which compares against the profile when the **plan** was built: regenerate twice
since then and that diff includes edits already baked into the sessions the athlete is
looking at.

Nothing is carried, so `plan_apply` needs no carry-forward, `plan_rollback` needs no rule,
and one profile edit across two goals produces one diff for one run rather than one per
plan.

### 6.2 The diff has to be complete

Two snapshots, not one. Plan-shaping profile fields live in `profile_snapshot`, threshold
anchors in `config_snapshot` (`coach/service/prompt.py`), and a regeneration records both
— otherwise an FTP retest between two runs is invisible to the next one.

**A profile edit currently swallows a concurrent threshold move.** `config_changed` checks
the profile hash first and **returns on the first thing it finds**; thresholds are only
examined when the profile is unchanged. So: the athlete retests, FTP 250 → 265, the plan
flags it correctly. Before anyone looks, the operator also corrects `preferences`. Now
`plan show` says "athlete profile changed: preferences" and shows that diff alone — the
FTP line is gone from the reason and from the diff, the coach's verdict never learns the
threshold moved 6%, and `plan keep` stamps **both** snapshots, dismissing it for good. The
fix is control flow: collect every reason instead of returning on the first, and report
"athlete profile changed: preferences; ftp changed 250 → 265 (+6.0%)". Threshold drift
still shows no unified diff — its reason carries the numbers, which
DESIGN_plan_staleness.md §10 settled and which is right.

**Goals and constraints are in neither.** `goals_hash` and `constraints_hash` are read in
exactly one place — `coach/service/planning.py`, deciding whether `plan generate` may
reuse an existing strategy. They are not part of `config_changed`, so a moved race date
does not flag the plan stale, shows no diff, does not reach the verdict and does not reach
this section. A moved goal is the largest reshaper there is. The snapshots to diff against
(`goals_snapshot`, `constraints_snapshot`, `all_constraints_snapshot`) are already written
on every plan save and read by nothing here. Both join the staleness reason and the diff.

**Equipment joins this diff and not the staleness one.** The two diffs answer different
questions — *should I rebuild the periodization* and *should these sessions change* — and
`equipment` is where they part. The codebase already knows it: the comment on
`SCHEDULE_NON_PLAN_KEYS` says these keys "shape individual sessions but not the block
structure — swapping a day's kit changes what that day is, not the periodization"
(`config.py`). Right for staleness; wrong here. If the turbo trainer dies, no block should
move and Tuesday must. So the renderer takes a field list, and this section's list adds
`equipment` and each day's `equipment`. `name` stays out of both, and so do the
prompt-context knobs (`coach.metrics_lookback_days` and friends), which change what the
coach sees rather than what the plan should be.

### 6.3 What the coach writes: one sentence per session, one for the week

Rev. 2 asked the re-shaping verdict for one sentence covering the whole edit. That was
wrong twice over. The verdict is handed the reason, the diff, the strategy prose and the
block list and nothing else (`coach/engine/planning.py::_plan_reshape_verdict`) — it has
never seen the calendar, so "starting this Friday" was a guess about a day it does not
know exists. And the sentence was written before anything was decided, then stamped onto
every session the run wrote, so it could contradict the outcome: it promises a fourth
session while the easing rule quietly keeps the week at three.

The call that decides is the call that writes. Both sentences come out of the generation
response, grounded in what it just did:

- **Per session** — `change_reason` on every committed session it revises, moves or drops
  (§4.5). One sentence, for the athlete, about that day.
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
revision's own `reason` as `Reason:` and the change's `summary` as `Change:`. For an
adaptation the summary is the batch's overall rationale — "HRV suppressed three mornings
running" — which is a second *why*, not a *what*, so the label misleads
(DESIGN_calendar_lineage.md §3's example shows exactly this). Rev. 2 proposed printing it
as a second `Reason:` line, which leaves two identically-labelled lines in one block with
nothing to say which is the session's and which is the batch's — and for a regeneration
the summary is the coach's four-sentence microcycle reasoning, which would then appear in
the History of every regenerated session.

So: **one label, one meaning.** `_entry` prints `Reason:` from the revision's own reason,
and falls back to the change summary under that same label only when the revision has
none. `Change:` disappears from the lineage renderer. The full batch text stays where it
belongs, in `workout batches`. DESIGN_calendar_lineage.md §3's example is corrected with
this change.

The current revision is rendered by `sync_workout`, which today prints `Reason:` for
removed and adapted sessions only, so the newest change is never described on the event
the athlete opens. It prints the reason for a generate revision too — under the label rule
in §5.1, so the session is not also re-titled `[Adapted]`.

**Into the morning push.** DESIGN_plan_staleness.md §9 keeps the companion silent about
the staleness *flag*, and that stays. A finished, applied change is different: it has
already happened to the athlete's week. The bot has no path for an unprompted message
(it replies, or it runs `bot morning`), and none is added. Instead `bot morning` opens with
the week line of the newest `generate` change it has not yet delivered:

> *"Your coach changed your week: four sessions a week now, never two hard days in a
> row."*

A `rollback` of such a change instead prints *"Yesterday's change to your week was
undone."*, and a rollback before the line was ever sent prints nothing: the athlete never
heard of the change.

**The push tracks the change it delivered, not the day.** `MORNING_MARKER` holds a date
(`cli/bot.py`), which is enough for per-day idempotency and not enough here: a forced
re-run would repeat the line, and the push's several silent early-returns stamp the marker
anyway, so a line could be consumed by a push that printed nothing. A second marker holds
the id of the last change whose line was delivered, and is written only when something was
actually sent.

## 7. Implementation notes

**One block, one resolver.** `_resolve_kept` becomes `_resolve_standing`: it maps
`keep`, `revise`, `replaces` and `drop` entries onto the committed list under §4.5's
conflict rules, and every pass after it sees a uniform list of full sessions plus explicit
drops.

**The window is a shared revision layer, not a second `adapt`.** Every continuity step
hands `generate` something `adapt` already had: a view of the standing sessions, `keep`,
lineage carried across a sport change, a required `change_reason`, and a "what changed"
section doing the job adapt's attribution block does. That is the same machinery and it
should be built once: the standing block, `_resolve_standing`, the §4.5 report, and the
lineage-carrying apply. What stays separate is the policy each command runs it under:
**adapt answers "how is the athlete", generate answers "what does the plan say".**
Adapt's cause is the athlete's state, it may not reshape the block
(DESIGN_block_boundary.md §2), and it holds its easing tag over its whole reach.
Generate's cause is the plan diff, it may reshape freely outside the window, and it reads
the easing tag only inside it (§4.6). `replaces` across dates is new to both; adapt may
adopt it later so that a move keeps its history there too.

**`generate` still resets the easing tally**, but only when it revises. A `keep` appends
nothing, which is why §4.6 bounds the rule by the window rather than letting a kept
session stay flagged forever.

**The staleness chain.** Three changes, all in service of §6.2 and all useful on their
own: `config_changed` collects every reason rather than returning on the first; goals and
constraints join it from the snapshots already stored; `cli/staleness.py::guidance` stops
saying a wording change is picked up by "your next `workout generate`" — a bare generate
does not reach the days already scheduled (§1.1) — and becomes *"…keep the plan —
`workout generate -d today..` applies it to the days already scheduled, and a bare
`workout generate` to the days after them."*

**`plan show` asks the verdict, and it is cached.** DESIGN_plan_staleness.md §10 has
`plan show` print the diff and ask nothing, "a read-only command makes no network call".
It is also the command whose whole job is to help the operator decide, which is what the
verdict is for, so it asks. But §10 declined to cache the verdict for a reason that stops
holding the moment it does — "both questions stamp on 'keep', so the same change is asked
about once" — and `plan show` stamps nothing. So the verdict is cached on the macrocycle
against the snapshot it was asked about, and §10's "Where it is not asked" and
"Deliberately not done" paragraphs are amended.

**Migration.** Four nullable columns on `workout_changes`: `note` (the week line),
`profile_snapshot` and `threshold_snapshot` (§6.1), `commitment_end` (§5.2); plus the
cached verdict on `macrocycles`. `schema_version` bumped. Nothing is added to
`macrocycles` for the pending change, which no longer exists.

**ARCHITECTURE.md** needs `leaves_trace`, the settings row, the recorded snapshots and the
`[Cancelled]` word.

## 8. Tests

`tests/test_prompt_gates.py` — the "SESSIONS ALREADY STANDING" block, the `replaces` and
`drop` schema members, the per-session `change_reason` and the "WHAT CHANGED" section are
one gated region: present together when the committed set is non-empty, absent together
when it is not.

`tests/test_workout_generate_window.py` (new) —
- `keep`, `revise`, `replaces` and `drop` each produce the expected rows: same lineage for
  keep/revise/replaces, a void plus a new-lineage rest row for drop.
- An unaccounted committed session is dropped with the fallback reason and flagged in the
  preview.
- `change_reason` reaches the row's `reason` column.
- Window counting: `N=0` protects nothing, `N=1` today only, `N=7` today through day six;
  `N` greater than the generation span is clamped with a notice.
- The committed set is the intersection: a forward-selected span leaves it empty even
  when the window holds sessions; a completed session today is excluded and preserved.
- A wording-only revision (same load, new prose) is reported as `kept` and appends nothing
  (DESIGN_workout_revisions.md §9) — the preview reads the written rows, not the response.
- The `replaces` conflict rules: duplicate targets, a destination outside the span, a
  source outside the committed set.
- The easing rule applies inside the window and not outside it; a session kept twice does
  not stay flagged eased for ever.
- A constraint-shaped change outranks a two-day-old easing.
- A day emptied by a drop carries the drop's reason, not "no session planned for this day";
  a rest constraint and a benchmark collision each write their own reason.

`tests/test_calendar.py` — `leaves_trace` for each clause: athlete kind outside the
window, manual outside the window, coach kind inside, coach kind outside (torn down),
`N=0`. The window is read from the change, so a void reconciled a fortnight later gets the
answer it had when written. A manual session generate writes over now has a void and keeps
its event. A moved or sport-changed session updates its event in place. A trace-keeping
void with no event gets one. `[Deleted]` for `rm`/`stand-down`, `[Cancelled]` for
`generate`/`adapt`; a generate revision with a reason is **not** titled `[Adapted]`, an
adapt revision is.

`tests/test_calendar_lineage.py` — a History entry prints `Reason:` from the revision, the
batch summary under the same label only when the revision has none, and `Change:` never.

`tests/test_cli_plan_staleness.py` — `config_changed` reports a profile edit and a
concurrent threshold move together; goals and constraints flag the plan and render a diff;
the verdict is cached against the snapshot, so `plan show` asks once for an unchanged edit.

`tests/test_workout_revisions.py` — a `generate` change records both snapshots and the
window end; the next regeneration's diff is computed against them and is unaffected by
`plan keep`, by `--force` and by a plan rollback in between.

`tests/test_cli_bot.py` — the push opens with the week line once, the day after a
regeneration that carried one; a forced second push the same day does not repeat it; a
push that returns silently does not consume it; it says "undone" after a rollback; it says
nothing when the rollback preceded the first push.

## 9. Worked example

`workout_commitment_days` is 7. It is Wednesday 10 September. The calendar holds a
90-minute ride on Thursday, a rest day on Friday and a 90-minute long run on Saturday. The
operator corrects `preferences` from "three sessions a week" to "four sessions a week,
never two hard days in a row".

**Today.** `plan show` shows the diff and the coach says "keep". The operator runs
`plan keep`, then `workout generate -m`. Because `plan keep` stamped, the regeneration
computes no diff at all. Thursday's ride event is deleted and a "Rest Day" appears in its
place with "no session planned for this day"; Saturday becomes something the athlete has
never seen; the whole block is re-rolled including the parts the correction never touched.
The athlete finds out by opening their phone.

**With this design.** `plan keep` stamps as before — it does not matter. The regeneration
reads the snapshots recorded by the run that wrote these sessions on 1 September, and the
diff is "three sessions a week" → "four sessions a week, never two hard days in a row". It
sees Thursday, Friday and Saturday standing in the committed set. It keeps Thursday. It
fills Friday with a tempo run. It revises Saturday's long run down to easy, because Friday
is now hard. It writes a sentence for each, and one for the week.

```
Your coach's note to the athlete: Four sessions a week now, never two hard days in a row.

Sessions you were already told about (next 7 days):
  Thu 11 Sep  Long ride   90m   kept
  Fri 12 Sep  Rest Day          → Tempo run 45m   a fourth session, on the day that was rest
  Sat 13 Sep  Long run    90m   → 90m easy        your long run goes easy — Friday is hard now
```

On the calendar, Thursday is untouched. Friday shows "[Cancelled] Rest Day" with its
reason, beside "Tempo run" — no `[Adapted]` prefix, since a regeneration wrote it — whose
body carries `Reason: a fourth session, on the day that was rest`. Saturday's event updates
in place, its History showing the threshold version above the easy one, each with its own
`Reason:` and no second one from the batch. On Thursday morning the push opens with the
week line, once.

Had the correction instead been "no running", the preview would have said
`Thu 11 Sep  Run 60m → Ride 60m   no running while the knee settles`, and Thursday's event
would have been retitled in place with the run in its History — the same thing adapt does
today. Had a rest constraint covered Friday, the tempo run would have been replaced
deterministically and the marker would read the constraint's title, not "not in the
regenerated plan".

## 10. Deliberately not done

**`preferences` is not split.** The cheapest way to reduce this whole cascade is to stop
firing it so often: split the field into a plan-shaping half and a session-flavour half.
DESIGN_plan_staleness.md §4 rejected having a model judge whether a prose diff is
plan-relevant, but a *structural* split is deterministic and cheap. It is a separate
change and should be done on its own merits.

**The whole horizon is not carried forward.** Showing the model every standing session
across the full span would partly reverse DESIGN_workout_revisions.md §7.1's choice —
"every other day stays the model's to write, which is what keeps `generate` a
regeneration rather than a second `adapt`". Bounding the carry-forward to the window
keeps that choice intact everywhere except a narrow span where there is a stated reason
to suspend it. Requirement 2 of §2 is therefore met inside the window only, and that is
the intended trade.

**No override flag.** See §4.4.

**`commitment-days` is not routable.** See §4.1.

**No unprompted bot message.** See §6.4: the morning push is the channel.

**`plan keep` is unchanged.** It stamps and asks nothing, which is right: by the time it
runs the operator has decided, and `plan show` is where the coach speaks. Nothing the
athlete sees depends on it any more (§6.1).

**`plan diff` still does not render profile changes.** DESIGN_plan_staleness.md §8 left
this as a follow-up and it stays one.
