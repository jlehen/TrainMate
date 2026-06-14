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

    def test_sync_context_ingests_tagged_events(self):
        """sync_context upserts tagged events, deletes cancelled ones, skips untagged
        events, and persists the nextSyncToken (DESIGN_calendar_context_ingest.md §6)."""
        # Pre-seed a row that an incoming cancelled event will delete.
        test_db.upsert_daily_context_by_event(
            google_event_id="evt-old", date="2026-06-10", metric="alcohol",
            value=1.0, text="1 drink", updated=None,
        )

        items = [
            {
                "id": "evt-1",
                "status": "confirmed",
                "summary": "Alcohol: 2 drinks",
                "description": "wine",
                "updated": "2026-06-13T20:00:00Z",
                "start": {"date": "2026-06-13"},
                "extendedProperties": {"private": {
                    "source": "trainmate-context", "metric": "alcohol", "value": "2",
                }},
            },
            {"id": "evt-old", "status": "cancelled"},  # deletes the pre-seeded row
            {  # not ours: defensive skip (server-side filter normally excludes it)
                "id": "evt-foreign",
                "status": "confirmed",
                "summary": "Dentist",
                "start": {"date": "2026-06-13"},
                "extendedProperties": {"private": {"source": "something-else"}},
            },
        ]
        mock_service = MagicMock()
        mock_service.events().list.return_value.execute.return_value = {
            "items": items, "nextSyncToken": "tok-next",
        }

        with patch.object(calendar_syncer, "service", mock_service), \
                patch.object(calendar_syncer, "calendar_id", "cal-test"):
            changed = calendar_syncer.sync_context()

        # evt-1 upserted + evt-old deleted; evt-foreign skipped.
        self.assertEqual(changed, 2)
        rows = test_db.get_daily_context()
        self.assertEqual([r["google_event_id"] for r in rows], ["evt-1"])
        self.assertEqual(rows[0]["metric"], "alcohol")
        self.assertEqual(rows[0]["value"], 2.0)
        self.assertIn("2 drinks", rows[0]["text"])

        # Token persisted for the next incremental sync.
        self.assertEqual(
            test_db.get_sync_state(key="calendar_context")["sync_token"], "tok-next"
        )
        # The list query used the server-side context filter.
        _, kwargs = mock_service.events().list.call_args
        self.assertEqual(kwargs.get("privateExtendedProperty"), "source=trainmate-context")

    def test_sync_context_expired_token_falls_back_to_full_pull(self):
        """A 410 on the stored syncToken discards it and restarts with a full pull."""
        from googleapiclient.errors import HttpError

        test_db.set_sync_state(
            through_date=None, last_pull_utc="t0", key="calendar_context",
            sync_token="stale-tok",
        )

        resp_410 = MagicMock()
        resp_410.status = 410
        full_pull_result = {
            "items": [{
                "id": "evt-2",
                "status": "confirmed",
                "summary": "Poor sleep",
                "start": {"date": "2026-06-14"},
                "extendedProperties": {"private": {
                    "source": "trainmate-context", "metric": "sleep_quality",
                }},
            }],
            "nextSyncToken": "fresh-tok",
        }

        mock_service = MagicMock()
        mock_service.events().list.return_value.execute.side_effect = [
            HttpError(resp_410, b"gone"),  # incremental with stale token -> 410
            full_pull_result,              # restarted full pull
        ]

        with patch.object(calendar_syncer, "service", mock_service), \
                patch.object(calendar_syncer, "calendar_id", "cal-test"):
            changed = calendar_syncer.sync_context()

        self.assertEqual(changed, 1)
        rows = test_db.get_daily_context()
        self.assertEqual([r["metric"] for r in rows], ["sleep_quality"])
        self.assertIsNone(rows[0]["value"])  # no value tag -> NULL
        self.assertEqual(
            test_db.get_sync_state(key="calendar_context")["sync_token"], "fresh-tok"
        )

if __name__ == "__main__":
    unittest.main()
