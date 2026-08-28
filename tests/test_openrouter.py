import unittest
from unittest.mock import patch, MagicMock
from trainmate.openrouter import OpenRouterClient


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


if __name__ == "__main__":
    unittest.main()
