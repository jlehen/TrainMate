import unittest
from unittest.mock import patch
import os
import io
import sys

# Define test database path
TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_feedback.db")

# Override db singleton inside trainmate before anything else imports it
from trainmate.db import Database
import trainmate.db

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db

import trainmate.coach
trainmate.coach.db = test_db

from trainmate.coach import coach_service
import trainmate_cli
trainmate_cli.db = test_db


class TestFeedback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.db = test_db
        trainmate_cli.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        # Clear tables
        with test_db._get_connection() as conn:
            conn.execute("DELETE FROM objectives")
            conn.execute("DELETE FROM lifeevents")
            conn.execute("DELETE FROM workouts")
            conn.execute("DELETE FROM coach_memory")
            conn.execute("DELETE FROM macrocycles")
            conn.execute("DELETE FROM mesocycles")
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

    def test_database_feedback_crud(self):
        # 1. Seed objective
        obj_id = test_db.add_objective(
            title="Zurich Marathon",
            target_date="2026-10-15",
            sport_type="running",
            priority=1
        )
        
        # 2. Save macrocycle & mesocycles
        mesos = [
            {
                "name": "Base Building",
                "start_date": "2026-06-01",
                "end_date": "2026-06-28",
                "focus": "Zone 2 runs"
            }
        ]
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="ghash",
            lifeevents_hash="lehash",
            mesocycles=mesos
        )
        
        # 3. Update feedback
        test_db.update_macrocycle_feedback(macro_id, "Strategy was too easy.")
        
        fetched_mesos = test_db.get_mesocycles_for_macrocycle(macro_id)
        self.assertEqual(len(fetched_mesos), 1)
        meso_id = fetched_mesos[0]['id']
        test_db.update_mesocycle_feedback(meso_id, "Increase duration of long runs.")
        
        # 4. Retrieve and verify
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertIsNotNone(macro)
        self.assertEqual(macro['feedback'], "Strategy was too easy.")
        
        meso = test_db.get_mesocycle(meso_id)
        self.assertIsNotNone(meso)
        self.assertEqual(meso['feedback'], "Increase duration of long runs.")

    def test_system_prompt_inserts_athlete_feedback(self):
        # 1. Seed objective and plan with feedback
        obj_id = test_db.add_objective(
            title="Zurich Marathon",
            target_date="2026-10-15",
            sport_type="running",
            priority=1
        )
        mesos = [
            {
                "name": "Base Building",
                "start_date": "2026-06-01",
                "end_date": "2026-06-28",
                "focus": "Zone 2 runs"
            }
        ]
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="ghash",
            lifeevents_hash="lehash",
            mesocycles=mesos
        )
        test_db.update_macrocycle_feedback(macro_id, "Strategy was too easy.")
        
        # Get system prompt via active strategy text and meso text
        objs = test_db.get_objectives(status='active')
        prompt = coach_service._get_coach_system_prompt(objs, [])
        
        # Verify prompt details
        self.assertIn("Keep heart rate low", prompt)
        self.assertIn("Base Building (2026-06-01 to 2026-06-28): Zone 2 runs", prompt)

    @patch('trainmate.coach.openrouter_client')
    def test_replan_injects_feedback_into_prompt(self, mock_client):
        # 1. Seed objective, plan and feedback
        obj_id = test_db.add_objective(
            title="Zurich Marathon",
            target_date="2026-10-15",
            sport_type="running",
            priority=1
        )
        mesos = [
            {
                "name": "Base Building",
                "start_date": "2026-06-01",
                "end_date": "2026-06-28",
                "focus": "Zone 2 runs"
            }
        ]
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="ghash",
            lifeevents_hash="lehash",
            mesocycles=mesos
        )
        test_db.update_macrocycle_feedback(macro_id, "Strategy was too easy.")
        
        fetched_mesos = test_db.get_mesocycles_for_macrocycle(macro_id)
        test_db.update_mesocycle_feedback(fetched_mesos[0]['id'], "Long runs are too short.")

        # Mock responses
        mock_macro_response = {
            "strategy": "New strategy building on previous",
            "mesocycles": [
                {
                    "name": "Specific Prep",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-28",
                    "focus": "Faster runs"
                }
            ]
        }
        mock_client.complete.return_value = mock_macro_response

        # 2. Trigger strategy replanning via generate_periodization_plan (forced)
        coach_service.generate_periodization_plan(force=True)

        # 3. Verify that OpenRouter was called with the athlete feedback text in prompt
        called_args = mock_client.complete.call_args[0]
        system_prompt_arg = called_args[0]

        self.assertIn("ATHLETE FEEDBACK ON THE PREVIOUS PLAN:", system_prompt_arg)
        self.assertIn("Strategy was too easy.", system_prompt_arg)
        self.assertIn("Long runs are too short.", system_prompt_arg)

    def test_cli_feedback_command(self):
        # 1. Feed command when no active goal
        exit_code, stdout, stderr = self.run_cli(['plan', 'feedback', '--macro', 'too hard'])
        self.assertEqual(exit_code, 1)
        self.assertIn("No active goals found", stdout)

        # 2. Add objective
        obj_id = test_db.add_objective(
            title="Zurich Marathon",
            target_date="2026-10-15",
            sport_type="running",
            priority=1
        )
        
        # Feed command when no plan exists
        exit_code, stdout, stderr = self.run_cli(['plan', 'feedback', '--macro', 'too hard'])
        self.assertEqual(exit_code, 1)
        self.assertIn("No active periodization plan exists", stdout)

        # 3. Save macrocycle & mesocycles
        mesos = [
            {
                "name": "Base Building",
                "start_date": "2026-06-01",
                "end_date": "2026-06-28",
                "focus": "Zone 2 runs"
            }
        ]
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="ghash",
            lifeevents_hash="lehash",
            mesocycles=mesos
        )

        # 4. Save macrocycle feedback via CLI
        exit_code, stdout, stderr = self.run_cli(['plan', 'f', '--macro', 'overall too easy'])
        self.assertEqual(exit_code, 0)
        self.assertIn("Feedback successfully saved for Macrocycle ID", stdout)
        self.assertIn("regenerate the periodization plan to apply this feedback", stdout)

        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro['feedback'], 'overall too easy')

        # 5. Save mesocycle feedback via CLI
        fetched_mesos = test_db.get_mesocycles_for_macrocycle(macro_id)
        meso_id = fetched_mesos[0]['id']
        exit_code, stdout, stderr = self.run_cli(
            ['plan', 'feedback', '--meso', str(meso_id), 'more speed']
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Feedback successfully saved for Mesocycle ID", stdout)
        self.assertIn("regenerate the periodization plan to apply this feedback", stdout)

        meso = test_db.get_mesocycle(meso_id)
        self.assertEqual(meso['feedback'], 'more speed')

        # 6. Verify outputs in plan show
        exit_code, stdout, stderr = self.run_cli(['plan', 'show'])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Objective [ID: {obj_id}]", stdout)
        self.assertIn(f"[ID: {meso_id}]", stdout)
        self.assertIn("Macrocycle Feedback:\n  overall too easy", stdout)
        self.assertIn("Mesocycle Feedback:\n    more speed", stdout)


if __name__ == '__main__':
    unittest.main()
