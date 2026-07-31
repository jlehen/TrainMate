# Design: Richer Analysis Evidence — Contextual Events + Body-Response Features

**Status:** Implemented (2026-06-14) · **Branch:** `richer-analysis-evidence-claude`

The shared `data bootstrap` / `data reflect` brain (`_analyze_workouts_logic`)
reconstructs training phases and authors coach learnings from one input:
`weekly_summaries`. Today each week is a coarse aggregate (load, zones, avg
RHR/HRV, max ATL:CTL, rest days) plus a `highlights` list gated on a hardcoded
TSS≥120 / RPE≥8 / name-match threshold. Two high-value, low-effort signals that
*already live in the database* are never shown to the model:

1. **Life events** — the analysis path passes **no** `lifeevents`, even though the
   planning path already fetches and formats them. The model is asked for
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
- The analysis prompt sees, **per week**, the life events overlapping that week and
  a small set of deterministic body-response features, co-located with the
  `week_commencing` so the LLM can attribute (and the evidence model can cite) them
  to the right week.
- Better, *better-grounded* learnings: fewer spurious "overtraining" learnings that
  were actually illness/travel; physiological insights anchored to baseline
  deviations rather than the model eyeballing raw averages.
- Cache-correct: changing a life event or a metric that feeds a feature invalidates
  the reused reconstruction.

**Non-Goals**
- No graduated/recency-tiered resolution (separate, lower-priority item).
- No intra-workout streams (the activity row is already the finest stored grain).
- No new LLM call per week. No new CLI verbs/flags.
- No learning-quality eval harness (recommended next, but out of scope here).

---

## 2. Feature #1 — Life events into the analysis path

### Fetch & window
`get_lifeevents(start_after=from_str)` returns events with `end_date >= from_str`.
Filter in the service for `start_date <= until_str` to get events **overlapping**
`[from_date, until_date]`. (No new DB method; the existing getter + a one-line
Python filter suffices.)

### Placement — per week, not a global blob
Attach to each weekly summary rather than a flat list in the system prompt. The
evidence model keys on `week_commencing`; co-locating the event with the week it
touches makes the causal link direct ("week 2026-03-09 load fell because a
`travel` event spanned it") and makes the model's `evidence: [week]` citations
trustworthy. An event is bucketed into **every** ISO week it intersects.

### Per-week shape
```json
"life_events": [
  {"title": "Flu", "type": "illness", "impact": "bed-bound 3 days",
   "coverage": "full"}      // "full" = spans the whole week, else "partial"
]
```
Omit dates (the week implies them); keep `title`/`type`/`impact` (the fields the
planning path already surfaces). `coverage` is cheap and tells the model whether
the week was wholly vs partly affected. Empty list when none overlap.

### Prompt
`_analyze_workouts_logic`'s TASK gains a sentence: when a week carries
`life_events`, consider them as a possible explanation for load/▼performance/▼recovery
anomalies before attributing to training adaptation, and avoid authoring a
training learning from a week whose anomaly a life event already explains.

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
z `(value − mean)/std`, then average the week's days:

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
  "life_events": [                                        // NEW (#1)
    {"title": "Work crunch", "type": "stress",
     "impact": "long hours, poor sleep", "coverage": "full"}
  ]
}
```
A coach (or the LLM) now reads this as: high load **and** suppressed recovery, but
a full-week stress event with poor sleep is present → likely life-driven, not an
overtraining signal worth a durable learning.

---

## 5. Cache / fingerprint

`_get_evidence_fingerprint` must shift when the new inputs change, or a reused
reconstruction goes stale:
- Add **`stress`** to the per-metric digest (currently omits it; now surfaced).
- Fold an **overlapping-life-events digest** (id, start, end, type, impact) into
  the hash. Pass the window-filtered `lifeevents` into the method (new param).

**Deliberate omission (consistent with §11 of DESIGN_backward_evaluation):**
baseline *recomputation* that changes a deviation without any in-window metric
changing will **not** invalidate the cache. Baselines are derived from the same
metrics, so in practice they move with metric changes; `--force` is the escape
hatch. Documented, not silently ignored.

---

## 6. Touch points

- `coach/service.py::_run_workout_analysis`
  - fetch + window-filter `lifeevents`;
  - in the per-week loop: bucket overlapping events; compute `avg_sleep_score`,
    `avg_stress`, `vs_baseline_z`, `recovery_state` (one `get_baseline` per week);
  - pass `lifeevents` to `_get_evidence_fingerprint`.
- `coach/engine.py::_get_evidence_fingerprint` — add `lifeevents` param + `stress`.
- `coach/engine.py::_analyze_workouts_logic` — TASK prompt additions (§2, §3); no
  signature change (features ride inside `weekly_summaries`).
- `db/` — **no new methods** (reuse `get_lifeevents`, `get_baseline`,
  `get_metrics_cache`).

## 7. Helpers (keep the week loop readable)

Extract two private statics in `CoachService` so the already-long week loop stays
legible and unit-testable in isolation:
- `_week_life_events(events, monday, week_end) -> list` (overlap + coverage).
- `_week_response_features(w_metrics, baseline) -> dict` (sleep/stress means,
  `vs_baseline_z`).

## 8. Tests

- `_week_response_features`: z-sign correctness; `std==0`/missing baseline → `None`;
  whole `vs_baseline_z` omitted when all three components are `None`; empty metrics.
- `_week_life_events`: full vs partial coverage; multi-week event bucketed into each
  week; non-overlapping event excluded.
- Fingerprint: changing a `stress` value or an overlapping life event shifts the
  hash; an out-of-window event does not.
- Integration: a week with a `suppressed` + life-event combo appears in the JSON
  handed to `openrouter_client.complete` (assert on `call_args`).

## 9. Risks

- **Token growth / signal dilution.** Per-week additions are small and bounded;
  acceptable. Watch total prompt size on long `bootstrap` windows.
- **Sparse baselines early in history.** Handled by `None`/omission; early weeks
  simply lack `vs_baseline_z` — honest, not zero-filled.
- **No quality eval.** We can't *prove* learnings improved. Recommend a small
  golden-case check before tuning thresholds further (out of scope, flagged).
```
