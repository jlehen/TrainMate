# Code review: Progress Timeline implementation vs DESIGN_progress_timeline.md

**Date:** 2026-07-04 · **Reviewed:** the uncommitted implementation of
DESIGN_progress_timeline.md (rev 4) — `trainmate/progression.py`,
`trainmate/cli/progress.py`, `/api/timeline`, the Progress tab, the photo
transport, and all associated tests.

> **Superseded in part (2026-07-07):** this review targets the rev-4 snapshot,
> which predates the PMC merge to main (`DESIGN_pmc_fitness_fatigue.md`,
> `ca8591b`). Design rev 5 reworks the very foundation this review calls
> trustworthy: `progression.py`'s own recursion, `CTL_DAYS`/`ATL_DAYS`, and
> mean-seeding are replaced by reading the stored `athlete_metrics_cache`
> series plus an anchored `compute_pmc(seed=...)` fold (see the rev-5 note and
> §4 there). The presentation-layer findings below still stand.

**Verdict:** the pure-math foundation (`progression.py`) is trustworthy, but
the presentation layer betrays it. Five blocking findings, then smaller
issues, then what's good. Tests run: 76 pass across `test_progression.py`,
`test_cli_progress.py`, `test_bot.py` (the web tests need flask, and the wider
suite needs the `google` package — both environment gaps, not regressions).

---

## Blocking findings

### 1. The design's centerpiece rule is computed and then thrown away

The single most emphasized behavior in the design — repeated in §3, §5, §6,
§7.1, and §9 — is the in-progress-week rule: the current week's planned figure
must cover elapsed days only, "identical on all three surfaces," with the
payload carrying `planned_load_elapsed` "so renderers can't diverge."

What actually happens: `weekly_aggregates` dutifully computes
`planned_load_elapsed` (trainmate/progression.py:249), and then **nothing
anywhere reads it**. The only references in the whole tree are inside
`progression.py` itself. The CLI row renderer (`_week_row`,
trainmate/cli/progress.py:129-143) uses the full-week `planned_load` for both
the plan column and the percentage. The web bars use `w.planned_load`. The PNG
uses `w["planned_load"]`.

Concrete failure: Monday morning, one easy session done, the week's plan
totals 360. `/progress` in Telegram reads
`Build 3  w/c 07-06*  360  ▓░░░░░░░░  45  13%` — exactly the "comparing a
full-week planned total against a partial actual reads as poor adherence every
Monday" scenario §3 exists to forbid. The mock shows `150 ... 138 92%`, 150
being the Monday-through-Friday elapsed slice. The asterisk is rendered; the
thing it signifies is not. The legend was also quietly downgraded from the
mock's `* in progress: plan = Mon–Fri` to just `* in progress`.

The tests are complicit: the CLI in-progress test asserts only that an
asterisk appears, and the 48-column width test hand-feeds `planned_load:
150.0` — the mock's *elapsed* value — directly into the fixture. These tests
were written to the code, not to the design.

### 2. Mesocycle labeling: one of three layers missing, and the layering isn't layered

§6.1 specifies three label sources in authority order: active plan, then
superseded plan versions for past weeks ("`macrocycles.created_at` identifies
which version was in force"), then the bootstrap reconstruction. The
implementation has two of the three. Both the CLI handler and the web endpoint
call `db.get_macrocycle_for_objective`, which (trainmate/db/periodization.py:9)
explicitly excludes superseded versions. After the next plan regeneration,
past weeks will be labeled by the *new* plan's mesocycles rather than "what
the plan asked at the time" — contradicting the consistency argument §6.1
makes with the planned-load bars. Worse, `progression.meso_bands`'s docstring
says "the caller resolves which macrocycle *version* governed a given past
week" — and neither caller does. The docstring documents an implementation
that doesn't exist.

The lookup also isn't layered — it's a flat majority vote. `_week_meso` counts
covered days across all spans, inferred and plan mixed, and the biggest count
wins. Concrete failure: the bootstrap's last inferred block runs through
plan-adoption week (realistic — the reconstruction runs up to the bootstrap
date, the plan starts days later). The inferred block covers all 7 days of the
transition week; the first plan mesocycle covers 4. Inferred wins 7–4 and the
week labels `~Base` even though a real plan governed it — the exact case
§6.1's authority ordering exists to prevent. The tie-break is an accident too:
`_week_meso`'s docstring demands chronologically ordered spans, but
`meso_bands` builds the list grouped by source (all inferred, then all plan),
so "plan beats inferred on a tie" only holds because of append order.

### 3. Governance is inferred from the label, turning a labeling nit into dropped data

The design defines an "ungoverned week" via plan coverage (the `adherence.py`
covered-ranges precedent). The implementation instead uses
`governed = meso_source == "plan"` (trainmate/progression.py:237): whether a
week gets a planned figure at all is decided by which mesocycle label won the
majority vote. In the transition-week scenario above, a week that **has
planned workout rows in the database** renders planned `—`, no bar comparison,
no adherence percentage — the planned load silently vanishes from the table
while the PMC projection three lines above happily consumed those exact same
workouts. Two halves of the same screen disagree about whether a plan exists.
Same failure if the macrocycle row is missing entirely (plan wiped, workouts
kept): the whole planned column goes `—` while the projection still runs.

### 4. The CTL seed can be contaminated by the plan

§4 pins the seed as the mean daily load of the first 42 days *of history* (a
rev-4 reviewed decision). `fitness_series` seeds from `day_points[:42]` — but
`day_points` is the *merged* series extending through plan end. Concrete
failure: fresh install, 10 days of Garmin history pulled, 28-day plan
generated → the CTL seed is the mean over 10 actual days plus 32 *planned*
days. The projection's starting fitness is partly derived from workouts that
haven't happened. Latent with ~6 months of history, but it's exactly the
fresh-install case the empty-states section worries about, and roughly a
one-line fix (seed from actual-source points only).

### 5. CLI and web disagree about the plan-gap warning (copy-paste divergence)

§3 says "every surface annotates the gap." The CLI
(trainmate/cli/progress.py:360-368): if the plan reaches objective 1, it
prints the per-objective projection and no gap warning — arguably right per
§11's "later objectives are simply off-canvas." The web endpoint warns about
the *next* objective beyond plan end even when the plan fully reaches
objective 1 — so with three live objectives, the moment `--until-goal` is run
the web tab starts nagging about a plan that doesn't exist yet and per §11
shouldn't be nagged about. The two also format the warning differently
(`(9 wks before)` vs `(9 wks before objective 2026-09-30)`).

Root cause: roughly forty lines — governing-macro resolution, meso-span
assembly, warning construction — are copy-pasted between `run_progress` and
`get_timeline` instead of living in one shared assembly function, and the
copies have already drifted. The design put the pure math in one module
precisely so the surfaces couldn't diverge; the glue that feeds it was
duplicated anyway.

---

## Significant deviations and defects

### The web tab is not the design's web tab

§7.3's stated reason for choosing uPlot was two panels with native cursor sync
on a shared time axis, paired bars via the vendored `seriesBarsPlugin`
(rollout step 5 lists vendoring it), meso band tint with hatching, and a
plan-end label. What shipped: the PMC panel in uPlot, and the load panel as a
hand-rolled DOM flexbox bar chart with a `title` tooltip. No cursor sync
(impossible across a canvas and a div), no shared axis (flex columns aren't on
a time scale), no hatch (an italic label), no plan-end label, no today marker
on the load panel. The `renderWeeklyBars` comment renegotiates the design
inline — and hand-rolled DOM/SVG is the *explicitly rejected alternative* in
§7.3's own rejection list. Possibly a defensible v1 simplification, but it's
an undocumented deviation.

### The PNG breaks its own shared time axis

`_render_chart_png` creates the two panels without `sharex=True`, then paints
`axvspan` meso bands from the **full, unclipped** span list onto the bottom
panel. Matplotlib extends axis limits to include spans, so inferred bootstrap
blocks from months back stretch the bottom panel's x-axis to the start of
history while the top panel shows the 8-week window — the §2 picture arrives
in Telegram with the two panels covering different date ranges and the weekly
bars squashed into the right edge. Also missing: objective flag labels and the
plan-end annotation (anonymous vertical lines only).

### The web endpoint omits the empty-state warnings

§3 requires the endpoint to return the "No activity history yet" warning with
empty `days`/`weeks`, and a "no plan generated — projection unavailable" note.
Neither appears in `warnings`; the web tab shows the no-history message only
because the JS hardcodes it.

### The web test suite contains a literal time bomb

`test_in_progress_week_carries_elapsed_split` hardcodes that the week of
2026-06-29 is in progress and comments "Real 'today' in this sandbox's clock
is 2026-07-03." From Monday 2026-07-06 it fails forever. Root cause is a
testability hole: `get_timeline` calls `today_str()` internally with no
injection seam, unlike the pure functions which take `today` as a parameter.

### The stale-workout warning never heals

`zero_load_workout_count` counts every non-removed workout in the whole table,
forever. The §3 warning is about *future* rows feeding the projection as 0.
Historical workouts that never got TSS/RPE keep the banner permanently lit
with a number unrelated to the current projection.

### "Width-aware" was not implemented

The design says width-aware via `util.default_wrap_width`; `progress.py` never
calls it. The FORM line is ~59 characters against the 48-column Telegram
budget, so on the primary surface the headline hard-wraps mid-sparkline. (The
design's own mock is 59 wide too, so the doc seeded this — but the code was
supposed to handle width and doesn't look at it.)

### A lapsed plan renders as a projection

If plan end is in the past (rolling horizon, no recent generate), the CLI
prints "Projected at plan end 05-20: CTL …" — "projected" attached to a
historical date, plus a gap banner. §3's empty states cover "no plan" but not
"stale plan," and the code routes it through projection wording.

---

## Smaller issues

- `_week_meso`'s `week_start` parameter is dead.
- `getattr(args, 'weeks', None) or 8` silently turns `--weeks 0` into 8 and a
  negative value into nonsense slicing.
- Under the json frontend, `_emit_chart` creates the temp file *before*
  importing matplotlib, so a missing matplotlib leaks a temp file per call.
- The uPlot `<script>` is blocking in `<head>`: if the CDN is unreachable the
  whole page stalls at render (`defer` fixes; the design's vendoring note
  exists for a reason).
- The JS date arithmetic round-trips through `toISOString()`, reintroducing
  the local-vs-UTC off-by-one that the adjacent comment quotes §7.3 warning
  about.
- The `renderPMCChart` comment claims "a null gap so uPlot doesn't draw a seam
  connector" while the code shares today's point between both halves — the
  comment describes code that isn't there.
- The endpoint clips weeks by `week_commencing >= start_date` while clipping
  days by date, so a mid-week window start keeps the days but drops their
  week's bar.
- The CLI legend prints "~ inferred (bootstrap)" even when nothing inferred is
  on screen.
- Test coverage mirrors the bugs rather than the design: no test that elapsed
  planned is *displayed*, none for the label/governance interaction, none for
  the endpoint empty-state warnings.

---

## What's good

`trainmate/progression.py`'s core math is clean and correct against §3/§4 —
the recursion, calendar-mean seeding (modulo the future-contamination edge),
seam rule including the zero-load-activity case, plan-end clamp, and
removed-row exclusion, all verified by genuinely good tests
(`test_progression.py` uses a fixed anchor date and explicit `today`, exactly
how the web test should have been written). The photo transport is implemented
essentially as specified — sentinel framing, caption in payload,
unknown-sentinel drop, bot-owned cleanup in `finally` — with proper round-trip
tests. The `planned_load` promotion, `p`-alias removal, `.gitignore`,
requirements tier, and ARCHITECTURE.md updates all landed per rollout step 6.

## Recommended blocking list before merge

1. Renderers (CLI row, web bars, PNG) consume `planned_load_elapsed` for the
   in-progress week.
2. Decouple week governance from the majority-vote meso label (use plan
   coverage).
3. Deduplicate the CLI/web assembly (governing macro, meso spans, warnings)
   into one shared helper.
4. Fix the clock-dependent web test and give `get_timeline` a `today` seam.
5. `sharex=True` + span clipping in the PNG renderer.

Everything else can be a fast follow.
