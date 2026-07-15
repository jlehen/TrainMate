import os
import unittest

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_calstate.db")

from trainmate.db import Database
import trainmate.db
from trainmate.calendar_state import calendar_signature, calendar_status


class TestCalendarState(unittest.TestCase):
    """Freshness (unpushed/synced/stale) is derived from `pushed_signature` vs the live
    content hash — no hand-maintained flag. See trainmate.calendar_state."""

    def setUp(self):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        # Restore the singleton in tearDown: other modules resolve the db through the
        # live `trainmate.db.db` (or capture it lazily), so leaving it pointed at this
        # test's Database — whose file we delete below — breaks later tests.
        self._orig_db = trainmate.db.db
        self.db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = self.db

    def tearDown(self):
        trainmate.db.db = self._orig_db
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def _row(self, wid):
        return self.db.get_workout_by_id(wid)

    def _push(self, wid, event_id="evt-1"):
        """Simulate a successful Calendar push for the workout's current content."""
        self.db.mark_workout_pushed(wid, event_id, calendar_signature(self._row(wid)))

    def test_unpushed_until_first_push(self):
        wid = self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self.assertEqual(calendar_status(self._row(wid)), "unpushed")

    def test_synced_after_push(self):
        wid = self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        self.assertEqual(calendar_status(self._row(wid)), "synced")

    def test_content_edit_makes_stale(self):
        wid = self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        # Edit a calendar-relevant field through the normal write path.
        self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="HARD intervals",
        )
        self.assertEqual(calendar_status(self._row(wid)), "stale")

    def test_rpe_edit_stays_synced(self):
        """rpe never reaches Calendar, so changing it must NOT mark the row stale —
        the win over the old `synced` flag, which save_workout would have reset."""
        wid = self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy", rpe=3,
        )
        self._push(wid)
        self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy", rpe=8,
        )
        self.assertEqual(calendar_status(self._row(wid)), "synced")

    def test_reschedule_makes_stale(self):
        wid = self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        self.db.update_workout_date(wid, "2026-07-03", "Travelling")
        self.assertEqual(calendar_status(self._row(wid)), "stale")

    def test_remove_and_restore_toggle_stale(self):
        wid = self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        self.db.mark_workout_removed(wid, reason="Sick")
        # Removed rows are excluded from default reads; fetch by id.
        self.assertEqual(calendar_status(self._row(wid)), "stale")
        # Re-push the removed state, then restore: back to stale again.
        self._push(wid)
        self.assertEqual(calendar_status(self._row(wid)), "synced")
        self.db.restore_workout(wid)
        self.assertEqual(calendar_status(self._row(wid)), "stale")

    def test_generated_workout_synced_after_eager_push(self):
        """A freshly generated workout is pushed from its persisted row (source='generated').
        Regression: a hand-built sync dict once omitted `source`, so the push-time hash was
        computed with source=None while the stored row had 'generated' — every generated
        workout read stale the instant it synced. Pushing the row itself keeps them equal."""
        wid = self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy",
            source="generated",
        )
        # Mirror workout_generate's eager sync: sign the persisted row, not a partial dict.
        self._push(wid)
        self.assertEqual(calendar_status(self._row(wid)), "synced")

    def test_repush_returns_to_synced(self):
        wid = self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        self.db.save_workout(
            date="2026-07-01", sport_type="running", title="Run", description="changed",
        )
        self.assertEqual(calendar_status(self._row(wid)), "stale")
        self._push(wid)
        self.assertEqual(calendar_status(self._row(wid)), "synced")


if __name__ == "__main__":
    unittest.main()
