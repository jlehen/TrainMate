"""TrainMate Telegram front-end.

A chat shim over the existing CLI: each incoming message is treated as a
TrainMate command line (the leading slash Telegram requires is optional) and run
through ``trainmate_cli.py`` as a subprocess. Driving the real CLI keeps the bot
in permanent parity with every command/flag the CLI gains and isolates each
invocation.

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
import sys
from typing import Dict, List, Optional, Tuple

from trainmate.config import config
from trainmate.prompt import PROMPT_SENTINEL, PROMPT_PROTOCOL_VERSION
from trainmate.util import ANSI_ESCAPE

# Telegram caps a message at 4096 chars; we wrap replies in <pre>…</pre> (7 chars
# of overhead) and want headroom, so chunk the body well under the hard limit.
MAX_MESSAGE_CHARS = 3800

CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trainmate_cli.py")

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
    ("workout", "List/generate/adapt/swap workouts"),
    ("plan", "Show/generate periodization plans"),
    ("goal", "Manage training goals"),
    ("data", "Pull/show Garmin metrics & activities"),
    ("context", "Author daily-context signals"),
    ("learnings", "Inspect coach learnings"),
    ("constraint", "Manage directives the coach works around"),
    ("cancel", "Abort the command awaiting your answer"),
    ("help", "Show command help"),
]


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


def strip_ansi(text: str) -> str:
    """Removes ANSI colour codes (NO_COLOR already disables them, but be defensive)."""
    return ANSI_ESCAPE.sub("", text)


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


def format_reply(text: str) -> List[str]:
    """Renders CLI output as one or more Telegram HTML messages.

    Each chunk is wrapped in <pre> so column alignment survives, with HTML-special
    characters escaped."""
    return [f"<pre>{html.escape(chunk)}</pre>" for chunk in chunk_text(text)]


def _cli_env(wrap_width: Optional[int]) -> Dict[str, str]:
    """Environment for a bot-driven CLI subprocess: structured prompts, no colour,
    unbuffered I/O (so prompt requests arrive before the child blocks on stdin), and
    the narrow wrap width phones want."""
    env = dict(os.environ)
    env["TRAINMATE_FRONTEND"] = "json"
    env["NO_COLOR"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if wrap_width:
        env["TRAINMATE_WRAP_WIDTH"] = str(wrap_width)
    return env


class _Session:
    """One in-flight command for a chat: the live CLI subprocess plus the state
    needed to route a pending prompt's answer back to it."""

    def __init__(self, chat_id: int, proc, nonce: str) -> None:
        self.chat_id = chat_id
        self.proc = proc
        self.nonce = nonce
        self.awaiting: Optional[dict] = None      # the prompt request awaiting an answer
        self.answer_future: Optional["asyncio.Future"] = None
        self.task: Optional["asyncio.Task"] = None
        self.sent = False                         # whether anything was sent to the chat


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
    wrap_width = config.telegram_wrap_width

    try:
        from telegram import (
            BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update,
        )
        from telegram.constants import ParseMode
        from telegram.ext import (
            Application, CallbackQueryHandler, MessageHandler, filters,
        )
    except ImportError:
        sys.exit(
            "python-telegram-bot is not installed. Run: "
            "venv/bin/pip install -r requirements.txt"
        )

    sessions: Dict[int, _Session] = {}

    def _log(chat_id: int, direction: str, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"{ts} [{chat_id}] {direction} {msg}", flush=True)

    application = Application.builder().token(token).post_init(_post_init).build()
    bot = application.bot

    async def _flush_output(session: "_Session", buf: List[str]) -> None:
        text = "\n".join(buf).strip()
        if not text:
            return
        session.sent = True
        for part in format_reply(text):
            await bot.send_message(chat_id=session.chat_id, text=part, parse_mode=ParseMode.HTML)
        _log(session.chat_id, "<<", f"{text.count(chr(10)) + 1} line(s)")

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
        prompt request inline, until the process exits."""
        buf: List[str] = []
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
                req = parse_prompt_request(raw)
                if req is not None:
                    await _flush_output(session, buf)
                    buf = []
                    response = await _present_prompt(session, req)
                    try:
                        session.proc.stdin.write((json.dumps(response) + "\n").encode())
                        await session.proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    continue
                buf.append(strip_ansi(raw).rstrip("\n"))
            await _flush_output(session, buf)
            await session.proc.wait()
            if not session.sent:
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

    async def _start_command(chat_id: int, argv: List[str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", CLI_PATH, *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_cli_env(wrap_width),
            cwd=os.path.dirname(CLI_PATH),
        )
        session = _Session(chat_id, proc, secrets.token_hex(4))
        sessions[chat_id] = session
        session.task = asyncio.create_task(_drive(session))

    async def _cancel(chat_id: int) -> str:
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
            await message.reply_text(WELCOME)
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

    async def on_callback(update: "Update", context) -> None:
        query = update.callback_query
        chat = update.effective_chat
        if query is None or chat is None:
            return
        await query.answer()
        if not is_authorized(chat.id, allowed_ids):
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

    # filters.TEXT catches commands too (a '/status' message is still text).
    application.add_handler(MessageHandler(filters.TEXT, on_message))
    application.add_handler(CallbackQueryHandler(on_callback))
    print("TrainMate Telegram bot started. Press Ctrl-C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


async def _post_init(app) -> None:
    from telegram import BotCommand
    await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in MENU_COMMANDS])


if __name__ == "__main__":
    main()
