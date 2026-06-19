import os
import unittest
from datetime import date
from unittest.mock import Mock, patch

from tests.helpers import clear_all_tables
from trainmate.adherence import analyze_adherence

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_adaptation.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.service.db = test_db

from trainmate.coach import coach_service


class TestAdaptation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.service.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adaptation_matching_and_discrepancies(self, mock_client):
        test_profile = {"lthr": 165, "max_hr": 185}

        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            mock_client.complete.return_value = {
                "change_needed": True,
                "reason": "Fatigue detected, RHR is elevated and HRV is suppressed.",
                "adapted_workouts": [
                    {
                        "date": "2026-06-03",
                        "sport_type": "rest",
                        "title": "Adapted Rest Day",
                        "description": "Swapped tempo run to rest.",
                        "duration_minutes": 0,
                        "rpe": 0,
                        "tss": 0.0,
                    }
                ],
            }

            test_db.save_metric_cache("2026-06-01", 50, 60, 80, 20, 10.0, 8.0, 1.2)
            test_db.save_metric_cache("2026-06-02", 52, 55, 75, 25, 12.0, 8.0, 1.5)
            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            test_db.save_workout(
                "2026-06-01", "running", "Easy Run", "30 mins",
                duration_minutes=30, rpe=4, tss=20,
            )
            test_db.save_workout(
                "2026-06-02", "road_biking", "Tempo Ride", "60 mins",
                duration_minutes=60, rpe=6, tss=40,
            )
            test_db.save_workout(
                "2026-06-03", "running", "Interval Session", "45 mins",
                duration_minutes=45, rpe=8, tss=60,
            )

            test_db.save_completed_activity(
                "act_1", "2026-06-01", "2026-06-01 08:00:00", "Easy Run",
                "running", 1800.0, 5.0, 50.0, 132, 150, 4, 20.0,
            )
            test_db.save_completed_activity(
                "act_2", "2026-06-02", "2026-06-02 08:00:00", "Short Cycling",
                "cycling", 1800.0, 12.0, 100.0, 132, 150, 4, 20.0,
            )

            reason, proposed = coach_service.workout_adapt("2026-06-03")

            self.assertTrue(mock_client.complete.called)
            self.assertEqual(reason, "Fatigue detected, RHR is elevated and HRV is suppressed.")
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["title"], "Adapted Rest Day")
            self.assertEqual(proposed[0]["sport_type"], "rest")

            prompt_user_content = mock_client.complete.call_args[0][1]
            self.assertIn(
                "Complete Miss! Missed planned workout 'Interval Session'",
                prompt_user_content,
            )
            self.assertIn("duration mismatch", prompt_user_content)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_is_read_only_for_learnings(self, mock_client):
        """Daily adaptation consumes coach learnings as context but authors none — durable,
        evidence-backed observations are written only by the weekly history analysis
        (DESIGN_evidence_based_confidence.md §2/§11)."""
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            # Pre-existing observation shown to the model as context.
            lid = test_db.add_learning(
                "Elevated RHR after consecutive hard days", sports="running"
            )

            # Even if the model returns learning_updates, adapt must ignore them.
            mock_client.complete.return_value = {
                "change_needed": False,
                "reason": "On track.",
                "adapted_workouts": [],
                "learning_updates": [
                    {"op": "add", "text": "Should NOT be saved by adapt"},
                ],
            }

            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            reason, proposed = coach_service.workout_adapt("2026-06-03")
            self.assertEqual(reason, "On track.")
            self.assertEqual(proposed, [])

            # The adapt prompt still surfaces the existing observation by id as context.
            system_prompt = mock_client.complete.call_args[0][0]
            self.assertIn(f"[{lid}|running|", system_prompt)

            # No learnings were written: only the pre-existing one remains.
            learnings = {l["id"]: l for l in test_db.get_learnings()}
            self.assertEqual(len(learnings), 1)
            self.assertIn(lid, learnings)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_drops_already_completed_session(self, mock_client):
        """A session already performed (matched by a completed activity) is locked history:
        the guard drops any proposal targeting it, even if the model returns one — you cannot
        adapt a workout you have already finished today."""
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            # The model tries to "adapt" today's already-completed ride (date == eval date),
            # plus legitimately adapt a future session still ahead of the athlete.
            mock_client.complete.return_value = {
                "change_needed": True,
                "reason": "Mild fatigue; ease the upcoming interval session.",
                "adapted_workouts": [
                    {
                        "date": "2026-06-03",
                        "sport_type": "road_biking",
                        "title": "Rewritten Ride (should be dropped)",
                        "description": "Restating the finished ride to match actual.",
                        "duration_minutes": 82,
                        "rpe": 4,
                        "tss": 52.0,
                    },
                    {
                        "date": "2026-06-04",
                        "sport_type": "running",
                        "title": "Eased Intervals",
                        "description": "Cut intensity for the future session.",
                        "duration_minutes": 40,
                        "rpe": 5,
                        "tss": 35.0,
                    },
                ],
            }

            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            # Today's planned ride and a future running session.
            test_db.save_workout(
                "2026-06-03", "road_biking", "Aerobic Base Endurance", "70 mins",
                duration_minutes=70, rpe=4, tss=45,
            )
            test_db.save_workout(
                "2026-06-04", "running", "Interval Session", "45 mins",
                duration_minutes=45, rpe=8, tss=60,
            )

            # The ride was actually completed today — this is what locks it.
            test_db.save_completed_activity(
                "act_ride", "2026-06-03", "2026-06-03 08:00:00", "Zwift Ride",
                "cycling", 4920.0, 30.0, 250.0, 132, 150, 4, 52.0,
            )

            reason, proposed = coach_service.workout_adapt("2026-06-03")

            dates = {p["date"] for p in proposed}
            self.assertNotIn("2026-06-03", dates)  # completed session dropped
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["date"], "2026-06-04")
            self.assertEqual(proposed[0]["title"], "Eased Intervals")

            # The completed session is surfaced to the model as locked history (the
            # authoritative signal), not left for it to re-derive from the activity list.
            prompt_user_content = mock_client.complete.call_args[0][1]
            self.assertIn(
                "Aerobic Base Endurance | Expected duration: 70m, RPE: 4, TSS: 45 "
                "[COMPLETED — locked history, not adaptable]",
                prompt_user_content,
            )

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
        test_db.save_workout("2026-06-09", "road_biking", "Threshold", "hard", rpe=8, tss=90)
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
        test_db.save_workout("2026-06-09", "road_biking", "Long Ride", "big", rpe=6, tss=130)
        test_db.save_workout("2026-06-15", "yoga", "Mobility", "easy", rpe=2, tss=40)

        ops = self._swap_ops_for_dates("2026-06-09", "2026-06-15")
        warnings = coach_service.workout_swap_validate(ops)
        self.assertTrue(
            any("spike your ACWR" in w for w in warnings),
            f"expected a weekly load-spike warning, got {warnings}",
        )

    def test_apply_swap_moves_dates_and_syncs(self):
        a = test_db.save_workout("2026-06-10", "running", "Run A", "a", rpe=4, tss=30)
        b = test_db.save_workout("2026-06-12", "road_biking", "Ride B", "b", rpe=4, tss=30)
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
        b = test_db.save_workout("2026-06-12", "road_biking", "Ride B", "b", rpe=4, tss=30)
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
            "2026-06-12", "road_biking", "Ride B", "b", rpe=4, tss=30
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

    def test_swap_back_partial_still_modified(self):
        """When only one of two swapped workouts is moved back, only that
        one should lose its modification flag."""
        a = test_db.save_workout(
            "2026-06-10", "running", "Run A", "a", rpe=4, tss=30
        )
        b = test_db.save_workout(
            "2026-06-12", "road_biking", "Ride B", "b", rpe=4, tss=30
        )
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=Mock()
        )

        # Swap A↔B
        service.workout_swap_apply(
            [{"id": a, "new_date": "2026-06-12"},
             {"id": b, "new_date": "2026-06-10"}],
            no_sync=True,
        )

        # Move only A back to its original date (to a free slot on 2026-06-10
        # — B was there but we move it elsewhere first).
        test_db.update_workout_date(
            b, "2026-06-14", "Swapped from 2026-06-10 to 2026-06-14"
        )
        test_db.update_workout_date(
            a, "2026-06-10", "Swapped from 2026-06-12 to 2026-06-10"
        )

        self.assertIsNone(
            test_db.get_workout_by_id(a)["modification_reason"],
            "A is back home; should be unmodified",
        )
        self.assertIsNotNone(
            test_db.get_workout_by_id(b)["modification_reason"],
            "B is NOT on its original date; should remain modified",
        )

    def test_workout_add_new_inserts_and_syncs(self):
        syncer = Mock()
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=syncer
        )
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
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=syncer
        )
        test_db.save_workout(
            "2026-06-20", "running", "Tempo Intervals", "6x3min Z4",
            duration_minutes=60, rpe=8, tss=85, google_event_id="evt-123",
        )

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
        # The replaced description becomes "Originally:" content on the event.
        self.assertEqual(saved["original_description"], "6x3min Z4")
        # The replaced session's title + stats and the athlete's reason are captured.
        reason = saved["modification_reason"]
        self.assertIn("Tempo Intervals", reason)
        self.assertIn("60m", reason)
        self.assertIn("TSS 85", reason)
        self.assertIn("RPE 8", reason)
        self.assertIn("legs feel cooked", reason)
        # The existing calendar event is carried over (updated in place, not orphaned).
        self.assertEqual(saved["google_event_id"], "evt-123")
        syncer.sync_workout.assert_called_once()
        # Only one row remains for that date/sport (replace, not double).
        self.assertEqual(
            len(test_db.get_workouts(start_date="2026-06-20", end_date="2026-06-20")), 1
        )

    def test_workout_add_default_keeps_other_sports(self):
        syncer = Mock()
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=syncer
        )
        test_db.save_workout(
            "2026-06-21", "yoga", "Mobility", "30min", google_event_id="evt-yoga",
        )

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
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=syncer
        )
        test_db.save_workout(
            "2026-06-22", "yoga", "Mobility", "30min Z1",
            duration_minutes=30, google_event_id="evt-yoga",
        )
        test_db.save_workout(
            "2026-06-22", "strength", "Old Lift", "5x5",
            duration_minutes=45, rpe=7, google_event_id="evt-str",
        )

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
        # Same-sport event reused in place; other-sport event deleted.
        self.assertEqual(saved["google_event_id"], "evt-str")
        syncer.delete_workout_event.assert_called_once_with("evt-yoga")
        # Both replaced sessions are recorded; the other sport is labelled.
        reason = saved["modification_reason"]
        self.assertIn("Old Lift", reason)
        self.assertIn("yoga Mobility", reason)
        self.assertIn("travel day", reason)

    def test_analyze_adherence_direct(self):
        planned = [
            {
                "date": "2026-06-01",
                "sport_type": "running",
                "title": "Run",
                "duration_minutes": 30,
                "rpe": 5,
                "tss": 25,
            },
            {
                "date": "2026-06-02",
                "sport_type": "rest",
                "title": "Rest Day",
                "duration_minutes": 0,
                "rpe": 0,
                "tss": 0,
            },
            {
                "date": "2026-06-03",
                "sport_type": "road_biking",
                "title": "Ride",
                "duration_minutes": 60,
                "rpe": 6,
                "tss": 40,
            },
        ]

        completed = [
            # June 1: workload mismatch (planned load 27.5, actual load 64)
            {
                "date": "2026-06-01",
                "activity_id": "act1",
                "activity_name": "Hard Run",
                "activity_type": "running",
                "duration_sec": 1800,
                "rpe": 8,
                "tss": 60.0,
            },
            # June 2: rest day violation (workload 15.0 > threshold 10.0)
            {
                "date": "2026-06-02",
                "activity_id": "act2",
                "activity_name": "Lawn Mowing",
                "activity_type": "walking",
                "duration_sec": 3600,
                "rpe": 5,
                "tss": 10.0,
            },
            # June 4: unplanned activity (workload 30.0 > threshold 10.0)
            {
                "date": "2026-06-04",
                "activity_id": "act4",
                "activity_name": "Extra Run",
                "activity_type": "running",
                "duration_sec": 1800,
                "rpe": 6,
                "tss": 27.0,
            },
        ]

        discrepancies, matching, _ = analyze_adherence(
            planned_workouts=planned,
            completed_activities=completed,
            start_date_obj=date(2026, 6, 1),
            history_days=4,
            minor_activity_load_threshold=10.0,
        )

        self.assertEqual(len(discrepancies), 4)
        self.assertTrue(any("workload mismatch" in d for d in discrepancies))
        self.assertTrue(
            any("Rest Day Violation! Performed 'Lawn Mowing'" in d for d in discrepancies)
        )
        self.assertTrue(
            any("Complete Miss! Missed planned workout 'Ride'" in d for d in discrepancies)
        )
        self.assertTrue(
            any("Unplanned Activity! Performed 'Extra Run'" in d for d in discrepancies)
        )

    def test_analyze_adherence_coverage_gates_unplanned(self):
        """An activity with no planned workout is an 'Unplanned Activity!' deviation only
        when its date falls inside a planned block; outside all coverage it is softened
        to an informational note instead."""
        completed = [{
            "date": "2026-06-04",
            "activity_id": "act4",
            "activity_name": "Extra Run",
            "activity_type": "running",
            "duration_sec": 1800,
            "rpe": 6,
            "tss": 27.0,
        }]

        # Covered: 2026-06-04 sits inside the planned block -> deviation.
        disc, _, info = analyze_adherence(
            planned_workouts=[],
            completed_activities=completed,
            start_date_obj=date(2026, 6, 1),
            history_days=7,
            minor_activity_load_threshold=10.0,
            covered_ranges=[("2026-06-01", "2026-06-30")],
        )
        self.assertTrue(any("Unplanned Activity! Performed 'Extra Run'" in d for d in disc))
        self.assertEqual(info, [])

        # Uncovered: the block starts after the activity -> informational, not a deviation.
        disc, _, info = analyze_adherence(
            planned_workouts=[],
            completed_activities=completed,
            start_date_obj=date(2026, 6, 1),
            history_days=7,
            minor_activity_load_threshold=10.0,
            covered_ranges=[("2026-06-10", "2026-06-30")],
        )
        self.assertEqual(disc, [])
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["activity_name"], "Extra Run")

        # No coverage at all (cold start) -> informational.
        disc, _, info = analyze_adherence(
            planned_workouts=[],
            completed_activities=completed,
            start_date_obj=date(2026, 6, 1),
            history_days=7,
            minor_activity_load_threshold=10.0,
            covered_ranges=[],
        )
        self.assertEqual(disc, [])
        self.assertEqual(len(info), 1)

    def test_analyze_adherence_variable_tolerance(self):
        # 1. Low expected load (exp_load = 10.0 <= 20.0, tolerance = 50%)
        planned_low = [{
            "date": "2026-06-01",
            "sport_type": "running",
            "title": "Easy Run",
            "duration_minutes": 30,
            "rpe": 2,
            "tss": 10,  # exp_load = planned tss = 10.0
        }]
        # 46.7% duration deviation (30 -> 44 minutes) is within 50%
        completed_low_ok = [{
            "date": "2026-06-01",
            "activity_id": "act_low_ok",
            "activity_name": "Easy Run Actual",
            "activity_type": "running",
            "duration_sec": 44 * 60,
            "rpe": 2,
            "tss": 10.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_low, completed_low_ok, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 0, f"Expected no discrepancies, got {disc}")

        # 53.3% duration deviation (30 -> 46 minutes) is outside 50%
        completed_low_err = [{
            "date": "2026-06-01",
            "activity_id": "act_low_err",
            "activity_name": "Easy Run Actual",
            "activity_type": "running",
            "duration_sec": 46 * 60,
            "rpe": 2,
            "tss": 10.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_low, completed_low_err, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 1)
        self.assertIn("duration mismatch", disc[0])

        # 2. High expected load (exp_load = 110.0 >= 100.0, tolerance = 15%)
        planned_high = [{
            "date": "2026-06-01",
            "sport_type": "running",
            "title": "Long Run",
            "duration_minutes": 120,
            "rpe": 6,
            "tss": 110,  # exp_load = planned tss = 110.0
        }]
        # 16.7% duration deviation (120 -> 140 minutes) is outside 15%
        completed_high_err = [{
            "date": "2026-06-01",
            "activity_id": "act_high_err",
            "activity_name": "Long Run Actual",
            "activity_type": "running",
            "duration_sec": 140 * 60,
            "rpe": 6,
            "tss": 110.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_high, completed_high_err, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 1)
        self.assertIn("duration mismatch", disc[0])

        # 3. Intermediate expected load (exp_load = 60.0, tolerance = 32.5%)
        planned_mid = [{
            "date": "2026-06-01",
            "sport_type": "running",
            "title": "Tempo",
            "duration_minutes": 60,
            "rpe": 5,
            "tss": 60,  # exp_load = planned tss = 60.0
        }]
        # 30% duration deviation (60 -> 78 minutes) is within 32.5%
        completed_mid_ok = [{
            "date": "2026-06-01",
            "activity_id": "act_mid_ok",
            "activity_name": "Tempo Actual",
            "activity_type": "running",
            "duration_sec": 78 * 60,
            "rpe": 5,
            "tss": 60.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_mid, completed_mid_ok, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 0, f"Expected no discrepancies, got {disc}")

        # 35% duration deviation (60 -> 81 minutes) is outside 32.5%
        completed_mid_err = [{
            "date": "2026-06-01",
            "activity_id": "act_mid_err",
            "activity_name": "Tempo Actual",
            "activity_type": "running",
            "duration_sec": 81 * 60,
            "rpe": 5,
            "tss": 60.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_mid, completed_mid_err, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 1)
        self.assertIn("duration mismatch", disc[0])

        # 4. Zero expected load (exp_load = 0.0, tolerance = 50%)
        planned_zero = [{
            "date": "2026-06-01",
            "sport_type": "running",
            "title": "Untargeted Workout",
            "duration_minutes": 60,
            "rpe": 0,
            "tss": 0,
        }]
        # 46.7% duration deviation (60 -> 88 minutes) is within 50%
        completed_zero_ok = [{
            "date": "2026-06-01",
            "activity_id": "act_zero_ok",
            "activity_name": "Workout Actual",
            "activity_type": "running",
            "duration_sec": 88 * 60,
            "rpe": 0,
            "tss": 0.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_zero, completed_zero_ok, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 0, f"Expected no discrepancies, got {disc}")

        # 53.3% duration deviation (60 -> 92 minutes) is outside 50%
        completed_zero_err = [{
            "date": "2026-06-01",
            "activity_id": "act_zero_err",
            "activity_name": "Workout Actual",
            "activity_type": "running",
            "duration_sec": 92 * 60,
            "rpe": 0,
            "tss": 0.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_zero, completed_zero_err, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 1)
        self.assertIn("duration mismatch", disc[0])


if __name__ == "__main__":
    unittest.main()
