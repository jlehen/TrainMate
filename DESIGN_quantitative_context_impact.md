# Design: Quantitative Context Impact

**Status:** Implemented · **Date:** 2026-06-14 · **Branch:** main

> **Revision (2026-06-14):** the per-signal-day row of the first draft is replaced
> by an **episode model** — consecutive (or near-consecutive) signal-days are
> grouped into one row carrying a *dose sequence* and a single before/during/after
> morning strip. This resolves the consecutive-signal-day cross-contamination case
> and folds the reference-day contrast into every episode, so separate reference
> rows are dropped. See §3, §4, §10.

This is the build-out of the **"Later (quantitative)"** path deferred in
`DESIGN_calendar_context_ingest.md` §7. Today the coach uses external daily
context signals (alcohol, a big meal, …) **qualitatively**, but the way they reach
the LLM is lossy: they ride inside **weekly aggregates** (`DESIGN_richer_analysis_evidence.md`),
so "alcohol: 2" on a Tuesday is averaged into a week, not paired with **Wednesday
morning's** recovery, and never shown next to **that day's training load** — the
other thing that moves the morning. The model is left to guess across smeared data.

This design closes that gap with **deterministic alignment, not statistics**: the app
groups a signal's days into *episodes* and pairs each with *its dose-and-load sequence*
and the **mornings bracketing it** (k before, the run during, k after), each expressed
as recovery-vs-normal, and hands the LLM those **per-day facts**. The LLM does the
judging — the size of the effect, how many days it lasts, the cumulative cost of
consecutive days, and whether drinking *and* a hard day stack worse than either alone.
**The app aligns; the LLM reasons.** No regression, no synergy term, no app-coded
model.

It stays inside the project's settled division of labour and feeds the **existing**
week-keyed evidence model (`DESIGN_evidence_based_confidence.md`): the measurement
is grounding, the LLM authors any durable conclusion, the app counts weeks. No new
confidence machinery; no domain knowledge about what any signal *means*.

---

## 1. Motivation

The `daily_context.value` column (the drink count) was added by
`DESIGN_calendar_context_ingest.md` precisely to keep this path open, and is
currently unused by logic. The qualitative path has two limits this addresses:

- **Wrong granularity and wrong alignment.** Signals are folded into weekly
  averages, so the overnight cause→effect (drink on **D** → bad morning **D+1**) is
  smeared away. A drink Tuesday and a clean Sunday land in the same weekly number.
- **The confound is invisible.** A bad morning can come from a hard workout, from
  drinking, or from **both together** (the worst case). The model never sees the
  *day's training load* sitting beside the *drinks*, so it cannot separate them — it
  will sometimes blame alcohol for what training did, or vice-versa.
- **Multi-day effects cross the week boundary and vanish.** A heavy Saturday night
  still depresses Monday's HRV (D+2), but a weekly aggregate buries that morning —
  worse, if Monday falls in the *next* ISO week, the signal and its lingering effect
  sit in different buckets entirely, and nothing connects them.

The raw material to fix all three is already in the DB: the signal (`daily_context`),
the following mornings' recovery (`athlete_metrics_cache` + `athlete_baselines`), and
daily training load (`completed_activities.tss`). This design **joins and aligns**
them per signal-day and shows the result to the LLM.

---

## 2. Goals / Non-Goals

**Goals**
- Hand the LLM **aligned per-episode facts** — for each *episode* (a run of one or
  more signal-days; §3): the **dose sequence** (each day's signal magnitude and
  training load) and the **strip of mornings bracketing it** — k before, the run
  during, and k after — each as recovery-vs-normal tagged with its own preceding
  day's load. So the LLM can reason about effect, size, **how many days it
  persists**, the drink-and-hard-day combination, and the **cumulative** cost of
  back-to-back days.
- The **alignment and normalization are deterministic** (app-side): grouping
  signal-days into episodes, the correct before/during/after morning pairing, the
  right load attached to each morning, and recovery expressed vs the athlete's
  personal baseline. This is the bookkeeping the LLM should not be doing (and cannot
  do reliably across week boundaries — §1).
- **Use the magnitude** (`value`): the raw drink count is shown, so 2 and 6 are
  visibly different, not one "drink day."
- **Category-agnostic:** the same alignment runs identically for `alcohol`, a big
  meal, … TrainMate never encodes what any of them mean.
- Feed the result into the coach's **existing** analysis prompt so a durable
  conclusion accrues confidence through the week-keyed evidence basis
  (`DESIGN_evidence_based_confidence.md`) — no second confidence path.

**Non-Goals**
- **No app-side statistics.** No regression, no fitted slopes, no interaction/synergy
  term, no significance tests. The app aligns facts; the LLM finds the relationship.
  (Earlier drafts proposed an app-fitted two-/three-dial regression; **dropped** —
  small signal-day counts rarely support honest slopes, and the LLM reads synergy
  naturally from the paired facts. See §4.)
- **No new LLM call.** The facts ride inside the one existing analysis pass, like
  `vs_baseline_z` does (`DESIGN_richer_analysis_evidence.md`).
- **The aligned facts are not stored as a learning** (§7). They are recomputed from
  data each run, like a baseline. Only the LLM's *conclusion* becomes a learning.
- **No domain logic / no per-signal rules.**
- **No app-modelled lag decay.** The app *shows* the before/during/after morning
  strip (§3) but fits no decay curve and asserts no "lasts N days" — the LLM reads
  persistence off the arc. (k bounds how far we look, not a claim about how far the
  effect reaches.)

---

## 3. The data the app aligns

For each signal category `m` (e.g. `alcohol`), the app groups the logged signal-days
into **episodes** and assembles one row per episode. An **episode is a maximal run of
signal-days separated by fewer than `k` drink-free days** (§3.0) — a single isolated
day is just an episode of length 1, so there is one uniform row shape. Each row
carries:

| Field | Source | Meaning |
|-------|--------|---------|
| `days` | one entry per signal-day in the episode | the **dose sequence**: each `{date, value, load_tss}` — the signal magnitude (drinks, shown raw) and that day's training load |
| `surrounding_mornings` | mornings spanning the episode | the recovery strip **before, during, and after** the run; per morning: recovery-vs-normal **and** that morning's *preceding* day's load |

**The look-ahead `k` is configurable, default 3** (`context_days_lookahead`). It sets
both how far *before* the first signal-day and how far *after* the last we surface
mornings — far enough to watch a heavy session/night clear (~2 days), not so far the
edges are dominated by intervening training. The morning strip spans
**`(first_day − k + 1) … (last_day + k)`**: the leading `k` mornings (all
drink-free) are a temporally-matched local "normal," the interior mornings show the
run accumulating, and the trailing `k` watch it clear.

Each morning carries the load of **the day before it** (`prev_day_load_tss`), so the
LLM can tell a still-depressed morning after a *rest* day (lingering signal) from one
after a hard day (training). A morning's preceding-day **dose** is recoverable by
matching its date back to the episode's `days` list (an interior morning whose prior
date is absent from `days` fell on a drink-free gap day). Grouping the run into one
row is what removes the **back-to-back confound**: a second drink the next night no
longer sits, unlabelled, inside a neighbouring day's "following" window — it is one
of the doses listed in the same row. This is also what makes the cross-week case (§1)
legible: the mornings are explicit and dated, never folded into a weekly bucket.

### 3.0 Episode grouping (the only app-side clustering)

Signal-days for a category are clustered greedily by date: two consecutive
signal-days join the same episode when the number of **drink-free days between them
is `< k`**. The rationale is purely to keep each morning strip un-confounded — if two
runs sit within `k` days, the trailing window of the first and the leading window of
the second overlap, so the mornings between them are shadowed by *both* and must be
read as one episode. Interior gap days (drink-free days inside a merged episode) are
**not** added to `days`, so their absence marks them drink-free, but their mornings
stay in `surrounding_mornings` (each with `prev_day_load_tss`) — a partial rebound on
a dry day mid-cluster stays visible. Episode length is unbounded in principle (a
chronic every-other-day pattern merges into one long row); this is correct
(it is not independent trials) but is a volume input to §6.

**Recovery is the existing baseline-relative deviation**, not a raw metric.
`athlete_baselines` already stores rolling `{rhr,hrv,sleep}_baseline_{mean,std}`;
the per-day z `(value − mean)/std` is already computed inside
`CoachService._week_response_features` (`coach/service.py:1250`). We **extract that
into a shared `_day_response_z(metric_row, baseline) -> {rhr,hrv,sleep}` static** so
the weekly feature and this alignment share one definition of "notches from
normal." Sign convention is documented as today: **+hrv better, +rhr worse, +sleep
better**. Showing R vs-normal (not raw "HRV 65") is what lets the LLM compare
mornings at all.

**Day-of load, not the rolling acute load.** `athlete_metrics_cache.acute_workload`
is an EWMA — it smears yesterday into today. The stimulus that drives a given
morning is the *actual training done the day before it*, so every load shown (each
day's `load_tss` in the dose sequence and each morning's `prev_day_load_tss`) is the
**sum of that day's `completed_activities.tss`** (0 on a rest day).

We surface all recovery channels (hrv, rhr, sleep) per morning and never collapse
them into a single "worse" score — picking what "worse" means is the LLM's job
(consistent with the "no derived recovery label" decision in
`DESIGN_richer_analysis_evidence.md` §3).

### 3.1 What `value` can be — the genericness has a resolution limit

The method is generic in its **mechanism** (it shows whatever number is logged),
but the *resolution* of any conclusion is only as good as the number the logger can
honestly supply. TrainMate never interprets the scale of `value`; the encoding is a
choice made **when the signal is logged**. Every signal therefore lands in one of
three tiers — and because the LLM (not a fitted model) reads the rows, the
degradation is automatic, no special casing:

| Tier | Example | What `value` is | What the LLM can conclude |
|------|---------|-----------------|---------------------------|
| **True magnitude** | alcohol = drinks; meal = grams; "ate N hours late" | a real count/quantity | a dose sense ("more drinks → worse, roughly proportional") |
| **Subjective rating** | "meal too big" = 1/2/3 | a rank the logger assigns | an ordinal sense, read cautiously (4 worse than 2, but not "twice") |
| **Presence only** | just "big meal", no number (`value` NULL) | nothing | a yes/no contrast ("flagged days run worse") |

So *meal too big* is honestly Tier 2 or 3. The app's behaviour is identical across
tiers — it shows the value (or its absence); the LLM calibrates how much to read
into it.

### 3.2 External-only, and never explain a channel with itself

Two scope rules keep the facts honest:

- **External signals are for what Garmin cannot see.** `sleep_score` and `stress`
  are already columns in `athlete_metrics_cache` — they are **response channels**,
  not external signals. The context channel is for the unmeasured: alcohol, a big or
  late meal, caffeine, a skipped meal, a work deadline, an argument, travel, a
  broken night. Logging a subjective copy of something Garmin already records just
  half-duplicates a metric.
- **Skip any response channel that duplicates the signal's own construct.** Showing
  a subjective *sleep* signal beside a following morning's `sleep_score` invites the LLM
  to "discover" that bad sleep predicts bad sleep — an echo, not an impact. When a
  signal's construct overlaps a response channel, the rows **omit that channel** for
  that signal and show the independent ones (test a subjective-sleep signal against
  HRV and RHR, not `sleep_score`). v1 applies this via a small static
  construct→channel exclusion map; categories with no overlap (the common case —
  alcohol, meals) exclude nothing.

---

## 4. What the app hands the LLM

A compact block beside `weekly_summaries`: per signal category, the aligned
**episode rows** (§3) — each a dose sequence plus its before/during/after morning
strip. Nothing is fitted or summarized.

```json
"context_days": {
  "alcohol": [
    {
      "days": [
        {"date": "2026-05-09", "value": 4, "load_tss": 85},
        {"date": "2026-05-10", "value": 2, "load_tss": 20},
        {"date": "2026-05-11", "value": 6, "load_tss": 40}
      ],
      "surrounding_mornings": [
        {"morning": "2026-05-07", "prev_day_load_tss": 30, "vs_normal": {"hrv":  0.1, "rhr": 0.0, "sleep":  0.0}},
        {"morning": "2026-05-08", "prev_day_load_tss": 0,  "vs_normal": {"hrv": -0.2, "rhr": 0.1, "sleep":  0.1}},
        {"morning": "2026-05-09", "prev_day_load_tss": 55, "vs_normal": {"hrv": -0.4, "rhr": 0.2, "sleep": -0.1}},
        {"morning": "2026-05-10", "prev_day_load_tss": 85, "vs_normal": {"hrv": -2.9, "rhr": 1.4, "sleep": -1.1}},
        {"morning": "2026-05-11", "prev_day_load_tss": 20, "vs_normal": {"hrv": -3.4, "rhr": 1.6, "sleep": -1.3}},
        {"morning": "2026-05-12", "prev_day_load_tss": 40, "vs_normal": {"hrv": -3.1, "rhr": 1.5, "sleep": -1.0}},
        {"morning": "2026-05-13", "prev_day_load_tss": 0,  "vs_normal": {"hrv": -1.4, "rhr": 0.7, "sleep": -0.4}},
        {"morning": "2026-05-14", "prev_day_load_tss": 30, "vs_normal": {"hrv": -0.5, "rhr": 0.2, "sleep": -0.1}}
      ]
    }
  ]
}
```

This one episode reads top-to-bottom: three mornings near baseline (z≈0) *before* the
run, recovery sinking deeper as the dose accumulates (4→2→6 drinks) *during*, then
clearing over ~2 days *after* the last drink. A lone signal-day is the same shape with
a one-entry `days` list and a `2k+1`-morning strip.

**Why both load and dose, day-aligned.** Showing them side by side is what lets the
LLM do the confound separation *and* spot synergy without any coded model: it can
compare drink-days at low vs high load (training's share), compare high- vs low-dose
days at similar load (the drink's share), watch the effect **decay across the trailing
mornings**, read the **cumulative** cost of consecutive days off the dose sequence,
and notice when *both* high is worse than either — the stated worst case — all as a
plain reading of the row. The `prev_day_load_tss` on each morning is what keeps a
still-depressed trailing morning (after a rest day) readable as lingering signal, not
new training.

**The before-mornings are the reference — no separate no-signal rows.** The leading
`k` mornings of every episode are a temporally-matched local "normal at this point in
the block," and `vs_normal` is already baseline-relative (z = 0 is a typical day). So
the contrast the first draft sought from sampled `value: 0` rows is built into each
episode instead — the model always has a clean "what mornings looked like just before"
to subtract, without a separate sampling policy (this **resolves** the old
reference-day open question; §10). The one thing before-mornings do not guarantee is a
high-load *no-drink* day when the athlete only ever drinks on rest days; we accept
that corner rather than build load-stratified sampling.

The prompt instructs: treat each `context_days` row as one episode of paired daily
evidence for whether a logged signal precedes worse recovery; weigh the **number of
distinct episodes** and the spread of doses within them (few episodes ⇒ weak); read
each morning's preceding-day **dose** (via `days`) and `prev_day_load_tss`
**together** before attributing a low morning to the signal; read the before→during→
after arc to judge how long the effect lasts and whether consecutive days stack; treat
a missing channel/morning as "no data," never as zero.

---

## 5. Honesty without app-side gates

There is no slope to test, so the old identifiability/significance gates are gone.
Honesty now rests on **what we show** plus the LLM's instructed caution:

- **Count is visible.** The LLM sees how many episodes (and how many constituent
  signal-days) back each category; the prompt tells it that a handful is weak
  evidence. Grouping makes the count coarser — a three-night bender is one episode,
  not three trials — which is the honest direction. (A `min_signal_days` floor —
  config, default ~5, counting **signal-days** not episodes — may still gate whether
  a category is worth including at all, to avoid prompting on one stray night. One
  small knob, not a model parameter.)
- **No false precision.** Because nothing is fitted, the app never emits a slope or
  p-value it can't stand behind; the LLM calibrates from the raw episodes and their
  count. This is *why* the no-statistics choice is honest at small n, not despite it.
- **Reproducibility caveat.** The LLM re-judges each run, so conclusions aren't
  bit-for-bit reproducible, and it could mis-read if handed *hundreds* of rows.
  Both are softened by existing machinery — the evidence model accumulates agreement
  across runs, and the analysis is cached unless inputs change (§8) — and by keeping
  the block **digestible** (§6 volume note).

---

## 6. Scope: full history of signal-days, windowed citation

The rows cover **all signal-days available** (full history), not just the analysis
window — the whole point is to let the LLM see the entire pattern. So
`_context_days` **fetches its own full-history data independently of the analysis
window** (`from_str`/`until_str`); it does *not* reuse the window-scoped data loaded
for the weekly summaries. A windowed fetch would be wrong: on an incremental
`data reflect` the window is only the new weeks, containing almost none of the
drinking history the LLM needs to see a pattern.

Where the LLM authors a durable conclusion ("higher alcohol suppresses next-day HRV,
worse on hard days"), it **cites the weeks inside the analysis window where
signal-days fall**, via the existing `evidence:[weeks]` delta protocol
(`DESIGN_evidence_based_confidence.md` §6). Confidence then accrues through the
normal week-keyed basis. The facts are the *grounding*; the **learning is still
LLM-authored and the app still counts weeks** — and the global/windowed split mirrors
how global baselines already feed a windowed analysis.

**Volume.** Signal-days are sparse (you don't drink daily) and grouping collapses
runs, so full history is typically a few-tens of episodes, each a short dose sequence
plus a `streak_length − 1 + 2k` morning strip — still small. A chronic
every-other-day pattern can merge into one long episode (§3.0); that is one genuine
confounded period, so we keep it whole. If a long history ever makes the block bulky,
cap to the most recent N **episodes** before trimming k; noted, not built (§10).

### 6.1 Which flows compute it, and when — no separate trigger

The block rides inside the **one analysis path** (`_run_workout_analysis`), which
has exactly two entry points; those are the only flows this change touches:

| Command | Behaviour change |
|---------|------------------|
| `data bootstrap` | builds `context_days` over full history; feeds it to the analysis prompt |
| `data reflect`   | **same** — rebuilds over full history (§6), not just the new weeks |

Everything else is unaffected: `plan generate`, `status`, and `workout adapt` read
durable *learnings*, not the facts, so they inherit the conclusions for free.

There is **no dedicated "analyse my drinking" command**, and the athlete triggers
nothing manually. Because the block is rebuilt full-history every run, the picture
**sharpens automatically** as signal-days accumulate — each routine `data reflect`
shows the LLM the whole history, even though it only analyses the newest weeks.

---

## 7. The facts are not a learning

Two distinct objects, and only one is persisted as a learning:

- **The aligned rows (`context_days`)** — **not** stored as a learning. They are a
  pure function of `daily_context` + `athlete_metrics_cache` + `athlete_baselines` +
  `completed_activities`, recomputed each run, like a baseline. Persisting them would
  only risk staleness.
- **The conclusion the LLM draws** ("alcohol hurts next-day HRV") — **yes**, an
  ordinary `coach_learnings` row, authored through the existing delta protocol,
  gaining/losing confidence as the week-keyed evidence accumulates. As more
  signal-days pile up and keep agreeing, the picture sharpens **and** the learning's
  basis grows.

Flow: **app aligns the facts → LLM turns a clear, repeated pattern into a learning →
that learning earns confidence over time.**

---

## 8. Caching / fingerprint

No fingerprint change needed. `_get_evidence_fingerprint` already folds in
`completed_activities`, `metrics`, and `daily_context` (`coach/service.py:1335`).
`context_days` is a pure function of inputs already hashed, so a changed signal,
metric, or activity invalidates the reused reconstruction exactly as required. The
**deliberate baseline-recompute omission** noted in
`DESIGN_richer_analysis_evidence.md` §5 carries over unchanged (`--force` is the
escape hatch).

---

## 9. Touch points

- `coach/service.py`
  - **Extract** `_day_response_z(metric_row, baseline)` from `_week_response_features`
    (shared per-day z; pure, unit-testable). `_week_response_features` then averages
    it over the week's days; the alignment uses it per morning.
  - **Add** `_context_days(daily_context, metrics, baselines, activities, k) -> dict`
    — per category: (1) cluster signal-days into **episodes**, merging runs less than
    `k` drink-free days apart (§3.0); (2) for each episode emit `days` (the dose
    sequence) and `surrounding_mornings` spanning `(first − k + 1) … (last + k)`, each
    morning tagged `prev_day_load_tss` and the existing z (§3, §4); (3) apply the
    value-tier pass-through (§3.1) and the construct→channel exclusion map (§3.2).
    **No statistics** — pure clustering + join + the existing z. No separate
    reference rows (the leading mornings are the contrast; §4). Optional
    `min_signal_days` / most-recent-N-episode caps (§5, §6).
  - Render `context_days` into the analysis payload next to `weekly_summaries`.
- `coach/engine.py::_data_analyze_logic` — accept a `context_days` argument, render it
  into the user content beside `weekly_summaries`, and add one TASK paragraph (§4):
  read each morning's preceding-day dose (via `days`) and `prev_day_load_tss`
  together; read the before→during→after arc for persistence and the cumulative cost
  of consecutive days; weigh the episode count; missing = no data, not zero.
- **Config loader** — `context_days_lookahead` (**k**, default 3; §3) and an optional
  `context_days_min_signal_days` floor, mirroring the `high_intensity_*` / `coach`
  sub-dict accessors.
- `ARCHITECTURE.md` (+ this doc's status) — per the standing rule to keep
  ARCHITECTURE.md in sync with behavioural change.
- **Optional debug surface:** a thin `context report` that prints `context_days`
  with no LLM call (the athlete's own eyeball of the same rows). Add only if wanted.

**No new dependency.** Because nothing is fitted, there is no numpy/statsmodels
need; it's a join plus the z arithmetic already in the codebase.

---

## 10. Open questions / deferred

**Resolved:**
- **App does the math? No.** App aligns the facts; the LLM finds the relationship and
  any synergy (replaces the earlier app-fitted regression/synergy proposal). Decided
  2026-06-14.
- **Multi-day lag is surfaced, not deferred.** Each episode unrolls a before/during/
  after morning strip (`k` configurable via `context_days_lookahead`, default 3) — so
  lingering and cross-week effects are explicit and dated, not buried in a weekly
  aggregate (§1, §3). Decided 2026-06-14.
- **Consecutive / near-consecutive signal-days → one episode.** Runs of signal-days
  (and runs less than `k` drink-free days apart) are grouped into a single row with a
  *dose sequence* and one morning strip, instead of overlapping per-day windows. This
  removes the back-to-back cross-contamination (a second night's drink is a listed
  dose, not an unlabelled confound in a neighbour's window) and exposes cumulative
  cost. Decided 2026-06-14 (§3, §3.0).
- **Reference-day sampling — folded into the episode, not sampled.** The leading `k`
  before-mornings of every episode are the temporally-matched local "normal," and
  `vs_normal` is already baseline-relative, so the separate `value: 0` reference rows
  (and the question of how to sample them) are dropped. Decided 2026-06-14 (§4).

**Open:**
- **Volume cap (§6).** If full history grows large, cap to most-recent-N **episodes**?
  What N, and does it bias toward recent behaviour?
- **`min_signal_days` floor (§5).** Worth a config knob (counting signal-days), or
  just always show what exists and let the LLM judge from the count?
- **Default `k` (§3).** 3 is the starting default; it now also sets the *before*
  window and the episode-merge gap, so it is worth revisiting once real blocks are
  inspected — long enough to watch a heavy session clear and to bracket a run,
  short enough that the edges aren't all intervening-training noise.

**Deferred (noted, not built):**
- **App-modelled lag decay** — we *show* the morning strip; fitting an actual decay
  curve or "effect lasts N days" stays out (the LLM reads persistence off the arc).
- **`context report` CLI** — ship only if the eyeball view is wanted.

---

## 11. Summary

The app groups each external signal's days into **episodes** (consecutive or
near-consecutive runs) and joins each episode to **its dose sequence** (per day:
signal magnitude + training load) and a **before/during/after morning strip** (`k`
mornings each side, `k` configurable, default 3), each morning as recovery-vs-normal
tagged with its own preceding-day load (`DESIGN_calendar_context_ingest.md` §7's
quantitative path). It does the **grouping, alignment, and baseline-normalization** —
the bookkeeping — and **no statistics**: the LLM reads load and dose together to judge
the signal's effect, its dose-response, **how many days it persists**, the cumulative
cost of back-to-back days, and the drink-and-hard-day synergy, with no coded model.
The episode's leading mornings are the built-in reference, so no separate no-signal
rows are sampled; the during/after mornings make lingering, cross-week effects (a
Saturday night still felt Monday) legible instead of lost in a weekly aggregate. The
rows are **not stored as a learning** — they are rendered into the existing analysis
prompt so the **LLM** authors any durable conclusion and the **existing week-keyed
evidence model** grows its confidence. Only `data bootstrap` and `data reflect`
change; the picture sharpens automatically as signal-days accumulate. TrainMate still
never learns what any single signal *means*.
