# Periodization: Concepts and Vocabulary

> **AUTHORITY: VOCABULARY.** This document defines periodization terms and the
> structures they name. It does not prescribe durations, loading ratios, taper
> magnitudes, or block order — a plan document in `science/` decides those.
> Where no plan document specifies, the defaults noted here apply.

## 1. Core Architectural Overview

Training time is organised as a hierarchy of nested containers. Time containers
(Macrocycles, Mesocycles, Microcycles) say *when*; Periodization Styles
(Traditional vs Block) say *how volume and intensity are distributed* inside
those containers.

*   **Macrocycle** (Overarching Goal Timeline)
    *   **Mesocycle Blocks** (Targeted Phase Adaptation)
        *   **Microcycles** (Fluid Work/Rest Schedule)
            *   **Daily Workout Blueprints**

---

## 2. Component Hierarchy & Container Logic

### A. Macrocycle (The Goal Timeline)

The macrocycle encompasses the entire timeline leading to an athlete's primary
objective.

* **With a target event:** the macrocycle is normally built *backward* from the
  event date, so the most event-specific work lands nearest to it.
* **Without a target event:** there is no date to work back from. Mesocycles
  cycle continuously, and the plan document's ordering rules govern.
* A timeline too long to hold a single coherent build is split into sequential
  macrocycles separated by a formal recovery period. What counts as "too long"
  is a plan-document decision, not a fixed number.

### B. Mesocycle (The Phase Block)

A medium-sized container dedicated to forcing a specific physical or metabolic
adaptation.

* **Traditional:** longer mesocycles; load and intensity change on a gradual,
  shallow gradient.
* **Block:** shorter mesocycles; changes occur on a steep, abrupt vertical step
  function, with training concentrated on one or two qualities at a time.

Both are common and neither is universally correct. A plan document may use
either, or a structure that is neither.

#### Block-model vocabulary

Plans written in the block-periodization tradition name their mesocycles by
function. These are *roles*, not durations:

* **Accumulation:** builds general capacity, typically at high volume and low
  intensity — the oxidative base that supports later intensive work.
* **Transmutation:** converts that capacity into event-specific fitness.
  Higher intensity, lower volume, concentrated on one or two limiters.
* **Realization:** sheds accumulated fatigue so the adaptation becomes
  performance. This is the taper.

A plan may name its blocks by their *stimulus* instead (e.g. a threshold block,
a sprint block). Either naming is fine; what matters is that each block states
what it is for, so its expected load signature can be derived
(`training_load.txt` §4).

#### Mesocycle Loading Progressions

Mesocycles usually alternate a run of progressively loaded microcycles with a
lighter recovery microcycle. The ratio of loading to recovery is a plan choice,
and some blocks — typically short, low-volume ones — carry no scheduled deload
at all.

* **Deload Protocol:** a deload **cuts volume substantially more than
  intensity**. That asymmetry is the mechanism: reduced volume clears systemic
  fatigue, while preserved intensity maintains neuromuscular and technical
  quality so the athlete does not return blunted. The magnitudes belong to the
  plan document.

### C. Microcycle (The Fluid Schedule)

The repeating work/rest unit from which daily workouts are allocated. **Where a
plan document expresses doses per week ("2/week", "75% of the previous week"),
the microcycle is 7 days** unless that plan says otherwise.

#### Microcycle Programming Defaults

These apply unless a plan document deliberately specifies otherwise:

* **The Fatigue Buffer:** high-intensity sessions (VO2max intervals, threshold
  workouts, anaerobic sprints) are not scheduled on consecutive days. A plan
  prescribing deliberately concentrated consecutive loading may override this —
  cap the run's length and watch recovery metrics closely
  (`recovery_metrics.txt`).
* **The Aerobic Anchor:** the microcycle's *longest* session should be a
  low-intensity one, so the week's greatest duration and its greatest intensity
  do not land on the same session. This is a comparative rule: it holds equally
  for a week of three short rides and a week containing a four-hour ride. How
  long that longest session is, and whether there is more than one, is a plan
  decision.
* **The Rest Mandate:** 1 to 2 complete rest or active-recovery days within any
  rolling 7-day equivalent.

---

## 3. Training Residuals

A **training residual** is how long an adaptation takes to decay back toward
baseline after training for it stops. Loss begins within days — the figure
marks roughly when the quality is gone, not a window in which it holds.
Figures are for trained athletes. What to do about a residual — block order,
revisit frequency — is a plan-document decision.

* **Aerobic endurance: 30 ± 5 days.**
* **Maximal strength: 30 ± 5 days.**
* **Anaerobic (glycolytic) endurance: 18 ± 4 days.**
* **Strength endurance: 15 ± 5 days.**
* **Maximal speed / alactic power (incl. rate of force development):
  5 ± 3 days.**

* Structural adaptations (capillarization, cardiac remodelling, hypertrophy)
  decay slowly; enzymatic and neural ones (glycolytic enzymes, buffering,
  phosphocreatine stores, neural drive) decay fast.
* Endurance decays delivery-first (plasma volume → stroke volume → VO2max,
  first ~3 weeks), then utilization (mitochondrial enzymes). Performance
  decays faster than VO2max: time-to-exhaustion falls ~4–25% inside the
  first 2–4 weeks.
* Novices (< 1–2 years of training) have much shorter residuals; a longer
  block leaves a longer residual.
* A maintenance dose stops the decay: intensity must survive, volume and
  frequency can drop sharply — as little as one weekly heavy session has
  been shown to hold maximal strength, and endurance has held 4–8 weeks on
  much-reduced volume. Same asymmetry as the deload protocol (§2B).
