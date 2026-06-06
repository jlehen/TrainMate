import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate_cli.db = test_db


class TestTrainMateCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate_cli.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def run_cli(self, args, input_value="n"):
        return run_cli(args, input_value)

    def test_help_and_usage(self):
        exit_code, stdout, stderr = self.run_cli(["--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("TrainMate - Local Training Coach CLI", stdout)
        self.assertIn("goal", stdout)
        self.assertIn("lifeevent", stdout)
        self.assertIn("workout", stdout)
        self.assertIn("plan", stdout)
        self.assertIn("status", stdout)
        self.assertIn("data", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("push", stdout)

        exit_code, stdout, stderr = self.run_cli(["data", "--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("pull", stdout)

    def test_goal_commands(self):
        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== TRAINING OBJECTIVES / GOALS ===", stdout)
        self.assertNotIn("ID:", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "--title", "Zurich Marathon",
            "--date", "2026-10-15",
            "--sport", "running",
            "--desc", "Target sub 3:30",
            "--priority", "1",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal 'Zurich Marathon' added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "--title", "Morning Yoga Flow",
            "--date", "2026-10-20",
            "--sport", "yoga",
            "--desc", "Daily mindfulness and flexibility",
            "--priority", "2",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal 'Morning Yoga Flow' added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "--title", "Hybrid Strength Endurance",
            "--date", "2026-11-30",
            "--sport", "road_biking", "strength_training",
            "--desc", "Aging well routine",
            "--priority", "3",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal 'Hybrid Strength Endurance' added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Zurich Marathon", stdout)
        self.assertIn("running", stdout)
        self.assertIn("2026-10-15", stdout)
        self.assertIn("Priority: 1", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertIn("Hybrid Strength Endurance", stdout)
        self.assertIn("road_biking,strength_training", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "rm", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal with ID 1 removed successfully", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Zurich Marathon", stdout)

    def test_lifeevent_commands(self):
        exit_code, stdout, stderr = self.run_cli(["lifeevent", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ATHLETE LIFE EVENTS ===", stdout)
        self.assertNotIn("ID:", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "lifeevent", "add",
            "--title", "Ibiza Vacation",
            "--start", "2026-07-01",
            "--end", "2026-07-08",
            "--type", "vacation",
            "--desc", "50% intensity",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Life event 'Ibiza Vacation' logged", stdout)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("vacation", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertNotIn("Impact:", stdout)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "list", "-v"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Impact:\n    50% intensity", stdout)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "list", "--verbose"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Impact:\n    50% intensity", stdout)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "show", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertIn("Impact:\n    50% intensity", stdout)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "show", "999"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Life event with ID 999 not found.", stdout)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "rm", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Life event with ID 1 removed successfully", stdout)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "list"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Ibiza Vacation", stdout)

    def test_goal_edit_command(self):
        self.run_cli([
            "goal", "add",
            "--title", "Berlin Marathon",
            "--date", "2026-09-27",
            "--sport", "running",
            "--desc", "Sub 3:15 goal",
            "--priority", "2",
        ])

        goals = test_db.get_objectives()
        self.assertEqual(len(goals), 1)
        g_id = goals[0]["id"]

        exit_code, stdout, stderr = self.run_cli([
            "goal", "edit", str(g_id),
            "--title", "Berlin Marathon Elite",
            "--date", "2026-09-28",
            "--sport", "running", "strength_training",
            "--desc", "Sub 3:10 elite goal",
            "--priority", "1",
            "--status", "completed",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Goal with ID {g_id} updated successfully", stdout)

        edited = test_db.get_objective(g_id)
        self.assertEqual(edited["title"], "Berlin Marathon Elite")
        self.assertEqual(edited["target_date"], "2026-09-28")
        self.assertEqual(edited["sport_type"], "running,strength_training")
        self.assertEqual(edited["description"], "Sub 3:10 elite goal")
        self.assertEqual(edited["priority"], 1)
        self.assertEqual(edited["status"], "completed")

        exit_code, stdout, stderr = self.run_cli(["goal", "edit", "999", "--title", "Fail"])
        self.assertEqual(exit_code, 1)
        self.assertIn("Goal with ID 999 not found", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "edit", str(g_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn("No fields to update", stdout)

    def test_lifeevent_edit_command(self):
        self.run_cli([
            "lifeevent", "add",
            "--title", "Summer Vacation",
            "--start", "2026-08-01",
            "--end", "2026-08-15",
            "--type", "vacation",
            "--desc", "No workouts",
        ])

        events = test_db.get_lifeevents()
        e_id = events[0]["id"]

        exit_code, stdout, stderr = self.run_cli([
            "lifeevent", "edit", str(e_id),
            "--title", "Summer Vacation Adapted",
            "--start", "2026-08-02",
            "--end", "2026-08-16",
            "--type", "business_trip",
            "--desc", "Light running only",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Life event with ID {e_id} updated successfully", stdout)

        edited = test_db.get_lifeevent(e_id)
        self.assertEqual(edited["title"], "Summer Vacation Adapted")
        self.assertEqual(edited["start_date"], "2026-08-02")
        self.assertEqual(edited["end_date"], "2026-08-16")
        self.assertEqual(edited["event_type"], "business_trip")
        self.assertEqual(edited["impact_description"], "Light running only")

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "edit", "999", "--title", "Fail"])
        self.assertEqual(exit_code, 1)
        self.assertIn("Life event with ID 999 not found", stdout)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "edit", str(e_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn("No fields to update", stdout)

    @patch("trainmate_cli.sheets_reader")
    @patch("trainmate_cli.coach_service")
    def test_workout_commands(self, mock_coach, mock_sheets_reader):
        mock_coach.adapt.return_value = (
            "Metrics are green",
            [{
                "date": "2026-06-03",
                "sport_type": "running",
                "title": "Steady Ride",
                "duration_minutes": 60,
                "rpe": 5,
                "tss": 40.0,
            }],
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUT SCHEDULE ===", stdout)
        self.assertNotIn("Description:", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "adapt", "--auto"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Evaluating daily Garmin metrics adaptation", stdout)
        self.assertIn("Metrics are green", stdout)
        self.assertIn("Adaptations applied and synced to calendar successfully.", stdout)
        mock_coach.adapt.assert_called_once()

        w_id = test_db.save_workout(
            date="2026-06-02",
            sport_type="running",
            title="Interval Session",
            description="5x800m",
            status="planned",
        )
        exit_code, stdout, stderr = self.run_cli(["workout", "rm", str(w_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn(
            f"Workout with ID {w_id} ('Interval Session') removed successfully", stdout
        )

    @patch("trainmate_cli.sheets_reader")
    @patch("trainmate_cli.coach_service")
    def test_plan_commands(self, mock_coach, mock_sheets_reader):
        mock_coach.generate_periodization_plan.return_value = (
            "Test coaching plan strategy",
            [{"name": "Base Building", "start_date": "2026-06-01", "end_date": "2026-06-28"}],
        )
        mock_coach.generate_workouts.return_value = (
            "Test workout reasoning",
            [{"title": "Test Workout"}],
        )

        exit_code, stdout, stderr = self.run_cli(["plan", "generate"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== PERIODIZATION PLAN GENERATED BY COACH ===", stdout)
        self.assertIn("Test coaching plan strategy", stdout)
        mock_coach.generate_periodization_plan.assert_called_once_with(force=False)

        mock_coach.generate_periodization_plan.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["plan", "generate", "-f"])
        self.assertEqual(exit_code, 0)
        mock_coach.generate_periodization_plan.assert_called_once_with(force=True)

        mock_coach.generate_periodization_plan.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["plan", "generate", "--force"])
        self.assertEqual(exit_code, 0)
        mock_coach.generate_periodization_plan.assert_called_once_with(force=True)

        exit_code, stdout, stderr = self.run_cli(["workout", "generate"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUTS GENERATED BY COACH ===", stdout)
        self.assertIn("Test workout reasoning", stdout)
        mock_coach.generate_workouts.assert_called_once()

        exit_code, stdout, stderr = self.run_cli(["plan", "show"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No active goals found", stdout)

        obj_id = test_db.add_objective(
            title="London Marathon",
            target_date="2026-09-20",
            sport_type="running",
            priority=1,
        )

        exit_code, stdout, stderr = self.run_cli(["plan", "show"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No active macrocycle strategy found", stdout)

        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Build base then taper",
            goals_hash="ghash",
            lifeevents_hash="chash",
            mesocycles=[{
                "name": "Base Building",
                "start_date": "2026-06-01",
                "end_date": "2026-06-28",
                "focus": "Aerobic threshold volume",
            }],
        )

        exit_code, stdout, stderr = self.run_cli(["plan", "show"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ACTIVE MACROCYCLE STRATEGY ===", stdout)
        self.assertIn("Build base then taper", stdout)
        self.assertIn("Base Building", stdout)

        mock_coach.delete_plan.reset_mock()
        obj_to_rm = test_db.add_objective(
            title="Goal to remove plan for",
            target_date="2026-09-22",
            sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_to_rm,
            strategy="Plan to delete",
            goals_hash="ghash",
            lifeevents_hash="lhash",
            mesocycles=[],
        )
        exit_code, stdout, stderr = self.run_cli(["plan", "rm", str(obj_to_rm)])
        self.assertEqual(exit_code, 0)
        self.assertIn("removed successfully", stdout)
        mock_coach.delete_plan.assert_called_once_with(obj_to_rm)

    def test_status_command(self):
        test_db.save_metric_cache(
            date="2026-05-31",
            rhr=48, hrv=82, sleep_score=90, stress=15,
            acute_workload=4.0, chronic_workload=3.5, acwr=1.14,
        )
        test_db.save_baseline(
            date="2026-05-31",
            rhr_mean=50.0, rhr_std=1.5,
            hrv_mean=78.0, hrv_std=4.0,
            sleep_mean=82.0, sleep_std=3.0,
        )
        test_db.add_objective(
            title="London Marathon", target_date="2026-09-20",
            sport_type="running", priority=1,
        )
        test_db.save_coach_memory("training_strategy", "Focus on aerobic base")
        test_db.save_coach_memory("athlete_learnings", "Rest well on Fridays")

        exit_code, stdout, stderr = self.run_cli(["status"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== TRAINMATE ATHLETE STATUS ===", stdout)
        self.assertIn("London Marathon", stdout)
        self.assertIn("Resting HR : 48 bpm", stdout)
        self.assertIn("Overnight HRV: 82 ms", stdout)
        self.assertIn("ACWR       : 1.14", stdout)
        self.assertIn("Baselines (28-day)", stdout)
        self.assertIn("Strategy:\n  Focus on aerobic base", stdout)
        self.assertIn("Learnings:\n  Rest well on Fridays", stdout)

        test_db.add_lifeevent(
            title="Ibiza Trip", start_date="2026-07-01", end_date="2026-07-08",
            event_type="vacation", impact_description="Rest weeks",
        )

        goals = test_db.get_objectives()
        g_id = goals[0]["id"]
        events = test_db.get_lifeevents()
        e_id = events[0]["id"]

        exit_code_v, stdout_v, _ = self.run_cli(["status", "-v"])
        self.assertEqual(exit_code_v, 0)
        self.assertIn("Goals:", stdout_v)
        self.assertIn(
            f"- [ACTIVE] ID: {g_id} | London Marathon (running) on 2026-09-20 (Priority: 1)",
            stdout_v,
        )
        self.assertIn("Life Events:", stdout_v)
        self.assertIn(
            f"- ID: {e_id} | Ibiza Trip (vacation): 2026-07-01 to 2026-07-08",
            stdout_v,
        )
        self.assertIn("  Impact:\n    Rest weeks", stdout_v)

        exit_code_vv, stdout_vv, _ = self.run_cli(["status", "--verbose"])
        self.assertEqual(exit_code_vv, 0)
        self.assertIn("Goals:", stdout_vv)
        self.assertIn(
            f"- [ACTIVE] ID: {g_id} | London Marathon (running) on 2026-09-20 (Priority: 1)",
            stdout_vv,
        )

    @patch("trainmate_cli.calendar_syncer")
    def test_workout_push_command(self, mock_calendar):
        exit_code, stdout, stderr = self.run_cli(["workout", "push"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No new or modified workouts to sync", stdout)
        mock_calendar.sync_multiple.assert_not_called()

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo Run",
            description="30 mins fast", status="planned",
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "push"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Syncing 1 workouts to Google Calendar", stdout)
        mock_calendar.sync_multiple.assert_called_once()

    @patch("trainmate_cli.sheets_reader")
    def test_data_pull_command(self, mock_sheets_reader):
        exit_code, stdout, stderr = self.run_cli(["data", "pull"])
        self.assertEqual(exit_code, 0)
        mock_sheets_reader.sync_data.assert_called_once()

    def test_invalid_command(self):
        exit_code, stdout, stderr = self.run_cli(["invalidcmd"])
        self.assertEqual(exit_code, 2)
        self.assertIn("invalid choice: 'invalidcmd'", stderr)

    @patch("trainmate_cli.calendar_syncer")
    def test_workout_rm_synced(self, mock_calendar):
        w_id = test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Synced Run",
            description="30 mins", status="synced", google_event_id="mock_event_123",
        )
        exit_code, stdout, stderr = self.run_cli(["workout", "rm", str(w_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn(
            "Workout is synced to Google Calendar. Attempting to delete calendar event",
            stdout,
        )
        self.assertIn(f"Workout with ID {w_id} ('Synced Run') removed successfully", stdout)
        mock_calendar.delete_workout_event.assert_called_once_with("mock_event_123")

    @patch("trainmate_cli.sheets_reader")
    def test_plan_workout_data_pull_prompt(self, mock_sheets_reader):
        test_db.save_metric_cache(
            date="2026-06-03", rhr=50, hrv=75, sleep_score=80, stress=20
        )
        with patch("builtins.input") as mock_input:
            exit_code, stdout, stderr = self.run_cli(["plan", "show"])
            self.assertEqual(exit_code, 0)
            mock_input.assert_not_called()
            mock_sheets_reader.sync_data.assert_not_called()

        with test_db._get_connection() as conn:
            conn.execute("DELETE FROM athlete_metrics_cache")
            conn.commit()

        exit_code, stdout, stderr = self.run_cli(["plan", "show"], input_value="n")
        self.assertEqual(exit_code, 0)
        mock_sheets_reader.sync_data.assert_not_called()

        exit_code, stdout, stderr = self.run_cli(["plan", "show"], input_value="y")
        self.assertEqual(exit_code, 0)
        mock_sheets_reader.sync_data.assert_called_once()

    def test_goal_wipe(self):
        test_db.add_objective(
            title="Wipe Target", target_date="2026-10-15", sport_type="running"
        )
        self.assertEqual(len(test_db.get_objectives()), 1)

        exit_code, stdout, stderr = self.run_cli(["goal", "wipe"], input_value="n")
        self.assertEqual(exit_code, 0)
        self.assertIn("Wipe cancelled.", stdout)
        self.assertEqual(len(test_db.get_objectives()), 1)

        exit_code, stdout, stderr = self.run_cli(["goal", "wipe"], input_value="y")
        self.assertEqual(exit_code, 0)
        self.assertIn("All training objectives wiped successfully.", stdout)
        self.assertEqual(len(test_db.get_objectives()), 0)

        test_db.add_objective(
            title="Wipe Target 2", target_date="2026-10-15", sport_type="running"
        )
        exit_code, stdout, stderr = self.run_cli(["goal", "wipe", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_objectives()), 0)

    def test_lifeevent_wipe(self):
        test_db.add_lifeevent(
            title="Wipe Event", start_date="2026-07-01",
            end_date="2026-07-02", event_type="party",
        )
        self.assertEqual(len(test_db.get_lifeevents()), 1)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "wipe"], input_value="n")
        self.assertEqual(exit_code, 0)
        self.assertIn("Wipe cancelled.", stdout)
        self.assertEqual(len(test_db.get_lifeevents()), 1)

        exit_code, stdout, stderr = self.run_cli(["lifeevent", "wipe"], input_value="y")
        self.assertEqual(exit_code, 0)
        self.assertIn("All life events wiped successfully.", stdout)
        self.assertEqual(len(test_db.get_lifeevents()), 0)

        test_db.add_lifeevent(
            title="Wipe Event 2", start_date="2026-07-01",
            end_date="2026-07-02", event_type="party",
        )
        exit_code, stdout, stderr = self.run_cli(["lifeevent", "wipe", "--yes"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_lifeevents()), 0)

    def test_plan_wipe(self):
        obj_id = test_db.add_objective(
            title="Plan Wipe Obj", target_date="2026-10-15", sport_type="running"
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Base",
            goals_hash="ghash",
            lifeevents_hash="lhash",
            mesocycles=[{
                "name": "Meso1", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Aerobic",
            }],
        )
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj_id))

        exit_code, stdout, stderr = self.run_cli(["plan", "wipe"], input_value="n")
        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj_id))

        exit_code, stdout, stderr = self.run_cli(["plan", "wipe"], input_value="y")
        self.assertEqual(exit_code, 0)
        self.assertIn("All periodization plans wiped successfully.", stdout)
        self.assertIsNone(test_db.get_macrocycle_for_objective(obj_id))

        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Base",
            goals_hash="ghash",
            lifeevents_hash="lhash",
            mesocycles=[{
                "name": "Meso1", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Aerobic",
            }],
        )
        exit_code, stdout, stderr = self.run_cli(["plan", "wipe", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertIsNone(test_db.get_macrocycle_for_objective(obj_id))

    @patch("trainmate_cli.calendar_syncer")
    def test_workout_wipe(self, mock_calendar):
        test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", status="synced", google_event_id="ge_1",
        )
        test_db.save_workout(
            date="2026-06-03", sport_type="running", title="Run 2",
            description="30 mins", status="planned",
        )
        self.assertEqual(len(test_db.get_workouts()), 2)

        exit_code, stdout, stderr = self.run_cli(["workout", "wipe"], input_value="n")
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_workouts()), 2)
        mock_calendar.delete_workout_event.assert_not_called()

        exit_code, stdout, stderr = self.run_cli(["workout", "wipe"], input_value="y")
        self.assertEqual(exit_code, 0)
        self.assertIn("All workouts wiped successfully.", stdout)
        self.assertEqual(len(test_db.get_workouts()), 0)
        mock_calendar.delete_workout_event.assert_called_once_with("ge_1")

        mock_calendar.reset_mock()
        test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", status="synced", google_event_id="ge_2",
        )
        exit_code, stdout, stderr = self.run_cli(["workout", "wipe", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_workouts()), 0)
        mock_calendar.delete_workout_event.assert_called_once_with("ge_2")

    def test_data_wipe(self):
        test_db.save_metric_cache(
            date="2026-05-31", rhr=48, hrv=82, sleep_score=90, stress=15
        )
        test_db.save_baseline(
            date="2026-05-31", rhr_mean=50.0, rhr_std=1.5,
            hrv_mean=78.0, hrv_std=4.0, sleep_mean=82.0, sleep_std=3.0,
        )
        test_db.save_completed_activity(
            activity_id="act_1", date="2026-05-31", start_time="10:00",
            activity_name="Run", activity_type="running", duration_sec=1800,
            distance_km=5.0, elevation_gain_m=50, avg_hr=150, max_hr=170,
            rpe=5, tss=30.0,
        )

        self.assertEqual(len(test_db.get_metrics_cache()), 1)
        self.assertIsNotNone(test_db.get_baseline("2026-05-31"))
        self.assertEqual(len(test_db.get_completed_activities()), 1)

        exit_code, stdout, stderr = self.run_cli(["data", "wipe"], input_value="n")
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_metrics_cache()), 1)
        self.assertEqual(len(test_db.get_completed_activities()), 1)

        exit_code, stdout, stderr = self.run_cli(["data", "wipe"], input_value="y")
        self.assertEqual(exit_code, 0)
        self.assertIn(
            "All metrics, baselines, and completed activities wiped successfully.", stdout
        )
        self.assertEqual(len(test_db.get_metrics_cache()), 0)
        self.assertIsNone(test_db.get_baseline("2026-05-31"))
        self.assertEqual(len(test_db.get_completed_activities()), 0)

        test_db.save_metric_cache(
            date="2026-05-31", rhr=48, hrv=82, sleep_score=90, stress=15
        )
        exit_code, stdout, stderr = self.run_cli(["data", "wipe", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_metrics_cache()), 0)

    def test_workout_list_filters(self):
        today_date = datetime.now(timezone.utc).date()
        today_str = today_date.strftime("%Y-%m-%d")
        tomorrow_str = (today_date + timedelta(days=1)).strftime("%Y-%m-%d")
        past_str = (today_date - timedelta(days=5)).strftime("%Y-%m-%d")
        future_str = (today_date + timedelta(days=10)).strftime("%Y-%m-%d")

        test_db.save_workout(
            date=today_str, sport_type="running", title="Today Run",
            description="30 mins", status="planned",
        )
        test_db.save_workout(
            date=tomorrow_str, sport_type="road_biking", title="Tomorrow Ride",
            description="60 mins", status="planned",
        )
        test_db.save_workout(
            date=past_str, sport_type="yoga", title="Past Yoga",
            description="15 mins", status="planned",
        )
        test_db.save_workout(
            date=future_str, sport_type="strength_training", title="Future Lift",
            description="45 mins", status="planned",
        )

        goal_id = test_db.add_objective(
            title="Berlin Marathon",
            target_date=(today_date + timedelta(days=20)).strftime("%Y-%m-%d"),
            sport_type="running",
            priority=1,
            status="active",
        )
        test_db.save_macrocycle(
            objective_id=goal_id,
            strategy="Base strategy",
            goals_hash="ghash",
            lifeevents_hash="lhash",
            mesocycles=[{
                "name": "Base Building",
                "start_date": (today_date - timedelta(days=2)).strftime("%Y-%m-%d"),
                "end_date": (today_date + timedelta(days=5)).strftime("%Y-%m-%d"),
                "focus": "Aerobic conditioning",
            }],
        )

        macro = test_db.get_macrocycle_for_objective(goal_id)
        mesos = test_db.get_mesocycles_for_macrocycle(macro["id"])
        meso_id = mesos[0]["id"]

        exit_code, stdout, stderr = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertIn("Past Yoga", stdout)
        self.assertIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "list", "--type", "running"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Today Run", stdout)
        self.assertNotIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "list", "--days", "2"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "--from", tomorrow_str, "--until", tomorrow_str
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "--mesocycle", str(meso_id)
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "--until-mesocycle", str(meso_id)
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "--goal", str(goal_id)
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "list", "--from-mesocycle"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertIn("Future Lift", stdout)

    def test_workout_compare(self):
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        yesterday_str = yesterday.strftime("%Y-%m-%d")
        today_str = today.strftime("%Y-%m-%d")

        # Planned run yesterday (will be matched), planned run today (missed)
        test_db.save_workout(
            date=yesterday_str, sport_type="running", title="Easy Run",
            description="30 mins", duration_minutes=30, rpe=4, tss=20,
        )
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo Run",
            description="45 mins", duration_minutes=45, rpe=7, tss=50,
        )
        # Complete yesterday's run (matching load/duration — no discrepancy expected)
        test_db.save_completed_activity(
            activity_id="act_cmp_1",
            date=yesterday_str,
            start_time=f"{yesterday_str} 08:00:00",
            activity_name="Morning Run",
            activity_type="running",
            duration_sec=1800.0,
            distance_km=5.0,
            elevation_gain_m=50.0,
            avg_hr=140,
            max_hr=160,
            rpe=4,
            tss=20.0,
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "compare", "--days", "2"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUT COMPARE ===", stdout)
        self.assertIn("Easy Run", stdout)
        self.assertIn("Morning Run", stdout)
        self.assertIn("Tempo Run", stdout)
        self.assertIn("(none — missed)", stdout)
        self.assertIn("=== DISCREPANCIES ===", stdout)
        self.assertIn("Complete Miss", stdout)

        # Date range with no data → empty message
        exit_code, stdout, stderr = self.run_cli([
            "workout", "compare", "--from", "2020-01-01", "--until", "2020-01-02"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("No planned workouts or completed activities found", stdout)

    @patch("trainmate_cli.coach_service")
    def test_data_analyze_command(self, mock_coach):
        mock_coach.analyze_workouts.return_value = {
            "macrocycle_summary": "Simulated base building results",
            "inferred_macrocycle": {
                "overall_focus": "aerobic base building",
                "start_date": "2026-01-01",
                "end_date": "2026-03-31"
            },
            "inferred_mesocycles": [
                {
                    "name": "Base Building Phase",
                    "start_date": "2026-01-01",
                    "end_date": "2026-02-15",
                    "focus_detected": "Volume",
                    "average_weekly_tss": 300,
                    "estimated_consistency": "High"
                }
            ],
            "physiological_insights": [
                "HRV was stable during peak volume."
            ],
            "learnings_for_coach_memory": "Responds well to volume"
        }

        exit_code, stdout, stderr = self.run_cli([
            "data", "analyze", "--from", "2026-01-01", "--until", "2026-03-31",
            "--context", "Felt good"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== HISTORICAL WORKOUT ANALYSIS REPORT ===", stdout)
        self.assertIn("Macrocycle Focus: aerobic base building", stdout)
        self.assertIn("Base Building Phase", stdout)
        self.assertIn("HRV was stable during peak volume", stdout)
        self.assertIn("Coach Observations (Saved to memory):", stdout)
        self.assertIn("Responds well to volume", stdout)
        mock_coach.analyze_workouts.assert_called_once_with(
            from_date_str="2026-01-01",
            until_date_str="2026-03-31",
            days=None,
            weeks=None,
            context="Felt good"
        )


if __name__ == "__main__":
    unittest.main()

