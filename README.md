# TrainMate

TrainMate is a local, AI-powered sports-science coach. You tell it your goals
(races, target dates, sports) and the life events that will get in the way; it
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

- **Periodized planning.** From your goals, life events, and fitness profile,
  TrainMate builds a full macrocycle → mesocycle → microcycle structure (long-term
  strategy down to individual sessions). Timelines longer than ~24 weeks are
  automatically broken into intermediate goals.

- **Context-aware daily adaptation.** Each day it weighs your recovery signals
  against the planned session and eases, reschedules, or holds the workout
  accordingly. Crucially, it distinguishes *training fatigue* from *lifestyle
  noise* — a poor morning explained by yesterday's alcohol or bad sleep won't be
  misread as "the block is too hard," so it won't permanently cut your volume on a
  false signal. It also avoids compounding cuts by remembering when a session was
  already eased.

- **Real sports-science load model.** Training load is computed per activity using
  the best available method — power-based TSS (Coggan), heart-rate TSS (Friel), or
  session-RPE (Foster) — and rolls up into acute/chronic workload and ACWR for
  injury-risk and readiness assessment. An "RPE divergence" flag surfaces sessions
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
  and reviews planned-vs-actual when it replans.

- **Lifestyle context ingest.** Tag ordinary Google Calendar events (alcohol,
  poor sleep, stress, travel) and TrainMate folds them into both daily adaptation
  and long-term analysis.

- **Plan versioning & rollback.** Regenerating a plan supersedes the old one
  rather than destroying it, so you can roll back a plan (and its workouts) to a
  previous version.

## Features at a glance

- **Garmin integration** — pulls daily metrics and completed activities directly
  from Garmin Connect, with a watermark so reads auto-refresh recent data.
- **Google Calendar sync** — schedules and updates planned workouts as calendar
  events, tags them with adherence verdicts after the fact, and ingests tagged
  context events back in.
- **Adherence tracking** — compares planned vs. completed and flags misses,
  load/duration mismatches, and rest-day violations.
- **Manual overrides** — add, swap, or remove individual workouts by hand;
  adaptation re-balances around them.

## System Architecture

TrainMate consists of two primary interfaces built on a unified coaching logic
and SQLite database:
1. **Command Line Interface (`trainmate_cli.py`)**: A rich CLI for managing
   goals, generating plans, syncing data, and viewing status.
2. **Web API (`trainmate_web.py`)**: A Flask-based REST API serving a web
   frontend for visual management.

For more deep-dive technical details on how the coaching logic and application
internals work, see the [Architecture Document](ARCHITECTURE.md).

## Getting Started

### Prerequisites

- Python 3.8+ (install dependencies with `pip install -r requirements.txt`)
- [OpenRouter API key](https://openrouter.ai/) for LLM access
- A Garmin Connect account (for daily metrics and activities)
- A Google Service Account with access to your Google Calendar (for workout sync)

### Configuration

Edit `config.yaml` to include your specific IDs and profile (use
`config_template.yaml` as a base). You will need:
- `openrouter_api_key`
- `google_calendar_id`
- A valid `service_account.json` file in the root directory.
- `garmin_email` and `garmin_password` (config.yaml is gitignored, keeping
  credentials out of the environment; FTP/LTHR come from `user_profile`).

### Basic Usage (CLI)

Add a goal:
```bash
python trainmate_cli.py goal add --title "Marathon Prep" --date "2026-10-15" --sport "running" --priority 1
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

Sync to your Google Calendar:
```bash
python trainmate_cli.py workout push
```

See `python trainmate_cli.py --help` for all available commands.

## Running the Web UI

To start the Flask server locally:
```bash
python trainmate_web.py
```
Then visit `http://127.0.0.1:5000` in your browser.
