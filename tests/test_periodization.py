import unittest
from unittest.mock import patch, MagicMock
import os
import sqlite3
from datetime import datetime, timezone

# Define test database path
TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_periodization.db")

# Override db singleton inside trainmate before anything else imports it
from trainmate.db import Database
import trainmate.db

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db

import trainmate.coach
trainmate.coach.db = test_db

from trainmate.coach import coach_engine

class TestPeriodization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.db = test_db

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

    def test_database_crud_and_cascade(self):
        # Add an objective
        obj_id = test_db.add_objective(
            title="Berlin Marathon",
            target_date="2026-09-27",
            sport_type="running",
            priority=1
        )
        
        # Verify get_macrocycle returns None initially
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertIsNone(macro)
        
        # Save a macrocycle
        mesos = [
            {"name": "Base Building", "start_date": "2026-06-01", "end_date": "2026-06-28", "focus": "Aerobic threshold"},
            {"name": "Specific prep", "start_date": "2026-06-29", "end_date": "2026-07-26", "focus": "Intensity increase"}
        ]
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="aerobic focus plan",
            goals_hash="hashgoals123",
            lifeevents_hash="hashconstraints456",
            mesocycles=mesos
        )
        self.assertIsNotNone(macro_id)
        
        # Fetch it
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertIsNotNone(macro)
        self.assertEqual(macro['strategy'], "aerobic focus plan")
        self.assertEqual(macro['goals_hash'], "hashgoals123")
        self.assertEqual(macro['lifeevents_hash'], "hashconstraints456")
        
        # Fetch mesocycles
        mesocycles = test_db.get_mesocycles_for_macrocycle(macro['id'])
        self.assertEqual(len(mesocycles), 2)
        self.assertEqual(mesocycles[0]['name'], "Base Building")
        self.assertEqual(mesocycles[0]['start_date'], "2026-06-01")
        self.assertEqual(mesocycles[1]['name'], "Specific prep")
        self.assertEqual(mesocycles[1]['start_date'], "2026-06-29")
        
        # Verify cascade delete when deleting objective
        test_db.delete_objective(obj_id)
        
        # Verify macrocycle and mesocycles are deleted
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertIsNone(macro)
        
        # Try getting mesocycles for the deleted macrocycle ID
        mesocycles = test_db.get_mesocycles_for_macrocycle(macro_id)
        self.assertEqual(len(mesocycles), 0)

    def test_hashing_helpers(self):
        # Empty objectives & constraints hashes
        hash1 = coach_engine._get_goals_hash([])
        hash2 = coach_engine._get_lifeevents_hash([])
        hash3 = coach_engine._get_config_hash()
        self.assertIsNotNone(hash1)
        self.assertIsNotNone(hash2)
        self.assertIsNotNone(hash3)
        
        # Add objective
        obj = {
            'id': 1,
            'title': 'Test Goal',
            'target_date': '2026-10-15',
            'sport_type': 'running',
            'description': 'sub 3hr',
            'priority': 1,
            'status': 'active'
        }
        hash1_with_obj = coach_engine._get_goals_hash([obj])
        self.assertNotEqual(hash1, hash1_with_obj)
        
        # Changing description changes hash
        obj['description'] = 'sub 2:50'
        hash1_with_obj_modified = coach_engine._get_goals_hash([obj])
        self.assertNotEqual(hash1_with_obj, hash1_with_obj_modified)
        
        # Constraints hash changes
        c = {
            'id': 1,
            'title': 'Spain Trip',
            'start_date': '2026-07-01',
            'end_date': '2026-07-08',
            'event_type': 'vacation',
            'impact_description': 'easy'
        }
        hash2_with_c = coach_engine._get_lifeevents_hash([c])
        self.assertNotEqual(hash2, hash2_with_c)

    @patch('trainmate.coach.openrouter_client')
    def test_replan_logic_and_caching(self, mock_client):
        # Seed objective
        obj_id = test_db.add_objective(
            title="Berlin Marathon",
            target_date="2026-09-27",
            sport_type="running",
            priority=1
        )
        
        # Mock OpenRouter returns
        mock_macro_response = {
            "strategy": "Simulated overall strategy",
            "mesocycles": [
                {"name": "Base Building", "start_date": "2026-06-01", "end_date": "2026-06-28", "focus": "Endurance"},
                {"name": "Peak & Taper", "start_date": "2026-06-29", "end_date": "2026-07-05", "focus": "Taper"}
            ]
        }
        mock_workouts_response = {
            "reasoning": "Microcycle generated reasoning",
            "athlete_learnings": "Simulated learnings",
            "workouts": [
                {"date": "2026-06-01", "sport_type": "running", "title": "Base Run", "description": "45 mins zone 2"}
            ]
        }
        
        # Side effect to handle multiple OpenRouter calls:
        # First call is macrocycle strategy generation
        # Second call is microcycles workouts generation
        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        
        # 1. First replan: Should generate macrocycle + microcycles
        reason, workouts = coach_engine.replan(force=False)
        self.assertEqual(reason, "Microcycle generated reasoning")
        self.assertEqual(len(workouts), 1)
        self.assertEqual(workouts[0]['title'], "Base Run")
        self.assertEqual(mock_client.complete.call_count, 2)
        
        # Verify macrocycle stored in DB
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertIsNotNone(macro)
        self.assertEqual(macro['strategy'], "Simulated overall strategy")
        
        # Verify mesocycles stored in DB
        mesos = test_db.get_mesocycles_for_macrocycle(macro['id'])
        self.assertEqual(len(mesos), 2)
        
        # 2. Second replan: No changes, force=False -> Should REUSE macrocycle, only generate microcycles
        mock_client.complete.reset_mock()
        # Side effect only needs to handle microcycle generation now, as macro is reused
        mock_client.complete.side_effect = [mock_workouts_response]
        
        reason, workouts = coach_engine.replan(force=False)
        self.assertEqual(mock_client.complete.call_count, 1) # Only microcycles generated!
        
        # 3. Third replan: No changes, force=True -> Should RE-GENERATE macrocycle + microcycles
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        
        reason, workouts = coach_engine.replan(force=True)
        self.assertEqual(mock_client.complete.call_count, 2) # Both generated!
        
        # 4. Fourth replan: Change goal, force=False -> Should RE-GENERATE macrocycle + microcycles
        test_db.add_objective(
            title="Mini Triathlon",
            target_date="2026-08-01",
            sport_type="road_biking",
            priority=2
        )
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        
        reason, workouts = coach_engine.replan(force=False)
        self.assertEqual(mock_client.complete.call_count, 2) # Both generated because goals changed!

    def test_system_prompt_inserts_periodization(self):
        # Add goal and macrocycle
        obj_id = test_db.add_objective(
            title="Zurich Marathon",
            target_date="2026-10-15",
            sport_type="running",
            priority=1
        )
        mesos = [
            {"name": "Base Building", "start_date": "2026-06-01", "end_date": "2026-06-28", "focus": "Zone 2 runs"},
            {"name": "Peak & Taper", "start_date": "2026-06-29", "end_date": "2026-07-05", "focus": "Tapering"}
        ]
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Run long and slow",
            goals_hash="hash1",
            lifeevents_hash="hash2",
            mesocycles=mesos
        )
        
        # Get active objectives
        objs = test_db.get_objectives(status='active')
        prompt = coach_engine._get_coach_system_prompt(objs, [])
        
        self.assertIn("Run long and slow", prompt)
        self.assertIn("Base Building (2026-06-01 to 2026-06-28): Zone 2 runs", prompt)
        self.assertIn("Peak & Taper (2026-06-29 to 2026-07-05): Tapering", prompt)
        self.assertIn("COACH MEMORY & ACTIVE PERIODIZATION STRATEGY:", prompt)
        self.assertIn("START OF SPORTS SCIENCE GUIDELINES", prompt)
        self.assertIn("END OF SPORTS SCIENCE GUIDELINES", prompt)

    @patch('trainmate.coach.openrouter_client')
    def test_replan_provides_previous_strategy_context_to_llm(self, mock_client):
        # 1. Seed initial objective and macrocycle strategy
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
                "focus": "Aerobic conditioning"
            }
        ]
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="old_goals_hash",
            lifeevents_hash="old_constraints_hash",
            mesocycles=mesos
        )

        # Mock LLM response for macrocycle and workouts
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
        mock_workouts_response = {
            "reasoning": "Reasoning",
            "workouts": []
        }
        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]

        # 2. Add a life event to trigger replanning (reused will be False)
        test_db.add_lifeevent(
            title="Business Trip",
            start_date="2026-06-10",
            end_date="2026-06-12",
            event_type="business_trip",
            impact_description="limited training time"
        )

        # 3. Trigger replanning
        coach_engine.replan(force=False)

        # 4. Verify that OpenRouter was called with the previous strategy in context
        self.assertEqual(mock_client.complete.call_count, 2)
        system_prompt_arg = mock_client.complete.call_args_list[0][0][0]

        # Assert context is included in system prompt
        self.assertIn("PREVIOUS PERIODIZATION STRATEGY (FOR CONTEXT):", system_prompt_arg)
        self.assertIn("Keep heart rate low", system_prompt_arg)
        self.assertIn(
            "Base Building (2026-06-01 to 2026-06-28): Aerobic conditioning",
            system_prompt_arg
        )
        self.assertIn(
            "For context, the PREVIOUS periodization strategy that was in place",
            system_prompt_arg
        )

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
                    "equipment": ["treadmill"]
                },
                "Wednesday": 0.0
            }
        }
        with patch.dict(trainmate.coach.config.data, {"user_profile": test_profile}):
            # Get prompt
            prompt = coach_engine._get_coach_system_prompt([], [])
            self.assertIn("Jane Doe", prompt)
            self.assertIn("Birth Year: 1990", prompt)
            self.assertIn("Tendency for runner's knee.", prompt)
            self.assertIn("Enjoys morning runs.", prompt)
            self.assertIn("Garmin Watch, Yoga Mat", prompt)
            self.assertIn(
                "Monday: 1.5 hours | Max sessions: 2 | Certainty: 95% (Equipment: treadmill)",
                prompt
            )
            self.assertIn("Wednesday: 0.0 hours", prompt)

    @patch('trainmate.coach.openrouter_client')
    def test_generate_plan_and_workouts_separately(self, mock_client):
        # Seed active goal
        obj_id = test_db.add_objective(
            title="Zurich Marathon",
            target_date="2026-10-15",
            sport_type="running",
            priority=1
        )

        mock_macro_response = {
            "strategy": "Separate strategy philosophy",
            "mesocycles": [
                {
                    "name": "Base Phase",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-28",
                    "focus": "Base"
                }
            ]
        }
        mock_workouts_response = {
            "reasoning": "Separate workout reasoning",
            "workouts": [
                {
                    "date": "2026-06-01",
                    "sport_type": "running",
                    "title": "Base Run",
                    "description": "30 mins"
                }
            ]
        }

        # Verify that generating workouts before plan raises ValueError
        with self.assertRaises(ValueError):
            coach_engine.generate_workouts()

        # Generate plan strategy
        mock_client.complete.return_value = mock_macro_response
        strategy, mesos = coach_engine.generate_periodization_plan(force=False)
        self.assertEqual(strategy, "Separate strategy philosophy")
        self.assertEqual(len(mesos), 1)
        mock_client.complete.assert_called_once()

        # Generate workouts
        mock_client.complete.reset_mock()
        mock_client.complete.return_value = mock_workouts_response
        reason, workouts = coach_engine.generate_workouts()
        self.assertEqual(reason, "Separate workout reasoning")
        self.assertEqual(len(workouts), 1)
        self.assertEqual(workouts[0]['title'], "Base Run")
        mock_client.complete.assert_called_once()

    @patch('trainmate.coach.config')
    def test_load_science_guidelines(self, mock_config):
        import tempfile
        import shutil

        # Create temporary directories for testing
        temp_app_dir = tempfile.mkdtemp()
        temp_user_dir = tempfile.mkdtemp()

        try:
            # Set up mock paths
            mock_config.app_science_dir = temp_app_dir
            mock_config.science_dir = temp_user_dir

            # Create sample files in app_science_dir
            app_file = os.path.join(temp_app_dir, "app_science.txt")
            with open(app_file, "w", encoding="utf-8") as f:
                f.write("App guideline text")

            # Create sample files in science_dir
            user_file = os.path.join(temp_user_dir, "user_science.txt")
            with open(user_file, "w", encoding="utf-8") as f:
                f.write("User guideline text")

            # Call _load_science_guidelines
            guidelines = coach_engine._load_science_guidelines()

            # Assert both contents are present
            self.assertIn("=== Guidelines from app_science.txt ===", guidelines)
            self.assertIn("App guideline text", guidelines)
            self.assertIn("=== Guidelines from user_science.txt ===", guidelines)
            self.assertIn("User guideline text", guidelines)

        finally:
            shutil.rmtree(temp_app_dir)
            shutil.rmtree(temp_user_dir)

    def test_config_hash_logic(self):
        # 1. Get initial config hash
        initial_hash = coach_engine._get_config_hash()
        self.assertIsNotNone(initial_hash)
        
        # 2. Mock a change to config.user_profile
        original_profile = dict(trainmate.coach.config.data['user_profile'])
        try:
            trainmate.coach.config.data['user_profile']['weekly_target_hours'] = 20.0
            new_hash = coach_engine._get_config_hash()
            self.assertNotEqual(initial_hash, new_hash)
        finally:
            trainmate.coach.config.data['user_profile'] = original_profile

    def test_db_config_hash_operations(self):
        obj_id = test_db.add_objective(
            title="Zurich Marathon",
            target_date="2026-10-15",
            sport_type="running",
            priority=1
        )
        # Save macrocycle with a specific config_hash
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Long runs",
            goals_hash="ghash",
            lifeevents_hash="lehash",
            config_hash="confhash123",
            mesocycles=[]
        )
        
        # Retrieve macrocycle
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro['config_hash'], "confhash123")
        
        # Update config_hash
        test_db.update_macrocycle_config_hash(macro_id, "newconfhash456")
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro['config_hash'], "newconfhash456")

if __name__ == '__main__':
    unittest.main()
