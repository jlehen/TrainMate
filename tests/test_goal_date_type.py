"""Goal date_type: event vs. training horizon (ARCHITECTURE.md §15 "Goal dates")."""
import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables, pin_clock, rebind_test_db, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_goal_date_type.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service

GOAL_DATE = "2026-09-27"

MOCK_MACRO_RESPONSE = {
    "strategy": "Simulated overall strategy",
    "mesocycles": [
        {"name": "Base Building", "start_date": "2026-06-01",
         "end_date": "2026-06-28", "focus": "Endurance"},
        {"name": "Build", "start_date": "2026-06-29",
         "end_date": "2026-09-27", "focus": "Specific work"},
    ],
}
MOCK_WORKOUTS_RESPONSE = {
    "reasoning": "Microcycle generated reasoning",
    "workouts": [{
        "date": "2026-06-01", "sport_type": "running",
        "title": "Base Run", "description": "45 mins zone 2",
    }],
}


class TestGoalDateType(unittest.TestCase):
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

    def _add_goal(self, **overrides):
        kwargs = dict(
            title="Berlin Marathon", target_date=GOAL_DATE,
            sport_type="running", priority=1,
        )
        kwargs.update(overrides)
        return test_db.add_objective(**kwargs)

    def test_defaults_to_event_and_stores_horizon(self):
        event_id = self._add_goal()
        self.assertEqual(test_db.get_objective(event_id)["date_type"], "event")

        horizon_id = self._add_goal(title="Climb PB", date_type="horizon")
        self.assertEqual(test_db.get_objective(horizon_id)["date_type"], "horizon")

    def test_goals_hash_stable_for_event_goals(self):
        # An event goal must hash the same as a pre-field goal row, so shipping the
        # field does not flag every existing plan stale (§4); a horizon goal must not.
        legacy = {
            "id": 1, "title": "Berlin Marathon", "target_date": GOAL_DATE,
            "sport_type": "running", "description": "", "priority": 1,
            "status": "active",
        }
        event = dict(legacy, date_type="event")
        horizon = dict(legacy, date_type="horizon")
        self.assertEqual(
            coach_service._get_goals_hash([legacy]),
            coach_service._get_goals_hash([event]),
        )
        self.assertNotEqual(
            coach_service._get_goals_hash([event]),
            coach_service._get_goals_hash([horizon]),
        )

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_horizon_goal_branches_the_planning_and_generation_prompts(
        self, mock_client, mock_calendar
    ):
        pin_clock(self, "2026-06-01")
        self._add_goal(title="Climb PB", date_type="horizon")

        mock_client.complete.side_effect = [MOCK_MACRO_RESPONSE, MOCK_WORKOUTS_RESPONSE]
        coach_service.replan(force=False)
        self.assertEqual(mock_client.complete.call_count, 2)

        plan_prompt = mock_client.complete.call_args_list[0][0][0]
        plan_user = mock_client.complete.call_args_list[0][0][1]
        self.assertIn("TRAINING HORIZON, not a scheduled event", plan_prompt)
        self.assertIn("Do NOT plan a peak, taper, or race-day realization", plan_prompt)
        self.assertNotIn("Race/Event", plan_prompt)
        self.assertIn(f"around {GOAL_DATE} (a training horizon", plan_prompt)
        self.assertIn(f"training toward a horizon of {GOAL_DATE}", plan_user)

        gen_prompt = mock_client.complete.call_args_list[1][0][0]
        self.assertIn("a boundary week near it is an ordinary boundary", gen_prompt)
        self.assertNotIn("competes with the effort it is meant to", gen_prompt)

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_event_goal_keeps_the_event_prompts(self, mock_client, mock_calendar):
        pin_clock(self, "2026-06-01")
        self._add_goal()

        mock_client.complete.side_effect = [MOCK_MACRO_RESPONSE, MOCK_WORKOUTS_RESPONSE]
        coach_service.replan(force=False)
        self.assertEqual(mock_client.complete.call_count, 2)

        plan_prompt = mock_client.complete.call_args_list[0][0][0]
        plan_user = mock_client.complete.call_args_list[0][0][1]
        self.assertIn("Peak & Taper, Race/Event", plan_prompt)
        self.assertNotIn("TRAINING HORIZON", plan_prompt)
        self.assertIn(f"'Berlin Marathon' on {GOAL_DATE}", plan_user)

        gen_prompt = mock_client.complete.call_args_list[1][0][0]
        self.assertIn("competes with the effort it is meant to", gen_prompt)
        self.assertNotIn("a boundary week near it is an ordinary boundary", gen_prompt)

    def test_event_date_for_macrocycle_is_none_for_horizon_goals(self):
        pin_clock(self, "2026-06-01")
        for date_type, expected_date in (("event", True), ("horizon", False)):
            obj_id = self._add_goal(title=f"Goal {date_type}", date_type=date_type)
            macro_id = test_db.save_macrocycle(
                objective_id=obj_id, strategy="s",
                goals_hash="h1", constraints_hash="h2",
                mesocycles=[{"name": "Base", "start_date": "2026-06-01",
                             "end_date": "2026-09-27", "focus": "f"}],
            )
            resolved = coach_service._event_date_for_macrocycle(macro_id)
            if expected_date:
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved.isoformat(), GOAL_DATE)
            else:
                self.assertIsNone(resolved)

    def test_cli_add_edit_and_list(self):
        code, out, _ = run_cli([
            "goal", "add", "Climb PB", GOAL_DATE, "cycling",
            "--date-type", "horizon",
        ])
        self.assertEqual(code, 0)
        self.assertIn("by ~", out)
        self.assertIn("(horizon)", out)
        goal = test_db.get_objectives()[0]
        self.assertEqual(goal["date_type"], "horizon")

        code, out, _ = run_cli([
            "goal", "edit", str(goal["id"]), "--date-type", "event",
        ])
        self.assertEqual(code, 0)
        self.assertNotIn("(horizon)", out)
        self.assertEqual(test_db.get_objective(goal["id"])["date_type"], "event")


if __name__ == "__main__":
    unittest.main()
