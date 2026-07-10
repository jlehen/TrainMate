import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_misc.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate_cli.db = test_db


class TestCliMisc(unittest.TestCase):
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
        self.assertIn("constraint", stdout)
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

    def test_help_command_shows_full_command_tree(self):
        exit_code, stdout, stderr = self.run_cli(["help"])
        self.assertEqual(exit_code, 0)
        # top-level commands
        self.assertIn("goal", stdout)
        self.assertIn("workout", stdout)
        self.assertIn("data", stdout)
        # and their sub-commands, which plain --help doesn't show recursively
        self.assertIn("add", stdout)
        self.assertIn("pull", stdout)
        self.assertIn("push", stdout)

    def test_invalid_command(self):
        exit_code, stdout, stderr = self.run_cli(["invalidcmd"])
        self.assertEqual(exit_code, 2)
        self.assertIn("invalid choice: 'invalidcmd'", stderr)

    def test_wipe_commands_confirmation_flow(self):
        """goal/constraint/plan/data wipe share one confirmation flow: `n` cancels,
        `y` confirms, `-y` skips the prompt. (`workout wipe` is covered separately
        for its calendar side-effect.)"""
        def seed_goal():
            test_db.add_objective(
                title="Wipe Target", target_date="2026-10-15", sport_type="running"
            )
        def count_goal(_):
            return len(test_db.get_objectives())

        def seed_constraint():
            test_db.add_constraint(
                title="Wipe Constraint", start_date="2026-07-01",
                end_date="2026-07-02",
            )
        def count_constraint(_):
            return len(test_db.get_constraints())

        def seed_plan():
            obj_id = test_db.add_objective(
                title="Plan Wipe Obj", target_date="2026-10-15", sport_type="running"
            )
            test_db.save_macrocycle(
                objective_id=obj_id, strategy="Base", goals_hash="ghash",
                constraints_hash="lhash",
                mesocycles=[{
                    "name": "Meso1", "start_date": "2026-06-01",
                    "end_date": "2026-06-28", "focus": "Aerobic",
                }],
            )
            return obj_id
        def count_plan(obj_id):
            return 1 if test_db.get_macrocycle_for_objective(obj_id) else 0

        def seed_data():
            test_db.save_metric_cache(
                date="2026-05-31", rhr=48, hrv=82, sleep_score=90, stress=15
            )
        def count_data(_):
            return len(test_db.get_metrics_cache())

        cases = [
            (["goal", "wipe"], seed_goal, count_goal,
             "All training objectives wiped successfully."),
            (["constraint", "wipe"], seed_constraint, count_constraint,
             "All constraints wiped successfully."),
            (["plan", "wipe"], seed_plan, count_plan,
             "All periodization plans wiped successfully."),
            (["data", "wipe"], seed_data, count_data,
             "Wiped Garmin metrics, baselines, and activities and "
             "ingested daily-context signals."),
        ]

        for args, seed, count, message in cases:
            with self.subTest(command=args[0]):
                # `n` cancels the wipe.
                token = seed()
                self.assertEqual(count(token), 1)
                exit_code, stdout, _ = self.run_cli(args, input_value="n")
                self.assertEqual(exit_code, 0)
                self.assertIn("Wipe cancelled.", stdout)
                self.assertEqual(count(token), 1)

                # `y` confirms.
                exit_code, stdout, _ = self.run_cli(args, input_value="y")
                self.assertEqual(exit_code, 0)
                self.assertIn(message, stdout)
                self.assertEqual(count(token), 0)

                # `-y` skips the prompt entirely.
                token = seed()
                self.assertEqual(count(token), 1)
                exit_code, _, _ = self.run_cli(args + ["-y"])
                self.assertEqual(exit_code, 0)
                self.assertEqual(count(token), 0)

    @patch("trainmate_cli.garmin")
    @patch("trainmate_cli.coach_service")
    def test_no_pull_behavior_across_commands(self, mock_coach, mock_garmin):
        # 1. workout compare without --no-pull
        exit_code, stdout, stderr = self.run_cli(["workout", "compare", "--days", "3"])
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_called_once()

        # 2. workout compare with --no-pull
        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "compare", "--days", "3", "--no-pull"]
        )
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_not_called()

        # 3. status with --no-pull
        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["status", "--no-pull"])
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_not_called()

        # 4. plan generate with --no-pull
        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["plan", "generate", "--no-pull"])
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_not_called()

        # 5. data bootstrap with --no-pull
        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli(
            ["data", "bootstrap", "--from", "2026-06-01", "--no-pull"]
        )
        self.assertEqual(exit_code, 0)
        mock_coach.data_bootstrap.assert_called_once()
        self.assertTrue(mock_coach.data_bootstrap.call_args[1].get("no_pull"))

    def test_llm_model_override(self):
        from trainmate.openrouter import openrouter_client
        original_model = openrouter_client.model
        try:
            exit_code, _, _ = self.run_cli([
                "--llm-model", "google/gemini-2.5-pro",
                "goal", "list"
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(openrouter_client.model, "google/gemini-2.5-pro")
        finally:
            openrouter_client.model = original_model
