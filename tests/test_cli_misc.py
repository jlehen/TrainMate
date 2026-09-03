import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db
from trainmate.cli.argparse_ext import _subparser_choices
from trainmate.util import strip_ansi
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_trainmate_cli_misc.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class TestCliMisc(unittest.TestCase):
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
        self.assertIn("swap", stdout)          # everyday command is listed
        self.assertNotIn("push", stdout)       # advanced command is hidden from -h

        exit_code, stdout, stderr = self.run_cli(["data", "--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("pull", stdout)

    def test_help_wraps_to_the_client_width(self):
        # Every help surface — usage, description, option column — has to fit the
        # width, including the chat client's. The sub-command slot is the one argparse
        # can't break, which is why it reads `<command>` rather than `{a,b,c,…}`.
        parser, _ = trainmate_cli.build_parser()
        for width in (80, 48):
            with patch.dict(os.environ, {"COLUMNS": str(width),
                                         "TRAINMATE_WRAP_WIDTH": str(width)}):
                for argv in ([], ["workout"], ["workout", "generate"]):
                    sub = parser
                    for token in argv:
                        sub = _subparser_choices(sub)[token]
                    text = strip_ansi(sub.format_help())
                    over = [line for line in text.splitlines() if len(line) > width]
                    self.assertEqual(over, [], f"{argv} at {width} cols")
        self.assertIn("<command>", strip_ansi(parser.format_help()))

    def test_help_description_keeps_its_paragraphs(self):
        # Wrapping re-flows prose, but a description that deliberately breaks into
        # paragraphs (benchmark record's Garmin warning) keeps those breaks.
        exit_code, stdout, stderr = self.run_cli(["benchmark", "record", "--help"])
        self.assertEqual(exit_code, 0)
        body = strip_ansi(stdout).split("\n\npositional arguments:")[0]
        self.assertIn("dated logbook", body)
        self.assertIn("\n\nIMPORTANT:", body)
        self.assertIn("\n\nFor --e1rm", body)

    def test_missing_argument_prints_help_then_the_missing_line(self):
        # DESIGN_cli_noargs.md §a: on a terminal the error answers itself with the
        # command's own help — the positional it names is only meaningful alongside
        # its description — and puts the missing line last, where the eye lands.
        with patch.dict(os.environ, {"TRAINMATE_FRONTEND": ""}):
            exit_code, stdout, stderr = self.run_cli(["constraint", "add"])
        self.assertEqual(exit_code, 2)
        self.assertIn("positional arguments:", stderr)
        self.assertIn("The directive, stated short", stderr)
        self.assertIn(
            "the following arguments are required: title",
            stderr.strip().splitlines()[-1],
        )

    def test_missing_argument_stays_one_line_on_the_json_frontend(self):
        # ...but a screenful of help is a screenful of chat, so the bot gets the
        # line plus a pointer to the help it can ask for (§a).
        with patch.dict(os.environ, {"TRAINMATE_FRONTEND": "json"}):
            exit_code, stdout, stderr = self.run_cli(["constraint", "add"])
        self.assertEqual(exit_code, 2)
        self.assertNotIn("positional arguments:", stderr)
        self.assertIn("the following arguments are required: title", stderr)
        self.assertIn("constraint add -h", stderr)

    def test_advanced_command_hidden_but_runnable(self):
        # `workout push` is hidden from listings yet still parses and describes itself.
        exit_code, stdout, stderr = self.run_cli(["workout", "push", "--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Google Calendar", stdout)

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
        # advanced maintenance commands stay out of the everyday tree...
        self.assertNotIn("push", stdout)
        self.assertNotIn("backfill-tss", stdout)

    def test_help_all_reveals_advanced_commands(self):
        exit_code, stdout, stderr = self.run_cli(["help", "--all"])
        self.assertEqual(exit_code, 0)
        # ...and only surface under `help --all`, tagged as maintenance.
        self.assertIn("push", stdout)
        self.assertIn("backfill-tss", stdout)
        self.assertIn("maintenance", stdout)

    def test_restore_and_rollback_help_point_at_each_other(self):
        # DESIGN_plan_rollback.md §9: the two easily-confused undos each name the other.
        exit_code, stdout, stderr = self.run_cli(["workout", "restore", "--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("rollback", stdout)
        exit_code, stdout, stderr = self.run_cli(["workout", "rollback", "--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("restore", stdout)

    def test_helpall_flag_reveals_advanced_at_root(self):
        # The top-level `--helpall` flag is the discoverable equivalent of `help --all`.
        exit_code, stdout, stderr = self.run_cli(["--helpall"])
        self.assertEqual(exit_code, 0)
        self.assertIn("push", stdout)
        self.assertIn("maintenance", stdout)

    def test_helpall_flag_exists_on_subcommands(self):
        # `--helpall` works at every level: `plan --helpall` reveals the hidden plan
        # maintenance commands scoped to that sub-tree.
        exit_code, stdout, stderr = self.run_cli(["plan", "--helpall"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Remove/delete", stdout)  # the hidden `plan rm`...
        self.assertIn("maintenance", stdout)    # ...tagged as maintenance
        # On a leaf with no sub-commands it degrades to that command's own -h.
        exit_code, stdout, stderr = self.run_cli(["plan", "rm", "--helpall"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal ID", stdout)

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
             "All goals wiped successfully."),
            (["constraint", "wipe"], seed_constraint, count_constraint,
             "All constraints wiped successfully."),
            (["plan", "wipe"], seed_plan, count_plan,
             "All periodization plans wiped successfully."),
            (["data", "wipe"], seed_data, count_data,
             "Wiped Garmin metrics, baselines, and activities and "
             "ingested daily signals."),
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

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_no_pull_behavior_across_commands(self, mock_coach, mock_garmin):
        # 1. workout compare without --no-pull
        exit_code, stdout, stderr = self.run_cli(["workout", "compare", "-d", "3d"])
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_called_once()

        # 2. workout compare with --no-pull
        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "compare", "-d", "3d", "--no-pull"]
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
            ["data", "bootstrap", "-d", "2026-06-01..", "--no-pull"]
        )
        self.assertEqual(exit_code, 0)
        mock_coach.data_bootstrap.assert_called_once()
        self.assertTrue(mock_coach.data_bootstrap.call_args[1].get("no_pull"))

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.timeline.build_timeline_payload")
    def test_progress_renders_end_to_end_from_the_payload(self, mock_build, mock_garmin):
        # Not an alias test — `pr` is prefix resolution, covered in test_cli_dashless.
        # What this pins is the only run_cli path through the progress renderer, which
        # test_cli_progress.py deliberately does not exercise (no DB, no dispatch).
        mock_build.return_value = {
            "today": "2026-07-18",
            "plan_end": "2026-08-31",
            "days": [
                {
                    "date": "2026-07-18",
                    "ctl": 10.0,
                    "atl": 10.0,
                    "tsb": 0.0,
                    "load": 0.0,
                    "source": "actual"
                }
            ],
            "weeks": [],
            "objectives": [],
            "warnings": [],
            "meso_bands": [],
        }
        exit_code, stdout, stderr = self.run_cli(["pr", "--no-pull"])
        self.assertEqual(exit_code, 0)
        self.assertIn("CTL 10", stdout)
