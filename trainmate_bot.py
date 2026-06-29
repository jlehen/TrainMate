"""TrainMate Telegram front-end.

A thin chat shim over the existing CLI: each incoming message is treated as a
TrainMate command line (the leading slash Telegram requires is optional) and run
through ``trainmate_cli.py`` as a subprocess. Capturing the real CLI keeps the
bot in permanent parity with every command/flag the CLI gains, isolates each
invocation, and sidesteps the per-handler ``input()`` confirmations by feeding a
stream of declines on stdin — destructive ops therefore no-op over chat unless
their explicit ``-y``/``--yes`` flag is passed, and the athlete sees the
handler's own "Aborted" message.

The pure helpers (parse/format/auth) are import-safe without ``python-telegram-bot``
so they can be unit-tested; the library is imported lazily inside ``main``.

Run with: ``./tm-bot`` (or ``venv/bin/python trainmate_bot.py``). Configure the
token + allowlist under a ``telegram:`` block in config.yaml (see
config_template.yaml).
"""
import asyncio
import datetime
import html
import os
import shlex
import subprocess
import sys
from typing import List, Optional

from trainmate.config import config
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
    "Note: destructive commands (wipe, rm) are declined unless you pass their "
    "-y/--yes flag, since I can't ask for confirmation over chat."
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
    ("lifeevent", "Manage life events"),
    ("help", "Show command help"),
]


def parse_message_to_argv(text: str, bot_username: Optional[str] = None) -> Optional[List[str]]:
    """Turns a raw chat message into a CLI argv list, or None if there's nothing to run.

    The leading ``/`` Telegram puts on commands is stripped, as is the ``@botname``
    suffix it appends in group chats. ``help`` and ``help <cmd>`` are rewritten to the
    argparse-native ``--help`` / ``<cmd> --help`` so the CLI prints usage. Raises
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
    # Map the chat-friendly 'help [cmd]' onto argparse's --help.
    if argv[0].lower() == "help":
        rest = argv[1:]
        return rest + ["--help"] if rest else ["--help"]
    return argv


def is_authorized(chat_id: int, allowed_ids: List[int]) -> bool:
    """True only when chat_id is on the allowlist. Empty allowlist authorizes no one."""
    return chat_id in allowed_ids


def run_cli(
    argv: List[str], timeout: int, wrap_width: Optional[int] = None,
    python_exe: Optional[str] = None, cli_path: str = CLI_PATH,
) -> str:
    """Runs the CLI as a subprocess and returns its plain-text output.

    stdin is fed a stream of 'n' declines so any confirmation prompt aborts cleanly
    instead of hanging; stdout+stderr are merged and ANSI codes stripped. wrap_width
    (when set) narrows the CLI's prose wrapping via TRAINMATE_WRAP_WIDTH so phone-width
    monospace replies don't get double-wrapped. Returns a friendly placeholder when the
    command produced no output."""
    python_exe = python_exe or sys.executable
    env = dict(os.environ)
    env["NO_COLOR"] = "1"  # belt-and-suspenders; piped stdout already disables color
    if wrap_width:
        env["TRAINMATE_WRAP_WIDTH"] = str(wrap_width)
    try:
        proc = subprocess.run(
            [python_exe, cli_path, *argv],
            input="n\n" * 100,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=os.path.dirname(cli_path),
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s."
    except Exception as e:  # pragma: no cover - defensive
        return f"Failed to run command: {e}"
    out = (proc.stdout or "") + (proc.stderr or "")
    out = ANSI_ESCAPE.sub("", out).strip()
    return out or "(no output)"


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
    timeout = config.telegram_command_timeout
    wrap_width = config.telegram_wrap_width

    try:
        from telegram import BotCommand, Update
        from telegram.constants import ParseMode
        from telegram.ext import Application, MessageHandler, filters
    except ImportError:
        sys.exit(
            "python-telegram-bot is not installed. Run: "
            "venv/bin/pip install -r requirements.txt"
        )

    def _log(chat_id: int, direction: str, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"{ts} [{chat_id}] {direction} {msg}", flush=True)

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

        if text in ("/start", "/help") or text.lower() == "start":
            if text != "/help":
                await message.reply_text(WELCOME)
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
        output = await asyncio.to_thread(run_cli, argv, timeout, wrap_width)
        lines = output.count("\n") + 1
        _log(chat.id, "<<", f"{lines} line(s)")
        for part in format_reply(output):
            await message.reply_text(part, parse_mode=ParseMode.HTML)

    async def post_init(app: "Application") -> None:
        await app.bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in MENU_COMMANDS]
        )

    app = Application.builder().token(token).post_init(post_init).build()
    # filters.TEXT catches commands too (a '/status' message is still text).
    app.add_handler(MessageHandler(filters.TEXT, on_message))
    print("TrainMate Telegram bot started. Press Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
