from tests.helpers import save_workout
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
        wid = save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self.assertEqual(calendar_status(self._row(wid)), "unpushed")

    def test_synced_after_push(self):
        wid = save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        self.assertEqual(calendar_status(self._row(wid)), "synced")

    def test_content_edit_makes_stale(self):
        wid = save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        # Edit a calendar-relevant field through the normal write path.
        save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="HARD intervals",
        )
        self.assertEqual(calendar_status(self._row(wid)), "stale")

    def test_rpe_edit_makes_stale(self):
        """rpe DOES reach Calendar — the event carries a `Duration | TSS | RPE` line — so
        editing it leaves the event wrong until it is re-pushed
        (DESIGN_calendar_lineage.md §6)."""
        wid = save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="easy", rpe=3,
        )
        self._push(wid)
        save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="easy", rpe=8,
        )
        self.assertEqual(calendar_status(self._row(wid)), "stale")

    def test_reschedule_makes_stale(self):
        wid = save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        with self.db.workout_change(kind="swap") as change:
            change.append(
                date="2026-07-03", sport_type="running", title="Run",
                description="easy", lineage_id=wid, reason="Travelling",
            )
        self.assertEqual(calendar_status(self._row(wid)), "stale")

    def test_remove_and_restore_toggle_stale(self):
        wid = save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        with self.db.workout_change(kind="rm") as change:
            change.void(date="2026-07-01", sport_type="running", reason="Sick")
        # Cancelled sessions are excluded from default reads; fetch by id.
        self.assertEqual(calendar_status(self._row(wid)), "stale")
        # Re-push the cancelled state, then restore: back to stale again.
        self._push(wid)
        self.assertEqual(calendar_status(self._row(wid)), "synced")
        with self.db.workout_change(kind="restore") as change:
            change.restore(self.db.revision_before_live_void(wid))
        self.assertEqual(calendar_status(self._row(wid)), "stale")

    def test_generated_workout_synced_after_eager_push(self):
        """A freshly generated workout is pushed from its persisted row (source='generated').
        Regression: a hand-built sync dict once omitted `source`, so the push-time hash was
        computed with source=None while the stored row had 'generated' — every generated
        workout read stale the instant it synced. Pushing the row itself keeps them equal."""
        wid = save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="easy",
            source="generated",
        )
        # Mirror workout_generate's eager sync: sign the persisted row, not a partial dict.
        self._push(wid)
        self.assertEqual(calendar_status(self._row(wid)), "synced")

    def test_repush_returns_to_synced(self):
        wid = save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="easy",
        )
        self._push(wid)
        save_workout(self.db,
            date="2026-07-01", sport_type="running", title="Run", description="changed",
        )
        self.assertEqual(calendar_status(self._row(wid)), "stale")
        self._push(wid)
        self.assertEqual(calendar_status(self._row(wid)), "synced")


if __name__ == "__main__":
    unittest.main()
