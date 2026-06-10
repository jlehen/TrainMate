# TrainMate

TrainMate is a local AI sports-science coaching application.  The user
configures goals and life events; TrainMate generates periodized training plans
(macrocycle → mesocycles) and workout schedules (microcycles), then adapts them
daily based on Garmin metrics. Plans and workouts can be pushed to Google
Calendar.

## Features

- **AI-Powered Periodization**: Generates long-term macrocycles and mesocycles
  based on user goals, events, and fitness profile.
- **Daily Adaptation**: Adjusts workouts daily using resting heart rate, HRV,
  and sleep metrics.
- **Garmin Integration**: Syncs daily metrics and completed activities from
  Garmin (via Google Sheets).
- **Calendar Sync**: Automatically schedules and updates planned workouts in
  Google Calendar.
- **Coach Learnings**: The system learns from your performance and adaptations
  over time to provide better personalized schedules.

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

- Python 3.8+
- [OpenRouter API key](https://openrouter.ai/) for LLM access
- A Google Service Account with access to a Google Sheet (for Garmin data) and
  Google Calendar

### Configuration

Edit `config.yaml` to include your specific IDs and profile (use
`config_template.yaml` as a base). You will need:
- `openrouter_api_key`
- `google_sheet_id`
- `google_calendar_id`
- A valid `service_account.json` file in the root directory.

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
