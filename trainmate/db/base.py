import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional
from trainmate.config import config


class BaseDB:
    """Connection management and schema initialization shared by all DB mixins."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        """Initializes database path and sets up tables."""
        self.db_path: str = db_path or config.db_path
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Creates and returns a connection to SQLite database with constraints enabled."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _table_has_column(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
        """Returns True if `table` currently has `column` (via PRAGMA table_info)."""
        cursor.execute(f"PRAGMA table_info({table})")
        return any(row[1] == column for row in cursor.fetchall())

    def _init_db(self) -> None:
        """Initializes tables in database if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Objectives table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS objectives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    sport_type TEXT NOT NULL,
                    description TEXT,
                    priority INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'active' -- 'active', 'completed', 'archived'
                )
            """)

            # Drop the legacy `lifeevents` table (and its even older `life_events` name).
            # It was superseded by `constraints` (DESIGN_constraints.md §5) and kept
            # read-only for one release while the `lifeevent` forwarder deprecated out; both
            # the forwarder and the table are now removed together (§10 step 8). The one-off
            # row copy that migrated its contents into `constraints` has already run for any
            # DB that had rows; dropping here reclaims the space. `DROP … IF EXISTS` is
            # idempotent, so it is safe to leave in `_init_db` (which runs every invocation).
            cursor.execute("DROP TABLE IF EXISTS lifeevents")
            cursor.execute("DROP TABLE IF EXISTS life_events")

            # Unified directives — everything the athlete asks the coach to work around, at
            # any horizon (DESIGN_constraints.md §5). Supersedes `lifeevents`. A constraint is
            # advisory prose the coach reads (title/description) unless `rest = 1`, the single
            # deterministic edge: a full no-training window whose dates skip the LLM and are
            # forced to rest (rev 6 — replaces the old hard/soft × sport matrix and the opaque
            # `type` label). `replan` marks a directive escalated to plan-shaping (§7);
            # `source` records how the row was authored ('manual'|'message'|'lifeevent').
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS constraints (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_date  TEXT NOT NULL,
                    end_date    TEXT NOT NULL,
                    rest        INTEGER NOT NULL DEFAULT 0,
                    title       TEXT NOT NULL,
                    description TEXT,
                    replan      INTEGER NOT NULL DEFAULT 0,
                    source      TEXT,
                    created     TEXT
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_constraints_start ON constraints(start_date)"
            )

            # Simplify the constraint model to one deterministic `rest` flag (rev 6). The
            # hard/soft × sport enforcement matrix and the never-branched-on `type` label are
            # removed: the only combination that ever forced rest — `hard` with no sport —
            # becomes rest=1; every other row becomes advisory prose (rest=0), which is how
            # hard+sport and every soft row already behaved. Dropping binding/sport/type is
            # pure DDL, guarded by column presence, so it is idempotent and lives here. It does
            # change the constraints_hash of any plan-shaping (replan=1) constraint, so run
            # scripts/migrate_constraints_drop_binding.py once to backfill that hash and avoid a
            # one-time spurious "inputs changed" regen prompt (§7).
            cursor.execute("PRAGMA table_info(constraints)")
            ccols = [row['name'] for row in cursor.fetchall()]
            if 'rest' not in ccols:
                cursor.execute(
                    "ALTER TABLE constraints ADD COLUMN rest INTEGER NOT NULL DEFAULT 0"
                )
                cursor.execute(
                    "UPDATE constraints SET rest = 1 "
                    "WHERE binding = 'hard' AND sport IS NULL"
                )
            if 'binding' in ccols:
                cursor.execute("ALTER TABLE constraints DROP COLUMN binding")
            if 'sport' in ccols:
                cursor.execute("ALTER TABLE constraints DROP COLUMN sport")
            if 'type' in ccols:
                cursor.execute("ALTER TABLE constraints DROP COLUMN type")

            # Workouts table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS workouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    sport_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    original_description TEXT,
                    pushed_signature TEXT, -- hash of calendar fields at last push; NULL = never pushed. Freshness derived (see trainmate.calendar_state)
                    marked_signature TEXT, -- hash of calendar fields + adherence verdict at last `compare --mark`; NULL = never marked (see trainmate.calendar_state.adherence_signature)
                    modification_reason TEXT, -- non-NULL <=> adapted/swapped; short per-workout note
                    adaptation_summary TEXT, -- batch-level adapt rationale, shared across the batch
                    google_event_id TEXT,
                    removed INTEGER DEFAULT 0, -- 1 <=> soft-deleted via `workout rm`
                    removed_reason TEXT, -- athlete's reason for removal (optional)
                    source TEXT -- origin, fixed at creation: 'generated'|'manual' (NULL = legacy)
                )
            """)

            # Add new columns to workouts table if they don't exist
            try:
                cursor.execute(
                    "ALTER TABLE workouts ADD COLUMN duration_minutes INTEGER DEFAULT NULL"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN rpe INTEGER DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN tss INTEGER DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            # Soft-delete axis: a workout removed via `workout rm` is marked rather
            # than deleted, so it can be excluded from reads yet still surfaced to the
            # coach as a deliberate cancellation (distinct from a miss).
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN removed INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN removed_reason TEXT")
            except sqlite3.OperationalError:
                pass
            # Split the adaptation rationale into two axes: modification_reason holds a
            # short per-workout note, while adaptation_summary holds the long batch-level
            # reason shared across every session in one `workout adapt` run (deduplicated
            # at display). NULL on swaps/manual edits and on legacy adapted rows.
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN adaptation_summary TEXT")
            except sqlite3.OperationalError:
                pass
            # Origin axis: who authored the session, fixed at creation and never
            # overwritten — 'generated' (plan/workout generate, or a session adapt
            # newly introduces) or 'manual' (workout add). NULL on legacy rows
            # predating this column. Lets the coach and analysis treat
            # athlete-scheduled sessions distinctly from AI-authored ones; orthogonal
            # to the synced/adaptation/removed axes (adapting a session keeps its
            # origin — the change lands on modification_reason instead).
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN source TEXT")
            except sqlite3.OperationalError:
                pass
            # Plan-version axis (see DESIGN_plan_rollback.md): macrocycle_id tags the
            # plan version a workout was created under, and archived_at marks workouts
            # that belonged to a superseded plan version (set when a regeneration or a
            # `plan rollback` displaces them). Archived rows are hidden from every read
            # by default and have their Calendar event torn down, but are kept so that
            # rolling back to their plan version can resurrect them. Distinct from
            # `removed` (a deliberate athlete cancellation that still surfaces to the
            # coach). NULL on legacy rows.
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN macrocycle_id INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN archived_at TEXT")
            except sqlite3.OperationalError:
                pass
            # Migrate the very old conflated `status` enum into an orthogonal `synced`
            # flag. Only relevant for DBs predating the `synced` column; guarded on the
            # `status` column so it doesn't re-add `synced` after we drop it below.
            if self._table_has_column(cursor, "workouts", "status"):
                try:
                    cursor.execute("ALTER TABLE workouts ADD COLUMN synced INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass
                cursor.execute("UPDATE workouts SET synced = 1 WHERE status = 'synced'")
            # Replace the hand-maintained `synced` flag with a derived freshness signal:
            # store `pushed_signature` (hash of calendar-relevant fields) on each push and
            # compare against the live hash to tell unpushed/synced/stale apart (see
            # trainmate.calendar_state). Backfill: a row that was `synced=1` matched the
            # calendar at push time, so its current content is its signature; everything
            # else stays NULL (reads as unpushed/stale). Then drop the now-dead column.
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN pushed_signature TEXT")
            except sqlite3.OperationalError:
                pass
            if self._table_has_column(cursor, "workouts", "synced"):
                from trainmate.calendar_state import calendar_signature
                cursor.execute("SELECT * FROM workouts WHERE synced = 1")
                for row in cursor.fetchall():
                    cursor.execute(
                        "UPDATE workouts SET pushed_signature = ? WHERE id = ?",
                        (calendar_signature(dict(row)), row["id"]),
                    )
                cursor.execute("ALTER TABLE workouts DROP COLUMN synced")
            # Original date: remembers where a workout was first placed so that
            # swapping it back clears the modification flag.
            try:
                cursor.execute(
                    "ALTER TABLE workouts ADD COLUMN original_date TEXT"
                )
                cursor.execute(
                    "UPDATE workouts SET original_date = date "
                    "WHERE original_date IS NULL"
                )
            except sqlite3.OperationalError:
                pass
            # Adaptation recency axis: `adapted_at` is the UTC ISO timestamp of the most
            # recent `workout adapt` run that touched this row (NULL = never adapted), and
            # `adaptation_count` counts how many distinct adapt runs have eased it. Unlike
            # the *kind* of modification (derived in trainmate.modification_state), neither
            # is derivable from any other column — they are facts of WHEN/HOW-OFTEN — so the
            # daily adaptation surfaces them to the prompt as a real recency signal and
            # avoids compounding a fresh cut onto a session it only just eased (recovery
            # metrics lag, so the morning after an easing still looks depressed).
            # Creation timestamp: UTC ISO of when this row first entered the plan, set
            # once on INSERT and never overwritten. Distinct from `date` (the day the
            # session is scheduled for) and `original_date` (its first scheduled day) —
            # this is WHEN it was authored, the natural counterpart to `adapted_at` for
            # showing a session's plan→adapt lifecycle. NULL on legacy rows.
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN created_at TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN adapted_at TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE workouts ADD COLUMN adaptation_count INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            # Original load snapshot: the duration / TSS / RPE the session carried when it
            # first entered the plan, captured once (set to the live value on INSERT, then
            # COALESCE-preserved across every later UPDATE — the same once-only treatment as
            # `original_description` / `original_date`). Lets the plan show how far an adapted
            # session has been walked down from its planned load without parsing prose. NULL
            # on legacy rows; their first adaptation backfills them from the pre-adapt value.
            for col in (
                "original_duration_minutes",
                "original_tss",
                "original_rpe",
            ):
                try:
                    cursor.execute(
                        f"ALTER TABLE workouts ADD COLUMN {col} INTEGER DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass
            # Adherence-mark freshness axis: hash of the calendar fields plus the
            # backward-looking adherence verdict at the last `workout compare --mark`
            # push. Lets compare skip a no-op Calendar update when the event already
            # carries the same verdict (see trainmate.calendar_state.adherence_signature).
            # Kept separate from `pushed_signature` on purpose: folding adherence into
            # that hash would make every marked past row read `stale`. NULL = never marked.
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN marked_signature TEXT")
            except sqlite3.OperationalError:
                pass
            # Benchmark identity: a session whose PURPOSE is measurement, not stimulus
            # (DESIGN_benchmark_workouts.md §3.1). Holds an anchor-kind slug
            # (ftp_20min | run_5k_tt | e1rm | …) when the session is a fitness test, NULL
            # otherwise. Creation-time intent like `source` (fixed when the session is
            # created), NOT a derived kind-column — so it is a stored column, threaded
            # through every save path and the model's generate/adapt output contracts so
            # protecting a test never strips its identity.
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN benchmark_type TEXT")
            except sqlite3.OperationalError:
                pass

            # Planned time in zone (DESIGN_intensity_distribution.md §9.8): the intensity
            # target of a session, stated by the coach as structured data at authoring
            # time. NOT derived from `tss` — `tss ~ duration x IF^2` invites backing out an
            # average intensity factor, which is §1 run backwards (TSS is the projection
            # that destroyed the distribution and it cannot be un-projected) and circular
            # besides, since planned zones computed from planned TSS make
            # planned-vs-measured zones a restatement of the adherence percentage that
            # already exists. HR sessions fill 1-5 and leave 6-7 NULL, mirroring what
            # `garmin/sync.py` writes on the measured side; swimming (CSS) and strength
            # (e1RM) yield no zone model and stay NULL throughout.
            for col in (
                ["planned_zone_currency TEXT"]
                + [f"planned_zone{i}_sec INTEGER DEFAULT NULL" for i in range(1, 8)]
            ):
                try:
                    cursor.execute(f"ALTER TABLE workouts ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass

            # Benchmark results logbook (DESIGN_benchmark_workouts.md §3.2): a dated log of
            # fitness-test outcomes, one row per measurement. With config's `ftp`/`lthr`
            # removed (§3.4), this is the ONLY home for the athlete's trainable thresholds —
            # the effective-threshold accessor reads the latest row per anchor_kind (newest
            # by date, id as tiebreak) and feeds it to the coaching prompt and the plan
            # staleness check. `workout_id` optionally links a result to the planned
            # benchmark it satisfied. `source` records how the value arrived
            # (test|manual|modeled — the last anticipates Phase-3 passive estimation).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    date        TEXT NOT NULL,
                    sport_type  TEXT NOT NULL,
                    anchor_kind TEXT NOT NULL, -- ftp|lthr|threshold_pace|css|e1rm|mas
                    value       REAL NOT NULL,
                    unit        TEXT NOT NULL, -- W|bpm|min/km|sec/100m|kg|km/h
                    source      TEXT NOT NULL DEFAULT 'test', -- test|manual|modeled
                    workout_id  INTEGER,       -- nullable link to the planned benchmark
                    note        TEXT,
                    created     TEXT
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_benchmark_results_kind "
                "ON benchmark_results(anchor_kind, date)"
            )

            # Completed activities table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS completed_activities (
                    activity_id TEXT PRIMARY KEY,
                    date TEXT NOT NULL,
                    start_time TEXT,
                    activity_name TEXT,
                    activity_type TEXT NOT NULL,
                    duration_sec REAL,
                    distance_km REAL,
                    elevation_gain_m REAL,
                    avg_hr INTEGER,
                    max_hr INTEGER,
                    rpe INTEGER,
                    tss REAL
                )
            """)

            # Add new columns to completed_activities if they don't exist
            for col in [
                "bike_avg_watts INTEGER DEFAULT NULL",
                "zone1_sec INTEGER DEFAULT NULL",
                "zone2_sec INTEGER DEFAULT NULL",
                "zone3_sec INTEGER DEFAULT NULL",
                "zone4_sec INTEGER DEFAULT NULL",
                "zone5_sec INTEGER DEFAULT NULL",
                # Power zones use Garmin's 7-zone model (cycling with a power meter).
                "power_zone1_sec INTEGER DEFAULT NULL",
                "power_zone2_sec INTEGER DEFAULT NULL",
                "power_zone3_sec INTEGER DEFAULT NULL",
                "power_zone4_sec INTEGER DEFAULT NULL",
                "power_zone5_sec INTEGER DEFAULT NULL",
                "power_zone6_sec INTEGER DEFAULT NULL",
                "power_zone7_sec INTEGER DEFAULT NULL",
            ]:
                try:
                    cursor.execute(f"ALTER TABLE completed_activities ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass

            # Athlete metrics cache table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS athlete_metrics_cache (
                    date TEXT PRIMARY KEY,
                    rhr INTEGER,
                    hrv INTEGER,
                    sleep_score INTEGER,
                    stress INTEGER,
                    ctl REAL,
                    atl REAL,
                    tsb REAL
                )
            """)
            # ACWR retired in favour of ATL/CTL, which reads off the PMC EWMAs already on
            # this row (training_load.txt §3). Pure DDL, guarded by column presence, so
            # it is idempotent and needs no separate migration script.
            cursor.execute("PRAGMA table_info(athlete_metrics_cache)")
            mcols = [row['name'] for row in cursor.fetchall()]
            for col in ("acute_workload", "chronic_workload", "acwr"):
                if col in mcols:
                    cursor.execute(
                        f"ALTER TABLE athlete_metrics_cache DROP COLUMN {col}"
                    )
            # Performance Management Chart columns (DESIGN_pmc_fitness_fatigue.md §4):
            # CTL/ATL/TSB, back-populated for the whole history by the next
            # recompute_derived() sweep. NULL-tolerant on existing rows; no migration.
            for col in ("ctl", "atl", "tsb"):
                try:
                    cursor.execute(
                        f"ALTER TABLE athlete_metrics_cache ADD COLUMN {col} REAL"
                    )
                except sqlite3.OperationalError:
                    pass

            # Athlete baselines table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS athlete_baselines (
                    date TEXT PRIMARY KEY,
                    rhr_baseline_mean REAL,
                    rhr_baseline_std REAL,
                    hrv_baseline_mean REAL,
                    hrv_baseline_std REAL,
                    sleep_baseline_mean REAL,
                    sleep_baseline_std REAL
                )
            """)

            # Coach learnings: discrete, addressable athlete-observation records.
            # The LLM updates these incrementally via deltas (see apply_learning_deltas)
            # rather than overwriting a single blob. `confidence` is now APP-COMPUTED from
            # the per-learning evidence basis (learning_evidence below), not LLM-asserted
            # (DESIGN_evidence_based_confidence.md). `proposed_confidence` holds a pending,
            # human-confirmable DOWNGRADE (NULL when none pending).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS coach_learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    sports TEXT NOT NULL DEFAULT 'general',
                    confidence TEXT NOT NULL DEFAULT 'tentative',
                    proposed_confidence TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_reinforced_at TEXT
                )
            """)

            # Phase 2 enrichment: add sport-scope, confidence, and recency columns to
            # coach_learnings created before they existed; plus the evidence-confidence
            # proposed_confidence column.
            for col in [
                "sports TEXT NOT NULL DEFAULT 'general'",
                "confidence TEXT NOT NULL DEFAULT 'tentative'",
                "proposed_confidence TEXT",
                "last_reinforced_at TEXT",
            ]:
                try:
                    cursor.execute(f"ALTER TABLE coach_learnings ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # Column already exists.
            # Backfill recency for migrated rows: treat creation as the last reinforcement.
            cursor.execute(
                "UPDATE coach_learnings SET last_reinforced_at = created_at "
                "WHERE last_reinforced_at IS NULL"
            )

            # Evidence basis (DESIGN_evidence_based_confidence.md §5). The distinct training
            # WEEKS that back each learning, tagged +1 supporting / -1 contradicting. Confidence
            # is a pure function of this basis. UNIQUE(learning_id, week_commencing, polarity)
            # is the dedup guarantee: re-citing a counted (week, polarity) is an INSERT-OR-IGNORE
            # no-op, so re-running / --force / overlapping windows cannot inflate confidence.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS learning_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    learning_id INTEGER NOT NULL,
                    week_commencing TEXT NOT NULL, -- YYYY-MM-DD (Monday)
                    polarity INTEGER NOT NULL,     -- +1 supporting | -1 contradicting
                    source TEXT,                   -- 'reflect'|'bootstrap'|'plan'|'manual'|'migration'
                    created_at TEXT NOT NULL,
                    UNIQUE(learning_id, week_commencing, polarity),
                    FOREIGN KEY (learning_id) REFERENCES coach_learnings(id) ON DELETE CASCADE
                )
            """)


            # Macrocycles table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS macrocycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    objective_id INTEGER NOT NULL,
                    strategy TEXT NOT NULL,
                    goals_hash TEXT NOT NULL,
                    constraints_hash TEXT NOT NULL,
                    config_hash TEXT,
                    config_snapshot TEXT,
                    goals_snapshot TEXT,
                    constraints_snapshot TEXT,
                    created_at TEXT NOT NULL,
                    feedback TEXT DEFAULT NULL,
                    FOREIGN KEY (objective_id) REFERENCES objectives(id) ON DELETE CASCADE
                )
            """)

            # Fold the plan's staleness fingerprint back to `constraints_hash` and its
            # snapshot to `constraints_snapshot` (DESIGN_constraints.md §7/§9). The prior
            # `lifeevents_hash`/`lifeevents_snapshot` names are renamed in place; existing
            # snapshot VALUES are left untouched as legacy (the display code tolerates plans
            # that predate a snapshot key). The old constraints_hash→lifeevents_hash rename
            # is gone — this is its reversal.
            cursor.execute("PRAGMA table_info(macrocycles)")
            columns = [row['name'] for row in cursor.fetchall()]
            if 'lifeevents_hash' in columns and 'constraints_hash' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles RENAME COLUMN lifeevents_hash TO constraints_hash"
                )
            if 'lifeevents_snapshot' in columns and 'constraints_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles "
                    "RENAME COLUMN lifeevents_snapshot TO constraints_snapshot"
                )
            cursor.execute("PRAGMA table_info(macrocycles)")
            columns = [row['name'] for row in cursor.fetchall()]
            if 'config_hash' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN config_hash TEXT"
                )
            # Snapshots of the goals and life events the plan was generated from, so they
            # can be shown after the fact even once the live records have changed. Stored
            # as the same cleaned JSON the goals_hash/lifeevents_hash fingerprint. NULL on
            # macrocycles created before this column existed.
            if 'goals_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN goals_snapshot TEXT"
                )
            if 'constraints_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN constraints_snapshot TEXT"
                )
            # Physiological thresholds (max_hr/lthr/ftp) the plan was generated with,
            # as JSON. Unlike the profile fields folded into config_hash, thresholds
            # only flag the plan stale past a relative drift tolerance, which needs
            # the original values, not a hash (coach/service.config_changed). NULL on
            # macrocycles created before this column existed (treated as no drift).
            if 'config_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN config_snapshot TEXT"
                )
            # Plan-version axis (see DESIGN_plan_rollback.md). Regenerating a plan no
            # longer deletes the prior macrocycle: it is marked 'superseded' (with the
            # moment recorded in superseded_at) and kept, so `plan rollback` can restore
            # an earlier version. Exactly one macrocycle per objective is 'active' at a
            # time; readers filter on status='active'. Legacy rows default to 'active'.
            if 'status' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN status TEXT DEFAULT 'active'"
                )
            if 'superseded_at' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN superseded_at TEXT"
                )

            # Mesocycles table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS mesocycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    macrocycle_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    focus TEXT NOT NULL,
                    feedback TEXT DEFAULT NULL,
                    FOREIGN KEY (macrocycle_id) REFERENCES macrocycles(id) ON DELETE CASCADE
                )
            """)

            # Backward-evaluation reconstruction cache (see DESIGN_backward_evaluation.md
            # §5.1). Each row is a cached reconstruction (inferred cycles + physiological
            # insights) of a past training window, keyed by an *evidence fingerprint* so
            # that a re-run over unchanged data can reuse it instead of paying for another
            # LLM pass. Retention is one row per `horizon` (UNIQUE): a new data pull shifts
            # the fingerprint and overwrites the slot, because we only ever want the current
            # reconstruction. The whole reconstruction is stored as one JSON blob — nothing
            # queries inside it; it is fetched whole, fed to a prompt, or rendered.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS analysis_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    horizon TEXT NOT NULL UNIQUE, -- 'long' | 'short' — the cache slot
                    fingerprint TEXT NOT NULL,    -- hash of activity-id set + metrics + window
                    window_start TEXT,
                    window_end TEXT,
                    reconstruction TEXT NOT NULL, -- JSON: inferred cycles + insights
                    created_at TEXT NOT NULL
                )
            """)

            # Sync watermark: how far Garmin data has been pulled, and when.
            # through_date is the FORWARD high-water mark (local YYYY-MM-DD); a
            # backward backfill never regresses it. last_pull_utc is an INSTANT
            # (UTC ISO) compared against now for the freshness interval. sync_token
            # is the opaque Calendar nextSyncToken — populated only by the
            # 'calendar_context' row (DESIGN_calendar_context_ingest.md §6.1).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    key            TEXT PRIMARY KEY,
                    through_date   TEXT,
                    last_pull_utc  TEXT,
                    sync_token     TEXT
                )
            """)
            # Migration: pre-existing DBs created sync_state without sync_token.
            cursor.execute("PRAGMA table_info(sync_state)")
            sync_state_cols = {row[1] for row in cursor.fetchall()}
            if "sync_token" not in sync_state_cols:
                cursor.execute("ALTER TABLE sync_state ADD COLUMN sync_token TEXT")

            # External daily context signals (alcohol, sleep, stress, …) ingested from
            # tagged Google Calendar events. TrainMate stays domain-agnostic: metric is
            # an opaque category, value an optional numeric magnitude, text the human
            # blurb for the LLM. google_event_id is the reconciliation key so ingestion
            # is an upsert with edit/delete detection (DESIGN_calendar_context_ingest.md §5).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_context (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    date            TEXT NOT NULL,
                    metric          TEXT NOT NULL,
                    value           REAL,
                    text            TEXT,
                    google_event_id TEXT NOT NULL UNIQUE,
                    updated         TEXT
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_context_date ON daily_context(date)"
            )

            # App preferences that outlive one invocation but aren't training data. Generic
            # key/value so the next single-value preference needs no schema change. First
            # key: 'llm_model' (DESIGN_model_selection.md §2).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            conn.commit()

        # Grandfather pre-evidence learnings with a synthetic basis that sustains their
        # stored confidence, so the first app-computed recompute does not silently demote
        # everything (DESIGN_evidence_based_confidence.md §9). Runs after the schema is
        # committed; idempotent (only acts on learnings with an empty basis).
        self._grandfather_learning_evidence()
