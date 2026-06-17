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
                    synced INTEGER DEFAULT 0, -- 0 = pending push, 1 = calendar current
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
            # Migrate the conflated `status` enum into an orthogonal `synced` flag.
            # The adaptation axis already lives in modification_reason; only the sync
            # axis needs its own column. On a fresh DB the ALTER fails (column already
            # exists from CREATE TABLE) and the backfill is skipped — correct, no rows.
            try:
                cursor.execute("ALTER TABLE workouts ADD COLUMN synced INTEGER DEFAULT 0")
                cursor.execute("UPDATE workouts SET synced = 1 WHERE status = 'synced'")
            except sqlite3.OperationalError:
                pass
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
