# Design: PMC Fitness/Fatigue/Form (CTL · ATL · TSB)

**Status:** Proposed (rev. 5) · **Date:** 2026-07-04 · **Branch:** worktree-design-pmc-fitness-fatigue

> **Rev. 5 (2026-07-04):** §4.4 (`Mesocycle.phase`) promoted from a sketch to an
> end-to-end implementation spec — a single `MESO_PHASES` vocabulary source in
> `types.py`; the exact `phase` key added to the `_plan_generate_strategy` JSON
> schema (`engine.py` L409–418 — the prompt lives in `engine.py`, not `service.py`
> as rev. 3 implied); a pure `normalize_meso_phase()` parser with the keyword
> fallback map at the `service.py` L698 seam; the empty-phase diagnostic as a
> `print(dim(...))` line (the codebase has no logging framework); and `phase`
> threaded through `save_macrocycle`'s INSERT (`db/periodization.py` L280–285).
> Three factual corrections from a code-verification pass: `_warn_manual` is in
> `garmin.py` L836, not `status.py` (§3.3b); `get_active_objective()` is
> `db/objectives.py` L47–50, not `objectives.py` L48–49 (§5.3); the science file
> is `trainmate/science/training_load.txt` (§ intro).
>
> **Rev. 4 (2026-07-04):** second review pass, folded in. Correctness: the taper
> projection anchors to the **highest-priority** upcoming event (ties → nearest),
> decays a stale/missing anchor to today, and ignores removed workouts (§5.3); the
> `wipe → recompute` call moves to the **command layer** so it runs after the delete
> commits and avoids a db→garmin import cycle (§4); the warm-up accuracy caveat now
> also covers **re-warm-ups after a long gap**, not just the left edge (§3.3b); the
> weekly-digest ramp is suppressed when its −7d lookback lands in the warm-up zone
> (§5.4); ramp is computed from the **full** stored history, not the prompt slice
> (§3.1, §5.2). Honesty: the beginner caveat is now **conditional** rather than
> asserting fitness is understated (§3.3b). Cleanups: `CHRONIC_WEEKS` is deleted and
> computed inline from the two window params (§3.4); the `Mesocycle.phase` color
> falls back to the free-text focus and logs empty-phase rate (§4.4); a TSB lag
> footnote, corrected "~78% (not 'converged')" wording, the `hr_zone_coverage_min`
> config precedent, a single-source warm-up cutoff, and non-overlapping color bands
> (§3.3a, §3.4, §5.1, §6.1). A new-user "PMC is dark for ~6 weeks" limitation is
> stated in §7.
>
> **Rev. 3 (2026-07-04):** author decisions on the four rev. 2 open forks, folded
> in. (1) Warm-up: 42-day display blank **plus** a data-sufficiency caveat that
> warns the user and the LLM when today's values are still warming (§3.3). (2)
> Ramp reaches the coach as **one summary line**, not per-day (§5.2). (3) TSB
> coloring becomes **phase-aware via a new structured `Mesocycle.phase` enum**
> set at generation (§4.4, §6.1) — this pulls a slice of plan-generation into
> scope. (4) The taper is now **solved, not just anchored**: phase 1 ships a
> best-effort **forward projection** of event-day TSB over planned workouts'
> existing `tss`, with a warning when the plan doesn't yet reach the event
> (§5.3). Forward projection is therefore **no longer a non-goal** (§7).
>
> **Rev. 2 (2026-07-04):** folded in `DESIGN_pmc_fitness_fatigue_review.md`.
> Every review finding is resolved in the body below; §9 maps each finding to
> where. Load-bearing changes over rev. 1: warm-up suppression (§3.3), corrected
> None/NULL handling (§5.1, §6), a real fix for `wipe_garmin_data` staleness
> (§4), a wider derivation pad (§3.4).

`trainmate/science/training_load.txt` (f0bb707) documents the full Performance Management
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
- **Week-over-week progression is unguided.** The science caps CTL ramp rate
  (~3–5 sustainable, >8 red flag), but no ramp number exists anywhere — plans
  can climb unsustainably for weeks while ACWR stays in the sweet spot, the
  exact divergence `training_load.txt` §3 warns about.
- **Taper is blind.** The A-event taper directive targets a TSB band
  (+5..+25 on event day). Plan generation gets no CTL/ATL at all, so the LLM
  can neither anchor the taper to *current* freshness nor project where it
  lands on race day.
- **The user can't see fitness either.** `tm status` shows ACWR/acute/chronic
  but nothing answers "am I fitter than last month?" or "how fresh am I?" —
  the two questions the PMC exists for.

**What this design fixes, precisely.** Backward-looking PMC resolves the first,
second, and fourth bullets outright. For the taper it does two things: the
backward pass *anchors* it (the coach sees real current CTL/ATL/TSB and ramp
instead of guessing), and a deterministic **forward projection over the plan's
own workouts** (§5.3) computes event-day TSB directly — the exponential-decay
arithmetic the "app computes, LLM reasons" principle says not to hand the LLM.
So the taper is **projected, not merely anchored** — with one honest caveat: the
projection is only as good as the plan's per-workout `tss` estimates and assumes
rest for any not-yet-planned days, both surfaced explicitly (§5.3). Refining
that projection's *quality* (better planned-load estimation) is the remaining
phase-2 work; the projection itself ships now.

Everything needed is already in the DB: per-activity load via the existing
`activity_load()` fallback hierarchy (power TSS → hrTSS → sRPE), summed per day
in `recompute_derived()`. This design adds three EWMAs over that same series.

---

## 2. What exists today (touchpoints)

| Layer | Where | Today |
|---|---|---|
| Compute | `trainmate/garmin.py` `recompute_derived()` | full-sweep acute (7d sum), chronic (28d/4), ACWR per metrics day |
| Store | `athlete_metrics_cache` (`db/base.py`), `save_metric_cache()` (`db/activities.py`), `AthleteMetric` (`types.py`) | `acute_workload`, `chronic_workload`, `acwr` columns |
| Wipe | `wipe_garmin_data()` (`db/wipes.py`) | deletes rows by range; **does not** recompute (see §4) |
| Coach, per-day | `format_metrics_history()` (`coach/formatting.py` L44–50) → generate & adapt prompts (`engine.py`) | `... ACWR=1.12` per day line — **unguarded** `:.2f`, see §5.1 |
| Coach, summary | data summary in `coach/service.py` (~L96) → strategy/plan prompts | "Current ACWR: 1.12 (latest)" |
| Coach, weekly | analysis weekly digest (`service.py` ~L2279) | `max_acwr` per week |
| Cache key | evidence fingerprint (`engine.py` L336–338) | hashes 6-tuple incl. `m.get('acwr')` per metrics row |
| User | `tm status` (`cli/status.py` L141–145), `tm data show-metrics` table/CSV (`cli/data.py`), `color_acwr` (`util.py` L91) | ACWR + acute/chronic shown; `acwr or 0.0` zero-fill |
| Plan phase *(rev. 3)* | `Mesocycle` (`types.py` L130), plan generation (`coach/service.py`); active meso already loaded in `status.py` L70–84 | free-text `focus` only — **no structured phase** (see §4.4) |
| Planned load *(rev. 3)* | `Workout.tss` (`types.py` L65), `objectives.target_date` | planned per-workout TSS and the event date already exist, unused by any metric (fed to the §5.3 projection) |

Rows 1–8 each get a PMC counterpart — no new subsystem. The two *rev. 3* rows are
existing plan-side data the taper projection and phase-aware color newly *read*.

---

## 3. Computation (`recompute_derived()`)

The existing function already builds `daily_load: Dict[str, float]` (ISO-date
string → summed load) from all completed activities. Add one chronological pass
**over calendar days** — not over metrics rows, because rest days and row gaps
must decay the EWMAs with zero load.

### 3.1 The recurrence

```
span:  min(first activity date, first metrics date) … max(last activity, last metrics date)
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
- **Span end is `max(last activity, last metrics)`**, not the last metrics date.
  An activities-only pull (`pull(metrics=False)`) can leave trailing activity
  days past the last metrics row; those days carry load and *must* be walked so
  the EWMAs decay/climb through them — otherwise §3.1's "walk every calendar
  day" promise is broken exactly where a metrics-less pull left a tail.
- **Ramp rate is derived, never stored:** `ramp = ctl_d − ctl_{d-7}` computed
  where displayed (status line, coach summary, weekly digest). Storing it would
  just denormalize a subtraction. Interior-gap rule for the `d−7` lookup: use
  the nearest **earlier** in-memory CTL if the exact `d−7` day has no value; if
  fewer than 7 days of history precede `d`, **omit** the ramp (don't emit
  garbage). Because the pass walks *every* calendar day in memory (not just
  rows), a true interior gap in the CTL series is impossible — the fallback
  matters only at the young-DB left edge and at analysis-window slices (§5.4).
  **Ramp reads the full stored CTL series, never the windowed prompt slice.**
  The generate/adapt prompts carry only a short metrics window (15 days for
  generate, `history_days` for adapt), so computing ramp from those rows alone
  would wrongly omit it whenever the window is < 8 days even though the DB holds
  years of CTL. The single ramp line (§5.2) is derived from a full-history
  lookup at prompt-assembly time and injected alongside the slice.

### 3.2 Persistence

- **Pure helper**, unit-testable without a DB. Types are ISO-date strings
  throughout, matching the real `daily_load` and `save_metric_cache` code (rev. 1
  mixed `date` and `str` in prose vs signature — resolved to `str`):

  ```python
  def compute_pmc(
      daily_load: Dict[str, float],       # ISO date -> summed load
      start: str, end: str,               # ISO dates (span from §3.1)
      ctl_days: int, atl_days: int,
  ) -> Dict[str, Tuple[float, float, float]]:   # ISO date -> (ctl, atl, tsb)
  ```

- `recompute_derived()` calls it once and folds the result into its existing
  per-row `save_metric_cache()` call.
- **Upsert onto existing metrics rows only**, via the same `save_metric_cache()`
  COALESCE upsert already used for acute/chronic. Days with an activity but no
  Garmin metrics row get no row — the in-memory pass still walks them (their
  load feeds the EWMA), we just don't widen the cache's "one row per Garmin
  metrics day" meaning.
- **Refresh paths:** the only callers of `recompute_derived()` are `pull()`
  (`garmin.py` L551) and `backfill_tss()` (L683) — plus `wipe_garmin_data()`
  after this design (§4). (Rev. 1 wrongly named "zone-rehierarchy" as a caller;
  zone re-hierarchisation reaches `recompute_derived` only *via* `pull()`.)
- **Cost:** the sweep is already O(all days); this adds three multiplications
  per day. Nothing to optimize.

### 3.3 Warm-up handling (was the rev. 1 seeding hole)

Seeding both EWMAs at 0 at the start of DB history and walking forward is
correct math but produces **weeks of misleading numbers**, and — unlike
TrainingPeaks/intervals.icu, where a human eyeballs and discounts the left edge
of a chart — those numbers land in LLM prompts right next to hard directive
thresholds, with no way for the LLM to know they're artifacts. Worked example
(steady 60 load/day, three weeks in): ATL ≈ 58 (converged) but CTL ≈ 28 (still
climbing), so TSB ≈ −30 — tripping "excessive fatigue, insert recovery" — and
the ramp reads +7..+10/week, past the ">8 red flag", though nothing about the
training changed. The analyze/bootstrap paths hit this every time, since their
weekly digests start at the beginning of DB history.

Two-part handling, per author decision:

**(a) Blank the worst region.** Compute from day one as in §3.1 (stored values
are the seed for later days), but **suppress at every surface** — treat as
`None`, same omission convention as any other missing field — every
CTL/ATL/TSB/ramp whose date is within the first **`τ_ctl` days (default 42)** of
DB history. One cutoff date, `history_start + τ_ctl`, is computed by a **single
helper** (e.g. `pmc_warmup_cutoff()` beside `compute_pmc`, reading the earliest
metrics/activity date) and passed to the per-day lines (§5.1), the weekly digest
(§5.4), and the CLI (§6) — so the four surfaces can't each derive it four
slightly-different ways. This is a *display* rule: stored rows keep their
converging values, so a window starting past the cutoff reads warm numbers
without recompute.

**(b) Caveat + convergence % when *today itself* is still warming.** Blanking
the leading edge does nothing for a *young DB*, where even today's value is
warm-up quality. The EWMA converges as `1 − e^{−d/τ}` for `d` days of
**effective** history behind the latest value (63% at d=τ, 78% at 1.5τ, 86% at
2τ, 95% at 3τ). So when that effective history is short, compute the figure and
surface it — the author asked for the accuracy percentage, and it costs one
`exp`:

- **To the coach** — one line appended to the data summary (§5.2):
  `- PMC data caveat: CTL is based on N days of history (~X% converged). If the
  athlete trained regularly before {history_start}, true fitness is higher than
  shown and low TSB / high ramp are partly warm-up artifacts; if they did not,
  the low values are real.`  with `X = round(100 · (1 − e^{−N/τ_ctl}))`. The
  caveat states the *condition* rather than asserting understatement, because a
  genuine beginner's low CTL is correct, not an artifact (the direction is the
  LLM's to judge from the athlete's pre-DB history; only the magnitude is ours).
- **To the user** — a matching short `tm status` warning, and the `_warn_manual`
  baseline text (`garmin.py` L836 — a module function, not in `status.py`; the
  ACWR-mention `cold=False` branch, finding #4a) gains a PMC line so a young-DB
  user sees *why* freshness reads low.

Trigger the caveat while `N < 3·τ_ctl` (≈126 days, the ~95% mark); above that
the artifact is negligible and the line is dropped. The percentage is strictly
more useful than a bare "limited data" flag: it gives the LLM the rough
magnitude of the possible discount, leaving the *direction* to the LLM's
knowledge of the athlete's pre-DB history. It is a convergence proxy under a
constant-load assumption, **not** a literal error bar on a varying signal —
framed to the LLM as "~X% converged," which is exactly how a TrainingPeaks-style
chart's ramp-in is implicitly read. No manual starting-CTL machinery is needed;
the practical fix for a young DB remains a deeper Garmin backfill via
`tm data pull --start`.

**Re-warm-ups after a gap.** The warm-up problem is not unique to the start of
DB history: an EWMA re-warms from a low floor after *any* layoff long enough to
decay CTL back toward zero (injury, off-season, travel — a run of ≳ τ_ctl
near-zero-load days). Coming back, CTL climbs from that low floor again,
re-tripping low TSB / high ramp exactly as at DB start — yet this region is
neither inside the first τ_ctl days (so §3.3a won't blank it) nor short on
*total* history (so a naïve `N = today − history_start` won't flag it). So `N`
above is **effective** history: days since the most recent such gap, or since
history start if none. Detecting the gap is cheap — the calendar-day pass
already holds the full daily-load series, so a run of ≳ τ_ctl near-zero-load
days ending after `history_start` resets the effective-history clock. The
caveat (b) then fires on a comeback too; the §3.3(a) blank still only covers the
true left edge.

The §3.4 data pad is the complementary *data* backstop (it warms CTL by pulling
prior history); (a) is the *display* backstop for the leading edge; (b) is the
*honesty* backstop for when neither can help because the history simply isn't
long enough yet.

### 3.4 Configurable windows (ACWR + PMC) and the derivation pad

All four window/time-constant numbers become config params via the existing
`config.py` property + `config_template.yaml` pattern (like
`hr_zone_coverage_min`, already a `garmin:` param — *not* `rpe_divergence_ratio`,
which lives under `coach:`), under the **`garmin:`** section (they are computation
constants living beside the windows they replace; the coach never reads them
directly):

| Param | Default | Replaces |
|---|---|---|
| `acwr_acute_days` | 7 | `garmin.py` `ACUTE_WINDOW_DAYS` |
| `acwr_chronic_days` | 28 | `garmin.py` `CHRONIC_WINDOW_DAYS` |
| `pmc_ctl_days` | 42 | (new) CTL time constant |
| `pmc_atl_days` | 7 | (new) ATL time constant |

`CHRONIC_WEEKS` is **deleted, not merely derived.** It is exactly
`chronic_days / acute_days`, so both a stored constant (frozen at import from the
old hardcoded windows — a config edit then wouldn't move it) and a standalone
param (settable inconsistently with the windows, silently corrupting ACWR) are
wrong. Compute it inline where chronic is normalized —
`chronic = total_chronic / (config.acwr_chronic_days / config.acwr_acute_days)` —
reading the live params every sweep, so it can never drift from the windows it is
defined by. The `garmin.py` header comment "Standard constants, not tunables"
(L24) becomes false and must be rewritten to say these are config-backed with the
calibration caveat below. Ramp rate stays a fixed 7-day delta — "per week" is its
definition, not a tunable window.

**Derivation pad (finding #4).** Rev. 1's `DERIVATION_PAD_DAYS = chronic` is
under-padded two ways:

1. **CTL needs ~1.5 × τ_ctl ≈ 63 days** of prior data to reach ~78% convergence
   at the left edge of a displayed window (full 95% takes 3·τ_ctl ≈ 126 days; the
   63-day pad is a deliberate cost/coverage tradeoff, with the §3.3(b) accuracy
   caveat carrying the residual — *not* "converged" as rev. 1 claimed); a 28-day
   pad neither pulls nor warms enough history even for that.
2. Making `acwr_chronic_days` configurable while pad = chronic is a latent
   corruption: set chronic to 14 and the pad drops **below the hardcoded 28-day
   baseline lookback** in `recompute_derived()` (`for d in range(1, 29)`,
   L619), silently mis-deriving baselines near window edges.

Fix: `DERIVATION_PAD_DAYS = max(acwr_chronic_days, 28, ceil(1.5 * pmc_ctl_days))`
(= 63 at defaults). The literal `28` floor pins it to the baseline lookback
regardless of a shrunk chronic window; the `1.5 × τ_ctl` term warms CTL.
(Warm-up suppression in §3.3 is the *display* backstop; the pad is the *data*
backstop — they are complementary, and the pad alone can't cover a DB whose
entire history is younger than the pad.)

**Config-change staleness (accepted, documented).** The rewriting sweep only
runs on the next path that calls `recompute_derived()` — i.e. the next `pull()`
/ `backfill_tss()` / wipe. Between editing a τ and that call, every displayed
PMC/ACWR value silently reflects the *old* constants. This matches how every
other derived value already behaves (nothing recomputes on config edit alone);
we accept it and note it beside the params rather than adding an eager-recompute
hook.

**Calibration caveat (unchanged, still load-bearing).** The science file's text
and every interpretation band (ACWR 0.8–1.3, TSB −30/+25, ramp 3–5/8, and
`color_acwr`/`color_tsb`) are calibrated to the defaults. Non-default constants
change what the numbers *mean* while the prompts and colors keep judging them
against the standard bands. These params are for deliberate experimentation, not
casual tuning; **the defaults are the supported configuration.**

No migration on constant change: the next full sweep rewrites every stored value
under the new constants, and the fingerprint shift (§5.5) refreshes cached
analyses.

---

## 4. Storage & wipe

- `db/base.py`: `ALTER TABLE athlete_metrics_cache ADD COLUMN {ctl,atl,tsb} REAL`
  using the same `try/except sqlite3.OperationalError` idempotent-migration
  pattern already used for the `completed_activities` zone columns. (The
  `CREATE TABLE IF NOT EXISTS` also gains the columns for fresh DBs.)
- `types.py` `AthleteMetric`: `ctl: Optional[float]`, `atl: Optional[float]`,
  `tsb: Optional[float]`.
- `db/activities.py` `save_metric_cache()`: three new optional params with the
  same `COALESCE(excluded.x, cache.x)` semantics as `acute_workload`, so a
  metrics-only Garmin save never nulls out previously-computed PMC values.
- **A wipe must be followed by `recompute_derived()` — at the command layer, not
  inside the db method (finding #5).** `wipe_garmin_data()` deletes
  activity/metrics rows by range and *never* recomputes today. For ACWR the
  staleness is bounded (flat 28-day window: only ≤28 days after the wiped range
  read stale values until the next pull). An EWMA has no such bound: deleted load
  stays baked into `ctl_{d}`/`atl_{d}` for **every** subsequent day, forever,
  until some future pull sweeps. This existing latent ACWR issue becomes
  unbounded under PMC, so it must be fixed now. **Where the recompute goes
  matters.** `recompute_derived()` lives in `garmin.py`, which imports `db`
  (`garmin.py` L19); a module-level call from `db/wipes.py` would be a circular
  import. And it opens its own connection, so calling it *inside* the wipe's
  `with self._get_connection()` block (before `conn.commit()`, `wipes.py` L77)
  either won't see the uncommitted deletes or hits SQLite `database is locked`.
  So the recompute belongs one level up, at the command layer that already
  imports both: `cli/data.py` L128 does `db.wipe_garmin_data(...)`, and calls
  `garmin.recompute_derived()` immediately after — once the wipe has returned and
  its transaction committed. The `wipe_metrics()` wrapper (`db/wipes.py` L99) and
  any other wipe entry point follow the same "wipe, then recompute" rule. Cost is
  one extra full sweep per wipe — wipes are rare and manual; acceptable.

### 4.4 `Mesocycle.phase` (structured phase, for phase-aware TSB color)

TSB `+5..+25` means "race-ready" only in a peak/taper block; mid-build the same
value means fitness is *decaying*. A CLI color map has no phase context, so
rev. 2 dropped the green band entirely. Rev. 3 instead gives the color real
context via a structured phase (author decision), which is independently
reusable (plan display, future coach signals). **This is the largest new surface
in the design** — it reaches into plan generation — and could reasonably be
split into its own follow-up PR; specified end-to-end below so it needs no
further design. Five touchpoints, in dependency order.

**Vocabulary — one source.** A frozen tuple
`MESO_PHASES = ("base", "build", "peak", "taper", "recovery")` defined once in
`types.py` beside `Mesocycle`, imported by both the parser (step 3) and
`color_tsb` (§6.1), so the writer's enum and the reader's enum can never drift.

**(1) Type & storage.**
- `types.py` `Mesocycle` (a `TypedDict`, L130–138): add `phase: Optional[str]`.
  `Optional` because pre-existing plans and bootstrap-inferred blocks predate it.
- `db/base.py`: idempotent `ALTER TABLE mesocycles ADD COLUMN phase TEXT` in the
  same `try/except sqlite3.OperationalError` block used for §4's metrics columns;
  the `mesocycles` `CREATE TABLE IF NOT EXISTS` (L463–474) gains `phase TEXT` for
  fresh DBs.

**(2) Generation — prompt & schema (`engine.py` `_plan_generate_strategy`, NOT
`service.py`).** The LLM already emits each mesocycle as a JSON object with
`name`/`start_date`/`end_date`/`focus` — the response-schema block at L409–418.
Add a fifth key so the model classifies the block as it writes it:

```
  "phase": "one of: base | build | peak | taper | recovery"
```

The `name`/`focus` text already carries phase language ("Base Building",
"Peak & Taper"); `phase` just pins it to the fixed vocabulary. No other prompt
change is needed — the L432+ system prompt already frames the model as designing
periodized blocks.

**(3) Parse & normalize (`service.py`, the `mesocycles = macro_data.get("mesocycles", [])`
seam at L698).** Today the raw LLM dicts flow straight into `save_macrocycle`
(L702) with no per-field validation. Insert one normalization pass *between* L698
and that call, running every block through a **pure helper** (unit-testable, no
DB):

```python
def normalize_meso_phase(raw_phase: Optional[str], focus: str) -> Optional[str]:
    """LLM phase → validated vocabulary; else infer from focus text; else None."""
    if raw_phase and raw_phase.strip().lower() in MESO_PHASES:
        return raw_phase.strip().lower()
    text = f"{raw_phase or ''} {focus}".lower()
    for kw, phase in (              # most specific first; first hit wins
        ("taper", "taper"), ("race", "taper"), ("peak", "peak"),
        ("deload", "recovery"), ("recover", "recovery"), ("rest", "recovery"),
        ("base", "base"), ("aerobic", "base"),
        ("build", "build"), ("progress", "build"),
    ):
        if kw in text:
            return phase
    return None                     # neither the field nor the focus classified it
```

Applied as `meso["phase"] = normalize_meso_phase(meso.get("phase"), meso["focus"])`
over each block before the L702 `save_macrocycle`. Any other path that re-persists
mesocycles (regeneration) runs the same pass.

**(4) Empty-phase diagnostic.** The payoff of this surface is the green
race-ready color (§6.1), which fires *only* in `peak`/`taper`: if the model never
emits a clean value **and** the focus-inference whiffs, the surface silently never
lights up. The codebase has **no logging framework** (diagnostics are `print()` +
color helpers), so emit a dim one-liner at generation time —
`print(dim(f"phase: {n_none}/{len(mesocycles)} blocks unclassified"))` where
`n_none` counts the `None` results from step 3 — so a persistent non-zero rate is
visible in normal use and tells us whether to harden the prompt.

**(5) Persistence (`db/periodization.py` `save_macrocycle`, the mesocycle INSERT
at L280–285).** Add `phase` to the column list and bind `meso.get("phase")`:

```python
INSERT INTO mesocycles (macrocycle_id, name, start_date, end_date, focus, phase)
VALUES (?, ?, ?, ?, ?, ?)
```

with `meso.get("phase")` appended to the params tuple. Nothing else in the write
path changes.

**Read-back & consumption.** `get_mesocycles_for_macrocycle` and
`get_active_mesocycle` already `return dict(row)` (`periodization.py` L114, L179),
so once the column exists `phase` reaches callers unchanged. `status.py` already
loads the active mesocycle (L70–77); it passes `active_meso.get('phase')` — use
`.get`, so a meso dict assembled without the key can't `KeyError`; §6.1's prose
sketch of `active_meso['phase']` should read `.get('phase')` to match — into
`color_tsb` (§6.1), the field's only consumer.

**Out of scope for phase.** Bootstrap reverse-engineering emits
`inferred_mesocycles` (`engine.py` L1008, consumed at `service.py` L198) as
reconstruction *context*, not via the plan-generation write path; those get no
phase and degrade to `phase = NULL` → phase-blind color. Only forward plan
generation classifies blocks.

Because the field is additive and NULL-tolerant end-to-end, it needs no
migration and imposes no ordering dependency on the rest of the design: plans
generated before it simply carry `phase = NULL` until regenerated, and the color
degrades to phase-blind (two ends only). Graceful degradation, no backfill.

---

## 5. Surfacing to the coach

No prompt-instruction changes are needed — the directives already ride in via
`_load_science_guidelines()`; the values just have to show up next to them
using the same vocabulary (`CTL`/`ATL`/`TSB`, which the science file defines).

### 5.1 Per-day history — and fixing the pre-existing NULL crash

`format_metrics_history()` (`coach/formatting.py` L49–50) today does
`f"...ACWR={m['acwr']:.2f}"` **with no guard**. That is not "ACWR is
implicitly omitted when absent" as rev. 1 claimed — a NULL `acwr` **crashes**
with `TypeError` (`:.2f` on `None`), and a NULL-acwr row *is* reachable: a pull
that dies between `_ingest_metrics` and the end-of-pull `recompute_derived()`
leaves raw metrics rows with derived columns still NULL. (The line also renders
`RHR=Nonebpm` today when RHR is missing.) Guarding only the three new PMC fields
would leave that crash in place.

So adopt **one None convention for the whole line**: build it field-by-field and
**omit any field whose value is `None`** (RHR, HRV, ACWR, and the new
CTL/ATL/TSB alike). Concretely the line becomes, for a fully-populated,
past-warm-up row:

```
- 2026-07-02: RHR=52bpm, HRV=61ms, Sleep=78, Stress=31, ACWR=1.12, CTL=62.4, ATL=71.7, TSB=-8.9
```

- The three PMC fields are omitted wholesale during the §3.3 warm-up window and
  for any pre-recompute NULL row.
- **TSB won't equal the shown CTL − ATL.** Per §3.1, `TSB = CTL(yesterday) −
  ATL(yesterday)`, but the line shows *today's* CTL/ATL — so in the example
  `62.4 − 71.7 = −9.3 ≠ −8.9`. This is correct (matching TrainingPeaks' lag),
  but reads as an arithmetic error to a human or the LLM, so a one-line footnote
  states the lag wherever the triple is surfaced (per-day block, summary,
  `tm status`).
- **Ramp is *not* on the per-day line.** Ramp is a slow-moving weekly figure;
  stamping it on all ~30 daily lines is repetition the LLM must wade through.
  Per author decision it reaches the coach as a **single line** instead (§5.2) —
  including in the generate/adapt context, which is what finding #3 requires.

This reaches the workout generate and adapt prompts, so adapt now sees the
fatigue trajectory (fixing failure #1).

### 5.2 Summary lines (PMC + the single ramp line)

Two summary lines, added after the existing ACWR line:

```
- Fitness/Fatigue (PMC): CTL 62.4 (fitness), ATL 71.7 (fatigue), TSB -8.9 (form)
- CTL ramp rate: +4.2/week (last 7 days)
```

- The **PMC line** goes into the data summary (`service.py` ~L96) that feeds the
  strategy/plan prompts, so plan generation can reason about sustainable build
  rates and current freshness.
- The **ramp line** is the single-line form of finding #3 (author decision).
  Ramp must reach the prompts that actually set weekly TSS — generate
  (`engine.py` L557) and adapt (L792) — *and* the strategy/plan prompts. So the
  same one-liner is emitted into **both** contexts: once in the data summary,
  and once in the generate/adapt metrics context (a single line beside the
  per-day block, not repeated per day). It is computed from the **full stored
  CTL series** (§3.1), independent of the short prompt window (15 days for
  generate, `history_days` for adapt), so a small window never spuriously drops
  it. One number, stated once per prompt, right where next week's load is
  decided — no 30× repetition, no seven-line mental subtraction.
- The §3.3(b) **warm-up caveat line** (with the convergence %) appends here too
  when today's values are still warming, so every prompt that reads PMC also
  reads how much to trust it.

Ramp obeys the §3.1 lookup/omit rule and is omitted entirely inside the §3.3(a)
warm-up window.

### 5.3 Forward taper projection (the taper solve)

Backward PMC anchors the taper to today's real freshness; this closes the loop
by projecting **event-day TSB** deterministically — the exponential-decay
arithmetic the "app computes" principle says the LLM should not do in its head.
Author decision: ship best-effort full projection now (not the zero-training
bound), with a warning when the plan doesn't yet reach the event.

Computed **on demand at prompt-assembly time** (`coach/service.py`), **not** in
`recompute_derived()` and **not stored**: it depends on the plan and the event
date and is forward-looking, so it must stay out of the pure, plan-independent
backward pass (which remains Garmin-only). This is the one place PMC reads the
plan, and it reads it read-only.

**Event selection.** The projection anchors to the athlete's **highest-priority
upcoming active objective**, ties broken by the nearest `target_date` (author
decision). This is *not* what `get_active_objective()` returns today — it sorts
`target_date ASC LIMIT 1`, i.e. soonest-regardless-of-priority
(`db/objectives.py` L47–50) — so the projection needs a priority-first selection
(`ORDER BY priority DESC, target_date ASC LIMIT 1` over `status='active' AND
target_date >= today`). There is no separate "A-event" tier in the schema
(`Objective` carries only `priority`), so the `+5..+25` band is labeled as the
coach's peak/taper target *for the chosen event*, meaningful when that event is
being tapered for — not as an unconditional "A-event" gate.

Inputs: the latest metrics row's CTL/ATL as the anchor, the chosen objective's
`target_date`, and planned `Workout.tss` for the days anchor→event. Walk §3.1's
recurrence forward over planned daily load (sum of that day's planned `tss` for
**non-removed** workouts; rest days = 0). Emit into the plan/adapt prompts:

```
- Projected event-day TSB (from current plan): +12   [taper target band +5..+25]
```

Honesty guards (the author's "warn if not all workouts are planned yet", plus
the anchor-quality guards a projected number needs):

- **Stale anchor.** The latest metrics row may be several days old (recent days
  unpulled or activity-only). Decay CTL/ATL forward from the anchor date to today
  over actual daily load *before* projecting the plan, so "from today" really
  starts at today — don't silently start the walk from last week's values.
- **Warm-up / missing anchor.** If the anchor row is inside the §3.3 warm-up
  window or its CTL/ATL is NULL, the projection inherits warm-up-grade
  uncertainty: carry the §3.3(b) accuracy caveat onto the projection line (or
  suppress the line entirely when the anchor is NULL), rather than printing a
  confident number over a shaky start.
- **Removed workouts.** A planned workout can be marked `removed = True`
  (`Workout`, `types.py` L66); its `tss` must **not** count toward projected
  load, or a cancelled session inflates the projection. Filter removed workouts
  (and honor `original_date` for moved ones) when summing planned daily load.
- **Plan doesn't reach the event.** If the last planned workout precedes the
  event, the tail days are assumed zero-load, which inflates projected TSB. The
  line self-annotates: `(note: last N days before the event are unplanned;
  assumes rest — realized TSB likely lower)`.
- **Unquantified workouts.** Planned workouts with `tss = None` (manual/legacy)
  count as zero load; if any fall in range, append `(M planned workouts lack a
  TSS estimate, counted as 0)` so the LLM discounts accordingly.

This upgrades the taper from *anchored* (rev. 2) to *projected*: the LLM reads a
concrete number against the +5..+25 directive instead of simulating two decays.
Accuracy is bounded by the plan's own TSS estimates — hence "best-effort,"
surfaced honestly rather than silently. Improving that estimate quality is the
only taper work left for phase 2.

### 5.4 Weekly digest (analysis, `service.py` ~L2279)

Alongside `max_acwr` add per week: `end_ctl` (CTL on the week's last day),
`week_ramp` (`end_ctl` − CTL 7 days earlier), `min_tsb`. Three numbers give the
analyze pass the multi-week fitness trajectory and each week's overload depth
without per-day noise.

Two guards (findings #1 and the boundary smaller-finding):

- **Warm-up:** weeks entirely inside the §3.3 warm-up window emit `None` for all
  three (the digest formatter omits them) — otherwise every bootstrap narrates a
  phantom overreach block from seeding artifacts. **Straddle guard:** for a week
  that only *partly* clears the cutoff, `end_ctl`/`min_tsb` may show but
  `week_ramp` is still suppressed whenever its `−7d` lookback lands *before* the
  warm-up cutoff — otherwise the first shown week reports a huge ramp measured
  against a suppressed (warm-up) baseline.
- **Week boundary:** `week_ramp` reaches into the *previous* week's slice for the
  `−7d` CTL. The first week of an analysis window has no prior week → `week_ramp`
  is omitted (None-guarded), and it is exactly where the warm-up artifacts would
  otherwise live, so the two guards reinforce.

### 5.5 Evidence fingerprint (`engine.py` `met_digest`, L336–338)

Add **all three** of `m.get('ctl')`, `m.get('atl')`, `m.get('tsb')` to the
per-row tuple (rev. 1 hashed only `tsb`). The prompt now carries all three per
day; hashing all three costs nothing and honors the docstring's stated rule that
data feeding the prompt must shift the fingerprint. This makes the first
post-deploy recompute (rows gaining PMC values) invalidate cached analyses so
the new signal reaches reconstructions without a manual `--force`.

**Accepted double-invalidation (smaller finding).** Widening the tuple from 6 to
9 fields shifts every fingerprint at deploy (values still NULL), and the first
recompute shifts them again (NULL → real). On the normal path both collapse into
a single refresh (deploy and first pull are effectively the same event); only a
`--no-pull` analysis run *between* deploy and first pull pays for two LLM
re-runs. That window is small and self-healing; not worth guarding.

---

## 6. Surfacing to the user (CLI first)

### 6.1 `tm status`

One line under ACWR (`cli/status.py`, near L141–145):

```
- Fitness    : CTL 62.4 | ATL 71.7 | TSB -8.9 | Ramp +4.2/wk
```

- **Never zero-fill (finding #7).** The ACWR line uses `last_metrics['acwr'] or
  0.0`; copying that for PMC would print `CTL 0.0 | ATL 0.0 | TSB 0.0` after a
  fresh deploy on `--no-pull`, and **`TSB 0.0` reads as a meaningful neutral
  "perfectly balanced" reading, not as missing data** — actively misleading.
  When any of CTL/ATL/TSB is NULL or the latest row is inside the §3.3 warm-up
  window, render `—` for that field (or omit the whole line if all three are
  absent). Same NULL/dash rule for the ramp, plus the §3.1 interior-gap /
  young-DB omit rule.
- **`color_tsb(tsb, phase)` (new, `util.py` beside `color_acwr` L91) is
  phase-aware (author decision).** The two risk ends color regardless of phase:
  `< −30` red (excessive fatigue), `> +25` yellow (detraining / over-tapered).
  The `+5..+25` band renders **green only when `phase in {peak, taper}`** —
  where freshness *is* the goal; in any other phase, and when `phase is None`
  (old plans, or no active mesocycle), it stays **uncolored**. This resolves
  finding #8's real objection: mid-build a TSB of +15 means fitness is
  *decaying* (the science file's "resume building" case), so green there would
  wrongly read as "you're great." Rev. 2 solved this by dropping green outright;
  rev. 3 keeps the race-ready cue but only where it is correct, using the
  structured `Mesocycle.phase` (§4.4) that `status.py` already has to hand.
  `status.py` passes `active_meso['phase'] if active_meso else None`. The
  `−30..+5` band stays uncolored in all phases (its meaning is phase-dependent
  and belongs to the coach). **Bands are half-open so no value is double-claimed**
  (rev. 1's `+5..+25` and `−30..+5` both claimed +5): `< −30` red,
  `−30 ≤ tsb < +5` uncolored, `+5 ≤ tsb ≤ +25` green (peak/taper only),
  `> +25` yellow.
- **Ramp bands must touch (finding #8):** `≥ 8` red, `5–8` yellow (i.e.
  `5 ≤ ramp < 8`), else plain. Rev. 1's "`>8` red, `5–7` yellow" left 7.5
  rendering plain between a yellow 6.0 and a red 9.0. No green band: a low ramp
  is correct during a taper, so green would wrongly bless it.
- **TSB lag footnote (§5.1).** `TSB = CTL(yesterday) − ATL(yesterday)`, so the
  printed `CTL | ATL | TSB` triple won't subtract to the shown TSB; a dim
  one-line footnote under the Fitness line states this so the user doesn't read
  it as a bug.

### 6.2 `tm data show-metrics`

Three new table columns and CSV fields next to the existing ACWR/Acute/Chronic
ones (`cli/data.py`). Same NULL → blank/`—` and warm-up omission as the status
line; the CSV emits empty cells (not `0`) for suppressed/NULL values so
downstream parsing doesn't read a zero as data.

### 6.3 Telegram / web

Nothing bespoke. The bot and web tab render what the shared status/summary code
produces, and the user lives in CLI + Telegram; a PMC chart in the web UI is
explicitly out of scope (§7).

---

## 7. Rollout & non-goals

**Rollout:**

- Idempotent `ALTER TABLE` on `athlete_metrics_cache` (§4) and `mesocycles`
  (§4.4) — no data migration; both NULL-tolerant on old rows.
- The next `tm data pull` (or any `recompute_derived()` path) back-populates PMC
  for the entire cached history in one sweep. Old analysis caches refresh
  naturally via the fingerprint change (§5.5).
- **Plan generation** gains the `phase` field (§4.4); the forward projection
  (§5.3) reads whatever plan exists — both degrade gracefully on plans that
  predate this design (no `phase`, no projection until a plan with an event
  exists). Regenerating a plan populates `phase`.
- **Docs:** update `ARCHITECTURE.md` (AGENTS.md L2–3 requires it) — the
  derived-metrics `garmin.py` row (~L132, add CTL/ATL/TSB to "the load model"),
  the wipe/analysis-cache note (~L478, `wipe_garmin_data` now recomputes), and
  the plan-generation flow (new `Mesocycle.phase`, forward projection reads the
  plan). Note the four new config params where `config_template.yaml` is
  documented.

**Sequencing note.** The descriptive core (§3–§4.3, §5.1–5.2, §5.4–5.5, §6.1–6.2
minus the green band) is self-contained and can land first. The two rev. 3
additions each have a clean seam and could be separate PRs if preferred: the
`Mesocycle.phase` enum + phase-aware green (§4.4, §6.1) touches plan generation;
the forward projection (§5.3) is the only plan-coupled piece of the compute
path. Neither blocks the core.

**Known limitation — PMC is dark for the first ~6 weeks of a fresh install.**
CTL is a 42-day EWMA, so it needs months of history to be trustworthy. Cold-start
backfill defaults to 90 days (`garmin_initial_backfill_days`); §3.3(a) blanks the
first 42, and the §3.3(b) caveat runs to ~126 days. So a new user sees **no** PMC
for ~6 weeks, then caveated (63–78% converged) numbers for weeks more, and never
reaches the un-caveated 126-day mark within the default backfill — whereas ACWR is
trustworthy at 28 days. This is inherent to the model, not a bug; stated here so
new-user emptiness is expected. The only real remedy is a deeper Garmin backfill
(`tm data pull --start`), itself bounded by Garmin's own retention.

**Out of scope, deliberately:**

- **Forward-projection *quality*** — §5.3 ships the projection itself, but its
  accuracy is bounded by the plan's per-workout `tss` estimates and by how far
  the plan extends toward the event. Better planned-load estimation (and filling
  unplanned tail days with a model instead of assuming rest) is the phase-2
  refinement; the honest best-effort version ships now.
- **Per-sport CTL splits** — the science file models one systemic load stream;
  splitting by sport is a different (and contested) model.
- **Replacing ACWR** — `training_load.txt` §3 is explicit that they are
  complementary guardrails; both stay.
- **Web PMC chart** — nice, not now.
- **Eager recompute on config edit** — §3.4; accepted staleness instead.

---

## 8. Testing

`compute_pmc()` unit tests (pure, no DB):

- constant daily load L converges: CTL → L, ATL → L, TSB → 0 (ATL faster).
- **TSB off-by-one pinned:** a single big day D raises ATL on D but TSB must
  only drop on D+1.
- **calendar gaps:** a 10-day rest span decays both EWMAs (regression against a
  rows-only iteration bug).
- **seeding:** first day's TSB is 0 (both EWMAs start equal).
- **time constants come from the params:** same series under τ_ctl=τ_atl makes
  CTL≡ATL (TSB pinned at 0); a shorter τ_ctl converges faster.
- **empty DB / degenerate span:** no activities and no metrics → `min()`/`max()`
  over no dates must not throw; `compute_pmc` returns `{}`.

Formatting / surfacing:

- `format_metrics_history()` with and without PMC fields, **and with a NULL
  ACWR/RHR row** (regression for the pre-existing `:.2f` crash and `Nonebpm`) —
  every None field omitted, no exception.
- **Warm-up (a) suppression:** rows inside the first τ_ctl days emit no PMC
  fields in the per-day line, weekly digest, and status line.
- **Warm-up (b) convergence caveat:** for a short-history DB the caveat line is
  emitted with the right `X`; e.g. N=τ_ctl → ~63%, N=1.5·τ_ctl → ~78%; N≥3·τ_ctl
  → line dropped. Pin the arithmetic so a τ_ctl change moves X.
- **Ramp single line:** ramp appears once in the generate/adapt context and once
  in the data summary, never per-day; omitted inside warm-up and at a span edge
  (<7 days history), nearest-earlier fallback at an interior d−7 gap.
- **Status-line NULL rendering:** NULL/warm-up CTL/ATL/TSB render `—`, never
  `0.0`.
- **Color bands touch:** the ramp bands have no uncolored hole (ramp 7.5 yellow,
  ≥8 red). **Phase-aware `color_tsb`:** +12 renders green under phase `taper`,
  uncolored under `build` and under `phase=None`; −31 red and +26 yellow in
  every phase.

Forward projection (§5.3, pure helper + service wiring):

- decay-only (no planned workouts in range) reproduces the closed-form
  `ctl·(1−1/τ)^d` decay to event day.
- planned load raises the projection above decay-only; `tss=None` workouts
  contribute 0 and trigger the "M lack a TSS estimate" annotation.
- plan ending before the event triggers the "last N days unplanned" annotation
  and the tail is treated as rest.
- no active future objective / no plan → no projection line emitted (not a
  crash); with several, the highest-priority upcoming one is chosen (ties →
  nearest).

DB integration (temp DB):

- one `recompute_derived()` run: activity-only days contribute load but create
  no metrics rows; existing rows get PMC values.
- **COALESCE regression:** a metrics-only re-pull (no activity change) must
  **not** null previously-stored PMC values — the whole persistence story (§4)
  relies on this and nothing else pins it.
- **wipe-then-recompute:** after a dated wipe *followed by the command-layer
  `recompute_derived()`*, no surviving row past the wiped range still carries
  load-through-the-gap in its CTL/ATL (finding #5 regression). The recompute runs
  after the wipe's transaction commits (no lock, sees the deletes).

Rev. 4 additions:

- **Event selection:** two active future objectives, the lower-priority one
  sooner → the projection anchors to the higher-priority one; equal priority →
  the nearer date wins.
- **Projection anchor:** a stale latest-metrics row decays to today before the
  plan walk; a `removed=True` workout in range does not raise projected load; a
  warm-up-window anchor carries the accuracy caveat, and a NULL anchor suppresses
  the line rather than crashing.
- **Digest straddle:** a week whose `−7d` CTL lookback lands before the warm-up
  cutoff omits `week_ramp` while still showing `end_ctl`/`min_tsb`.
- **Effective-history caveat:** a DB with a ≳ τ_ctl near-zero-load gap re-arms the
  convergence caveat after the gap even though total history is long.
- **Ramp from full history:** an 8-day prompt window still emits the ramp line
  (computed from the full stored series), not omitted.
- **`CHRONIC_WEEKS` inline:** changing `acwr_acute_days`/`acwr_chronic_days`
  moves the normalized chronic value in the same sweep (no frozen 4.0).

---

## 9. Review resolutions

Traceability against `DESIGN_pmc_fitness_fatigue_review.md`. Every blocking and
structural finding is resolved in the body above.

Rows marked **(rev. 3)** reflect an author decision that changed the rev. 2
resolution; see the header note for the four decisions.

| # | Finding | Resolution |
|---|---|---|
| 1 | Zero-seeded EWMAs feed ~6 weeks of garbage to the LLM | §3.3(a) blanks the first τ_ctl days at every surface; **(rev. 3)** §3.3(b) adds a convergence-% caveat to the coach + user when today's own values are still warming; `_warn_manual` mentions it |
| 2 | "Fixes all four failures" overclaims the taper | **(rev. 3)** taper is now *solved*: §5.3 ships best-effort forward projection of event-day TSB over planned `tss`, with unplanned-tail / missing-TSS warnings; only projection *quality* is left to phase 2 (§7) |
| 3 | Ramp never reaches generate/adapt prompts | **(rev. 3)** §5.2 emits ramp as a single line into both the generate/adapt context (reaches generate L557 & adapt L792) and the data summary — not per-day |
| 4 | `DERIVATION_PAD_DAYS = chronic` under-padded twice | §3.4 pad = `max(chronic, 28, ceil(1.5·τ_ctl))`; 4a warm-up note added to `_warn_manual` |
| 5 | `wipe_garmin_data()` never recomputes → unbounded EWMA staleness | §4 adds `recompute_derived()` to the wipe; test in §8 |
| 6 | None-guard claim misread the code (NULL acwr *crashes*) | §5.1 corrects it; one omit-if-None convention for the whole line (acwr, rhr included); test in §8 |
| 7 | Status NULL/zero-fill lies (`TSB 0.0` looks neutral) | §6.1 renders `—`, never `0.0`; interior-gap ramp rule in §3.1; §6.2 CSV emits blanks |
| 8 | Color band hole + green contradicts own reasoning | §6.1 ramp bands touch (`5–8` yellow, `≥8` red); **(rev. 3)** green +5..+25 kept but gated to `phase ∈ {peak, taper}` via the new structured `Mesocycle.phase` (§4.4), uncolored otherwise |
| — | Double cache invalidation; hash all three | §5.5 hashes ctl/atl/tsb; two-refresh window accepted & documented |
| — | Config-change staleness; `garmin:` vs `coach:` | §3.4 params under `garmin:`, staleness accepted & documented; "not tunables" comment to be rewritten |
| — | Span end contradicts §3's own text | §3.1 span ends at `max(last activity, last metrics)` |
| — | Missing §4 header (dangling storage bullets) | Storage promoted to its own §4 |
| — | Type inconsistencies (`date` vs `str`) | §3.2 signature is ISO `str` throughout |
| — | "zone-rehierarchy funnels through recompute_derived" | §3.2 names real callers: `pull()`, `backfill_tss()` |
| — | ARCHITECTURE.md absent from rollout | §7 adds it (rows ~L132 and ~L478, plus plan-generation flow) |
| — | Weekly-digest ramp crosses week boundaries | §5.4 None-guards the first week; reinforced by warm-up guard |
| — | Missing tests (empty DB, COALESCE, d−7 gap, NULL status) | all added in §8, plus rev. 3 projection / phase-color / caveat tests |

Rows below are the **rev. 4** second-review pass (all folded into the body above):

| # | Finding | Resolution |
|---|---|---|
| R4 | Taper anchored to soonest event, not highest-priority | §5.3 selects `priority DESC, target_date ASC`; ties → nearest; no schema "A-event" |
| R4 | `wipe → recompute` import cycle + uncommitted-read/lock | §4 moves the recompute to the command layer, run after the wipe commits |
| R4 | Warm-up unguarded after a long training gap (comeback) | §3.3(b) uses *effective* history; caveat re-arms on comebacks |
| R4 | Beginner: caveat wrongly asserts fitness is understated | §3.3(b) reworded conditional on pre-DB training history |
| R4 | Projection: warm-up/stale anchor + removed workouts | §5.3 adds stale-anchor decay, warm-up caveat, removed-filter |
| R4 | Digest straddle week ramps off a warm-up baseline | §5.4 suppresses `week_ramp` when its −7d lands pre-cutoff |
| R4 | Ramp dropped when the prompt window is < 8 days | §3.1/§5.2 compute ramp from the full stored CTL series |
| R4 | `1.5·τ_ctl` mislabeled "converged" (really ~78%) | §3.4 states ~78%; the §3.3(b) accuracy caveat carries the residual |
| R4 | TSB ≠ shown CTL − ATL reads as a bug | §5.1/§6.1 add a one-line lag footnote |
| R4 | `garmin:` precedent cited a `coach:` param | §3.4 cites `hr_zone_coverage_min` |
| R4 | `Mesocycle.phase` green fails on any non-enum value | §4.4 falls back to focus-text inference + logs empty-phase rate |
| R4 | TSB color band overlap at +5 | §6.1 bands are half-open, each value owned once |
| R4 | `CHRONIC_WEEKS` frozen at import vs configurable windows | §3.4 deletes it; computed inline from the two window params |
| R4 | Warm-up cutoff computed per-surface (four ways) | §3.3(a) one helper computes it, passed to all four surfaces |
