# TrainMate Architecture & System Manifest

This document outlines the codebase design, module responsibilities, database schema,
and sports science calculations of TrainMate. It is intended to help developers and AI agents
quickly build a mental model of the system.

---

## 1. System Overview & Module Map

TrainMate is a local AI sports science coaching application that coordinates training objectives,
life events, and Garmin metrics.

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

*   [trainmate_cli.py](file:///home/jlh/src/TrainMate/trainmate_cli.py): Console CLI interface. Dispatches commands for goals, life events, metrics pull, and generating plans/workouts.
*   [trainmate_web.py](file:///home/jlh/src/TrainMate/trainmate_web.py): Flask local server providing static assets and a REST API endpoint layer for the web UI.
*   [trainmate/types.py](file:///home/jlh/src/TrainMate/trainmate/types.py): Central type repository holding standard TypedDicts for goals, workouts, metrics, and cycles.
*   [trainmate/config.py](file:///home/jlh/src/TrainMate/trainmate/config.py): Config singleton parsing settings from `config.yaml` and system environment variables.
*   [trainmate/db.py](file:///home/jlh/src/TrainMate/trainmate/db.py): SQLite wrapper containing table creation and CRUD operations for domain objects.
*   [trainmate/coach.py](file:///home/jlh/src/TrainMate/trainmate/coach.py): Implements a Repository/Service pattern (`CoachRepository`, `CoachEngine`, `CoachService`) to decouple database and calendar side-effects from pure coaching business logic.
*   [trainmate/openrouter.py](file:///home/jlh/src/TrainMate/trainmate/openrouter.py): OpenRouter client using Gemini for planning and structured JSON response schemas.
*   [trainmate/google_sheets.py](file:///home/jlh/src/TrainMate/trainmate/google_sheets.py): Syncs metrics and training workload activities from Google Sheets.
*   [trainmate/google_calendar.py](file:///home/jlh/src/TrainMate/trainmate/google_calendar.py): Syncs planned and adapted workouts as all-day events on Google Calendar.

---

## 2. Terminology: Plans vs. Workouts

To provide clear semantic separation, TrainMate distinguishes between long-term strategy and daily execution:
*   **Plans**: Refers strictly to the overall periodization strategy, consisting of `macrocycles` (the overarching timeline tied to an objective) and `mesocycles` (block training phases). Generated via `plan generate`.
*   **Workouts**: Refers to the daily microcycle activities that implement the mesocycle's focus. Generated via `workout generate` and adapted based on daily metrics via `workout adapt`.

---

## 3. Database Schema

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

### lifeevents
Stores life event dates which limit training availability (formerly `constraints`).
*   `id` (INTEGER PRIMARY KEY)
*   `title` (TEXT)
*   `start_date` (TEXT - YYYY-MM-DD)
*   `end_date` (TEXT - YYYY-MM-DD)
*   `event_type` (TEXT - business_trip, vacation, party, other)
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
*   `duration_minutes` (INTEGER)
*   `rpe` (INTEGER)
*   `tss` (INTEGER)

### completed_activities
Stores historical Garmin activity logs pulled from Google Sheets.
*   `activity_id` (TEXT PRIMARY KEY)
*   `date` (TEXT)
*   `start_time` (TEXT)
*   `activity_name` (TEXT)
*   `activity_type` (TEXT)
*   `duration_sec` (REAL)
*   `distance_km` (REAL)
*   `elevation_gain_m` (REAL)
*   `avg_hr` (INTEGER)
*   `max_hr` (INTEGER)
*   `rpe` (INTEGER)
*   `tss` (REAL)

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
*   `goals_hash` / `lifeevents_hash` (TEXT - status tracking for plan invalidation)
*   `config_hash` (TEXT - hash of config.yaml to detect configuration changes)
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

## 4. Sports Science & Coaching Mathematics

### Science Guidelines
TrainMate uses specific prompt guidelines for LLM coaching:
*   **Built-in Guidelines**: Located in `trainmate/science/`. These dictate internal system logic such as ACWR calculations and periodization rules.
*   **Custom Athlete Guidelines**: Located in the root `science/` directory. This is an empty folder dedicated to user-provided research documents to personalize the AI coach's philosophy.

### Workload Calculation
Workload is computed per activity in `google_sheets.py`:
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

### Daily Readiness & Adaptation
TrainMate tracks multi-day rolling trajectories (up to 5 days) for Resting Heart Rate (RHR), Heart Rate Variability (HRV), and Sleep against a 28-day baseline. 
*   If HRV drops > 1 standard deviation below the mean, or RHR elevates > 1 std (min +3 bpm), it flags potential overtraining and dynamically proposes lighter, adapted workouts via `workout adapt`.
