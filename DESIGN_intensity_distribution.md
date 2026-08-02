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
`data pull --from/--until` re-pulls any window while `save_completed_activity` upserts every
zone column explicitly, so adding the alias and re-pulling that range fills the data back in.
Nothing is lost permanently, which is what would otherwise have argued for keeping the
permissive substring net.

**Canonicalize on read, not on write.** `completed_activities.activity_type` keeps Garmin's
raw string; every read path goes through `canonical_sport()`. AGENTS.md asks for exact values
in storage with reduction only on display, and overwriting `gravel_cycling` with `cycling`
in the column is a lossy write undoable only by a re-pull. Read-time normalization gives the
same single vocabulary — it is already how planned workouts match activities — without
discarding the original.

The rename touches six code sites that spell `road_biking` out: the generate and adapt LLM
response schemas (`coach/engine/workouts.py:98`, `:355`), two CLI `choices` lists
(`cli/goals.py:132`, `:147`), the learnings-schema example (`coach/engine/__init__.py:29`),
and `workout add`'s help text (`cli/workouts/parser.py:178`). `SPORT_ANCHORS`
(`benchmarks.py`) already carries both `road_biking` and `cycling` keys. Stored
`workouts.sport_type` rows keep matching through the alias, but a one-off
`UPDATE workouts SET sport_type='cycling' WHERE sport_type='road_biking'` stops the plan and
the report disagreeing on screen. Thirteen files under `tests/` mention `road_biking`.

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
- **`tm progress [sport]` — the trend (§9.6).** *Where did it change?* One sport, weekly
  grain, the whole displayed window. The only view that survives a mesocycle boundary moving.

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

### 9.6 `tm progress [sport]` — one sport, weekly grain

`tm progress` (`DESIGN_progress_timeline.md`) is the fullest rendering anywhere of the
picture §1 calls a lie: a CTL sparkline, a weekly TSS column, an adherence percentage and a
forward projection, every one of them computed from load alone. An athlete whose easy days
have drifted to tempo reads that screen as seven flat weeks at 97–101% adherence. So this is
where the correction belongs, not only in `status`.

**A positional sport, defaulting to `athlete.sport_preferences[0]`.** Not a rendering
preference — it is what makes a weekly grain possible at all. §4 forbids merging sports and
§6 forbids adding the HR and power rows of one sport; together those make a week's honest
distribution three or four rows of five-to-seven named cells, which is a screenful per week.
Fix the sport and the whole week collapses to one line. The argument buys the grain.

**It scopes the intensity content only.** CTL, ATL, TSB, the projection and the WEEKLY LOAD
table stay whole-athlete. A running-only CTL is not a quantity — the fitness model integrates
every session the body paid for — and adherence is measured against the whole plan. `tm
progress cycling` therefore shows whole-athlete form beside cycling-only intensity, and the
help text must say so, because the command shape invites the opposite reading. (An optional
argument with a default is `--sport`'s case by the convention in `cli/`; positional is the
call taken, for `tm progress cycling` over `tm progress --sport cycling`.)

**The default view is the per-zone table itself — every zone, no rollup, no flag.** An earlier
draft put a single `easy` column (the selected sport's Z1-2 share) beside `adh` and left the
zone table behind `--zones`. That column existed only because a merged-sport table could not
fit a row, and the sport argument removed that constraint; keeping it would have left §5's
argument — no banding, every zone stands alone, because every boundary the grouping erases
carries a coaching decision — contradicted by the one view an athlete looks at daily. It also
read worse. `easy 86% → 68%` says *something moved*; `Z2 5h00 → 4h00` beside `Z3 35m → 1h10`
says a fifth of the aerobic base was traded for tempo, which is the sentence the feature
exists to produce. The rollup stays derivable for any consumer that wants it (§5); nothing
renders it here.

Two stacked tables, sharing week labels and band rules so they scan as one unit — the load
half, then the intensity half:

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
* in progress · plan ends 08-05 (Wed)

ZONES running [HR] — minutes per week
week          Z1   Z2   Z3   Z4   Z5
── Base 1 — Aerobic Volume Accumulation ────────
w/c 05-25    50m 5h00  35m  15m   5m
w/c 06-08    48m 5h02  36m  16m   5m
── unplanned ───────────────────────────────────
w/c 06-15    28m 2h26  20m   8m   4m ~
w/c 06-22    55m 4h18 1h02  17m   7m
── Base 2 — Aerobic Volume Consolidation ───────
w/c 06-29    55m 4h00 1h10  17m   7m
w/c 07-06*   22m 1h22  38m   7m   3m
Z1 recovery · Z2 aerobic · Z3 tempo · Z4 threshold · Z5 VO2max+ · ~ low zone coverage
```

The zone table covers past and in-progress weeks only — the future half of the load table is
planned TSS, and nothing in the schema gives a planned workout an intensity target to put
under it (§10, phase 2). The load table keeps its ghost bars; the zone table simply stops at
today.

**Coverage is a marker, not a column.** `~` on any week whose zone coverage falls under
`hr_zone_coverage_min`, named in the legend. Two reasons it cannot stay a column. It does not
fit: 11 + 7×5 + 4 overruns the 48-column budget for the power table, and a per-week figure
reading `95%` on nine rows out of ten spends four columns to say nothing. And with the `easy`
column gone, the coverage guard that used to blank it has nowhere else to live — this is the
week the strap died, and `Z2 2h26` against the neighbouring `5h00` reads as an athlete who
stopped training rather than as a week that was not recorded. The marker is the whole defence
against that misreading, so it is not optional.

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

This is not hypothetical loss. In the worked example above, the two weeks a regeneration
orphaned are `06-15` and `06-22`, and `06-22` is precisely the week Z3 doubled (35m → 1h02).
The block delta reports Z3 up 100% and cannot say when; the weekly table points at the week.

**`--blocks` keeps the graded view.** Per-week rates over completed weeks, beside the block's
stated `focus`, with §4.1's block-over-block delta and the structural rows — `block_report`
unchanged but handed a sport-filtered fetch. It replaces the weekly zone table rather than
appending to it: the flag is a choice of grain, not an extra section. Two grains, two
questions: the week table answers *when did it change*, the block table answers *did the block
do what it said*. Only the block has a stated intent to be graded against, which is why the
weekly table carries no verdict and no focus.

**One currency for the whole table, chosen once.** Power where any displayed week recorded a
meter, HR otherwise (§7 prefers power; it is instantaneous). Chosen over the window and not
per row, because a column that switched currency mid-table would be adding HR minutes to
power minutes down the page — §6's one prohibition, committed vertically instead of
horizontally. A week with no data in the chosen currency renders `(no power recorded)` rather
than falling back to the other one.

**Width and length.** The HR zone table runs 36 columns, the 7-zone power table 46, the load
row 40 — all inside the 48-column budget, so Telegram and a TTY render identically, the §7.1
contract the load table already holds. The cost is vertical: a default `tm progress` over 8
weeks goes from ~23 lines to ~38. That is the price of the feature and it is paid on every
invocation, which is the point — intensity drift is the failure an athlete cannot know to ask
about. `--weeks` already windows both tables together for anyone who wants it shorter. The
band rules render twice, once per table; that is deliberate, since it is what lets the two
halves be read row against row.

**The whole option surface**, two of them new:

| Option | | Effect |
|---|---|---|
| `[sport]` | new | Canonical sport to report intensity for. Default `athlete.sport_preferences[0]`. Scopes the zone tables only — never the PMC, the projection or the load table. An unrecognised value lists the canonical sports present in the window rather than rendering an empty table. |
| `--blocks` | new | Per mesocycle instead of per week: rates over completed weeks, the stated `focus`, §4.1's delta, the current week, the structural rows. Replaces the weekly zone table; the load table stays. |
| `--weeks N\|all` | | Windows *both* tables together, and the block set with them — `--blocks` reports the mesocycles overlapping the displayed weeks. Default 8. |
| `--explain` | | The PMC footnote (§7.1). Does not touch either table. |
| `--chart [PATH]` | | Unchanged, and **unaffected by `[sport]`**: the PNG's two panels are PMC and whole-athlete weekly load. A per-sport zone stack is `DESIGN_progress_timeline.md` §8 follow-on 3. |
| `--no-pull` / `--force-pull` | | The standard auto-ensure throttle, mutually exclusive. No effect on layout. |

**Two things the option sweep exposes**, both small and both in `intensity.py`:

- `format_notes` emits `HR_REST_NOTE` — the caveat about rest intervals inside strength and
  interval work — for any table containing an HR row. On the merged table that was right. On
  `tm progress running` it is a note about a sport not on screen. It should key on the sports
  actually present. Worse under `--blocks`, where the notes repeat per block: three blocks
  render the same two caveats three times, nine lines saying two things. They belong once per
  section, under the last block, not once per `block_report`.
- **`--blocks` reproduces the very loss §9.6 exists to prevent.** The mesocycles overlapping
  the window are reported; weeks belonging to none are silently absent — in the worked example
  `06-15` and `06-22` vanish, and `06-22` is where Z3 doubled. So the block section must end
  with a line naming them: `2 weeks in this window belong to no block (06-15, 06-22) — see
  the weekly view`. Silence there would be the block-grained blindness this section was
  written about, reintroduced by the flag that opts into block grain.

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
- **Cross-referencing planned interval structure against measured zone time.** The plan already
  says "6×3min @ VO2max", and prescribed intent beats the HR bucket for short intervals (§7).
  Stronger than either alone, but a new data path. Phase 2.

## 11. Housekeeping this lands on

- **ARCHITECTURE.md** gains the aggregation helper and the `cycling` canonical rename.
- **Tests** (`unittest`, `venv/bin/python -m unittest discover -s tests -p "test_*.py"`).
  Thirteen files under `tests/` spell `road_biking` and move with §6.1. Worth their own: the
  completed-weeks divisor at a block's first 6 days and across a partial tail (§4); the
  coverage formula with a meterless ride in the set (§7); the §9.5 stopgap, specifically a
  description-only edit leaving `adaptation_count` untouched.
- **README.md** and `benchmark record`'s help gain the Garmin auto-detection note (§7.1).
- **`DESIGN_progress_timeline.md`** §8 follow-on 3 (the zone-distribution stack) is where
  §9.6 lands; its chart half stays open and inherits §9.6's one-sport, one-currency rules.
  Worth their own tests: the `~` marker at `hr_zone_coverage_min`; a week whose
  chosen currency has no data; an orphaned week appearing in the weekly table and in no block.
