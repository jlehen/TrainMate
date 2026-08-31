import ast
import io
import os
import unittest
from unittest.mock import patch
from trainmate.util import (
    RESET, wrap_text, visible_len, pad_visible, color_load_ratio, format_labeled_text,
    render_table, truncate_visible, yellow, Progress,
)


class TestUtils(unittest.TestCase):
    def test_wrap_text_utility(self):
        text = (
            "This is a very long sentence that will definitely wrap "
            "when formatted at a smaller width limit such as thirty characters."
        )
        wrapped = wrap_text(text, width=30)
        self.assertEqual(
            wrapped,
            "This is a very long sentence\n"
            "that will definitely wrap when\n"
            "formatted at a smaller width\n"
            "limit such as thirty\n"
            "characters."
        )

        list_text = (
            "  - This is an indented list item that will also wrap "
            "properly at a width of forty characters."
        )
        wrapped_list = wrap_text(list_text, width=40)
        self.assertEqual(
            wrapped_list,
            "  - This is an indented list item that\n"
            "    will also wrap properly at a width\n"
            "    of forty characters."
        )

    def test_visible_len(self):
        self.assertEqual(visible_len("hello"), 5)
        # Test with ANSI escape codes
        self.assertEqual(visible_len("\033[31mhello\033[0m"), 5)
        self.assertEqual(visible_len("\x1b[1m\x1b[32mtest\x1b[0m"), 4)

    def test_pad_visible(self):
        self.assertEqual(pad_visible("hi", 5), "hi   ")
        self.assertEqual(pad_visible("\033[31mhi\033[0m", 5), "\033[31mhi\033[0m   ")
        self.assertEqual(pad_visible("hi", 5, align_left=False), "   hi")

    def test_color_load_ratio(self):
        # Force colour on: off a TTY colorize() is a no-op, which would make every
        # band render bare and the assertions below pass without testing anything.
        with patch("trainmate.util.is_color_enabled", return_value=True):
            # Only the overload end is coloured. A LOW ratio is phase-dependent
            # (taper, deload, intensity block), so it must render bare — see
            # training_load.md §3/§4.
            self.assertEqual("0.70", color_load_ratio(0.70))
            self.assertEqual("1.10", color_load_ratio(1.10))
            # Bands are half-open (> 1.3 yellow, > 1.5 red), so both edges belong to
            # the calmer band and no value is claimed twice.
            self.assertEqual("1.30", color_load_ratio(1.30))
            self.assertIn("\033[33m", color_load_ratio(1.40))
            self.assertIn("\033[33m", color_load_ratio(1.50))
            self.assertIn("\033[31m", color_load_ratio(1.60))

    def test_format_labeled_text(self):
        from trainmate.util import yellow
        label = "  Reason: "
        text = "This is a daily adaptation decision because the HRV values are drop."
        formatted = format_labeled_text(label, text, width=40)
        self.assertEqual(
            formatted,
            "  Reason: This is a daily adaptation\n"
            "          decision because the HRV\n"
            "          values are drop."
        )

        # Test with color function
        with patch("trainmate.util.is_color_enabled", return_value=True):
            formatted_colored = format_labeled_text(label, text, width=40, color_fn=yellow)
        self.assertIn("\033[33mThis is a daily adaptation", formatted_colored)
        self.assertIn("values are drop.\033[0m", formatted_colored)


class TestProgress(unittest.TestCase):
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    def test_bar_draws_on_a_terminal_and_erases_itself(self):
        out = self._Tty()
        with patch("sys.stdout", out):
            with Progress(2) as bar:
                bar.step()
                bar.step()
        written = out.getvalue()
        self.assertIn("1/2", written)
        self.assertIn("2/2", written)
        # Nothing survives on the line: the summary above the bar is the only trace left.
        self.assertTrue(written.endswith("\r\033[K"))

    def test_silent_when_not_a_terminal_or_empty(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            with Progress(3) as bar:
                bar.step()
        self.assertEqual(out.getvalue(), "")

        tty = self._Tty()
        with patch("sys.stdout", tty):
            with Progress(0) as bar:
                bar.step()
        self.assertEqual(tty.getvalue(), "")


class TestWrapWidth(unittest.TestCase):
    """`default_wrap_width()` and the TRAINMATE_WRAP_WIDTH override the Telegram
    front-end sets so output fits a chat bubble."""

    def setUp(self):
        self._saved = os.environ.pop("TRAINMATE_WRAP_WIDTH", None)
        from trainmate import util
        self.util = util

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("TRAINMATE_WRAP_WIDTH", None)
        else:
            os.environ["TRAINMATE_WRAP_WIDTH"] = self._saved

    def test_default_is_80(self):
        self.assertEqual(self.util.default_wrap_width(), 80)

    def test_env_override(self):
        os.environ["TRAINMATE_WRAP_WIDTH"] = "40"
        self.assertEqual(self.util.default_wrap_width(), 40)

    def test_invalid_env_falls_back_to_80(self):
        os.environ["TRAINMATE_WRAP_WIDTH"] = "not-a-number"
        self.assertEqual(self.util.default_wrap_width(), 80)

    def test_env_is_floored_at_20(self):
        os.environ["TRAINMATE_WRAP_WIDTH"] = "5"
        self.assertEqual(self.util.default_wrap_width(), 20)

    def test_wrap_text_honors_env_when_width_unset(self):
        os.environ["TRAINMATE_WRAP_WIDTH"] = "30"
        long = "word " * 40
        wrapped = self.util.wrap_text(long)
        self.assertTrue(all(len(line) <= 30 for line in wrapped.splitlines()))

    def test_explicit_width_still_wins_over_env(self):
        os.environ["TRAINMATE_WRAP_WIDTH"] = "30"
        long = "word " * 40
        wrapped = self.util.wrap_text(long, width=60)
        lines = wrapped.splitlines()
        # Both halves matter: wider than the env value, but still actually wrapped —
        # an unwrapped single line would satisfy the first check alone.
        self.assertTrue(any(len(line) > 30 for line in lines))
        self.assertTrue(all(len(line) <= 60 for line in lines))


class TestFlexColumn(unittest.TestCase):
    """A table with one unbounded cell fits the screen instead of wrapping it
    (DESIGN_logging.md §7.2)."""

    HEADERS = ["RUN", "COMMAND", "END"]

    def setUp(self):
        self._saved = os.environ.get("TRAINMATE_WRAP_WIDTH")
        # Not a terminal under test, so `display_width` is the wrap width: pin it.
        os.environ["TRAINMATE_WRAP_WIDTH"] = "80"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("TRAINMATE_WRAP_WIDTH", None)
        else:
            os.environ["TRAINMATE_WRAP_WIDTH"] = self._saved

    def test_a_short_cell_is_left_alone(self):
        rows = [["5a0e", "plan generate", "ok"]]
        table = render_table(self.HEADERS, rows, narrow=False, flex=1)
        self.assertIn("plan generate", table)
        self.assertNotIn("…", table)

    def test_a_long_cell_is_clipped_so_every_line_fits(self):
        rows = [["5a0e", "workout adapt -m '" + "long note " * 30 + "'", "ok"]]
        table = render_table(self.HEADERS, rows, narrow=False, flex=1)
        for line in table.split("\n"):
            self.assertLessEqual(visible_len(line), 80)
        self.assertIn("workout adapt -m 'long", table)
        self.assertIn("…", table)

    def test_the_other_columns_keep_their_own_width(self):
        rows = [["5a0e", "x" * 200, "FAILED"]]
        table = render_table(self.HEADERS, rows, narrow=False, flex=1)
        self.assertIn("FAILED", table)
        self.assertIn("5a0e", table)

    def test_truncate_visible_counts_colour_as_no_width(self):
        self.assertEqual(truncate_visible("abcdef", 6), "abcdef")
        self.assertEqual(truncate_visible("abcdef", 4), "abc…")
        # Forced on: off a TTY colorize() is a no-op and there would be no escapes left
        # to measure — which is the whole point of the assertion.
        with patch("trainmate.util.is_color_enabled", return_value=True):
            clipped = truncate_visible(yellow("abcdef"), 4)
        self.assertEqual(visible_len(clipped), 4)
        self.assertTrue(clipped.endswith(RESET))


class TestAsides(unittest.TestCase):
    """Side information prints on a terminal and not in chat
    (DESIGN_output_verbosity.md §3)."""

    def setUp(self):
        self._saved = {
            k: os.environ.pop(k, None) for k in ("TRAINMATE_VERBOSE", "TRAINMATE_FRONTEND")
        }
        from trainmate import util
        self.util = util

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _emit(self) -> str:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            self.util.aside("side information")
        return buf.getvalue()

    def test_prints_on_a_terminal(self):
        self.assertTrue(self.util.asides_enabled())
        self.assertIn("side information", self._emit())

    def test_silent_under_the_chat_frontend(self):
        os.environ["TRAINMATE_FRONTEND"] = "json"
        self.assertFalse(self.util.asides_enabled())
        self.assertEqual(self._emit(), "")

    def test_env_forces_them_back_on_in_chat(self):
        os.environ["TRAINMATE_FRONTEND"] = "json"
        os.environ["TRAINMATE_VERBOSE"] = "1"
        self.assertIn("side information", self._emit())

    def test_env_forces_them_off_on_a_terminal(self):
        os.environ["TRAINMATE_VERBOSE"] = "0"
        self.assertEqual(self._emit(), "")


class TestCommandsSurviveTheWrap(unittest.TestCase):
    """A command the athlete is told to run has to be copy-pastable, so the wrap never
    splits one across two lines (DESIGN_output_verbosity.md §3.6)."""

    def _wrapped(self, text: str, width: int = 48) -> list:
        from trainmate.util import wrap_text
        return wrap_text(text, width=width).split("\n")

    def test_a_quoted_command_lands_on_one_line(self):
        lines = self._wrapped(
            "The plan ran out on 2026-09-30, so run 'workout generate -d today..2026-10-14' "
            "to extend it."
        )
        self.assertTrue(
            any("'workout generate -d today..2026-10-14'" in line for line in lines),
            lines,
        )

    def test_two_commands_in_one_sentence_stay_separate(self):
        # The prose between them must still wrap: a match that ran from the first
        # command's closing quote to the second's would glue the sentence into one line.
        lines = self._wrapped(
            "Swap two dates (e.g. 'workout swap 2026-06-09 2026-06-11 'travelling'') "
            "or two workout IDs (e.g. 'workout swap 5 8 'travelling'')."
        )
        self.assertGreater(len(lines), 2, lines)
        self.assertTrue(any("'workout swap 5 8 'travelling''" in l for l in lines), lines)

    def test_an_apostrophe_does_not_open_a_command(self):
        lines = self._wrapped(
            "the athlete's own plan is what the coach reads, and the athlete's notes "
            "are what shapes it next time"
        )
        for line in lines:
            self.assertLessEqual(len(line), 48, lines)

    def test_a_bare_command_is_marked_by_its_caller(self):
        from trainmate.util import keep_whole
        text = "To backfill, run:\n  " + keep_whole(
            "python trainmate_cli.py data pull 2026-01-01 2026-08-31"
        )
        lines = self._wrapped(text)
        # Whole, still indented, and over the budget: a command longer than the width
        # cannot both fit and stay in one piece, and staying in one piece wins.
        self.assertIn("  python trainmate_cli.py data pull 2026-01-01 2026-08-31", lines)

    def test_the_marks_never_reach_a_log(self):
        from trainmate.util import keep_whole, strip_ansi
        self.assertEqual(strip_ansi(keep_whole("data pull -d 2026-01-01")),
                         "data pull -d 2026-01-01")

    def test_an_indent_survives_the_greedy_branch(self):
        lines = self._wrapped(
            "  - a list item quoting 'plan generate --force' that has to wrap because "
            "it is far too long for one line"
        )
        self.assertTrue(lines[0].startswith("  - "), lines)
        self.assertTrue(all(l.startswith("    ") for l in lines[1:]), lines)


class TestWarningTierWraps(unittest.TestCase):
    """Every printer of the warning tier wraps to the client's width
    (DESIGN_output_verbosity.md §3.5) — the whole reason `notice` exists."""

    LONG = ("The plan runs out on 2026-09-30, before this horizon (2026-10-14) — "
            "sessions after it have no block to follow. Run 'plan generate' to extend "
            "the periodization first.")

    def setUp(self):
        self._saved = os.environ.pop("TRAINMATE_WRAP_WIDTH", None)
        os.environ["TRAINMATE_WRAP_WIDTH"] = "48"
        from trainmate import util
        self.util = util

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("TRAINMATE_WRAP_WIDTH", None)
        else:
            os.environ["TRAINMATE_WRAP_WIDTH"] = self._saved

    def _emit(self, fn, *args) -> str:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            fn(*args)
        return self.util.strip_ansi(buf.getvalue())

    def _assert_fits(self, out: str) -> None:
        for line in out.splitlines():
            self.assertLessEqual(visible_len(line), 48, f"too wide: {line!r}")

    def test_notice_wraps(self):
        out = self._emit(self.util.notice, self.LONG)
        self._assert_fits(out)
        self.assertIn("The plan runs out on", out)

    def test_warn_wraps_its_prefix_along_with_the_text(self):
        # The prefix is part of the first line's budget, so it wraps with the text.
        out = self._emit(self.util.warn, self.LONG)
        self._assert_fits(out)
        self.assertTrue(out.startswith("Warning: "))

    def test_fail_wraps(self):
        out = self._emit(self.util.fail, self.LONG)
        self._assert_fits(out)
        self.assertTrue(out.startswith("Error: "))

    def test_hand_made_layout_survives_the_wrap(self):
        out = self._emit(self.util.notice, "Backfill from 2026-01-01:\n  data pull -d …")
        self._assert_fits(out)
        self.assertIn("\n  data pull", out)

    def test_no_warning_is_printed_by_hand(self):
        """The rule the wrap depends on: a whole yellow or red message goes through
        `notice`/`warn`/`fail`, never a bare print. Colour used as a *fragment* — a bold
        heading, one cell of a row, `red(x) + hint` — is untouched by this: it is not a
        message, and wrapping it would break the layout it sits in."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, "trainmate")):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                if os.path.samefile(path, os.path.join(root, "trainmate", "util.py")):
                    continue        # where `notice`, `warn` and `fail` are defined
                tree = ast.parse(open(path).read())
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or node.keywords:
                        continue
                    if getattr(node.func, "id", None) != "print" or len(node.args) != 1:
                        continue
                    arg = node.args[0]
                    if (isinstance(arg, ast.Call)
                            and getattr(arg.func, "id", None) in ("yellow", "red")):
                        offenders.append(f"{os.path.relpath(path, root)}:{node.lineno}")
        self.assertEqual(offenders, [], "use notice()/warn()/fail(): " + ", ".join(offenders))

    def test_no_message_spells_the_prefix_itself(self):
        """`warn` owns `Warning: `, and owning it is what puts the line in the journal
        at warn level. A message that spells the word itself is one that skipped it.

        `config.py` and `journal.py` say it by hand on purpose — config load runs before
        `util` can be imported, and the journal cannot journal its own write failure."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        exempt = {"util.py", "config.py", "journal.py"}
        offenders = []

        def leading_text(node):
            while True:
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                    node = node.left
                elif (isinstance(node, ast.Call) and node.args
                      and getattr(node.func, "id", None) in ("yellow", "red", "bold")):
                    node = node.args[0]
                else:
                    break
            if isinstance(node, ast.JoinedStr) and node.values:
                node = node.values[0]
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value.lstrip("\n")
            return ""

        for dirpath, dirnames, filenames in os.walk(os.path.join(root, "trainmate")):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if not name.endswith(".py") or name in exempt:
                    continue
                path = os.path.join(dirpath, name)
                for node in ast.walk(ast.parse(open(path).read())):
                    if not isinstance(node, ast.Call) or not node.args:
                        continue
                    if getattr(node.func, "id", None) not in ("print", "notice"):
                        continue
                    if leading_text(node.args[0]).startswith("Warning:"):
                        offenders.append(f"{os.path.relpath(path, root)}:{node.lineno}")
        self.assertEqual(offenders, [], "use warn(): " + ", ".join(offenders))

    def test_the_journal_keeps_the_unwrapped_line(self):
        # A log is not read at 48 columns (DESIGN_logging.md §5.3).
        with patch("trainmate.journal.note") as note:
            self._emit(self.util.warn, self.LONG)
        self.assertEqual(note.call_args.args[0], self.LONG)


if __name__ == "__main__":
    unittest.main()
