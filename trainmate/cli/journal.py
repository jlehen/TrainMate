"""`journal` command: read the run journal back (DESIGN_logging.md §7).

`trainmate/journal.py` owns what a record is and how it is written; this file only reads
the files it wrote and renders them. It is `journal` and not `log` because `tm log` would
read as the athlete's training log, which is what the workouts already are (§5.2).

Every stamp in the files is UTC (§4). Everything printed here goes through the athlete's
zone first, so "what happened on Tuesday" is still answered in local terms.
"""
import argparse
import os
import textwrap
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from trainmate import journal
from trainmate.cli.selectors import add_selector_args, resolve_window
from trainmate.clock import to_local
from trainmate.util import (
    bold, cyan, dim, display_width, flex_width, fmt_timestamp, gray, green, pad_visible,
    red, render_table, visible_len, yellow,
)

# How many runs the listing shows when nothing else is asked for.
DEFAULT_LIMIT = 20

# Seconds between polls while `--follow` tails the file.
FOLLOW_POLL_SECONDS = 0.5

_LEVEL_COLOR = {"warn": yellow, "error": red, "debug": gray}

# The read-only views the listing leaves out until `-a` asks for them (§7.1). Keyed on the
# verb — the last word of the canonical command path — so one entry covers every group's
# `list`, and a `show` added under a new group tomorrow is covered the day it lands.
READ_ONLY_VERBS = frozenset({
    "list", "list-metrics", "show", "show-metrics", "show-activities", "show-analysis",
    "status", "progress", "compare", "batches", "versions", "diff", "journal", "help",
    "shell",
    # A bare `settings` is `settings list`: the one group that acts, read-only, when it is
    # given no sub-command (DESIGN_cli_noargs.md §a3).
    "settings",
})

# An argv carrying one of these printed help and did nothing else.
HELP_FLAGS = ("-h", "--help", "--helpall")

# What the END column says, the colour it says it in, and what it means. The legend
# glosses the words that are actually on screen, in this order (§7.2).
_END_GLOSS = (
    ("ok", green, "finished"),
    ("warn", yellow, "finished, but logged a warning or an error"),
    ("cancelled", dim, "stopped with Ctrl-C"),
    ("FAILED", red, "raised — 'journal <id>' has the traceback"),
    ("?", yellow, "no end recorded: still running, or killed"),
)
_END_COLOR = {word: color for word, color, _gloss in _END_GLOSS}


@dataclass
class RunSummary:
    """One run as the listing sees it: the two bracket records and nothing else (§7)."""
    id: str
    started: str = ""
    source: str = "?"
    command: str = ""                   # the line as it was typed, prefixes and all
    path: str = ""                      # the canonical command that line resolved to
    argv: List[str] = field(default_factory=list)
    parent: Optional[str] = None
    pid: Optional[int] = None
    config: Optional[str] = None
    db: Optional[str] = None
    outcome: Optional[str] = None       # None = no run.end: still going, or killed
    exit_code: Optional[int] = None
    ms: Optional[int] = None
    llm_calls: int = 0
    tokens: int = 0
    warns: int = 0
    errors: int = 0
    error: Optional[str] = None
    traceback: Optional[str] = None


# --- reading ---------------------------------------------------------------------

def _local(ts: Any) -> Optional[datetime]:
    """A record's UTC stamp as an instant in the athlete's zone."""
    try:
        return to_local(datetime.fromisoformat(str(ts)))
    except (TypeError, ValueError):
        return None


def _command_of(argv: List[str]) -> str:
    """The command a run is, without its arguments: the leading two bare words.

    `plan generate -g 2` is `plan generate`; `status` is `status`. Only a fallback now:
    what was typed may be any unambiguous prefix, so `wo a` reads as `wo a` here and not
    as `workout adapt` — `_path` prefers the canonical name the record carries (§7.1)."""
    words = []
    for token in argv:
        if token.startswith("-") or len(words) == 2:
            break
        words.append(token)
    return " ".join(words)


def _path(run: "RunSummary") -> str:
    """The canonical command the run turned out to be, falling back to the words it was
    typed as for a run that was killed before its parse — or that predates `cmd` (§7.1)."""
    return run.path or _command_of(run.argv)


def _collect(start_day: Optional[date], end_day: Optional[date]) -> Dict[str, RunSummary]:
    """Every run in the window, keyed by id, from its two bracket records."""
    runs: Dict[str, RunSummary] = {}
    for rec in journal.iter_records(start_day, end_day):
        ev = rec.get("ev")
        if ev not in ("run.start", "run.end"):
            continue
        run_id = str(rec.get("run") or journal.NO_RUN)
        summary = runs.setdefault(run_id, RunSummary(id=run_id))
        d = rec.get("d") or {}
        if ev == "run.start":
            summary.started = str(rec.get("ts") or "")
            summary.source = str(d.get("source") or "?")
            summary.argv = list(d.get("argv") or [])
            summary.command = str(rec.get("msg") or "")
            summary.parent = d.get("parent")
            summary.pid = d.get("pid")
            summary.config = d.get("config")
            summary.db = d.get("db")
            continue
        summary.outcome = str(d.get("outcome") or "ok")
        summary.path = str(d.get("cmd") or "")
        summary.exit_code = d.get("exit")
        summary.ms = d.get("ms")
        summary.llm_calls = int(d.get("llm_calls") or 0)
        summary.tokens = int(d.get("tokens") or 0)
        summary.warns = int(d.get("warns") or 0)
        summary.errors = int(d.get("errors") or 0)
        summary.error = d.get("error")
        summary.traceback = d.get("traceback")
    return runs


def _window(args: argparse.Namespace) -> tuple:
    """The local date window `-d` asked for, or (None, None) for everything on disk.

    The same `-d start..end` grammar every other range command speaks
    (DESIGN_cli_selectors.md §3), rather than the `--since`/`--until` pair that retired
    with it — one vocabulary, so `-d 7d` reads here exactly as it does in `data pull`."""
    start, end = resolve_window(args)
    return _as_date(start), _as_date(end)


def _as_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(str(value).strip())


def _select(args: argparse.Namespace) -> Tuple[List[RunSummary], int]:
    """The runs the flags ask for, newest first, and how many views were left out."""
    since, until = _window(args)
    # This very run is excluded: it has no `run.end` yet, so it would head every
    # listing as a `?` and turn up under --failed as the command you just typed.
    mine = journal.current_id()
    runs = [r for r in _collect(since, until).values() if r.started and r.id != mine]
    if since or until:
        runs = [r for r in runs if _in_window(r, since, until)]
    source = getattr(args, "source", None)
    if source:
        runs = [r for r in runs if r.source == source]
    wanted = getattr(args, "command_filter", None)
    if wanted:
        runs = [r for r in runs if _matches_command(r, wanted)]
    if getattr(args, "failed", False):
        runs = [r for r in runs if _is_trouble(r)]
    hidden = 0
    # Naming a command is asking for it, views included; otherwise this listing is about
    # what the app did, and a run that only printed did nothing (§7.1).
    if not getattr(args, "show_all", False) and not wanted:
        kept = [r for r in runs if not _is_view(r)]
        hidden = len(runs) - len(kept)
        runs = kept
    runs.sort(key=lambda r: r.started, reverse=True)
    return runs, hidden


def _matches_command(run: RunSummary, wanted: str) -> bool:
    """What `--command "workout adapt"` matches: the canonical name first, so a run typed
    `wo a` answers to it too, and the typed line for records that predate `cmd` (§7.1)."""
    wanted = wanted.strip().lower()
    return _path(run).startswith(wanted) or run.command.startswith(wanted)


def _is_view(run: RunSummary) -> bool:
    """Whether the run only looked at things: a read-only view, or a help print (§7.1).

    Trouble and model calls are never a view, whatever the command was: an error raised
    inside `workout list` is exactly what this listing exists to put in front of you."""
    if _is_trouble(run) or run.llm_calls:
        return False
    if not run.argv or any(flag in run.argv for flag in HELP_FLAGS):
        return True
    return _path(run).split(" ")[-1] in READ_ONLY_VERBS


def _in_window(run: RunSummary, since: Optional[date], until: Optional[date]) -> bool:
    """Whether the run started inside the athlete's local window."""
    moment = _local(run.started)
    if moment is None:
        return False
    if since and moment.date() < since:
        return False
    return not (until and moment.date() > until)


def _is_trouble(run: RunSummary) -> bool:
    """What `--failed` means: it failed, it was killed before it could say anything, or
    it finished having written a warning or an error."""
    if run.outcome is None:
        return True
    return run.outcome == "failed" or run.warns > 0 or run.errors > 0


# --- rendering -------------------------------------------------------------------

def _end_word(run: RunSummary) -> str:
    """The END column: the outcome, plus what the run wrote (§7)."""
    if run.outcome is None:
        return "?"
    if run.outcome == "failed":
        return "FAILED"
    if run.outcome == "cancelled":
        return "cancelled"
    if run.warns or run.errors:
        return "warn"
    return "ok"


def _end_cell(run: RunSummary) -> str:
    word = _end_word(run)
    return _END_COLOR[word](word)


def _time_cell(run: RunSummary) -> str:
    if run.ms is None:
        return "—"
    return f"{run.ms / 1000:.1f}s"


def _llm_cell(run: RunSummary) -> str:
    if not run.llm_calls:
        return "—"
    return f"{run.llm_calls} · {run.tokens / 1000:.0f}k"


def _when_cell(run: RunSummary) -> str:
    return fmt_timestamp(run.started)


_HEADERS = ["RUN", "WHEN", "SRC", "COMMAND", "TIME", "LLM", "END"]

# The command line is the one cell with no upper bound, so it is the one that gives way
# when the table would otherwise run past the screen (§7.2).
_COMMAND_COLUMN = _HEADERS.index("COMMAND")


def _print_listing(
    runs: List[RunSummary], limit: int, hidden: int = 0, verbose: bool = False
) -> None:
    shown = runs[:limit] if limit else runs
    if not shown:
        print("No runs match." if journal.day_paths() else "No runs recorded yet.")
        if hidden:
            print(dim(_omissions(hidden, 0, False)))
        return
    rows = [
        [run.id, _when_cell(run), run.source, run.command,
         _time_cell(run), _llm_cell(run), _end_cell(run)]
        for run in shown
    ]
    room = flex_width(_HEADERS, rows, _COMMAND_COLUMN)
    clipped = not verbose and any(
        visible_len(row[_COMMAND_COLUMN]) > room for row in rows
    )
    print(render_table(_HEADERS, rows, flex=None if verbose else _COMMAND_COLUMN))
    print()
    for line in _legend(shown):
        print(gray(line))
    for line in _wrap(_omissions(hidden, len(runs) - len(shown), clipped)):
        print(dim(line))


def _legend(shown: List[RunSummary]) -> List[str]:
    """The gray footer that says what the two coded columns mean (§7.2).

    Only the outcomes actually on screen are glossed: a legend that explains what is not
    there is a paragraph the eye learns to skip."""
    words = {_end_word(run) for run in shown}
    lines = ["END  " + " · ".join(
        f"{word} = {gloss}" for word, _color, gloss in _END_GLOSS if word in words
    )]
    if any(run.llm_calls for run in shown):
        lines.append("LLM  model calls · tokens")
    return [line for text in lines for line in _wrap(text, indent="     ")]


def _wrap(text: str, indent: str = "") -> List[str]:
    """One footer line inside the client's width, continuations indented so they read as
    the same line rather than as a new one."""
    return textwrap.wrap(
        text, width=display_width(), subsequent_indent=indent, break_on_hyphens=False
    )


def _omissions(hidden: int, older: int, clipped: bool) -> str:
    """The dim line naming what the listing left out, and the flag that brings it back."""
    parts = []
    if hidden:
        parts.append(f"{hidden} read-only run(s) hidden (-a for all)")
    if clipped:
        parts.append("command lines clipped (-v for the full one)")
    if older:
        parts.append(f"{older} older run(s) not shown (raise -n)")
    return " · ".join(parts)


def _print_detail(run: RunSummary, runs: Dict[str, RunSummary]) -> None:
    """One run: its header, every record it wrote, and the runs it spawned."""
    print(bold(f"run {run.id}") + " · " + (run.command or "?"))
    where = [run.source]
    if run.pid is not None:
        where.append(f"pid {run.pid}")
    if run.config:
        where.append(os.path.basename(run.config))
    if run.db:
        where.append(os.path.basename(run.db))
    print(dim("  " + " · ".join(where)))
    print(dim("  " + _span_line(run)))
    if run.parent:
        print(dim(f"  parent {run.parent}"))
    print()
    _print_events(run)
    children = sorted(
        (r for r in runs.values() if r.parent == run.id), key=lambda r: r.started
    )
    if not children:
        return
    print()
    print(bold(f"{len(children)} run(s) started by this one:"))
    for child in children:
        print(f"  {child.id}  {_when_cell(child)}  "
              f"{pad_visible(child.command, 30)}  {_end_cell(child)}")


def _span_line(run: RunSummary) -> str:
    """`2026-08-24 Mon 19:22 → 19:23 · 96.4s · failed (exit 1)`."""
    started = _local(run.started)
    parts = [fmt_timestamp(run.started)]
    if started is not None and run.ms is not None:
        ended = started.timestamp() + run.ms / 1000
        parts[0] += " → " + datetime.fromtimestamp(
            ended, tz=started.tzinfo
        ).strftime("%H:%M")
        parts.append(f"{run.ms / 1000:.1f}s")
    if run.outcome is None:
        parts.append("no end recorded — still running, or killed")
        return " · ".join(parts)
    ending = run.outcome
    if run.exit_code is not None:
        ending += f" (exit {run.exit_code})"
    parts.append(ending)
    return " · ".join(parts)


def _print_events(run: RunSummary) -> None:
    """Every record the run wrote, as offsets from its ``run.start``."""
    records = [
        rec for rec in journal.iter_records(*_day_bounds(run))
        if str(rec.get("run")) == run.id
    ]
    records.sort(key=lambda rec: (str(rec.get("ts")), rec.get("seq") or 0))
    origin = _local(run.started)
    width = max([len(str(rec.get("ev") or "")) for rec in records] + [9])
    for rec in records:
        moment = _local(rec.get("ts"))
        offset = (moment - origin).total_seconds() if moment and origin else 0.0
        level = str(rec.get("lvl") or "info")
        color = _LEVEL_COLOR.get(level, str)
        head = f"{offset:+7.1f}s  {pad_visible(color(level), 5)}  "
        head += pad_visible(cyan(str(rec.get("ev") or "")), width)
        lines = str(rec.get("msg") or "").split("\n")
        print(f"{head}  {lines[0]}")
        indent = " " * visible_len(head) + "  "
        for extra in lines[1:]:
            print(indent + extra)
        for extra in _continuations(rec):
            print(indent + dim(extra))


def _continuations(rec: Dict[str, Any]) -> List[str]:
    """The lines that hang under a record: the exchange file, the traceback."""
    d = rec.get("d") or {}
    out = []
    if d.get("file"):
        out.append(str(d["file"]))
    trace = d.get("traceback")
    if trace:
        out.extend(str(trace).rstrip("\n").split("\n"))
    return out


def _day_bounds(run: RunSummary) -> tuple:
    """The local day the run started and the one it ended on — a long run has its tail
    in the next day's file (§4)."""
    started = _local(run.started)
    if started is None:
        return None, None
    ended = started
    if run.ms:
        ended = datetime.fromtimestamp(
            started.timestamp() + run.ms / 1000, tz=started.tzinfo
        )
    return started.date(), ended.date()


# --- the cost rollup --------------------------------------------------------------

def _print_cost(args: argparse.Namespace) -> None:
    """Volume by model and by command (§7).

    Tokens, not money: prompt caching means the two are not proportional, so read this
    as a volume rather than a bill."""
    since, until = _window(args)
    runs = _collect(since, until)
    wanted = {
        run_id for run_id, run in runs.items()
        if run.started and _in_window(run, since, until)
    }
    by_model: Dict[str, List[int]] = defaultdict(lambda: [0, 0])   # calls, tokens
    model_runs: Dict[str, set] = defaultdict(set)
    for rec in journal.iter_records(since, until):
        if rec.get("ev") != "llm.call":
            continue
        run_id = str(rec.get("run"))
        if run_id not in wanted:
            continue
        d = rec.get("d") or {}
        model = str(d.get("model") or "?")
        by_model[model][0] += 1
        by_model[model][1] += int(d.get("tokens") or 0)
        model_runs[model].add(run_id)
    if not by_model:
        print("No model calls recorded in that window.")
        return

    rows = []
    for model in sorted(by_model, key=lambda m: -by_model[m][1]):
        calls, tokens = by_model[model]
        rows.append([model, str(len(model_runs[model])), str(calls), f"{tokens:,}"])
    rows.append([
        "", str(len({r for s in model_runs.values() for r in s})),
        str(sum(v[0] for v in by_model.values())),
        f"{sum(v[1] for v in by_model.values()):,}",
    ])
    print(render_table(["MODEL", "RUNS", "CALLS", "TOKENS"], rows))

    by_command: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])  # runs, calls, tokens
    for run_id in wanted:
        run = runs[run_id]
        if not run.llm_calls:
            continue
        entry = by_command[_path(run) or run.command]
        entry[0] += 1
        entry[1] += run.llm_calls
        entry[2] += run.tokens
    if not by_command:
        return
    print()
    print(render_table(
        ["COMMAND", "RUNS", "CALLS", "TOKENS"],
        [[name, str(v[0]), str(v[1]), f"{v[2]:,}"]
         for name, v in sorted(by_command.items(), key=lambda kv: -kv[1][2])],
    ))


# --- follow -----------------------------------------------------------------------

def _follow() -> None:
    """Tail today's file, one rendered line per new record, until Ctrl-C.

    Re-resolves the path every poll so a run that crosses midnight UTC keeps printing
    from the next day's file (§4)."""
    print(dim(f"Following {journal.runs_dir()} — Ctrl-C to stop."))
    path, handle = None, None
    try:
        while True:
            current = journal.today_path()
            if current != path:
                if handle is not None:
                    handle.close()
                path, handle = current, _open_at_end(current)
            for line in handle or []:
                rec = journal.parse_record(line)
                if rec is not None:
                    _print_follow_line(rec)
            time.sleep(FOLLOW_POLL_SECONDS)
    except KeyboardInterrupt:
        print()
    finally:
        if handle is not None:
            handle.close()


def _open_at_end(path: str):
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    handle.seek(0, os.SEEK_END)
    return handle


def _print_follow_line(rec: Dict[str, Any]) -> None:
    moment = _local(rec.get("ts"))
    stamp = moment.strftime("%H:%M:%S") if moment else "??:??:??"
    color = _LEVEL_COLOR.get(str(rec.get("lvl")), str)
    print(f"{dim(stamp)}  {rec.get('run')}  "
          f"{pad_visible(cyan(str(rec.get('ev') or '')), 14)}  "
          f"{color(str(rec.get('msg') or ''))}")


# --- handlers ---------------------------------------------------------------------

def run_journal(args: argparse.Namespace) -> None:
    """The listing, the cost rollup, or a tail — `journal show` has the detail view."""
    if getattr(args, "follow", False):
        _follow()
        return
    if getattr(args, "cost", False):
        _print_cost(args)
        return
    runs, hidden = _select(args)
    _print_listing(
        runs, getattr(args, "limit", DEFAULT_LIMIT), hidden,
        verbose=getattr(args, "verbose", False),
    )


def run_journal_show(args: argparse.Namespace) -> None:
    """One run in full, found by a unique id prefix — git-style (§7)."""
    prefix = str(args.run or "").strip().lower()
    runs = _collect(None, None)
    matches = sorted(
        (run for run_id, run in runs.items() if run_id.startswith(prefix) and run.started),
        key=lambda r: r.started, reverse=True,
    )
    if not matches:
        print(red(f"No run whose id starts with '{prefix}'."))
        return
    if len(matches) > 1:
        print(yellow(f"'{prefix}' matches {len(matches)} runs:"))
        _print_listing(matches, len(matches))
        return
    _print_detail(matches[0], runs)


def run_journal_prune(args: argparse.Namespace) -> None:
    """Force the retention sweep the daily stamp file would otherwise gate (§10)."""
    days, exchanges = journal.prune()
    print(green(
        f"Pruned {days} journal day file(s) and {exchanges} LLM exchange file(s)."
    ))


def add_journal_parser(subparsers):
    # journal command & subparsers — the operator's view of what the app did
    # (DESIGN_logging.md §7).
    journal_parser = subparsers.add_parser(
        "journal",
        help="What this app did, and when: one line per command run",
        description=(
            "The operational record beside the training one: which command ran, from "
            "where, how long it took, what it called out to and how it ended. Runs are "
            "listed newest first; name a run's id (a unique prefix is enough) to read "
            "everything it wrote, including the traceback if it failed. The read-only "
            "views — 'list', 'show', 'status', 'journal', help — are left out unless -a "
            "asks for them, and each command line is clipped to the width of the screen "
            "unless -v asks for it. Kept for logging.retain_days and never read by the "
            "app itself."
        )
    )
    # Read-only at the top level, so a bare `journal` lists rather than printing help
    # (DESIGN_cli_noargs.md §a3).
    journal_parser.set_defaults(func=run_journal)
    # `tm journal 5a0e` is the short form of `tm journal show 5a0e`: `prune` stays a
    # real sub-command, which is what keeps it from being read as a run id (§7).
    journal_parser.set_defaults(_fallback_subcommand="show")
    journal_parser.add_argument(
        "-n", dest="limit", type=int, default=DEFAULT_LIMIT, metavar="N",
        help=f"How many runs to list (default {DEFAULT_LIMIT}; 0 for all)"
    )
    # `none`: each side of the window is bounded only where the athlete bounded it, so
    # `-d 2026-08-01..` reads as "everything since then" rather than being narrowed to a
    # default span. `-d 7d` is still the last seven days.
    add_selector_args(journal_parser, date=True, direction="none")
    journal_parser.add_argument(
        "--source", metavar="SRC",
        help="Only runs from one front-end: cli, repl, bot, push, route, web, test"
    )
    journal_parser.add_argument(
        # Not dest="command": that is the top-level sub-parser's own dest, and a
        # sub-parser's namespace is copied wholesale over its parent's.
        "--command", dest="command_filter", metavar="CMD",
        help="Only runs of one command, e.g. --command \"workout adapt\""
    )
    journal_parser.add_argument(
        "--failed", action="store_true",
        help="Only runs that failed, were killed, or logged a warning or an error"
    )
    journal_parser.add_argument(
        "-a", "--all", action="store_true", dest="show_all",
        help="Also list the read-only views: 'list', 'show', 'status', 'journal', help"
    )
    journal_parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print each command line in full rather than clipping it to the screen"
    )
    journal_parser.add_argument(
        "--cost", action="store_true",
        help="Roll the model calls up by model and by command instead of listing runs"
    )
    journal_parser.add_argument(
        "--follow", action="store_true",
        help="Print each new record as it lands, until Ctrl-C"
    )
    journal_subparsers = journal_parser.add_subparsers(
        dest="subcommand", help="Journal sub-commands"
    )

    # journal show
    journal_show = journal_subparsers.add_parser(
        "show",
        help="Everything one run wrote, found by id prefix",
        description=(
            "One run in full: where it ran, every event it recorded as an offset from "
            "its start, the LLM exchange files it produced, the traceback if it failed, "
            "and the runs it spawned. The id may be any unique prefix; an ambiguous one "
            "lists what it matched. Same as naming the id straight after 'journal'."
        )
    )
    journal_show.set_defaults(func=run_journal_show)
    journal_show.add_argument("run", metavar="RUN", help="Run id, or a unique prefix of one")

    # journal prune
    journal_prune = journal_subparsers.add_parser(
        "prune",
        help="Delete journal days and LLM exchanges past their retention now",
        description=(
            "Forces the sweep that otherwise runs at most once a day, off the first "
            "command to finish after midnight UTC. Deletes journal files older than "
            "logging.retain_days and LLM exchange files older than "
            "logging.retain_exchange_days; nothing else in either directory is touched."
        )
    )
    journal_prune.set_defaults(func=run_journal_prune)
