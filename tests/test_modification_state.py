import os
import unittest

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_modstate.db")

from trainmate.db import Database
import trainmate.db
from trainmate.modification_state import modification_status


class TestModificationStatusPure(unittest.TestCase):
    """The kind of modification is derived from existing columns (no stored flag)."""

    def test_unmodified_when_no_reason(self):
        self.assertEqual(
            modification_status({"modification_reason": None}), "unmodified"
        )

    def test_adapted_when_summary_present(self):
        w = {
            "modification_reason": "easier intervals",
            "adaptation_summary": "Batch: autonomic suppression",
            "date": "2026-07-01", "original_date": "2026-07-01",
        }
        self.assertEqual(modification_status(w), "adapted")

    def test_swapped_when_date_moved_without_summary(self):
        w = {
            "modification_reason": "Swapped from 2026-07-01 to 2026-07-03",
            "adaptation_summary": None,
            "date": "2026-07-03", "original_date": "2026-07-01",
        }
        self.assertEqual(modification_status(w), "swapped")

    def test_replaced_when_in_place_manual(self):
        w = {
            "modification_reason": "Manually replaced previous session: Easy Run",
            "adaptation_summary": None,
            "date": "2026-07-01", "original_date": "2026-07-01",
            "source": "manual",
        }
        self.assertEqual(modification_status(w), "replaced")

    def test_adapted_takes_precedence_over_later_swap(self):
        # Adapted then swapped: summary persists, so it still reads as adapted.
        w = {
            "modification_reason": "Swapped from 2026-07-01 to 2026-07-03",
            "adaptation_summary": "Batch: deload week",
            "date": "2026-07-03", "original_date": "2026-07-01",
        }
        self.assertEqual(modification_status(w), "adapted")

    def test_missing_original_date_is_not_swapped(self):
        # Legacy row with no original_date must not be misread as a move.
        w = {
            "modification_reason": "Manually replaced previous session: Run",
            "adaptation_summary": None,
            "date": "2026-07-01", "original_date": None,
            "source": "manual",
        }
        self.assertEqual(modification_status(w), "replaced")

    def test_legacy_adapt_without_summary_reads_adapted(self):
        # A generated row modified before the rationale split: full reason in
        # modification_reason, no summary, no date move, not manual → still an adapt.
        w = {
            "modification_reason": "Severe autonomic suppression; converting to rest.",
            "adaptation_summary": None,
            "date": "2026-07-01", "original_date": "2026-07-01",
            "source": None,
        }
        self.assertEqual(modification_status(w), "adapted")


class TestModificationStatusViaDB(unittest.TestCase):
    """End-to-end: a swap through update_workout_date reads as `swapped`."""

    def setUp(self):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        self.db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = self.db

    def tearDown(self):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def test_swap_then_swap_back_clears_to_unmodified(self):
        wid = self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self.assertEqual(modification_status(self.db.get_workout_by_id(wid)), "unmodified")

        self.db.update_workout_date(wid, "2026-07-03", "Swapped from 2026-07-01 to 2026-07-03")
        self.assertEqual(modification_status(self.db.get_workout_by_id(wid)), "swapped")

        # Moving back to the original date clears modification_reason → unmodified.
        self.db.update_workout_date(wid, "2026-07-01", "Swapped back")
        self.assertEqual(modification_status(self.db.get_workout_by_id(wid)), "unmodified")


if __name__ == "__main__":
    unittest.main()
