# Benchmark Workouts

**Status:** Phase 1 & Phase 2 **implemented**; Phase 3 unbuilt (§7).

> **Rev. 4 (2026-08-19) — the flag travels with the test, not with the date.** A
> model-comparison run found three models converting an FTP-test day into a social ride and
> emitting the replacement with `benchmark_type` still set — "Friends Group Ride
> [BENCHMARK]". Two independent causes, both fixed:
>
> - **The prompt described the flag as a property of the slot.** "Preserve VERBATIM when
>   the session is a benchmark", read against a date that holds a test, says preserve. §4.2
>   now states the rule in terms of the session — the flag travels with the test, and any
>   other session landing on the test's date carries `null` — and names the two costs
>   (a social ride read as an FTP result; the block believing it already tested).
> - **The flag could not be cleared even when the model got it right.** `save_workout`'s
>   UPDATE branch COALESCE-preserved `benchmark_type`, so a proposal emitting `null` was
>   silently overridden by the stored value — which made §4.2's POSTPONE fallback
>   ("replace it with an ordinary easy session (no benchmark_type)") unimplementable
>   through the adapt path. `save_workout` gains `clear_benchmark`, and adapt sets it when
>   a returned change lands on a benchmark row without re-emitting the flag (§3.1, §4.2).
>
> Left as-is deliberately: adapt may still legitimately drop a test the athlete cannot do.
> With the flag cleared, the block-progress section reports no test run and the next
> `workout generate` re-places it — the system self-heals rather than needing a guard.

> **Rev. 3 (2026-08-17) — due-ness moved to the science file.** Rev. 2's placement rule
> ("one test per mesocycle boundary", §4.1; resolved in §8 as the cadence knob) over-tested
> short blocks: an 11- and a 21-day block produced FTP tests 21 days apart, inside the
> noise floor `benchmarks.md` §1 already named — the TASK prompt was overriding the
> guideline it pointed at. `benchmarks.md` now carries the mechanics as FLOOR-marked
> rules (4-week same-anchor minimum, 8-12 week typical cadence, minimum meaningful
> change, protocol lock, deload-end-vs-taper split), and the code defers to it:
>
> - The generate prompt names the boundary week as the *slot* and leaves due-ness to the
>   guidelines, explicitly counting tests placed in the same span (the one fact the
>   science file cannot know). A new **ANCHORS ON RECORD** user-content section
>   (`_anchor_history_text()`, `coach/service/context.py`) supplies each anchor's last
>   value, provenance and measured date — the dates the interval floor is judged against.
> - `_warn_missing_boundary_benchmarks()` gains a fourth silence: any test — proposed in
>   the batch, live before the span, or a measured logbook row — within `MIN_RETEST_DAYS`
>   (`trainmate/benchmarks.py`) of the boundary. Any anchor silences, since the check
>   cannot know which anchor a missing test would have measured; a false silence costs
>   one un-nudged athlete where a false nag contradicts the generator.
> - Adapt's last-day fallback (§4.2) inverted: a benchmark that cannot be moved to a
>   fresh in-block day is POSTPONED (replaced with an easy session), not run compromised
>   — a skipped test costs a retest, a wrong anchor mis-scales a block.
> - `benchmark record --source` defaults to `manual`: a typed value is an assumption
>   unless declared a test, so §3.4's seeding commands record what they are and cannot
>   start the interval clock or satisfy the never-measured trigger.
>
> §4.1's prose below still describes the rev. 2 unconditional rule and §8's "cadence
> knob — resolved" bullet is superseded accordingly; both read through this note.

> **Rev. 2 (2026-08-04) — post-implementation reconciliation.** Rev. 1 was the
> pre-implementation spec, written in the imperative future ("Add…", "Introduce…") with
> "change this line" pointers. Phases 1 and 2 shipped and the architecture held: the
> logbook is the only home for trainable thresholds, `effective_thresholds()` is the one
> accessor both the prompt and the staleness check read through, no anchor kind is
> privileged in the vocabulary, and placement/protection are instructed rather than
> engineered. This revision rewrites the shipped parts in the present tense, refreshes the
> file pointers, and folds in five things the code does that rev. 1 did not say:
>
> 1. **`e1rm` is exempt from the drift check** (§3.3, §3.5) — it feeds the prompt but can
>    never trip a replan, because one e1RM value collides across lifts.
> 2. **`save_workout` keeps an app-side `COALESCE` guard** on `benchmark_type` (§3.1), so
>    a partial re-save preserves the flag rather than relying on the model alone.
> 3. **A threshold key left in `config.yaml` still reaches the prompt** — inert for the
>    logbook, not stripped (§3.4).
> 4. **§5.3's activity-disambiguation rule was never built**; it is Phase 3 work, and the
>    matcher that actually runs is described in its place.
> 5. **Two surfaces rev. 1 did not name ship today**: the read-only web `/api/benchmarks`
>    view and the anchor-trend lines in the intensity block report (§6). The progress
>    timeline plot rev. 1 listed under §6 is still Phase 3.

Make fitness tests (FTP, threshold pace, CSS, e1RM) a **first-class, planned**
part of TrainMate: the planner schedules them on fresh days, daily adaptation
protects them, and their results are recorded in a dated logbook that becomes the
source of truth for the athlete's thresholds — feeding the coaching prompt, the
replan trigger, and long-term progress tracking.

*Before this design*, a benchmark existed only as prose in
`trainmate/science/benchmarks.md`. The coach *could* schedule one as an ordinary
workout, but nothing placed them reliably, nothing stopped daily `adapt` from softening
one into meaninglessness, and there was nowhere to put the result. This design closes
those three gaps.

---

## 1. Why this is safe: TSS does not depend on the app's thresholds

The scary version of this feature is "a benchmark updates my FTP, which rewrites
all my historical TSS and corrupts the PMC." **That cannot happen here**, and it
is worth stating up front because it shapes everything below.

TrainMate computes TSS from Garmin's *time-in-zone seconds* (`garmin/load.py`),
where the zoning was already done inside the athlete's Garmin account. The
app-side `ftp`/`lthr` values (the benchmark logbook, §3.2) are never read by the
load model. They feed exactly two things:

- the **coaching prompt** the LLM reads (so it prescribes zones/targets), and
- the **plan-staleness check** (`config_changed()`, `coach/service/prompt.py:60`).

A third reader arrived later and is worth naming: the intensity **block report** renders
the anchor trend over the reported window (`intensity.py:710`, §6). It feeds the coaching
prompt too, so it does not widen the blast radius — but it is a consumer of the logbook.

So updating an app-side threshold is a *forward-looking prescription* change, not
a *retroactive accounting* change. There is no corruption pathway. (The real
lever on historical TSS accuracy is the FTP configured in the athlete's Garmin
account — which the app can remind the athlete to update but cannot set.)

---

## 2. The core model

A benchmark is a workout whose **purpose is measurement**, not stimulus. That one
difference gives it two behaviors no ordinary session has:

- **Before the test — it needs freshness.** A test on a fatigued day (negative
  TSB) reads low and then mis-scales every workout after it. So it wants a rested
  or opener day in front of it, and it must be *moved* rather than *eased* if the
  athlete is not fresh on the day.
- **After the test — it produces a result.** A threshold value (FTP watts,
  threshold pace, CSS, e1RM) that should be recorded with a date and fed forward.

Everything else about a benchmark is already an ordinary `Workout` on a date with
a sport. So we do **not** build a new "benchmark entity." We add a marker to the
workout, and a separate logbook for results.

---

## 3. Data model

### 3.1 `benchmark_type` on `Workout`

A nullable `benchmark_type` lives on the `Workout` TypedDict (`types.py:47`, the field at
`:66`) and the `workouts` table (`db/base.py:112`, migrated at `:286-296`). When set, the
session is a test:

    ftp_20min | ftp_ramp | run_threshold_30min | run_5k_tt |
    css_400_200 | e1rm | mas_cooper | ...

The column itself is cheap; carrying the value is not free. A workout is never
passed around as an opaque row — every hand-off rebuilds it from an explicit
field list, and a field missing from any list is silently dropped. The flag is
therefore threaded through each enumeration:

- the model's JSON output contract in **both** generate
  (`coach/engine/workouts.py:187-190`) and adapt (`:503-505`), with a prompt
  instruction to preserve the field when re-emitting a session. The model, not
  the app, owns the flag's survival across an adaptation — consistent with
  §4.2's no-guards stance;
- `save_workout` (signature + SQL, `db/workouts.py:21, 105, 137, 156`) on the
  generate save path (`coach/service/workouts.py:429`);
- the adapt rebuild dict (`coach/service/adaptation.py:251-276`) and
  `workout_adapt_apply` (`:384`) — the spot a first pass misses. Without it, the
  model's proposal to move a test is rebuilt without the flag and saved as an
  ordinary workout: the act of protecting the test is exactly what would strip
  its benchmark identity, silently.

**One app-side guard, deliberately.** "The model owns survival" is the rule for the
*proposal*; the SQL keeps a belt-and-braces default underneath it. The UPDATE branch of
`save_workout` writes `benchmark_type = COALESCE(?, benchmark_type)`
(`db/workouts.py:105`), exactly like `source`, `tss` and the other optional columns — so a
same-`(date, sport)` re-save that simply omits the field preserves the stored value
instead of nulling it. This is not the kind of guard §4.2 argues against: it reverses no
model intent and reads no proposal batch, it only stops an omission from being read as a
deletion.

Preserving an omission must not mean the flag is *unclearable*, though — §4.2's POSTPONE
fallback needs to strip it, and for one revision could not. `save_workout` therefore takes
a `clear_benchmark` flag (`db/workouts.py:21, 105`) that blanks the column in place; the
UPDATE reads `CASE WHEN ? THEN NULL ELSE COALESCE(?, benchmark_type) END`. It is off for
every caller but adapt (§4.2), so the default behaviour — omission preserves — is
unchanged, and a delete-then-insert (the manual-replace path,
`coach/service/editing.py:248, 257`) still clears the flag as it always did.

As a stored column it is creation-time intent, exactly like the existing
`source` column — *not* like the `[MANUAL]`/`[SWAPPED]` markers, which are
deliberately **derived** from `modification_reason` (`modification_state.py`
documents why derived kind-columns are preferred for mutable state). A
benchmark's identity is fixed when the session is created, so a column is the
right shape here.

### 3.2 `benchmark_results` — the logbook

This is the one genuinely new abstraction, and — with §3.4 — the **only** place
the athlete's thresholds live. It is **not** an abstract "anchor store" — it is
a dated logbook of test results. One row per measurement:

| column        | meaning                                              |
| ------------- | ---------------------------------------------------- |
| `id`          | pk                                                   |
| `date`        | when the test was performed                          |
| `sport_type`  | cycling, running, swimming, strength, …              |
| `anchor_kind` | `ftp` \| `lthr` \| `threshold_pace` \| `css` \| `e1rm` \| `mas` |
| `value`       | the number (e.g. 250)                                |
| `unit`        | `W` \| `bpm` \| `min/km` \| `sec/100m` \| `kg` \| `km/h` |
| `source`      | `test` \| `manual` \| `modeled`                      |
| `workout_id`  | nullable link to the planned benchmark it satisfied — a **lineage** id, so it names the session rather than one of its revisions (DESIGN_workout_revisions.md §15) |
| `note`        | free text (protocol, conditions)                     |

The `unit` strings in that table are the *literal stored values* — they come from the one
vocabulary table in `trainmate/benchmarks.py:35-45`, so a query or a fixture can be
written straight off it.

Each sport has its plausible anchor kind(s) — cycling→`ftp`, running→`threshold_pace` /
`mas`, swimming→`css`, strength→`e1rm`, with `lthr` valid on every endurance sport
(`benchmarks.py:55-61`). The map is a **sanity check, not a schema**: `benchmark record`
warns (`cli/benchmarks.py:96-104`) when the kind is not one the named sport is normally
tested on (a mistyped `record swimming --ftp 250`) and **refuses the record**, exit 1. The
map is deliberately generous, because cross-sport pairings are real — a cyclist's LTHR, a
rower's threshold pace — and a sport with no entry at all has no opinion and is accepted.
Within that, a mismatch is a typo rather than a legitimate entry, and refusing costs one
retyped command where accepting silently pollutes the logbook the prompt prescribes from.
`lthr` is a first-class kind — a run threshold test produces it, and it is one of the
values the prompt prescribes from, so the logbook must be able to supersede it (§3.4).

"Latest" is defined as **newest by `date`, `id` as tiebreak**, so backdated
entries behave. Two reads answer everything:

- **"What is my FTP right now?"** → the latest `cycling` / `ftp` row.
- **"Is my fitness rising?"** → read down the column: 235 → 242 → 250. That
  progression *is* the signal `benchmarks.md` cares about ("a rising anchor
  confirms progressive overload; a stalled or falling anchor signals plateau").

A single overwritten scalar cannot show that trend; a dated logbook can. One
display caveat: for pace kinds (`threshold_pace`, `css`) *lower is better*, so
trend/delta rendering carries a per-kind sign — a faster runner must not be
shown a negative-looking progression.

### 3.3 The effective-threshold accessor — the linchpin

**One** accessor, which both the prompt and the staleness check read through:

    effective_thresholds()  ->  {ftp, lthr, ...} from the latest logbook rows

It lives in the **service layer** (`coach/service/prompt.py:28-43`), the only layer with
both config and DB access — the engine is a pure prompt-builder over data handed to it and
imports no `db`, and that stayed true. The service passes `profile=` into every engine
call (`coach/service/prompt.py:258`, `coach/service/workouts.py:349`,
`coach/service/adaptation.py:159`, `coach/service/planning.py:301`,
`coach/service/analysis.py:691`); `_effective_profile()` (`coach/service/prompt.py:45-53`)
overlays the effective thresholds onto `config.user_profile` first and hands over the
merged result. Two engine-side changes completed the wiring (an earlier draft claimed "no
engine change" — wrong on inspection):

- **The prompt.** `_format_athlete_profile()` (`coach/engine/prompt.py:21-48`) used to
  have hand-named FTP/LTHR lines; it now renders *whatever* threshold kinds the
  profile dict carries, generically with the unit, iterating `ANCHOR_KINDS`
  (`:37-39`) — no kind is privileged (§3.5), so a first swim test shows up in the
  prompt with zero further code.
- **The staleness check.** `_get_config_thresholds()` used to read
  `config.user_profile` directly in the engine — once `ftp`/`lthr` left config
  (§3.4) it would have silently returned only `max_hr`. It is **gone**: the
  threshold read moved to the service, so `_get_config_snapshot()` and
  `config_changed()` (`coach/service/prompt.py:55-107`) judge drift over effective
  values, and `PROFILE_THRESHOLD_FIELDS` (`config.py:450`, read by `plan_profile()`)
  keeps the thresholds out of the config *hash*, so they are judged on the tolerance
  axis only.
  The plan **snapshots** whatever the effective value was at generation time
  (`coach/service/planning.py:359`); `config_changed()` compares effective-now
  against that snapshot. A benchmark result that moves the effective value **>5%**
  (the existing `coach.threshold_replan_pct`, `config.py:147-154`) flows through the
  *same* threshold-drift axis that already existed → the plan is flagged stale and a
  replan is suggested. Sub-5% retest corrections feed the next workout generation
  without invalidating the strategy — exactly the pre-existing behavior.

**Snapshot scope: uniform, no privileged kinds.** Every anchor kind on record
joins the snapshot — `ftp`/`lthr` are not special (§3.5). That needed one small
fix first: `config_changed()` used to treat a key it had never seen as instant drift
("`css` was added"), so a first-ever swim test would have flagged every pre-existing plan
stale, bypassing the 5% tolerance. It now **skips keys absent from the *old* snapshot**
(`coach/service/prompt.py:99-101`): a newly recorded kind starts feeding prompts
immediately and joins drift-checking from the next generated plan onward; only a >5%
*change* in a kind the plan was actually built with triggers a replan. A key that
*disappears* still reads as drift (`:102-103`) — a threshold the plan relied on going
missing is real.

**The one exception: `e1rm` never trips a replan** (`coach/service/prompt.py:97-98`). The
logbook has no per-exercise field, so a single `e1rm` value collides across lifts: a
deadlift PR logged the week after a squat PR reads as one anchor jumping ~70%, which under
the uniform rule would invalidate a whole periodization on a bookkeeping artefact
(DESIGN_intensity_distribution.md §10). `e1rm` is therefore excluded from the drift loop
only — it is recorded, snapshotted, rendered into the prompt, trended and displayed
exactly like every other kind. This is a *drift-axis* exception, not a privileged kind:
the fix that would retire it is a per-exercise dimension on the logbook, not special-case
code elsewhere. The same caveat is repeated where the athlete meets it — the
`benchmark record` help text (`cli/benchmarks.py:221-223`) tells them to track one lift.

One accessor, one relocated threshold read, one skip rule and one `e1rm` exclusion in
`config_changed()` — no other staleness logic changed.

### 3.4 `ftp`/`lthr` leave `config.yaml` entirely

The logbook is the *only* home for trainable thresholds. `user_profile.ftp` and
`user_profile.lthr` were **removed** from `config.yaml` and
`config_template.yaml` (replaced by a pointer comment, `config_template.yaml:156-160`).
`max_hr` stays in config: the split is principled — config keeps quasi-fixed physiology
and life logistics (age, availability, equipment), the logbook keeps *trainable,
measured* quantities. This deletes the "seed value that becomes inert" concept
outright: there is no fallback branch in the accessor, no precedence rule to
document, and no config field that looks editable but silently is not.

**Honest caveat about a re-added key.** Nothing *strips* the profile dict.
`_effective_profile()` is `{**config.user_profile, **effective_thresholds()}`
(`coach/service/prompt.py:53`), so an `ftp:` typed back into `config.yaml` does still
render in the coaching prompt — while being invisible to `effective_thresholds()`, the
drift snapshot, `status` and `benchmark list`, and while a logbook row of the same kind
silently overrides it. That asymmetry is the deliberate price of having no fallback
branch and no precedence rule in the accessor: the profile dict is pass-through data and
the code asserts nothing about which keys it holds (tested in
`tests/test_benchmarks.py:159-175` — "rides through, untouched"). The claim above is that
config has no *documented* threshold field, not that the loader refuses one.

**Seeding is a one-off, not machinery.** By the time seeding is possible,
`benchmark record` exists — and two invocations of it *are* the migration:

    tm benchmark record cycling --ftp 220 --note "seeded from config"
    tm benchmark record running --lthr 165 --note "seeded from config"

Two rules make the cutover seamless:

- **Sequence:** upgrade → seed → only then run any coach command. Once the code
  reads thresholds from the DB alone, a `generate`/`status` run before the seed
  rows exist finds no `ftp` key, compares against a macrocycle snapshot that has
  one, and reports a spurious "ftp was removed"
  (`coach/service/prompt.py:102-103`).
- **Values:** seed the *exact* numbers currently in config, so existing
  macrocycle snapshots still match and `config_changed()` stays quiet.

**Cold start: nudge, never refuse.** A fresh install has no thresholds — and
the codebase already degrades gracefully: the prompt formatter emits threshold
lines conditionally (`coach/engine/prompt.py:37-39`), and the snapshot/drift code skips
absent keys. A plan generated with no FTP on record prescribes by RPE and HR
feel, which is what a coach does with an untested athlete. So generation
proceeds, and a cold-start hint (`_maybe_nudge_no_threshold()`,
`coach/service/prompt.py:222-237`, fired from `coach/service/workouts.py:350`) mirrors
`_maybe_nudge_bootstrap()` (`:239`, fired from `coach/service/planning.py:330`):
*"No fitness thresholds on record — prescriptions will use RPE/HR feel until you record
one (`benchmark record …`) or complete the scheduled benchmark."* `max_hr` alone does not
silence it: config physiology is not a measured anchor. The very first generated plan
schedules a benchmark anyway (§4.1),
so the gap closes itself within the first block. Refusing to plan would create
a bootstrapping paradox — the planner is how a benchmark gets scheduled.

### 3.5 No privileged anchor kinds

`ftp`/`lthr` are not special — they are merely the first two rows the logbook
happens to hold. Every kind (`css`, `threshold_pace`, `e1rm`, `mas`, …) flows
identically: recorded via `benchmark record`, rendered into the prompt generically
(§3.3), drift-checked by the same skip-absent-keys snapshot rule (§3.3), trended by
`benchmark list`, the web logbook and the block report (§6). Adding a future kind is a
vocabulary addition — a new row in `ANCHOR_KINDS` carrying its unit and its
better-direction sign (§3.2) — not new machinery.

The single documented exception is `e1rm`'s exclusion from the *drift check* (§3.3), and
it is an exception to one loop rather than a privileged kind: `e1rm` is recorded,
snapshotted, prompted, trended and displayed like everything else.

---

## 4. Behavior

### 4.1 Placement — instruct, then verify

Benchmarks belong at block boundaries and on a ~4–6 week cadence
(`benchmarks.md` §1). The split of labor plays to each side's strength:

- **The LLM places.** The generation prompt (`coach/engine/workouts.py:155-167`)
  instructs the coach to schedule one benchmark of the appropriate kind in each
  mesocycle-boundary week the generated span covers — except the terminal block's —
  preceded by an opener/easy day so TSB is positive on test day, phrased
  venue-neutrally (§5.4), and never in a rest week. Day choice stays with the
  model — it already handles weekly availability, equipment, and rest days, and
  a deterministic pass re-implementing that logic is exactly the machinery we
  do not want.

**Why the boundary is the block's END.** The anchor exists to scale the *next*
block's targets, and `workout generate` writes a whole span in one shot — so
everything scheduled after a test in that span was authored before the result
existed. Testing at the end of block N leaves only the tail of a finishing block
authored blind, and the next generate run writes all of N+1 from the new anchor;
testing at the *start* of N+1 leaves N+1 itself — the block the test was meant to
calibrate — authored blind. The seam placement also lands a >`threshold_replan_pct`
move before `config_changed()` (§3.3) rewrites the upcoming block, rather than part-way
into days already begun.

**Why there is no pre-goal validation test.** An earlier revision asked for one in
the last week before the goal, on top of the boundary tests. It was dropped. A
maximal test is physiologically the same event as the goal effort, so in a taper it
spends the freshness it is measuring — and the taper deliberately lifts performance
above the tested value, making a mid-taper anchor stale in the optimistic direction
by race day. The final boundary test already sets a current anchor, and
`benchmarks.md` §1 puts re-tests inside ~3-4 weeks in the noise. The clause also
collided with the boundary rule whenever the taper was short enough to make "the
block's final week" and "the last week before the goal" adjacent.

Removing it is not sufficient on its own, because a goal-directed macrocycle's
**last block ends ON the goal date** — its boundary week *is* race week, so the
boundary rule alone would still ask for a test there. Hence the goal-week exemption,
applied on both sides: the prompt excludes such a week, and
`_warn_missing_boundary_benchmarks()` skips any boundary ending later than seven days
before the goal (`_goal_date_for_macrocycle()` resolves the date through the
macrocycle's objective; an unresolvable goal exempts nothing).

The exemption keys on the **goal date, not the block's ordinal position**: a macrocycle
whose final block ends months before its target date is an ordinary boundary and still
gets its test. Only the run-in to the event is protected.
- **A deterministic post-check verifies.** After generation, if a covered
  boundary week ended up with no `benchmark_type` workout,
  `_warn_missing_boundary_benchmarks()` (`coach/service/workouts.py:195-233`, called at
  `:426`) prints a warning — same spirit as the rest-window pass
  (`_enforce_rest_windows_generate`), but a warning rather than an insertion: a
  missing test surfaces for the athlete to regenerate, it is not silently
  auto-fixed. The check stays silent when the boundary week sits under a `rest`
  constraint — **rest wins**, and warning about it would be noise. The boundary
  "week" is the seven days ending on the mesocycle's `end_date`, and only boundaries
  whose end falls inside the generated span are checked. It is likewise silent when a
  test was **already run** earlier in the boundary week: regenerating mid-boundary-week
  would otherwise advise regenerating again to recover a benchmark the athlete has
  already done. That lookup is bounded below `gen_start`, because the displaced plan's
  future rows are still live when the check runs and must not answer for sessions this
  run just replaced (DESIGN_block_progress.md §4.1).

**Not placing a test the block already ran.** The placement instruction above is
unconditional on its own, so a regeneration inside the boundary week re-places a
completed test. It is bounded at the source rather than post-hoc: when the generate prompt
carries a block-progress section, that section names the tests the block has already run
and tells the model a boundary week listed there needs no second test
(DESIGN_block_progress.md §4.1). De-duplication keys on the planned benchmark *session*,
not the logbook, so a test performed but never recorded still counts.

**Same-day collision.** `save_workout` keys on (date, sport), so a second
same-sport session on a benchmark date would overwrite the test. Deterministic
rule, in `_drop_benchmark_collisions()` (`coach/service/workouts.py:165-193`, called at
`:413`, before the rest pass and before any save): on a date holding a benchmark of
sport X, drop any other proposed sport-X session and warn. The benchmark is identified
by its flag — no guessing needed. Sports are compared canonically, so a `road_biking`
session cannot slip past a `cycling` benchmark on a spelling.

### 4.2 Adapt — "reschedule, don't dilute"

**It is no longer adapt's section.** `workout accommodate` reschedules sessions in a
constraint's own window and can land on a test day, so the section is now a shared,
scope-parametrized helper (`coach/engine/workouts.py::_benchmark_task`) that both TASKs
append. The argument below is unchanged; what changed is that two commands now rely on it,
and that three phrases move with the scope — "a later day within THIS block" becomes "within
this window", the last-day fallback names the window's last day, and adapt's postponement
escape ("the next generated block re-places the test when it is due") becomes "the daily
adapt or the next generated block re-places it". That third one is the load-bearing
difference: adapt's promise is honest because a block boundary really does bring a
`workout generate`, and a window sitting mid-block has no such guarantee, so repeating it
would tell the model a postponement is cheaper than it is
(DESIGN_constraint_reschedule.md §9).

The section's closing line — "a benchmark you are NOT changing need not be returned at all"
— travels with it and must: `workout_revision_apply` infers `clear_benchmark` from a
returned change that drops the flag, and that inference is only sound because the prompt has
told the model an unchanged test may be omitted.

This is the one rule genuinely different from every other session, and it is
enforced the way every other adapt behavior is: **by instructing the model, not
by engineering guards around it**. A deterministic guard here would have to
reverse-engineer intent from a proposal batch — is this pair of changes a move,
a displacement, or a softening? (Concretely: exempting benchmark rows from the
overridden-workout deletion in `workout_revision_apply`,
`coach/service/adaptation.py:299-322`, would block the very deletion that completes a
legitimate move, leaving the test duplicated on both days.) That is precisely the
judgement the model already has in front of it, so the model keeps it — the deletion
there carries no benchmark exemption, as designed.

**The prompt rule** (`coach/engine/workouts.py:373-381`): never reduce or soften a
benchmark session; if the athlete
will not be fresh (negative TSB), move it *intact* — same content,
`benchmark_type` preserved — to a later day within the block and lighten the
days before it. Moving is necessarily the LLM's call: TSB is backward-looking
only (`garmin/pmc.py:47` — computed from *completed* load), so no deterministic
pass can know which future day will be fresh; the model, which sees the TSB
history and the planned load ahead, judges it. Fallback the model is told
explicitly: when the benchmark sits on the last day of the block and no later
in-block day exists, leave it in place and lighten the days before it —
slightly-off freshness beats a lost test.

A moved benchmark rides the normal apply path like any rescheduled session; the
flag travels because it is part of the model's output contract (§3.1). No
exemptions, no proposal rejection, no special-casing in the apply step. So that the
model can see what it is being asked to protect, the adapt prompt's planned-workout
rendering marks benchmark sessions (`coach/formatting.py:204-207`).

**The flag belongs to the test, not to its date.** A move and a postponement both leave
*another* session sitting on the test's old date, and that session is not the test. Stating
the rule as "preserve `benchmark_type` when the session is a benchmark" invited exactly the
wrong reading — the date holds a benchmark, so preserve — and a model-comparison run caught
three models emitting "Friends Group Ride [BENCHMARK]" after converting a test day into a
social ride. The prompt now says the flag travels with the test and that any other session
on that date carries `null`, and names what a mislabel costs: `benchmark record` and the
adherence matcher read the ride as the completed test, `_block_benchmark_lines` tells the
next generate run the block already tested (DESIGN_block_progress.md §4.1), and
`_drop_benchmark_collisions` starts protecting a group ride's date.

The app-side half is not a guard on the model's judgement but the absence of one: with
`benchmark_type` COALESCE-preserved on UPDATE (§3.1), a proposal that correctly emitted
`null` was overridden by the stored value, so the POSTPONE fallback above could not be
carried out however well the model followed it. `workout_adapt_apply` now passes
`clear_benchmark` when a returned change lands on a benchmark row without re-emitting the
flag (`coach/service/adaptation.py:401`). The signal is sound here specifically because
adapt tells the model that *a benchmark it is not changing need not be returned at all* —
so a returned change that drops the flag is a statement, not an omission. The accepted
cost is the mirror case: a model that softens a test *and* forgets the flag loses the
test's identity rather than keeping a diluted test — the better of two failures, since the
next generate re-places a missing test but nothing detects a 45-minute "FTP test". The
narrowest version of that mistake costs nothing anyway: a verbatim re-list that merely drops
the field never reaches the apply step, because `_revision_is_change()` compares title,
description and load and discards it as a no-op.

This composes cleanly with the existing block-boundary firewall
(`DESIGN_block_boundary.md`): `adapt` already never crosses into the next
mesocycle, and the end-of-block benchmark lands exactly where the block-boundary
machinery already nudges the athlete to replan the next block against fresh
numbers.

---

## 5. Capture

### 5.1 Propose → confirm, never silent

Recording a result **proposes** the update and asks for confirmation before it
touches anything — the same pattern the codebase already uses for constraint
extraction and coach-learning downgrades. Nothing auto-mutates thresholds
(`cli/benchmarks.py:119-144`: confirm before the write, replan hint after it).

Example:

    New FTP 250 (was 235, +6.4%) — record and suggest replanning the next block? [y/N]

The "suggest replanning" half of that sentence appears only when the new value crosses
`coach.threshold_replan_pct` against the current latest of the kind — the same band
`config_changed()` judges on (§3.3) — so the prompt promises a replan hint exactly when
one will follow.

### 5.2 Manual entry is the primary path for cycling (Zwift)

FTP tests are done indoors on Zwift (ramp or 20-min protocol), which **computes
and displays the FTP number on screen**. Meanwhile the app stores only Garmin's
bucketed zone-seconds, **not** the raw power stream (`garmin/load.py`) — so it
cannot recompute "20-min best power × 0.95" after the fact. The data simply isn't
there. Therefore:

    tm benchmark record cycling --ftp 250

is the primary capture path — reliable, one line, using Zwift's authoritative
value. Auto-extraction from the activity stream is explicitly **not** built first
(possible "later, other sports" idea, not load-bearing).

### 5.3 Activity matching confirms the test happened

The completed ride reaches Garmin Connect regardless of the Zwift→Garmin link,
because the athlete also records on a Garmin device. TrainMate's normal Garmin
pull sees it, and the adherence matcher (derived per-run — nothing persists a
completion flag today) confirms the *planned* benchmark was done. The FTP
*number* comes from the `benchmark record` command; Garmin's job is only "yes,
the test happened."

**What actually runs today.** The adherence matcher is benchmark-agnostic —
`trainmate/adherence.py` contains no benchmark-aware code at all. For each day it sorts
that day's activities by **load descending** (`:165`) and matches the *first*
sport-compatible one to each planned session (`:190-199`), consuming it so a second
planned session cannot claim it again. On a test date with two same-sport activities, the
heavier one wins. That is usually the right answer for a benchmark — a test is the day's
hard effort — but it is a heuristic, not a decision, and it never reports ambiguity.

> **Phase 3 (not built).** Rev. 1 specified a benchmark-aware refinement here: *"when two
> same-sport activities land on the test date, match the one whose load/duration is closest
> to the planned test; if the candidates are too close to call, print an error and let the
> athlete resolve it — never guess."* No such code exists. It belongs with the rest of
> Phase 3's benchmark-aware matching (auto-linking a result to its planned `workout_id`,
> §7), and is recorded here so the rule is not mistaken for shipped behaviour.

**No dedup needed:** the athlete deletes the Zwift-uploaded copy in Garmin
Connect by hand, so a single Garmin-native activity remains. (Known failure
mode, accepted: sync keys on `activity_id`, so a forgotten deletion
double-counts that day's load in the PMC. Not worth machinery until it actually
happens.)

### 5.4 Venue lives in preferences, not in the benchmark

The planned benchmark's description stays **venue-neutral** ("20-min FTP test or
ramp test"). The athlete's `user_profile.preferences` free text — already injected
into the coaching prompt (`coach/engine/prompt.py:52-57`) — carries the venue:

```yaml
  preferences: |
    ...
    Tests FTP indoors on Zwift (ramp or 20-min protocol).
```

The coach reads that during `workout generate` and phrases the session as
indoor/Zwift on its own. Zero new machinery, and the benchmark *type* stays
generic so an outdoor test just needs a preference edit.

---

## 6. Surfacing

- `workout list` carries a `[BENCHMARK]` marker (alongside `[MANUAL]`, `[SWAPPED]`) —
  `cli/workouts/_helpers.py:60-62`.
- CLI verb `benchmark`, mirroring `goal` / `constraint` (`cli/benchmarks.py`,
  dispatched at `trainmate_cli.py:325-337`):
  - `benchmark record <sport> --<kind> <value> [--date …] [--note …] [--source …]`
    — one `--<kind>` flag per logbook kind, generated from the vocabulary, so a new
    kind gains its flag for free. Pace kinds accept `M:SS` or a decimal.
  - `benchmark list` — the logbook, newest first, with deltas (signed per kind:
    lower is better for pace anchors, §3.2).
  - `benchmark rm <id>` — the correction path. A typo here is high-consequence
    (`--ftp 520` jumps the effective threshold and flags a replan);
    "latest row wins" makes delete-and-re-record a sufficient editing story.
  - `benchmark wipe` — advanced/hidden, alongside the other `wipe` verbs
    (`cli/benchmarks.py:188-197, 259-264`; `db/wipes.py:19-22`). Confirmation-gated;
    the blunt reset for a bad import.
- `status` shows the current effective threshold per kind and its last-tested date,
  with `max_hr` labelled `(config)` (`cli/status.py:128-157`).
- The intensity **block report** renders anchor movement across the reported window —
  `_benchmark_lines()` (`intensity.py:710`), fed both into the coaching prompt
  (`coach/service/context.py:251, 295-300`) and into `progress`
  (`cli/progress.py:846-856`). This is the shipped "is overload working?" surface.
- The read-only web dashboard has a **Benchmarks** view: `GET /api/benchmarks`
  (`trainmate_web.py:571-617`) returns the logbook plus the effective threshold set,
  each row carrying its `formatted` value and a direction-aware `delta`. It shares
  `benchmarks.with_previous()` with `benchmark list`, so the terminal and the browser
  cannot disagree about what a row is compared against. Reads only — recording stays
  in the CLI (see the read-only demotion in ARCHITECTURE.md §"Web dashboard").
- **Phase 3, not built:** the progress timeline (`DESIGN_progress_timeline.md`) plotting
  the anchor trend beside CTL. `trainmate/chart.py` has no anchor series today; the block
  report above covers the need in text.

---

## 7. Phasing

Removing thresholds from config (§3.4) makes the logbook the foundation
everything sits on, so it shipped first — not as a V2 refinement.

**Phase 1 — the logbook replaces config thresholds — SHIPPED:**
`benchmark_results` table · `benchmark record` / `list` / `rm` CLI ·
propose→confirm capture · service-layer effective-threshold overlay wired into
the profile flow and the staleness snapshot (threshold read relocated out of
the engine, generic prompt rendering, skip-absent-keys drift rule — §3.3) ·
one-off seeding + removal of `ftp`/`lthr` from config · cold-start nudge.
Standalone value delivered: thresholds are dated, trended, and feed the existing >5%
replan trigger.

**Phase 2 — planning & protection — SHIPPED:**
`benchmark_type` threaded through the workout plumbing (§3.1: column, model
output contracts in generate *and* adapt, `save_workout`, adapt rebuild dict) ·
placement prompt instruction + boundary-week post-check warning · opener day ·
the adapt "reschedule, don't dilute" prompt rule (§4.2) · same-day collision
rule · `[BENCHMARK]` marker.

Shipped after both phases, not planned in rev. 1: the `benchmark wipe` verb, the
block-report anchor lines, the read-only web Benchmarks view (all §6), and the
sport/kind mismatch check on `record` (§3.2).

**Phase 3 — richer — NOT BUILT:**
Activity matching auto-links results to planned benchmarks (`workout_id`), including
the same-date disambiguation rule (§5.3) · modeled/passive anchors (power-duration
curve for FTP, e1RM from rep-max sets) · progress-timeline integration (§6) · a
measured-vs-modeled coach learning. A per-exercise dimension on `e1rm` belongs here
too — it is what would retire the drift exclusion in §3.3.

---

## 8. Open decisions

- ~~**Anchor kinds beyond FTP/LTHR.**~~ **Resolved.** The machinery is kind-agnostic
  (§3.5) and the vocabulary shipped complete: `ftp`, `lthr`, `threshold_pace`, `css`,
  `e1rm`, `mas`. Later kinds reuse the identical `benchmark_results` shape, generic
  prompt rendering, `--<kind>` flag generation, and the skip-absent-keys drift rule
  with no code change beyond a row in `ANCHOR_KINDS`.
- ~~**Cadence knob.**~~ **Resolved as recommended:** fixed to one per mesocycle
  boundary, expressed as a prompt instruction plus the boundary-week post-check
  (§4.1). No config value.
- **e1RM auto-capture** eventually blurs the test/normal-session line (any
  rep-max set is a passive test) — deferred to Phase 3, noted here so the schema
  (`source: modeled`) already anticipates it.

---

## 9. Interactions with existing designs

- `DESIGN_block_boundary.md` — end-of-block benchmark is the natural replan
  trigger; adapt's within-block firewall already prevents a test from being
  dragged across a boundary.
- `DESIGN_pmc_fitness_fatigue.md` — TSB gates test-day freshness (the adapt
  prompt rule reads TSB history; benchmarks do not alter PMC math). TSB is
  backward-looking, which is why *moving* a test is the LLM's judgement, not a
  deterministic pass (§4.2).
- `DESIGN_progress_timeline.md` — the anchor time series is a first-class
  progress signal to plot beside CTL. Still Phase 3 (§6); the block report carries the
  signal in text meanwhile.
- `DESIGN_intensity_distribution.md` §10 — the block report's anchor-trend lines, and the
  reason `e1rm` is excluded from the drift check (§3.3).
