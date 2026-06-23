import requests
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional
from trainmate.config import config

class OpenRouterClient:
    """Client for communicating with the OpenRouter LLM API."""

    def __init__(self) -> None:
        """Initializes API endpoint and model from configuration."""
        self.api_url: str = "https://openrouter.ai/api/v1/chat/completions"
        self.model: str = config.openrouter_model

    def _log_exchange(
        self,
        label: str,
        system_content: str,
        user_content: str,
        response_data: Optional[dict[str, Any]] = None,
        error_msg: Optional[str] = None
    ) -> None:
        """Writes the LLM exchange to a markdown file in the logs directory."""
        try:
            logs_dir = config.llm_logs_dir
            os.makedirs(logs_dir, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{timestamp}_{label}.md"
            filepath = os.path.join(logs_dir, filename)
            
            usage_str = "N/A"
            if response_data:
                usage = response_data.get("usage", {})
                if usage:
                    usage_str = (
                        f"Prompt: {usage.get('prompt_tokens')}, "
                        f"Completion: {usage.get('completion_tokens')}, "
                        f"Total: {usage.get('total_tokens')}"
                    )
            
            content_str = ""
            if response_data:
                choices = response_data.get("choices", [])
                if choices:
                    content_str = choices[0].get("message", {}).get("content", "")
            
            lines = [
                f"# LLM Exchange: {label.replace('_', ' ').title()}",
                f"- **Timestamp**: {datetime.now(timezone.utc).isoformat()}",
                f"- **Model**: {self.model}",
                f"- **Token Usage**: {usage_str}",
                ""
            ]
            
            if error_msg:
                lines.extend([
                    "## ERROR STATUS",
                    "```",
                    error_msg,
                    "```",
                    ""
                ])
                
            lines.extend([
                "## System Prompt",
                "<details>",
                "<summary>Click to expand system prompt</summary>",
                "",
                system_content,
                "</details>",
                "",
                "## User Content",
                "```",
                user_content,
                "```",
                ""
            ])
            
            if content_str:
                lines.extend([
                    "## Raw Response (JSON)",
                    "```json",
                    content_str,
                    "```"
                ])
                
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
                
            print(f"Logged LLM exchange to: {filepath}")
        except Exception as e:
            print(f"Warning: Failed to log LLM exchange: {e}")

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        """Parses the model's JSON response, tolerating common chatty output.

        Strips an optional ```json ... ``` markdown fence and ignores any
        trailing data after the first complete JSON value, so responses that
        append prose or a stray fence don't abort the whole exchange.
        """
        text = content.strip()
        if text.startswith("```"):
            # Drop the opening fence (e.g. ```json) and trailing fence.
            text = text.split("\n", 1)[-1] if "\n" in text else text
            if text.endswith("```"):
                text = text[: -len("```")]
            text = text.strip()
        # raw_decode parses the first JSON value and ignores trailing data.
        obj, _ = json.JSONDecoder().raw_decode(text)
        return obj

    def complete(
        self, system_content: str, user_content: str, label: str = "exchange"
    ) -> dict[str, Any]:
        """Sends a request to OpenRouter with system and user prompts.

        Expects a structured JSON object response from the LLM.

        Args:
            system_content: Large context / rules placed in system role prompt.
            user_content: Immediate instruction or data payload for the LLM.
            label: Descriptive name of the action being logged.

        Returns:
            The parsed JSON response dictionary from the model.

        Raises:
            ValueError: If the OpenRouter API Key is missing or response is empty.
            requests.exceptions.HTTPError: If HTTP error occurs during requests.
        """
        if getattr(self, "show_prompt_only", False):
            import sys
            print("=== SYSTEM PROMPT ===")
            print(system_content)
            print("\n=== USER PROMPT ===")
            print(user_content)
            sys.exit(0)

        api_key = config.openrouter_api_key
        if not api_key:
            raise ValueError(
                "OpenRouter API key is not configured. Please set the "
                "OPENROUTER_API_KEY env variable or update config.yaml."
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/google-deepmind/antigravity",
            "X-Title": "TrainMate Coach"
        }

        # Structure messages for prompt caching: large static content in system prompt
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content}
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"}
        }

        response = None
        resp_data = None
        try:
            print(f"Querying OpenRouter with model: {self.model}")
            response = requests.post(self.api_url, headers=headers, json=payload, timeout=45)
            response.raise_for_status()
            resp_data = response.json()
            
            # Print token usage details for prompt caching verification
            usage = resp_data.get("usage", {})
            print(
                f"OpenRouter Tokens - Prompt: {usage.get('prompt_tokens')}, "
                f"Completion: {usage.get('completion_tokens')}, "
                f"Total: {usage.get('total_tokens')}"
            )
            
            # Log successful exchange
            self._log_exchange(label, system_content, user_content, response_data=resp_data)
            
            choices = resp_data.get("choices", [])
            if not choices:
                raise ValueError("Empty completion returned from OpenRouter.")
                
            content = choices[0].get("message", {}).get("content", "")
            return self._parse_json_content(content)

        except requests.exceptions.HTTPError as he:
            print(f"HTTP Error calling OpenRouter: {he}")
            resp_text = response.text if response is not None else ""
            if resp_text:
                print(f"Response Body: {resp_text}")
            err_msg = f"HTTP Error: {he}\nResponse: {resp_text}"
            self._log_exchange(label, system_content, user_content, error_msg=err_msg)
            raise he
        except Exception as e:
            print(f"Error calling OpenRouter completions: {e}")
            self._log_exchange(
                label, system_content, user_content,
                response_data=resp_data, error_msg=str(e)
            )
            raise e

# Singleton instance
openrouter_client = OpenRouterClient()
