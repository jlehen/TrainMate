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


def run_goal_add(args: argparse.Namespace) -> None:
    """Creates a new objective goal via command line."""
    sports_str = ",".join(args.sport)
    cli.db.add_objective(
        title=args.title,
        target_date=args.date,
        sport_type=sports_str,
        description=args.desc,
        priority=args.priority,
        status='active'
    )
    print(
        green(f"Goal '{args.title}' added successfully. Run ")
        + bold(green("'plan generate'"))
        + green(" to generate training cycles.")
    )


def run_goal_edit(args: argparse.Namespace) -> None:
    """Edits an existing goal/objective."""
    goal = cli.db.get_objective(args.id)
    if not goal:
        print(red(f"Goal with ID {args.id} not found."))
        sys.exit(1)

    kwargs = {}
    if args.title is not None:
        kwargs['title'] = args.title
    if args.date is not None:
        kwargs['target_date'] = args.date
    if args.sport is not None:
        kwargs['sport_type'] = ",".join(args.sport)
    if args.desc is not None:
        kwargs['description'] = args.desc
    if args.priority is not None:
        kwargs['priority'] = args.priority
    if args.status is not None:
        kwargs['status'] = args.status

    if not kwargs:
        print(yellow("No fields to update. Provide at least one field to change."))
        return

    cli.db.update_objective(args.id, **kwargs)
    print(
        green(f"Goal with ID {args.id} updated successfully. Run ")
        + bold(green("'plan generate'"))
        + green(" to regenerate training cycles if needed.")
    )


def run_goal_list() -> None:
    """Lists all active and past training objective goals."""
    goals = cli.db.get_objectives()
    print(bold(cyan("=== TRAINING OBJECTIVES / GOALS ===")))
    for g in goals:
        sport_str = g['sport_type']
        status_tag = g['status'].upper()
        if g['status'] == 'active':
            status_disp = green(f"[{status_tag}]")
            title_disp = cyan(g['title'])
        else:
            status_disp = gray(f"[{status_tag}]")
            title_disp = gray(g['title'])
        print(
            f"{status_disp} ID: {g['id']} | {title_disp} "
            f"({sport_str}) on {cyan(g['target_date'])} (Priority: {g['priority']})"
        )
        if g.get('description'):
            print(format_labeled_block("  Description:", g['description']))


def run_goal_rm(args: argparse.Namespace) -> None:
    """Deletes an objective goal by ID."""
    cli.db.delete_objective(args.id)
    print(green(f"Goal with ID {args.id} removed successfully."))


def run_goal_wipe(args: argparse.Namespace) -> None:
    """Wipes all goals from the database after confirmation."""
    if not args.yes:
        try:
            confirm = input(
                "Are you sure you want to wipe all training objectives? [y/N]: "
            ).strip().lower()
        except EOFError:
            confirm = 'n'
        if confirm not in ('y', 'yes'):
            print("Wipe cancelled.")
            return

    cli.db.wipe_objectives()
    print(green("All training objectives wiped successfully."))
