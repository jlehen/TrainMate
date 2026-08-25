"""Front-end-agnostic prompt broker.

Commands ask the athlete yes/no, one-of-N, or free-text questions through a
``Prompt`` rather than calling ``input()`` directly, so the same handler works on
a TTY, over the Telegram bot, or any future front-end. The active implementation
is chosen at startup by ``make_prompt()`` from the ``TRAINMATE_FRONTEND`` env var
and exposed as the patchable ``trainmate_cli.prompt`` singleton; handlers reach it
as ``cli.prompt``.

Two transports ship here:

* ``TtyPrompt`` — the original behaviour: ``input()`` with ``[y/N]`` rendering,
  EOF falling back to the supplied default (this is what keeps piped/cron runs
  aborting cleanly).
* ``JsonPrompt`` — non-blocking *for the front-end*: it writes one structured
  request line to ``out`` (sentinel-framed JSON, see ``PROMPT_SENTINEL``) and
  blocks reading a single response line from ``inp``. The process stays alive,
  parked on its stdin read, while the front-end renders buttons and waits for the
  human. A response of ``{"cancelled": true}`` (an explicit ``/cancel`` or an idle
  timeout) raises ``PromptCancelled``, which the CLI dispatcher turns into a clean
  abort.

The module imports nothing beyond the stdlib so it stays unit-testable in
isolation (feed ``JsonPrompt`` a pair of ``io.StringIO``-like streams).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

# Wire framing for JsonPrompt requests. A request is one line on stdout:
#   \x1eTM-PROMPT {json}\n
# The \x1e (ASCII record-separator) sentinel never appears in normal CLI output,
# so a front-end can tell control traffic from prose regardless of what a command
# prints. The matching answer comes back as one JSON line on the process's stdin.
PROMPT_SENTINEL = "\x1eTM-PROMPT "
PROMPT_PROTOCOL_VERSION = 1

# Sibling one-way sentinel: a command that rendered a chart tells the front-end
# where the PNG lives (no answer expected, unlike TM-PROMPT). Framing is the
# same \x1e-prefixed single line, so a front-end that doesn't recognise it can
# drop it instead of forwarding raw protocol bytes as chat text
# (DESIGN_progress_timeline.md §7.2).
PHOTO_SENTINEL = "\x1eTM-PHOTO "


def emit_photo(path: str, caption: Optional[str] = None, out=None) -> None:
    """Writes one sentinel-framed photo-ready line: ``\\x1eTM-PHOTO {json}``,
    ``{"path": ..., "caption": ...}``. The caption travels in the payload
    because the front-end has no other way to know it — it must not re-parse
    forwarded chat text (§7.2)."""
    if out is None:
        out = sys.stdout
    out.write(PHOTO_SENTINEL + json.dumps({"path": path, "caption": caption}) + "\n")
    out.flush()


# Third sentinel: a NON-blocking inline-button row attached to the output just
# flushed. Prompts ask and block; buttons offer and exit — the CLI keeps deciding
# WHAT to offer, the front-end only renders (DESIGN_bot_simple_frontend.md §4.4).
BUTTONS_SENTINEL = "\x1eTM-BUTTONS "


def emit_buttons(buttons: Sequence[dict], out=None) -> None:
    """Writes one sentinel-framed button-row line: ``\\x1eTM-BUTTONS {json}``,
    ``{"buttons": [...]}``. Each button is ``{"label": ...}`` plus exactly one of:
    ``send`` (a canned utterance the front-end feeds back through its normal command
    pipeline when tapped), ``ack`` (a short reply text; nothing runs), or ``menu`` (a
    nested list of send/ack buttons) — DESIGN_bot_simple_frontend.md §4.4."""
    if out is None:
        out = sys.stdout
    out.write(BUTTONS_SENTINEL + json.dumps({"buttons": list(buttons)}) + "\n")
    out.flush()


class PromptCancelled(Exception):
    """Raised when the front-end cancels an in-flight prompt (``/cancel`` or idle
    timeout), or when the answer channel closes.

    An ordinary exception. It inherited ``BaseException`` only so the handlers' broad
    ``except Exception`` nets could not mistake a deliberate abort for a command error;
    those nets are gone, and `trainmate_cli.main` catches this before its own boundary
    and reports a clean cancellation. A test asserts no `except Exception` block
    encloses a prompt call, which is the condition that made this safe."""


@dataclass(frozen=True)
class Choice:
    """One option in a ``choose()`` prompt: a stable ``value`` sent back as the
    answer and a human ``label`` rendered as the button / menu entry."""
    value: str
    label: str


class TtyPrompt:
    """Interactive-terminal transport — ``input()`` with the classic rendering.

    EOF (piped stdin, cron) falls back to the supplied default so unattended runs
    abort cleanly instead of raising."""

    def confirm(self, message: str, *, default: bool = False,
                danger: bool = False) -> bool:
        suffix = " [Y/n]: " if default else " [y/N]: "
        try:
            ans = input(message + suffix).strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        return ans in ("y", "yes")

    def choose(self, message: str, choices: Sequence[Choice], *,
               default: Optional[str] = None) -> str:
        print(message)
        for i, c in enumerate(choices, 1):
            marker = " (default)" if c.value == default else ""
            print(f"  [{i}] {c.label}{marker}")
        try:
            ans = input("Choice: ").strip().lower()
        except EOFError:
            ans = ""
        if not ans:
            return default if default is not None else choices[0].value
        if ans.isdigit():
            idx = int(ans) - 1
            if 0 <= idx < len(choices):
                return choices[idx].value
        for c in choices:
            if ans == c.value.lower() or ans == c.label.lower():
                return c.value
        return default if default is not None else choices[0].value

    def ask_text(self, message: str, *, secret: bool = False,
                 default: Optional[str] = None) -> str:
        try:
            if secret:
                import getpass
                ans = getpass.getpass(message + ": ")
            else:
                ans = input(message + ": ")
        except EOFError:
            ans = ""
        ans = ans.strip()
        if not ans and default is not None:
            return default
        return ans


class JsonPrompt:
    """Structured transport for chat/web front-ends.

    Each call emits one sentinel-framed JSON request on ``out`` then blocks until a
    matching JSON answer line arrives on ``inp``. The front-end is responsible for
    rendering the question and writing back ``{"v": 1, "id": ..., "answer": ...}``
    or ``{"v": 1, "id": ..., "cancelled": true}``."""

    def __init__(self, out=None, inp=None) -> None:
        self._out = out if out is not None else sys.stdout
        self._inp = inp if inp is not None else sys.stdin
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"p{self._counter}"

    def _exchange(self, payload: dict) -> dict:
        pid = self._next_id()
        request = {"v": PROMPT_PROTOCOL_VERSION, "id": pid, **payload}
        self._out.write(PROMPT_SENTINEL + json.dumps(request) + "\n")
        self._out.flush()
        while True:
            line = self._inp.readline()
            if not line:  # answer channel closed — front-end is gone
                raise PromptCancelled()
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") != pid:  # stale / out-of-order; keep waiting
                continue
            if resp.get("cancelled"):
                raise PromptCancelled()
            return resp

    def confirm(self, message: str, *, default: bool = False,
                danger: bool = False) -> bool:
        resp = self._exchange({
            "type": "confirm", "message": message,
            "default": default, "danger": danger,
        })
        return bool(resp.get("answer", default))

    def choose(self, message: str, choices: Sequence[Choice], *,
               default: Optional[str] = None) -> str:
        resp = self._exchange({
            "type": "choose", "message": message,
            "choices": [{"value": c.value, "label": c.label} for c in choices],
            "default": default,
        })
        answer = resp.get("answer")
        valid = {c.value for c in choices}
        if answer in valid:
            return answer
        return default if default is not None else choices[0].value

    def ask_text(self, message: str, *, secret: bool = False,
                 default: Optional[str] = None) -> str:
        resp = self._exchange({
            "type": "text", "message": message, "secret": secret,
            "default": default,
        })
        answer = resp.get("answer")
        if answer is None:
            return default if default is not None else ""
        return str(answer)


def is_json_frontend(frontend: Optional[str] = None) -> bool:
    """True when a structured front-end (the Telegram bot) is driving the CLI.

    The one place the ``TRAINMATE_FRONTEND`` env var is interpreted, so the transport
    choice below and the output shapes that vary by front-end (chart delivery, the
    missing-argument error of DESIGN_cli_noargs.md §a) cannot drift apart."""
    if frontend is None:
        frontend = os.environ.get("TRAINMATE_FRONTEND", "")
    return frontend.lower() == "json"


def make_prompt(frontend: Optional[str] = None, out=None, inp=None):
    """Returns the prompt transport for the active front-end.

    ``frontend`` defaults to the ``TRAINMATE_FRONTEND`` env var; ``"json"`` selects
    the structured ``JsonPrompt`` (used by the Telegram bot), anything else the
    interactive ``TtyPrompt``."""
    if is_json_frontend(frontend):
        return JsonPrompt(out, inp)
    return TtyPrompt()
