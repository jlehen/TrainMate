# Design: Progress Timeline (past + projected training progression)

**Status:** Shipped; simplification pass (rev 9) · **Date:** 2026-08-03 ·
**Companion to:** ARCHITECTURE.md §12 (load model),
`DESIGN_pmc_fitness_fatigue.md` (the **shipped** backward PMC core this feature
consumes — and whose deferred Phase 2 projection this feature delivers, §4),
`DESIGN_backward_evaluation.md` (the analysis-side view of the past; this doc is
the *presentation*-side view of past **and future**)

> **Rev 9 (2026-08-03) — the simplification pass.** A review of the shipped
> feature against real data (six months of activities under a 28-day rolling
> plan) cut machinery that could not earn its keep and fixed what it had been
> hiding. Seven changes:
> (1) **Version-in-force governance is cut** (§6.1). A week now shows a planned
> total iff it *contains non-removed workout rows* — the same rows the total is
> summed from, so the two can never disagree. The rule it replaces resolved,
> per objective, which macrocycle version was in force when the week ended,
> over a bespoke `db.get_governance_versions()` that deliberately collected
> superseded versions. On the real database both live versions were created the
> same day with **identical** mesocycle spans, so the rule had no discriminating
> power at all — and it was incoherent with the data it gated, because
> `get_workouts()` excludes *archived* rows (precisely the superseded version's
> workouts), so a week "governed" by a superseded plan rendered `plan 0`,
> asserting the plan asked for nothing. Rev 7 cut the label half of the same
> version archaeology as "machinery out of proportion to the harm"; the
> governance half fails the same test. `get_governance_versions()` is deleted.
> (2) **The plan-start boundary is fixed** (§3). The elaborate rule above was
> licensing arithmetic nonsense: a plan beginning on a Thursday left the week of
> 07-27 dividing one planned day (72 TSS) by seven trained ones (344) and
> printing **477%**. §3's in-progress rule guarded *today* and the partial-final
> week guarded *plan end*; nobody guarded the week the plan *starts*. Generalised
> to one rule — a week gets an adherence percentage only when the plan covers
> every day it is being compared over — carried in the payload as
> `partial_plan`. `*` correspondingly means "this row's planned figure spans
> fewer than seven days", which is in-progress and plan-edge alike; the footnote
> names whichever edge falls in view (`plan starts 08-01 (Sat)`).
> (3) **The CLI≡endpoint equivalence test is deleted** (§9). Once `timeline.py`
> made both surfaces call one builder, the test asserted `f(db) == f(db)` and
> could not fail. The shared function *is* the pin.
> (4) **Warnings are structured** (§6.0): `{code, text, command?}` instead of
> prose. The CLI recognised the plan-gap by prefix-matching its wording, skipped
> it, then recomputed `plan_gap()` to draw it richly — and scanned warning text
> for a backtick-quoted command name against a hardcoded registry. `plan_gap` is
> now its own payload field; renderers dispatch on `code`.
> (5) **One windowing implementation** (§7.1). "Which weeks does `--weeks` show"
> lived in three places — the text table, `clip_payload_for_weeks(cap_future)`
> for the chart, and `--blocks`. All three now call `progression.select_weeks`.
> (6) **The zone tables move behind `-z`/`--zones`** (naming a sport implies it).
> They tripled a numbers-first command from ~27 lines to 96 at phone width and
> answer a different question from the load table.
> (7) Leftovers: `zero_load_workout_count` is dated from today, so a past row
> nobody can fix no longer keeps the banner permanently lit; the FORM line prints
> CTL/ATL at one decimal to match the TSB beside it and `tm status`; the config
> `sport_preferences` check only runs when zone tables are asked for; and
> `--blocks` no longer crashes on `get_previous_macrocycle()` (its hand-rolled
> test stub had the wrong signature, so the suite passed while the flag died on
> every real database).
>
> **Rev 8 (2026-07-09) — the renderer pass.** The rev-7 payload survived first
> contact with real data; its two *renderers* did not. Nothing in §3–§6 changes;
> §7.1 and §7.2 do. Five fixes, all presentation:
> (1) **The per-row meso column is replaced by a band rule** (§7.1). At 8
> columns it truncated every real mesocycle name to mush — `Specifi…` on five
> consecutive rows — while costing a sixth of the width budget. The label is now
> written once, in full, on a rule spanning the table above its weeks.
> (2) **Bars became bullet bars** (§7.1). The old bar drew absolute actual load
> and *not* plan-vs-actual, which is the one comparison the table exists for;
> adherence lived only in the pct column. A `│` tick now marks plan on the same
> scale, so overshoot and shortfall read at a glance.
> (3) **Future weeks draw a ghost bar** (`▒` to planned) instead of the literal
> text `(planned)`. This also closes a real defect: future weeks fed `scale_max`
> but drew nothing, so a big September week silently squashed June's bars.
> (4) **The projected half is windowed by `--weeks` too**, with `--weeks all`
> for the whole plan and a legend note naming what was dropped. A 12-row
> `(planned)` tail buried the four rows carrying measured data.
> (5) **`PMC_TSB_LAG_NOTE` moves behind `--explain`** (§7.1). A two-line caveat
> printed on every invocation is a caveat nobody reads.
> (6) **The `CTL Nw` sparkline label counts cells drawn**, not the window asked
> for — rev 7 rendered `CTL 8w ▁` on a one-week-old DB (§7.1).
> Chart-side (§7.2), the same pass fixed two drawing bugs the rev-7 label work
> introduced: meso labels centred at full length in narrow spans overprinted
> each other, and a plan generated *to* an objective drew two rotated labels on
> the same x.

> **Rev 7 (2026-07-08) — implementation-review scope cuts.** Three decisions
> from the rev-6 implementability review:
> (1) **The v1 web tab shows the PNG.** The interactive uPlot tab is deferred
> whole to a follow-on (§8.5). V1's Progress tab is an `<img>` on
> `GET /api/timeline.png` (§6), drawn by the same renderer as the Telegram
> photo — one chart drawing instead of rev 6's two "accepted-cost"
> hand-maintained renderings. The renderer is extracted to
> `trainmate/chart.py` (§7.2) so CLI and web share it, and the JSON endpoint
> now ships *with* its first consumer (§8.5) instead of ahead of any.
> (2) **Superseded-version *labels* are cut** (rev 6's §6.1 layer 2):
> exhuming old plan versions via `macrocycles.created_at` to put a cosmetic
> name on a past week was machinery out of proportion to the harm of that
> label falling back to `~inferred`/`—` — the §3 standard this doc applies
> elsewhere. **Governance is untouched**: whether a week shows planned
> totals and an adherence percentage still follows the version-in-force
> rule, which keeps the `created_at` pin (§6.1) — that half is load-bearing.
> (3) **Branch-state inventory** (§10.1): the rev-4 snapshot on this branch
> already implements parts of §10 in older form (public `planned_load`, the
> photo transport, `tm progress` + menu entry, the `p`-alias removal,
> matplotlib in requirements, `--chart`); the rollout now says per step what
> is done, what is reworked, and what is new — so nobody hunts for a
> `_planned_load` that is already public. Stale references fixed (rounding
> at garmin.py:632; the pre-snapshot `p`-alias line), and the §6.1
> governing-objective lookup is flagged as a **new** db helper.
>
> **Rev 6 (2026-07-08) — review decisions (all rev-5 review findings).**
> The five critical holes, resolved:
> (1) **The anchor is strictly before today.** A morning auto-ensure pull
> writes today's metrics row (load 0) before the evening session happens;
> anchoring the fold on it silently dropped today's planned session from the
> projection (~12 TSB too optimistic per 100-TSS session) and made the CLI
> (which pulls) disagree with the pure-reader endpoint. Today is now always
> fold territory per the §3 today-rule; the `tm status` ≡ `tm progress`
> invariant is correspondingly restated: TSB always identical, CTL/ATL
> identical once today's load has synced, and the FORM line tags today's
> source (§7.1). (2) **Full-precision storage.** `compute_pmc()` stops
> rounding its outputs; rounding moves to display (where every consumer
> already formats `:.1f`, and where acute/chronic/ACWR storage already set
> the precedent). The seed is thereby exact and the §9 split/refold test can
> legitimately demand exactness — with 1-dp anchors it was flaky by
> construction. The shipped-core change count is now two (seed + unrounding),
> not one. (3) The anchor skips trailing rows with **NULL PMC columns** (the
> interrupted-pull state, PMC design §5.1). (4) **Plan end = last generated
> workout** — a manually-added far-future placeholder no longer drags a
> months-long decay ghost (§3). (5) **The lapsed plan** (plan end < today,
> the rolling horizon outrun) is a specced fourth empty state (§3).
>
> Consistency batch: payload assembly is shared via `assemble_timeline()` —
> callers fetch rows only — pinned by a CLI≡endpoint equivalence test
> (§5/§9); §6.1 gains earlier-objectives plan layers, a label-independent
> governed-week rule, the `created_at` timestamp pin, and band trimming; the
> objective-selection divergence vs Phase 2 is acknowledged as deliberate
> (§6.1); the bootstrap cache's `--force` staleness is stated honestly, with
> defensive parsing and corrected path/field names (§6.1); the config-edit
> seam kink is an accepted limitation (§3); a plan with no history renders
> its planned bars instead of a blank tab (§3). Edge cases, pinned without
> machinery: elapsed-planned counts today only once synced, straddling
> weeks/bands return whole, zero-max bars / blank and flat sparklines /
> `--weeks ≥ 1` defined, the 48-col budget covers every line via
> `visible_len` (mock redrawn), `planned_load` treats explicit `tss=0` as 0
> with the warning wording aligned, a mid-week plan end marks the partial
> final week, completed objectives keep their flags (§6/§11).
>
> **Rev 5 (2026-07-07) — realigned on the shipped PMC core.** Between rev 4 and
> this revision, the PMC fitness/fatigue feature merged to main
> (`DESIGN_pmc_fitness_fatigue.md`, merge `ca8591b`): `garmin.compute_pmc()` now
> computes CTL/ATL/TSB over full history on every `recompute_derived()` sweep,
> stores them on `athlete_metrics_cache`, and surfaces them in the coach prompts,
> `tm status`, and `tm data show-metrics`. That obsoletes rev 4's §4 ("new math,
> additive"): this feature no longer computes anything about the *past* — it
> reads the stored series (recomputing under different conventions would make
> `tm progress` disagree with `tm status` on the same day's CTL, a variant of
> the very seam-lie §3 exists to prevent) — and its projection becomes a forward
> fold of the same recurrence seeded from the last stored row, which is exactly
> the PMC design's deferred **Phase 2**, generalized from one event-day number
> to the full daily series. Consequences threaded through: τ constants come from
> config (`pmc_ctl_days`/`pmc_atl_days`), not module constants; rev 4's
> mean-seeding is **superseded** by the shipped zero-seed + warm-up-blanking +
> young-DB-caveat scheme (rationale preserved in §4); the §3.3 warm-up rules and
> `pmc_data_caveat` flag are adopted wholesale; `compute_pmc()` gains an
> optional `seed` parameter (the single change to the shipped core). The rev-4
> implementation snapshot on this branch predates the PMC merge (see
> `CODE_REVIEW_progress_timeline.md`) and is reworked to this rev before merge.
>
> Rev 4 (implementability review): photo caption carried in the TM-PHOTO
> payload, CLI weekly bars as absolute load on a shared max-anchored scale,
> table fixed-width at the 48-col budget, sparkline sampling pinned, endpoint
> returns all active objectives with renderers clipping. Rev 3: projection
> clamped to the generated plan, planned-side sRPE fallback via `adherence`,
> layered mesocycle labels incl. the bootstrap reconstruction, in-progress-week
> rule, empty states, CLI auto-ensure.

A single continuous timeline that shows how training load has actually
accumulated and where the current plan takes it: **past days are measured
load** (from `completed_activities`), **future days are planned load** (from
`workouts`), and one fitness/fatigue model runs across the seam. The
centerpiece is a projected Performance Management Chart — CTL (fitness), ATL
(fatigue), TSB (form) — read from the stored PMC series up to the last pull
and continued forward over the plan as far as workouts have been generated
(§4), so the user can
see *whether the plan as currently written delivers peak fitness with
positive form on race day*. Below it, weekly planned-vs-actual load bars
labeled by mesocycle make the periodization wave and adherence visible at a
glance.

V1 ships on **all three front-ends**: `tm progress` (text, numbers-first),
Telegram (same text for free via CLI parity, plus the full chart as a PNG
photo), and a web **Progress** tab that frames the **same PNG** (the
interactive rendering is a follow-on, §8.5). One computation (§5) and one
chart renderer (§7.2) feed all three; only the delivery differs (§7).

---

## 1. Motivation

TrainMate holds both halves of the progression story but never draws them in
one place:

- The **past** is visible as tables (`workout compare`, the History tab) and —
  since the PMC merge — as *today's* CTL/ATL/TSB numbers (`tm status`, the
  coach prompts). But there is still no trend: "am I fitter than in April?"
  means scanning `tm data show-metrics` rows and integrating by eye.
- The **future** is visible only as a list of workouts and a mesocycle
  strategy blob — the periodization wave (build/recover, volume ramp) exists
  in the data (every planned workout carries `tss`) but the user cannot *see*
  it.
- Nothing connects them. The stored PMC series stops at the last pull; nothing
  projects it forward over the plan — the PMC design defers exactly this as
  its Phase 2 ("forward taper projection"), and this feature is where it ships
  (§4). "Am I on track?" currently requires reading the plan, the compare
  view, and the metrics table and doing the integration in one's head.

The data is already sufficient — no new ingestion, no LLM calls, no schema
change (the `ctl`/`atl`/`tsb` columns already exist and are populated, §4). As of writing: ~6 months of completed activities, a TSS-annotated
plan through the first objective (2026-09-30), 9 mesocycles, and three active
objectives spanning to 2027-01-31. Note that the plan reaching the objective
is a **snapshot, not an invariant**: `workout generate` defaults to a rolling
`workout_generation_span_days` (28-day) horizon, so most of the time workouts
stop well short of race day unless `--until-goal` was used — §3's plan-end
rule exists for exactly this. This feature is a pure read-side derivation.

## 2. What the picture shows

The canonical layout — two panels on a shared time axis (default window:
8 weeks back → **max(today, plan end)**, plan end being the last generated
workout date, §3). The Telegram photo and the web tab (§7.3) show the same
§7.2 PNG rendering of it; the CLI (§7.1) is a numbers-first projection of
the same content:

```
  CTL/ATL/TSB ──────────────────────┬────────────────────────
                          solid     │ dashed (projection)   🏁 objective
       CTL ───────────────────────╮ │ ╭╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┄ plan ends
       ATL ∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿ │ ∿∿╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┄ 07-31
       TSB ·························│····················┄
  ──────────────────────────────today──────────────────────────
  weekly load ▐planned▌▐actual▌     │ ▐planned only▌
       bars   ██▓▓ ██▓▓ ██▓▓ ██▓▓   │ ██   ██   ██   ▄▄
  meso band   [~Base ][ Build 2     ][ Build 3 ][ Peak ]
```

- **Top panel (daily):** CTL, ATL, TSB lines. Solid up to today, dashed
  beyond, **ending at plan end** — never extrapolated past the last
  generated workout (§3). Vertical "today" rule; flag markers on objective
  `target_date`s that fall inside the window. Days inside the PMC warm-up
  window carry no values and render blank (§4) — with the default 8-week
  window and months of history the cutoff is normally off-canvas.
- **Bottom panel (weekly):** for past weeks, planned TSS and actual load as
  paired bars (adherence at a glance); for future weeks, planned only. The
  mesocycle band beneath labels each week's block via the layered lookup
  (§6.1): plan mesocycles where a plan governed the week, bootstrap-inferred
  blocks (rendered hatched/lighter, `~`-prefixed) for pre-plan history.

The projection is the point of the feature: because `workout adapt` and
`workout generate` rewrite future `workouts` rows, the dashed half answers
"what does the *current* plan do to my fitness" — and visibly moves when the
plan is adapted.

## 3. Load semantics — one currency across the seam

The past and future halves must be in the same units or the seam is a lie.

- **Past days** (`date < today`): daily load = Σ `garmin.activity_load(act)`
  over that day's `completed_activities`. This is the canonical derived load
  (power TSS → hrTSS → sRPE fallback, §12), **not** the stored `tss` column —
  the same currency the ACWR pipeline already uses, and byte-identical to the
  `daily_load` series `recompute_derived()` feeds `compute_pmc()`, so the
  load bars and the stored CTL/ATL/TSB they sit under agree by construction.
  This also absorbs the ~24 activities with NULL `tss` without special-casing.
- **Future days** (`date > today`): daily load = Σ `adherence.planned_load(w)`
  over that day's non-`removed` `workouts` rows — the coach's `tss` when set,
  else sRPE (RPE × 10 × hours) from `rpe` + `duration_minutes`. This is the
  *existing* planned-side valuation the adherence verdicts already use
  (`adherence._planned_load`, promoted public in step 1 of §10); inventing a
  stricter "NULL tss → 0" rule here would make the weekly bars disagree with
  the adherence percentages shown beside them. One fix folded into the
  promotion: `_planned_load`'s `if tss:` treats an explicit `tss=0` as
  unset and falls through to sRPE — the public `planned_load` tests
  `tss is not None` (an explicit 0 means 0; a one-line behavior change to
  adherence, pinned by a test). Non-`rest` rows that value to **0** —
  no usable TSS and no RPE+duration — are counted into the payload
  `warnings` field, worded to match that predicate: *"2 planned workouts
  lack TSS/RPE — count as 0"* (mirroring the aggregated-underestimate
  pattern of §12). `rest` rows are excluded — a rest day legitimately has
  no load.
- **Today**: actual if any completed activity **with load > 0** exists for
  today, else planned (a zero-load activity — no power, HR, or RPE — must
  not suppress a planned session). Accepted approximation: a two-session day
  where only one is done yet counts actual-only until the second syncs.
  This rule governs the projection too: even when a metrics row for today
  already exists (a morning pull writes one before the evening session has
  happened), the fold values today by this rule — the stored today-row never
  anchors the projection (§4).

**Plan end.** The projection runs exactly to the last non-removed
**generated** workout date (`workouts.source = 'generated'`) and stops — no
zero-fill beyond it (a decaying ghost line would read as fitness collapse,
which is a statement about missing data, not the plan). Manual rows
(`workout add` / `POST /api/workouts`) count toward daily loads inside that
range but never *extend* it: one manually-added race-day placeholder months
out would otherwise drag plan end there and draw the projection over months
of assumed rest — the exact ghost this rule forbids, through the front door.
Non-removed workouts dated after plan end are counted into `warnings`
("1 workout beyond plan end — not projected"). Fallback: a DB with no
generated workouts at all (fully manual planning) uses the last non-removed
workout of any source. A plan ending mid-week leaves the final future
week's planned total genuinely partial (Mon–Wed only); that week takes
`partial_plan` and its `*` under the comparable-days rule above — rather than
being resliced or hidden: the bar stays honest, the marker explains it.
When plan end < the next objective's `target_date`, every surface annotates
the gap — payload `warnings`, CLI banner, chart label: *"plan generated
through 2026-07-31 (9 wks before objective)"* — and per-objective projected
CTL/TSB figures are shown **only** for objectives the plan actually reaches.
The CLI hint names the fix (`workout generate --until-goal`).

**Comparable days (rev 9).** A percentage is only honest when its two halves
cover the same days. Two ways a week fails that, one rule for both:

- *The current week* is neither past nor future: comparing a full-week planned
  total against a partial actual reads as poor adherence every Monday. Its
  planned figure covers **elapsed days only** — Monday through yesterday, plus
  today **iff today's load has synced** (today's §3 source is `actual`).
  Including an unfinished today would make an evening-training athlete read
  <100% all day, every day, and would count today *against* actual where the
  today-rule counts it *in lieu of* actual. The payload carries `planned_load`
  (full week), `planned_load_elapsed`, and `in_progress: true`.
- *A week a plan edge falls inside* — the week the plan **starts** or **ends**
  mid-week — has a planned total spanning fewer days than its actual does. It
  carries `partial_plan: true` and renders **no percentage at all**: there is
  no elapsed-style slice to fall back on, because the missing days are missing
  from the plan, not from the calendar. Shipped without this guard, a plan
  beginning on a Thursday printed `72 … 344 477%` — §3's own Monday-morning lie
  at the other boundary (rev 9).

Both mark the row `*`, one marker with one meaning: *this row's planned figure
spans fewer than seven days*. The footnote names whichever plan edge is in view
(`plan starts 08-01 (Sat) · plan ends 08-27 (Thu)`), so the marker says *that*
the row is partial and the footnote says *why*.

**Empty states** (all reachable on a fresh install; none may crash):

- *No completed activities at all* (fresh install — a plan may already be
  generated) → no past series and no PMC (no anchor, §4), but the planned
  future still renders: weekly bars for planned weeks, PMC panel suppressed,
  and the warning "no activity history yet — run `tm data pull` first" on
  every surface. A blank tab would be strictly less useful than showing the
  plan about to start. (Rev 5 said "empty `days`/`weeks`", contradicting
  §4's "the weekly bars don't depend on PMC and render regardless" — the §4
  reading wins. Consequence for §5 `daily_loads`: the series starts at
  min(first activity, first planned workout), since "first activity" alone
  is undefined here.)
- *No planned workouts* → the classic past-only PMC: solid lines to today,
  no dashed segment, window ends today, note "no plan generated — projection
  unavailable". As with the plan-end banner, the CLI names the fix:
  `plan generate` (preceded by `data bootstrap` if never run — which also
  lights up the §6.1 inferred meso labels). `data bootstrap` cannot
  substitute for a plan here: it reconstructs the past, it generates no
  future workouts, so it enriches this state but never adds the projection.
- *Plan lapsed* (plan end < today — the rolling 28-day horizon outrun by a
  few weeks without `workout generate`) → same shape as *no planned
  workouts*: solid lines to today, no dashed segment, window ends at today
  (hence the §2 default `max(today, plan end)`), no per-objective projection
  lines, banner "plan lapsed 2026-05-20 — run `workout generate`". Past
  weeks keep their planned/actual bars and percentages — the plan existed
  when they ran; only the projection is absent.
- *Weeks the plan never covered* (pre-adoption history inside the window, or
  a week whose every planned row was removed) → actual bar renders, planned
  shows `—`, **no adherence percentage** (never divide by zero) — matching
  `adherence.py`'s precedent of treating activity outside planned coverage as
  informational, not a deviation. "Covered" means the week holds at least one
  non-removed workout row: the same rows the planned total is summed from, so
  the flag and the figure cannot disagree (rev 9 — see §6.1).

**Accepted limitations (v1):**

- Planned `tss` is an LLM estimate; measured `activity_load` is
  instrument-derived. If the plan systematically over- or under-states TSS,
  the projection inherits that bias. A display-time calibration layer
  (scaling planned loads by the historical actual÷planned ratio) was
  considered and **rejected** as too indirect: if the coach's TSS estimates
  are biased, the fix belongs in plan generation, not in rescaling at render
  time. The projection stays *consistent* with the coach's own beliefs —
  the same numbers already drive adaptation decisions.
- Relatedly, actual loads include the RPE-divergence bump (§12): weeks heavy
  in sessions where RPE overrides the measurement (typically strength) can
  legitimately show >100% of planned. Accepted for the same reason: the
  number is honest, and the bias source is upstream of this feature.
- Days with no Garmin data (sync gap, vacation, dated wipe) are
  indistinguishable from genuine rest: they zero-fill and CTL decays as if
  the athlete rested. `sync_state` watermarks can't disambiguate per-day.
- Between a config edit (the τ constants, or the thresholds feeding
  `activity_load`) and the next pull, the stored past line and the
  live-computed bars/fold run under different constants — the projection
  can visibly kink where it meets the stored line. Transient and
  self-healing: the CLI auto-ensures (a pull re-sweeps everything under the
  new config); the web tab is a pure reader and shows the kink until the
  next pull happens elsewhere. Detecting the mismatch (config fingerprints
  on stored rows) would be machinery out of proportion to the harm.

## 4. Fitness/fatigue model — consume the shipped PMC core, fold it forward

The backward half of this model **already ships** (`DESIGN_pmc_fitness_fatigue.md`,
merged `ca8591b`): `garmin.compute_pmc()` walks every calendar day of history
with the classic Coggan discrete `1/τ` recurrence

```
CTL_d = CTL_{d-1} + (load_d − CTL_{d-1}) / τ_ctl    # config pmc_ctl_days, default 42
ATL_d = ATL_{d-1} + (load_d − ATL_{d-1}) / τ_atl    # config pmc_atl_days, default 7
TSB_d = CTL_{d-1} − ATL_{d-1}          (form going *into* day d)
```

and `recompute_derived()` stores the result on `athlete_metrics_cache`
(`ctl`/`atl`/`tsb` columns, full-sweep on every pull/backfill/wipe path). Those
stored numbers already reach the coach prompts, `tm status`, and `tm data
show-metrics`. This feature therefore computes **nothing new about the past**
and gains a hard consistency requirement instead:

- **Past half: read, don't recompute.** Past DayPoints — dates strictly
  before today; today belongs to the fold below — take `ctl`/`atl`/`tsb`
  verbatim from the stored metrics rows. `tm progress` and `tm status` must
  show the *same* CTL for the same past day — two implementations of
  "fitness on day d" disagreeing across commands would be a variant of the
  seam-lie §3 exists to prevent. (For *today*, where the fold deliberately
  runs ahead of a stored load-0 row, the precise consistency contract is in
  §7.1.) Days with load but no metrics row (possible for trailing
  activity days past the last metrics pull) carry no PMC point; renderers
  join the line across the gap.
- **Warm-up rules adopted wholesale** (PMC design §3.3): points dated before
  `pmc_warmup_cutoff_for(history_start, τ_ctl)` carry `null` PMC values
  (leading-edge artifacts — same blanking `pmc_display_values` applies on the
  status line); when today's own history is short, the `pmc_data_caveat()`
  young-DB flag joins the payload `warnings` and the CLI output; and while
  the *entire* history is still inside the warm-up window the PMC panel is
  suppressed outright with the still-warming message — the weekly bars don't
  depend on PMC and render regardless.
- **Future half: the anchored fold.** The *anchor* is the latest stored
  metrics row dated **strictly before today** whose `ctl`/`atl` are non-NULL
  (date A, values `(CTL_A, ATL_A)`). Both qualifiers are load-bearing:
  - *Strictly before today.* A morning auto-ensure pull writes today's row
    with load 0 before the evening session has happened; anchoring on it
    would drop today's planned session from the projection entirely (one
    100-TSS session ≈ 12 TSB too optimistic for tomorrow) and make the CLI
    (which pulls, §7.1) systematically disagree with the pure-reader
    endpoint (§6) about the first projected days. Today is always fold
    territory, loaded per the §3 today-rule; a stored today-row is ignored
    by the projection (consistency consequence and the FORM-line source tag:
    §7.1).
  - *Non-NULL.* A pull that dies between `_ingest_metrics` and
    `recompute_derived()` leaves trailing rows with NULL PMC columns (PMC
    design §5.1); seeding from one is a `TypeError`. Skip back to the newest
    valid row; if none exists anywhere, that is the no-anchor state below.
  From the anchor, the same recurrence is folded forward over the §3 merged
  daily loads — actual for A+1..yesterday (so a stale anchor decays over
  what actually happened), the §3 rule for today, planned beyond — through
  plan end. Implementation: `compute_pmc()` gains an optional
  `seed=(ctl0, atl0)` parameter (default `(0.0, 0.0)`, pinned in
  `tests/test_pmc.py`), and `progression.py` calls it for the fold rather
  than owning a second copy of the recurrence.
- **Stored values become full-precision** — the second (and last) change to
  the shipped core: `compute_pmc()` drops the 1-dp rounding of its outputs
  (garmin.py:632); values are stored exact and rounded only at display —
  which every consumer already does (`:.1f` in status / show-metrics /
  prompt formatting), and which acute/chronic/ACWR storage already
  practices, so CTL/ATL/TSB rounding-at-storage was the odd one out. This
  makes the seed exact: a fold from any anchor reproduces the unbroken
  series bit-identically (the §9 refold test), where a 1-dp-rounded anchor
  carried up to ±0.05 of error that could cross display-rounding boundaries
  on arbitrary fixtures. Existing rounded rows self-heal on the next
  `recompute_derived()` full sweep (every pull).
- **Time constants come from config** (`config.pmc_ctl_days` /
  `config.pmc_atl_days`) — rev 4 pinned them as module constants, which is now
  wrong twice over: the config params exist (PMC design §3.4), and a τ
  mismatch between the stored past and the folded future would kink every
  line exactly at the seam.
- **No anchor** (no metrics row before today with non-NULL PMC columns —
  metrics never pulled, or only ever pulled this morning) → no PMC series;
  rendered as the young-DB state above: bars render, the PMC panel shows the
  message.

**This is PMC Phase 2, generalized.** `DESIGN_pmc_fitness_fatigue.md` defers
"forward taper projection" — event-day TSB folded over the plan's own workouts
— as Phase 2. The anchored fold above *is* that projection, generalized from
one event-day number to the full daily series, and §3 already carries its
honesty guards under other names: stale-anchor decay (the A+1..today segment),
removed workouts excluded, unquantified workouts warned (the
neither-TSS-nor-RPE → 0 warning), plan-not-reaching-the-event annotated (the
plan-end banner). One deliberate difference: where Phase 2 zero-fills
unplanned tail days up to the event and annotates, this feature **stops at
plan end** (§3) — a projection over assumed rest is a statement about missing
data, and the per-objective figures are only shown for objectives the plan
reaches. Phase 2's remaining deliverable — the projected event-day TSB *line
in the coach prompts* — is not part of this feature, but becomes a one-call
follow-on into the §5 fold (§8).

**Superseded (rev 4 → rev 5): mean-seeding.** Rev 4 specced its own recursion
seeded at the calendar-mean daily load of the first τ days of history, to
avoid the from-zero warm-up transient. The shipped core solves the same
problem differently — zero-seed + display blanking of the first τ_ctl days +
the derivation pad (§3.4) as the data backstop + the young-DB flag — and
running both conventions side by side would make the chart disagree with the
status line over exactly the region where the difference matters. Stored
convention wins; the mean-seed rationale stays recorded here as the rejected
alternative (it remains the better *single-surface* answer, but consistency
across surfaces outranks it).

**Relationship to existing acute/chronic workload:** unchanged from the PMC
design: the rolling-sum acute/chronic/ACWR stay the injury-risk vocabulary,
the PMC EWMAs are the fitness/form vocabulary, and both coexist the way they
do in the sports-science literature. (Rev 4 called CTL/ATL/TSB "a
presentation-side model" — no longer true: the backward core is coach-facing.
What *this* feature owns is presentation: the trend picture, the seam, and
the projection.)

## 5. New module: `trainmate/progression.py`

Pure functions, no singleton state — same shape as `trainmate/adherence.py`
and `coach/formatting.py` (the "pure helpers" precedent). Takes rows as
arguments, never touches `db` directly, so it is shared verbatim by all
three front-ends (§7). One purity caveat, same as `adherence.py`'s:
`garmin.activity_load` reads `config` thresholds and importing
`trainmate.garmin` imports the `db` singleton, so tests follow the existing
patch-before-import pattern (`tests/test_analysis.py` precedent) — the
functions are still deterministic given rows + config.

```python
DayPoint = dict  # {date, load, source: 'actual'|'planned',
                 #  ctl, atl, tsb: float | None}
                 # None inside the warm-up window, on days with no stored
                 # metrics row, or when there is no anchor (§4).
                 # tsb is day-ENTERING form (§4): CTL_{d-1} − ATL_{d-1}

def daily_loads(activities, workouts, today) -> list[DayPoint]
    # merged per-day load series per §3 (no gaps: zero-load days included,
    # from min(first activity, first planned workout) through plan end)

def fitness_series(day_points, metrics_rows, ctl_days, atl_days,
                   warmup_cutoff) -> list[DayPoint]
    # past (dates < today): ctl/atl/tsb copied verbatim from the stored rows,
    # nulled before warmup_cutoff (pmc_display_values semantics); from the
    # anchor (latest row strictly before today with non-NULL ctl/atl, §4):
    # garmin.compute_pmc(..., seed=(ctl_A, atl_A)) folded over the day_points
    # loads through plan end (§4). Never recomputes the past. The caller
    # fetches metrics_rows, the config τs, and the cutoff (via
    # garmin.pmc_history_start / pmc_warmup_cutoff_for) so this stays
    # row-in/row-out.

def weekly_aggregates(activities, workouts, today, meso_spans) -> list[dict]
    # {week_commencing (Monday, per learning_evidence precedent),
    #  planned_load, planned_load_elapsed?, in_progress?, partial_plan?,
    #  actual_load, meso_label?, meso_source?}
    # planned = Σ adherence.planned_load over non-removed workouts (the
    # *adapted* plan — "what the plan asked at the time"; original_tss is
    # reserved for the drift follow-on, §8). meso_spans is the §6.1 layered
    # span list, built by assemble_timeline below.

def assemble_timeline(activities, workouts, metrics_rows, mesocycles,
                      inferred_mesocycles, objectives, today,
                      ctl_days, atl_days, warmup_cutoff) -> dict
    # The ENTIRE §6.0 payload — days, weeks, meso_bands, objectives, plan_gap,
    # warnings — built here and ONLY here, from the helpers
    # above plus the §6.1 layered lookup. Callers do db reads and hand rows
    # in; neither the CLI handler nor the endpoint owns any assembly or
    # warning-wording logic. This is deliberate: the rev-4 snapshot let each
    # caller assemble its own payload and the two copies had already
    # diverged on when the plan-gap warning fires and how it is worded
    # (CODE_REVIEW finding #5). Purity and sharing are not in conflict —
    # this stays row-in/row-out.
```

## 6. Web API: `GET /api/timeline.png`

The v1 web surface serves the picture, not the data. Thin handler in
`trainmate_web.py`: `db` reads (`get_completed_activities`, `get_workouts`,
`get_metrics_cache` for the stored PMC rows (§4), the first-evidence dates
behind `garmin.pmc_history_start` for the warm-up cutoff, macrocycle
versions + mesocycles for labels and governance (§6.1), active objectives,
`get_analysis_cache("long")` for the bootstrap reconstruction) + one call
to `assemble_timeline` (§5) + one call to `chart.render_timeline_png`
(§7.2), returned as `image/png`. The handler fetches rows and renders the
result; it assembles nothing itself, and the CLI handler is the same shape,
so the two surfaces render one payload (§5).

A JSON endpoint (`GET /api/timeline`, serializing the §6.0 payload
verbatim) is **deferred to the interactive-tab follow-on** (§8.5): with the
v1 tab showing the PNG, a JSON API would ship with zero consumers. The
rev-4 snapshot's JSON endpoint is removed in the rework (§10.1).

Properties, consistent with the existing API's stance (§8 of
ARCHITECTURE.md):

- **Pure reader.** No `ensure_data`, no Garmin, no calendar, no LLM (reading
  the cached bootstrap reconstruction is a `db` read, not an analysis run).
  Data freshness is already surfaced via `sync_state` on the Dashboard.
- **No caching.** Recomputed and re-rendered per request — a few hundred
  rows of arithmetic plus one Agg figure. Deliberately *not* an
  `analysis_cache` slot: the projection must move the instant
  `adapt`/`generate`/`swap`/`remove` rewrite future workouts, and a
  fingerprint scheme would just re-derive "did anything change" at higher
  complexity than recomputing.
- Query param `?weeks=N` re-windows the past half exactly like the CLI's
  `--weeks` (default 8; must be ≥ 1, else 400 — the argparse rule of §7.1,
  mirrored); `?weeks=all` extends to full history. The future half always
  runs to plan end (§3).
- **matplotlib absent** (it is optional-tier, §7.2) → HTTP 503 with the
  same install hint the CLI prints; the tab shows it verbatim (§7.3).

### 6.0 The timeline payload

`assemble_timeline`'s output dict — the single internal contract every
renderer consumes: the CLI formatter, the PNG renderer, and the future JSON
endpoint (§8.5), which will serialize it verbatim. Window clipping (applied
by `?weeks` today; by `?start_date=&end_date=`, matching `/api/workouts`
naming, when the JSON endpoint ships) clips the **returned** window only
(default: today − 56 days → max(today, plan end), §2); the stored past
series is full-history by construction and the fold starts at the anchor
(§4), so a clipped window never changes any value inside it. Clipping
semantics, pinned: `days` clip by date; `weeks` and `meso_bands` are
returned **whole** whenever they overlap the window — a mid-week window
edge returns the straddling week entire, never resliced (reslicing would
change its totals and silently corrupt the adherence percentage) and never
silently dropped.

```jsonc
{
  "today": "2026-07-03",
  "plan_start": "2026-07-01",      // first non-removed workout (§3), or null
  "plan_end": "2026-07-31",        // last non-removed generated workout (§3), or null
  "days": [   // §5 DayPoint series, window-clipped
    {"date": "2026-07-02", "load": 62.4, "source": "actual",
     "ctl": 55.1, "atl": 61.0, "tsb": -5.9},   // tsb = day-entering (§4)
    {"date": "2026-07-04", "load": 80.0, "source": "planned", ...}
    // ctl/atl/tsb are null inside the warm-up window, on days without a
    // stored metrics row, and everywhere when there is no anchor (§4).
    // Today's ctl/atl/tsb come from the §4 fold, never the stored today-row;
    // its `source` field is the web counterpart of the §7.1 FORM-line tag.
  ],
  "weeks": [
    {"week_commencing": "2026-06-22", "planned_load": 320,
     "actual_load": 214, "meso_label": "Build 2", "meso_source": "plan"},
    {"week_commencing": "2026-06-29", "planned_load": 340,
     "planned_load_elapsed": 150, "in_progress": true,
     "actual_load": 138, "meso_label": "Build 3", "meso_source": "plan"}
  ],
  "meso_bands": [   // chart band spans, layered per §6.1
    {"label": "~Base", "source": "inferred",
     "start_date": "2026-05-04", "end_date": "2026-05-31"},
    {"label": "Build 2", "source": "plan",
     "start_date": "2026-06-19", "end_date": "2026-07-16"}
  ],
  "objectives": [   // ALL active AND completed objectives, unfiltered — a
                    // race three weeks ago still gets its flag (seeing the
                    // TSB you raced at is half the point of a PMC); renderers
                    // clip to their window (the CLI and the PNG renderer clip
                    // when drawing; the §8.5 interactive tab will re-window
                    // client-side — pre-filtering would drop flags)
    {"id": 1, "title": "...", "target_date": "2026-09-30", "priority": 1}
  ],
  // Structured since rev 9, NOT a `warnings` string: the CLI draws it as a
  // three-line banner and the chart as a footer line, and neither should have to
  // recognise it by prefix-matching prose and then recompute it.
  "plan_gap": {"objective": {...}, "weeks_before": 9, "plan_end": "2026-07-31"},
  "warnings": [   // {code, text, command?} — renderers dispatch on `code`, never
                  // on the wording; `command` is what a surface may style as a
                  // call to action, so no renderer keeps its own registry of
                  // command names to look for (rev 9)
    {"code": "no_history",
     "text": "no activity history yet — run `data pull` first",
     "command": "data pull"},
    {"code": "zero_load_workouts",
     "text": "2 planned workouts lack TSS/RPE — count as 0"}
    // codes: no_history, bootstrap_dates, zero_load_workouts, beyond_plan_end,
    // pmc_warming (the §4 pmc_data_caveat flag on a young DB)
  ]
}
```

### 6.1 Mesocycle labels — layered lookup

Weeks are labeled from two sources, most authoritative first (rev 7 cut the
third — see the removal note below); the same spans drive the CLI meso
column and the chart band:

1. **Active plan** — the macrocycle serving the *governing objective*: the
   active objective with the earliest `target_date` that has a plan (i.e.
   the objective the current workouts implement). Its mesocycles label the
   weeks they cover. With three active objectives this is the deliberate,
   documented choice; later objectives' plans don't exist yet anyway.
   This lookup is a **new db helper** — nothing existing implements
   "earliest active objective that has a plan"; it is written for this
   feature. (Deliberately different from two neighboring rules: Phase 2's
   event selection `ORDER BY priority DESC, target_date ASC` — the coach's
   event-day-TSB line (§8.4) may anchor a higher-priority *later* race —
   and `db.get_active_objective()`'s earliest-active-plan-or-not. Meso
   labels must follow whichever plan the current workouts implement; the
   divergence is by design, not a bug.)
2. **Bootstrap reconstruction** — for weeks the active plan doesn't cover: `data
   bootstrap`'s reverse-engineered blocks from `analysis_cache["long"]`,
   nested at `cache["reconstruction"]["inferred_mesocycles"]` (fields
   `name`/`start_date`/`end_date`/`focus_detected`, engine.py:1022 — rev 5
   misnamed both the path and the focus field). These are *descriptive*
   (what the athlete actually did), not prescriptive, so they render
   `~`-prefixed in text and hatched/lighter as chart bands. Staleness,
   honestly: `data bootstrap --force` *rewrites* this cache up to the
   present, so the blocks are LLM output that can change shape between runs
   and can sprawl over plan-governed weeks — the label layering (plan wins)
   and the band trimming below contain that, and a re-run relabeling
   *pre-plan* weeks is acceptable precisely because this layer is
   descriptive. The dates are LLM-authored strings: blocks with unparseable
   dates are skipped (and counted into `warnings`), spans sorted, overlaps
   resolved by the trim rule — never compared raw.
3. **No match** (bootstrap never run, no plan coverage, or a genuine gap)
   → no label (`—`).

**Removed (rev 7): superseded-version labels.** Rev 6 had a second layer
labeling past weeks from *earlier and superseded plan versions*, exhumed
via `macrocycles.created_at`. Cut as machinery out of proportion to the
harm — cross-version archaeology whose entire output was a cosmetic name on
an old week (§3's own standard, cf. the rejected config fingerprints).
Accepted consequence, stated honestly: weeks that only a superseded version
(or a completed objective's plan) covered fall back to `~inferred` labels
or `—`; their planned bars and adherence percentages are untouched, because
governance below never depended on labels. The version-in-force rule
survives *only* where it is load-bearing — governance.

Week → block assignment: neither real nor inferred mesocycles are
Monday-aligned, so a week belongs to the block covering the **majority of
its days** (tie → the later block, so a block starting mid-week owns that
week from its first majority). Labels longer than the CLI column (8 chars)
are truncated with `…`.

**Covered weeks — read from the rows, not from the label or the version
history.** A week shows a planned total iff it contains at least one
non-removed workout row. That is the same set of rows the total is summed
from, so the flag and the figure can never disagree — and it stays
independent of the meso label, which is what CODE_REVIEW finding #3 was
about (the rev-4 snapshot inferred coverage from the label vote, so a
labelling nit silently deleted planned data).

**Removed (rev 9): version-in-force governance.** Rev 6 decided coverage by
resolving, per objective, which macrocycle *version* was in force when the
week ended (`created_at[:10] <= week_sunday`) and testing that version's
mesocycle spans — over a `db.get_governance_versions()` written for this
feature alone and deliberately including superseded versions. Cut for the
same reason rev 7 cut superseded-version *labels*: machinery out of
proportion to the harm. On real data both live versions were created the
same day with identical mesocycle spans, so the rule never once discriminated
— and it contradicted the data it gated, because `get_workouts()` excludes
*archived* rows, which are exactly the superseded version's workouts: a week
"governed" by a superseded plan therefore rendered `plan 0`, asserting the
plan had asked for nothing. Accepted consequence, stated plainly: a week
whose every planned row was later removed now shows `—` rather than a
planned zero. That is the better of the two readings anyway — nothing
survives to compare against.

**Band trimming.** `meso_bands` are date spans, not per-week votes, so
overlaps are possible where an inferred block runs into plan coverage (the
transition week). Precedence follows the layers: plan bands win; inferred
bands are trimmed to the non-overlapping remainder and dropped when fully
covered. The payload never contains overlapping bands — renderers draw
spans as given.

## 7. Front-ends — all three in v1

CLI and Telegram are the surfaces the athlete actually checks daily; the web
tab is the least visited and, in v1, frames the same PNG the bot sends. All
three consume the §5 functions — CLI/bot directly (`progression.py` + `db`
reads inside the CLI handler), the web via `/api/timeline.png` (§6). Rollout
order follows usage:
CLI first (§10), which also honours the existing convention that the web API
*tracks* the CLI feature set (ARCHITECTURE §8), rather than inverting it.

### 7.1 CLI: `tm progress`

New command family `trainmate/cli/progress.py` (`run_progress`) + dispatcher
entry in `trainmate_cli.py`, alias **`prog`**. The top-level `p` alias for
`plan` (trainmate_cli.py:708) is **removed in the same change** — following
the recent removal of the deprecated `c` alias for `context` — so the two
command names can't be confused mid-typing. Follows the standard **auto-ensure**
convention (`cli/common.py`): fresh Garmin data is pulled under the usual
`data_refresh_minutes` throttle before rendering, with `--no-pull` /
`--force-pull` as everywhere else — otherwise yesterday's un-synced ride
reads as a 0-load day and the ATL/TSB numbers are visibly wrong, the very
seam-lie §3 exists to prevent. (The web endpoint stays a pure reader; the
Dashboard already surfaces freshness. The mild asymmetry — bot text can be
fresher than a web tab viewed a minute later — is the existing, documented
stance for every other command, ARCHITECTURE §10.)

Numbers-first: in a terminal the answer to "am I on track" is projected
CTL/TSB at plan end (and at each objective the plan reaches) plus recent
adherence percentages — sparklines are garnish, not the load-bearing
content.

```
FORM today (actual)  CTL 55  ATL 61  TSB −6
CTL 8w ▁▂▂▃▃▅▅▆   plan end 07-31: CTL 61 TSB +1
⚠ plan generated through 07-31 — 9 wks before
  🏁 2026-09-30 Trail marathon
  (workout generate --until-goal)

WEEKLY LOAD plan  ▓done ▒plan  done  adh
── ~Base Accumulation ─────────────────────────
w/c 05-25      —  ▓▓▓▓▓▓▓░░░░░  262    —
── Build 2 ───────────────────────────────────
w/c 06-22    320  ▓▓▓▓▓│░░░░░░  214   67%
── Build 3 ───────────────────────────────────
w/c 06-29*   150  ▓▓▓│░░░░░░░░  138   92%
w/c 07-06    360  ▒▒▒▒▒▒▒▒▒▒░░
~ inferred · * in progress · +4 more (--weeks all)
⚠ 2 planned workouts lack TSS/RPE — count as 0
```

(When the plan reaches an objective, the banner is replaced by the per-
objective line, wrapped inside the same width budget:
`🏁 2026-09-30 Trail marathon` / `   projected CTL 68, TSB +12`.)

- **The FORM line shows today per the §4 fold** — and tags where today's
  load came from: `FORM today (actual)` once today's session has synced,
  `FORM today (planned)` while the fold is counting the planned session in
  its place. Presentation conventions are `tm status`'s, reused not
  reimplemented: same `garmin.pmc_ramp` over the stored CTL series, same
  `color_tsb`/`color_ramp`, same `PMC_TSB_LAG_NOTE` footnote wherever TSB is
  printed (the PMC design's §6.1 conventions). The consistency contract with
  `tm status` (pinned by tests, §9): **TSB today is always identical** (it is
  day-entering — computed from yesterday's values, which both commands read
  from the same stored rows); **CTL/ATL today are identical whenever today's
  load has synced** (full-precision storage + the same recurrence make the
  fold reproduce the stored row bit-exactly, §4) and differ *deliberately*
  while a planned session is pending — progress includes it, status shows
  the stored load-0 snapshot; the `(planned)` tag is what keeps that honest
  rather than confusing. `tm status` stays the snapshot; `tm progress` adds
  the trajectory and the projection.
- **Warm-up states** (§4): on a young DB the FORM line and projection are
  replaced by the still-warming message (mirroring the status line), and the
  `pmc_data_caveat` line joins the warning footer; the WEEKLY LOAD section
  renders regardless.
- **Sparkline semantics.** The second header line's `CTL 8w ▁▂▂▃▃▅▅▆` is one
  cell per displayed week (default 8, follows `--weeks`), sampling CTL on the
  week's **last day**, min–max scaled over those weeks. Range-stretching can
  make a small climb look steep, accepted: the real numbers sit on the same
  line (numbers-first — the sparkline is garnish). The `Nw` label counts the
  **cells drawn**, not the window requested — rev 7 printed `CTL 8w ▁` on a DB
  with one week of history, which is precisely the young-DB state where a
  reader is least able to spot the lie.
- **Bar semantics — a bullet bar on one shared absolute scale.** A full
  12-cell bar is the **maximum weekly load among the displayed rows** (planned
  or actual, future weeks' planned included so the scale doesn't jump when
  they arrive). Past/in-progress rows fill `▓` to **actual** load and mark
  **planned** load with a `│` tick on that same scale; future rows ghost-fill
  `▒` to planned. Rationale for the tick (rev 8): the scale must stay absolute
  so the periodization wave reads down the column and ungoverned weeks still
  draw, matching the web panel (§7.3) — but a bar that encodes only absolute
  actual load says nothing about the plan-vs-actual comparison the table is
  *for*, and pushing that comparison entirely into the pct column wasted the
  one graphical channel on the row. A tick keeps both.
  - **Every displayed row draws.** Rev 7's future rows printed `(planned)` and
    no bar while still contributing their planned load to `scale_max` — a real
    defect: a heavy week three months out compressed every measured bar to
    nothing. Ghost-filling them makes the series continuous across the seam
    *and* makes the scale honest, one change for both.
  - **Degenerate ticks.** An ungoverned week (`plan` None or 0) draws no tick.
    The tick sits on the first cell *beyond* plan, so a bar filled up to the
    tick reads as on-plan and a tick inside the fill reads as overshoot — but
    it is **clamped into the bar**: the week whose planned load *is* `scale_max`
    maps to cell `width`, and that is precisely the peak week whose tick a
    reader most wants. Dropping it there (as the first rev-8 draft did) loses
    the marker on exactly one row, which reads as a rendering bug.
  - In the mock above the scale anchor is the 360-planned week: 262→9 cells,
    214→7, 138→5.
- **Mesocycle band rules, not a meso column** (rev 8). Real mesocycle names run
  40–58 characters (`Specific Build II - Peak Specific Load & Fatigue
  Resistance`); the rev-7 8-column field rendered them as `Specifi…` on five
  consecutive rows — ten columns of width buying no information, while §6.1's
  band-inference machinery existed largely to produce that field. The label is
  now written **once per contiguous run of weeks**, in full where it fits, on a
  `── Label ─────` rule spanning the table. Runs are contiguous: a mesocycle
  that recurs after another opens a fresh rule. Weeks the plan never governed
  band under `unplanned`. `~` still prefixes inferred labels (§6.1), so the
  `~ inferred` legend still earns its place.
- **Degenerate inputs, pinned** (no machinery — each is a one-line guard):
  all displayed weeks at zero load → bars render empty, no division by the
  zero max; sparkline cells with no CTL (warm-up edge inside the window, or
  no anchor) render blank, and a flat series (min = max) renders all cells
  at the floor glyph; `--weeks` must be ≥ 1, rejected at argparse (the
  rev-4 snapshot's `or 8` silently swallowed 0).
- **Width-aware** via the existing `TRAINMATE_WRAP_WIDTH` mechanism
  (`util.default_wrap_width`). Budget: the bot's `telegram_wrap_width`
  default is **48** — column layout above is week 11 + plan 4 + bar 12 +
  actual 4 + pct 4 + separators = 40, asserted by a renderer test (§9). The
  band rule may spend the full 48 (its label is cut to
  `TABLE_WIDTH - 5`, always leaving one closing `─` so the right edge stays
  straight). The table (bar included) is **fixed-width**: it is
  laid out once for the 48-column budget and does not widen on a wider
  terminal — CLI and Telegram render identically (what you see on a TTY is
  what the bot sends), and the width test stays a single assertion. Wider
  terminals just get whitespace on the right. The budget covers **every**
  line, not just the table (rev 5's own mock FORM line measured ~59 — the
  doc seeded the violation): the FORM header is two lines (values, then
  sparkline + projection), banner / per-objective / footnote lines wrap
  through the existing `wrap_text`, and width is measured with
  `visible_len` — `⚠`/`🏁` are double-width in most terminals — never
  `len`.
- Past weeks: bullet bar + percentage; `—` planned/percentage for ungoverned
  weeks (§3 empty states); future weeks: planned number + ghost bar, the
  `done`/`adh` columns omitted rather than filled with em-dashes; current week
  per the §3 in-progress rule. Objective lines only for objectives inside the
  window (§11).
- `--weeks N` windows **both halves** (default 8: 8 past, 8 projected);
  `--weeks all` shows the whole plan, matching the web endpoint's `?weeks=all`.
  Rev 7 ran the future half to plan end unconditionally, which on a 6-month
  plan meant twelve `(planned)` rows under four rows of measured data — the
  projection is the point of the feature, but the *table* is not where it
  earns its keep (the FORM/projection lines above it are, and they still run
  to plan end regardless of `--weeks`). Whatever the window drops is named in
  the legend (`+4 more (--weeks all)`) — never a silent truncation, and the
  count covers **both** sides: a default run over a long history hides far more
  past weeks than projected ones.
- **`--chart` frames the same span the table does.** `clip_payload_for_weeks`
  takes `cap_future=True` from the CLI, so a PNG showing twenty projected weeks
  can't sit under a legend reading `+12 more`. The web endpoint (§6) leaves it
  off: an `<img>` has no accompanying table to agree with, and the whole
  projection is what that panel is for (§7.3). One consequence, guarded by a
  test: the chart's plan-end marker must be suppressed when the cap puts plan
  end outside the drawn window, because an `axvline` past the last day drags
  the x-axis out to meet it.
- **`--explain`** appends the PMC footnotes (`PMC_TSB_LAG_NOTE`: why TSB won't
  equal the shown same-day CTL − ATL). Rev 7 printed it on every invocation
  where TSB was shown; a standing caveat that appears every time is one the eye
  learns to skip, and it cost two of the ~20 lines the command has.
- Rendering split as pure formatting helpers (fed by §5 outputs) so they are
  unit-testable without a DB, per `coach/formatting.py` precedent.

### 7.2 Telegram — text for free, chart as a photo

No bot-native command; both paths ride the CLI-as-subprocess parity model
(ARCHITECTURE §2), which is what makes them cheap:

- **Text:** `/progress` in chat just runs `tm progress`; the width-aware
  renderer (§7.1) is the whole story. `MENU_COMMANDS` in `trainmate_bot.py`
  (hand-synced by design) gains a `progress` entry.
- **Chart:** `/progress --chart` renders the full §2 two-panel picture to
  PNG and sends it as a photo. The drawing itself lives in
  **`trainmate/chart.py`** — `render_timeline_png(payload) -> bytes`
  (matplotlib, `Agg` backend, imported lazily inside the function), fed the
  §6.0 payload — called by this path and by the web endpoint (§6), so the
  §2 picture has exactly **one** implementation. (The rev-4 snapshot drew
  it inside `cli/progress.py:_render_chart_png`; the rework extracts it,
  §10.1.) `--chart` is additive: the text output still prints/sends — the
  photo is the picture, the text is the numbers.
  - On a TTY (and any non-json frontend, incl. piped): writes
    `./progress.png` (or the `--chart PATH` argument), overwriting, and
    prints the path. `progress.png` joins `.gitignore`.
  - Under `TRAINMATE_FRONTEND=json` (how the bot launches the CLI): writes a
    `tempfile.NamedTemporaryFile(delete=False)` PNG and emits a **photo
    line** on stdout — `\x1eTM-PHOTO {"path": ..., "caption": ...}` — a
    sibling of the existing `PROMPT_SENTINEL` protocol. The CLI supplies the
    §7.1 FORM line as `caption`; the payload carries it because the bot
    process has no other way to know it (it must not re-parse forwarded chat
    text). `trainmate/prompt.py` gains `PHOTO_SENTINEL` + an
    `emit_photo(path, caption=None)` helper (the `\x1e` record-separator
    framing already guarantees prose never collides; the photo line is
    written alone on its line and flushed atomically).
    `trainmate_bot.py:_drive()` gains one branch beside
    `parse_prompt_request`: flush the text buffer, `bot.send_photo` with the
    payload's `caption`, and unlink the temp file in a `finally` (so a
    failed send, `/cancel` kill, or timeout doesn't orphan it).
    `parse_photo_request` joins the pure helpers unit-tested in
    `tests/test_bot.py`.
  - Robustness: `_drive()` also learns to **drop** (not forward as chat
    text) any unrecognized `\x1e`-prefixed line, so a future sentinel added
    on the CLI side degrades to a silently-missing feature instead of raw
    protocol bytes in chat. Deploy note: the photo branch requires the
    restarted bot — send `/restart` after upgrading, else a stale bot shows
    the raw line (mitigated by the same drop rule from then on).
  - Photo vs document: `send_photo` gives the clean inline photo bubble;
    Telegram recompresses photos, which can soften thin lines on phones —
    mitigated at the source by rendering at ~2× DPI with ≥2-px lines, so
    there is little for recompression to smear. `send_document` would
    deliver the exact PNG but renders as a file card (filename/size
    clutter, lands under Files not Photos); its real downsides are only
    cosmetic, so switching is a one-line change if photo blur ever annoys.
  - This is deliberately a *transport*, not a feature: any future CLI
    command can send a photo the same way (e.g. the drift/zone charts, §8).
  - **Label collision rules (rev 8).** Drawing the labels this section specced
    is not the same as drawing them legibly; two bugs shipped with the first
    attempt, and both are properties of *real* data that no mock exposes:
    - *Meso band labels.* A 3-week band is a few dozen pixels wide; a real
      mesocycle name is ~50 characters. Centring the full name in each span
      overprinted every neighbour into a purple smear. Each label is now cut to
      what its own span can hold (`_fit_label` / `_chars_per_axis`), dropped
      entirely below `_MIN_BAND_LABEL_CHARS` (the tint still draws), and
      consecutive labels alternate between two heights so two that each just fit
      still cannot touch. `_draw_weekly_bars` reserves headroom (`ylim` top =
      peak × 1.5) for that strip, and the bottom legend anchors below it.
    - *Measure last.* Fitting is a **separate pass** (`_draw_meso_spans` then,
      after `fig.canvas.draw()`, `_draw_meso_band_labels`) because both inputs
      move: `axvspan` feeds the x-autoscaler, so a band reaching past the bars
      widens the very x-range the fit divides by, and `tight_layout` then
      resizes the axes box. Measuring before either runs fits labels to an axis
      that no longer exists — they come out oversized and collide, which is the
      failure the machinery exists to prevent. With the figure drawn, capacity
      is read from the real axes extent rather than a fudge factor.
    - *Coincident vertical markers.* The plan-end marker is suppressed when an
      objective already marks that date. A plan generated **to** an objective is
      the common case, not an edge case — `workout generate --until-goal` makes
      it the default — and two rotated labels on one `x` are unreadable.
- **matplotlib** sits in `requirements.txt`'s optional tier (like
  `python-telegram-bot`; already there since the snapshot, §10.1): imported
  lazily inside `chart.py`; without it, text mode works, `--chart` fails
  with an install hint, and the web endpoint answers 503 carrying the same
  hint (§6). First import builds the font cache (seconds, one-time) — well
  inside the bot's `telegram_command_timeout` (180 s), noted here so a slow
  first `/progress --chart` isn't mistaken for a hang.

### 7.3 Web: **Progress** tab — the PNG, framed

Fifth top-level tab in `static/index.html` / `static/app.js`, loaded lazily
via the existing `loadedTabs` mechanism. V1 is deliberately minimal — the
picture, not an app:

- The tab body is an `<img>` pointing at `GET /api/timeline.png` (§6), plus
  quick-range buttons (8 w / 26 w / all) that set `?weeks=` and reload the
  image. No chart library, no client-side chart state.
- Failure states pass through, no special-casing: a 503 (matplotlib
  missing, §6) shows the endpoint's install-hint body; the §3 empty states
  and the §4 still-warming state are drawn *inside* the PNG by the renderer,
  identical to what the bot photo shows.
- The rev-4 snapshot's uPlot tab — the CDN include (`uplot@1.6.30`), the
  `app.js` fetch/render code, the bar-plugin plan — is **removed in the
  rework** (§10.1) and returns as the §8.5 follow-on.
- The trade is explicit: rev 6 rejected a server-rendered PNG tab because
  it "kills the hover-to-inspect interaction"; rev 7 accepts that loss
  **for v1** to get one chart renderer instead of two hand-maintained
  renderings that rev 6 itself admitted would drift ("accepted cost" — now
  un-accepted). Hover returns with §8.5 when it is actually missed, rather
  than being pre-paid on the least-visited surface.

**Rejected alternatives (web):** keeping the uPlot tab in v1 (the drift
cost above); hand-rolled SVG (tooltips/cursor/scales are real work — moot
in v1, which has no interactivity to build, and still rejected for §8.5
when a 45 KB lib exists); Chart.js (heavier than uPlot, same verdict when
§8.5 ships); a bot-native chart command bypassing the CLI (breaks the
parity model that keeps the bot maintenance-free).

## 8. Follow-ons (designed-for, explicitly out of v1)

The endpoint payload and `progression.py` are shaped so each of these is
additive:

1. **ACWR projection ribbon.** Extend the *existing* rolling-sum
   acute/chronic/ACWR math (§12 semantics, unchanged) forward over the
   merged §3 series; shade the 0.8–1.3 band and flag future weeks the plan
   pushes past 1.3. Actionable: the fix is one `adapt`/`swap` away.
2. **Plan-drift view.** Third bar layer from `original_tss` /
   `original_duration_minutes` + `adaptation_count`: original plan vs
   adapted plan vs actual — a visualization of the adaptation engine itself.
3. **Zone-distribution stack.** Weekly stacked zone1–5 time (HR and power
   variants) from `completed_activities`. **The text half has shipped** —
   `DESIGN_intensity_distribution.md` §9.6: a positional sport argument
   (defaulting to every qualifying `sport_preferences` entry, one table
   each, not just the first), a per-zone weekly table stacked under the
   WEEKLY LOAD table, and `--blocks` for the graded per-mesocycle view.
   **Behind `-z`/`--zones` since rev 9** (naming a sport implies it): on real
   data the tables took `tm progress` from 27 lines to 96 at phone width, and
   they answer a different question from the load table they sit under — the
   command's own answer to "am I on track" must stay readable without them. It
   scopes only the intensity content: CTL/ATL/TSB, the projection and the
   load table stay whole-athlete. The weekly grain is load-bearing rather
   than cosmetic — a regenerated plan moves mesocycle boundaries and
   orphans completed activities, and a calendar week is the one bucket
   that cannot move. It is no longer past-only either: §9.8 put planned
   zone targets on `workouts`, so weeks ahead of today carry the plan's
   own distribution, marked `+`.
   **The chart stack remains open**, and inherits the same one-sport,
   one-currency rules — `--chart` is unaffected by the sport argument, so
   the PNG's two panels stay PMC and whole-athlete weekly load, and the
   web endpoint gains no intensity view.
4. **Coach-facing event-day TSB line** — the original deliverable of PMC
   Phase 2 (`DESIGN_pmc_fitness_fatigue.md`, end of doc): emit
   `Projected event-day TSB (from current plan): +12` into the plan/adapt
   prompts at prompt-assembly time. Once this feature ships it is one call
   into the §5 fold (pick the event per Phase 2's
   `ORDER BY priority DESC, target_date ASC` rule, read the folded TSB on
   `target_date`, carry the §3 warnings verbatim); the projection math,
   stale-anchor decay, and honesty guards all come from §4. Phase 2's
   zero-fill-to-event behavior (with its assumes-rest annotation) is the one
   piece not covered here, since this feature stops at plan end (§4).
5. **Interactive web Progress tab** — rev 6's §7.3 design, deferred whole
   (rev 7): uPlot (~45 KB, no build step; the snapshot pinned
   `uplot@1.6.30` — re-pin on revival, vendoring into `static/vendor/` if
   offline use matters) with the vendored `seriesBarsPlugin` for paired
   weekly bars, each PMC series split into a solid pre-today / dashed
   post-today pair (legend collapses each pair), native cursor sync across
   the two panels, meso band tint + labels (hatched for `inferred`) via
   `draw` hooks, and `YYYY-MM-DD` → epoch at **UTC midnight** on both axes
   (the local-vs-UTC off-by-one-day is the classic foot-gun with date-only
   data). Ships together with `GET /api/timeline` (JSON), which serializes
   the §6.0 payload verbatim under the pinned `?start_date=&end_date=`
   clipping — the endpoint arrives with its first consumer. Trigger:
   hover-to-inspect actually being missed on the PNG tab (§7.3), not
   calendar time.

*(Rev 1 listed Telegram `/chart` and a CLI sparkline as follow-ons; both
were promoted into v1 — see §7.1–7.2 — because the CLI and the bot are the
surfaces actually used daily. A calibrated-projection follow-on was
considered and rejected — see the §3 limitations. Follow-ons 1–3 inherit
the §7.2 photo transport for free where they need a chart in chat.)*

## 9. Testing

- `tests/test_pmc.py` (the shipped core's suite) covers the two core changes:
  `seed=(0,0)` reproduces current behavior, and a series split at an
  arbitrary date and re-folded with `seed=` the first half's final values
  reproduces the unsplit series **exactly** — a legitimate demand only
  *because* storage is now full-precision (§4); against 1-dp-rounded anchors
  this test would be flaky by construction. The existing pinned
  hand-computed values switch to comparing `round(x, 1)` (the unrounding is
  a deliberate output change). The recursion values themselves (TSB
  off-by-one, calendar-gap decay) are already pinned there and are *not*
  re-tested in this feature's suite.
- `tests/test_progression.py` — pure-function tests (patch-before-import
  pattern, §5): fixture activities + workouts + metrics rows spanning the
  today-seam; assert seam rule incl. the zero-load-activity case (§3),
  past points equal the stored rows verbatim and are nulled before the
  warm-up cutoff (§4), the anchored fold over a zero-load tail reproduces
  the closed-form decay `ctl_A·(1−1/τ)^d`, planned load raises the
  projection, seam continuity under **non-default τ** (config plumbed, no
  kink at the anchor), the morning-pull case (a stored load-0 row for today
  is ignored as anchor; today folds per §3 and the planned session raises
  tomorrow's ATL), trailing NULL-PMC rows skipped when picking the anchor,
  no-anchor / young-DB suppression, zero-gap day filling, planned-side sRPE
  fallback + zero-valued-row warning counting (rest rows excluded; explicit
  `tss=0` values to 0, not the sRPE fallback), Monday week bucketing,
  in-progress-week elapsed split (today counted only once synced), the
  part-week plan rule (a plan starting or ending mid-week takes
  `partial_plan`; a week the plan spans whole does not), plan-end clamp incl. the
  generated-only rule (a manual workout beyond plan end leaves it unchanged
  and warns; no-generated-workouts fallback), the lapsed plan (plan_end <
  today → no fold, window ends today), §6.1 majority-overlap labeling over
  fixture spans, the rev-7 label fallback (weeks covered only by a
  superseded version or a completed objective's plan fall to
  `~inferred`/`—` labels while their planned totals and percentages still
  render), coverage decided by the week's own non-removed workout rows rather
  than by the label (an all-removed week falls to `—`, not a planned zero),
  the zero-load warning ignoring past rows, band trimming (payload
  never contains overlapping spans), payload-shape assertions on
  `assemble_timeline`'s dict (incl. `plan_start`/`plan_end`, the structured
  `plan_gap` field and `{code, text, command?}` warnings, in-progress week fields,
  `meso_bands` layering, nullable `ctl`/`atl`/`tsb` with the warm-up nulls
  and the young-DB caveat in `warnings`, and the §6.0 clipping semantics —
  a point *inside* the window must reflect load *before* the window; a
  mid-week window edge returns the straddling week whole), and the four
  empty states.
- CLI renderer tests (`tests/test_cli_progress.py` or alongside existing CLI
  tests): the pure formatting helpers (§7.1) over fixture series — plan-end
  banner vs per-objective projection lines, past / in-progress / ungoverned
  / future week rows, bar scaling (shared max anchor incl. a future planned
  week; ungoverned week still gets a bar), warning footer, the §7.1
  status-consistency contract over a fixture pair (today synced: FORM ≡
  status verbatim, `(actual)` tag; today pending: TSB equal, CTL/ATL
  deliberately diverge, `(planned)` tag), `PMC_TSB_LAG_NOTE` carried, the
  lapsed-plan banner, the partial-final-week marker, the degenerate inputs
  (zero-max bar scale, blank and flat sparklines, `--weeks 0` rejected), the
  young-DB still-warming state; plus a narrow `TRAINMATE_WRAP_WIDTH` variant
  asserting `visible_len(line) ≤ 48` for **every** output line (not `len` —
  emoji are double-width).
- `tests/test_bot.py`: `parse_photo_request` round-trip with `emit_photo`
  framing (sentinel/JSON incl. the `caption` field, non-photo lines return
  `None`, unknown-sentinel lines dropped) — same pattern as the existing
  `parse_prompt_request` tests. The matplotlib rendering itself stays untested (visual output),
  matching the front-end stance below.
- Endpoint test alongside the existing web tests: `GET /api/timeline.png`
  over a fixture DB returns 200, `image/png`, and a body starting with the
  PNG magic bytes (pixels stay untested, matching the front-end stance
  below); `?weeks` validation (`0` → 400, `all` accepted); matplotlib
  absent (import patched out) → 503 carrying the install hint; pure-reader
  property (no Garmin/LLM mocks needed — that's the assertion).
  **No CLI≡endpoint equivalence test (rev 9).** Rev 6 pinned the two surfaces
  against each other after CODE_REVIEW finding #5. Once `timeline.py` gave both
  a single row-fetching path, that test read `assertEqual(f(db), f(db))` and
  could not fail — ceremony, not a pin. The shared builder *is* the guarantee;
  a test can only restate it.
- Web front-end stays untested, per existing practice.

## 10. Rollout

### 10.1 State of this branch — what the rev-4 snapshot already did

The WIP snapshot (`60c6e6c`) implemented rev 4 across all three front-ends
before this rework was specced. Reconcile against it rather than
implementing §10.2 from scratch — several steps are already partly done.

**Done in the snapshot, correct as-is (keep):**

- `adherence.planned_load` is already public — only the `tss is not None`
  fix (§3) remains there.
- The photo transport, whole: `PHOTO_SENTINEL`/`emit_photo` in
  `trainmate/prompt.py`; `parse_photo_request`, the `_drive()` photo branch
  and the unknown-sentinel drop in `trainmate_bot.py`; the bot tests.
- `tm progress` dispatcher entry + `prog` alias in `trainmate_cli.py`; the
  `p` alias for `plan` is **already removed** (rev 6 cited its pre-snapshot
  line 708 — stale; nothing left to do); the `MENU_COMMANDS` `progress`
  entry in `trainmate_bot.py`.
- matplotlib in `requirements.txt` (optional tier); `progress.png` in
  `.gitignore`.

**Exists in rev-4 form — reworked to this rev:**

- `trainmate/progression.py`: delete the module-level `CTL_DAYS`/`ATL_DAYS`
  and the from-zero full-history recursion; replace with
  read-the-stored-rows + the anchored fold (§4), config τs, and
  `assemble_timeline` (§5 — the snapshot has **no** such function; each
  caller assembles its own payload, the CODE_REVIEW #5 divergence).
- `trainmate/cli/progress.py`: the renderer gains the rev-6 pins
  (elapsed-week rule, governance, degenerate-input guards, the
  `visible_len` width budget); `_render_chart_png` moves out to
  `trainmate/chart.py` (§7.2).
- `trainmate_web.py`: the snapshot's JSON `/api/timeline` becomes
  `GET /api/timeline.png` (§6); JSON is deferred to §8.5.
- `static/`: the uPlot tab (CDN include, `app.js` chart code, styles) is
  replaced by the `<img>` tab (§7.3).
- `tests/test_progression.py`, `test_cli_progress.py`, `test_web.py`:
  rewritten to the §9 list.

**New — no snapshot counterpart:**

- `compute_pmc`'s `seed` parameter + the unrounding (+ `tests/test_pmc.py`
  updates) (§4).
- `trainmate/chart.py` (§7.2; extraction, but the module is new).
- The governing-objective db helper and the version-in-force governance
  rule (§6.1).

### 10.2 Steps

Ordered by usage (CLI/bot before web), each step independently shippable:

1. `garmin.compute_pmc` gains the optional `seed` parameter and drops its
   1-dp output rounding (full-precision storage, §4) (+ the
   `tests/test_pmc.py` updates, §9); rework `trainmate/progression.py` +
   `tests/test_progression.py` per §10.1 (incl. `assemble_timeline`, §5);
   the `tss is not None` fix in `adherence.planned_load` (§3).
2. Rework `tm progress` text mode to this rev (`trainmate/cli/progress.py`
   + formatting-helper tests, §9). Telegram text follows via parity; the
   dispatcher/alias/menu work is already done (§10.1).
3. Extract `trainmate/chart.py` from the snapshot's `_render_chart_png`
   (§7.2); the transport around it is already done (§10.1).
4. `GET /api/timeline.png` in `trainmate_web.py`, replacing the snapshot's
   JSON endpoint, + endpoint test (§9).
5. **Progress** tab: replace the snapshot's uPlot tab with the `<img>` +
   range buttons (§7.3); drop the uPlot CDN include from `index.html`.
6. ARCHITECTURE.md: §2 module map (+`progression.py`, +`chart.py`,
   +`cli/progress.py`, bot photo protocol bullet), §7 CLI command table
   (+`progress`), §8 endpoint table (+`/api/timeline.png`) + front-end tab
   list (+Progress), §12 — **fold into the existing PMC documentation**,
   don't duplicate it: the core (constants, storage, warm-up rules) is
   already documented with the PMC feature; §12 gains only the projection
   layer (stored-series + anchored fold, pointer to this doc), §14
   test-file table (+`test_progression.py`, +CLI renderer tests), §15
   pointer to this doc. (The snapshot's ARCHITECTURE edits are rev-4 —
   redo them to this rev.)

## 11. Open questions

- **Rest-day TSB display**: TSB uses day-*entering* form (§4, and so does
  the payload field — documented in §6.0); whether a tooltip should also
  show day-closing values only arises with the interactive tab (§8.5) —
  the v1 PNG has no hover, so there is nothing to settle yet.
- **Multi-objective seasons** — live today, not hypothetical: three
  objectives are active (2026-09-30 → 2027-01-31) while the plan only
  extends through the first. The *governing objective* for meso labels is
  defined (§6.1); v1 draws flags for `active` and `completed` objectives
  whose `target_date` falls inside the *displayed* window — clipping is the
  renderer's job, the endpoint returns them all (§6) — later ones are simply
  off-canvas until their plan exists; `priority` can gate flags if the panel
  gets noisy.
- **Planned-today undercount** (§3 today rule; rev 2 mislabeled this
  "double-count") is accepted; if it proves annoying in practice (frequent
  two-session days), the rule can move to per-`sport_type` matching using
  `adherence.py`'s pairing logic.
- **Chart-by-default in chat?** Bare `/progress` gives text (§7.2); if the
  photo turns out to be what's wanted every time, defaulting `--chart` on
  under the json frontend is a one-line change — but it makes the same
  command behave differently per frontend, so it waits for felt need.
