# TrainMate

TrainMate is a local, AI-powered sports-science coach. You tell it your goals
(races, target dates, sports) and the constraints it must work around (travel, an
injury layoff, a capped-time day); it
designs a periodized training plan, writes your day-to-day workouts, and then
**adapts them every day** in response to how your body is actually responding —
resting heart rate, HRV, sleep, training load, and even lifestyle factors like a
late night or a few drinks. It pulls your data straight from Garmin Connect and
pushes the resulting schedule to Google Calendar, so your plan lives where you
already look.

It runs entirely on your machine against a local SQLite database; the only
external calls are to Garmin, Google Calendar, and an LLM (via OpenRouter) that
does the coaching reasoning.

## What makes it intelligent

- **Periodized planning.** From your goals, constraints, and fitness profile,
  TrainMate builds a full macrocycle → mesocycle → microcycle structure (long-term
  strategy down to individual sessions), over whatever horizon your goal sits on —
  how a short run-in or a multi-season build should be structured comes from the
  science guidelines you supply, not from thresholds baked into the app. A goal's
  date can mean two different things, and the plan respects the difference: an
  **event** date is a day something happens on (a race), so the plan builds to a
  peak and taper for it; a **horizon** date only says how far you want to train
  toward the goal, so the plan still ends around it — but with an ordinary
  training block, no taper pinned to a day nothing happens on.

- **Context-aware daily adaptation.** Each day it weighs your recovery signals
  against the planned session and eases, reschedules, or holds the workout
  accordingly. Crucially, it distinguishes *training fatigue* from *lifestyle
  noise* — a poor morning explained by yesterday's alcohol or bad sleep won't be
  misread as "the block is too hard," so it won't permanently cut your volume on a
  false signal. It also avoids compounding cuts by remembering when a session was
  already eased.

- **Real sports-science load model.** Training load is computed per activity using
  the best available method — power-based TSS (Coggan), heart-rate TSS (Friel), or
  session-RPE (Foster) — and rolls up into the Performance Management Chart
  (CTL/ATL/TSB) plus an ATL:CTL relative-overload ratio for injury-risk and
  readiness assessment. An "RPE divergence" flag surfaces sessions
  that *felt* far harder than they measured (heat, sleep debt, muscular damage).

- **A coach that learns, with evidence.** TrainMate maintains durable "coach
  learnings" about you (e.g. how you respond to back-to-back hard days). Each
  learning's confidence is **computed from cited evidence** — the distinct training
  weeks that support or contradict it — not asserted by the model. Learnings decay
  if unreinforced, downgrades are proposed rather than silently applied, and you
  can inspect or curate every record.

- **Backward evaluation / bootstrap.** TrainMate can reverse-engineer your past
  training from completed activities and metrics, reconstructing the cycles you
  *actually* did and seeding coach learnings — so it starts smart instead of cold,
  and reviews planned-vs-actual when it replans. `data show-analysis` prints that
  reconstruction back to you; `progress` draws its blocks as `~`-prefixed bands.

- **Real-world context: one principled split.** TrainMate separates *observations*
  (things that happened / are true about you) from *directives* (things you ask the
  coach to work around):
  - *Constraints* are **directives** at any horizon — "no run Thursday", "only 45 min
    today", "3-week injury layoff". A single `constraint` object bounds both plan
    generation and daily adaptation. A blanket `hard` constraint (no sport) deterministically
    nulls out training on its dates; a `hard` constraint scoped to a sport, and every `soft`
    one, is advisory — preferences the coach honors by judgement. Whether a constraint
    *reshapes the plan* is **derived** from its
    magnitude and confirmed by you — never a category you pick blind.
  - *Daily context* adds **weighted signals** (alcohol, poor sleep, stress)
    ingested automatically from tagged Google Calendar events. These don't
    reshape the plan; they help the daily adaptation tell lifestyle noise from
    training fatigue, and feed long-term analysis.
  - `workout adapt --message` is a **fast-capture inbox**: a durable, constraint-shaped
    note ("away with no gym Thursday") is classified and saved as a real constraint you
    can inspect and `rm`; a one-off nudge ("felt flat, ease today") stays a single-run
    hint folded into the session's adaptation reason.

- **Plan versioning & rollback.** Regenerating a plan supersedes the old one
  rather than destroying it, so you can roll back a plan (and its workouts) to a
  previous version. Workouts get the same undo on their own axis: every
  regeneration archives the sessions it displaces as a batch, and
  `workout rollback` restores one (`workout batches` lists them) without
  touching the strategy.

## Features at a glance

- **Garmin integration** — pulls daily metrics and completed activities directly
  from Garmin Connect, with a watermark so reads auto-refresh recent data.
- **Google Calendar sync** — schedules and updates planned workouts as calendar
  events, tags them with adherence verdicts after the fact, and ingests tagged
  context events back in.
- **Adherence tracking** — compares planned vs. completed and flags misses,
  load/duration mismatches, and rest-day violations.
- **Time in zone, per sport** — a single load number like TSS blends volume
  and intensity together, so a week where your easy days quietly drifted into
  tempo can still show the planned load and 100% adherence. The weekly
  time-in-zone table `tm progress` prints under the load table catches exactly
  that: for each sport you train, the minutes you actually spent in each zone
  behind today, next to what the plan prescribes ahead of it.
- **Manual overrides** — add, swap, or remove individual workouts by hand;
  adaptation re-balances around them.

## System Architecture

TrainMate consists of three interfaces built on a unified coaching logic
and SQLite database:
1. **Command Line Interface (`trainmate_cli.py`)**: A rich CLI for managing
   goals, generating plans, syncing data, and viewing status.
2. **Web dashboard (`trainmate_web.py`)**: A Flask-served, **read-only** view of
   your training — status, workouts, plan, time in zone, benchmarks, learnings and
   history. It never changes anything; every action lives in the CLI.
3. **Telegram bot (`trainmate_bot.py`)**: A chat front-end that runs the same
   CLI commands from your phone (see [Running the Telegram bot](#running-the-telegram-bot)).

For more deep-dive technical details on how the coaching logic and application
internals work, see the [Architecture Document](docs/ARCHITECTURE.md).

## Getting Started

### Quick start

The whole path, end to end — every step is detailed in the sections below:

1. **Install** — run `./tm` once (it creates `venv/` and stops), then
   `venv/bin/pip install -r requirements.txt`.
2. **Configure** — copy `config_template.yaml` to `config.yaml` and fill in
   your OpenRouter key, Google Calendar + service account, Garmin login, and
   athlete profile ([Configuration](#configuration)).
3. **Pin Garmin's settings** — turn off automatic threshold detection and keep
   the default zones on the right basis
   ([One-time Garmin settings](#one-time-garmin-settings)).
4. **Say what you're training for** —
   `./tm goal add "Marathon Prep" "2026-10-15" running`.
5. **Record your thresholds** — `./tm benchmark record cycling --ftp 220`
   (skippable: with nothing on record the coach prescribes by feel and
   schedules a benchmark test to establish the numbers).
6. **Generate** — `./tm plan generate`, then `./tm workout generate`; the
   workouts land in your Google Calendar.
7. **Live with it** — `./tm data pull && ./tm workout adapt` daily, and
   `./tm status` whenever you want to know where you stand.

### Prerequisites

- Python 3.10+
- [OpenRouter API key](https://openrouter.ai/) for LLM access
- A Garmin Connect account (for daily metrics and activities)
- A Google Service Account with access to your Google Calendar (for workout
  sync — [Configuration](#configuration) walks through creating one)

The `./tm` wrapper is the everyday entry point: it runs the CLI inside the
repo's own virtualenv, creating `venv/` on first run. Install the dependencies
into it once:
```bash
./tm            # first run creates venv/ and stops
venv/bin/pip install -r requirements.txt
```
Every example below uses `./tm`; `python trainmate_cli.py` is the same thing
if you manage your own environment.

### One-time Garmin settings

Garmin buckets every activity into heart-rate and power zones the moment it is
recorded, using whatever thresholds and zone boundaries your profile holds at
the time — and there is no raw stream to re-bucket later. TrainMate reads
those buckets as-is, so pin four settings in Garmin Connect before your first
pull:

1. Disable **automatic FTP detection**.
2. Disable **automatic lactate-threshold detection** (a separate setting —
   turning off one leaves the other on).
3. **Power zones**: keep the default %FTP bands (the Coggan 7-zone model).
   Don't hand-tune the percentages.
4. **Heart-rate zones**: set the basis to **%LTHR** (not %max HR) and keep the
   default bands.

Then one habit: whenever `./tm benchmark record` establishes a new FTP or
LTHR, enter the same value in Garmin Connect. Change the anchor values, never
the percentage bands.

Why it matters: TrainMate treats your benchmark logbook as the truth about
your thresholds, and its zone vocabulary (`trainmate/science/zones.md`) and
load math (`trainmate/garmin/load.py`) assume Garmin's default bands sit on
those anchors. If Garmin silently auto-bumps your FTP, the Z4/Z5 boundary
moves and the same effort starts landing one zone lower — a training block
looks easier than it was, and two sessions prescribed identically six months
apart mean different efforts, with nothing in the data to show it. The %LTHR
basis matters for the same reason: LTHR is a benchmarkable anchor like FTP,
while %max HR is not, so leaving HR zones on it quietly de-anchors your HR
data from the logbook.

### Configuration

Copy `config_template.yaml` to `config.yaml` and fill it in. `config.yaml` is
gitignored, so your credentials stay in the file and out of git and the
environment. (`config.sample.yaml` shows what a working install's config
actually looks like filled in — all values fictional — with the full knob
documentation staying in the template.) The blocks you must fill:

- **`llm:`** — `api_key` (an `OPENROUTER_API_KEY` env var overrides it) and
  `models`, the list of OpenRouter models this install may use; `model set`
  switches between them at runtime, and the first entry is the default.
- **`google:`** — `calendar_id` of the calendar your workouts are written to,
  and `service_account_file`, the service-account JSON used to authenticate
  (`service_account.json` in the repo root by default).
- **`garmin:`** — `email` and `password` for Garmin Connect.
- **`user_profile:`** — the athlete the coach is planning for: `max_hr`,
  `weekly_target_hours`, sport preferences, chronic injuries, free-text
  preferences, and a per-day `weekly_schedule` — how many hours and sessions
  each weekday can hold, how *certain* you are to actually train that day
  (the coach weights planning toward higher-certainty days), and what
  equipment is at hand. The plan is built around this block, so fill it
  honestly rather than optimistically.

If you don't have a Google service account yet, it's a one-time setup:

1. In the [Google Cloud Console](https://console.cloud.google.com/), create
   (or pick) a project and enable the **Google Calendar API** for it.
2. Create a **service account** (no roles needed), add a **JSON key** to it,
   and save the downloaded file as `service_account.json` in the repo root.
3. In Google Calendar, open the settings of the calendar your workouts should
   land in and **share it with the service account's email** (the
   `…@….iam.gserviceaccount.com` address from the JSON) with **"Make changes
   to events"** permission.
4. That calendar's ID — shown under "Integrate calendar" on the same settings
   page — is what goes in `google.calendar_id`.

Everything else in the template is optional tuning and documented inline —
sensible defaults apply when a key is commented out.

Note what is *not* in the config: trainable thresholds (FTP, LTHR, threshold
pace, …) live in the dated benchmark logbook, recorded with
`./tm benchmark record` — that logbook, not `config.yaml`, is what the coach
reads (see [Basic Usage](#basic-usage-cli)).

### Personalizing TrainMate

Beyond the `user_profile:` block above (who you are), the deepest way to
personalize the coach is the top-level `science/` directory: *how you want to
be coached*.

**What it is.** Every `.md` file in `science/` (gitignored, empty by default)
is injected into TrainMate's coaching prompts alongside the built-in guidelines
in `trainmate/science/`. It serves two purposes:

- **Your training philosophy.** The built-ins teach the coach mainstream
  sports science — zones, load math, periodization theory, benchmarking,
  recovery metrics. They deliberately don't pick a methodology. A file here is
  where you say *which* approach the coach should actually plan with: how
  blocks should be structured, what a hard week looks like, how you want to
  taper.
- **Extra reference material.** Domain knowledge the built-ins don't cover —
  say, how strength work should coexist with endurance blocks — that the coach
  should be able to draw on when reasoning about your plan.

**How to build a file.** Pick the articles, YouTube videos, or podcasts that
reflect the approach you want, pull their text with
[Link2Text](https://github.com/jlehen/Link2Text), and hand the result to an
LLM to synthesize into a single guideline doc. That same conversation is a
good place to pressure-test the material — ask the LLM to flag internal
contradictions, and to separate what's *prescriptive* (do this) from what's
merely *explanatory* — before the coach ever sees it.

**Be deliberate about what goes in.** Everything in `science/` rides along on
every coaching call, so this is a place where less is more:

- **Don't overload it.** Every page competes for the coach's attention with
  your metrics, plan history, and learnings. A handful of focused documents
  beats a library.
- **Keep it consistent.** Two documents that quietly disagree — one polarized,
  one sweet-spot; two different taper prescriptions — don't average out. They
  confuse the coach and make its plans less predictable. When you keep
  overlapping documents, say which one wins: the samples below do this with an
  **AUTHORITY** banner at the top of each file, declaring it either
  *prescriptive* (a source of workout parameters) or *reference only*
  (rationale and background, never parameters).
- **Don't restate the built-ins.** Generic periodization or zone theory is
  already in `trainmate/science/` — duplicating it adds bulk without adding
  signal.

**Worked examples.** The `science.sample/` directory contains the files the
author actually trains with — copy the ones you like into `science/` and adapt,
or just imitate their shape. Each names its sources:

- `sustainable_training.md` — the **prescriptive** methodology the coach plans
  workouts from (block structure, intensities, work:rest ratios), lightly
  summarized from Jem Arnold's [Sustainable Training](https://sparecycles.blog/2022/01/02/sustainable-training/).
- `strength_integration.md` — **reference only**: how heavy lifting and
  high-intensity endurance work coexist. Synthesized from
  [Number One Mistake Cyclists Make with Weight Training](https://www.youtube.com/watch?v=PsEMv2oOscQ),
  [How to mix Weightlifting with High Intensity Cycling?](https://www.youtube.com/watch?v=ThDnA-Ct2DE)
  and [Dr. Andy Galpin's 9 Core Principles of Training](https://www.youtube.com/watch?v=rBlaGSwOXSA).
- `plan_customization.md` — **reference only**: adjusting a plan around real
  life (secondary races, travel, missed weeks), from TrainingPeaks'
  [Easy Ways to Customize Your Readymade Endurance Training Plan](https://www.trainingpeaks.com/blog/customize-your-training-plan/).

### Basic Usage (CLI)

Add a goal:
```bash
./tm goal add "Marathon Prep" "2026-10-15" running
```
By default the date is an **event** — race day — and the plan peaks and tapers
for it. If nothing happens on the date itself ("get my FTP to 280 by next
summer"), add `--date-type horizon`: the plan still ends around the date, but
its last block is ordinary training with no taper, and goal-week benchmark
tests aren't suppressed (there's no event for them to compete with).
`goal edit <id> --date-type …` flips an existing goal and flags the plan for
regeneration.

Record your current thresholds — the coach prescribes workout targets from
these (remember to pin the [one-time Garmin settings](#one-time-garmin-settings) first):
```bash
./tm benchmark record cycling --ftp 220
./tm benchmark record running --lthr 165
```
With nothing on record the coach still works: it prescribes by RPE and
heart-rate feel, and schedules a benchmark session to establish the numbers.

Generate a periodization plan and initial workouts:
```bash
./tm plan generate
./tm workout generate
```

Pull Garmin data and adapt the plan daily:
```bash
./tm data pull
./tm workout adapt
```
The first `data pull` looks at how far back your Garmin history reaches and,
when the gap is large, hands you the backfill command to run rather than
fetching months of data unannounced. If you arrive with a real training past,
run `data bootstrap` once after backfilling: it reverse-engineers the training
blocks you actually did and seeds coach learnings from them, so the coach
starts warm instead of cold.

When you just want to know where things stand, one command answers:
```bash
./tm status
```
It shows your current state and recent recovery metrics, your active goals,
and what the coach has learned about you so far.

See where the plan is going, and how it is actually being executed:
```bash
./tm progress                  # every sport you train
./tm progress cycling running  # just these two, in this order
./tm progress --blocks         # per mesocycle, graded on its focus
```
The load half (CTL/ATL/TSB, the projection, the weekly bars) is always
whole-athlete — naming a sport scopes the zone tables only, because fitness
and fatigue accumulate in one body: a running-only CTL isn't a meaningful
number. To see one session's recording rather than a
week's, `data show-activities --zones` gives you per-activity zones and the
coverage that tells you when the strap dropped out.

Your workouts sync to Google Calendar automatically as part of `plan generate`,
`workout generate` (once you accept its proposal), and the daily `workout adapt` —
there's no separate sync step.
(To force a manual re-push after a Calendar mishap, the maintenance command
`workout push` is still there; see below.)

Switch the LLM behind the coach without editing config by hand — `model` lists what
`llm.models` in `config.yaml` offers, numbered, and `model set` picks one:
```bash
./tm model
./tm model set 3
```
The choice is stored and survives restarts; `--llm-model <id>` still overrides it for a
single command without storing anything.

Which model to pick is not a coin flip. On 2026-08-18 the author benchmarked
fifteen OpenRouter models head-to-head — one isolated TrainMate install per
model, same athlete, same two-goal season, same late-breaking constraints.
**`google/gemini-3.1-pro-preview` ranked first**: the only model that reworked
the schedule around both constraints dropped on it after planning.
**`anthropic/claude-opus-5` wrote the best 14-week plan** — the only one derived
from the athlete's per-day equipment calendar, with a reasoned FTP-test
placement — but, like five others, adapted nothing. The full measured
comparison (periodization, load rhythm, constraint compliance, adaptation,
cost) is in
[docs/model_comparison_2026-08.md](docs/model_comparison_2026-08.md).

See `./tm --help` for the everyday commands, or
`./tm help` to see every command and its sub-commands at once.
Rarely-used maintenance commands — `wipe`, `workout push`, `data backfill-tss`,
`data bootstrap` — are kept out of the default listings to reduce clutter;
`./tm help --all` reveals them.

### Steering the plan: which channel, and how a regen behaves

When life gets in the way, which tool you reach for depends on whether the
change is *strategic* (it should reshape the plan) or *tactical* (it only
affects a run or a few days). The real-world context channels above map onto that
choice, with a fourth for the plan itself:

| Channel | Reach for it when… |
| --- | --- |
| `constraint add` | You're asking the coach to *work around* something — "no run Thursday", "only 45 min today", a trip, an injury layoff. One object covers every horizon: a blanket `hard` constraint deterministically rests those dates, while a sport-scoped `hard` and every `soft` one stay advisory. If it's big enough to reshape the plan, TrainMate **derives** that from its magnitude and asks to regenerate — or pass `--replan` to say so up front. |
| `context add` (or tagged Calendar events) | You're *reporting* something that happened — alcohol, poor sleep, stress — so a rough morning reads as lifestyle noise, not "the block is too hard." Signals never reshape the plan. |
| `workout adapt --message "…"` | Quick capture in the moment. A durable, constraint-shaped note ("away, no gym Thursday") is saved as a real `constraint` you can inspect and `rm`; a one-off nudge ("felt flat, ease today") is folded into that session's adaptation reason. |
| `plan feedback "…"` | You have an *opinion about the plan itself* — "drop the second FTP test", "the Friday sessions should progress duration, not surges". Notes pile up against the current plan (nothing is overwritten, nothing calls the LLM, so capture is instant) and the next `plan generate` reads them all and must address each one. `-m` files a note to one block by name, date or ID; `--rm ID` drops one; `--replan` regenerates on the spot. |

Alongside these, **manual overrides** (`workout add`, `rm`, `swap`) let you edit
individual sessions by hand. Daily `adapt` treats a hand-added session as
deliberate intent and re-balances the surrounding days around it — though it can
still ease one if your recovery demands it (only *completed* sessions are locked
history).

Two things to know when you regenerate:

- **A regen is not a cold start.** The new plan is fed your previous strategy, a
  planned-vs-actual review of the blocks you've *already* trained, and your coach
  learnings — so it refines the existing arc rather than redrawing it from scratch.
  When that arc is the thing you want gone, `plan generate --fresh` withholds the
  plan in place, and only that: the review of what you actually trained, your
  learnings and your plan feedback still go in, because a plan drawn blind to your
  training is a template, not a plan.
- **`workout generate` archives and rebuilds the workouts in the span you asked for**,
  manual edits included (they are recoverable via `workout rollback` or `plan rollback`,
  not deleted; a session you've already completed today is preserved). The selectors name
  that span at both ends — `-m 5` is block 5's own days, `-g` is a goal's whole plan, and
  with no flag it is today onward for 28 days — and sessions outside it are left exactly
  as they are. Because it replaces rather than fills in, it asks twice: once before
  spending the LLM call, naming how many sessions are at stake and how many you added by
  hand, and again once it can show you the coach's proposal — listed exactly as
  `workout list` would show it — before anything is written. `-f/--force` skips both
  questions for unattended runs. So make strategic changes *first*
  (a plan-shaping `constraint` → `plan generate` → `workout generate`), then layer
  manual `add`/`swap` tweaks on top — not the other way around.

## Running the Web UI

To start the Flask server locally (the dependencies live in the repo's
virtualenv):
```bash
venv/bin/python trainmate_web.py
```
Then visit `http://127.0.0.1:5000` in your browser.

The dashboard is **read-only** — it shows what is in the database and nothing more.
It never writes a row, pulls from Garmin, calls the LLM or touches your calendar, so
it is safe to leave running and cannot race the CLI or the bot. Six tabs: Dashboard
(status, metrics, objectives, constraints, strategy, active model), Workouts (schedule
+ plan-vs-actual compare), Progress (the timeline chart + per-sport time in zone),
Benchmarks (thresholds + logbook), Learnings (with evidence), and History (activities,
recovery metrics, and a daily-context heat strip).

Anything that changes something is a CLI command, and each panel names the one it
wants — `tm goal add`, `tm plan generate`, `tm workout swap`, `tm learnings demote`,
`tm benchmark record`, `tm context add`, `tm model set`, `tm data pull`.

## Running the Telegram bot

The bot lets you drive TrainMate from your phone using the same commands as the
CLI — the leading `/` Telegram requires is optional:

```
/status
/workout list -d 1w
/workout adapt -m "tired today"
/help workout
```

Each message is run through `trainmate_cli.py` as a subprocess, so the bot
always supports exactly what the CLI does.

Chat replies are shorter than the terminal's. The bot delivers a whole run as
one message, so progress lines ("Auto-syncing Garmin…", "Querying OpenRouter…"),
cache-reuse notes and "you could now run X" hints would arrive after the work
they describe, above the answer — they are suppressed there and kept in the
terminal. Answers, warnings and errors always show. Set `TRAINMATE_VERBOSE=1`
to get them back in chat, or `=0` to silence them in the terminal.

Setup:

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Message your bot once, then find your numeric chat id (e.g. via
   [@userinfobot](https://t.me/userinfobot)).
3. Add a `telegram:` block to `config.yaml` (see `config_template.yaml`):
   ```yaml
   telegram:
     bot_token: "123456789:ABCdef..."   # or set TELEGRAM_BOT_TOKEN
     allowed_chat_ids:
       - 123456789                      # only these chat ids may use the bot
   ```
4. Start the long-polling bot (no public URL needed):
   ```bash
   ./tm-bot
   ```

Only allow-listed chat ids are served. Because the bot can't ask for
confirmation, destructive commands (`wipe`, `rm`) are declined unless you pass
their `-y`/`--yes` flag.

## License

TrainMate is released under the [BSD 3-Clause License](LICENSE).
