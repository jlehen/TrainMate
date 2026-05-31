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
            conn.execute("DELETE FROM constraints")
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
        self.assertIn("constraint", stdout)
        self.assertIn("workout", stdout)
        self.assertIn("status", stdout)
        self.assertIn("sync", stdout)
        self.assertIn("sync-sheets", stdout)

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

        # 3. List goals again to check info
        exit_code, stdout, stderr = self.run_cli(['goal', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Zurich Marathon", stdout)
        self.assertIn("running", stdout)
        self.assertIn("2026-10-15", stdout)
        self.assertIn("Priority: 1", stdout)
        self.assertIn("ID: 1", stdout)

        # 4. Remove goal
        exit_code, stdout, stderr = self.run_cli(['goal', 'rm', '1'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal with ID 1 removed successfully", stdout)

        # 5. Verify removal via list
        exit_code, stdout, stderr = self.run_cli(['goal', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Zurich Marathon", stdout)

    def test_constraint_commands(self):
        # 1. List constraints when empty
        exit_code, stdout, stderr = self.run_cli(['constraint', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ATHLETE CONSTRAINTS ===", stdout)
        self.assertNotIn("ID:", stdout)

        # 2. Add constraint
        exit_code, stdout, stderr = self.run_cli([
            'constraint', 'add',
            '--title', 'Ibiza Vacation',
            '--start', '2026-07-01',
            '--end', '2026-07-08',
            '--type', 'vacation',
            '--desc', '50% intensity'
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Constraint 'Ibiza Vacation' logged", stdout)

        # 3. List constraints again
        exit_code, stdout, stderr = self.run_cli(['constraint', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("vacation", stdout)
        self.assertIn("ID: 1", stdout)

        # 4. Remove constraint
        exit_code, stdout, stderr = self.run_cli(['constraint', 'rm', '1'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Constraint with ID 1 removed successfully", stdout)

        # 5. Verify removal
        exit_code, stdout, stderr = self.run_cli(['constraint', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Ibiza Vacation", stdout)

    @patch('trainmate_cli.sheets_reader')
    @patch('trainmate_cli.coach_engine')
    def test_workout_commands(self, mock_coach, mock_sheets_reader):
        # Mock responses
        mock_coach.replan.return_value = ("Test coaching plan reasoning", [{"title": "Test Workout"}])
        mock_coach.daily_adapt.return_value = ("Metrics are green", {"title": "Steady Ride"})

        # 1. List workouts when empty
        exit_code, stdout, stderr = self.run_cli(['workout', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUT SCHEDULE ===", stdout)
        self.assertNotIn("Description:", stdout)

        # 2. Replan workouts
        exit_code, stdout, stderr = self.run_cli(['workout', 'plan'])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== PLAN GENERATED BY COACH ===", stdout)
        self.assertIn("Test coaching plan reasoning", stdout)
        mock_coach.replan.assert_called_once()

        # 3. Adapt workouts
        exit_code, stdout, stderr = self.run_cli(['workout', 'adapt'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Evaluating daily Garmin metrics adaptation", stdout)
        self.assertIn("Metrics are green", stdout)
        self.assertIn("Adapted Workout Synced to Calendar: Steady Ride", stdout)
        mock_coach.daily_adapt.assert_called_once()

        # 4. Remove workout
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
    def test_sync_command(self, mock_calendar):
        # 1. Sync when no planned workouts
        exit_code, stdout, stderr = self.run_cli(['sync'])
        self.assertEqual(exit_code, 0)
        self.assertIn("No new or modified workouts to sync", stdout)
        mock_calendar.sync_multiple.assert_not_called()

        # 2. Add a workout to db
        test_db.save_workout(
            date="2026-06-01",
            sport_type="running",
            title="Tempo Run",
            description="30 mins fast",
            status="planned"
        )

        # Sync again
        exit_code, stdout, stderr = self.run_cli(['sync'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Syncing 1 workouts to Google Calendar", stdout)
        mock_calendar.sync_multiple.assert_called_once()

    @patch('trainmate_cli.sheets_reader')
    def test_sync_sheets_command(self, mock_sheets_reader):
        exit_code, stdout, stderr = self.run_cli(['sync-sheets'])
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
