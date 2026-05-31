import sqlite3
import os
from datetime import datetime, timezone
from trainmate.config import config

class Database:
    def __init__(self, db_path=None):
        self.db_path = db_path or config.db_path
        self._init_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
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
            
            # Life events table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS life_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    event_type TEXT NOT NULL, -- 'injury', 'vacation', 'party', 'other'
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
            
            # Coach memory table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS coach_memory (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                )
            """)
            
            conn.commit()

    # --- Objectives CRUD ---
    def add_objective(self, title, target_date, sport_type, description="", priority=1, status='active'):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO objectives (title, target_date, sport_type, description, priority, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (title, target_date, sport_type, description, priority, status))
            conn.commit()
            return cursor.lastrowid

    def get_objectives(self, status=None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute("SELECT * FROM objectives WHERE status = ? ORDER BY target_date ASC", (status,))
            else:
                cursor.execute("SELECT * FROM objectives ORDER BY target_date ASC")
            return [dict(row) for row in cursor.fetchall()]

    def update_objective(self, obj_id, **kwargs):
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [obj_id]
        with self._get_connection() as conn:
            conn.cursor().execute(f"UPDATE objectives SET {fields} WHERE id = ?", values)
            conn.commit()

    def delete_objective(self, obj_id):
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM objectives WHERE id = ?", (obj_id,))
            conn.commit()

    # --- Life Events CRUD ---
    def add_life_event(self, title, start_date, end_date, event_type, impact_description=""):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO life_events (title, start_date, end_date, event_type, impact_description)
                VALUES (?, ?, ?, ?, ?)
            """, (title, start_date, end_date, event_type, impact_description))
            conn.commit()
            return cursor.lastrowid

    def get_life_events(self, start_after=None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_after:
                cursor.execute("SELECT * FROM life_events WHERE end_date >= ? ORDER BY start_date ASC", (start_after,))
            else:
                cursor.execute("SELECT * FROM life_events ORDER BY start_date ASC")
            return [dict(row) for row in cursor.fetchall()]

    def delete_life_event(self, event_id):
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM life_events WHERE id = ?", (event_id,))
            conn.commit()

    # --- Workouts CRUD ---
    def save_workout(self, date, sport_type, title, description, original_description=None, status='planned', modification_reason=None, google_event_id=None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Check if workout already exists for this date and sport_type
            cursor.execute("SELECT id, google_event_id FROM workouts WHERE date = ? AND sport_type = ?", (date, sport_type))
            row = cursor.fetchone()
            if row:
                workout_id = row['id']
                # Keep existing google_event_id if not provided
                ge_id = google_event_id if google_event_id is not None else row['google_event_id']
                cursor.execute("""
                    UPDATE workouts
                    SET title = ?, description = ?, original_description = COALESCE(?, original_description), status = ?, modification_reason = ?, google_event_id = ?
                    WHERE id = ?
                """, (title, description, original_description, status, modification_reason, ge_id, workout_id))
            else:
                cursor.execute("""
                    INSERT INTO workouts (date, sport_type, title, description, original_description, status, modification_reason, google_event_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (date, sport_type, title, description, original_description or description, status, modification_reason, google_event_id))
                workout_id = cursor.lastrowid
            conn.commit()
            return workout_id

    def get_workout(self, date, sport_type):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM workouts WHERE date = ? AND sport_type = ?", (date, sport_type))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_workouts(self, start_date=None, end_date=None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_date and end_date:
                cursor.execute("SELECT * FROM workouts WHERE date >= ? AND date <= ? ORDER BY date ASC", (start_date, end_date))
            elif start_date:
                cursor.execute("SELECT * FROM workouts WHERE date >= ? ORDER BY date ASC", (start_date,))
            else:
                cursor.execute("SELECT * FROM workouts ORDER BY date ASC")
            return [dict(row) for row in cursor.fetchall()]

    def clear_future_workouts(self, from_date):
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM workouts WHERE date >= ? AND status != 'synced'", (from_date,))
            conn.commit()

    # --- Athlete Metrics Cache ---
    def save_metric_cache(self, date, rhr, hrv, sleep_score, stress, acute_workload=None, chronic_workload=None, acwr=None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO athlete_metrics_cache (date, rhr, hrv, sleep_score, stress, acute_workload, chronic_workload, acwr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rhr=excluded.rhr,
                    hrv=excluded.hrv,
                    sleep_score=excluded.sleep_score,
                    stress=excluded.stress,
                    acute_workload=COALESCE(excluded.acute_workload, athlete_metrics_cache.acute_workload),
                    chronic_workload=COALESCE(excluded.chronic_workload, athlete_metrics_cache.chronic_workload),
                    acwr=COALESCE(excluded.acwr, athlete_metrics_cache.acwr)
            """, (date, rhr, hrv, sleep_score, stress, acute_workload, chronic_workload, acwr))
            conn.commit()

    def get_metrics_cache(self, start_date=None, end_date=None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_date and end_date:
                cursor.execute("SELECT * FROM athlete_metrics_cache WHERE date >= ? AND date <= ? ORDER BY date ASC", (start_date, end_date))
            elif start_date:
                cursor.execute("SELECT * FROM athlete_metrics_cache WHERE date >= ? ORDER BY date ASC", (start_date,))
            else:
                cursor.execute("SELECT * FROM athlete_metrics_cache ORDER BY date ASC")
            return [dict(row) for row in cursor.fetchall()]

    # --- Athlete Baselines ---
    def save_baseline(self, date, rhr_mean, rhr_std, hrv_mean, hrv_std, sleep_mean, sleep_std):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO athlete_baselines (date, rhr_baseline_mean, rhr_baseline_std, hrv_baseline_mean, hrv_baseline_std, sleep_baseline_mean, sleep_baseline_std)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rhr_baseline_mean=excluded.rhr_baseline_mean,
                    rhr_baseline_std=excluded.rhr_baseline_std,
                    hrv_baseline_mean=excluded.hrv_baseline_mean,
                    hrv_baseline_std=excluded.hrv_baseline_std,
                    sleep_baseline_mean=excluded.sleep_baseline_mean,
                    sleep_baseline_std=excluded.sleep_baseline_std
            """, (date, rhr_mean, rhr_std, hrv_mean, hrv_std, sleep_mean, sleep_std))
            conn.commit()

    def get_baseline(self, date):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM athlete_baselines WHERE date <= ? ORDER BY date DESC LIMIT 1", (date,))
            row = cursor.fetchone()
            return dict(row) if row else None

    # --- Coach Memory ---
    def save_coach_memory(self, key, value):
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO coach_memory (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
            """, (key, value, updated_at))
            conn.commit()

    def get_coach_memory(self, key):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM coach_memory WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row['value'] if row else None

# Singleton instance
db = Database()
