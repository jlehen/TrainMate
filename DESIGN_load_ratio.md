# Retiring ACWR: the ATL:CTL load ratio

## 1. The problem

Two problems, one root cause.

**(a) ACWR fought block periodization.** `periodization.txt` §2B offers a Block style
whose mesocycles change by "a steep, abrupt vertical step function". `training_load.txt`
told the coach to keep ACWR in 0.8–1.3, to trim planned volume above 1.3, and to
"rebuild load gradually" below 0.8. Those instructions are incompatible: block
periodization is *made of* deliberate steps, and ACWR flags every step as a defect.

The bite was not where it looked. A sustained volume step up barely moves ACWR, because
the chronic window catches up behind it — a +40% step off a 400 TSS/week base peaks at
1.27 and falls from there. The damage was on the *down*-steps:

| Phase (from a 560 TSS/wk accumulation block) | Acute | Chronic | ACWR | Old file said |
|---|---|---|---|---|
| Transmutation wk1 (volume −40%) | 340 | 505 | 0.67 | "Under-training. Fitness declining." |
| Transmutation wk2 | 340 | 450 | 0.76 | still under-training |
| Realization / taper | 200 | 305 | 0.66 | "Under-training" — during the taper |

Two of three transmutation weeks were flagged as under-training, with a standing
directive to add load back into the one block whose whole mechanism is staying
concentrated and low-volume — and the same again during the race taper.

**(b) ACWR and TSB contradicted each other inside one file.** §1 called ACWR 1.3–1.5 a
"Danger Zone" while §2 called TSB −10 to −30 "productive overload, normal and desirable
inside a build block". For a CTL-100 athlete those describe the same state. §3 then said
to "treat EITHER crossing its red line as a caution", which makes the stricter model
always win — a one-way ratchet toward flattening the plan.

Structurally, ACWR won regardless of which model was better: it was a live number in the
daily prompt context backed by imperative directives ("Reduce load immediately"), while
block periodization was one static paragraph. A concrete number with an imperative beats
an abstract philosophy. TrainMate could label a macrocycle "Block" and silently execute a
linear ramp.

## 2. Why not just drop ACWR

Because it carried one thing CTL/ATL/TSB genuinely do not: **it is a ratio, and TSB is a
difference.**

TSB = CTL − ATL, in absolute load units, so a fixed band means different things at
different fitness levels. What ATL must reach for TSB to hit −30:

| CTL | ATL for TSB −30 | implied fatigue:fitness |
|---|---|---|
| 15 | 45 | 3.0 |
| 30 | 60 | 2.0 |
| 60 | 90 | 1.5 |
| 100 | 130 | 1.3 |

The "insert recovery" trigger fires at a 1.3× overload for a well-trained athlete and
only at 3.0× for someone back from a layoff — permissive exactly where the athlete is
most fragile. Concretely: back from a layoff at CTL 15, one 250 TSS week puts ATL near 32.
TSB reads −17 ("productive overload, normal") while the ratio reads 2.1. TSB ranks that
athlete as *safer* than a CTL-100 athlete at ATL 125 (TSB −25, ratio 1.25). The ratio is
right; TSB is wrong.

The same absolute-band flaw applies to the CTL ramp bands (+5/week is ~5%/week at CTL 100
but ~25%/week at CTL 20), so before this change every stored guardrail except ACWR was
mis-calibrated for a low-CTL athlete — and ACWR was the one being ignored inside blocks.

## 3. Decision

Retire ACWR. Keep the ratio, computed as **ATL / CTL** off the PMC EWMAs.

Same relative-overload signal, three improvements:

- **Smooth windows.** Flat 7-day windows step when a single big session ages out on day
  8, an artifact with no physiological event behind it. EWMAs decay smoothly.
- **Less coupling.** ACWR's acute 7 days were 25% of its 28-day chronic sum by
  construction. In the EWMAs a given day weighs ~14% in ATL but only ~2% in CTL.
- **Nothing new to store.** It is a division of two columns already on every row.

`garmin.pmc.load_ratio(atl, ctl)` is the single implementation, returning `None` when
either EWMA is NULL or CTL has not warmed above zero (a ratio against ~0 is noise). It is
derived at read time and never stored, so it cannot drift from the EWMAs it divides.

### Job assignment after the change

| Metric | Question |
|---|---|
| CTL | How fit am I? (absolute) |
| TSB | How fresh am I — am I peaked? |
| ATL:CTL | How spiked am I relative to my own base? (the low-CTL guard TSB misses) |
| CTL ramp | Is this multi-week climb sustainable? |

## 4. Phase-awareness — the part that actually fixes §1(a)

Dropping ACWR removes the most block-hostile bands, but a ratio is still a ratio: a
transmutation block drives ATL:CTL to ~0.7 by design. So the science file gains
`training_load.txt` §4, "Planned vs unplanned":

> TSB and ATL:CTL detect UNINTENDED load. Judge the athlete's numbers against the PLAN,
> not against a universal band.

with expected trajectories per block type (accumulation steps up to 1.2–1.4 and settles;
transmutation and taper sit at 0.6–0.9 and that is the goal), and the explicit
instruction not to trim a planned step or backfill a planned unload. The universal bands
apply where there *is* no planned trajectory: unstructured training, off-plan weeks,
return from layoff or illness — which is where an unplanned spike is genuinely dangerous
and where the ratio earns its keep.

The display layer follows the same rule. `util.color_load_ratio` colors only the overload
end (>1.5 red, 1.3–1.5 yellow) and leaves everything at or below 1.3 bare, matching the
precedent `color_tsb` already set: a phase-dependent value's interpretation belongs to
the coach reading the science file, not to a phase-blind color map. The old `color_acwr`
yellow-flagged everything under 0.8, which is precisely the normal state of an intensity
block or a taper.

## 5. What this does NOT do

It does not implement planned-vs-actual comparison in code. §4 tells the coach to judge
against the plan, but nothing computes a *planned* ATL:CTL trajectory to compare against
— the coach infers the phase from the mesocycle context already in its prompt.

`progression.daily_loads` already produces a merged actual/planned daily load series
through plan end, so a planned PMC and a planned ratio are a small addition on top. The
actionable form is a deviation: "ATL:CTL 1.44 (planned 1.40) — on plan" versus
"ATL:CTL 1.44 (planned 1.05) — unplanned spike". That is the natural next step and is
deliberately out of scope here.

TSB keeps a milder version of the same phase-blindness: "sustained TSB well above +25
means fitness is decaying, resume building" can still misfire inside a realization block.
§5 of the science file now carves out the planned-taper case in prose, but no code
enforces it.

## 6. Migration

Single-user, so a one-off guarded DDL in `db/base.py` (AGENTS.md) drops
`acute_workload`, `chronic_workload`, and `acwr` from `athlete_metrics_cache`. No
separate script and no backward compatibility: `save_metric_cache()` loses the three
parameters, `AthleteMetric` loses the three fields, and `recompute_derived()` loses the
rolling-sum pass entirely.

`config.acwr_acute_days` / `acwr_chronic_days` are removed. The Garmin history prefetch
in `client._derivation_pad_days()` is unaffected — it was
`max(acwr_chronic_days, 28, ceil(1.5 * pmc_ctl_days))` and the CTL term (63 at defaults)
already dominated, so it becomes `max(28, ceil(1.5 * pmc_ctl_days))` with the same value.

`coach/service/editing.py`'s swap warning was labelled an "ACWR proxy" but never read
ACWR — it is a pure weekly-TSS-delta heuristic, so only its wording changed. The analysis
weekly digest key `max_acwr` becomes `max_load_ratio`.
