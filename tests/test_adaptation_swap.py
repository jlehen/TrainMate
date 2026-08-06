import os
import unittest
from unittest.mock import Mock

from tests.helpers import clear_all_tables, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_adaptation_swap.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service


class TestAdaptationSwap(unittest.TestCase):
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

    def _swap_ops_for_dates(self, date1, date2):
        """Builds swap ops the way the CLI does: exchange all workouts on two dates."""
        on_1 = test_db.get_workouts(start_date=date1, end_date=date1)
        on_2 = test_db.get_workouts(start_date=date2, end_date=date2)
        return (
            [{"id": w["id"], "new_date": date2} for w in on_1]
            + [{"id": w["id"], "new_date": date1} for w in on_2]
        )

    def test_swap_validation_consecutive_hard_days(self):
        # Week of Mon 2026-06-08. Hard on Mon/Tue, easy Wed, hard Thu.
        test_db.save_workout("2026-06-08", "running", "Intervals", "hard", rpe=8, tss=80)
        test_db.save_workout("2026-06-09", "cycling", "Threshold", "hard", rpe=8, tss=90)
        test_db.save_workout("2026-06-10", "yoga", "Mobility", "easy", rpe=2, tss=10)
        test_db.save_workout("2026-06-11", "running", "Tempo", "hard", rpe=8, tss=85)

        # Swapping the easy Wed with the hard Thu makes Mon-Tue-Wed three hard days.
        ops = self._swap_ops_for_dates("2026-06-10", "2026-06-11")
        warnings = coach_service.workout_swap_validate(ops)
        self.assertTrue(
            any("consecutive high-intensity" in w for w in warnings),
            f"expected a consecutive-hard-days warning, got {warnings}",
        )

    def test_swap_validation_safe_swap(self):
        # Two easy days far from any hard block: swapping them is harmless.
        test_db.save_workout("2026-06-10", "yoga", "Mobility", "easy", rpe=2, tss=10)
        test_db.save_workout("2026-06-12", "running", "Recovery", "easy", rpe=3, tss=15)
        ops = self._swap_ops_for_dates("2026-06-10", "2026-06-12")
        self.assertEqual(coach_service.workout_swap_validate(ops), [])

    def test_swap_validation_weekly_load_spike(self):
        # Cross-week swap that shifts a big TSS session into a light week.
        test_db.save_workout("2026-06-08", "running", "Long", "big", rpe=6, tss=120)
        test_db.save_workout("2026-06-09", "cycling", "Long Ride", "big", rpe=6, tss=130)
        test_db.save_workout("2026-06-15", "yoga", "Mobility", "easy", rpe=2, tss=40)

        ops = self._swap_ops_for_dates("2026-06-09", "2026-06-15")
        warnings = coach_service.workout_swap_validate(ops)
        self.assertTrue(
            any("spike your acute load" in w for w in warnings),
            f"expected a weekly load-spike warning, got {warnings}",
        )

    def test_apply_swap_moves_dates_and_syncs(self):
        a = test_db.save_workout("2026-06-10", "running", "Run A", "a", rpe=4, tss=30)
        b = test_db.save_workout("2026-06-12", "cycling", "Ride B", "b", rpe=4, tss=30)
        ops = [
            {"id": a, "new_date": "2026-06-12"},
            {"id": b, "new_date": "2026-06-10"},
        ]
        syncer = Mock()
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=syncer
        )
        updated = service.workout_swap_apply(ops, no_sync=False)

        self.assertEqual(test_db.get_workout_by_id(a)["date"], "2026-06-12")
        self.assertEqual(test_db.get_workout_by_id(b)["date"], "2026-06-10")
        self.assertIsNotNone(test_db.get_workout_by_id(a)["modification_reason"])
        self.assertEqual(len(updated), 2)
        self.assertEqual(syncer.sync_workout.call_count, 2)

    def test_apply_swap_records_reason(self):
        a = test_db.save_workout("2026-06-10", "running", "Run A", "a", rpe=4, tss=30)
        b = test_db.save_workout("2026-06-12", "cycling", "Ride B", "b", rpe=4, tss=30)
        ops = [
            {"id": a, "new_date": "2026-06-12"},
            {"id": b, "new_date": "2026-06-10"},
        ]
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=Mock()
        )
        service.workout_swap_apply(ops, no_sync=True, reason="knee felt sore")
        for wid in (a, b):
            mr = test_db.get_workout_by_id(wid)["modification_reason"]
            self.assertIn("Swapped from", mr)
            self.assertIn("Reason given: knee felt sore", mr)

    def test_apply_swap_no_sync(self):
        a = test_db.save_workout("2026-06-10", "running", "Run A", "a", rpe=4, tss=30)
        ops = [{"id": a, "new_date": "2026-06-11"}]
        syncer = Mock()
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=syncer
        )
        service.workout_swap_apply(ops, no_sync=True)
        self.assertEqual(test_db.get_workout_by_id(a)["date"], "2026-06-11")
        syncer.sync_workout.assert_not_called()

    def test_apply_swap_back_clears_modified(self):
        """Swapping two workouts and then swapping them back should clear
        the modification flag on both — they're back on their original dates."""
        a = test_db.save_workout(
            "2026-06-10", "running", "Run A", "a", rpe=4, tss=30
        )
        b = test_db.save_workout(
            "2026-06-12", "cycling", "Ride B", "b", rpe=4, tss=30
        )
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=Mock()
        )

        # First swap: A→12, B→10
        service.workout_swap_apply(
            [{"id": a, "new_date": "2026-06-12"},
             {"id": b, "new_date": "2026-06-10"}],
            no_sync=True,
        )
        self.assertIsNotNone(
            test_db.get_workout_by_id(a)["modification_reason"]
        )
        self.assertIsNotNone(
            test_db.get_workout_by_id(b)["modification_reason"]
        )

        # Swap back: A→10, B→12 (original dates)
        service.workout_swap_apply(
            [{"id": a, "new_date": "2026-06-10"},
             {"id": b, "new_date": "2026-06-12"}],
            no_sync=True,
        )
        self.assertIsNone(
            test_db.get_workout_by_id(a)["modification_reason"],
            "A is back on its original date; modification_reason should "
            "be cleared",
        )
        self.assertIsNone(
            test_db.get_workout_by_id(b)["modification_reason"],
            "B is back on its original date; modification_reason should "
            "be cleared",
        )

