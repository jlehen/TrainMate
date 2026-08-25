"""TrainMate Telegram front-end.

A chat shim over the existing CLI: each incoming message is treated as a
TrainMate command line (the leading slash Telegram requires is optional) and run
through ``trainmate_cli.py`` as a subprocess. Driving the real CLI keeps the bot
in permanent parity with every command/flag the CLI gains and isolates each
invocation.

With ``telegram.ui: simple`` the same pipeline gains a companion persona
(DESIGN_bot_simple_frontend.md): a persistent reply keyboard maps buttons onto fixed
argv, unarmed free text goes through an intent router (``tm bot route``), a morning
scheduler spawns ``tm bot morning``, and replies arrive as plain prose
(``TRAINMATE_RENDER=simple``) instead of ``<pre>`` blocks. Expert mode (the default)
is untouched; slash-prefixed text stays the expert path in both modes.

Interactive commands work over chat via a structured-prompt protocol. The CLI is
launched with ``TRAINMATE_FRONTEND=json`` so its prompt broker
(``trainmate.prompt``), instead of blocking on ``input()``, emits a sentinel-framed
JSON request line on stdout and blocks reading the answer from stdin. The bot
keeps that subprocess alive, renders each request as an inline keyboard (confirm /
choose) or an awaited text reply, and writes the athlete's answer back to stdin so
the command resumes. ``/cancel`` (or an idle timeout) sends a cancellation the CLI
turns into a clean abort. State for the single in-flight command per chat lives in
``_Session``; a per-prompt ``nonce`` rejects stale button taps.

The pure helpers (parse/format/auth/prompt-encoding) are import-safe without
``python-telegram-bot`` so they can be unit-tested; the library is imported lazily
inside ``main``.

``tm-bot`` runs this module twice removed: it's a supervisor loop that relaunches
a worker (this process) whenever the worker exits with ``RESTART_EXIT_CODE`` —
which is exactly what ``/restart`` does. This module doesn't know it's being
supervised; it just exits with that code. See ``DESIGN_bot_restart.md``.

Because a restart needs the athlete to be able to reach ``/restart`` at all, and
because the athlete's answer to an open prompt has to arrive live, Telegram
polling (``getUpdates``) is only paused while a command is silently computing
with no prompt open — never while idle or while a prompt is awaiting an answer.
See ``_pause_polling``/``_resume_polling`` in ``main``.

Run with: ``./tm-bot`` (or ``venv/bin/python trainmate_bot.py``). Configure the
token + allowlist under a ``telegram:`` block in config.yaml (see
config_template.yaml).
"""
import asyncio
import datetime
import html
import json
import os
import secrets
import shlex
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from trainmate.config import config
from trainmate.prompt import (
    PROMPT_SENTINEL, PROMPT_PROTOCOL_VERSION, PHOTO_SENTINEL, BUTTONS_SENTINEL,
)
from trainmate.util import cmd, strip_ansi

# Telegram caps a message at 4096 chars; we wrap replies in <pre>…</pre> (7 chars
# of overhead) and want headroom, so chunk the body well under the hard limit.
MAX_MESSAGE_CHARS = 3800

CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trainmate_cli.py")

# Exit code that tells the tm-bot supervisor to relaunch us (rather than exit for
# good). Arbitrary, borrowed from EX_TEMPFAIL in sysexits.h — just needs to not
# collide with Python's own exit code for uncaught exceptions (1). Must match the
# supervisor's RESTART_EXIT_CODE in tm-bot.
RESTART_EXIT_CODE = 75

# Bounds every blocking step of a /restart teardown (an open prompt's subprocess
# exiting cleanly, then the long-poll closing): nothing may keep us from reaching
# the exit code above. See DESIGN_bot_restart.md §5.2.
RESTART_GRACE_SECONDS = 2.0

WELCOME = (
    "TrainMate is connected. Send any CLI command — the leading slash is "
    "optional.\n\n"
    "Examples:\n"
    "  /status\n"
    "  /workout list --weeks 1\n"
    "  /workout adapt -m \"tired today\"\n"
    "  /plan show\n\n"
    "Send /help for the full command list, or /help <command> (e.g. "
    "/help workout) for a command's options.\n\n"
    "When a command needs a decision (apply a plan, confirm a wipe) I'll show "
    "buttons — tap one, or send /cancel to abort."
)

# Top-level command families surfaced in Telegram's command menu (set_my_commands).
# Kept in sync by hand with the CLI's subparsers; purely cosmetic (any command
# still works whether or not it's listed here).
MENU_COMMANDS = [
    ("status", "Athlete status, goals, recent metrics"),
    ("progress", "Training progress timeline (add --chart for a PNG)"),
    ("workout", "List/generate/adapt/swap workouts"),
    ("plan", "Show/generate periodization plans"),
    ("goal", "Manage training goals"),
    ("data", "Pull/show Garmin metrics & activities"),
    ("signal", "Author daily signals"),
    ("learnings", "Inspect coach learnings"),
    ("constraint", "Manage directives the coach works around"),
    ("model", "List/choose the LLM model"),
    ("cancel", "Abort the command awaiting your answer"),
    ("restart", "Restart the bot process (picks up new code)"),
    ("help", "Show command help"),
]

# --- Simple mode ("companion") tables — DESIGN_bot_simple_frontend.md §5 ---
# Pure data beside MENU_COMMANDS: import-safe and unit-testable without the
# telegram library, like the helpers below.

SIMPLE_WELCOME = (
    "Hi! I'm your training coach 🏃\n\n"
    "Use the buttons below:\n"
    "📅 Today — today's session\n"
    "🗓 My week — the days ahead\n"
    "📈 Progress — how your fitness is building\n"
    "💬 Tell my coach — pass something on (tired, busy, sore…)\n\n"
    "Or just type what you want, in your own words."
)

SIMPLE_HELP = SIMPLE_WELCOME

CAPTURE_PROMPT = "I'm listening — what should I know? (or /cancel)"

ROUTER_FALLBACK = (
    "I didn't quite get that 🤔 — try one of the buttons below, or say it another way."
)

# Reply-keyboard label → fixed argv; None arms free-text capture (§5.1/§5.2).
# Buttons never reach beyond this table.
SIMPLE_KEYBOARD = [
    ("📅 Today", ["workout", "list", "-d", "today"]),
    ("🗓 My week", ["workout", "list"]),
    ("📈 Progress", ["progress", "--chart"]),
    ("💬 Tell my coach", None),
]

# The router's intent → argv table (§5.3): the model (via `tm bot route`) only picks
# an intent from trainmate.cli.bot.ROUTER_INTENTS; this table owns the argv, so a
# hostile or confused message cannot reach flags it doesn't expose. coach_message
# carries the athlete's original text; help/unclear are answered by the bot itself.
ROUTER_INTENT_ARGV = {
    "show_today": ["workout", "list", "-d", "today"],
    "show_week": ["workout", "list"],
    "show_progress": ["progress", "--chart"],
}

# One short italic echo per routed intent, so the athlete learns the vocabulary and a
# misroute is visible immediately (§5.3, open question 1: always shown).
ROUTER_ECHO = {
    "show_today": "showing today",
    "show_week": "showing your week",
    "show_progress": "showing your progress",
    "coach_message": "passing that on to your coach",
}

# Simple mode trims the Telegram command menu to what the athlete needs; every CLI
# command still works when typed with a leading slash.
SIMPLE_MENU_COMMANDS = [
    ("cancel", "Stop what's running"),
    ("help", "What can I ask?"),
]

# Seconds a `bot route` classification may take before the tap falls back to
# 'unclear' — a router that hangs must not wedge the chat.
ROUTER_TIMEOUT_SECONDS = 30


def keyboard_action(text: str) -> Optional[Tuple[str, Optional[List[str]]]]:
    """What a simple-keyboard tap maps to: ("run", argv), ("capture", None), or None
    when the text isn't a keyboard label."""
    stripped = (text or "").strip()
    for label, argv in SIMPLE_KEYBOARD:
        if stripped == label:
            return ("run", list(argv)) if argv else ("capture", None)
    return None


def next_push_delay(
    now: "datetime.datetime", morning: str = "08:00", deadline: str = "15:00"
) -> float:
    """Seconds until `bot morning` should next run: 0 inside today's
    [morning, deadline] window, else the wait to the window's next opening
    (DESIGN_bot_simple_frontend.md §4.3). Unparseable times fall back to the
    defaults; a deadline before the send time means no catch-up window."""
    def _parse(raw: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
        try:
            hours, minutes = (raw or "").strip().split(":")
            hours, minutes = int(hours), int(minutes)
        except (ValueError, AttributeError):
            return fallback
        if 0 <= hours < 24 and 0 <= minutes < 60:
            return hours, minutes
        return fallback

    send_h, send_m = _parse(morning, (8, 0))
    dead_h, dead_m = _parse(deadline, (15, 0))
    start = now.replace(hour=send_h, minute=send_m, second=0, microsecond=0)
    end = now.replace(hour=dead_h, minute=dead_m, second=0, microsecond=0)
    if end < start:
        end = start
    if now < start:
        return (start - now).total_seconds()
    if now <= end:
        return 0.0
    return (start + datetime.timedelta(days=1) - now).total_seconds()


def parse_message_to_argv(text: str, bot_username: Optional[str] = None) -> Optional[List[str]]:
    """Turns a raw chat message into a CLI argv list, or None if there's nothing to run.

    The leading ``/`` Telegram puts on commands is stripped, as is the ``@botname``
    suffix it appends in group chats. Bare ``help`` is passed through to the CLI's own
    ``help`` command (the full command/sub-command tree); ``help <cmd>`` is rewritten to
    the argparse-native ``<cmd> --help`` for that command's options. Raises
    ``ValueError`` on unbalanced quotes (so the caller can report it)."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("/"):
        text = text[1:]
    argv = shlex.split(text)
    if not argv:
        return None
    # Strip a '@botname' suffix Telegram adds to the command token in groups.
    head = argv[0]
    if "@" in head:
        name, _, suffix = head.partition("@")
        if bot_username is None or suffix.lower() == bot_username.lower():
            argv[0] = name
    # Bare 'help' runs the CLI's own help command (full tree); 'help <cmd>' maps
    # onto argparse's --help for that one command.
    if argv[0].lower() == "help":
        rest = argv[1:]
        return rest + ["--help"] if rest else ["help"]
    return argv


def is_authorized(chat_id: int, allowed_ids: List[int]) -> bool:
    """True only when chat_id is on the allowlist. Empty allowlist authorizes no one."""
    return chat_id in allowed_ids


def parse_prompt_request(line: str) -> Optional[dict]:
    """Decodes a sentinel-framed prompt request line, or None if it isn't one.

    The CLI's JsonPrompt writes ``\\x1eTM-PROMPT {json}`` on its own stdout line; any
    other line is ordinary command output."""
    if not line.startswith(PROMPT_SENTINEL):
        return None
    try:
        return json.loads(line[len(PROMPT_SENTINEL):])
    except json.JSONDecodeError:
        return None


def parse_photo_request(line: str) -> Optional[dict]:
    """Decodes a sentinel-framed photo-ready line, or None if it isn't one.

    `trainmate.prompt.emit_photo` writes ``\\x1eTM-PHOTO {json}`` (fields
    ``path``/``caption``) on its own stdout line — the CLI's ``--chart`` path
    (DESIGN_progress_timeline.md §7.2)."""
    if not line.startswith(PHOTO_SENTINEL):
        return None
    try:
        return json.loads(line[len(PHOTO_SENTINEL):])
    except json.JSONDecodeError:
        return None


def parse_buttons_request(line: str) -> Optional[dict]:
    """Decodes a sentinel-framed non-blocking button row, or None if it isn't one.

    `trainmate.prompt.emit_buttons` writes ``\\x1eTM-BUTTONS {json}`` (field
    ``buttons``) on its own stdout line — unlike TM-PROMPT the CLI exits without
    waiting; each button carries a canned follow-up the bot feeds back through the
    normal pipeline when tapped (DESIGN_bot_simple_frontend.md §4.4)."""
    if not line.startswith(BUTTONS_SENTINEL):
        return None
    try:
        return json.loads(line[len(BUTTONS_SENTINEL):])
    except json.JSONDecodeError:
        return None


# Any other \x1e-prefixed sentinel a future CLI version might emit: recognised
# framing but not (yet) understood by this bot build. Dropped rather than
# forwarded as chat text, so a stale bot degrades to a silently-missing
# feature instead of leaking raw protocol bytes (§7.2).
_SENTINEL_PREFIX = "\x1e"


def ui_callback_data(token: str, path: str) -> str:
    """callback_data for a TM-BUTTONS button: ``"ui:{token}:{path}"``. The ``ui:``
    namespace keeps these taps apart from prompt answers; the token invalidates rows
    replaced by a newer push; the path indexes into the stored payload ("2", "2.1")."""
    return f"ui:{token}:{path}"


def decode_ui_callback(data: str) -> Optional[Tuple[str, str]]:
    """Splits ``"ui:{token}:{path}"`` back into (token, path), or None if it isn't a
    ui-namespace callback or is malformed."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "ui" or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def resolve_ui_action(buttons: List[Any], path: str) -> Optional[dict]:
    """The button dict a callback path names: "2" is buttons[2], "2.1" entry 1 of its
    menu. None when the path doesn't resolve (malformed, stale, or hostile data)."""
    steps = path.split(".")
    if len(steps) > 2:
        return None
    try:
        node = buttons[int(steps[0])]
        if len(steps) == 2:
            node = (node.get("menu") or [])[int(steps[1])]
    except (ValueError, IndexError, AttributeError, TypeError):
        return None
    return node if isinstance(node, dict) else None


def ui_button_rows(buttons: List[dict], token: str) -> List[List[Tuple[str, str]]]:
    """Top-level TM-BUTTONS layout: one row across, like the §4.1 mock."""
    return [[(b.get("label", ""), ui_callback_data(token, str(i)))
             for i, b in enumerate(buttons)]]


def ui_menu_rows(menu: List[dict], token: str, parent: str) -> List[List[Tuple[str, str]]]:
    """A tapped `menu` button's sub-choices: one per row, like a choose prompt."""
    return [[(b.get("label", ""), ui_callback_data(token, f"{parent}.{i}"))]
            for i, b in enumerate(menu)]


def prompt_buttons(req: dict, nonce: str) -> List[List[Tuple[str, str]]]:
    """Inline-keyboard layout for a prompt request: rows of ``(label, callback_data)``.

    callback_data is ``"{nonce}:{prompt_id}:{value}"`` — well under Telegram's 64-byte
    cap, with the nonce letting the bot reject taps from a stale/replaced session. A
    ``text`` prompt has no buttons (the athlete just replies), so this returns []."""
    pid = req.get("id", "")
    ptype = req.get("type")
    if ptype == "confirm":
        yes = "⚠️ Confirm" if req.get("danger") else "✅ Yes"
        return [[(yes, f"{nonce}:{pid}:y"), ("✖️ No", f"{nonce}:{pid}:n")]]
    if ptype == "choose":
        return [[(c["label"], f"{nonce}:{pid}:{c['value']}")]
                for c in req.get("choices", [])]
    return []


def decode_callback(data: str) -> Optional[Tuple[str, str, str]]:
    """Splits ``"{nonce}:{prompt_id}:{value}"`` back into its parts, or None if malformed."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def format_prompt_message(req: dict) -> str:
    """The plain-text question shown above a prompt's buttons (ANSI stripped)."""
    return strip_ansi(req.get("message", "")).strip()


def chunk_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> List[str]:
    """Splits text into <=limit-char chunks, preferring line boundaries.

    A single over-long line is hard-split. Always returns at least one chunk."""
    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            head, line = line[:limit], line[limit:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    chunks.append(current)
    return chunks


def format_reply(text: str, simple: bool = False) -> List[str]:
    """Renders CLI output as one or more Telegram HTML messages.

    Expert form wraps each chunk in <pre> so column alignment survives; simple mode
    sends plain escaped prose the client flows naturally
    (DESIGN_bot_simple_frontend.md §6)."""
    if simple:
        return [html.escape(chunk) for chunk in chunk_text(text)]
    return [f"<pre>{html.escape(chunk)}</pre>" for chunk in chunk_text(text)]


def _cli_env(wrap_width: Optional[int], simple: bool = False) -> Dict[str, str]:
    """Environment for a bot-driven CLI subprocess: structured prompts, no colour,
    unbuffered I/O (so prompt requests arrive before the child blocks on stdin), and
    the narrow wrap width phones want. `simple` opts commands into the companion
    rendering (DESIGN_bot_simple_frontend.md §6)."""
    env = dict(os.environ)
    env["TRAINMATE_FRONTEND"] = "json"
    env["NO_COLOR"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if simple:
        env["TRAINMATE_RENDER"] = "simple"
    if wrap_width:
        env["TRAINMATE_WRAP_WIDTH"] = str(wrap_width)
    return env


class _Session:
    """One in-flight command for a chat: the live CLI subprocess plus the state
    needed to route a pending prompt's answer back to it."""

    def __init__(self, chat_id: int, proc, nonce: str, quiet: bool = False) -> None:
        self.chat_id = chat_id
        self.proc = proc
        self.nonce = nonce
        self.awaiting: Optional[dict] = None      # the prompt request awaiting an answer
        self.answer_future: Optional["asyncio.Future"] = None
        self.task: Optional["asyncio.Task"] = None
        self.sent = False                         # whether anything was sent to the chat
        self.quiet = quiet                        # scheduler-run: silence "(no output)"
        self.last_message_id: Optional[int] = None  # anchor for a TM-BUTTONS row


async def _exited_within_grace(proc) -> bool:
    """True if proc exited on its own within RESTART_GRACE_SECONDS."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=RESTART_GRACE_SECONDS)
        return True
    except asyncio.TimeoutError:
        return False


async def restart_teardown(session: Optional[_Session], stop_polling) -> None:
    """Ends any live command and closes the Telegram long-poll, so /restart's hard exit
    strands neither an orphaned subprocess nor an unconfirmed getUpdates offset.

    Both halves are bounded and failure-tolerant: reaching os._exit(RESTART_EXIT_CODE)
    matters more than a tidy teardown. See DESIGN_bot_restart.md §5.2."""
    if session is not None and session.proc.returncode is None:
        fut = session.answer_future
        answered = session.awaiting is not None and fut is not None and not fut.done()
        if answered:
            fut.set_result({"v": PROMPT_PROTOCOL_VERSION, "id": session.awaiting.get("id"),
                            "cancelled": True})
        if not answered or not await _exited_within_grace(session.proc):
            try:
                session.proc.kill()
            except ProcessLookupError:
                pass
    try:
        await asyncio.wait_for(stop_polling(), timeout=RESTART_GRACE_SECONDS)
    except Exception as e:
        print(f"restart: could not stop polling cleanly: {e!r}", flush=True)


def main() -> None:
    """Starts the long-polling Telegram bot. Blocks until interrupted."""
    token = config.telegram_bot_token
    if not token:
        sys.exit(
            "No Telegram bot token configured. Set TELEGRAM_BOT_TOKEN or add a "
            "telegram.bot_token to config.yaml (see config_template.yaml)."
        )
    allowed_ids = config.telegram_allowed_chat_ids
    if not allowed_ids:
        print(
            "Warning: telegram.allowed_chat_ids is empty — the bot will refuse every "
            "message. Add your numeric chat id to authorize yourself.",
            file=sys.stderr,
        )
    prompt_timeout = config.telegram_prompt_timeout
    command_timeout = config.telegram_command_timeout
    # Simple mode sends prose the client flows itself, so hard-wrapping at phone
    # width would only add ragged mid-sentence breaks — wrap far past any real line
    # instead (DESIGN_bot_simple_frontend.md §6).
    simple_ui = config.telegram_ui == "simple"
    wrap_width = 900 if simple_ui else config.telegram_wrap_width

    try:
        from telegram import (
            BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
            ReplyKeyboardMarkup, Update,
        )
        from telegram.constants import ParseMode
        from telegram.ext import (
            Application, CallbackQueryHandler, MessageHandler, filters,
        )
    except ImportError:
        sys.exit(
            "python-telegram-bot is not installed. Run: "
            + cmd("venv/bin/pip install -r requirements.txt", quote=False)
        )

    sessions: Dict[int, _Session] = {}
    # Simple-mode chat state: chats armed for "Tell my coach" capture (chat_id →
    # monotonic arm time, cleared after one message, /cancel or the prompt timeout,
    # §5.2), and the live TM-BUTTONS payload per chat (chat_id → (token, buttons),
    # valid until replaced by the next push, §4.4).
    armed: Dict[int, float] = {}
    ui_actions: Dict[int, Tuple[str, List[dict]]] = {}

    # The persistent §5.1 reply keyboard, 2×2. Re-asserted on every simple-mode
    # message so the athlete can never lose it.
    reply_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(SIMPLE_KEYBOARD[0][0]), KeyboardButton(SIMPLE_KEYBOARD[1][0])],
         [KeyboardButton(SIMPLE_KEYBOARD[2][0]), KeyboardButton(SIMPLE_KEYBOARD[3][0])]],
        resize_keyboard=True, is_persistent=True,
    ) if simple_ui else None

    def _log(chat_id: int, direction: str, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"{ts} [{chat_id}] {direction} {msg}", flush=True)

    application = Application.builder().token(token).post_init(_post_init).build()
    bot = application.bot
    updater = application.updater

    # Polling (getUpdates) runs continuously except while a command subprocess is
    # silently computing with no prompt open — see the module docstring and
    # DESIGN_bot_restart.md §5.1. _drive() calls these around that phase; the lock
    # just guards against overlapping pause/resume calls, since start_polling()/
    # stop() aren't safe to double-call concurrently.
    _polling_lock = asyncio.Lock()
    restarting = False  # latched by /restart: nothing may reopen the long-poll (§5.2)

    async def _pause_polling() -> None:
        async with _polling_lock:
            if updater.running:
                await updater.stop()

    async def _resume_polling() -> None:
        async with _polling_lock:
            if restarting or updater.running:
                return
            await updater.start_polling(allowed_updates=Update.ALL_TYPES)

    async def _flush_output(session: "_Session", buf: List[str]) -> None:
        text = "\n".join(buf).strip()
        if not text:
            return
        session.sent = True
        for part in format_reply(text, simple=simple_ui):
            sent = await bot.send_message(
                chat_id=session.chat_id, text=part, parse_mode=ParseMode.HTML,
                reply_markup=reply_keyboard,
            )
            session.last_message_id = sent.message_id
        _log(session.chat_id, "<<", f"{text.count(chr(10)) + 1} line(s)")

    async def _send_ui_buttons(session: "_Session", req: dict) -> None:
        """Attaches a TM-BUTTONS row to the output just flushed (§4.4). Non-blocking:
        the CLI has already moved on; the payload is stored per chat and taps feed the
        canned utterance back through the normal pipeline. Falls back to its own
        message when there is nothing to anchor to."""
        buttons = [b for b in (req.get("buttons") or []) if isinstance(b, dict)]
        if not buttons:
            return
        token = secrets.token_hex(3)
        ui_actions[session.chat_id] = (token, buttons)
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
             for row in ui_button_rows(buttons, token)]
        )
        session.sent = True
        if session.last_message_id is not None:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=session.chat_id, message_id=session.last_message_id,
                    reply_markup=keyboard,
                )
                _log(session.chat_id, "<<", f"{len(buttons)} ui button(s)")
                return
            except Exception:
                pass  # older client / edited race — degrade to a fresh message
        await bot.send_message(
            chat_id=session.chat_id, text="👇", reply_markup=keyboard,
        )
        _log(session.chat_id, "<<", f"{len(buttons)} ui button(s)")

    async def _send_photo(session: "_Session", req: dict) -> None:
        """Sends the chart PNG a `--chart` run pointed at, then unlinks the temp
        file regardless of send outcome (§7.2) — the CLI wrote it with
        `delete=False` specifically so the bot owns cleanup."""
        path = req.get("path")
        caption = req.get("caption")
        session.sent = True
        try:
            with open(path, "rb") as f:
                await bot.send_photo(chat_id=session.chat_id, photo=f, caption=caption)
            _log(session.chat_id, "<<", f"photo {path}")
        except Exception as e:
            await bot.send_message(chat_id=session.chat_id, text=f"Could not send chart: {e}")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def _present_prompt(session: "_Session", req: dict) -> dict:
        """Renders a prompt, parks until the athlete answers, returns the response
        dict to write back to the CLI's stdin. Idle timeout -> cancellation."""
        loop = asyncio.get_running_loop()
        session.awaiting = req
        session.answer_future = loop.create_future()
        session.sent = True
        message = format_prompt_message(req)
        rows = prompt_buttons(req, session.nonce)
        if rows:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                 for row in rows]
            )
            await bot.send_message(chat_id=session.chat_id, text=message, reply_markup=keyboard)
            _log(session.chat_id, "??", f"{req.get('type')} prompt {req.get('id')}")
        else:  # text prompt: the next message is the answer
            await bot.send_message(
                chat_id=session.chat_id,
                text=message + "\n\n(send your reply, or /cancel)",
            )
            _log(session.chat_id, "??", f"text prompt {req.get('id')}")
        try:
            return await asyncio.wait_for(session.answer_future, timeout=prompt_timeout)
        except asyncio.TimeoutError:
            await bot.send_message(
                chat_id=session.chat_id, text="Prompt timed out — command cancelled."
            )
            return {"v": PROMPT_PROTOCOL_VERSION, "id": req.get("id"), "cancelled": True}
        finally:
            session.awaiting = None
            session.answer_future = None

    async def _drive(session: "_Session") -> None:
        """Reads the CLI's stdout, streaming prose to the chat and handling each
        prompt request inline, until the process exits.

        Polling is paused for the silent-compute span (no prompt open) and resumed
        around each prompt, so the athlete can still reach /cancel, /restart, or
        answer a prompt while a subprocess is blocked on stdin, but sending
        anything during silent compute just queues at Telegram until polling
        resumes (§5.1)."""
        buf: List[str] = []
        await _pause_polling()
        try:
            while True:
                # Inactivity watchdog over the compute phase: a command that goes
                # silent for command_timeout is killed. A legitimately-awaited prompt
                # is handled below and has its own (longer) prompt_timeout.
                try:
                    line = await asyncio.wait_for(
                        session.proc.stdout.readline(), timeout=command_timeout
                    )
                except asyncio.TimeoutError:
                    await _flush_output(session, buf)
                    await bot.send_message(
                        chat_id=session.chat_id,
                        text=f"Command timed out after {command_timeout}s.",
                    )
                    break
                if not line:
                    break
                raw = line.decode("utf-8", "replace")
                photo_req = parse_photo_request(raw)
                if photo_req is not None:
                    await _flush_output(session, buf)
                    buf = []
                    await _send_photo(session, photo_req)
                    continue
                buttons_req = parse_buttons_request(raw)
                if buttons_req is not None:
                    await _flush_output(session, buf)
                    buf = []
                    await _send_ui_buttons(session, buttons_req)
                    continue
                req = parse_prompt_request(raw)
                if req is None and raw.rstrip("\n").startswith(_SENTINEL_PREFIX):
                    # Recognised framing but an unknown sentinel (a future CLI
                    # version) — drop rather than forward as chat text.
                    continue
                if req is not None:
                    await _flush_output(session, buf)
                    buf = []
                    await _resume_polling()
                    response = await _present_prompt(session, req)
                    await _pause_polling()
                    try:
                        session.proc.stdin.write((json.dumps(response) + "\n").encode())
                        await session.proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    continue
                buf.append(strip_ansi(raw).rstrip("\n"))
            await _flush_output(session, buf)
            await session.proc.wait()
            # A scheduler-spawned run (`bot morning` already sent today) may
            # legitimately end silent; only interactive commands owe a reply.
            if not session.sent and not session.quiet:
                await bot.send_message(chat_id=session.chat_id, text="(no output)")
        except Exception as e:  # pragma: no cover - defensive
            _log(session.chat_id, "!!", f"drive error: {e}")
            await bot.send_message(chat_id=session.chat_id, text=f"Internal error: {e}")
        finally:
            sessions.pop(session.chat_id, None)
            if session.proc.returncode is None:
                try:
                    session.proc.kill()
                except ProcessLookupError:
                    pass
            await _resume_polling()  # back to idle: always end this session live

    async def _start_command(chat_id: int, argv: List[str], quiet: bool = False) -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", CLI_PATH, *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_cli_env(wrap_width, simple=simple_ui),
            cwd=os.path.dirname(CLI_PATH),
        )
        session = _Session(chat_id, proc, secrets.token_hex(4), quiet=quiet)
        sessions[chat_id] = session
        session.task = asyncio.create_task(_drive(session))

    async def _route_intent(text: str) -> str:
        """Runs `tm bot route` silently — output captured here, never streamed to the
        chat — and returns the intent, 'unclear' on any failure or timeout (§5.3)."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", CLI_PATH, "bot", "route", text,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_cli_env(None),
                cwd=os.path.dirname(CLI_PATH),
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=ROUTER_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return "unclear"
        except Exception:
            return "unclear"
        # The intent is the last JSON line; anything above it is stray CLI prose.
        for line in reversed(out.decode("utf-8", "replace").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                return str(json.loads(line).get("intent") or "unclear")
            except json.JSONDecodeError:
                continue
        return "unclear"

    async def _simple_route(chat_id: int, text: str) -> Optional[List[str]]:
        """Maps unarmed simple-mode free text onto argv via the intent router (§5.3).
        Replies itself (help text, gentle fallback) and returns None when nothing
        should run; otherwise echoes the routed action and returns the argv."""
        intent = await _route_intent(text)
        _log(chat_id, "  ", f"routed: {intent}")
        if intent == "coach_message":
            argv = ["workout", "adapt", "-m", text]
        elif intent in ROUTER_INTENT_ARGV:
            argv = list(ROUTER_INTENT_ARGV[intent])
        elif intent == "help":
            await bot.send_message(
                chat_id=chat_id, text=SIMPLE_HELP, reply_markup=reply_keyboard
            )
            return None
        else:
            await bot.send_message(
                chat_id=chat_id, text=ROUTER_FALLBACK, reply_markup=reply_keyboard
            )
            return None
        echo = ROUTER_ECHO.get(intent)
        if echo:
            await bot.send_message(
                chat_id=chat_id, text=f"<i>→ {html.escape(echo)}</i>",
                parse_mode=ParseMode.HTML,
            )
        return argv

    def _capture_armed(chat_id: int) -> bool:
        """Consumes a chat's "Tell my coach" arming; stale arming (past the prompt
        timeout) reads as unarmed, so a next-morning message isn't swallowed (§5.2)."""
        armed_at = armed.pop(chat_id, None)
        return armed_at is not None and (time.monotonic() - armed_at) <= prompt_timeout

    async def _cancel(chat_id: int) -> str:
        armed.pop(chat_id, None)  # /cancel also disarms "Tell my coach" (§5.2)
        session = sessions.get(chat_id)
        if session is None:
            return "Nothing to cancel."
        fut = session.answer_future
        if session.awaiting and fut is not None and not fut.done():
            fut.set_result({"v": PROMPT_PROTOCOL_VERSION, "id": session.awaiting.get("id"),
                            "cancelled": True})
        else:  # mid-compute: kill the process; _drive cleans up
            try:
                session.proc.kill()
            except ProcessLookupError:
                pass
        return "Cancelling…"

    async def _restart(chat_id: int) -> None:
        """Tears down, replies, then hard-exits with RESTART_EXIT_CODE for the tm-bot
        supervisor to relaunch us. See DESIGN_bot_restart.md §5.2."""
        nonlocal restarting
        restarting = True
        await restart_teardown(sessions.get(chat_id), _pause_polling)
        await bot.send_message(chat_id=chat_id, text="Restarting…")
        os._exit(RESTART_EXIT_CODE)

    async def on_message(update: "Update", context) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not message.text:
            return

        text = message.text.strip()
        _log(chat.id, ">>", repr(text))

        if not is_authorized(chat.id, allowed_ids):
            _log(chat.id, "--", "unauthorized")
            await message.reply_text(
                f"Not authorized. Your chat id is {chat.id}; add it to "
                "telegram.allowed_chat_ids to enable access."
            )
            return

        token_low = text.lstrip("/").lower()
        if token_low == "cancel":
            await message.reply_text(await _cancel(chat.id))
            return
        if token_low == "start":
            if simple_ui:
                await message.reply_text(SIMPLE_WELCOME, reply_markup=reply_keyboard)
            else:
                await message.reply_text(WELCOME)
            return
        if token_low == "help" and simple_ui:
            # Bare help gets the companion card; `/help <cmd>` still reaches the CLI
            # tree for the operator (§5.1).
            await message.reply_text(SIMPLE_HELP, reply_markup=reply_keyboard)
            return
        if token_low == "restart":
            await _restart(chat.id)
            return

        session = sessions.get(chat.id)
        if session is not None:
            awaiting = session.awaiting
            fut = session.answer_future
            if (awaiting and awaiting.get("type") == "text"
                    and fut is not None and not fut.done()):
                fut.set_result({"v": PROMPT_PROTOCOL_VERSION, "id": awaiting.get("id"),
                                "answer": text})
                _log(chat.id, "  ", "text answer")
                return
            await message.reply_text(
                "A command is still running. Use the buttons above, or /cancel."
            )
            return

        # Simple mode: non-slash text is the companion surface — keyboard labels,
        # armed capture, then the free-text router (§5). A leading slash stays the
        # expert path, so the operator can still drive the instance from its chat.
        if simple_ui and not text.startswith("/"):
            action = keyboard_action(text)
            if action is not None and action[0] == "capture":
                armed[chat.id] = time.monotonic()
                await message.reply_text(CAPTURE_PROMPT, reply_markup=reply_keyboard)
                return
            if action is not None:
                argv = list(action[1])
            elif _capture_armed(chat.id):
                argv = ["workout", "adapt", "-m", text]
            else:
                await context.bot.send_chat_action(chat_id=chat.id, action="typing")
                argv = await _simple_route(chat.id, text)
                if argv is None:
                    return
            _log(chat.id, "  ", f"run: {shlex.join(argv)}")
            await context.bot.send_chat_action(chat_id=chat.id, action="typing")
            await _start_command(chat.id, argv)
            return

        bot_username = context.bot.username if context.bot else None
        try:
            argv = parse_message_to_argv(text, bot_username)
        except ValueError:
            await message.reply_text("Couldn't parse that — check your quotes.")
            return
        if not argv:
            return

        _log(chat.id, "  ", f"run: {shlex.join(argv)}")
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        await _start_command(chat.id, argv)

    async def _handle_ui_callback(query, chat_id: int, data: str) -> None:
        """A tap on a non-blocking TM-BUTTONS row (§4.4): a stale token drops the dead
        buttons; a `menu` button swaps the row for its sub-choices; `send` feeds the
        canned utterance through the normal command pipeline; `ack` just replies."""
        decoded = decode_ui_callback(data)
        current = ui_actions.get(chat_id)
        if decoded is None or current is None or decoded[0] != current[0]:
            try:  # replaced by a newer push: drop the dead buttons
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        token, path = decoded
        action = resolve_ui_action(current[1], path)
        if action is None:
            return
        menu = action.get("menu")
        if menu:
            rows = ui_menu_rows(menu, token, path)
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data=cb) for label, cb in row]
                 for row in rows]
            )
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception:
                pass
            return
        utterance = action.get("send")
        if utterance and sessions.get(chat_id) is not None:
            # Busy chat: leave the buttons alive so the tap can be retried.
            await bot.send_message(
                chat_id=chat_id,
                text="One moment — still finishing the last thing. Tap again shortly.",
            )
            return
        _log(chat_id, "  ", f"ui tap: {action.get('label')}")
        try:  # a decided row is spent: drop the buttons
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        ui_actions.pop(chat_id, None)
        if not utterance:
            ack = action.get("ack")
            if ack:
                await bot.send_message(chat_id=chat_id, text=str(ack))
            return
        try:
            argv = parse_message_to_argv(str(utterance))
        except ValueError:
            argv = None
        if not argv:
            return
        _log(chat_id, "  ", f"run: {shlex.join(argv)}")
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        await _start_command(chat_id, argv)

    async def on_callback(update: "Update", context) -> None:
        query = update.callback_query
        chat = update.effective_chat
        if query is None or chat is None:
            return
        await query.answer()
        if not is_authorized(chat.id, allowed_ids):
            return
        data = query.data or ""
        if data.startswith("ui:"):
            await _handle_ui_callback(query, chat.id, data)
            return
        decoded = decode_callback(query.data or "")
        if decoded is None:
            return
        nonce, pid, value = decoded
        session = sessions.get(chat.id)
        awaiting = session.awaiting if session else None
        fut = session.answer_future if session else None
        if (session is None or session.nonce != nonce or awaiting is None
                or awaiting.get("id") != pid or fut is None or fut.done()):
            try:  # stale tap (session replaced/expired): drop the dead buttons
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        if awaiting.get("type") == "confirm":
            answer = value == "y"
            chosen = "Yes" if answer else "No"
            fut.set_result({"v": PROMPT_PROTOCOL_VERSION, "id": pid, "answer": answer})
        else:
            chosen = next((c["label"] for c in awaiting.get("choices", [])
                           if c["value"] == value), value)
            fut.set_result({"v": PROMPT_PROTOCOL_VERSION, "id": pid, "answer": value})
        _log(chat.id, "  ", f"answer: {chosen}")
        try:  # echo the choice in place of the buttons
            await query.edit_message_text(text=f"{format_prompt_message(awaiting)}\n\n→ {chosen}")
        except Exception:
            pass

    # The morning push goes to the first allowlisted chat — the single-athlete
    # instance model makes that the athlete (§4.3).
    push_chat_id = allowed_ids[0] if allowed_ids else None
    push_enabled = (
        simple_ui and config.telegram_push_enabled and push_chat_id is not None
    )

    async def _push_loop() -> None:
        """Fires `bot morning` inside the [morning_time, deadline] window, once per
        bot-day (§4.3). Sleeps at most 5 minutes at a time so a laptop suspend (which
        stalls the monotonic clock asyncio sleeps on) can't oversleep the window. Real
        idempotency lives in the database marker `bot morning` checks; `fired` only
        avoids re-spawning the subprocess every tick within one bot lifetime."""
        fired: Optional[str] = None
        while True:
            now = datetime.datetime.now()
            delay = next_push_delay(
                now, config.telegram_push_morning_time,
                config.telegram_push_morning_deadline,
            )
            if delay <= 0 and fired != now.date().isoformat():
                if sessions.get(push_chat_id) is not None:
                    # §4.3: never collide with an in-flight command — retry shortly.
                    await asyncio.sleep(180)
                    continue
                fired = now.date().isoformat()
                _log(push_chat_id, "**", "morning push")
                await _start_command(push_chat_id, ["bot", "morning"], quiet=True)
                continue
            await asyncio.sleep(min(max(delay, 60), 300))

    async def _serve() -> None:
        """Runs the bot until SIGINT/SIGTERM. Equivalent to
        Application.run_polling(), but with polling started/stopped explicitly
        (via the Updater directly) instead of being wired to the Application's own
        lifetime — run_polling() doesn't expose a way to pause fetching without
        tearing the whole thing down, and _pause_polling/_resume_polling need to
        do exactly that mid-session (§5.1)."""
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        async with application:
            await application.start()
            await updater.start_polling(allowed_updates=Update.ALL_TYPES)
            push_task = asyncio.create_task(_push_loop()) if push_enabled else None
            await stop_event.wait()
            if push_task is not None:
                push_task.cancel()
            if updater.running:
                await updater.stop()
            await application.stop()

    # filters.TEXT catches commands too (a '/status' message is still text).
    application.add_handler(MessageHandler(filters.TEXT, on_message))
    application.add_handler(CallbackQueryHandler(on_callback))
    print("TrainMate Telegram bot started. Press Ctrl-C to stop.")
    asyncio.run(_serve())


async def _post_init(app) -> None:
    from telegram import BotCommand
    commands = (
        SIMPLE_MENU_COMMANDS if config.telegram_ui == "simple" else MENU_COMMANDS
    )
    await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in commands])


if __name__ == "__main__":
    main()
