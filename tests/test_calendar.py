import os
import unittest
from unittest.mock import patch, MagicMock

from tests.helpers import rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_calendar.db")

from trainmate.db import Database
import trainmate.db
import trainmate.google_calendar
from trainmate.google_calendar import calendar_syncer

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

class TestCalendarSync(unittest.TestCase):
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
        from tests.helpers import clear_all_tables, rebind_test_db
        clear_all_tables(test_db)

    def test_sync_workout_adapted_body_is_the_current_form_only(self):
        """The body states what the session IS now, plus why. Every earlier form is the
        history block's job (DESIGN_calendar_lineage.md §5), so the old
        "Adapted:"/"Originally:" prose pair is gone."""
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

        desc = body.get("description", "")
        self.assertIn("Duration: 20m | TSS: 15", desc)
        self.assertIn("Short 20 min recovery jog.", desc)
        self.assertIn("Reason:\nSwapped with yoga due to fatigue.", desc)
        self.assertNotIn("Adapted:", desc)
        self.assertNotIn("Originally:", desc)
        self.assertNotIn("Long 60 min intervals.", desc)

    def test_sync_workout_lifecycle_footer(self):
        # An adapted session surfaces its plan->adapt lifecycle in the footer. The load it
        # was planned with is NOT repeated here: the history block carries it, with its
        # date and target (DESIGN_calendar_lineage.md §5).
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
        self.assertNotIn("Originally:", desc)
        # Lifecycle line: planned timestamp, last-adapted timestamp, ease count.
        self.assertIn("Planned: 2026-06-01 14:30", desc)
        self.assertIn("Last adapted: 2026-06-11 09:00", desc)
        self.assertIn("Adapted ×2", desc)
        # The lifecycle line sits above the technical ID footer.
        self.assertTrue(desc.index("Planned: 2026-06-01") < desc.index("Workout: 42"))

    def test_sync_workout_unadapted_shows_planned_only(self):
        # A session that has never been eased shows the Planned line and nothing else:
        # no last-adapted timestamp and no count.
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

        with patch("trainmate.runtime.calendar_syncer") as mock_syncer:
            marked = mark_adherence_from_results(results, today_str=today)

        self.assertEqual(marked, 1)
        self.assertEqual(mock_syncer.sync_workout.call_count, 1)
        call = mock_syncer.sync_workout.call_args
        self.assertEqual(call.args[0]["google_event_id"], "evt-past")
        self.assertEqual(call.kwargs["adherence"]["status"], "done")

    def test_mark_adherence_from_results_skips_noop_when_already_marked(self):
        # Re-marking a settled past event is a no-op: the first pass writes and
        # records the adherence signature; a second pass with that signature
        # stored on the row skips the Calendar update entirely.
        from trainmate.cli.common import mark_adherence_from_results

        today = "2026-06-20"

        def make_results():
            return [{
                "date": "2026-06-18",
                "planned": {
                    "id": 7, "date": "2026-06-18", "sport_type": "running",
                    "title": "Run", "duration_minutes": 30, "tss": 30,
                    "google_event_id": "evt-past",
                },
                "completed": {
                    "activity_id": "a", "activity_name": "Morning Run",
                    "activity_type": "running", "duration_sec": 1800,
                    "rpe": 6, "tss": 32.0,
                },
            }]

        # First pass: nothing recorded yet -> pushes and stamps the signature.
        with patch("trainmate.runtime.calendar_syncer") as mock_syncer, \
                patch("trainmate.runtime.db") as mock_db:
            first = make_results()
            marked = mark_adherence_from_results(first, today_str=today)
            self.assertEqual(marked, 1)
            self.assertEqual(mock_syncer.sync_workout.call_count, 1)
            mock_db.mark_workout_adherence_pushed.assert_called_once()
            wid, signature = mock_db.mark_workout_adherence_pushed.call_args.args
            self.assertEqual(wid, 7)

        # Second pass: the row now carries that signature -> skipped, no write.
        with patch("trainmate.runtime.calendar_syncer") as mock_syncer, \
                patch("trainmate.runtime.db") as mock_db:
            second = make_results()
            second[0]["planned"]["adherence_pushed_signature"] = signature
            marked = mark_adherence_from_results(second, today_str=today)
            self.assertEqual(marked, 0)
            mock_syncer.sync_workout.assert_not_called()
            mock_db.mark_workout_adherence_pushed.assert_not_called()

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

    def test_sync_signals_ingests_tagged_events(self):
        """sync_signals upserts tagged events, deletes cancelled ones, skips untagged
        events, and persists the nextSyncToken (DESIGN_calendar_signal_ingest.md §6)."""
        # Pre-seed a row that an incoming cancelled event will delete.
        test_db.upsert_daily_signal_by_event(
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
            changed = calendar_syncer.sync_signals()

        # evt-1 upserted + evt-old deleted; evt-foreign skipped.
        self.assertEqual(changed, 2)
        rows = test_db.get_daily_signals()
        self.assertEqual([r["google_event_id"] for r in rows], ["evt-1"])
        self.assertEqual(rows[0]["metric"], "alcohol")
        self.assertEqual(rows[0]["value"], 2.0)
        self.assertIn("2 drinks", rows[0]["text"])

        # Token persisted for the next incremental sync.
        self.assertEqual(
            test_db.get_sync_state(key="calendar_signals")["sync_token"], "tok-next"
        )
        # The list query used the server-side signal filter.
        _, kwargs = mock_service.events().list.call_args
        self.assertEqual(kwargs.get("privateExtendedProperty"), "source=trainmate-context")

    def test_sync_signals_ignores_cancelled_events_that_are_not_ours(self):
        """On the incremental path the stream carries every cancelled event, not just
        tagged ones; a cancellation that deletes no row must not count as a change
        (DESIGN_calendar_signal_ingest.md §6)."""
        test_db.set_sync_state(
            through_date=None, last_pull_utc="t0", key="calendar_signals",
            sync_token="tok-prev",
        )
        test_db.upsert_daily_signal_by_event(
            google_event_id="evt-ctx", date="2026-06-10", metric="alcohol",
            value=1.0, text="1 drink", updated=None,
        )

        mock_service = MagicMock()
        mock_service.events().list.return_value.execute.return_value = {
            "items": [
                {"id": "evt-ctx", "status": "cancelled"},      # ours: deletes a row
                {"id": "evt-workout", "status": "cancelled"},  # a cancelled workout
                {"id": "evt-dentist", "status": "cancelled"},  # a private appointment
            ],
            "nextSyncToken": "tok-next",
        }

        with patch.object(calendar_syncer, "service", mock_service), \
                patch.object(calendar_syncer, "calendar_id", "cal-test"):
            changed = calendar_syncer.sync_signals()

        self.assertEqual(changed, 1)
        self.assertEqual(test_db.get_daily_signals(), [])
        # The incremental query cannot carry the server-side filter alongside the token.
        _, kwargs = mock_service.events().list.call_args
        self.assertEqual(kwargs.get("syncToken"), "tok-prev")
        self.assertNotIn("privateExtendedProperty", kwargs)

    def test_sync_signals_expired_token_falls_back_to_full_pull(self):
        """A 410 on the stored syncToken discards it and restarts with a full pull."""
        from googleapiclient.errors import HttpError

        test_db.set_sync_state(
            through_date=None, last_pull_utc="t0", key="calendar_signals",
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
            changed = calendar_syncer.sync_signals()

        self.assertEqual(changed, 1)
        rows = test_db.get_daily_signals()
        self.assertEqual([r["metric"] for r in rows], ["sleep_quality"])
        self.assertIsNone(rows[0]["value"])  # no value tag -> NULL
        self.assertEqual(
            test_db.get_sync_state(key="calendar_signals")["sync_token"], "fresh-tok"
        )

    def test_list_workout_events_pages_and_filters_by_tag(self):
        """list_workout_events follows nextPageToken and asks the server for the
        workout tag only (the ownership handle a wiped database no longer has)."""
        mock_service = MagicMock()
        mock_service.events().list.return_value.execute.side_effect = [
            {"items": [{"id": "evt-a"}], "nextPageToken": "page-2"},
            {"items": [{"id": "evt-b"}]},
        ]

        with patch.object(calendar_syncer, "service", mock_service), \
                patch.object(calendar_syncer, "calendar_id", "cal-test"):
            events = calendar_syncer.list_workout_events()

        self.assertEqual([e["id"] for e in events], ["evt-a", "evt-b"])
        first_kwargs = mock_service.events().list.call_args_list[-2][1]
        second_kwargs = mock_service.events().list.call_args_list[-1][1]
        self.assertEqual(
            first_kwargs.get("privateExtendedProperty"), "source=TrainMate"
        )
        self.assertNotIn("pageToken", first_kwargs)
        self.assertEqual(second_kwargs.get("pageToken"), "page-2")

    def test_quiet_events_silences_per_event_lines_but_not_failures(self):
        """`quiet_events` hides the routine 'Created'/'Deleted' lines a batch push would
        repeat per session; failures stay visible, and the flag is restored on exit."""
        from trainmate.google_calendar import quiet_events

        mock_service = MagicMock()
        with patch.object(calendar_syncer, "service", mock_service), \
                patch.object(calendar_syncer, "calendar_id", "cal-test"):
            with quiet_events():
                with patch("builtins.print") as quiet_print:
                    calendar_syncer.delete_event("evt-a")
                mock_service.events().delete.return_value.execute.side_effect = (
                    RuntimeError("boom")
                )
                with patch("builtins.print") as failure_print:
                    calendar_syncer.delete_event("evt-b")
            mock_service.events().delete.return_value.execute.side_effect = None
            with patch("builtins.print") as loud_print:
                calendar_syncer.delete_event("evt-c")

        quiet_print.assert_not_called()
        self.assertIn("boom", failure_print.call_args.args[0])
        self.assertIn("evt-c", loud_print.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
