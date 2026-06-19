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

            # Check for existing tables from oldest to newest
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='lifeevents'"
            )
            lifeevents_exists = cursor.fetchone()

            if not lifeevents_exists:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='constraints'"
                )
                constraints_exists = cursor.fetchone()
                if constraints_exists:
                    cursor.execute("ALTER TABLE constraints RENAME TO lifeevents")
                else:
                    cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='life_events'"
                    )
                    life_events_exists = cursor.fetchone()
                    if life_events_exists:
                        cursor.execute("ALTER TABLE life_events RENAME TO lifeevents")
                    else:
                        # Create lifeevents table from scratch
                        cursor.execute("""
                            CREATE TABLE IF NOT EXISTS lifeevents (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                title TEXT NOT NULL,
                                start_date TEXT NOT NULL,
                                end_date TEXT NOT NULL,
                                -- event_type values: 'business_trip', 'vacation', 'party', 'other'
                                event_type TEXT NOT NULL,
                                impact_description TEXT
                            )
                        """)
            else:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS lifeevents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        start_date TEXT NOT NULL,
                        end_date TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        impact_description TEXT
                    )
                """)

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
                    acute_workload REAL,
                    chronic_workload REAL,
                    acwr REAL
                )
            """)

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
                    lifeevents_hash TEXT NOT NULL,
                    config_hash TEXT,
                    goals_snapshot TEXT,
                    lifeevents_snapshot TEXT,
                    created_at TEXT NOT NULL,
                    feedback TEXT DEFAULT NULL,
                    FOREIGN KEY (objective_id) REFERENCES objectives(id) ON DELETE CASCADE
                )
            """)

            # Migrate column constraints_hash to lifeevents_hash if constraints_hash exists
            cursor.execute("PRAGMA table_info(macrocycles)")
            columns = [row['name'] for row in cursor.fetchall()]
            if 'constraints_hash' in columns and 'lifeevents_hash' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles RENAME COLUMN constraints_hash TO lifeevents_hash"
                )
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
            if 'lifeevents_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN lifeevents_snapshot TEXT"
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

            conn.commit()

        # Grandfather pre-evidence learnings with a synthetic basis that sustains their
        # stored confidence, so the first app-computed recompute does not silently demote
        # everything (DESIGN_evidence_based_confidence.md §9). Runs after the schema is
        # committed; idempotent (only acts on learnings with an empty basis).
        self._grandfather_learning_evidence()
