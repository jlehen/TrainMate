# Mesocycle intensity distribution

**Status:** Draft · **Date:** 2026-08-02

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

## 3. What already exists

Zone-seconds are already summed in two places, so this design is mostly consolidation:

- **Per elapsed mesocycle** — `coach/service/context.py::_build_prior_training_context` emits
  HR `Z1-2/Z3/Z4-5` and power `Z1-2/Z3-4/Z5-7` minutes per block, for the strategy prompt.
- **Per week** — `coach/service/analysis.py` puts `zone_distribution_sec` and
  `power_zone_distribution_sec` into every weekly summary fed to the analysis LLM.

Three gaps, and they are the whole of this design:

1. **Not per sport.** Running Z4 and cycling Z4 are summed into one number.
2. **Prior plan only.** The block-grained view covers the *previous* macrocycle. The block the
   athlete is currently in is never summarized, so drift is diagnosed after it is fixable.
3. **No delta.** Absolute minutes describe; block-over-block change is what carries signal.

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
per-week figures alongside `weeks_elapsed`, so a partial block reads as partial
(`Build 1 (2 of 4 weeks elapsed)`).

Both minutes and percentages. Percentages answer "was this block polarized"; minutes answer
"did easy volume actually go up". A block can hold 85% easy in both of two blocks while easy
volume falls 20%.

**Percentages are of recorded zone-seconds**, not of session duration — so they sum to 100 and
the coverage line (§7) carries the recording gap on its own. The two differ by exactly that
gap, and the gap is not spread evenly: it sits entirely below Z1.

### 4.1 The delta

Gap 3 above is the reason for the feature, so it needs specifying rather than implying:

- The **current block to date** against the **immediately preceding block**, both as per-week
  rates. Comparing a partial rate to a complete rate is the only sane pairing, and rates are
  what make it legitimate.
- The preceding block may live in the **prior macrocycle**. `db/periodization.py` has
  `get_next_mesocycle` but no previous-block accessor, so one is added — ordered by date
  across macrocycle boundaries, not scoped within one.
- A block whose sport mix differs materially from its predecessor gets the delta suppressed
  per-sport rather than globally: a sport absent from one side is reported as absent, never as
  a -100% swing.
- Deltas spanning the Garmin zone-freeze date (§7.1) are suppressed with a stated reason.

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
Base 2 (4 weeks) — focus "aerobic volume"
  Intensity distribution, per week
    running           [HR]  Z1 55m (13%)  Z2 3h39 (53%)  Z3 1h20 (19%)  Z4 48m (12%)  Z5 11m (3%)
    road_biking       [pwr] Z1 22m (13%)  Z2 1h50 (63%)  Z3 25m (14%)   Z4 12m (7%)   Z5 4m (2%)
                            Z6 1m (1%)    Z7 0m (0%)
    road_biking       [HR]  Z1 30m (12%)  Z2 2h40 (64%)  Z3 40m (16%)   Z4 16m (6%)   Z5 5m (2%)
    strength_training [HR]  Z1 12m (9%)   Z2 74m (55%)   Z3 26m (19%)   Z4 22m (16%)  Z5 1m (1%)
    Coverage: running 94% HR · road_biking 96% HR, 69% power · strength_training 88% HR
    Note: HR during strength and interval-with-rest work reflects rest intervals as
    much as effort — read these rows beside the session RPE below.
  Structural work
    strength_training  11 sessions, 8h15, avg RPE 7.2, 410 sRPE load
    e1RM               102kg -> 108kg (+5.9%)
```

Three things that table does that the routed version could not: the kettlebell sessions' 22 min
of Z4 are counted; heavy-lifting Z2 minutes stay visible but annotated, so a model can discount
them against the RPE instead of never seeing them; and the bike appears under both currencies
on purpose — the HR row spans 4h11 of riding and the power row 2h54, so 69% of bike time had a
meter. Rides without a meter stay visible in the HR row instead of vanishing from a power-only
view, and that gap is the coverage line's job to state, not the table's to hide.

**Never sum the HR and power tables.** A ride with a power meter appears in both; they are two
views of the same time, never a total. The per-row percentages invite exactly that mistake, so
the rule belongs here and not in §10.

### 6.1 One canonical sport mapping

Two vocabularies currently disagree. `SPORT_MAPPING` (`sports.py`) has no `gravel_cycling` or
`cyclocross`; `CYCLING_TERMS` (`garmin/load.py`) has both. So a gravel ride *does* get its
power zones fetched, then falls through `canonical_sport` unchanged into a `gravel_cycling`
row of its own — one athlete's cycling split across two rows, each looking like less volume
than it is. That is a live bug today, independent of this feature.

Consolidate to a single table in `sports.py`, one row per canonical sport, carrying the alias
list; `CYCLING_TERMS` becomes a derived view of it. **No currency or "HR is ambiguous here"
flags** — under the no-routing rule nothing would read them, and the annotation above is one
static footnote rather than per-sport configuration.

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
bucketed under whatever zones were in force then, which is why §4.1 suppresses deltas that
straddle the freeze date. Manually-edited Garmin zones remain invisible; that is accepted.

No app-coded verdict on whether a block matched its focus. The block's free-text `focus` is
emitted beside the measured distribution and the model judges — the same split as the existing
"planned vs actual" section, which this replaces.

## 8. Storage: computed on read

A sum over rows already stored. No new table, no migration, no Garmin calls, no LLM calls. The
aggregation currently inline in `_build_prior_training_context` is extracted into a reusable
helper and called for the current block and the preceding one.

One accessor detail, since the current block is new territory: `get_active_mesocycle` falls
back to the next *future* block, then to the absolute first one, when today sits inside none.
Called naively that renders `Build 1 (0 of 4 weeks elapsed)` with an empty table for a block
that has not started. The helper takes the block it is given and reports nothing when today is
outside every block.

## 9. Where it surfaces

- **`workout adapt` — primary.** Drift caught in week 2 of a block is correctable; drift
  diagnosed at plan-generation time is history.
- **Strategy prompt.** Replaces the inline computation in `_build_prior_training_context`,
  now per-sport and resolved.
- **CLI.** Rendered in the block summary display.

This also closes the loop with `DESIGN_evidence_based_confidence.md`: the coach prescribes a
concrete distribution ("hold Z2 near 4h30/wk, add 20 min Z4"), and the next block's measured
table grades it.

### 9.1 Why `adapt` does nothing with this today

`DESIGN_block_boundary.md` §2 constrains `adapt` in two ways that must not be confused. The
**range boundary** — adapt's writes stop at the mesocycle end, and the next block is out of
reach — is mechanized in code and is what that document calls a firewall. It is irrelevant
here: intensity correction happens entirely inside the current block. The **mandate** — adapt
is tactical, eases transiently, does not reshape periodization — is prompt text plus one tag
string, and it is the part in play.

The obstacle is narrower than "the mandate forbids it". It is a missing branch. The adapt TASK
(`coach/engine/workouts.py`) offers exactly three:

```
- If they are showing high fatigue or injury risk ... replace hard workouts with
  recovery or rest.
- If they have missed key workouts, adjust the remaining workouts ...
- If they are fully recovered and on track, keep the plan as scheduled or make
  minor optimal adjustments.
```

Fatigue, absence, fine. An athlete three weeks into running their easy days at Z3 has normal
RHR, normal HRV, TSB -5 and a perfect attendance record. They fall into branch three and the
plan is kept as scheduled — with the drift table sitting unused in the prompt. The model is
additionally told to return only changed sessions, so silence is the compliant answer.

**Every path in that TASK treats adaptation as a response to fatigue or absence. Intensity
drift is neither: the athlete showed up for everything and feels fine.**

### 9.2 The line: adapt owns execution, generate owns periodization

> Changing what zone Tuesday's run is prescribed at is **execution** — adapt's call.
> Changing how many hard sessions the block contains is **periodization** — not adapt's call.

Stated in §2's vocabulary: adapt may move the intensity factor of a scheduled session. It may
not change the block's composition. This resolves the apparent conflict with
`DESIGN_block_boundary.md` without loosening anything about fatigue-driven cuts, and it gives
"do not reshape the mesocycle" a definition it currently lacks.

### 9.3 Prompt changes

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
and zone, beside the block's stated focus. When the two disagree, that is an
execution error, not a fatigue signal — and it is yours to fix.

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

### 9.4 The one collision with existing machinery

A drift correction saves through `save_workout`, which stamps `adapted_at` and bumps
`adaptation_count` unconditionally (`db/workouts.py`). The session then carries
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
feature does not block on it. Interim stopgap: do not stamp `adapted_at` when duration and TSS
are both unchanged.

## 10. Deliberately not done

- **RPE or %1RM bands for strength.** Inventing a distribution from one per-session RPE number
  is fake precision. Revisit only if per-set data is ever logged.
- **A `movement` column on `benchmark_results`.** `e1rm` collides across lifts — the logbook
  has no per-exercise field and `latest_thresholds()` keys on `anchor_kind` alone, so a
  deadlift PR logged after a squat PR becomes one `e1rm` value jumping 70%. TrainMate does not
  plan progressive strength well enough yet to justify the schema. Two smaller fixes instead:
  exclude `e1rm` from `config_changed()`'s drift check (a squat PR should never invalidate a
  periodization), and note in `benchmark record`'s help that one lift should be tracked for
  now. The §6 mockup drops the exercise name accordingly.
- **Storing Garmin's zone boundaries per activity.** The airtight answer to §7.1, and a
  migration. Disabling auto-detection removes the cause at zero cost; revisit only if
  boundaries turn out to move anyway.
- **Efficiency at constant zone** — same Z2 minutes at a faster pace or higher watts is the
  richer progression signal, and `distance_km` / `bike_avg_watts` support it. But the stored
  values are whole-activity averages, not per-zone, so it is only clean for single-zone
  sessions. Phase 2.
- **Cross-referencing planned interval structure against measured zone time.** The plan already
  says "6×3min @ VO2max", and prescribed intent beats the HR bucket for short intervals (§7).
  Stronger than either alone, but a new data path. Phase 2.
