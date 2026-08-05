# Mesocycle intensity distribution

**Status:** Implemented · **Date:** 2026-08-02 · **Last revised:** 2026-08-04

Everything below ships. The sections written before implementation keep their present tense
where it still describes the code; where the code moved past them the section says so in
bold, and §11's rev notes carry the changes made after the first landing.

## 1. The problem

**TSS compresses two independent facts into one number.** 500 TSS is 12h30 of easy riding or
6h15 of threshold work, and CTL/ATL/TSB cannot separate them. Neither can the coach.
Time-in-zone is the only signal already in the database that recovers the distinction.

The failure mode this leaves open is **intensity creep**: easy days drift to moderate across a
base block while volume quietly gives way to make room. Weekly TSS stays flat, CTL stays flat,
the PMC reads healthy — and the athlete stagnates:

| Running, per week | Base 1 | Base 2 |        Δ |
|-------------------|--------|--------|----------|
| Z1 recovery       |    50m |    55m |     +10% |
| Z2 aerobic        |   5h00 |   3h39 |     -27% |
| Z3 tempo          |    35m |   1h20 |    +129% |
| Z4 threshold      |    15m |    18m |     +20% |
| Z5 VO2max+        |     5m |     7m |     +40% |
| Easy share (Z1-2) |    86% |    72% |          |
| **hrTSS/wk**      |    279 |    279 |  **+0%** |

The bottom row is what the coach sees today. The rows above it are what happened.

Two mechanisms hide this, and it is worth being precise about which:

- **Projection.** hrTSS is a *weighted sum of the very table above* (`HR_ZONE_TSS_PER_SEC`,
  `garmin/load.py`). It is not blind to intensity — it is a projection of a five-dimensional
  fact onto one axis. Many distributions map to the same scalar, and the shape is what is
  lost. Note what the table had to do to keep the bottom row flat: 81 minutes of Z2 had to
  disappear. That trade is the creep.
- **Smoothing.** Even when the projection does move, CTL is a 42-day EWMA. A step change of a
  few percent enters as something indistinguishable from ordinary week-to-week noise.

## 2. Volume, intensity, load

Three quantities this codebase does not consistently name apart. The rest of the document,
and §9's split of responsibility between `adapt` and `generate`, depends on the distinction:

- **Volume** — time. `duration_sec` on an activity, `duration_minutes` on a planned workout.
- **Intensity** — effort relative to threshold. In TrainMate this is *only* ever observable as
  the zone distribution. There is no other intensity signal in the schema.
- **Load** — TSS. One scalar folding the other two together (`compute_load`), structurally
  `duration × IF²`, and for HR-derived sessions literally `Σ(zone_seconds × weight)`.

```
load = volume × intensity
```

One number, two factors. Given load you cannot recover either factor — that is §1. It also
means **"hold the load" is an ambiguous instruction**: load can be held while trading volume
against intensity in either direction. §9 relies on naming the axis explicitly.

## 3. What already existed

**This section is the pre-implementation motivation. All three gaps are closed** — it is kept
because the rest of the design argues against it, not because anything here is still open.

Zone-seconds were already summed in two places, so this design was mostly consolidation:

- **Per elapsed mesocycle** — `coach/service/context.py::_build_prior_training_context` emitted
  HR `Z1-2/Z3/Z4-5` and power `Z1-2/Z3-4/Z5-7` minutes per block, for the strategy prompt. It
  now delegates to `_intensity_history_context` and the old rollup is gone.
- **Per week** — `coach/service/analysis.py` puts `zone_distribution_sec` and
  `power_zone_distribution_sec` into every weekly summary fed to the analysis LLM.

Three gaps, and they were the whole of this design:

1. **Not per sport.** Running Z4 and cycling Z4 were summed into one number. Closed by §6.
2. **Prior plan only.** The block-grained view covered the *previous* macrocycle. The block the
   athlete is currently in was never summarized, so drift was diagnosed after it was fixable.
   Closed by §4.1/§9.3.
3. **No delta.** Absolute minutes describe; block-over-block change is what carries signal.
   Closed by §4.1.

## 4. Grain

**(mesocycle × canonical sport × currency × zone)**, with a canonical sport mapping folding
aliases.

Per sport is not optional. Garmin applies different HR zone boundaries per sport, so running
Z4 and cycling Z4 are different physiological places; power zones exist only where a meter was
recording, so a merged table mixes a 7-zone Coggan model with a 5-zone Friel model. And the
merged view hides the finding: *"85% easy overall"* is unactionable next to *"running was 89%
easy, but every hard minute in the block went into cycling"*.

**Currency is per activity, not per sport (§6).** An activity joins its sport's HR row if it
recorded HR zones and the power row if it recorded power zones — both, when it did both.

**Rates, not totals.** Blocks are unequal length and the current one is always partial. Emit
per-week figures alongside the week count, so a partial block reads as partial
(`Build 1 (2 completed weeks of 4)`).

**A week is 7 days from the block's `start_date`, and only completed weeks divide:**

```
completed_weeks = (today - block.start_date).days // 7
```

The partial tail is excluded from the rate entirely. Including it biases every mid-block
reading the same way: the long easy session usually sits on the weekend, so a Wednesday
reading that divides by 2.3 weeks understates easy volume — every time, in the same
direction, which reads as a trend rather than as noise. That matters here more than
elsewhere: the drift this feature exists to catch is a ~27% fall in Z2, and the choice of
divisor moves the reported figure by more than the signal does (7h of Z2 over 16 days is
3h03/wk at 2.3 weeks, 3h30/wk at 2). Under 7 elapsed days there is no rate — emit raw
minutes and say the block is too young to rate. Weeks run from the block start rather than
from calendar Mondays, so a block starting on a Thursday has Thursday-to-Wednesday weeks:
ragged at one end only, and consistent between blocks, which is what comparison needs.

Both minutes and percentages. Percentages answer "was this block polarized"; minutes answer
"did easy volume actually go up". A block can hold 85% easy in both of two blocks while easy
volume falls 20%.

**Percentages are of recorded zone-seconds**, not of session duration — so they sum to 100 and
the coverage line (§7) carries the recording gap on its own. The two differ by exactly that
gap, and the gap is not spread evenly: it sits entirely below Z1.

### 4.1 The delta

Gap 3 above is the reason for the feature, so it needs specifying rather than implying.

**The delta is a plan-generation view, not an adapt view.** Block-over-block change answers
*"is intensity creeping across the macrocycle"* — a periodization question, and §9.2 assigns
those to `generate`. `adapt` gets the current block measured against its own stated focus,
plus the current week (§9.3); it never needs a preceding block.

- The **current block to date** against the **immediately preceding block**, both as
  per-week rates over completed weeks. Comparing a partial rate to a complete rate is the
  only sane pairing, and rates are what make it legitimate.
- **No new accessor, and no date-ordered mesocycle query.** Navigate macrocycle-first:
  `get_mesocycles_for_macrocycle` returns a block list in order, so the preceding block is
  the preceding element. `_build_prior_training_context` already walks the prior
  macrocycle's list exactly this way, and `planning.py` already holds `prev_macro` for the
  cross-plan case (`get_previous_macrocycle` exists). The one thing to avoid is the obvious
  query — `SELECT … FROM mesocycles WHERE end_date < ? ORDER BY end_date DESC`. Every
  existing mesocycle accessor filters `mac.status = 'active'`, and `set_active_macrocycle`
  marks the outgoing plan `superseded`, so that filter hides precisely the cross-plan case;
  but dropping it is worse, because `superseded` also covers earlier *versions* of the
  current plan (rollback history) whose blocks overlap the live ones in date and describe
  training that never happened. Navigating by macrocycle id fixes the lineage before any
  dates are compared, so neither trap can fire.
- A block whose sport mix differs materially from its predecessor gets the delta suppressed
  per-sport rather than globally: a sport absent from one side is reported as absent, never as
  a -100% swing.

## 5. Zones: every zone reported separately

```
HR      Z1 recovery · Z2 aerobic · Z3 tempo · Z4 threshold · Z5 VO2max+
Power   Z1 recovery · Z2 endurance · Z3 tempo · Z4 threshold · Z5 VO2max ·
        Z6 anaerobic · Z7 neuromuscular
```

No banding. The existing `Z1-2 / Z3 / Z4-5` grouping comes from the polarized-training
framing, which answers *"is the overall distribution 80/20?"*. Grading a block's stated intent
is a different question, and every boundary the grouping erases carries a coaching decision:

- **Z3 / Z4 / Z5.** Tempo, threshold and VO2max are three different blocks written three
  different ways. Under `Z4-5` a block labelled "VO2max development" that produced 48 min
  threshold and 11 min VO2max reads as "hard work up 90%, on track"; separated, it reads as a
  threshold block wearing a VO2max label. That verdict is the point of the feature.
- **Z1 vs Z2.** Z1 is recovery; Z2 is the aerobic base where a polarized week's volume is
  meant to sit. A "base" block showing 6h Z1 against 1h Z2 is not base training — it is junk
  volume, or sessions logged with a lot of coasting. Recovery weeks legitimately shift toward
  Z1. The artifacts that argue for merging (warm-ups, walking back to the car, autopause gaps)
  are real but small, and are a data-hygiene matter rather than grounds for erasing a
  physiological boundary.
- **Z6 vs Z7.** Power Z6 is anaerobic capacity (30s–3min, glycolytic); Z7 is neuromuscular
  sprint work. Merged, a `Z6-7: 25 min` block cannot be told apart from a lot of surging out
  of corners.

The aggregation in §8 sums per-zone seconds either way, so banding is extra code whose only
effect is to discard information.

The zone **labels** above travel with every figure — bare `Z1..Z5` is markedly less legible to
a model than named zones.

The polarized `Z1-2 / Z3 / Z4-5` rollup stays derivable at render time for any view that wants
it, including the existing weekly summaries, which are left alone. It is a lossy view
regardless: LT1 falls somewhere inside Garmin Z2–Z3, so the 3-band grouping never mapped
cleanly onto the polarized model to begin with.

## 6. Every sport gets a row; no sport is routed away

Routing sports into "endurance" and "structural" sections — `strength_training` to the latter,
its zone minutes dropped — was considered and rejected. The canonical mapping shows why
(`sports.py`):

```python
"strength_training": ["strength_training", "strength", "indoor_cardio", "fitness"],
"hiking":            ["hiking", "walking"],
```

Garmin's `indoor_cardio` and `fitness` fold into `strength_training`. Those are the buckets a
HIIT kettlebell session lands in — genuinely hard cardiovascular work, deliberately scheduled
here in place of a cycling interval session. Routing by sport drops its Z4 minutes, and a
block containing four such sessions reports **zero hard minutes**. A coach reading that
prescribes more intensity on top of intensity already done. That is not a rounding error, it
is the opposite conclusion.

So: **no routing.** Every canonical sport gets a zone row whenever it has zone data. Sports
with RPE or benchmark data additionally get a structural row. Nothing is excluded from either.

```
Base 2 (4 completed weeks) — focus "aerobic volume"
  Intensity distribution, per week
    running           [HR]  Z1 55m (13%)  Z2 3h39 (53%)  Z3 1h20 (19%)  Z4 48m (12%)  Z5 11m (3%)
    cycling           [pwr] Z1 22m (13%)  Z2 1h50 (63%)  Z3 25m (14%)   Z4 12m (7%)   Z5 4m (2%)
                            Z6 1m (1%)    Z7 0m (0%)
    cycling           [HR]  Z1 30m (12%)  Z2 2h40 (64%)  Z3 40m (16%)   Z4 16m (6%)   Z5 5m (2%)
    strength_training [HR]  Z1 12m (9%)   Z2 74m (55%)   Z3 26m (19%)   Z4 22m (16%)  Z5 1m (1%)
    Coverage: running 94% HR · cycling 96% HR, 67% power · strength_training 88% HR
    Note: HR during strength and interval-with-rest work reflects rest intervals as
    much as effort — read these rows beside the session RPE below.
  Structural work
    strength_training  11 sessions, 8h15, avg RPE 7.2, 410 sRPE load
    e1RM               102kg -> 108kg (+5.9%)
```

Three things that table does that the routed version could not: the kettlebell sessions' 22 min
of Z4 are counted; heavy-lifting Z2 minutes stay visible but annotated, so a model can discount
them against the RPE instead of never seeing them; and the bike appears under both currencies
on purpose — 4h21 of riding, of which the HR strap captured 4h11 and a meter recorded 2h54, so
67% of bike time had power. Rides without a meter stay visible in the HR row instead of
vanishing from a power-only view, and that gap is the coverage line's job to state, not the
table's to hide.

**Never sum the HR and power tables.** A ride with a power meter appears in both; they are two
views of the same time, never a total. The per-row percentages invite exactly that mistake, so
the rule belongs here and not in §10.

### 6.1 One canonical sport mapping

Two vocabularies currently disagree, and they are different *kinds* of list. `SPORT_MAPPING`
(`sports.py`) holds complete activity-type names, matched exactly by `canonical_sport`.
`CYCLING_TERMS` (`garmin/load.py`) holds substring *fragments* — `"ride"` is not an activity
type — matched loosely by `sync.py`. `SPORT_MAPPING` has no `gravel_cycling` or `cyclocross`;
`CYCLING_TERMS` has both. So a gravel ride *does* get its power zones fetched, then falls
through `canonical_sport` unchanged into a `gravel_cycling` row of its own — one athlete's
cycling split across two rows, each looking like less volume than it is. That is a live bug
today, independent of this feature.

**One table, exact matching, canonical name `cycling`:**

```python
"cycling": ["cycling", "road_cycling", "road_biking", "gravel_cycling",
            "mountain_biking", "cyclocross", "bmx", "indoor_cycling",
            "virtual_ride", "biking"],
```

`CYCLING_TERMS` is deleted and `sync.py`'s gate becomes
`if canonical_sport(type_key) == "cycling":`. `road_biking` was never an honest canonical
name — `indoor_cycling` and `virtual_ride` are already aliases of it, so a trainer session is
currently stored as "road biking" — and road, gravel, cyclocross, MTB and BMX all share one
set of Garmin cycling zone boundaries, which is the §4 criterion for sharing a row.

**The gate stays, and it is a classifier, not a pre-filter.** It guards reading `avgPower`
from the activity summary into `bike_avg_watts`, and power zones are fetched only when that
produced a number. Garmin reports `avgPower` for running too (watch- or Stryd-derived), and
running power has nothing to do with the Coggan cycling model — without the gate a run's
watts land in `bike_avg_watts` and its zones get scored against a cycling FTP.

**Exact over substring, because a miss is visible and repairable.** An unrecognised type
falls through `canonical_sport` unchanged and appears as its own row with an HR row and no
power row — you see `e_bike_ride` sitting in the table and know what alias to add. And
`data pull -d A..B` re-pulls any window while `save_completed_activity` upserts every
zone column explicitly, so adding the alias and re-pulling that range fills the data back in.
Nothing is lost permanently, which is what would otherwise have argued for keeping the
permissive substring net.

**Canonicalize on read, not on write.** `completed_activities.activity_type` keeps Garmin's
raw string; every read path goes through `canonical_sport()`. AGENTS.md asks for exact values
in storage with reduction only on display, and overwriting `gravel_cycling` with `cycling`
in the column is a lossy write undoable only by a re-pull. Read-time normalization gives the
same single vocabulary — it is already how planned workouts match activities — without
discarding the original.

**The rename is DONE — do not re-apply any of it (checklist kept for the record).** It touched
six code sites that spelled `road_biking` out: the generate and adapt LLM response schemas
(`coach/engine/workouts.py`), two CLI `choices` lists (`cli/goals.py`), the learnings-schema
example (`coach/engine/__init__.py`) and `workout add`'s help text
(`cli/workouts/parser.py`). All six now say `cycling`; the only survivors of the old spelling
are `SPORT_MAPPING`'s alias list and `benchmarks.SPORT_ANCHORS`, both of which keep it on
purpose so history still resolves. The one-off
`UPDATE workouts SET sport_type='cycling' WHERE sport_type='road_biking'` has been applied
(zero such rows remain), so the plan and the report no longer disagree on screen. The test
files that still spell it use it as an alias-folding fixture.

**No currency or "HR is ambiguous here" flags** — under the no-routing rule nothing would
read them, and the annotation above is one static footnote rather than per-sport
configuration.

What is lost: a technical MTB ride and a road endurance ride now share a row, and their zone
shapes genuinely differ (terrain-driven surges, coasting). Accepted — the split-row bug costs
more than the merged view does.

## 7. Measurement caveats are emitted, not corrected

The app aligns; the LLM reasons (`DESIGN_quantitative_context_impact.md`). Each artifact that
would mislead a model reading the numbers naively is stated as a fact beside them:

- **Coverage.** `config.hr_zone_coverage_min` exists because low HR coverage means the effort
  sat *below* Z1. A base block of genuinely easy sessions can show few zone minutes, and a
  model reading that will prescribe more aerobic volume. Emit the coverage fraction per sport
  **and per currency**, so absence reads as "not recorded", not "not done" — the same
  reasoning as the existing `power_zone_distribution_sec: None` guard. Note the columns
  themselves cannot distinguish the two: `garmin/sync.py` writes zeros, not NULLs, when an
  activity has no average HR. The coverage fraction does all this work.

  **One formula, both currencies** — matching the existing per-activity `_hr_zone_coverage`:

  ```
  coverage = Σ(zone seconds for that currency) ÷ Σ(duration_sec)
  ```

  summed over every activity of that canonical sport in the window, so the numerators share
  a denominator and the two percentages are directly comparable. Deriving the power figure
  against *HR* time instead would put two different denominators under one word. Two
  consequences: a ride with no meter contributes its full duration and zero power seconds,
  which is exactly what "a third of your bike time had no meter" should mean; and a currency
  row is emitted only when that currency has recorded seconds for the sport, so strength
  training never grows a `[pwr] 0%` row.
- **HR lag under-reports Z5 on short intervals.** HR needs 60–90s to climb, so a 30/30 VO2max
  session banks most of its seconds in Z4. Measured by HR, a genuine VO2max block *will* look
  like a threshold block. Two consequences: for cycling the power table is preferred (power is
  instantaneous), and a one-line note travels with any HR-derived Z5 figure.

### 7.1 Threshold drift is prevented, not detected

Garmin bucketed each activity using the zones in force *at the time*, derived from Garmin's
own FTP and lactate-threshold values — not TrainMate's benchmark logbook. An auto-detected FTP
bump moves the Z4/Z5 boundary, and the same effort then lands one zone lower: a block looks
easier when nothing changed. Per-zone reporting surfaces this at every boundary rather than
two, and §4.1's delta is precisely the view it contaminates.

An earlier draft proposed detecting these moves by reading Garmin's threshold history
(`get_cycling_ftp`, `get_lactate_threshold`). Rejected in favour of removing the cause:

**Prerequisite — Garmin's automatic FTP and lactate-threshold detection must be off.** They
are two independent settings (cycling FTP, running lactate threshold); disabling one leaves
the other drifting. TrainMate treats the athlete's recorded benchmarks as authoritative, and
Garmin's auto-updates silently redefine the zone boundaries in the activity history underneath
them.

This is documented in README.md and in `benchmark record`'s help text — the latter because
that is exactly the moment an athlete is entering a real tested value and needs to know Garmin
will otherwise overwrite the ruler behind their back.

Nothing recomputes anything: the bucketing is already done when the data arrives, and there is
no raw stream to re-bucket. So this fixes the future only. Every activity already stored was
bucketed under whatever zones were in force then.

**Documentation only — no freeze date is tracked, and no delta is suppressed.** An earlier
draft had §4.1 discard any comparison straddling the day auto-detection was disabled. Dropped:
the switch is flipped in Garmin Connect, so the app is never told when it happened, and there
is no honest way to learn it. Storing the date as config would mean an athlete hand-entering a
number that then silently governs whether the feature produces output at all — and the two
readings of an unset value (trust nothing, so every delta vanishes; trust everything, so the
caveat never fires) are both wrong. Not worth the machinery.

The residual risk is stated here instead, once: a delta showing hard minutes falling sharply
for no visible reason may be an FTP auto-bump moving the Z4/Z5 boundary rather than a change
in training. Manually-edited Garmin zones are invisible in the same way. Both accepted.

No app-coded verdict on whether a block matched its focus. The block's free-text `focus` is
emitted beside the measured distribution and the model judges — the same split as the existing
"planned vs actual" section, which this replaces.

## 8. Storage: computed on read

A sum over rows already stored. No new table, no migration, no Garmin calls, no LLM calls. The
aggregation currently inline in `_build_prior_training_context` is extracted into a reusable
helper taking a date window, and called three ways: the current block to date and the current
week (for `adapt`), and the preceding block (for the strategy prompt's delta). One `UPDATE`
on `workouts.sport_type` for the §6.1 rename is the only write.

One accessor detail, since the current block is new territory: `get_active_mesocycle` falls
back to the next *future* block, then to the absolute first one, when today sits inside none.
Called naively that renders `Build 1 (0 completed weeks of 4)` with an empty table for a block
that has not started. The helper takes the block it is given and reports nothing when today is
outside every block.

## 9. Where it surfaces

Two views, because two different questions:

- **`workout adapt` — primary.** *Is this block being executed as written?* The current block
  to date beside its stated focus, plus the current week (§9.3). No preceding block, no
  delta. Drift caught in week 2 of a block is correctable; drift diagnosed at plan-generation
  time is history.
- **Strategy prompt — the delta's home among the prompts.** *Is intensity creeping across
  blocks?* Replaces the inline computation in `_build_prior_training_context`, now per-sport
  and per-zone, and extended to the current plan's elapsed blocks (it walks only the prior
  macrocycle today — gap 2 of §3). No *prompt* other than this one carries the delta; §9.6
  puts it in front of a human too, which is a different constraint and not in tension with
  §4.1.
- **`tm status` — the block you are in.** Rendered in the block summary display, beside
  `Cycle Focus`. The snapshot: current block, all sports, no history.
- **`tm progress -z [sport ...]` — the trend (§9.6).** *Where did it change?* One table per
  sport, weekly grain, the whole displayed window. The only view that survives a mesocycle
  boundary moving.
- **`data show-activities` — the receipt (§9.7).** *Which session, and is the recording
  trustworthy?* One row per activity, both currencies, nothing aggregated.

This also closes the loop with `DESIGN_evidence_based_confidence.md`: the coach prescribes a
concrete distribution ("hold Z2 near 4h30/wk, add 20 min Z4"), and the next block's measured
table grades it.

### 9.1 Why `adapt` did nothing with this

**Pre-implementation motivation; the missing branch below now ships.** The adapt TASK carries a
fourth branch (`drift_branch`, gated on `has_intensity`) beside the `CORRECTING EXECUTION
DRIFT` guidance and the `MEASURED INTENSITY DISTRIBUTION OF THE ACTIVE BLOCK` data section
(`coach/engine/workouts.py`). The argument is kept because it is what those three pieces of
prompt text answer to.

`DESIGN_block_boundary.md` §2 constrains `adapt` in two ways that must not be confused. The
**range boundary** — adapt's writes stop at the mesocycle end, and the next block is out of
reach — is mechanized in code and is what that document calls a firewall. It is irrelevant
here: intensity correction happens entirely inside the current block. The **mandate** — adapt
is tactical, eases transiently, does not reshape periodization — is prompt text plus one tag
string, and it is the part in play.

The obstacle was narrower than "the mandate forbids it". It was a missing branch. The adapt
TASK (`coach/engine/workouts.py`) offered exactly three:

```
- If they are showing high fatigue or injury risk ... replace hard workouts with
  recovery or rest.
- If they have missed key workouts, adjust the remaining workouts ...
- If they are fully recovered and on track, keep the plan as scheduled or make
  minor optimal adjustments.
```

Fatigue, absence, fine. An athlete three weeks into running their easy days at Z3 has normal
RHR, normal HRV, TSB -5 and a perfect attendance record. They fell into branch three and the
plan was kept as scheduled — with the drift table sitting unused in the prompt. The model is
additionally told to return only changed sessions, so silence was the compliant answer.

**Every path in that TASK treated adaptation as a response to fatigue or absence. Intensity
drift is neither: the athlete showed up for everything and feels fine.** Hence the fourth
branch, §9.4's.

### 9.2 The line: adapt owns execution, generate owns periodization

> Changing what zone Tuesday's run is prescribed at is **execution** — adapt's call.
> Changing how many hard sessions the block contains is **periodization** — not adapt's call.

Stated in §2's vocabulary: adapt may move the intensity factor of a scheduled session. It may
not change the block's composition. This resolves the apparent conflict with
`DESIGN_block_boundary.md` without loosening anything about fatigue-driven cuts, and it gives
"do not reshape the mesocycle" a definition it currently lacks.

### 9.2a Amendment: the other end of the handoff

§9.4's drift section ends by telling the model that a block genuinely containing too much hard
work "belongs to the next `workout generate`, not to you". As first shipped, that escalation
landed nowhere: `workout generate` was given no measured intensity at all. The athlete was
told to run the one command that could act — and it could not see the evidence.

This section is the amendment. **`generate` now receives the measured distribution too**,
threaded through the block-progress section (`DESIGN_block_progress.md`). §9.2's line is
unchanged — adapt still may not alter composition — but the consumer §9.2 assigns composition
to can finally read the signal it is meant to act on.

Three things travel to `generate` that `adapt` deliberately does not get:

- **The block-over-block delta** (`block_report`'s `previous=`). Its own docstring already
  said this belongs to plan generation; `_intensity_block_context` withholds it from adapt for
  exactly that reason. Intensity creeping up every block is periodization by definition.
- **What the plan PRESCRIBED over the same weeks** (`block_report`'s `fetch_workouts=`), from
  §9.8's `planned_zone_sec`, rendered by the same `format_table` at the same divisor so the
  two are compared line for line.
- **The composition verdict itself** — permission to change how many hard sessions the
  remaining weeks hold, which is the thing adapt is forbidden to touch.

**Why the prescribed table is load-bearing, not decoration.** A block measuring off its focus
has two opposite causes and they demand opposite responses:

| measured vs prescribed | measured vs focus | cause | whose |
| --- | --- | --- | --- |
| agrees | disagrees | the plan is mis-designed | `generate` — re-shape the remaining weeks |
| disagrees | disagrees | the athlete is mis-executing | `adapt` — sharpen the prescription |
| agrees | agrees | nothing wrong | nobody |

Without the prescribed table `generate` sees only the second column, and the failure mode is
sharp: an athlete running their easy days at Z3 makes a threshold block measure like a tempo
block, and a coach reading that alone cuts the threshold work. That **rewards the drift** —
the athlete gets an easier block for ignoring the plan, and the block's intent is lost to the
very deviation adapt was correcting. The prompt therefore forbids re-shaping around a
measured-vs-prescribed gap by name.

`adapt` is deliberately not given the prescribed table. Measured diverging from the
prescription is precisely the execution question adapt already owns via §9.4, and it has the
sharper instrument for it: a guard rail on the next session.

**`plan generate` gets it too.** The rule is the consumer's job, not one command's: whoever may
reshape blocks needs both columns of the table above, and `plan generate` reshapes them at the
coarsest grain there is. It reads the pair per *elapsed* block via
`_intensity_history_context`, which already passed `previous=` for the block-over-block delta
and now passes `fetch_workouts=` alongside it. Without the prescribed column the failure mode is
the same one described above, one level up: a threshold block that measures like a tempo block
because its easy days were run hard would be *replanned* as a tempo block, writing the drift
into the periodization instead of correcting it. The consumer list is therefore
"the two generate paths, never adapt" — see DESIGN_backward_evaluation.md §6 (AS BUILT).

### 9.3 What `adapt` sees, and how it gets there

Two blocks, neither of them a rate:

```
Build 1 — focus "threshold development" (2 completed weeks of 4, plus 2 days)
  Block to date, per week (2 completed weeks)
    running  [HR]  Z1 55m (13%)  Z2 3h39 (53%)  Z3 1h20 (19%)  Z4 48m (12%)  Z5 11m (3%)
    ...
  Current week so far — day 2 of 7 (29% elapsed)
    running  [HR]  Z1 8m  Z2 46m  Z3 22m  Z4 4m  Z5 0m
    ...
```

The current week is **raw minutes with the elapsed fraction stated, never extrapolated**.
Turning 22 min of Z3 on day 2 into "77 min Z3 this week" would be a fabrication; a model given
the raw figure and "29% elapsed" reasons about it perfectly well. It is also the part that
makes this feature worth putting in `adapt` at all: two days in, already over the week's whole
Z3 allowance is correctable *now*, which the block-to-date average would take another fortnight
to reveal.

**Threading.** Follow `pmc_context` exactly — it already does this end to end. Compute in the
service layer in `coach/service/adaptation.py`, beside the `_pmc_prompt_context(...)` call;
pass as a new named argument into `self.engine._workout_adapt_logic(...)`; accept it in that
function's signature in `coach/engine/workouts.py` (which already ends `pmc_context:
Optional[str] = None`) and render it as its own section.

**Not via `meso_text`.** That string is built by `_get_active_strategy_and_meso_text`
(`coach/service/prompt.py`) and handed to *both* plan generation and adaptation, so putting
the table there would silently grow the generate prompt a section §9.2 says it should not
have. It is the shortest path and nothing would fail; hence stating it.

### 9.4 Prompt changes

One added TASK bullet, alongside the existing three:

```
- If the block's measured intensity distribution has diverged from its stated
  focus, correct the prescriptions of the sessions still ahead — even when
  recovery metrics are fine. A healthy athlete executing the wrong workout is
  the case no other branch here covers.
```

One added section, in the shape of the existing `DO NOT COMPOUND` and `PROTECTING A BENCHMARK`
blocks:

```
CORRECTING EXECUTION DRIFT:
The block summary shows what the athlete's sessions ACTUALLY measured, per sport
and zone, beside the block's stated focus — as a per-week rate over the block's
completed weeks, then the current week's raw minutes so far with how much of that
week has elapsed. The current week is NOT extrapolated: read it against the
elapsed fraction yourself. When the measured picture and the focus disagree, that
is an execution error, not a fatigue signal — and it is yours to fix.

Correct it by changing HOW the remaining sessions are prescribed, not how much
they contain. Hold duration and planned TSS; sharpen the intensity target and
give it an explicit guard rail the athlete can act on mid-session (a HR ceiling,
a pace cap, "walk the hills"). Name the evidence in change_reason so the athlete
sees why.

Drift upward usually means the athlete WANTS more, so do not only cap it — say
where the appetite may legitimately go, in the batch-level reason. Spend it in
the block's own currency: in a volume block, more easy minutes; in an intensity
block, a fuller effort on the days already designated hard. If what they want
exceeds that, say plainly that it is a change to the block itself and belongs to
the next plan generation, not to a daily adaptation.

Drift downward mirrors this: a VO2max block measuring as a threshold block means
the sessions are being under-executed, so the guard rail becomes a floor and the
advice is about how to reach it. Condition this on the power table where one
exists — HR lag makes under-execution look real when it is not (§7).

This is not a load reduction and must not become one. If the block genuinely
contains too much hard work — as opposed to easy work being run too hard — that
is a periodization question, and it belongs to the next `workout generate`, not
to you.
```

That last paragraph hands a decision to `workout generate`. §9.2a is the other end of the
handoff: generate receives the measured distribution, what was prescribed beside it, and the
block-over-block delta, so the escalation reaches a prompt that can act on it.

The correction is load-neutral by construction. What the model emits for a drifting Tuesday
changes only the prose:

```
date / sport_type / duration_minutes / tss / title    unchanged
description    "60 min conversational. HR ceiling 145 — hard cap, walk the
                climbs if needed."
change_reason  "Third block week where 'easy' runs averaged Z3; adding an
                explicit ceiling."
```

…with the batch-level reason (`adaptation_summary`) carrying the appetite advice: *"…if you
want more this week, add 15–20 min to Sunday at the same easy effort — that's this block's
currency. Adding actual hard work changes the block's shape and belongs in the next plan
generation."*

### 9.5 The one collision with existing machinery

A drift correction saves through `save_workout`, which stores `adapted_at` and bumps
`adaptation_count` whenever it is handed a timestamp — and `coach/service/adaptation.py`
mints one per run and passes it to *every* session it saves. The session then carries
`[ALREADY EASED by a prior adaptation …]`, and the `DO NOT COMPOUND` section instructs the
model to default to holding it and to raise its bar with each prior easing.

Nothing was eased. But the next time the athlete is genuinely wrecked, the model now has a
raised bar for cutting a session that was never cut. **Left unhandled, this feature would
blunt adapt's fatigue response.**

Classifying the edit at save time is not the fix — it is a judgement call with a real grey
zone. Pinning a session's intensity while holding duration leaves *planned* TSS untouched but
does reduce *realized* load, because the athlete had been overshooting. Whether that is "a
cut" has no clean answer, and any rule encoded here will be wrong sometimes.

The fix is an **append-only workout changelog** — every edit recorded in order with every
field we have (title, description, zones, duration, TSS, date, timestamp, origin) — so the
prompt reads the actual history and the model classifies. Same principle as §7. It also turns
six hand-maintained denormalizations (`original_description`, `original_duration`,
`original_tss`, `original_date`, `adapted_at`, `adaptation_count`) into derived views of a
history that would properly exist, which is what `modification_state.py` already argues for.

That is a new table touching every workout write path and belongs in its own design doc; this
feature does not block on it.

**Interim stopgap — decide per session in the save loop, not in `save_workout`.** The DB layer
is a generic writer that stamps whatever it is told, and other callers rely on that; the caller
is what must stop handing over the timestamp. The loop in `coach/service/adaptation.py` already
fetches the pre-save row on its first line (`existing = self._db.get_workout(...)`), so the
comparison is free:

```python
# A drift correction rewrites the prescription without touching the load, so it is not
# an easing — stamping it would raise the DO NOT COMPOUND bar for a session never cut (§9.5).
eased = not existing or (
    w.get('duration_minutes') != existing['duration_minutes']
    or float(w.get('tss') or 0) != float(existing['tss'] or 0)
)
```

then `adapted_at=adapted_at if eased else None`. Three things that wording settles:

- **Against `existing`, not `original_*`.** A session planned at 60 min, cut to 45 last week,
  re-worded today: against the current row nothing moved, so it stays unstamped — right.
  Against `original_duration_minutes` it looks changed, re-arming the tag for an edit that
  eased nothing.
- **TSS is compared as a float with a `or 0` on both sides.** The model emits `30.0` against a
  stored `30`, and `None != 30.0` would otherwise read a missing TSS as a change.
- **A session `adapt` newly introduced (`not existing`) counts as eased.** There is no prior
  form to compound and no load to compare, so this preserves today's behaviour.

### 9.6 `tm progress -z [sport ...]` — per sport, weekly grain

`tm progress` (`DESIGN_progress_timeline.md`) is the fullest rendering anywhere of the
picture §1 calls a lie: a CTL sparkline, a weekly TSS column, an adherence percentage and a
forward projection, every one of them computed from load alone. An athlete whose easy days
have drifted to tempo reads that screen as seven flat weeks at 97–101% adherence. So this is
where the correction belongs, not only in `status`.

**Positional sports, one table each, defaulting to every trained preference** (under `-z`, the
flag argued for below). Fixing the sport is what makes a weekly grain possible at all: §4
forbids merging sports and §6 forbids adding the HR and power rows of one sport, so a week's
honest distribution across a
multisport athlete is three or four rows of five-to-seven named cells — a screenful per week.
Fix the sport and a week collapses to one line. The argument buys the grain.

But fixing it to *one* sport buys the grain at the price of a new lie, and it is the mirror
image of §1's. An athlete who swapped two planned runs for two rides of equal TSS reads
`tm progress running` as whole-athlete load held flat beside a collapsed aerobic base —
which is the exact signature of intensity creep, on a week where nothing went wrong. So the
grain is per *table*, not per *screen*: the default stacks one zone table per sport, and the
cycling table rising as the running table falls makes "they rode instead" self-evident.

Which sports, in order:

- Those named in `athlete.sport_preferences`, **in config order** — the athlete's own
  priority list, and stable across invocations. A screen someone checks daily must not
  reshuffle its rows because last week's volume moved.
- That have at least one activity **with zone data** in the window. A sport with sessions but
  no recording renders a table of `—` and says nothing; it is named in the footer instead.
- That account for at least **10% of the window's total duration**. The rest are named, never
  silently dropped, matching the honesty the load table already has with
  `+N more (--weeks all)`:

  ```
  yoga, ski_touring omitted (under 10% of volume) — name them to see: tm progress yoga
  ```

Naming sports explicitly overrides all three filters: `tm progress running cycling` renders
exactly those two, in that order, however little of the window they cover.

**Two failures the argument has to handle, and only one of them is the typo.** An
unrecognised name is the rare case; the common one is a name that resolves fine and has no
rows — `tm progress yoga` on an athlete who does yoga without a strap. Both must key on *no
rows in the window*, not on *not a known sport*, and both list the sports that do have data.
Note `canonical_sport` passes unknown values through stripped and lowercased (`sports.py`),
so nothing is "unrecognised" at that layer and the list must come from the data.

That leaves the default itself as the one unguarded input. `sport_preferences` is free-text
config whose only current consumer joins it into a prompt string
(`coach/engine/prompt.py`), so `"Road cycling"` normalises to `"road cycling"` and matches
nothing. **TrainMate should warn at config load on any preference that is not a canonical
sport** — a warning and not an error, because `SPORT_MAPPING` has no `swimming` or `rowing`
entry and a genuinely new sport must still round-trip:

```
Warning: sport_preferences: 'Road cycling' is
  not a known sport — it will be matched
  literally against activity types. Did you
  mean 'cycling'?
```

**"At config load" means at the point of use, not in `Config.__init__`.** `config.py` has no
validation pass — every setting is a lazy property, and the only thing that happens at load is
`yaml.safe_load`, at import time, in every process there is. A warning there greets `tm --help`,
the Telegram bot and the web app alike, none of which read this list. It belongs where the list
is consumed: `progress` resolving its default sports, once per invocation that uses them.

**It scopes the intensity content only.** CTL, ATL, TSB, the projection and the WEEKLY LOAD
table stay whole-athlete. A running-only CTL is not a quantity — the fitness model integrates
every session the body paid for — and adherence is measured against the whole plan. `tm
progress cycling` therefore shows whole-athlete form beside cycling-only intensity, and the
help text must say so, because the command shape invites the opposite reading. The multi-sport
default largely dissolves the false-creep reading above, but an explicitly narrowed
invocation reintroduces it, which is why each zone table's header carries its share of the
window: `5h42 of 9h10 total`. One number, and the collapsed week reads as a sport swap
instead of a collapse.

(An optional argument with a default is `--sport`'s case by the convention in `cli/`;
positional is the call taken, for `tm progress cycling` over `tm progress --sport cycling`.
`nargs="*"` carries the multi-sport form, and `_build_keyword_spec` skips positionals
(`cli/argparse_ext.py`) so bare tokens still pass the dashless translator — worth a test, since
this is the first positional on `progress` and `tm progress weeks 4` must keep working.)

**When the tables are shown, they are the per-zone tables themselves — every zone, no rollup.**
An earlier draft put a single `easy` column (the selected sport's Z1-2 share) beside `adh` and
kept only that on the default screen. That column existed only because a merged-sport table
could not fit a row, and the sport argument removed that constraint; keeping it would have
left §5's argument — no banding, every zone stands alone, because every boundary the grouping
erases carries a coaching decision — contradicted by the one view an athlete looks at daily.
It also read worse. `easy 86% → 68%` says *something moved*; `Z2 5h00 → 4h00` beside
`Z3 35m → 1h10` says a fifth of the aerobic base was traded for tempo, which is the sentence
the feature exists to produce. The rollup stays derivable for any consumer that wants it (§5);
nothing renders it here.

**The tables are opt-in, behind `-z/--zones`.** An earlier draft of this section put them on
every invocation, arguing that intensity drift is the failure an athlete cannot know to ask
about. Measured on the shipped layout that price is too high to charge unconditionally: the
tables roughly triple the length of `tm progress` (~23 lines to ~38 for one sport, ~13 more
per additional sport), and they answer a different question from the load table above them —
*where did the intensity go*, not *how much work was done*. So the load table, the PMC and the
projection stay the default screen and `-z` adds the intensity half. **Naming a sport implies
`-z`**, because naming a sport is already a request for its zone table, and `--blocks` implies
it too. The help text and `ARCHITECTURE.md` state the flag; nothing about the tables' content
changes with it.

The load table, then one table per sport, all sharing week labels and band rules so they scan
as one unit:

```
WEEKLY LOAD plan  ▓done ▒plan  done  adh
── Base 1 — Aerobic Volume Accumulation ────────
w/c 05-25    655  ▓▓▓▓▓▓▓▓▓▓▓│  660 101%
w/c 06-08    655  ▓▓▓▓▓▓▓▓▓▓▓│  661 101%
── unplanned ───────────────────────────────────
w/c 06-15    655  ▓▓▓▓▓▓▓▓▓▓▓│  660 101%
w/c 06-22    655  ▓▓▓▓▓▓▓▓▓▓▓│  651  99%
── Base 2 — Aerobic Volume Consolidation ───────
w/c 06-29    655  ▓▓▓▓▓▓▓▓▓▓▓│  644  98%
w/c 07-06*   470  ▓▓▓▓▓▓▓▓▓│░░  473 101%
w/c 07-13    655  ▒▒▒▒▒▒▒▒▒▒▒░
w/c 07-20    655  ▒▒▒▒▒▒▒▒▒▒▒░
w/c 07-27    655  ▒▒▒▒▒▒▒▒▒▒▒░
w/c 08-03    280  ▒▒▒▒▒░░░░░░░
* in progress · plan ends 08-05 (Wed)

ZONES running [HR 94%] — 5h42 of 9h10 total
week           Z1    Z2   Z3   Z4   Z5
── Base 1 — Aerobic Volume Accumulation ────────
w/c 05-25     50m  5h00  35m  15m   5m
w/c 06-08     48m  5h02  36m  16m   5m
── unplanned ───────────────────────────────────
w/c 06-15!    28m  2h26  20m   8m   4m
w/c 06-22     55m  4h18 1h02  17m   7m
── Base 2 — Aerobic Volume Consolidation ───────
w/c 06-29       —     —    —    —    —
w/c 07-06*    22m  1h22  38m   7m   3m
Z1 recovery · Z2 aerobic · Z3 tempo
  · Z4 threshold · Z5 VO2max+ · * in progress

ZONES cycling [pwr 88%] — 11h20 of 12h05 total
week           Z1    Z2   Z3   Z4   Z5   Z6   Z7
── Base 1 — Aerobic Volume Accumulation ────────
w/c 05-25     40m  2h10  25m  12m   6m   2m   1m
w/c 06-08     42m  2h05  28m  14m   6m   2m   1m
── unplanned ───────────────────────────────────
w/c 06-15     38m  1h58  22m  10m   5m   2m   1m
w/c 06-22     45m  2h20  30m  15m   7m   3m   1m
── Base 2 — Aerobic Volume Consolidation ───────
w/c 06-29    1h30  9h30 1h10  35m  15m   5m   3m
w/c 07-06*    35m  2h40  20m   8m   4m   1m    —
Z1 recovery · Z2 endurance · Z3 tempo · Z4
  threshold · Z5 VO2max · Z6 anaerobic
  · Z7 neuromuscular · * in progress
— not trained · ! zone minutes undercounted
  — the recording missed time
+3 more weeks (--weeks all)
```

Read the two zone tables together and `06-29` tells its own story: running absent, cycling Z2
at 9h30. One sport's table alone would have called that a collapsed aerobic base.

**~~The zone tables cover past and in-progress weeks only.~~ Superseded by §9.8.** As first
written: the future half of the load table is planned TSS, and until §9.8 lands there is no
intensity target to put under it, so the load table keeps its ghost bars while the zone tables
simply stop at today — the one place the two halves do *not* align row for row, visible in the
mockup above as four planned weeks below the last zone row.

§9.8 shipped, so that asymmetry is closed: weeks beyond today render what the plan
PRESCRIBES, marked `+`, ghost rows under today exactly like the load table's ghost bars, with
a footer note where the plan for a sport was written in the other currency. The mockup above
is therefore the *past-only* shape and is left as drawn; §9.8 carries the current one.

**Cell format: four characters, five for Z1 and Z2.** `fmt_duration` renders `12h30` at five
characters, and a 7-zone power table of five-character cells is 55 columns — it overruns the
48-column budget precisely for the high-volume cyclist the power table exists to serve. The
table therefore caps its cells:

| Duration | Cell | Chars |
|---|---|---|
| under 1h | `55m` | 3 |
| 1h–9h59 | `5h00` | 4 |
| 10h and up | `12h` | 3 |

with Z1 and Z2 given a six-wide column so they keep their minutes past ten hours — the only
two zones that ever get there, and they get there on exactly the hiking, ski-touring and
high-volume cycling weeks where the aerobic base is the whole question. Z3 and above never
reach ten hours in a week, so nothing above tempo loses precision anywhere.

That lands the 7-zone power table at **48 columns** and the 5-zone HR table at **38**, both
inside the budget, and the band rules span the full 48 in every table so the halves align.
Against the existing `WEEK_COL_WIDTH = 11`: 11 + 6 + 6 + 5×5 = 48 and 11 + 6 + 6 + 3×5 = 38,
beside a load row of 40. `block_report` keeps plain `fmt_duration`: it runs at `PROMPT_WIDTH`
and lays one cell per line at phone width, so width is not its constraint.

**The capped cell is a new formatter, and the grid lives with the load table.** `fmt_duration`
is unchanged — `block_report` and both prompt paths want `12h30` — so the cap is its own
function in `cli/progress.py`, beside `format_weekly_table`. The grid belongs there too, not in
`intensity.py`: it has to align row for row with the load table and it shares that table's week
column, band walk and 48-column budget. `intensity.py` keeps what it already owns, the
aggregation (`zone_rows`) and the prompt-width table the coach reads.

**Every glyph means one thing, and none of them overlap.** `~` is already taken: `meso_bands`
prefixes it to the label of a block TrainMate *reconstructed from training history* rather
than one a plan prescribed (`progression.py`), and the load table's legend reads `~ inferred`.
Reusing it for coverage would put two definitions of one character fifteen lines apart on one
screen.

The full set on this screen, kept here because this table is where a reader looks a marker up
(`cli/progress.py`, `NOT_TRAINED`/`UNDERCOUNTED`/`LOAD_SPARSE`/`PLANNED`):

| Glyph | Where | Means |
|---|---|---|
| `~` | prefix on a band label | block inferred from history, not prescribed by a plan (unchanged) |
| `*` | after the week label | week in progress, so its numbers are partial (unchanged) |
| `!` | after the week label | zone minutes undercounted — the recording missed time |
| `—` | in place of the numbers | this sport was not trained that week |
| `+` | after the week label | a FUTURE week: these are the plan's prescribed zones, not measurement (§9.8) |
| `?` | on the load table's week | the week's LOAD is undercounted too — a recording gap with no RPE (§11) |

Every legend is emitted only for the markers a given screen actually used, so the list above
is longer than any one invocation's footer.

The markers sit **in the week column**, beside the existing `*`, not in a right-hand gutter.
That is what frees the two columns the power table needs, and it is where they belong: they
qualify the week, not the last zone. `w/c 07-06*!` is eleven characters and fits — the current
week can be both in progress and undercounted, so that row wants a test.

**`—` is not a zero, and this is why the marker set needs three states rather than two.**
Coverage divides a currency's recorded seconds by the sport's *total* duration in the window
(§7), so a sport with no sessions divides by nothing and reads 0.0 — and `zone_rows` emits no
row for it at all. Rendered as zeros with a coverage marker, a week the athlete simply did
not run would assert that they trained without recording it, which is §7's meaning turned
exactly backwards. Not-trained, badly-recorded and genuinely-easy are three different facts.

**Coverage gets its own threshold, per currency.** `hr_zone_coverage_min` (default 0.5) exists
for one purpose: `garmin/load.py` uses it to decide whether to trust hrTSS or fall back to the
athlete's RPE. That is a "safe to compute load from" bar, not a "safe to show a human" bar — a
week at 55% passes it while missing nearly half its recorded time. And it is HR-named while
the power table needs one too, where coverage is structurally lower because a ride with no
meter contributes its full duration and zero power seconds. A display threshold of its own,
default 0.8, keeps `!` rare enough to still be read.

**~~A module constant in `intensity.py`, not config~~ — retracted; see §11's rev note.** The
argument was that `hr_zone_coverage_min` earns its config key by changing a computed load,
while this one only decides whether a table admits it is incomplete, which is not a knob an
athlete has a reason to turn. Real data disproved the second half: the flat 0.8 was an
endurance-sport bar and it fired on nearly every strength, ski, hike and yoga week, because
their uncovered time is rest between sets and chairlifts rather than a dead strap. The bar is
therefore three things now, resolved in this order by `intensity.coverage_display_min(sport)`:

1. `config.zone_coverage_display_min_by_sport[sport]` — the per-sport override, for an athlete
   whose own recording habits differ from the shipped calibration.
2. `intensity.COVERAGE_MIN_BY_SPORT[sport]` — the shipped per-sport table, keyed on
   **canonical** sports (§11's 2026-08-04 note: an alias key is unreachable).
3. `config.zone_coverage_display_min` — the global default, 0.8.

Still one value covering both currencies, and still no per-currency key. Where the currency
rule picks power below the bar — power at 65% because HR is worse — every week takes a `!`,
which is the honest reading and which the header's `[pwr 65%]` has already said once.

The global default shares a number with the currency rule's "prefer power at 80% window
coverage" (`PREFER_POWER_COVERAGE_MIN`) and shares nothing else: that one decides which column
a table is drawn in, over the whole window; this one decides whether a single week's row is
trustworthy. They are free to diverge and should not be folded into one constant.

**`!` speaks for the zone row alone, and must not borrow the load table's story.** An earlier
draft had the legend read `TSS beside them came from your RPE`, reasoning from `compute_load`'s
fallback:

| `method` | what happened |
|---|---|
| `power` | from power zones |
| `hr` | from HR zones, coverage adequate |
| `rpe` | coverage poor, load rescued from the athlete's entered RPE |
| `hr_sparse` | coverage poor **and** no RPE — the load number is undercounted too |

But that swap happens at `hr_zone_coverage_min`, not at the display bar. A week at 70% takes a
`!` and still returns `hr`: its TSS is hrTSS computed from these very seconds, and the sentence
would be false across the whole 0.5–0.8 band — which is to say on most `!` weeks, since the
display bar was introduced precisely to cover that band. So the legend keeps the glyph table's
wording, `! zone minutes undercounted — the recording missed time`, and asserts nothing about
the number beside it.

Where the load came from is a real fact and currently an invisible one, so it is stated where
it is true rather than inferred where it is not: per activity in `data show-activities` (§9.7),
and on the load table itself for the `hr_sparse` week (§11). Each half of the screen accounts
for its own instrument.

The `hr_sparse` case is worse and belongs to the *load* table, not this one: the athlete
trained normally, the strap died, no RPE was entered, and the week reads as a genuine
adherence miss that the coach will then adapt the plan around. `_measurement_is_load`
(`garmin/load.py`) already implements the test; the load table should mark it. Filed in §11
because it is a `progress` defect that predates this design.

**One currency per table, chosen by coverage.** An earlier draft chose power where *any*
displayed week recorded a meter. One metered ride in eight weeks then flips the table to
power and renders seven rows of `(no power recorded)`. Take the ordinary mixed case instead —
a cyclist whose weekend rides are on the power bike and whose two weekday commutes are on the
beater with a strap:

- power coverage ≈ 60% — accurate, and blind to the commutes
- HR coverage ≈ 95% — less precise at the top end, and complete

Power cannot answer "did my easy volume shrink", because the 40% it cannot see *is* the easy
commutes. So the rule is: **prefer power when its window coverage reaches 80%, otherwise take
whichever currency covers more of the window.** Chosen once over the window, never per row —
a column that switched currency mid-table would be adding HR minutes to power minutes down
the page, §6's one prohibition committed vertically instead of horizontally. `--power` and
`--hr` force the other one. A week with no data in the chosen currency renders `—`, not a
fallback to the other.

The chosen currency and its coverage go in the header — `[HR 94%]`, `[pwr 88%]` — so the
reader knows what fraction of the sport's time is on screen without consulting a legend. For
a mixed-meter cyclist neither currency is the truth: HR under-reads VO2max work, power
under-reads the commutes. That header number is the honest disclosure.

**Why a weekly grain exists at all, when §4's grain is the block.** A mesocycle is a plan
object and `workout generate` rewrites plan objects. Regenerate, and the boundaries move:
blocks shorten, shift or open a gap, and completed activities that used to sit inside a block
now sit inside none. Every block-grained view drops them silently — `block_report` reports on
the window it is handed and nothing tells it a fortnight went missing. **A calendar week is
not a plan object.** It cannot move, and every activity belongs to exactly one, so the weekly
table is the only intensity view whose coverage of the athlete's actual training is
guaranteed. `progress` already renders these weeks — `progression._week_meso` returns a null
label and `band_header` bands them `unplanned` — so the zone table inherits the fix by
reusing the band walk.

**Where the numbers come from: `weekly_aggregates`, not a second fetch.** `render_progress` is
handed one payload and reads no database, and that payload — `progression.assemble_timeline` —
carries load only. The zone rows join it where the load figures are already computed:
`weekly_aggregates` receives every activity row and already buckets them by week to sum
`activity_load`, so a week's zone rows are that same list passed to `intensity.zone_rows`. No new
query, no second fetch path, and the renderer stays pure and DB-free — which is the property the
one-payload rule exists to protect (`timeline.py`, CODE_REVIEW #5). `clip_payload_for_weeks`
returns weeks whole, so windowing needs no change. The chart endpoint then carries rows its PNG
ignores; that is the price of one payload and it is a few hundred floats — and `/api/zones`
turned out to want exactly those rows anyway.

The sport filter, the 10% floor and the currency choice are all computed over the **displayed**
window, which is what the header's share figure (`5h42 of 9h10 total`) means. One consequence to
expect rather than to fix: `--weeks all` can qualify a different set of sports than the default
does, and can draw one of them in a different currency, because both rules read the window they
are given.

This is not hypothetical loss. In the worked example above, the two weeks a regeneration
orphaned are `06-15` and `06-22`, and `06-22` is precisely the week Z3 doubled (35m → 1h02).
The block delta reports Z3 up 100% and cannot say when; the weekly table points at the week.

**The two grains slice time differently, and that is fine as long as it is written down.**
`rate_window` runs a block's weeks from the block's own start date, not calendar Mondays, so
blocks compare like for like (§4); the weekly table is Monday-aligned because a calendar week
is the thing that cannot move. So `Z3 1h02 in w/c 06-22` and `Z3 45m/wk in Base 2` are
averages over different seven-day spans and will not reconcile. Related: `_week_meso` labels
a week by majority overlap, so a week straddling two blocks sits under one band while its
earlier days counted into the other block's numbers. Neither is a defect and neither is worth
fixing — but the coach reads block grain (`coach/service/context.py`) while the athlete's
default screen is week grain, so the mismatch can surface inside one conversation, and the
next reader of this code will otherwise try to "fix" it.

**`--blocks` keeps the graded view.** Per-week rates over completed weeks, beside the block's
stated `focus`, with §4.1's block-over-block delta and the structural rows — `block_report`
handed a sport-filtered fetch. It replaces the weekly zone table rather than appending to it:
the flag is a choice of grain, not an extra section. Two grains, two questions: the week table
answers *when did it change*, the block table answers *did the block do what it said*. Only
the block has a stated intent to be graded against, which is why the weekly table carries no
verdict and no focus.

`--blocks` stays **single-sport**, defaulting to the first qualifying preference. N sports × M
blocks is not a view. And it trades brevity for grain rather than the other way round: at
phone width `_lay_out` fits one zone per line, so a single block with one sport and one
currency is about 25 lines, and three blocks is seventy-five. That is the opposite of what a
reader reaching for a coarser grain expects, so the help text says so.

**Width and length.** The HR zone table runs 38 columns, the 7-zone power table 48, the load
row 40 — all inside the 48-column budget, so Telegram and a TTY render identically, the §7.1
contract the load table already holds. The cost is vertical: `tm progress -z` over 8
weeks goes from ~23 lines to ~38 for one sport, and roughly 13 more per additional sport,
which is what the 10% volume floor exists to bound. That price is what put the tables behind
`-z` rather than on every invocation. `--weeks` windows them for anyone who wants it shorter.
The band rules render once per table; that is deliberate, since it is what lets the halves be
read row against row.

**The whole option surface**, three of them new:

| Option | | Effect |
|---|---|---|
| `[sport ...]` | new | Canonical sports to report intensity for, one table each in the order given. **Implies `-z`.** Default: every `sport_preferences` entry with zone data in the window and at least 10% of its duration, in config order, the rest named in the footer. Scopes the zone tables only — never the PMC, the projection or the load table. A name with no rows in the window lists the sports that have them. |
| `-z` / `--zones` | new | Show the weekly zone tables at all. Off by default: they roughly triple the output. Implied by naming a sport and by `--blocks`. |
| `--blocks` | new | Per mesocycle instead of per week, single-sport: rates over completed weeks, the stated `focus`, §4.1's delta, the current week, the structural rows. Replaces the weekly zone table; the load table stays. Longer than what it replaces, not shorter. |
| `--power` / `--hr` | new | Force the currency instead of choosing it by coverage. Mutually exclusive; no effect on a sport that has only one. |
| `--weeks N\|all` | | Windows the tables: `--weeks 8` shows 8 past *and* 8 future weeks, of load and (since §9.8) of zones alike. `--blocks` reports the mesocycles overlapping the *past* half — a future block has not started, and `block_report` returns nothing for it. Default 8. |
| `--explain` | | The PMC footnote (§7.1). Does not touch any table. |
| `--chart [PATH]` | | Unchanged, and **unaffected by `[sport]`**: the PNG's two panels are PMC and whole-athlete weekly load. A per-sport zone stack is `DESIGN_progress_timeline.md` §8 follow-on 3. The web app's *chart* is the same PNG and gains nothing; its read-only `/api/zones` view is a separate surface (see §10). |
| `--no-pull` / `--force-pull` | | The standard auto-ensure throttle, mutually exclusive. No effect on layout. |

**What the option sweep exposes**, all of it in `intensity.py` and `cli/progress.py`:

- **`block_report` must stop printing its own notes, which means it does change.** Today
  `format_notes` is called inside it, so three blocks render the same caveats three times —
  nine lines saying two things. They belong once per section, under the last block. That needs
  a `notes: bool = True` parameter, defaulting true so `cli/status.py` and
  `coach/service/context.py` are untouched, with `--blocks` passing `notes=False` and emitting
  once itself. The prompt path keeps its per-block notes deliberately: it sends one block.
- **`HR_REST_NOTE` has to be split in two, not re-keyed.** It reads "HR during strength *and
  interval-with-rest* work reflects rest intervals as much as effort", which is two claims
  with different scopes joined by an "and". Strength is a property of the sport and can be
  suppressed when that sport is not on screen; interval work with rest happens in running,
  cycling and rowing alike and must not be. Re-keying the existing note on "the sports present"
  does neither job — and after a sport-filtered fetch the rows contain only the selected
  sport anyway, so that predicate is already what the current code effectively tests. Split it:
  a strength note gated on the sports present, an interval note emitted wherever there is an
  HR row. Gating the first needs a strength-sport set in `sports.py`, which has no notion of
  sport categories today — small, but a new concept in a module that advertises itself as flat
  vocabulary, and a change to the coach's prompt text rather than a formatting tweak.
- **`--blocks` reproduces the very loss §9.6 exists to prevent, and by more than one route.**
  The mesocycles overlapping the window are reported; weeks belonging to none are silently
  absent — in the worked example `06-15` and `06-22` vanish, and `06-22` is where Z3 doubled.
  `rate_window` additionally excludes each block's partial tail from both sides of its
  division, correctly (§4) and invisibly, dropping up to six more days per block. So the block
  section must end naming both, and pointing at a view the flag has just replaced:

  ```
  2 weeks in this window belong to no block (06-15, 06-22), and each block's final
  partial week is excluded from its rate — run without --blocks for the weekly view
  ```

  At `--weeks all` that list is capped: `12 weeks … (06-15, 06-22, +10 more)`. Silence in
  either case would be the block-grained blindness this section was written about,
  reintroduced by the flag that opts into block grain.
- **`block_report`'s prose lines ignore `width`.** `format_header` and the `Change vs …`
  header are bare appends, measuring 57 and 84 characters at `width=48` — the 48-column
  contract this section claims is false under `--blocks` today. Both are prose; route them
  through `_wrap`. The zone *rows* must not be.
- **`run_progress` re-wraps any line over 48 columns**, which is exactly what `intensity.py`'s
  module docstring forbids ("wrapped once here and never re-wrapped downstream — a
  screen-width re-wrap would shred the columns"). **As shipped, only the `--blocks` section is
  printed outside that loop** — `block_report` lays one zone cell per line at phone width and
  genuinely can exceed 48. The weekly zone tables stay inside it, protected by width instead
  of by structure: every zone line is ≤48 columns *by construction* (`fmt_zone_cell`'s cap,
  the column arithmetic above, `_legend` wrapping the prose to `TABLE_WIDTH`), the wrap is a
  no-op on them, and the 48-column contract is pinned by a test. Keeping them in the list is
  what lets `render_progress` return one line list its callers can page or send. If a future
  cell ever widens past the budget, that test fails first — which is the guard the structural
  separation was asked for.
- **The zone tables' legend must carry the hidden-week count.** `format_weekly_table` names
  its truncation (`+N more (--weeks all)`); a zone table that quietly shows fewer weeks than
  exist would be silent about it in the half of the screen this whole section is about.
- **`* in progress` is explained under the load table**, fifteen lines above the zone row it
  also governs, and the partial current week sits inline with full weeks — the comparison
  `rate_window` explicitly refuses to make for blocks. The `*` needs repeating in each zone
  legend; the inline partial week is accepted, because the marker is what §4's argument asks
  for at weekly grain and a separate section for one week would cost more than it saves.

### 9.7 `data show-activities` — the raw view

§9.6 is a coaching view: aggregated to the week, scoped to a sport, one currency chosen for
the reader. `data show-activities` is the opposite and should stay that way — one row per
activity, both currencies, nothing chosen on the reader's behalf. It is also the only view
where a bad recording is *actionable*, because it names the session you would go and fix.

The constraint is width. The table already carries twelve columns
(`Date | Time | Type | Name | Duration | Distance | Elev | Avg HR | Max HR | Avg Watts | RPE |
TSS`) at roughly 140 characters, and five HR zones plus seven power zones cannot join them.
`render_table` collapses to one vertical record per activity on a narrow client, so on the bot
this is lines per activity rather than width — twelve becomes fourteen. Three parts, each
fitting its medium:

**The default table gains provenance, not zones.** The highest-value fact missing here is not
the breakdown, it is where the `TSS` number came from. Render it as a tag: `188 (pwr)`,
`142 (hr)`, `243 (rpe)`, `95 (sparse!)`, `210 (rpe+)`. About six characters, no new column, and
it is §9.6's `!` explained at source: every downstream confusion about the load half disagreeing
with the zone half traces back to a provenance that was invisible.

**The column shows the load, which means the figure changes.** Today it prints the stored `tss`,
which `measured_tss` defines as a pure measurement — no coverage gate, never RPE — while
`progress`, the PMC and every coaching path use `activity_load()`. So the two commands already
disagree about a session's load, silently, and a tag on the measurement would name a provenance
that is not the provenance of the number shown. The column and the summary total therefore move
to `activity_load()`. The view is informational and nothing is recorded from it, so the cost is
one changed figure on some rows and the benefit is that every surface finally quotes one number.

**Seven tags, because `activity_load` has paths `compute_load` has no word for.** Four are
`compute_load`'s methods (`pwr`, `hr`, `rpe`, `sparse!`); `rpe+` is the RPE-divergence override
(`rpe_divergence`), where the measurement was trustworthy but the athlete's RPE implied
materially more strain, so the load came from RPE anyway — the kettlebell case §6 exists for.
Without it those rows would read `(hr)` above a figure hrTSS never produced. The remaining two
close `load_method`'s return set rather than adding a claim: `tss` for a stored TSS with no
zone columns to attribute it to, and `—` for a session with no power, HR or RPE at all. One
tag per value `load_method` can return, so no row ever falls through to `?`.

The figure prints one decimal (`188.0 (pwr)`): `activity_load` returns a float and this is the
raw view, the one place a rounded figure would not reconcile with the CSV beside it.

**`--zones` swaps the columns rather than widening.** Distance, Elev, Avg HR, Max HR and Avg
Watts are not what the flag was reached for:

```
Date | Type | Duration | Cur | Z1 | Z2 | Z3 | Z4 | Z5 | Z6 | Z7 | Cov | TSS
```

One row per **activity × currency**, so a ride with both a meter and a strap renders two rows
tagged `[pwr]` and `[HR]` — `zone_rows`' existing model, and it keeps §6's prohibition
structural: the two views of the same time are separate rows, not adjacent columns inviting
addition. `NEVER_SUM_NOTE` goes in the footer. HR rows leave Z6/Z7 blank. `Cov` is what earns
the view its place: per-activity coverage turns "the strap dropped out somewhere this week"
into a named session.

**CSV gets everything, unconditionally.** `_show_activities_csv` has no width constraint and
its consumers want completeness, so all twelve zone-second columns, coverage and method go in
with no flag gating them.

**One adjacent defect, which §6.1 makes worse.** `--type` filters on exact lowercase equality
against the raw `activity_type`, so `data sa --type cycling` today misses `road_biking`,
`gravel_cycling`, `mountain_biking` and `indoor_cycling` — the athlete filters for their
cycling and sees a fraction of it. `sport_aliases()` exists for exactly this. The fix rides
with §6.1's canonical rename, since that is the change touching this vocabulary.

### 9.8 Planned zones — the future half of the table

§9.6 stopped the zone tables at today because nothing in the schema gave a planned workout an
intensity target. The coach already decides one — it writes `6×3min @ VO2max` — and it is the
only thing in the system that knows the intent. So it should emit the distribution as
structured data at authoring time.

**Not derived from `tss`.** A planned session has `duration_minutes` and `tss`, and
`tss ≈ duration × IF²` invites backing out an average intensity factor. That is §1 run
backwards: TSS is the projection that destroyed the distribution and it cannot be un-projected.
It is also circular — planned zones computed from planned TSS make planned-vs-measured zones a
restatement of planned-vs-measured TSS, which is the adherence percentage that already exists.

**The currency is per sport, and therefore per workout.** `SPORT_ANCHORS` (`benchmarks.py`)
already carries the mapping: cycling is tested on FTP, running on threshold pace and LTHR,
swimming on CSS. A triathlete's week is a bike session prescribed in power zones and a run
prescribed in HR zones, so this cannot be an athlete-level setting. But the sport alone is not
enough — a cyclist without a meter must be planned in HR whatever `SPORT_ANCHORS` says — so
**the planning currency is chosen by §9.6's rule**, the same one the display uses. One rule
applied twice: get it wrong in either place and the plan is written in a currency the table
never renders.

**§9.6's rule needs a window, and authoring has none, so it borrows the display default.** The
rule reads coverage over the window it is shown; at generation time there is no window, only
twelve weeks of forward plan. Take the trailing 8 weeks — the same span a bare `tm progress`
uses — so the ordinary case agrees by construction. When it does not agree the failure is
declared, not silent: the comparison is withheld and says why (below). The transient worth
naming is the athlete who has just bought a power meter, whose trailing coverage still says HR
while the display flips to power as the meter's weeks accumulate; the future half goes dark until
the next `workout generate` re-picks the currency from fresh coverage. Self-healing, on the same
rolling horizon that regenerates everything else.

```
planned_zone_currency   TEXT     -- 'hr' | 'power' | NULL
planned_zone1_sec … planned_zone7_sec
```

HR sessions fill 1–5 and leave 6–7 NULL, mirroring what `garmin/sync.py` already does on the
measured side. **Comparison is offered only when the planned currency matches the displayed
one**, and says so when it does not. Power Z6 (anaerobic) and Z7 (neuromuscular) have no HR
equivalent — that absence is `HR_LAG_NOTE`'s point restated — so collapsing seven onto five is
banding by the back door and §5 forbids it.

**The fields are declared the way every other field is: a prose-annotated JSON example in the
prompt** (`coach/engine/workouts.py`), exactly as `duration_minutes` and `tss` are today. That is
not in tension with §10's refusal to parse prose — §10 rejects reading intent back *out* of
`description` after the fact. Here the model states the distribution as JSON while it still knows
the intent, and the athlete-readable sentence is rendered back from the columns at display time.
A session whose zone seconds do not sum to `duration_minutes` is stored as emitted and rendered
as emitted: the numbers are a prescription, not an accounting identity, and silently scaling them
would put the app back in the business of correcting the model rather than aligning for it (§7).

**`adapt` emits them too.** §9.2 gives adapt the intensity factor of a scheduled session, and
§9.4's whole drift correction is a rewrite of how a session is prescribed. An adapt that left the
planned zones alone would leave them describing the prescription it has just replaced — the
future half of the table would grade the athlete against a target no longer on the page. So the
fields join the adapt response schema alongside the generate one.

**And that leaves §9.5's stopgap correct as written, which is worth saying out loud.** It decides
"was this an easing?" by comparing `duration_minutes` and `tss` only, so a drift correction that
rewrites the planned zones while holding both stays unstamped — right, because nothing was cut.
The rule predates this section; re-read with planned zones in the schema it still lands where it
should, and the next reader should not have to re-derive that.

**Back-compat needs nothing, and `data pull` could not help if it did.** A pull re-fetches
*completed activities* from Garmin; these columns sit on `workouts` and are authored by the coach,
so there is nothing upstream to fetch. Nor is there anything to backfill: the columns feed the
*future* half of the table only, and the rolling horizon rewrites the future on every generation.
Sessions planned before this ships render no ghost row until the next `workout generate`, which
is a gap of days and wants one note line, not a migration.

**Nothing has to be done about the calendar hash, and one thing must not be done.**
`CALENDAR_FIELDS` (`calendar_state.py`) is an allowlist, so new columns are excluded by
default; `rpe` is already there as deliberate precedent. But `description` *is* in the
allowlist, so the athlete-readable sentence — `Target: ~25min recovery, ~30min aerobic, ~10min
threshold, ~18min VO2max+` — must be **rendered from the columns at display time and never
stored**, or every regeneration that nudges a target by two minutes marks the row stale and
re-pushes the calendar event.

**Zone names, not indices.** `CURRENCIES` already carries them. `30 min aerobic` is what the
athlete can act on, and it survives the ruler shift below in a way `30 min Z2` does not; the
index is the join key, the name is the prescription.

**§7.1 stops being advice and becomes a dependency.** Garmin bucketed each activity using
Garmin's own FTP and lactate-threshold values, not TrainMate's logbook — so TrainMate does not
own the definition of Z2. For *measurement* §7.1 states that residual risk once and accepts
it, because a delta compares like with like. A *prescription* outlives the moment it was
written: with auto-detection left on, two sessions planned identically six months apart mean
different efforts, and neither athlete nor coach can see it. The README and `benchmark
record` note is therefore a prerequisite of this section, not a cross-reference.

**Some sports get no planned zones at all.** Swimming is anchored on CSS and strength on
e1RM; neither yields a zone model, and the columns stay NULL. §6's rule that no sport is
routed away governs the *measured* table — a kettlebell session's Z4 minutes are real work and
belong there. Prescribing HR zone targets for it would be inventing a distribution from a
session whose HR mostly reflects rest between sets: the fake precision §10 already refuses for
RPE bands, and the artifact the strength half of `HR_REST_NOTE` warns about.

What this unlocks is bigger than the comparison: the zone tables gain a future half, ghost
rows under today exactly like the load table's ghost bars, and §9.6's one asymmetry closes.

## 10. Deliberately not done

- **RPE or %1RM bands for strength.** Inventing a distribution from one per-session RPE number
  is fake precision. Revisit only if per-set data is ever logged.
- **A `movement` column on `benchmark_results`.** `e1rm` collides across lifts — the logbook
  has no per-exercise field and `latest_thresholds()` keys on `anchor_kind` alone, so a
  deadlift PR logged after a squat PR becomes one `e1rm` value jumping 70%. TrainMate does not
  plan progressive strength well enough yet to justify the schema. Two smaller fixes instead:
  exclude `e1rm` from the drift check in `config_changed()` (`coach/service/prompt.py`, the
  loop over anchor kinds — a squat PR should never invalidate a periodization), and note in
  `benchmark record`'s help that one lift should be tracked for now. The §6 mockup drops the
  exercise name accordingly.
- **Storing Garmin's zone boundaries per activity.** The airtight answer to §7.1, and a
  migration. Disabling auto-detection removes the cause at zero cost; revisit only if
  boundaries turn out to move anyway.
- **Efficiency at constant zone** — same Z2 minutes at a faster pace or higher watts is the
  richer progression signal, and `distance_km` / `bike_avg_watts` support it. But the stored
  values are whole-activity averages, not per-zone, so it is only clean for single-zone
  sessions. Phase 2.
- **Parsing planned interval structure out of `description`.** The plan already says "6×3min @
  VO2max" in prose, and prescribed intent beats the HR bucket for short intervals (§7). But
  free-text parsing of LLM-authored prose is the brittle route to a fact the LLM can simply
  state: §9.8 has it emit the distribution as structured data instead.
- **A per-sport zone stack in `--chart`.** The PNG's two panels stay PMC and whole-athlete
  weekly load, so `--chart` is unaffected by the sport argument.
  `DESIGN_progress_timeline.md` §8 follow-on 3. **Since revised for the web app**, which now
  serves a read-only `/api/zones` (per-sport time in zone, measured behind today and
  prescribed ahead, plus a stacked proportion bar per week in the Progress tab). It reuses
  `window_sport_stats`/`select_zone_sports`/`zone_currency`, so a sport the terminal omits is
  omitted there for the same reason — the rules live in `intensity.py` and neither surface
  owns a second copy. `--chart` itself is still untouched.

## 11. Housekeeping this lands on

> **Rev note (2026-08-04) — the per-sport bar was half unreachable.**
> `COVERAGE_MIN_BY_SPORT` was keyed on `resort_skiing` and `resort_snowboarding`,
> but `coverage_display_min` canonicalizes before it looks up, and `sports.py`
> folds both aliases into `downhill_skiing`. Neither key was ever hit:
> `coverage_display_min('resort_skiing')` returned the global 0.8, so every
> resort-ski week took a `!` — the exact over-firing the note below says it
> fixed. One entry now, `downhill_skiing: 0.15`, and a guard test asserts every
> key in the table is already canonical, because the failure is silent: a
> mis-keyed sport does not raise, it quietly reverts to the endurance bar.
>
> The floor gained the branch it was missing at the same time. `!` on a week
> where a sport was **trained but recorded nothing in this currency** read the
> sport's whole duration, so a single 5-minute unrecorded session lit the row
> while a 5-minute *recorded* one was correctly ignored. It now reads
> `judged_sport_seconds` — `sport_durations` over the judgeable sessions,
> computed in `weekly_aggregates` beside the unfiltered one — so both `!` paths
> answer to the same floor. `/api/zones`' `undercounted` flag moves with it;
> its `trained` flag does not, because "did they train" is a fact about the week
> and not a claim about the recording.

> **Rev note (2026-08-03) — the undercount markers get a sense of proportion.**
> Both markers shipped as bare thresholds and both fired on the majority of rows,
> which is the one thing an exception flag may not do. `?` lit 5 of 8 weeks, `!`
> lit 9 rows across 3 tables. Two fixes, one idea — *ask whether the evidence is
> big enough to carry the claim*:
>
> - **A duration floor** (`config.zone_min_activity_minutes`, default 20). Seven
>   of the ten `hr_sparse` sessions in the window were under 15 minutes: a
>   5-minute yoga worth 0.8 TSS, a 5-minute strength session worth 0.3. None of
>   them is evidence that a 344-TSS week is undercounted, and none is evidence
>   about the strap either — they are below the noise floor of both questions.
>   Sessions under the floor keep their load and their zone minutes everywhere;
>   they lose only their vote on the markers. 20 minutes sits just above this
>   athlete's 25th-percentile session length (16 min), so it excludes the
>   mobility/micro-session tail without touching a real workout. Implemented as
>   `intensity.judgeable`, read by `progression.weekly_aggregates` (for `?`) and
>   by `zone_rows`' new `judged_coverage` (for `!`).
> - **A per-sport bar** (`intensity.COVERAGE_MIN_BY_SPORT`, overridable via
>   `config.zone_coverage_display_min_by_sport`). The flat 0.8 was an
>   endurance-sport bar applied to every sport, and uncovered time is not always
>   a failed recording: it is also the rest between sets, the chairlift back up,
>   the gentle walking on a hike, the held pose. Those seconds sit below zone 1
>   where no zone claims them. Measured over six months of history — cycling and
>   ski touring hold ~0.95 median coverage, strength training 0.90 with a 0.49
>   lower quartile, resort skiing 0.26, hiking 0.15, yoga 0.05 — each bar is set
>   near its own sport's 25th percentile, so `!` marks the worst quarter of that
>   sport's weeks rather than every one. The code already knew this (the
>   `HR_STRENGTH_NOTE` exists to say so) and graded against 0.8 anyway.
>
> A third state falls out: a sport-week where *nothing* cleared the floor has an
> **unanswerable** coverage (`judged_coverage is None`) and takes no marker.
> Unanswerable is not the same as failed. Result on real data: `?` 5 → 3 weeks,
> `!` 9 → 4 rows, each survivor a genuine hole (a 3h16 hike at 12% coverage
> valued at 8 TSS).
>
> `hr_zone_coverage_min` is deliberately untouched: it decides whether a stored
> TSS is trusted, so a per-sport version of *that* would change the load values
> and the PMC series, not a marker.

- **ARCHITECTURE.md** gains the aggregation helper, the `cycling` canonical rename and §9.8's
  planned-zone columns.
- **Tests** (`unittest`, `venv/bin/python -m unittest discover -s tests -p "test_*.py"`).
  The `road_biking` sweep is done; the files that still spell it use it as an alias-folding
  fixture and should keep it. Worth their own: the
  completed-weeks divisor at a block's first 6 days and across a partial tail (§4); the
  coverage formula with a meterless ride in the set (§7); the §9.5 stopgap, specifically a
  description-only edit leaving `adaptation_count` untouched; `show-activities` quoting the
  same load as `progress` for an RPE-divergent session, tagged `rpe+` (§9.7).
- **README.md** and `benchmark record`'s help gain the Garmin auto-detection note (§7.1) —
  which §9.8 promotes from advice to a prerequisite.
- **Two defects `progress` already has**, both surfaced by §9.6 and neither caused by it: the
  `hr_sparse` week that reads as an adherence miss when the strap died and no RPE was entered
  (§9.6, `_measurement_is_load` already implements the test), and `data show-activities`'
  `--type` filtering on exact `activity_type` equality so `--type cycling` misses every alias
  (§9.7, `sport_aliases()` is the fix, rides with §6.1).
- **Config validation.** `sport_preferences` entries that are not canonical sports warn where
  the list is consumed (§9.6) — a warning, not an error, since `SPORT_MAPPING` has no
  `swimming` entry, and not in `Config.__init__`, which runs in every process.
- **`DESIGN_progress_timeline.md`** §8 follow-on 3 (the zone-distribution stack) is where
  §9.6 lands; its chart half stays open and inherits §9.6's one-sport, one-currency rules.
  Worth their own tests: `!` at the display coverage threshold, distinct from
  `hr_zone_coverage_min`; a week whose chosen currency has no data; a sport not trained in a
  week rendering `—` rather than zeros; `w/c 07-06*!` fitting the week column; the 7-zone
  power table at exactly 48 columns with a 10h+ Z2; an orphaned week appearing in the weekly
  table and in no block; `--blocks` naming both the orphaned weeks and the excluded partial
  tails; `tm progress weeks 4` still reaching the dashless translator past the new positional.
