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

    def test_sync_workout_provenance_block(self):
        # An adapted session whose load was walked down from its planned values should
        # surface the original load snapshot plus its plan->adapt lifecycle in the footer.
        workout = {
            "date": "2026-06-12",
            "sport_type": "running",
            "title": "Easy Run",
            "description": "Short 20 min recovery jog.",
            "original_description": "Long 60 min intervals.",
            "modification_reason": "Fatigue.",
            "duration_minutes": 20,
            "tss": 15,
            "rpe": 4,
            "original_duration_minutes": 60,
            "original_tss": 80,
            "original_rpe": 7,
            "created_at": "2026-06-01T14:30:00+00:00",
            "adapted_at": "2026-06-11T09:00:00+00:00",
            "adaptation_count": 2,
            "id": 42,
            "google_event_id": None,
        }

        mock_service = MagicMock()
        mock_service.events().insert().execute.return_value = {"id": "evt-1"}
        with patch.object(calendar_syncer, "service", mock_service):
            calendar_syncer.sync_workout(workout)

        body = [
            c for c in mock_service.events().insert.call_args_list
            if c.kwargs.get("body")
        ][0].kwargs["body"]
        desc = body.get("description", "")

        # Current load line now carries RPE too.
        self.assertIn("Duration: 20m | TSS: 15 | RPE: 4", desc)
        # Full original-load snapshot, shown because the load drifted.
        self.assertIn("Originally: 60m | TSS 80 | RPE 7", desc)
        # Lifecycle line: planned timestamp, last-adapted timestamp, ease count.
        self.assertIn("Planned: 2026-06-01 14:30", desc)
        self.assertIn("Last adapted: 2026-06-11 09:00", desc)
        self.assertIn("Adapted ×2", desc)
        # Provenance sits above the technical ID footer.
        self.assertTrue(desc.index("Originally: 60m") < desc.index("Workout: 42"))

    def test_sync_workout_unadapted_shows_planned_only(self):
        # A session at its planned load shows the Planned line but no "Originally"
        # snapshot and no last-adapted/count (it has never been eased).
        workout = {
            "date": "2026-06-12",
            "sport_type": "running",
            "title": "Easy Run",
            "description": "30 min jog.",
            "original_description": "30 min jog.",
            "duration_minutes": 30,
            "tss": 25,
            "rpe": 3,
            "original_duration_minutes": 30,
            "original_tss": 25,
            "original_rpe": 3,
            "created_at": "2026-06-01T14:30:00+00:00",
            "adapted_at": None,
            "adaptation_count": 0,
            "id": 7,
            "google_event_id": None,
        }

        mock_service = MagicMock()
        mock_service.events().insert().execute.return_value = {"id": "evt-2"}
        with patch.object(calendar_syncer, "service", mock_service):
            calendar_syncer.sync_workout(workout)

        body = [
            c for c in mock_service.events().insert.call_args_list
            if c.kwargs.get("body")
        ][0].kwargs["body"]
        desc = body.get("description", "")

        self.assertIn("Planned: 2026-06-01 14:30", desc)
        self.assertNotIn("Originally:", desc)
        self.assertNotIn("Last adapted:", desc)
        self.assertNotIn("Adapted ×", desc)

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

    def test_sync_workout_manual_marks_summary(self):
        # A manually-added session (no replacement) should be flagged "[Manual]"
        # in the calendar summary so it's distinguishable from generated ones.
        workout = {
            "date": "2026-06-13",
            "sport_type": "running",
            "title": "Easy Run",
            "description": "Casual 30 min jog.",
            "original_description": "Casual 30 min jog.",
            "duration_minutes": 30,
            "tss": 25,
            "google_event_id": None,
            "source": "manual",
        }

        mock_service = MagicMock()
        mock_event_result = {"id": "evt-manual-1", "htmlLink": "http://calendar/event/3"}
        mock_service.events().insert().execute.return_value = mock_event_result

        with patch.object(calendar_syncer, "service", mock_service):
            calendar_syncer.sync_workout(workout)

        insert_calls = [
            call for call in mock_service.events().insert.call_args_list
            if call.kwargs.get("body")
        ]
        self.assertEqual(len(insert_calls), 1)
        body = insert_calls[0].kwargs["body"]
        self.assertEqual(body.get("summary"), "[Manual] Easy Run")

    def test_sync_workout_manual_replacement_composes_markers(self):
        # A manual add that overwrote an existing session is both manual and
        # adapted; the summary carries both markers.
        workout = {
            "date": "2026-06-14",
            "sport_type": "running",
            "title": "Tempo Run",
            "description": "New 45 min tempo.",
            "original_description": "Old 60 min intervals.",
            "modification_reason": "Manually replaced previous session: Intervals.",
            "duration_minutes": 45,
            "tss": 55,
            "google_event_id": None,
            "source": "manual",
        }

        mock_service = MagicMock()
        mock_event_result = {"id": "evt-manual-2", "htmlLink": "http://calendar/event/4"}
        mock_service.events().insert().execute.return_value = mock_event_result

        with patch.object(calendar_syncer, "service", mock_service):
            calendar_syncer.sync_workout(workout)

        insert_calls = [
            call for call in mock_service.events().insert.call_args_list
            if call.kwargs.get("body")
        ]
        self.assertEqual(len(insert_calls), 1)
        body = insert_calls[0].kwargs["body"]
        self.assertEqual(body.get("summary"), "[Manual] [Adapted] Tempo Run")

    def test_sync_workout_adherence_marks_title_and_header(self):
        # A past event marked with an adherence verdict gets a [Partial] title tag
        # and an "Adherence" header (status + actual effort + notes) at the top of
        # the description, above the planned Duration/TSS prefix.
        workout = {
            "date": "2026-06-15",
            "sport_type": "running",
            "title": "Tempo Run",
            "description": "45 min tempo.",
            "original_description": "45 min tempo.",
            "duration_minutes": 45,
            "tss": 55,
            "google_event_id": "evt-existing-1",
        }
        adherence = {
            "status": "partial",
            "actual": "[running] Morning Run (52min, load 70, TSS 66)",
            "reasons": ["duration mismatch +/-15% (planned 45m, actual 52m)"],
        }

        mock_service = MagicMock()
        mock_event_result = {"id": "evt-existing-1", "htmlLink": "http://calendar/event/5"}
        mock_service.events().update().execute.return_value = mock_event_result

        with patch.object(calendar_syncer, "service", mock_service):
            calendar_syncer.sync_workout(workout, adherence=adherence)

        update_calls = [
            call for call in mock_service.events().update.call_args_list
            if call.kwargs.get("body")
        ]
        self.assertEqual(len(update_calls), 1)
        body = update_calls[0].kwargs["body"]
        self.assertEqual(body.get("summary"), "[Partial] Tempo Run")
        desc = body.get("description")
        self.assertTrue(desc.startswith("Adherence: Partial\n"))
        self.assertIn("Actual: [running] Morning Run (52min, load 70, TSS 66)", desc)
        self.assertIn("Notes: duration mismatch +/-15% (planned 45m, actual 52m)", desc)
        # Header sits above the planned prefix and the original body.
        self.assertLess(desc.index("Adherence:"), desc.index("Duration: 45m"))
        self.assertLess(desc.index("Duration: 45m"), desc.index("45 min tempo."))

    def test_sync_workout_adherence_rest_ok_omits_actual_and_notes(self):
        # An adhered rest day: [Rest OK] tag, no Actual/Notes lines.
        workout = {
            "date": "2026-06-15",
            "sport_type": "rest",
            "title": "Rest",
            "description": "Recovery day.",
            "original_description": "Recovery day.",
            "google_event_id": "evt-rest-1",
        }
        adherence = {"status": "rest_ok", "actual": None, "reasons": []}

        mock_service = MagicMock()
        mock_service.events().update().execute.return_value = {
            "id": "evt-rest-1", "htmlLink": "http://calendar/event/6"
        }

        with patch.object(calendar_syncer, "service", mock_service):
            calendar_syncer.sync_workout(workout, adherence=adherence)

        update_calls = [
            call for call in mock_service.events().update.call_args_list
            if call.kwargs.get("body")
        ]
        body = update_calls[0].kwargs["body"]
        self.assertEqual(body.get("summary"), "[Rest OK] Rest")
        desc = body.get("description")
        self.assertTrue(desc.startswith("Adherence: Rest OK"))
        self.assertNotIn("Actual:", desc)
        self.assertNotIn("Notes:", desc)

    def test_mark_adherence_from_results_skips_today_and_eventless(self):
        # Only strictly-past planned workouts that already have a Calendar event
        # get marked: today/future and event-less rows are skipped.
        from trainmate.cli.common import mark_adherence_from_results

        today = "2026-06-20"
        results = [
            {  # past + has event -> marked
                "date": "2026-06-18",
                "planned": {
                    "date": "2026-06-18", "sport_type": "running", "title": "Run",
                    "duration_minutes": 30, "tss": 30, "google_event_id": "evt-past",
                },
                "completed": {
                    "activity_id": "a", "activity_name": "Morning Run",
                    "activity_type": "running", "duration_sec": 1800,
                    "rpe": 6, "tss": 32.0,
                },
            },
            {  # today -> skipped
                "date": today,
                "planned": {
                    "date": today, "sport_type": "running", "title": "Run",
                    "google_event_id": "evt-today",
                },
                "completed": None,
            },
            {  # past but no event -> skipped
                "date": "2026-06-17",
                "planned": {
                    "date": "2026-06-17", "sport_type": "running", "title": "Run",
                    "google_event_id": None,
                },
                "completed": None,
            },
        ]

        with patch("trainmate_cli.calendar_syncer") as mock_syncer:
            marked = mark_adherence_from_results(results, today_str=today)

        self.assertEqual(marked, 1)
        self.assertEqual(mock_syncer.sync_workout.call_count, 1)
        call = mock_syncer.sync_workout.call_args
        self.assertEqual(call.args[0]["google_event_id"], "evt-past")
        self.assertEqual(call.kwargs["adherence"]["status"], "done")

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
