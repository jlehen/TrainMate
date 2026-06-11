import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_calendar.db")

from trainmate.db import Database
import trainmate.db
import trainmate.google_calendar
from trainmate.google_calendar import calendar_syncer

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.google_calendar.db = test_db

class TestCalendarSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.google_calendar.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        from tests.helpers import clear_all_tables
        clear_all_tables(test_db)

    def test_sync_workout_adapted_description_order(self):
        # Setup workout dictionary with adaptation details
        workout = {
            "date": "2026-06-12",
            "sport_type": "running",
            "title": "Easy Run",
            "description": "Short 20 min recovery jog.",
            "original_description": "Long 60 min intervals.",
            "modification_reason": "Swapped with yoga due to fatigue.",
            "duration_minutes": 20,
            "tss": 15,
            "google_event_id": None
        }

        # Mock the event insert API response
        mock_service = MagicMock()
        mock_event_result = {"id": "evt-new-123", "htmlLink": "http://calendar/event/1"}
        mock_service.events().insert().execute.return_value = mock_event_result

        # Run the sync with patched service
        with patch.object(calendar_syncer, "service", mock_service):
            event_id = calendar_syncer.sync_workout(workout)

        # Verify returned event ID
        self.assertEqual(event_id, "evt-new-123")

        # Find the call that has the body parameter
        insert_calls = [
            call for call in mock_service.events().insert.call_args_list
            if call.kwargs.get("body")
        ]
        self.assertEqual(len(insert_calls), 1)
        body = insert_calls[0].kwargs["body"]
        self.assertEqual(body.get("summary"), "[Adapted] Easy Run")
        
        # Verify the description order: Adapted first, then Originally, then Reason
        desc = body.get("description", "")
        self.assertIn("Duration: 20m | TSS: 15", desc)
        self.assertIn("Adapted:\nShort 20 min recovery jog.", desc)
        self.assertIn("Originally:\nLong 60 min intervals.", desc)
        self.assertIn("Reason:\nSwapped with yoga due to fatigue.", desc)
        
        # Check that "Adapted:" comes BEFORE "Originally:"
        adapted_idx = desc.index("Adapted:")
        originally_idx = desc.index("Originally:")
        self.assertTrue(
            adapted_idx < originally_idx,
            "Adapted description should come first"
        )

    def test_sync_workout_swap_does_not_duplicate_description(self):
        # A swap moves a workout's date without changing its content, so
        # description == original_description. The calendar should show the
        # description once rather than identical "Adapted"/"Originally" blocks.
        workout = {
            "date": "2026-06-11",
            "sport_type": "running",
            "title": "Tempo Run",
            "description": "40 min tempo at threshold.",
            "original_description": "40 min tempo at threshold.",
            "modification_reason": "Swapped from 2026-06-09 to 2026-06-11",
            "duration_minutes": 40,
            "tss": 50,
            "google_event_id": None,
        }

        mock_service = MagicMock()
        mock_event_result = {"id": "evt-swap-1", "htmlLink": "http://calendar/event/2"}
        mock_service.events().insert().execute.return_value = mock_event_result

        with patch.object(calendar_syncer, "service", mock_service):
            calendar_syncer.sync_workout(workout)

        insert_calls = [
            call for call in mock_service.events().insert.call_args_list
            if call.kwargs.get("body")
        ]
        self.assertEqual(len(insert_calls), 1)
        body = insert_calls[0].kwargs["body"]
        self.assertEqual(body.get("summary"), "[Adapted] Tempo Run")

        desc = body.get("description", "")
        self.assertNotIn("Adapted:", desc)
        self.assertNotIn("Originally:", desc)
        self.assertIn("40 min tempo at threshold.", desc)
        self.assertIn("Reason:\nSwapped from 2026-06-09 to 2026-06-11", desc)
        # The description text appears exactly once.
        self.assertEqual(desc.count("40 min tempo at threshold."), 1)

    def test_sync_workout_removed(self):
        # Setup workout dictionary with removed details
        workout = {
            "date": "2026-06-12",
            "sport_type": "running",
            "title": "Easy Run",
            "description": "Short recovery jog.",
            "original_description": "Short recovery jog.",
            "duration_minutes": 20,
            "tss": 15,
            "google_event_id": "evt-removed-123",
            "removed": True,
            "removed_reason": "Injury flare-up",
            "synced": False
        }

        # Mock the event update API response
        mock_service = MagicMock()
        mock_event_result = {"id": "evt-removed-123", "htmlLink": "http://calendar/event/1"}
        mock_service.events().update().execute.return_value = mock_event_result

        # Run the sync with patched service
        with patch.object(calendar_syncer, "service", mock_service):
            event_id = calendar_syncer.sync_workout(workout)

        # Verify returned event ID
        self.assertEqual(event_id, "evt-removed-123")

        # Find the call that has the body parameter
        update_calls = [
            call for call in mock_service.events().update.call_args_list
            if call.kwargs.get("body")
        ]
        self.assertEqual(len(update_calls), 1)
        body = update_calls[0].kwargs["body"]
        self.assertEqual(body.get("summary"), "[Deleted] Easy Run")
        
        # Verify description contains Duration, TSS, and Reason
        desc = body.get("description", "")
        self.assertIn("Duration: 20m | TSS: 15", desc)
        self.assertIn("Short recovery jog.", desc)
        self.assertIn("Reason:\nInjury flare-up", desc)

if __name__ == "__main__":
    unittest.main()
