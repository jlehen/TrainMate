import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional
from trainmate.config import config

# Bump when the DDL below changes, so existing databases pick the change up once. The
# migrations are idempotent, so this is a "skip the work" marker rather than a ledger of
# steps to replay — TrainMate has one user and one database, and the alternative (a
# numbered migration framework) would be more machinery than that warrants.
SCHEMA_VERSION = 11


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

    def __init__(self, db_path: Optional[str] = None, calendar_hook=None) -> None:
        """Initializes database path and sets up tables.

        `calendar_hook` is what the §8 reconcile pass calls when a workout change closes:
        `hook(db, lineage_ids)`. Left unset the pass is inert, which is what an isolated
        unit test wants; `runtime._build_db` attaches the real one, so the running app
        cannot write workouts without reconciling Calendar
        (DESIGN_workout_revisions.md §8).
        """
        self.db_path: str = db_path or config.db_path
        # A fresh `data_dir:` instance must run before its directory exists — sqlite
        # won't create parents, and every other writer already makedirs its own.
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._joined: Optional[_JoinedConnection] = None
        self.calendar_hook = calendar_hook
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

    @staticmethod
    def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
        """Returns True if `table` is present in the database."""
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        )
        return cursor.fetchone() is not None

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

            # Dead tables, replaced by `constraints` (DESIGN_constraints.md §5); the row
            # copy across has already run. Idempotent, so it stays in `_init_db`.
            cursor.execute("DROP TABLE IF EXISTS lifeevents")
            cursor.execute("DROP TABLE IF EXISTS life_events")

            # Unified directives — everything the athlete asks the coach to work around, at
            # any horizon (DESIGN_constraints.md §5). A constraint is advisory prose the coach
            # reads unless `rest = 1`, the single deterministic edge: a no-training window
            # whose dates skip the LLM and are forced to rest. `replan` marks one escalated to
            # plan-shaping (§7).
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

            # Collapse binding/sport/type onto the one `rest` flag (DESIGN_constraints.md §5).
            # Every dropped row already behaved as advisory prose, so only hard+no-sport
            # carries over. Changes the constraints_hash of a replan=1 constraint: run
            # scripts/migrate_constraints_drop_binding.py once to avoid a spurious regen (§7).
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
            # every day of it still ahead (DESIGN_constraint_honoring.md §2). NULL = the
            # plan does not reflect it yet. Not "the plan definitely changed".
            self._add_column(
                cursor, "constraints", "honored_at",
                "ALTER TABLE constraints ADD COLUMN honored_at TEXT"
            )

            # Workouts — an append-only revision log (DESIGN_workout_revisions.md §2).
            # A row is one revision of one session and is never updated or deleted; the
            # highest `id` in a `(date, sport_canonical)` slot is the live one. The
            # triggers at the end of this method are what enforce that (§14).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS workouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    change_id INTEGER NOT NULL, -- the workout_changes row that appended this revision
                    lineage_id INTEGER, -- stable session identity; equals id on a first revision (§4)
                    date TEXT NOT NULL,
                    sport_canonical TEXT NOT NULL, -- the slot key (trainmate.sports)
                    sport_type TEXT NOT NULL, -- the spelling as written
                    title TEXT NOT NULL,
                    description TEXT,
                    duration_minutes INTEGER,
                    rpe INTEGER,
                    tss INTEGER,
                    void INTEGER NOT NULL DEFAULT 0, -- 1 <=> this slot holds no session as of this revision
                    reason TEXT, -- per-revision note; why it changed, or why it was cancelled
                    restored_from INTEGER, -- on a rollback/restore/reinstate copy: the revision copied (§7)
                    macrocycle_id INTEGER, -- plan version this session belongs to
                    created_at TEXT, -- when the SESSION entered the plan, carried across revisions
                    benchmark_type TEXT, -- set <=> a fitness test (DESIGN_benchmark_workouts.md §3.1)
                    planned_zone_currency TEXT, -- 'hr' | 'power' (DESIGN_intensity_distribution.md §9.8)
                    planned_zone1_sec INTEGER,
                    planned_zone2_sec INTEGER,
                    planned_zone3_sec INTEGER,
                    planned_zone4_sec INTEGER,
                    planned_zone5_sec INTEGER,
                    planned_zone6_sec INTEGER,
                    planned_zone7_sec INTEGER
                )
            """)

            # One row per command invocation that wrote workouts (§3). `kind` is fixed at
            # write time from the §3 vocabulary, `summary` is the batch rationale, and
            # `macrocycle_id` is the plan version in force when the command ran — context
            # for `workout batches`, distinct from the per-row tag on `workouts`.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS workout_changes (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at    TEXT    NOT NULL,
                    kind          TEXT    NOT NULL,
                    summary       TEXT,
                    macrocycle_id INTEGER
                )
            """)

            # The coach's one line to the athlete about this change, for the morning push
            # (DESIGN_plan_change_continuity.md §6.3). NULL on a change with nothing the
            # athlete would notice, which is most of them.
            self._add_column(
                cursor, "workout_changes", "note",
                "ALTER TABLE workout_changes ADD COLUMN note TEXT"
            )
            # The last day of the commitment window in force when this change ran, so a
            # void is judged by the window it was written under rather than by the one
            # standing whenever the Calendar sync happens to run (§5.2).
            self._add_column(
                cursor, "workout_changes", "commitment_end",
                "ALTER TABLE workout_changes ADD COLUMN commitment_end TEXT"
            )

            # Calendar sync bookkeeping, keyed by lineage (§8). Off the row because a
            # successful push is not a prescription change: leaving it there would make
            # `workout push -f` append a revision per session.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS workout_calendar_state (
                    lineage_id                 INTEGER PRIMARY KEY,
                    google_event_id            TEXT,
                    pushed_signature           TEXT,
                    adherence_pushed_signature TEXT
                )
            """)

            # The first serves the live view, the second every lineage derivation (§3).
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_workouts_slot "
                "ON workouts(date, sport_canonical, id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_workouts_lineage "
                "ON workouts(lineage_id, id)"
            )

            # The live view (§5): `id` is AUTOINCREMENT and therefore monotonic, so the
            # highest id in a slot is its newest revision. Void revisions are included on
            # purpose — a cancelled session is still a fact readers must see.
            cursor.execute("DROP VIEW IF EXISTS live_workouts")
            cursor.execute("""
                CREATE VIEW live_workouts AS
                SELECT w.* FROM workouts w
                WHERE w.id = (
                    SELECT MAX(w2.id) FROM workouts w2
                    WHERE w2.date = w.date AND w2.sport_canonical = w.sport_canonical
                )
            """)

            # Append-only, enforced where it cannot be skipped (§14). The UPDATE trigger
            # exempts exactly one transition — the post-insert lineage seeding of §4 — and
            # pins the value it may write to the row's own id.
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS workouts_no_update BEFORE UPDATE ON workouts
                WHEN NOT (OLD.lineage_id IS NULL AND NEW.lineage_id = NEW.id)
                BEGIN SELECT RAISE(ABORT, 'workouts is append-only: append a revision'); END
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS workouts_no_delete BEFORE DELETE ON workouts
                BEGIN SELECT RAISE(ABORT, 'workouts is append-only: append a void revision'); END
            """)

            # Benchmark results logbook (DESIGN_benchmark_workouts.md §3.2), one row per
            # measurement and the ONLY home for the athlete's trainable thresholds (§3.4).
            # The effective-threshold accessor reads the latest row per anchor_kind — newest
            # by date, id as tiebreak — and feeds it to the prompt and the staleness check.
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

            # The athlete's answers to "is this activity that session?" — the pairing
            # questions `adherence.is_ambiguous_match` raises (ARCHITECTURE.md §15).
            # Keyed by (activity, sport), not by workout id: workout rows are replaced on
            # every revision, so a workout id would go stale the next time the day is
            # adapted, and the athlete would be asked the same question again.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS activity_match_decisions (
                    activity_id TEXT NOT NULL,
                    sport_canonical TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    decided_at TEXT NOT NULL,
                    PRIMARY KEY (activity_id, sport_canonical)
                )
            """)

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

            # Coach learnings: discrete, addressable athlete-observation records, updated
            # incrementally via deltas (see apply_learning_deltas). `confidence` is
            # APP-COMPUTED from the evidence basis in learning_evidence below, never
            # LLM-asserted (DESIGN_evidence_based_confidence.md); `proposed_confidence`
            # holds a pending human-confirmable DOWNGRADE.
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
            # snapshot VALUES are left untouched (the display code tolerates plans that
            # predate a snapshot key).
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
            # The coach's re-shaping read, cached against the snapshot it was asked
            # about, so `plan show` asks once per edit rather than on every read
            # (DESIGN_plan_change_continuity.md §7).
            self._add_column(
                cursor, "macrocycles", "reshape_verdict",
                "ALTER TABLE macrocycles ADD COLUMN reshape_verdict TEXT"
            )
            self._add_column(
                cursor, "macrocycles", "reshape_verdict_key",
                "ALTER TABLE macrocycles ADD COLUMN reshape_verdict_key TEXT"
            )

            cursor.execute("PRAGMA table_info(macrocycles)")
            columns = [row['name'] for row in cursor.fetchall()]
            if 'config_hash' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN config_hash TEXT"
                )
            # Snapshots of the goals and constraints the plan was generated from, so they
            # can be shown after the fact even once the live records have changed. Stored as
            # the same cleaned JSON the goals_hash/constraints_hash fingerprint.
            if 'goals_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN goals_snapshot TEXT"
                )
            if 'constraints_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN constraints_snapshot TEXT"
                )
            # Every constraint active at generation time, not just the `replan = 1` subset
            # `constraints_snapshot` fingerprints, tagged per-entry with its `replan` flag.
            # Display-only: the prompt sees every active constraint, so "Constraints
            # considered" must too (DESIGN_constraints.md §7).
            if 'all_constraints_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN all_constraints_snapshot TEXT"
                )
            # Physiological thresholds (max_hr/lthr/ftp) the plan was generated with,
            # as JSON. Unlike the profile fields folded into config_hash, thresholds
            # only flag the plan stale past a relative drift tolerance, which needs the
            # original values, not a hash (coach/service.config_changed). NULL is no drift.
            if 'config_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN config_snapshot TEXT"
                )
            # The plan-shaping profile fields (config.plan_profile) the plan was generated
            # with, as JSON. config_hash alone answers "did something change" but not
            # "what", so the staleness reason could not name the field that moved
            # (DESIGN_plan_staleness.md §5). NULL falls back to the unnamed reason.
            if 'profile_snapshot' not in columns:
                cursor.execute(
                    "ALTER TABLE macrocycles ADD COLUMN profile_snapshot TEXT"
                )
            # Plan-version axis (see DESIGN_plan_rollback.md). Regenerating a plan keeps the
            # prior macrocycle, marked 'superseded' at `superseded_at`, so `plan rollback` can
            # restore it. Exactly one macrocycle per objective is 'active'; readers filter
            # on status='active'.
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

            # Backward-evaluation reconstruction cache (DESIGN_backward_evaluation.md §5.1),
            # keyed by an *evidence fingerprint* so a re-run over unchanged data skips the
            # LLM pass. One row per `horizon` (UNIQUE): a new data pull shifts the
            # fingerprint and overwrites the slot. One JSON blob — nothing queries inside it.
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
            # 'calendar_signals' row (DESIGN_calendar_signal_ingest.md §6.1).
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

            # One-off: `daily_context` and its sync_state row are the pre-rename names of
            # the daily-signal store (DESIGN_calendar_signal_ingest.md §5). Renaming the
            # table keeps the rows and the Calendar sync token, so no re-pull is needed.
            if self._table_exists(cursor, "daily_context"):
                cursor.execute("ALTER TABLE daily_context RENAME TO daily_signals")
                cursor.execute("DROP INDEX IF EXISTS idx_daily_context_date")
                cursor.execute(
                    "UPDATE sync_state SET key = 'calendar_signals' "
                    "WHERE key = 'calendar_context'"
                )

            # External daily signals (alcohol, sleep, stress, …) ingested from
            # tagged Google Calendar events. TrainMate stays domain-agnostic: metric is
            # an opaque category, value an optional numeric magnitude, text the human
            # blurb for the LLM. google_event_id is the reconciliation key so ingestion
            # is an upsert with edit/delete detection (DESIGN_calendar_signal_ingest.md §5).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_signals (
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
                "CREATE INDEX IF NOT EXISTS idx_daily_signals_date ON daily_signals(date)"
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
