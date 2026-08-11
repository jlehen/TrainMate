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
  science guidelines you supply, not from thresholds baked into the app.

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
    magnitude and confirmed by you — never a category you pick blind. (Life events are
    just plan-shaping constraints; the old `lifeevent` command has been removed in
    favor of `constraint`.)
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
- **Time in zone, per sport** — TSS folds volume and intensity into one number,
  so easy days drifting to tempo read as flat weekly load at 100% adherence.
  `tm progress` puts a weekly zone table under the load table for each sport you
  train — what you measured behind today, what the plan prescribes ahead of it.
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
internals work, see the [Architecture Document](ARCHITECTURE.md).

## Getting Started

### Prerequisites

- Python 3.8+ (install dependencies with `pip install -r requirements.txt`)
- [OpenRouter API key](https://openrouter.ai/) for LLM access
- A Garmin Connect account (for daily metrics and activities)
- A Google Service Account with access to your Google Calendar (for workout sync)

### Turn off Garmin's automatic threshold detection

**Do this before you record your first benchmark.** In Garmin Connect, disable
**automatic FTP detection** and **automatic lactate-threshold detection** — they are two
independent settings, and turning off one leaves the other drifting.

TrainMate treats the values you record with `benchmark record` as authoritative. Garmin,
however, buckets each activity into heart-rate and power zones using *its own* threshold
values as they stood at the time. When Garmin auto-detects a new FTP, the Z4/Z5 boundary
moves, and from then on the same effort lands one zone lower. A training block then looks
easier than it was, for no reason visible anywhere in the data.

Nothing can be recomputed after the fact: the bucketing is already done when the activity
arrives and there is no raw stream to re-bucket. So this fixes the future only — every
activity already stored was bucketed under whatever zones were in force then. If an
intensity report shows hard minutes falling sharply for no visible reason, an FTP
auto-bump moving the boundary is a likely explanation. (Manually editing your Garmin zones
has the same effect, and is invisible in the same way.)

This matters more for the sessions ahead of you than for the ones behind. A *measurement*
compares like with like, so a moved boundary shows up as a one-off step; a *prescription*
outlives the moment it was written. With auto-detection left on, two sessions planned
identically six months apart mean different efforts, and neither you nor the coach can
see it.

### Configuration

Edit `config.yaml` to include your specific IDs and profile (use
`config_template.yaml` as a base). You will need:
- `openrouter_api_key`
- `google_calendar_id`
- A valid `service_account.json` file in the root directory.
- `garmin_email` and `garmin_password` (config.yaml is gitignored, keeping
  credentials out of the environment; FTP/LTHR come from `user_profile`).

### Personalizing TrainMate

**Goals.** Don't fill in `goal add` cold. The title, target date, sport,
and priority are much easier to get right once you've actually thought the goal
through — so brainstorm it first with an LLM (ChatGPT, Claude, whatever you
use) through a short interview: what's the event, why does it matter, what's
your current fitness, what constraints (time, injuries, other goals) does the
plan need to respect. Then turn the outcome of that conversation into your
`goal add` call(s).

**The `science/` directory.** Every `.txt` file in the top-level `science/`
directory (gitignored, empty by default) is injected into TrainMate's coaching
prompts alongside the built-in guidelines in `trainmate/science/` — it's how
you teach the coach the training philosophy you actually want it to follow,
rather than a generic one.

The easiest way to build one of these files is to pick articles, YouTube
videos, or podcasts that reflect your preferred approach, pull their text with
[Link2Text](https://github.com/jlehen/Link2Text), and hand the result to an
LLM to synthesize into a single guideline doc. For reference, here are the
sources TrainMate's author used to generate `science/jeremie_science_summary.txt`:

| # | Title | URL |
| - | --- | --- |
| 1 | How to mix Weighlifting with High Intensity Cycling? | https://www.youtube.com/watch?v=ThDnA-Ct2DE |
| 2 | The Simple Framework That Actually Builds FTP | https://www.youtube.com/watch?v=pt-VIQuQGdc |
| 3 | Periodization Training Simplified: A Strategic Guide \| NASM Blog | https://blog.nasm.org/periodization-training-simplified |
| 4 | Dr. Andy Galpin Unveils the 9 Core Principles of Training: Ultimate Human Performance Blueprint | https://www.youtube.com/watch?v=rBlaGSwOXSA |
| 5 | Block Periodization in Action: A Case Study | https://www.trainingpeaks.com/blog/block-periodization-in-action/ |
| 6 | Cycling Power Zones Explained | https://www.trainingpeaks.com/blog/power-training-levels/ |
| 7 | Exploring Types of Periodization | https://www.trainingpeaks.com/blog/exploring-periodization-methods/ |
| 8 | Implementing Block Periodization in Endurance Training | https://www.trainingpeaks.com/blog/implementing-block-periodization/ |
| 9 | Polarized vs. Pyramidal Training — Which is Better For Your Athletes? | https://www.trainingpeaks.com/coach-blog/polarized-pyramidal-training-which-is-better/ |
| 10 | Easy Ways to Customize Your Readymade Endurance Training Plan | https://www.trainingpeaks.com/blog/customize-your-training-plan/ |
| 11 | Number One Mistake Cyclists Make with Weight Training | https://www.youtube.com/watch?v=PsEMv2oOscQ |

### Basic Usage (CLI)

Add a goal:
```bash
python trainmate_cli.py goal add "Marathon Prep" "2026-10-15" running --priority 1
```

Generate a periodization plan and initial workouts:
```bash
python trainmate_cli.py plan generate
python trainmate_cli.py workout generate
```

Pull Garmin data and adapt the plan daily:
```bash
python trainmate_cli.py data pull
python trainmate_cli.py workout adapt
```

See where the plan is going, and how it is actually being executed:
```bash
python trainmate_cli.py progress                  # every sport you train
python trainmate_cli.py progress cycling running  # just these two, in this order
python trainmate_cli.py progress --blocks         # per mesocycle, graded on its focus
```
The load half (CTL/ATL/TSB, the projection, the weekly bars) is always
whole-athlete — naming a sport scopes the zone tables only, because a
running-only CTL is not a quantity. To see one session's recording rather than a
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
python trainmate_cli.py model
python trainmate_cli.py model set 3
```
The choice is stored and survives restarts; `--llm-model <id>` still overrides it for a
single command without storing anything.

See `python trainmate_cli.py --help` for the everyday commands, or
`python trainmate_cli.py help` to see every command and its sub-commands at once.
Rarely-used maintenance commands — `wipe`, `workout push`, `data backfill-tss`,
`data bootstrap` — are kept out of the default listings to reduce clutter;
`python trainmate_cli.py help --all` reveals them.

### Steering the plan: which channel, and how a regen behaves

When life gets in the way, which tool you reach for depends on whether the
change is *strategic* (it should reshape the plan) or *tactical* (it only
affects a run or a few days). The three real-world context channels above map
onto that choice:

| Channel | Reach for it when… |
| --- | --- |
| `constraint add` | You're asking the coach to *work around* something — "no run Thursday", "only 45 min today", a trip, an injury layoff. One object covers every horizon: a blanket `hard` constraint deterministically rests those dates, while a sport-scoped `hard` and every `soft` one stay advisory. If it's big enough to reshape the plan, TrainMate **derives** that from its magnitude and asks to regenerate — or pass `--replan` to say so up front. |
| `context add` (or tagged Calendar events) | You're *reporting* something that happened — alcohol, poor sleep, stress — so a rough morning reads as lifestyle noise, not "the block is too hard." Signals never reshape the plan. |
| `workout adapt --message "…"` | Quick capture in the moment. A durable, constraint-shaped note ("away, no gym Thursday") is saved as a real `constraint` you can inspect and `rm`; a one-off nudge ("felt flat, ease today") is folded into that session's adaptation reason. |

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
- **`workout generate` archives and rebuilds all future workouts**, manual edits
  included (they are recoverable via `workout rollback` or `plan rollback`, not
  deleted; a session you've already completed today is preserved). Because it
  replaces rather than fills in, it asks twice: once before spending the LLM call,
  naming how many sessions are at stake and how many you added by hand, and again once
  it can show you the coach's proposal — listed exactly as `workout list` would show it —
  before anything is written. `-f/--force` skips both questions for unattended runs. So
  make strategic changes *first*
  (a plan-shaping `constraint` → `plan generate` → `workout generate`), then layer
  manual `add`/`swap` tweaks on top — not the other way around.

## Running the Web UI

To start the Flask server locally:
```bash
python trainmate_web.py
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
always supports exactly what the CLI does. Setup:

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
