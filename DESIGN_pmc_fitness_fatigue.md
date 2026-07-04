# Design: PMC Fitness/Fatigue/Form (CTL · ATL · TSB)

**Status:** Proposed · **Date:** 2026-07-03 · **Branch:** main

`science/training_load.txt` (f0bb707) documents the full Performance Management
Chart model — CTL (fitness), ATL (fatigue), TSB (form), and the CTL ramp rate —
and its §4 coaching directives tell the coach things like *"when TSB falls below
−30, default to recovery"* and *"taper so TSB rises into +5..+25 by event day"*.
But the app never computes any of these numbers. The science file is concatenated
verbatim into every coach prompt, so the LLM knows the *theory* perfectly and can
never apply it: the directives reference values that appear nowhere in its input.
The ACWR half of the same document **is** implemented end-to-end
(`garmin.recompute_derived()` → `athlete_metrics_cache` → prompts → `tm status`);
this design gives the PMC half the identical treatment.

**The app computes; the LLM reasons.** Same division of labour as everywhere
else: deterministic EWMAs in the app, interpretation (build vs taper vs
overreach) left to the coach reading the science directives against real values.

---

## 1. Motivation — concrete failures today

- **Adapt can't see accumulated fatigue.** ACWR compares 7 days against 28 with
  flat windows; after three smoothly-ramped hard weeks ACWR sits innocently near
  1.1 while TSB has sunk to −35. The adapt prompt shows the LLM only the innocent
  number, so the science directive that should fire ("TSB < −30 → recovery")
  cannot.
- **Taper is blind.** The A-event taper directive targets a TSB band (+5..+25 on
  event day). Plan generation gets no CTL/ATL at all, so the LLM can only guess a
  generic "reduce volume ~40%" taper instead of reasoning from the athlete's
  actual freshness trajectory.
- **Week-over-week progression is unguided.** The science caps CTL ramp rate
  (~3–5 sustainable, >8 red flag), but no ramp number exists anywhere — plans
  can climb unsustainably for weeks while ACWR stays in the sweet spot, the
  exact divergence `training_load.txt` §3 warns about.
- **The user can't see fitness either.** `tm status` shows ACWR/acute/chronic
  but nothing answers "am I fitter than last month?" or "how fresh am I?" —
  the two questions the PMC exists for.

Everything needed is already in the DB: per-activity load via the existing
`activity_load()` fallback hierarchy (power TSS → hrTSS → sRPE), summed per day
in `recompute_derived()`. This design adds three EWMAs over that same series.

---

## 2. What exists today (touchpoints)

| Layer | Where | Today |
|---|---|---|
| Compute | `trainmate/garmin.py` `recompute_derived()` | full-sweep acute (7d sum), chronic (28d/4), ACWR per metrics day |
| Store | `athlete_metrics_cache` (`db/base.py`), `save_metric_cache()` (`db/activities.py`), `AthleteMetric` (`types.py`) | `acute_workload`, `chronic_workload`, `acwr` columns |
| Coach, per-day | `format_metrics_history()` (`coach/formatting.py`) → generate & adapt prompts (`engine.py`) | `... ACWR=1.12` per day line |
| Coach, summary | data summary in `coach/service.py` (~L96) → strategy/plan prompts | "Current ACWR: 1.12 (latest)" |
| Coach, weekly | analysis weekly digest (`service.py` ~L2279) | `max_acwr` per week |
| Cache key | evidence fingerprint (`engine.py` ~L338) | hashes `m['acwr']` per metrics row |
| User | `tm status` (`cli/status.py`), `tm data show-metrics` table/CSV (`cli/data.py`), `color_acwr` (`util.py`) | ACWR + acute/chronic shown |

Each row of that table gets a PMC counterpart. No new subsystem.

---

## 3. Computation (`recompute_derived()`)

The existing function already builds `daily_load: Dict[date, float]` from all
completed activities. Add one chronological pass **over calendar days** — not
over metrics rows, because rest days and row gaps must decay the EWMAs with
zero load:

```
span:  min(first activity date, first metrics date) … last metrics date
ctl_0 = atl_0 = 0.0
ctl_d = ctl_{d-1} + (load_d − ctl_{d-1}) / τ_ctl     # τ_ctl: config, default 42
atl_d = atl_{d-1} + (load_d − atl_{d-1}) / τ_atl     # τ_atl: config, default 7
tsb_d = ctl_{d-1} − atl_{d-1}          # yesterday's values, per the science file
```

- **Form of the EWMA:** the classic Coggan discrete `1/τ` recurrence, matching
  TrainingPeaks convention, rather than `1 − exp(−1/τ)` (difference is ~1%;
  picking the convention the athlete's other tools use makes numbers
  comparable).
- **TSB off-by-one is deliberate and load-bearing:** today's form is what you
  woke up with — it must not include today's workout. `training_load.txt` §2
  states `TSB = CTL(yesterday) − ATL(yesterday)`; implement exactly that, and
  pin it with a test (§8), because it is the classic mistake.
- **Seeding:** start both EWMAs at 0 at the beginning of DB history (the
  earliest activity/metrics date — never "today"), then walk forward. CTL
  under-reads until ~1.5×τ (≈ 6 weeks) of data exists. With a normal Garmin
  backfill the sweep walks months of history and current values are fully
  converged; on a young DB the numbers are "warm-up quality", which is exactly
  how TrainingPeaks/intervals.icu behave too. Not worth machinery — note it in
  the science file's vicinity if at all. (A deeper Garmin backfill via
  `tm data pull --start` is the practical fix on a young DB.)
- **Ramp rate is derived, never stored:** `ramp = ctl_d − ctl_{d-7}` computed
  where displayed (status line, coach summary, weekly digest). Storing it would
  just denormalize a subtraction.
- **Persistence:** upsert `ctl`/`atl`/`tsb` onto **existing** metrics rows only,
  via the same `save_metric_cache()` COALESCE upsert already used for
  acute/chronic. Days with an activity but no Garmin metrics row get no row —
  the in-memory pass still walks them (their load feeds the EWMA), we just
  don't widen the cache's "one row per Garmin metrics day" meaning.
- **Cost:** the sweep is already O(all days); this adds three multiplications
  per day. Nothing to optimize.

Extract the pass into a pure helper so it's unit-testable without a DB:

```python
def compute_pmc(daily_load: Dict[str, float], start: date, end: date,
                ctl_days: int, atl_days: int
                ) -> Dict[str, Tuple[float, float, float]]:  # date -> (ctl, atl, tsb)
```

`recompute_derived()` calls it once and folds the result into its existing
per-row `save_metric_cache()` call. `backfill_tss` and zone-rehierarchy already
funnel through `recompute_derived()`, so every existing refresh path picks the
new values up for free.

### Configurable windows (ACWR + PMC)

All four window/time-constant numbers become config params via the existing
`config.py` property + `config_template.yaml` pattern (like
`rpe_divergence_ratio`):

| Param | Default | Replaces |
|---|---|---|
| `acwr_acute_days` | 7 | `garmin.py` `ACUTE_WINDOW_DAYS` |
| `acwr_chronic_days` | 28 | `garmin.py` `CHRONIC_WINDOW_DAYS` |
| `pmc_ctl_days` | 42 | (new) CTL time constant |
| `pmc_atl_days` | 7 | (new) ATL time constant |

`CHRONIC_WEEKS` and `DERIVATION_PAD_DAYS` stay derived
(`chronic/acute`, `= chronic`). Ramp rate stays a fixed 7-day delta — "per
week" is its definition, not a tunable window.

No migration on change: the next `recompute_derived()` full sweep rewrites
every stored value under the new constants, and the fingerprint shift (§5.4)
refreshes cached analyses.

**Caveat:** the science file's text and every interpretation band (ACWR
0.8–1.3, TSB −30/+25, ramp 3–5/8, and `color_acwr`/`color_tsb`) are calibrated
to the defaults. Non-default constants change what the numbers mean while the
prompts and colors keep judging them against the standard bands — these params
are for deliberate experimentation, not casual tuning; the defaults are the
supported configuration.

- `db/base.py`: `ALTER TABLE athlete_metrics_cache ADD COLUMN {ctl,atl,tsb} REAL`
  using the same `try/except sqlite3.OperationalError` idempotent-migration
  pattern already used for the `completed_activities` zone columns. (The
  `CREATE TABLE IF NOT EXISTS` also gains the columns for fresh DBs.)
- `types.py` `AthleteMetric`: `ctl: Optional[float]`, `atl: Optional[float]`,
  `tsb: Optional[float]`.
- `db/activities.py` `save_metric_cache()`: three new optional params with the
  same `COALESCE(excluded.x, cache.x)` semantics as `acute_workload`, so a
  metrics-only Garmin save never nulls out previously-computed PMC values.

---

## 5. Surfacing to the coach

No prompt-instruction changes are needed — the directives already ride in via
`_load_science_guidelines()`; the values just have to show up next to them
using the same vocabulary (`CTL`/`ATL`/`TSB`, which the science file defines).

1. **Per-day history** — `format_metrics_history()` line becomes:

   ```
   - 2026-07-02: RHR=52bpm, HRV=61ms, Sleep=78, Stress=31, ACWR=1.12, CTL=62.4, ATL=71.7, TSB=-8.9
   ```

   Guard `None` (rows predating the first recompute) by omitting the fields,
   as the line already implicitly assumes for ACWR. This reaches the workout
   generate and adapt prompts (`engine.py` L557, L792) — adapt now sees the
   fatigue trajectory, fixing failure #1.

2. **Data summary** (`service.py` ~L96, feeds strategy/plan prompts) — after
   the ACWR line add:

   ```
   - Fitness/Fatigue (PMC): CTL 62.4 (fitness), ATL 71.7 (fatigue), TSB -8.9 (form)
   - CTL ramp rate: +4.2/week (last 7 days)
   ```

   Plan generation can now reason about taper targets and sustainable build
   rates from real numbers, fixing failures #2 and #3.

3. **Weekly digest** (analysis, `service.py` ~L2279) — alongside `max_acwr`
   add per week: `end_ctl` (CTL on the week's last day), `week_ramp`
   (end_ctl − CTL 7 days earlier), `min_tsb`. Three numbers give the analyze
   pass the multi-week fitness trajectory and depth of each week's overload
   without per-day noise.

4. **Evidence fingerprint** (`engine.py` `met_digest`) — add `m.get('tsb')`.
   Strictly it's derivable from already-hashed activities, but including it
   makes the first post-deploy recompute (rows gaining PMC values) invalidate
   cached analyses, so the new signal reaches reconstructions without everyone
   needing `--force`. Same principle the docstring already states: data that
   feeds the prompt must shift the fingerprint.

---

## 6. Surfacing to the user (CLI first)

1. **`tm status`** — one line under ACWR:

   ```
   - Fitness    : CTL 62.4 | ATL 71.7 | TSB -8.9 | Ramp +4.2/wk
   ```

   Colorize TSB via a new `util.color_tsb()` using the science bands, coloring
   only the unambiguous ends: `< −30` red (excessive fatigue), `> +25` yellow
   (detraining risk), `+5..+25` green (race-ready) — the middle bands
   (−30..+5) stay uncolored because whether "productive overload" is good is
   phase-dependent, and that judgment belongs to the coach, not a color map.
   Ramp: `> 8` red, `5–7` yellow, else plain (green would wrongly bless a ramp
   during a taper).

2. **`tm data show-metrics`** — three new table columns and CSV fields next to the
   existing ACWR/Acute/Chronic ones (`cli/data.py`).

3. **Telegram / web** — nothing bespoke. The bot and web tab render what the
   shared status/summary code produces, and the user lives in CLI + Telegram;
   a PMC chart in the web UI is explicitly out of scope (§7).

---

## 7. Rollout & non-goals

**Rollout:** none needed beyond the idempotent `ALTER TABLE`. The next
`tm data pull` (or any path that calls `recompute_derived()`) back-populates PMC for
the entire cached history in one sweep. Old analysis caches refresh naturally
via the fingerprint change (§5.4).

**Out of scope, deliberately:**

- **Forward projection** — simulating CTL/ATL over *planned* workouts' planned
  load to answer "will TSB land in +5..+25 on event day?" deterministically.
  It's the natural phase 2 (feeds a single "projected event-day TSB: +12" line
  into plan/adapt prompts) but needs planned-TSS quality decisions that don't
  block shipping the descriptive half. Backward-looking PMC alone already fixes
  all four §1 failures.
- **Per-sport CTL splits** — the science file models one systemic load stream;
  splitting by sport is a different (and contested) model.
- **Replacing ACWR** — `training_load.txt` §3 is explicit that they are
  complementary guardrails; both stay.
- **Web PMC chart** — nice, not now.

---

## 8. Testing

- `compute_pmc()` unit tests (pure, no DB):
  - constant daily load L converges: CTL → L, ATL → L, TSB → 0 (ATL faster).
  - **TSB off-by-one pinned:** a single big day D raises ATL on D but TSB must
    only drop on D+1.
  - calendar gaps: a 10-day rest span decays both EWMAs (regression against a
    rows-only iteration bug).
  - seeding: first day's TSB is 0 (both EWMAs start equal).
  - time constants come from the params: same series under τ_ctl=τ_atl makes
    CTL≡ATL (TSB pinned at 0); a shorter τ_ctl converges faster.
- `format_metrics_history()` with and without PMC fields (None-guard).
- Ramp derivation at a span edge (< 7 days of history → ramp omitted, not
  garbage).
- One `recompute_derived()` integration test on a temp DB: activity-only days
  contribute load but create no metrics rows; existing rows get PMC values.
