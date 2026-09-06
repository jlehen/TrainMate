# Design: Continuity when a plan change reaches the sessions

**Status:** Draft · **Date:** 2026-09-06 (rev. 2) · **Branch:** worktree-plan-change-continuity

Revision 2 follows a review of the first draft against the code. The review found that
several of the first draft's premises were wrong — a dropped day is not blank, adapt does
not tear events down, the "what changed" section would have been empty in the design's own
example — and the decisions taken on it are folded in here. The first draft is in the
branch history.

## 1. The problem

An athlete profile is not written once and left alone. It gets corrected as whoever set
the instance up learns what the athlete actually wants, and as the athlete says it better
the second time. "Three sessions a week" becomes "four, but never two hard days in a row".

A correction to `preferences` is plan-shaping (DESIGN_plan_staleness.md §4), so it starts
a chain. The chain is right. What comes out the other end of it is not.

### 1.1 The chain

1. The macrocycle flags stale. `plan show` names `preferences` as the field that moved and
   shows the edit as a diff (DESIGN_plan_staleness.md §10).
2. The coach is asked whether the edit re-shapes the plan (§10). On "re-shaping" the
   operator runs `plan generate`. On "keep" they do not. **Either way the athlete's
   sessions are untouched at this point.**
3. To make the change reach the sessions that already exist, the operator passes a
   selector — `workout generate -d today..`, or `-m 1`. A bare `workout generate` opens
   the day after the schedule's coverage ends (`cli/workouts/generate.py::_resolve_span`),
   so without a selector the change does not reach the athlete for up to
   `workout_generation_span_days`.
4. That selected run rewrites the span wholesale.
   `coach/service/workouts.py::workout_generate_apply` voids every live session in the
   span the new proposal does not re-propose, then writes the new ones.

Step 4 is where the surprise is made.

### 1.2 What the athlete actually sees

Take a concrete week. It is Wednesday. The calendar holds a 90-minute ride on Thursday, a
rest day on Friday and a long run on Saturday. The operator corrects `preferences` from
three sessions a week to four, and runs `workout generate -m 1`.

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
replacement for the §12 notice — but no void row is written for the manual lineage. It
simply has no live revision any more, `_plan` sees `head is None`, and the event is
deleted.

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
design changes adapt's behaviour. The design is about making `generate` reach the same
standard.

**A regeneration does not volunteer to rewrite.** `_resolve_span` opening after the
covered days means the destructive form has to be asked for. That is already a continuity
property; this design keeps it.

**The staleness check already has the diff.** DESIGN_plan_staleness.md §10 renders the
edit as a diff and asks the coach the re-shaping question over it. That is the one place
in the chain that reliably sees old against new, whichever way the question is answered.
§6 builds on it.

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
rebuild (§9). §5 makes every removal inside the window visible, and §6 answers the
third.

## 3. Decision

**Four parts.**

- **§4 — A commitment window.** A new setting, `workout_commitment_days`. The sessions
  standing inside it reach the generation prompt, and the model has to account for each
  one: keep it, revise its load, move it, change its sport, or drop it — each with a
  reason. A move or a sport change carries the session's lineage, exactly as adapt does.
  The preview lists what the model said it did to each of them, before the operator
  accepts.
- **§5 — A removal leaves a trace.** A session the coach removes inside the window keeps
  its Calendar event, retitled `[Cancelled]` with the reason. A session the athlete added
  by hand keeps its event whenever the coach removes it, inside the window or not.
  `[Deleted]` stays the word for a removal the athlete asked for.
- **§6 — The change says what it is, in one sentence.** The re-shaping verdict call
  (DESIGN_plan_staleness.md §10) also returns one sentence for the athlete. It is stored
  with the diff as a *pending change*, and the first regeneration that rewrites standing
  sessions shows the coach the diff, attaches the sentence to what it writes, and uses the
  pending change up. The morning push carries the sentence to the companion athlete.
- **§7 — Tests.** Named up front, since the first draft had none.

## 4. The commitment window

### 4.1 The setting

`workout_commitment_days`, default 7. `coach.workout_commitment_days` in `config.yaml`,
beside `workout_generation_span_days`, and a `Setting(...)` row in
`trainmate/settings.py` under a new `Schedule` group, so the athlete-facing name is
`commitment-days`. It earns a registry row on DESIGN_settings.md §1's test — "a knob the
athlete might reasonably change from a phone" — and the companion's `change_setting`
intent already reaches the registry.

**Counting.** The window is the `N` days starting today. `7` covers today through the
sixth day after it. `1` covers today only. `0` covers nothing: today's session may change
too. `window_end = today + (N - 1)`; with `N = 0` the window is empty. One thing never
changes whatever `N` is: a session already completed today is preserved by the existing
rule in `workout_generate`.

`parse` is a non-negative integer. Nothing else in this design is conditional on the
value: an empty window simply contains no sessions.

### 4.2 Where it bites

**Only where the generated span overlaps sessions that already exist.** A bare
`workout generate` extends into empty days, so the window contains nothing and the prompt
is the one it always was. A selected run that reaches back over scheduled days is, by
construction, proposing a diff to something the athlete has already read — and that is
the case the window is for. The window is measured from today, not from the span start.

The companion's runway button (DESIGN_runway_nudge.md §6) is a bare generate, so it never
overlaps the window, and its no-preview flow is unaffected. Previewing from the phone is
not expected at this stage of the app's life.

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
but still prints §4.5's report, so a forced run says what it did.

### 4.5 The model accounts for every standing session

Today the model is shown the eased sessions and may answer `keep` for each
(DESIGN_workout_revisions.md §7.1). The window widens that to every standing session in
the window and adds three more answers. For each session listed in the prompt's
"SESSIONS ALREADY STANDING" block, the response holds exactly one of:

| answer | response entry | what apply does |
|---|---|---|
| **keep** | `{"date", "sport_type", "keep": true}` | nothing; the session and its event stand |
| **revise** | a full entry in the same `(date, sport)`, with `change_reason` | a revision on the same lineage; History shows the old load |
| **replace** | a full entry in another slot, with `"replaces": {"date", "sport_type"}` and `change_reason` | a void where it left and the new entry **on the same lineage** — a move or a sport change, exactly as adapt's new-sport rule and `workout swap` already do |
| **drop** | `{"date", "sport_type", "drop": true, "change_reason"}` | a void on the lineage, reason attached; the day gets its rest row as a new session |

A standing session the model does not account for is treated as dropped with the reason
"the coach gave no reason", and the preview says so in red. The model is never asked to
half-do anything: `keep` returns nothing to adjust, and a revision is written out in full.

`change_reason` is required on revise, replace and drop. Generate's apply passes it into
the row's `reason` column, which it does not do today; that is what puts it on the
Calendar `Reason:` line and on the `[Cancelled]` marker (§5).

**The lineage rules.** `_lineage_for` already answers explicit-lineage appends (swap,
adapt's sport change) and appends over a void (new session). `replaces` maps onto the
explicit case: apply voids the named slot first, then appends the new entry with
`lineage_id` of the session it replaces. The vacated date gets its rest row from the
coverage backstop as a new session, as it does today. Nothing new in `db/workouts.py`.

**The preview.** Because every answer is the model's own statement, the report is a
table of what it said, with no matching heuristics:

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

One block, "SESSIONS ALREADY STANDING", replaces the eased-sessions list. Each session
carries tags rather than sitting in a separate section, so the model sees it once:

- `[IN THE WINDOW]` — the §4.3 rule applies.
- `[EASED ×2, last 3 days ago: sleep debt]` — the existing §7.1 rule applies, and it
  wins: an eased session is kept unless the moment the easing answered has passed. A
  REPLACE of an eased session is a revision and needs a `change_reason` like any other.
- `[BENCHMARK: ftp_20min]` — a scheduled fitness test. The strongest commitment on the
  calendar, since the athlete arranges to be fresh for it. Moving or dropping one needs a
  reason that says why the test can wait.
- `[ADDED BY THE ATHLETE]` — a manual session (§5.2).
- `[REST DAY]` — a planned rest day, so the model knows it is committing to a shape
  change when it fills it.

Each session is shown with its date, sport, title, duration, target and — for window
sessions — its description, so a revision can be minimal rather than re-invented.

**The model must be told what changed.** "Avoid visible changes if possible" is not
usable on its own. The model needs to know what would make it *not* possible. Consider
"describe her sessions with more detail on form cues" against "she's had knee pain — no
running for now". The first holds every shape; the second cannot. If the prompt supplies
only the new profile and the standing sessions, the model sees the same thing in both
cases. So the pending change (§6.2) — the diff, the coach's own re-shaping verdict and
its sentence — is rendered as a "WHAT CHANGED SINCE THESE SESSIONS WERE WRITTEN" section
whenever the window is non-empty, and the instruction becomes decidable:

> Does this session's change follow from what changed? If it does, make it and give the
> reason. If it does not, keep the session.

## 5. A removal leaves a trace

### 5.1 Two words

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
The syncer picks the word off the hydrated row's `change_kind`. Both carry the void's
`reason` as a `Reason:` block, which §4.5 now fills with the model's own words instead of
"Not in the regenerated plan".

### 5.2 Which voids keep their event

`ATHLETE_VOID_KINDS = ("rm", "stand-down")` has two consumers asking different questions.
`coach/service/adaptation.py::workout_adapt` asks **who asked for this?** — so the coach
is not told the athlete cancelled a day the plan merely stopped scheduling. That is about
authorship, and the constant stays as it is for that job. `calendar_reconcile.py::_plan`
asks **should this day leave a trace?**, and borrowed the authorship set because the two
happened to coincide. They no longer do:

```python
def leaves_trace(void, window_end: str) -> bool:
    """Whether a void keeps its Calendar event, retitled, rather than the event being
    torn down (DESIGN_plan_change_continuity.md §5.2)."""
    return (
        void["change_kind"] in ATHLETE_VOID_KINDS   # the athlete's own: always
        or void["source"] == "manual"                # a session they added: always
        or void["date"] <= window_end                # inside the window: always
    )
```

In one sentence: **a day you were counting on does not disappear — it is marked, with a
reason.** Outside the window, a coach-removed session the athlete never looked at is torn
down as today, so a 28-day rewrite does not litter the calendar.

`window_end` reaches `_plan()` as a settings read, the same way the bot re-reads settings
on each tick (DESIGN_settings.md §5). With `commitment-days` at `0`, `window_end` is
yesterday and only the first two clauses apply.

### 5.3 The manual session needs a void to keep

The third clause has nothing to fire on today: when generate writes over a manual session,
`_lineage_for` starts a new lineage and no void is written for the old one (§1.2). So
`workout_generate_apply` voids a manual session it does not keep *before* appending over
its slot, the same order it already uses for every other displaced session. The new
session then starts its own lineage by the append-over-void rule, the manual lineage's
head is a void with `source == "manual"`, and the marker appears. The §12 "Replaced the
session you added" notice stays.

### 5.4 A rest day is cancelled like any other session

Friday's "Rest Day" filled by a fourth session gets "[Cancelled] Rest Day" beside the new
session, with the reason. It is a session the athlete was told about, and the fact that
its sport is `rest` does not change what a marker is for.

### 5.5 Markers stay

The first draft swept markers dated before today. That is dropped: a marker is the
athlete's record that a decision was made on that day, and a past `[Deleted]` already
stays forever. At most a window's worth of new markers can arrive per regeneration, and
`workout prune-calendar` continues to keep every event a row still claims.

## 6. The change says what it is, in one sentence

### 6.1 Where the sentence comes from

Three options were considered in the first draft: the operator types it, derive it from
the snapshot, or ask the strategy call. The third was chosen and was wrong, because the
strategy call is exactly the one that is *not* made when the coach says "keep" — and
"keep" is the common verdict, since `preferences` over-triggers by design
(DESIGN_plan_staleness.md §4).

The right place is the **re-shaping verdict** (DESIGN_plan_staleness.md §10). It is asked
before both questions, it already holds the diff and the plan, and it runs whichever way
the answer goes. It answers `{"reshaping", "why"}` today. It gains a third member:

```
"athlete_note": "One sentence to the athlete: what changes for them, and from when,
  in their words rather than the plan's. OMIT when nothing they would notice changes."
```

The model that just decided whether the change re-shapes the plan is the one best placed
to say what it means for the week. Not "your preferences were updated" but "four
sessions a week now instead of three, and never two hard days in a row".

### 6.2 The pending change

The verdict's inputs and output — the change reason (`staleness.reason`, which names
profile fields, goal edits or constraints alike), the diff text, the verdict's `why` and
its `athlete_note` — are stored as a **pending change** on the macrocycle the verdict was
asked about. When `plan generate` replaces that macrocycle, `plan_apply` carries the
pending change onto the new one; when `plan keep` stamps it, the pending change stays.
So whichever way the question went, the next regeneration finds it.

A regeneration whose window is non-empty renders the pending change as the "WHAT CHANGED"
section (§4.6), attaches `athlete_note` as the change's summary, and clears the pending
change. A bare generate leaves it alone: nothing the athlete was told about was rewritten,
so the sentence is not yet true. A newer verdict replaces an older pending change.

### 6.3 Where the sentence goes

**Into the preview.** The operator reads it above §4.5's table and can reject the
proposal. Nothing branches on it; it is prose from a model.

**Onto the Calendar.** The change summary is what `calendar_lineage.py::_entry` prints as
`Change:` for earlier revisions. The current revision is rendered by `sync_workout`,
which today prints `Reason:` for removed and adapted sessions only, so the summary of the
newest change is never on the event the athlete opens. `sync_workout` gains one line: a
current revision whose change carried an `athlete_note` prints `Change: {note}` under its
load line. `Reason:` stays what it is — why *this* session changed — and `Change:` is
what the whole change was about.

**Into the morning push.** DESIGN_plan_staleness.md §9 keeps the companion silent about
the staleness *flag*, and that stays. A finished, applied change is different: it has
already happened to the athlete's week. The bot has no path for an unprompted message
(it replies, or it runs `bot morning`), and none is added. Instead `bot morning` reads the
newest `generate` change since its last marker and, if that change carried an
`athlete_note`, opens with one line: *"Your coach changed your week: four sessions a week
now, starting Friday. Thursday's ride and Saturday's run stay as they are."* A `rollback`
of such a change since the last marker instead prints *"Yesterday's change to your week
was undone."* A rollback before the push was ever sent prints nothing: the athlete never
heard of the change.

## 7. Tests

`tests/test_prompt_gates.py` — the "SESSIONS ALREADY STANDING" block, the `replaces` and
`drop` schema members and the "WHAT CHANGED" section are one gated region: present
together when the window is non-empty, absent together when it is not.

`tests/test_workout_generate_window.py` (new) —
- `keep`, `revise`, `replaces` and `drop` each produce the expected rows: same lineage for
  keep/revise/replaces, a void plus a new-lineage rest row for drop.
- An unaccounted window session is dropped with the fallback reason and flagged in the
  preview.
- `change_reason` reaches the row's `reason` column.
- Window counting: `N=0` protects nothing, `N=1` today only, `N=7` today through day six.
- A completed session today is preserved whatever `N` is.
- A wording-only revision (same load, new prose) is reported as `kept` and appends nothing
  (§9 no-op rule).

`tests/test_calendar_reconcile.py` — `leaves_trace` for each clause: athlete kind outside
the window, manual outside the window, coach kind inside, coach kind outside (torn down),
`N=0`. A manual session generate writes over now has a void and keeps its event. A moved
or sport-changed session updates its event in place.

`tests/test_google_calendar.py` — `[Deleted]` for `rm`/`stand-down`, `[Cancelled]` for
`generate`/`adapt`, `Change:` printed on a current revision whose change carried a note,
and not otherwise.

`tests/test_cli_plan_staleness.py` — the verdict's `athlete_note` is stored as a pending
change on keep and on regenerate; `plan_apply` carries it to the new macrocycle; a bare
generate leaves it; a selected generate consumes it; a second verdict replaces it.

`tests/test_bot_morning.py` — the push opens with the note once, the day after a
regeneration that carried one; says "undone" after a rollback of it; says nothing when the
rollback preceded the first push.

## 8. Worked example

`workout_commitment_days` is 7. It is Wednesday 10 September. The calendar holds a
90-minute ride on Thursday, a rest day on Friday and a 90-minute long run on Saturday. The
operator corrects `preferences` from "three sessions a week" to "four sessions a week,
never two hard days in a row".

**Today.** `plan show` shows the diff and the coach says "keep". The operator runs
`workout generate -m 1`. Thursday's ride event is deleted and a "Rest Day" appears in its
place with "no session planned for this day"; Saturday becomes something the athlete has
never seen; the whole month is re-rolled including the parts the correction never touched.
The athlete finds out by opening their phone.

**With this design.** The verdict call also answers: *"Four sessions a week now, never two
hard days in a row — starting this Friday."* That is stored as the pending change. The
regeneration sees Thursday, Friday and Saturday standing in the window, and it sees the
diff and the sentence. It keeps Thursday. It fills Friday with a tempo run and says why.
It revises Saturday's long run down to easy, because Friday is now hard, and says why.

The preview says so before the operator accepts:

```
Your coach's note to the athlete: Four sessions a week now, never two hard days in a
row — starting this Friday.

Sessions you were already told about (next 7 days):
  Thu 11 Sep  Long ride   90m   kept
  Fri 12 Sep  Rest Day          → Tempo run 45m       fourth session of the week
  Sat 13 Sep  Long run    90m   → 90m easy            no hard day after Friday's tempo
```

On the calendar, Thursday is untouched. Friday shows "[Cancelled] Rest Day" with the
reason, beside "Tempo run" whose body carries `Change: Four sessions a week now…`.
Saturday's event updates in place, its History showing the threshold version above the
easy one. On Thursday morning the push opens with the note.

Had the correction instead been "no running", the preview would have said
`Thu 11 Sep  Run 60m → Ride 60m   no running while the knee settles`, and Thursday's event
would have been retitled in place with the run in its History — the same thing adapt does
today.

## 9. Implementation notes

**One block, one resolver.** `_resolve_kept` becomes `_resolve_standing`: it maps
`keep`, `revise`, `replaces` and `drop` entries onto the standing list, and every pass
after it sees a uniform list of full sessions plus explicit drops. The eased-session rules
(DESIGN_workout_revisions.md §7.1) are unchanged and take precedence for eased sessions.

**The window is a shared revision layer, not a second `adapt`.** Every continuity step
hands `generate` something `adapt` already had: a view of the standing sessions, `keep`,
lineage carried across a sport change, a required `change_reason`, and a "what changed"
section doing the job adapt's attribution block does. That is the same machinery and it
should be built once: the standing block, `_resolve_standing`, the §4.5 report, and the
lineage-carrying apply. What stays separate is the policy each command runs it under:
**adapt answers "how is the athlete", generate answers "what does the plan say".**
Adapt's cause is the athlete's state, it may not reshape the block
(DESIGN_block_boundary.md §2), and its revisions count toward the compounding guard.
Generate's cause is the plan diff, it may reshape freely outside the window, and its
revisions reset the easing tally. `replaces` across dates is new to both; adapt may adopt
it later so that a move keeps its history there too.

**`generate` still resets the easing tally**, but only when it revises. A `keep` appends
nothing, so an eased session the model keeps stays `[ALREADY EASED]`. That is why §4.6
makes `keep` win for eased sessions.

**The staleness guidance is corrected.** `cli/staleness.py::guidance` says a wording
change is picked up by "your next `workout generate`". A bare generate does not reach the
days already scheduled (§1.1). The line becomes: *"…keep the plan — `workout generate -d
today..` applies it to the days already scheduled, and a bare `workout generate` to the
days after them."* The rubric in `_plan_reshape_verdict` says "absorbed by the next
workout generation" and is correct as a statement about the model; it stays.

**Migration.** Four nullable columns on `macrocycles`: `pending_change_reason`,
`pending_change_diff`, `pending_change_why`, `pending_change_note`; `schema_version`
bumped. `workout_changes` needs nothing: the note travels as the change's `summary`.

**ARCHITECTURE.md** needs `leaves_trace`, the settings row, the pending change and the
`[Cancelled]` word.

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

**No unprompted bot message.** See §6.3: the morning push is the channel.

**`plan diff` still does not render profile changes.** DESIGN_plan_staleness.md §8 left
this as a follow-up and it stays one.
