"""The one-off rebuild of `workouts` into a revision log (DESIGN_workout_revisions.md §13).

It runs once, by hand, against the athlete's only database, so the parts that cannot be
retried — insertion order, the synthetic pre-revision that keeps the DO NOT COMPOUND guard
fed, the calendar handles — are pinned here against a hand-built pre-migration table.
"""
import importlib.util
import os
import sqlite3
import tempfile
import unittest

from tests.helpers import rebind_test_db
from trainmate.db import Database

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "migrate_workout_revisions.py",
)
_spec = importlib.util.spec_from_file_location("migrate_workout_revisions", _SCRIPT)
migrate_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_script)


OLD_TABLE = """
CREATE TABLE workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    sport_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    original_description TEXT,
    pushed_signature TEXT,
    marked_signature TEXT,
    modification_reason TEXT,
    adaptation_summary TEXT,
    google_event_id TEXT,
    removed INTEGER DEFAULT 0,
    removed_reason TEXT,
    source TEXT,
    duration_minutes INTEGER,
    rpe INTEGER,
    tss INTEGER,
    macrocycle_id INTEGER,
    archived_at TEXT,
    original_date TEXT,
    created_at TEXT,
    adapted_at TEXT,
    adaptation_count INTEGER DEFAULT 0,
    original_duration_minutes INTEGER,
    original_tss INTEGER,
    original_rpe INTEGER,
    benchmark_type TEXT,
    planned_zone_currency TEXT,
    planned_zone1_sec INTEGER,
    planned_zone2_sec INTEGER,
    planned_zone3_sec INTEGER,
    planned_zone4_sec INTEGER,
    planned_zone5_sec INTEGER,
    planned_zone6_sec INTEGER,
    planned_zone7_sec INTEGER
)
"""


def _insert(conn, **fields):
    columns = ", ".join(fields)
    placeholders = ", ".join("?" * len(fields))
    cursor = conn.execute(
        f"INSERT INTO workouts ({columns}) VALUES ({placeholders})", tuple(fields.values())
    )
    return int(cursor.lastrowid)


class TestMigrateWorkoutRevisions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "legacy.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(OLD_TABLE)
        self.conn.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        self.conn.execute("""
            CREATE TABLE benchmark_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
                sport_type TEXT NOT NULL, anchor_kind TEXT NOT NULL, value REAL NOT NULL,
                unit TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'test',
                workout_id INTEGER, note TEXT, created TEXT
            )
        """)

    def tearDown(self):
        self.conn.close()

    def _migrate(self):
        with self.conn:
            summary = migrate_script.migrate(self.conn)
        self.conn.close()
        db = Database(db_path=self.path)
        rebind_test_db(db)
        return summary, db

    def test_an_in_place_adapt_keeps_its_tally_and_its_recency(self):
        """A row adapted in place has no archived intermediate — adapt edited it — so its
        `original_*` columns are the only record it was ever walked down. Step 2 rebuilds
        that as a real pre-revision, or the guard forgets every eased session on
        migration day."""
        _insert(
            self.conn, date="2026-09-01", sport_type="cycling", title="Long ride",
            description="65 min", original_description="90 min",
            duration_minutes=65, tss=80, original_duration_minutes=90, original_tss=110,
            adaptation_count=3, adapted_at="2026-08-30T06:00:00+00:00",
            adaptation_summary="HRV suppressed", modification_reason="Eased",
            created_at="2026-08-20T06:00:00+00:00",
        )
        _summary, db = self._migrate()

        session = db.get_workout("2026-09-01", "cycling")
        self.assertEqual(session["duration_minutes"], 65)
        self.assertEqual(session["original_duration_minutes"], 90)
        self.assertEqual(session["original_tss"], 110)
        # A legacy count of 3 collapses to 1: the intermediate forms were overwritten in
        # place and there is nothing to rebuild them from (§16).
        self.assertEqual(session["adaptation_count"], 1)
        # `adapted_at`, the recency signal the guard weighs hardest, survives exactly.
        self.assertEqual(session["adapted_at"], "2026-08-30T06:00:00+00:00")
        self.assertEqual(session["change_kind"], "adapt")

    def test_a_rolled_back_slot_does_not_resurrect_its_dead_generation(self):
        """`workout rollback` revived old rows in place, so a slot ever rolled back holds
        a live row with a LOWER id than its archived siblings. Migrate ids as they are and
        the live view resurrects a dead generation as the plan (§13 step 4)."""
        _insert(
            self.conn, date="2026-09-02", sport_type="running", title="Restored Run",
            description="the plan in force", created_at="2026-08-01T06:00:00+00:00",
        )
        _insert(
            self.conn, date="2026-09-02", sport_type="running", title="Displaced Run",
            description="superseded", archived_at="2026-08-15T06:00:00+00:00",
            created_at="2026-08-10T06:00:00+00:00",
        )
        _summary, db = self._migrate()

        live = db.get_workouts()
        self.assertEqual([w["title"] for w in live], ["Restored Run"])
        # Both revisions are kept, in the same slot and the same lineage.
        revisions = db.get_plan_revisions()
        self.assertEqual([r["title"] for r in revisions],
                         ["Displaced Run", "Restored Run"])
        self.assertEqual(len({r["lineage_id"] for r in revisions}), 1)

    def test_a_manual_session_still_reads_as_the_athletes(self):
        """A manual session added onto an empty day carries no `modification_reason`, so
        the heuristic would call it unmodified and root it in a `generate` — and §5 would
        then derive its source as 'generated'. The override roots it in an `add`."""
        _insert(
            self.conn, date="2026-09-03", sport_type="yoga", title="Mobility",
            description="30 min", source="manual",
            created_at="2026-08-20T06:00:00+00:00",
        )
        _summary, db = self._migrate()

        session = db.get_workout("2026-09-03", "yoga")
        self.assertEqual(session["source"], "manual")
        self.assertEqual(session["change_kind"], "add")

    def test_a_removed_session_migrates_to_a_void_that_keeps_its_reason(self):
        _insert(
            self.conn, date="2026-09-04", sport_type="running", title="Tempo",
            description="40 min", removed=1, removed_reason="work trip",
            google_event_id="evt-removed", created_at="2026-08-20T06:00:00+00:00",
        )
        _summary, db = self._migrate()

        session = db.get_workout("2026-09-04", "running")
        self.assertTrue(session["removed"])
        self.assertEqual(session["removed_reason"], "work trip")
        self.assertEqual(session["google_event_id"], "evt-removed")
        self.assertEqual(db.get_workouts(), [])

    def test_calendar_state_follows_the_lineage_and_benchmarks_name_it(self):
        old_id = _insert(
            self.conn, date="2026-09-05", sport_type="cycling", title="FTP test",
            description="20 min test", benchmark_type="ftp_20min",
            google_event_id="evt-live", pushed_signature="sig", marked_signature="mark",
            created_at="2026-08-20T06:00:00+00:00",
        )
        self.conn.execute(
            "INSERT INTO benchmark_results (date, sport_type, anchor_kind, value, unit, "
            "workout_id) VALUES ('2026-09-05', 'cycling', 'ftp', 250, 'W', ?)",
            (old_id,),
        )
        _summary, db = self._migrate()

        session = db.get_workout("2026-09-05", "cycling")
        self.assertEqual(session["google_event_id"], "evt-live")
        self.assertEqual(session["pushed_signature"], "sig")
        self.assertEqual(session["adherence_pushed_signature"], "mark")
        self.assertEqual(session["benchmark_type"], "ftp_20min")
        result = db.get_benchmark_results()[0]
        self.assertEqual(result["workout_id"], session["id"])

    def test_the_migrated_table_is_sealed(self):
        _insert(
            self.conn, date="2026-09-06", sport_type="running", title="Run",
            description="30 min", created_at="2026-08-20T06:00:00+00:00",
        )
        _summary, db = self._migrate()
        with db._get_connection() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE workouts SET title = 'edited'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM workouts")

    def test_a_slot_the_plan_emptied_stays_empty(self):
        """Under the old model an empty day was expressed by absence: every row archived
        and nothing put back. The live view's rule is "newest revision wins", so carrying
        those rows over unchanged would put a session back on a day the plan had emptied.
        The closing void is what says that out loud (§13 step 4)."""
        _insert(
            self.conn, date="2026-09-08", sport_type="running", title="Dropped Run",
            description="the plan stopped scheduling this",
            archived_at="2026-08-15T06:00:00+00:00",
            created_at="2026-08-10T06:00:00+00:00",
        )
        _insert(
            self.conn, date="2026-09-09", sport_type="running", title="Kept Run",
            description="still on the plan", created_at="2026-08-10T06:00:00+00:00",
        )
        _summary, db = self._migrate()

        self.assertEqual([w["title"] for w in db.get_workouts()], ["Kept Run"])
        # It is a void the PLAN produced, not one the athlete asked for, so the coach is
        # not told the session was cancelled.
        dropped = db.get_workouts(include_removed=True)[0]
        self.assertTrue(dropped["removed"])
        self.assertEqual(dropped["change_kind"], "generate")

    def test_it_refuses_a_database_it_has_already_rebuilt(self):
        _insert(
            self.conn, date="2026-09-07", sport_type="running", title="Run",
            description="30 min", created_at="2026-08-20T06:00:00+00:00",
        )
        _summary, db = self._migrate()
        with db._get_connection() as conn:
            self.assertTrue(migrate_script.already_migrated(conn))


if __name__ == "__main__":
    unittest.main()
