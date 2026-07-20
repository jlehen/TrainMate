# Benchmark Workouts

Make fitness tests (FTP, threshold pace, CSS, e1RM) a **first-class, planned**
part of TrainMate: the planner schedules them on fresh days, daily adaptation
protects them, and their results are recorded in a dated logbook that becomes the
source of truth for the athlete's thresholds — feeding the coaching prompt, the
replan trigger, and long-term progress tracking.

Today a benchmark exists only as prose in `trainmate/science/benchmarks.txt`. The
coach *may* schedule one as an ordinary workout, but nothing places them
reliably, nothing stops daily `adapt` from softening one into meaninglessness,
and there is nowhere to put the result. This design closes those three gaps.

---

## 1. Why this is safe: TSS does not depend on the app's FTP

The scary version of this feature is "a benchmark updates my FTP, which rewrites
all my historical TSS and corrupts the PMC." **That cannot happen here**, and it
is worth stating up front because it shapes everything below.

TrainMate computes TSS from Garmin's *time-in-zone seconds* (`garmin/load.py`),
where the zoning was already done inside the athlete's Garmin account. The
`ftp`/`lthr` scalars in `config.yaml` are never read by the load model. They feed
exactly two things:

- the **coaching prompt** the LLM reads (so it prescribes zones/targets), and
- the **plan-staleness check** (`config_changed()`, `service/prompt.py:33`).

So updating an app-side threshold is a *forward-looking prescription* change, not
a *retroactive accounting* change. There is no corruption pathway. (The real
lever on historical TSS accuracy is the FTP configured in the athlete's Garmin
account — which the app can remind the athlete to update but cannot set.)

---

## 2. The core model

A benchmark is a workout whose **purpose is measurement**, not stimulus. That one
difference gives it two behaviors no ordinary session has:

- **Before the test — it needs freshness.** A test on a fatigued day (negative
  TSB) reads low and then mis-scales every workout after it. So it wants a rested
  or opener day in front of it, and it must be *moved* rather than *eased* if the
  athlete is not fresh on the day.
- **After the test — it produces a result.** A threshold value (FTP watts,
  threshold pace, CSS, e1RM) that should be recorded with a date and fed forward.

Everything else about a benchmark is already an ordinary `Workout` on a date with
a sport. So we do **not** build a new "benchmark entity." We add a marker to the
workout, and a separate logbook for results.

---

## 3. Data model

### 3.1 `benchmark_type` on `Workout`

Add a nullable `benchmark_type` to the `Workout` TypedDict (`types.py:47`) and the
`workouts` table (`db/base.py:112`). When set, the session is a test:

    ftp_20min | ftp_ramp | run_threshold_30min | run_5k_tt |
    css_400_200 | e1rm | mas_cooper | ...

This rides the existing generate / adapt / list / calendar-sync machinery for
free — it is just a workout carrying a marker, exactly like today's `[MANUAL]`
or `[SWAPPED]`.

### 3.2 `benchmark_results` — the logbook

This is the one genuinely new abstraction. It is **not** an abstract "anchor
store" — it is a dated logbook of test results. One row per measurement:

| column        | meaning                                              |
| ------------- | ---------------------------------------------------- |
| `id`          | pk                                                   |
| `date`        | when the test was performed                          |
| `sport_type`  | cycling, running, swimming, strength, …              |
| `anchor_kind` | `ftp` \| `threshold_pace` \| `css` \| `e1rm` \| `mas` |
| `value`       | the number (e.g. 250)                                |
| `unit`        | `W` \| `min_per_km` \| `sec_per_100m` \| `kg` …        |
| `source`      | `test` \| `modeled` \| `manual` \| `seed`             |
| `workout_id`  | nullable link to the planned benchmark it satisfied  |
| `note`        | free text (protocol, conditions)                     |

Two reads answer everything:

- **"What is my FTP right now?"** → the most recent `cycling` / `ftp` row.
- **"Is my fitness rising?"** → read down the column: 235 → 242 → 250. That
  progression *is* the signal `benchmarks.txt` cares about ("a rising anchor
  confirms progressive overload; a stalled or falling anchor signals plateau").

A single overwritten scalar cannot show that trend; a dated logbook can.

### 3.3 The `effective_threshold` accessor — the linchpin

Introduce **one** accessor that both the prompt and the staleness check read
through:

    effective_threshold(sport, kind)  ->  latest benchmark_results row if any,
                                          else config.user_profile value

Then wire it into the single existing read point, `_get_config_thresholds()`
(`engine/prompt.py:251`), which already feeds *both* the macrocycle threshold
snapshot and the drift comparison in `config_changed()`. Route the prompt-side
threshold injection through the same accessor.

Because `_get_config_thresholds()` is the one place thresholds are read, this one
change gives us the whole behavior with no special cases:

- The plan **snapshots** whatever the *effective* value was at generation time.
- `config_changed()` compares *effective-now* against that snapshot.
- Once a logbook value exists, editing `config.yaml`'s `ftp` no longer changes
  *effective-now*, so it **cannot** trigger a replan — not because of an
  "ignore ftp" rule, but because ftp is simply no longer the effective value.
  It becomes inert everywhere, automatically.
- A benchmark result that moves the effective value **>5%** (the existing
  `coach.threshold_replan_pct`, `config.py:130`) flows through the *same*
  threshold-drift axis that already exists → the plan is flagged stale and a
  replan is suggested. Sub-5% retest corrections feed the next workout
  generation without invalidating the strategy — exactly today's behavior.

No new staleness logic. One accessor, one wiring change.

### 3.4 `config.yaml` becomes the seed, not the source of truth

`user_profile.ftp` / `lthr` do **not** disappear. They are the **cold-start
seed** — effectively "row zero" of the logbook (`source: seed`) before any test
exists. Once a real result lands, the latest logbook row wins and config is no
longer read for prescription. The athlete never hand-edits it again; tests move
the number forward.

Annotate both `config_template.yaml` and the real `config.yaml` so this is
explicit:

```yaml
  lthr: 165   # threshold HR — starting value only; superseded once you record a benchmark
  ftp: 220    # FTP watts — starting value only; superseded once you record a benchmark
```

---

## 4. Behavior

### 4.1 Placement — deterministic backbone, LLM garnish

Benchmarks belong at block boundaries and on a ~4–6 week cadence
(`benchmarks.txt` §1). Place the backbone **deterministically** — one benchmark
at each mesocycle boundary plus one validation test before the goal — the same
way rest windows are enforced today (`_enforce_rest_windows`). Deterministic
placement means a test can't silently vanish because a prompt got distracted.
The strategy LLM may *add* sport-specific extras on top.

The workout-gen pass then drops an **opener / easy day** in front of each test so
TSB is positive on test day.

### 4.2 Adapt — "reschedule, don't dilute"

This is the one rule genuinely different from every other session, and it needs a
**deterministic guard** (like rest-window enforcement), not just prose, so the
LLM can't quietly soften a test:

- A benchmark is **never eased** (easing defeats its purpose).
- If TSB is negative on the benchmark date, `adapt` **moves** the test to the
  next fresh day within the block and lightens the days before it.

This composes cleanly with the existing block-boundary firewall
(`DESIGN_block_boundary.md`): `adapt` already never crosses into the next
mesocycle, and the end-of-block benchmark lands exactly where the block-boundary
machinery already nudges the athlete to replan the next block against fresh
numbers.

---

## 5. Capture

### 5.1 Propose → confirm, never silent

Recording a result **proposes** the update and asks for confirmation before it
touches anything — the same pattern the codebase already uses for constraint
extraction and coach-learning downgrades. Nothing auto-mutates thresholds.

Example:

    New FTP 250 (was 235, +6.4%) — record and suggest replanning the next block? [y/N]

### 5.2 Manual entry is the primary path for cycling (Zwift)

FTP tests are done indoors on Zwift (ramp or 20-min protocol), which **computes
and displays the FTP number on screen**. Meanwhile the app stores only Garmin's
bucketed zone-seconds, **not** the raw power stream (`garmin/load.py`) — so it
cannot recompute "20-min best power × 0.95" after the fact. The data simply isn't
there. Therefore:

    tm benchmark record --sport cycling --ftp 250

is the primary capture path — reliable, one line, using Zwift's authoritative
value. Auto-extraction from the activity stream is explicitly **not** built first
(possible "later, other sports" idea, not load-bearing).

### 5.3 Activity matching marks the test done

The completed ride reaches Garmin Connect regardless of the Zwift→Garmin link,
because the athlete also records on a Garmin device. TrainMate's normal Garmin
pull sees it, and the adherence matcher marks the *planned* benchmark complete.
The FTP *number* comes from the `benchmark record` command; Garmin's job is only
"yes, the test happened."

**No dedup needed:** the athlete deletes the Zwift-uploaded copy in Garmin
Connect by hand, so a single Garmin-native activity remains.

### 5.4 Venue lives in preferences, not in the benchmark

The planned benchmark's description stays **venue-neutral** ("20-min FTP test or
ramp test"). The athlete's `user_profile.preferences` free text — already injected
into the coaching prompt (`engine/prompt.py:57`) — carries the venue:

```yaml
  preferences: |
    ...
    Tests FTP indoors on Zwift (ramp or 20-min protocol).
```

The coach reads that during `workout generate` and phrases the session as
indoor/Zwift on its own. Zero new machinery, and the benchmark *type* stays
generic so an outdoor test just needs a preference edit.

---

## 6. Surfacing

- `workout list` gains a `[BENCHMARK]` marker (alongside `[MANUAL]`, `[SWAPPED]`).
- New CLI verb `benchmark`, mirroring `goal` / `constraint`:
  - `benchmark record --sport cycling --ftp 250 [--date …] [--note …]`
  - `benchmark list` — the logbook, newest first, with deltas.
- `status` shows the current effective threshold per sport and its last-tested date.
- Progress timeline (`DESIGN_progress_timeline.md`) plots the anchor trend beside
  CTL — the "is overload working?" line.

---

## 7. Phasing — pick an ambition

**MVP (pure planning win, planning + adapt only):**
`benchmark_type` flag on `Workout` · deterministic block-boundary placement +
opener day · the "reschedule-don't-ease" adapt guard · `[BENCHMARK]` marker. No
result capture yet — the athlete just reliably gets tests on fresh days.

**V2 (the logbook unlocks the rest):**
`benchmark_results` table · `effective_threshold` accessor wired into
`_get_config_thresholds` · `benchmark record` / `benchmark list` CLI ·
propose→confirm capture · config seed comments · anchor feeds the prompt and the
existing >5% staleness/replan trigger.

**V3 (richer):**
Activity matching auto-marks benchmarks done · modeled/passive anchors
(power-duration curve for FTP, e1RM from rep-max sets) · progress-timeline
integration · a measured-vs-modeled coach learning.

The MVP is useful standalone and touches only planning/adapt; the logbook (V2) is
the piece that turns a benchmark from "a session the LLM happened to schedule"
into "a measurement that updates a durable source of truth."

---

## 8. Open decisions

- **Anchor kinds beyond FTP.** MVP/V2 can ship cycling FTP alone; threshold pace,
  CSS, and e1RM reuse the identical `benchmark_results` shape and can follow.
- **Cadence knob.** Whether the 4–6 week cadence is a config value or fixed to
  "one per mesocycle boundary." Recommend the latter for MVP (simpler, matches
  block structure).
- **e1RM auto-capture** eventually blurs the test/normal-session line (any
  rep-max set is a passive test) — deferred to V3, noted here so the schema
  (`source: modeled`) already anticipates it.

---

## 9. Interactions with existing designs

- `DESIGN_block_boundary.md` — end-of-block benchmark is the natural replan
  trigger; adapt's within-block firewall already prevents a test from being
  dragged across a boundary.
- `DESIGN_pmc_fitness_fatigue.md` — TSB gates test-day freshness (the
  reschedule guard reads TSB); benchmarks do not alter PMC math.
- `DESIGN_progress_timeline.md` — the anchor time series is a first-class
  progress signal to plot beside CTL.
