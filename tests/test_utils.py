import unittest
from unittest.mock import patch
from trainmate.util import (
    wrap_text, visible_len, pad_visible, color_load_ratio, format_labeled_text,
    yellow,
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
        with unittest.mock.patch("trainmate.util.is_color_enabled", return_value=True):
            # Only the overload end is coloured. A LOW ratio is phase-dependent
            # (taper, deload, intensity block), so it must render bare — see
            # training_load.txt §3/§4.
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


if __name__ == "__main__":
    unittest.main()
