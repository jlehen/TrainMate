"""Tests for the Telegram front-end's pure helpers (no telegram dependency)."""
import asyncio
import os
import sys
import unittest
from unittest import mock

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

    def test_photo_line_is_not_a_prompt_request(self):
        from trainmate.prompt import PHOTO_SENTINEL
        line = PHOTO_SENTINEL + '{"path": "/tmp/x.png", "caption": "FORM today"}\n'
        self.assertIsNone(bot.parse_prompt_request(line))

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


class PhotoProtocolTest(unittest.TestCase):
    def test_roundtrips_through_emit_photo(self):
        import io
        from trainmate.prompt import emit_photo
        out = io.StringIO()
        emit_photo("/tmp/chart.png", caption="FORM today   CTL 55", out=out)
        line = out.getvalue()
        req = bot.parse_photo_request(line)
        self.assertEqual(req["path"], "/tmp/chart.png")
        self.assertEqual(req["caption"], "FORM today   CTL 55")

    def test_emit_photo_without_caption(self):
        import io
        from trainmate.prompt import emit_photo
        out = io.StringIO()
        emit_photo("/tmp/chart.png", out=out)
        req = bot.parse_photo_request(out.getvalue())
        self.assertEqual(req["path"], "/tmp/chart.png")
        self.assertIsNone(req["caption"])

    def test_non_photo_line_returns_none(self):
        self.assertIsNone(bot.parse_photo_request("All workouts wiped.\n"))

    def test_prompt_line_is_not_a_photo_request(self):
        from trainmate.prompt import PROMPT_SENTINEL
        line = PROMPT_SENTINEL + '{"v":1,"id":"p1","type":"confirm","message":"go?"}\n'
        self.assertIsNone(bot.parse_photo_request(line))

    def test_unknown_sentinel_is_dropped_not_forwarded(self):
        # A future CLI sentinel this bot build doesn't understand: both parsers
        # must return None so _drive()'s catch-all drop rule applies (§7.2).
        line = "\x1eTM-FUTURE-THING {\"x\": 1}\n"
        self.assertIsNone(bot.parse_prompt_request(line))
        self.assertIsNone(bot.parse_photo_request(line))
        self.assertTrue(line.startswith(bot._SENTINEL_PREFIX))


class _FakeProc:
    """Stand-in for the CLI subprocess: exits on its own only if told to."""

    def __init__(self, exits_on_its_own: bool = False) -> None:
        self.returncode = None
        self.killed = False
        self._exits_on_its_own = exits_on_its_own

    async def wait(self) -> int:
        if not self._exits_on_its_own:
            await asyncio.sleep(3600)
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class RestartTeardownTest(unittest.IsolatedAsyncioTestCase):
    """/restart's teardown (DESIGN_bot_restart.md §5.2): no orphaned subprocess and no
    long-poll left open when os._exit() fires."""

    def setUp(self):
        patcher = mock.patch.object(bot, "RESTART_GRACE_SECONDS", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stops = []

    async def _stop_polling(self):
        self.stops.append(True)

    def _session(self, awaiting=None, proc=None):
        session = bot._Session(42, proc if proc is not None else _FakeProc(), "n0nce")
        if awaiting is not None:
            session.awaiting = awaiting
            session.answer_future = asyncio.get_running_loop().create_future()
        return session

    async def test_kills_a_silently_computing_session(self):
        # A single getUpdates batch can deliver a command and /restart together, so
        # /restart can land with a subprocess running and no prompt open.
        session = self._session()
        await bot.restart_teardown(session, self._stop_polling)
        self.assertTrue(session.proc.killed)

    async def test_open_prompt_is_answered_cancelled_not_killed(self):
        session = self._session({"id": "p1", "type": "confirm"}, _FakeProc(exits_on_its_own=True))
        await bot.restart_teardown(session, self._stop_polling)
        self.assertTrue(session.answer_future.result()["cancelled"])
        self.assertFalse(session.proc.killed)

    async def test_open_prompt_whose_process_lingers_is_killed_after_the_grace(self):
        session = self._session({"id": "p1", "type": "confirm"})
        await bot.restart_teardown(session, self._stop_polling)
        self.assertTrue(session.proc.killed)

    async def test_finished_process_is_left_alone(self):
        session = self._session()
        session.proc.returncode = 0
        await bot.restart_teardown(session, self._stop_polling)
        self.assertFalse(session.proc.killed)

    async def test_releases_the_long_poll(self):
        # Without this, the abandoned getUpdates never confirms its offset and the
        # relaunched worker is served the same /restart again (§7).
        await bot.restart_teardown(None, self._stop_polling)
        self.assertEqual(self.stops, [True])
        session = self._session()
        await bot.restart_teardown(session, self._stop_polling)
        self.assertEqual(len(self.stops), 2)

    async def test_a_wedged_stop_still_returns(self):
        async def _hangs():
            await asyncio.sleep(3600)

        await bot.restart_teardown(None, _hangs)  # must not block the hard exit

    async def test_a_failing_stop_still_returns(self):
        async def _raises():
            raise RuntimeError("This Updater is not running!")

        await bot.restart_teardown(None, _raises)


class MenuCommandsTest(unittest.TestCase):
    def test_restart_is_advertised_in_the_command_menu(self):
        # Not treated as special: the teardown ends any live session cleanly, so a
        # mis-tap costs a reconnect, not work (DESIGN_bot_restart.md §7).
        names = [name for name, _ in bot.MENU_COMMANDS]
        self.assertIn("restart", names)
        self.assertIn("cancel", names)


if __name__ == "__main__":
    unittest.main()
