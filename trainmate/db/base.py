import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional
from trainmate.config import config

# Bump when the DDL below changes, so existing databases pick the change up once. The
# migrations are idempotent, so this is a "skip the work" marker rather than a ledger of
# steps to replay — TrainMate has one user and one database, and the alternative (a
# numbered migration framework) would be more machinery than that warrants.
SCHEMA_VERSION = 7


# How long a connection waits for a writer to finish before raising "database is
# locked". The CLI, the web app and the Telegram bot are concurrent surfaces against one
# file, so a pull that overlaps a dashboard refresh is ordinary, not exceptional.
BUSY_TIMEOUT_SECONDS = 5.0


class _JoinedConnection:
    """A connection borrowed from an open `transaction()`.

    Methods are written `with db._get_connection() as conn: ...; conn.commit()`, which
    is right when each call owns its connection. Inside a transaction those commits
    would end it early — one fsync per row, which is the cost the transaction exists to
    avoid — so they are deferred to the single commit at the end. Everything else
    forwards to the real connection untouched.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def commit(self) -> None:
        """Deferred: the enclosing transaction commits once, at the end."""

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BaseDB:
    """Connection management and schema initialization shared by all DB mixins."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        """Initializes database path and sets up tables."""
        self.db_path: str = db_path or config.db_path
        self._joined: Optional[_JoinedConnection] = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS)
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets a reader carry on while a writer commits, which is the normal case
        # here: the dashboard polls while `data pull` writes. It is a property of the
        # database file, so this takes effect once and persists.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Creates and returns a connection to SQLite database with constraints enabled."""
        if self._joined is not None:
            yield self._joined
            return
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Runs everything inside as one unit of work: one connection, one commit.

        Without it, each write opens, commits and closes its own connection — a PMC
        recompute over a long history did that around fifteen hundred times, after
        every pull. It also makes multi-step work atomic: regenerating a plan archives,
        supersedes, inserts and pushes, and a crash between those steps used to leave
        archived workouts with no active plan.

        Nesting joins the outer transaction rather than starting a second one, so a
        method that opens one is safe to call from inside another.
        """
        if self._joined is not None:
            yield self._joined
            return
        conn = self._connect()
        joined = _JoinedConnection(conn)
        self._joined = joined
        try:
            with conn:          # commits once here, or rolls back if the body raises
                yield joined
        finally:
            self._joined = None
            conn.close()

    @staticmethod
    def _table_has_column(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
        """Returns True if `table` currently has `column` (via PRAGMA table_info)."""
        cursor.execute(f"PRAGMA table_info({table})")
        return any(row[1] == column for row in cursor.fetchall())

    @classmethod
    def _add_column(
        cls, cursor: sqlite3.Cursor, table: str, column: str, ddl: str
    ) -> None:
        """Adds `column` to `table` if it is missing.

        Migrations used to be written `try: ALTER ... except OperationalError: pass`,
        reading "the column is already there". But OperationalError is also how SQLite
        reports "database is locked" and "disk I/O error", so a locked database at
        startup half-migrated in silence and left no way to tell afterwards. Asking
        first means the only errors that reach here are real ones.
        """
        if cls._table_has_column(cursor, table, column):
            return
        cursor.execute(ddl)

    def _schema_version(self, conn: sqlite3.Connection) -> int:
        """The version this database has been brought up to, 0 if never stamped."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER NOT NULL,"
            "  applied_at TEXT NOT NULL"
            ")"
        )
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _stamp_schema_version(self, conn: sqlite3.Connection, version: int) -> None:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )

    def _init_db(self) -> None:
        """Brings the database up to SCHEMA_VERSION, then does nothing on later starts.

        This used to run unconditionally: ~630 lines of DDL and roughly thirty in-place
        migrations, on every process start, including `tm --help`. It also *wrote* to the
        file to do it. Now the work happens once and the result is stamped, so a startup
        against a current database is a single SELECT.

        The migrations themselves are unchanged and remain idempotent (CREATE TABLE IF
        NOT EXISTS, guarded ALTERs), so stamping is a shortcut rather than a new contract
        — a database that somehow misses a column is still repaired by clearing its
        schema_version row.
        """
        with self._get_connection() as conn:
            if self._schema_version(conn) >= SCHEMA_VERSION:
                return

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
                    status TEXT DEFAULT 'active', -- 'active' | 'archived'; see below
                    date_type TEXT NOT NULL DEFAULT 'event' -- 'event' | 'horizon'; see below
                )
            """)

            # Whether target_date is a scheduled event or just how far to train
            # (ARCHITECTURE.md §15 "Goal dates"). Existing rows read 'event', which is
            # what they meant.
            self._add_column(
                cursor, "objectives", "date_type",
                "ALTER TABLE objectives ADD COLUMN date_type TEXT NOT NULL DEFAULT 'event'"
            )

            # `priority` was never read by any logic and never reached a prompt, so
            # editing it only flagged the plan stale (DOMAIN_MODEL.md §2 "Goal").
            if self._table_has_column(cursor, "objectives", "priority"):
                cursor.execute("ALTER TABLE objectives DROP COLUMN priority")

            # One-off (single-user app): 'completed' is no longer a stored state. A goal
            # the athlete has not archived and whose target date has passed IS completed,
            # derived by `db.objectives.goal_state()` — the column now records only
            # whether the goal was called off (DESIGN_backward_evaluation.md §12).
            cursor.execute(
                "UPDATE objectives SET status = 'active' WHERE status = 'completed'"
            )

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

            # When a coach pass last had this constraint in scope with authority over
            # every day of it still ahead (DESIGN_constraint_reschedule.md §8). NULL =
            # the plan does not reflect it yet, which is what the `workout accommodate`
            # sweep looks for. Not "the plan definitely changed".
            self._add_column(
                cursor, "constraints", "honored_at",
                "ALTER TABLE constraints ADD COLUMN honored_at TEXT"
            )

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
            self._add_column(
                cursor, "workouts", "duration_minutes",
                "ALTER TABLE workouts ADD COLUMN duration_minutes INTEGER DEFAULT NULL"
            )
            self._add_column(
                cursor, "workouts", "rpe",
                "ALTER TABLE workouts ADD COLUMN rpe INTEGER DEFAULT NULL"
            )
            self._add_column(
                cursor, "workouts", "tss",
                "ALTER TABLE workouts ADD COLUMN tss INTEGER DEFAULT NULL"
            )
            # Soft-delete axis: a workout removed via `workout rm` is marked rather
            # than deleted, so it can be excluded from reads yet still surfaced to the
            # coach as a deliberate cancellation (distinct from a miss).
            self._add_column(
                cursor, "workouts", "removed",
                "ALTER TABLE workouts ADD COLUMN removed INTEGER DEFAULT 0"
            )
            self._add_column(
                cursor, "workouts", "removed_reason",
                "ALTER TABLE workouts ADD COLUMN removed_reason TEXT"
            )
            # Split the adaptation rationale into two axes: modification_reason holds a
            # short per-workout note, while adaptation_summary holds the long batch-level
            # reason shared across every session in one `workout adapt` run (deduplicated
            # at display). NULL on swaps/manual edits and on legacy adapted rows.
            self._add_column(
                cursor, "workouts", "adaptation_summary",
                "ALTER TABLE workouts ADD COLUMN adaptation_summary TEXT"
            )
            # Origin axis: who authored the session, fixed at creation and never
            # overwritten — 'generated' (plan/workout generate, or a session adapt
            # newly introduces) or 'manual' (workout add). NULL on legacy rows
            # predating this column. Lets the coach and analysis treat
            # athlete-scheduled sessions distinctly from AI-authored ones; orthogonal
            # to the synced/adaptation/removed axes (adapting a session keeps its
            # origin — the change lands on modification_reason instead).
            self._add_column(
                cursor, "workouts", "source",
                "ALTER TABLE workouts ADD COLUMN source TEXT"
            )
            # Plan-version axis (see DESIGN_plan_rollback.md): macrocycle_id tags the
            # plan version a workout was created under, and archived_at marks workouts
            # that belonged to a superseded plan version (set when a regeneration or a
            # `plan rollback` displaces them). Archived rows are hidden from every read
            # by default and have their Calendar event torn down, but are kept so that
            # rolling back to their plan version can resurrect them. Distinct from
            # `removed` (a deliberate athlete cancellation that still surfaces to the
            # coach). NULL on legacy rows.
            self._add_column(
                cursor, "workouts", "macrocycle_id",
                "ALTER TABLE workouts ADD COLUMN macrocycle_id INTEGER"
            )
            self._add_column(
                cursor, "workouts", "archived_at",
                "ALTER TABLE workouts ADD COLUMN archived_at TEXT"
            )
            # Migrate the very old conflated `status` enum into an orthogonal `synced`
            # flag. Only relevant for DBs predating the `synced` column; guarded on the
            # `status` column so it doesn't re-add `synced` after we drop it below.
            if self._table_has_column(cursor, "workouts", "status"):
                self._add_column(
                    cursor, "workouts", "synced",
                    "ALTER TABLE workouts ADD COLUMN synced INTEGER DEFAULT 0"
                )
                cursor.execute("UPDATE workouts SET synced = 1 WHERE status = 'synced'")
            # Replace the hand-maintained `synced` flag with a derived freshness signal:
            # store `pushed_signature` (hash of calendar-relevant fields) on each push and
            # compare against the live hash to tell unpushed/synced/stale apart (see
            # trainmate.calendar_state). Backfill: a row that was `synced=1` matched the
            # calendar at push time, so its current content is its signature; everything
            # else stays NULL (reads as unpushed/stale). Then drop the now-dead column.
            self._add_column(
                cursor, "workouts", "pushed_signature",
                "ALTER TABLE workouts ADD COLUMN pushed_signature TEXT"
            )
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
            if not self._table_has_column(cursor, "workouts", "original_date"):
                cursor.execute("ALTER TABLE workouts ADD COLUMN original_date TEXT")
                cursor.execute(
                    "UPDATE workouts SET original_date = date "
                    "WHERE original_date IS NULL"
                )
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
            self._add_column(
                cursor, "workouts", "created_at",
                "ALTER TABLE workouts ADD COLUMN created_at TEXT"
            )
            self._add_column(
                cursor, "workouts", "adapted_at",
                "ALTER TABLE workouts ADD COLUMN adapted_at TEXT"
            )
            self._add_column(
                cursor, "workouts", "adaptation_count",
                "ALTER TABLE workouts ADD COLUMN adaptation_count INTEGER DEFAULT 0"
            )
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
                self._add_column(
                    cursor, "workouts", col,
                    f"ALTER TABLE workouts ADD COLUMN {col} INTEGER DEFAULT NULL"
                )
            # Adherence-mark freshness axis: hash of the calendar fields plus the
            # backward-looking adherence verdict at the last `workout compare --mark`
            # push. Lets compare skip a no-op Calendar update when the event already
            # carries the same verdict (see trainmate.calendar_state.adherence_signature).
            # Kept separate from `pushed_signature` on purpose: folding adherence into
            # that hash would make every marked past row read `stale`. NULL = never marked.
            self._add_column(
                cursor, "workouts", "marked_signature",
                "ALTER TABLE workouts ADD COLUMN marked_signature TEXT"
            )
            # Benchmark identity: a session whose PURPOSE is measurement, not stimulus
            # (DESIGN_benchmark_workouts.md §3.1). Holds an anchor-kind slug
            # (ftp_20min | run_5k_tt | e1rm | …) when the session is a fitness test, NULL
            # otherwise. Creation-time intent like `source` (fixed when the session is
            # created), NOT a derived kind-column — so it is a stored column, threaded
            # through every save path and the model's generate/adapt output contracts so
            # protecting a test never strips its identity.
            self._add_column(
                cursor, "workouts", "benchmark_type",
                "ALTER TABLE workouts ADD COLUMN benchmark_type TEXT"
            )

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
                self._add_column(
                    cursor, "workouts", col.split()[0],
                    f"ALTER TABLE workouts ADD COLUMN {col}"
                )

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
                self._add_column(
                    cursor, "completed_activities", col.split()[0],
                    f"ALTER TABLE completed_activities ADD COLUMN {col}"
                )

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
            # this row (training_load.md §3). Pure DDL, guarded by column presence, so
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
                self._add_column(
                    cursor, "athlete_metrics_cache", col,
                    f"ALTER TABLE athlete_metrics_cache ADD COLUMN {col} REAL"
                )

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
                self._add_column(
                    cursor, "coach_learnings", col.split()[0],
                    f"ALTER TABLE coach_learnings ADD COLUMN {col}"
                )  # Column already exists.
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
                    profile_snapshot TEXT,
                    goals_snapshot TEXT,
                    constraints_snapshot TEXT,
                    all_constraints_snapshot TEXT,
                    created_at TEXT NOT NULL,
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
            # Every constraint active at generation time (not just the `replan = 1`
            # subset `constraints_snapshot` fingerprints), tagged per-entry with its
            # `replan` flag. Display-only: `plan show`'s "Constraints considered" used
            # to read `constraints_snapshot` alone, which could print "None" even though
            # a tactical constraint had visibly shaped the LLM's prompt (it sees every
            # active constraint, not just plan-shaping ones — DESIGN_constraints.md §7).
            # NULL on macrocycles created before this column existed.
            if 'all_constraints_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN all_constraints_snapshot TEXT"
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
            # The plan-shaping profile fields (config.plan_profile) the plan was generated
            # with, as JSON. config_hash alone answers "did something change" but not
            # "what", so the staleness reason could not name the field that moved
            # (DESIGN_plan_staleness.md §5). NULL on macrocycles created before this
            # column existed — those fall back to the unnamed reason.
            if 'profile_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN profile_snapshot TEXT"
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
                    FOREIGN KEY (macrocycle_id) REFERENCES macrocycles(id) ON DELETE CASCADE
                )
            """)

            # The plan's feedback log (DESIGN_plan_feedback.md §6): an append-only list of
            # notes the athlete addressed to the NEXT plan version, attached to the version
            # they were written against. `mesocycle_id` NULL = plan-level. Pending means
            # "on the goal's active macrocycle" — supersession is the consumption event, so
            # there is no consumed flag.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS plan_feedback (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    macrocycle_id INTEGER NOT NULL,
                    mesocycle_id  INTEGER,
                    created_at    TEXT NOT NULL,
                    text          TEXT NOT NULL,
                    FOREIGN KEY (macrocycle_id) REFERENCES macrocycles(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (mesocycle_id) REFERENCES mesocycles(id) ON DELETE CASCADE
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_plan_feedback_macro "
                "ON plan_feedback(macrocycle_id)"
            )
            # One-off (§6): each non-empty overwrite slot becomes one log row, then the
            # slots go. A mesocycle carries no timestamp of its own, so its note inherits
            # the parent macro's — the best available, and it only orders a backfill.
            if self._table_has_column(cursor, "macrocycles", "feedback"):
                cursor.execute(
                    "INSERT INTO plan_feedback "
                    "  (macrocycle_id, mesocycle_id, created_at, text) "
                    "SELECT id, NULL, created_at, feedback FROM macrocycles "
                    "WHERE feedback IS NOT NULL AND TRIM(feedback) != ''"
                )
                cursor.execute("ALTER TABLE macrocycles DROP COLUMN feedback")
            if self._table_has_column(cursor, "mesocycles", "feedback"):
                cursor.execute(
                    "INSERT INTO plan_feedback "
                    "  (macrocycle_id, mesocycle_id, created_at, text) "
                    "SELECT m.macrocycle_id, m.id, mac.created_at, m.feedback "
                    "FROM mesocycles m JOIN macrocycles mac ON m.macrocycle_id = mac.id "
                    "WHERE m.feedback IS NOT NULL AND TRIM(m.feedback) != ''"
                )
                cursor.execute("ALTER TABLE mesocycles DROP COLUMN feedback")

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

            self._stamp_schema_version(conn, SCHEMA_VERSION)
            conn.commit()

        # Grandfather pre-evidence learnings with a synthetic basis that sustains their
        # stored confidence, so the first app-computed recompute does not silently demote
        # everything (DESIGN_evidence_based_confidence.md §9). Runs after the schema is
        # committed; idempotent (only acts on learnings with an empty basis).
        self._grandfather_learning_evidence()
