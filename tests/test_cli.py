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

    def run_cli(self, args, input_value='n'):
        """Helper to invoke CLI main() with captured stdout/stderr and custom argv."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, 'argv', ['trainmate_cli.py'] + args):
            with (
                patch('sys.stdout', stdout),
                patch('sys.stderr', stderr),
                patch('builtins.input', return_value=input_value)
            ):
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

        # 3. List life events again (default should not show impact description)
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("vacation", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertNotIn("Impact:", stdout)

        # 3b. List life events with -v / --verbose (should show impact description)
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'list', '-v'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("vacation", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertIn("Impact:\n    50% intensity", stdout)

        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'list', '--verbose'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Impact:\n    50% intensity", stdout)

        # 3c. Show life event by ID
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'show', '1'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("vacation", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertIn("Impact:\n    50% intensity", stdout)

        # Show non-existent life event
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'show', '999'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Life event with ID 999 not found.", stdout)

        # 4. Remove life event
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'rm', '1'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Life event with ID 1 removed successfully", stdout)

        # 5. Verify removal
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'list'])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Ibiza Vacation", stdout)

    def test_goal_edit_command(self):
        # 1. Add goal
        exit_code, stdout, stderr = self.run_cli([
            'goal', 'add',
            '--title', 'Berlin Marathon',
            '--date', '2026-09-27',
            '--sport', 'running',
            '--desc', 'Sub 3:15 goal',
            '--priority', '2'
        ])
        self.assertEqual(exit_code, 0)
        
        # Get its ID (should be 1 since setUp clears db)
        goals = test_db.get_objectives()
        self.assertEqual(len(goals), 1)
        g_id = goals[0]['id']
        
        # 2. Edit goal fields
        exit_code, stdout, stderr = self.run_cli([
            'goal', 'edit', str(g_id),
            '--title', 'Berlin Marathon Elite',
            '--date', '2026-09-28',
            '--sport', 'running', 'strength_training',
            '--desc', 'Sub 3:10 elite goal',
            '--priority', '1',
            '--status', 'completed'
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Goal with ID {g_id} updated successfully", stdout)
        
        # 3. Verify changes
        edited_goal = test_db.get_objective(g_id)
        self.assertIsNotNone(edited_goal)
        self.assertEqual(edited_goal['title'], 'Berlin Marathon Elite')
        self.assertEqual(edited_goal['target_date'], '2026-09-28')
        self.assertEqual(edited_goal['sport_type'], 'running,strength_training')
        self.assertEqual(edited_goal['description'], 'Sub 3:10 elite goal')
        self.assertEqual(edited_goal['priority'], 1)
        self.assertEqual(edited_goal['status'], 'completed')

        # 4. Try editing non-existent goal
        exit_code, stdout, stderr = self.run_cli([
            'goal', 'edit', '999', '--title', 'Fail'
        ])
        self.assertEqual(exit_code, 1)
        self.assertIn("Goal with ID 999 not found", stdout)

        # 5. Try editing with no fields
        exit_code, stdout, stderr = self.run_cli(['goal', 'edit', str(g_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn("No fields to update", stdout)

    def test_lifeevent_edit_command(self):
        # 1. Add lifeevent
        exit_code, stdout, stderr = self.run_cli([
            'lifeevent', 'add',
            '--title', 'Summer Vacation',
            '--start', '2026-08-01',
            '--end', '2026-08-15',
            '--type', 'vacation',
            '--desc', 'No workouts'
        ])
        self.assertEqual(exit_code, 0)
        
        # Get its ID
        events = test_db.get_lifeevents()
        self.assertEqual(len(events), 1)
        e_id = events[0]['id']
        
        # 2. Edit event fields
        exit_code, stdout, stderr = self.run_cli([
            'lifeevent', 'edit', str(e_id),
            '--title', 'Summer Vacation Adapted',
            '--start', '2026-08-02',
            '--end', '2026-08-16',
            '--type', 'business_trip',
            '--desc', 'Light running only'
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Life event with ID {e_id} updated successfully", stdout)
        
        # 3. Verify changes
        edited_event = test_db.get_lifeevent(e_id)
        self.assertIsNotNone(edited_event)
        self.assertEqual(edited_event['title'], 'Summer Vacation Adapted')
        self.assertEqual(edited_event['start_date'], '2026-08-02')
        self.assertEqual(edited_event['end_date'], '2026-08-16')
        self.assertEqual(edited_event['event_type'], 'business_trip')
        self.assertEqual(edited_event['impact_description'], 'Light running only')

        # 4. Try editing non-existent event
        exit_code, stdout, stderr = self.run_cli([
            'lifeevent', 'edit', '999', '--title', 'Fail'
        ])
        self.assertEqual(exit_code, 1)
        self.assertIn("Life event with ID 999 not found", stdout)

        # 5. Try editing with no fields
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'edit', str(e_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn("No fields to update", stdout)

    @patch('trainmate_cli.sheets_reader')
    @patch('trainmate_cli.coach_service')
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
    @patch('trainmate_cli.coach_service')
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
        self.assertIn("No active macrocycle strategy found", stdout)

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
        self.assertIn("=== ACTIVE MACROCYCLE STRATEGY ===", stdout)
        self.assertIn("Build base then taper", stdout)
        self.assertIn("Base Building", stdout)

        # 3. Plan rm command
        mock_coach.delete_plan.reset_mock()
        obj_to_rm = test_db.add_objective(
            title="Goal to remove plan for",
            target_date="2026-09-22",
            sport_type="running"
        )
        test_db.save_macrocycle(
            objective_id=obj_to_rm,
            strategy="Plan to delete",
            goals_hash="ghash",
            lifeevents_hash="lhash",
            mesocycles=[]
        )
        exit_code, stdout, stderr = self.run_cli(['plan', 'rm', str(obj_to_rm)])
        self.assertEqual(exit_code, 0)
        self.assertIn("removed successfully", stdout)
        mock_coach.delete_plan.assert_called_once_with(obj_to_rm)

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
        self.assertIn("Strategy:\n  Focus on aerobic base", stdout)
        self.assertIn("Learnings:\n  Rest well on Fridays", stdout)

        # Test verbose status with goals and lifeevents
        test_db.add_lifeevent(
            title="Ibiza Trip",
            start_date="2026-07-01",
            end_date="2026-07-08",
            event_type="vacation",
            impact_description="Rest weeks"
        )

        goals = test_db.get_objectives()
        g_id = goals[0]['id']
        events = test_db.get_lifeevents()
        e_id = events[0]['id']

        exit_code_v, stdout_v, stderr_v = self.run_cli(['status', '-v'])
        self.assertEqual(exit_code_v, 0)
        self.assertIn("Goals:", stdout_v)
        self.assertIn(
            f"- [ACTIVE] ID: {g_id} | London Marathon (running) on 2026-09-20 (Priority: 1)",
            stdout_v
        )
        self.assertIn("Life Events:", stdout_v)
        self.assertIn(
            f"- ID: {e_id} | Ibiza Trip (vacation): 2026-07-01 to 2026-07-08",
            stdout_v
        )
        self.assertIn("  Impact:\n    Rest weeks", stdout_v)

        exit_code_verbose, stdout_verbose, stderr_verbose = self.run_cli(
            ['status', '--verbose']
        )
        self.assertEqual(exit_code_verbose, 0)
        self.assertIn("Goals:", stdout_verbose)
        self.assertIn(
            f"- [ACTIVE] ID: {g_id} | London Marathon (running) on 2026-09-20 (Priority: 1)",
            stdout_verbose
        )
        self.assertIn("Life Events:", stdout_verbose)
        self.assertIn(
            f"- ID: {e_id} | Ibiza Trip (vacation): 2026-07-01 to 2026-07-08",
            stdout_verbose
        )
        self.assertIn("  Impact:\n    Rest weeks", stdout_verbose)

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
        self.assertIn(
            "Workout is synced to Google Calendar. Attempting to delete calendar event",
            stdout
        )
        self.assertIn(f"Workout with ID {w_id} ('Synced Run') removed successfully", stdout)
        mock_calendar.delete_workout_event.assert_called_once_with("mock_event_123")

    @patch('trainmate_cli.sheets_reader')
    def test_plan_workout_metrics_pull_prompt(self, mock_sheets_reader):
        # Case 1: Metrics are already pulled (not empty cache)
        test_db.save_metric_cache(
            date="2026-06-03", rhr=50, hrv=75, sleep_score=80, stress=20
        )
        with patch('builtins.input') as mock_input:
            exit_code, stdout, stderr = self.run_cli(['plan', 'show'])
            self.assertEqual(exit_code, 0)
            mock_input.assert_not_called()
            mock_sheets_reader.sync_data.assert_not_called()

        # Clear metrics cache to trigger prompt
        with test_db._get_connection() as conn:
            conn.execute("DELETE FROM athlete_metrics_cache")
            conn.commit()

        # Case 2: Metrics cache is empty, user declines prompt ('n')
        exit_code, stdout, stderr = self.run_cli(['plan', 'show'], input_value='n')
        self.assertEqual(exit_code, 0)
        mock_sheets_reader.sync_data.assert_not_called()

        # Case 3: Metrics cache is empty, user accepts prompt ('y')
        exit_code, stdout, stderr = self.run_cli(['plan', 'show'], input_value='y')
        self.assertEqual(exit_code, 0)
        mock_sheets_reader.sync_data.assert_called_once()

    def test_goal_wipe(self):
        # Seed objective
        test_db.add_objective(
            title="Wipe Target", target_date="2026-10-15", sport_type="running"
        )
        self.assertEqual(len(test_db.get_objectives()), 1)

        # 1. Decline wipe
        exit_code, stdout, stderr = self.run_cli(['goal', 'wipe'], input_value='n')
        self.assertEqual(exit_code, 0)
        self.assertIn("Wipe cancelled.", stdout)
        self.assertEqual(len(test_db.get_objectives()), 1)

        # 2. Confirm wipe
        exit_code, stdout, stderr = self.run_cli(['goal', 'wipe'], input_value='y')
        self.assertEqual(exit_code, 0)
        self.assertIn("All training objectives wiped successfully.", stdout)
        self.assertEqual(len(test_db.get_objectives()), 0)

        # Seed again
        test_db.add_objective(
            title="Wipe Target 2", target_date="2026-10-15", sport_type="running"
        )
        self.assertEqual(len(test_db.get_objectives()), 1)

        # 3. Wipe with -y
        exit_code, stdout, stderr = self.run_cli(['goal', 'wipe', '-y'])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_objectives()), 0)

    def test_lifeevent_wipe(self):
        # Seed life event
        test_db.add_lifeevent(
            title="Wipe Event", start_date="2026-07-01",
            end_date="2026-07-02", event_type="party"
        )
        self.assertEqual(len(test_db.get_lifeevents()), 1)

        # 1. Decline
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'wipe'], input_value='n')
        self.assertEqual(exit_code, 0)
        self.assertIn("Wipe cancelled.", stdout)
        self.assertEqual(len(test_db.get_lifeevents()), 1)

        # 2. Confirm
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'wipe'], input_value='y')
        self.assertEqual(exit_code, 0)
        self.assertIn("All life events wiped successfully.", stdout)
        self.assertEqual(len(test_db.get_lifeevents()), 0)

        # Seed again
        test_db.add_lifeevent(
            title="Wipe Event 2", start_date="2026-07-01",
            end_date="2026-07-02", event_type="party"
        )
        self.assertEqual(len(test_db.get_lifeevents()), 1)

        # 3. Wipe with --yes
        exit_code, stdout, stderr = self.run_cli(['lifeevent', 'wipe', '--yes'])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_lifeevents()), 0)

    def test_plan_wipe(self):
        # Seed macro and meso
        obj_id = test_db.add_objective(
            title="Plan Wipe Obj", target_date="2026-10-15", sport_type="running"
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Base",
            goals_hash="ghash",
            lifeevents_hash="lhash",
            mesocycles=[{"name": "Meso1", "start_date": "2026-06-01", "end_date": "2026-06-28",
                         "focus": "Aerobic"}]
        )
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj_id))

        # 1. Decline
        exit_code, stdout, stderr = self.run_cli(['plan', 'wipe'], input_value='n')
        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj_id))

        # 2. Confirm
        exit_code, stdout, stderr = self.run_cli(['plan', 'wipe'], input_value='y')
        self.assertEqual(exit_code, 0)
        self.assertIn("All periodization plans wiped successfully.", stdout)
        self.assertIsNone(test_db.get_macrocycle_for_objective(obj_id))

        # Seed again
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Base",
            goals_hash="ghash",
            lifeevents_hash="lhash",
            mesocycles=[{"name": "Meso1", "start_date": "2026-06-01", "end_date": "2026-06-28",
                         "focus": "Aerobic"}]
        )
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj_id))

        # 3. Wipe with -y
        exit_code, stdout, stderr = self.run_cli(['plan', 'wipe', '-y'])
        self.assertEqual(exit_code, 0)
        self.assertIsNone(test_db.get_macrocycle_for_objective(obj_id))

    @patch('trainmate_cli.calendar_syncer')
    def test_workout_wipe(self, mock_calendar):
        # Seed workouts
        w_id1 = test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", status="synced", google_event_id="ge_1"
        )
        w_id2 = test_db.save_workout(
            date="2026-06-03", sport_type="running", title="Run 2",
            description="30 mins", status="planned"
        )
        self.assertEqual(len(test_db.get_workouts()), 2)

        # 1. Decline
        exit_code, stdout, stderr = self.run_cli(['workout', 'wipe'], input_value='n')
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_workouts()), 2)
        mock_calendar.delete_workout_event.assert_not_called()

        # 2. Confirm
        exit_code, stdout, stderr = self.run_cli(['workout', 'wipe'], input_value='y')
        self.assertEqual(exit_code, 0)
        self.assertIn("All workouts wiped successfully.", stdout)
        self.assertEqual(len(test_db.get_workouts()), 0)
        mock_calendar.delete_workout_event.assert_called_once_with("ge_1")

        # Seed again
        mock_calendar.reset_mock()
        test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", status="synced", google_event_id="ge_2"
        )
        self.assertEqual(len(test_db.get_workouts()), 1)

        # 3. Wipe with -y
        exit_code, stdout, stderr = self.run_cli(['workout', 'wipe', '-y'])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_workouts()), 0)
        mock_calendar.delete_workout_event.assert_called_once_with("ge_2")

    def test_metrics_wipe(self):
        # Seed metrics
        test_db.save_metric_cache(
            date="2026-05-31", rhr=48, hrv=82, sleep_score=90, stress=15
        )
        test_db.save_baseline(
            date="2026-05-31", rhr_mean=50.0, rhr_std=1.5, hrv_mean=78.0,
            hrv_std=4.0, sleep_mean=82.0, sleep_std=3.0
        )
        test_db.save_completed_activity(
            activity_id="act_1", date="2026-05-31", start_time="10:00",
            activity_name="Run", activity_type="running", duration_sec=1800,
            distance_km=5.0, elevation_gain_m=50, avg_hr=150, max_hr=170,
            rpe=5, tss=30.0
        )

        self.assertEqual(len(test_db.get_metrics_cache()), 1)
        self.assertIsNotNone(test_db.get_baseline("2026-05-31"))
        self.assertEqual(len(test_db.get_completed_activities()), 1)

        # 1. Decline
        exit_code, stdout, stderr = self.run_cli(['metrics', 'wipe'], input_value='n')
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_metrics_cache()), 1)
        self.assertEqual(len(test_db.get_completed_activities()), 1)

        # 2. Confirm
        exit_code, stdout, stderr = self.run_cli(['metrics', 'wipe'], input_value='y')
        self.assertEqual(exit_code, 0)
        self.assertIn(
            "All metrics, baselines, and completed activities wiped successfully.", stdout
        )
        self.assertEqual(len(test_db.get_metrics_cache()), 0)
        self.assertIsNone(test_db.get_baseline("2026-05-31"))
        self.assertEqual(len(test_db.get_completed_activities()), 0)

        # Seed again
        test_db.save_metric_cache(
            date="2026-05-31", rhr=48, hrv=82, sleep_score=90, stress=15
        )
        self.assertEqual(len(test_db.get_metrics_cache()), 1)

        # 3. Wipe with -y
        exit_code, stdout, stderr = self.run_cli(['metrics', 'wipe', '-y'])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_metrics_cache()), 0)

if __name__ == "__main__":
    unittest.main()
