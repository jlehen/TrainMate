import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db, save_workout

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_goals.db")


def _days_out(n: int) -> str:
    """Fixtures ride on today: a plan window needs its goal in the future, so a
    hardcoded date expires the test the day it passes."""
    return (datetime.now(timezone.utc).date() + timedelta(days=n)).isoformat()

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class TestCliGoals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

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
        self.assertIn("=== GOALS ===", stdout)
        self.assertNotIn("ID:", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "Zurich Marathon", "2026-10-15", "running",
            "--desc", "Target sub 3:30",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("[UPCOMING] ID: 1 | Zurich Marathon (running) on 2026-10-15", stdout)
        self.assertIn("Description:", stdout)
        self.assertIn("Goal added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "Morning Yoga Flow", "2026-10-20", "yoga",
            "--desc", "Daily mindfulness and flexibility",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Morning Yoga Flow", stdout)
        self.assertIn("Goal added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "Hybrid Strength Endurance", "2026-11-30", "cycling", "strength_training",
            "--desc", "Aging well routine",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Hybrid Strength Endurance", stdout)
        self.assertIn("Goal added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Zurich Marathon", stdout)
        self.assertIn("running", stdout)
        self.assertIn("2026-10-15", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertIn("Hybrid Strength Endurance", stdout)
        self.assertIn("cycling,strength_training", stdout)

        # Unconfirmed `goal rm` shows the cascade inventory and keeps the goal (§14).
        exit_code, stdout, stderr = self.run_cli(["goal", "rm", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("also deletes", stdout)
        self.assertIn("--status archived", stdout)
        self.assertIn("Removal cancelled", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "rm", "1", "-y"])
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
        self.assertIn(
            "ID: 1 | Ibiza Vacation: 2026-07-01 Wed to 2026-07-08 Wed | advisory", stdout
        )
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
        ])

        goals = test_db.get_objectives()
        self.assertEqual(len(goals), 1)
        g_id = goals[0]["id"]

        exit_code, stdout, stderr = self.run_cli([
            "goal", "edit", str(g_id),
            "--title", "Berlin Marathon Elite",
            "--target-date", "2026-09-28",
            "--sport", "running", "strength_training",
            "--desc", "Sub 3:10 elite goal",
            "--status", "archived",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn(
            f"[ARCHIVED] ID: {g_id} | Berlin Marathon Elite "
            "(running,strength_training) on 2026-09-28",
            stdout,
        )
        self.assertIn("Goal updated successfully", stdout)

        edited = test_db.get_objective(g_id)
        self.assertEqual(edited["title"], "Berlin Marathon Elite")
        self.assertEqual(edited["target_date"], "2026-09-28")
        self.assertEqual(edited["sport_type"], "running,strength_training")
        self.assertEqual(edited["description"], "Sub 3:10 elite goal")
        self.assertEqual(edited["status"], "archived")

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
            f"ID: {c_id} | Summer Vacation Adapted: 2026-08-02 Sun to 2026-08-16 Sun "
            f"| no training",
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


class TestCliGoalArchival(unittest.TestCase):
    """Its own class so its fixtures don't consume the row IDs `TestCliGoals` asserts on
    — `clear_all_tables` empties the tables but leaves the AUTOINCREMENT counters."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

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

    def _goal_with_session(self):
        """A goal with a one-block plan and one session inside it."""
        oid = test_db.add_objective("Spring Race", _days_out(60), "running", "", 1)
        mid = test_db.save_macrocycle(
            oid, "strategy", "gh", "ch",
            [{"name": "Base", "start_date": _days_out(0), "end_date": _days_out(30),
              "focus": "aerobic"}],
        )
        save_workout(test_db, _days_out(3), "running", "Long run", "x",
                             macrocycle_id=mid)
        return oid, mid

    @patch("trainmate.runtime.calendar_syncer")
    def test_calling_a_goal_off_stands_its_sessions_down_and_back_up(self, _cal):
        """`goal edit --status archived` is the reversible way to drop a goal: the
        sessions go with it, and reinstating offers them back
        (DESIGN_backward_evaluation.md §14)."""
        oid, mid = self._goal_with_session()

        exit_code, stdout, _stderr = self.run_cli(
            ["goal", "edit", str(oid), "--status", "archived"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Stood down 1 upcoming session", stdout)
        self.assertEqual(test_db.get_workouts(start_date=_days_out(0)), [])
        # Archiving keeps the plan — that is the difference from `goal rm`.
        self.assertIsNotNone(test_db.get_macrocycle(mid))

        exit_code, stdout, _stderr = self.run_cli(
            ["goal", "edit", str(oid), "--status", "active"], input_value="y"
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Restored 1 session", stdout)
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=_days_out(0))],
            ["Long run"],
        )

    @patch("trainmate.runtime.calendar_syncer")
    def test_declining_the_restore_leaves_the_sessions_archived(self, _cal):
        """Reinstating asks first: a goal picked back up months later should not silently
        re-push sessions from a plan that no longer suits the athlete (§14)."""
        oid, _mid = self._goal_with_session()
        self.run_cli(["goal", "edit", str(oid), "--status", "archived"])

        exit_code, stdout, _stderr = self.run_cli(
            ["goal", "edit", str(oid), "--status", "active"], input_value="n"
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Sessions left archived", stdout)
        self.assertEqual(test_db.get_workouts(start_date=_days_out(0)), [])

    @patch("trainmate.runtime.calendar_syncer")
    def test_goal_rm_inventories_what_the_cascade_will_take(self, _cal):
        """`goal rm` names the plan history it destroys and points at the reversible
        alternative before asking (§14)."""
        oid, mid = self._goal_with_session()
        test_db.add_plan_feedback(mid, "too much volume in week 3")

        exit_code, stdout, _stderr = self.run_cli(["goal", "rm", str(oid)])
        self.assertEqual(exit_code, 0)
        self.assertIn("1 periodization plan version(s)", stdout)
        self.assertIn("1 mesocycle block(s)", stdout)
        self.assertIn("1 plan feedback note(s)", stdout)
        self.assertIn("leaves 1 upcoming session(s)", stdout)
        self.assertIn("--status archived", stdout)
        self.assertIsNotNone(test_db.get_objective(oid))
