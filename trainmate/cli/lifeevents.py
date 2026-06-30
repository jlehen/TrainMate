import argparse
import sys
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    color_acwr, visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, today_str as _today_str, today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data


def run_lifeevent_add(args: argparse.Namespace) -> None:
    """Creates a new life event via command line."""
    cli.db.add_lifeevent(
        title=args.title,
        start_date=args.start,
        end_date=args.end,
        event_type=args.type,
        impact_description=args.desc
    )
    print(
        green(f"Life event '{args.title}' logged. This will be factored in when running ")
        + bold(green("'plan generate'"))
        + green(" or ")
        + bold(green("'workout adapt'"))
        + green(".")
    )


def run_lifeevent_edit(args: argparse.Namespace) -> None:
    """Edits an existing life event."""
    event = cli.db.get_lifeevent(args.id)
    if not event:
        print(red(f"Life event with ID {args.id} not found."))
        sys.exit(1)

    kwargs = {}
    if args.title is not None:
        kwargs['title'] = args.title
    if args.start is not None:
        kwargs['start_date'] = args.start
    if args.end is not None:
        kwargs['end_date'] = args.end
    if args.type is not None:
        kwargs['event_type'] = args.type
    if args.desc is not None:
        kwargs['impact_description'] = args.desc

    if not kwargs:
        print(yellow("No fields to update. Provide at least one field to change."))
        return

    cli.db.update_lifeevent(args.id, **kwargs)
    print(
        green(f"Life event with ID {args.id} updated successfully. Run ")
        + bold(green("'plan generate'"))
        + green(" or ")
        + bold(green("'workout adapt'"))
        + green(" to factor in the changes.")
    )


def _lifeevent_print(e: dict, show_impact: bool = True) -> None:
    """Prints a single life event formatting its fields."""
    print(
        f"ID: {e['id']} | {yellow(e['title'])} ({magenta(e['event_type'])}): "
        f"{cyan(e['start_date'])} to {cyan(e['end_date'])}"
    )
    if show_impact and e.get('impact_description'):
        print(format_labeled_block("  Impact:", e['impact_description']))


def run_lifeevent_list(args: argparse.Namespace) -> None:
    """Lists all logged training life events."""
    events = cli.db.get_lifeevents()
    print(bold(cyan("=== ATHLETE LIFE EVENTS ===")))
    for e in events:
        _lifeevent_print(e, show_impact=args.verbose)


def run_lifeevent_show(args: argparse.Namespace) -> None:
    """Displays a specific life event and its impact description by ID."""
    event = cli.db.get_lifeevent(args.id)
    if not event:
        print(red(f"Life event with ID {args.id} not found."))
        return

    _lifeevent_print(event, show_impact=True)


def run_lifeevent_rm(args: argparse.Namespace) -> None:
    """Deletes a life event by ID."""
    cli.db.delete_lifeevent(args.id)
    print(green(f"Life event with ID {args.id} removed successfully."))


def run_lifeevent_wipe(args: argparse.Namespace) -> None:
    """Wipes all life events from the database after confirmation."""
    if not args.yes:
        if not cli.prompt.confirm(
            "Are you sure you want to wipe all life events?", danger=True
        ):
            print("Wipe cancelled.")
            return

    cli.db.wipe_lifeevents()
    print(green("All life events wiped successfully."))
