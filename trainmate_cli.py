import argparse
import sys
import textwrap
from datetime import datetime, timezone
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
    subparsers.add_parser(
        "status",
        help="Show current athlete status, active goals, recent metrics, and memories"
    )
    
    # sync command
    subparsers.add_parser("sync", help="Commit all local planned workouts to Google Calendar")
    
    # sync-sheets command
    subparsers.add_parser("sync-sheets", help="Fetch latest activities and metrics from Sheets")
    
    # goal command & subparsers
    goal_parser = subparsers.add_parser("goal", help="Manage training objectives / goals")
    goal_subparsers = goal_parser.add_subparsers(dest="subcommand", help="Goal sub-commands")
    
    # goal add
    g_add = goal_subparsers.add_parser("add", help="Add a new training objective/goal")
    g_add.add_argument("--title", required=True, help="Goal title (e.g. Marathon)")
    g_add.add_argument("--date", required=True, help="Target event date (YYYY-MM-DD)")
    g_add.add_argument(
        "--sport", required=True,
        choices=["running", "road_biking", "hiking", "strength_training", "yoga", "ski_touring"],
        help="Sport type"
    )
    g_add.add_argument("--desc", default="", help="Description")
    g_add.add_argument("--priority", type=int, default=1, help="Goal priority (1 = highest)")
    
    # goal rm
    g_rm = goal_subparsers.add_parser("rm", help="Remove a goal by ID")
    g_rm.add_argument("id", type=int, help="Goal ID to remove")
    
    # goal list
    goal_subparsers.add_parser("list", help="Show all training objectives")
    
    # constraint command & subparsers
    constraint_parser = subparsers.add_parser("constraint", help="Manage constraints (life events)")
    constraint_subparsers = constraint_parser.add_subparsers(
        dest="subcommand", help="Constraint sub-commands"
    )
    
    # constraint add
    c_add = constraint_subparsers.add_parser("add", help="Add a new constraint (life event)")
    c_add.add_argument("--title", required=True, help="Constraint title (e.g. Vacation to Spain)")
    c_add.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    c_add.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    c_add.add_argument(
        "--type", required=True, choices=["injury", "vacation", "party", "other"],
        help="Constraint type"
    )
    c_add.add_argument("--desc", default="", help="Description/Impact description")
    
    # constraint rm
    c_rm = constraint_subparsers.add_parser("rm", help="Remove a constraint by ID")
    c_rm.add_argument("id", type=int, help="Constraint ID to remove")
    
    # constraint list
    constraint_subparsers.add_parser("list", help="Show all logged constraints")
    
    # plan command & subparsers
    plan_parser = subparsers.add_parser(
        "plan", help="Manage and consult the periodized training plan"
    )
    plan_subparsers = plan_parser.add_subparsers(
        dest="subcommand", help="Plan sub-commands"
    )
    
    # plan generate
    p_gen = plan_subparsers.add_parser(
        "generate",
        help="Generate or adapt the 4-week periodized training plan (saves locally)"
    )
    p_gen.add_argument(
        "-f", "--force", action="store_true",
        help="Force regeneration of the macrocycle/mesocycle strategy"
    )
    
    # plan show
    plan_subparsers.add_parser(
        "show",
        help="Show the active macrocycle and mesocycles periodization strategy"
    )
    
    # workout command & subparsers
    workout_parser = subparsers.add_parser("workout", help="Manage workouts")
    workout_subparsers = workout_parser.add_subparsers(
        dest="subcommand", help="Workout sub-commands"
    )
    
    # workout list
    workout_subparsers.add_parser("list", help="Show all planned workouts")
    
    # workout rm
    w_rm = workout_subparsers.add_parser("rm", help="Remove a workout by ID")
    w_rm.add_argument("id", type=int, help="Workout ID to remove")
    
    # workout adapt
    w_adapt = workout_subparsers.add_parser(
        "adapt",
        help="Run the daily Garmin check for today (syncs adapted workouts to Calendar)"
    )
    w_adapt.add_argument("--date", help="Date in YYYY-MM-DD format (defaults to UTC today)")
    
    # Parse the arguments
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
        
    cmd = args.command.lower()
    
    if cmd == "status":
        run_status()
    elif cmd == "sync":
        run_sync()
    elif cmd == "sync-sheets":
        run_sync_sheets()
    elif cmd == "goal":
        if not args.subcommand:
            goal_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub == "add":
            run_goal_add(args)
        elif sub == "rm":
            run_goal_rm(args)
        elif sub == "list":
            run_goal_list()
    elif cmd == "constraint":
        if not args.subcommand:
            constraint_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub == "add":
            run_constraint_add(args)
        elif sub == "rm":
            run_constraint_rm(args)
        elif sub == "list":
            run_constraint_list()
    elif cmd == "workout":
        if not args.subcommand:
            workout_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub == "list":
            run_workout_list()
        elif sub == "rm":
            run_workout_rm(args)
        elif sub == "adapt":
            run_workout_adapt(args)
    elif cmd == "plan":
        if not args.subcommand:
            plan_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub == "generate":
            run_plan_generate(args)
        elif sub == "show":
            run_plan_show()
    else:
        print(f"Unknown command: '{cmd}'")
        parser.print_help()
        sys.exit(1)

def run_status() -> None:
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
            print("Active Cycle  : No periodization strategy established. Run 'plan generate' first.")
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
        print("\nRecent Garmin Metrics: No cached metrics. Run 'sync-sheets' first.")

    # Coach Memory
    strategy = db.get_coach_memory("training_strategy")
    learnings = db.get_coach_memory("athlete_learnings")
    print("\nCoach Memory:")
    print(f"- Strategy  : {strategy or 'Not established'}")
    print(f"- Learnings : {learnings or 'None yet'}")
    print("\n================================")

def run_plan_generate(args: argparse.Namespace) -> None:
    """Executes the AI replanning workout scheduler command."""
    # Make sure we have latest metrics cached
    metrics = db.get_metrics_cache()
    if not metrics:
        print("Warning: Metrics cache is empty. Fetching from Google Sheets first...")
        sheets_reader.sync_data()
        
    try:
        reasoning, workouts = coach_engine.replan(force=bool(args.force))
        print("\n=== PLAN GENERATED BY COACH ===")
        print(f"Reasoning:\n{reasoning}\n")
        print(f"Generated {len(workouts)} workouts starting from today. Save complete.")
        print("Run 'sync' to commit this plan to Google Calendar.")
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

def run_workout_adapt(args: argparse.Namespace) -> None:
    """Executes the daily workout Garmin adaptation checks command."""
    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Evaluating daily Garmin metrics adaptation for {date_str}...")
    
    try:
        reason, adapted_workout = coach_engine.adapt(date_str)
        print(f"\nDecision Summary:\n{reason}")
        if adapted_workout:
            print(f"\nAdapted Workout Synced to Calendar: {adapted_workout['title']}")
    except Exception as e:
        print(f"Error executing daily adaptation: {e}")

def run_sync() -> None:
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

def run_sync_sheets() -> None:
    """Pulls athlete metrics and activities from Google Sheets."""
    try:
        sheets_reader.sync_data()
    except Exception as e:
        print(f"Error syncing Google Sheets: {e}")

def run_goal_add(args: argparse.Namespace) -> None:
    """Creates a new objective goal via command line."""
    db.add_objective(
        title=args.title,
        target_date=args.date,
        sport_type=args.sport,
        description=args.desc,
        priority=args.priority,
        status='active'
    )
    print(
        f"Goal '{args.title}' added successfully. "
        f"Run 'plan generate' to generate training cycles."
    )

def run_goal_rm(args: argparse.Namespace) -> None:
    """Deletes an objective goal by ID."""
    db.delete_objective(args.id)
    print(f"Goal with ID {args.id} removed successfully.")

def run_constraint_add(args: argparse.Namespace) -> None:
    """Creates a new constraint via command line."""
    db.add_constraint(
        title=args.title,
        start_date=args.start,
        end_date=args.end,
        event_type=args.type,
        impact_description=args.desc
    )
    print(
        f"Constraint '{args.title}' logged. "
        f"This will be factored in when running 'plan generate' or 'workout adapt'."
    )

def run_constraint_rm(args: argparse.Namespace) -> None:
    """Deletes a constraint by ID."""
    db.delete_constraint(args.id)
    print(f"Constraint with ID {args.id} removed successfully.")

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

def run_constraint_list() -> None:
    """Lists all logged training constraints."""
    events = db.get_constraints()
    print("=== ATHLETE CONSTRAINTS ===")
    for e in events:
        print(
            f"ID: {e['id']} | {e['title']} ({e['event_type']}): "
            f"{e['start_date']} to {e['end_date']}"
        )
        if e.get('impact_description'):
            print(f"  Impact: {e['impact_description']}")

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

if __name__ == "__main__":
    main()
