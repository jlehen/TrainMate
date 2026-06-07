# TrainMate Architecture & System Manifest

This document is the primary reference for coding agents. Read it before reading source files —
in most cases it will be sufficient. Read a source file only when you need to change it or when
a specific detail is not covered here.

---

## 1. System Overview

TrainMate is a local AI sports-science coaching application. The user configures goals and life
events; TrainMate generates periodized training plans (macrocycle → mesocycles) and workout
schedules (microcycles), then adapts them daily based on Garmin metrics. Plans and workouts can
be pushed to Google Calendar.

```
  +--------------------------------------------------+
  |               User Interface Layer               |
  |  trainmate_cli.py   trainmate_web.py (Flask)     |
  +---------------------------+----------------------+
                              |
  +---------------------------v----------------------+
  |               Coaching Logic Layer               |
  |  trainmate/coach.py  ─  CoachService             |
  |    │ orchestrates DB + calendar + LLM calls       |
  |  trainmate/coach.py  ─  CoachEngine              |
  |    │ pure logic: prompt building, hash, LLM calls │
  |  trainmate/openrouter.py  (OpenRouter LLM client) |
  |  trainmate/adherence.py   (plan vs actual diff)   |
  +---------------------------+----------------------+
                              |
  +---------------------------v----------------------+
  |              Data & Integration Layer            |
  |  trainmate/db.py            (SQLite CRUD)        |
  |  trainmate/google_sheets.py (Garmin metrics pull)|
  |  trainmate/google_calendar.py (Calendar sync)    |
  +--------------------------------------------------+
```

---

## 2. Module Map

Every module exposes one **singleton** at module level (see section 6 for the list). UIs and
tests import the singleton directly — never instantiate the classes themselves.

### Entry Points

| File                 | Purpose                                                              |
|----------------------|----------------------------------------------------------------------|
| `trainmate_cli.py`   | argparse CLI; dispatches to handler functions `run_*()`. No          |
|                      | business logic.                                                      |
| `trainmate_web.py`   | Flask REST API; thin handler functions calling `db`,                 |
|                      | `coach_service`, `sheets_reader`, `calendar_syncer`.                 |

### Package `trainmate/`

| File                 | Class / Singleton    | Purpose                                          |
|----------------------|----------------------|--------------------------------------------------|
| `types.py`           | —                    | TypedDicts: `Objective`, `LifeEvent`, `Workout`, |
|                      |                      | `CompletedActivity` (includes `bike_avg_watts`,  |
|                      |                      | `zone1_sec`–`zone5_sec`), `AthleteMetric`,       |
|                      |                      | `AthleteBaseline`, `Macrocycle`, `Mesocycle`     |
| `config.py`          | `config`             | Reads `config.yaml`; exposes typed properties.   |
| `db.py`              | `db`                 | SQLite wrapper; full CRUD for all tables.        |
| `coach.py`           | `coach_service`      | `CoachService` orchestrator + `CoachEngine`      |
|                      |                      | pure logic.                                      |
| `openrouter.py`      | `openrouter_client`  | HTTP client for OpenRouter; always expects       |
|                      |                      | `json_object` response.                          |
| `google_sheets.py`   | `sheets_reader`      | Reads Google Sheets; saves metrics + completed   |
|                      |                      | activities to DB.                                |
| `google_calendar.py` | `calendar_syncer`    | Creates/updates/deletes all-day Google Calendar  |
|                      |                      | events for workouts.                             |
| `adherence.py`       | —                    | `analyze_adherence()` pure function; compares    |
|                      |                      | planned vs completed.                            |
| `util.py`            | —                    | ANSI color helpers (`bold`, `green`, `red`, …),  |
|                      |                      | `wrap_text`, `format_labeled_text`.              |

---

## 3. coach.py Architecture

`coach.py` contains two classes and one module-level function:

### `_load_science_guidelines(app_science_dir, science_dir) → str`
Module-level function. Concatenates all `*.txt` files from `trainmate/science/` (built-in) and
`science/` (user-provided). Called by `CoachService._load_science_guidelines()`.

### `CoachEngine`
**Pure business logic — no DB or I/O.** All methods are prefixed `_` (called by `CoachService`
or directly by tests).

| Method                               | What it does                                        |
|--------------------------------------|-----------------------------------------------------|
| `_build_system_prompt(...)`          | Assembles the main LLM system prompt with           |
|                                      | guidelines, strategy, goals, life events, athlete   |
|                                      | profile.                                            |
| `_format_athlete_profile(profile)`   | Formats `config.user_profile` dict into a readable  |
|                                      | prompt segment.                                     |
| `_get_goals_hash(objectives)`        | SHA-256 of sorted objectives list.                  |
| `_get_lifeevents_hash(lifeevents)`   | SHA-256 of sorted life events list.                 |
| `_get_config_hash()`                 | SHA-256 of `user_profile` + `metrics_history_days`. |
| `_generate_macrocycle_strategy(...)` | LLM call → `{strategy, mesocycles}`. Label:         |
|                                      | `periodization_plan`.                               |
| `_generate_workouts_logic(...)`      | LLM call → `{reasoning, athlete_learnings,          |
|                                      | workouts[]}`. Accepts `num_days` (default 28) which |
|                                      | drives the horizon in the prompt. Label:            |
|                                      | `workout_generation`.                               |
| `_adapt_logic(...)`                  | LLM call → `{change_needed, reason,                 |
|                                      | adapted_workouts[]}`. Label: `workout_adaptation`.  |
| `_generate_intermediate_goals(...)`  | LLM call → `{goals[]}` when timeline > 24 weeks.    |
|                                      | Label: `generate_intermediate_goals`.               |

### `CoachService`
**Orchestrator — owns all DB and calendar access.** Exposes the public API called by the UIs.

| Method                                              | What it does                                                        |
|-----------------------------------------------------|---------------------------------------------------------------------|
| `generate_periodization_plan(force, objective_id)`  | Fetches objectives/lifeevents, checks hashes, calls                 |
|                                                     | `CoachEngine._generate_macrocycle_strategy()`, saves to DB.         |
|                                                     | Auto-splits timelines > 24 weeks.                                   |
| `generate_workouts(objective_id, end_date)`         | Requires an existing macrocycle. Computes `num_days` from           |
|                                                     | `end_date` (or `config.workout_generate_days` if omitted).          |
|                                                     | Fetches history, calls `CoachEngine._generate_workouts_logic()`,    |
|                                                     | saves workouts to DB.                                               |
| `replan(force, objective_id)`                       | Convenience: calls `generate_periodization_plan` then               |
|                                                     | `generate_workouts`.                                                |
| `adapt(target_date_str)`                            | Fetches metrics + workouts in rolling window, calls                 |
|                                                     | `CoachEngine._adapt_logic()`. Returns                               |
|                                                     | `(reason, proposed_workouts)`.                                      |
| `apply_adaptations(proposed, reason, start, end)`   | Deletes overridden workouts (+ calendar events), saves adapted      |
|                                                     | workouts, syncs to Calendar.                                        |
| `delete_plan(objective_id)`                         | Deletes macrocycle + mesocycles for that objective (cascades in     |
|                                                     | DB).                                                                |
| `_get_config_hash()`                                | Delegates to `CoachEngine._get_config_hash()`. Used by CLI/web to   |
|                                                     | detect stale plans.                                                 |
| `_get_coach_system_prompt(objectives, lifeevents, ...)`| Builds system prompt without making an LLM call (used by         |
|                                                     | tests).                                                             |

**Singleton:** `coach_service = CoachService()` at the bottom of `coach.py`. Import as:
```python
from trainmate.coach import coach_service
```

---

## 4. Database — Key Patterns

**File:** `trainmate/db.py` · **Singleton:** `db = Database()`

- Every method opens a fresh `sqlite3` connection (context manager), commits, and closes.
- `foreign_keys = ON` is set on every connection; cascades are used on macrocycles→mesocycles.
- `save_workout(date, sport_type, ...)` is an **upsert**: it looks up by `(date, sport_type)`
  and updates if found, inserts otherwise. The `google_event_id` is preserved unless explicitly
  passed.
- `clear_future_workouts(from_date)` deletes future workouts with `status != 'synced'`.

### Key methods by domain

**Objectives:** `add_objective`, `get_objectives(status=)`, `get_objective(id)`,
`update_objective(id, **kwargs)`, `delete_objective`, `wipe_objectives`

**Life Events:** `add_lifeevent`, `get_lifeevents(start_after=)`, `get_lifeevent(id)`,
`update_lifeevent(id, **kwargs)`, `delete_lifeevent`, `wipe_lifeevents`

**Workouts:** `save_workout` (upsert), `get_workout(date, sport_type)`,
`get_workouts(start_date, end_date, sport_type)`,
`get_workout_by_id(id)`, `delete_workout_by_id`, `clear_future_workouts`, `wipe_workouts`

**Completed Activities:** `save_completed_activity` (upsert on `activity_id`),
`get_completed_activities(start_date, end_date)`

**Metrics & Baselines:** `save_metric_cache` (upsert), `get_metrics_cache(start_date, end_date)`,
`save_baseline`, `get_baseline(date)` (returns closest prior baseline), `wipe_metrics`

**Coach Memory:** `save_coach_memory(key, value)` (upsert), `get_coach_memory(key)`.
Key in use: `athlete_learnings` (free-text observations). Periodization strategy lives in
the `macrocycles` table, not here.

**Macrocycles/Mesocycles:** `save_macrocycle` (deletes existing for objective, then inserts),
`get_macrocycle_for_objective(objective_id)`, `get_last_macrocycle()`,
`get_mesocycles_for_macrocycle(macrocycle_id)`, `get_mesocycle(id)`,
`update_macrocycle_feedback(id, feedback)`, `update_mesocycle_feedback(id, feedback)`,
`update_macrocycle_config_hash(id, hash)`, `delete_macrocycle_for_objective`,
`wipe_plans`

---

## 5. Database Schema

SQLite database at `trainmate.db` (path from `config.db_path`).

### objectives
| Column        | Type       | Notes                                                |
|---------------|------------|------------------------------------------------------|
| `id`          | INTEGER PK |                                                      |
| `title`       | TEXT       |                                                      |
| `target_date` | TEXT       | YYYY-MM-DD                                           |
| `sport_type`  | TEXT       | Single or comma-separated (e.g. `running,road_biking`) |
| `status`      | TEXT       | `active`, `completed`, `archived`                    |
| `priority`    | INTEGER    | 1 = highest                                          |
| `description` | TEXT       |                                                      |

### lifeevents
| Column               | Type       | Notes                                          |
|----------------------|------------|------------------------------------------------|
| `id`                 | INTEGER PK |                                                |
| `title`              | TEXT       |                                                |
| `start_date`         | TEXT       | YYYY-MM-DD                                     |
| `end_date`           | TEXT       | YYYY-MM-DD                                     |
| `event_type`         | TEXT       | `business_trip`, `vacation`, `party`, `other`  |
| `impact_description` | TEXT       |                                                |

### workouts
| Column                 | Type       | Notes                                            |
|------------------------|------------|--------------------------------------------------|
| `id`                   | INTEGER PK |                                                  |
| `date`                 | TEXT       | YYYY-MM-DD                                       |
| `sport_type`           | TEXT       |                                                  |
| `title`                | TEXT       |                                                  |
| `description`          | TEXT       | Current description (may be adapted)             |
| `original_description` | TEXT       | Set once on creation, never overwritten          |
|                        |            | (COALESCE)                                       |
| `status`               | TEXT       | `planned`, `modified`, `synced`                  |
| `modification_reason`  | TEXT       |                                                  |
| `google_event_id`      | TEXT       |                                                  |
| `duration_minutes`     | INTEGER    |                                                  |
| `rpe`                  | INTEGER    | Expected RPE 1–10                                |
| `tss`                  | INTEGER    | Expected Training Stress Score                   |

### completed\_activities
| Column              | Type    | Notes                                              |
|---------------------|---------|----------------------------------------------------|
| `activity_id`       | TEXT PK | Garmin activity ID                                 |
| `date`              | TEXT    | YYYY-MM-DD                                         |
| `start_time`        | TEXT    |                                                    |
| `activity_name`     | TEXT    |                                                    |
| `activity_type`     | TEXT    | Garmin type string (e.g. `running`, `road_biking`) |
| `duration_sec`      | REAL    |                                                    |
| `distance_km`       | REAL    |                                                    |
| `elevation_gain_m`  | REAL    |                                                    |
| `avg_hr`            | INTEGER |                                                    |
| `max_hr`            | INTEGER |                                                    |
| `rpe`               | INTEGER | From sheet; estimated if missing                   |
| `tss`               | REAL    | From sheet; estimated if missing                   |
| `bike_avg_watts`    | INTEGER | From sheet; NULL for non-bike activities           |
| `zone1_sec`         | INTEGER | Time in HR/power zone 1 (seconds); NULL if missing |
| `zone2_sec`         | INTEGER | Time in HR/power zone 2 (seconds); NULL if missing |
| `zone3_sec`         | INTEGER | Time in HR/power zone 3 (seconds); NULL if missing |
| `zone4_sec`         | INTEGER | Time in HR/power zone 4 (seconds); NULL if missing |
| `zone5_sec`         | INTEGER | Time in HR/power zone 5 (seconds); NULL if missing |

### athlete_\metrics_\cache
| Column            | Type    | Notes                  |
|-------------------|---------|------------------------|
| `date`            | TEXT PK | YYYY-MM-DD             |
| `rhr`             | INTEGER | Resting heart rate     |
| `hrv`             | INTEGER | Overnight HRV average  |
| `sleep_score`     | INTEGER | 0–100                  |
| `stress`          | INTEGER |                        |
| `acute_workload`  | REAL    | 7-day rolling sum      |
| `chronic_workload`| REAL    | 28-day sum ÷ 4         |
| `acwr`            | REAL    | acute / chronic        |

### athlete_\baselines
28-day rolling baseline computed during `sheets_reader.sync_data()`.

| Column                      | Type    |
|-----------------------------|---------|
| `date`                      | TEXT PK |
| `rhr_baseline_mean`         | REAL    |
| `rhr_baseline_std`          | REAL    |
| `hrv_baseline_mean`         | REAL    |
| `hrv_baseline_std`          | REAL    |
| `sleep_baseline_mean`       | REAL    |
| `sleep_baseline_std`        | REAL    |

### coach_\memory
Key-value store for LLM-updated coach notes.

| Column       | Type    | Notes                                          |
|--------------|---------|------------------------------------------------|
| `key`        | TEXT PK | `athlete_learnings`                            |
| `value`      | TEXT    | LLM-generated text blob                        |
| `updated_at` | TEXT    | ISO timestamp                                  |

### macrocycles
| Column            | Type                  | Notes                                            |
|-------------------|-----------------------|--------------------------------------------------|
| `id`              | INTEGER PK            |                                                  |
| `objective_id`    | INTEGER FK→objectives | Cascade delete                                   |
| `strategy`        | TEXT                  | LLM-generated strategy text                      |
| `goals_hash`      | TEXT                  | SHA-256 of objectives at generation time         |
| `lifeevents_hash` | TEXT                  | SHA-256 of life events at generation time        |
| `config_hash`     | TEXT                  | SHA-256 of `user_profile` +                      |
|                   |                       | `metrics_history_days`                           |
| `created_at`      | TEXT                  | ISO timestamp                                    |
| `feedback`        | TEXT                  | Athlete feedback for next replanning             |

### mesocycles
| Column          | Type                    | Notes                                   |
|-----------------|-------------------------|-----------------------------------------|
| `id`            | INTEGER PK              |                                         |
| `macrocycle_id` | INTEGER FK→macrocycles  | Cascade delete                          |
| `name`          | TEXT                    | E.g. "Base Building"                    |
| `start_date`    | TEXT                    | YYYY-MM-DD                              |
| `end_date`      | TEXT                    | YYYY-MM-DD                              |
| `focus`         | TEXT                    | E.g. "Zone 2 aerobic base, high volume" |
| `feedback`      | TEXT                    | Athlete feedback for next replanning    |

---

## 6. Singletons

All modules export a singleton at the bottom. Import these, never instantiate the classes:

```python
from trainmate.config import config          # Config
from trainmate.db import db                  # Database
from trainmate.coach import coach_service    # CoachService
from trainmate.openrouter import openrouter_client  # OpenRouterClient
from trainmate.google_sheets import sheets_reader   # GarminSheetsReader
from trainmate.google_calendar import calendar_syncer  # CalendarSyncer
```

For tests, the DB singleton can be overridden by patching the module-level `db` variable in
affected modules (see `tests/test_adaptation.py` for the pattern: assign `test_db` to
`trainmate.coach.db`, `trainmate.google_sheets.db`, etc. before importing the singletons).

---

## 7. CLI Commands Reference

Invoked as `python trainmate_cli.py <command> [subcommand] [args]`.
Handler functions are named `run_<command>_<subcommand>()` in `trainmate_cli.py`.

| Command      | Subcommand   | Alias    | Description                                                              |
|--------------|--------------|----------|--------------------------------------------------------------------------|
| `status`     | —            | `s`      | Show active goals, recent metrics, coach memory                          |
| `goal`       | `add`        | `g a`    | Add objective (`--title`, `--date`, `--sport`, `--desc`, `--priority`)   |
| `goal`       | `edit`       | `g e`    | Edit objective by ID                                                     |
| `goal`       | `rm`         | `g r`    | Remove objective by ID                                                   |
| `goal`       | `list`       | `g l`    | List all objectives                                                      |
| `goal`       | `wipe`       | —        | Delete all objectives                                                    |
| `lifeevent`  | `add`        | `le a`   | Add life event (`--title`, `--start`, `--end`, `--type`, `--desc`)       |
| `lifeevent`  | `edit`       | `le e`   | Edit life event by ID                                                    |
| `lifeevent`  | `rm`         | `le r`   | Remove life event by ID                                                  |
| `lifeevent`  | `list`       | `le l`   | List life events                                                         |
| `lifeevent`  | `show`       | `le s`   | Show life event details by ID                                            |
| `lifeevent`  | `wipe`       | —        | Delete all life events                                                   |
| `plan`       | `generate`   | `p g`    | Generate/reuse macrocycle+mesocycles (`-f` to force, `--goal ID`)        |
| `plan`       | `show`       | `p s`    | Show active periodization plan                                           |
| `plan`       | `rm`         | `p d`    | Delete plan for a goal ID                                                |
| `plan`       | `feedback`   | `p f`    | Add feedback (`--macro` or `--meso ID`, `--goal ID`, text)               |
| `plan`       | `wipe`       | —        | Delete all plans                                                         |
| `workout`    | `list`       | `w l`    | Show planned workouts (`--type TYPE`, `--days N`,        |
|              |              |          | `--weeks N`, `--from DATE`, `--until DATE`,              |
|              |              |          | `--from-mesocycle`, `--until-mesocycle [ID]`,            |
|              |              |          | `--mesocycle [ID]`, `--goal [ID]`)                       |
| `workout`    | `compare`    | `w c`    | Compare planned workouts vs completed activities.        |
|              |              |          | Calls `analyze_adherence()` and prints PLANNED/ACTUAL    |
|              |              |          | per day; flags missed sessions (red), rest violations    |
|              |              |          | (red), unplanned high-load activities (yellow), then     |
|              |              |          | shows a discrepancy summary. Date range flags same as    |
|              |              |          | `workout list` (`--type`, `--days`, `--weeks`,           |
|              |              |          | `--from`, `--until`, `--from-mesocycle`,                 |
|              |              |          | `--until-mesocycle [ID]`, `--mesocycle [ID]`,            |
|              |              |          | `--goal [ID]`). Default: 14-day lookback. `--days`/      |
|              |              |          | `--weeks` look *back* (not forward). End date is always  |
|              |              |          | capped at today.                                         |
| `workout`    | `generate`   | `w g`    | Generate workouts from active strategy (`--goal ID`,                     |
|              |              |          | `--days N`, `--weeks N`, `--until DATE`,                                 |
|              |              |          | `--until-goal [ID]`, `--until-mesocycle ID`)                             |
| `workout`    | `rm`         | `w r`    | Remove workout by ID                                                     |
| `workout`    | `adapt`      | `w a`    | Run daily adaptation check (`--date YYYY-MM-DD`, `-y` auto-apply)        |
| `workout`    | `push`       | `w p`    | Sync planned workouts to Google Calendar                                 |
| `workout`    | `wipe`       | —        | Delete all workouts                                                      |
| `data`       | `pull`       | `d pull` | Fetch Garmin metrics and activities from Google Sheets                  |
| `data`       | `analyze`    | `d a`    | Analyze completed workouts/metrics to detect cycles                      |
|              |              |          | (`--from`, `--until`, `--days`, `--weeks`, `--context`)                  |
| `data`       | `wipe`       | —        | Delete all metrics, baselines, completed activities                      |

---

## 8. Web API Endpoints

Flask server at `trainmate_web.py`, runs on port 5000. Static files served from `static/`.

| Method      | Path                            | Description                                  |
|-------------|---------------------------------|----------------------------------------------|
| GET         | `/api/status`                   | Active goal, latest metrics, coach memory,   |
|             |                                 | macrocycle+mesocycles                        |
| GET/POST    | `/api/objectives`               | List all / create objective                  |
| DELETE/PUT  | `/api/objectives/<id>`          | Delete or update objective                   |
| GET/POST    | `/api/life-events`              | List upcoming / create life event            |
| DELETE/PUT  | `/api/life-events/<id>`         | Delete or update life event                  |
| GET         | `/api/workouts`                 | List workouts (`?start_date=&end_date=`)     |
| POST        | `/api/plan`                     | Generate periodization plan (`{goal_id?}`)   |
| DELETE      | `/api/plan/<goal_id>`           | Delete plan for goal                         |
| POST        | `/api/macrocycles/<id>/feedback`| Save macrocycle feedback (`{feedback}`)      |
| POST        | `/api/mesocycles/<id>/feedback` | Save mesocycle feedback (`{feedback}`)       |
| POST        | `/api/workouts/generate`        | Generate workouts (`{goal_id?}`)             |
| POST        | `/api/adapt`                    | Run daily adaptation (`{date?}`)             |
| POST        | `/api/workouts/push`            | Sync workouts to Google Calendar             |
| POST        | `/api/metrics/pull`             | Pull Garmin metrics from Google Sheets       |
| GET         | `/api/metrics`                  | Last 30 days of cached metrics               |


---

## 9. Configuration (`config.yaml`)

Required fields:

| Key                    | Type | Description                                                   |
|------------------------|------|---------------------------------------------------------------|
| `openrouter_api_key`   | str  | Also readable from `OPENROUTER_API_KEY` env var               |
| `openrouter_model`     | str  | Default: `google/gemini-3.5-flash`                            |
| `google_sheet_id`      | str  | Spreadsheet ID for Garmin metrics                             |
| `google_calendar_id`   | str  | Target calendar ID                                            |
| `service_account_file` | str  | Path to service account JSON (default:                        |
|                        |      | `service_account.json`)                                       |
| `metrics_history_days`  | int  | Rolling window for adaptation (default: 15)                  |
| `workout_generate_days` | int  | Default horizon for `workout generate` (default: 28)         |
| `low_load_threshold`    | float| Workload score below which an activity is "minor"            |
|                         |      | (default: 25). Controls two behaviors: (1) matched-activity  |
|                         |      | mismatch tolerance widens to 50% instead of 30%, and (2)     |
|                         |      | unplanned activities are shown as `(minor)` (gray) rather    |
|                         |      | than `UNPLANNED` (yellow) in `workout compare`.              |
| `user_profile`          | dict | Must contain `lthr` or `ftp` (see below)                     |

`user_profile` keys: `name`, `birth_year`, `max_hr`, `lthr`, `ftp`, `weekly_target_hours`,
`sport_preferences`, `chronic_injuries`, `preferences`, `equipment`, `weekly_schedule`.
`weekly_schedule` maps day names to `{total_available_hours, max_sessions, certainty_percent, equipment}`.

---

## 10. Key Data Flows

### Plan Generation (`plan generate`)
1. `CoachService.generate_periodization_plan()` fetches active objectives + life events.
2. Computes `goals_hash`, `lifeevents_hash`, `config_hash`.
3. If existing macrocycle has matching hashes and `force=False` → reuse.
4. Otherwise: calls `CoachEngine._generate_macrocycle_strategy()` → LLM → `{strategy, mesocycles}`.
5. If timeline > 24 weeks: calls `CoachEngine._generate_intermediate_goals()` first, saves
   intermediate objectives, then re-runs with the first goal.
6. Saves new macrocycle + mesocycles to DB (old ones deleted via `save_macrocycle`).

### Workout Generation (`workout generate`)
1. CLI resolves the generation horizon (end date) from flags in priority order:
   `--days` / `--weeks` → `--until DATE` → `--until-goal [ID]` → `--until-mesocycle ID` →
   `config.workout_generate_days` (default 28).
2. `CoachService.generate_workouts(end_date=...)` verifies a macrocycle exists, computes
   `num_days` from `(end_date − today)`.
3. Fetches metrics history (last `metrics_history_days` days) + baseline.
4. Calls `CoachEngine._generate_workouts_logic(num_days=...)` → LLM →
   `{reasoning, athlete_learnings, workouts[]}`.
5. Saves `athlete_learnings` to `coach_memory`.
6. Clears future unsynced workouts (`clear_future_workouts`), then saves new workouts.

### Daily Adaptation (`workout adapt`)
1. `CoachService.adapt()` fetches metrics + planned workouts + completed activities in window.
2. `analyze_adherence()` (`adherence.py`) computes discrepancies (misses, duration/load mismatches,
   rest violations).
3. Finds active mesocycle for the target date → sets `meso_end_date` for adaptation range.
4. Calls `CoachEngine._adapt_logic()` → LLM → `{change_needed, reason, adapted_workouts[]}`.
5. Returns `(reason, proposed_workouts)` — caller decides whether to apply.
6. If applied: `apply_adaptations()` deletes overridden calendar events + DB rows, saves adapted
   workouts with `status='modified'`, syncs to Calendar.

### Data Pull (`data pull`)
1. `GarminSheetsReader.sync_data()` fetches "Daily Metrics" and "Activities" sheets.
2. For each day: saves `athlete_metrics_cache` with ACWR computed over 7/28-day windows.
3. Computes 28-day rolling baseline (RHR, HRV, sleep mean/std) and saves to `athlete_baselines`.
4. RPE and TSS are read from the sheet; if missing, estimated from `avg_hr` / `lthr`.
5. `bike_avg_watts` and `zone1_sec`–`zone5_sec` are read from the sheet and stored as-is
   (NULL when absent). These are included verbatim in the LLM-facing activity formatter
   (`format_completed_activities` in `coach.py`).

### Data Analysis (`data analyze`)
1. `CoachService.analyze_workouts()` determines target start/end dates automatically based on
   active and preceding goals if date range parameters are omitted.
2. Queries the database for completed activities and physiological metrics for that date range.
3. Groups metrics and activities week-by-week using Monday-commencing ISO weeks.
4. Queries `CoachEngine._analyze_workouts_logic()` -> LLM -> `{macrocycle_summary,
   inferred_macrocycle, inferred_mesocycles[], physiological_insights[],
   learnings_for_coach_memory}`.
5. Updates the coach's memory with any newly discovered `athlete_learnings`.

---

## 11. Terminology: Plans vs. Workouts

- **Plan** = periodization strategy: one macrocycle (per objective) + mesocycle blocks.
  Commands: `plan generate/show/rm/feedback`.
- **Workouts** = daily microcycle activities implementing the mesocycle focus.
  Commands: `workout generate/adapt/push`.

Plan must be generated before workouts. Workouts cover a rolling window from today whose length
is controlled by the horizon flags on `workout generate` (default: `workout_generate_days` in
`config.yaml`, falling back to 28 days). `replan()` calls `generate_periodization_plan` then
`generate_workouts` in one step (always uses the config default).

---

## 12. Sports Science & Coaching Mathematics

### Workload per activity
```
Workload = TSS + RPE × (duration_sec / 3600)
```

### Acute Workload (7 days)
Sum of daily workloads over the past 7 days (current day included).

### Chronic Workload (28 days)
Sum of workloads over past 28 days ÷ 4 (≈ average weekly load).

### ACWR
```
ACWR = Acute / Chronic
```
- < 0.8: under-training
- 0.8–1.3: "sweet spot"
- > 1.5: elevated injury risk

### Daily Readiness Signals
- HRV drops > 1 std below baseline mean → flag potential overtraining
- RHR rises > 1 std above baseline mean (min +3 bpm) → flag potential overtraining
- `workout adapt` acts on these signals over the rolling `metrics_history_days` window.

### Science Guidelines Files
- `trainmate/science/` — built-in: `acwr.txt`, `periodization.txt`, `recovery_metrics.txt`
- `science/` — user-provided; empty by default; any `.txt` files added here are injected into
  every LLM prompt.

---

## 13. Testing

Tests use `unittest`. Run with:
```
venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

| File                           | What it tests                                                   |
|--------------------------------|-----------------------------------------------------------------|
| `tests/test_adaptation.py`     | `CoachService.adapt()` end-to-end                               |
| `tests/test_cli.py`            | CLI command dispatch + output                                   |
| `tests/test_feedback.py`       | Feedback saving + use in replanning                             |
| `tests/test_periodization.py`  | `generate_periodization_plan`, `generate_workouts`, hash logic  |
| `tests/test_sheets.py`         | Google Sheets sync logic                                        |
| `tests/test_utils.py`          | `adherence.py` + `util.py` helpers                              |

Tests inject a fresh in-memory SQLite DB by assigning `test_db` to module-level `db` variables
*before* importing the singletons. `openrouter_client` is mocked via `@patch`.

Integration / manual test scripts (not part of the test suite):
- `tests/run_integration.py`, `tests/run_sheets.py`, `tests/run_calendar.py`
