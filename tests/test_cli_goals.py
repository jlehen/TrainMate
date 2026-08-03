import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_goals.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate_cli.db = test_db


class TestCliGoals(unittest.TestCase):
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

    def test_goal_commands(self):
        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== TRAINING OBJECTIVES / GOALS ===", stdout)
        self.assertNotIn("ID:", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "Zurich Marathon", "2026-10-15", "running",
            "--desc", "Target sub 3:30",
            "--priority", "1",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("[ACTIVE] ID: 1 | Zurich Marathon (running) on 2026-10-15 (Priority: 1)", stdout)
        self.assertIn("Description:", stdout)
        self.assertIn("Goal added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "Morning Yoga Flow", "2026-10-20", "yoga",
            "--desc", "Daily mindfulness and flexibility",
            "--priority", "2",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Morning Yoga Flow", stdout)
        self.assertIn("Goal added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "Hybrid Strength Endurance", "2026-11-30", "cycling", "strength_training",
            "--desc", "Aging well routine",
            "--priority", "3",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Hybrid Strength Endurance", stdout)
        self.assertIn("Goal added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Zurich Marathon", stdout)
        self.assertIn("running", stdout)
        self.assertIn("2026-10-15", stdout)
        self.assertIn("Priority: 1", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertIn("Hybrid Strength Endurance", stdout)
        self.assertIn("cycling,strength_training", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "rm", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal with ID 1 removed successfully", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Zurich Marathon", stdout)

    def test_constraint_commands(self):
        exit_code, stdout, stderr = self.run_cli(["constraint", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ATHLETE CONSTRAINTS ===", stdout)

        # Quick capture: positional title, flag-free (no prompts), advisory by default.
        exit_code, stdout, stderr = self.run_cli([
            "constraint", "add", "Ibiza Vacation",
            "--start", "2026-07-01", "--end", "2026-07-08",
            "--desc", "50% intensity",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("ID: 1 | Ibiza Vacation: 2026-07-01 to 2026-07-08 | advisory", stdout)
        self.assertIn("Constraint added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli(["cons", "list", "--all"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("advisory", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertNotIn("Details:", stdout)

        exit_code, stdout, stderr = self.run_cli(["cons", "list", "--all", "-v"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Details:\n    50% intensity", stdout)

        exit_code, stdout, stderr = self.run_cli(["constraint", "show", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertIn("Details:\n    50% intensity", stdout)

        exit_code, stdout, stderr = self.run_cli(["constraint", "show", "999"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Constraint with ID 999 not found.", stdout)

        exit_code, stdout, stderr = self.run_cli(["constraint", "rm", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Constraint [1] removed.", stdout)

        exit_code, stdout, stderr = self.run_cli(["cons", "list", "--all"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Ibiza Vacation", stdout)

    def test_goal_edit_command(self):
        self.run_cli([
            "goal", "add",
            "Berlin Marathon", "2026-09-27", "running",
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
        self.assertIn(
            f"[COMPLETED] ID: {g_id} | Berlin Marathon Elite "
            "(running,strength_training) on 2026-09-28 (Priority: 1)",
            stdout,
        )
        self.assertIn("Goal updated successfully", stdout)

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

    def test_constraint_edit_command(self):
        self.run_cli([
            "constraint", "add", "Summer Vacation",
            "--start", "2026-08-01", "--end", "2026-08-15",
            "--desc", "No workouts",
        ])

        constraints = test_db.get_constraints()
        c_id = constraints[0]["id"]

        exit_code, stdout, stderr = self.run_cli([
            "constraint", "edit", str(c_id),
            "--title", "Summer Vacation Adapted",
            "--start", "2026-08-02", "--end", "2026-08-16",
            "--desc", "Light running only", "--rest",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn(
            f"ID: {c_id} | Summer Vacation Adapted: 2026-08-02 to 2026-08-16 | no training",
            stdout,
        )
        self.assertIn("Constraint updated successfully", stdout)

        edited = test_db.get_constraint(c_id)
        self.assertEqual(edited["title"], "Summer Vacation Adapted")
        self.assertEqual(edited["start_date"], "2026-08-02")
        self.assertEqual(edited["end_date"], "2026-08-16")
        self.assertEqual(edited["rest"], 1)
        self.assertEqual(edited["description"], "Light running only")

        exit_code, stdout, stderr = self.run_cli(
            ["constraint", "edit", "999", "--title", "Fail"]
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("Constraint with ID 999 not found", stdout)

        exit_code, stdout, stderr = self.run_cli(["constraint", "edit", str(c_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn("No fields to update", stdout)
