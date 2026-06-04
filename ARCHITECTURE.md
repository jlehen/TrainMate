# TrainMate Architecture & System Manifest

This document outlines the codebase design, module responsibilities, database schema,
and sports science calculations of TrainMate. It is intended to help developers and AI agents
quickly build a mental model of the system.

---

## 1. System Overview & Module Map

TrainMate is a local AI sports science coaching application that coordinates training objectives,
life constraints, and Garmin metrics.

```
       +---------------------------------------------+
       |             User Interface Layer            |
       |  - trainmate_cli.py (Command Line Utility)  |
       |  - trainmate_web.py (Flask Dashboard App)   |
       +----------------------+-+--------------------+
                              | |
       +----------------------v v--------------------+
       |              Coaching Logic Layer           |
       |  - trainmate/coach.py (Orchestrates Plans)  |
       |  - trainmate/openrouter.py (LLM Connection) |
       +----------------------+-+--------------------+
                              | |
       +----------------------v v--------------------+
       |              Data & Integration Layer       |
       |  - trainmate/db.py (SQLite DB Operations)   |
       |  - trainmate/google_sheets.py (Garmin pull) |
       |  - trainmate/google_calendar.py (Calendar)  |
       +---------------------------------------------+
```

### Module Responsibilities

*   [trainmate_cli.py](file:///home/jlh/src/TrainMate/trainmate_cli.py): Console CLI interface. Dispatches commands to create goals, sync metrics, trigger plans, or run check-ins.
*   [trainmate_web.py](file:///home/jlh/src/TrainMate/trainmate_web.py): Flask local server providing static assets and a REST API endpoint layer for the web UI.
*   [trainmate/types.py](file:///home/jlh/src/TrainMate/trainmate/types.py): Central type repository holding standard TypedDicts for goals, workouts, metrics, and cycles.
*   [trainmate/config.py](file:///home/jlh/src/TrainMate/trainmate/config.py): Config singleton parsing settings from `config.yaml` and system environment variables.
*   [trainmate/db.py](file:///home/jlh/src/TrainMate/trainmate/db.py): SQLite wrapper containing table creation and CRUD operations for domain objects.
*   [trainmate/openrouter.py](file:///home/jlh/src/TrainMate/trainmate/openrouter.py): OpenRouter client using Gemini for planning and structured JSON response schemas.
*   [trainmate/google_sheets.py](file:///home/jlh/src/TrainMate/trainmate/google_sheets.py): Syncs metrics and training workload activities from Google Sheets.
*   [trainmate/google_calendar.py](file:///home/jlh/src/TrainMate/trainmate/google_calendar.py): Syncs planned and adapted workouts as all-day events on Google Calendar.

---

## 2. Database Schema

The SQLite database (`trainmate.db`) consists of the following tables:

### objectives
Stores target athlete objectives.
*   `id` (INTEGER PRIMARY KEY)
*   `title` (TEXT)
*   `target_date` (TEXT - YYYY-MM-DD)
*   `sport_type` (TEXT - running, road_biking, etc.)
*   `description` (TEXT)
*   `priority` (INTEGER)
*   `status` (TEXT - active, completed, archived)

### constraints
Stores life event dates which limit training availability.
*   `id` (INTEGER PRIMARY KEY)
*   `title` (TEXT)
*   `start_date` (TEXT - YYYY-MM-DD)
*   `end_date` (TEXT - YYYY-MM-DD)
*   `event_type` (TEXT - injury, vacation, party, other)
*   `impact_description` (TEXT)

### workouts
Planned or adapted training workouts.
*   `id` (INTEGER PRIMARY KEY)
*   `date` (TEXT - YYYY-MM-DD)
*   `sport_type` (TEXT)
*   `title` (TEXT)
*   `description` (TEXT)
*   `original_description` (TEXT)
*   `status` (TEXT - planned, modified, synced)
*   `modification_reason` (TEXT)
*   `google_event_id` (TEXT)

### athlete_metrics_cache
Garmin metrics synchronized from sheets.
*   `date` (TEXT PRIMARY KEY)
*   `rhr` (INTEGER - resting heart rate)
*   `hrv` (INTEGER - overnight HRV average)
*   `sleep_score` (INTEGER - 0-100)
*   `stress` (INTEGER - stress average)
*   `acute_workload` (REAL)
*   `chronic_workload` (REAL)
*   `acwr` (REAL - acute:chronic workload ratio)

### athlete_baselines
Baselines computed from the past 28 days of metrics.
*   `date` (TEXT PRIMARY KEY)
*   `rhr_baseline_mean` / `rhr_baseline_std` (REAL)
*   `hrv_baseline_mean` / `hrv_baseline_std` (REAL)
*   `sleep_baseline_mean` / `sleep_baseline_std` (REAL)

### macrocycles
Active macro training cycles tied to target objectives.
*   `id` (INTEGER PRIMARY KEY)
*   `objective_id` (INTEGER, FOREIGN KEY to objectives.id)
*   `strategy` (TEXT - overall coaching strategy)
*   `goals_hash` / `constraints_hash` (TEXT - status tracking)
*   `created_at` (TEXT)

### mesocycles
Specific block training phases within a macrocycle.
*   `id` (INTEGER PRIMARY KEY)
*   `macrocycle_id` (INTEGER, FOREIGN KEY to macrocycles.id)
*   `name` (TEXT)
*   `start_date` (TEXT)
*   `end_date` (TEXT)
*   `focus` (TEXT)

---

## 3. Sports Science Math

### Workload Calculation
Workload is computed per activity in [google_sheets.py](file:///home/jlh/src/TrainMate/trainmate/google_sheets.py#L91-L102):
$$\text{Workload} = \frac{\text{Duration in Seconds}}{3600} \times \text{Average Heart Rate}$$

### Acute Workload (7 Days)
Sum of training workload from the current day and the previous 6 days.

### Chronic Workload (28 Days)
Sum of training workload over the past 28 days divided by 4 (to represent average weekly load).

### Acute:Chronic Workload Ratio (ACWR)
$$\text{ACWR} = \frac{\text{Acute Workload}}{\text{Chronic Workload}}$$
*   ACWR < 0.8: Under-training.
*   ACWR between 0.8 and 1.3: "Sweet Spot" (safe progression).
*   ACWR > 1.5: Elevated injury risk ("Danger Zone").
