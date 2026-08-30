import requests
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from trainmate import journal
from trainmate.config import config
from trainmate.prompt import emit_flush
from trainmate.util import aside, warn

# A fenced reply may be one line (```{"a":1}```) or many, with or without a language
# tag; the one-line form has no newline to split on. The `$` anchor deliberately
# captures through to the *last* closing fence, so a reply that fences two attempts
# hands both to the decoder — see `_parse_json_content`.
_FENCED_JSON = re.compile(r"^```[A-Za-z0-9_+-]*\s*(.*?)\s*```\s*$", re.DOTALL)
_OPEN_FENCE = re.compile(r"^```[A-Za-z0-9_+-]*[ \t]*\n?")


class OpenRouterClient:
    """Client for communicating with the OpenRouter LLM API."""

    def __init__(self) -> None:
        """Initializes the API endpoint. The model resolves lazily — see `model`."""
        self.api_url: str = "https://openrouter.ai/api/v1/chat/completions"
        self._model: Optional[str] = None

    @property
    def model(self) -> str:
        """The model to query, resolved on first use rather than at import: resolution reads
        the database, which must not be opened just because this module was imported
        (DESIGN_model_selection.md §3.1). Assigning to it pins a model for this invocation,
        which is how `--llm-model` overrides the stored choice."""
        if self._model is None:
            from trainmate.llm_models import active_model
            self._model = active_model()
        return self._model

    @model.setter
    def model(self, value: str) -> None:
        self._model = value

    def reset_model(self) -> None:
        """Drops the cached model so the next call re-resolves it. The REPL runs many
        commands in one process, so a model change must not leave the old one pinned."""
        self._model = None

    def _log_exchange(
        self,
        label: str,
        system_content: str,
        user_content: str,
        response_data: Optional[dict[str, Any]] = None,
        error_msg: Optional[str] = None
    ) -> Optional[str]:
        """Writes the LLM exchange to a markdown file, returning its path.

        The name carries the run id, so `ls logs/llm_exchanges/*<run>*` is the whole of
        "show me the prompts from that run" (DESIGN_logging.md §6). The id is the join
        and not the timestamp on purpose: these names come from the machine's local
        clock while the journal is UTC, so matching by time would mean reconciling two
        zones on every lookup.
        """
        try:
            logs_dir = config.llm_logs_dir
            os.makedirs(logs_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            run_id = journal.current_id()
            stamp = f"{timestamp}_{run_id}" if run_id else timestamp
            filepath = os.path.join(logs_dir, f"{stamp}_{label}.md")

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
                
            # Both messages are fenced: the prompt carries its own `##`/`###` section
            # markers (DESIGN_prompt_structure.md §2), and unfenced they would render as
            # headings of this log file and read as its structure rather than the prompt's.
            lines.extend([
                "## System Prompt",
                "<details>",
                "<summary>Click to expand system prompt</summary>",
                "",
                "```",
                system_content,
                "```",
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

            aside(f"Logged LLM exchange to: {filepath}")
            return filepath
        except Exception as e:
            # The log failing to log: the one place that belongs in the journal more
            # than anywhere else (DESIGN_logging.md §5.3).
            warn(f"Failed to log LLM exchange: {e}")
            return None

    def _record_call(
        self, label: str, started: float, ok: bool,
        response_data: Optional[dict[str, Any]] = None,
        path: Optional[str] = None, error: Optional[str] = None,
    ) -> None:
        """The `llm.call` journal record for one completion (DESIGN_logging.md §6).

        This is the join that did not exist: from a run you reach its prompts, and from
        a prompt you reach the command that asked for it. The two asides in `complete`
        keep their printed form on a terminal; their content lives here."""
        usage = (response_data or {}).get("usage") or {}
        journal.llm_call(
            label=label, model=self.model,
            ms=int((time.monotonic() - started) * 1000), ok=ok,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            path=path, error=error,
        )

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        """Parses the model's JSON response, tolerating common chatty output.

        Strips an optional ```json ... ``` markdown fence and ignores trailing prose, so a
        reply that appends a comment or a stray fence doesn't abort the exchange. Of two
        or more objects the last wins: a model that answers, sees its answer was partial
        and answers again means the second one.
        """
        text = content.strip()
        fenced = _FENCED_JSON.match(text)
        if fenced:
            text = fenced.group(1)
        elif text.startswith("```"):
            # Opening fence with no closing one: drop the fence and any language tag.
            text = _OPEN_FENCE.sub("", text)
        text = text.strip()
        # raw_decode parses one JSON value and reports where it stopped; an unparseable
        # first value is a failed exchange and raises out of here as it always did.
        decoder = json.JSONDecoder()
        obj, end = decoder.raw_decode(text)
        obj, extra = OpenRouterClient._later_objects(decoder, text, end, obj)
        if extra:
            journal.note(
                f"model returned {extra + 1} JSON objects, kept the last",
                lvl="warn", dropped=extra,
            )
        return obj

    @staticmethod
    def _later_objects(
        decoder: json.JSONDecoder, text: str, end: int, obj: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        """Scans the text after the first value for further JSON objects, returning the
        last one that parses and how many were found past the first.

        Every `{` from `end` on is a candidate, because the abandoned attempt and the
        real one are separated by prose and fences that decode nowhere."""
        found = 0
        while True:
            start = text.find("{", end)
            if start < 0:
                return obj, found
            try:
                candidate, end = decoder.raw_decode(text, start)
            except ValueError:
                # Not the start of a value — a brace in prose. Keep looking past it.
                end = start + 1
                continue
            # A value starting at `{` decodes to a dict or not at all.
            obj = candidate
            found += 1

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
        # One completion is one `llm.call` record, so `logged` now guards the journal
        # record as well as the markdown file: a second one would double-count the
        # call's tokens in `journal --cost`.
        logged = False
        started = time.monotonic()
        try:
            aside(f"Querying OpenRouter with model: {self.model}")
            # Everything printed so far belongs to the setup, not the answer. A chat
            # front-end buffers to a prompt or to exit, so without this the two arrive
            # as one block after a wait of tens of seconds (DESIGN_output_verbosity.md
            # §7). Every LLM command passes through here, so this is the one call site.
            emit_flush()
            response = requests.post(
                self.api_url, headers=headers, json=payload,
                timeout=config.llm_request_timeout,
            )
            response.raise_for_status()
            resp_data = response.json()

            if "error" in resp_data:
                err_obj = resp_data["error"]
                err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                print(f"OpenRouter API error: {err_msg}")
                path = self._log_exchange(
                    label, system_content, user_content,
                    response_data=resp_data, error_msg=f"OpenRouter Error: {err_msg}"
                )
                self._record_call(
                    label, started, False, resp_data, path, f"OpenRouter Error: {err_msg}"
                )
                logged = True
                raise ValueError(f"OpenRouter API error: {err_msg}")

            # Token usage, for prompt-caching verification — a terminal-only aside
            # (DESIGN_output_verbosity.md §3).
            usage = resp_data.get("usage", {})
            aside(
                f"OpenRouter Tokens - Prompt: {usage.get('prompt_tokens')}, "
                f"Completion: {usage.get('completion_tokens')}, "
                f"Total: {usage.get('total_tokens')}"
            )
            
            # Log successful exchange
            path = self._log_exchange(
                label, system_content, user_content, response_data=resp_data
            )
            self._record_call(label, started, True, resp_data, path)
            logged = True

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
            path = self._log_exchange(
                label, system_content, user_content, error_msg=err_msg
            )
            self._record_call(label, started, False, None, path, f"HTTP Error: {he}")
            raise he
        except Exception as e:
            print(f"Error calling OpenRouter completions: {e}")
            if not logged:
                path = self._log_exchange(
                    label, system_content, user_content,
                    response_data=resp_data, error_msg=str(e)
                )
                self._record_call(label, started, False, resp_data, path, str(e))
            raise e

# Singleton instance
openrouter_client = OpenRouterClient()
