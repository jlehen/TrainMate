# TrainMate Architecture & System Manifest

This document is the primary reference for coding agents. Read it before
reading source files — in most cases it will be sufficient. Read a source file
only when you need to change it or when a specific detail is not covered here.

Each section is **current-state reference**. The *why* behind non-obvious design
choices (and what they replaced) lives in [§15 Design Rationale & History](#15-design-rationale--history)
and the `DESIGN_*.md` files, so the reference sections stay lookup-friendly.

## Contents

1. [System Overview](#1-system-overview)
2. [Module Map](#2-module-map)
3. [coach Package Architecture](#3-coach-package-architecture) — **canonical** coach-learnings / confidence model
4. [Database — Key Patterns](#4-database--key-patterns)
5. [Database Schema](#5-database-schema)
6. [Singletons](#6-singletons)
7. [CLI Commands Reference](#7-cli-commands-reference)
8. [Web API Endpoints](#8-web-api-endpoints)
9. [Configuration (`config.yaml`)](#9-configuration-configyaml)
10. [Key Data Flows](#10-key-data-flows)
11. [Terminology: Plans vs. Workouts](#11-terminology-plans-vs-workouts)
12. [Sports Science & Coaching Mathematics](#12-sports-science--coaching-mathematics)
13. [Daily Context (Calendar Ingest)](#13-daily-context-calendar-ingest)
14. [Testing](#14-testing)
15. [Design Rationale & History](#15-design-rationale--history)

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
  |  trainmate_cli.py (shim) + trainmate/cli/        |
  |  trainmate_web.py (Flask)                        |
  +---------------------------+----------------------+
                              |
  +---------------------------v----------------------+
  |               Coaching Logic Layer               |
  |  trainmate/coach/service.py ─ CoachService       |
  |    │ orchestrates DB + calendar + LLM calls       |
  |  trainmate/coach/engine.py  ─ CoachEngine        |
  |    │ pure logic: prompt building, hash, LLM calls │
  |  trainmate/coach/formatting.py (pure helpers)    |
  |  trainmate/openrouter.py  (OpenRouter LLM client) |
  |  trainmate/adherence.py   (plan vs actual diff)   |
  +---------------------------+----------------------+
                              |
  +---------------------------v----------------------+
  |              Data & Integration Layer            |
  |  trainmate/db/              (SQLite CRUD)        |
  |  trainmate/garmin.py        (Garmin direct pull) |
  |  trainmate/google_calendar.py (Calendar sync)    |
  +--------------------------------------------------+
```

---

## 2. Module Map

Every module exposes one **singleton** at module level (see [§6](#6-singletons) for
the list). UIs and tests import the singleton directly — never instantiate the
classes themselves.

### Entry Points

- **`trainmate_cli.py`** — thin shim: argparse dispatcher (`main()`), the patchable
  singletons/helpers handlers reference via `import trainmate_cli as cli`, and a
  `__main__` alias. No business logic.
- **`trainmate/cli/`** — per-command-family handler modules (`run_*()`): `status`,
  `goals`, `lifeevents`, `context`, `learnings`, `plans`, `workouts`, `data`, plus
  shared `common`.
- **`trainmate_web.py`** — Flask REST API; thin handler functions calling `db`,
  `coach_service`, `calendar_syncer` (pure reader — never pulls).

### Package `trainmate/`

| File                 | Class / Singleton    | Purpose                                          |
|----------------------|----------------------|--------------------------------------------------|
| `types.py`           | —                    | TypedDicts: `Objective`, `LifeEvent`, `Workout`, |
|                      |                      | `CompletedActivity` (includes `bike_avg_watts`,  |
|                      |                      | `zone1_sec`–`zone5_sec`), `AthleteMetric`,       |
|                      |                      | `AthleteBaseline`, `Macrocycle`, `Mesocycle`     |
| `config.py`          | `config`             | Reads `config.yaml`; exposes typed properties.   |
| `db/`                | `db`                 | SQLite wrapper; `Database` composed from         |
|                      |                      | per-domain mixins. Full CRUD for all tables.     |
| `coach/`             | `coach_service`      | `service.py` `CoachService` orchestrator +       |
|                      |                      | `engine.py` `CoachEngine` pure logic +           |
|                      |                      | `formatting.py` prompt helpers.                  |
| `openrouter.py`      | `openrouter_client`  | HTTP client for OpenRouter; always expects       |
|                      |                      | `json_object` response.                          |
| `garmin.py`          | module functions     | Logs into Garmin Connect; pulls metrics +        |
|                      |                      | activities to DB, recomputes derived metrics,    |
|                      |                      | maintains the `sync_state` watermark, and        |
|                      |                      | `ensure_data()` auto-refreshes on read. Owns the |
|                      |                      | load model: `measured_tss`, `activity_load`,     |
|                      |                      | `rpe_divergence` (see §12).                       |
| `google_calendar.py` | `calendar_syncer`    | Creates/updates/deletes all-day Google Calendar  |
|                      |                      | events for workouts (outbound), and ingests       |
|                      |                      | tagged daily-context events into `daily_context` |
|                      |                      | (inbound — `sync_calendar_context`, see §13).    |
| `adherence.py`       | —                    | `analyze_adherence()` + `classify_adherence()`   |
|                      |                      | pure functions; compare planned vs completed     |
|                      |                      | (the latter yields a per-workout verdict).       |
| `sports.py`          | —                    | Canonical sport vocabulary (`SPORT_MAPPING`,     |
|                      |                      | `canonical_sport`, `sport_aliases`); dependency- |
|                      |                      | free so DB + adherence share it without a cycle. |
| `util.py`            | —                    | ANSI color helpers (`bold`, `green`, `red`, …),  |
|                      |                      | `wrap_text`, `format_labeled_text`.              |

---

## 3. coach Package Architecture

The `trainmate/coach/` package re-exports its public API from `__init__.py` (so
`from trainmate.coach import coach_service` keeps working) and is split into
three submodules:

- `formatting.py` — pure prompt-formatting helpers (no I/O, no LLM):
  `format_metrics_history`, `format_completed_activities`,
  `format_planned_workouts`, `format_planned_workouts_detailed` (adapt-only
  variant that includes each session's full description so the model preserves
  interval/rest detail it isn't deliberately changing, and tags already-completed
  sessions `[COMPLETED — locked history, not adaptable]` from the adherence
  `completed_keys`), `format_removed_workouts`, `format_daily_context`
  (renders the window's `daily_context` rows into the adapt prompt),
  `format_baseline`, `_load_science_guidelines`.
- `engine.py` — `CoachEngine` (prompt construction, hashing, LLM calls). Owns
  the `openrouter_client` binding — **patch target for tests:**
  `trainmate.coach.engine.openrouter_client`.
- `service.py` — `CoachService` + the `coach_service` singleton (data I/O,
  caching, orchestration). Owns the `db` / `calendar_syncer` / `config`
  bindings — **patch targets:** `trainmate.coach.service.db`, etc.

### `_load_science_guidelines(app_science_dir, science_dir) → str`
Module-level function in `formatting.py`. Concatenates all `*.txt` files from
`trainmate/science/` (built-in) and `science/` (user-provided). Called by
`CoachService._load_science_guidelines()`.

### `CoachEngine`
**Pure business logic — no DB or I/O.** All methods are prefixed `_` (called by
`CoachService` or directly by tests).

- **`_build_system_prompt(...)`** — assembles the main LLM system prompt
  (guidelines, strategy, goals, life events, athlete profile).
- **`_format_athlete_profile(profile)`** — formats `config.user_profile` into a
  readable prompt segment.
- **`_clean_goals(objectives)` / `_clean_lifeevents(lifeevents)`** — the
  planning-relevant fields, normalized and stably ordered. Single source of truth
  shared by the hash functions and the snapshots persisted on the macrocycle.
- **`_get_goals_hash(objectives)`** — SHA-256 of the `_clean_goals` list.
- **`_get_lifeevents_hash(lifeevents)`** — SHA-256 of the `_clean_lifeevents` list.
- **`_get_config_hash()`** — SHA-256 of `user_profile` + `metrics_lookback_days`.
- **`_plan_generate_strategy(...)`** — LLM call → `{strategy, mesocycles}`. Label
  `periodization_plan`.
- **`_workout_generate_logic(...)`** — LLM call → `{reasoning, workouts[]}`. Accepts
  `num_days` (default 28) driving the horizon and `start_str` (defaults to today) for
  the first day to schedule — the prompt tells the model to begin there. **Read-only**
  w.r.t. learnings. Label `workout_generation`.
- **`_workout_adapt_logic(...)`** — LLM call →
  `{change_needed, reason, adapted_workouts[]}`. **Read-only** w.r.t. learnings.
  Label `workout_adaptation`.
- **`_data_analyze_logic(...)`** — LLM call → `{macrocycle_summary,
  inferred_macrocycle, inferred_mesocycles[], physiological_insights[],
  learning_updates[]}`. Reverse-engineers cycles from weekly summaries. Label
  `workout_analysis`.
- **`_generate_intermediate_goals(...)`** — LLM call → `{goals[]}` when timeline >
  24 weeks. Label `generate_intermediate_goals`.

**Coach learnings via evidence-cited deltas:** only `_data_analyze_logic`
(the `data bootstrap`/`data reflect` flow) emits a `learning_updates` array
(shared prompt field `LEARNING_UPDATES_FIELD`). `_workout_generate_logic` **and**
`_workout_adapt_logic` are **read-only** — they consume the rendered learnings but author
none (DESIGN_backward_evaluation.md §11; DESIGN_evidence_based_confidence.md §2).
The app owns the merge via `CoachService._apply_learning_updates()` →
`db.apply_learning_deltas(deltas, available_weeks, source)`, so a model that omits
an existing learning cannot lose it. Each learning carries a **sport scope**
(`sports`) and an **app-computed confidence** (`tentative`/`moderate`/`established`).
The LLM **no longer sets confidence** — it only attributes each observation to the
training **week(s)** it was shown (`week_commencing` Mondays). The five ops:
- `{"op": "add", text, sports?, evidence:[weeks]}` — new record; seeds its
  supporting basis from `evidence`; confidence derived.
- `{"op": "revise", id, text?, sports?, evidence?:[weeks]}` — edit fields; if
  `evidence` given, also adds supporting weeks.
- `{"op": "reinforce", id, evidence:[weeks]}` — add supporting weeks (no
  reword). **No `evidence` ⇒ no-op.**
- `{"op": "contradict", id, evidence:[weeks]}` — add contradicting weeks; may
  trigger a *proposed* demotion.
- `{"op": "retire", id}` — hard delete (basis cascades).

Cited weeks are validated against `available_weeks` (the analysed window's
Mondays); weeks outside it are dropped (skip-malformed philosophy).

**Confidence = f(evidence basis)** (DESIGN_evidence_based_confidence.md §3).
`net = distinct supporting weeks − distinct contradicting weeks`; thresholds
(`config.learning_confidence_thresholds`, default moderate 3 / established 5) map
`net` to a level. **Upgrades auto-apply; downgrades are proposed, not applied** —
`db._recompute_confidence()` writes the lower level to `proposed_confidence` and
leaves the live `confidence` intact. Re-citing counted weeks is a structural
no-op (the `UNIQUE(learning_id, week, polarity)` constraint), so re-running /
`--force` / overlapping windows cannot inflate confidence. `last_reinforced_at`
refreshes only when a *new* supporting week lands (or on a staleness demotion).

**Decay (soft) + staleness demotion:** a learning is *dormant* once unreinforced
past its confidence budget (`db.LEARNING_STALENESS_DAYS`: tentative 21d / moderate
60d / established 180d, via `db.learning_is_dormant()`). Dormant records stay in
the DB, are listed by `learnings list` (marked), and are **excluded from prompts**. Crossing the
budget also **proposes a one-level staleness demotion** (`derive_staleness_proposals`);
accepting it re-arms the clock at the lower (shorter) budget, so an untouched
learning walks established → moderate → tentative → retire over real time.

**Propose / confirm flow:** pending downgrades (contradiction- or
staleness-driven) are resolved **interactively** at the end of `data
bootstrap`/`data reflect` — accept (`db.demote_learning`), keep
(`db.keep_learning` — dismiss + affirm: drop the −1 rows for a contradiction, or
refresh recency for staleness), or skip — or out of band via `learnings demote
<id>` / `learnings keep <id>`. `--auto` skips the prompts: staleness demotions
apply directly; contradiction demotions stay queued for the next interactive
review. `CoachService._get_learnings_text()` renders only active learnings as
`[id|sports|confidence] text` into every using flow's prompt.

**Inspection / curation:** the `learnings` command family is the home for viewing
and hand-curating records — `list` (filters: `--sport`/`--confidence`/`--dormant`),
`show <id>` (full text + per-week evidence basis), `edit`, `rm`, `demote`, `keep`,
`wipe`. `status` carries only a one-line summary (`N active, M dormant, K pending
demotion`) pointing at `learnings list`.

**Cold-start nudge:** when there are no active learnings, `plan generate` and
`status` suggest running `data bootstrap` (the only flow that authors learnings).

**Migration:** learnings predating the evidence model are grandfathered with a
synthetic supporting basis sized to sustain their stored level
(`db._grandfather_learning_evidence()`, source `migration`), so the first
recompute does not silently demote them (see [§15](#15-design-rationale--history)).

### `CoachService`
**Orchestrator — owns all DB and calendar access.** Exposes the public API
called by the UIs.

- **`plan_generate(force, objective_id)`** — fetches objectives/lifeevents, checks
  hashes, calls `CoachEngine._plan_generate_strategy()`, saves to DB. Auto-splits
  timelines > 24 weeks.
- **`workout_generate(objective_id, end_date)`** — requires an existing macrocycle.
  Fetches history, then **preserves a completed session**: if today's planned workout
  already has a matching completed activity (decided by `_today_workout_completed`, a
  one-day `analyze_adherence` pass), generation starts *tomorrow* so the finished
  workout isn't overwritten; otherwise it starts today. Computes `num_days` from
  `end_date` (or `config.workout_generation_span_days` if omitted) relative to that
  start, calls `CoachEngine._workout_generate_logic()`. **Eager:** archives the previous
  plan's future workouts from the generation start (tearing down their Calendar events),
  saves the new workouts tagged with the active `macrocycle_id`, then pushes them to
  Calendar straight away — the calendar always mirrors the active plan. A defensive
  filter drops any model-emitted workout dated before the generation start so it can't
  overwrite the preserved day. The archive (not delete) makes the regeneration undoable
  via `plan_rollback` (DESIGN_plan_rollback.md).
- **`plan_apply(objective_id, strategy, mesocycles)`** — persists an
  already-generated strategy + mesocycles to the DB (recomputes the goals/lifeevents/
  config hashes and snapshots). Used by the intermediate-goals branch of `plan generate`.
- **`replan(force, objective_id)`** — convenience: `plan_generate` then
  `workout_generate`.
- **`workout_adapt(target_date_str)`** — fetches metrics + workouts in the rolling
  window, calls `CoachEngine._workout_adapt_logic()`. Returns
  `(reason, proposed_workouts)`. The prompt shows the LLM the whole forward plan
  through the mesocycle end for context but instructs it to return **only sessions it
  is actually changing** — omitted sessions are preserved (apply never drops a date
  with no proposal), so the model is not pushed to re-author the entire block.
  Sessions that already have a matching completed activity (incl. one performed
  earlier on the evaluation date) are **locked history**: any proposal targeting such
  a `(date, sport)` is dropped before returning, so a workout already finished today is
  never "adapted". A **no-op backstop** (`_adapt_is_change`) then drops any proposal
  that reproduces an existing same-sport session on every meaningful field (title,
  description, duration/RPE/TSS — whitespace- and int/float-insensitive), so a session
  the model re-lists unchanged is never re-stamped as adapted or needlessly re-synced.
- **`workout_adapt_apply(proposed, reason, start, end)`** — deletes overridden
  workouts (+ calendar events), saves adapted workouts, syncs to Calendar. Each
  session keeps its short per-workout `change_reason` in `modification_reason`; the
  long batch `reason` is stamped on every session's `adaptation_summary`.
- **`workout_add(date, sport_type, title, description, …, replace_day=False)`** —
  manually schedules a workout (deterministic, no LLM), **replacing** any same-sport
  session that day — or every session that day with `replace_day`. Captures the
  overwritten session onto the new row (replaced description → `original_description`
  / "Originally:"; replaced title+duration/TSS/RPE + athlete reason →
  `modification_reason` / "Reason:"), carries the old `google_event_id` over, then
  syncs. Load re-balancing is left to `workout_adapt`. (See [§11](#11-terminology-plans-vs-workouts).)
- **`data_bootstrap(...)` / `data_reflect(...)`** — reverse-engineer past training
  cycles from completed activities + metrics via the shared `_run_workout_analysis`
  core. `bootstrap` = cold-start over the full backlog (horizon `long`), sets the
  reflect watermark; `reflect` = incremental since the watermark (horizon `short`),
  advances it. Both reuse `analysis_cache` on unchanged evidence; `force` recomputes;
  `inspect_only` renders without writing. See DESIGN_backward_evaluation.md §5, §8, §9.
- **`_build_prior_training_context(prior_macro, today)`** — builds the read-only
  "planned vs actual" review injected into the `plan generate` strategy prompt
  (Option A). Anchored on the prior plan's elapsed mesocycle windows; folds in the
  cached reconstruction's summary, inferred macro/mesocycle blocks, and physiological
  insights. Writes no `feedback` field.
- **`plan_rm(objective_id)`** — deletes macrocycle + mesocycles for that objective
  (cascades in DB; removes *all* versions, active and superseded).
- **`plan_rollback(objective_id, target_macrocycle_id)`** — restores a superseded plan
  version (the chronologically previous one by default, or a specific id) and its
  workouts. Archives the current plan's future workouts (deleting their events), flips
  the active macrocycle, resurrects the target version's archived workouts, and re-pushes
  them — the symmetric inverse of eager generation (DESIGN_plan_rollback.md).
- **`_get_config_hash()`** — delegates to `CoachEngine._get_config_hash()`. Used by
  CLI/web to detect stale plans.
- **`_get_coach_system_prompt(objectives, lifeevents, ...)`** — builds the system
  prompt without making an LLM call (used by tests).

**Singleton:** `coach_service = CoachService()` at the bottom of `coach/service.py`. Import as:
```python
from trainmate.coach import coach_service
```

---

## 4. Database — Key Patterns

**Package:** `trainmate/db/` · **Singleton:** `db = Database()` (in `__init__.py`)

`Database` is composed from per-domain mixins — `base.py` (`BaseDB`:
connection + schema setup), `objectives.py`, `lifeevents.py`, `dailycontext.py`,
`workouts.py`, `activities.py`, `learnings.py`, `analysis.py`, `periodization.py`,
`wipes.py` — all re-exported from `__init__.py` so `from trainmate.db import ...`
is unchanged.

- Every method opens a fresh `sqlite3` connection (context manager), commits,
  and closes.
- `foreign_keys = ON` is set on every connection; cascades are used on
  macrocycles→mesocycles.
- `save_workout(date, sport_type, ...)` is an **upsert**: it looks up by
  `(date, sport_type)` and updates if found, inserts otherwise. The
  `google_event_id` is preserved unless explicitly passed.
- **Sport-type matching is alias-aware** (`trainmate/sports.py`, `SPORT_MAPPING`):
  the coach's prompts/generated/adapted workouts speak canonical names
  (`strength_training`), while manual (`workout add`) or legacy rows may use an alias
  (`strength`). `workout add` normalizes input via `canonical_sport()`; `get_workout`
  and `save_workout`'s upsert match any alias of the same canonical sport
  (case-insensitively) so a canonical lookup/save resolves an aliased row instead of
  reporting it missing or inserting a duplicate. Adaptation's override check
  (`workout_adapt_apply`) compares canonically too. `adherence.py` re-exports
  `SPORT_MAPPING` from `trainmate/sports.py` (kept dependency-free to avoid the
  `adherence → garmin → trainmate.db` import cycle).
- `archive_future_workouts(from_date)` **soft-archives** every live future workout
  (sets `archived_at`, clears the Calendar handle) and returns the pre-archive rows so
  the caller can delete their events. Used by eager `workout generate` and
  `plan rollback` to displace a plan's workouts without losing them — the archived rows
  stay tagged with their `macrocycle_id` so `restore_macrocycle_workouts` can resurrect
  them (DESIGN_plan_rollback.md).

### Key methods by domain

**Objectives:** `add_objective`, `get_objectives(status=)`,
`get_objective(id)`, `update_objective(id, **kwargs)`, `delete_objective`,
`wipe_objectives`

**Life Events:** `add_lifeevent`, `get_lifeevents(start_after=)`,
`get_lifeevent(id)`, `update_lifeevent(id, **kwargs)`, `delete_lifeevent`,
`wipe_lifeevents`

**Daily Context:** `upsert_daily_context_by_event(google_event_id, …)`,
`get_daily_context(start_date=, end_date=, metric=)`,
`delete_daily_context_by_event(google_event_id)` — external signals reconciled
by Calendar event id (cleared by `wipe_metrics`; see §13). For the `context`
command: `get_daily_context_by_id(id)`, `delete_daily_context(id)`,
`list_context_metrics()`.

**Workouts:** `save_workout` (upsert),
`get_workout(date, sport_type)`,
`get_workouts(start_date, end_date, sport_type, include_removed=False,
include_archived=False)` (excludes soft-removed and archived rows unless asked),
`get_workout_by_id(id)`,
`delete_workout_by_id` (hard delete), `mark_workout_removed(id, reason=None)` (soft
delete — sets `removed=1`/`removed_reason`; the content change reads as `stale`),
`mark_workout_pushed(id, google_event_id, signature)` (records a successful push — the
only writer of `pushed_signature`), `restore_workout(id)` (clears the soft-delete
flags — `workout restore`), `update_workout_date(id, date)` (moves a row — used by
`workout swap`), `archive_future_workouts(from_date)` (soft-archives every live future
row — sets `archived_at`, clears the Calendar handle, returns the pre-archive rows so
the caller can delete events; used by eager generate + rollback),
`restore_macrocycle_workouts(macrocycle_id)` (un-archives that plan version's
most-recently-archived batch), `wipe_workouts`. `get_workouts`/`get_workout` exclude
archived rows by default (DESIGN_plan_rollback.md).

**Completed Activities:** `save_completed_activity` (upsert on `activity_id`),
`get_completed_activities(start_date, end_date)`

**Metrics & Baselines:** `save_metric_cache` (upsert),
`get_metrics_cache(start_date, end_date)`, `save_baseline`,
`get_baseline(date)` (returns closest prior baseline). Scoped wipes:
`wipe_garmin_data(start, end)` (metrics/baselines/activities + the evidence-derived
`analysis_cache`; resets the garmin/reflect/bootstrap watermarks on a full wipe,
leaves them on a dated wipe since re-pull detects gaps by row presence),
`wipe_calendar_context(start, end)` (daily context + always resets the Calendar
sync token, since the incremental sync can't otherwise backfill deleted rows), and
`wipe_metrics()` = both (full reset)

**Coach Learnings:** `get_learnings()` (each record annotated with a computed
`dormant` flag and its `proposed_confidence`), `get_learning_evidence(id)`,
`add_learning(text, sports='general', confidence='tentative')` (seeds a synthetic
basis sustaining the level), `update_learning(id, text)`, `delete_learning(id)`,
`apply_learning_deltas(deltas, available_weeks=None, source='reflect')`,
`recompute_all_confidence()`, `derive_staleness_proposals(auto=False)`,
`demote_learning(id)`, `keep_learning(id)`, `wipe_learnings()`. Module-level
helpers: `normalize_sports()`, `valid_confidence()`, `confidence_rank()`,
`step_down()`, `derive_confidence()`, `learning_is_dormant()`; constants
`CONFIDENCE_LEVELS` / `LEARNING_STALENESS_DAYS` / `RETIRE_PROPOSAL`. The
evidence/confidence/decay model these implement is the **canonical** description in
[§3](#3-coach-package-architecture); tables `coach_learnings` + `learning_evidence`
in [§5](#5-database-schema). Periodization strategy lives in `macrocycles`, not here.

**Analysis Cache:** `save_analysis_cache(horizon, fingerprint, window_start,
window_end, reconstruction)` (upsert, one row per `horizon`),
`get_analysis_cache(horizon)` (returns the row with `reconstruction` parsed
from JSON, or `None`), `wipe_analysis_cache()`. Caches a backward-evaluation
reconstruction keyed by an evidence fingerprint so a re-run over unchanged data
reuses it instead of re-calling the LLM (table `analysis_cache`; see
DESIGN_backward_evaluation.md §5.1).

**Macrocycles/Mesocycles:** `save_macrocycle` (**supersedes** the existing active
version for the objective — marks it `superseded`, keeps it — then inserts the new
active one), `get_macrocycle_for_objective(objective_id)` (the active version only),
`get_macrocycle(id)` (any version by id), `get_macrocycle_versions(objective_id)`
(all versions, newest first), `get_previous_macrocycle(objective_id, before_id=None)`
(walk-back navigation for rollback), `set_active_macrocycle(id)` (promote a version,
superseding the rest), `get_last_macrocycle()`,
`get_mesocycles_for_macrocycle(macrocycle_id)`, `get_mesocycle(id)`,
`update_macrocycle_feedback(id, feedback)`, `update_mesocycle_feedback(id, feedback)`,
`update_macrocycle_config_hash(id, hash)`, `delete_macrocycle_for_objective` (all
versions), `wipe_plans`. Plan versioning + rollback: DESIGN_plan_rollback.md.

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
| `pushed_signature`     | TEXT       | Hash of calendar-relevant fields captured at the |
|                        |            | last successful push. Freshness is *derived* by  |
|                        |            | comparing it to the live hash — not stored. NULL  |
|                        |            | ⟺ never pushed (see `trainmate.calendar_state`).  |
| `modification_reason`  | TEXT       | Modification axis: non-NULL ⟺ modified. A short   |
|                        |            | per-workout note. Kind (adapted/swapped/replaced) |
|                        |            | derived via `trainmate.modification_state`.       |
| `adaptation_summary`   | TEXT       | Long batch-level adapt rationale, stamped on      |
|                        |            | every session of one `workout adapt` run and      |
|                        |            | deduplicated when listed. NULL on swaps/manual/    |
|                        |            | legacy rows.                                      |
| `google_event_id`      | TEXT       | Non-NULL ⟺ a Calendar event exists (may be stale)|
| `duration_minutes`     | INTEGER    |                                                  |
| `rpe`                  | INTEGER    | Expected RPE 1–10                                |
| `tss`                  | INTEGER    | Expected Training Stress Score                   |
| `removed`              | INTEGER    | 0/1 — soft-delete: 1 ⟺ removed via `workout rm`  |
| `removed_reason`       | TEXT       | Athlete's reason for removal (`--reason`); optional |
| `original_date`        | TEXT       | YYYY-MM-DD — set once on creation, never            |
|                        |            | overwritten (COALESCE); used to detect swap-back   |
| `source`               | TEXT       | Origin axis, fixed at creation: `generated`        |
|                        |            | (plan/generate, or a session adapt newly adds) or  |
|                        |            | `manual` (`workout add`). NULL on legacy rows.     |
|                        |            | Orthogonal to adaptation — adapting keeps origin.  |
| `macrocycle_id`        | INTEGER    | Plan version this row belongs to, fixed at creation|
|                        |            | (never overwritten). Used by `plan rollback` to    |
|                        |            | resurrect a version's workouts. NULL on legacy rows.|
| `archived_at`          | TEXT       | Plan-version axis: non-NULL ⟺ archived (belonged to|
|                        |            | a superseded plan version). Hidden from reads by   |
|                        |            | default, event torn down. Distinct from `removed`. |
|                        |            | See DESIGN_plan_rollback.md.                       |

#### Workout state = four orthogonal axes (not one enum)

Four independent facts, none stored as a single status string. The first three are
derived; the fourth (archived) is a stored lifecycle flag. See [§15](#15-design-rationale--history)
for why the rest are derived rather than stored.

**1. Modified?** = `modification_reason IS NOT NULL`. The *kind* is derived by
`trainmate.modification_state.modification_status(workout)` — there is no stored
kind flag. Checked **in order**:

| Result | Condition |
|--------|-----------|
| `unmodified` | `modification_reason IS NULL` |
| `adapted` | `adaptation_summary IS NOT NULL` (⟺ from `workout adapt`) |
| `swapped` | reason starts `SWAP_REASON_PREFIX` (`"Swapped from "`) **or** `date != original_date` |
| `replaced` | reason starts `MANUAL_REPLACE_REASON_PREFIX` (`"Manually replaced previous "`) **or** `source == 'manual'` |
| `adapted` | catch-all (a legacy adapt: rationale in `modification_reason`, no summary) |

  - `adapted` takes precedence over `swapped` — a session adapted *then* swapped
    keeps its summary and still reads `adapted`.
  - `modification_reason` is set directly, so a swap-back to `original_date` can
    clear it to NULL. `adaptation_summary` is COALESCE-preserved on re-save (only
    an adapt writes it, never cleared).
  - The two prefix constants are shared by `coach/service.py`'s swap and manual-add
    writers so reader and writer can't drift (a test guards this).
  - `workout list` markers `[ADAPTED]`/`[SWAPPED]`/`[REPLACED]` come from this
    accessor. *(The Calendar event summary's `[Adapted]`/before-after framing in
    `google_calendar.sync_workout` is separate — it keys on whether the
    **description** changed, not on the modification kind.)*

**2. Calendar state** = derived by `trainmate.calendar_state.calendar_status(workout)`:

| Result | Condition |
|--------|-----------|
| `unpushed` | `google_event_id IS NULL` |
| `synced` | `pushed_signature == ` current calendar-field hash |
| `stale` | `pushed_signature != ` current hash |

  - `pushed_signature` is the hash of calendar-relevant fields captured at the last
    successful push; `mark_workout_pushed` is its **only** writer. Any edit through
    any path leaves it untouched, so the row reads `stale` automatically.
  - The signature excludes `rpe` (never reaches Calendar), so editing RPE does not
    mark a workout for re-push.
  - Push eligibility = `calendar_status != 'synced'`; calendar cleanup keys on
    `google_event_id`.
  - **Backward adherence marking** is the past-looking counterpart to the forward
    push. For each *strictly past* planned workout that already has an event, it
    re-renders the event with an adherence verdict from `adherence.classify_adherence`
    — a `[Done]`/`[Missed]`/`[Partial]`/`[Rest OK]`/`[Rest broken]` title tag and an
    `Adherence:` description header (status, actual effort, discrepancy notes). It
    reuses `sync_workout(workout, adherence=...)`, so the `pushed_signature` re-stamp
    is unchanged (the workout fields are still fully represented, so the row stays
    `synced`, not `stale`). Today/future events are skipped (a not-yet-done session
    would falsely read as missed). It runs **by default** on both `data pull` (once
    fresh activity data lands, over the pulled range) and `workout compare` (over the
    compared window); pass `--no-mark` to either to skip it. Best-effort — a Calendar
    failure never breaks the command, and it is a no-op when no calendar is configured.
    The shared pipeline (`mark_adherence_range` → `mark_adherence_from_results`) lives
    in `cli/common.py` so the two entry points can't drift. Re-marking re-pushes the
    (idempotent) event each run; no per-event skip signature is kept (the default
    windows are small enough that it isn't worth the state).

**3. Removed?** = `removed = 1` — a **soft delete**. `workout rm` calls
`mark_workout_removed` (`removed=1`, preserves `google_event_id`; content change reads
as `stale`) and marks the Calendar event deleted — the row is **kept**. `get_workouts`
excludes removed rows by default (`include_removed=False`), so they vanish from
`workout list`/`compare`, adherence, generation, and the web API, and are **not**
counted as misses. The adapt flow re-fetches them (`include_removed=True`) and surfaces
them to the coach as deliberate cancellations (with the optional `removed_reason` from
`workout rm --reason`), distinct from a miss. `save_workout`'s upsert resets
`removed=0`/`removed_reason`, so re-generating or adapting onto a removed
`(date, sport_type)` slot revives it.

**4. Archived?** = `archived_at IS NOT NULL` — the **plan-version** axis, orthogonal to
the three above. Set by `archive_future_workouts` when a regeneration or `plan rollback`
displaces the current plan's workouts; the row is **kept** (tagged with its
`macrocycle_id`) so the matching rollback can resurrect it via
`restore_macrocycle_workouts`, but its Calendar event is torn down and its handle
cleared. `get_workouts`/`get_workout` exclude archived rows by default
(`include_archived=False`) and `save_workout`'s upsert ignores them, so archived
workouts are invisible to listings, adherence, generation, adaptation, and the calendar
push until restored. See DESIGN_plan_rollback.md.

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
Per-source sync progress, one row per `key`. The `garmin` row holds the pull
watermark: `through_date` is the forward high-water mark (local YYYY-MM-DD) and
only ever advances; `last_pull_utc` is an instant compared against now for the
freshness interval. The `calendar_context` row instead holds `sync_token` (the
opaque Calendar `nextSyncToken`) with `through_date` NULL. Each source populates
only the columns it uses. See §8 (Data Pull), §13 (Daily Context),
`DESIGN_garmin_direct_pull.md`, and `DESIGN_calendar_context_ingest.md`.

| Column          | Type    | Notes                                            |
|-----------------|---------|--------------------------------------------------|
| `key`           | TEXT PK | Source key: `garmin` or `calendar_context`       |
| `through_date`  | TEXT    | Garmin forward high-water mark (local YYYY-MM-DD)|
| `last_pull_utc` | TEXT    | ISO instant of last successful sync              |
| `sync_token`    | TEXT    | Calendar `nextSyncToken` (calendar_context row)  |

### daily_\context
External daily context signals (alcohol, sleep, stress, …) ingested from tagged
Google Calendar events. TrainMate is domain-agnostic: `metric` is an opaque
category and `value` an optional numeric magnitude. Reconciled by
`google_event_id` (upsert on edit, delete on cancellation). See §13 and
`DESIGN_calendar_context_ingest.md`.

| Column            | Type        | Notes                                          |
|-------------------|-------------|------------------------------------------------|
| `id`              | INTEGER PK  | Autoincrement                                  |
| `date`            | TEXT        | YYYY-MM-DD the signal applies to               |
| `metric`          | TEXT        | Opaque category, e.g. `alcohol`                |
| `value`           | REAL        | Optional numeric magnitude (NULL if untagged)  |
| `text`            | TEXT        | Human blurb (summary/description) for the coach|
| `google_event_id` | TEXT UNIQUE | Calendar event id — reconciliation key         |
| `updated`         | TEXT        | Event `updated` RFC3339 (debug)                |

### coach_\learnings
Discrete, addressable athlete-observation records. Confidence is **app-computed**
from the `learning_evidence` basis (below), not asserted by the LLM. Full model:
[§3](#3-coach-package-architecture).

| Column                | Type       | Notes                                                  |
|-----------------------|------------|--------------------------------------------------------|
| `id`                  | INTEGER PK | Referenced by `revise`/`reinforce`/`contradict`/`retire` deltas |
| `text`                | TEXT       | LLM-generated observation                              |
| `sports`              | TEXT       | Comma-separated sport scope, or `general` (default)    |
| `confidence`          | TEXT       | App-computed: `tentative` / `moderate` / `established` |
| `proposed_confidence` | TEXT       | Pending, human-confirmable **downgrade** (`retire` = propose retirement); NULL when none |
| `created_at`          | TEXT       | ISO timestamp                                          |
| `updated_at`          | TEXT       | ISO timestamp; last content/metadata change            |
| `last_reinforced_at`  | TEXT       | ISO timestamp; drives decay → `dormant` (see §3); refreshed only by a *new* supporting week or a staleness demotion |

### learning\_evidence
The per-learning **evidence basis**: the distinct training **weeks** backing each
learning, tagged supporting/contradicting; `confidence` is a pure function of it.
The `UNIQUE(learning_id, week_commencing, polarity)` constraint is the dedup
guarantee (re-citing a counted week is an `INSERT OR IGNORE` no-op). Full model:
[§3](#3-coach-package-architecture); DESIGN_evidence_based_confidence.md §5.

| Column            | Type       | Notes                                              |
|-------------------|------------|----------------------------------------------------|
| `id`              | INTEGER PK |                                                    |
| `learning_id`     | INTEGER    | FK → coach_learnings.id (ON DELETE CASCADE)        |
| `week_commencing` | TEXT       | YYYY-MM-DD (Monday) — the evidence anchor          |
| `polarity`        | INTEGER    | +1 supporting · −1 contradicting                   |
| `source`          | TEXT       | `reflect` \| `bootstrap` \| `plan` \| `manual` \| `migration` |
| `created_at`      | TEXT       | ISO timestamp                                      |

`UNIQUE(learning_id, week_commencing, polarity)`

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
| `goals_snapshot`  | TEXT                  | JSON of the goals the plan was generated from    |
|                   |                       | (same cleaned data the hash covers); NULL on     |
|                   |                       | plans predating the column. Shown by `plan show` |
|                   |                       | and the web strategy card.                       |
| `lifeevents_snapshot` | TEXT              | JSON of the life events the plan was generated   |
|                   |                       | from; NULL on pre-snapshot plans                 |
| `created_at`      | TEXT                  | ISO timestamp                                    |
| `feedback`        | TEXT                  | Athlete feedback for next replanning             |
| `status`          | TEXT                  | `active` or `superseded`. Exactly one active     |
|                   |                       | version per objective; readers filter on active. |
|                   |                       | Defaults to `active` (legacy rows). See           |
|                   |                       | DESIGN_plan_rollback.md.                          |
| `superseded_at`   | TEXT                  | ISO timestamp a version stopped being active;     |
|                   |                       | NULL while active.                               |

Regenerating a plan **supersedes** the prior version (kept) rather than deleting it, so
`plan rollback` can restore an earlier version and its workouts (DESIGN_plan_rollback.md).

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
| `fingerprint`    | TEXT       | Hash of activity-id set + metrics + overlapping life events + window |
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
assign `test_db` to `trainmate.coach.service.db`, `trainmate.garmin.db`, etc.
before importing the singletons). Because the logic now lives in submodules,
patch the name where it is *used* — e.g. `trainmate.coach.engine.openrouter_client`,
`trainmate.coach.service.db`.

---

## 7. CLI Commands Reference

Invoked as `python trainmate_cli.py [--llm-model MODEL] <command> [subcommand] [args]`.
`trainmate_cli.py` holds only `main()` (the argparse dispatcher) and the
patchable singletons; the handler functions, named
`run_<command>_<subcommand>()`, live in the `trainmate/cli/` package
(one module per command family: `status`, `goals`, `lifeevents`, `context`,
`learnings`, `plans`, `workouts`, `data`).

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
| `context`    | `add`        | `c a`    | Author daily-context signal(s) (`text` or `-l/--label`, `-m METRIC`, `--value N`, `--from`, `--until`; one tagged all-day event per day, prompts if omitted) |
| `context`    | `rm`         | `c r`    | Remove signal(s) by ID(s), or by `--from`/`--until`/`-m` (deletes calendar event + local row) |
| `context`    | `list`       | `c l`    | List signals (`-m METRIC`, `--from`, `--until`; default window `metrics_lookback_days`) |
| `context`    | `list-metrics` | `c lm` | Show distinct metrics in use with counts and date span                  |
| `learnings`  | `list`       | `l`      | Show coach learnings (`--sport`, `--confidence`, `--dormant`)            |
| `learnings`  | `show`       | —        | Show a learning's full text + per-week evidence basis by ID             |
| `learnings`  | `edit`       | —        | Edit a learning's text by ID                                            |
| `learnings`  | `rm`         | `r`      | Delete a learning by ID                                                 |
| `learnings`  | `demote`     | —        | Accept a pending confidence downgrade by ID                            |
| `learnings`  | `keep`       | —        | Dismiss + affirm a pending downgrade by ID                             |
| `learnings`  | `wipe`       | —        | Delete all coach learnings                                             |
| `plan`       | `generate`   | `p g`    | Generate/reuse macrocycle+mesocycles (`-f` to force, `--goal ID`)        |
| `plan`       | `show`       | `p s`    | Show active periodization plan                                           |
| `plan`       | `rm`         | `p d`    | Delete plan for a goal ID                                                |
| `plan`       | `feedback`   | `p f`    | Add feedback (`--macro` or `--meso ID`, `--goal ID`, text;              |
|              |              |          | `--edit` opens `$EDITOR` seeded with current feedback)                  |
| `plan`       | `wipe`       | —        | Delete all plans                                                         |
| `workout`    | `list`       | `w l`    | Show planned workouts (`--type TYPE`, `--days N`,        |
|              |              |          | `--weeks N`, `--from DATE`, `--until DATE`,              |
|              |              |          | `--from-mesocycle`, `--until-mesocycle [ID]`,            |
|              |              |          | `--mesocycle [ID]`, `--goal [ID]`, `--removed`).         |
|              |              |          | Defaults to showing workouts from today for 7 days.      |
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
|              |              |          | `--until-goal [ID]`, `--until-mesocycle ID`). With no                    |
|              |              |          | horizon flag, generates `config.workout_generation_span_days`                   |
|              |              |          | ahead (28 default). Saves to DB only; run `push` after.                  |
| `workout`    | `rm`         | `w r`    | Soft-remove workout by ID (`--reason TEXT` required);   |
|              |              |          | marks `removed`, updates Calendar event to be marked    |
|              |              |          | deleted; kept in DB, hidden from list/compare, shown    |
|              |              |          | to coach as a cancellation (with the reason)            |
| `workout`    | `restore`    | `w res`  | Restore soft-removed workout by ID. Clears `removed`    |
|              |              |          | flags and syncs to Calendar to remove `[Deleted]` mark. |
| `workout`    | `adapt`      | `w a`    | Run daily adaptation check (`--date YYYY-MM-DD`, `-y` auto-apply)        |
| `workout`    | `push`       | `w p`    | Sync planned workouts to Google Calendar. Defaults to                    |
|              |              |          | today onward; pushes only unsynced workouts unless                       |
|              |              |          | `-f`/`--force` re-pushes already-synced ones.                            |
| `workout`    | `swap`       | `w s`    | Swap workouts between two dates (`<date1> <date2>`)     |
|              |              |          | or two IDs (`--id1 X --id2 Y`). Requires `--reason`.    |
|              |              |          | Runs recovery checks (consecutive hard days, weekly     |
|              |              |          | load spikes, mesocycle crossings) and prompts on        |
|              |              |          | warnings unless `-f`/`--force`. Syncs to Calendar       |
|              |              |          | unless `--no-sync`. `--reason` is folded into the       |
|              |              |          | `modification_reason` and shown to the coach.           |
| `workout`    | `wipe`       | —        | Delete all workouts                                                      |
| `data`       | `pull`       | `d p`    | Fetch Garmin activities/metrics and Google Calendar context (`--days`/`--from`/`--until`/`--metrics-only`/`--activities-only`/`--sleep`). Defaults to the last 2 days ending today. |
| `data`       | `bootstrap`  | `d b`    | Cold-start reconstruction over the full backlog; seeds  |
|              |              |          | evidence-based learnings, sets the reflect watermark    |
|              |              |          | (`--from`, `--until`, `--days`, `--weeks`, `--context`, |
|              |              |          | `--force`, `--inspect-only`, `--auto`). No date filter →|
|              |              |          | window auto-detected (since previous goal, else 12 wk). |
| `data`       | `reflect`    | `d r`    | Incremental analysis since the reflect watermark;       |
|              |              |          | updates learnings + resolves pending confidence         |
|              |              |          | demotions (same flags as `bootstrap`). `--auto`:        |
|              |              |          | unattended — staleness demotions auto-apply,            |
|              |              |          | contradiction ones stay queued.                         |
| `data`       | `show-metrics` | `d sm` | Show athlete metrics over a date range. Defaults to a    |
|              |              |          | 7-day lookback ending today. Supports standard date     |
|              |              |          | range options, `-a`/`--all` (shows all data),           |
|              |              |          | `--no-pull` to bypass Garmin sync, and `--csv`.           |
| `data`       | `show-activities` | `d sa` | Show completed activities over a date range. Defaults  |
|              |              |          | to a 7-day lookback ending today. Supports date         |
|              |              |          | options, `-a`/`--all`, `--type` filter, `--no-pull`,      |
|              |              |          | and `--csv`.                                              |
| `data`       | `backfill-tss` | —      | Recompute the measured `tss` for all stored activities under the current zone model (no Garmin calls), then refresh derived workload |
| `data`       | `wipe`       | `--garmin`, `--calendar`, `--from/--until/--days`, `-y` | Delete cached data. No scope flag = everything (Garmin evidence + daily context) and reset watermarks; `--garmin`/`--calendar` narrow the scope; date flags restrict to a window |

---

## 8. Web API Endpoints

Flask server at `trainmate_web.py`, runs on port 5000. Static files served from
`static/`.

The API tracks the CLI feature set (mirrors `data`/`workout`/`learnings` flows);
all mutating handlers stay thin wrappers over `coach_service`/`db`/`calendar_syncer`.
It remains a **pure reader** w.r.t. Garmin (never calls `ensure_data`/`pull`); the
calendar *writes* it does perform (workout sync, swap/remove/restore/adapt-apply)
use the non-interactive service account.

The **front-end** (`static/index.html` + `static/app.js`) is organized into four
top-level tabs — **Dashboard** (status/actions/metrics/strategy + goal & life-event
CRUD + plan delete), **Workouts** (date/sport/removed-filtered list with derived
state markers, manual add, swap, remove/restore, and the compare/adherence view),
**Learnings** (filterable manager with per-week evidence, edit, demote/keep), and
**History** (read-only activities/metrics/daily-context tables) — and surfaces every
endpoint below. The deliberately-CLI-only flows (`data bootstrap`/`reflect` and
Garmin `data pull`) are not web actions; the UI shows the command to run instead
(interactive / MFA-bound, DESIGN_garmin_direct_pull.md §11). `GET /api/workouts`
annotates each row with derived `calendar_status` + `modification_status` (via
`_annotate_workout`) so the front-end renders the same `[SYNCED]`/`[ADAPTED]`/etc.
markers as `workout list` without re-deriving the rules (§5).

| Method      | Path                            | Description                                  |
|-------------|---------------------------------|----------------------------------------------|
| GET         | `/api/status`                   | Active goal, latest metrics, coach learnings |
|             |                                 | (under `coach_learnings.learnings` +         |
|             |                                 | `.summary`), macrocycle+mesocycles,          |
|             |                                 | `sync_state` (data freshness)                |
| GET/POST    | `/api/objectives`               | List all / create objective                  |
| DELETE/PUT  | `/api/objectives/<id>`          | Delete or update objective                   |
| GET/POST    | `/api/life-events`              | List upcoming / create life event            |
| DELETE/PUT  | `/api/life-events/<id>`         | Delete or update life event                  |
| GET/POST    | `/api/workouts`                 | List workouts (`?start_date=&end_date=`,     |
|             |                                 | `?sport_type=&include_removed=`) / add a session|
|             |                                 | (`workout add`: `{date, sport_type, title,   |
|             |                                 | description, duration_minutes?, rpe?, tss?,  |
|             |                                 | reason?}`). Listed rows carry derived         |
|             |                                 | `calendar_status` + `modification_status`.    |
| GET         | `/api/workouts/compare`         | Plan-vs-actual adherence (`workout compare`); |
|             |                                 | pure reader, no `ensure_data`. `?start_date=&|
|             |                                 | end_date=&sport=` (default 14-day lookback,   |
|             |                                 | end capped at today) → `{filters, days[],     |
|             |                                 | discrepancies[], informational[]}`            |
| POST        | `/api/workouts/<id>/remove`     | Soft-remove a workout (`{reason?}`)          |
| POST        | `/api/workouts/<id>/restore`    | Restore a soft-removed workout               |
| POST        | `/api/workouts/swap`            | Swap two workouts (`{ops:[{id,new_date}],    |
|             |                                 | reason, force?, no_sync?}`); returns          |
|             |                                 | `{warnings}` unapplied unless `force`         |
| POST        | `/api/plan`                     | Generate periodization plan (`{goal_id?}`)   |
| GET         | `/api/plan/versions`            | List plan versions for a goal                |
|             |                                 | (`?goal_id=`; active + superseded)           |
| POST        | `/api/plan/rollback`            | Restore a plan version + its workouts        |
|             |                                 | (`{goal_id?, version?}`; DESIGN_plan_rollback)|
| DELETE      | `/api/plan/<goal_id>`           | Delete plan for goal (all versions)          |
| POST        | `/api/macrocycles/<id>/feedback`| Save macrocycle feedback (`{feedback}`)      |
| POST        | `/api/mesocycles/<id>/feedback` | Save mesocycle feedback (`{feedback}`)       |
| POST        | `/api/workouts/generate`        | Generate workouts (`{goal_id?}`); eager —    |
|             |                                 | archives old + pushes new to Calendar        |
| POST        | `/api/adapt`                    | Run daily adaptation check (read-only;       |
|             |                                 | `{date?}` → `{reason, change_needed,         |
|             |                                 | workouts}`)                                   |
| POST        | `/api/adapt/apply`              | Apply proposed adaptations + sync            |
|             |                                 | (`{workouts, reason}`)                        |
| POST        | `/api/workouts/push`            | Sync workouts to Google Calendar             |
| GET         | `/api/learnings`                | List coach learnings (`?sport=&confidence=&  |
|             |                                 | dormant=`) + `summary`                        |
| GET         | `/api/learnings/<id>/evidence`  | Per-week evidence basis (supporting/contra)  |
| PUT/DELETE  | `/api/learnings/<id>`           | Edit text / delete a learning                |
| POST        | `/api/learnings/<id>/demote`    | Accept a pending confidence downgrade        |
| POST        | `/api/learnings/<id>/keep`      | Dismiss + affirm a pending downgrade         |
| GET         | `/api/activities`               | Completed activities (`?start_date=&end_date=`)|
| GET         | `/api/daily-context`            | Daily-context signals (`?start_date=&end_date=`)|
| POST        | `/api/metrics/pull`             | Returns 409 — Garmin pulls are CLI-only      |
| GET         | `/api/metrics`                  | Cached metrics (range, else last 30 days)    |


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
| `workout_generation_span_days` | int  | Default horizon for `workout generate` (default: 28)         |
| `minor_activity_load_threshold`    | float| Workload score below which an activity is "minor"            |
|                         |      | (default: 25). Controls rest-day violations and unplanned    |
|                         |      | activity visibility (shown as gray/minor if below threshold,  |
|                         |      | yellow/unplanned if above). Mismatch tolerance for planned   |
|                         |      | workouts is dynamically computed from expected workload.     |
| `rpe_divergence_ratio`  | float| sRPE-load ÷ measured-load above which a session is flagged   |
|                         |      | to the coach as "felt harder than measured" (default: 1.5;   |
|                         |      | set very high to disable)                                    |
| `learning_confidence_thresholds` | dict | Distinct net supporting weeks to reach each confidence |
|                         |      | level: `{moderate: 3, established: 5}` (defaults). Tentative ≥1 |
|                         |      | and proposed-retirement ≤0 are fixed. Re-levels on recompute. |
| `user_profile`          | dict | Must contain `lthr` or `ftp` (see below)                     |

`user_profile` keys: `name`, `birth_year`, `max_hr`, `lthr`, `ftp`,
`weekly_target_hours`, `sport_preferences`, `chronic_injuries`, `preferences`,
`equipment`, `weekly_schedule`. `weekly_schedule` maps day names to
`{total_available_hours, max_sessions, certainty_percent, equipment}`.

---

## 10. Key Data Flows

### Plan Generation (`plan generate`)
1. `CoachService.plan_generate()` fetches active objectives +
   life events.
2. Computes `goals_hash`, `lifeevents_hash`, `config_hash`.
3. If existing macrocycle has matching hashes and `force=False` → reuse.
4. Otherwise: builds a read-only **planned-vs-actual review** of the prior plan
   via `_build_prior_training_context()` (Option A — anchored on the prior
   plan's elapsed mesocycle windows, plus the cached reconstruction's summary,
   reverse-engineered macro/mesocycle blocks, and physiological insights;
   written to no `feedback` field), prints it, and passes it as
   `prior_training_text` into `CoachEngine._plan_generate_strategy()` →
   LLM → `{strategy, mesocycles}`.  See DESIGN_backward_evaluation.md §6.
5. If timeline > 24 weeks: calls `CoachEngine._generate_intermediate_goals()`
   first, saves intermediate objectives, then re-runs with the first goal.
6. Saves new macrocycle + mesocycles to DB (old ones deleted via
   `save_macrocycle`).

### Workout Generation (`workout generate`)
1. CLI resolves the generation horizon (end date) from flags in priority order:
   `--days` / `--weeks` → `--until DATE` → `--until-goal [ID]` →
   `--until-mesocycle ID` → `config.workout_generation_span_days` (default 28).
2. `CoachService.workout_generate(end_date=...)` verifies a macrocycle exists,
   computes `num_days` from `(end_date − today)`.
3. Fetches metrics history (last `metrics_lookback_days` days) + baseline.
4. Calls `CoachEngine._workout_generate_logic(num_days=...)` → LLM →
   `{reasoning, workouts[]}`. **Read-only** w.r.t. coach learnings (see
   [§3](#3-coach-package-architecture)).
5. Clears future unsynced workouts (`clear_future_workouts`), then saves new
   workouts.

### Daily Adaptation (`workout adapt`)
1. `CoachService.workout_adapt()` fetches metrics + planned workouts + completed
   activities in window. Workouts in the window are fetched with
   `include_removed=True` and partitioned into active (planned) vs `removed`;
   removed ones are passed to `_workout_adapt_logic` and rendered in the prompt as
   deliberate cancellations (not misses). It also fetches the window's
   `daily_context` rows (reaching one day before the metrics window, since recovery
   lags the signal) so the LLM can attribute a depressed morning to lifestyle noise
   (alcohol/poor sleep the day before) vs genuine training fatigue. It may still ease or
   **reschedule** today's hard session for acute readiness, but must not read a
   lifestyle-suppressed morning as evidence the *block* is too hard (no permanent cut to
   planned volume, not counted as training fatigue). This is what makes the
   quantitative-context learning actually move a decision rather than stay inert at the
   daily load call (DESIGN_quantitative_context_impact.md §6.1).
2. `analyze_adherence()` (`adherence.py`) computes discrepancies (misses,
   duration/load mismatches, rest violations) over the **active** workouts only —
   removed workouts never count as misses.
3. Finds active mesocycle for the target date → sets `meso_end_date` for
   adaptation range.
4. Calls `CoachEngine._workout_adapt_logic()` → LLM → `{change_needed, reason,
   adapted_workouts[]}`. **Read-only w.r.t. coach learnings** (see
   [§3](#3-coach-package-architecture)).
5. Returns `(reason, proposed_workouts)` — caller decides whether to apply.
6. If applied: `workout_adapt_apply()` deletes overridden calendar events + DB
   rows, saves adapted workouts (each carrying its `modification_reason` +
   `adaptation_summary`, so they read as `adapted`; see [§5](#5-database-schema)),
   syncs to Calendar.

### Data Pull (`data pull`) and auto-ensure

Data is pulled **directly from Garmin Connect** (`trainmate/garmin.py`). Full
design: `DESIGN_garmin_direct_pull.md`.

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
   absent) and feed `format_completed_activities` in `coach/formatting.py` verbatim.

**Auto-ensure.** Read-side commands call `garmin.ensure_data(start, end)` at
entry (idempotent per process via an in-memory memo). It pulls the
28-day-padded required window where the gap is small/recent and **prints a
copy-pastable `data pull` command for large backfills** (cold start, big
forward/backward gaps), always continuing with cached data. These commands support
the `--no-pull` option to bypass the sync check and read purely from the local SQLite
cache. Calendar dates use the machine-local timezone (`util.today_str`/`today_date`);
stored instants stay UTC. The web app never calls this — it is a pure reader (see §1).

### Data Analysis (`data bootstrap` / `data reflect`)
Both commands share the `CoachService._run_workout_analysis()` core; they differ
only in how the window is resolved and whether they set vs. advance the reflect
watermark (stored in `sync_state` under the `reflect` key).
- **`data bootstrap`** (cold-start, run once): resolves a wide window
  automatically from active/preceding goals (else 12 weeks back) when no date
  filter is given, runs under horizon `long`, and **sets** the reflect watermark
  to the window end. This is the reconstruction `plan generate` reuses. Completion
  is recorded under the `bootstrap` `sync_state` key; a repeat run is detected and
  confirmed before re-running (`--force` proceeds, `--auto` skips, `--inspect-only`
  is never gated), since re-running re-pays for the LLM pass and resets the baseline.
- **`data reflect`** (incremental): starts the window at the day *after* the
  reflect watermark (or an explicit date filter), runs under horizon `short`, and
  **advances** the watermark forward only. Because overlapping history is never
  re-ingested, repeated runs no longer ratchet confidence to `established`. With
  no watermark yet it falls back to a recent window and nudges toward
  `data bootstrap`; with no new evidence it returns early without an LLM call.

The shared core then:
1. Queries completed activities, physiological metrics, and life events
   overlapping the window.
2. Computes the evidence fingerprint (activities + metrics + overlapping life
   events) and checks `analysis_cache[horizon]`. If the fingerprint matches and
   `--force` is absent → returns the cached reconstruction (no LLM call). `--force`
   recomputes regardless.
3. Groups metrics and activities week-by-week using Monday-commencing ISO weeks,
   enriching each weekly summary (DESIGN_richer_analysis_evidence.md) with the
   life events overlapping that week (tagged `full`/`partial`), `avg_sleep_score`/
   `avg_stress`, and `vs_baseline_z` (deterministic rhr/hrv/sleep z-scores vs the
   rolling baseline, omitted when unsupported). All deterministic — no extra LLM
   call. The per-day z is computed by the shared `_day_response_z(metric_row,
   baseline)` static; `_week_response_features` averages it over the week.
3a. Builds `context_days` — episode-aligned external-signal impact rows
   (`_context_days`, DESIGN_quantitative_context_impact.md). Per signal category it
   clusters logged signal-days into *episodes* (runs separated by fewer than `k`
   drink-free days, `k = context_days_lookahead`, default 3) and emits, per episode,
   a `days` dose sequence ({date, value, day-of `load_tss`}) plus a
   `surrounding_mornings` strip spanning `(first − k + 1) … (last + k)` — each
   morning tagged with its preceding day's load and the `_day_response_z` recovery
   deltas, dropping any channel that duplicates the signal's own construct. Pure
   clustering + join + the existing z — **no statistics**. Unlike the weekly
   summaries this is fetched over the athlete's **full signal-day history** (not the
   analysis window), so the LLM sees the whole pattern even on an incremental
   reflect; categories below `context_days_min_signal_days` are dropped. The rows are
   recomputed each run (never stored as a learning); only the LLM's conclusion
   becomes a `coach_learnings` row, citing the in-window weeks the signal-days fall
   in (DESIGN_evidence_based_confidence.md §6).
4. Queries `CoachEngine._data_analyze_logic()` (now also handed `context_days`) -> LLM ->
   `{macrocycle_summary, inferred_macrocycle, inferred_mesocycles[],
   physiological_insights[], learning_updates[]}`.
5. Unless `--inspect-only`: applies `learning_updates` deltas — the LLM attributes
   each observation to the `week_commencing` weeks it was shown; the app validates
   them against the window, dedupes into each learning's evidence basis, and
   re-derives confidence (upgrade auto / downgrade proposed; see
   [§3](#3-coach-package-architecture)). It then caches the reconstruction in
   `analysis_cache`.
6. Unless `--inspect-only`: `_review_learning_proposals(auto)` sweeps staleness
   demotions and resolves pending downgrades — interactively (accept / keep / skip)
   or, under `--auto`, applying staleness directly while leaving contradiction
   proposals queued. See DESIGN_evidence_based_confidence.md §7.

---

## 11. Terminology: Plans vs. Workouts

- **Plan** = periodization strategy: one *active* macrocycle (per objective, with
  superseded versions kept) + mesocycle blocks.  Commands:
  `plan generate/show/versions/rm/rollback/feedback`. `plan versions` lists every kept
  version; `plan show --version <id>` renders a specific (e.g. superseded) one.
- **Workouts** = daily microcycle activities implementing the mesocycle focus.
  Commands: `workout generate/adapt/push/swap/add`. `workout generate` pushes to
  Calendar eagerly; `plan rollback` undoes a plan regeneration and its workouts
  (DESIGN_plan_rollback.md).

A `workout add` manually schedules a single session on a date (athlete-driven,
not coach-driven, and LLM-free). It **replaces** any existing same-sport workout
that day — or, with `--replace-day`, **every** session that day regardless of
sport — recording the overwritten session(s) on the new row the way an
adaptation does (`CoachService.workout_add`, §3): the replaced description
becomes `original_description` (rendered "Originally:" on the event) and each
replaced title + duration/TSS/RPE plus the athlete's `--reason` become the
`modification_reason` (rendered "Reason:"); other-sport entries are prefixed
with their sport. The same-sport row's `google_event_id` is carried over so its
existing Calendar event is updated in place; any other replaced sessions'
Calendar events are deleted. The row's `source='manual'` surfaces on the
Calendar event as a `[Manual]` summary prefix (composing with `[Adapted]` when
the manual add also replaced a session), so athlete-added sessions are
distinguishable at a glance from coach-generated ones. Load re-balancing of
surrounding days is intentionally **not** done here — run `workout adapt` for that.

A `workout swap` exchanges the dates of two workouts (or moves one onto an
empty rest day). Moved workouts get a `modification_reason` recording the swap
(`Swapped from X to Y`, plus the athlete's optional `--reason` appended as
`. Reason given: …`), so they read as `swapped` ([§5](#5-database-schema)), are
re-synced by `workout push`, and are visibly distinguished from untouched ones.
If a swap returns a workout to its
`original_date`, the `modification_reason` is cleared to `NULL` — the workout
is no longer considered modified. The `modification_reason` is surfaced to the
coach in the adaptation prompt (`format_planned_workouts_detailed`), so a swap informs
the coach symmetrically to how `workout rm`'s `removed_reason` does. Swaps are
validated first (`CoachService.workout_swap_validate`): the
new schedule is simulated and the user is warned about newly-created >2-day
high-intensity streaks, weekly load spikes (an ACWR proxy), and
mesocycle-boundary crossings.

Plan must be generated before workouts. Workouts cover a rolling window from
today whose length is controlled by the horizon flags on `workout generate`
(default: `workout_generation_span_days` in `config.yaml`, falling back to 28 days).
`replan()` calls `plan_generate` then `workout_generate` in one
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
`Workload = activity_load(act)` — the single fallback value above.

### Acute Workload (7 days)
Sum of daily workloads over the past 7 days (current day included).

### Chronic Workload (28 days)
Sum of workloads over past 28 days ÷ 4 (≈ average weekly load).

### ACWR
`ACWR = Acute / Chronic`
- < 0.8: under-training
- 0.8–1.3: "sweet spot"
- > 1.5: elevated injury risk

### Daily Readiness Signals
- HRV drops > 1 std below baseline mean → flag potential overtraining
- RHR rises > 1 std above baseline mean (min +3 bpm) → flag potential
  overtraining
- `workout adapt` acts on these signals over the rolling
  `metrics_lookback_days` window, but first discounts a depressed morning that a
  logged `daily_context` signal the day before explains (lifestyle noise, not training
  fatigue): today's session may still be eased or rescheduled for acute readiness, but
  the block's planned load is not cut on a non-training artifact.

### Science Guidelines Files
- `trainmate/science/` — built-in: `acwr.txt`, `periodization.txt`,
  `recovery_metrics.txt`
- `science/` — user-provided; empty by default; any `.txt` files added here are
  injected into every LLM prompt.

---

## 13. Daily Context (Calendar Ingest)

External daily signals the coach should factor in — alcohol, sleep quality,
stress, big meals, a heatwave — reach TrainMate through the **single existing
Google Calendar**, not through app-specific features. Producers write one all-day
event per signal-day, tagged in `extendedProperties.private`:
`source=trainmate-context` (the positive marker, configurable via
`calendar_context_tag`), `metric` (opaque category), and an optional numeric
`value`. Two producers exist: a separate syncer (out of scope, mirroring
`GarminScraper`) for spreadsheet-backed streams, and TrainMate's own `context`
command (outbound, below) for ad-hoc signals. Full specs:
`DESIGN_calendar_context_ingest.md` (ingest) and `DESIGN_context_authoring.md`
(authoring).

**Inbound flow:**

```
Calendar (tagged events) ──► google_calendar.sync_calendar_context
   ──► calendar_syncer.sync_context (syncToken, server-side filtered)
   ──► db.upsert/delete_daily_context_by_event ──► daily_context table
   ──► coach analysis weekly summaries (per-week `daily_context`)
```

- **Distinguishing events:** TrainMate writes workouts tagged `source=TrainMate`
  and reads only events tagged `source=trainmate-context` (or custom config); untagged events (real
  appointments) are never fetched (server-side `privateExtendedProperty` filter).
- **Sync, not append:** incremental via Calendar `syncToken` — edits upsert by
  `google_event_id`, cancellations delete. First run / expired token (HTTP 410)
  falls back to a full pull of all tagged events (no date horizon needed; the
  list is bulk and sparse). Token persisted in `sync_state[calendar_context]`.
- **Cadence:** rides along `data pull` (force) and the auto-ensure-before-read
  path (`garmin.ensure_data` → bridge `_sync_calendar_context`, throttled to the
  Garmin refresh window and memoized once per process). Best-effort: a missing
  calendar config or any Calendar error is swallowed with a warning. The gating
  and error handling live in `google_calendar.sync_calendar_context`; `garmin.py`
  only bridges to it via a guarded lazy import.
- **Coach use:** two complementary paths. (1) *Qualitative* — each week's summary
  carries a `daily_context` list (all rows, no collapsing) the LLM reads beside the
  metrics, the same way `life_events` contextualize anomalies. (2) *Quantitative*
  (`context_days`, step 3a above; DESIGN_quantitative_context_impact.md) — the
  optional numeric `value` is aligned per-episode against the bracketing mornings'
  recovery and day-of load, full-history, so the LLM can read dose-response,
  persistence, and the drink-and-hard-day confound. Both are hashed into the analysis
  evidence fingerprint so an added/edited/deleted signal invalidates the cached
  reconstruction.

**Outbound flow (first-party authoring — `context` command, alias `c`):** for
ad-hoc signals where standing up a syncer is overkill (a heatwave), the user can
author the same tagged events directly, since the private-property tag is
unsettable from the Calendar UI. `context add` writes one tagged all-day event
per day in a range (`add_context_event`, idempotent upsert-by-(date, metric)) and
mirrors the rows locally via `upsert_daily_context_by_event` so they appear before
the next pull. `context rm` deletes the **calendar event** (`delete_event`) before
the local row, so a full re-pull (`data wipe --calendar`) can't resurrect it.
`context list` (default window: `metrics_lookback_days`, the coach's context-read
window) and `context list-metrics` inspect what's recorded. Authored events are
indistinguishable from synced ones downstream — `sync_context`, the analysis
prompt, and the evidence fingerprint are untouched. Handlers: `cli/context.py`;
spec: `DESIGN_context_authoring.md`.

---

## 14. Testing

Tests use `unittest`. Run with:
```
venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

| File                           | What it tests                                                   |
|--------------------------------|-----------------------------------------------------------------|
| `tests/test_adaptation.py`     | `CoachService.workout_adapt()` end-to-end, swap validation/apply, |
|                                | `adherence.analyze_adherence()` (misses, tolerances, violations) |
| `tests/test_analysis.py`       | `data_bootstrap`/`data_reflect`: date resolution, weekly |
|                                | aggregation, cache reuse/force/inspect_only, learnings           |
|                                | injection, reflect watermark advance/skip, bootstrap re-run      |
|                                | guard, per-week life events + body-response z-scores             |
| `tests/test_cli.py`            | CLI command dispatch + output                                   |
| `tests/test_calendar.py`       | `calendar_syncer.sync_workout` event description formatting      |
| `tests/test_coach_format.py`   | `format_completed_activities` (HR/power-zone rendering)          |
| `tests/test_db.py`             | `Database` CRUD, evidence-based confidence (derivation, dedup,   |
|                                | week validation, contradiction/demote/keep, staleness,          |
|                                | grandfather migration), decay, `analysis_cache`                 |
| `tests/test_feedback.py`       | Feedback saving + use in replanning                             |
| `tests/test_periodization.py`  | `plan_generate`, `workout_generate`, hash logic, |
|                                | system-prompt building                                          |
| `tests/test_garmin.py`         | Garmin transforms (load model), zone parsing, watermark/         |
|                                | auto-ensure policy, recompute, `backfill_tss`                    |
| `tests/test_utils.py`          | `util.py` helpers (text wrapping, ANSI width, ACWR coloring)     |

Tests inject a fresh in-memory SQLite DB by assigning `test_db` to module-level
`db` variables *before* importing the singletons. `openrouter_client` is mocked
via `@patch`.

Integration / manual test scripts (not part of the test suite):
- `tests/run_integration.py`, `tests/run_calendar.py`

---

## 15. Design Rationale & History

*Why* the current design looks the way it does, and what it replaced. The
reference sections above describe only the current state; this section explains
the non-obvious choices. The `DESIGN_*.md` files hold the full deep-dives.

### Workout state: three derived axes, not a stored `status` enum
A single `status` string once conflated *modified*, *calendar*, and *removed*.
Each write path had to remember to set it correctly, and the three facts are
genuinely independent, so they're now **derived** (see [§5](#5-database-schema)):

- **Calendar axis** replaced a hand-maintained `synced` boolean that every write
  path had to remember to reset. Now `pushed_signature` is written only on a
  successful push; any later edit through any path leaves it untouched and the row
  reads `stale` automatically — no flag to forget. The signature deliberately
  excludes `rpe` (never reaches Calendar) so editing RPE no longer marks a workout
  for re-push.
- **Modification kind** is derived rather than stored for the same reason: a stored
  kind would reintroduce the hand-maintained denormalization the calendar rework
  removed.
- The `SWAP_REASON_PREFIX` / `MANUAL_REPLACE_REASON_PREFIX` prefixes exist because
  the column-only signals have blind spots: the `original_date` backfill set
  `original_date = date` on legacy rows, hiding any pre-column swap's move — the
  prefix recovers it. (`workout adapt` writes a free-form rationale, never these
  prefixes, so it is never misread as a swap/replace.)
- `adapted` takes precedence over `swapped`: a session adapted *then* swapped keeps
  its summary and still reads `adapted`.

### Coach learnings: confidence dropped `suppress_reinforcement`
Confidence is now a pure function of the per-learning evidence basis. Because
re-citing a counted `(week, polarity)` is a `UNIQUE`-constrained `INSERT OR IGNORE`
no-op, re-running / `--force` / overlapping windows cannot inflate confidence — so
the old `suppress_reinforcement` flag became unnecessary and was removed. Learnings
predating this model are grandfathered with a synthetic basis sized to sustain their
stored level (`db._grandfather_learning_evidence()`, source `migration`) so the first
recompute doesn't silently demote them. Full model:
[§3](#3-coach-package-architecture); DESIGN_evidence_based_confidence.md.

### Load model: single fallback, no additive blend
`Workload = activity_load(act)` picks one best-available method (power TSS → hrTSS →
sRPE); the former `TSS + RPE × hours` additive blend was removed because it
double-counted internal and external load. RPE is user-entered only and never
synthesised from power/HR. See [§12](#12-sports-science--coaching-mathematics).

### Data pull: direct from Garmin
Data is pulled directly from Garmin Connect; the former Google Sheets ingestion
path is gone. See DESIGN_garmin_direct_pull.md.
