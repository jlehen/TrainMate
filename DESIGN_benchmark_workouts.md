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

## 1. Why this is safe: TSS does not depend on the app's thresholds

The scary version of this feature is "a benchmark updates my FTP, which rewrites
all my historical TSS and corrupts the PMC." **That cannot happen here**, and it
is worth stating up front because it shapes everything below.

TrainMate computes TSS from Garmin's *time-in-zone seconds* (`garmin/load.py`),
where the zoning was already done inside the athlete's Garmin account. The
app-side `ftp`/`lthr` values (the benchmark logbook, §3.2) are never read by the
load model. They feed exactly two things:

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

The column itself is cheap; carrying the value is not free. A workout is never
passed around as an opaque row — every hand-off rebuilds it from an explicit
field list, and a field missing from any list is silently dropped. The flag must
therefore be threaded through each enumeration:

- the model's JSON output contract in **both** generate and adapt, with a prompt
  instruction to preserve the field when re-emitting a session. The model, not
  the app, owns the flag's survival across an adaptation — consistent with
  §4.2's no-guards stance;
- `save_workout` (signature + SQL) on the generate save path;
- the adapt rebuild dict (`adaptation.py:243-263`) and `workout_adapt_apply` —
  the spot a first pass misses. Without it, the model's proposal to move a test
  is rebuilt without the flag and saved as an ordinary workout: the act of
  protecting the test is exactly what would strip its benchmark identity,
  silently.

As a stored column it is creation-time intent, exactly like the existing
`source` column — *not* like the `[MANUAL]`/`[SWAPPED]` markers, which are
deliberately **derived** from `modification_reason` (`modification_state.py`
documents why derived kind-columns are preferred for mutable state). A
benchmark's identity is fixed when the session is created, so a column is the
right shape here.

### 3.2 `benchmark_results` — the logbook

This is the one genuinely new abstraction, and — with §3.4 — the **only** place
the athlete's thresholds live. It is **not** an abstract "anchor store" — it is
a dated logbook of test results. One row per measurement:

| column        | meaning                                              |
| ------------- | ---------------------------------------------------- |
| `id`          | pk                                                   |
| `date`        | when the test was performed                          |
| `sport_type`  | cycling, running, swimming, strength, …              |
| `anchor_kind` | `ftp` \| `lthr` \| `threshold_pace` \| `css` \| `e1rm` \| `mas` |
| `value`       | the number (e.g. 250)                                |
| `unit`        | `W` \| `bpm` \| `min_per_km` \| `sec_per_100m` \| `kg` … |
| `source`      | `test` \| `manual` \| `modeled`                      |
| `workout_id`  | nullable link to the planned benchmark it satisfied  |
| `note`        | free text (protocol, conditions)                     |

Each sport has its natural anchor kind(s): cycling→`ftp`, running→
`threshold_pace` and `lthr`, swimming→`css`, strength→`e1rm`. `lthr` is a
first-class kind — a run threshold test produces it, and it is one of the values
the prompt prescribes from, so the logbook must be able to supersede it (§3.4).

"Latest" is defined as **newest by `date`, `id` as tiebreak**, so backdated
entries behave. Two reads answer everything:

- **"What is my FTP right now?"** → the latest `cycling` / `ftp` row.
- **"Is my fitness rising?"** → read down the column: 235 → 242 → 250. That
  progression *is* the signal `benchmarks.txt` cares about ("a rising anchor
  confirms progressive overload; a stalled or falling anchor signals plateau").

A single overwritten scalar cannot show that trend; a dated logbook can. One
display caveat: for pace kinds (`threshold_pace`, `css`) *lower is better*, so
trend/delta rendering carries a per-kind sign — a faster runner must not be
shown a negative-looking progression.

### 3.3 The effective-threshold accessor — the linchpin

Introduce **one** accessor that both the prompt and the staleness check read
through:

    effective_thresholds()  ->  {ftp, lthr, ...} from the latest logbook rows

It lives in the **service layer**, which is the only layer that has both config
and DB access — the engine is a pure prompt-builder over data handed to it and
imports no `db`, and that stays true. The service already passes
`profile=config.user_profile` into every engine call (`service/prompt.py:199`,
`service/workouts.py:213`, `service/adaptation.py:159`); it now overlays the
effective thresholds onto that dict first and passes the merged result. Two
engine-side changes complete the wiring (an earlier draft claimed "no engine
change" — wrong on inspection):

- **The prompt.** `_format_athlete_profile()` (`engine/prompt.py:39-44`) has
  hand-named FTP/LTHR lines; it instead renders *whatever* threshold kinds the
  profile dict carries, generically with the unit — no kind is privileged
  (§3.5), so a first swim test shows up in the prompt with zero further code.
- **The staleness check.** `_get_config_thresholds()` (`engine/prompt.py:251`)
  today reads `config.user_profile` directly — once `ftp`/`lthr` leave config
  (§3.4) it would silently return only `max_hr`. The threshold read moves to
  the service (or takes the merged profile as an argument), so
  `_get_config_snapshot()` and `config_changed()` (`service/prompt.py:28-64`)
  judge drift over effective values. The
  plan **snapshots** whatever the effective value was at generation time;
  `config_changed()` compares effective-now against that snapshot. A benchmark
  result that moves the effective value **>5%** (the existing
  `coach.threshold_replan_pct`, `config.py:130`) flows through the *same*
  threshold-drift axis that already exists → the plan is flagged stale and a
  replan is suggested. Sub-5% retest corrections feed the next workout
  generation without invalidating the strategy — exactly today's behavior.

**Snapshot scope: uniform, no privileged kinds.** Every anchor kind on record
joins the snapshot — `ftp`/`lthr` are not special (§3.5). That needs one small
fix first: `config_changed()` treats a key it has never seen as instant drift
("`css` was added", `service/prompt.py:59-60`), so a first-ever swim test would
flag every pre-existing plan stale, bypassing the 5% tolerance. Teach it to
**skip keys absent from the *old* snapshot**: a newly recorded kind starts
feeding prompts immediately and joins drift-checking from the next generated
plan onward; only a >5% *change* in a kind the plan was actually built with
triggers a replan. A key that *disappears* still reads as drift — a threshold
the plan relied on going missing is real. Same rule for every kind.

One accessor, one relocated threshold read, one skip rule in
`config_changed()` — no other staleness logic changes.

### 3.4 `ftp`/`lthr` leave `config.yaml` entirely

The logbook is the *only* home for trainable thresholds. `user_profile.ftp` and
`user_profile.lthr` are **removed** from `config.yaml` and
`config_template.yaml` (replaced by a pointer comment). `max_hr` stays in
config: the split is principled — config keeps quasi-fixed physiology and life
logistics (age, availability, equipment), the logbook keeps *trainable,
measured* quantities. This deletes the "seed value that becomes inert" concept
outright: there is no fallback branch in the accessor, no precedence rule to
document, and no config field that looks editable but silently is not.

**Seeding is a one-off, not machinery.** By the time seeding is possible,
`benchmark record` exists — and two invocations of it *are* the migration:

    tm benchmark record cycling --ftp 220 --note "seeded from config"
    tm benchmark record running --lthr 165 --note "seeded from config"

Two rules make the cutover seamless:

- **Sequence:** upgrade → seed → only then run any coach command. Once the code
  reads thresholds from the DB alone, a `generate`/`status` run before the seed
  rows exist finds no `ftp` key, compares against a macrocycle snapshot that has
  one, and reports a spurious "ftp was removed" (`service/prompt.py:59-60`).
- **Values:** seed the *exact* numbers currently in config, so existing
  macrocycle snapshots still match and `config_changed()` stays quiet.

**Cold start: nudge, never refuse.** A fresh install has no thresholds — and
the codebase already degrades gracefully: the prompt formatter emits threshold
lines conditionally (`engine/prompt.py:43`), and the snapshot/drift code skips
absent keys. A plan generated with no FTP on record prescribes by RPE and HR
feel, which is what a coach does with an untested athlete. So generation
proceeds, and a cold-start hint mirrors `_maybe_nudge_bootstrap()`
(`service/prompt.py:179`): *"No FTP on record — prescriptions will use RPE/HR
until you record one (`tm benchmark record …`) or complete the scheduled
benchmark."* The very first generated plan schedules a benchmark anyway (§4.1),
so the gap closes itself within the first block. Refusing to plan would create
a bootstrapping paradox — the planner is how a benchmark gets scheduled.

### 3.5 No privileged anchor kinds

Once this design lands, `ftp`/`lthr` are not special — they are merely the
first two rows the logbook happens to hold. Every kind (`css`,
`threshold_pace`, `e1rm`, `mas`, …) flows identically: recorded via
`benchmark record`, rendered into the prompt generically (§3.3), drift-checked
by the same skip-absent-keys snapshot rule (§3.3), trended by `benchmark list`
and the progress timeline. Adding a future kind is a vocabulary addition — a
new `anchor_kind` value, its unit, and its better-direction sign (§3.2) — not
new machinery.

---

## 4. Behavior

### 4.1 Placement — instruct, then verify

Benchmarks belong at block boundaries and on a ~4–6 week cadence
(`benchmarks.txt` §1). The split of labor plays to each side's strength:

- **The LLM places.** The generation prompt instructs the coach to schedule one
  benchmark of the appropriate kind in each mesocycle-boundary week the
  generated span covers (plus one validation test before the goal), preceded by
  an opener/easy day so TSB is positive on test day. Day choice stays with the
  model — it already handles weekly availability, equipment, and rest days, and
  a deterministic pass re-implementing that logic is exactly the machinery we
  do not want.
- **A deterministic post-check verifies.** After generation, if a covered
  boundary week ended up with no `benchmark_type` workout, print a warning —
  same spirit as the rest-window pass (`_enforce_rest_windows_generate`), but a
  warning rather than an insertion: a missing test surfaces for the athlete to
  regenerate, it is not silently auto-fixed. The check stays silent when the
  boundary week sits under a `rest` constraint — **rest wins**, and warning
  about it would be noise.

**Same-day collision.** `save_workout` keys on (date, sport), so a second
same-sport session on a benchmark date would overwrite the test. Deterministic
rule: on a date holding a benchmark of sport X, drop any other proposed sport-X
session and warn. The benchmark is identified by its flag — no guessing needed.

### 4.2 Adapt — "reschedule, don't dilute"

This is the one rule genuinely different from every other session, and it is
enforced the way every other adapt behavior is: **by instructing the model, not
by engineering guards around it**. A deterministic guard here would have to
reverse-engineer intent from a proposal batch — is this pair of changes a move,
a displacement, or a softening? (Concretely: exempting benchmark rows from the
overridden-workout deletion in `workout_adapt_apply`, `adaptation.py:299-308`,
would block the very deletion that completes a legitimate move, leaving the
test duplicated on both days.) That is precisely the judgement the model
already has in front of it, so the model keeps it.

**The prompt rule:** never reduce or soften a benchmark session; if the athlete
will not be fresh (negative TSB), move it *intact* — same content,
`benchmark_type` preserved — to a later day within the block and lighten the
days before it. Moving is necessarily the LLM's call: TSB is backward-looking
only (`garmin/pmc.py:47` — computed from *completed* load), so no deterministic
pass can know which future day will be fresh; the model, which sees the TSB
history and the planned load ahead, judges it. Fallback the model is told
explicitly: when the benchmark sits on the last day of the block and no later
in-block day exists, leave it in place and lighten the days before it —
slightly-off freshness beats a lost test.

A moved benchmark rides the normal apply path like any rescheduled session; the
flag travels because it is part of the model's output contract (§3.1). No
exemptions, no proposal rejection, no special-casing in the apply step.

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

    tm benchmark record cycling --ftp 250

is the primary capture path — reliable, one line, using Zwift's authoritative
value. Auto-extraction from the activity stream is explicitly **not** built first
(possible "later, other sports" idea, not load-bearing).

### 5.3 Activity matching confirms the test happened

The completed ride reaches Garmin Connect regardless of the Zwift→Garmin link,
because the athlete also records on a Garmin device. TrainMate's normal Garmin
pull sees it, and the adherence matcher (derived per-run — nothing persists a
completion flag today) confirms the *planned* benchmark was done. The FTP
*number* comes from the `benchmark record` command; Garmin's job is only "yes,
the test happened."

When two same-sport activities land on the test date, match the one whose
load/duration is closest to the planned test; if the candidates are too close
to call, print an error and let the athlete resolve it — never guess.

**No dedup needed:** the athlete deletes the Zwift-uploaded copy in Garmin
Connect by hand, so a single Garmin-native activity remains. (Known failure
mode, accepted: sync keys on `activity_id`, so a forgotten deletion
double-counts that day's load in the PMC. Not worth machinery until it actually
happens.)

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
  - `benchmark record cycling --ftp 250 [--date …] [--note …]`
  - `benchmark list` — the logbook, newest first, with deltas (signed per kind:
    lower is better for pace anchors, §3.2).
  - `benchmark rm <id>` — the correction path. A typo here is high-consequence
    (`--ftp 520` jumps the effective threshold and flags a replan);
    "latest row wins" makes delete-and-re-record a sufficient editing story.
- `status` shows the current effective threshold per sport and its last-tested date.
- Progress timeline (`DESIGN_progress_timeline.md`) plots the anchor trend beside
  CTL — the "is overload working?" line.

---

## 7. Phasing

Removing thresholds from config (§3.4) makes the logbook the foundation
everything sits on, so it ships first — not as a V2 refinement.

**Phase 1 — the logbook replaces config thresholds:**
`benchmark_results` table · `benchmark record` / `list` / `rm` CLI ·
propose→confirm capture · service-layer effective-threshold overlay wired into
the profile flow and the staleness snapshot (threshold read relocated out of
the engine, generic prompt rendering, skip-absent-keys drift rule — §3.3) ·
one-off seeding + removal of `ftp`/`lthr` from config · cold-start nudge.
Standalone value: thresholds become dated, trended, and feed the existing >5%
replan trigger.

**Phase 2 — planning & protection:**
`benchmark_type` threaded through the workout plumbing (§3.1: column, model
output contracts in generate *and* adapt, `save_workout`, adapt rebuild dict) ·
placement prompt instruction + boundary-week post-check warning · opener day ·
the adapt "reschedule, don't dilute" prompt rule (§4.2) · same-day collision
rule · `[BENCHMARK]` marker.

**Phase 3 — richer:**
Activity matching auto-links results to planned benchmarks (`workout_id`) ·
modeled/passive anchors (power-duration curve for FTP, e1RM from rep-max sets)
· progress-timeline integration · a measured-vs-modeled coach learning.

---

## 8. Open decisions

- **Anchor kinds beyond FTP/LTHR.** Phase 1 may ship with only cycling FTP
  (+ LTHR) *recorded*, but the machinery is kind-agnostic from day one (§3.5):
  later kinds reuse the identical `benchmark_results` shape, generic prompt
  rendering, and skip-absent-keys drift rule with no code change beyond the
  kind/unit vocabulary.
- **Cadence knob.** Whether the 4–6 week cadence is a config value or fixed to
  "one per mesocycle boundary." Recommend the latter (simpler, matches block
  structure).
- **e1RM auto-capture** eventually blurs the test/normal-session line (any
  rep-max set is a passive test) — deferred to Phase 3, noted here so the schema
  (`source: modeled`) already anticipates it.

---

## 9. Interactions with existing designs

- `DESIGN_block_boundary.md` — end-of-block benchmark is the natural replan
  trigger; adapt's within-block firewall already prevents a test from being
  dragged across a boundary.
- `DESIGN_pmc_fitness_fatigue.md` — TSB gates test-day freshness (the adapt
  prompt rule reads TSB history; benchmarks do not alter PMC math). TSB is
  backward-looking, which is why *moving* a test is the LLM's judgement, not a
  deterministic pass (§4.2).
- `DESIGN_progress_timeline.md` — the anchor time series is a first-class
  progress signal to plot beside CTL.
