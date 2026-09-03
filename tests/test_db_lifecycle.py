"""Schema stamping, transactions, and the invariants that used to live at the CLI."""
import hashlib
import os
import sqlite3
import tempfile
import unittest

from tests.helpers import bind_test_db, clear_all_tables, unstamp_schema
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_db_lifecycle.db")

from trainmate.db import Database
from trainmate.db.base import SCHEMA_VERSION

test_db = bind_test_db(TEST_DB_PATH)


class TestSchemaStamping(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "stamp.db")

    def test_a_fresh_database_is_stamped_at_the_current_version(self):
        db = Database(db_path=self.path)
        with db._get_connection() as conn:
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)

    def test_a_stamped_database_skips_the_migrations(self):
        """The 630-line DDL block used to run on every process start — for `--help`, and
        against the athlete's real file."""
        Database(db_path=self.path)

        # sqlite3's own tracer sees every statement the connection runs.
        statements = []
        real_connect = sqlite3.connect

        def tracing_connect(*a, **k):
            conn = real_connect(*a, **k)
            conn.set_trace_callback(statements.append)
            return conn

        sqlite3.connect = tracing_connect
        try:
            Database(db_path=self.path)
        finally:
            sqlite3.connect = real_connect

        # `schema_version` itself is created before it can be read; everything else
        # belongs to the migration block that should not have run.
        ddl = [
            s for s in statements
            if ("CREATE TABLE" in s or "ALTER TABLE" in s) and "schema_version" not in s
        ]
        self.assertEqual(ddl, [], f"re-ran schema work on an up-to-date database: {ddl}")

    def test_changing_the_schema_forces_a_version_bump(self):
        """`date_type` shipped without bumping SCHEMA_VERSION past the value the previous
        commit had already used, so every stamped database skipped the ALTER and crashed on
        the first goal edit. Fresh databases — every other test — were unaffected.

        If this fails because you added or removed a column: bump SCHEMA_VERSION and put
        the new pair below.
        """
        db = Database(db_path=self.path)
        with db._get_connection() as conn:
            tables = sorted(
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
            columns = [
                f"{t}.{r[1]}"
                for t in tables
                for r in conn.execute(f"PRAGMA table_info({t})")
            ]
        fingerprint = hashlib.sha256("\n".join(columns).encode()).hexdigest()[:16]

        self.assertEqual(
            (SCHEMA_VERSION, fingerprint), (10, "fb497c39cefb31fd"),
            "the schema changed without a matching SCHEMA_VERSION bump — existing "
            "databases would skip the migration",
        )

    def test_clearing_the_stamp_reruns_them(self):
        db = Database(db_path=self.path)
        with db._get_connection() as conn:
            conn.execute("DROP TABLE workouts")
            conn.commit()
        unstamp_schema(db)

        Database(db_path=self.path)

        with db._get_connection() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertIn("workouts", tables)

    def test_the_signal_rename_carries_the_rows_and_the_sync_token(self):
        """`daily_context` → `daily_signals` runs once, against the only database that
        exists, so the rows and the Calendar syncToken have to survive it — a fresh
        CREATE TABLE instead of the ALTER would silently empty the signal history and
        force a full re-pull (DESIGN_calendar_signal_ingest.md §6.1)."""
        db = Database(db_path=self.path)
        with db._get_connection() as conn:
            conn.execute("DROP TABLE daily_signals")
            conn.execute("""
                CREATE TABLE daily_context (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    date            TEXT NOT NULL,
                    metric          TEXT NOT NULL,
                    value           REAL,
                    text            TEXT,
                    google_event_id TEXT NOT NULL UNIQUE,
                    updated         TEXT
                )
            """)
            conn.execute(
                "INSERT INTO daily_context (date, metric, value, text, google_event_id) "
                "VALUES ('2026-06-10', 'alcohol', 2.0, 'Alcohol: 2.0', 'evt-1')"
            )
            conn.execute(
                "INSERT INTO sync_state (key, sync_token) VALUES ('calendar_context', 'tok')"
            )
            conn.commit()
        unstamp_schema(db)

        Database(db_path=self.path)

        with db._get_connection() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            rows = conn.execute(
                "SELECT metric, value FROM daily_signals"
            ).fetchall()
            token = conn.execute(
                "SELECT sync_token FROM sync_state WHERE key = 'calendar_signals'"
            ).fetchone()
        self.assertNotIn("daily_context", tables)
        self.assertEqual([tuple(r) for r in rows], [("alcohol", 2.0)])
        self.assertEqual(token[0], "tok")


class TestTransaction(unittest.TestCase):
    def setUp(self):
        clear_all_tables(test_db)

    def test_writes_inside_one_transaction_share_a_connection(self):
        opened = []
        real_connect = sqlite3.connect

        def counting_connect(*a, **k):
            opened.append(a[0] if a else k.get("database"))
            return real_connect(*a, **k)

        sqlite3.connect = counting_connect
        try:
            with test_db.transaction():
                for day in range(1, 6):
                    test_db.save_metric_cache(
                        date=f"2026-06-0{day}", rhr=50, hrv=70, sleep_score=80, stress=20
                    )
        finally:
            sqlite3.connect = real_connect

        self.assertEqual(len(opened), 1, f"expected one connection, opened {len(opened)}")
        self.assertEqual(len(test_db.get_metrics_cache()), 5)

    def test_a_failure_rolls_the_whole_unit_back(self):
        """Plan regeneration archives, supersedes, inserts and pushes; a crash part-way
        used to leave archived workouts with no active plan."""
        with self.assertRaises(RuntimeError):
            with test_db.transaction():
                test_db.save_metric_cache(
                    date="2026-07-01", rhr=50, hrv=70, sleep_score=80, stress=20
                )
                raise RuntimeError("interrupted midway")

        self.assertEqual(test_db.get_metrics_cache(), [])

    def test_nesting_joins_the_outer_transaction(self):
        """A method that opens a transaction stays safe to call from inside one."""
        with test_db.transaction():
            test_db.save_metric_cache(
                date="2026-07-02", rhr=50, hrv=70, sleep_score=80, stress=20
            )
            with test_db.transaction():
                test_db.save_metric_cache(
                    date="2026-07-03", rhr=51, hrv=71, sleep_score=81, stress=21
                )
        self.assertEqual(len(test_db.get_metrics_cache()), 2)


class TestWipingOwnsItsRecompute(unittest.TestCase):
    """The post-wipe PMC sweep was enforced by one CLI call site plus one test; any
    other caller silently left deleted load baked into later days' CTL/ATL."""

    def setUp(self):
        clear_all_tables(test_db)

    def _seed(self):
        for day in range(1, 11):
            date = f"2026-06-{day:02d}"
            test_db.save_metric_cache(
                date=date, rhr=50, hrv=70, sleep_score=80, stress=20
            )
            test_db.save_completed_activity(
                activity_id=f"a{day}", date=date, start_time=f"{date} 08:00:00",
                activity_name="Run", activity_type="running", duration_sec=3600.0,
                distance_km=10.0, elevation_gain_m=0.0, avg_hr=140, max_hr=160,
                rpe=5, tss=50.0,
            )
        from trainmate.garmin.pmc import recompute_derived
        recompute_derived(dbh=test_db)

    def test_wiping_the_window_walks_the_ewmas_again(self):
        self._seed()
        before = {m["date"]: m["ctl"] for m in test_db.get_metrics_cache()}
        self.assertTrue(any(v for v in before.values()), "no CTL to compare against")

        # Remove the first half's evidence; the later days' CTL must fall.
        test_db.wipe_garmin_data("2026-06-01", "2026-06-05")

        after = {m["date"]: m["ctl"] for m in test_db.get_metrics_cache()}
        self.assertTrue(after, "the later days should survive the ranged wipe")
        for date, ctl in after.items():
            self.assertLess(
                ctl, before[date],
                f"{date} still carries load from the wiped days (CTL {ctl} vs {before[date]})",
            )


if __name__ == "__main__":
    unittest.main()
