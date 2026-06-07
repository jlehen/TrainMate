import sqlite3
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, List, Dict, Generator
from contextlib import contextmanager
from trainmate.config import config
from trainmate.types import (
    Objective, LifeEvent, Workout, AthleteMetric, AthleteBaseline, Macrocycle, Mesocycle,
    CompletedActivity
)

# Coach-learning enrichment (Phase 2).
# Ordered confidence levels the LLM assigns to each observation.
CONFIDENCE_LEVELS = ("tentative", "moderate", "established")

# A learning is "dormant" — kept in the DB but excluded from LLM prompts — once it
# has gone unreinforced for longer than the budget for its confidence level. Decay is
# soft: a dormant learning revives the moment it is reinforced again.
LEARNING_STALENESS_DAYS = {
    "tentative": 21,
    "moderate": 60,
    "established": 180,
}


def normalize_sports(value: Any) -> str:
    """Normalizes a sport-scope value (list or comma string) to a comma-joined,
    lowercased string; empty/missing becomes 'general'."""
    if not value:
        return "general"
    if isinstance(value, (list, tuple)):
        parts = [str(s).strip().lower() for s in value if str(s).strip()]
    else:
        parts = [s.strip().lower() for s in str(value).split(",") if s.strip()]
    return ",".join(parts) if parts else "general"


def valid_confidence(value: Any) -> Optional[str]:
    """Returns the confidence value if it is a recognized level, else None."""
    return value if value in CONFIDENCE_LEVELS else None


def learning_is_dormant(learning: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True if a learning has gone unreinforced past its confidence-based budget."""
    now = now or datetime.now(timezone.utc)
    ref = learning.get("last_reinforced_at") or learning.get("created_at")
    if not ref:
        return False
    try:
        ref_dt = datetime.fromisoformat(ref)
    except (ValueError, TypeError):
        return False
    budget = LEARNING_STALENESS_DAYS.get(
        learning.get("confidence") or "tentative", LEARNING_STALENESS_DAYS["tentative"]
    )
    return (now - ref_dt) > timedelta(days=budget)


class Database:
    """Handles all database schema setups and operations using SQLite."""

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
                    status TEXT DEFAULT 'planned', -- 'planned', 'modified', 'synced'
                    modification_reason TEXT,
                    google_event_id TEXT
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
            # rather than overwriting a single blob.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS coach_learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    sports TEXT NOT NULL DEFAULT 'general',
                    confidence TEXT NOT NULL DEFAULT 'tentative',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_reinforced_at TEXT
                )
            """)

            # Phase 2 enrichment: add sport-scope, confidence, and recency columns to
            # coach_learnings created before they existed.
            for col in [
                "sports TEXT NOT NULL DEFAULT 'general'",
                "confidence TEXT NOT NULL DEFAULT 'tentative'",
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
            
            conn.commit()

    # --- Objectives CRUD ---
    def add_objective(
        self, title: str, target_date: str, sport_type: str,
        description: str = "", priority: int = 1, status: str = 'active'
    ) -> int:
        """Adds a new objective to the database and returns its ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO objectives (
                    title, target_date, sport_type, description, priority, status
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (title, target_date, sport_type, description, priority, status))
            conn.commit()
            return int(cursor.lastrowid)

    def get_objectives(self, status: Optional[str] = None) -> List[Objective]:
        """Fetches objectives from the database, filtered by status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    "SELECT * FROM objectives WHERE status = ? ORDER BY target_date ASC", (status,)
                )
            else:
                cursor.execute("SELECT * FROM objectives ORDER BY target_date ASC")
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_active_objective(self, objective_id: Optional[int] = None) -> Optional[Objective]:
        """Fetches the target active goal, or the next upcoming one if no ID is provided."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if objective_id is not None:
                cursor.execute(
                    "SELECT * FROM objectives WHERE id = ? AND status = 'active'", 
                    (objective_id,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM objectives WHERE status = 'active' "
                    "ORDER BY target_date ASC LIMIT 1"
                )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_preceding_objectives(
        self, target_date: str, history_days: Optional[int] = None
    ) -> List[Objective]:
        """Fetches active objectives strictly before target_date, up to history_days ago."""
        if history_days is None:
            history_days = config.goals_history_days
        
        # Calculate the lower bound date
        from datetime import datetime, timedelta, timezone # imported locally to avoid modifying imports block
        target_date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        reference_date = min(today, target_date_obj)
        lower_bound_obj = reference_date - timedelta(days=history_days)
        lower_bound_str = lower_bound_obj.strftime("%Y-%m-%d")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM objectives "
                "WHERE status = 'active' AND target_date < ? AND target_date >= ? "
                "ORDER BY target_date DESC",
                (target_date, lower_bound_str)
            )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_objective(self, obj_id: int) -> Optional[Objective]:
        """Fetches a specific objective by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM objectives WHERE id = ?", (obj_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def update_objective(self, obj_id: int, **kwargs: Any) -> None:
        """Updates objective properties in the database."""
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [obj_id]
        with self._get_connection() as conn:
            conn.cursor().execute(f"UPDATE objectives SET {fields} WHERE id = ?", values)
            conn.commit()

    def delete_objective(self, obj_id: int) -> None:
        """Deletes an objective by ID."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM objectives WHERE id = ?", (obj_id,))
            conn.commit()

    # --- LifeEvents CRUD ---
    def add_lifeevent(
        self, title: str, start_date: str, end_date: str, event_type: str,
        impact_description: str = ""
    ) -> int:
        """Adds a new life event and returns its ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO lifeevents (title, start_date, end_date, event_type, impact_description)
                VALUES (?, ?, ?, ?, ?)
            """, (title, start_date, end_date, event_type, impact_description))
            conn.commit()
            return int(cursor.lastrowid)

    def get_lifeevents(self, start_after: Optional[str] = None) -> List[LifeEvent]:
        """Fetches all life events, optionally active on or after a date."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_after:
                cursor.execute(
                    "SELECT * FROM lifeevents WHERE end_date >= ? ORDER BY start_date ASC",
                    (start_after,)
                )
            else:
                cursor.execute("SELECT * FROM lifeevents ORDER BY start_date ASC")
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_lifeevent(self, lifeevent_id: int) -> Optional[LifeEvent]:
        """Fetches a life event by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM lifeevents WHERE id = ?", (lifeevent_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def update_lifeevent(self, lifeevent_id: int, **kwargs: Any) -> None:
        """Updates life event properties in the database."""
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [lifeevent_id]
        with self._get_connection() as conn:
            conn.cursor().execute(f"UPDATE lifeevents SET {fields} WHERE id = ?", values)
            conn.commit()

    def delete_lifeevent(self, lifeevent_id: int) -> None:
        """Deletes a life event by ID."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM lifeevents WHERE id = ?", (lifeevent_id,))
            conn.commit()


    # --- Workouts CRUD ---
    def save_workout(
        self, date: str, sport_type: str, title: str, description: str,
        original_description: Optional[str] = None, status: str = 'planned',
        modification_reason: Optional[str] = None, google_event_id: Optional[str] = None,
        duration_minutes: Optional[int] = None, rpe: Optional[int] = None,
        tss: Optional[int] = None
    ) -> int:
        """Saves a workout, updating it if it already exists for the date/sport_type."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, google_event_id FROM workouts WHERE date = ? AND sport_type = ?",
                (date, sport_type)
            )
            row = cursor.fetchone()
            if row:
                workout_id = row['id']
                ge_id = google_event_id if google_event_id is not None else row['google_event_id']
                cursor.execute("""
                    UPDATE workouts
                    SET title = ?, description = ?,
                        original_description = COALESCE(?, original_description),
                        status = ?, modification_reason = ?, google_event_id = ?,
                        duration_minutes = COALESCE(?, duration_minutes),
                        rpe = COALESCE(?, rpe),
                        tss = COALESCE(?, tss)
                    WHERE id = ?
                """, (title, description, original_description, status,
                      modification_reason, ge_id, duration_minutes, rpe, tss,
                      workout_id))
            else:
                cursor.execute("""
                    INSERT INTO workouts (
                        date, sport_type, title, description, original_description,
                        status, modification_reason, google_event_id,
                        duration_minutes, rpe, tss
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (date, sport_type, title, description,
                      original_description or description, status,
                      modification_reason, google_event_id, duration_minutes,
                      rpe, tss))
                workout_id = cursor.lastrowid
            conn.commit()
            return int(workout_id)

    def get_workout(self, date: str, sport_type: str) -> Optional[Workout]:
        """Fetches a workout by date and sport type."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM workouts WHERE date = ? AND sport_type = ?",
                (date, sport_type)
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_workouts(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        sport_type: Optional[str] = None
    ) -> List[Workout]:
        """Fetches workouts ordered by date, optionally within a range or by sport type."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM workouts WHERE 1=1"
            params = []
            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND date <= ?"
                params.append(end_date)
            if sport_type:
                query += " AND LOWER(sport_type) = ?"
                params.append(sport_type.lower())
            query += " ORDER BY date ASC"
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def clear_future_workouts(self, from_date: str) -> None:
        """Deletes future workouts that are not already synced with Google Calendar."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "DELETE FROM workouts WHERE date >= ? AND status != 'synced'", (from_date,)
            )
            conn.commit()

    def get_workout_by_id(self, workout_id: int) -> Optional[Workout]:
        """Fetches a workout by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def delete_workout_by_id(self, workout_id: int) -> None:
        """Deletes a workout by ID."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
            conn.commit()

    def update_workout_date(
        self, workout_id: int, new_date: str, modification_reason: str
    ) -> None:
        """Moves a workout to a new date, flagging it as modified and recording why."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET date = ?, status = 'modified', "
                "modification_reason = ? WHERE id = ?",
                (new_date, modification_reason, workout_id)
            )
            conn.commit()

    # --- Completed Activities ---
    def save_completed_activity(
        self, activity_id: str, date: str, start_time: Optional[str],
        activity_name: Optional[str], activity_type: str, duration_sec: float,
        distance_km: float, elevation_gain_m: float, avg_hr: Optional[int],
        max_hr: Optional[int], rpe: int, tss: float,
        bike_avg_watts: Optional[int] = None, zone1_sec: Optional[int] = None,
        zone2_sec: Optional[int] = None, zone3_sec: Optional[int] = None,
        zone4_sec: Optional[int] = None, zone5_sec: Optional[int] = None
    ) -> None:
        """Saves a completed Garmin activity, updating it if it already exists."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO completed_activities (
                    activity_id, date, start_time, activity_name, activity_type,
                    duration_sec, distance_km, elevation_gain_m, avg_hr, max_hr,
                    rpe, tss, bike_avg_watts, zone1_sec, zone2_sec, zone3_sec,
                    zone4_sec, zone5_sec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    date=excluded.date,
                    start_time=excluded.start_time,
                    activity_name=excluded.activity_name,
                    activity_type=excluded.activity_type,
                    duration_sec=excluded.duration_sec,
                    distance_km=excluded.distance_km,
                    elevation_gain_m=excluded.elevation_gain_m,
                    avg_hr=excluded.avg_hr,
                    max_hr=excluded.max_hr,
                    rpe=excluded.rpe,
                    tss=excluded.tss,
                    bike_avg_watts=excluded.bike_avg_watts,
                    zone1_sec=excluded.zone1_sec,
                    zone2_sec=excluded.zone2_sec,
                    zone3_sec=excluded.zone3_sec,
                    zone4_sec=excluded.zone4_sec,
                    zone5_sec=excluded.zone5_sec
            """, (activity_id, date, start_time, activity_name, activity_type,
                  duration_sec, distance_km, elevation_gain_m, avg_hr, max_hr,
                  rpe, tss, bike_avg_watts, zone1_sec, zone2_sec, zone3_sec,
                  zone4_sec, zone5_sec))
            conn.commit()

    def get_completed_activities(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> List[CompletedActivity]:
        """Fetches completed activities, optionally within a date range."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_date and end_date:
                cursor.execute(
                    "SELECT * FROM completed_activities WHERE date >= ? AND date <= ? "
                    "ORDER BY date ASC, start_time ASC",
                    (start_date, end_date)
                )
            elif start_date:
                cursor.execute(
                    "SELECT * FROM completed_activities WHERE date >= ? "
                    "ORDER BY date ASC, start_time ASC",
                    (start_date,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM completed_activities "
                    "ORDER BY date ASC, start_time ASC"
                )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    # --- Athlete Metrics Cache ---
    def save_metric_cache(
        self, date: str, rhr: Optional[int], hrv: Optional[int],
        sleep_score: Optional[int], stress: Optional[int],
        acute_workload: Optional[float] = None, chronic_workload: Optional[float] = None,
        acwr: Optional[float] = None
    ) -> None:
        """Caches daily athlete metrics in the database, updating on conflict."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO athlete_metrics_cache (
                    date, rhr, hrv, sleep_score, stress, acute_workload,
                    chronic_workload, acwr
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rhr=excluded.rhr,
                    hrv=excluded.hrv,
                    sleep_score=excluded.sleep_score,
                    stress=excluded.stress,
                    acute_workload=COALESCE(
                        excluded.acute_workload, athlete_metrics_cache.acute_workload
                    ),
                    chronic_workload=COALESCE(
                        excluded.chronic_workload, athlete_metrics_cache.chronic_workload
                    ),
                    acwr=COALESCE(excluded.acwr, athlete_metrics_cache.acwr)
            """, (date, rhr, hrv, sleep_score, stress, acute_workload,
                  chronic_workload, acwr))
            conn.commit()

    def get_metrics_cache(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> List[AthleteMetric]:
        """Fetches cached metrics, optionally in a range."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_date and end_date:
                cursor.execute(
                    "SELECT * FROM athlete_metrics_cache "
                    "WHERE date >= ? AND date <= ? ORDER BY date ASC",
                    (start_date, end_date)
                )
            elif start_date:
                cursor.execute(
                    "SELECT * FROM athlete_metrics_cache WHERE date >= ? "
                    "ORDER BY date ASC", (start_date,)
                )
            else:
                cursor.execute("SELECT * FROM athlete_metrics_cache ORDER BY date ASC")
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    # --- Athlete Baselines ---
    def save_baseline(
        self, date: str, rhr_mean: float, rhr_std: float, hrv_mean: float,
        hrv_std: float, sleep_mean: float, sleep_std: float
    ) -> None:
        """Saves calculated athlete baseline values."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO athlete_baselines (
                    date, rhr_baseline_mean, rhr_baseline_std, hrv_baseline_mean,
                    hrv_baseline_std, sleep_baseline_mean, sleep_baseline_std
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rhr_baseline_mean=excluded.rhr_baseline_mean,
                    rhr_baseline_std=excluded.rhr_baseline_std,
                    hrv_baseline_mean=excluded.hrv_baseline_mean,
                    hrv_baseline_std=excluded.hrv_baseline_std,
                    sleep_baseline_mean=excluded.sleep_baseline_mean,
                    sleep_baseline_std=excluded.sleep_baseline_std
            """, (date, rhr_mean, rhr_std, hrv_mean, hrv_std, sleep_mean, sleep_std))
            conn.commit()

    def get_baseline(self, date: str) -> Optional[AthleteBaseline]:
        """Fetches baseline valid on or closest prior to the date."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM athlete_baselines WHERE date <= ? "
                "ORDER BY date DESC LIMIT 1", (date,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    # --- Coach Learnings ---
    def get_learnings(self) -> List[Dict[str, Any]]:
        """Returns all athlete-observation records ordered by id. Each record carries a
        computed `dormant` flag (True once it has decayed past its confidence budget)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, text, sports, confidence, created_at, updated_at, "
                "last_reinforced_at FROM coach_learnings ORDER BY id"
            )
            learnings = [dict(row) for row in cursor.fetchall()]
        now = datetime.now(timezone.utc)
        for learning in learnings:
            learning["dormant"] = learning_is_dormant(learning, now)
        return learnings

    def add_learning(
        self, text: str, sports: str = "general", confidence: str = "tentative"
    ) -> int:
        """Adds a single athlete-observation record and returns its id."""
        now = datetime.now(timezone.utc).isoformat()
        sports = normalize_sports(sports)
        confidence = valid_confidence(confidence) or "tentative"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO coach_learnings (text, sports, confidence, created_at, "
                "updated_at, last_reinforced_at) VALUES (?, ?, ?, ?, ?, ?)",
                (text, sports, confidence, now, now, now)
            )
            return cursor.lastrowid

    def update_learning(self, learning_id: int, text: str) -> None:
        """Revises the text of an existing observation record."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE coach_learnings SET text=?, updated_at=? WHERE id=?",
                (text, now, learning_id)
            )

    def delete_learning(self, learning_id: int) -> None:
        """Removes an observation record."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM coach_learnings WHERE id=?", (learning_id,))

    def apply_learning_deltas(self, deltas: List[Dict[str, Any]]) -> None:
        """Applies a list of incremental learning operations in one transaction.

        Each delta is one of:
          {"op": "add", "text": "...", "sports"?: "...", "confidence"?: "..."}
          {"op": "revise", "id": <int>, "text"?: "...", "sports"?: "...", "confidence"?: "..."}
          {"op": "reinforce", "id": <int>, "confidence"?: "..."}
          {"op": "retire", "id": <int>}
        `add` defaults sports to 'general' and confidence to 'tentative'. `revise` and
        `reinforce` refresh recency (last_reinforced_at), so a reaffirmed learning leaves
        the dormant state. Invalid confidence values and malformed deltas (empty text,
        missing id, unknown op) are skipped so a partially-valid response still applies.
        """
        if not deltas:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for delta in deltas:
                if not isinstance(delta, dict):
                    continue
                op = delta.get("op")
                if op == "add":
                    text = (delta.get("text") or "").strip()
                    if text:
                        cursor.execute(
                            "INSERT INTO coach_learnings (text, sports, confidence, "
                            "created_at, updated_at, last_reinforced_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (text, normalize_sports(delta.get("sports")),
                             valid_confidence(delta.get("confidence")) or "tentative",
                             now, now, now)
                        )
                elif op == "revise":
                    learning_id = delta.get("id")
                    if learning_id is None:
                        continue
                    # Apply only the fields the model supplied; a revise reflects fresh
                    # evidence, so refresh recency too.
                    sets, params = [], []
                    text = (delta.get("text") or "").strip()
                    if text:
                        sets.append("text=?")
                        params.append(text)
                    if delta.get("sports"):
                        sets.append("sports=?")
                        params.append(normalize_sports(delta.get("sports")))
                    confidence = valid_confidence(delta.get("confidence"))
                    if confidence:
                        sets.append("confidence=?")
                        params.append(confidence)
                    if not sets:
                        continue
                    sets += ["updated_at=?", "last_reinforced_at=?"]
                    params += [now, now, learning_id]
                    cursor.execute(
                        f"UPDATE coach_learnings SET {', '.join(sets)} WHERE id=?", params
                    )
                elif op == "reinforce":
                    learning_id = delta.get("id")
                    if learning_id is None:
                        continue
                    sets, params = ["last_reinforced_at=?"], [now]
                    confidence = valid_confidence(delta.get("confidence"))
                    if confidence:
                        sets += ["confidence=?", "updated_at=?"]
                        params += [confidence, now]
                    params.append(learning_id)
                    cursor.execute(
                        f"UPDATE coach_learnings SET {', '.join(sets)} WHERE id=?", params
                    )
                elif op == "retire":
                    learning_id = delta.get("id")
                    if learning_id is not None:
                        cursor.execute(
                            "DELETE FROM coach_learnings WHERE id=?", (learning_id,)
                        )

    # --- Macrocycles & Mesocycles ---
    def get_macrocycle_for_objective(self, objective_id: int) -> Optional[Macrocycle]:
        """Fetches the latest macrocycle created for a specific objective."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM macrocycles WHERE objective_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (objective_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_last_macrocycle(self) -> Optional[Macrocycle]:
        """Fetches the absolute latest macrocycle created."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM macrocycles ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_mesocycles_for_macrocycle(self, macrocycle_id: int) -> List[Mesocycle]:
        """Fetches all mesocycles in chronological order belonging to a macrocycle."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM mesocycles WHERE macrocycle_id = ? ORDER BY start_date ASC",
                (macrocycle_id,)
            )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_mesocycle(self, mesocycle_id: int) -> Optional[Mesocycle]:
        """Fetches a specific mesocycle by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM mesocycles WHERE id = ?", (mesocycle_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_active_mesocycle(self, target_date: str) -> Optional[Mesocycle]:
        """Finds the active mesocycle block for a given date.
        
        Falls back to the next future mesocycle, or the absolute first mesocycle
        if none contain or follow the target date. Only considers active objectives.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Primary: mesocycle containing target_date
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active'
                  AND m.start_date <= ? AND m.end_date >= ?
                ORDER BY m.start_date ASC LIMIT 1
            """, (target_date, target_date))
            row = cursor.fetchone()
            if row: return dict(row) # type: ignore

            # Fallback 1: first mesocycle that ends in the future
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active'
                  AND m.end_date >= ?
                ORDER BY m.start_date ASC LIMIT 1
            """, (target_date,))
            row = cursor.fetchone()
            if row: return dict(row) # type: ignore

            # Fallback 2: absolute first mesocycle
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active'
                ORDER BY m.start_date ASC LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None # type: ignore

    def update_macrocycle_feedback(self, macrocycle_id: int, feedback: str) -> None:
        """Saves user feedback for a specific macrocycle strategy."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "UPDATE macrocycles SET feedback = ? WHERE id = ?",
                (feedback, macrocycle_id)
            )
            conn.commit()

    def update_mesocycle_feedback(self, mesocycle_id: int, feedback: str) -> None:
        """Saves user feedback for a specific training phase/mesocycle block."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "UPDATE mesocycles SET feedback = ? WHERE id = ?",
                (feedback, mesocycle_id)
            )
            conn.commit()

    def save_macrocycle(
        self, objective_id: int, strategy: str, goals_hash: str,
        lifeevents_hash: str, mesocycles: List[Dict[str, Any]],
        config_hash: str = ""
    ) -> int:
        """Saves a macrocycle and its nested mesocycles for the objective."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Delete any existing macrocycles for this objective (cascade deletes mesocycles)
            cursor.execute("DELETE FROM macrocycles WHERE objective_id = ?", (objective_id,))
            
            created_at = datetime.now(timezone.utc).isoformat()
            cursor.execute("""
                INSERT INTO macrocycles (
                    objective_id, strategy, goals_hash, lifeevents_hash, config_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (objective_id, strategy, goals_hash, lifeevents_hash, config_hash, created_at))
            macrocycle_id = cursor.lastrowid
            
            for meso in mesocycles:
                cursor.execute("""
                    INSERT INTO mesocycles (macrocycle_id, name, start_date, end_date, focus)
                    VALUES (?, ?, ?, ?, ?)
                """, (macrocycle_id, meso['name'], meso['start_date'], meso['end_date'],
                      meso['focus']))
            
            conn.commit()
            return int(macrocycle_id)

    def update_macrocycle_config_hash(self, macrocycle_id: int, config_hash: str) -> None:
        """Updates the config hash for a specific macrocycle."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE macrocycles SET config_hash = ? WHERE id = ?",
                (config_hash, macrocycle_id)
            )
            conn.commit()

    def delete_macrocycle_for_objective(self, objective_id: int) -> None:
        """Deletes the macrocycle and its nested mesocycles for a specific objective."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "DELETE FROM macrocycles WHERE objective_id = ?",
                (objective_id,)
            )
            conn.commit()


    def wipe_objectives(self) -> None:
        """Deletes all objectives from the database."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM objectives")
            conn.commit()

    def wipe_lifeevents(self) -> None:
        """Deletes all life events from the database."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM lifeevents")
            conn.commit()

    def wipe_plans(self) -> None:
        """Deletes all macrocycles and mesocycles from the database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM mesocycles")
            cursor.execute("DELETE FROM macrocycles")
            conn.commit()

    def wipe_workouts(self) -> None:
        """Deletes all workouts from the database."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM workouts")
            conn.commit()

    def wipe_metrics(self) -> None:
        """Deletes all metrics, baselines, and completed activities from the database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM completed_activities")
            cursor.execute("DELETE FROM athlete_metrics_cache")
            cursor.execute("DELETE FROM athlete_baselines")
            conn.commit()

# Singleton instance
db = Database()
