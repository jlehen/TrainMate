import io
import os
import unittest
from unittest.mock import patch
from trainmate.util import (
    wrap_text, visible_len, pad_visible, color_load_ratio, format_labeled_text,
    yellow, Progress,
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


if __name__ == "__main__":
    unittest.main()
