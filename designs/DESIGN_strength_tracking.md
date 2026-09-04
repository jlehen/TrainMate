# Strength tracking: reading the sets, naming the blocks, prescribing in kilograms

## 1. Motivation

TrainMate plans strength sessions but has no idea what happens in them. A completed
strength activity is stored as duration, average heart rate and a session RPE — the same
shape as a ride, and about as informative as "went to the gym". The coach therefore
prescribes in relative terms, because relative terms are all it can defend:

> Gym, 65 min. Squat 4x4 and RDL 3x4 at 8RM load, 2–3 min rests, always 3+ reps in
> reserve. Press 3x5, row 3x5, core and mobility 10 min.

"At 8RM load" delegates the only number that matters to the athlete. Today the athlete
fills the gap by hand: a Google Doc of past sessions (exercise, reps, weight — nothing
else), pasted into a general-purpose LLM together with TrainMate's prescription, to get a
session with kilograms in it, which then gets tweaked anyway. That is the athlete doing
the coach's job because the coach cannot see the logbook.

The logbook already exists in a place TrainMate can reach. The Garmin watch records
strength sessions set by set, Garmin Connect exposes them through the same API TrainMate
already logs into, and the athlete already opens Garmin Connect after a session to fix
the numbers the watch got wrong. What is missing is the reading, a vocabulary to read it
into, and a coach that uses it.

## 2. Goals / Non-Goals

Goals:

- Store every recorded set (exercise, reps, load, duration, rest) for strength
  activities, from Garmin or from the athlete, so that `workout compare` can show what
  was lifted and the coach can see a lift's history.
- Resolve exercise names with **no guessing**: TrainMate records a name only if Garmin
  reported it or a human confirmed it.
- Derive, with no per-athlete configuration, which lifts are worth progressing and what
  the athlete's current strength on each of them is.
- Let the coach prescribe strength sessions in absolute loads, with a progression it can
  justify from the record, so the Google Doc and the outside LLM retire.
- Work for strength training in general — barbells, dumbbells, kettlebells, cables,
  machines, bodyweight — and for more than one athlete without branching on who they are.

Non-goals:

- Writing back to Garmin. Garmin Connect is where the athlete edits; TrainMate reads.
- Pushing planned sessions to the watch as guided workouts. Possible later (§13), not now.
- Per-set RPE, velocity, or notes. The athlete's own log never had them; Garmin's session
  RPE is enough for load.
- Periodizing accessory work. Curls and lateral raises count toward volume and fatigue and
  are otherwise left alone.

## 3. What Garmin actually gives

Verified against the athlete's account on 2026-09-04 with
`garminconnect.Garmin.get_activity_exercise_sets(activity_id)`. A strength activity
returns an ordered `exerciseSets` list alternating `ACTIVE` and `REST` entries. An active
set carries `exercises[0].category` and `.name` (Garmin's own vocabulary, e.g.
`BENCH_PRESS` / `DUMBBELL_FLYE`), `repetitionCount`, `weight` in **grams**, and
`duration` in seconds; a rest entry carries only its duration. Weight and reps are what
the athlete entered or corrected on the watch or in Connect, so they are as good as the
athlete's own log.

The names are not. Over the athlete's last 15 sessions (2026-06-24 → 2026-09-03,
~390 working sets), 47% of sets came back `UNKNOWN`, and some named ones were wrong
(`SIT_UP` at 100 kg). The pattern behind the noise is structural, not random: the watch
recognises exercises from wrist motion, so **free-weight and bodyweight movements are
usually named and cable or machine movements essentially never are**. A whole machine
session — belt squat, inclined leg press, cable row, chest press, shoulder press,
pulldown — comes back as a list of unnamed blocks with correct reps and loads.

Two consequences shape everything below. Naming is a permanent step in the workflow, not
a teething problem. And loads are not comparable across exercises: 110 kg on a pulldown
stack and 80 kg on a deadlift bar are both "kg", and the smaller number is the harder
lift. Nothing in this design ranks lifts by load.

The library also offers `set_activity_exercise_sets` (rewrite names on an activity),
`upload_workout` and `schedule_workout`. All three are deliberately unused (§2).

## 4. Vocabulary: movement patterns and equipment

A shipped, static table maps every exercise TrainMate knows about to one **movement
pattern** and one **equipment** class. It ships with the code, like the zone tables in
`garmin/load.py`; nobody configures it. Garmin's category/name pairs are aliases into it,
and so are the plain-English names the athlete or the coach use.

Patterns are physiology, not preference, and there are eight:

| Pattern | Examples |
|---|---|
| squat | back squat, belt squat, leg press, goblet squat |
| hinge | deadlift, Romanian deadlift, kettlebell swing, hip thrust |
| single_leg | lunge, split squat, step-up |
| push_horizontal | bench press, machine chest press, push-up |
| push_vertical | overhead press, shoulder press machine, dumbbell press |
| pull_horizontal | barbell row, cable row, one-arm row |
| pull_vertical | pull-up, lat pulldown |
| core_carry | plank, sit-up, farmer's carry |

Equipment is one of `barbell | dumbbell | kettlebell | cable | machine | bodyweight`, and
it earns its column for three reasons:

1. **Detectability.** Cable and machine exercises are expected to arrive unnamed (§3);
   TrainMate can treat an unnamed block on a machine day as normal rather than as an error.
2. **How progress is measured.** Loaded exercises progress in kilograms. Bodyweight
   exercises progress in reps, then in added load; an estimated 1RM makes no sense for a
   push-up, "max reps at bodyweight" does (§8).
3. **Substitution.** The coach prescribes by pattern and picks the exercise from what is
   available. A travel week whose constraint says "hotel gym, dumbbells only" gets goblet
   squats and dumbbell RDLs for the same squat and hinge slots, and adherence still says
   "done" (§10). This is what makes a plan survive a different gym.

The pattern answers *did you do it*; the exercise answers *how strong are you*. Those are
different questions at different granularities, and the design keeps them apart: a goblet
squat done instead of a back squat satisfies the squat pattern and must never feed the
back squat's numbers.

## 5. Data model

One new table:

```sql
CREATE TABLE exercise_sets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id   TEXT NOT NULL,        -- completed_activities.activity_id, or a synthetic
                                        -- id for manual/imported sessions
    seq           INTEGER NOT NULL,     -- position within the session, rest entries included
    set_type      TEXT NOT NULL,        -- active | rest
    exercise      TEXT,                 -- canonical name from the vocabulary; NULL = unnamed
    garmin_name   TEXT,                 -- what Garmin said, verbatim, for audit
    reps          INTEGER,
    load_kg       REAL,                 -- NULL for pure bodyweight; 0 is a real value
    duration_sec  REAL,
    source        TEXT NOT NULL,        -- garmin | manual | import
    named_by      TEXT,                 -- garmin | athlete | NULL while unnamed
    UNIQUE (activity_id, seq)
);
```

`source` is what makes the design serve more than one athlete: Garmin is the writer for
an athlete whose watch records sets, a Telegram or CLI quick-log (`strength log deadlift
4x5@100`) is the writer for one without, and the one-time Google Doc import (§12) is the
first `import` writer. The coach block (§9) reads the table and never asks where a row came
from.

`named_by` is the no-guessing rule made auditable: every named row can say whether the
name came from Garmin or from a person. Nothing else ever sets it.

Manual and imported sessions need a `completed_activities` row to hang off; they get one
with `activity_type = strength_training`, a synthetic `activity_id`, and whatever
duration/RPE the athlete gave. Nothing downstream distinguishes them from Garmin rows,
which is the point.

## 6. The pull path

`data pull` already iterates activities in the window. For each strength activity it
additionally fetches exercise sets and replaces that activity's rows wholesale (delete by
`activity_id`, insert the fresh list). Replace-not-merge because the athlete edits in
Connect and the corrected version must win.

That edit happens *after* the session, often after TrainMate's first pull of it. So on
every pull, sets for strength activities from the last **7 days** are re-fetched and
replaced, regardless of the window asked for. It is one API call per activity, two or
three per pull, and it removes any need to detect "was this activity edited". Older than
7 days is considered settled; `data pull --force` re-fetches the whole window.

Athlete-confirmed names (§7) live in the same rows, so a re-fetch must not erase them: the
replace step carries `exercise`/`named_by = athlete` forward by `seq` when the new fetch
still reports that set as unnamed. If the athlete named it in Connect since, Garmin's name
wins and `named_by` becomes `garmin`.

## 7. Naming

The rule, and the one sentence that explains the feature to an athlete:

> TrainMate reads your sets from Garmin. Anything it can't name, it asks you about.

Three sources of names, in order of preference, and nothing else:

1. **Garmin.** Whatever the watch detected or the athlete corrected in Connect. Since the
   athlete already opens Connect to fix reps and loads, fixing a name is the same gesture in
   the same screen; this is the primary path.
2. **The athlete, via Telegram or CLI.** After a pull leaves unnamed blocks, the companion
   asks one question per unnamed *block* (consecutive unnamed sets with the same reps and
   load are one block), with the athlete's recent exercises as buttons:

   > Sep 1 — 4×10 @ 100 kg, then 9×110. What was it?
   > [lat pulldown] [leg press] [cable row] [other…]

   The answer is written with `named_by = athlete`. Unanswered questions expire quietly;
   the sets stay unnamed and count as volume only.
3. **Nothing.** An unnamed block is a legitimate state. It contributes to session volume
   and fatigue (§9) and to nothing else.

What is explicitly *not* a source: inference. TrainMate does not conclude "the plan said
squat 4×4 and here are four sets of four, so this is squat", nor "most of this session
matched the machine template so the rest is cable". Garmin's record is too unreliable and
the athlete too free-form (extra exercises, reordered machines) for a partial match to be
evidence, and a wrong name silently corrupts a lift's history. Template proposals —
"this looks like your machine session, same order?" with a one-tap confirm — remain a
possible later convenience because they still end in a human decision; they are out of
the first version (§11).

## 8. Tracked lifts and the estimated 1RM

Nobody configures a list of lifts. The set of **tracked** exercises is derived from two
sources, unioned:

- **Prescribed:** any exercise the coach named in a planned strength session in the
  current macrocycle.
- **Habitual:** any exercise the athlete did in at least 3 of their last 8 strength
  sessions.

Frequency, never load (§3). For an athlete who follows the plan exactly, the tracked set
is the plan's lifts. For one who adds a push/pull block to every session, those lifts
join automatically and lateral raises never do. A lift that stops being done ages out.

For each tracked exercise TrainMate keeps a rolling **strength state**:

- the last three sessions' working sets (`sets × reps @ kg`, plus session RPE);
- an estimated one-rep max (e1RM) from the session's **top set** — the set with the
  highest `load × reps` product — using Epley (`load × (1 + reps/30)`), computed only
  when reps ≤ 12 because the formula drifts past that;
- for bodyweight exercises, the best set's reps at bodyweight (or reps at added load)
  instead of an e1RM.

Top set only, because a session's rows mix warm-ups, back-off sets and light variants: the
athlete's named deadlifts range from 12×16 kg to 4×80 kg in one fortnight, and averaging
that says nothing. The e1RM is stored per exercise, never per pattern (§4).

The existing `benchmark_results` table already has an `e1rm` anchor kind (kg) that was
never populated because the CLI had "no per-exercise field". It gains an `exercise`
column, and the strength state writes a `source = modeled` row per tracked exercise when
its e1RM changes by more than 2.5%. A formal 1RM test, if the athlete ever does one, is a
`source = test` row on the same exercise and takes precedence exactly as FTP tests do over
modeled FTP.

## 9. Coach consumption

The coach gets a compact **strength state** block, one line per tracked exercise, plus a
one-line summary of untracked volume:

```
STRENGTH STATE (tracked lifts; loads are per exercise and not comparable across them)
  deadlift (barbell, hinge)        e1RM 91 kg   last: 4x4@80 RPE6 | 5x5@55 | 3x10@60
  belt squat (machine, squat)      e1RM 137 kg  last: 4x10@105 | 4x10@100 | 4x10@100
  bench press (barbell, push_h)    e1RM 61 kg   last: 3x5@52 | 5x5@50 | 5x5@48
  lat pulldown (cable, pull_v)     e1RM 143 kg  last: 5x10@110 | 5x10@95
  pull-up (bodyweight, pull_v)     best 8 reps  last: 4x6 | 3x8 | 4x5
  untracked: ~12 sets/session of core, flyes, triceps; adds ~20 min
```

With that in front of it the coach writes "belt squat 4×10 @ 110 (+5 from last week,
keep 3 in reserve)" instead of "at 8RM load", and can defend the number.

**The prescription is a separate LLM call.** The main planner (`workout generate` /
`workout adapt`) decides that Tuesday is a 65-minute non-failure strength session, which
patterns it covers, and what fatigue it may cost the Thursday intervals — the slot and its
purpose. A second, small call receives only the strength science, the strength state,
the equipment constraints in force, and that slot's purpose, and returns the exercise
list with sets, reps and loads. The reasons for splitting here and only here:

- the strength call's inputs (set history, equipment) are of no use to the endurance
  decisions, and the strength science leaves the endurance prompt entirely;
- each prompt has one job, which is where accuracy comes from as prompts grow (§11);
- the endurance planner's own decisions are coupled — moving Thursday's intervals moves
  Saturday's long ride — and splitting *coupled* decisions across calls is where
  multi-call designs fail. It stays one call.

The contract between the two is the planned workout row: the planner writes the slot
(title, duration, patterns, purpose, RPE budget) and the strength call fills the
`description` with the prescription. If the strength call fails, the row keeps the
planner's pattern-level text, which is exactly today's output.

Untracked volume matters to the planner for one thing: fatigue. An athlete who habitually
adds 20 minutes of upper-body accessories to a leg day makes every strength session
heavier than planned; that is the shape of a coach learning ("adds ~20 min accessories
to strength sessions") and the existing learnings machinery is the right home for it —
the planner then budgets 85 minutes for a 65-minute prescription, or trims the
prescription, and stops being surprised.

## 10. Adherence

"Did you do it" for a strength session means: every prescribed *pattern* was covered by
some exercise in that pattern, at roughly the prescribed sets × reps, at a load within a
band of the prescribed one when the exercise is the prescribed one. Extras are neutral —
information for §9, never a deviation. Two things are deviations worth flagging:

- a prescribed pattern with no exercise in it (skipped the hinge);
- a tracked lift done at a very different rep scheme from the prescription (4×10 where
  4×4 was asked), because that is a different stimulus, not a substitution.

Substitution within a pattern (goblet squat for back squat, pulldown for pull-up) is
adherence, with the note that the numbers landed on a different exercise.

## 11. Phasing, and the science trim

**Phase 1 — data, no prompt changes.** The vocabulary table, `exercise_sets`, the pull
with 7-day re-fetch, the Telegram naming question, the CLI quick-log, the Doc import
(§12), and sets shown in `workout compare` / "Done lately". At the end of it TrainMate
knows what the athlete lifts and the coach does not use it yet. Deliberately boring, so it
can be checked against reality — do Connect edits come through, is 7 days enough — before
anything depends on it.

**Phase 2 — the coach.** Tracked-lift derivation, strength state, the `benchmark_results`
extension, the separate prescription call, strength adherence. Built as a second call from
the start: bolting a lift-history block onto the existing prompt and splitting later is a
detour through the direction the prompt is trying to leave.

**Between them, the science trim** (TODO §PROMPT). The adapt prompt is ~50k tokens; ~15k
is science, split evenly between shipped and athlete files and sent whole to every
command. Tagging files by command and sport and including only matches is independent of
strength and should land before phase 2, so that when the strength call appears the
endurance prompt has already shed the strength science and the two-call shape is visible
rather than buried.

## 12. The one-time import

The athlete's Google Doc has exercise, reps and weight per session and nothing else —
the same three fields as `exercise_sets`. A one-off script parses it into `import` rows
with synthetic activities, so phase 2 starts with a history rather than three sessions.
After that the Doc retires. This is a script, not a feature: no ongoing Docs access,
no parser to maintain.

## 13. Decisions and open questions

Decided:

- Read-only toward Garmin; Connect is the editing surface.
- No inferred names, ever. Templates, if they come, are proposals with a human tap.
- Tracked lifts by frequency and prescription, never by load, never by config.
- e1RM per exercise from the top set; patterns for adherence only.
- Strength prescription is its own LLM call; the endurance planner stays one call.

Open:

- The habitual threshold (3 of the last 8 sessions) is a first guess; revisit once the
  table has two months of rows.
- Whether the naming question should batch a whole session ("3 unnamed blocks on Sep 1")
  or send one message per block. Start with one message listing all blocks, one reply
  per block.
- Pushing the prescription to the watch (`upload_workout` + `schedule_workout`) would
  make the watch name the exercises itself, which removes §7 for an athlete who follows
  the plan exactly and adds friction for one who improvises. Garmin's strength-workout
  payload is the least documented corner of the API; verify the format before promising it.

## 14. Out of scope

Velocity-based training, per-set RPE, exercise technique notes, and any third-party
lifting app (Hevy, Strong) as a source. The `source` column leaves the door open for the
last one without designing it now.
