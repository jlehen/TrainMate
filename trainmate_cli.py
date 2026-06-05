import argparse
import sys
import textwrap
from datetime import datetime, timezone, timedelta
from trainmate.db import db
from trainmate.google_sheets import sheets_reader
from trainmate.google_calendar import calendar_syncer
from trainmate.coach import coach_engine
from trainmate.config import config


def main() -> None:
    """Entry point for the TrainMate Command Line Interface."""
    parser = argparse.ArgumentParser(
        description="TrainMate - Local Training Coach CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # status command
    status_parser = subparsers.add_parser(
        "status",
        aliases=["s"],
        help="Show current athlete status, active goals, recent metrics, and memories"
    )
    status_parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show all training objectives/goals and life events"
    )
    
    # goal command & subparsers
    goal_parser = subparsers.add_parser(
        "goal",
        aliases=["g"],
        help="Manage training objectives / goals"
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
    g_wipe = goal_subparsers.add_parser("wipe", help="Wipe all training objectives")
    g_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # lifeevent command & subparsers
    lifeevent_parser = subparsers.add_parser(
        "lifeevent",
        aliases=["le", "e"],
        help="Manage life events"
    )
    lifeevent_subparsers = lifeevent_parser.add_subparsers(
        dest="subcommand", help="Life event sub-commands"
    )
    
    # lifeevent add
    c_add = lifeevent_subparsers.add_parser("add", aliases=["a"], help="Add a new life event")
    c_add.add_argument("--title", required=True, help="Life event title (e.g. Vacation to Spain)")
    c_add.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    c_add.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    c_add.add_argument(
        "--type", required=True, choices=["business_trip", "vacation", "party", "other"],
        help="Life event type"
    )
    c_add.add_argument("--desc", default="", help="Description/Impact description")

    # lifeevent edit
    le_edit = lifeevent_subparsers.add_parser(
        "edit", aliases=["e"], help="Edit an existing life event"
    )
    le_edit.add_argument("id", type=int, help="Life event ID to edit")
    le_edit.add_argument("--title", help="New life event title")
    le_edit.add_argument("--start", help="New start date (YYYY-MM-DD)")
    le_edit.add_argument("--end", help="New end date (YYYY-MM-DD)")
    le_edit.add_argument(
        "--type", choices=["business_trip", "vacation", "party", "other"],
        help="New life event type"
    )
    le_edit.add_argument("--desc", help="New description/Impact description")
    
    # lifeevent rm
    c_rm = lifeevent_subparsers.add_parser("rm", aliases=["r"], help="Remove a life event by ID")
    c_rm.add_argument("id", type=int, help="Life event ID to remove")
    
    # lifeevent list
    le_list = lifeevent_subparsers.add_parser(
        "list", aliases=["l"], help="Show all logged life events"
    )
    le_list.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show life event details including impact description"
    )

    # lifeevent show
    le_show = lifeevent_subparsers.add_parser(
        "show", aliases=["s"], help="Show details of a life event by ID"
    )
    le_show.add_argument("id", type=int, help="Life event ID to display")

    # lifeevent wipe
    le_wipe = lifeevent_subparsers.add_parser("wipe", help="Wipe all life events")
    le_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # plan command & subparsers
    plan_parser = subparsers.add_parser(
        "plan",
        aliases=["p"],
        help="Manage and consult the periodized training plan (macrocycles & mesocycles)"
    )
    plan_subparsers = plan_parser.add_subparsers(
        dest="subcommand", help="Plan sub-commands"
    )
    
    # plan generate
    p_gen = plan_subparsers.add_parser(
        "generate", aliases=["g"],
        help=(
            "Generate or adapt the periodized training plan strategy "
            "(macrocycles & mesocycles)"
        )
    )
    p_gen.add_argument(
        "-f", "--force", action="store_true",
        help="Force regeneration of the macrocycle/mesocycle strategy"
    )
    
    # plan show
    plan_subparsers.add_parser(
        "show", aliases=["s"],
        help="Show the active macrocycle and mesocycles periodization strategy"
    )

    # plan wipe
    p_wipe = plan_subparsers.add_parser("wipe", help="Wipe all periodization plans")
    p_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # workout command & subparsers
    workout_parser = subparsers.add_parser(
        "workout",
        aliases=["w"],
        help="Manage workouts (microcycles)"
    )
    workout_subparsers = workout_parser.add_subparsers(
        dest="subcommand", help="Workout sub-commands"
    )
    
    # workout list
    workout_subparsers.add_parser(
        "list", aliases=["l"], help="Show all planned workouts"
    )
    
    # workout generate
    workout_subparsers.add_parser(
        "generate", aliases=["g"],
        help="Generate the 4-week workouts (microcycles) based on the active strategy"
    )
    
    # workout rm
    w_rm = workout_subparsers.add_parser(
        "rm", aliases=["r"], help="Remove a workout by ID"
    )
    w_rm.add_argument("id", type=int, help="Workout ID to remove")
    
    # workout adapt
    w_adapt = workout_subparsers.add_parser(
        "adapt", aliases=["a"],
        help="Run the daily Garmin check for today (syncs adapted workouts to Calendar)"
    )
    w_adapt.add_argument("--date", help="Date in YYYY-MM-DD format (defaults to UTC today)")
    w_adapt.add_argument(
        "-y", "--yes", "--auto", action="store_true", dest="auto",
        help="Apply proposed adaptations automatically without prompting"
    )
    
    # workout push
    workout_subparsers.add_parser(
        "push", aliases=["p"],
        help="Commit all local planned workouts to Google Calendar"
    )

    # workout wipe
    w_wipe = workout_subparsers.add_parser(
        "wipe",
        help="Wipe all workouts from the database and Google Calendar"
    )
    w_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # metrics command & subparsers
    metrics_parser = subparsers.add_parser(
        "metrics",
        aliases=["m"],
        help="Manage and sync athlete metrics"
    )
    metrics_subparsers = metrics_parser.add_subparsers(
        dest="subcommand", help="Metrics sub-commands"
    )
    
    # metrics pull
    metrics_subparsers.add_parser(
        "pull",
        help="Fetch latest activities and metrics from Sheets"
    )

    # metrics wipe
    m_wipe = metrics_subparsers.add_parser("wipe", help="Wipe all metrics from the database")
    m_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # Parse the arguments
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
        
    cmd = args.command.lower()

    if cmd in ("plan", "p", "workout", "w"):
        metrics = db.get_metrics_cache()
        if not metrics:
            try:
                confirm = input(
                    "Garmin metrics have not been pulled yet. "
                    "Would you like to pull them now? [y/N]: "
                ).strip().lower()
            except EOFError:
                confirm = 'n'
            if confirm in ('y', 'yes'):
                run_metrics_pull()

    if cmd in ("status", "s"):
        run_status(verbose=args.verbose)
    elif cmd in ("goal", "g"):
        if not args.subcommand:
            goal_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("add", "a"):
            run_goal_add(args)
        elif sub in ("edit", "e"):
            run_goal_edit(args)
        elif sub in ("rm", "r"):
            run_goal_rm(args)
        elif sub in ("list", "l"):
            run_goal_list()
        elif sub == "wipe":
            run_goal_wipe(args)
    elif cmd in ("lifeevent", "le", "e"):
        if not args.subcommand:
            lifeevent_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("add", "a"):
            run_lifeevent_add(args)
        elif sub in ("edit", "e"):
            run_lifeevent_edit(args)
        elif sub in ("rm", "r"):
            run_lifeevent_rm(args)
        elif sub in ("list", "l"):
            run_lifeevent_list(args)
        elif sub in ("show", "s"):
            run_lifeevent_show(args)
        elif sub == "wipe":
            run_lifeevent_wipe(args)
    elif cmd in ("workout", "w"):
        if not args.subcommand:
            workout_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("list", "l"):
            run_workout_list()
        elif sub in ("generate", "g"):
            run_workout_generate()
        elif sub in ("rm", "r"):
            run_workout_rm(args)
        elif sub in ("adapt", "a"):
            run_workout_adapt(args)
        elif sub in ("push", "p"):
            run_workout_push()
        elif sub == "wipe":
            run_workout_wipe(args)
    elif cmd in ("metrics", "m"):
        if not args.subcommand:
            metrics_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub == "pull":
            run_metrics_pull()
        elif sub == "wipe":
            run_metrics_wipe(args)
    elif cmd in ("plan", "p"):
        if not args.subcommand:
            plan_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("generate", "g"):
            run_plan_generate(args)
        elif sub in ("show", "s"):
            run_plan_show()
        elif sub == "wipe":
            run_plan_wipe(args)
    else:
        print(f"Unknown command: '{cmd}'")
        parser.print_help()
        sys.exit(1)


# ==============================================================================
# Status Command
# ==============================================================================

def run_status(verbose: bool = False) -> None:
    """Displays current athlete goals, Garmin metrics, baselines, and memories."""
    print("=== TRAINMATE ATHLETE STATUS ===")
    
    # Active Goal & Periodization Strategy
    objectives = db.get_objectives(status='active')
    if objectives:
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]
        sport_str = next_goal['sport_type'].upper()
        
        # Calculate days remaining
        today = datetime.now(timezone.utc).date()
        target_date = datetime.strptime(next_goal['target_date'], "%Y-%m-%d").date()
        days_rem = (target_date - today).days
        days_rem_str = f" ({days_rem} days remaining)" if days_rem >= 0 else ""
        
        print(f"\nNext Objective: {next_goal['title']} ({sport_str})")
        print(f"Target Date   : {next_goal['target_date']}{days_rem_str}")
        print(f"Description   : {next_goal.get('description', '')}")
        
        # Query active mesocycle
        macro = db.get_macrocycle_for_objective(next_goal['id'])
        if macro:
            current_hash = coach_engine._get_config_hash()
            if macro.get('config_hash') != current_hash:
                print("\nWarning: config.yaml has changed since the active periodization plan "
                      "was generated.\nRun 'plan generate' to regenerate.")
            
            mesos = db.get_mesocycles_for_macrocycle(macro['id'])
            active_meso = None
            for m in mesos:
                start = datetime.strptime(m['start_date'], "%Y-%m-%d").date()
                end = datetime.strptime(m['end_date'], "%Y-%m-%d").date()
                if start <= today <= end:
                    active_meso = m
                    break
            
            if active_meso:
                print(f"Active Cycle  : {active_meso['name']} ({active_meso['start_date']} to "
                      f"{active_meso['end_date']})")
                print(f"Cycle Focus   : {active_meso['focus']}")
            else:
                print("Active Cycle  : None active today (outside mesocycle boundaries)")
        else:
            print(
                "Active Cycle  : No periodization strategy established. "
                "Run 'plan generate' first."
            )
    else:
        print("\nNext Objective: None (TrainMate needs at least one goal to start planning)")

    # Recent Garmin metrics
    metrics = db.get_metrics_cache()
    if metrics:
        last_metrics = metrics[-1]
        print(f"\nRecent Garmin Metrics ({last_metrics['date']}):")
        print(f"- Resting HR : {last_metrics['rhr']} bpm")
        print(f"- Overnight HRV: {last_metrics['hrv']} ms")
        print(f"- Sleep Score: {last_metrics['sleep_score']}")
        print(f"- Stress     : {last_metrics['stress']}")
        
        acute = last_metrics['acute_workload'] or 0.0
        chronic = last_metrics['chronic_workload'] or 0.0
        acwr = last_metrics['acwr'] or 0.0
        print(f"- ACWR       : {acwr:.2f} (Acute: {acute:.1f}, Chronic: {chronic:.1f})")
        
        # Baselines
        baseline = db.get_baseline(last_metrics['date'])
        if baseline:
            print("Baselines (28-day):")
            print(
                f"- RHR Mean   : {baseline['rhr_baseline_mean']:.1f} "
                f"(std: {baseline['rhr_baseline_std']:.2f})"
            )
            print(
                f"- HRV Mean   : {baseline['hrv_baseline_mean']:.1f} "
                f"(std: {baseline['hrv_baseline_std']:.2f})"
            )
            print(
                f"- Sleep Mean : {baseline['sleep_baseline_mean']:.1f} "
                f"(std: {baseline['sleep_baseline_std']:.2f})"
            )
    else:
        print("\nRecent Garmin Metrics: No cached metrics. Run 'metrics pull' first.")

    # Coach Memory
    strategy = db.get_coach_memory("training_strategy")
    learnings = db.get_coach_memory("athlete_learnings")
    print("\nCoach Memory:")
    print(f"- Strategy  : {strategy or 'Not established'}")
    print(f"- Learnings : {learnings or 'None yet'}")

    if verbose:
        goals = db.get_objectives()
        print("\nGoals:")
        if not goals:
            print("- None")
        for g in goals:
            sport_str = g['sport_type']
            print(
                f"- [{g['status'].upper()}] ID: {g['id']} | {g['title']} "
                f"({sport_str}) on {g['target_date']} (Priority: {g['priority']})"
            )
            if g.get('description'):
                print(f"  Description: {g['description']}")

        events = db.get_lifeevents()
        print("\nLife Events:")
        if not events:
            print("- None")
        for e in events:
            print(
                f"- ID: {e['id']} | {e['title']} ({e['event_type']}): "
                f"{e['start_date']} to {e['end_date']}"
            )
            if e.get('impact_description'):
                print(f"  Impact: {e['impact_description']}")

    print("\n================================")


# ==============================================================================
# Goal Command
# ==============================================================================

def run_goal_add(args: argparse.Namespace) -> None:
    """Creates a new objective goal via command line."""
    sports_str = ",".join(args.sport)
    db.add_objective(
        title=args.title,
        target_date=args.date,
        sport_type=sports_str,
        description=args.desc,
        priority=args.priority,
        status='active'
    )
    print(
        f"Goal '{args.title}' added successfully. "
        f"Run 'plan generate' to generate training cycles."
    )


def run_goal_edit(args: argparse.Namespace) -> None:
    """Edits an existing goal/objective."""
    goal = db.get_objective(args.id)
    if not goal:
        print(f"Goal with ID {args.id} not found.")
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
        print("No fields to update. Provide at least one field to change.")
        return

    db.update_objective(args.id, **kwargs)
    print(
        f"Goal with ID {args.id} updated successfully. "
        "Run 'plan generate' to regenerate training cycles if needed."
    )


def run_goal_list() -> None:
    """Lists all active and past training objective goals."""
    goals = db.get_objectives()
    print("=== TRAINING OBJECTIVES / GOALS ===")
    for g in goals:
        sport_str = g['sport_type']
        print(
            f"[{g['status'].upper()}] ID: {g['id']} | {g['title']} "
            f"({sport_str}) on {g['target_date']} (Priority: {g['priority']})"
        )
        if g.get('description'):
            print(f"  Description: {g['description']}")


def run_goal_rm(args: argparse.Namespace) -> None:
    """Deletes an objective goal by ID."""
    db.delete_objective(args.id)
    print(f"Goal with ID {args.id} removed successfully.")


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

    db.wipe_objectives()
    print("All training objectives wiped successfully.")


# ==============================================================================
# Lifeevent Command
# ==============================================================================

def run_lifeevent_add(args: argparse.Namespace) -> None:
    """Creates a new life event via command line."""
    db.add_lifeevent(
        title=args.title,
        start_date=args.start,
        end_date=args.end,
        event_type=args.type,
        impact_description=args.desc
    )
    print(
        f"Life event '{args.title}' logged. "
        f"This will be factored in when running 'plan generate' or 'workout adapt'."
    )


def run_lifeevent_edit(args: argparse.Namespace) -> None:
    """Edits an existing life event."""
    event = db.get_lifeevent(args.id)
    if not event:
        print(f"Life event with ID {args.id} not found.")
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
        print("No fields to update. Provide at least one field to change.")
        return

    db.update_lifeevent(args.id, **kwargs)
    print(
        f"Life event with ID {args.id} updated successfully. "
        "Run 'plan generate' or 'workout adapt' to factor in the changes."
    )


def _lifeevent_print(e: dict, show_impact: bool = True) -> None:
    """Prints a single life event formatting its fields."""
    print(
        f"ID: {e['id']} | {e['title']} ({e['event_type']}): "
        f"{e['start_date']} to {e['end_date']}"
    )
    if show_impact and e.get('impact_description'):
        print(f"  Impact: {e['impact_description']}")


def run_lifeevent_list(args: argparse.Namespace) -> None:
    """Lists all logged training life events."""
    events = db.get_lifeevents()
    print("=== ATHLETE LIFE EVENTS ===")
    for e in events:
        _lifeevent_print(e, show_impact=args.verbose)


def run_lifeevent_show(args: argparse.Namespace) -> None:
    """Displays a specific life event and its impact description by ID."""
    event = db.get_lifeevent(args.id)
    if not event:
        print(f"Life event with ID {args.id} not found.")
        return

    _lifeevent_print(event, show_impact=True)


def run_lifeevent_rm(args: argparse.Namespace) -> None:
    """Deletes a life event by ID."""
    db.delete_lifeevent(args.id)
    print(f"Life event with ID {args.id} removed successfully.")


def run_lifeevent_wipe(args: argparse.Namespace) -> None:
    """Wipes all life events from the database after confirmation."""
    if not args.yes:
        try:
            confirm = input(
                "Are you sure you want to wipe all life events? [y/N]: "
            ).strip().lower()
        except EOFError:
            confirm = 'n'
        if confirm not in ('y', 'yes'):
            print("Wipe cancelled.")
            return

    db.wipe_lifeevents()
    print("All life events wiped successfully.")


# ==============================================================================
# Plan Command
# ==============================================================================

def run_plan_generate(args: argparse.Namespace) -> None:
    """Executes the AI periodization strategy plan generation command."""
    # Make sure we have latest metrics cached
    metrics = db.get_metrics_cache()
    if not metrics:
        print("Warning: Metrics cache is empty. Proceeding without Garmin metrics.")
        
    try:
        objectives = db.get_objectives(status='active')
        if objectives:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]
            macro = db.get_macrocycle_for_objective(next_goal['id'])
            if macro:
                current_hash = coach_engine._get_config_hash()
                if macro.get('config_hash') != current_hash and not args.force:
                    try:
                        confirm = input(
                            "\nConfiguration in config.yaml has changed since the last "
                            "plan generation.\n"
                            "Would you like to regenerate the periodization strategy? [y/N]: "
                        ).strip().lower()
                    except EOFError:
                        confirm = 'n'
                    if confirm in ('y', 'yes'):
                        args.force = True
                    else:
                        print("Keeping current periodization strategy. "
                              "Updating configuration hash in database.")
                        db.update_macrocycle_config_hash(macro['id'], current_hash)

        strategy, mesocycles = coach_engine.generate_periodization_plan(force=bool(args.force))
        print("\n=== PERIODIZATION PLAN GENERATED BY COACH ===")
        print(f"Strategy:\n{strategy}\n")
        print(f"Generated {len(mesocycles)} mesocycles. Save complete.")
        print("Run 'workout generate' to schedule workouts based on this plan.")
    except Exception as e:
        print(f"Error during plan generation: {e}")


def run_plan_show() -> None:
    """Displays the active training macrocycle and mesocycles periodization timeline."""
    objectives = db.get_objectives(status='active')
    if not objectives:
        print("No active goals found. TrainMate needs at least one objective.")
        return
        
    objectives.sort(key=lambda x: str(x['target_date']))
    next_goal = objectives[0]
    
    macrocycle = db.get_macrocycle_for_objective(next_goal['id'])
    if not macrocycle:
        print(f"No active periodization strategy found for goal '{next_goal['title']}'.")
        print("Run 'plan generate' to create one.")
        return
        
    mesocycles = db.get_mesocycles_for_macrocycle(macrocycle['id'])
    
    print("\n=== ACTIVE PERIODIZATION STRATEGY ===")
    sport_str = next_goal['sport_type'].upper()
    print(f"Objective: {next_goal['title']} ({sport_str}) on {next_goal['target_date']}")
    print(f"Overall Strategy:\n{macrocycle['strategy']}\n")
    print("Periodization Timeline:")
    
    today = datetime.now(timezone.utc).date()
    
    for m in mesocycles:
        start = datetime.strptime(m['start_date'], "%Y-%m-%d").date()
        end = datetime.strptime(m['end_date'], "%Y-%m-%d").date()
        
        total_days = (end - start).days + 1
        if total_days <= 0:
            total_days = 1
            
        bar_length = 20
        if end < today:
            status_str = "[DONE]  "
            bar = "=" * bar_length
            extra = ""
        elif start <= today <= end:
            status_str = "[ACTIVE]"
            days_passed = (today - start).days + 1
            days_passed = max(1, min(days_passed, total_days))
            filled = round(bar_length * days_passed / total_days)
            filled = max(0, min(filled, bar_length))
            bar = "=" * filled + "." * (bar_length - filled)
            extra = f" (Day {days_passed}/{total_days})"
        else:
            status_str = "[FUTURE]"
            bar = "." * bar_length
            extra = ""
            
        if total_days >= 7:
            weeks = total_days / 7
            if weeks.is_integer():
                duration_desc = f"({int(weeks)} weeks)"
            else:
                duration_desc = f"({weeks:.1f} weeks)"
        else:
            duration_desc = f"({total_days} days)"
            
        prefix = "|->" if status_str == "[ACTIVE]" else "|--"
        print(f"{prefix} {status_str} {m['name']:<15} ({m['start_date']} -> {m['end_date']}) "
              f"[{bar}]{extra} {duration_desc}")
              
        focus_lines = textwrap.wrap(m['focus'], width=80)
        for line in focus_lines:
            print(f"            {line}")
        print("            " + "-" * 40)


def run_plan_wipe(args: argparse.Namespace) -> None:
    """Wipes all plans from the database after confirmation."""
    if not args.yes:
        try:
            confirm = input(
                "Are you sure you want to wipe all periodization plans? [y/N]: "
            ).strip().lower()
        except EOFError:
            confirm = 'n'
        if confirm not in ('y', 'yes'):
            print("Wipe cancelled.")
            return

    db.wipe_plans()
    print("All periodization plans wiped successfully.")


# ==============================================================================
# Workout Command
# ==============================================================================

def run_workout_adapt(args: argparse.Namespace) -> None:
    # Executes the daily workout Garmin adaptation checks command.
    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    metrics = db.get_metrics_cache()
    if not metrics:
        print("Warning: Metrics cache is empty. Skipping Garmin metrics sync.")
    else:
        print("Syncing latest metrics from Google Sheets first...")
        run_metrics_pull()

    # Display rolling trajectory
    try:
        history_days = config.metrics_history_days
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_date = (date_obj - timedelta(days=history_days - 1)).strftime("%Y-%m-%d")
        metrics_history = db.get_metrics_cache(start_date=start_date, end_date=date_str)
        
        print("\n=== METRICS TRAJECTORY (PAST 5 DAYS) ===")
        print(f"{'Date':<12} | {'HRV (ms)':<8} | {'RHR (bpm)':<9} | {'Sleep':<5} | {'ACWR':<5}")
        print("-" * 50)
        for m in metrics_history:
            base = db.get_baseline(m['date'])
            hrv_marker = ""
            rhr_marker = ""
            sleep_marker = ""
            
            if base:
                if m['hrv'] is not None and base['hrv_baseline_mean'] is not None:
                    sd = base['hrv_baseline_std'] or 1.0
                    if m['hrv'] < (base['hrv_baseline_mean'] - sd):
                        hrv_marker = " (v)"
                if m['rhr'] is not None and base['rhr_baseline_mean'] is not None:
                    sd = base['rhr_baseline_std'] or 1.0
                    if m['rhr'] > (base['rhr_baseline_mean'] + max(3.0, sd)):
                        rhr_marker = " (^)"
                if m['sleep_score'] is not None and m['sleep_score'] < 60:
                    sleep_marker = " (v)"
                    
            hrv_str = f"{m['hrv'] or 'N/A'}{hrv_marker}"
            rhr_str = f"{m['rhr'] or 'N/A'}{rhr_marker}"
            sleep_str = f"{m['sleep_score'] or 'N/A'}{sleep_marker}"
            acwr_str = f"{m['acwr']:.2f}" if m['acwr'] is not None else "N/A"
            print(f"{m['date']:<12} | {hrv_str:<8} | {rhr_str:<9} | {sleep_str:<5} | {acwr_str:<5}")
        print("((v) suppressed/poor, (^) elevated compared to baseline)\n")
    except Exception as e:
        print(f"Warning: Could not display metrics trajectory: {e}")

    print(f"Evaluating daily Garmin metrics adaptation for {date_str}...")
    try:
        reason, proposed_workouts = coach_engine.adapt(date_str)
        print(f"\nDecision Summary:\n{reason}")
        
        if not proposed_workouts:
            print("\nAll metrics are green and workout plan is on track. No changes recommended.")
            return

        print("\nPROPOSED WORKOUT ADAPTATIONS:")
        print(
            f"{'Date':<12} | {'Sport':<12} | {'Original Workout':<25} | "
            f"{'Adapted Workout':<25} | {'Duration/RPE/TSS':<16}"
        )
        print("-" * 100)
        for pw in proposed_workouts:
            existing = db.get_workout(pw['date'], pw['sport_type'])
            orig_title = existing['title'] if existing else "[None]"
            orig_stats = ""
            if existing:
                orig_stats = (
                    f"{existing.get('duration_minutes') or 0}m/"
                    f"RPE{existing.get('rpe') or 0}/"
                    f"TSS{existing.get('tss') or 0}"
                )
            new_stats = (
                f"{pw.get('duration_minutes') or 0}m/"
                f"RPE{pw.get('rpe') or 0}/"
                f"TSS{pw.get('tss') or 0}"
            )
            stats_diff = f"{orig_stats} -> {new_stats}" if orig_stats else new_stats
            print(
                f"{pw['date']:<12} | {pw['sport_type'].upper():<12} | {orig_title:<25} | "
                f"{pw['title']:<25} | {stats_diff:<16}"
            )

        if args.auto:
            confirm = "y"
        else:
            confirm = input(
                "\nApply these adaptations to your training plan and sync to Calendar? [y/N]: "
            ).strip().lower()

        if confirm == 'y':
            print("\nApplying adaptations...")
            all_dates = [pw['date'] for pw in proposed_workouts]
            start_date_adapt = min(all_dates)
            end_date_adapt = max(all_dates)
            coach_engine.apply_adaptations(
                proposed_workouts, reason, start_date_adapt, end_date_adapt
            )
            print("Adaptations applied and synced to calendar successfully.")
        else:
            print("\nAdaptations discarded.")

    except Exception as e:
        print(f"Error executing daily adaptation: {e}")


def run_workout_generate() -> None:
    """Executes the AI workout generation command based on active strategy."""
    # Make sure we have latest metrics cached
    metrics = db.get_metrics_cache()
    if not metrics:
        print("Warning: Metrics cache is empty. Proceeding without Garmin metrics.")
        
    try:
        objectives = db.get_objectives(status='active')
        if objectives:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]
            macro = db.get_macrocycle_for_objective(next_goal['id'])
            if macro:
                current_hash = coach_engine._get_config_hash()
                if macro.get('config_hash') != current_hash:
                    try:
                        confirm = input(
                            "\nWarning: config.yaml has changed since the active "
                            "periodization plan was generated.\n"
                            "Generating workouts using the out-of-date plan might "
                            "result in incorrect training targets.\n"
                            "It is highly recommended to run 'plan generate' first. "
                            "Proceed anyway? [y/N]: "
                        ).strip().lower()
                    except EOFError:
                        confirm = 'n'
                    if confirm not in ('y', 'yes'):
                        print("Workout generation cancelled. Please run 'plan generate' first.")
                        return
                    else:
                        print("Proceeding. Updating configuration hash in database.")
                        db.update_macrocycle_config_hash(macro['id'], current_hash)

        reasoning, workouts = coach_engine.generate_workouts()
        print("\n=== WORKOUTS GENERATED BY COACH ===")
        print(f"Reasoning:\n{reasoning}\n")
        print(f"Generated {len(workouts)} workouts starting from today. Save complete.")
        print("Run 'workout push' to commit this plan to Google Calendar.")
    except Exception as e:
        print(f"Error during workout generation: {e}")


def run_workout_list() -> None:
    """Lists all stored workouts chronologically."""
    workouts = db.get_workouts()
    print("=== WORKOUT SCHEDULE ===")
    for w in workouts:
        mod_marker = " [ADAPTED]" if w['status'] == 'modified' or w['modification_reason'] else ""
        sync_marker = " [SYNCED]" if w['status'] == 'synced' else ""
        print(
            f"ID: {w['id']} | {w['date']} | {w['sport_type'].upper()} | "
            f"{w['title']}{mod_marker}{sync_marker}"
        )
        print(f"  Description: {w['description']}")
        if w.get('modification_reason'):
            print(f"  Reason: {w['modification_reason']}")
        print("-" * 40)


def run_workout_push() -> None:
    """Synchronizes planned workouts with Google Calendar."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    planned_workouts = db.get_workouts(start_date=today_str)
    # Filter to only unsynced/planned ones
    unsynced = [w for w in planned_workouts if w['status'] in ('planned', 'modified')]
    
    if not unsynced:
        print("No new or modified workouts to sync. Run 'plan generate' to generate a schedule.")
        return
        
    print(f"Syncing {len(unsynced)} workouts to Google Calendar...")
    try:
        calendar_syncer.sync_multiple(unsynced)
        print("Google Calendar synchronization completed.")
    except Exception as e:
        print(f"Error syncing to Google Calendar: {e}")


def run_workout_rm(args: argparse.Namespace) -> None:
    """Deletes a planned workout by its database ID."""
    workout = db.get_workout_by_id(args.id)
    if not workout:
        print(f"Workout with ID {args.id} not found.")
        return
        
    if workout.get('google_event_id') and workout.get('status') == 'synced':
        print("Workout is synced to Google Calendar. Attempting to delete calendar event...")
        if workout['google_event_id'] is not None:
            calendar_syncer.delete_workout_event(workout['google_event_id'])
        
    db.delete_workout_by_id(args.id)
    print(f"Workout with ID {args.id} ('{workout['title']}') removed successfully.")


def run_workout_wipe(args: argparse.Namespace) -> None:
    """Wipes all workouts from the database and Google Calendar after confirmation."""
    if not args.yes:
        try:
            msg = (
                "Are you sure you want to wipe all workouts "
                "(including Google Calendar events)? [y/N]: "
            )
            confirm = input(msg).strip().lower()
        except EOFError:
            confirm = 'n'
        if confirm not in ('y', 'yes'):
            print("Wipe cancelled.")
            return

    workouts = db.get_workouts()
    synced_workouts = [w for w in workouts if w.get('google_event_id')]
    if synced_workouts:
        print(f"Deleting {len(synced_workouts)} events from Google Calendar...")
        for w in synced_workouts:
            ge_id = w['google_event_id']
            if ge_id:
                calendar_syncer.delete_workout_event(ge_id)

    db.wipe_workouts()
    print("All workouts wiped successfully.")


# ==============================================================================
# Metrics Command
# ==============================================================================

def run_metrics_pull() -> None:
    """Pulls athlete metrics and activities from Google Sheets."""
    try:
        sheets_reader.sync_data()
    except Exception as e:
        print(f"Error syncing Google Sheets: {e}")


def run_metrics_wipe(args: argparse.Namespace) -> None:
    """Wipes all metrics, baselines, and activities after confirmation."""
    if not args.yes:
        try:
            msg = (
                "Are you sure you want to wipe all metrics, baselines, "
                "and completed activities? [y/N]: "
            )
            confirm = input(msg).strip().lower()
        except EOFError:
            confirm = 'n'
        if confirm not in ('y', 'yes'):
            print("Wipe cancelled.")
            return

    db.wipe_metrics()
    print("All metrics, baselines, and completed activities wiped successfully.")


if __name__ == "__main__":
    main()
