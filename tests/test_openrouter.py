import unittest
from unittest.mock import patch, MagicMock
from trainmate.openrouter import OpenRouterClient

class TestOpenRouterClient(unittest.TestCase):

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

        client = OpenRouterClient()
        # Pin the model: an unpinned client resolves it from the database, which this test
        # has no business reaching (DESIGN_model_selection.md §3.1).
        client.model = "openai/gpt-5.4"
        with patch.object(client, "_log_exchange"):
            with self.assertRaises(ValueError) as ctx:
                client.complete("system prompt", "user prompt", label="test")
        
        self.assertIn("Rate limit reached for gpt-5.4", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
