import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_periodization.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.service.db = test_db

from trainmate.coach import coach_service


class TestPeriodization(unittest.TestCase):
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

    def test_hashing_helpers(self):
        hash1 = coach_service._get_goals_hash([])
        hash2 = coach_service._get_lifeevents_hash([])
        hash3 = coach_service._get_config_hash()
        self.assertIsNotNone(hash1)
        self.assertIsNotNone(hash2)
        self.assertIsNotNone(hash3)

        obj = {
            "id": 1, "title": "Test Goal", "target_date": "2026-10-15",
            "sport_type": "running", "description": "sub 3hr",
            "priority": 1, "status": "active",
        }
        hash1_with_obj = coach_service._get_goals_hash([obj])
        self.assertNotEqual(hash1, hash1_with_obj)

        obj["description"] = "sub 2:50"
        self.assertNotEqual(hash1_with_obj, coach_service._get_goals_hash([obj]))

        c = {
            "id": 1, "title": "Spain Trip", "start_date": "2026-07-01",
            "end_date": "2026-07-08", "event_type": "vacation",
            "impact_description": "easy",
        }
        self.assertNotEqual(hash2, coach_service._get_lifeevents_hash([c]))

    @patch("trainmate.coach.engine.openrouter_client")
    def test_replan_logic_and_caching(self, mock_client):
        obj_id = test_db.add_objective(
            title="Berlin Marathon", target_date="2026-09-27",
            sport_type="running", priority=1,
        )

        mock_macro_response = {
            "strategy": "Simulated overall strategy",
            "mesocycles": [
                {"name": "Base Building", "start_date": "2026-06-01",
                 "end_date": "2026-06-28", "focus": "Endurance"},
                {"name": "Peak & Taper", "start_date": "2026-06-29",
                 "end_date": "2026-07-05", "focus": "Taper"},
            ],
        }
        mock_workouts_response = {
            "reasoning": "Microcycle generated reasoning",
            "learning_updates": [{"op": "add", "text": "Simulated learnings"}],
            "workouts": [{
                "date": "2026-06-01", "sport_type": "running",
                "title": "Base Run", "description": "45 mins zone 2",
            }],
        }

        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        reason, workouts = coach_service.replan(force=False)
        self.assertEqual(reason, "Microcycle generated reasoning")
        self.assertEqual(len(workouts), 1)
        self.assertEqual(workouts[0]["title"], "Base Run")
        self.assertEqual(mock_client.complete.call_count, 2)

        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertIsNotNone(macro)
        self.assertEqual(macro["strategy"], "Simulated overall strategy")
        self.assertEqual(len(test_db.get_mesocycles_for_macrocycle(macro["id"])), 2)

        # No changes + force=False → reuse macrocycle, generate workouts only
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = [mock_workouts_response]
        coach_service.replan(force=False)
        self.assertEqual(mock_client.complete.call_count, 1)

        # force=True → regenerate everything
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        coach_service.replan(force=True)
        self.assertEqual(mock_client.complete.call_count, 2)

        # New goal added → hash mismatch → regenerate everything
        test_db.add_objective(
            title="Mini Triathlon", target_date="2026-08-01",
            sport_type="road_biking", priority=2,
        )
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        coach_service.replan(force=False)
        self.assertEqual(mock_client.complete.call_count, 2)

    def test_system_prompt_inserts_periodization(self):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Run long and slow",
            goals_hash="hash1",
            lifeevents_hash="hash2",
            mesocycles=[
                {"name": "Base Building", "start_date": "2026-06-01",
                 "end_date": "2026-06-28", "focus": "Zone 2 runs"},
                {"name": "Peak & Taper", "start_date": "2026-06-29",
                 "end_date": "2026-07-05", "focus": "Tapering"},
            ],
        )

        objs = test_db.get_objectives(status="active")
        prompt = coach_service._get_coach_system_prompt(objs, [])

        self.assertIn("Run long and slow", prompt)
        self.assertIn("Base Building (2026-06-01 to 2026-06-28): Zone 2 runs", prompt)
        self.assertIn("Peak & Taper (2026-06-29 to 2026-07-05): Tapering", prompt)
        self.assertIn("COACH LEARNINGS & ACTIVE PERIODIZATION STRATEGY:", prompt)
        self.assertIn("START OF SPORTS SCIENCE GUIDELINES", prompt)
        self.assertIn("END OF SPORTS SCIENCE GUIDELINES", prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_replan_provides_previous_strategy_context_to_llm(self, mock_client):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="old_goals_hash",
            lifeevents_hash="old_constraints_hash",
            mesocycles=[{
                "name": "Base Building", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Aerobic conditioning",
            }],
        )

        mock_client.complete.side_effect = [
            {
                "strategy": "New strategy building on previous",
                "mesocycles": [{
                    "name": "Specific Prep", "start_date": "2026-06-01",
                    "end_date": "2026-06-28", "focus": "Faster runs",
                }],
            },
            {"reasoning": "Reasoning", "workouts": []},
        ]

        # Life event triggers hash mismatch → replanning
        test_db.add_lifeevent(
            title="Business Trip", start_date="2026-06-10", end_date="2026-06-12",
            event_type="business_trip", impact_description="limited training time",
        )

        coach_service.replan(force=False)

        self.assertEqual(mock_client.complete.call_count, 2)
        system_prompt = mock_client.complete.call_args_list[0][0][0]
        self.assertIn("PREVIOUS PERIODIZATION STRATEGY (FOR CONTEXT):", system_prompt)
        self.assertIn("Keep heart rate low", system_prompt)
        self.assertIn(
            "Base Building (2026-06-01 to 2026-06-28): Aerobic conditioning",
            system_prompt,
        )
        self.assertIn(
            "For context, the PREVIOUS periodization strategy that was in place",
            system_prompt,
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_generate_injects_planned_vs_actual(self, mock_client):
        # Option A (DESIGN_backward_evaluation.md §6): the prior plan's elapsed blocks are
        # compared against what was actually completed, and fed into the strategy prompt.
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="Old strategy",
            goals_hash="g", lifeevents_hash="l",
            mesocycles=[{
                "name": "Base Building", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Aerobic conditioning",
            }],
        )
        # A completed session inside that elapsed block.
        test_db.save_completed_activity(
            activity_id="a1", date="2026-06-01", start_time="08:00:00",
            activity_name="Base Run", activity_type="running",
            duration_sec=3600.0, distance_km=10.0, elevation_gain_m=50.0,
            avg_hr=140, max_hr=160, rpe=5, tss=60.0,
        )
        mock_client.complete.return_value = {
            "strategy": "New strategy", "mesocycles": [{
                "name": "Build", "start_date": "2026-06-08",
                "end_date": "2026-10-15", "focus": "Threshold",
            }],
        }
        coach_service.plan_generate(force=True, objective_id=obj_id)
        system_prompt = mock_client.complete.call_args[0][0]
        self.assertIn("PRIOR TRAINING REVIEW:", system_prompt)
        self.assertIn("PLANNED vs ACTUAL", system_prompt)
        self.assertIn("Aerobic conditioning", system_prompt)
        self.assertIn("1 sessions", system_prompt)

    def test_system_prompt_inserts_athlete_profile(self):
        test_profile = {
            "name": "Jane Doe",
            "birth_year": 1990,
            "max_hr": 190,
            "lthr": 170,
            "weekly_target_hours": 8.0,
            "sport_preferences": ["running", "yoga"],
            "chronic_injuries": "Tendency for runner's knee.",
            "preferences": "Enjoys morning runs.",
            "equipment": ["Garmin Watch", "Yoga Mat"],
            "weekly_schedule": {
                "Monday": {
                    "total_available_hours": 1.5,
                    "max_sessions": 2,
                    "certainty_percent": 95,
                    "equipment": ["treadmill"],
                },
                "Wednesday": 0.0,
            },
        }
        with patch.dict(trainmate.coach.config.data, {"user_profile": test_profile}):
            prompt = coach_service._get_coach_system_prompt([], [])
            self.assertIn("Jane Doe", prompt)
            self.assertIn("Birth Year: 1990", prompt)
            self.assertIn("Tendency for runner's knee.", prompt)
            self.assertIn("Enjoys morning runs.", prompt)
            self.assertIn("Garmin Watch, Yoga Mat", prompt)
            self.assertIn(
                "Monday: 1.5 hours | Max sessions: 2 | Certainty: 95% (Equipment: treadmill)",
                prompt,
            )
            self.assertIn("Wednesday: 0.0 hours", prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_plan_and_workouts_separately(self, mock_client):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )

        mock_client.complete.return_value = {
            "strategy": "Separate strategy philosophy",
            "mesocycles": [{
                "name": "Base Phase", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }

        with self.assertRaises(ValueError):
            coach_service.workout_generate()

        strategy, mesos, _ = coach_service.plan_generate(force=False)
        self.assertEqual(strategy, "Separate strategy philosophy")
        self.assertEqual(len(mesos), 1)
        mock_client.complete.assert_called_once()

        mock_client.complete.reset_mock()
        mock_client.complete.return_value = {
            "reasoning": "Separate workout reasoning",
            "workouts": [{
                "date": "2026-06-01", "sport_type": "running",
                "title": "Base Run", "description": "30 mins",
            }],
        }
        reason, workouts = coach_service.workout_generate()
        self.assertEqual(reason, "Separate workout reasoning")
        self.assertEqual(len(workouts), 1)
        self.assertEqual(workouts[0]["title"], "Base Run")
        mock_client.complete.assert_called_once()

    @patch("trainmate.coach.service.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_workouts_clears_stale_synced_workouts(
        self, mock_client, mock_calendar
    ):
        """Regenerating workouts must wipe the previous plan's future workouts,
        including synced ones (and delete their Google Calendar events)."""
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )

        mock_client.complete.return_value = {
            "strategy": "Strategy", "mesocycles": [{
                "name": "Base", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }
        coach_service.plan_generate(force=False)

        # Simulate a stale workout from the old plan that was synced to Calendar.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=future, sport_type="running", title="Old Plan Run",
            description="stale", synced=True, google_event_id="evt-old-123",
        )

        mock_client.complete.reset_mock()
        mock_client.complete.return_value = {
            "reasoning": "New plan", "workouts": [{
                "date": today, "sport_type": "running",
                "title": "New Run", "description": "fresh",
            }],
        }
        coach_service.workout_generate()

        # The stale synced workout is gone, and only the new workout remains.
        remaining = test_db.get_workouts(start_date=today)
        titles = [w["title"] for w in remaining]
        self.assertNotIn("Old Plan Run", titles)
        self.assertEqual(titles, ["New Run"])
        # Its Google Calendar event was deleted.
        mock_calendar.delete_workout_event.assert_called_once_with("evt-old-123")

    @patch("trainmate.coach.service.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_workouts_clears_stale_unsynced_calendar_workouts(
        self, mock_client, mock_calendar
    ):
        """A workout that was pushed then adapted/swapped (synced=0 but with a
        google_event_id) must still have its Calendar event deleted on regenerate —
        i.e. cleanup keys on google_event_id, not the sync flag (orphan guard)."""
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )

        mock_client.complete.return_value = {
            "strategy": "Strategy", "mesocycles": [{
                "name": "Base", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }
        coach_service.plan_generate(force=False)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        # On the calendar (google_event_id) but pending re-push after an adaptation.
        test_db.save_workout(
            date=future, sport_type="running", title="Adapted Run",
            description="adapted", synced=False, google_event_id="evt-stale-456",
            modification_reason="swapped",
        )

        mock_client.complete.reset_mock()
        mock_client.complete.return_value = {
            "reasoning": "New plan", "workouts": [{
                "date": today, "sport_type": "running",
                "title": "New Run", "description": "fresh",
            }],
        }
        coach_service.workout_generate()

        remaining = test_db.get_workouts(start_date=today)
        self.assertEqual([w["title"] for w in remaining], ["New Run"])
        mock_calendar.delete_workout_event.assert_called_once_with("evt-stale-456")

    @patch("trainmate.coach.service.config")
    def test_load_science_guidelines(self, mock_config):
        temp_app_dir = tempfile.mkdtemp()
        temp_user_dir = tempfile.mkdtemp()

        try:
            mock_config.app_science_dir = temp_app_dir
            mock_config.science_dir = temp_user_dir

            with open(os.path.join(temp_app_dir, "app_science.txt"), "w") as f:
                f.write("App guideline text")
            with open(os.path.join(temp_user_dir, "user_science.txt"), "w") as f:
                f.write("User guideline text")

            guidelines = coach_service._load_science_guidelines()

            self.assertIn("=== Guidelines from app_science.txt ===", guidelines)
            self.assertIn("App guideline text", guidelines)
            self.assertIn("=== Guidelines from user_science.txt ===", guidelines)
            self.assertIn("User guideline text", guidelines)
        finally:
            shutil.rmtree(temp_app_dir)
            shutil.rmtree(temp_user_dir)

    def test_config_hash_logic(self):
        initial_hash = coach_service._get_config_hash()
        self.assertIsNotNone(initial_hash)

        original_profile = dict(trainmate.coach.config.data["user_profile"])
        try:
            trainmate.coach.config.data["user_profile"]["weekly_target_hours"] = 20.0
            self.assertNotEqual(initial_hash, coach_service._get_config_hash())
        finally:
            trainmate.coach.config.data["user_profile"] = original_profile

    def test_db_config_hash_operations(self):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Long runs",
            goals_hash="ghash",
            lifeevents_hash="lehash",
            config_hash="confhash123",
            mesocycles=[],
        )

        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro["config_hash"], "confhash123")

        test_db.update_macrocycle_config_hash(macro_id, "newconfhash456")
        self.assertEqual(
            test_db.get_macrocycle_for_objective(obj_id)["config_hash"], "newconfhash456"
        )

    def test_validation_under_5_weeks(self):
        today = datetime.now(timezone.utc).date()
        test_db.add_objective(
            title="Short Goal",
            target_date=(today + timedelta(weeks=3)).strftime("%Y-%m-%d"),
            sport_type="running",
            priority=1,
        )

        with self.assertRaises(ValueError) as ctx:
            coach_service.plan_generate()
        self.assertIn("too close", str(ctx.exception))

    @patch("trainmate.coach.engine.openrouter_client")
    def test_splitting_over_24_weeks(self, mock_client):
        today = datetime.now(timezone.utc).date()
        target_date_str = (today + timedelta(weeks=30)).strftime("%Y-%m-%d")

        test_db.add_objective(
            title="Ultra Marathon", target_date=target_date_str,
            sport_type="running", priority=1,
        )

        phase_date_str = (today + timedelta(weeks=15)).strftime("%Y-%m-%d")
        mock_client.complete.side_effect = [
            {
                "goals": [{
                    "title": "Ultra Marathon - Interim: Half Marathon Tune-Up",
                    "target_date": phase_date_str,
                    "sport_type": "running",
                    "description": "Mid-way aerobic benchmark",
                    "priority": 1,
                }]
            },
            {
                "strategy": "Simulated base building strategy",
                "mesocycles": [{
                    "name": "Base Building",
                    "start_date": today.strftime("%Y-%m-%d"),
                    "end_date": phase_date_str,
                    "focus": "Aerobic conditioning",
                }],
            },
        ]

        strategy, mesos, _ = coach_service.plan_generate(force=True)
        self.assertEqual(mock_client.complete.call_count, 2)

        active_objs = sorted(
            test_db.get_objectives(status="active"), key=lambda x: str(x["target_date"])
        )
        self.assertEqual(len(active_objs), 2)

        intermediate = active_objs[0]
        self.assertEqual(intermediate["title"], "Ultra Marathon - Interim: Half Marathon Tune-Up")
        self.assertEqual(intermediate["target_date"], phase_date_str)

        macro = test_db.get_macrocycle_for_objective(intermediate["id"])
        self.assertIsNotNone(macro)
        self.assertEqual(macro["strategy"], "Simulated base building strategy")

    @patch("trainmate.coach.engine.openrouter_client")
    def test_multi_goal_planning_and_deletion(self, mock_client):
        obj1_id = test_db.add_objective(
            title="Goal A", target_date="2026-08-01",
            sport_type="running", priority=1,
        )
        obj2_id = test_db.add_objective(
            title="Goal B", target_date="2026-11-01",
            sport_type="running", priority=2,
        )

        mock_client.complete.side_effect = [
            {
                "strategy": "Plan A strategy",
                "mesocycles": [{
                    "name": "Base Building A", "start_date": "2026-06-05",
                    "end_date": "2026-08-01", "focus": "Aerobic conditioning",
                }],
            },
            {
                "strategy": "Plan B strategy",
                "mesocycles": [{
                    "name": "Base Building B", "start_date": "2026-08-02",
                    "end_date": "2026-11-01", "focus": "Aerobic threshold",
                }],
            },
        ]

        strategy_a, mesos_a, _ = coach_service.plan_generate(
            force=True, objective_id=obj1_id
        )
        self.assertEqual(strategy_a, "Plan A strategy")
        self.assertEqual(mesos_a[0]["start_date"], "2026-06-05")

        strategy_b, mesos_b, _ = coach_service.plan_generate(
            force=True, objective_id=obj2_id
        )
        self.assertEqual(strategy_b, "Plan B strategy")
        self.assertIn("from 2026-08-02 until", mock_client.complete.call_args_list[1][0][0])

        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj1_id))
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj2_id))

        coach_service.plan_rm(obj1_id)
        self.assertIsNone(test_db.get_macrocycle_for_objective(obj1_id))
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj2_id))

    def test_config_user_profile_validation(self):
        with patch.dict(trainmate.coach.config.data, {"user_profile": {"lthr": 170, "ftp": 220}}):
            profile = trainmate.coach.config.user_profile
            self.assertEqual(profile["lthr"], 170)
            self.assertEqual(profile["ftp"], 220)

        with patch.dict(trainmate.coach.config.data, {"user_profile": {"lthr": 170}}):
            self.assertEqual(trainmate.coach.config.user_profile["lthr"], 170)

        with patch.dict(trainmate.coach.config.data, {"user_profile": {"ftp": 220}}):
            self.assertEqual(trainmate.coach.config.user_profile["ftp"], 220)

        with patch.dict(trainmate.coach.config.data, {"user_profile": {"name": "Test Athlete"}}):
            with self.assertRaises(ValueError) as ctx:
                _ = trainmate.coach.config.user_profile
            self.assertIn("must contain at least 'lthr' or 'ftp'", str(ctx.exception))

    @patch("trainmate.coach.engine.openrouter_client")
    def test_recent_history_summary_periodization_plan(self, mock_client):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_completed_activity(
            activity_id="act_test_1", date="2026-06-04", start_time="08:00:00",
            activity_name="Morning Run", activity_type="running",
            duration_sec=3600.0, distance_km=10.0, elevation_gain_m=100.0,
            avg_hr=150, max_hr=170, rpe=6, tss=60.0,
        )
        test_db.save_metric_cache(
            date="2026-06-04", rhr=55, hrv=60, sleep_score=80, stress=25,
            acute_workload=420.0, chronic_workload=400.0, acwr=1.05,
        )

        mock_client.complete.return_value = {
            "strategy": "Separate strategy philosophy",
            "mesocycles": [{
                "name": "Base Phase", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }

        coach_service.plan_generate(force=True)

        system_prompt = mock_client.complete.call_args[0][0]
        self.assertIn("ATHLETE RECENT TRAINING SUMMARY (PAST 15 DAYS):", system_prompt)
        self.assertIn("Completed Workouts (Past 15 days):", system_prompt)
        self.assertIn("running: 1 sessions", system_prompt)
        self.assertIn("Resting Heart Rate: 55.0 bpm", system_prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_recent_history_workout_generation(self, mock_client):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="hash1",
            lifeevents_hash="hash2",
            mesocycles=[{
                "name": "Base Phase", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        )
        test_db.save_metric_cache(
            date="2026-06-04", rhr=55, hrv=60, sleep_score=80, stress=25,
            acute_workload=420.0, chronic_workload=400.0, acwr=1.05,
        )

        mock_client.complete.return_value = {
            "reasoning": "Separate workout reasoning",
            "workouts": [{
                "date": "2026-06-05", "sport_type": "running",
                "title": "Base Run", "description": "30 mins",
            }],
        }

        coach_service.workout_generate()

        user_content = mock_client.complete.call_args[0][1]
        self.assertIn("Athlete's Metrics History (Past 15 Days):", user_content)
        self.assertIn("RHR=55bpm, HRV=60ms", user_content)


if __name__ == "__main__":
    unittest.main()
