# TrainMate Architecture & System Manifest

This document is the primary reference for coding agents. Read it before
reading source files — in most cases it will be sufficient. Read a source file
only when you need to change it or when a specific detail is not covered here.

---

## 1. System Overview

TrainMate is a local AI sports-science coaching application. The user
configures goals and life events; TrainMate generates periodized training plans
(macrocycle → mesocycles) and workout schedules (microcycles), then adapts them
daily based on Garmin metrics. Plans and workouts can be pushed to Google
Calendar.

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
  |  trainmate/garmin.py        (Garmin direct pull) |
  |  trainmate/google_calendar.py (Calendar sync)    |
  +--------------------------------------------------+
```

---

## 2. Module Map

Every module exposes one **singleton** at module level (see section 6 for the
list). UIs and tests import the singleton directly — never instantiate the
classes themselves.

### Entry Points

| File                 | Purpose                                                              |
|----------------------|----------------------------------------------------------------------|
| `trainmate_cli.py`   | argparse CLI; dispatches to handler functions `run_*()`. No          |
|                      | business logic.                                                      |
| `trainmate_web.py`   | Flask REST API; thin handler functions calling `db`,                 |
|                      | `coach_service`, `calendar_syncer` (pure reader — never pulls).      |

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
| `garmin.py`          | module functions     | Logs into Garmin Connect; pulls metrics +        |
|                      |                      | activities to DB, recomputes derived metrics,    |
|                      |                      | maintains the `sync_state` watermark, and        |
|                      |                      | `ensure_data()` auto-refreshes on read. Owns the |
|                      |                      | load model: `measured_tss`, `activity_load`,     |
|                      |                      | `rpe_divergence` (see §12).                       |
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
Module-level function. Concatenates all `*.txt` files from `trainmate/science/`
(built-in) and `science/` (user-provided). Called by
`CoachService._load_science_guidelines()`.

### `CoachEngine`
**Pure business logic — no DB or I/O.** All methods are prefixed `_` (called by
`CoachService` or directly by tests).

| Method                               | What it does                                        |
|--------------------------------------|-----------------------------------------------------|
| `_build_system_prompt(...)`          | Assembles the main LLM system prompt with           |
|                                      | guidelines, strategy, goals, life events, athlete   |
|                                      | profile.                                            |
| `_format_athlete_profile(profile)`   | Formats `config.user_profile` dict into a readable  |
|                                      | prompt segment.                                     |
| `_get_goals_hash(objectives)`        | SHA-256 of sorted objectives list.                  |
| `_get_lifeevents_hash(lifeevents)`   | SHA-256 of sorted life events list.                 |
| `_get_config_hash()`                 | SHA-256 of `user_profile` + `metrics_lookback_days`. |
| `_generate_macrocycle_strategy(...)` | LLM call → `{strategy, mesocycles}`. Label:         |
|                                      | `periodization_plan`.                               |
| `_generate_workouts_logic(...)`      | LLM call → `{reasoning, workouts[]}`. **Read-only** |
|                                      | w.r.t. learnings — emits no `learning_updates`      |
|                                      | (DESIGN_backward_evaluation.md §11). Accepts        |
|                                      | `num_days` (default 28) driving the horizon. Label: |
|                                      | `workout_generation`.                               |
| `_adapt_logic(...)`                  | LLM call → `{change_needed, reason,                 |
|                                      | learning_updates[], adapted_workouts[]}`. Label:    |
|                                      | `workout_adaptation`.                               |
| `_analyze_workouts_logic(...)`       | LLM call → `{macrocycle_summary,                    |
|                                      | inferred_macrocycle, inferred_mesocycles[],         |
|                                      | physiological_insights[], learning_updates[]}`.     |
|                                      | Reverse-engineers cycles from weekly summaries.     |
|                                      | Label: `workout_analysis`.                          |
| `_generate_intermediate_goals(...)`  | LLM call → `{goals[]}` when timeline > 24 weeks.    |
|                                      | Label: `generate_intermediate_goals`.               |

**Coach learnings via deltas:** `_analyze_workouts_logic` and `_adapt_logic` emit
a `learning_updates` array (shared prompt field `LEARNING_UPDATES_FIELD`) of
incremental ops rather than a full learnings blob. (`_generate_workouts_logic`
is **read-only** — it consumes learnings but emits none; see
DESIGN_backward_evaluation.md §11.) The app owns the merge via
`CoachService._apply_learning_updates()` → `db.apply_learning_deltas()`, so a
model that omits an existing learning cannot lose it. Each learning carries a
**sport scope** (`sports`: comma-list or `general`) and a **confidence** level
(`tentative` | `moderate` | `established`). The four ops:
- `{"op": "add", text, sports?, confidence?}` — new record (defaults
  `general`/`tentative`).
- `{"op": "revise", id, text?, sports?, confidence?}` — change supplied fields;
  refreshes recency.
- `{"op": "reinforce", id, confidence?}` — reaffirm without rewording;
  refreshes recency.
- `{"op": "retire", id}` — hard delete.

**Decay (soft):** a learning is *dormant* once it goes unreinforced past a
confidence-based budget (`db.LEARNING_STALENESS_DAYS`: tentative 21d / moderate
60d / established 180d), computed by `db.learning_is_dormant()`.
`get_learnings()` annotates each record with a `dormant` flag; dormant records
stay in the DB and show in `status` (marked) but are **excluded from prompts**
until a `revise`/`reinforce` refreshes them.
`CoachService._get_learnings_text()` renders only active learnings as
`[id|sports|confidence] text`. Every flow that *uses* learnings (generate,
analyze, adapt) injects this rendered block into its prompt — generate/adapt
via `_build_system_prompt`, analyze under its own `COACH LEARNINGS` heading —
so the two delta-emitting flows (analyze, adapt) can
`revise`/`reinforce`/`retire` by `[id]` instead of blindly re-adding
near-duplicates on repeated runs. `CoachService.adapt()` applies the deltas at
evaluation time (regardless of whether the proposed workout changes are later
applied).

**Reinforcement integrity (`suppress_reinforcement`):**
`db.apply_learning_deltas(deltas, suppress_reinforcement=False)` accepts a flag
that, when True, drops the purely-ratcheting effects so re-reading *unchanged*
evidence cannot inflate confidence or reset decay — `reinforce` is skipped and
`revise` keeps content edits but not the recency refresh, while `add`/`retire`
still apply. The evidence fingerprint that decides "unchanged" is
`CoachEngine._get_evidence_fingerprint(activities, metrics, window)`; the
cached reconstruction it gates lives in the `analysis_cache` table. Full model
in DESIGN_backward_evaluation.md §5, §8.

### `CoachService`
**Orchestrator — owns all DB and calendar access.** Exposes the public API
called by the UIs.

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
| `analyze_workouts(from, until, days, weeks, context, force, inspect)`| Reverse-engineers past training cycles from completed   |
|                                                     | activities + metrics. Auto-resolves the date range from             |
|                                                     | active/preceding goals when omitted. **Reuses** the `analysis_cache`|
|                                                     | when the evidence fingerprint is unchanged (skips the LLM); `force` |
|                                                     | recomputes anyway (a forced unchanged re-run still suppresses the   |
|                                                     | reinforcement ratchet); `inspect` renders without writing learnings |
|                                                     | or cache. Otherwise calls `CoachEngine._analyze_workouts_logic()`,  |
|                                                     | applies `learning_updates`, and caches the reconstruction. See      |
|                                                     | DESIGN_backward_evaluation.md §5, §8, §9.                           |
| `_build_prior_training_context(prior_macro, today)` | Builds the read-only "planned vs actual" review injected into the   |
|                                                     | `plan generate` strategy prompt (Option A, §6). Anchored on the     |
|                                                     | prior plan's elapsed mesocycle windows; folds in the cached         |
|                                                     | reconstruction's insights. Writes no `feedback` field.             |
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

- Every method opens a fresh `sqlite3` connection (context manager), commits,
  and closes.
- `foreign_keys = ON` is set on every connection; cascades are used on
  macrocycles→mesocycles.
- `save_workout(date, sport_type, ...)` is an **upsert**: it looks up by
  `(date, sport_type)` and updates if found, inserts otherwise. The
  `google_event_id` is preserved unless explicitly passed.
- `clear_future_workouts(from_date)` deletes future workouts with `status !=
  'synced'`.

### Key methods by domain

**Objectives:** `add_objective`, `get_objectives(status=)`,
`get_objective(id)`, `update_objective(id, **kwargs)`, `delete_objective`,
`wipe_objectives`

**Life Events:** `add_lifeevent`, `get_lifeevents(start_after=)`,
`get_lifeevent(id)`, `update_lifeevent(id, **kwargs)`, `delete_lifeevent`,
`wipe_lifeevents`

**Workouts:** `save_workout` (upsert), `get_workout(date, sport_type)`,
`get_workouts(start_date, end_date, sport_type)`, `get_workout_by_id(id)`,
`delete_workout_by_id`, `clear_future_workouts`, `wipe_workouts`

**Completed Activities:** `save_completed_activity` (upsert on `activity_id`),
`get_completed_activities(start_date, end_date)`

**Metrics & Baselines:** `save_metric_cache` (upsert),
`get_metrics_cache(start_date, end_date)`, `save_baseline`,
`get_baseline(date)` (returns closest prior baseline), `wipe_metrics` (also
clears `analysis_cache`, which is evidence-derived)

**Coach Learnings:** `get_learnings()` (each record annotated with a computed
`dormant` flag), `add_learning(text, sports='general',
confidence='tentative')`, `update_learning(id, text)`, `delete_learning(id)`,
`apply_learning_deltas(deltas, suppress_reinforcement=False)`. Discrete,
addressable athlete-observation records (table `coach_learnings`) enriched with
sport scope, confidence, and recency; updated incrementally via LLM deltas
(`add`/`revise`/`reinforce`/`retire`). `apply_learning_deltas` runs all ops in
one transaction and silently skips malformed deltas (and invalid confidence
values); `suppress_reinforcement` drops the ratcheting effects on unchanged
evidence (see section 3). Module-level helpers: `normalize_sports()`,
`valid_confidence()`, `learning_is_dormant()`, constants `CONFIDENCE_LEVELS` /
`LEARNING_STALENESS_DAYS` (see section 3 for the delta/decay model).
Periodization strategy lives in the `macrocycles` table, not here.

**Analysis Cache:** `save_analysis_cache(horizon, fingerprint, window_start,
window_end, reconstruction)` (upsert, one row per `horizon`),
`get_analysis_cache(horizon)` (returns the row with `reconstruction` parsed
from JSON, or `None`), `wipe_analysis_cache()`. Caches a backward-evaluation
reconstruction keyed by an evidence fingerprint so a re-run over unchanged data
reuses it instead of re-calling the LLM (table `analysis_cache`; see
DESIGN_backward_evaluation.md §5.1).

**Macrocycles/Mesocycles:** `save_macrocycle` (deletes existing for objective,
then inserts), `get_macrocycle_for_objective(objective_id)`,
`get_last_macrocycle()`, `get_mesocycles_for_macrocycle(macrocycle_id)`,
`get_mesocycle(id)`, `update_macrocycle_feedback(id, feedback)`,
`update_mesocycle_feedback(id, feedback)`, `update_macrocycle_config_hash(id,
hash)`, `delete_macrocycle_for_objective`, `wipe_plans`

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
| `rpe`               | INTEGER | User-entered in Garmin (directWorkoutRpe); NULL if not entered (never synthesised) |
| `tss`               | REAL    | Measured TSS only: power TSS if a power meter recorded, else hrTSS; NULL if neither. Training *load* is derived on the fly, not stored — see Load model below |
| `bike_avg_watts`    | INTEGER | From Garmin; NULL for non-bike activities          |
| `zone1_sec`–`zone5_sec`     | INTEGER | Time in each HR zone (seconds); NULL if missing |
| `power_zone1_sec`–`power_zone7_sec` | INTEGER | Time in each Coggan power zone (seconds); NULL unless a power meter recorded |

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
28-day rolling baseline computed during `garmin.recompute_derived()` (a full
sweep run after every pull).

| Column                      | Type    |
|-----------------------------|---------|
| `date`                      | TEXT PK |
| `rhr_baseline_mean`         | REAL    |
| `rhr_baseline_std`          | REAL    |
| `hrv_baseline_mean`         | REAL    |
| `hrv_baseline_std`          | REAL    |
| `sleep_baseline_mean`       | REAL    |
| `sleep_baseline_std`        | REAL    |

### sync_\state
Garmin pull watermark (one row, `key='garmin'`). `through_date` is the forward
high-water mark (local YYYY-MM-DD) and only ever advances; `last_pull_utc` is an
instant compared against now for the freshness interval. See section 8 (Data
Pull) and `DESIGN_garmin_direct_pull.md`.

| Column          | Type    | Notes                                       |
|-----------------|---------|---------------------------------------------|
| `key`           | TEXT PK | Source key, e.g. `garmin`                   |
| `through_date`  | TEXT    | Forward high-water mark (local YYYY-MM-DD)  |
| `last_pull_utc` | TEXT    | ISO instant of last successful Garmin pull  |

### coach_\learnings
Discrete, addressable athlete-observation records, updated incrementally via LLM
deltas (`add`/`revise`/`reinforce`/`retire`). Enriched with sport scope,
confidence, and recency (see section 3 for the decay model).

| Column               | Type       | Notes                                                  |
|----------------------|------------|--------------------------------------------------------|
| `id`                 | INTEGER PK | Referenced by `revise`/`reinforce`/`retire` deltas     |
| `text`               | TEXT       | LLM-generated observation                              |
| `sports`             | TEXT       | Comma-separated sport scope, or `general` (default)    |
| `confidence`         | TEXT       | `tentative` (default) / `moderate` / `established`     |
| `created_at`         | TEXT       | ISO timestamp                                          |
| `updated_at`         | TEXT       | ISO timestamp; last content/metadata change            |
| `last_reinforced_at` | TEXT       | ISO timestamp; drives decay → `dormant` (see §3)       |

### macrocycles
| Column            | Type                  | Notes                                            |
|-------------------|-----------------------|--------------------------------------------------|
| `id`              | INTEGER PK            |                                                  |
| `objective_id`    | INTEGER FK→objectives | Cascade delete                                   |
| `strategy`        | TEXT                  | LLM-generated strategy text                      |
| `goals_hash`      | TEXT                  | SHA-256 of objectives at generation time         |
| `lifeevents_hash` | TEXT                  | SHA-256 of life events at generation time        |
| `config_hash`     | TEXT                  | SHA-256 of `user_profile` +                      |
|                   |                       | `metrics_lookback_days`                          |
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

### analysis\_cache
Cached backward-evaluation reconstruction (inferred cycles + insights), keyed by
an evidence fingerprint. One row per `horizon`; cleared by `wipe_metrics`. See
DESIGN_backward_evaluation.md §5.1.

| Column           | Type       | Notes                                              |
|------------------|------------|----------------------------------------------------|
| `id`             | INTEGER PK |                                                    |
| `horizon`        | TEXT       | `long` \| `short` — UNIQUE; the cache slot         |
| `fingerprint`    | TEXT       | Hash of activity-id set + metrics + window         |
| `window_start`   | TEXT       | YYYY-MM-DD                                         |
| `window_end`     | TEXT       | YYYY-MM-DD                                         |
| `reconstruction` | TEXT       | JSON: inferred cycles + physiological insights     |
| `created_at`     | TEXT       | ISO timestamp                                      |

---

## 6. Singletons

All modules export a singleton at the bottom. Import these, never instantiate
the classes:

```python
from trainmate.config import config          # Config
from trainmate.db import db                  # Database
from trainmate.coach import coach_service    # CoachService
from trainmate.openrouter import openrouter_client  # OpenRouterClient
from trainmate.google_calendar import calendar_syncer  # CalendarSyncer
from trainmate import garmin                     # module functions (pull, ensure_data, …)
```

`trainmate/garmin.py` exposes module-level functions rather than a singleton:
`pull()`, `ensure_data()`, `recompute_derived()`, plus the `GarminClient` class
and `GarminAuthRequired`.

For tests, the DB singleton can be overridden by patching the module-level `db`
variable in affected modules (see `tests/test_adaptation.py` for the pattern:
assign `test_db` to `trainmate.coach.db`, `trainmate.garmin.db`, etc.  before
importing the singletons).

---

## 7. CLI Commands Reference

Invoked as `python trainmate_cli.py <command> [subcommand] [args]`.
Handler functions are named `run_<command>_<subcommand>()` in `trainmate_cli.py`.

| Command      | Subcommand   | Alias    | Description                                                              |
|--------------|--------------|----------|--------------------------------------------------------------------------|
| `status`     | —            | `s`      | Show active goals, recent metrics, coach learnings                       |
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
| `plan`       | `feedback`   | `p f`    | Add feedback (`--macro` or `--meso ID`, `--goal ID`, text;              |
|              |              |          | `--edit` opens `$EDITOR` seeded with current feedback)                  |
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
| `workout`    | `swap`       | `w s`    | Swap workouts between two dates (`<date1> <date2>`) or two IDs           |
|              |              |          | (`--id1 X --id2 Y`). Runs recovery checks (consecutive hard             |
|              |              |          | days, weekly load spikes, mesocycle crossings) and prompts on           |
|              |              |          | warnings unless `-f`/`--force`. Syncs to Calendar unless `--no-sync`.    |
| `workout`    | `wipe`       | —        | Delete all workouts                                                      |
| `data`       | `pull`       | `d pull` | Fetch metrics and activities directly from Garmin (`--days`/`--from`/`--until`/`--metrics-only`/`--activities-only`/`--sleep`) |
| `data`       | `analyze`    | `d a`    | Analyze completed workouts/metrics to detect cycles                      |
|              |              |          | (`--from`, `--until`, `--days`, `--weeks`, `--context`,                  |
|              |              |          | `--force` to recompute, `--inspect` for read-only)                      |
| `data`       | `show-metrics` | `d sm` / `sm` | Show athlete metrics over a date range. Supports standard |
|              |              |          | date range options, `-a`/`--all` (shows all data),      |
|              |              |          | `--no-pull` to bypass Garmin sync, and `--csv`.           |
| `data`       | `show-activities` | `d sa` / `sa` | Show completed activities over a date range. Supports |
|              |              |          | date options, `-a`/`--all`, `--type` filter, `--no-pull`,  |
|              |              |          | and `--csv`.                                              |
| `data`       | `backfill-tss` | —      | Recompute the measured `tss` for all stored activities under the current zone model (no Garmin calls), then refresh derived workload |
| `data`       | `wipe`       | —        | Delete all metrics, baselines, completed activities                      |

---

## 8. Web API Endpoints

Flask server at `trainmate_web.py`, runs on port 5000. Static files served from
`static/`.

| Method      | Path                            | Description                                  |
|-------------|---------------------------------|----------------------------------------------|
| GET         | `/api/status`                   | Active goal, latest metrics, coach learnings |
|             |                                 | (under `coach_learnings.learnings`),         |
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
| POST        | `/api/metrics/pull`             | Returns 409 — Garmin pulls are CLI-only      |
| GET         | `/api/metrics`                  | Last 30 days of cached metrics               |


---

## 9. Configuration (`config.yaml`)

Required fields:

| Key                    | Type | Description                                                   |
|------------------------|------|---------------------------------------------------------------|
| `openrouter_api_key`   | str  | Also readable from `OPENROUTER_API_KEY` env var               |
| `openrouter_model`     | str  | Default: `google/gemini-3.5-flash`                            |
| `google_calendar_id`   | str  | Target calendar ID                                            |
| `garmin_email` / `garmin_password` | str | Garmin login; config.yaml only (kept out of the environment) |
| `garmin_refresh_minutes` / `garmin_mutable_days` / `garmin_backfill_prompt_days` / `garmin_initial_backfill_days` / `garmin_throttle_seconds` | — | Auto-ensure tuning (see §8) |
| `service_account_file` | str  | Path to service account JSON (default:                        |
|                        |      | `service_account.json`)                                       |
| `metrics_lookback_days`  | int  | Rolling window for adaptation (default: 15)                  |
| `workout_generate_days` | int  | Default horizon for `workout generate` (default: 28)         |
| `low_load_threshold`    | float| Workload score below which an activity is "minor"            |
|                         |      | (default: 25). Controls rest-day violations and unplanned    |
|                         |      | activity visibility (shown as gray/minor if below threshold,  |
|                         |      | yellow/unplanned if above). Mismatch tolerance for planned   |
|                         |      | workouts is dynamically computed from expected workload.     |
| `rpe_divergence_ratio`  | float| sRPE-load ÷ measured-load above which a session is flagged   |
|                         |      | to the coach as "felt harder than measured" (default: 1.5;   |
|                         |      | set very high to disable)                                    |
| `user_profile`          | dict | Must contain `lthr` or `ftp` (see below)                     |

`user_profile` keys: `name`, `birth_year`, `max_hr`, `lthr`, `ftp`,
`weekly_target_hours`, `sport_preferences`, `chronic_injuries`, `preferences`,
`equipment`, `weekly_schedule`. `weekly_schedule` maps day names to
`{total_available_hours, max_sessions, certainty_percent, equipment}`.

---

## 10. Key Data Flows

### Plan Generation (`plan generate`)
1. `CoachService.generate_periodization_plan()` fetches active objectives +
   life events.
2. Computes `goals_hash`, `lifeevents_hash`, `config_hash`.
3. If existing macrocycle has matching hashes and `force=False` → reuse.
4. Otherwise: builds a read-only **planned-vs-actual review** of the prior plan
   via `_build_prior_training_context()` (Option A — anchored on the prior
   plan's elapsed mesocycle windows, plus the cached reconstruction's insights;
   written to no `feedback` field), prints it, and passes it as
   `prior_training_text` into `CoachEngine._generate_macrocycle_strategy()` →
   LLM → `{strategy, mesocycles}`.  See DESIGN_backward_evaluation.md §6.
5. If timeline > 24 weeks: calls `CoachEngine._generate_intermediate_goals()`
   first, saves intermediate objectives, then re-runs with the first goal.
6. Saves new macrocycle + mesocycles to DB (old ones deleted via
   `save_macrocycle`).

### Workout Generation (`workout generate`)
1. CLI resolves the generation horizon (end date) from flags in priority order:
   `--days` / `--weeks` → `--until DATE` → `--until-goal [ID]` →
   `--until-mesocycle ID` → `config.workout_generate_days` (default 28).
2. `CoachService.generate_workouts(end_date=...)` verifies a macrocycle exists,
   computes `num_days` from `(end_date − today)`.
3. Fetches metrics history (last `metrics_lookback_days` days) + baseline.
4. Calls `CoachEngine._generate_workouts_logic(num_days=...)` → LLM →
   `{reasoning, workouts[]}`. **Read-only** w.r.t. coach learnings — it
   consumes the rendered learnings in its prompt but emits/applies no
   `learning_updates` (DESIGN_backward_evaluation.md §11).
5. Clears future unsynced workouts (`clear_future_workouts`), then saves new
   workouts.

### Daily Adaptation (`workout adapt`)
1. `CoachService.adapt()` fetches metrics + planned workouts + completed
   activities in window.
2. `analyze_adherence()` (`adherence.py`) computes discrepancies (misses,
   duration/load mismatches, rest violations).
3. Finds active mesocycle for the target date → sets `meso_end_date` for
   adaptation range.
4. Calls `CoachEngine._adapt_logic()` → LLM → `{change_needed, reason,
   learning_updates[], adapted_workouts[]}`.
5. Applies `learning_updates` deltas to `coach_learnings`
   (`_apply_learning_updates`) — at evaluation time, independent of whether the
   workout changes are applied.
6. Returns `(reason, proposed_workouts)` — caller decides whether to apply.
7. If applied: `apply_adaptations()` deletes overridden calendar events + DB
   rows, saves adapted workouts with `status='modified'`, syncs to Calendar.

### Data Pull (`data pull`) and auto-ensure Data is pulled **directly from
Garmin Connect** (`trainmate/garmin.py`); the former Google Sheets path is
gone. Full design: `DESIGN_garmin_direct_pull.md`.

1. `garmin.pull(start, end)` logs into Garmin (token persistence; TTY-gated
   MFA), fetches activities (storing the **measured** TSS — power TSS or hrTSS —
   and the user's `directWorkoutRpe` if entered; neither is synthesised) and
   daily metrics. It writes a row to `athlete_metrics_cache` for **every day in
   range — even all-null ones** — so the table's date coverage records what has
   been pulled. Activities with low HR-zone coverage and no RPE are reported in
   an aggregated warning (their load is an underestimate).
2. `garmin.recompute_derived()` runs a **full sweep** over all cached days:
   acute/chronic workload + ACWR (7/28-day windows) and the 28-day
   RHR/HRV/sleep baseline. A full sweep is cheap locally and avoids
   windowed-recompute bugs.
3. The `sync_state` watermark advances (`through_date` forward only,
   `last_pull_utc` = now).
4. `bike_avg_watts` and `zone1_sec`–`zone5_sec` come from Garmin (NULL when
   absent) and feed `format_completed_activities` in `coach.py` verbatim.

**Auto-ensure.** Read-side commands call `garmin.ensure_data(start, end)` at
entry (idempotent per process via an in-memory memo). It pulls the
28-day-padded required window where the gap is small/recent and **prints a
copy-pastable `data pull` command for large backfills** (cold start, big
forward/backward gaps), always continuing with cached data. Calendar dates use
the machine-local timezone (`util.today_str`/`today_date`); stored instants
stay UTC. The web app never calls this — it is a pure reader (see §1).

### Data Analysis (`data analyze`)
1. `CoachService.analyze_workouts()` determines target start/end dates
   automatically based on active and preceding goals if date range parameters
   are omitted.
2. Queries the database for completed activities and physiological metrics for
   that date range.
3. Computes the evidence fingerprint and checks `analysis_cache['long']`. If
   the fingerprint matches and `--force` is absent → returns the cached
   reconstruction (no LLM call). `--force` recomputes regardless.
4. Groups metrics and activities week-by-week using Monday-commencing ISO
   weeks.
5. Queries `CoachEngine._analyze_workouts_logic()` -> LLM ->
   `{macrocycle_summary, inferred_macrocycle, inferred_mesocycles[],
   physiological_insights[], learning_updates[]}`.
6. Unless `--inspect`: applies `learning_updates` deltas (with
   `suppress_reinforcement=True` when the evidence was unchanged) and caches
   the reconstruction in `analysis_cache`. `--inspect` renders but writes
   nothing.  See DESIGN_backward_evaluation.md §5, §8, §9.

---

## 11. Terminology: Plans vs. Workouts

- **Plan** = periodization strategy: one macrocycle (per objective) + mesocycle
  blocks.  Commands: `plan generate/show/rm/feedback`.
- **Workouts** = daily microcycle activities implementing the mesocycle focus.
  Commands: `workout generate/adapt/push/swap`.

A `workout swap` exchanges the dates of two workouts (or moves one onto an
empty rest day). Moved workouts are flagged `status='modified'` with a
`modification_reason` recording the swap, exactly like an adaptation — so they
are re-synced by `workout push` and visibly distinguished from untouched
`planned` ones. Swaps are validated first (`CoachService.validate_swap`): the
new schedule is simulated and the user is warned about newly-created >2-day
high-intensity streaks, weekly load spikes (an ACWR proxy), and
mesocycle-boundary crossings.

Plan must be generated before workouts. Workouts cover a rolling window from
today whose length is controlled by the horizon flags on `workout generate`
(default: `workout_generate_days` in `config.yaml`, falling back to 28 days).
`replan()` calls `generate_periodization_plan` then `generate_workouts` in one
step (always uses the config default).

---

## 12. Sports Science & Coaching Mathematics

### Load model (per activity)
The stored `tss` column is the **objective measurement only**; the training
**load** is derived on the fly (`garmin.activity_load`) via a best-available
fallback — methods are never blended or max-ed:

1. **Power TSS** (Coggan 7-zone, `POWER_ZONE_TSS_PER_SEC`) when a power meter
   recorded — `TSS/s = IF² × 100 / 3600` per zone (Z6/Z7 extrapolated, Z7 IF
   capped at 1.60).
2. **hrTSS** (Friel 5-zone, `HR_ZONE_TSS_PER_SEC`) when HR-zone coverage
   ≥ `HR_ZONE_COVERAGE_MIN` (0.5). Coverage = Σ(HR-zone secs)/duration; guards
   against Garmin's HR zone-1 floor zeroing out low-intensity work (yoga, easy
   walks, lift-served skiing).
3. **Session RPE** (Foster sRPE = `RPE × 10 × hours`) when the user entered an
   RPE and power is absent / HR is too sparse. **RPE is user-entered only and
   never computed from power or HR.**

When method 3 should apply but no RPE was entered, the weak hrTSS (or 0) is kept
and the activity is counted in an aggregated underestimate warning. There is no
activity-type special case — coverage + the divergence flag cover strength and
hybrid sessions (e.g. kettlebell HIIT: hrTSS captures the cardio, divergence
flags the muscular cost).

### RPE divergence (external vs internal load)
`garmin.rpe_divergence` flags, to the coach only, sessions where the load came
from power/HR but the user's RPE implies ≥ `rpe_divergence_ratio` (config,
default 1.5) × the measured load — i.e. it felt harder than it measured (heat,
sleep debt, muscular damage). It never inflates the stored load.

### Workload per activity
`Workload = activity_load(act)` — the single fallback value above (the former
`TSS + RPE × hours` additive blend was removed).

### Acute Workload (7 days) Sum of daily workloads over the past 7 days
(current day included).

### Chronic Workload (28 days) Sum of workloads over past 28 days ÷ 4 (≈
average weekly load).

### ACWR ``` ACWR = Acute / Chronic ```
- < 0.8: under-training
- 0.8–1.3: "sweet spot"
- > 1.5: elevated injury risk

### Daily Readiness Signals
- HRV drops > 1 std below baseline mean → flag potential overtraining
- RHR rises > 1 std above baseline mean (min +3 bpm) → flag potential
  overtraining
- `workout adapt` acts on these signals over the rolling
  `metrics_lookback_days` window.

### Science Guidelines Files
- `trainmate/science/` — built-in: `acwr.txt`, `periodization.txt`,
  `recovery_metrics.txt`
- `science/` — user-provided; empty by default; any `.txt` files added here are
  injected into every LLM prompt.

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
| `tests/test_garmin.py`         | Garmin transforms, watermark/auto-ensure policy, recompute      |
| `tests/test_utils.py`          | `adherence.py` + `util.py` helpers                              |

Tests inject a fresh in-memory SQLite DB by assigning `test_db` to module-level
`db` variables *before* importing the singletons. `openrouter_client` is mocked
via `@patch`.

Integration / manual test scripts (not part of the test suite):
- `tests/run_integration.py`, `tests/run_calendar.py`
