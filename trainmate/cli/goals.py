import argparse
import sys
from trainmate import runtime
from trainmate.util import (
    bold, green, red, cyan, gray, cmd, format_labeled_block, fmt_date,
    today_str as _today_str, notice,
)
from trainmate.cli.common import (
    is_simple_render, print_plan_cascade, report_unhonored, simple_goal_lines,
)
from trainmate.db.objectives import goal_state, GOAL_UPCOMING, ARCHIVED
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
    # 'on' a date something happens on; 'by ~' a date that only bounds the plan.
    if g.get('date_type') == 'horizon':
        date_disp = f"by ~{cyan(fmt_date(g['target_date']))} (horizon)"
    else:
        date_disp = f"on {cyan(fmt_date(g['target_date']))}"
    print(
        f"{status_disp} ID: {g['id']} | {title_disp} "
        f"({sport_str}) {date_disp}"
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
        status='active',
        date_type=args.date_type
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
        notice(f"Goal with ID {args.id} not found.", red)
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
    if args.status is not None:
        kwargs['status'] = args.status
    if args.date_type is not None:
        kwargs['date_type'] = args.date_type

    if not kwargs:
        notice("No fields to update. Provide at least one field to change.")
        return

    was_archived = goal.get('status') == ARCHIVED
    runtime.db.update_objective(args.id, **kwargs)
    updated = runtime.db.get_objective(args.id)
    if updated:
        _print_goal(updated)

    now_archived = (updated or {}).get('status') == ARCHIVED
    if now_archived and not was_archived:
        _report_archived_sessions(runtime.coach_service.goal_archive(args.id))
    elif was_archived and not now_archived:
        _offer_reinstated_sessions(args.id)

    print(
        green("Goal updated successfully. Run " + cmd("plan generate")
              + " to regenerate training cycles if needed.")
    )


def _report_archived_sessions(result: dict) -> None:
    """Says what calling the goal off stood down, and what it deliberately left alone.

    Untagged sessions belong to no plan version, so no goal can claim them (§14); saying
    so beats both sweeping them and staying silent."""
    if result['archived_workouts']:
        print(green(
            f"Stood down {result['archived_workouts']} upcoming session(s) "
            f"({fmt_date(result['first_date'])} → {fmt_date(result['last_date'])}). "
            "Reinstate the goal to bring them back."
        ))
    else:
        print(gray("No upcoming sessions belonged to this goal."))
    if result['untagged']:
        notice(
            f"Left {result['untagged']} upcoming session(s) in place: they carry no plan "
            "version, so no goal owns them. Remove them with " + cmd("workout rm")
            + " if they were for this goal.",
        )


def _offer_reinstated_sessions(objective_id: int) -> None:
    """Asks before resurrecting the sessions the goal's archival stood down (§14).

    Always asked: a goal reinstated months later would otherwise silently re-push
    sessions from a plan that no longer suits the athlete."""
    if not runtime.prompt.confirm(
        "Restore the upcoming sessions that were stood down when this goal was "
        "called off?", default=True
    ):
        print(gray("Sessions left archived. " + cmd("workout batches") + " lists them."))
        return
    result = runtime.coach_service.goal_reinstate(objective_id)
    if not result['restored_workouts']:
        print(gray("No archived sessions were left to restore."))
        return
    print(green(
        f"Restored {result['restored_workouts']} session(s) "
        f"({fmt_date(result['first_date'])} → {fmt_date(result['last_date'])})."
    ))
    report_unhonored(result['unhonored'])


def run_goal_list(args=None) -> None:
    """Lists all active and past training objective goals."""
    goals = runtime.db.get_objectives()
    # Companion prose instead of the tagged list (DESIGN_bot_simple_frontend.md §11).
    if is_simple_render():
        for line in simple_goal_lines(goals, _today_str()):
            print(line)
        return
    print(bold(cyan("=== GOALS ===")))
    for g in goals:
        _print_goal(g)


def run_goal_rm(args: argparse.Namespace) -> None:
    """Deletes an objective goal by ID, with everything the delete cascades to.

    The escape hatch for a goal entered by mistake, not the way to call one off — that is
    `goal edit --status archived`, which keeps the history and is reversible (§14). The
    inventory is printed first because the cascade reaches further than the goal row."""
    goal = runtime.db.get_objective(args.id)
    if not goal:
        notice(f"Goal with ID {args.id} not found.", red)
        sys.exit(1)

    if not args.yes:
        notice(f"Removing goal '{goal['title']}' (ID {args.id}) also deletes:")
        print_plan_cascade(args.id)
        print(gray(
            "To call the goal off reversibly instead, use "
            + cmd(f"goal edit {args.id} --status archived") + "."
        ))
        if not runtime.prompt.confirm("Delete it anyway?", danger=True):
            print("Removal cancelled.")
            return

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
    g_add.set_defaults(func=run_goal_add)
    g_add.add_argument("title", help="Goal title (e.g. Marathon)")
    g_add.add_argument("date", help="Target event date (YYYY-MM-DD)")
    g_add.add_argument(
        "sport", nargs="+", metavar="SPORT",
        choices=CANONICAL_SPORTS,
        help="Sport types, one or more: %(choices)s"
    )
    g_add.add_argument("--desc", default="", help="Description")
    g_add.add_argument(
        "--date-type", dest="date_type", choices=["event", "horizon"], default="event",
        help="What the date means: 'event' = something happens on that day, so the plan "
             "peaks for it; 'horizon' = just how far you want to train toward the goal — "
             "no taper or peak pinned to the date."
    )

    # goal edit
    g_edit = goal_subparsers.add_parser(
        "edit", help="Edit an existing goal"
    )
    g_edit.set_defaults(func=run_goal_edit)
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
    # No 'completed': a goal whose date has passed is completed by that fact (§12). This
    # flag only says whether the goal was called off.
    g_edit.add_argument(
        "--status", choices=["active", "archived"],
        help="Call the goal off ('archived') or reinstate it ('active'). A goal completes "
             "on its own once its target date passes."
    )
    g_edit.add_argument(
        "--date-type", dest="date_type", choices=["event", "horizon"],
        help="What the date means: 'event' = something happens on that day, so the plan "
             "peaks for it; 'horizon' = just how far you want to train toward the goal — "
             "no taper or peak pinned to the date."
    )
    
    # goal rm
    g_rm = goal_subparsers.add_parser(
        "rm",
        help="Delete a goal and its plan history (to call a goal off reversibly, "
             "use 'goal edit --status archived')"
    )
    g_rm.set_defaults(func=run_goal_rm)
    g_rm.add_argument("id", type=int, help="Goal ID to remove")
    g_rm.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # goal list
    _list_parser = goal_subparsers.add_parser("list", help="Show all goals")
    _list_parser.set_defaults(func=run_goal_list)

    # goal wipe
    g_wipe = goal_subparsers.add_parser(
        "wipe", advanced=True, help="Wipe all goals")
    g_wipe.set_defaults(func=run_goal_wipe)
    g_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    

    return goal_parser
