# TrainMate

**A local AI coach that plans your season, writes your workouts, and adjusts
them every morning from your Garmin data.**

You tell TrainMate what you are training for and what life is doing to your
week. It builds a periodized plan, turns it into day-by-day sessions, puts them
in your Google Calendar, and then re-reads your recovery every morning to ease,
move or hold the day's workout. It runs on your own machine against a local
SQLite database. The only outside calls are to Garmin Connect, Google Calendar,
and an LLM through OpenRouter.

The coaching philosophy is yours as well. The coach plans from Markdown
guidelines you put in a `science/` directory, and `science.sample/` ships the
files the author trains with, so you start from a real methodology and change
what you disagree with.

```
$ ./tm status
=== TRAINMATE ATHLETE STATUS ===

Next Goal: Hillcrest hill-climb (CYCLING)
Target Date: 2027-06-13 Sun (34 days remaining)

Active Mesocycle: Hill Power (2027-05-10 Mon to 2027-05-23 Sun)
Cycle Focus:
  Two loading weeks turning the base block's aerobic work into sustained
  climbing power. Two quality sessions a week, never on consecutive days, each
  preceded by an easy or rest day; the longest ride stays easy ...

Fitness Thresholds:
- Functional Threshold Power (FTP): 262 W (cycling, tested 2027-05-08 Sat, 2d ago)
- Max Heart Rate: 178 bpm (config)
```

Three ways in: a command line for the operator, a read-only web dashboard, and
a Telegram bot that either mirrors the CLI or, in **companion mode**, talks like
a coach so an athlete who never wants to learn a command can still use it.

## Contents

- [Who it is for](#who-it-is-for)
- [Quick start](#quick-start)
- [A week with TrainMate](#a-week-with-trainmate)
- [Features](#features)
- [The three interfaces](#the-three-interfaces)
- [Setup in detail](#setup-in-detail)
- [The science directory: your coaching philosophy](#the-science-directory-your-coaching-philosophy)
- [Steering the plan](#steering-the-plan)
- [Choosing a model](#choosing-a-model)
- [Going deeper](#going-deeper)
- [Development](#development)
- [License](#license)

## Who it is for

Self-coached endurance athletes who wear a Garmin, keep their week in Google
Calendar, and are comfortable running a command from a terminal. Through
companion mode, also the people they set it up for: a partner or a friend who
wants a coach on their phone, not a command language.

You need:

- Python 3.10 or newer.
- An [OpenRouter](https://openrouter.ai/) API key. TrainMate uses OpenRouter so
  you can pick the model. See [Choosing a model](#choosing-a-model).
- A Garmin Connect account. TrainMate reads daily metrics and activities
  directly from it.
- A Google service account with access to one Google Calendar. The
  [setup section](#google-calendar-and-the-service-account) walks through it.

What it costs to run: the coach is called when you generate a plan, when you
generate workouts, and once a day when you adapt. Each of those is one large
call with tens of thousands of tokens of context. Nothing else spends money.
`./tm journal --cost` rolls up your own spend by model and by command, so
after a week you will know your number.

## Quick start

1. **Install.** The `./tm` wrapper runs the CLI inside the repo's own
   virtualenv and creates it on first run:
   ```bash
   ./tm                                   # first run creates venv/ and stops
   venv/bin/pip install -r requirements.txt
   ```
2. **Configure.** Copy `config_template.yaml` to `config.yaml` and fill in the
   four credential blocks: OpenRouter, Google Calendar, Garmin, and your
   athlete profile. Details in [Configuration](#configuration).
3. **Pin Garmin's settings.** Turn off automatic threshold detection and keep
   the default zones on the right basis, once, in Garmin Connect. Why this
   matters is in [One-time Garmin settings](#one-time-garmin-settings).
4. **Pick a coaching philosophy.** Copy the guideline files you like from
   `science.sample/` into `science/`, or write your own. Skippable to start,
   since the built-in sports science works alone, but this is where the coach
   learns your methodology
   ([The science directory](#the-science-directory-your-coaching-philosophy)).
5. **Say what you are training for.**
   ```bash
   ./tm goal add "Hillcrest hill-climb" 2027-06-13 cycling
   ```
6. **Record your thresholds.** Skippable: with nothing on record the coach
   prescribes by feel and schedules a test.
   ```bash
   ./tm benchmark record cycling --ftp 250
   ```
7. **Generate.** A plan first, then the sessions. The workouts land in your
   calendar.
   ```bash
   ./tm plan generate
   ./tm workout generate
   ```
8. **Live with it.**
   ```bash
   ./tm data pull && ./tm workout adapt    # every morning
   ./tm status                             # whenever you wonder where you stand
   ```

Any unambiguous prefix works as a command: `./tm wo li` is `workout list`.
`./tm help` prints every command and sub-command on one page, and `./tm shell`
opens an interactive prompt if you prefer not to retype `./tm`.

## A week with TrainMate

Here is what the daily rhythm looks like once a plan is in place. The athlete
and the numbers are fictional, and the transcripts are abridged, but the
shape of every output is the real one.

**Monday morning.** You pull last night's data and let the coach look at it:

```
$ ./tm data pull && ./tm workout adapt
Auto-syncing Garmin…
Querying OpenRouter… this usually takes about 25s.

Decision Summary:
HRV and resting HR sit on baseline and yesterday's ride stayed easy as
prescribed. Today's strength session stands.

All metrics are green and workout plan is on track. No changes recommended.
```

**Wednesday.** You slept badly and had two glasses of wine. You say so, in
your own words, and the coach eases the day without rewriting the block:

```
$ ./tm workout adapt -m "late night, a couple of drinks, feel flat"
Querying OpenRouter… this usually takes about 25s.

Decision Summary:
This morning's low HRV is explained by the late night and the alcohol, so it
reads as lifestyle noise rather than block fatigue. The intervals move to
Thursday and today becomes an easy spin. The block's load is untouched.

PROPOSED WORKOUT ADAPTATIONS:
Date        Sport          Original Workout      Proposed Workout
2027-05-12  CYCLING        Hill Repeats 5x4 min  Easy Spin
2027-05-13  REST->CYCLING  Rest Day              Hill Repeats 5x4 min
Apply these adaptations to your training plan and sync to Calendar? [y/N] y
```

The distinction matters. A rough morning that has an explanation does not get
read as "the block is too hard", so your volume is not cut on a false signal.

**Thursday.** Work drops a trip on you. That is a directive, not a report, so
it becomes a constraint the coach has to work around:

```
$ ./tm constraint add "away, no bike" --start 2027-05-16 --end 2027-05-18
ID: 7 | away, no bike: 2027-05-16 Sun to 2027-05-18 Tue | advisory · not yet in the plan
Constraint added successfully.
```

An advisory constraint is honored by the coach's judgement. Add `--rest` for a
hard no-training window, and TrainMate rests those dates without asking the
model. If a constraint is big enough to reshape the plan, TrainMate derives
that from its size and asks whether to regenerate.

**Saturday.** You look at how the week went and what is coming:

```
$ ./tm workout list -d -7d..
=== WORKOUT SCHEDULE ===
ID: 118 | 2027-05-10 Mon | STRENGTH_TRAINING | Strength: Lower Body [DONE] [SYNCED] | 60min | TSS 28 | RPE 6
ID: 121 | 2027-05-11 Tue | YOGA | Mobility Session [DONE] [ADAPTED] [SYNCED] | 30min | TSS 4 | RPE 1
ID: 124 | 2027-05-12 Wed | CYCLING | Easy Spin [DONE] [ADAPTED] [SYNCED] | 45min | TSS 28 | RPE 3
ID: 125 | 2027-05-13 Thu | CYCLING | Hill Repeats 5x4 min [PARTIAL] [ADAPTED] [SYNCED] | 75min | TSS 84 | RPE 8
ID: 127 | 2027-05-14 Fri | REST | Rest Day [REST OK] [SYNCED] | TSS 0 | RPE 0
ID: 130 | 2027-05-15 Sat | CYCLING | Long Easy Ride [NOT YET] [SYNCED] | 150min | TSS 95 | RPE 4
ID: 133 | 2027-05-16 Sun | REST | Rest Day [SYNCED] | TSS 0 | RPE 0
```

Every session behind you carries a verdict: `[DONE]`, `[PARTIAL]`, `[MISSED]`,
`[REST OK]` or `[REST BROKEN]`. Today's session reads `[NOT YET]` because the
day is not over. The same verdict is stamped on the Calendar event.

**Sunday.** You check the bigger picture:

```
$ ./tm progress
FORM today (planned) CTL 41.2 ATL 47.9 TSB -6.7
CTL 8w ▃▄▅▅▆▆▇█   plan end 06-12: CTL 45 TSB +3

WEEKLY LOAD plan  ▓done ▒plan  done  adh
── Base & Threshold ──
w/c 04-26    300  ▓▓▓▓▓▓▓▓░│░░  284  95%
w/c 05-03    310  ▓▓▓▓▓▓▓▓▓│░░  336 108%
── Hill Power ──
w/c 05-10    320  ▓▓▓▓▓▓▓▓│░░░  298  93%
w/c 05-17    340  ▒▒▒▒▒▒▒▒▒▒░░
── Sharpening & Attempt Window ──
w/c 05-24    290  ▒▒▒▒▒▒▒▒░░░░
```

The top line is the classic fitness, fatigue and form model (CTL, ATL and TSB,
explained under [Features](#features)). Below it, each training block is a band,
each week a bar: planned load against what you actually did, and how close the
two came. A time-in-zone table per sport follows, so a week where your easy
days quietly drifted into tempo is visible even when the load number says
everything went to plan.

**The same week on a phone**, if the bot runs in companion mode. The morning
message arrives by itself:

```
🚴 Today: Hill Repeats 5x4 min — 75 min

[ 👍 Got it ]  [ 😴 Feeling tired ]  [ 🕐 Can't today ]
```

Tapping "Feeling tired" runs the same adaptation as the Wednesday command
above. Typing "no bike Sunday to Tuesday, I'm travelling" saves the same
constraint as the Thursday command, after asking you to confirm. "My week"
shows:

```
🗓 Coming up:
Wed 12 · ✅ 🚴 Easy Spin — 45 min
Thu 13 · 🚴 Hill Repeats 5x4 min — 75 min
Fri 14 · 🛌 Rest day
Sat 15 · 🚴 Long Easy Ride — 150 min

1 of 4 sessions already done — keep it rolling 💪
```

## Features

**Planning**

- **Your philosophy, not the app's.** The built-in guidelines teach mainstream
  sports science but pick no methodology. Markdown files you drop into
  `science/` say how you want to be coached, and `science.sample/` ships three
  worked examples to copy. See
  [The science directory](#the-science-directory-your-coaching-philosophy).
- **Periodized plans.** From your goals, constraints and profile, TrainMate
  builds a macrocycle (the whole arc to your goal), its mesocycles (blocks of a
  few weeks with one focus each) and the microcycles (your actual weeks). How a
  short run-in or a multi-season build should be structured comes from the
  science guidelines you supply, not from thresholds baked into the app.
- **Event dates and horizon dates.** A race is an event: the plan peaks and
  tapers for it. "Get my FTP to 280 by next summer" is a horizon: the plan ends
  around the date with an ordinary block, and no taper pinned to a day nothing
  happens on. `goal add --date-type horizon` says which.
- **Plan feedback.** `plan feedback "drop the second FTP test"` files a note
  against the plan. Notes pile up, cost nothing to capture, and the next
  `plan generate` has to address every one.
- **Versioning and rollback.** Regenerating supersedes the old plan instead of
  destroying it. `plan versions` lists them, `plan diff` compares two, and
  `plan rollback` restores one with its workouts. Workouts have the same undo on
  their own axis: `workout batches` and `workout rollback`.
- **Staleness tracking.** When your profile, goals or thresholds change after a
  plan was generated, `plan show` flags what changed and what to do about it.
  `plan keep` clears the flag when the change would not have altered the plan.

**Daily coaching**

- **Recovery-aware adaptation.** Each morning the coach weighs resting heart
  rate, HRV, sleep and training load against the planned session, and eases,
  moves or holds it. It remembers when a session was already eased, so cuts do
  not compound.
- **Lifestyle noise versus training fatigue.** A poor morning explained by
  alcohol, a late night or stress is treated as noise, not as evidence the
  block is too hard. Signals come from tagged Google Calendar events or
  `signal add`.
- **A fast-capture inbox.** `workout adapt -m "away with no gym Thursday"` is
  classified and saved as a real constraint you can list and remove. "Felt
  flat, ease today" stays a one-off hint for today's session.
- **Manual overrides.** `workout add`, `swap`, `rm` and `restore` edit single
  sessions by hand. Adaptation treats a hand-added session as deliberate and
  rebalances the days around it.
- **A coach that learns, with evidence.** TrainMate keeps durable learnings
  about you, such as how you respond to back-to-back hard days. Each learning's
  confidence is computed from the training weeks that support or contradict it,
  not asserted by the model. Learnings decay if unreinforced, downgrades are
  proposed rather than applied, and `learnings show`, `edit`, `demote` and
  `keep` let you curate every record. `data reflect` updates them from what
  happened since the last time.

**Measurement**

- **A real load model.** Training load per activity uses the best method
  available for that activity: power-based TSS (Coggan), heart-rate TSS (Friel)
  or session-RPE (Foster). Loads roll up into the Performance Management Chart:
  CTL (fitness, a 42-day average), ATL (fatigue, a 7-day average) and TSB
  (form, their difference), plus an ATL:CTL ratio for overload risk. An RPE
  divergence flag marks sessions that felt far harder than they measured.
- **Time in zone, per sport.** Because a single load number blends volume and
  intensity, `progress` also prints the minutes you spent in each zone, per
  sport, next to what the plan prescribed.
- **Adherence tracking.** Planned versus completed, with misses, load and
  duration mismatches, and rest-day violations, graded once and shown in the
  CLI, the dashboard and the Calendar event alike.
- **Backward evaluation.** `data bootstrap` reverse-engineers the blocks you
  actually trained from your Garmin history and seeds coach learnings, so a new
  install starts warm. `data show-analysis` prints that reconstruction back.
- **A benchmark logbook.** Thresholds (FTP, LTHR, threshold pace, and so on) are
  dated records, not config values. `benchmark record` adds one; the coach
  reads the latest.

**Integration and safety**

- **Garmin Connect.** Daily metrics and completed activities, pulled directly,
  with a watermark so reads refresh recent data on their own.
- **Google Calendar.** Workouts are written as events, updated when adapted,
  tagged with their verdict afterwards, and tagged signal events are read back
  in. There is no separate sync step.
- **Reversible by default.** `goal rm` calls a goal off but keeps its plan
  history; `--purge` is the irreversible cascade. Regenerated workouts are
  archived, not deleted. The Telegram bot declines destructive commands unless
  you pass `-y`.
- **A run journal.** `./tm journal` shows what the app did and when: each
  command, how long it took, what it called and how it ended. `--cost` rolls up
  model calls; `--failed` shows only the runs that broke.
- **Runway warnings.** When the generated schedule is about to run out, every
  daily surface says so and names the command that extends it.

## The three interfaces

All three sit on the same coaching logic and the same database.

**The CLI** (`./tm`) is where everything that changes state lives. `./tm --help`
lists the everyday commands; `./tm help --all` also shows the rarely-used
maintenance ones such as `wipe`, `workout push` and `data backfill-tss`.

**The web dashboard** (`venv/bin/python trainmate_web.py`, then
`http://127.0.0.1:5000`) is read-only. It never writes a row, pulls from Garmin,
calls the LLM or touches your calendar, so it is safe to leave running beside
the CLI and the bot. Six tabs: Dashboard, Workouts, Progress, Benchmarks,
Learnings and History. Each panel names the CLI command that would change it.

**The Telegram bot** (`./tm-bot`) runs every message through the CLI as a
subprocess, so it supports exactly what the CLI does. It has two personae,
chosen per install with `telegram.ui:` in the config:

- **Expert mode** (the default) is a terminal in a chat window. Every message is
  a command line, with or without the leading slash: `/status`,
  `workout list -d 1w`, `/help workout`. Replies keep their column alignment.
- **Companion mode** (`telegram.ui: simple`) is for an athlete who does not want
  a command language. A persistent keyboard covers the daily surface: Today, My
  week, Done lately, Goals, My plan, Progress, and Talk to me. Free text goes
  through a small intent router on a cheap model: "what's on today" shows the
  day, "I'm wrecked" goes to the coach, "no running until Friday" is saved as a
  constraint after a confirmation. The bot opens each day with a morning message
  and three buttons, and replies read as short prose rather than tables.
  Typed commands still work. `/ui` flips the mode until the next restart.

Setup for either mode:

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Message your bot once, then find your numeric chat id, for example via
   [@userinfobot](https://t.me/userinfobot).
3. Add a `telegram:` block to `config.yaml`:
   ```yaml
   telegram:
     bot_token: "123456789:ABCdef..."   # or set TELEGRAM_BOT_TOKEN
     allowed_chat_ids:
       - 123456789                      # only these chat ids may use the bot
     ui: simple                         # omit for expert mode
   ```
   `config_template_full.yaml` documents the companion knobs: the morning push
   time and deadline, whether the push runs the daily adaptation first, and
   `operator_name`, which is how the bot refers to the person who set it up.
4. Start the long-polling bot. No public URL is needed:
   ```bash
   ./tm-bot
   ```

Only allow-listed chat ids are served. Because the bot cannot ask for
confirmation, destructive commands such as `wipe` and `rm` are declined unless
you pass their `-y` flag. A command that calls the coach first sends you what it
is working from, then tells you how long the wait usually is, then the answer.
The estimate is the median of that command's recent runs on your own machine.

## Setup in detail

### Configuration

Copy `config_template.yaml` to `config.yaml` and fill it in. The file is
gitignored, so your credentials stay out of git. Three files describe the same
schema at three lengths:

| File | What it is |
| --- | --- |
| `config_template.yaml` | The short template: only what you must fill in. Start here. |
| `config_template_full.yaml` | Every knob the app reads, commented out at its default, with the reasoning. Copy a block over when you want to change one. |
| `config.sample.yaml` | A realistic filled-in config to imitate. All values fictional. |

The blocks you must fill:

- **`llm:`** has `api_key` (an `OPENROUTER_API_KEY` environment variable
  overrides it) and `models`, the list of OpenRouter models this install may
  use. The first entry is the default coach; `settings set coach-model`
  switches at runtime. `router_model` names the cheap model companion mode
  routes chat with.
- **`google:`** has `calendar_id`, the calendar your workouts are written to, and
  `service_account_file`, the JSON key used to authenticate.
- **`garmin:`** has your Garmin Connect `email` and `password`.
- **`user_profile:`** describes the athlete: `gender`, `max_hr`,
  `weekly_target_hours`, sport preferences, chronic injuries, free-text
  preferences, and an optional per-day `weekly_schedule` with hours, session
  count, how certain you are to train that day, and what equipment is at hand.
  The plan is built around this block, so fill it honestly rather than
  optimistically. Omit `weekly_schedule` and the coach places sessions on any
  day, sized by `weekly_target_hours`.

Trainable thresholds do not go in the config. FTP, LTHR and threshold pace live
in the dated benchmark logbook, recorded with `./tm benchmark record`, and that
logbook is what the coach reads.

Everything you might want to change without editing the file by hand lives
behind one command. `./tm settings` lists each preference, its value, and where
it came from. Values are stored in the database and survive restarts, and the
same command works from Telegram, which is the point: the phone has no editor.

```bash
./tm settings                             # the whole list
./tm settings set coach-model 2           # switch the LLM behind the coach
./tm settings set timezone Europe/Paris   # what "today" means
./tm settings set morning-time 07:00      # when the bot opens your day
./tm settings reset morning-time          # back to what config.yaml says
```

Set the timezone if the machine runs on UTC, otherwise the training day rolls
over at the wrong hour. You do not need the exact name: `settings set timezone
paris` lists the matching zones and lets you pick.

### Google Calendar and the service account

If you do not have a Google service account yet, it is a one-time setup:

1. In the [Google Cloud Console](https://console.cloud.google.com/), create or
   pick a project and enable the **Google Calendar API** for it.
2. Create a **service account** (no roles needed), add a **JSON key** to it, and
   save the downloaded file as `service_account.json` in the repo root.
3. In Google Calendar, open the settings of the calendar your workouts should
   land in and **share it with the service account's email** (the
   `…@….iam.gserviceaccount.com` address from the JSON) with **"Make changes to
   events"** permission.
4. That calendar's ID, shown under "Integrate calendar" on the same page, goes
   in `google.calendar_id`.

### One-time Garmin settings

Garmin buckets every activity into heart-rate and power zones the moment it is
recorded, using whatever thresholds your profile holds at the time. There is no
raw stream to re-bucket later, and TrainMate reads those buckets as they are.
So pin four settings in Garmin Connect before your first pull:

1. Disable **automatic FTP detection**.
2. Disable **automatic lactate-threshold detection**. It is a separate setting.
3. **Power zones**: keep the default %FTP bands (the Coggan 7-zone model).
4. **Heart-rate zones**: set the basis to **%LTHR**, not %max HR, and keep the
   default bands.

Then one habit: whenever `./tm benchmark record` establishes a new FTP or LTHR,
enter the same value in Garmin Connect. Change the anchor values, never the
percentage bands.

Why: TrainMate treats your benchmark logbook as the truth about your
thresholds, and its zone vocabulary and load math assume Garmin's default bands
sit on those anchors. If Garmin silently bumps your FTP, the same effort starts
landing one zone lower, and two sessions prescribed identically six months
apart mean different efforts with nothing in the data to show it.

### First pull and bootstrap

The first `data pull` looks at how far back your Garmin history reaches and,
when the gap is large, hands you the backfill command rather than fetching
months of data unannounced. If you arrive with a real training past, run
`data bootstrap` once after backfilling. It reverse-engineers the blocks you
actually trained and seeds coach learnings from them, so the first plan is
written against your history rather than a blank page.

### A second athlete on the same checkout

Every relative path in `config.yaml` resolves against the directory holding the
config file, or against `data_dir:` when that key is set. So a second athlete
gets a complete, separate instance by putting their `config.yaml` in a directory
of its own and pointing every command at it:

```bash
TRAINMATE_CONFIG=/path/to/other/config.yaml ./tm status
TRAINMATE_CONFIG=/path/to/other/config.yaml ./tm-bot
```

Their database, logs, science guidelines and Garmin token store all land beside
that file. Keep the Garmin token store per instance: a store that loads is used
as-is, so two instances sharing one would both pull whichever account logged in
last. This is also how one person runs the CLI for themselves and a companion
mode bot for someone else: two configs, two bots, no routing code.

## The science directory: your coaching philosophy

`user_profile:` says who you are. The `science/` directory says how you want to
be coached, and it is the deepest lever you have on the plans the coach writes.

The quickest start is `science.sample/`: the guideline files the author trains
with, described at the end of this section. Copy the ones you like into
`science/` and edit them, or imitate their shape for your own.

Every `.md` file in `science/` (gitignored, empty by default) is injected into
the coaching prompts alongside the built-in guidelines in `trainmate/science/`.
The built-ins teach mainstream sports science: zones, load math, periodization
theory, benchmarking, recovery metrics. They deliberately do not pick a
methodology. A file in `science/` is where you say which approach the coach
should plan with: how blocks are structured, what a hard week looks like, how
you taper. It is also the place for domain knowledge the built-ins lack, such
as how strength work should coexist with endurance blocks.

**How to build a file.** Pick the articles, videos or podcasts that reflect the
approach you want, pull their text with
[Link2Text](https://github.com/jlehen/Link2Text), and hand the result to an LLM
to synthesize into one guideline document. Ask it to flag internal
contradictions and to separate what is prescriptive (do this) from what is
merely explanatory, before the coach ever sees it.

**Less is more.** Everything in `science/` rides along on every coaching call.

- A handful of focused documents beats a library. Every page competes with your
  metrics, plan history and learnings for the coach's attention.
- Two documents that quietly disagree do not average out. When you keep
  overlapping ones, say which wins. The samples do this with an **AUTHORITY**
  banner at the top of each file: *prescriptive* (a source of workout
  parameters) or *reference only* (background, never parameters).
- Do not restate the built-ins. Generic zone or periodization theory is already
  there.

**Worked examples.** `science.sample/` holds the files the author actually
trains with. Copy the ones you like into `science/` and adapt them.

- `sustainable_training.md` is the **prescriptive** methodology the coach plans
  from, summarized from Jem Arnold's
  [Sustainable Training](https://sparecycles.blog/2022/01/02/sustainable-training/).
- `strength_integration.md` is **reference only**: how heavy lifting and
  high-intensity endurance work coexist, synthesized from
  [Number One Mistake Cyclists Make with Weight Training](https://www.youtube.com/watch?v=PsEMv2oOscQ),
  [How to mix Weightlifting with High Intensity Cycling?](https://www.youtube.com/watch?v=ThDnA-Ct2DE)
  and [Dr. Andy Galpin's 9 Core Principles of Training](https://www.youtube.com/watch?v=rBlaGSwOXSA).
- `plan_customization.md` is **reference only**: adjusting a plan around real
  life, from TrainingPeaks'
  [Easy Ways to Customize Your Readymade Endurance Training Plan](https://www.trainingpeaks.com/blog/customize-your-training-plan/).

## Steering the plan

When life gets in the way, the tool you reach for depends on whether the change
is strategic (it should reshape the plan) or tactical (it affects a day or a
few). TrainMate keeps two kinds of record apart: **observations** are things
that happened to you, **directives** are things you ask the coach to work
around.

| Channel | Reach for it when |
| --- | --- |
| `constraint add` | You want the coach to *work around* something: "no run Thursday", "only 45 min today", a trip, an injury layoff. `--rest` makes it a hard no-training window that rests those dates deterministically; without it the constraint is advisory and the coach honors it by judgement. Whether it is big enough to reshape the plan is derived from its size and confirmed by you, or forced with `--replan`. |
| `signal add`, or a tagged Calendar event | You are *reporting* something: alcohol, poor sleep, stress, heat. Signals help the morning adaptation read a rough day correctly. They never reshape the plan. |
| `workout adapt -m "…"` | Quick capture in the moment. A durable note ("away, no gym Thursday") is saved as a real constraint; a one-off nudge ("felt flat, ease today") is folded into today's adaptation. |
| `plan feedback "…"` | You have an opinion about the plan itself: "drop the second FTP test", "Friday sessions should progress duration, not surges". The next `plan generate` must address each note. `-m` files a note to one block, `--replan` regenerates on the spot. |

Alongside these, `workout add`, `swap`, `rm` and `restore` edit single sessions
by hand. Adaptation treats a hand-added session as deliberate intent and
rebalances around it, though it can still ease one if your recovery demands it.
Only completed sessions are locked history.

Two things to know when you regenerate:

- **A regen is not a cold start.** The new plan is fed your previous strategy, a
  planned-versus-actual review of the blocks you already trained, and your coach
  learnings, so it refines the arc rather than redrawing it. When the arc is the
  thing you want gone, `plan generate --fresh` withholds only the plan in place.
  `--show-llm-context` shows exactly what the coach was shown.
- **`workout generate` replaces the span you name.** Manual edits included,
  though they are recoverable with `workout rollback`. With no flag the span is
  today onward for 28 days; `-m 5` is block 5's own days; `-g` is a goal's whole
  plan. Because it replaces rather than fills in, it asks twice: once before
  spending the LLM call, naming how many sessions are at stake, and again with
  the coach's proposal in front of you. `-f` skips both for unattended runs. So
  make strategic changes first (constraint, `plan generate`, `workout
  generate`), then layer manual tweaks on top, not the other way around.

## Choosing a model

TrainMate uses two model roles, both picked from the `llm.models` list in your
config.

- **The coach** writes plans, generates workouts and runs the daily adaptation.
  This is where quality matters. **The recommendation is the most recent Claude
  Opus** (`anthropic/claude-opus-5` at the time of writing). In the author's
  2026-08-18 benchmark of fifteen OpenRouter models on the same two-goal
  season, it wrote the best 14-week plan, and the only one derived from the
  athlete's per-day equipment calendar. Be aware of the benchmark's other
  finding: `google/gemini-3.1-pro-preview` ranked first overall because it was
  the only model that reworked the schedule around both constraints dropped on
  it after planning, while Opus, like five others, adapted nothing in that
  scenario. The full measured comparison, with wall times, is in
  [docs/model_comparison_2026-08.md](docs/model_comparison_2026-08.md).
- **The router** only classifies free-text chat messages in companion mode into
  one of a dozen intents. A cheap, fast model is plenty. **The recommendation is
  Gemini Flash** (`google/gemini-3.5-flash`), set with `llm.router_model` or
  `settings set router-model`. Unset, the coach model routes too, which works
  but wastes money and seconds on every message.

```yaml
llm:
  models:
    - model: "anthropic/claude-opus-5"      # first entry is the default coach
    - model: "google/gemini-3.5-flash"
  router_model: "google/gemini-3.5-flash"
```

`./tm settings list coach-model` prints the numbered menu and marks which entry
holds which role. `--llm-model <id>` overrides the coach for a single command
without storing anything.

## Going deeper

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the coaching logic
  and the application fit together. Read it before changing code.
- [docs/DOMAIN_MODEL.md](docs/DOMAIN_MODEL.md) defines every record the app
  keeps: goals, plans, blocks, workouts, constraints, signals, learnings.
- [docs/model_comparison_2026-08.md](docs/model_comparison_2026-08.md) is the
  full fifteen-model benchmark.
- `designs/` holds one design document per feature, with the reasoning behind
  each decision. Code comments point at the relevant section rather than
  repeating it.

## Development

Tests run from the repo's virtualenv with the standard library's runner:

```bash
venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

The suite installs two guards before anything else is imported: no test may
open a network socket, and none may open your real training database. A
`config.yaml` still has to exist in the checkout for the modules to load, so
in a fresh clone copy the template first.

`AGENTS.md` records the project's conventions for humans and coding agents
alike: plain language, update the architecture document with the code, prefer
exact values in the database and round only on display, and one-off migrations
rather than backward-compatibility layers, since each install serves one
athlete.

## License

TrainMate is released under the [BSD 3-Clause License](LICENSE).
