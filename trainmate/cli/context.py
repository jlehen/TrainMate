"""Handlers for the `context` command: first-party authoring of daily-context
signals (DESIGN_context_authoring.md).

TrainMate writes the same tagged `source=<context_tag>` all-day events the
external syncer produces, then mirrors them into `daily_context` locally so they
show up before the next `data pull`. Removal deletes the calendar event too, so a
full re-pull (`data wipe --calendar`) can't resurrect it.
"""
import argparse
import sys
from datetime import datetime, timedelta
from typing import Iterator, Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.util import bold, dim, green, red, yellow, cyan, magenta
from trainmate.util import today_str as _today_str


def _date_range(start: str, end: str) -> Iterator[str]:
    """Yields each YYYY-MM-DD from start to end inclusive."""
    d = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= last:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def _context_summary(metric: str, value: Optional[float], label: str) -> str:
    """Builds the calendar entry / LLM blurb. A provided label gets the value
    appended in parentheses (`severe heatwave (38.0)`); with no label it's
    `Metric: value` (matching ingested events like `Alcohol: 2.0`)."""
    if label:
        return f"{label} ({value})" if value is not None else label
    base = metric[:1].upper() + metric[1:]
    return f"{base}: {value}" if value is not None else base


def _context_line(row: dict) -> str:
    """One-line rendering of a context row for `list` (and the `add` echo)."""
    val = f" = {row['value']}" if row.get("value") is not None else ""
    text = row.get("text") or ""
    return (
        f"ID: {row['id']} | {cyan(row['date'])} | {magenta(row['metric'])}{val}"
        + (f" — {text}" if text else "")
    )


def run_context_add(args: argparse.Namespace) -> None:
    """Authors a daily-context signal over a day or date range, writing one tagged
    all-day event per day and mirroring the rows locally."""
    if not cli.calendar_syncer.calendar_id:
        print(red("No Google Calendar configured; cannot author context events."))
        sys.exit(1)

    start = args.from_date or _today_str()
    end = args.until_date or start
    if end < start:
        print(red("--until is before --from."))
        sys.exit(1)

    metric = args.metric
    value = args.value

    label = (args.label or " ".join(args.text or [])).strip()
    text = _context_summary(metric, value, label)

    written = []
    for day in _date_range(start, end):
        existing = cli.db.get_daily_context(day, day, metric=metric)
        existing_id = existing[0]["google_event_id"] if existing else None
        event_id = cli.calendar_syncer.add_context_event(
            day, metric, value, text, existing_id
        )
        if not event_id:
            print(red(f"Failed to write context event for {day}."))
            continue
        cli.db.upsert_daily_context_by_event(event_id, day, metric, value, text)
        written.extend(cli.db.get_daily_context(day, day, metric=metric))

    for row in written:
        print(_context_line(row))
    count = len(written)
    print(green(
        f"Logged '{text}' as {magenta(metric)} "
        f"({count} day{'s' if count != 1 else ''})."
    ))


def run_context_list(args: argparse.Namespace) -> None:
    """Lists context signals within a date window (default: the coach's metrics
    lookback), optionally filtered by metric."""
    end = args.until_date or _today_str()
    if args.from_date:
        start = args.from_date
    else:
        window = config.metrics_lookback_days
        start = (
            datetime.strptime(end, "%Y-%m-%d").date() - timedelta(days=window - 1)
        ).strftime("%Y-%m-%d")

    rows = cli.db.get_daily_context(start, end, metric=args.metric)
    title = f"=== DAILY CONTEXT {start}..{end}"
    if args.metric:
        title += f" [{args.metric}]"
    print(bold(cyan(title + " ===")))
    if not rows:
        print(dim("(none)"))
        return
    for row in rows:
        print(_context_line(row))


def run_context_list_metrics(args: argparse.Namespace) -> None:
    """Shows the distinct metrics in use with counts and date span."""
    metrics = cli.db.list_context_metrics()
    print(bold(cyan("=== CONTEXT METRICS ===")))
    if not metrics:
        print(dim("(none)"))
        return
    for m in metrics:
        span = (
            m["first_date"] if m["first_date"] == m["last_date"]
            else f"{m['first_date']}..{m['last_date']}"
        )
        print(
            f"{magenta(m['metric'])}: {m['count']} "
            f"day{'s' if m['count'] != 1 else ''} ({cyan(span)})"
        )


def run_context_rm(args: argparse.Namespace) -> None:
    """Removes context signal(s) by id, or by date range + metric. Deletes the
    calendar event before the local row so a full re-pull can't resurrect it."""
    if args.ids:
        rows = []
        for cid in args.ids:
            row = cli.db.get_daily_context_by_id(cid)
            if row:
                rows.append(row)
            else:
                print(yellow(f"No context signal with ID {cid}."))
    else:
        if not (args.from_date or args.until_date or args.metric):
            print(red(
                "Refusing to remove everything: give IDs, or narrow with "
                "--from/--until/--metric."
            ))
            sys.exit(1)
        rows = cli.db.get_daily_context(args.from_date, args.until_date, args.metric)
        if not rows:
            print(yellow("No matching context signals."))
            return
        if len(rows) > 1 and not args.yes:
            for row in rows:
                print(_context_line(row))
            if not cli.prompt.confirm(
                f"Remove these {len(rows)} signals?", danger=True
            ):
                print("Removal cancelled.")
                return

    removed = 0
    for row in rows:
        if row.get("google_event_id"):
            cli.calendar_syncer.delete_event(row["google_event_id"])
        cli.db.delete_daily_context(row["id"])
        removed += 1
    print(green(f"Removed {removed} context signal{'s' if removed != 1 else ''}."))


def add_context_parser(subparsers):
    # context command & subparsers — first-party daily-context authoring
    context_parser = subparsers.add_parser(
        "context",
        aliases=["ctx"],
        help="Author/list/remove daily-context signals (heat, sleep, stress, …)",
        description=(
            "Manage external daily-context signals — the same tagged Google Calendar "
            "events that 'data pull' ingests into the coach's view. 'add' authors them "
            "(one all-day event per day), 'rm' removes them (calendar event + local row), "
            "'list' and 'list-metrics' inspect what's recorded. TrainMate stays "
            "domain-agnostic: a metric is opaque free text."
        )
    )
    context_subparsers = context_parser.add_subparsers(
        dest="subcommand", help="Context sub-commands"
    )

    # context add
    ctx_add = context_subparsers.add_parser(
        "add", aliases=["a"],
        help="Author a context signal over a day or date range",
        description=(
            "Write a tagged all-day context event per day in the range and mirror it "
            "locally. Defaults to today; the label is optional. Re-adding "
            "the same (date, metric) updates in place rather than duplicating."
        )
    )
    ctx_add.add_argument(
        "text", nargs="*", help="Human label/summary (e.g. severe heatwave)"
    )
    ctx_add.add_argument(
        "-l", "--label",
        help="Human label/summary (alternative to the positional text; takes "
             "precedence, and avoids word-splitting for multi-word labels)"
    )
    ctx_add.add_argument(
        "-m", "--metric", required=True,
        help="Opaque category, e.g. heat, sleep, stress"
    )
    ctx_add.add_argument(
        "--value", type=float, metavar="N",
        help="Optional free numeric magnitude (severity, °C, count — uninterpreted)"
    )
    ctx_add.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Start date (default: today)"
    )
    ctx_add.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="End date (default: --from)"
    )

    # context rm
    ctx_rm = context_subparsers.add_parser(
        "rm", aliases=["r"],
        help="Remove signal(s) by ID, or by date range + metric",
        description=(
            "Delete context signal(s). Pass row IDs, or narrow with "
            "--from/--until/--metric. The calendar event is deleted too, so a full "
            "re-pull cannot resurrect it."
        )
    )
    ctx_rm.add_argument("ids", nargs="*", type=int, help="Context row IDs to remove")
    ctx_rm.add_argument("-m", "--metric", help="Restrict range removal to this metric")
    ctx_rm.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Start date for range removal"
    )
    ctx_rm.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="End date for range removal"
    )
    ctx_rm.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation for multi-row removal"
    )

    # context list
    ctx_list = context_subparsers.add_parser(
        "list", aliases=["l"],
        help="List context signals (default window: the coach's metrics lookback)"
    )
    ctx_list.add_argument("-m", "--metric", help="Filter to a single metric")
    ctx_list.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Start date (default: metrics_lookback_days before --until)"
    )
    ctx_list.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="End date (default: today)"
    )

    # context list-metrics
    context_subparsers.add_parser(
        "list-metrics", aliases=["lm"],
        help="Show distinct metrics in use with counts and date span"
    )

    return context_parser
