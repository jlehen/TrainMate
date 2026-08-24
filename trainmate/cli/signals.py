"""Handlers for the `signal` command: first-party authoring of daily signals
(DESIGN_signal_authoring.md).

TrainMate writes the same tagged `source=<signal_tag>` all-day events the
external syncer produces, then mirrors them into `daily_signals` locally so they
show up before the next `data pull`. Removal deletes the calendar event too, so a
full re-pull (`data wipe --calendar`) can't resurrect it.
"""
import argparse
import sys
from datetime import datetime, timedelta
from typing import Iterator, Optional
from trainmate import runtime
from trainmate.config import config
from trainmate.util import bold, dim, green, red, yellow, cyan, magenta
from trainmate.util import today_str as _today_str
from trainmate.cli.selectors import add_selector_args, has_selector, resolve_window


def _date_range(start: str, end: str) -> Iterator[str]:
    """Yields each YYYY-MM-DD from start to end inclusive."""
    d = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= last:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def _signal_summary(metric: str, value: Optional[float], label: str) -> str:
    """Builds the calendar entry / LLM blurb. A provided label gets the value
    appended in parentheses (`severe heatwave (38.0)`); with no label it's
    `Metric: value` (matching ingested events like `Alcohol: 2.0`)."""
    if label:
        return f"{label} ({value})" if value is not None else label
    base = metric[:1].upper() + metric[1:]
    return f"{base}: {value}" if value is not None else base


def _signal_line(row: dict) -> str:
    """One-line rendering of a signal row for `list` (and the `add` echo)."""
    val = f" = {row['value']}" if row.get("value") is not None else ""
    text = row.get("text") or ""
    return (
        f"ID: {row['id']} | {cyan(row['date'])} | {magenta(row['metric'])}{val}"
        + (f" — {text}" if text else "")
    )


def run_signal_add(args: argparse.Namespace) -> None:
    """Authors a daily signal over a day or date range, writing one tagged
    all-day event per day and mirroring the rows locally."""
    if not runtime.calendar_syncer.calendar_id:
        print(red("No Google Calendar configured; cannot author signal events."))
        sys.exit(1)

    start, end = resolve_window(args)
    start = start or _today_str()
    if end is None:
        print(red("A signal needs a bounded range: -d DATE or -d A..B."))
        sys.exit(1)

    metric = args.metric
    value = args.value

    label = (args.label or " ".join(args.text or [])).strip()
    text = _signal_summary(metric, value, label)

    written = []
    for day in _date_range(start, end):
        existing = runtime.db.get_daily_signals(day, day, metric=metric)
        existing_id = existing[0]["google_event_id"] if existing else None
        event_id = runtime.calendar_syncer.add_signal_event(
            day, metric, value, text, existing_id
        )
        if not event_id:
            print(red(f"Failed to write signal event for {day}."))
            continue
        runtime.db.upsert_daily_signal_by_event(event_id, day, metric, value, text)
        written.extend(runtime.db.get_daily_signals(day, day, metric=metric))

    for row in written:
        print(_signal_line(row))
    count = len(written)
    print(green(
        f"Logged '{text}' as {magenta(metric)} "
        f"({count} day{'s' if count != 1 else ''})."
    ))


def run_signal_list(args: argparse.Namespace) -> None:
    """Lists signals within a date window (default: the coach's metrics
    lookback), optionally filtered by metric."""
    start, end = resolve_window(args)
    metric = args.metric or args.metric_target

    rows = runtime.db.get_daily_signals(start, end, metric=metric)
    title = f"=== DAILY SIGNALS {start}..{end}"
    if metric:
        title += f" [{metric}]"
    print(bold(cyan(title + " ===")))
    if not rows:
        print(dim("(none)"))
        return
    for row in rows:
        print(_signal_line(row))


def run_signal_list_metrics(args: argparse.Namespace) -> None:
    """Shows the distinct metrics in use with counts and date span."""
    metrics = runtime.db.list_signal_metrics()
    print(bold(cyan("=== SIGNAL METRICS ===")))
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


def run_signal_rm(args: argparse.Namespace) -> None:
    """Removes signal(s) by id, or by date range + metric. Deletes the
    calendar event before the local row so a full re-pull can't resurrect it."""
    ids = [t for t in (args.targets or []) if t.isdigit()]
    metric = args.metric or next((t for t in (args.targets or []) if not t.isdigit()), None)
    if ids:
        rows = []
        for cid in ids:
            row = runtime.db.get_daily_signal_by_id(int(cid))
            if row:
                rows.append(row)
            else:
                print(yellow(f"No signal with ID {cid}."))
    else:
        if not (has_selector(args) or metric):
            print(red(
                "Refusing to remove everything: give IDs, a metric, or narrow with "
                "-d/-m/-M/-g."
            ))
            sys.exit(1)
        start, end = resolve_window(args)
        rows = runtime.db.get_daily_signals(start, end, metric)
        if not rows:
            print(yellow("No matching signals."))
            return
        if len(rows) > 1 and not args.yes:
            for row in rows:
                print(_signal_line(row))
            if not runtime.prompt.confirm(
                f"Remove these {len(rows)} signals?", danger=True
            ):
                print("Removal cancelled.")
                return

    removed = 0
    for row in rows:
        if row.get("google_event_id"):
            runtime.calendar_syncer.delete_event(row["google_event_id"])
        runtime.db.delete_daily_signal(row["id"])
        removed += 1
    print(green(f"Removed {removed} signal{'s' if removed != 1 else ''}."))


def add_signal_parser(subparsers):
    # signal command & subparsers — first-party daily-signal authoring
    signal_parser = subparsers.add_parser(
        "signal",
        help="Author/list/remove daily signals (heat, sleep, stress, …)",
        description=(
            "Manage external daily signals — the same tagged Google Calendar "
            "events that 'data pull' ingests into the coach's view. 'add' authors them "
            "(one all-day event per day), 'rm' removes them (calendar event + local row), "
            "'list' and 'list-metrics' inspect what's recorded. TrainMate stays "
            "domain-agnostic: a metric is opaque free text."
        )
    )
    signal_subparsers = signal_parser.add_subparsers(
        dest="subcommand", help="Signal sub-commands"
    )

    # signal add
    sig_add = signal_subparsers.add_parser(
        "add",
        help="Author a signal over a day or date range",
        description=(
            "Write a tagged all-day signal event per day in the range and mirror it "
            "locally. Defaults to today; the label is optional. Re-adding "
            "the same (date, metric) updates in place rather than duplicating."
        )
    )
    sig_add.set_defaults(func=run_signal_add)
    sig_add.add_argument(
        "metric", help="Opaque category, e.g. heat, sleep, stress"
    )
    sig_add.add_argument(
        "text", nargs="*", help="Human label/summary (e.g. severe heatwave)"
    )
    sig_add.add_argument(
        "-l", "--label",
        help="Human label/summary (alternative to the positional text; takes "
             "precedence, and avoids word-splitting for multi-word labels)"
    )
    sig_add.add_argument(
        "--value", type=float, metavar="N",
        help="Optional free numeric magnitude (severity, °C, count — uninterpreted)"
    )
    # No -m/-M here: a signal is written over days, and a block is not a day the athlete
    # can hand to the calendar. -d must stay bounded for the same reason.
    add_selector_args(sig_add, direction="none", default="today")

    # signal rm
    sig_rm = signal_subparsers.add_parser(
        "rm",
        help="Remove signal(s) by ID, or by date range + metric",
        description=(
            "Delete signal(s). Name row IDs or a metric, and/or narrow with "
            "-d/-m/-M/-g. The calendar event is deleted too, so a full re-pull cannot "
            "resurrect it."
        )
    )
    sig_rm.set_defaults(func=run_signal_rm)
    sig_rm.add_argument(
        "targets", nargs="*", metavar="TARGET",
        help="Signal row IDs to remove, or a metric name to remove within the window"
    )
    sig_rm.add_argument("--metric", help="Restrict range removal to this metric")
    add_selector_args(sig_rm, meso=True, macro=True, goal=True, direction="none")
    sig_rm.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation for multi-row removal"
    )

    # signal list
    sig_list = signal_subparsers.add_parser(
        "list", aliases=["l"],
        help="List signals (default window: the coach's metrics lookback)"
    )
    sig_list.set_defaults(func=run_signal_list)
    sig_list.add_argument(
        "metric_target", nargs="?", metavar="METRIC", help="Filter to a single metric"
    )
    sig_list.add_argument("--metric", help="Filter to a single metric (same as the positional)")
    add_selector_args(
        sig_list, meso=True, macro=True, goal=True, direction="backward",
        default=f"{config.metrics_lookback_days}d", span_days=config.metrics_lookback_days,
    )

    # signal list-metrics
    _list_metrics_parser = signal_subparsers.add_parser(
        "list-metrics", aliases=["lm"],
        help="Show distinct metrics in use with counts and date span"
    )
    _list_metrics_parser.set_defaults(func=run_signal_list_metrics)

    return signal_parser
