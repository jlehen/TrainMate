# Design: PMC Fitness/Fatigue/Form (CTL · ATL · TSB)

**Status:** Proposed (rev. 2) · **Date:** 2026-07-04 · **Branch:** worktree-design-pmc-fitness-fatigue

> **Rev. 2 (2026-07-04):** folds in `DESIGN_pmc_fitness_fatigue_review.md`.
> Every review finding is resolved in the body below; §9 maps each finding to
> where. The load-bearing changes since rev. 1: **warm-up suppression** (§3.3),
> corrected None/NULL handling everywhere (§5.1, §6), a real fix for the
> `wipe_garmin_data` staleness (§4), a wider derivation pad (§3.4), and honest
> scoping of the taper claim (§1, §5.2, §7).

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
- **Week-over-week progression is unguided.** The science caps CTL ramp rate
  (~3–5 sustainable, >8 red flag), but no ramp number exists anywhere — plans
  can climb unsustainably for weeks while ACWR stays in the sweet spot, the
  exact divergence `training_load.txt` §3 warns about.
- **Taper is *partly* blind.** The A-event taper directive targets a TSB band
  (+5..+25 on event day). Plan generation gets no CTL/ATL at all, so the LLM
  can't even anchor the taper to the athlete's *current* freshness. Backward
  PMC fixes the anchor but not the projection — see the honest scoping below.
- **The user can't see fitness either.** `tm status` shows ACWR/acute/chronic
  but nothing answers "am I fitter than last month?" or "how fresh am I?" —
  the two questions the PMC exists for.

**What this design fixes, precisely.** Backward-looking PMC fully resolves the
first, second, and fourth bullets. It only *partly* resolves the taper bullet:
it gives the coach the athlete's real current CTL/ATL/TSB and ramp, so a taper
starts from truth instead of a guess — but the directive's target is TSB *on
event day, weeks out*, and computing that requires simulating two exponential
decays forward across the taper. That forward simulation is exactly the
arithmetic our own "app computes, LLM reasons" principle says not to hand the
LLM. So the taper is **anchored, not solved**; the deterministic forward
projection is the natural phase 2 (§7), with a cheap partial (§5.2) available
now.

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

Each row of that table gets a PMC counterpart. No new subsystem.

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
  matters only at the young-DB left edge and at analysis-window slices (§5.3).

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

### 3.3 Warm-up suppression (was the rev. 1 seeding hole)

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

**Rule:** compute from day one as in §3.1 (values are still stored — they're the
seed for later days), but **suppress at every surface** — treat as `None`,
using the same omission convention as any other missing field — every
CTL/ATL/TSB/ramp whose date is within the first **`τ_ctl` days** of DB history
(default 42). One warm-up cutoff date, computed once as
`history_start + τ_ctl days`, applied uniformly in:

- the per-day metrics lines (§5.1),
- the weekly digest (§5.3),
- the CLI status line and `show-metrics` table (§6).

This is a display rule, not a storage rule: stored rows keep their (converging)
values so a later window that starts *after* the cutoff reads fully-warmed
numbers without recompute. No manual starting-CTL machinery (the
TrainingPeaks/intervals.icu escape hatch) is needed — the practical fix for a
young DB remains a deeper Garmin backfill via `tm data pull --start`. The
`_warn_manual` baseline warning in `status.py` should mention PMC warm-up too
(finding #4a), so a young-DB user sees *why* the fields are blank.

### 3.4 Configurable windows (ACWR + PMC) and the derivation pad

All four window/time-constant numbers become config params via the existing
`config.py` property + `config_template.yaml` pattern (like
`rpe_divergence_ratio`), under the **`garmin:`** section (they are computation
constants living beside the windows they replace; the coach never reads them
directly):

| Param | Default | Replaces |
|---|---|---|
| `acwr_acute_days` | 7 | `garmin.py` `ACUTE_WINDOW_DAYS` |
| `acwr_chronic_days` | 28 | `garmin.py` `CHRONIC_WINDOW_DAYS` |
| `pmc_ctl_days` | 42 | (new) CTL time constant |
| `pmc_atl_days` | 7 | (new) ATL time constant |

`CHRONIC_WEEKS` stays derived (`chronic/acute`). The `garmin.py` header comment
"Standard constants, not tunables" (L24) becomes false and must be rewritten to
say these are config-backed with the calibration caveat below. Ramp rate stays a
fixed 7-day delta — "per week" is its definition, not a tunable window.

**Derivation pad (finding #4).** Rev. 1's `DERIVATION_PAD_DAYS = chronic` is
under-padded two ways:

1. **CTL needs ~1.5 × τ_ctl ≈ 63 days** of prior data to be converged at the
   left edge of a displayed window; a 28-day pad neither pulls nor warms enough
   history for it.
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
under the new constants, and the fingerprint shift (§5.4) refreshes cached
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
- **`db/wipes.py` `wipe_garmin_data()` must call `recompute_derived()` at the
  end (finding #5).** It deletes activity/metrics rows by range and *never*
  recomputes today — for ACWR the resulting staleness is bounded (flat 28-day
  window, so only ≤28 days after the wiped range read stale values until the
  next pull). An EWMA has no such bound: deleted load stays baked into
  `ctl_{d}`/`atl_{d}` for **every** subsequent day, forever, until some future
  pull happens to sweep. This is an existing latent issue for ACWR that PMC
  turns unbounded, so the fix belongs here. `recompute_derived()` reads the
  post-delete DB, so calling it after the deletes (still inside, or right after,
  the connection block) fully re-derives the surviving span. Cost is one extra
  full sweep per wipe — wipes are rare and manual; acceptable.

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
CTL/ATL/TSB/ramp alike). Concretely the line becomes, for a fully-populated,
past-warm-up row:

```
- 2026-07-02: RHR=52bpm, HRV=61ms, Sleep=78, Stress=31, ACWR=1.12, CTL=62.4, ATL=71.7, TSB=-8.9, Ramp=+4.2/wk
```

- The four PMC fields are omitted wholesale during the §3.3 warm-up window and
  for any pre-recompute NULL row.
- **Ramp is included here (finding #3):** generate (`engine.py` L557) and adapt
  (L792) are the prompts that actually *write and adjust weekly TSS* — the
  subject of failure "progression is unguided" — yet rev. 1 put ramp only in the
  data summary. Surfacing it per-day means the LLM never has to subtract two CTL
  values seven lines apart. Ramp uses the §3.1 lookup/omit rule.

This reaches the workout generate and adapt prompts, so adapt now sees the
fatigue trajectory (fixing failure #1) and the ramp (fixing "progression").

### 5.2 Data summary (`service.py` ~L96, feeds strategy/plan prompts)

After the ACWR line add:

```
- Fitness/Fatigue (PMC): CTL 62.4 (fitness), ATL 71.7 (fatigue), TSB -8.9 (form)
- CTL ramp rate: +4.2/week (last 7 days)
```

Plan generation can now reason about sustainable build rates and *anchor* a
taper to the athlete's real current freshness (fixing "progression"; anchoring —
not solving — the taper).

**Optional cheap partial for the taper (finding #2), gated to A-event plans.**
A *zero-further-training* projection is pure decay math — no planned-TSS
decisions, no forward-simulation of workouts:

```
ctl_event = ctl_today * (1 − 1/τ_ctl)^days_to_event
atl_event = atl_today * (1 − 1/τ_atl)^days_to_event
- Event-day TSB if no further training from today: +18   (hard floor: real taper keeps some load, so true TSB ≤ this)
```

It gives the LLM a concrete anchor for the +5..+25 target instead of pure
mental arithmetic, while staying honest that it's a bound, not the plan's actual
projected TSB (that's phase 2). Ship it only if it lands cleanly; it is not a
blocker for the descriptive half.

### 5.3 Weekly digest (analysis, `service.py` ~L2279)

Alongside `max_acwr` add per week: `end_ctl` (CTL on the week's last day),
`week_ramp` (`end_ctl` − CTL 7 days earlier), `min_tsb`. Three numbers give the
analyze pass the multi-week fitness trajectory and each week's overload depth
without per-day noise.

Two guards (findings #1 and the boundary smaller-finding):

- **Warm-up:** weeks entirely inside the §3.3 warm-up window emit `None` for all
  three (the digest formatter omits them) — otherwise every bootstrap narrates a
  phantom overreach block from seeding artifacts.
- **Week boundary:** `week_ramp` reaches into the *previous* week's slice for the
  `−7d` CTL. The first week of an analysis window has no prior week → `week_ramp`
  is omitted (None-guarded), and it is exactly where the warm-up artifacts would
  otherwise live, so the two guards reinforce.

### 5.4 Evidence fingerprint (`engine.py` `met_digest`, L336–338)

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
- **`color_tsb()` (new, `util.py` beside `color_acwr` L91) colors only the two
  unambiguous risk ends:** `< −30` red (excessive fatigue), `> +25` yellow
  (detraining / over-tapered). **Everything between −30 and +25 stays
  uncolored** — including the +5..+25 "race-ready" band, which rev. 1 colored
  green. Finding #8: the very argument rev. 1 used to leave −30..+5 uncolored
  ("whether this is good is phase-dependent; that judgment belongs to the
  coach") applies just as hard to +5..+25 — mid-build, a TSB of +15 means
  fitness is *decaying* (the science file's "resume building" case), so a green
  "you're great" color would be wrong. A color map has no phase context; so we
  drop green and color only the two ends where the reading is unambiguous
  regardless of phase. (The small signal loss is deliberate — the coach, which
  *has* phase context, does the interpretation.)
- **Ramp bands must touch (finding #8):** `≥ 8` red, `5–8` yellow (i.e.
  `5 ≤ ramp < 8`), else plain. Rev. 1's "`>8` red, `5–7` yellow" left 7.5
  rendering plain between a yellow 6.0 and a red 9.0. No green band: a low ramp
  is correct during a taper, so green would wrongly bless it.

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

- Idempotent `ALTER TABLE` (§4) — no data migration.
- The next `tm data pull` (or any `recompute_derived()` path) back-populates PMC
  for the entire cached history in one sweep. Old analysis caches refresh
  naturally via the fingerprint change (§5.4).
- **Docs:** update `ARCHITECTURE.md` (AGENTS.md L2–3 requires it) — the
  derived-metrics `garmin.py` row (~L132, add CTL/ATL/TSB to "the load model")
  and the wipe/analysis-cache note (~L478, `wipe_garmin_data` now recomputes).
  Note the four new config params where `config_template.yaml` is documented.

**Out of scope, deliberately:**

- **Forward projection** — simulating CTL/ATL over *planned* workouts' planned
  load to answer "will TSB land in +5..+25 on event day?" deterministically.
  It's the natural phase 2 (feeds a single "projected event-day TSB: +12" line
  into plan/adapt prompts) but needs planned-TSS quality decisions that don't
  block shipping the descriptive half. Backward-looking PMC plus the §5.2
  zero-training bound already anchors the taper and fully fixes the other §1
  failures. (This design deliberately does **not** claim to *solve* the taper —
  see §1.)
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
- **Warm-up suppression:** rows inside the first τ_ctl days emit no PMC fields in
  the per-day line, weekly digest, and status line.
- **Ramp derivation:** at a span edge (<7 days of history → ramp omitted, not
  garbage); at an interior d−7 gap (nearest-earlier fallback).
- **Status-line NULL rendering:** NULL/warm-up CTL/ATL/TSB render `—`, never
  `0.0`.
- **Color bands touch:** `color_tsb` and the ramp bands have no uncolored hole
  (e.g. ramp 7.5 is yellow, TSB −30..+25 uncolored, TSB −31 red, +26 yellow).

DB integration (temp DB):

- one `recompute_derived()` run: activity-only days contribute load but create
  no metrics rows; existing rows get PMC values.
- **COALESCE regression:** a metrics-only re-pull (no activity change) must
  **not** null previously-stored PMC values — the whole persistence story (§4)
  relies on this and nothing else pins it.
- **`wipe_garmin_data()` recomputes:** after a dated wipe, no surviving row past
  the wiped range still carries load-through-the-gap in its CTL/ATL (finding #5
  regression).

---

## 9. Review resolutions

Traceability against `DESIGN_pmc_fitness_fatigue_review.md`. Every blocking and
structural finding is resolved in the body above.

| # | Finding | Resolution |
|---|---|---|
| 1 | Zero-seeded EWMAs feed ~6 weeks of garbage to the LLM | §3.3 warm-up suppression (`None` for first τ_ctl days at every surface); §5.1/§5.3/§6 apply it; `_warn_manual` mentions it |
| 2 | "Fixes all four failures" overclaims the taper | §1 rescoped to "anchored, not solved"; §5.2 adds optional zero-training TSB bound; §7 keeps forward projection as phase 2 |
| 3 | Ramp never reaches generate/adapt prompts | §5.1 puts ramp in the per-day line (reaches generate L557 & adapt L792), not only the data summary |
| 4 | `DERIVATION_PAD_DAYS = chronic` under-padded twice | §3.4 pad = `max(chronic, 28, ceil(1.5·τ_ctl))`; 4a warm-up note added to `_warn_manual` |
| 5 | `wipe_garmin_data()` never recomputes → unbounded EWMA staleness | §4 adds `recompute_derived()` to the wipe; test in §8 |
| 6 | None-guard claim misread the code (NULL acwr *crashes*) | §5.1 corrects it; one omit-if-None convention for the whole line (acwr, rhr included); test in §8 |
| 7 | Status NULL/zero-fill lies (`TSB 0.0` looks neutral) | §6.1 renders `—`, never `0.0`; interior-gap ramp rule in §3.1; §6.2 CSV emits blanks |
| 8 | Color band hole + green contradicts own reasoning | §6.1 ramp bands touch (`5–8` yellow, `≥8` red); green +5..+25 band dropped, only the two unambiguous ends colored |
| — | Double cache invalidation; hash all three | §5.4 hashes ctl/atl/tsb; two-refresh window accepted & documented |
| — | Config-change staleness; `garmin:` vs `coach:` | §3.4 params under `garmin:`, staleness accepted & documented; "not tunables" comment to be rewritten |
| — | Span end contradicts §3's own text | §3.1 span ends at `max(last activity, last metrics)` |
| — | Missing §4 header (dangling storage bullets) | Storage promoted to its own §4 |
| — | Type inconsistencies (`date` vs `str`) | §3.2 signature is ISO `str` throughout |
| — | "zone-rehierarchy funnels through recompute_derived" | §3.2 names real callers: `pull()`, `backfill_tss()` |
| — | ARCHITECTURE.md absent from rollout | §7 adds it (rows ~L132 and ~L478) |
| — | Weekly-digest ramp crosses week boundaries | §5.3 None-guards the first week; reinforced by warm-up guard |
| — | Missing tests (empty DB, COALESCE, d−7 gap, NULL status) | all added in §8 |
