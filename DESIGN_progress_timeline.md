# Design: Progress Timeline (past + projected training progression)

**Status:** Implemented (rev 4 — implementability review folded in: photo caption
carried in the TM-PHOTO payload, CLI weekly bars defined as absolute load on
a shared max-anchored scale, table fixed-width at the 48-col budget,
sparkline sampling pinned, seed mean pinned as calendar mean (rest days
count as 0), endpoint returns all active objectives with renderers clipping.
Rev 3:
projection clamped to the generated plan, planned-side sRPE fallback via
`adherence`, layered mesocycle labels incl. the bootstrap reconstruction,
in-progress-week rule, empty states, CLI auto-ensure) ·
**Date:** 2026-07-03 · **Companion to:**
ARCHITECTURE.md §12 (load model), `DESIGN_backward_evaluation.md` (the
analysis-side view of the past; this doc is the *presentation*-side view of
past **and future**)

A single continuous timeline that shows how training load has actually
accumulated and where the current plan takes it: **past days are measured
load** (from `completed_activities`), **future days are planned load** (from
`workouts`), and one fitness/fatigue model runs across the seam. The
centerpiece is a projected Performance Management Chart — CTL (fitness), ATL
(fatigue), TSB (form) — computed over actuals up to today and continued
forward over the plan as far as workouts have been generated, so the user can
see *whether the plan as currently written delivers peak fitness with
positive form on race day*. Below it, weekly planned-vs-actual load bars
labeled by mesocycle make the periodization wave and adherence visible at a
glance.

V1 ships on **all three front-ends**: `tm progress` (text, numbers-first),
Telegram (same text for free via CLI parity, plus the full chart as a PNG
photo), and a web **Progress** tab (the richest, interactive rendering). One
computation (§5) feeds all three; only the rendering differs (§7).

---

## 1. Motivation

TrainMate holds both halves of the progression story but never draws them in
one place:

- The **past** is visible only as tables (`workout compare`, the History tab)
  — per-day adherence, no accumulation, no trend.
- The **future** is visible only as a list of workouts and a mesocycle
  strategy blob — the periodization wave (build/recover, volume ramp) exists
  in the data (every planned workout carries `tss`) but the user cannot *see*
  it.
- Nothing connects them. "Am I on track?" currently requires reading the
  plan, the compare view, and the metrics table and doing the integration in
  one's head.

The data is already sufficient — no new ingestion, no LLM calls, no schema
change. As of writing: ~6 months of completed activities, a TSS-annotated
plan through the first objective (2026-09-30), 9 mesocycles, and three active
objectives spanning to 2027-01-31. Note that the plan reaching the objective
is a **snapshot, not an invariant**: `workout generate` defaults to a rolling
`workout_generation_span_days` (28-day) horizon, so most of the time workouts
stop well short of race day unless `--until-goal` was used — §3's plan-end
rule exists for exactly this. This feature is a pure read-side derivation.

## 2. What the picture shows

The canonical layout — two panels on a shared time axis (default window:
8 weeks back → **plan end**, the last generated workout date). The web tab
(§7.3) and the Telegram PNG (§7.2) render it as drawn; the CLI (§7.1) is a
numbers-first projection of the same content:

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
  `target_date`s that fall inside the window.
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
  the same currency the ACWR pipeline already uses. This also absorbs the ~24
  activities with NULL `tss` without special-casing.
- **Future days** (`date > today`): daily load = Σ `adherence.planned_load(w)`
  over that day's non-`removed` `workouts` rows — the coach's `tss` when set,
  else sRPE (RPE × 10 × hours) from `rpe` + `duration_minutes`. This is the
  *existing* planned-side valuation the adherence verdicts already use
  (`adherence._planned_load`, promoted public in step 1 of §10); inventing a
  stricter "NULL tss → 0" rule here would make the weekly bars disagree with
  the adherence percentages shown beside them. Rows with **neither** tss nor
  rpe+duration contribute 0 and are counted into the payload `warnings`
  field (mirroring the aggregated-underestimate pattern of §12). `rest` rows
  are excluded from that warning — a rest day legitimately has no load.
- **Today**: actual if any completed activity **with load > 0** exists for
  today, else planned (a zero-load activity — no power, HR, or RPE — must
  not suppress a planned session). Accepted approximation: a two-session day
  where only one is done yet counts actual-only until the second syncs.

**Plan end.** The projection runs exactly to the last non-removed workout
date and stops — no zero-fill beyond it (a decaying ghost line would read as
fitness collapse, which is a statement about missing data, not the plan).
When plan end < the next objective's `target_date`, every surface annotates
the gap — payload `warnings`, CLI banner, chart label: *"plan generated
through 2026-07-31 (9 wks before objective)"* — and per-objective projected
CTL/TSB figures are shown **only** for objectives the plan actually reaches.
The CLI hint names the fix (`workout generate --until-goal`).

**In-progress week.** The current week is neither past nor future: comparing
a full-week planned total against a partial actual reads as poor adherence
every Monday. Rule, identical on all three surfaces: the current week's
planned figure covers **elapsed days only** (Monday through today), the row
is marked *"in progress"*, and the payload carries both `planned_load`
(full week) and `planned_load_elapsed` plus `in_progress: true` so renderers
can't diverge.

**Empty states** (all reachable on a fresh install; none may crash):

- *No completed activities at all* → no series to compute. CLI prints "No
  activity history yet — run `tm data pull` first"; the endpoint returns
  empty `days`/`weeks` plus that warning; the web tab shows the message.
- *No planned workouts* → the classic past-only PMC: solid lines to today,
  no dashed segment, window ends today, note "no plan generated — projection
  unavailable". As with the plan-end banner, the CLI names the fix:
  `plan generate` (preceded by `data bootstrap` if never run — which also
  lights up the §6.1 inferred meso labels). `data bootstrap` cannot
  substitute for a plan here: it reconstructs the past, it generates no
  future workouts, so it enriches this state but never adds the projection.
- *Weeks with actual load but no governing plan* (pre-adoption history
  inside the window) → actual bar renders, planned shows `—`, **no
  adherence percentage** (never divide by zero) — matching `adherence.py`'s
  precedent of treating activity outside planned coverage as informational,
  not a deviation.

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

## 4. Fitness/fatigue model (new math, additive to §12)

Standard impulse-response PMC (Banister via Coggan's simplification),
exponentially weighted:

```
CTL_d = CTL_{d-1} + (load_d − CTL_{d-1}) / 42
ATL_d = ATL_{d-1} + (load_d − ATL_{d-1}) / 7
TSB_d = CTL_{d-1} − ATL_{d-1}          (form going *into* day d)
```

- Time constants **42 / 7 days** are module constants in code, like
  `HR_ZONE_TSS_PER_SEC` — not config. Nobody tunes these in practice.
- **Seeding:** the series is always computed over the **full history**
  regardless of the requested display window. The recursion starts on the
  first `completed_activities` date, seeded at the **mean daily load of the
  first 42 days** of history for CTL and the first 7 days for ATL (less
  history than that: mean over what exists). This is a **calendar mean**:
  zero-load days count as 0 in the numerator and stay in the denominator
  (÷42, not ÷days-trained) — that is the value the recursion itself
  converges to; skipping rest days would seed at trained-every-day fitness,
  the very overestimate seeding exists to avoid. Seeding at 0 would assume an
  untrained athlete at data start, and that error decays only as
  `e^(−age/42)` — after a dated `data wipe --garmin` + short re-pull the
  whole visible window would read low. Mean-seeding makes the start
  self-consistent with the athlete's opening training level, demoting the
  transient to a second-order error, and costs three lines. The endpoint is
  cheap enough (a few hundred rows) that full-history computation costs
  nothing.
- **Relationship to existing acute/chronic workload:** none replaced. The
  rolling-sum acute (7-day Σ) / chronic (28-day Σ/4) / ACWR in
  `athlete_metrics_cache` remain the injury-risk vocabulary used by
  adaptation prompts. CTL/ATL/TSB is a *presentation-side* model with
  different semantics (asymptotic fitness/form, not ratio-based risk). §12
  gains a subsection documenting it; the two coexist the way they do in the
  sports-science literature.

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
DayPoint = dict  # {date, load, source: 'actual'|'planned', ctl, atl, tsb}
                 # tsb is day-ENTERING form (§4): CTL_{d-1} − ATL_{d-1}

def daily_loads(activities, workouts, today) -> list[DayPoint]
    # merged per-day load series per §3 (no gaps: zero-load days included,
    # from first activity date through plan end)

def fitness_series(day_points) -> list[DayPoint]
    # folds §4 recursion over the output of daily_loads

def weekly_aggregates(activities, workouts, today, meso_spans) -> list[dict]
    # {week_commencing (Monday, per learning_evidence precedent),
    #  planned_load, planned_load_elapsed?, in_progress?, actual_load,
    #  meso_label?, meso_source?}
    # planned = Σ adherence.planned_load over non-removed workouts (the
    # *adapted* plan — "what the plan asked at the time"; original_tss is
    # reserved for the drift follow-on, §8). meso_spans is the precomputed
    # labeled-span list from §6.1 — the caller (CLI handler / endpoint)
    # assembles it from db + analysis_cache so this stays row-in/row-out.
```

## 6. Web API: `GET /api/timeline`

Thin handler in `trainmate_web.py`: `db` reads (`get_completed_activities`,
`get_workouts`, macrocycle versions + mesocycles for the governing objective
(§6.1), active objectives, `get_analysis_cache("long")` for the bootstrap
reconstruction) + the §5 functions. Properties, consistent with the existing
API's stance (§8 of ARCHITECTURE.md):

- **Pure reader.** No `ensure_data`, no Garmin, no calendar, no LLM (reading
  the cached bootstrap reconstruction is a `db` read, not an analysis run).
  Data freshness is already surfaced via `sync_state` on the Dashboard.
- **No caching.** Recomputed per request — a few hundred rows of arithmetic.
  Deliberately *not* an `analysis_cache` slot: the projection must move the
  instant `adapt`/`generate`/`swap`/`remove` rewrite future workouts, and a
  fingerprint scheme would just re-derive "did anything change" at higher
  complexity than recomputing.
- Query params `?start_date=&end_date=` (matching `/api/workouts` and
  `/api/activities` naming) clip the **returned** window only (default:
  today − 56 days → plan end); computation always runs over full history
  (§4 seeding).

```jsonc
{
  "today": "2026-07-03",
  "plan_end": "2026-07-31",        // last non-removed workout date, or null
  "days": [   // §5 DayPoint series, window-clipped
    {"date": "2026-07-02", "load": 62.4, "source": "actual",
     "ctl": 55.1, "atl": 61.0, "tsb": -5.9},   // tsb = day-entering (§4)
    {"date": "2026-07-04", "load": 80.0, "source": "planned", ...}
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
  "objectives": [   // ALL active objectives, unfiltered — renderers clip to
                    // their window (the web tab re-windows client-side, §7.3,
                    // so pre-filtering would drop flags from wider zooms)
    {"id": 1, "title": "...", "target_date": "2026-09-30", "priority": 1}
  ],
  "warnings": [
    "plan generated through 2026-07-31 (9 wks before objective 2026-09-30)",
    "2 planned workouts have neither TSS nor RPE and count as 0 load"
  ]
}
```

### 6.1 Mesocycle labels — layered lookup

Weeks are labeled from three sources, most authoritative first; the same
spans drive the CLI meso column and the chart band:

1. **Active plan** — the macrocycle serving the *governing objective*: the
   active objective with the earliest `target_date` that has a plan (i.e.
   the objective the current workouts implement). Its mesocycles label the
   weeks they cover. With three active objectives this is the deliberate,
   documented choice; later objectives' plans don't exist yet anyway.
2. **Superseded plan versions** — for past weeks that predate the active
   version: versions are kept (ARCHITECTURE §11), and `macrocycles.
   created_at` identifies which version was in force during a given week
   (latest version created before the week ended). Consistent with the
   planned-load bars, which likewise show "what the plan asked at the time".
3. **Bootstrap reconstruction** — for weeks before any plan: the
   `inferred_mesocycles` from `analysis_cache["long"]` (`data bootstrap`'s
   reverse-engineered blocks — name, span, focus). These are *descriptive*
   (what the athlete actually did), not prescriptive, so they render
   `~`-prefixed in text and hatched/lighter as chart bands. Pre-plan history
   never changes, so the one-shot nature of the bootstrap cache is not a
   staleness concern here.
4. **No match** (bootstrap never run, or a genuine gap) → no label (`—`).

Week → block assignment: neither real nor inferred mesocycles are
Monday-aligned, so a week belongs to the block covering the **majority of
its days** (tie → the later block, so a block starting mid-week owns that
week from its first majority). Labels longer than the CLI column (8 chars)
are truncated with `…`.

## 7. Front-ends — all three in v1

CLI and Telegram are the surfaces the athlete actually checks daily; the web
tab is the richest rendering but the least visited. All three consume the §5
functions — CLI/bot directly (`progression.py` + `db` reads inside the CLI
handler), the web via `/api/timeline` (§6). Rollout order follows usage:
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
FORM today   CTL 55   ATL 61   TSB −6     CTL ▁▂▂▃▃▅▅▆ (8w)
Projected at plan end 07-31:  CTL 61  TSB +1
⚠ plan generated through 07-31 — 9 wks before
  🏁 2026-09-30 Trail marathon (workout generate --until-goal)

WEEKLY LOAD          plan  actual
~Base     w/c 05-25    —   ▓▓▓▓▓▓▓░░  262    —
Build 2   w/c 06-22   320  ▓▓▓▓▓░░░░  214   67%
Build 3   w/c 06-29*  150  ▓▓▓░░░░░░  138   92%
Build 3   w/c 07-06   360  (planned)
~ inferred (bootstrap) · * in progress: plan = Mon–Fri
⚠ 2 planned workouts have neither TSS nor RPE; count as 0
```

(When the plan reaches an objective, the banner is replaced by the per-
objective line: `🏁 2026-09-30 Trail marathon — projected CTL 68, TSB +12`.)

- **Sparkline semantics.** The FORM line's `CTL ▁▂▂▃▃▅▅▆ (8w)` is one cell
  per displayed week (default 8, follows `--weeks`), sampling CTL on the
  week's **last day**, min–max scaled over those weeks. Range-stretching can
  make a small climb look steep, accepted: the real numbers sit on the same
  line (numbers-first — the sparkline is garnish).
- **Bar semantics — absolute load, one shared scale.** A full 9-cell bar is
  the **maximum weekly load among the displayed rows** (planned or actual,
  future weeks' planned included so the scale doesn't jump when they arrive);
  each past/in-progress row's bar shows its **actual** load on that scale
  (`▓` filled, `░` remainder, rounded to nearest cell). The bar is *not*
  actual÷planned — adherence is already the pct column — so it works
  unchanged for ungoverned weeks (no plan, still a bar) and makes the
  periodization wave readable down the column, matching the web panel's
  absolute-scale bars (§7.3). Future weeks stay number-only (`(planned)`),
  per the mock. In the mock above the scale anchor is the 360-planned week:
  262→7 cells, 214→5, 138→3.
- **Width-aware** via the existing `TRAINMATE_WRAP_WIDTH` mechanism
  (`util.default_wrap_width`). Budget: the bot's `telegram_wrap_width`
  default is **48** — column layout above is meso 8 (truncated per §6.1) +
  week 10 + plan 4 + bar 9 + actual 4 + pct 4 + separators ≤ 48, asserted by
  a renderer test (§9). The table (bar included) is **fixed-width**: it is
  laid out once for the 48-column budget and does not widen on a wider
  terminal — CLI and Telegram render identically (what you see on a TTY is
  what the bot sends), and the width test stays a single assertion. Wider
  terminals just get whitespace on the right.
- Past weeks: planned vs actual bar + percentage; `—` planned/percentage for
  ungoverned weeks (§3 empty states); future weeks: planned number only;
  current week per the §3 in-progress rule. Objective lines only for
  objectives inside the window (§11).
- `--weeks N` re-windows the past half (default 8); the future half always
  runs to plan end — that's the point of the feature.
- Rendering split as pure formatting helpers (fed by §5 outputs) so they are
  unit-testable without a DB, per `coach/formatting.py` precedent.

### 7.2 Telegram — text for free, chart as a photo

No bot-native command; both paths ride the CLI-as-subprocess parity model
(ARCHITECTURE §2), which is what makes them cheap:

- **Text:** `/progress` in chat just runs `tm progress`; the width-aware
  renderer (§7.1) is the whole story. `MENU_COMMANDS` in `trainmate_bot.py`
  (hand-synced by design) gains a `progress` entry.
- **Chart:** `/progress --chart` renders the full §2 two-panel picture to
  PNG (matplotlib, `Agg` backend) and sends it as a photo. `--chart` is
  additive: the text output still prints/sends — the photo is the picture,
  the text is the numbers.
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
- **matplotlib** joins `requirements.txt` in the optional tier (like
  `python-telegram-bot`): imported lazily inside the `--chart` path; without
  it, text mode works and `--chart` fails with an install hint. First import
  builds the font cache (seconds, one-time) — well inside the bot's
  `telegram_command_timeout` (180 s), noted here so a slow first `/progress
  --chart` isn't mistaken for a hang.

### 7.3 Web: **Progress** tab

Fifth top-level tab in `static/index.html` / `static/app.js`, loaded lazily
via the existing `loadedTabs` mechanism.

- **Charting: uPlot** (~45 KB + its stylesheet, no build step), loaded from
  CDN exactly as Font Awesome and Google Fonts already are — consistent with
  existing practice; vendoring into `static/vendor/` is a trivial later
  hardening step if offline use matters.
- **Two panels, native uPlot cursor sync** on the shared time axis:
  - *PMC panel:* three daily series; the actual→projected transition
    rendered by splitting each series into a solid pre-today and dashed
    post-today pair (six series; legend collapses each pair to one entry).
    Today rule + objective flags + plan-end label via a small `draw` hook.
  - *Load panel:* weekly paired bars. Honest note: paired/grouped bars are
    **not** stock uPlot — they come from the `seriesBarsPlugin` demo plugin
    (vendored alongside, it's a single file) or a custom paths builder.
    Mesocycle band tint + labels (hatched for `inferred`, §6.1) via a `draw`
    hook. This is the bulk of the web work; still far less than hand-rolled
    SVG with tooltips/cursors/scales.
  - Dates: `YYYY-MM-DD` strings are converted to epoch at **UTC midnight**
    consistently on both axes — the classic local-vs-UTC off-by-one-day is
    the main foot-gun with a time-series lib on date-only data.
- Default window 8 weeks back → plan end, with quick-range buttons
  (8w / season / all). No server round-trip needed for re-windowing beyond
  the initial fetch of the full default range.
- **Accepted cost:** the PNG (matplotlib, §7.2) and the tab (uPlot) are two
  hand-maintained renderings of the §2 picture and will drift in detail.
  §2 is the canonical layout both answer to; pixel parity is a non-goal.

**Rejected alternatives (web):** hand-rolled SVG (tooltips/cursor/scales are
real work; not worth it for a local app when a 45 KB lib exists); Chart.js
(heavier, and its mixed-type support buys nothing once the two-panel split
is chosen); rendering the web tab server-side to PNG (kills the
hover-to-inspect interaction that makes adherence bars useful — PNG is the
right shape for chat, §7.2, where there is no hover). Also rejected:
reusing the §7.2 matplotlib PNG *as* the web tab (same hover objection) and
a bot-native chart command bypassing the CLI (breaks the parity model that
keeps the bot maintenance-free).

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
   variants) from `completed_activities`; past-only until planned workouts
   carry intensity targets.

*(Rev 1 listed Telegram `/chart` and a CLI sparkline as follow-ons; both
were promoted into v1 — see §7.1–7.2 — because the CLI and the bot are the
surfaces actually used daily. A calibrated-projection follow-on was
considered and rejected — see the §3 limitations. Follow-ons 1–3 inherit
the §7.2 photo transport for free where they need a chart in chat.)*

## 9. Testing

- `tests/test_progression.py` — pure-function tests (patch-before-import
  pattern, §5): fixture activities + workouts spanning the today-seam;
  assert seam rule incl. the zero-load-activity case (§3), recursion values
  against hand-computed CTL/ATL/TSB, mean-load seeding incl. the
  shorter-than-42-days fallback (§4), zero-gap day filling, planned-side
  sRPE fallback + no-tss-no-rpe warning counting (rest rows excluded),
  Monday week bucketing, in-progress-week elapsed split, plan-end clamp,
  §6.1 majority-overlap labeling over fixture spans, and the three empty
  states.
- CLI renderer tests (`tests/test_cli_progress.py` or alongside existing CLI
  tests): the pure formatting helpers (§7.1) over fixture series — plan-end
  banner vs per-objective projection lines, past / in-progress / ungoverned
  / future week rows, bar scaling (shared max anchor incl. a future planned
  week; ungoverned week still gets a bar), warning footer; plus a narrow
  `TRAINMATE_WRAP_WIDTH` variant asserting nothing exceeds 48 chars.
- `tests/test_bot.py`: `parse_photo_request` round-trip with `emit_photo`
  framing (sentinel/JSON incl. the `caption` field, non-photo lines return
  `None`, unknown-sentinel lines dropped) — same pattern as the existing
  `parse_prompt_request` tests. The matplotlib rendering itself stays untested (visual output),
  matching the front-end stance below.
- Endpoint test alongside the existing web tests: payload shape (incl.
  `plan_end`, in-progress week fields, `meso_bands` layering), window
  clipping vs full-history computation (a point *inside* the window must
  reflect load *before* the window), pure-reader property (no Garmin/LLM
  mocks needed — that's the assertion).
- Web front-end stays untested, per existing practice.

## 10. Rollout

Ordered by usage (CLI/bot before web), each step independently shippable:

1. `trainmate/progression.py` + `tests/test_progression.py`; promote
   `adherence._planned_load` → `adherence.planned_load` (public, §3).
2. `tm progress` text mode (`trainmate/cli/progress.py` with auto-ensure,
   dispatcher entry + `prog` alias, removal of the `p` alias for `plan`,
   formatting-helper tests). This alone lights up Telegram text via parity —
   plus the `MENU_COMMANDS` entry in `trainmate_bot.py`.
3. Photo transport + chart: `PHOTO_SENTINEL`/`emit_photo` in
   `trainmate/prompt.py`, the `_drive()` photo branch (+ unknown-sentinel
   drop) + `parse_photo_request` in `trainmate_bot.py` (+ tests), `--chart`
   matplotlib rendering, matplotlib in `requirements.txt` (optional tier),
   `progress.png` in `.gitignore`.
4. `GET /api/timeline` in `trainmate_web.py` + endpoint test.
5. **Progress** tab (index.html, app.js, style.css; uPlot CDN include +
   stylesheet; vendored `seriesBarsPlugin`).
6. ARCHITECTURE.md: §2 module map (+`progression.py`, +`cli/progress.py`,
   bot photo protocol bullet), §7 CLI command table (+`progress`), §8
   endpoint table + front-end tab list (+Progress), §12 new PMC subsection
   (constants, coexistence with rolling-sum ACWR), §14 test-file table
   (+`test_progression.py`, +CLI renderer tests), §15 pointer to this doc.

## 11. Open questions

- **Rest-day TSB display**: TSB uses day-*entering* form (§4, and so does
  the payload field — documented in §6); whether the tooltip should also
  show day-closing values is a presentation nit to settle in review.
- **Multi-objective seasons** — live today, not hypothetical: three
  objectives are active (2026-09-30 → 2027-01-31) while the plan only
  extends through the first. The *governing objective* for meso labels is
  defined (§6.1); v1 draws flags for `active` objectives whose `target_date`
  falls inside the *displayed* window — clipping is the renderer's job, the
  endpoint returns all active objectives (§6) — later ones are simply
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
