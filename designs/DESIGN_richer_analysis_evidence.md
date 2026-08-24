# Design: Richer Analysis Evidence — Contextual Events + Body-Response Features

**Status:** Implemented (2026-06-14) · **Branch:** `richer-analysis-evidence-claude`

> **Retrofit note (vocabulary + layout).** This design was written against the
> `lifeevent` entity and the pre-split `coach/service.py` / `coach/engine.py` modules.
> Two later refactors renamed things without changing this feature's mechanism, and the
> document has been updated in place to match the code:
> - `DESIGN_constraints.md` rev 6 (2026-07-10) superseded `lifeevent` with **`constraint`**.
>   The `lifeevents` table is dropped in `db/base.py`; the per-week key is `constraints`;
>   the authoring-time `type` taxonomy is gone (§2).
> - `coach/service.py` and `coach/engine.py` are now **packages**, and the prompt builder
>   `_analyze_workouts_logic` is now `CoachEngine._data_analyze_logic`
>   (`coach/engine/analysis.py`).
>
> Historical prose below reads in the present tense of the current code, not of 2026-06-14.

The shared `data bootstrap` / `data reflect` brain (`_data_analyze_logic`)
reconstructs training phases and authors coach learnings from one input:
`weekly_summaries`. Today each week is a coarse aggregate (load, zones, avg
RHR/HRV, max ATL:CTL, rest days) plus a `highlights` list gated on a hardcoded
TSS≥120 / RPE≥8 / name-match threshold. Two high-value, low-effort signals that
*already live in the database* are never shown to the model:

1. **Constraints** (then: life events) — the analysis path passes **none**, even though
   the planning path already fetches and formats them. The model is asked for
   "HRV/RHR trends vs load" with no way to know a dip was illness or travel.
2. **Body-response features** — daily `sleep_score` / `stress` are stored but
   dropped from the weekly summary, and `athlete_baselines` (rolling rhr/hrv/sleep
   mean+std) — the raw material for *deviation-vs-baseline* — is never consulted
   here.

This design feeds both into the existing single LLM call per analysis. **No new
model calls**; the "how the body reacted" analysis is deterministic feature
engineering, not a per-week LLM pass.

---

## 1. Goals / Non-Goals

**Goals**
- The analysis prompt sees, **per week**, the constraints overlapping that week and
  a small set of deterministic body-response features, co-located with the
  `week_commencing` so the LLM can attribute (and the evidence model can cite) them
  to the right week.
- Better, *better-grounded* learnings: fewer spurious "overtraining" learnings that
  were actually illness/travel; physiological insights anchored to baseline
  deviations rather than the model eyeballing raw averages.
- Cache-correct: changing a constraint or a metric that feeds a feature invalidates
  the reused reconstruction.

**Non-Goals**
- No graduated/recency-tiered resolution (separate, lower-priority item).
- No intra-workout streams (the activity row is already the finest stored grain).
- No new LLM call per week. No new CLI verbs/flags.
- No learning-quality eval harness (recommended next, but out of scope here).

---

## 2. Feature #1 — Constraints into the analysis path

### Fetch & window
`get_constraints(from_str, until_str)` returns the constraints **overlapping**
`[from_date, until_date]` directly: the two-sided test (`start_date <= until AND
end_date >= from`) is done in SQL (`db/constraints.py`), so the service does no
filtering of its own. (No new DB method; the existing getter suffices. The original
draft specified a one-sided getter plus a Python filter — that filter would be dead
code against the current getter.)

### Placement — per week, not a global blob
Attach to each weekly summary rather than a flat list in the system prompt. The
evidence model keys on `week_commencing`; co-locating the constraint with the week it
touches makes the causal link direct ("week 2026-03-09 load fell because a
travel constraint spanned it") and makes the model's `evidence: [week]` citations
trustworthy. A constraint is bucketed into **every** ISO week it intersects.

### Per-week shape
```json
"constraints": [
  {"title": "Flu", "impact": "bed-bound 3 days", "coverage": "full"}
]
```
Omit dates (the week implies them); keep `title` and `impact` (the constraint's
`description` — the fields the planning path already surfaces). There is deliberately
**no `type`**: constraints replaced the authoring-time taxonomy with free prose, and the
live table has no such column (`DESIGN_constraints.md` §5). Empty list when none overlap.

`coverage` tells the model whether the week was wholly vs partly affected. It is judged
against the week's **in-window** span (`days_in_week[0] .. days_in_week[-1]`), not the
calendar Monday–Sunday. So a truncated first or last week can read `"full"` for a
constraint that only covers the in-window part. That is intended: the analysis only ever
reasons about the days it was given, and tagging such a week `"partial"` would suggest
untouched training days that are not actually in the summary.

### Prompt
`_data_analyze_logic`'s TASK gains a "reading the per-week context fields" block: when a
week carries `constraints`, consider them as a possible explanation for
load/▼performance/▼recovery anomalies before attributing to training adaptation, and
avoid authoring a training learning from a week whose anomaly a constraint already
explains.

**The discount-only asymmetry.** The prompt goes one step further than "weigh it first",
and this is load-bearing enough to state plainly: *a constraint may only explain an
anomaly away — it may never be cited as supporting evidence for a learning.* The field is
one-directional. It can lower confidence in a would-be learning ("that HRV crash was the
flu, not the block"), but it can never raise confidence in one ("the athlete adapts well
under work stress" is not something a `work crunch` constraint may be used to argue).

The reason is the observation/directive split in `DESIGN_constraints.md` §2/§6. A
constraint is a **directive** — something the athlete asked the coach to work around. It
records an intention, not a measurement. "I couldn't train Thursday" is not physiological
data that the block is too hard, so letting it flow into the evidence/confidence machinery
would manufacture durable learnings out of scheduling. Discounting is the one sanctioned
crossing because it only ever *removes* unwarranted confidence, which is safe in a way the
supporting direction is not. Observations (`signal` / `daily_signals`) are the objects
allowed to support a learning; constraints are not.

Practically this means a week whose only notable feature is a constraint should yield **no**
learning at all, rather than a learning that cites the constraint.

---

## 3. Feature #2 — Deterministic body-response features per week

### Surfaced raw metrics (already stored, currently dropped)
Add to each weekly summary: `avg_sleep_score`, `avg_stress` (mean over the week's
metric rows; `None` when absent), alongside the existing `avg_rhr` / `avg_hrv` /
`max_load_ratio` (renamed from `max_acwr`, see DESIGN_load_ratio.md).

### Baseline-relative deviation (the real "response" signal)
`athlete_baselines` stores rolling `{rhr,hrv,sleep}_baseline_{mean,std}` per date.
Per week, fetch **one** representative baseline via
`get_baseline(min(week_end, until_date))` (baselines move slowly, so one lookup per
week — not per day — is enough and keeps this O(weeks)). For each metric `x` in
{rhr, hrv, sleep} with a usable baseline (`std` present and > 0), compute a per-day
z `(value − mean)/std`, then average the week's days. The per-day z now lives in the
shared static `_day_response_z(metric_row, baseline)` (§7) — the single definition of
"notches from normal", also used per-morning by the signal-impact path:

```json
"vs_baseline_z": { "rhr": +1.4, "hrv": -1.1, "sleep": -0.6 }
```
Sign convention documented in the prompt: **+rhr = elevated (worse)**, **+hrv =
higher (better)**, **+sleep = better**. Any component is `None` when no
baseline/std or no metric days. Omit the whole `vs_baseline_z` object when all
three are `None` (mirrors how `power_zone_distribution_sec` is omitted when empty).

**No derived recovery label.** An earlier draft bucketed the z-scores into a
`recovery_state` (suppressed/normal/fresh). Dropped: the continuous z-scores carry
more nuance, the bucket thresholds would be arbitrary and untunable without an
eval, and nothing downstream consumes the label (evidence is week-keyed
regardless). The LLM interprets the raw z-scores directly.

### Lag caveat (documented, not engineered)
Morning RHR/HRV respond to a hard week partly in the **following** week. We emit
*same-week* deviations and the TASK prompt states the lag explicitly so the model
reads a high-load week together with the next week's `vs_baseline_z`. A precomputed
lagged feature (`vs_baseline_z_next`) is a noted extension, not in this cut.

### Prompt
TASK gains: use `vs_baseline_z` as the evidence for `physiological_insights` and
any recovery/overreaching learning; expect the response to lag load by ~a week, so
read a high-load week together with the **next** week's `vs_baseline_z`; treat
`null` as "no data," never as zero.

---

## 4. Enriched weekly-summary example (after)

```json
{
  "week_commencing": "2026-03-09",
  "total_duration_hours": 7.5, "total_tss": 410.0, "average_rpe": 6.2,
  "sports": {"road_biking": 4, "running": 1},
  "zone_distribution_sec": {"Z1_Z2": 18000, "Z3": 2400, "Z4_Z5": 1200},
  "avg_rhr": 53.0, "avg_hrv": 78.0, "max_load_ratio": 1.31,
  "avg_sleep_score": 71.0, "avg_stress": 38.0,          // NEW (#2)
  "vs_baseline_z": {"rhr": 1.4, "hrv": -1.1, "sleep": -0.6},  // NEW (#2)
  "rest_days": 1,
  "highlights": [ ... ],
  "constraints": [                                        // NEW (#1)
    {"title": "Work crunch", "impact": "long hours, poor sleep", "coverage": "full"}
  ]
}
```
A coach (or the LLM) now reads this as: high load **and** suppressed recovery, but
a full-week work-crunch constraint with poor sleep is present → likely life-driven, not
an overtraining signal worth a durable learning. Note that per §2 this is a reason to
author *no* learning for the week, not to author one citing the constraint.

**Not the complete shape — see also.** The example shows only what *this* design adds.
Later designs put more fields on the same weekly summary: `power_zone_distribution_sec`
(DESIGN_intensity_distribution.md), `end_ctl` / `week_ramp` / `min_tsb`
(DESIGN_pmc_fitness_fatigue.md), and an optional `daily_signals` list
(DESIGN_calendar_signal_ingest.md). A separate full-history `signal_days` block rides
*beside* the summaries in the same user content (DESIGN_quantitative_signal_impact.md).
`trainmate/coach/service/analysis.py` is the authority on the emitted shape.

---

## 5. Cache / fingerprint

`_get_evidence_fingerprint` (`coach/engine/prompt.py`) must shift when the new inputs
change, or a reused reconstruction goes stale:
- Add **`stress`** to the per-metric digest (it was omitted; now surfaced).
- Fold an **overlapping-constraints digest** into the hash, via a new `constraints`
  parameter carrying exactly the rows `get_constraints(from, until)` returned.

The hashed tuples, as implemented (this list governs cache correctness, so keep it exact):

| digest | tuple |
|---|---|
| `activities` | `activity_id, date, activity_type, duration_sec, tss, rpe, zone1..zone5_sec` |
| `metrics` | `date, rhr, hrv, sleep_score, stress, ctl, atl, tsb` |
| `constraints` | `id, start_date, end_date, rest, title, description` |
| `daily_signals` | `date, metric, value, text` |
| `signal_days` | the assembled block itself, hashed as computed |

plus the `[window_start, window_end]` pair. Each digest is a sorted set/list, serialized
with `sort_keys=True` and SHA-256'd.

Notes on what is *not* in the constraints tuple: `replan` and `source` are deliberately
excluded — they steer plan invalidation and record provenance, and neither reaches the
analysis input, so hashing them would invalidate the cache for a change the model cannot
see. `rest` **is** hashed (as `int(rest or 0)`), since a hard-rest constraint changes how a
week must be read.

`daily_signals` and `signal_days` are later additions (DESIGN_calendar_signal_ingest.md
§7, DESIGN_quantitative_signal_impact.md §8), not part of this design's cut; they are
listed because the method now takes them and the reader needs the whole tuple. `signal_days`
is *full-history*, so unlike every other input a change **outside** `[from, until]` still
shifts the fingerprint — correctly, because it changes the prompt (§8).

**Deliberate omission (consistent with §11 of DESIGN_backward_evaluation):**
baseline *recomputation* that changes a deviation without any in-window metric
changing will **not** invalidate the cache. Baselines are derived from the same
metrics, so in practice they move with metric changes; `--force` is the escape
hatch. Documented, not silently ignored.

---

## 6. Touch points

`coach/service.py` and `coach/engine.py` are packages; the files below are the current
homes.

- `coach/service/analysis.py::DataAnalysisMixin._run_workout_analysis`
  - fetch overlapping constraints (`get_constraints(from, until)` — no service-side
    filter, §2);
  - in the per-week loop: bucket overlapping constraints; compute `avg_sleep_score`,
    `avg_stress`, `vs_baseline_z` (one `get_baseline` per week, at the week's last
    in-window day);
  - pass `constraints` to `_get_evidence_fingerprint`.
- `coach/engine/prompt.py::_get_evidence_fingerprint` — add `constraints` param +
  `stress` (§5).
- `coach/engine/analysis.py::AnalysisLogicMixin._data_analyze_logic` — TASK prompt
  additions (§2, §3); no signature change (features ride inside `weekly_summaries`).
- `db/` — **no new methods** (reuse `get_constraints`, `get_baseline`,
  `get_metrics_cache`).

No `recovery_state` is computed — §3 dropped the derived label, and the code has none.
(An earlier draft of this list still named it; that was the leftover, not the plan.)

## 7. Helpers (keep the week loop readable)

Private statics on the analysis mixin, so the already-long week loop stays legible and
unit-testable in isolation:
- `_week_constraints(constraints, week_start, week_end) -> list` (overlap + coverage).
- `_week_response_features(w_metrics, baseline) -> dict` (sleep/stress means,
  `vs_baseline_z`).
- `_day_response_z(metric_row, baseline) -> dict` — the per-day `(value − mean)/std` for
  rhr/hrv/sleep, extracted out of `_week_response_features` by
  DESIGN_quantitative_signal_impact.md so the weekly feature and the per-morning
  signal-impact strip share one definition. It is the single source of truth for the z;
  `_week_response_features` just averages its output over the week. The channel/column
  mapping lives beside it in `_RESPONSE_Z_CHANNELS`.

## 8. Tests

In `tests/test_analysis.py`:

- `_week_response_features` (`TestWeekResponseFeatures`): z-sign correctness;
  `std==0`/missing baseline → `None`; whole `vs_baseline_z` omitted when all three
  components are `None`; empty metrics.
- `_week_constraints` (`TestWeekLifeEvents`, class name kept from the rename): full vs
  partial coverage; multi-week constraint bucketed into each week; non-overlapping
  constraint excluded.
- Fingerprint (`TestRicherEvidenceIntegration`): editing an overlapping constraint
  shifts the hash, so the next run recomputes; a constraint lying entirely **outside**
  `[from, until]` does not, because `get_constraints` never returns it. Contrast with
  `signal_days`, which *is* full-history: an out-of-window daily signal **does**
  shift the hash (§5) and has its own test.
- Integration: the per-week `constraints` entry and `vs_baseline_z` / `avg_stress` appear
  in the JSON handed to `openrouter_client.complete` (assert on `call_args`).

## 9. Risks

- **Token growth / signal dilution.** Per-week additions are small and bounded;
  acceptable. Watch total prompt size on long `bootstrap` windows.
- **Sparse baselines early in history.** Handled by `None`/omission; early weeks
  simply lack `vs_baseline_z` — honest, not zero-filled.
- **No quality eval.** We can't *prove* learnings improved. Recommend a small
  golden-case check before tuning thresholds further (out of scope, flagged).
