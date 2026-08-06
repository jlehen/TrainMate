"""One error boundary, and the condition that lets a cancellation stay ordinary.

Handlers used to wrap themselves in `except Exception` and print the message. That
discarded the traceback, reported success (exit 0) for a failed command, and hid real
bugs behind a one-line summary — the `next_goal` NameError read as an error *string*
for as long as it existed, and two tests in this suite were passing against a mock
whose shape no longer matched, because the net swallowed the AttributeError.
"""
import ast
import os
import unittest
from unittest.mock import patch

from tests.helpers import bind_test_db, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_error_boundary.db")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

test_db = bind_test_db(TEST_DB_PATH)

from trainmate.prompt import PromptCancelled


class TestFailuresReachTheBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global test_db
        test_db = bind_test_db(TEST_DB_PATH)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_a_failing_command_exits_non_zero(self, mock_coach, _garmin):
        mock_coach.plan_generate.side_effect = RuntimeError("the model refused")

        exit_code, stdout, _ = run_cli(["plan", "generate"])

        self.assertEqual(exit_code, 1, "a failed command must not report success")
        self.assertIn("the model refused", stdout)
        self.assertIn("--debug", stdout)

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_debug_reraises_so_the_traceback_survives(self, mock_coach, _garmin):
        mock_coach.plan_generate.side_effect = RuntimeError("the model refused")

        import io
        import trainmate_cli
        with self.assertRaises(RuntimeError):
            with (
                patch("builtins.input", return_value="n"),
                patch("sys.stdout", io.StringIO()),
            ):
                trainmate_cli.main(["--debug", "plan", "generate"])

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_a_cancelled_prompt_is_not_a_failure(self, mock_coach, _garmin):
        mock_coach.plan_generate.side_effect = PromptCancelled()

        exit_code, stdout, _ = run_cli(["plan", "generate"])

        self.assertEqual(exit_code, 130)
        self.assertIn("Cancelled.", stdout)

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_a_successful_command_still_exits_zero(self, mock_coach, _garmin):
        mock_coach.plan_generate.return_value = {
            "strategy": "s", "mesocycles": [], "reused": True, "goal": None,
        }
        exit_code, _, _ = run_cli(["plan", "generate"])
        self.assertEqual(exit_code, 0)


class TestNoNetEnclosesAPrompt(unittest.TestCase):
    """The condition that lets PromptCancelled be an ordinary Exception again.

    If a broad `except Exception` ever encloses a prompt call, a deliberate abort
    becomes indistinguishable from a command error — which is why the class used to
    inherit BaseException.
    """

    def test_no_except_exception_block_wraps_a_prompt_call(self):
        offenders = []
        for root, _, files in os.walk(os.path.join(REPO, "trainmate")):
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                tree = ast.parse(open(path).read())
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Try):
                        continue
                    if not any(isinstance(h.type, ast.Name) and h.type.id == "Exception"
                               for h in node.handlers):
                        continue
                    for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                        if (isinstance(sub, ast.Call)
                                and isinstance(sub.func, ast.Attribute)
                                and sub.func.attr in ("confirm", "choose", "ask_text")):
                            offenders.append(
                                f"{os.path.relpath(path, REPO)}:{node.lineno}"
                            )
        self.assertEqual(offenders, [], f"prompt calls inside a broad net: {offenders}")


if __name__ == "__main__":
    unittest.main()
