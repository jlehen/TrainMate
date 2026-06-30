import os
import re
import sys
import textwrap
from datetime import date
from typing import Optional

# ANSI escape codes for terminal coloring
ANSI_ESCAPE = re.compile(r'(?:\033|\x1b)\[[0-9;]*m')


def default_wrap_width() -> int:
    """The column width text wrapping targets, default 80.

    Overridable via the TRAINMATE_WRAP_WIDTH env var so a narrow client (e.g. the
    Telegram bot rendering into a phone-width monospace block) can ask the CLI to
    wrap tighter and avoid the client double-wrapping 80-col lines. Floored at 20."""
    raw = os.environ.get("TRAINMATE_WRAP_WIDTH")
    if not raw:
        return 80
    try:
        return max(20, int(raw))
    except ValueError:
        return 80


def today_date() -> date:
    """Returns today's date in the machine's local timezone.

    Garmin keys daily metrics and activities on the athlete's local calendar
    date, so every "what day is it" computation must use local time rather than
    UTC (a UTC frontier drifts a day at the boundary hours). Instants stored for
    comparison (created_at, last-pull timestamps) stay in UTC elsewhere.
    """
    return date.today()


def today_str() -> str:
    """Returns today's local calendar date as a YYYY-MM-DD string."""
    return today_date().strftime("%Y-%m-%d")


def is_color_enabled() -> bool:
    """Checks if color output is supported and not explicitly disabled."""
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def colorize(text: str, color_code: str) -> str:
    """Wraps text in ANSI escape code if coloring is enabled."""
    if is_color_enabled():
        return f"{color_code}{text}\033[0m"
    return text


def bold(text: str) -> str:
    return colorize(text, "\033[1m")


def dim(text: str) -> str:
    return colorize(text, "\033[2m")


def green(text: str) -> str:
    return colorize(text, "\033[32m")


def red(text: str) -> str:
    return colorize(text, "\033[31m")


def yellow(text: str) -> str:
    return colorize(text, "\033[33m")


def cyan(text: str) -> str:
    return colorize(text, "\033[36m")


def blue(text: str) -> str:
    return colorize(text, "\033[34m")


def magenta(text: str) -> str:
    return colorize(text, "\033[35m")


def gray(text: str) -> str:
    return colorize(text, "\033[90m")


def color_acwr(acwr: float) -> str:
    """Returns colorized ACWR string based on values."""
    acwr_str = f"{acwr:.2f}"
    if acwr < 0.8:
        return yellow(acwr_str)
    elif 0.8 <= acwr <= 1.3:
        return green(acwr_str)
    elif 1.3 < acwr <= 1.5:
        return yellow(acwr_str)
    else:
        return red(acwr_str)


def visible_len(s: str) -> int:
    """Calculates visible length of a string, ignoring ANSI escape codes."""
    return len(ANSI_ESCAPE.sub('', s))


def pad_visible(s: str, width: int, align_left: bool = True) -> str:
    """Pads a string considering its visible length (ignoring ANSI codes)."""
    v_len = visible_len(s)
    padding = ' ' * max(0, width - v_len)
    if align_left:
        return s + padding
    else:
        return padding + s


def is_narrow_client() -> bool:
    """True when the CLI is driven by a narrow front-end (e.g. the Telegram bot)
    that asked for a tight wrap width via TRAINMATE_WRAP_WIDTH.

    Wide columnar tables wrap unreadably in a phone-width monospace block, so on a
    narrow client we collapse them to a vertical record layout instead. A real
    terminal (default width 80) stays False and keeps the familiar table. The 70
    threshold matches the CLI's argparse help formatter."""
    return default_wrap_width() < 70


def render_table(
    headers: list, rows: list, narrow: Optional[bool] = None
) -> str:
    """Renders a table for the active client and returns it as text.

    ``rows`` is a list of rows, each a list of pre-formatted cell strings (ANSI
    colour is fine — widths are measured with visible_len) matching ``headers``.

    On a normal-width terminal this is the familiar columnar table: a bold header,
    ``" | "`` separators, and a gray rule, with each column auto-sized to its
    widest cell. On a narrow client (the bot) the same data becomes one vertical
    record per row — the first column as a heading, the remaining columns as
    aligned ``label  value`` lines — so figures stay readable without the wide
    line being re-wrapped by the client. Records are blank-line separated.

    Returns the rendered text with no trailing newline."""
    if narrow is None:
        narrow = is_narrow_client()

    if narrow:
        label_w = max((visible_len(h) for h in headers[1:]), default=0)
        blocks = []
        for row in rows:
            lines = [str(row[0])]
            for header, cell in zip(headers[1:], row[1:]):
                lines.append(f"  {pad_visible(header, label_w)}  {cell}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    widths = []
    for i, header in enumerate(headers):
        cell_w = max((visible_len(str(row[i])) for row in rows), default=0)
        widths.append(max(visible_len(header), cell_w))

    out = [bold(" | ".join(pad_visible(h, widths[i]) for i, h in enumerate(headers)))]
    total = sum(widths) + 3 * (len(widths) - 1)
    out.append(gray("-" * total))
    for row in rows:
        out.append(
            " | ".join(pad_visible(str(c), widths[i]) for i, c in enumerate(row))
        )
    return "\n".join(out)


def wrap_text(text: str, width: Optional[int] = None) -> str:
    """Wraps text at the specified width while preserving layout and indentation.

    When width is None it falls back to default_wrap_width() (80, or the
    TRAINMATE_WRAP_WIDTH override)."""
    if width is None:
        width = default_wrap_width()
    if not text:
        return text
    paragraphs = text.split('\n')
    wrapped_paragraphs = []
    for para in paragraphs:
        if not para.strip():
            wrapped_paragraphs.append('')
            continue
        
        # Detect leading whitespace and list prefix (e.g. "- ", "* ", "1. ")
        match = re.match(r'^(\s*(?:[-*+]\s+|\d+\.\s+)?)(.*)', para)
        if match:
            prefix, content = match.groups()
            indent = ' ' * len(prefix)
            # Wrap the paragraph, using the prefix indent for subsequent lines
            wrapped = textwrap.wrap(para, width=width, subsequent_indent=indent)
            wrapped_paragraphs.extend(wrapped)
        else:
            wrapped_paragraphs.append(textwrap.fill(para, width=width))
            
    return '\n'.join(wrapped_paragraphs)


def format_labeled_text(
    label: str, text: str, width: Optional[int] = None, color_fn=None
) -> str:
    """Wraps and indents text dynamically under its label, optional coloring."""
    if width is None:
        width = default_wrap_width()
    indent_len = visible_len(label)
    wrapped_width = max(20, width - indent_len)
    wrapped_text = wrap_text(text, width=wrapped_width)
    if color_fn:
        wrapped_text = color_fn(wrapped_text)
    indented_text = wrapped_text.replace('\n', '\n' + ' ' * indent_len)
    return f"{label}{indented_text}"


def format_labeled_block(
    label: str, text: str, width: Optional[int] = None, color_fn=None
) -> str:
    """Wraps text on a new line, indented 2 spaces deeper than the label."""
    if width is None:
        width = default_wrap_width()
    if not text:
        return f"{label}"
    match = re.match(r'^(\s*)', label)
    leading_spaces = match.group(1) if match else ""
    block_indent = leading_spaces + "  "
    
    wrapped_width = max(20, width - len(block_indent))
    wrapped_text = wrap_text(text, width=wrapped_width)
    if color_fn:
        wrapped_text = color_fn(wrapped_text)
    
    indented_text = block_indent + wrapped_text.replace('\n', '\n' + block_indent)
    return f"{label}\n{indented_text}"

