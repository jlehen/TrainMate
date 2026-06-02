import argparse
import sys
from datetime import datetime, timezone
from trainmate.db import db
from trainmate.google_sheets import sheets_reader
from trainmate.google_calendar import calendar_syncer
from trainmate.coach import coach_engine
from trainmate.config import config

def main():
    parser = argparse.ArgumentParser(
        description="TrainMate - Local Training Coach CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # status command
    subparsers.add_parser("status", help="Show current athlete status, active goals, recent metrics, and coach memories")
    
    # sync command
    subparsers.add_parser("sync", help="Commit all local planned workouts to Google Calendar")
    
    # sync-sheets command
    subparsers.add_parser("sync-sheets", help="Fetch latest activities and daily metrics from Google Sheets")
    
    # goal command & subparsers
    goal_parser = subparsers.add_parser("goal", help="Manage training objectives / goals")
    goal_subparsers = goal_parser.add_subparsers(dest="subcommand", help="Goal sub-commands")
    
    # goal add
    g_add = goal_subparsers.add_parser("add", help="Add a new training objective/goal")
    g_add.add_argument("--title", required=True, help="Goal title (e.g. Marathon)")
    g_add.add_argument("--date", required=True, help="Target event date (YYYY-MM-DD)")
    g_add.add_argument("--sport", required=True, choices=["running", "road_biking", "hiking", "strength_training", "yoga", "ski_touring"], help="Sport type")
    g_add.add_argument("--desc", default="", help="Description")
    g_add.add_argument("--priority", type=int, default=1, help="Goal priority (1 = highest)")
    
    # goal rm
    g_rm = goal_subparsers.add_parser("rm", help="Remove a goal by ID")
    g_rm.add_argument("id", type=int, help="Goal ID to remove")
    
    # goal list
    goal_subparsers.add_parser("list", help="Show all training objectives")
    
    # constraint command & subparsers
    constraint_parser = subparsers.add_parser("constraint", help="Manage constraints (life events)")
    constraint_subparsers = constraint_parser.add_subparsers(dest="subcommand", help="Constraint sub-commands")
    
    # constraint add
    c_add = constraint_subparsers.add_parser("add", help="Add a new constraint (life event)")
    c_add.add_argument("--title", required=True, help="Constraint title (e.g. Vacation to Spain)")
    c_add.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    c_add.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    c_add.add_argument("--type", required=True, choices=["injury", "vacation", "party", "other"], help="Constraint type")
    c_add.add_argument("--desc", default="", help="Description/Impact description")
    
    # constraint rm
    c_rm = constraint_subparsers.add_parser("rm", help="Remove a constraint by ID")
    c_rm.add_argument("id", type=int, help="Constraint ID to remove")
    
    # constraint list
    constraint_subparsers.add_parser("list", help="Show all logged constraints")
    
    # workout command & subparsers
    workout_parser = subparsers.add_parser("workout", help="Manage workouts")
    workout_subparsers = workout_parser.add_subparsers(dest="subcommand", help="Workout sub-commands")
    
    # workout list
    workout_subparsers.add_parser("list", help="Show all planned workouts")
    
    # workout plan
    w_plan = workout_subparsers.add_parser("plan", help="Generate or adapt the 4-week periodized training plan (saves locally)")
    w_plan.add_argument("-f", "--force", action="store_true", help="Force regeneration of the macrocycle/mesocycle strategy")
    
    # workout rm
    w_rm = workout_subparsers.add_parser("rm", help="Remove a workout by ID")
    w_rm.add_argument("id", type=int, help="Workout ID to remove")
    
    # workout adapt
    w_adapt = workout_subparsers.add_parser("adapt", help="Run the daily Garmin check for today (syncs adapted workouts to Calendar)")
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
        elif sub == "plan":
            run_workout_plan(args)
        elif sub == "adapt":
            run_workout_adapt(args)
    else:
        print(f"Unknown command: '{cmd}'")
        parser.print_help()
        sys.exit(1)

def run_status():
    print("=== TRAINMATE ATHLETE STATUS ===")
    
    # Active Goal
    objectives = db.get_objectives(status='active')
    if objectives:
        objectives.sort(key=lambda x: x['target_date'])
        next_goal = objectives[0]
        print(f"\nNext Objective: {next_goal['title']} ({next_goal['sport_type'].upper()})")
        print(f"Target Date   : {next_goal['target_date']}")
        print(f"Description   : {next_goal.get('description', '')}")
    else:
        print("\nNext Objective: None (TrainMate needs at least one goal to start planning)")

    # Recent Garmin metrics
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    metrics = db.get_metrics_cache()
    if metrics:
        last_metrics = metrics[-1]
        print(f"\nRecent Garmin Metrics ({last_metrics['date']}):")
        print(f"- Resting HR : {last_metrics['rhr']} bpm")
        print(f"- Overnight HRV: {last_metrics['hrv']} ms")
        print(f"- Sleep Score: {last_metrics['sleep_score']}")
        print(f"- Stress     : {last_metrics['stress']}")
        print(f"- ACWR       : {last_metrics['acwr']:.2f} (Acute: {last_metrics['acute_workload']:.1f}, Chronic: {last_metrics['chronic_workload']:.1f})")
        
        # Baselines
        baseline = db.get_baseline(last_metrics['date'])
        if baseline:
            print(f"Baselines (28-day):")
            print(f"- RHR Mean   : {baseline['rhr_baseline_mean']:.1f} (std: {baseline['rhr_baseline_std']:.2f})")
            print(f"- HRV Mean   : {baseline['hrv_baseline_mean']:.1f} (std: {baseline['hrv_baseline_std']:.2f})")
            print(f"- Sleep Mean : {baseline['sleep_baseline_mean']:.1f} (std: {baseline['sleep_baseline_std']:.2f})")
    else:
        print("\nRecent Garmin Metrics: No cached metrics. Run 'sync-sheets' first.")

    # Coach Memory
    strategy = db.get_coach_memory("training_strategy")
    learnings = db.get_coach_memory("athlete_learnings")
    print(f"\nCoach Memory:")
    print(f"- Strategy  : {strategy or 'Not established'}")
    print(f"- Learnings : {learnings or 'None yet'}")
    print("\n================================")

def run_workout_plan(args):
    # Make sure we have latest metrics cached
    metrics = db.get_metrics_cache()
    if not metrics:
        print("Warning: Metrics cache is empty. Fetching from Google Sheets first...")
        sheets_reader.sync_data()
        
    try:
        reasoning, workouts = coach_engine.replan(force=args.force)
        print("\n=== PLAN GENERATED BY COACH ===")
        print(f"Reasoning:\n{reasoning}\n")
        print(f"Generated {len(workouts)} workouts starting from today. Save complete.")
        print("Run 'sync' to commit this plan to Google Calendar.")
    except Exception as e:
        print(f"Error during plan generation: {e}")

def run_workout_adapt(args):
    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Evaluating daily Garmin metrics adaptation for {date_str}...")
    
    try:
        reason, adapted_workout = coach_engine.adapt(date_str)
        print(f"\nDecision Summary:\n{reason}")
        if adapted_workout:
            print(f"\nAdapted Workout Synced to Calendar: {adapted_workout['title']}")
    except Exception as e:
        print(f"Error executing daily adaptation: {e}")

def run_sync():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    planned_workouts = db.get_workouts(start_date=today_str)
    # Filter to only unsynced/planned ones
    unsynced = [w for w in planned_workouts if w['status'] in ('planned', 'modified')]
    
    if not unsynced:
        print("No new or modified workouts to sync. Run 'workout plan' to generate a schedule.")
        return
        
    print(f"Syncing {len(unsynced)} workouts to Google Calendar...")
    try:
        calendar_syncer.sync_multiple(unsynced)
        print("Google Calendar synchronization completed.")
    except Exception as e:
        print(f"Error syncing to Google Calendar: {e}")

def run_sync_sheets():
    try:
        sheets_reader.sync_data()
    except Exception as e:
        print(f"Error syncing Google Sheets: {e}")

def run_goal_add(args):
    db.add_objective(
        title=args.title,
        target_date=args.date,
        sport_type=args.sport,
        description=args.desc,
        priority=args.priority,
        status='active'
    )
    print(f"Goal '{args.title}' added successfully. Run 'workout plan' to generate training cycles.")

def run_goal_rm(args):
    db.delete_objective(args.id)
    print(f"Goal with ID {args.id} removed successfully.")

def run_constraint_add(args):
    db.add_constraint(
        title=args.title,
        start_date=args.start,
        end_date=args.end,
        event_type=args.type,
        impact_description=args.desc
    )
    print(f"Constraint '{args.title}' logged. This will be factored in when running 'workout plan' or 'workout adapt'.")

def run_constraint_rm(args):
    db.delete_constraint(args.id)
    print(f"Constraint with ID {args.id} removed successfully.")

def run_goal_list():
    goals = db.get_objectives()
    print("=== TRAINING OBJECTIVES / GOALS ===")
    for g in goals:
        print(f"[{g['status'].upper()}] ID: {g['id']} | {g['title']} ({g['sport_type']}) on {g['target_date']} (Priority: {g['priority']})")
        if g.get('description'):
            print(f"  Description: {g['description']}")

def run_constraint_list():
    events = db.get_constraints()
    print("=== ATHLETE CONSTRAINTS ===")
    for e in events:
        print(f"ID: {e['id']} | {e['title']} ({e['event_type']}): {e['start_date']} to {e['end_date']}")
        if e.get('impact_description'):
            print(f"  Impact: {e['impact_description']}")

def run_workout_rm(args):
    workout = db.get_workout_by_id(args.id)
    if not workout:
        print(f"Workout with ID {args.id} not found.")
        return
        
    if workout.get('google_event_id') and workout.get('status') == 'synced':
        print(f"Workout is synced to Google Calendar. Attempting to delete calendar event...")
        calendar_syncer.delete_workout_event(workout['google_event_id'])
        
    db.delete_workout_by_id(args.id)
    print(f"Workout with ID {args.id} ('{workout['title']}') removed successfully.")

def run_workout_list():
    workouts = db.get_workouts()
    print("=== WORKOUT SCHEDULE ===")
    for w in workouts:
        mod_marker = " [ADAPTED]" if w['status'] == 'modified' or w['modification_reason'] else ""
        sync_marker = " [SYNCED]" if w['status'] == 'synced' else ""
        print(f"ID: {w['id']} | {w['date']} | {w['sport_type'].upper()} | {w['title']}{mod_marker}{sync_marker}")
        print(f"  Description: {w['description']}")
        if w.get('modification_reason'):
            print(f"  Reason: {w['modification_reason']}")
        print("-" * 40)

if __name__ == "__main__":
    main()
