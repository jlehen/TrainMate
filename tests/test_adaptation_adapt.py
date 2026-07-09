import os
import unittest
from datetime import date
from unittest.mock import Mock, patch

from tests.helpers import clear_all_tables
from trainmate.adherence import analyze_adherence

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_adaptation_adapt.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.service.db = test_db

from trainmate.coach import coach_service


class TestAdaptationAdapt(unittest.TestCase):
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

            reason, proposed, _new_constraints = coach_service.workout_adapt("2026-06-03")

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

            reason, proposed, _new_constraints = coach_service.workout_adapt("2026-06-03")
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
    def test_adapt_message_surfaced_in_prompt(self, mock_client):
        """An athlete message for the run is rendered as a bounded section of the adapt
        prompt (advisory, ephemeral) and omitted entirely when no message is given."""
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            mock_client.complete.return_value = {
                "change_needed": False,
                "reason": "On track.",
                "adapted_workouts": [],
            }

            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            coach_service.workout_adapt(
                "2026-06-03", message="  knee is sore, keep impact low  "
            )
            user_content = mock_client.complete.call_args[0][1]
            system_prompt = mock_client.complete.call_args[0][0]
            self.assertIn("ATHLETE'S NOTE FOR THIS ADAPTATION", user_content)
            # Surrounding whitespace is trimmed before rendering.
            self.assertIn("knee is sore, keep impact low", user_content)
            self.assertNotIn("  knee is sore", user_content)
            self.assertIn("ATHLETE'S NOTE FOR TODAY", system_prompt)

            # No message → the section is absent (message-less run is unchanged).
            mock_client.complete.reset_mock()
            mock_client.complete.return_value = {
                "change_needed": False,
                "reason": "On track.",
                "adapted_workouts": [],
            }
            coach_service.workout_adapt("2026-06-03")
            self.assertNotIn(
                "ATHLETE'S NOTE FOR THIS ADAPTATION",
                mock_client.complete.call_args[0][1],
            )

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

            reason, proposed, _new_constraints = coach_service.workout_adapt("2026-06-03")

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

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_drops_noop_relisted_session(self, mock_client):
        """No-op backstop: if the model re-lists a session unchanged (here verbatim, plus a
        cosmetic whitespace-only variant), it is dropped so an untouched session is never
        re-stamped as adapted. A genuinely changed session on the same run is kept."""
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
                "reason": "Mostly on track; only the Friday tempo needs easing.",
                "adapted_workouts": [
                    # Verbatim re-list of the planned session — a no-op, must be dropped.
                    {
                        "date": "2026-06-04",
                        "sport_type": "running",
                        "title": "Easy Run",
                        "description": "30 mins easy Z2",
                        "duration_minutes": 30,
                        "rpe": 4,
                        "tss": 20.0,
                    },
                    # Same content but cosmetic whitespace churn + int/float tss — still a
                    # no-op, must be dropped.
                    {
                        "date": "2026-06-05",
                        "sport_type": "road_biking",
                        "title": "Endurance Ride",
                        "description": "60 mins  aerobic   base",
                        "duration_minutes": 60,
                        "rpe": 5,
                        "tss": 40,
                    },
                    # Genuine change — must survive.
                    {
                        "date": "2026-06-06",
                        "sport_type": "running",
                        "title": "Eased Tempo",
                        "description": "Cut to easy Z2 to shed intensity.",
                        "duration_minutes": 35,
                        "rpe": 5,
                        "tss": 30.0,
                    },
                ],
            }

            test_db.save_metric_cache("2026-06-03", 50, 60, 80, 20, 10.0, 8.0, 1.1)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            test_db.save_workout(
                "2026-06-04", "running", "Easy Run", "30 mins easy Z2",
                duration_minutes=30, rpe=4, tss=20,
            )
            test_db.save_workout(
                "2026-06-05", "road_biking", "Endurance Ride", "60 mins aerobic base",
                duration_minutes=60, rpe=5, tss=40,
            )
            test_db.save_workout(
                "2026-06-06", "running", "Friday Tempo", "45 mins w/ tempo blocks",
                duration_minutes=45, rpe=7, tss=55,
            )

            reason, proposed, _new_constraints = coach_service.workout_adapt("2026-06-03")

            dates = {p["date"] for p in proposed}
            self.assertEqual(dates, {"2026-06-06"})
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["title"], "Eased Tempo")

    def test_adapt_apply_stamps_recency_and_bumps_count(self):
        """Applying an adaptation stamps `adapted_at` and bumps `adaptation_count`;
        a second adapt of the same session bumps it again. Non-adapt saves leave both
        untouched."""
        test_db.save_workout(
            "2026-06-20", "running", "Friday Tempo", "45 mins w/ tempo blocks",
            duration_minutes=45, rpe=7, tss=55,
        )
        # A plain save (no adapted_at) must not start the counter.
        row = test_db.get_workout("2026-06-20", "running")
        self.assertIsNone(row["adapted_at"])
        self.assertEqual(row["adaptation_count"], 0)

        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=Mock()
        )
        proposed = [{
            "date": "2026-06-20", "sport_type": "running", "title": "Easy Tempo",
            "description": "Cut to Z2", "modification_reason": "Eased for fatigue",
            "duration_minutes": 35, "rpe": 5, "tss": 30,
        }]
        service.workout_adapt_apply(proposed, "Block too hard", "2026-06-20", "2026-06-20")
        row = test_db.get_workout("2026-06-20", "running")
        self.assertIsNotNone(row["adapted_at"])
        self.assertEqual(row["adaptation_count"], 1)

        proposed[0]["description"] = "Cut further to easy walk"
        service.workout_adapt_apply(proposed, "Still fatigued", "2026-06-20", "2026-06-20")
        row = test_db.get_workout("2026-06-20", "running")
        self.assertEqual(row["adaptation_count"], 2)

    def test_adapt_swap_inherits_displaced_session_as_original(self):
        """A cross-sport swap (strength -> yoga) deletes the planned strength session
        and inserts a yoga one. The new session should inherit the displaced strength
        session's description + load as its `original_*` snapshot, so the Calendar event
        can surface what was originally planned."""
        test_db.save_workout(
            "2026-07-02", "strength_training", "Heavy Legs",
            "5x5 back squat + accessories.",
            duration_minutes=60, rpe=7, tss=70,
        )

        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=Mock()
        )
        proposed = [{
            "date": "2026-07-02", "sport_type": "yoga", "title": "Easy Mobility",
            "description": "20 min easy mobility flow.",
            "modification_reason": "Swapped from strength after a workload spike.",
            "duration_minutes": 30, "rpe": 1, "tss": 4,
        }]
        service.workout_adapt_apply(
            proposed, "Reduce load", "2026-07-02", "2026-07-02"
        )

        # The strength row is gone; the yoga row carries the strength session's
        # planned description and load as its original snapshot.
        self.assertIsNone(test_db.get_workout("2026-07-02", "strength_training"))
        row = test_db.get_workout("2026-07-02", "yoga")
        self.assertEqual(row["description"], "20 min easy mobility flow.")
        self.assertEqual(row["original_description"], "5x5 back squat + accessories.")
        self.assertEqual(row["original_duration_minutes"], 60)
        self.assertEqual(row["original_tss"], 70)
        self.assertEqual(row["original_rpe"], 7)
        # Current load reflects the swapped-in yoga session.
        self.assertEqual(row["duration_minutes"], 30)
        self.assertEqual(row["tss"], 4)
        self.assertEqual(row["rpe"], 1)

    def test_already_eased_tag_in_planned_prompt(self):
        """An already-eased session is tagged with its count + recency for the adapt
        prompt; an unadapted session is not."""
        from trainmate.coach.formatting import format_planned_workouts_detailed

        eased = {
            "date": "2026-06-18", "sport_type": "running", "title": "Easy Tempo",
            "description": "Z2", "duration_minutes": 35, "rpe": 5, "tss": 30,
            "modification_reason": "Eased", "adaptation_summary": "Block too hard",
            "adapted_at": "2026-06-17T08:00:00+00:00", "adaptation_count": 2,
        }
        untouched = {
            "date": "2026-06-19", "sport_type": "yoga", "title": "Mobility",
            "description": "easy", "duration_minutes": 20, "rpe": 2, "tss": 10,
        }
        text = format_planned_workouts_detailed(
            [eased, untouched], eval_date="2026-06-20"
        )
        self.assertIn("ALREADY EASED", text)
        self.assertIn("2x", text)
        self.assertIn("3 days ago", text)
        # The unadapted yoga line carries no such tag.
        yoga_line = [ln for ln in text.splitlines() if "(YOGA)" in ln][0]
        self.assertNotIn("ALREADY EASED", yoga_line)
