# Design: PMC Fitness/Fatigue/Form (CTL · ATL · TSB)

**Status:** Shipped (rev. 7, merged to main in `ca8591b`) · **Date:** 2026-07-07 · **Branch:** worktree-design-pmc-fitness-fatigue

> **Three amendments apply to the whole document — read these before anything below.**
>
> 1. **ACWR is retired** (2026-07-31, DESIGN_load_ratio.md). Every mention of
>    `acute_workload` / `chronic_workload` / `acwr` / the `acwr_*_days` config params /
>    `color_acwr` *anywhere* in this doc — above and below — is historical. `ATL:CTL`
>    (`garmin/pmc.py` `load_ratio`, `util.py` `color_load_ratio`) took the ACWR slot on
>    every line this design specifies; details and the per-surface list in §7.
> 2. **The module paths were rewritten by a package split.** `garmin.py`,
>    `coach/service.py`, `coach/engine.py` and `cli/workouts.py` are all packages now.
>    §2's table carries the current homes; line numbers have been dropped everywhere
>    (they rot).
> 3. **Two sections no longer describe live code:** §6.2b (the `workout adapt`
>    trajectory table) was shipped and then deliberately removed, and Phase 2's forward
>    fold shipped under DESIGN_progress_timeline.md. Both are marked in place.

> **Rev. 6 (2026-07-06) — scope rebalance.** Revs. 1–5 grew this design to cover
> every conceivable case; the aggregate complexity outran the payoff. This revision
> sorts the work by value-per-unit-complexity and cuts accordingly. Three decisions,
> each explained where it lands:
>
> 1. **Kept, because the model would otherwise lie:** the three EWMAs (§3.1–3.2),
>    leading-edge warm-up **blanking** (§3.3), the config windows + derivation pad
>    (§3.4), the coach per-day/summary/ramp surfacing (§5.1–5.2), the weekly digest
>    (§5.4), the fingerprint (§5.5), and the CLI (§6). This is the backward-looking
>    core and it ships in this PR.
> 2. **Simplified — the elaborate convergence machinery is gone.** Rev. 5's
>    convergence-percentage caveat, effective-history *gap detection*, and comeback
>    re-arming (the `pmc_effective_history` / `_PMC_REST_WEEK_LOAD` state machine)
>    are replaced by a single **static "still warming up" flag** (§3.3b). A young or
>    just-returned athlete sees low fitness numbers under a plain flag; nobody's
>    training is wrecked by that, so the precise "~78% converged" figure and the
>    gap-detection scan are not worth their cost. Rejected alternative recorded in §7.
> 3. **Cut — phase-aware TSB green.** The green `+5..+25` band gated to peak/taper
>    (rev. 3's `Mesocycle.phase` machinery: a new DB column, type field, LLM schema
>    field, `normalize_meso_phase` parser, and diagnostic — six surfaces for one
>    terminal color) is dropped. TSB colors only its two risk ends; the mid-range is
>    uncolored (§6.1) — which is exactly what **rev. 2 already decided** before rev. 3
>    reversed it. Rationale + the general lesson in §7.
> 4. **Deferred to a follow-up PR — forward taper projection.** The event-day-TSB
>    projection (rev. 3/4's §5.3) delivers real coaching value but is a *second
>    feature* ("predict the future from the plan") bolted onto a backward-looking
>    compute change. It reads the plan; everything else here is Garmin-only. Its
>    design is preserved intact as **Phase 2** (end of this doc) and ships separately.
>
> Net effect: the shipping feature is the top ~30% of the old design that carried
> ~90% of the value. Full rev. 1–5 history (and the cut §4.4 / rev-5 §3.3b specs) is
> in git.

> **Rev. 7 (2026-07-07) — post-implementation review fixes.** Four code-review
> findings folded back into the spec: (1) the §3.1 ramp fallback now *rescales* the
> nearest-earlier delta to a per-week rate and bounds the lookback at 14 days — an
> unscaled gap delta labeled "/week" would misstate the rate the bands judge (the
> implementation was right; the spec text was stale). (2) The §3.3(b) flag fires
> from `N = 0` — a first-pull DB is the youngest case, not an excluded one.
> (3) The §3.3(b) caveat wording is parameterized by τ_ctl, and while the whole
> history is still inside the warm-up window it states that values are *suppressed*
> instead of caveating numbers that aren't shown — and `tm status` shows it even
> then. (4) The TSB-lag footnote appears only where TSB itself is shown.

`trainmate/science/training_load.md` (f0bb707) documents the full Performance Management
Chart model — CTL (fitness), ATL (fatigue), TSB (form), and the CTL ramp rate —
and its §5 coaching directives tell the coach things like *"when TSB falls below
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
This principle is also the knife used in rev. 6: wherever a piece had the *app*
making an interpretive call (is this TSB "good"? how much should you "trust" this
number?), it was cut or simplified — the LLM already makes those judgments for
free from the science file.

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
  exact divergence `training_load.md` warns about (§5 directives, after the file's
  ACWR→ATL:CTL renumbering).
- **The user can't see fitness either.** `tm status` shows ACWR/acute/chronic
  but nothing answers "am I fitter than last month?" or "how fresh am I?" —
  the two questions the PMC exists for.

**What this design fixes.** Backward-looking PMC resolves all three bullets: the
coach and the user both see real current CTL/ATL/TSB and the ramp rate next to the
science directives that reference them.

The *taper* — where the directive targets a TSB band on a future event day — is
only *anchored* by backward PMC (the coach sees current freshness instead of
guessing). Actually *projecting* event-day TSB over the plan's own workouts is the
**Phase 2** follow-up (end of this doc); it is deliberately out of scope here.

Everything needed is already in the DB: per-activity load via the existing
`activity_load()` fallback hierarchy (power TSS → hrTSS → sRPE), summed per day
in `recompute_derived()`. This design adds three EWMAs over that same series.

---

## 2. What exists today (touchpoints)

The **Where** column is kept current; the **Then** column is the pre-PMC state this
design started from (ACWR-era, historical per the amendment banner).

| Layer | Where (current) | Then (pre-PMC) |
|---|---|---|
| Compute | `trainmate/garmin/pmc.py` `recompute_derived()` | full-sweep acute (7d sum), chronic (28d/4), ACWR per metrics day |
| Store | `athlete_metrics_cache` (`db/base.py`), `save_metric_cache()` (`db/activities.py`), `AthleteMetric` (`types.py`) | `acute_workload`, `chronic_workload`, `acwr` columns |
| Wipe | `wipe_garmin_data()` (`db/wipes.py`) | deletes rows by range; **does not** recompute (see §4) |
| Coach, per-day | `format_metrics_history()` (`coach/formatting.py`) → generate & adapt prompts (`coach/engine/workouts.py`) | `... ACWR=1.12` per day line — **unguarded** `:.2f`, see §5.1 |
| Coach, summary | data summary in `coach/service/context.py` → strategy/plan prompts | "Current ACWR: 1.12 (latest)" |
| Coach, weekly | analysis weekly digest (`coach/service/analysis.py`) | `max_acwr` per week |
| Cache key | evidence fingerprint (`coach/engine/prompt.py` `met_digest`) | hashes 6-tuple incl. `m.get('acwr')` per metrics row |
| User | `tm status` (`cli/status.py`), `tm data show-metrics` table/CSV (`cli/data.py`), `color_acwr` (`util.py`) | ACWR + acute/chronic shown; `acwr or 0.0` zero-fill |

**Package split (post-ship).** The four modules this design names were split into
packages after it merged, so the original paths no longer resolve. Current homes:

| Design says | Actually lives in |
|---|---|
| `garmin.py` — `recompute_derived`, `compute_pmc`, `pmc_*`, `backfill_tss` | `trainmate/garmin/pmc.py` |
| `garmin.py` — `pull()`, `_warn_manual` | `trainmate/garmin/sync.py` |
| `garmin.py` — the derivation pad | `trainmate/garmin/client.py` `_derivation_pad_days()` |
| `coach/service.py` — data summary, PMC/ramp/caveat lines | `trainmate/coach/service/context.py` |
| `coach/service.py` — weekly digest | `trainmate/coach/service/analysis.py` |
| `coach/engine.py` — `met_digest` fingerprint | `trainmate/coach/engine/prompt.py` |
| `coach/engine.py` — generate/adapt prompt assembly | `trainmate/coach/engine/workouts.py` |
| `cli/workouts.py` | `trainmate/cli/workouts/*.py` (but see §6.2b — that surface is gone) |

Each row gets a PMC counterpart — no new subsystem. (Rev. 5 also listed plan-side
`Mesocycle`/`Workout.tss` rows for the phase color and taper projection; those are
cut / deferred in rev. 6 — see §7 and Phase 2.)

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
  woke up with — it must not include today's workout. `training_load.md` §1
  states `TSB = CTL(yesterday) − ATL(yesterday)`; implement exactly that, and
  pin it with a test (§8), because it is the classic mistake.
- **Span end is `max(last activity, last metrics)`**, not the last metrics date.
  An activities-only pull (`pull(metrics=False)`) can leave trailing activity
  days past the last metrics row; those days carry load and *must* be walked so
  the EWMAs decay/climb through them.
- **Ramp rate is derived, never stored:** `ramp = ctl_d − ctl_{d-7}` computed
  where displayed (status line, coach summary, weekly digest). Storing it would
  just denormalize a subtraction. Interior-gap rule for the `d−7` lookup: use
  the nearest **earlier** in-memory CTL if the exact `d−7` day has no value,
  **rescaling the delta to a per-week rate** (`Δ · 7 / span`) — an unscaled
  10-day delta labeled "/week" would overstate the rate the bands judge — and
  bounding the fallback at 14 days back (beyond that, omit rather than
  extrapolate); if fewer than 7 days of history precede `d`, **omit** the ramp
  (don't emit garbage). **Ramp reads the full stored CTL series, never the windowed prompt
  slice.** The generate/adapt prompts carry only a short metrics window (15 days
  for generate, `history_days` for adapt), so computing ramp from those rows
  alone would wrongly omit it whenever the window is < 8 days even though the DB
  holds years of CTL. The single ramp line (§5.2) is derived from a full-history
  lookup at prompt-assembly time and injected alongside the slice.

### 3.2 Persistence

- **Pure helper**, unit-testable without a DB. Types are ISO-date strings
  throughout, matching the real `daily_load` and `save_metric_cache` code:

  ```python
  def compute_pmc(
      daily_load: Dict[str, float],       # ISO date -> summed load
      start: str, end: str,               # ISO dates (span from §3.1)
      ctl_days: int, atl_days: int,
      seed: Tuple[float, float] = (0.0, 0.0),   # (ctl, atl) at end of `start - 1`
  ) -> Dict[str, Tuple[float, float, float]]:   # ISO date -> (ctl, atl, tsb)
  ```

- **`seed` (added when the forward fold shipped — see Phase 2).** The default
  `(0.0, 0.0)` is this design's from-zero full-history sweep. A non-zero seed lets a
  caller resume the *same* recurrence from an arbitrary day, given that day's stored
  (CTL, ATL) — that is how `progression.fitness_series()` folds the plan forward from
  its anchor (DESIGN_progress_timeline.md §4).
- **Outputs are stored at full precision; rounding happens only at display.** This is
  a contract, not a style choice: a seeded fold must reproduce the unbroken series
  bit-exactly, and rounding on store would make the future kink at the seam. Pinned by
  `test_outputs_are_full_precision_not_rounded` and
  `test_split_and_refold_reproduces_unsplit_series_exactly`. It also matches AGENTS.md's
  "store precise, round on display" rule.
- `recompute_derived()` calls it once and folds the result into its existing
  per-row `save_metric_cache()` call.
- **Upsert onto existing metrics rows only**, via the same `save_metric_cache()`
  COALESCE upsert already used for acute/chronic. Days with an activity but no
  Garmin metrics row get no row — the in-memory pass still walks them (their
  load feeds the EWMA), we just don't widen the cache's "one row per Garmin
  metrics day" meaning.
- **Refresh paths:** the only callers of `recompute_derived()` are `pull()`
  (`garmin/sync.py`) and `backfill_tss()` (`garmin/pmc.py`) — plus the post-wipe
  recompute this design adds at the command layer (§4).
- **Cost:** the sweep is already O(all days); this adds three multiplications
  per day. Nothing to optimize.

### 3.3 Warm-up handling

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

**(a) Blank the worst region.** Compute from day one as in §3.1 (stored values
are the seed for later days), but **suppress at every surface** — treat as
`None`, same omission convention as any other missing field — every
CTL/ATL/TSB/ramp whose date is within the first **`τ_ctl` days (default 42)** of
DB history. One cutoff date, `history_start + τ_ctl`, is derived once per command and
passed to the per-day lines (§5.1), the weekly digest (§5.4), and the CLI (§6) — so
the surfaces can't each derive it slightly differently. This is a *display* rule:
stored rows keep their converging values, so a window starting past the cutoff reads
warm numbers without recompute.

**As built: three pieces, not one helper.** The original single `pmc_warmup_cutoff()`
split along the pure/impure line, which is the better shape and is what the code does:

| Piece | Where | Role |
|---|---|---|
| `pmc_history_start(dbh=None)` | `garmin/pmc.py` | the two `MIN()` reads (first activity, first metrics); the *only* DB hit |
| `pmc_warmup_cutoff_for(start, ctl_days)` | `garmin/pmc.py` | pure `start + ctl_days`; unit-testable, no DB |
| `pmc_warmup_cutoff(history_start=None)` | `cli/common.py` | CLI convenience wrapper binding the two against `cli.db` |

Two invariants ride on this split and must survive any refactor:

- **`dbh=` injection.** `pmc_history_start` takes the caller's DB handle, so the cutoff
  is derived from the *same* database as the metrics it gates — CoachService's injected
  handle, the CLI's rebindable one, the post-wipe recompute's. A module-singleton read
  would silently gate one DB's rows with another DB's history start.
- **Fetch once, pass down.** The "surfaces can't derive it differently" guarantee is now
  enforced by threading `history_start` (or the cutoff derived from it) down through a
  command, not by there being one function. `cli/status.py` and
  `coach/service/context.py::_pmc_prompt_context` both do exactly one lookup per command
  and hand the result to every consumer.

**(b) A plain "still warming up" flag when *today itself* is short on history.**
Blanking the leading edge does nothing for a *young DB*, where even today's value
is warm-up quality — you can't blank today or there's nothing to show. So when
total history behind the latest value is short, surface a single **static** flag,
not a computed accuracy figure:

- **To the coach** — one line appended to the data summary (§5.2):
  `- PMC data caveat: CTL is based on N days of history (a {τ_ctl}-day average
  needs months to settle). If the athlete trained regularly before
  {history_start}, true fitness is higher than shown and low TSB / high ramp are
  partly warm-up artifacts; if they did not, the low values are real.` — `N =
  today − history_start`, and the τ in the wording reads the live config (a
  hardcoded "42" would lie under a non-default τ_ctl — the same frozen-constant
  drift §3.4 kills `CHRONIC_WEEKS` over). The caveat states the *condition*
  rather than asserting understatement, because a genuine beginner's low CTL is
  correct, not an artifact (the direction is the LLM's to judge from the
  athlete's pre-DB history; the app only flags that the number is young).
  While `N < τ_ctl` the *entire* history is still inside the §3.3(a) warm-up
  window, so every surfaced value is suppressed; then the line instead states
  that PMC is suppressed and why, rather than caveating numbers the prompt
  doesn't contain.
- **To the user** — a matching short `tm status` line, and the `_warn_manual`
  baseline text (`garmin/sync.py`) gains a PMC sentence so a young-DB user sees
  *why* freshness reads low. **Warm branch only:** `_warn_manual(cold=True)` — the
  "no Garmin data has been pulled yet" case — carries no PMC sentence, because with
  zero data there is no freshness reading on screen to explain; the sentence rides
  only the `cold=False` "this view needs data back to X" branch.

Fire the flag while `0 ≤ N < 3·τ_ctl` (≈126 days) — including `N = 0`, the
first-pull day, which is the youngest history a DB can have; above the ceiling
the artifact is negligible and the line is dropped.

> **What rev. 6 deliberately does *not* do here.** Rev. 5 turned this into a
> convergence-percentage figure (`1 − e^{−N/τ}` → "~78% converged") plus
> *effective*-history gap detection — a rolling weekly-rest scan
> (`_PMC_REST_WEEK_LOAD`) that re-armed the caveat after any layoff. That is a lot
> of machinery, and a magic rest-threshold, to turn "still warming up" into a
> number the LLM does nothing materially different with. The static flag above
> covers the young-DB case; a *comeback* athlete simply sees low numbers under the
> same flag for a few weeks (their `N` is large, so the flag won't fire, but the
> numbers self-correct as the EWMA re-warms and the coach reads the raw values).
> If comeback mis-reads ever prove to be a real complaint in practice, the gap
> detection can be added then — it is not needed to ship an honest v1. (§7
> records this as a rejected alternative.)

The §3.4 data pad is the complementary *data* backstop (it warms CTL by pulling
prior history); (a) is the *display* backstop for the leading edge; (b) is the
*honesty* backstop for when neither can help because the history simply isn't
long enough yet.

### 3.4 Configurable windows (ACWR + PMC) and the derivation pad

All four window/time-constant numbers become config params via the existing
`config.py` property + `config_template_full.yaml` pattern (like `hr_zone_coverage_min`,
already a `garmin:` param), under the **`garmin:`** section (they are computation
constants living beside the windows they replace; the coach never reads them
directly):

| Param | Default | Replaces |
|---|---|---|
| ~~`acwr_acute_days`~~ | 7 | `garmin.py` `ACUTE_WINDOW_DAYS` — **gone with ACWR** |
| ~~`acwr_chronic_days`~~ | 28 | `garmin.py` `CHRONIC_WINDOW_DAYS` — **gone with ACWR** |
| `pmc_ctl_days` | 42 | (new) CTL time constant |
| `pmc_atl_days` | 7 | (new) ATL time constant |

Only the two `pmc_*` params still exist; the `acwr_*` pair was removed with ACWR
(DESIGN_load_ratio.md). `ATL:CTL` needs no windows of its own — it divides the two
EWMAs above, which is exactly why it could replace ACWR without new config.

`CHRONIC_WEEKS` is **deleted, not merely derived.** It is exactly
`chronic_days / acute_days`, so both a stored constant (frozen at import) and a
standalone param (settable inconsistently with the windows) are wrong. Compute it
inline where chronic is normalized —
`chronic = total_chronic / (config.acwr_chronic_days / config.acwr_acute_days)` —
reading the live params every sweep, so it can never drift from the windows it is
defined by. The `garmin.py` header comment "Standard constants, not tunables"
becomes false and is rewritten.

**Ramp rate stays a fixed 7-day delta** — "per week" is its definition, not a tunable
window, so there is deliberately no `pmc_ramp_days` config param. As built, `pmc_ramp`
does take `window: int = 7` (`garmin/pmc.py`), but that is a **test seam, not a knob**:
no caller passes it, and the parameter exists so the interior-gap rules it also drives
(the `2 × window` lookback bound and the `Δ · window / offset` rescale) can be exercised
at small windows without fabricating weeks of fixture data. Wiring it to config would
contradict this paragraph, not implement it.

**Derivation pad.** Rev. 1's `DERIVATION_PAD_DAYS = chronic` is under-padded two
ways: (1) CTL needs ~1.5 × τ_ctl ≈ 63 days of prior data to reach ~78% of its
settled value at the left edge of a displayed window; a 28-day pad neither pulls
nor warms enough history. (2) Making `acwr_chronic_days` configurable while pad =
chronic is a latent corruption: set chronic to 14 and the pad drops **below the
hardcoded 28-day baseline lookback** in `recompute_derived()` (`for d in
range(1, 29)`).

Fix: `max(acwr_chronic_days, 28, ceil(1.5 * pmc_ctl_days))` (= 63 at defaults). The
literal `28` floor pins it to the baseline lookback regardless of a shrunk chronic
window; the `1.5 × τ_ctl` term warms CTL. (Warm-up suppression in §3.3 is the *display*
backstop; the pad is the *data* backstop — they are complementary, and the pad alone
can't cover a DB whose entire history is younger than the pad, which is what
§3.3(b)'s flag is for.)

**As built: a function, not a constant.** It is `_derivation_pad_days()`
(`garmin/client.py`), returning `max(28, ceil(1.5 * config.pmc_ctl_days))` — same 63 at
defaults. Two corrections to the text above:

- It is **not** a module-level `DERIVATION_PAD_DAYS`. A constant is evaluated at import,
  which is precisely the frozen-constant drift this section deletes `CHRONIC_WEEKS` over;
  a function reads the live τ on every call, so the pad can never disagree with the
  window it pads for.
- The `acwr_chronic_days` term went with ACWR (DESIGN_load_ratio.md). The `28` floor
  stays and now carries that job alone, since it was always the real backstop for the
  hardcoded baseline lookback.

**Config-change staleness (accepted, documented).** The rewriting sweep only runs
on the next path that calls `recompute_derived()` (next `pull()` / `backfill_tss()`
/ wipe). Between editing a τ and that call, every displayed PMC/ACWR value silently
reflects the *old* constants. This matches how every other derived value already
behaves; we accept it and note it beside the params rather than adding an
eager-recompute hook.

**Calibration caveat (load-bearing).** The science file's text and every
interpretation band (ACWR 0.8–1.3, TSB −30/+25, ramp 3–5/8, and
`color_acwr`/`color_tsb`) are calibrated to the defaults. Non-default constants
change what the numbers *mean* while the prompts and colors keep judging them
against the standard bands. **The defaults are the supported configuration**; the
params exist for deliberate experimentation, not casual tuning.

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
  inside the db method.** `wipe_garmin_data()` deletes activity/metrics rows by
  range and *never* recomputes today. For ACWR the staleness is bounded (flat
  28-day window: only ≤28 days after the wiped range read stale values until the
  next pull). An EWMA has no such bound: deleted load stays baked into
  `ctl_{d}`/`atl_{d}` for **every** subsequent day, forever, until some future
  pull sweeps. So it must be fixed now. **Where the recompute goes matters.**
  `recompute_derived()` lives in the `garmin` package, which imports `db`; a
  module-level call from `db/wipes.py` would be a circular import. And it
  opens its own connection, so calling it *inside* the wipe's
  `with self._get_connection()` block either won't see the uncommitted deletes or
  hits SQLite `database is locked`. So the recompute belongs one level up, at the
  command layer that already imports both: `cli/data.py` does
  `db.wipe_garmin_data(...)`, then `garmin.recompute_derived(dbh=cli.db)` immediately
  after — once the wipe has returned and its transaction committed, and pinned to the
  CLI's own handle so it sweeps the database the wipe just ran against. The
  `wipe_metrics()` wrapper and any other wipe entry point follow the same
  "wipe, then recompute" rule. Cost is one extra full sweep per wipe — wipes are
  rare and manual; acceptable.

---

## 5. Surfacing to the coach

No prompt-instruction changes are needed — the directives already ride in via
`_load_science_guidelines()`; the values just have to show up next to them
using the same vocabulary (`CTL`/`ATL`/`TSB`, which the science file defines).

### 5.1 Per-day history — and fixing the pre-existing NULL crash

`format_metrics_history()` (`coach/formatting.py`) today does
`f"...ACWR={m['acwr']:.2f}"` **with no guard**. That is not "ACWR is implicitly
omitted when absent" — a NULL `acwr` **crashes** with `TypeError` (`:.2f` on
`None`), and a NULL-acwr row *is* reachable: a pull that dies between
`_ingest_metrics` and the end-of-pull `recompute_derived()` leaves raw metrics
rows with derived columns still NULL. (The line also renders `RHR=Nonebpm` today
when RHR is missing.) Guarding only the three new PMC fields would leave that
crash in place.

So adopt **one None convention for the whole line**: build it field-by-field and
**omit any field whose value is `None`** (RHR, HRV, ACWR, and the new
CTL/ATL/TSB alike). For a fully-populated, past-warm-up row:

```
- 2026-07-02: RHR=52bpm, HRV=61ms, Sleep=78, Stress=31, CTL=62.4, ATL=71.7, TSB=-8.9, ATL:CTL=1.15
```

(As designed the field after `Stress` was `ACWR=1.12`; `ATL:CTL` replaced it at the end
of the PMC group — see §7. The rest of the line is unchanged.)

- The three PMC fields are omitted wholesale during the §3.3 warm-up window and
  for any pre-recompute NULL row. `ATL:CTL` rides *inside* that gate, since it
  divides two of them.
- **A row where every field is None renders `- 2026-07-02: (no data)`**, not a
  dangling `- 2026-07-02: `. The omit rule alone would produce the dangling form,
  which reads as a rendering bug rather than as "Garmin had nothing that day" — and
  dropping the row entirely would be worse, since a silently-absent date is
  indistinguishable from a date outside the window. Pinned by
  `test_all_null_row_marked_no_data`.
- **TSB won't equal the shown CTL − ATL.** Per §3.1, `TSB = CTL(yesterday) −
  ATL(yesterday)`, but the line shows *today's* CTL/ATL. This is correct
  (matching TrainingPeaks' lag) but reads as an arithmetic error, so a one-line
  footnote states the lag wherever TSB itself is surfaced (a CTL/ATL-only block
  has no lag to explain, so it gets no footnote).
- **Ramp is *not* on the per-day line.** Ramp is a slow-moving weekly figure;
  stamping it on all ~30 daily lines is repetition the LLM must wade through. It
  reaches the coach as a **single line** instead (§5.2).

This reaches the workout generate and adapt prompts, so adapt now sees the
fatigue trajectory (fixing failure #1).

### 5.2 Summary lines (PMC + the single ramp line)

Two summary lines, added after the existing ACWR line:

```
- Fitness/Fatigue (PMC): CTL 62.4 (fitness), ATL 71.7 (fatigue), TSB -8.9 (form), ATL:CTL 1.15 (relative overload)
- CTL ramp rate: +4.2/week (last 7 days)
```

(The trailing `ATL:CTL` field replaced the separate ACWR line this design appended
after — see §7.)

- The **PMC line** goes into the data summary (`coach/service/context.py`) that feeds
  the strategy/plan prompts, so plan generation can reason about sustainable build
  rates and current freshness.
- The **ramp line** must reach the prompts that actually set weekly TSS —
  generate and adapt (`coach/engine/workouts.py`) — *and* the strategy/plan
  prompts. So the same one-liner is emitted into **both** contexts: once in the
  data summary, and once in the generate/adapt metrics context (a single line
  beside the per-day block, not repeated per day). It is computed from the **full
  stored CTL series** (§3.1), independent of the short prompt window, so a small
  window never spuriously drops it.
- The §3.3(b) **warm-up flag line** appends here too when today's values are
  still warming, so every prompt that reads PMC also reads how much to trust it.

Ramp obeys the §3.1 lookup/omit rule and is omitted entirely inside the §3.3(a)
warm-up window.

> Forward taper projection (rev. 5's §5.3) is **deferred** — see Phase 2 at the
> end of this doc. The `+5..+25` event-day directive is *anchored* by the current
> TSB shown above; projecting it forward over the plan ships separately. **Update:**
> the projection itself shipped under DESIGN_progress_timeline.md §4; only the
> event-day *prompt line* is still missing. See the Phase 2 section for the split.

### 5.4 Weekly digest (analysis, `coach/service/analysis.py`)

Alongside `max_acwr` (now `max_load_ratio` — §7) add per week: `end_ctl` (CTL on the
week's last day),
`week_ramp` (`end_ctl` − CTL 7 days earlier), `min_tsb`. Three numbers give the
analyze pass the multi-week fitness trajectory and each week's overload depth
without per-day noise. (The digest is `json.dumps`-ed into the analysis prompt,
so a suppressed field serializes as `null` — unambiguous to the LLM.)

Two guards:

- **Warm-up:** weeks entirely inside the §3.3 warm-up window emit `None` for all
  three — otherwise every bootstrap narrates a phantom overreach block from
  seeding artifacts. **Straddle guard:** for a week that only *partly* clears the
  cutoff, `end_ctl`/`min_tsb` may show but `week_ramp` is still suppressed
  whenever its `−7d` lookback lands *before* the warm-up cutoff — otherwise the
  first shown week reports a huge ramp measured against a suppressed baseline.
- **Week boundary:** `week_ramp` reaches into the *previous* week's slice for the
  `−7d` CTL. The first week of an analysis window has no prior week → `week_ramp`
  is omitted (None-guarded), and it is exactly where the warm-up artifacts would
  otherwise live, so the two guards reinforce.

### 5.5 Evidence fingerprint (`coach/engine/prompt.py` `met_digest`)

Add **all three** of `m.get('ctl')`, `m.get('atl')`, `m.get('tsb')` to the
per-row tuple. The prompt now carries all three per day; hashing all three costs
nothing and honors the docstring's stated rule that data feeding the prompt must
shift the fingerprint. This makes the first post-deploy recompute (rows gaining
PMC values) invalidate cached analyses so the new signal reaches reconstructions
without a manual `--force`.

**Accepted double-invalidation.** Widening the tuple shifts every fingerprint at
deploy (values still NULL), and the first recompute shifts them again (NULL →
real). On the normal path both collapse into a single refresh; only a `--no-pull`
analysis run *between* deploy and first pull pays for two LLM re-runs. That window
is small and self-healing; not worth guarding.

---

## 6. Surfacing to the user (CLI first)

### 6.1 `tm status`

One line under ACWR (`cli/status.py`):

```
- Fitness    : CTL 62.4 | ATL 71.7 | TSB -8.9 | ATL:CTL 1.15 | Ramp +4.2/wk
```

(`ATL:CTL` took the retired ACWR line's slot — §7 — and sits between TSB and Ramp.)

- **Never zero-fill.** The ACWR line uses `last_metrics['acwr'] or 0.0`; copying
  that for PMC would print `CTL 0.0 | ATL 0.0 | TSB 0.0` after a fresh deploy on
  `--no-pull`, and **`TSB 0.0` reads as a meaningful neutral "perfectly balanced"
  reading, not as missing data** — actively misleading. When any of CTL/ATL/TSB
  is NULL or the latest row is inside the §3.3 warm-up window, render `—` for that
  field (or omit the whole line if all three are absent). Same NULL/dash rule for
  the ramp, plus the §3.1 interior-gap / young-DB omit rule.
- **`color_tsb(tsb)` (new, `util.py` beside `color_acwr`) colors only the two
  risk ends, phase-blind:** `< −30` red (excessive fatigue), `> +25` yellow
  (detraining / over-tapered). **The `−30..+25` middle stays uncolored** — its
  meaning is phase-dependent (mid-build a +15 means fitness is *decaying*;
  peaking, it means race-ready), and that judgment belongs to the coach reading
  the science file, not to a phase-blind color map. Bands are half-open so no
  value is double-claimed: `< −30` red, `−30 ≤ tsb ≤ +25` uncolored, `> +25`
  yellow.

  > Rev. 3 briefly added a green `+5..+25` band gated to `peak`/`taper` via a new
  > structured `Mesocycle.phase`. Rev. 6 **cuts** it (§7): six surfaces — a DB
  > column, a type field, an LLM schema field, a parser, and a diagnostic — for
  > one terminal color, encoding an interpretive call the LLM already makes. This
  > restores rev. 2's decision. `color_tsb` therefore takes no `phase` argument.

- **Ramp bands must touch:** `≥ 8` red, `5–8` yellow (i.e. `5 ≤ ramp < 8`), else
  plain. No green band: a low ramp is correct during a taper, so green would
  wrongly bless it. (`color_ramp` in `util.py`.)
- **TSB lag footnote.** `TSB = CTL(yesterday) − ATL(yesterday)`, so the printed
  triple won't subtract to the shown TSB; a dim one-line footnote states this so
  the user doesn't read it as a bug.

### 6.2 `tm data show-metrics`

Three new table columns and CSV fields next to the existing ACWR/Acute/Chronic
ones (`cli/data.py`). Same NULL → blank/`—` and warm-up omission as the status
line; the CSV emits empty cells (not `0`) for suppressed/NULL values so
downstream parsing doesn't read a zero as data.

### 6.2b `workout adapt` metrics trajectory — SHIPPED, THEN REMOVED

> **This surface does not exist. Do not go looking for it, and do not "restore" it
> without revisiting the decision below.** It shipped in `613b47c` and was deliberately
> deleted in `ce0b74d` (*"workout adapt: collapse metrics table to a one-line day
> count"*). `trainmate/cli/workouts/generate.py` now prints only
> `Using N days of recovery metrics (past N-day window).`
>
> **Why it went:** the reason for the table was "the athlete should see what the coach
> saw", but the coach reads that same window and *quotes the relevant numbers in its own
> reasoning*, so a full trajectory table above the Decision Summary was redundant noise
> on every adapt — several screenfuls of columns to re-derive a judgment the paragraph
> underneath already states. The one-line day count keeps the honest part (how much data
> fed the decision) at ~1% of the vertical space. `tm status` and `tm data show-metrics`
> remain the places to read the trajectory itself.

The original spec, kept for the record: the same three columns on the trajectory table
(`cli/workouts.py`, now the `cli/workouts/` package), printing directly above the
Decision Summary, with the same warm-up blanking and TSB-lag footnote as the status
line and the §3.3(b) warm-up flag riding along to explain a column of `—`.

### 6.3 Telegram / web

Nothing bespoke *for this design* — the bot and web tab render what the shared
status/summary code produces.

> **Superseded: the web PMC chart shipped.** §7 lists it as a non-goal ("nice, not
> now"); that is no longer true. `GET /api/timeline.png` (`trainmate_web.py`) serves
> the web **Progress** tab a PNG of merged past/planned load *plus the projected
> CTL/ATL/TSB series*, rendered by `chart.render_timeline_png`; the Telegram bot posts
> the identical image and `tm progress` prints the same triple as text. That work is
> specified by **DESIGN_progress_timeline.md** (§6 for the endpoint, §7 for the three
> front-ends) — read it there, not here. Anyone treating §7's non-goal as current
> would rebuild a shipped feature.

---

## 7. Rollout & non-goals

**Rollout:**

- Idempotent `ALTER TABLE` on `athlete_metrics_cache` (§4) — no data migration;
  NULL-tolerant on old rows.
- The next `tm data pull` (or any `recompute_derived()` path) back-populates PMC
  for the entire cached history in one sweep. Old analysis caches refresh
  naturally via the fingerprint change (§5.5).
- **Docs:** update `ARCHITECTURE.md` (AGENTS.md L2–3 requires it) — the
  derived-metrics `garmin.py` row (add CTL/ATL/TSB to "the load model") and the
  wipe/analysis-cache note (`wipe_garmin_data` now recomputes). Note the four new
  config params where `config_template_full.yaml` is documented.

**Known limitation — PMC is dark for the first ~6 weeks of a fresh install.**
CTL is a 42-day EWMA, so it needs months of history to be trustworthy. Cold-start
backfill defaults to 90 days (`garmin_initial_backfill_days`); §3.3(a) blanks the
first 42, and the §3.3(b) flag runs to ~126 days. So a new user sees **no** PMC
for ~6 weeks, then flagged numbers for weeks more — whereas ACWR is trustworthy at
28 days. This is inherent to the model, not a bug. The only real remedy is a
deeper Garmin backfill (`tm data pull --start`), itself bounded by Garmin's
retention.

**Out of scope, deliberately:**

- **Forward taper projection** — real value, but a distinct plan-coupled feature;
  designed as **Phase 2** below and shipped separately. *(Partly shipped since: the
  forward fold itself is live under DESIGN_progress_timeline.md §4; only the
  event-day prompt line remains. See Phase 2.)*
- **Phase-aware TSB green (cut, rev. 6).** Gating a green `+5..+25` band to
  peak/taper required a structured `Mesocycle.phase` threaded through the DB, the
  type, the LLM plan-generation schema, a normalizer, and a diagnostic — six
  surfaces for one terminal color, and the app making an interpretive call
  ("is this TSB good?") the LLM already makes from the science file. The general
  lesson: complexity that stops the app from *lying* (warm-up blanking) is worth
  paying; complexity that only makes a *true* number prettier is not. TSB colors
  its two risk ends only (§6.1).
- **Convergence-percentage + comeback gap detection (rejected, rev. 6).** Rev. 5's
  `1 − e^{−N/τ}` figure and `pmc_effective_history` rest-run scan replaced by the
  static flag (§3.3b): a young/returned athlete sees low numbers under a plain
  flag, which wrecks no training decision. Revisit only if comeback mis-reads
  become a real, observed complaint.
- **Per-sport CTL splits** — the science file models one systemic load stream;
  splitting by sport is a different (and contested) model.
- **Replacing ACWR** — the science file was explicit at the time that ACWR and PMC
  were complementary guardrails; both stay.

  > **Superseded (2026-07-31, DESIGN_load_ratio.md) — this non-goal was reversed.**
  > ACWR *was* retired, and its relative-overload role handed to the **`ATL:CTL` load
  > ratio**, which divides the two EWMAs this document introduced (so it needed no new
  > columns, no new config, and no new sweep). `training_load.md` was rewritten to
  > match: its §3 is now the ATL:CTL section.
  >
  > **Scope of the supersede: this entire document, above and below.** Every mention
  > *anywhere here* of the stored `acute_workload` / `chronic_workload` / `acwr`
  > columns, the `acwr_acute_days` / `acwr_chronic_days` config params, or
  > `color_acwr` is historical — most of that text is in §§1–6, i.e. **above** this
  > note. (An earlier revision of this note said "everything below", which pointed at
  > roughly the one paragraph that isn't affected.) In live code the only ACWR residue
  > is the idempotent `DROP COLUMN` migration in `db/base.py`.
  >
  > **What took its slot, surface by surface** — `ATL:CTL` is not merely ACWR deleted;
  > it is ACWR *replaced*, on every line this design specifies:
  >
  > | Surface (this doc) | Was | Now |
  > |---|---|---|
  > | per-day prompt line (§5.1) | `ACWR=1.12` | `ATL:CTL=1.15` (`coach/formatting.py`) |
  > | data-summary line (§5.2) | "Current ACWR: 1.12 (latest)" | `ATL:CTL 1.15 (relative overload)`, folded into the PMC line (`coach/service/context.py`) |
  > | weekly digest key (§5.4) | `max_acwr` | `max_load_ratio` (`coach/service/analysis.py`) |
  > | `tm status` (§6.1) | ACWR line | `ATL:CTL` field on the Fitness line (`cli/status.py`) |
  > | `tm data show-metrics` (§6.2) | ACWR column / CSV field | `ATL:CTL` column / CSV field (`cli/data.py`) |
  > | coloring | `color_acwr` (0.8–1.3 band) | `color_load_ratio` — overload end only: `> 1.5` red, `1.3–1.5` yellow, low uncolored (`util.py`) |
  >
  > Helpers: `garmin.load_ratio(atl, ctl)` (`garmin/pmc.py`) — `None` when either EWMA
  > is NULL or `ctl <= 0`, so it inherits the §3.3(a) warm-up blanking of the values it
  > divides. Rationale for the retirement (ACWR fighting block periodization, and its
  > direct contradiction with the TSB bands) is in DESIGN_load_ratio.md §§1–2.

- ~~**Web PMC chart** — nice, not now.~~ **Shipped** — the Progress tab's
  `/api/timeline.png` plots the projected CTL/ATL/TSB series; see §6.3 and
  DESIGN_progress_timeline.md.
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

Ramp (`pmc_ramp`):

- exact 7-day delta; nearest-earlier fallback at an interior d−7 gap, rescaled
  to a per-week rate (`Δ · 7 / span`); omit when the nearest baseline is more
  than 14 days back; omit when fewer than 7 days precede; omit when the baseline
  lands before the warm-up cutoff (straddle guard); computed from full history
  so an 8-day prompt window still emits it.

Formatting / surfacing:

- `format_metrics_history()` with and without PMC fields, **and with a NULL
  ACWR/RHR row** (regression for the pre-existing `:.2f` crash and `Nonebpm`) —
  every None field omitted, no exception.
- **Warm-up (a) suppression:** rows inside the first τ_ctl days emit no PMC
  fields in the per-day line, weekly digest, and status line.
- **Warm-up (b) static flag:** for a short-history DB the flag line is emitted
  with the right `N`; once `N ≥ 3·τ_ctl` the line is dropped. (No convergence-%
  arithmetic to pin — the flag is static.)
- **Status-line NULL rendering:** NULL/warm-up CTL/ATL/TSB render `—`, never
  `0.0`.
- **Color bands touch:** ramp 7.5 yellow, ≥8 red (no uncolored hole).
  **`color_tsb` phase-blind:** −31 red, +26 yellow, and +12 / −10 / +5 all
  uncolored (no green band, no `phase` argument).

DB integration (temp DB):

- one `recompute_derived()` run: activity-only days contribute load but create
  no metrics rows; existing rows get PMC values.
- **COALESCE regression:** a metrics-only re-pull (no activity change) must
  **not** null previously-stored PMC values.
- **wipe-then-recompute:** after a dated wipe *followed by the command-layer
  `recompute_derived()`*, no surviving row past the wiped range still carries
  load-through-the-gap in its CTL/ATL. The recompute runs after the wipe's
  transaction commits (no lock, sees the deletes).
- ~~**`CHRONIC_WEEKS` inline:** changing `acwr_acute_days`/`acwr_chronic_days`
  moves the normalized chronic value in the same sweep (no frozen 4.0).~~ Retired
  with ACWR (§7).
- **Non-positive config window fails loud** at read time.

Added since (pinning contracts §3.2 gained for the forward fold):

- **full precision on store:** `compute_pmc` outputs are not rounded
  (`test_outputs_are_full_precision_not_rounded`).
- **seeded refold is exact:** splitting a span in two and refolding the second half
  from the first half's last (CTL, ATL) reproduces the unsplit series bit-for-bit
  (`test_split_and_refold_reproduces_unsplit_series_exactly`).
- **all-None row:** renders `(no data)`, not a dangling prefix
  (`test_all_null_row_marked_no_data`).

---

## Phase 2 (separate PR): forward taper projection

> **MOSTLY SHIPPED — read this before implementing anything below.** Phase 2 was
> deferred from rev. 6, but the hard part of it — the forward fold itself — was built
> afterwards as part of **DESIGN_progress_timeline.md §4**, which states outright that
> it *is* PMC Phase 2, generalized from one event-day number to the full daily series.
> The section below is the *original* spec, kept verbatim for its rationale. Treating
> it as a to-do list means re-deriving working code.
>
> **Shipped (`trainmate/progression.py`, DESIGN_progress_timeline.md §4):**
>
> | Phase-2 element | As built |
> |---|---|
> | forward walk of §3.1's recurrence over planned load | `fitness_series()` → `garmin.compute_pmc(..., seed=(ctl_a, atl_a))` over merged actual→planned daily load |
> | the anchor | `_anchor()` — latest stored row **strictly before today** with non-NULL CTL *and* ATL |
> | stale-anchor decay | implicit: the fold starts at anchor+1 and runs over *actual* load up to today before continuing into the plan, so no separate decay pass is needed |
> | warm-up / missing anchor | no anchor → every point's PMC is `None` and the panel is suppressed; past days are blanked via `pmc_display_values()` |
> | removed workouts excluded | yes, via `get_workouts` defaults |
> | unquantified-workout annotation | `zero_load_workout_count()`, counted **from today on** so the banner heals |
> | seeded fold correctness | `compute_pmc(seed=...)` + full-precision storage (§3.2) make the fold reproduce the stored series bit-exactly |
>
> One deliberate difference from the spec below: the shipped feature **stops at plan
> end** rather than zero-filling the tail up to the event and annotating it — a
> projection over assumed rest is a statement about missing data, so it isn't drawn.
>
> **Still genuinely unshipped — the whole remaining deliverable:**
>
> 1. **Event selection.** Nothing anywhere picks the event to project to. The
>    "Event selection" paragraph below is still the spec — but see its rev note:
>    the `priority` column it was written against no longer exists.
> 2. **The coach-facing prompt line.** No `Projected event-day TSB` line exists in any
>    prompt. Given the shipped fold this is a *read*, not a computation: pick the event
>    per (1), read the folded TSB on its `target_date`, and carry the fold's existing
>    warnings. DESIGN_progress_timeline.md §8 tracks it as follow-on #4.
>
> Both are small and neither needs new projection math. Everything else below is
> historical context.

Deferred from rev. 6. Ships as its own change once the backward core above is in.
Design preserved intact so it needs no re-derivation.

Backward PMC anchors the taper to today's real freshness; this closes the loop by
projecting **event-day TSB** deterministically — the exponential-decay arithmetic
the "app computes" principle says the LLM should not do in its head. Best-effort
full projection over the plan's own workouts (not a zero-training bound), with a
warning when the plan doesn't yet reach the event.

Computed **on demand at prompt-assembly time** (now `coach/service/context.py`), **not** in
`recompute_derived()` and **not stored**: it depends on the plan and the event
date and is forward-looking, so it stays out of the pure, plan-independent
backward pass. This is the one place PMC reads the plan, read-only.

**Event selection.** Anchor to the athlete's **nearest upcoming active objective**
— exactly what `get_active_objective()` returns, and what `upcoming_objectives()`
lists first.

> **Rev note — the `priority` tiebreak is gone.** This paragraph originally
> specified `ORDER BY priority DESC, target_date ASC LIMIT 1`, anchoring to the
> *highest-priority* upcoming objective rather than the nearest. That query was
> never written, and the column it read was removed: `objectives.priority` was set
> and displayed but never read by any logic and never rendered into a prompt, so it
> was dropped (`DOMAIN_MODEL.md §2 "Goal"`). The sort was also inverted — with
> `1 = highest`, `priority DESC` picks the *least* important goal — which is the
> clearest evidence nobody ever ran it. Nearest-date is what every other reader
> already does and what the system prompt tells the coach to do ("focus scheduling
> on the NEXT CHRONOLOGICAL GOAL only"). If an A/B/C race tier is wanted later it
> should be designed as one, not reconstructed from a dormant integer.

There is no "A-event" tier in the schema, so the `+5..+25` band is labeled as the
coach's peak/taper target *for the chosen event*, not an unconditional gate.

Inputs: the latest metrics row's CTL/ATL as the anchor, the chosen objective's
`target_date`, and planned `Workout.tss` for anchor→event. Walk §3.1's recurrence
forward over planned daily load (sum of that day's planned `tss` for non-removed
workouts; rest days = 0). Emit into the plan/adapt prompts:

```
- Projected event-day TSB (from current plan): +12   [taper target band +5..+25]
```

Honesty guards:

- **Stale anchor.** Decay CTL/ATL forward from the anchor date to today over
  actual daily load *before* projecting the plan, so "from today" really starts
  at today.
- **Warm-up / missing anchor.** If the anchor row is inside the §3.3 warm-up
  window or its CTL/ATL is NULL, carry the §3.3(b) flag onto the projection line
  (or suppress the line when the anchor is NULL) rather than printing a confident
  number over a shaky start.
- **Removed workouts.** Excluded by `get_workouts` default; a cancelled session
  must not inflate the projection.
- **Plan doesn't reach the event.** If the last planned workout precedes the
  event, tail days are assumed zero-load, inflating projected TSB. The line
  self-annotates: `(note: last N days before the event are unplanned; assumes
  rest — realized TSB likely lower)`.
- **Unquantified workouts.** Planned workouts with `tss = None` count as zero
  load; if any fall in range, append `(M planned workouts lack a TSS estimate,
  counted as 0)`.

Accuracy is bounded by the plan's own TSS estimates — surfaced honestly. Improving
that estimate quality is the only taper work left beyond Phase 2.

Phase-2 tests: decay-only reproduces the closed-form `ctl·(1−1/τ)^d`; planned load
raises the projection; `tss=None` triggers the annotation; plan ending early
triggers the unplanned-tail annotation; no upcoming objective / no plan → no line;
nearest-date event selection; stale-anchor decay;
warm-up-anchor flag; NULL-anchor suppression.
