import argparse
import sys
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray, cmd,
    visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, today_str as _today_str, today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data


def _print_goal(g: dict) -> None:
    """Prints one goal in the 'goal list' format."""
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


def run_goal_add(args: argparse.Namespace) -> None:
    """Creates a new objective goal via command line."""
    sports_str = ",".join(args.sport)
    goal_id = cli.db.add_objective(
        title=args.title,
        target_date=args.date,
        sport_type=sports_str,
        description=args.desc,
        priority=args.priority,
        status='active'
    )
    goal = cli.db.get_objective(goal_id)
    if goal:
        _print_goal(goal)
    print(
        green("Goal added successfully. Run " + cmd("plan generate")
              + " to generate training cycles.")
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
    updated = cli.db.get_objective(args.id)
    if updated:
        _print_goal(updated)
    print(
        green("Goal updated successfully. Run " + cmd("plan generate")
              + " to regenerate training cycles if needed.")
    )


def run_goal_list() -> None:
    """Lists all active and past training objective goals."""
    goals = cli.db.get_objectives()
    print(bold(cyan("=== TRAINING OBJECTIVES / GOALS ===")))
    for g in goals:
        _print_goal(g)


def run_goal_rm(args: argparse.Namespace) -> None:
    """Deletes an objective goal by ID."""
    cli.db.delete_objective(args.id)
    print(green(f"Goal with ID {args.id} removed successfully."))


def run_goal_wipe(args: argparse.Namespace) -> None:
    """Wipes all goals from the database after confirmation."""
    if not args.yes:
        if not cli.prompt.confirm(
            "Are you sure you want to wipe all training objectives?", danger=True
        ):
            print("Wipe cancelled.")
            return

    cli.db.wipe_objectives()
    print(green("All training objectives wiped successfully."))


def add_goal_parser(subparsers):
    # goal command & subparsers
    goal_parser = subparsers.add_parser(
        "goal",
        aliases=["g"],
        help="Manage training objectives / goals of your training plan"
    )
    goal_subparsers = goal_parser.add_subparsers(dest="subcommand", help="Goal sub-commands")
    
    # goal add
    g_add = goal_subparsers.add_parser(
        "add", aliases=["a"], help="Add a new training objective/goal"
    )
    g_add.add_argument("--title", required=True, help="Goal title (e.g. Marathon)")
    g_add.add_argument("--date", required=True, help="Target event date (YYYY-MM-DD)")
    g_add.add_argument(
        "--sport", required=True, nargs="+",
        choices=["running", "road_biking", "hiking", "strength_training", "yoga", "ski_touring"],
        help="Sport types (one or more)"
    )
    g_add.add_argument("--desc", default="", help="Description")
    g_add.add_argument("--priority", type=int, default=1, help="Goal priority (1 = highest)")

    # goal edit
    g_edit = goal_subparsers.add_parser(
        "edit", aliases=["e"], help="Edit an existing goal/objective"
    )
    g_edit.add_argument("id", type=int, help="Goal ID to edit")
    g_edit.add_argument("--title", help="New goal title")
    g_edit.add_argument("--date", help="New target event date (YYYY-MM-DD)")
    g_edit.add_argument(
        "--sport", nargs="+",
        choices=["running", "road_biking", "hiking", "strength_training", "yoga", "ski_touring"],
        help="New sport types (one or more)"
    )
    g_edit.add_argument("--desc", help="New description")
    g_edit.add_argument("--priority", type=int, help="New priority (1 = highest)")
    g_edit.add_argument(
        "--status", choices=["active", "completed", "archived"],
        help="New status ('active', 'completed', 'archived')"
    )
    
    # goal rm
    g_rm = goal_subparsers.add_parser("rm", aliases=["r"], help="Remove a goal by ID")
    g_rm.add_argument("id", type=int, help="Goal ID to remove")
    
    # goal list
    goal_subparsers.add_parser("list", aliases=["l"], help="Show all training objectives")

    # goal wipe
    g_wipe = goal_subparsers.add_parser(
        "wipe", advanced=True, help="Wipe all training objectives")
    g_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    

    return goal_parser
