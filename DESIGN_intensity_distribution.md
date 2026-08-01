# Mesocycle intensity distribution

**Status:** Draft · **Date:** 2026-08-01

## 1. The problem

**TSS is intensity-blind by construction.** 500 TSS is 8 hours easy or 4 hours hard, and
CTL/ATL/TSB cannot separate them. Neither can the coach. Time-in-zone is the only signal
already in the database that recovers the distinction.

The failure mode this leaves open is **intensity creep**: easy days drift to moderate across a
base block. Weekly TSS stays flat, CTL stays flat, the PMC reads healthy — and the athlete
stagnates. Nothing in the current pipeline can see it:

| Running, per week | Base 1 | Base 2 |        Δ |
|-------------------|--------|--------|----------|
| Z1 recovery       |    50m |    55m |     +10% |
| Z2 aerobic        |   5h00 |   4h10 |     -17% |
| Z3 tempo          |    35m |   1h20 |    +129% |
| Z4 threshold      |    15m |    18m |     +20% |
| Z5 VO2max+        |     5m |     7m |     +40% |
| **TSS/wk**        |    340 |    355 | **+4%** |

The bottom row is what the coach sees today. The rows above it are what happened.

## 2. What already exists

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

## 3. Grain

**(mesocycle × canonical sport × zone)**, with `sports.py::canonical_sport` folding aliases.

Per sport is not optional. Garmin applies different HR zone boundaries per sport, so running
Z4 and cycling Z4 are different physiological places; power zones exist only for cycling, so a
merged table mixes a 7-zone Coggan model with a 5-zone Friel model. And the merged view hides
the finding: *"85% easy overall"* is unactionable next to *"running was 89% easy, but every
hard minute in the block went into cycling"*.

**Rates, not totals.** Blocks are unequal length and the current one is always partial. Emit
per-week figures alongside `weeks_elapsed`, so a partial block reads as partial
(`Build 1 (2 of 4 weeks elapsed)`).

Both minutes and percentages. Percentages answer "was this block polarized"; minutes answer
"did easy volume actually go up". A block can hold 85% easy in both of two blocks while easy
volume falls 20%.

## 4. Zones: every zone reported separately

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
- **HR's ceiling becomes self-evident.** HR stops at one "above threshold" bucket while power
  resolves three. Shown side by side, that is the case for preferring power on the bike (§6),
  demonstrated rather than asserted.

Reporting every zone is also the *simpler* implementation. §7 sums per-zone seconds either
way, so banding is additional code whose only effect is to discard information. Cost of
keeping it all: 5 HR + 7 power figures per sport per block instead of 4 + 5.

The zone **labels** above travel with every figure — bare `Z1..Z5` is markedly less legible to
a model than named zones. That vocabulary is what the banding discussion was actually worth.

The polarized `Z1-2 / Z3 / Z4-5` rollup stays derivable at render time for any view that wants
it, including the existing weekly summaries, which are left alone. It is a lossy view
regardless: LT1 falls somewhere inside Garmin Z2–Z3, so the 3-band grouping never mapped
cleanly onto the polarized model to begin with.

## 5. Non-endurance sports: a second currency, not a forced fit

Zones are a cardiovascular construct. A lifting session's HR profile reflects **rest
intervals**, not training intent — three sets of heavy squats can register as Z2. The codebase
already draws this line: `garmin/load.py::compute_load` falls through to Foster sRPE for
exactly this case, and `load_method` records which currency was used.

So the block summary carries two sections, honest about units:

```
Base 2 (4 weeks) — focus "aerobic volume"
  Endurance intensity distribution, per week
    running [HR]      Z1 55m (8%)   Z2 4h10 (58%)  Z3 1h20 (18%)  Z4 48m (11%)  Z5 11m (3%)
    road_biking [pwr] Z1 22m (13%)  Z2 1h50 (63%)  Z3  25m (14%)  Z4 12m (7%)   Z5 4m (2%)
                      Z6 1m (1%)    Z7 0m (0%)
  Structural work
    strength_training  11 sessions, 8h15, avg RPE 7.2, 410 sRPE load
    e1RM (back squat)  102kg -> 108kg (+5.9%)
  Coverage: 94% of endurance minutes fell inside a recorded HR zone
```

The strength progression signal comes from the **benchmark logbook** (`benchmarks.py`, `e1rm`
anchor kind), which is a better measure than any zone proxy would have been.

## 6. Measurement caveats are emitted, not corrected

The app aligns; the LLM reasons (`DESIGN_quantitative_context_impact.md`). Three artifacts
would mislead a model reading the numbers naively, so each is stated as a fact beside them:

- **Coverage.** `config.hr_zone_coverage_min` exists because low HR coverage means the effort
  sat *below* Z1. A base block of genuinely easy sessions can show few zone minutes, and a
  model reading that will prescribe more aerobic volume. Emit the coverage fraction per block
  and sport so absence reads as "not recorded", not "not done" — the same reasoning as the
  existing `power_zone_distribution_sec: None` guard.
- **HR lag under-reports Z5 on short intervals.** HR needs 60–90s to climb, so a 30/30 VO2max
  session banks most of its seconds in Z4. Measured by HR, a genuine VO2max block *will* look
  like a threshold block. Two consequences: for cycling the power table is preferred (power is
  instantaneous), and a one-line note travels with any HR-derived Z5 figure.
- **Threshold drift breaks cross-block comparison.** Garmin bucketed each activity using the
  zones in force *at the time*. An FTP or LTHR bump moves the Z4/Z5 boundary, so the same
  effort lands one zone lower and a block looks like it got easier when nothing changed.
  Per-zone reporting surfaces this as a real-looking swing where merged bands hid it, and it
  now applies at every boundary rather than two. Each block is therefore annotated with the
  dated anchor in force from the benchmark logbook, and a move mid-comparison is flagged.
  **This ships with §4, not after it.**

No app-coded verdict on whether a block matched its focus. The block's free-text `focus` is
emitted beside the measured distribution and the model judges — the same split as the existing
"planned vs actual" section, which this replaces.

## 7. Storage: computed on read

A sum over rows already stored. Blocks are ≤6 weeks; no new table, no migration, no Garmin
calls, no LLM calls. The aggregation currently inline in `_build_prior_training_context` is
extracted into a reusable helper and called for the current block and the preceding one.

## 8. Where it surfaces

- **`workout adapt` prompt — primary.** Drift caught in week 2 of a block is correctable;
  drift diagnosed at plan-generation time is history.
- **Strategy prompt.** Replaces the inline computation in `_build_prior_training_context`,
  now per-sport and resolved.
- **CLI.** Rendered in the block summary display.

This also closes the loop with `DESIGN_evidence_based_confidence.md`: the coach prescribes a
concrete distribution ("hold Z2 near 4h30/wk, add 20 min Z4"), and the next block's measured
table grades it.

## 9. Deliberately not done

- **RPE or %1RM bands for strength.** Inventing a distribution from one per-session RPE number
  is fake precision. Revisit only if per-set data is ever logged.
- **Efficiency at constant zone** — same Z2 minutes at a faster pace or higher watts is the
  richer progression signal, and `distance_km` / `bike_avg_watts` support it. But the stored
  values are whole-activity averages, not per-zone, so it is only clean for single-zone
  sessions. Phase 2.
- **Cross-referencing planned interval structure against measured zone time.** The plan already
  says "6×3min @ VO2max", and prescribed intent beats the HR bucket for short intervals (§6).
  Stronger than either alone, but a new data path. Phase 2.
- **Summing the HR and power tables.** A cycling session with a power meter appears in both.
  Two views of the same time, never a total.
