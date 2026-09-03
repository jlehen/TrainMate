import os
import unittest
from unittest.mock import Mock

from tests.helpers import clear_all_tables, rebind_test_db, save_workout
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_adaptation_add.db")

from trainmate import runtime
from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service


class TestAdaptationAdd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_workout_add_new_inserts_and_syncs(self):
        syncer = Mock()
        service = trainmate.coach.CoachService(db_instance=test_db)
        runtime.calendar_syncer = syncer
        syncer.reset_mock()
        saved, replaced = service.workout_add(
            "2026-06-20", "running", "Long Run", "90min Z2",
            duration_minutes=90, tss=70,
        )
        self.assertEqual(replaced, [])
        self.assertEqual(saved["title"], "Long Run")
        self.assertEqual(saved["tss"], 70)
        # A brand-new session that replaced nothing is not flagged as modified.
        self.assertIsNone(saved["modification_reason"])
        syncer.sync_workout.assert_called_once()

    def test_workout_add_replaces_and_records_overwritten_session(self):
        syncer = Mock()
        service = trainmate.coach.CoachService(db_instance=test_db)
        runtime.calendar_syncer = syncer
        save_workout(test_db,
            "2026-06-20", "running", "Tempo Intervals", "6x3min Z4",
            duration_minutes=60, rpe=8, tss=85, google_event_id="evt-123",
        )

        syncer.reset_mock()

        saved, replaced = service.workout_add(
            "2026-06-20", "running", "Easy Recovery", "40min Z2",
            duration_minutes=40, tss=30, reason="legs feel cooked",
        )

        self.assertEqual(len(replaced), 1)
        self.assertEqual(saved["title"], "Easy Recovery")
        # New session's own stats win — the replaced session's RPE is not inherited.
        self.assertEqual(saved["tss"], 30)
        self.assertEqual(saved["duration_minutes"], 40)
        self.assertIsNone(saved["rpe"])
        # A manual session is a NEW session, so its "originally" is its own first form,
        # not the one it displaced (DESIGN_workout_revisions.md §4/§12). What it replaced
        # is recorded in the note below instead.
        self.assertEqual(saved["original_description"], "40min Z2")
        self.assertEqual(saved["source"], "manual")
        # The replaced session's title + stats and the athlete's reason are captured.
        reason = saved["modification_reason"]
        self.assertIn("Tempo Intervals", reason)
        self.assertIn("60m", reason)
        self.assertIn("TSS 85", reason)
        self.assertIn("RPE 8", reason)
        self.assertIn("legs feel cooked", reason)
        # The replaced session's event is torn down and the new session gets its own: a
        # new lineage must not inherit the Calendar event of the one it replaced (§4).
        syncer.delete_workout_event.assert_called_once_with("evt-123")
        syncer.sync_workout.assert_called_once()
        # Only one row remains for that date/sport (replace, not double).
        self.assertEqual(
            len(test_db.get_workouts(start_date="2026-06-20", end_date="2026-06-20")), 1
        )

    def test_workout_add_default_keeps_other_sports(self):
        syncer = Mock()
        service = trainmate.coach.CoachService(db_instance=test_db)
        runtime.calendar_syncer = syncer
        save_workout(test_db,
            "2026-06-21", "yoga", "Mobility", "30min", google_event_id="evt-yoga",
        )

        syncer.reset_mock()

        saved, replaced = service.workout_add(
            "2026-06-21", "strength", "KB HIIT", "circuit",
        )

        # Different sport: the yoga session is untouched, no calendar deletes.
        self.assertEqual(replaced, [])
        self.assertIsNone(saved["modification_reason"])
        syncer.delete_workout_event.assert_not_called()
        self.assertEqual(
            len(test_db.get_workouts(start_date="2026-06-21", end_date="2026-06-21")), 2
        )

    def test_workout_add_replace_day_clears_all_sports(self):
        syncer = Mock()
        service = trainmate.coach.CoachService(db_instance=test_db)
        runtime.calendar_syncer = syncer
        save_workout(test_db,
            "2026-06-22", "yoga", "Mobility", "30min Z1",
            duration_minutes=30, google_event_id="evt-yoga",
        )
        save_workout(test_db,
            "2026-06-22", "strength", "Old Lift", "5x5",
            duration_minutes=45, rpe=7, google_event_id="evt-str",
        )

        syncer.reset_mock()

        saved, replaced = service.workout_add(
            "2026-06-22", "strength", "KB HIIT", "circuit",
            duration_minutes=40, reason="travel day",
            replace_day=True,
        )

        # Both prior sessions are replaced; only the new one remains.
        self.assertEqual(len(replaced), 2)
        self.assertEqual(
            len(test_db.get_workouts(start_date="2026-06-22", end_date="2026-06-22")), 1
        )
        # Both replaced sessions' events are torn down; the new one gets its own (§4).
        self.assertEqual(
            sorted(c.args[0] for c in syncer.delete_workout_event.call_args_list),
            ["evt-str", "evt-yoga"],
        )
        # Both replaced sessions are recorded; the other sport is labelled.
        reason = saved["modification_reason"]
        self.assertIn("Old Lift", reason)
        self.assertIn("yoga Mobility", reason)
        self.assertIn("travel day", reason)
