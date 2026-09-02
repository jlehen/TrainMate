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
            bot.parse_message_to_argv("/workout list -d 1w"),
            ["workout", "list", "-d", "1w"],
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

    def test_simple_menu_keeps_cancel_reachable(self):
        names = [name for name, _ in bot.SIMPLE_MENU_COMMANDS]
        self.assertIn("cancel", names)


class UiSwitchTest(unittest.TestCase):
    """The /ui runtime persona switch (§5.6): bare form flips, explicit form sets,
    anything else reads as usage (None)."""

    def test_bare_ui_flips_the_current_mode(self):
        self.assertIs(bot.parse_ui_switch("ui", simple_now=False), True)
        self.assertIs(bot.parse_ui_switch("ui", simple_now=True), False)

    def test_explicit_arguments_set_the_mode_regardless_of_current(self):
        self.assertIs(bot.parse_ui_switch("ui simple", simple_now=True), True)
        self.assertIs(bot.parse_ui_switch("ui expert", simple_now=False), False)
        self.assertIs(bot.parse_ui_switch("ui on", simple_now=True), True)
        self.assertIs(bot.parse_ui_switch("ui off", simple_now=False), False)

    def test_unknown_or_extra_arguments_read_as_usage(self):
        self.assertIsNone(bot.parse_ui_switch("ui blorp", simple_now=False))
        self.assertIsNone(bot.parse_ui_switch("ui simple please", simple_now=False))

    def test_only_the_expert_menu_advertises_the_switch(self):
        # The simple menu stays the athlete's two entries; the §5.6 confirmation
        # lines teach the way back instead.
        self.assertIn("ui", [n for n, _ in bot.MENU_COMMANDS])
        self.assertNotIn("ui", [n for n, _ in bot.SIMPLE_MENU_COMMANDS])


class SimpleKeyboardTest(unittest.TestCase):
    """The §5.1 reply keyboard: labels map onto a fixed argv table, nothing else."""

    def test_labels_map_to_fixed_argv(self):
        self.assertEqual(
            bot.keyboard_action("📅 Today"),
            ("run", ["workout", "list", "-d", "today"]),
        )
        self.assertEqual(bot.keyboard_action("🗓 My week"), ("run", ["workout", "list"]))
        self.assertEqual(bot.keyboard_action("🎯 Goals"), ("run", ["goal", "list"]))
        self.assertEqual(bot.keyboard_action("🧭 My plan"), ("run", ["plan", "show"]))
        self.assertEqual(
            bot.keyboard_action("📈 Progress"), ("run", ["progress", "--chart"])
        )

    def test_capture_button_arms_instead_of_running(self):
        self.assertEqual(bot.keyboard_action("💬 Talk to me"), ("capture", None))

    def test_welcome_names_every_keyboard_label(self):
        """The welcome teaches the keyboard, so a relabelled button cannot drift
        out of it (§5.1)."""
        for label, _ in bot.SIMPLE_KEYBOARD:
            self.assertIn(label, bot.SIMPLE_WELCOME, label)

    def test_non_label_text_is_not_a_button(self):
        self.assertIsNone(bot.keyboard_action("show me my week"))
        self.assertIsNone(bot.keyboard_action(""))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(
            bot.keyboard_action("  📅 Today  "),
            ("run", ["workout", "list", "-d", "today"]),
        )

    def test_returned_argv_is_a_copy(self):
        kind, argv = bot.keyboard_action("📅 Today")
        argv.append("--verbose")
        self.assertEqual(
            bot.keyboard_action("📅 Today"),
            ("run", ["workout", "list", "-d", "today"]),
        )


class StaleKeyboardTest(unittest.TestCase):
    """A restart returns to config's persona while the phone keeps the §5.1 keyboard;
    a tap on it must reach the companion, not shlex (§5.6)."""

    def test_label_tapped_in_expert_is_a_stale_tap(self):
        for label, _ in bot.SIMPLE_KEYBOARD:
            self.assertTrue(bot.stale_keyboard_tap(label, simple_now=False), label)

    def test_nothing_is_stale_while_simple(self):
        for label, _ in bot.SIMPLE_KEYBOARD:
            self.assertFalse(bot.stale_keyboard_tap(label, simple_now=True), label)

    def test_expert_typing_is_untouched(self):
        self.assertFalse(bot.stale_keyboard_tap("workout list", simple_now=False))
        self.assertFalse(bot.stale_keyboard_tap("/ui", simple_now=False))
        self.assertFalse(bot.stale_keyboard_tap("", simple_now=False))


class GuardrailTest(unittest.TestCase):
    """§7: buttons and router intents only reach read-only views, `adapt -m`, and the
    §5.5 constraints view (whose picker offers single-ID `constraint rm` — pinned in
    tests/test_cli_bot.py) — nothing plan-shaping or expensive is reachable without
    typing."""

    ALLOWED_PREFIXES = {
        ("workout", "list"), ("goal", "list"), ("plan", "show"),
        ("progress", "--chart"), ("bot", "constraints"),
    }

    def test_keyboard_argv_stays_read_only(self):
        for label, argv in bot.SIMPLE_KEYBOARD:
            if argv is None:
                continue
            self.assertIn(tuple(argv[:2]), self.ALLOWED_PREFIXES, label)

    def test_router_argv_stays_read_only(self):
        for intent, argv in bot.ROUTER_INTENT_ARGV.items():
            self.assertIn(tuple(argv[:2]), self.ALLOWED_PREFIXES, intent)

    def test_morning_button_utterances_reach_only_adapt_m(self):
        from trainmate.cli.bot import MORNING_BUTTONS

        def leaves(buttons):
            for b in buttons:
                if b.get("menu"):
                    yield from leaves(b["menu"])
                else:
                    yield b

        sends = [b["send"] for b in leaves(MORNING_BUTTONS) if b.get("send")]
        self.assertTrue(sends)
        for utterance in sends:
            argv = bot.parse_message_to_argv(utterance)
            self.assertEqual(argv[:2], ["workout", "adapt"], utterance)
            self.assertIn("-m", argv)
            self.assertNotIn("-y", argv)


class RouterTablesTest(unittest.TestCase):
    """The intent names live in trainmate/cli/bot.py (what the model may pick) and the
    argv in trainmate_bot.py (what each intent runs) — a rule that spans files, pinned
    here so the two tables cannot drift (§5.3)."""

    # The intents that carry the athlete's text into the `adapt -m` inbox. They run the
    # same argv and differ only in the echo, so a misroute among them stores the same
    # thing — which is why the inbox needs no per-kind intent (§5.5).
    INBOX = {"coach_message", "add_constraint", "add_signal"}
    # Intents the bot answers itself or maps with the athlete's text attached.
    # `new_goal` is reply-only and deliberately runs nothing (DESIGN_runway_nudge.md §6).
    SPECIAL = INBOX | {"help", "unclear", "new_goal"}

    def test_every_cli_intent_lands_somewhere_in_the_bot(self):
        from trainmate.cli.bot import ROUTER_INTENTS
        for intent in ROUTER_INTENTS:
            self.assertTrue(
                intent in bot.ROUTER_INTENT_ARGV or intent in self.SPECIAL, intent
            )

    def test_bot_tables_name_no_unknown_intent(self):
        from trainmate.cli.bot import ROUTER_INTENTS
        for intent in list(bot.ROUTER_INTENT_ARGV) + list(bot.ROUTER_ECHO):
            self.assertIn(intent, ROUTER_INTENTS)

    def test_inbox_intents_carry_text_and_differ_only_in_the_echo(self):
        from trainmate.cli.bot import ROUTER_INTENTS
        for intent in self.INBOX:
            self.assertIn(intent, ROUTER_INTENTS, intent)
            # No argv of their own: the text rides along, so the dispatch builds it.
            self.assertNotIn(intent, bot.ROUTER_INTENT_ARGV, intent)
            # An echo each, and a distinct one — the only thing the split buys.
            self.assertIn(intent, bot.ROUTER_ECHO, intent)
        echoes = [bot.ROUTER_ECHO[i] for i in self.INBOX]
        self.assertEqual(len(set(echoes)), len(echoes))


class ButtonsProtocolTest(unittest.TestCase):
    def test_roundtrips_through_emit_buttons(self):
        import io
        from trainmate.prompt import emit_buttons
        buf = io.StringIO()
        emit_buttons([{"label": "A", "send": "status"}], out=buf)
        # split("\n"), not splitlines(): \x1e is itself a str.splitlines boundary,
        # while the bot reads byte lines split on \n alone.
        line = buf.getvalue().split("\n")[0]
        req = bot.parse_buttons_request(line)
        self.assertEqual(req["buttons"], [{"label": "A", "send": "status"}])

    def test_plain_output_is_not_a_buttons_request(self):
        self.assertIsNone(bot.parse_buttons_request("workout listed"))

    def test_prompt_line_is_not_a_buttons_request(self):
        self.assertIsNone(bot.parse_buttons_request('\x1eTM-PROMPT {"id": "p1"}'))

    def test_buttons_line_is_not_a_prompt_or_photo(self):
        line = '\x1eTM-BUTTONS {"buttons": []}'
        self.assertIsNone(bot.parse_prompt_request(line))
        self.assertIsNone(bot.parse_photo_request(line))


class FlushProtocolTest(unittest.TestCase):
    """The payload-free flush marker (DESIGN_output_verbosity.md §7)."""

    def test_roundtrips_through_emit_flush(self):
        import io
        from trainmate.prompt import emit_flush
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TRAINMATE_FRONTEND": "json"}):
            emit_flush(out=out)
        self.assertTrue(bot.is_flush_request(out.getvalue()))

    def test_a_terminal_gets_nothing(self):
        # Emitted unconditionally, the \x1e frame would land in the athlete's own
        # scrollback as protocol bytes. The gate is inside emit_flush.
        import io
        from trainmate.prompt import emit_flush
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TRAINMATE_FRONTEND": ""}):
            emit_flush(out=out)
        self.assertEqual(out.getvalue(), "")

    def test_ordinary_output_is_not_a_flush_request(self):
        self.assertFalse(bot.is_flush_request("Plan discarded.\n"))

    def test_a_sibling_sentinel_is_not_a_flush_request(self):
        # Each sentinel has its own branch in _drive; matching a sibling here would
        # swallow a prompt and hang the command waiting for an answer.
        from trainmate.prompt import PROMPT_SENTINEL, PHOTO_SENTINEL, BUTTONS_SENTINEL
        for sentinel in (PROMPT_SENTINEL, PHOTO_SENTINEL, BUTTONS_SENTINEL):
            self.assertFalse(bot.is_flush_request(sentinel + '{"id": "p1"}\n'))


class UiCallbackTest(unittest.TestCase):
    def test_roundtrips(self):
        data = bot.ui_callback_data("abc123", "2.1")
        self.assertEqual(bot.decode_ui_callback(data), ("abc123", "2.1"))

    def test_rejects_malformed_and_foreign_namespaces(self):
        self.assertIsNone(bot.decode_ui_callback("nonce:p1:y"))
        self.assertIsNone(bot.decode_ui_callback("ui:onlytoken"))
        self.assertIsNone(bot.decode_ui_callback(""))
        self.assertIsNone(bot.decode_ui_callback("ui::2"))

    def test_resolves_top_level_and_menu_paths(self):
        buttons = [
            {"label": "A", "ack": "ok"},
            {"label": "B", "menu": [{"label": "B1", "send": "status"}]},
        ]
        self.assertEqual(bot.resolve_ui_action(buttons, "0")["label"], "A")
        self.assertEqual(bot.resolve_ui_action(buttons, "1.0")["label"], "B1")

    def test_unresolvable_paths_return_none(self):
        buttons = [{"label": "A"}]
        self.assertIsNone(bot.resolve_ui_action(buttons, "5"))
        self.assertIsNone(bot.resolve_ui_action(buttons, "0.0"))
        self.assertIsNone(bot.resolve_ui_action(buttons, "0.0.0"))
        self.assertIsNone(bot.resolve_ui_action(buttons, "x"))

    def test_morning_buttons_fit_telegrams_64_byte_callback_cap(self):
        from trainmate.cli.bot import MORNING_BUTTONS
        token = "aabbcc"  # secrets.token_hex(3) width
        rows = bot.ui_button_rows(MORNING_BUTTONS, token)
        for i, button in enumerate(MORNING_BUTTONS):
            for menu_row in bot.ui_menu_rows(button.get("menu") or [], token, str(i)):
                rows.append(menu_row)
        for row in rows:
            for _label, data in row:
                self.assertLessEqual(len(data.encode()), 64, data)

    def test_top_level_renders_one_row_menu_one_per_line(self):
        buttons = [{"label": "A"}, {"label": "B"}, {"label": "C"}]
        self.assertEqual(len(bot.ui_button_rows(buttons, "t")), 1)
        self.assertEqual(len(bot.ui_menu_rows(buttons, "t", "2")), 3)

    def test_a_fourth_button_wraps_and_keeps_its_flat_position(self):
        """The morning push gains one when the schedule is running out
        (DESIGN_runway_nudge.md §6); four across would shrink all four past reading."""
        buttons = [{"label": c} for c in "ABCD"]
        rows = bot.ui_button_rows(buttons, "t")
        self.assertEqual([len(r) for r in rows], [3, 1])
        self.assertEqual(rows[1][0][1], bot.ui_callback_data("t", "3"))


class PushScheduleTest(unittest.TestCase):
    """`next_push_delay` — the §4.3 send/catch-up window arithmetic."""

    def _at(self, hour, minute=0):
        import datetime as dt
        return dt.datetime(2026, 8, 25, hour, minute)

    def test_before_the_window_waits_for_morning(self):
        self.assertEqual(bot.next_push_delay(self._at(6), "08:00", "15:00"), 7200.0)

    def test_inside_the_window_fires_now(self):
        self.assertEqual(bot.next_push_delay(self._at(8), "08:00", "15:00"), 0.0)
        self.assertEqual(bot.next_push_delay(self._at(14, 59), "08:00", "15:00"), 0.0)

    def test_past_the_deadline_skips_to_tomorrow(self):
        delay = bot.next_push_delay(self._at(16), "08:00", "15:00")
        self.assertEqual(delay, 16 * 3600.0)  # 16:00 → 08:00 next day

    def test_unparseable_times_fall_back_to_defaults(self):
        self.assertEqual(bot.next_push_delay(self._at(9), "morning!", "nope"), 0.0)
        self.assertEqual(bot.next_push_delay(self._at(6), "25:99", ""), 7200.0)

    def test_deadline_before_morning_means_no_catchup(self):
        delay = bot.next_push_delay(self._at(9), "08:00", "07:00")
        self.assertGreater(delay, 0)  # window already closed for the day


if __name__ == "__main__":
    unittest.main()
