import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import io

# Define test database path
TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli.db")

# Override db singleton inside trainmate before anything else imports it
from trainmate.db import Database
import trainmate.db

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db

# Now import trainmate_cli and override its db reference
import trainmate_cli
trainmate_cli.db = test_db

class TestTrainMateCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Remove any leftover test database
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        # Re-initialize database schema
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate_cli.db = test_db

    @classmethod
    def tearDownClass(cls):
        # Remove test database
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        # Clear all tables to start each test on a clean slate
        with test_db._get_connection() as conn:
            conn.execute("DELETE FROM objectives")
            conn.execute("DELETE FROM lifeevents")
            conn.execute("DELETE FROM workouts")
            conn.execute("DELETE FROM athlete_metrics_cache")
            conn.execute("DELETE FROM athlete_baselines")
            conn.execute("DELETE FROM coach_memory")
            conn.commit()

    def run_cli(self, args):
        """Helper to invoke CLI main() with captured stdout/stderr and custom argv."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, 'argv', ['trainmate_cli.py'] + args):
            with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
                try:
                    trainmate_cli.main()
                    exit_code = 0
                except SystemExit as e:
                    exit_code = e.code
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_help_and_usage(self):
        exit_code, stdout, stderr = self.run_cli(['--help'])
        self.assertEqual(exit_code, 0)
        self.assertIn("TrainMate - Local Training Coach CLI", stdout)
        self.assertIn("goal", stdout)
        self.assertIn("lifeevent", stdout)
        self.assertIn("workout", stdout)
        self.assertIn("plan", stdout)
        self.assertIn("status", stdout)
        self.assertIn("metrics", stdout)

        # Test workout sub-command help
        exit_code, stdout, stderr = self.run_cli(['workout', '--help'])
        self.assertEqual(exit_code, 0)
        self.assertIn("push", stdout)

        # Test metrics sub-command help
        exit_code, stdout, stderr = self.run_cli(['metrics', '--help'])
        self.assertEqual(exit_code, 0)
        self.assertIn("pull", stdout)

    def test_goal_commands(self):
        # 1. List goals when empty
        exit_code, stdout, stderr = self.run_cli(['goal', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== TRAINING OBJECTIVES / GOALS ===", stdout)
        self.assertNotIn("ID:", stdout)

        # 2. Add goal
        exit_code, stdout, stderr = self.run_cli([
            'goal', 'add',
            '--title', 'Zurich Marathon',
            '--date', '2026-10-15',
            '--sport', 'running',
            '--desc', 'Target sub 3:30',
            '--priority', '1'
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal 'Zurich Marathon' added successfully", stdout)

        # 2b. Add goal with yoga
        exit_code, stdout, stderr = self.run_cli([
            'goal', 'add',
            '--title', 'Morning Yoga Flow',
            '--date', '2026-10-20',
            '--sport', 'yoga',
            '--desc', 'Daily mindfulness and flexibility',
            '--priority', '2'
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal 'Morning Yoga Flow' added successfully", stdout)

        # 2c. Add goal with multiple sports
        exit_code, stdout, stderr = self.run_cli([
            'goal', 'add',
            '--title', 'Hybrid Strength Endurance',
            '--date', '2026-11-30',
            '--sport', 'road_biking', 'strength_training',
            '--desc', 'Aging well routine',
            '--priority', '3'
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal 'Hybrid Strength Endurance' added successfully", stdout)

        # 3. List goals again to check info
        exit_code, stdout, stderr = self.run_cli(['goal', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Zurich Marathon", stdout)
        self.assertIn("running", stdout)
        self.assertIn("2026-10-15", stdout)
        self.assertIn("Priority: 1", stdout)
        self.assertIn("ID: 1", stdout)
        
        self.assertIn("Hybrid Strength Endurance", stdout)
        self.assertIn("road_biking,strength_training", stdout)

        # 4. Remove goal
        exit_code, stdout, stderr = self.run_cli(['goal', 'rm', '1'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal with ID 1 removed successfully", stdout)

        # 5. Verify removal via list
        exit_code, stdout, stderr = self.run_cli(['goal', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Zurich Marathon", stdout)

    def test_lifeevent_commands(self):
        # 1. List life events when empty
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ATHLETE LIFE EVENTS ===", stdout)
        self.assertNotIn("ID:", stdout)

        # 2. Add life event
        exit_code, stdout, stderr = self.run_cli([
            'lifeevent', 'add',
            '--title', 'Ibiza Vacation',
            '--start', '2026-07-01',
            '--end', '2026-07-08',
            '--type', 'vacation',
            '--desc', '50% intensity'
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Life event 'Ibiza Vacation' logged", stdout)

        # 3. List life events again
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("vacation", stdout)
        self.assertIn("ID: 1", stdout)

        # 4. Remove life event
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'rm', '1'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Life event with ID 1 removed successfully", stdout)

        # 5. Verify removal
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Ibiza Vacation", stdout)

    @patch('trainmate_cli.sheets_reader')
    @patch('trainmate_cli.coach_engine')
    def test_workout_commands(self, mock_coach, mock_sheets_reader):
        # Mock responses
        mock_coach.adapt.return_value = (
            "Metrics are green",
            [{
                "date": "2026-06-03",
                "sport_type": "running",
                "title": "Steady Ride",
                "duration_minutes": 60,
                "rpe": 5,
                "tss": 40.0
            }]
        )
 
        # 1. List workouts when empty
        exit_code, stdout, stderr = self.run_cli(['workout', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUT SCHEDULE ===", stdout)
        self.assertNotIn("Description:", stdout)
 
        # 2. Adapt workouts
        exit_code, stdout, stderr = self.run_cli(['workout', 'adapt', '--auto'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Evaluating daily Garmin metrics adaptation", stdout)
        self.assertIn("Metrics are green", stdout)
        self.assertIn("Adaptations applied and synced to calendar successfully.", stdout)
        mock_coach.adapt.assert_called_once()
 
        # 3. Remove workout
        w_id = test_db.save_workout(
            date="2026-06-02",
            sport_type="running",
            title="Interval Session",
            description="5x800m",
            status="planned"
        )
        exit_code, stdout, stderr = self.run_cli(['workout', 'rm', str(w_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Workout with ID {w_id} ('Interval Session') removed successfully", stdout)

    @patch('trainmate_cli.sheets_reader')
    @patch('trainmate_cli.coach_engine')
    def test_plan_commands(self, mock_coach, mock_sheets_reader):
        mock_coach.generate_periodization_plan.return_value = (
            "Test coaching plan strategy",
            [{"name": "Base Building", "start_date": "2026-06-01", "end_date": "2026-06-28"}]
        )
        mock_coach.generate_workouts.return_value = (
            "Test workout reasoning",
            [{"title": "Test Workout"}]
        )

        # 1. Generate plan (plan generate)
        exit_code, stdout, stderr = self.run_cli(['plan', 'generate'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== PERIODIZATION PLAN GENERATED BY COACH ===", stdout)
        self.assertIn("Test coaching plan strategy", stdout)
        mock_coach.generate_periodization_plan.assert_called_once_with(force=False)

        # 1b. Generate plan with force
        mock_coach.generate_periodization_plan.reset_mock()
        exit_code, stdout, stderr = self.run_cli(['plan', 'generate', '-f'])
        self.assertEqual(exit_code, 0)
        mock_coach.generate_periodization_plan.assert_called_once_with(force=True)

        mock_coach.generate_periodization_plan.reset_mock()
        exit_code, stdout, stderr = self.run_cli(['plan', 'generate', '--force'])
        self.assertEqual(exit_code, 0)
        mock_coach.generate_periodization_plan.assert_called_once_with(force=True)

        # 1c. Generate workouts (workout generate)
        exit_code, stdout, stderr = self.run_cli(['workout', 'generate'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUTS GENERATED BY COACH ===", stdout)
        self.assertIn("Test workout reasoning", stdout)
        mock_coach.generate_workouts.assert_called_once()

        # 2. Plan show when no active goal
        exit_code, stdout, stderr = self.run_cli(['plan', 'show'])
        self.assertEqual(exit_code, 0)
        self.assertIn("No active goals found", stdout)

        # Add active goal
        obj_id = test_db.add_objective(
            title="London Marathon",
            target_date="2026-09-20",
            sport_type="running",
            priority=1
        )
        
        # Plan show when no macrocycle
        exit_code, stdout, stderr = self.run_cli(['plan', 'show'])
        self.assertEqual(exit_code, 0)
        self.assertIn("No active periodization strategy found", stdout)

        # Save macrocycle and mesocycles
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Build base then taper",
            goals_hash="ghash",
            lifeevents_hash="chash",
            mesocycles=[
                {
                    "name": "Base Building",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-28",
                    "focus": "Aerobic threshold volume"
                }
            ]
        )

        # Plan show success
        exit_code, stdout, stderr = self.run_cli(['plan', 'show'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ACTIVE PERIODIZATION STRATEGY ===", stdout)
        self.assertIn("Build base then taper", stdout)
        self.assertIn("Base Building", stdout)

    def test_status_command(self):
        # Seed test metrics
        test_db.save_metric_cache(
            date="2026-05-31",
            rhr=48,
            hrv=82,
            sleep_score=90,
            stress=15,
            acute_workload=4.0,
            chronic_workload=3.5,
            acwr=1.14
        )
        test_db.save_baseline(
            date="2026-05-31",
            rhr_mean=50.0,
            rhr_std=1.5,
            hrv_mean=78.0,
            hrv_std=4.0,
            sleep_mean=82.0,
            sleep_std=3.0
        )
        test_db.add_objective(
            title="London Marathon",
            target_date="2026-09-20",
            sport_type="running",
            priority=1
        )
        test_db.save_coach_memory("training_strategy", "Focus on aerobic base")
        test_db.save_coach_memory("athlete_learnings", "Rest well on Fridays")

        exit_code, stdout, stderr = self.run_cli(['status'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== TRAINMATE ATHLETE STATUS ===", stdout)
        self.assertIn("London Marathon", stdout)
        self.assertIn("Resting HR : 48 bpm", stdout)
        self.assertIn("Overnight HRV: 82 ms", stdout)
        self.assertIn("ACWR       : 1.14", stdout)
        self.assertIn("Baselines (28-day)", stdout)
        self.assertIn("Strategy  : Focus on aerobic base", stdout)
        self.assertIn("Learnings : Rest well on Fridays", stdout)

    @patch('trainmate_cli.calendar_syncer')
    def test_workout_push_command(self, mock_calendar):
        # 1. Sync when no planned workouts
        exit_code, stdout, stderr = self.run_cli(['workout', 'push'])
        self.assertEqual(exit_code, 0)
        self.assertIn("No new or modified workouts to sync", stdout)
        mock_calendar.sync_multiple.assert_not_called()

        # 2. Add a workout to db
        from datetime import datetime, timezone
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=today_str,
            sport_type="running",
            title="Tempo Run",
            description="30 mins fast",
            status="planned"
        )

        # Sync again
        exit_code, stdout, stderr = self.run_cli(['workout', 'push'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Syncing 1 workouts to Google Calendar", stdout)
        mock_calendar.sync_multiple.assert_called_once()

    @patch('trainmate_cli.sheets_reader')
    def test_metrics_pull_command(self, mock_sheets_reader):
        exit_code, stdout, stderr = self.run_cli(['metrics', 'pull'])
        self.assertEqual(exit_code, 0)
        mock_sheets_reader.sync_data.assert_called_once()

    def test_invalid_command(self):
        # argparse prints error and exits with code 2 for invalid subcommand
        exit_code, stdout, stderr = self.run_cli(['invalidcmd'])
        self.assertEqual(exit_code, 2)
        self.assertIn("invalid choice: 'invalidcmd'", stderr)

    @patch('trainmate_cli.calendar_syncer')
    def test_workout_rm_synced(self, mock_calendar):
        w_id = test_db.save_workout(
            date="2026-06-02",
            sport_type="running",
            title="Synced Run",
            description="30 mins",
            status="synced",
            google_event_id="mock_event_123"
        )
        exit_code, stdout, stderr = self.run_cli(['workout', 'rm', str(w_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn("Workout is synced to Google Calendar. Attempting to delete calendar event", stdout)
        self.assertIn(f"Workout with ID {w_id} ('Synced Run') removed successfully", stdout)
        mock_calendar.delete_workout_event.assert_called_once_with("mock_event_123")

if __name__ == "__main__":
    unittest.main()
