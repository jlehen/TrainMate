import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_dashless.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate_cli.db = test_db


class TestDashlessOptionTranslator(unittest.TestCase):
    """Network-appliance-style dashless options (`workout adapt message "..." no-pull`)
    are rewritten back into `--flag` form by translate_dashless_argv before argparse,
    so both syntaxes share one parser definition."""

    def _parser(self):
        # A miniature tree mirroring the real shapes the translator must handle:
        # a sub-command level, a single-value option, a boolean flag, an
        # nargs="+" (comma-list) option, and an nargs="?" optional-value option.
        import argparse
        p = argparse.ArgumentParser()
        subs = p.add_subparsers(dest="command")
        w = subs.add_parser("workout", aliases=["w"])
        wsubs = w.add_subparsers(dest="subcommand")
        adapt = wsubs.add_parser("adapt", aliases=["a"])
        adapt.add_argument("-m", "--message", dest="message")
        adapt.add_argument("--no-pull", action="store_true", dest="no_pull")
        add = wsubs.add_parser("add")
        add.add_argument("--sport", nargs="+")
        add.add_argument("--title")
        lst = wsubs.add_parser("list")
        lst.add_argument("--mesocycle", type=int, nargs="?", const=-1, dest="meso_id")
        lst.add_argument("--type", dest="sport_type")
        return p

    def _xlate(self, tokens):
        import trainmate_cli
        return trainmate_cli.translate_dashless_argv(self._parser(), tokens)

    def test_flag_and_value_keywords(self):
        # `w a message "..." no-pull` → recurse aliases, expand value + boolean.
        self.assertEqual(
            self._xlate(["w", "a", "message", "feeling sluggish lately", "no-pull"]),
            ["w", "a", "--message", "feeling sluggish lately", "--no-pull"],
        )

    def test_multivalue_comma_split(self):
        self.assertEqual(
            self._xlate(["workout", "add", "sport", "running,hiking", "title", "Big Day"]),
            ["workout", "add", "--sport", "running", "hiking", "--title", "Big Day"],
        )

    def test_optional_value_peek(self):
        # Bare `mesocycle` keeps its const default (no value consumed)...
        self.assertEqual(
            self._xlate(["workout", "list", "mesocycle"]),
            ["workout", "list", "--mesocycle"],
        )
        # ...takes a following plain value...
        self.assertEqual(
            self._xlate(["workout", "list", "mesocycle", "5"]),
            ["workout", "list", "--mesocycle", "5"],
        )
        # ...but does NOT swallow a following keyword.
        self.assertEqual(
            self._xlate(["workout", "list", "mesocycle", "type", "running"]),
            ["workout", "list", "--mesocycle", "--type", "running"],
        )

    def test_single_value_binds_even_when_value_collides_with_keyword(self):
        # A value equal to a keyword name (a workout literally titled "message") is
        # still bound as the value, because single-value options consume unconditionally.
        self.assertEqual(
            self._xlate(["workout", "adapt", "message", "message"]),
            ["workout", "adapt", "--message", "message"],
        )

    def test_dashed_syntax_passes_through_untouched(self):
        # Classic --flag invocations are byte-identical after translation.
        tokens = ["workout", "adapt", "--message", "hi", "--no-pull"]
        self.assertEqual(self._xlate(tokens), tokens)
        self.assertEqual(
            self._xlate(["workout", "add", "--sport", "running", "hiking", "--title", "X"]),
            ["workout", "add", "--sport", "running", "hiking", "--title", "X"],
        )

    def test_canonical_option_uses_longest_spelling(self):
        # `message` resolves to the long form even though `-m` is also registered.
        self.assertEqual(
            self._xlate(["workout", "adapt", "m", "hello"]),
            ["workout", "adapt", "--message", "hello"],
        )


class TestCommandPrefixResolution(unittest.TestCase):
    """Any unambiguous prefix of a command resolves to it, and every accepted
    spelling — prefix or surviving alias — reaches argparse as the canonical name
    (DESIGN_cli_noargs.md §d). Runs against the real command tree, since the point
    is which real commands collide."""

    @classmethod
    def setUpClass(cls):
        cls.parser, _ = trainmate_cli.build_parser()

    def _xlate(self, line):
        return trainmate_cli.translate_dashless_argv(self.parser, line.split())

    def test_prefix_resolves_at_every_level(self):
        self.assertEqual(self._xlate("st"), ["status"])
        self.assertEqual(self._xlate("wo li"), ["workout", "list"])
        self.assertEqual(self._xlate("constr ed 3"), ["constraint", "edit", "3"])

    def test_retired_aliases_still_work_as_prefixes(self):
        # The shortcuts removed when prefixes landed keep resolving unchanged.
        for line, expected in [
            ("pl g", ["plan", "generate"]),
            ("w l", ["workout", "list"]),
            ("cons l", ["constraint", "list"]),
            ("bench rec", ["benchmark", "record"]),
            ("l s 4", ["learnings", "show", "4"]),
        ]:
            self.assertEqual(self._xlate(line), expected, line)

    def test_surviving_aliases_normalize_to_canonical(self):
        # Kept because they are not prefixes ('ctx', 'lm') or are ambiguous ones ('s').
        self.assertEqual(self._xlate("ctx lm"), ["context", "list-metrics"])
        self.assertEqual(self._xlate("s"), ["status"])
        self.assertEqual(self._xlate("data sm"), ["data", "show-metrics"])

    def test_ambiguous_prefix_is_rejected(self):
        for line, expected in [("c", "constraint, context"), ("workout ad", "adapt, add")]:
            with patch("sys.stderr", io.StringIO()) as err:
                with self.assertRaises(SystemExit) as ctx:
                    self._xlate(line)
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn(expected, err.getvalue())

    def test_option_keywords_win_over_command_prefixes(self):
        # 'help' is a real top-level command; 'helpall' is a root flag whose exact
        # keyword must still bind as the flag rather than prefix-matching a command.
        self.assertEqual(self._xlate("helpall"), ["--helpall"])


class TestDashlessEndToEnd(unittest.TestCase):
    """End-to-end: dashless argv flows through real main() and reaches the handlers."""

    @classmethod
    def setUpClass(cls):
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate_cli.db = test_db

    def setUp(self):
        clear_all_tables(test_db)

    def run_cli(self, args, input_value="n"):
        return run_cli(args, input_value)

    @patch("trainmate_cli.garmin")
    @patch("trainmate_cli.coach_service")
    def test_workout_adapt_message_and_no_pull(self, mock_coach, mock_garmin):
        mock_coach.workout_adapt.return_value = ("ok", [], [])
        exit_code, _, _ = self.run_cli(
            ["w", "a", "auto", "message", "feeling sluggish lately", "no-pull"]
        )
        self.assertEqual(exit_code, 0)
        # The athlete message threaded through verbatim...
        self.assertEqual(
            mock_coach.workout_adapt.call_args.kwargs.get("message"),
            "feeling sluggish lately",
        )
        # ...and `no-pull` reached ensure_recent_data → no Garmin pull.
        mock_garmin.ensure_data.assert_not_called()

    def test_goal_add_comma_list_and_collision(self):
        # Comma list expands to two sports.
        exit_code, stdout, _ = self.run_cli([
            "goal", "add", "title", "Marathon", "date", "2026-10-15",
            "sport", "running,strength_training", "priority", "1",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("added successfully", stdout)
        g = test_db.get_objectives()[0]
        self.assertEqual(g["sport_type"], "running,strength_training")
        self.assertEqual(g["title"], "Marathon")

        # A value colliding with a keyword name ("date") is still bound as the value.
        exit_code, _, _ = self.run_cli([
            "goal", "add", "title", "date", "date", "2026-11-01", "sport", "running",
        ])
        self.assertEqual(exit_code, 0)
        titled_date = [o for o in test_db.get_objectives() if o["title"] == "date"]
        self.assertEqual(len(titled_date), 1)
        self.assertEqual(titled_date[0]["target_date"], "2026-11-01")
