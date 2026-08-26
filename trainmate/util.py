import os
import re
import sys
import textwrap
from datetime import date, datetime
from typing import Optional, Tuple

# ANSI escape codes for terminal coloring
ANSI_ESCAPE = re.compile(r'(?:\033|\x1b)\[[0-9;]*m')
RESET = "\033[0m"


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
    """Returns today's date in the athlete's timezone (DESIGN_user_timezone.md §1).

    Garmin keys daily metrics and activities on the athlete's local calendar
    date, so every "what day is it" computation must use local time rather than
    UTC (a UTC frontier drifts a day at the boundary hours). Instants stored for
    comparison (created_at, last-pull timestamps) stay in UTC elsewhere.
    """
    from trainmate.clock import now
    return now().date()


def today_str() -> str:
    """Returns today's local calendar date as a YYYY-MM-DD string."""
    return today_date().strftime("%Y-%m-%d")


def days_between(start: str, end: str) -> int:
    """Returns whole days from `start` to `end` (both YYYY-MM-DD), negative if end precedes it."""
    fmt = "%Y-%m-%d"
    return (datetime.strptime(end, fmt).date() - datetime.strptime(start, fmt).date()).days


def fmt_date(date_str: Optional[str]) -> str:
    """Renders a YYYY-MM-DD date as 'YYYY-MM-DD Ddd' (e.g. '2026-06-05 Fri').

    The one date renderer for every surface with room for the weekday. Falls back to
    the raw string when the value isn't a parseable date, so a caller can hand this
    whatever a row happens to hold."""
    if not date_str:
        return "?"
    try:
        return datetime.strptime(str(date_str), "%Y-%m-%d").strftime("%Y-%m-%d %a")
    except ValueError:
        return str(date_str)


def fmt_span(start: Optional[str], end: Optional[str], sep: str = " to ") -> str:
    """Renders a date range with the weekday on both ends. A range that starts and
    ends on the same day collapses to that one date."""
    if start and end and start == end:
        return fmt_date(start)
    return f"{fmt_date(start)}{sep}{fmt_date(end)}"


def fmt_timestamp(iso: Optional[str]) -> str:
    """Renders a stored UTC ISO timestamp as 'YYYY-MM-DD Ddd HH:MM' in the athlete's
    timezone — stored precise, converted only on display (DESIGN_user_timezone.md §5).

    Falls back to the raw string if it isn't parseable (e.g. a date-only legacy value)."""
    if not iso:
        return "?"
    from trainmate.clock import to_local
    try:
        return to_local(datetime.fromisoformat(iso)).strftime("%Y-%m-%d %a %H:%M")
    except ValueError:
        return iso


def is_color_enabled() -> bool:
    """Checks if color output is supported and not explicitly disabled."""
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def colorize(text: str, color_code: str) -> str:
    """Wraps text in ANSI escape code if coloring is enabled.

    Any reset already inside `text` re-opens this code, so wrapping a string that
    embeds its own colouring (a red error carrying a cmd(), say) keeps the rest of
    the line in the outer colour instead of dropping it to the terminal default."""
    if not is_color_enabled():
        return text
    return color_code + text.replace(RESET, RESET + color_code) + RESET


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


def asides_enabled() -> bool:
    """Whether side information prints: a terminal reads it live, a chat front-end gets
    it as history above the answer. TRAINMATE_VERBOSE=1/0 forces either way — no CLI
    flag, `-v` is taken (DESIGN_output_verbosity.md §2/§4)."""
    raw = os.environ.get("TRAINMATE_VERBOSE")
    if raw:
        return raw.lower() not in ("0", "no", "false")
    from trainmate.prompt import is_json_frontend
    return not is_json_frontend()


def aside(text: str, color_fn=None) -> None:
    """Prints one piece of side information — progress, cache reuse, a next-step hint.
    The answer, warnings and errors use `print` and reach every front-end
    (DESIGN_output_verbosity.md §3)."""
    if not asides_enabled():
        return
    print((color_fn or dim)(text))


def strip_ansi(text: str) -> str:
    """Drops ANSI colour codes — for surfaces that aren't a terminal (JSON, logs)."""
    return ANSI_ESCAPE.sub("", text)


def cmd(text: str, *, quote: bool = True) -> str:
    """Renders a command the message is telling the athlete to run, single-quoted.

    Colour-neutral on purpose — bold only — so it comes out as the bright shade of
    whatever colour encloses it: bright red inside an error, bright yellow inside a
    warning. Nest it *inside* the surrounding colour call rather than concatenating
    beside it; colorize() re-opens that colour afterwards, so the tail of the sentence
    keeps it. `quote=False` for a command printed alone on its own line, where the
    quotes are just noise."""
    return bold(f"'{text}'" if quote else text)


def color_load_ratio(ratio: float) -> str:
    """ATL/CTL (fatigue vs fitness) coloring — colors only the overload end, phase-blind
    (training_load.md §3).

    > 1.5 red (excessive relative spike), 1.3-1.5 yellow (caution). Everything at or
    below 1.3 stays uncolored: a *low* ratio is phase-dependent, not a fault — an
    intensity or realization block drives it to ~0.7 by design, and coloring that as
    "under-training" is what made the old ACWR band fight block periodization. Bands are
    half-open so no value is double-claimed."""
    s = f"{ratio:.2f}"
    if ratio > 1.5:
        return red(s)
    if ratio > 1.3:
        return yellow(s)
    return s


# The printed CTL | ATL | TSB triple won't subtract to the shown TSB, because TSB is
# CTL(yesterday) - ATL(yesterday) (training_load.md §1) while CTL/ATL are today's. This
# lag is correct (matching TrainingPeaks) but reads as an arithmetic error, so this
# one-line footnote rides wherever TSB is surfaced (per-day prompt block, coach summary,
# tm status). Lives here — not in coach.formatting — because both the CLI and the coach
# layer render it.
PMC_TSB_LAG_NOTE = (
    "(Note: TSB is CTL(yesterday) - ATL(yesterday), so it won't equal the shown "
    "same-day CTL - ATL; this ~1-day lag is expected, not an error.)"
)


def color_tsb(tsb: float) -> str:
    """TSB (form) coloring — colors only the two risk ends, phase-blind
    (DESIGN_pmc_fitness_fatigue.md §6.1).

    < -30 red (excessive fatigue), > +25 yellow (detraining / over-tapered). The
    -30..+25 middle stays uncolored: its meaning is phase-dependent (mid-build a +15
    means fitness is decaying; peaking, it means race-ready), and that interpretive call
    belongs to the coach reading the science file, not to a phase-blind color map. Bands
    are half-open so no value is double-claimed."""
    s = f"{tsb:.1f}"
    if tsb < -30:
        return red(s)
    if tsb > 25:
        return yellow(s)
    return s


def color_ramp(ramp: float) -> str:
    """CTL ramp-rate coloring, bands touching so no value falls in an uncolored gap
    (§6.1): >= 8 red (unsustainable), 5 <= ramp < 8 yellow (watch), else plain. No green
    band — a low ramp is correct during a taper, so green would wrongly bless it."""
    s = f"{ramp:+.1f}"
    if ramp >= 8:
        return red(s)
    if 5 <= ramp < 8:
        return yellow(s)
    return s


def pmc_cells(
    ctl: Optional[float], atl: Optional[float], tsb: Optional[float]
) -> Tuple[str, str, str]:
    """The CTL/ATL/TSB triple as display strings, shared by every user surface that
    renders it (tm status, data show-metrics, the workout-adapt trajectory).

    Feed it the output of garmin.pmc_display_values(), which already blanks warm-up rows.
    A missing value renders "—" and never "0.0": a printed "TSB 0.0" reads as a real
    neutral balance rather than as absent data (DESIGN_pmc_fitness_fatigue.md §6.1)."""
    return (
        f"{ctl:.1f}" if ctl is not None else "—",
        f"{atl:.1f}" if atl is not None else "—",
        color_tsb(tsb) if tsb is not None else "—",
    )


def pmc_warming_note(n_days: int, ctl_days: int) -> str:
    """The §3.3(b) "PMC still warming" caveat, parameterized by τ_ctl so it never
    hardcodes a 42 that a non-default config would make a lie."""
    return (
        f"PMC still warming: CTL based on {n_days} days of history (a {ctl_days}-day "
        f"average needs ~{3 * ctl_days} days to settle); fitness/freshness may read low."
    )


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


class Progress:
    """A single self-erasing '[####....] 7/28' line for a loop whose only other option is
    one printed line per item (the Calendar round-trips of generate/rollback).

    Silent unless stdout is a terminal, so piped output, the bot and the tests keep just
    the summary line that introduced it. Usable as a context manager, which erases the
    line on the way out."""

    BAR_WIDTH = 24

    def __init__(self, total: int) -> None:
        self.total = total
        self.done_count = 0
        self.active = total > 0 and sys.stdout.isatty()
        self._draw()

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def step(self, n: int = 1) -> None:
        self.done_count = min(self.total, self.done_count + n)
        self._draw()

    def close(self) -> None:
        """Erases the bar, leaving the surrounding output as if it never drew."""
        if self.active:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self.active = False

    def _draw(self) -> None:
        if not self.active:
            return
        filled = round(self.BAR_WIDTH * self.done_count / self.total)
        bar = "#" * filled + "." * (self.BAR_WIDTH - filled)
        sys.stdout.write(f"\r\033[K  [{bar}] {self.done_count}/{self.total}")
        sys.stdout.flush()


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


def _wrap_paragraph(para: str, width: int, subsequent_indent: str) -> list:
    """One paragraph wrapped to `width`, measured in visible columns.

    textwrap counts ANSI escape bytes as columns, so a coloured paragraph would wrap
    ~10 columns short per colour span; a greedy word wrap on visible_len avoids that.
    Uncoloured text still goes through textwrap so existing layout is untouched."""
    if not ANSI_ESCAPE.search(para):
        return textwrap.wrap(para, width=width, subsequent_indent=subsequent_indent)
    lines, cur = [], ''
    for word in para.split(' '):
        if not cur:
            cur = word
        elif visible_len(cur) + 1 + visible_len(word) <= width:
            cur += ' ' + word
        else:
            lines.append(cur)
            cur = subsequent_indent + word
    if cur:
        lines.append(cur)
    return lines


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
            wrapped = _wrap_paragraph(para, width, indent)
            wrapped_paragraphs.extend(wrapped)
        else:
            wrapped_paragraphs.extend(_wrap_paragraph(para, width, ''))
            
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

