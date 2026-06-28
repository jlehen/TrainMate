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


def _prompt(label: str, default: Optional[str] = None) -> Optional[str]:
    """Reads a line interactively, returning `default` on empty input or EOF."""
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return default
    return raw or default


def _context_summary(metric: str, value: Optional[float], label: str) -> str:
    """Builds the calendar entry / LLM blurb. A provided label gets the value
    appended in parentheses (`severe heatwave (38.0)`); with no label it's
    `Metric: value` (matching ingested events like `Alcohol: 2.0`)."""
    if label:
        return f"{label} ({value})" if value is not None else label
    base = metric[:1].upper() + metric[1:]
    return f"{base}: {value}" if value is not None else base


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
    if not metric:
        existing = [m["metric"] for m in cli.db.list_context_metrics()]
        if existing:
            print(dim(f"Existing metrics: {', '.join(existing)}"))
        metric = _prompt("Metric (e.g. heat, sleep, stress)")
        if not metric:
            print(red("A metric is required."))
            sys.exit(1)

    value = args.value

    label = (args.label or " ".join(args.text or [])).strip()
    if not label:
        label = (_prompt("Label (optional)", default="") or "").strip()
    text = _context_summary(metric, value, label)

    count = 0
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
        count += 1

    span = start if start == end else f"{start}..{end}"
    print(green(
        f"Logged '{text}' as {magenta(metric)} over {cyan(span)} "
        f"({count} day{'s' if count != 1 else ''})."
    ))


def _context_line(row: dict) -> str:
    """One-line rendering of a context row for `list`."""
    val = f" = {row['value']}" if row.get("value") is not None else ""
    text = row.get("text") or ""
    return (
        f"ID: {row['id']} | {cyan(row['date'])} | {magenta(row['metric'])}{val}"
        + (f" — {text}" if text else "")
    )


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
            try:
                confirm = input(f"Remove these {len(rows)} signals? [y/N]: ").strip().lower()
            except EOFError:
                confirm = "n"
            if confirm not in ("y", "yes"):
                print("Removal cancelled.")
                return

    removed = 0
    for row in rows:
        if row.get("google_event_id"):
            cli.calendar_syncer.delete_event(row["google_event_id"])
        cli.db.delete_daily_context(row["id"])
        removed += 1
    print(green(f"Removed {removed} context signal{'s' if removed != 1 else ''}."))
