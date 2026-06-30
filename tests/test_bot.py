"""Tests for the Telegram front-end's pure helpers (no telegram dependency)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trainmate_bot as bot


class ParseMessageTest(unittest.TestCase):
    def test_strips_leading_slash(self):
        self.assertEqual(bot.parse_message_to_argv("/status"), ["status"])

    def test_plain_text_is_a_command_too(self):
        self.assertEqual(bot.parse_message_to_argv("status"), ["status"])

    def test_subcommand_and_flags(self):
        self.assertEqual(
            bot.parse_message_to_argv("/workout list --weeks 1"),
            ["workout", "list", "--weeks", "1"],
        )

    def test_quoted_argument_kept_whole(self):
        self.assertEqual(
            bot.parse_message_to_argv('/workout adapt -m "tired today"'),
            ["workout", "adapt", "-m", "tired today"],
        )

    def test_strips_matching_bot_username_suffix(self):
        self.assertEqual(
            bot.parse_message_to_argv("/status@TrainMateBot", bot_username="TrainMateBot"),
            ["status"],
        )

    def test_strips_bot_suffix_when_username_unknown(self):
        self.assertEqual(bot.parse_message_to_argv("/status@SomeBot"), ["status"])

    def test_help_alone_maps_to_help_command(self):
        self.assertEqual(bot.parse_message_to_argv("/help"), ["help"])

    def test_help_with_command_maps_to_command_help(self):
        self.assertEqual(bot.parse_message_to_argv("help workout"), ["workout", "--help"])

    def test_blank_returns_none(self):
        self.assertIsNone(bot.parse_message_to_argv("   "))
        self.assertIsNone(bot.parse_message_to_argv("/"))

    def test_unbalanced_quotes_raise(self):
        with self.assertRaises(ValueError):
            bot.parse_message_to_argv('/workout adapt -m "oops')


class AuthTest(unittest.TestCase):
    def test_allowlisted_id_passes(self):
        self.assertTrue(bot.is_authorized(42, [1, 42, 3]))

    def test_unlisted_id_rejected(self):
        self.assertFalse(bot.is_authorized(99, [1, 42, 3]))

    def test_empty_allowlist_rejects_everyone(self):
        self.assertFalse(bot.is_authorized(42, []))


class ChunkTest(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(bot.chunk_text("hello"), ["hello"])

    def test_splits_on_line_boundaries(self):
        text = "\n".join(["x" * 100 for _ in range(60)])
        chunks = bot.chunk_text(text, limit=250)
        self.assertTrue(len(chunks) > 1)
        self.assertTrue(all(len(c) <= 250 for c in chunks))
        # Round-trips back to the original once rejoined.
        self.assertEqual("\n".join(chunks), text)

    def test_hard_splits_overlong_single_line(self):
        chunks = bot.chunk_text("y" * 1000, limit=300)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertEqual("".join(chunks), "y" * 1000)


class FormatReplyTest(unittest.TestCase):
    def test_wraps_in_pre_and_escapes_html(self):
        parts = bot.format_reply("a < b & c > d")
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0].startswith("<pre>"))
        self.assertTrue(parts[0].endswith("</pre>"))
        self.assertIn("&lt;", parts[0])
        self.assertIn("&amp;", parts[0])


class PromptProtocolTest(unittest.TestCase):
    def test_parses_sentinel_framed_request(self):
        from trainmate.prompt import PROMPT_SENTINEL
        line = PROMPT_SENTINEL + '{"v":1,"id":"p1","type":"confirm","message":"go?"}\n'
        req = bot.parse_prompt_request(line)
        self.assertEqual(req["id"], "p1")
        self.assertEqual(req["type"], "confirm")

    def test_plain_output_is_not_a_request(self):
        self.assertIsNone(bot.parse_prompt_request("All workouts wiped.\n"))

    def test_confirm_buttons_yes_no(self):
        rows = bot.prompt_buttons({"id": "p1", "type": "confirm"}, "ab12")
        labels = [label for row in rows for label, _ in row]
        datas = [data for row in rows for _, data in row]
        self.assertIn("✅ Yes", labels)
        self.assertEqual(datas, ["ab12:p1:y", "ab12:p1:n"])

    def test_danger_confirm_uses_warning_label(self):
        rows = bot.prompt_buttons({"id": "p1", "type": "confirm", "danger": True}, "ab12")
        self.assertEqual(rows[0][0][0], "⚠️ Confirm")

    def test_choose_buttons_one_per_choice(self):
        req = {"id": "p2", "type": "choose",
               "choices": [{"value": "demote", "label": "Demote"},
                           {"value": "keep", "label": "Keep"}]}
        rows = bot.prompt_buttons(req, "n0nce")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], ("Demote", "n0nce:p2:demote"))

    def test_text_prompt_has_no_buttons(self):
        self.assertEqual(bot.prompt_buttons({"id": "p3", "type": "text"}, "n"), [])

    def test_callback_roundtrips_through_buttons(self):
        rows = bot.prompt_buttons({"id": "p1", "type": "confirm"}, "ab12")
        _, data = rows[0][0]
        self.assertEqual(bot.decode_callback(data), ("ab12", "p1", "y"))

    def test_decode_callback_rejects_malformed(self):
        self.assertIsNone(bot.decode_callback("only:two"))

    def test_format_prompt_message_strips_ansi(self):
        msg = bot.format_prompt_message({"message": "\033[33mProceed?\033[0m"})
        self.assertEqual(msg, "Proceed?")


class WrapWidthTest(unittest.TestCase):
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
        self.assertTrue(any(len(line) > 30 for line in wrapped.splitlines()))


if __name__ == "__main__":
    unittest.main()
