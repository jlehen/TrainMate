import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db


# A plan window needs its goal in the future, so a fixed date expires the tests the
# day it passes (same rot 2a7cd71 fixed in test_constraints.py).
GOAL_DATE = (datetime.now(timezone.utc).date() + timedelta(days=71)).isoformat()

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_feedback.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)
rebind_test_db(test_db)

from trainmate.coach import coach_service


class TestFeedback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)
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

    @patch("trainmate.coach.engine.openrouter_client")
    def test_replan_injects_feedback_into_prompt(self, mock_client):
        obj_id = test_db.add_objective(
            title="Zurich Marathon",
            target_date=GOAL_DATE,
            sport_type="running",
            priority=1,
        )
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="ghash",
            constraints_hash="lehash",
            mesocycles=[{
                "name": "Base Building",
                "start_date": "2026-06-01",
                "end_date": "2026-06-28",
                "focus": "Zone 2 runs",
            }],
        )
        test_db.update_macrocycle_feedback(macro_id, "Strategy was too easy.")
        fetched_mesos = test_db.get_mesocycles_for_macrocycle(macro_id)
        test_db.update_mesocycle_feedback(fetched_mesos[0]["id"], "Long runs are too short.")

        mock_client.complete.return_value = {
            "strategy": "New strategy building on previous",
            "mesocycles": [{
                "name": "Specific Prep",
                "start_date": "2026-06-01",
                "end_date": "2026-06-28",
                "focus": "Faster runs",
            }],
        }

        coach_service.plan_generate(force=True)

        system_prompt = mock_client.complete.call_args[0][0]
        self.assertIn("### ATHLETE FEEDBACK ON THE PREVIOUS PLAN", system_prompt)
        self.assertIn("Strategy was too easy.", system_prompt)
        self.assertIn("Long runs are too short.", system_prompt)

    def test_cli_feedback_command(self):
        exit_code, stdout, stderr = self.run_cli(["plan", "feedback", "--macro", "too hard"])
        self.assertEqual(exit_code, 1)
        self.assertIn("No active goals found", stdout)

        obj_id = test_db.add_objective(
            title="Zurich Marathon",
            target_date=GOAL_DATE,
            sport_type="running",
            priority=1,
        )

        exit_code, stdout, stderr = self.run_cli(["plan", "feedback", "--macro", "too hard"])
        self.assertEqual(exit_code, 1)
        self.assertIn("No active periodization plan exists", stdout)

        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="ghash",
            constraints_hash="lehash",
            mesocycles=[{
                "name": "Base Building",
                "start_date": "2026-06-01",
                "end_date": "2026-06-28",
                "focus": "Zone 2 runs",
            }],
        )

        exit_code, stdout, stderr = self.run_cli(["plan", "f", "--macro", "overall too easy"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Feedback successfully saved for Macrocycle ID", stdout)
        self.assertIn("regenerate the periodization plan to apply this feedback", stdout)
        self.assertEqual(
            test_db.get_macrocycle_for_objective(obj_id)["feedback"], "overall too easy"
        )

        fetched_mesos = test_db.get_mesocycles_for_macrocycle(macro_id)
        meso_id = fetched_mesos[0]["id"]
        exit_code, stdout, stderr = self.run_cli([
            "plan", "feedback", "--meso", str(meso_id), "more speed"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Feedback successfully saved for Mesocycle ID", stdout)
        self.assertIn("regenerate the periodization plan to apply this feedback", stdout)
        self.assertEqual(test_db.get_mesocycle(meso_id)["feedback"], "more speed")

        exit_code, stdout, stderr = self.run_cli(["plan", "show"])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"[Goal ID: {obj_id}]", stdout)
        self.assertIn(f"[Mesocycle ID: {meso_id}]", stdout)
        self.assertIn("Macrocycle Feedback:\n  overall too easy", stdout)
        # Mesocycle feedback is indented one step deeper than its own block indent.
        self.assertRegex(stdout, r"Mesocycle Feedback:\n +more speed")

    @patch("trainmate.cli.plans._edit_text_in_editor")
    def test_cli_feedback_edit(self, mock_editor):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running", priority=1,
        )
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id, strategy="s", goals_hash="g", constraints_hash="l",
            mesocycles=[{"name": "Base", "start_date": "2026-06-01",
                         "end_date": "2026-06-28", "focus": "Z2"}],
        )
        test_db.update_macrocycle_feedback(macro_id, "old note")

        # --edit opens the editor seeded with the current feedback and saves the result.
        mock_editor.return_value = "edited note"
        exit_code, stdout, stderr = self.run_cli(["plan", "feedback", "--macro", "--edit"])
        self.assertEqual(exit_code, 0)
        mock_editor.assert_called_once_with("old note")
        self.assertEqual(
            test_db.get_macrocycle_for_objective(obj_id)["feedback"], "edited note"
        )

        # An aborted edit (editor returns None) leaves the feedback untouched.
        mock_editor.reset_mock()
        mock_editor.return_value = None
        exit_code, stdout, stderr = self.run_cli(["plan", "feedback", "--macro", "--edit"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            test_db.get_macrocycle_for_objective(obj_id)["feedback"], "edited note"
        )


if __name__ == "__main__":
    unittest.main()
