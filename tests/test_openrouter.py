import io
import os
import unittest
from unittest.mock import patch, MagicMock
from trainmate.openrouter import OpenRouterClient, _human_wait
from trainmate.prompt import FLUSH_SENTINEL


def _ok_response(content: str) -> MagicMock:
    """A 200 whose single choice carries `content`."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return resp


class TestParseJsonContent(unittest.TestCase):
    """_parse_json_content — the tolerance for chatty model output."""

    def test_bare_json(self):
        self.assertEqual(OpenRouterClient._parse_json_content('{"a": 1}'), {"a": 1})

    def test_multiline_fence_with_language_tag(self):
        text = '```json\n{"a": 1}\n```'
        self.assertEqual(OpenRouterClient._parse_json_content(text), {"a": 1})

    def test_single_line_fence(self):
        # No newline to split on, so the opening fence has to be stripped by width.
        self.assertEqual(OpenRouterClient._parse_json_content('```{"a": 1}```'), {"a": 1})

    def test_single_line_fence_with_language_tag(self):
        self.assertEqual(
            OpenRouterClient._parse_json_content('```json {"a": 1}```'), {"a": 1}
        )

    def test_unterminated_fence(self):
        # A truncated reply still parses: the closing fence is optional.
        self.assertEqual(OpenRouterClient._parse_json_content('```json\n{"a": 1}'), {"a": 1})

    def test_trailing_prose_after_the_value_is_ignored(self):
        text = '{"a": 1}\n\nLet me know if you want me to adjust anything!'
        self.assertEqual(OpenRouterClient._parse_json_content(text), {"a": 1})

    def test_nested_object_survives_the_fence(self):
        text = '```json\n{"workouts": [{"date": "2026-06-01"}]}\n```'
        self.assertEqual(
            OpenRouterClient._parse_json_content(text),
            {"workouts": [{"date": "2026-06-01"}]},
        )

    def test_self_correction_keeps_the_last_object(self):
        # A model that answers, notices the answer was partial and answers again: the
        # second block is the answer, the first is the one it abandoned.
        text = (
            '```json\n'
            '{"change_needed": true, "reason": "nothing needs easing"}\n'
            '```\n'
            'Wait — I must return the full object.\n\n'
            '```json\n'
            '{"change_needed": true, "reason": "nothing gets eased", '
            '"adapted_workouts": [{"date": "2026-09-02"}]}\n'
            '```'
        )
        self.assertEqual(
            OpenRouterClient._parse_json_content(text),
            {
                "change_needed": True,
                "reason": "nothing gets eased",
                "adapted_workouts": [{"date": "2026-09-02"}],
            },
        )

    def test_a_brace_in_the_prose_between_blocks_is_skipped(self):
        # The scan must step over a brace that starts no value, or it stops before the
        # correction and keeps the abandoned block after all.
        text = (
            '{"a": 1}\n'
            'Hold on, that is wrong {not json here}. Again:\n'
            '{"a": 2}'
        )
        self.assertEqual(OpenRouterClient._parse_json_content(text), {"a": 2})

    def test_dropping_a_block_is_recorded(self):
        # Silence was half the bug: nothing said the discarded characters existed.
        with patch("trainmate.openrouter.journal.note") as note:
            OpenRouterClient._parse_json_content('{"a": 1}\n{"a": 2}')
        note.assert_called_once()
        self.assertEqual(note.call_args.kwargs["lvl"], "warn")

    def test_a_single_object_is_not_reported(self):
        with patch("trainmate.openrouter.journal.note") as note:
            OpenRouterClient._parse_json_content('{"a": 1}\n\nAnything else?')
        note.assert_not_called()

    def test_unparseable_content_raises(self):
        with self.assertRaises(ValueError):
            OpenRouterClient._parse_json_content("I'm afraid I can't do that.")


class TestOpenRouterClient(unittest.TestCase):

    def setUp(self):
        self.client = OpenRouterClient()
        # Pin the model: an unpinned client resolves it from the database, which these
        # tests have no business reaching (DESIGN_model_selection.md §3.1).
        self.client.model = "openai/gpt-5.4"

    @patch("trainmate.openrouter.requests.post")
    def test_openrouter_error_payload_raises_valueerror(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "error": {
                "message": "Rate limit reached for gpt-5.4 on tokens per min (TPM)",
                "code": 429
            }
        }
        mock_post.return_value = mock_response

        with patch.object(self.client, "_log_exchange"):
            with self.assertRaises(ValueError) as ctx:
                self.client.complete("system prompt", "user prompt", label="test")

        self.assertIn("Rate limit reached for gpt-5.4", str(ctx.exception))

    @patch("trainmate.openrouter.requests.post")
    def test_error_payload_is_logged_exactly_once(self, mock_post):
        # The ValueError raised on the error branch falls into the generic handler, so
        # without a guard the same exchange gets written to two log files.
        mock_post.return_value = _ok_response("")
        mock_post.return_value.json.return_value = {"error": {"message": "boom"}}

        with patch.object(self.client, "_log_exchange") as log:
            with self.assertRaises(ValueError):
                self.client.complete("system prompt", "user prompt", label="test")

        self.assertEqual(log.call_count, 1)

    @patch("trainmate.openrouter.requests.post")
    def test_non_dict_error_value_still_surfaces(self, mock_post):
        mock_post.return_value = _ok_response("")
        mock_post.return_value.json.return_value = {"error": "upstream exploded"}

        with patch.object(self.client, "_log_exchange"):
            with self.assertRaises(ValueError) as ctx:
                self.client.complete("s", "u", label="test")

        self.assertIn("upstream exploded", str(ctx.exception))

    @patch("trainmate.openrouter.requests.post")
    def test_empty_choices_raises(self, mock_post):
        resp = _ok_response("")
        resp.json.return_value = {"choices": []}
        mock_post.return_value = resp

        with patch.object(self.client, "_log_exchange"):
            with self.assertRaises(ValueError) as ctx:
                self.client.complete("s", "u", label="test")

        self.assertIn("Empty completion", str(ctx.exception))

    @patch("trainmate.openrouter.requests.post")
    def test_an_error_inside_the_choice_surfaces_as_the_providers_words(self, mock_post):
        # A provider that dies after generation began still answers 200, with the
        # error beside the partial content — a lone "{" with no usage this morning.
        resp = _ok_response("{")
        resp.json.return_value = {"choices": [{
            "message": {"content": "{"}, "finish_reason": "error",
            "error": {"code": 502, "message": "Provider disconnected mid-stream"},
        }]}
        mock_post.return_value = resp

        with patch.object(self.client, "_log_exchange") as log, \
                patch.object(self.client, "_record_call") as rec:
            with self.assertRaises(ValueError) as ctx:
                self.client.complete("s", "u", label="test")

        self.assertIn("Provider disconnected mid-stream", str(ctx.exception))
        self.assertNotIn("Expecting", str(ctx.exception))
        self.assertEqual(log.call_count, 1)
        self.assertIn("Provider disconnected", log.call_args.kwargs["error_msg"])
        self.assertIs(rec.call_args.args[2], False)

    @patch("trainmate.openrouter.requests.post")
    def test_an_unreadable_reply_is_a_failed_call_that_names_its_finish_reason(
        self, mock_post
    ):
        resp = _ok_response("{")
        resp.json.return_value["choices"][0]["finish_reason"] = "length"
        mock_post.return_value = resp

        with patch.object(self.client, "_log_exchange") as log, \
                patch.object(self.client, "_record_call") as rec:
            with self.assertRaises(ValueError) as ctx:
                self.client.complete("s", "u", label="test")

        self.assertIn("finish_reason: 'length'", str(ctx.exception))
        # Logged once, as a failure: the journal used to say ok before parsing.
        self.assertEqual(log.call_count, 1)
        self.assertIn("not readable JSON", log.call_args.kwargs["error_msg"])
        self.assertIs(rec.call_args.args[2], False)

    @patch("trainmate.openrouter.requests.post")
    def test_a_readable_reply_is_recorded_once_as_ok(self, mock_post):
        mock_post.return_value = _ok_response('{"ok": true}')

        with patch.object(self.client, "_log_exchange") as log, \
                patch.object(self.client, "_record_call") as rec:
            self.client.complete("s", "u", label="test")

        self.assertEqual(log.call_count, 1)
        self.assertNotIn("error_msg", log.call_args.kwargs)
        self.assertIs(rec.call_args.args[2], True)

    @patch("trainmate.openrouter.requests.post")
    def test_request_carries_auth_json_mode_and_timeout(self, mock_post):
        mock_post.return_value = _ok_response('{"ok": true}')

        with patch.object(self.client, "_log_exchange"):
            out = self.client.complete("SYSTEM", "USER", label="test")

        self.assertEqual(out, {"ok": True})
        kwargs = mock_post.call_args.kwargs
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("Bearer "))
        # json_object mode and a timeout are what keep a chatty or hung model from
        # breaking the CLI; both are easy to drop in a refactor.
        self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertIsNotNone(kwargs["timeout"])
        # System/user split is what makes prompt caching possible.
        roles = [m["role"] for m in kwargs["json"]["messages"]]
        self.assertEqual(roles, ["system", "user"])

    @patch("trainmate.openrouter.requests.post")
    def test_missing_api_key_raises_before_any_request(self, mock_post):
        with patch("trainmate.openrouter.config") as cfg:
            cfg.openrouter_api_key = ""
            with self.assertRaises(ValueError) as ctx:
                self.client.complete("s", "u")
        self.assertIn("API key is not configured", str(ctx.exception))
        mock_post.assert_not_called()


class TestChatFlush(unittest.TestCase):
    """Every LLM command goes quiet here for tens of seconds, and a chat front-end
    buffers until a prompt or exit — so this is where the setup is sent
    (DESIGN_output_verbosity.md §7)."""

    def setUp(self):
        self.client = OpenRouterClient()
        self.client.model = "openai/gpt-5.4"

    def _complete_capturing_stdout(self, frontend: str) -> str:
        """Runs one completion with stdout captured, returning what had been written
        by the time `requests.post` was entered — not at the end of the call, which
        would pass whether the flush came before the wait or after it."""
        buf = io.StringIO()
        at_post = {}

        def _record(*args, **kwargs):
            at_post["stdout"] = buf.getvalue()
            return _ok_response('{"ok": true}')

        with patch.dict(os.environ, {"TRAINMATE_FRONTEND": frontend}), \
                patch("sys.stdout", buf), \
                patch("trainmate.openrouter.requests.post", side_effect=_record), \
                patch.object(self.client, "_log_exchange"):
            self.client.complete("SYSTEM", "USER", label="test")
        return at_post["stdout"]

    def test_the_chat_buffer_is_flushed_before_the_request_goes_out(self):
        self.assertIn(FLUSH_SENTINEL, self._complete_capturing_stdout("json"))

    def test_a_terminal_run_writes_no_marker(self):
        self.assertNotIn(FLUSH_SENTINEL, self._complete_capturing_stdout(""))

    def test_the_wait_notice_is_inside_the_message_the_flush_sends(self):
        # Printed after the flush it would be stranded in the buffer until the answer
        # arrived, which is the one message it exists to precede (§8).
        out = self._complete_capturing_stdout("json")
        self.assertLess(out.index("Working on it"), out.index(FLUSH_SENTINEL))


class TestWaitNotice(unittest.TestCase):
    """What the athlete is told before the call goes quiet
    (DESIGN_output_verbosity.md §8)."""

    def setUp(self):
        self.client = OpenRouterClient()
        self.client.model = "openai/gpt-5.4"

    def _announce(self, frontend: str, samples, *, notice: bool = True) -> str:
        """Everything `_announce_wait` printed, with `journal.llm_durations` answering
        `samples` (ms) for every lookup."""
        buf = io.StringIO()
        with patch.dict(os.environ, {"TRAINMATE_FRONTEND": frontend,
                                     "TRAINMATE_VERBOSE": ""}, clear=False), \
                patch("sys.stdout", buf), \
                patch("trainmate.openrouter.journal.llm_durations",
                      return_value=list(samples)):
            self.client._announce_wait("workout_adapt", notice)
        return buf.getvalue()

    def test_chat_is_told_how_long_this_usually_takes(self):
        # 40s and 44s -> a 42s median, rounded to the 40s bucket.
        out = self._announce("json", [40000, 44000])
        self.assertIn("Working on it", out)
        self.assertIn("about 40s", out)

    def test_chat_with_no_history_still_says_there_is_a_wait(self):
        out = self._announce("json", [])
        self.assertIn("Working on it", out)
        self.assertNotIn("about", out)

    def test_a_terminal_carries_the_estimate_on_its_own_narration(self):
        # The terminal already narrates progress, so the number rides along there
        # rather than adding a second line saying the same thing.
        out = self._announce("", [40000, 44000])
        self.assertIn("Querying OpenRouter", out)
        self.assertIn("~40s", out)
        self.assertNotIn("Working on it", out)

    def test_a_call_nobody_watches_says_nothing_to_the_chat(self):
        out = self._announce("json", [40000, 44000], notice=False)
        self.assertNotIn("Working on it", out)

    def test_one_sample_is_not_history(self):
        # A single past call is as likely to be an outlier as a typical one.
        self.assertIsNone(self._wait_estimate([40000]))

    def _wait_estimate(self, samples):
        with patch("trainmate.openrouter.journal.llm_durations",
                   return_value=list(samples)):
            return self.client._wait_estimate("workout_adapt")

    def test_the_median_ignores_the_one_call_that_crawled(self):
        self.assertEqual(self._wait_estimate([40000, 42000, 600000]), 42.0)

    def test_a_model_with_no_history_falls_back_to_the_command(self):
        calls = []

        def _durations(label, model=None, **kwargs):
            calls.append(model)
            return [] if model else [50000, 50000]

        with patch("trainmate.openrouter.journal.llm_durations", side_effect=_durations):
            self.assertEqual(self.client._wait_estimate("workout_adapt"), 50.0)
        self.assertEqual(calls, ["openai/gpt-5.4", None])

    def test_an_unreadable_journal_costs_the_number_and_not_the_call(self):
        buf = io.StringIO()
        with patch.dict(os.environ, {"TRAINMATE_FRONTEND": "json"}, clear=False), \
                patch("sys.stdout", buf), \
                patch("trainmate.openrouter.journal.llm_durations",
                      side_effect=OSError("no logs")):
            self.client._announce_wait("workout_adapt", True)
        self.assertIn("Working on it", buf.getvalue())


class TestHumanWait(unittest.TestCase):
    """The rounding: a median of past runs is a "roughly", and must read as one."""

    def test_seconds_round_to_five(self):
        self.assertEqual(_human_wait(37), "35s")
        self.assertEqual(_human_wait(43), "45s")

    def test_a_very_fast_call_never_reads_as_zero(self):
        self.assertEqual(_human_wait(1.2), "5s")

    def test_over_ninety_seconds_reads_in_minutes(self):
        self.assertEqual(_human_wait(100), "1.5 minutes")
        self.assertEqual(_human_wait(210), "3.5 minutes")
        self.assertEqual(_human_wait(240), "4 minutes")


if __name__ == "__main__":
    unittest.main()
