import io
import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_dashless.db")

from trainmate.coach.proposals import RevisionProposal
from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


def tearDownModule():
    """Only TestDashlessEndToEnd needs a database, but the handle above creates the
    file at import time, so the module has to remove it however the run ends."""
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except OSError:
            pass


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

    def test_the_new_verbs_do_not_disturb_the_prefixes_around_them(self):
        # `workout accommodate` is spelled that way and not `reschedule` precisely so
        # `w res` keeps resolving to `restore` (DESIGN_constraint_reschedule.md §4/§13);
        # pinned here so a later verb change cannot quietly re-break it.
        self.assertEqual(self._xlate("w res 3"), ["workout", "restore", "3"])
        self.assertEqual(self._xlate("w ac"), ["workout", "accommodate"])
        self.assertEqual(self._xlate("w a"), ["workout", "adapt"])

    def test_surviving_aliases_normalize_to_canonical(self):
        # Kept because they are not prefixes ('ctx', 'lm') or are ambiguous ones ('s').
        self.assertEqual(self._xlate("ctx lm"), ["context", "list-metrics"])
        self.assertEqual(self._xlate("s"), ["status"])
        self.assertEqual(self._xlate("data sm"), ["data", "show-metrics"])

    def test_ambiguous_prefix_is_rejected(self):
        # 'rm' gets no tiebreaker alias on purpose: no one-letter destructive command.
        for line, expected in [("c", "constraint, context"), ("workout ad", "adapt, add"),
                               ("workout r", "restore, rm, rollback"),
                               ("benchmark r", "record, rm")]:
            with patch("sys.stderr", io.StringIO()) as err:
                with self.assertRaises(SystemExit) as ctx:
                    self._xlate(line)
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn(expected, err.getvalue())

    def test_progress_keywords_still_bind_past_the_new_positional(self):
        # `progress` gained an nargs="*" SPORT positional
        # (DESIGN_intensity_distribution.md §9.6). `_build_keyword_spec` skips
        # positionals, so bare tokens must still reach the keyword translator: a sport
        # name falls through as a positional, an option keyword still binds.
        self.assertEqual(self._xlate("progress weeks 4"), ["progress", "--weeks", "4"])
        self.assertEqual(self._xlate("progress cycling"), ["progress", "cycling"])
        self.assertEqual(
            self._xlate("progress running cycling weeks 4"),
            ["progress", "running", "cycling", "--weeks", "4"],
        )
        self.assertEqual(self._xlate("progress blocks"), ["progress", "--blocks"])

    def test_option_keywords_win_over_command_prefixes(self):
        # 'help' is a real top-level command; 'helpall' is a root flag whose exact
        # keyword must still bind as the flag rather than prefix-matching a command.
        self.assertEqual(self._xlate("helpall"), ["--helpall"])


class TestCommandTreeInvariants(unittest.TestCase):
    """Guard rails on the shape of the command tree itself (DESIGN_cli_noargs.md §d).

    Dashless options and command prefixes share one namespace at any level that has
    both — today only the root, but these walk the whole tree, so a group-level
    option added later is checked the same way. Each failure names the offending
    level and what to do about it, because none of them is obvious from the symptom:
    a shadowed spelling simply resolves to the wrong thing, silently."""

    @classmethod
    def setUpClass(cls):
        cls.parser, _ = trainmate_cli.build_parser()

    def _levels(self):
        """Yield (path, sub-parsers action, parser) for every level that has commands."""
        from trainmate.cli.argparse_ext import _subparsers_action

        def walk(parser, path):
            action = _subparsers_action(parser)
            if action is None:
                return
            yield path or "<root>", action, parser
            for name in action.canonical_names:
                yield from walk(action.choices[name], f"{path} {name}".strip())

        yield from walk(self.parser, "")

    @staticmethod
    def _as_command(action, token):
        """What `token` would mean as a command here: a canonical name, or None."""
        if token in action.canonical_names:
            return token
        if token in action.alias_of:
            return action.alias_of[token]
        matches = [n for n in action.canonical_names if n.startswith(token)]
        return matches[0] if len(matches) == 1 else None

    def test_no_option_keyword_collides_with_a_command(self):
        # A dashless keyword that also names or abbreviates a command at the same level
        # makes one of the two unreachable: exact commands are resolved before keywords,
        # keywords before prefixes. Rename the option, or give it no dashless spelling.
        from trainmate.cli.argparse_ext import _build_keyword_spec

        for path, action, parser in self._levels():
            for keyword in _build_keyword_spec(parser):
                clash = self._as_command(action, keyword)
                self.assertIsNone(
                    clash,
                    f"option '{keyword}' at level '{path}' also resolves to command "
                    f"'{clash}' — one of the two becomes unreachable dashlessly",
                )

    def test_no_alias_shadows_another_commands_prefix(self):
        # The `plan d` trap: an exact alias beats prefix matching, so aliasing a
        # command to a letter that unambiguously abbreviates a *different* command
        # silently steals it. Pick another alias, or drop it.
        for path, action, _ in self._levels():
            for alias, target in action.alias_of.items():
                matches = [n for n in action.canonical_names if n.startswith(alias)]
                if len(matches) == 1 and matches[0] != target:
                    self.fail(
                        f"alias '{alias}' -> '{target}' at level '{path}' shadows "
                        f"'{matches[0]}', which it would otherwise abbreviate"
                    )

    def test_no_alias_is_redundant_with_prefix_matching(self):
        # An alias earns its place only by being unreachable as a prefix (`ctx`, `lm`)
        # or by breaking a tie (`s`, `a`). Anything else is a shortcut prefix matching
        # already provides — delete it rather than maintain it in two places.
        for path, action, _ in self._levels():
            for alias, target in action.alias_of.items():
                matches = [n for n in action.canonical_names if n.startswith(alias)]
                self.assertNotEqual(
                    matches, [target],
                    f"alias '{alias}' -> '{target}' at level '{path}' is redundant: "
                    f"'{alias}' already resolves there by prefix",
                )


class TestDashlessEndToEnd(unittest.TestCase):
    """End-to-end: dashless argv flows through real main() and reaches the handlers."""

    @classmethod
    def setUpClass(cls):
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

    def setUp(self):
        clear_all_tables(test_db)

    def run_cli(self, args, input_value="n"):
        return run_cli(args, input_value)

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_workout_adapt_message_and_no_pull(self, mock_coach, mock_garmin):
        mock_coach.workout_adapt.return_value = RevisionProposal(
            reason="ok", workouts=[], new_constraints=[],
            range_start="2026-06-01", range_end="2026-06-30",
        )
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

    def test_goal_edit_comma_list_and_collision(self):
        # `goal add` takes its mandatory fields positionally, so the option keywords
        # this exercises live on `goal edit`, where every field is optional.
        gid = test_db.add_objective(
            title="Marathon", target_date="2026-10-15", sport_type="running",
            description="", status="active",
        )
        # Comma list expands to two sports.
        exit_code, stdout, _ = self.run_cli([
            "goal", "edit", str(gid), "sport", "running,strength_training",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("updated successfully", stdout)
        self.assertEqual(
            test_db.get_objective(gid)["sport_type"], "running,strength_training"
        )

        # A value colliding with a keyword name ("date") is still bound as the value.
        exit_code, _, _ = self.run_cli(["goal", "edit", str(gid), "title", "date"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(test_db.get_objective(gid)["title"], "date")
