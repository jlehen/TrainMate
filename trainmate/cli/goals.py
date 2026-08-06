import argparse
import sys
from trainmate import runtime
from trainmate.util import bold, green, red, yellow, cyan, gray, cmd, format_labeled_block
from trainmate.db.objectives import goal_state, GOAL_UPCOMING
from trainmate.sports import CANONICAL_SPORTS


def _print_goal(g: dict) -> None:
    """Prints one goal in the 'goal list' format.

    The state is derived, never read off the row: UPCOMING is the live one, COMPLETED
    means the date has passed, ARCHIVED means it was called off (§12)."""
    sport_str = g['sport_type']
    state = goal_state(g)
    status_tag = state.upper()
    if state == GOAL_UPCOMING:
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
    goal_id = runtime.db.add_objective(
        title=args.title,
        target_date=args.date,
        sport_type=sports_str,
        description=args.desc,
        priority=args.priority,
        status='active'
    )
    goal = runtime.db.get_objective(goal_id)
    if goal:
        _print_goal(goal)
    print(
        green("Goal added successfully. Run " + cmd("plan generate")
              + " to generate training cycles.")
    )


def run_goal_edit(args: argparse.Namespace) -> None:
    """Edits an existing goal/objective."""
    goal = runtime.db.get_objective(args.id)
    if not goal:
        print(red(f"Goal with ID {args.id} not found."))
        sys.exit(1)

    kwargs = {}
    if args.title is not None:
        kwargs['title'] = args.title
    if args.target_date is not None:
        kwargs['target_date'] = args.target_date
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

    runtime.db.update_objective(args.id, **kwargs)
    updated = runtime.db.get_objective(args.id)
    if updated:
        _print_goal(updated)
    print(
        green("Goal updated successfully. Run " + cmd("plan generate")
              + " to regenerate training cycles if needed.")
    )


def run_goal_list() -> None:
    """Lists all active and past training objective goals."""
    goals = runtime.db.get_objectives()
    print(bold(cyan("=== GOALS ===")))
    for g in goals:
        _print_goal(g)


def run_goal_rm(args: argparse.Namespace) -> None:
    """Deletes an objective goal by ID."""
    runtime.db.delete_objective(args.id)
    print(green(f"Goal with ID {args.id} removed successfully."))


def run_goal_wipe(args: argparse.Namespace) -> None:
    """Wipes all goals from the database after confirmation."""
    if not args.yes:
        if not runtime.prompt.confirm(
            "Are you sure you want to wipe all goals?", danger=True
        ):
            print("Wipe cancelled.")
            return

    runtime.db.wipe_objectives()
    print(green("All goals wiped successfully."))


def add_goal_parser(subparsers):
    # goal command & subparsers
    goal_parser = subparsers.add_parser(
        "goal",
        help="Manage the goals your training plan is built around"
    )
    goal_subparsers = goal_parser.add_subparsers(dest="subcommand", help="Goal sub-commands")
    
    # goal add
    g_add = goal_subparsers.add_parser(
        "add", help="Add a new goal"
    )
    g_add.add_argument("title", help="Goal title (e.g. Marathon)")
    g_add.add_argument("date", help="Target event date (YYYY-MM-DD)")
    g_add.add_argument(
        "sport", nargs="+", metavar="SPORT",
        choices=CANONICAL_SPORTS,
        help="Sport types, one or more: %(choices)s"
    )
    g_add.add_argument("--desc", default="", help="Description")
    g_add.add_argument("--priority", type=int, default=1, help="Goal priority (1 = highest)")

    # goal edit
    g_edit = goal_subparsers.add_parser(
        "edit", help="Edit an existing goal"
    )
    g_edit.add_argument("id", type=int, help="Goal ID to edit")
    g_edit.add_argument("--title", help="New goal title")
    # --target-date, not --date: -d/--date is the selector vocabulary everywhere else, and
    # this one writes a value rather than filtering (DESIGN_cli_selectors.md §5).
    g_edit.add_argument(
        "--target-date", dest="target_date", help="New target event date (YYYY-MM-DD)"
    )
    g_edit.add_argument(
        "--sport", nargs="+", metavar="SPORT",
        choices=CANONICAL_SPORTS,
        help="New sport types, one or more: %(choices)s"
    )
    g_edit.add_argument("--desc", help="New description")
    g_edit.add_argument("--priority", type=int, help="New priority (1 = highest)")
    # No 'completed': a goal whose date has passed is completed by that fact (§12). This
    # flag only says whether the goal was called off.
    g_edit.add_argument(
        "--status", choices=["active", "archived"],
        help="Call the goal off ('archived') or reinstate it ('active'). A goal completes "
             "on its own once its target date passes."
    )
    
    # goal rm
    g_rm = goal_subparsers.add_parser("rm", help="Remove a goal by ID")
    g_rm.add_argument("id", type=int, help="Goal ID to remove")
    
    # goal list
    goal_subparsers.add_parser("list", help="Show all goals")

    # goal wipe
    g_wipe = goal_subparsers.add_parser(
        "wipe", advanced=True, help="Wipe all goals")
    g_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    

    return goal_parser
