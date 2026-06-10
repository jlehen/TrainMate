import argparse
import csv as csv_mod
import os
import subprocess
import sys
import tempfile
import textwrap
from typing import Optional
from datetime import datetime, timedelta
from trainmate.db import db
from trainmate import garmin
from trainmate.google_calendar import calendar_syncer
from trainmate.coach import coach_service
from trainmate.adherence import analyze_adherence
from trainmate.config import config
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    color_acwr, visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, today_str as _today_str, today_date as _today_date,
)



def fmt_date(date_str: str) -> str:
    """Return 'YYYY-MM-DD Ddd' (e.g. '2026-06-05 Fri')."""
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d %a")


def main() -> None:
    """Entry point for the TrainMate Command Line Interface."""
    parser = argparse.ArgumentParser(
        description="TrainMate - Local Training Coach CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Common parser for commands that support bypassing the Garmin pull
    pull_bypass_parser = argparse.ArgumentParser(add_help=False)
    pull_bypass_parser.add_argument(
        "--no-pull", action="store_true", dest="no_pull",
        help="Skip pull check from Garmin, reading purely from SQLite cache"
    )

    # status command
    status_parser = subparsers.add_parser(
        "status",
        aliases=["s"],
        parents=[pull_bypass_parser],
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
        parents=[pull_bypass_parser],
        help=(
            "Generate or adapt the periodized training plan strategy "
            "(macrocycles & mesocycles)"
        )
    )
    p_gen.add_argument(
        "-f", "--force", action="store_true",
        help="Force regeneration of the macrocycle/mesocycle strategy"
    )
    p_gen.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID to generate the periodization plan for"
    )
    
    # plan show
    p_show = plan_subparsers.add_parser(
        "show", aliases=["s"],
        help="Show the active macrocycle and mesocycles periodization strategy"
    )
    p_show.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID to show the periodization plan for"
    )

    # plan rm
    p_rm = plan_subparsers.add_parser(
        "rm", aliases=["d"],
        help="Remove/delete a specific periodization plan by Goal ID"
    )
    p_rm.add_argument(
        "id", type=int,
        help="Goal ID whose periodization plan should be removed"
    )

    # plan feedback
    p_fb = plan_subparsers.add_parser(
        "feedback",
        aliases=["f"],
        description="Add athlete feedback (either --macro or --meso is mandatory).",
        help="Add athlete feedback (either --macro or --meso is mandatory)"
    )
    p_fb.add_argument(
        "--macro", action="store_true",
        help="Provide general feedback on the overall macrocycle strategy"
    )
    p_fb.add_argument(
        "--meso", type=int,
        help="Provide feedback on a specific mesocycle ID"
    )
    p_fb.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help=(
            "Target goal ID whose plan the feedback should attach to "
            "(default to the current active goal)"
        )
    )
    p_fb.add_argument(
        "--edit", action="store_true",
        help="Open $EDITOR seeded with the current feedback (takes no text argument)"
    )
    p_fb.add_argument(
        "text", nargs="?", default=None,
        help="Feedback content string (omit when using --edit)"
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
    w_list = workout_subparsers.add_parser(
        "list", aliases=["l"], help="Show all planned workouts"
    )
    w_list.add_argument(
        "--type", "--sport-type", dest="sport_type",
        help="Filter workouts by sport type"
    )
    w_list.add_argument(
        "--days", type=int, dest="days", metavar="N",
        help="Show workouts for N days"
    )
    w_list.add_argument(
        "--weeks", type=float, dest="weeks", metavar="N",
        help="Show workouts for N weeks"
    )
    w_list.add_argument(
        "--from", "--from-date", dest="from_date",
        help="Show workouts starting from DATE (YYYY-MM-DD)"
    )
    w_list.add_argument(
        "--until", "--until-date", dest="until_date",
        help="Show workouts until DATE (YYYY-MM-DD)"
    )
    w_list.add_argument(
        "--from-mesocycle", action="store_true", dest="from_meso",
        help="Show workouts starting from the start of the current mesocycle"
    )
    w_list.add_argument(
        "--until-mesocycle", type=int, nargs="?", const=-1, dest="until_meso_id", metavar="ID",
        help="Show workouts until the end of a mesocycle (uses current if ID omitted)"
    )
    w_list.add_argument(
        "--mesocycle", type=int, nargs="?", const=-1, dest="meso_id", metavar="ID",
        help="Show workouts within a mesocycle (uses current if ID omitted)"
    )
    w_list.add_argument(
        "--goal", "--goal-id", type=int, nargs="?", const=-1, dest="goal_id", metavar="ID",
        help="Show workouts for a goal's plan duration (uses active goal if ID omitted)"
    )
    
    # workout compare
    w_cmp = workout_subparsers.add_parser(
        "compare", aliases=["c"], parents=[pull_bypass_parser],
        help="Compare planned workouts against completed activities"
    )
    w_cmp.add_argument(
        "--type", "--sport-type", dest="sport_type",
        help="Filter display by sport type"
    )
    w_cmp.add_argument(
        "--days", type=int, dest="days", metavar="N",
        help="Compare workouts for N days from today"
    )
    w_cmp.add_argument(
        "--weeks", type=float, dest="weeks", metavar="N",
        help="Compare workouts for N weeks from today"
    )
    w_cmp.add_argument(
        "--from", "--from-date", dest="from_date",
        help="Compare workouts starting from DATE (YYYY-MM-DD)"
    )
    w_cmp.add_argument(
        "--until", "--until-date", dest="until_date",
        help="Compare workouts until DATE (YYYY-MM-DD)"
    )
    w_cmp.add_argument(
        "--from-mesocycle", action="store_true", dest="from_meso",
        help="Compare workouts starting from the start of the current mesocycle"
    )
    w_cmp.add_argument(
        "--until-mesocycle", type=int, nargs="?", const=-1, dest="until_meso_id", metavar="ID",
        help="Compare workouts until the end of a mesocycle (uses current if ID omitted)"
    )
    w_cmp.add_argument(
        "--mesocycle", type=int, nargs="?", const=-1, dest="meso_id", metavar="ID",
        help="Compare workouts within a mesocycle (uses current if ID omitted)"
    )
    w_cmp.add_argument(
        "--goal", "--goal-id", type=int, nargs="?", const=-1, dest="goal_id", metavar="ID",
        help="Compare workouts for a goal's plan duration (uses active goal if ID omitted)"
    )

    # workout generate
    p_w_gen = workout_subparsers.add_parser(
        "generate", aliases=["g"],
        parents=[pull_bypass_parser],
        help="Generate workouts (microcycles) based on the active strategy"
    )
    p_w_gen.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID to generate workouts for"
    )
    p_w_gen_horizon = p_w_gen.add_mutually_exclusive_group()
    p_w_gen_horizon.add_argument(
        "--days", type=int, dest="horizon_days", metavar="N",
        help="Generate workouts for N days from today"
    )
    p_w_gen_horizon.add_argument(
        "--weeks", type=float, dest="horizon_weeks", metavar="N",
        help="Generate workouts for N weeks from today"
    )
    p_w_gen_horizon.add_argument(
        "--until", dest="horizon_until", metavar="DATE",
        help="Generate workouts until DATE (YYYY-MM-DD)"
    )
    p_w_gen_horizon.add_argument(
        "--until-goal", type=int, nargs="?", const=-1, dest="horizon_goal_id", metavar="ID",
        help="Generate workouts until the target date of a goal (uses current goal if ID omitted)"
    )
    p_w_gen_horizon.add_argument(
        "--until-mesocycle", type=int, dest="horizon_meso_id", metavar="ID",
        help="Generate workouts until the end date of a mesocycle"
    )
    
    # workout rm
    w_rm = workout_subparsers.add_parser(
        "rm", aliases=["r"], help="Remove a workout by ID"
    )
    w_rm.add_argument("id", type=int, help="Workout ID to remove")
    
    # workout adapt
    w_adapt = workout_subparsers.add_parser(
        "adapt", aliases=["a"],
        parents=[pull_bypass_parser],
        help="Run the daily Garmin check for today (syncs adapted workouts to Calendar)"
    )
    w_adapt.add_argument("--date", help="Date in YYYY-MM-DD format (defaults to UTC today)")
    w_adapt.add_argument(
        "-y", "--yes", "--auto", action="store_true", dest="auto",
        help="Apply proposed adaptations automatically without prompting"
    )
    
    # workout push
    w_push = workout_subparsers.add_parser(
        "push", aliases=["p"],
        help="Commit local planned workouts to Google Calendar"
    )
    w_push.add_argument(
        "-f", "--force", action="store_true",
        help="Re-push already-synced workouts, overwriting existing calendar entries"
    )
    w_push.add_argument(
        "--type", "--sport-type", dest="sport_type",
        help="Filter workouts by sport type"
    )
    w_push.add_argument(
        "--days", type=int, dest="days", metavar="N",
        help="Push workouts for N days from today"
    )
    w_push.add_argument(
        "--weeks", type=float, dest="weeks", metavar="N",
        help="Push workouts for N weeks from today"
    )
    w_push.add_argument(
        "--from", "--from-date", dest="from_date",
        help="Push workouts starting from DATE (YYYY-MM-DD)"
    )
    w_push.add_argument(
        "--until", "--until-date", dest="until_date",
        help="Push workouts until DATE (YYYY-MM-DD)"
    )
    w_push.add_argument(
        "--from-mesocycle", action="store_true", dest="from_meso",
        help="Push workouts starting from the start of the current mesocycle"
    )
    w_push.add_argument(
        "--until-mesocycle", type=int, nargs="?", const=-1, dest="until_meso_id", metavar="ID",
        help="Push workouts until the end of a mesocycle (uses current if ID omitted)"
    )
    w_push.add_argument(
        "--mesocycle", type=int, nargs="?", const=-1, dest="meso_id", metavar="ID",
        help="Push workouts within a mesocycle (uses current if ID omitted)"
    )
    w_push.add_argument(
        "--goal", "--goal-id", type=int, nargs="?", const=-1, dest="goal_id", metavar="ID",
        help="Push workouts for a goal's plan duration (uses active goal if ID omitted)"
    )

    # workout swap
    w_swap = workout_subparsers.add_parser(
        "swap", aliases=["s"],
        help="Swap workouts between two dates (or two IDs), with recovery checks"
    )
    w_swap.add_argument(
        "date1", nargs="?", help="First date to swap (YYYY-MM-DD)"
    )
    w_swap.add_argument(
        "date2", nargs="?", help="Second date to swap (YYYY-MM-DD)"
    )
    w_swap.add_argument(
        "--id1", type=int, help="First workout ID (use together with --id2)"
    )
    w_swap.add_argument(
        "--id2", type=int, help="Second workout ID (use together with --id1)"
    )
    w_swap.add_argument(
        "--no-sync", action="store_true", dest="no_sync",
        help="Do not sync the swapped workouts to Google Calendar"
    )
    w_swap.add_argument(
        "-f", "--force", action="store_true", dest="force",
        help="Apply the swap without prompting, even if warnings are raised"
    )



    # workout wipe
    w_wipe = workout_subparsers.add_parser(
        "wipe",
        help="Wipe all workouts from the database and Google Calendar"
    )
    w_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # data command & subparsers
    data_parser = subparsers.add_parser(
        "data",
        aliases=["d"],
        help="Manage and sync athlete metrics and activities"
    )
    data_subparsers = data_parser.add_subparsers(
        dest="subcommand", help="Data sub-commands"
    )
    
    # data pull
    d_pull = data_subparsers.add_parser(
        "pull",
        help="Fetch latest activities and metrics directly from Garmin Connect"
    )
    d_pull.add_argument(
        "--days", type=int, default=2, metavar="N",
        help="Number of days to pull, ending today (default: 2)"
    )
    d_pull.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Start date for an explicit range (overrides --days)"
    )
    d_pull.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="End date for an explicit range (defaults to today)"
    )
    d_pull.add_argument(
        "--sleep", type=float, dest="sleep", metavar="SECONDS",
        help="Throttle: seconds to sleep between Garmin calls (default from config)"
    )
    pull_group = d_pull.add_mutually_exclusive_group()
    pull_group.add_argument(
        "--metrics-only", action="store_true", help="Pull daily metrics only"
    )
    pull_group.add_argument(
        "--activities-only", action="store_true", help="Pull activities only"
    )

    # data analyze
    d_an = data_subparsers.add_parser(
        "analyze", aliases=["a"],
        parents=[pull_bypass_parser],
        help="Analyze recorded workouts and metrics to determine macrocycle/mesocycles"
    )
    d_an.add_argument(
        "--from", "--from-date", dest="from_date",
        help="Start date of the historical period to analyze (YYYY-MM-DD)"
    )
    d_an.add_argument(
        "--until", "--until-date", dest="until_date",
        help="End date of the historical period to analyze (YYYY-MM-DD, defaults to today)"
    )
    d_an.add_argument(
        "--days", type=int, dest="days", metavar="N",
        help="Number of days to analyze looking back from --until"
    )
    d_an.add_argument(
        "--weeks", type=float, dest="weeks", metavar="N",
        help="Number of weeks to analyze looking back from --until"
    )
    d_an.add_argument(
        "--context", dest="context",
        help="Optional text context detailing subjective athlete notes (travel, illness, etc.)"
    )
    d_an.add_argument(
        "-f", "--force", action="store_true",
        help="Recompute even if the evidence is unchanged (bypass the analysis cache)"
    )
    d_an.add_argument(
        "--inspect", action="store_true",
        help="Read-only: show the analysis without writing coach learnings or the cache"
    )

    # data backfill-tss
    d_btss = data_subparsers.add_parser(
        "backfill-tss",
        help="Recompute TSS for all stored activities using the current "
             "zone-based model (no Garmin calls needed)"
    )
    d_btss.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Only process activities on or after this date"
    )
    d_btss.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="Only process activities on or before this date"
    )
    d_btss.add_argument(
        "-v", "--verbose", action="store_true",
        help="List activities with low HR-zone coverage that need an RPE"
    )

    # data show-metrics
    d_sm = data_subparsers.add_parser(
        "show-metrics", aliases=["sm"],
        parents=[pull_bypass_parser],
        help="Show athlete metrics over a date range"
    )
    d_sm.add_argument(
        "--days", type=int, dest="days", metavar="N",
        help="Show metrics for N days"
    )
    d_sm.add_argument(
        "--weeks", type=float, dest="weeks", metavar="N",
        help="Show metrics for N weeks"
    )
    d_sm.add_argument(
        "--from", "--from-date", dest="from_date",
        help="Show metrics starting from DATE (YYYY-MM-DD)"
    )
    d_sm.add_argument(
        "--until", "--until-date", dest="until_date",
        help="Show metrics until DATE (YYYY-MM-DD)"
    )
    d_sm.add_argument(
        "--from-mesocycle", action="store_true", dest="from_meso",
        help="Show metrics starting from the start of the current mesocycle"
    )
    d_sm.add_argument(
        "--until-mesocycle", type=int, nargs="?", const=-1, dest="until_meso_id",
        metavar="ID", help="Show metrics until the end of a mesocycle"
    )
    d_sm.add_argument(
        "--mesocycle", type=int, nargs="?", const=-1, dest="meso_id",
        metavar="ID", help="Show metrics within a mesocycle"
    )
    d_sm.add_argument(
        "--goal", "--goal-id", type=int, nargs="?", const=-1, dest="goal_id",
        metavar="ID", help="Show metrics for a goal's plan duration"
    )

    d_sm.add_argument(
        "--csv", action="store_true", dest="csv",
        help="Output data as CSV for script consumption"
    )
    d_sm.add_argument(
        "-a", "--all", action="store_true", dest="all",
        help="Show all cached athlete metrics"
    )

    # data show-activities
    d_sa = data_subparsers.add_parser(
        "show-activities", aliases=["sa"],
        parents=[pull_bypass_parser],
        help="Show completed activities over a date range"
    )
    d_sa.add_argument(
        "--days", type=int, dest="days", metavar="N",
        help="Show activities for N days"
    )
    d_sa.add_argument(
        "--weeks", type=float, dest="weeks", metavar="N",
        help="Show activities for N weeks"
    )
    d_sa.add_argument(
        "--from", "--from-date", dest="from_date",
        help="Show activities starting from DATE (YYYY-MM-DD)"
    )
    d_sa.add_argument(
        "--until", "--until-date", dest="until_date",
        help="Show activities until DATE (YYYY-MM-DD)"
    )
    d_sa.add_argument(
        "--from-mesocycle", action="store_true", dest="from_meso",
        help="Show activities starting from the start of the current mesocycle"
    )
    d_sa.add_argument(
        "--until-mesocycle", type=int, nargs="?", const=-1, dest="until_meso_id",
        metavar="ID", help="Show activities until the end of a mesocycle"
    )
    d_sa.add_argument(
        "--mesocycle", type=int, nargs="?", const=-1, dest="meso_id",
        metavar="ID", help="Show activities within a mesocycle"
    )
    d_sa.add_argument(
        "--goal", "--goal-id", type=int, nargs="?", const=-1, dest="goal_id",
        metavar="ID", help="Show activities for a goal's plan duration"
    )
    d_sa.add_argument(
        "--type", "--sport-type", dest="sport_type",
        help="Filter activities by sport type"
    )

    d_sa.add_argument(
         "--csv", action="store_true", dest="csv",
         help="Output data as CSV for script consumption"
     )
    d_sa.add_argument(
        "-a", "--all", action="store_true", dest="all",
        help="Show all cached completed activities"
    )

    # data wipe
    d_wipe = data_subparsers.add_parser(
        "wipe", help="Wipe all metrics and completed activities from the database"
    )
    d_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # Parse the arguments
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
        
    cmd = args.command.lower()

    if cmd in ("status", "s"):
        run_status(verbose=args.verbose, no_pull=args.no_pull)
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
            run_workout_list(args)
        elif sub in ("compare", "c"):
            run_workout_compare(args)
        elif sub in ("generate", "g"):
            run_workout_generate(args)
        elif sub in ("rm", "r"):
            run_workout_rm(args)
        elif sub in ("adapt", "a"):
            run_workout_adapt(args)
        elif sub in ("push", "p"):
            run_workout_push(args)
        elif sub in ("swap", "s"):
            run_workout_swap(args)
        elif sub == "wipe":
            run_workout_wipe(args)
    elif cmd in ("data", "d"):
        if not args.subcommand:
            data_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub == "pull":
            run_data_pull(args)
        elif sub in ("analyze", "a"):
            run_data_analyze(args)
        elif sub == "backfill-tss":
            run_data_backfill_tss(args)
        elif sub in ("show-metrics", "sm"):
            run_data_show_metrics(args)
        elif sub in ("show-activities", "sa"):
            run_data_show_activities(args)
        elif sub == "wipe":
            run_data_wipe(args)
    elif cmd in ("plan", "p"):
        if not args.subcommand:
            plan_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("generate", "g"):
            run_plan_generate(args)
        elif sub in ("show", "s"):
            run_plan_show(args)
        elif sub in ("rm", "d"):
            run_plan_rm(args)
        elif sub in ("feedback", "f"):
            run_plan_feedback(args)
        elif sub == "wipe":
            run_plan_wipe(args)
    else:
        print(f"Unknown command: '{cmd}'")
        parser.print_help()
        sys.exit(1)


# ==============================================================================
# Status Command
# ==============================================================================

def run_status(verbose: bool = False, no_pull: bool = False) -> None:
    """Displays current athlete goals, Garmin metrics, baselines, and memories."""
    _ensure_recent_data(no_pull=no_pull)
    print(bold(cyan("=== TRAINMATE ATHLETE STATUS ===")))
    
    # Active Goal & Periodization Strategy
    objectives = db.get_objectives(status='active')
    if objectives:
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]
        sport_str = next_goal['sport_type'].upper()
        
        # Calculate days remaining
        today = _today_date()
        target_date = datetime.strptime(next_goal['target_date'], "%Y-%m-%d").date()
        days_rem = (target_date - today).days
        days_rem_str = f" ({days_rem} days remaining)" if days_rem >= 0 else ""
        
        print(
            f"\n{bold('Next Objective')}: {cyan(next_goal['title'])} "
            f"({magenta(sport_str)})"
        )
        print(f"{bold('Target Date')}: {cyan(next_goal['target_date'])}{gray(days_rem_str)}")
        
        print(format_labeled_block(f"{bold('Description')}:", next_goal.get('description', '')))
        
        # Query active mesocycle
        macro = db.get_macrocycle_for_objective(next_goal['id'])
        if macro:
            current_hash = coach_service._get_config_hash()
            if macro.get('config_hash') != current_hash:
                print(yellow(
                    "\nWarning: config.yaml has changed since the active periodization plan "
                    "was generated.\nRun 'plan generate' to regenerate."
                ))
            
            mesos = db.get_mesocycles_for_macrocycle(macro['id'])
            active_meso = None
            for m in mesos:
                start = datetime.strptime(m['start_date'], "%Y-%m-%d").date()
                end = datetime.strptime(m['end_date'], "%Y-%m-%d").date()
                if start <= today <= end:
                    active_meso = m
                    break
            
            if active_meso:
                print(
                    f"{bold('Active Mesocycle')}: {green(active_meso['name'])} "
                    f"({cyan(active_meso['start_date'])} to {cyan(active_meso['end_date'])})"
                )
                print(format_labeled_block(f"{bold('Cycle Focus')}:", active_meso['focus']))
            else:
                print(
                    f"{bold('Active Mesocycle')}: "
                    f"None active today (outside mesocycle boundaries)"
                )
        else:
            print(
                f"{bold('Active Mesocycle')}: "
                f"No periodization strategy established. Run 'plan generate' first."
            )
    else:
        print(
            f"\n{bold('Next Objective')}: None (TrainMate needs at least one goal to start planning)"
        )

    # Recent Garmin metrics
    metrics = db.get_metrics_cache()
    if metrics:
        last_metrics = metrics[-1]
        print(f"\nRecent Garmin Metrics ({cyan(last_metrics['date'])}):")
        
        baseline = db.get_baseline(last_metrics['date'])
        rhr_val = last_metrics['rhr']
        hrv_val = last_metrics['hrv']
        sleep_val = last_metrics['sleep_score']
        stress_val = last_metrics['stress']
        
        rhr_display = f"{rhr_val} bpm"
        hrv_display = f"{hrv_val} ms"
        sleep_display = str(sleep_val)
        stress_display = str(stress_val)
        
        if baseline:
            if rhr_val is not None and baseline['rhr_baseline_mean'] is not None:
                sd = baseline['rhr_baseline_std'] or 1.0
                if rhr_val > (baseline['rhr_baseline_mean'] + max(3.0, sd)):
                    rhr_display = red(f"{rhr_val} bpm (^)")
                else:
                    rhr_display = green(f"{rhr_val} bpm")
            if hrv_val is not None and baseline['hrv_baseline_mean'] is not None:
                sd = baseline['hrv_baseline_std'] or 1.0
                if hrv_val < (baseline['hrv_baseline_mean'] - sd):
                    hrv_display = red(f"{hrv_val} ms (v)")
                else:
                    hrv_display = green(f"{hrv_val} ms")
            if sleep_val is not None:
                if sleep_val < 60:
                    sleep_display = red(f"{sleep_val} (v)")
                else:
                    sleep_display = green(str(sleep_val))
                    
        print(f"- Resting HR : {rhr_display}")
        print(f"- Overnight HRV: {hrv_display}")
        print(f"- Sleep Score: {sleep_display}")
        print(f"- Stress     : {stress_display}")
        
        acute = last_metrics['acute_workload'] or 0.0
        chronic = last_metrics['chronic_workload'] or 0.0
        acwr = last_metrics['acwr'] or 0.0
        print(
            f"- ACWR       : {color_acwr(acwr)} "
            f"(Acute: {acute:.1f}, Chronic: {chronic:.1f})"
        )
        
        # Baselines
        if baseline:
            print(bold("Baselines (28-day):"))
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
        print(yellow("\nRecent Garmin Metrics: No cached metrics. Run 'data pull' first."))

    # Coach Learnings
    learnings = db.get_learnings()
    print(bold("\nCoach Learnings:"))
    if learnings:
        print("- Learnings:")
        for l in learnings:
            tag = f"  [{l['id']}|{l.get('sports') or 'general'}|{l.get('confidence') or 'tentative'}]"
            if l.get("dormant"):
                # Decayed: kept on record but no longer fed to the coach until reaffirmed.
                print(format_labeled_block(gray(tag), gray(f"{l['text']} (dormant)")))
            else:
                print(format_labeled_block(tag, l['text']))
    else:
        print(format_labeled_block("- Learnings:", "None yet"))

    if verbose:
        goals = db.get_objectives()
        print(bold(cyan("\nGoals:")))
        if not goals:
            print("- None")
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
                f"- {status_disp} ID: {g['id']} | {title_disp} "
                f"({sport_str}) on {cyan(g['target_date'])} (Priority: {g['priority']})"
            )
            if g.get('description'):
                print(format_labeled_block("  Description:", g['description']))

        events = db.get_lifeevents()
        print(bold(cyan("\nLife Events:")))
        if not events:
            print("- None")
        for e in events:
            print(
                f"- ID: {e['id']} | {yellow(e['title'])} ({magenta(e['event_type'])}): "
                f"{cyan(e['start_date'])} to {cyan(e['end_date'])}"
            )
            if e.get('impact_description'):
                print(format_labeled_block("  Impact:", e['impact_description']))

    print(bold(cyan("\n================================")))


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
    print(green(
        f"Goal '{args.title}' added successfully. "
        f"Run 'plan generate' to generate training cycles."
    ))


def run_goal_edit(args: argparse.Namespace) -> None:
    """Edits an existing goal/objective."""
    goal = db.get_objective(args.id)
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

    db.update_objective(args.id, **kwargs)
    print(green(
        f"Goal with ID {args.id} updated successfully. "
        "Run 'plan generate' to regenerate training cycles if needed."
    ))


def run_goal_list() -> None:
    """Lists all active and past training objective goals."""
    goals = db.get_objectives()
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
    db.delete_objective(args.id)
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

    db.wipe_objectives()
    print(green("All training objectives wiped successfully."))


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
    print(green(
        f"Life event '{args.title}' logged. "
        f"This will be factored in when running 'plan generate' or 'workout adapt'."
    ))


def run_lifeevent_edit(args: argparse.Namespace) -> None:
    """Edits an existing life event."""
    event = db.get_lifeevent(args.id)
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

    db.update_lifeevent(args.id, **kwargs)
    print(green(
        f"Life event with ID {args.id} updated successfully. "
        "Run 'plan generate' or 'workout adapt' to factor in the changes."
    ))


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
    events = db.get_lifeevents()
    print(bold(cyan("=== ATHLETE LIFE EVENTS ===")))
    for e in events:
        _lifeevent_print(e, show_impact=args.verbose)


def run_lifeevent_show(args: argparse.Namespace) -> None:
    """Displays a specific life event and its impact description by ID."""
    event = db.get_lifeevent(args.id)
    if not event:
        print(red(f"Life event with ID {args.id} not found."))
        return

    _lifeevent_print(event, show_impact=True)


def run_lifeevent_rm(args: argparse.Namespace) -> None:
    """Deletes a life event by ID."""
    db.delete_lifeevent(args.id)
    print(green(f"Life event with ID {args.id} removed successfully."))


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
    print(green("All life events wiped successfully."))


# ==============================================================================
# Plan Command
# ==============================================================================

def run_plan_generate(args: argparse.Namespace) -> None:
    """Executes the AI periodization strategy plan generation command."""
    # Make sure we have latest metrics cached
    _ensure_recent_data(no_pull=args.no_pull)
    metrics = db.get_metrics_cache()
    if not metrics:
        print(yellow("Warning: Metrics cache is empty. Proceeding without Garmin metrics."))
        
    try:
        objectives = db.get_objectives(status='active')
        if objectives:
            if args.goal_id is not None:
                target_goals = [o for o in objectives if o['id'] == args.goal_id]
                next_goal = target_goals[0] if target_goals else None
            else:
                objectives.sort(key=lambda x: str(x['target_date']))
                next_goal = objectives[0]

            if next_goal:
                macro = db.get_macrocycle_for_objective(next_goal['id'])
                if macro:
                    current_hash = coach_service._get_config_hash()
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

        plan_kwargs = {}
        if args.goal_id is not None:
            plan_kwargs['objective_id'] = args.goal_id
        strategy, mesocycles = coach_service.generate_periodization_plan(
            force=bool(args.force), **plan_kwargs
        )
        print(bold(cyan("\n=== PERIODIZATION PLAN GENERATED BY COACH ===")))
        print(f"{bold('Strategy')}:\n{wrap_text(strategy, width=80)}\n")
        print(green(f"Generated {len(mesocycles)} mesocycles. Save complete."))
        print("Run 'workout generate' to schedule workouts based on this plan.")
    except Exception as e:
        print(red(f"Error during plan generation: {e}"))


def run_plan_show(args: argparse.Namespace) -> None:
    """Displays the active training macrocycle and mesocycles periodization timeline."""
    objectives = db.get_objectives(status='active')
    if not objectives:
        print(yellow("No active goals found. TrainMate needs at least one objective."))
        return
        
    if args.goal_id is not None:
        target_goals = [o for o in objectives if o['id'] == args.goal_id]
        if not target_goals:
            # Check if goal exists but is archived/completed
            goal = db.get_objective(args.goal_id)
            if not goal:
                print(red(f"Goal with ID {args.goal_id} not found."))
                return
            next_goal = goal
        else:
            next_goal = target_goals[0]
    else:
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]
    
    macrocycle = db.get_macrocycle_for_objective(next_goal['id'])
    if not macrocycle:
        print(yellow(
            f"No active macrocycle strategy found for goal '{next_goal['title']}'."
        ))
        print("Run 'plan generate' to create one.")
        return
        
    mesocycles = db.get_mesocycles_for_macrocycle(macrocycle['id'])
    
    print(bold(cyan("\n=== ACTIVE MACROCYCLE STRATEGY ===")))
    sport_str = next_goal['sport_type'].upper()
    print(
        f"{bold('Objective')} [ID: {next_goal['id']}]: "
        f"{cyan(next_goal['title'])} ({magenta(sport_str)}) "
        f"on {cyan(fmt_date(next_goal['target_date']))}"
    )
    print(format_labeled_block(f"{bold('Macrocycle Strategy')}:", macrocycle['strategy']))
    if macrocycle.get('feedback'):
        print(format_labeled_block(f"{bold('Macrocycle Feedback')}:", macrocycle['feedback']))
    print()
    print(bold("Mesocycle Timeline:"))
    
    today = _today_date()
    
    for m in mesocycles:
        start = datetime.strptime(m['start_date'], "%Y-%m-%d").date()
        end = datetime.strptime(m['end_date'], "%Y-%m-%d").date()
        
        total_days = (end - start).days + 1
        if total_days <= 0:
            total_days = 1
            
        bar_length = 20
        if end < today:
            status_str = gray("[DONE]  ")
            bar = gray("=" * bar_length)
            extra = ""
        elif start <= today <= end:
            status_str = green("[ACTIVE]")
            days_passed = (today - start).days + 1
            days_passed = max(1, min(days_passed, total_days))
            filled = round(bar_length * days_passed / total_days)
            filled = max(0, min(filled, bar_length))
            bar = green("=" * filled) + gray("." * (bar_length - filled))
            extra = green(f" (Day {days_passed}/{total_days})")
        else:
            status_str = blue("[FUTURE]")
            bar = gray("." * bar_length)
            extra = ""
            
        if total_days >= 7:
            weeks = total_days / 7
            if weeks.is_integer():
                duration_desc = f"({int(weeks)} weeks)"
            else:
                duration_desc = f"({weeks:.1f} weeks)"
        else:
            duration_desc = f"({total_days} days)"
            
        prefix = "|->" if start <= today <= end else "|--"
        if start <= today <= end:
            prefix = green(prefix)
            m_name_disp = green(m['name'])
        else:
            m_name_disp = m['name']
            
        print(
            f"{prefix} {status_str} [ID: {m['id']}] {pad_visible(m_name_disp, 15)} "
            f"({cyan(fmt_date(m['start_date']))} -> {cyan(fmt_date(m['end_date']))}) "
            f"[{bar}]{extra} {duration_desc}"
        )
              
        focus_lines = textwrap.wrap(m['focus'], width=80)
        for line in focus_lines:
            print(f"  {line}")
        if m.get('feedback'):
            fb_lines = textwrap.wrap(m['feedback'], width=80)
            print(f"  {bold('Mesocycle Feedback')}:")
            for line in fb_lines:
                print(f"    {line}")
        print("  " + gray("-" * 40))


def run_plan_rm(args: argparse.Namespace) -> None:
    """Deletes the periodization plan for a specific goal."""
    goal = db.get_objective(args.id)
    if not goal:
        print(red(f"Goal with ID {args.id} not found."))
        return

    macro = db.get_macrocycle_for_objective(args.id)
    if not macro:
        print(yellow(f"No periodization plan exists for goal '{goal['title']}' (ID {args.id})."))
        return

    # Delete the plan
    coach_service.delete_plan(args.id)
    print(green(f"Periodization plan for goal '{goal['title']}' removed successfully."))

    # Warn about subsequent plans
    objectives = db.get_objectives(status='active')
    subsequent_goals_with_plans = []
    for obj in objectives:
        if str(obj['target_date']) > str(goal['target_date']):
            if obj['id'] is not None:
                sub_macro = db.get_macrocycle_for_objective(obj['id'])
                if sub_macro:
                    subsequent_goals_with_plans.append(obj)

    if subsequent_goals_with_plans:
        print(yellow(
            "\nWarning: The following subsequent active goals have existing plans that\n"
            "were aligned with the plan you just deleted. You may need to regenerate them\n"
            "so their dates align correctly (e.g. running 'plan generate --goal <ID> --force'):"
        ))
        for sg in subsequent_goals_with_plans:
            print(yellow(f" - ID {sg['id']}: '{sg['title']}' (Target date: {sg['target_date']})"))


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
    print(green("All periodization plans wiped successfully."))


def _edit_text_in_editor(initial: str) -> Optional[str]:
    """Opens $EDITOR (falling back to vi) seeded with `initial`, returns the saved text.

    Returns None if the editor exits non-zero (treated as an abort). Trailing newlines are
    stripped. Used by `plan feedback --edit`.
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="trainmate-feedback-", delete=False
    ) as tf:
        tf.write(initial or "")
        path = tf.name
    try:
        result = subprocess.run([editor, path])
        if result.returncode != 0:
            print(red(f"Editor exited with status {result.returncode}; feedback unchanged."))
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read().rstrip("\n")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _resolve_feedback_text(args: argparse.Namespace, current: Optional[str]) -> Optional[str]:
    """Returns the feedback text to save: either the editor result (--edit, seeded with the
    current value) or the positional `text`. Returns None to signal 'do not save' (aborted
    edit or empty input)."""
    if args.edit:
        new_text = _edit_text_in_editor(current or "")
        if new_text is None:
            return None
        if not new_text.strip():
            print(red("Error: Feedback is empty; nothing saved."))
            return None
        return new_text
    if not args.text:
        print(red("Error: Feedback text cannot be empty (or use --edit)."))
        sys.exit(1)
    return args.text


_FEEDBACK_REGEN_NOTE = (
    "Note: You must regenerate the periodization plan to apply this feedback.\n"
    "Run 'plan generate --force' (or with '--goal <ID> --force') to update the plan."
)


def run_plan_feedback(args: argparse.Namespace) -> None:
    """Saves athlete feedback for a macrocycle or specific mesocycle.

    With --edit, opens $EDITOR seeded with the current feedback instead of taking text.
    """
    if not args.macro and not args.meso:
        print(red("Error: You must specify --macro or --meso <id>."))
        sys.exit(1)

    # 1. Handle mesocycle feedback directly if specified
    if args.meso:
        meso = db.get_mesocycle(args.meso)
        if not meso:
            print(red(f"Mesocycle with ID {args.meso} not found."))
            sys.exit(1)
        text = _resolve_feedback_text(args, meso.get('feedback'))
        if text is None:
            return
        db.update_mesocycle_feedback(args.meso, text)
        print(green(
            f"Feedback successfully saved for Mesocycle ID {args.meso} ('{meso['name']}')."
        ))
        print(yellow(_FEEDBACK_REGEN_NOTE))
        return

    # 2. Handle macrocycle feedback. Find target goal first.
    objectives = db.get_objectives(status='active')
    if not objectives:
        print(yellow("No active goals found. TrainMate needs at least one objective."))
        sys.exit(1)

    if args.goal_id is not None:
        target_goals = [o for o in objectives if o['id'] == args.goal_id]
        if not target_goals:
            print(red(f"Active goal with ID {args.goal_id} not found."))
            sys.exit(1)
        next_goal = target_goals[0]
    else:
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]

    macro = db.get_macrocycle_for_objective(next_goal['id'])
    if not macro:
        print(yellow(f"No active periodization plan exists for goal '{next_goal['title']}'."))
        sys.exit(1)

    text = _resolve_feedback_text(args, macro.get('feedback'))
    if text is None:
        return
    db.update_macrocycle_feedback(macro['id'], text)
    print(green(
        f"Feedback successfully saved for Macrocycle ID {macro['id']} "
        f"(Goal: '{next_goal['title']}')."
    ))
    print(yellow(_FEEDBACK_REGEN_NOTE))


# ==============================================================================
# Workout Command
# ==============================================================================

def _ensure_recent_data(end_date: Optional[str] = None, no_pull: bool = False) -> None:
    """Ensures Garmin data covering the recent metrics window is present and fresh,
    auto-pulling small/recent gaps and surfacing large backfills as a command. Warns
    if today's metrics are still unavailable afterward."""
    if no_pull:
        return
    end_date = end_date or _today_str()
    history_days = config.metrics_lookback_days
    start_date = (
        datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(days=history_days - 1)
    ).strftime("%Y-%m-%d")
    garmin.ensure_data(start_date, end_date)

    today = _today_str()
    if end_date == today:
        rows = db.get_metrics_cache(start_date=today, end_date=today)
        present = bool(rows) and not (
            rows[0].get('rhr') is None and rows[0].get('hrv') is None
            and rows[0].get('sleep_score') is None and rows[0].get('stress') is None
        )
        if not present:
            print(yellow(f"Note: Garmin metrics for today ({today}) are not available yet."))


def run_workout_adapt(args: argparse.Namespace) -> None:
    # Executes the daily workout Garmin adaptation checks command.
    date_str = args.date or _today_str()

    _ensure_recent_data(date_str, no_pull=args.no_pull)

    # Display rolling trajectory
    try:
        history_days = config.metrics_lookback_days
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_date = (date_obj - timedelta(days=history_days - 1)).strftime("%Y-%m-%d")
        metrics_history = db.get_metrics_cache(start_date=start_date, end_date=date_str)
        
        print(bold(cyan("\n=== METRICS TRAJECTORY (PAST 5 DAYS) ===")))
        print(bold(
            f"{'Date':<12} | {'HRV (ms)':<8} | {'RHR (bpm)':<9} | {'Sleep':<5} | {'ACWR':<5}"
        ))
        print(gray("-" * 50))
        for m in metrics_history:
            base = db.get_baseline(m['date'])
            
            hrv_val = m['hrv']
            rhr_val = m['rhr']
            sleep_val = m['sleep_score']
            acwr_val = m['acwr']
            
            hrv_str = f"{hrv_val or 'N/A'}"
            rhr_str = f"{rhr_val or 'N/A'}"
            sleep_str = f"{sleep_val or 'N/A'}"
            acwr_str = color_acwr(acwr_val) if acwr_val is not None else "N/A"
            
            if base:
                if hrv_val is not None and base['hrv_baseline_mean'] is not None:
                    sd = base['hrv_baseline_std'] or 1.0
                    if hrv_val < (base['hrv_baseline_mean'] - sd):
                        hrv_str = red(f"{hrv_val} (v)")
                    else:
                        hrv_str = green(str(hrv_val))
                if rhr_val is not None and base['rhr_baseline_mean'] is not None:
                    sd = base['rhr_baseline_std'] or 1.0
                    if rhr_val > (base['rhr_baseline_mean'] + max(3.0, sd)):
                        rhr_str = red(f"{rhr_val} (^)")
                    else:
                        rhr_str = green(str(rhr_val))
                if sleep_val is not None:
                    if sleep_val < 60:
                        sleep_str = red(f"{sleep_val} (v)")
                    else:
                        sleep_str = green(str(sleep_val))
            
            date_col = pad_visible(m['date'], 12)
            hrv_col = pad_visible(hrv_str, 8)
            rhr_col = pad_visible(rhr_str, 9)
            sleep_col = pad_visible(sleep_str, 5)
            acwr_col = pad_visible(acwr_str, 5)
            print(f"{date_col} | {hrv_col} | {rhr_col} | {sleep_col} | {acwr_col}")
        print(gray("((v) suppressed/poor, (^) elevated compared to baseline)\n"))
    except Exception as e:
        print(yellow(f"Warning: Could not display metrics trajectory: {e}"))

    print(f"Evaluating daily Garmin metrics adaptation for {date_str}...")
    try:
        reason, proposed_workouts = coach_service.adapt(date_str)
        print(f"\n{bold('Decision Summary')}:\n{wrap_text(reason, width=80)}")
        
        if not proposed_workouts:
            print(green(
                "\nAll metrics are green and workout plan is on track. No changes recommended."
            ))
            return

        print(bold(yellow("\nPROPOSED WORKOUT ADAPTATIONS:")))
        print(bold(
            f"{'Date':<12} | {'Sport':<12} | {'Original Workout':<25} | "
            f"{'Adapted Workout':<25} | {'Duration/RPE/TSS':<16}"
        ))
        print(gray("-" * 100))
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
            
            date_col = pad_visible(cyan(pw['date']), 12)
            sport_col = pad_visible(magenta(pw['sport_type'].upper()), 12)
            orig_col = pad_visible(gray(orig_title), 25)
            new_col = pad_visible(green(pw['title']), 25)
            stats_col = pad_visible(yellow(stats_diff), 16)
            
            print(f"{date_col} | {sport_col} | {orig_col} | {new_col} | {stats_col}")

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
            coach_service.apply_adaptations(
                proposed_workouts, reason, start_date_adapt, end_date_adapt
            )
            print(green("Adaptations applied and synced to calendar successfully."))
        else:
            print("\nAdaptations discarded.")

    except Exception as e:
        print(red(f"Error executing daily adaptation: {e}"))


def _resolve_workout_end_date(
    args: argparse.Namespace, resolved_goal: dict | None
) -> str | None:
    """Returns the end date string for workout generation based on CLI horizon flags."""
    today = _today_date()

    if getattr(args, 'horizon_days', None) is not None:
        return (today + timedelta(days=args.horizon_days)).strftime("%Y-%m-%d")

    if getattr(args, 'horizon_weeks', None) is not None:
        return (today + timedelta(days=round(args.horizon_weeks * 7))).strftime("%Y-%m-%d")

    if getattr(args, 'horizon_until', None) is not None:
        try:
            datetime.strptime(args.horizon_until, "%Y-%m-%d")
        except ValueError:
            print(red(f"Invalid date format for --until: '{args.horizon_until}'. Use YYYY-MM-DD."))
            sys.exit(1)
        return args.horizon_until

    if getattr(args, 'horizon_goal_id', None) is not None:
        goal_id = args.horizon_goal_id
        if goal_id == -1:
            # Sentinel: use the already-resolved goal for this generate run
            if resolved_goal is None:
                print(red("No active goal found for --until-goal."))
                sys.exit(1)
            return resolved_goal['target_date']
        goal = next((o for o in db.get_objectives(status='active') if o['id'] == goal_id), None)
        if goal is None:
            print(red(f"Active goal with ID {goal_id} not found."))
            sys.exit(1)
        return goal['target_date']

    if getattr(args, 'horizon_meso_id', None) is not None:
        meso = db.get_mesocycle(args.horizon_meso_id)
        if meso is None:
            print(red(f"Mesocycle with ID {args.horizon_meso_id} not found."))
            sys.exit(1)
        return meso['end_date']

    return None  # fall back to config default in generate_workouts()


def run_workout_generate(args: argparse.Namespace) -> None:
    """Executes the AI workout generation command based on active strategy."""
    _ensure_recent_data(no_pull=args.no_pull)

    try:
        objectives = db.get_objectives(status='active')
        if objectives:
            if args.goal_id is not None:
                target_goals = [o for o in objectives if o['id'] == args.goal_id]
                next_goal = target_goals[0] if target_goals else None
            else:
                objectives.sort(key=lambda x: str(x['target_date']))
                next_goal = objectives[0]

            if next_goal:
                macro = db.get_macrocycle_for_objective(next_goal['id'])
                if macro:
                    current_hash = coach_service._get_config_hash()
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
                            print(yellow(
                                "Workout generation cancelled. Please run 'plan generate' first."
                            ))
                            return
                        else:
                            print("Proceeding. Updating configuration hash in database.")
                            db.update_macrocycle_config_hash(macro['id'], current_hash)

        workout_kwargs = {}
        if args.goal_id is not None:
            workout_kwargs['objective_id'] = args.goal_id

        end_date = _resolve_workout_end_date(args, next_goal if objectives else None)
        if end_date is not None:
            workout_kwargs['end_date'] = end_date

        reasoning, workouts = coach_service.generate_workouts(**workout_kwargs)
        print(bold(cyan("\n=== WORKOUTS GENERATED BY COACH ===")))
        print(f"{bold('Reasoning')}:\n{wrap_text(reasoning, width=80)}\n")
        print(green(
            f"Generated {len(workouts)} workouts starting from today. Save complete."
        ))
        print("Run 'workout push' to commit this plan to Google Calendar.")
    except Exception as e:
        print(red(f"Error during workout generation: {e}"))



def _resolve_workout_date_range(
    args: argparse.Namespace,
) -> tuple[str | None, str | None]:
    """Resolves (start_date, end_date) from the shared date-filter CLI args."""
    today_str = _today_str()

    # Resolve start_date
    start_date = None
    if getattr(args, 'from_date', None) is not None:
        start_date = args.from_date
    elif getattr(args, 'from_meso', False):
        active_meso = db.get_active_mesocycle(today_str)
        if not active_meso:
            print(red("Error: No active mesocycle found to start from."))
            sys.exit(1)
        start_date = active_meso['start_date']
    elif getattr(args, 'meso_id', None) is not None:
        meso_id = args.meso_id
        if meso_id == -1:
            active_meso = db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        start_date = meso['start_date']
    elif getattr(args, 'until_meso_id', None) is not None:
        meso_id = args.until_meso_id
        if meso_id == -1:
            active_meso = db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        # --until-mesocycle starts from today by default
        start_date = today_str
    elif getattr(args, 'goal_id', None) is not None:
        goal_id = args.goal_id
        if goal_id == -1:
            active_goal = db.get_active_objective()
            if not active_goal:
                print(red("Error: No active goal found."))
                sys.exit(1)
            goal_id = active_goal['id']
        macro = db.get_macrocycle_for_objective(goal_id)
        if not macro:
            print(red(f"Error: No plan exists for Goal ID {goal_id}."))
            sys.exit(1)
        mesos = db.get_mesocycles_for_macrocycle(macro['id'])
        if not mesos:
            print(red(f"Error: No mesocycles found for Goal ID {goal_id}."))
            sys.exit(1)
        start_date = min(m['start_date'] for m in mesos)
    elif (
        getattr(args, 'days', None) is not None
        or getattr(args, 'weeks', None) is not None
        or getattr(args, 'until_date', None) is not None
    ):
        start_date = today_str

    # Resolve end_date — map list/push args to the horizon_* namespace expected
    # by _resolve_workout_end_date()
    target_meso_id = None
    if getattr(args, 'until_meso_id', None) is not None:
        target_meso_id = args.until_meso_id
    elif getattr(args, 'meso_id', None) is not None:
        target_meso_id = args.meso_id

    if target_meso_id == -1:
        active_meso = db.get_active_mesocycle(today_str)
        if not active_meso:
            print(red("Error: No active mesocycle found."))
            sys.exit(1)
        target_meso_id = active_meso['id']

    target_goal_id = None
    if getattr(args, 'goal_id', None) is not None:
        target_goal_id = args.goal_id
        if target_goal_id == -1:
            active_goal = db.get_active_objective()
            if not active_goal:
                print(red("Error: No active goal found."))
                sys.exit(1)
            target_goal_id = active_goal['id']

    horizon_args = argparse.Namespace(
        horizon_days=getattr(args, 'days', None),
        horizon_weeks=getattr(args, 'weeks', None),
        horizon_until=getattr(args, 'until_date', None),
        horizon_goal_id=target_goal_id,
        horizon_meso_id=target_meso_id,
    )

    next_goal = db.get_active_objective()
    end_date = _resolve_workout_end_date(horizon_args, next_goal)

    return start_date, end_date


def run_workout_list(args: argparse.Namespace) -> None:
    """Lists stored workouts chronologically, with optional date, goal, or type filters."""
    start_date, end_date = _resolve_workout_date_range(args)

    # Fetch workouts using our extended db.get_workouts
    workouts = db.get_workouts(
        start_date=start_date,
        end_date=end_date,
        sport_type=args.sport_type
    )
    
    print(bold(cyan("=== WORKOUT SCHEDULE ===")))
    if start_date or end_date or args.sport_type:
        filter_parts = []
        if start_date:
            filter_parts.append(f"From: {start_date}")
        if end_date:
            filter_parts.append(f"Until: {end_date}")
        if args.sport_type:
            filter_parts.append(f"Type: {args.sport_type}")
        print(gray(f"Filters: {', '.join(filter_parts)}"))
        
    for w in workouts:
        mod_marker = ""
        if w['modification_reason']:
            mod_marker = bold(yellow(" [ADAPTED]"))
        sync_marker = ""
        if w['synced']:
            sync_marker = bold(green(" [SYNCED]"))
        duration = w.get('duration_minutes')
        tss = w.get('tss')
        duration_str = f" | {duration}min" if duration else ""
        tss_str = f" | TSS {tss}" if tss is not None else ""
        print(
            f"ID: {w['id']} | {cyan(fmt_date(w['date']))} | {magenta(w['sport_type'].upper())} | "
            f"{bold(w['title'])}{mod_marker}{sync_marker}{duration_str}{tss_str}"
        )
        print(format_labeled_block("  Description:", w['description']))
        if w.get('modification_reason'):
            print(format_labeled_block("  Reason:", w['modification_reason'], color_fn=yellow))
        print(gray("-" * 40))


def run_workout_compare(args: argparse.Namespace) -> None:
    """Compares planned workouts against completed activities for the given date range."""
    today_str = _today_str()
    today_obj = _today_date()

    start_date, end_date = _resolve_workout_date_range(args)

    # For compare, --days/--weeks mean "look back N days" instead of "look forward N days".
    # An explicit --from/--mesocycle/--goal already sets start_date to the right anchor.
    has_explicit_start = (
        getattr(args, 'from_date', None) is not None
        or getattr(args, 'from_meso', False)
        or getattr(args, 'meso_id', None) is not None
        or getattr(args, 'goal_id', None) is not None
    )
    if not has_explicit_start:
        days = getattr(args, 'days', None)
        weeks = getattr(args, 'weeks', None)
        if days is not None:
            start_date = (today_obj - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        elif weeks is not None:
            ndays = max(1, round(weeks * 7))
            start_date = (today_obj - timedelta(days=ndays - 1)).strftime("%Y-%m-%d")
        else:
            # Default or --until-only: 14-day lookback
            start_date = (today_obj - timedelta(days=13)).strftime("%Y-%m-%d")

    # Cap end_date at today — we can only compare past/present activities
    if end_date is None or end_date > today_str:
        end_date = today_str

    if not getattr(args, 'no_pull', False):
        try:
            garmin.ensure_data(start_date, end_date)
        except Exception as e:
            print(yellow(f"Warning: Could not ensure recent data: {e}"))

    all_workouts = db.get_workouts(start_date=start_date, end_date=end_date)
    activities = db.get_completed_activities(start_date=start_date, end_date=end_date)

    start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
    history_days = (end_date_obj - start_date_obj).days + 1

    discrepancies, matching_results = analyze_adherence(
        planned_workouts=all_workouts,
        completed_activities=activities,
        start_date_obj=start_date_obj,
        history_days=history_days,
        low_load_threshold=config.low_load_threshold,
    )

    sport_filter = (getattr(args, 'sport_type', None) or "").lower() or None

    matched_act_ids = {
        r['completed']['activity_id'] for r in matching_results if r['completed']
    }

    acts_by_date: dict = {}
    for act in activities:
        acts_by_date.setdefault(act['date'], []).append(act)

    results_by_date: dict = {}
    for r in matching_results:
        results_by_date.setdefault(r['date'], []).append(r)

    print(bold(cyan("=== WORKOUT COMPARE ===")))
    filter_parts = [f"From: {start_date}", f"Until: {end_date}"]
    if sport_filter:
        filter_parts.append(f"Type: {sport_filter}")
    print(gray(f"Filters: {', '.join(filter_parts)}"))
    print()

    def _fmt_act(act: dict) -> str:
        dur = f"{act['duration_sec'] / 60:.0f}min"
        parts: list[str] = [dur]
        if act.get('tss'):
            parts.append(f"TSS {act['tss']:.0f}")
        if act.get('rpe'):
            parts.append(f"RPE {act['rpe']}")
        return f"[{act['activity_type']}] {act['activity_name']} ({', '.join(parts)})"

    has_output = False
    for d in range(history_days):
        date_curr = (start_date_obj + timedelta(days=d)).strftime("%Y-%m-%d")
        day_results = results_by_date.get(date_curr, [])
        day_acts = acts_by_date.get(date_curr, [])
        unplanned = [a for a in day_acts if a['activity_id'] not in matched_act_ids]

        if sport_filter:
            day_results = [
                r for r in day_results
                if r['planned']['sport_type'].lower() == sport_filter
            ]
            unplanned = [
                a for a in unplanned
                if sport_filter in a['activity_type'].lower()
            ]

        if not day_results and not unplanned:
            continue

        has_output = True
        print(bold(cyan(fmt_date(date_curr))))

        for r in day_results:
            w = r['planned']
            act = r['completed']
            is_rest = w['sport_type'] == 'rest'

            if is_rest:
                print(f"  PLANNED:    [{magenta('REST')}]")
            else:
                parts = []
                if w.get('duration_minutes'):
                    parts.append(f"{w['duration_minutes']}min")
                if w.get('tss') is not None:
                    parts.append(f"TSS {w['tss']}")
                info = f" ({', '.join(parts)})" if parts else ""
                print(f"  PLANNED:    [{magenta(w['sport_type'].upper())}] {bold(w['title'])}{info}")

            if act:
                act_str = _fmt_act(act)
                if is_rest:
                    print(f"  ACTUAL:     {red(act_str)} {bold(red('[REST VIOLATION]'))}")
                else:
                    print(f"  ACTUAL:     {green(act_str)}")
            elif not is_rest:
                print(f"  ACTUAL:     {red('(none — missed)')}")

        for act in unplanned:
            act_load = (
                (act.get('tss') or 0.0)
                + (act.get('rpe') or 0) * (act['duration_sec'] / 3600.0)
            )
            act_str = _fmt_act(act)
            if act_load >= config.low_load_threshold:
                print(f"  UNPLANNED:  {yellow(act_str)}")
            else:
                print(gray(f"  (minor):    {act_str}"))

        print(gray("-" * 40))

    if not has_output:
        print(gray("No planned workouts or completed activities found in this range."))
        return

    print()
    if discrepancies:
        print(bold(yellow("=== DISCREPANCIES ===")))
        for disc in discrepancies:
            print(yellow(disc))
        print()
        n = len(discrepancies)
        print(bold(yellow(f"{n} discrepanc{'ies' if n != 1 else 'y'} found.")))
    else:
        print(bold(green("No discrepancies found. Great adherence!")))


def run_workout_push(args: argparse.Namespace) -> None:
    """Synchronizes planned workouts with Google Calendar."""
    today_str = _today_str()
    force = getattr(args, 'force', False)

    start_date, end_date = _resolve_workout_date_range(args)
    # Default to today onwards when no date filter is given
    if start_date is None:
        start_date = today_str

    all_workouts = db.get_workouts(
        start_date=start_date,
        end_date=end_date,
        sport_type=getattr(args, 'sport_type', None),
    )

    to_push = all_workouts if force else [w for w in all_workouts if not w['synced']]

    if not to_push:
        if force:
            print("No workouts found in the specified range.")
        else:
            print(
                "No new or modified workouts to sync. "
                "Run 'workout generate' to generate a schedule, "
                "or use -f to re-push already-synced workouts."
            )
        return

    print(f"Syncing {len(to_push)} workouts to Google Calendar...")
    try:
        calendar_syncer.sync_multiple(to_push)
        print(green("Google Calendar synchronization completed."))
    except Exception as e:
        print(red(f"Error syncing to Google Calendar: {e}"))


def run_workout_rm(args: argparse.Namespace) -> None:
    """Deletes a planned workout by its database ID."""
    workout = db.get_workout_by_id(args.id)
    if not workout:
        print(red(f"Workout with ID {args.id} not found."))
        return
        
    if workout.get('google_event_id'):
        print("Workout is synced to Google Calendar. Attempting to delete calendar event...")
        if workout['google_event_id'] is not None:
            calendar_syncer.delete_workout_event(workout['google_event_id'])
        
    db.delete_workout_by_id(args.id)
    print(green(
        f"Workout with ID {args.id} ('{workout['title']}') removed successfully."
    ))


def _resolve_swap_ops(args: argparse.Namespace) -> list | None:
    """Turns CLI args into swap operations, or returns None on a usage/lookup error."""
    using_ids = args.id1 is not None or args.id2 is not None
    using_dates = bool(args.date1 or args.date2)

    if using_ids and using_dates:
        print(red("Provide either two dates or --id1/--id2, not both."))
        return None

    if using_ids:
        if args.id1 is None or args.id2 is None:
            print(red("Both --id1 and --id2 are required for an ID-based swap."))
            return None
        w1 = db.get_workout_by_id(args.id1)
        w2 = db.get_workout_by_id(args.id2)
        if not w1:
            print(red(f"Workout with ID {args.id1} not found."))
            return None
        if not w2:
            print(red(f"Workout with ID {args.id2} not found."))
            return None
        if w1['date'] == w2['date']:
            print(yellow("Both workouts are already on the same date; nothing to swap."))
            return None
        print(
            f"Swapping [{w1['id']}] {w1['title']} ({w1['date']}) <-> "
            f"[{w2['id']}] {w2['title']} ({w2['date']})"
        )
        return [
            {'id': w1['id'], 'new_date': w2['date']},
            {'id': w2['id'], 'new_date': w1['date']},
        ]

    if args.date1 and args.date2:
        for d in (args.date1, args.date2):
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                print(red(f"Invalid date format: '{d}'. Use YYYY-MM-DD."))
                return None
        if args.date1 == args.date2:
            print(yellow("The two dates are identical; nothing to swap."))
            return None
        on_1 = db.get_workouts(start_date=args.date1, end_date=args.date1)
        on_2 = db.get_workouts(start_date=args.date2, end_date=args.date2)
        if not on_1 and not on_2:
            print(yellow(
                f"No workouts on either {args.date1} or {args.date2}; nothing to swap."
            ))
            return None
        desc_1 = ", ".join(w['title'] for w in on_1) or "(rest)"
        desc_2 = ", ".join(w['title'] for w in on_2) or "(rest)"
        print(f"Swapping {args.date1} [{desc_1}] <-> {args.date2} [{desc_2}]")
        return (
            [{'id': w['id'], 'new_date': args.date2} for w in on_1]
            + [{'id': w['id'], 'new_date': args.date1} for w in on_2]
        )

    print(red("Specify two dates (e.g. 'workout swap 2026-06-09 2026-06-11') "
              "or --id1 and --id2."))
    return None


def run_workout_swap(args: argparse.Namespace) -> None:
    """Exchanges workouts between two dates or two IDs, with recovery validation."""
    ops = _resolve_swap_ops(args)
    if not ops:
        return

    warnings = coach_service.validate_swap(ops)
    if warnings:
        print(bold(yellow("\nSwap warnings:")))
        for msg in warnings:
            print(yellow(f"  - {msg}"))
        if not args.force:
            confirm = input("\nProceed with the swap anyway? [y/N]: ").strip().lower()
            if confirm != 'y':
                print("\nSwap cancelled.")
                return

    updated = coach_service.apply_swap(ops, args.no_sync)
    print(green(f"\nSwapped {len(updated)} workout(s) successfully."))
    for w in updated:
        print(f"  [{w['id']}] {w['title']} -> {w['date']}")
    if args.no_sync:
        print(gray("Calendar sync skipped (--no-sync)."))


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
    print(green("All workouts wiped successfully."))


# ==============================================================================
# Data Command
# ==============================================================================

def run_data_pull(args: argparse.Namespace) -> None:
    """Pulls athlete metrics and activities directly from Garmin Connect.

    Explicit/manual pull: does exactly the range asked for (mirrors GarminScraper's
    options) and advances the watermark. The watermark/auto-ensure logic lives in
    garmin.ensure_data, which commands call when reading.
    """
    end_date = args.until_date or _today_str()
    if args.from_date:
        start_date = args.from_date
    else:
        days = max(1, args.days)
        start_date = (
            datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(days=days - 1)
        ).strftime("%Y-%m-%d")

    try:
        garmin.pull(
            start_date, end_date,
            metrics=not args.activities_only,
            activities=not args.metrics_only,
            throttle=args.sleep,
        )
    except garmin.GarminAuthRequired as e:
        print(red(f"Garmin authentication required: {e}"))
        print(yellow("Run this command in an interactive terminal to complete MFA."))
    except Exception as e:
        print(red(f"Error pulling from Garmin: {e}"))


def run_data_backfill_tss(args: argparse.Namespace) -> None:
    """Recomputes stored TSS for all cached activities under the current
    zone-based hierarchy, then refreshes derived workload/ACWR."""
    changed = garmin.backfill_tss(
        start_date=args.from_date,
        end_date=args.until_date,
        verbose=getattr(args, "verbose", False),
    )
    print(green(f"Backfill complete. {changed} activities updated."))


def run_data_wipe(args: argparse.Namespace) -> None:
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
    print(green("All metrics, baselines, and completed activities wiped successfully."))


def _resolve_historical_date_range(
    args: argparse.Namespace, default_days: int = 7
) -> tuple[Optional[str], Optional[str]]:
    """Resolves (start_date, end_date) for historical queries, looking back by default."""
    if getattr(args, 'all', False):
        return None, None

    today_str = _today_str()

    # Determine end_date (default is today_str, capped/anchored by until/mesocycle/goal)
    end_date = today_str
    if getattr(args, 'until_date', None) is not None:
        end_date = args.until_date
    elif getattr(args, 'until_meso_id', None) is not None:
        meso_id = args.until_meso_id
        if meso_id == -1:
            active_meso = db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        end_date = meso['end_date']
    elif getattr(args, 'meso_id', None) is not None:
        meso_id = args.meso_id
        if meso_id == -1:
            active_meso = db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        end_date = meso['end_date']
    elif getattr(args, 'goal_id', None) is not None:
        goal_id = args.goal_id
        if goal_id == -1:
            active_goal = db.get_active_objective()
            if not active_goal:
                print(red("Error: No active goal found."))
                sys.exit(1)
            goal_id = active_goal['id']
        macro = db.get_macrocycle_for_objective(goal_id)
        if not macro:
            print(red(f"Error: No plan exists for Goal ID {goal_id}."))
            sys.exit(1)
        mesos = db.get_mesocycles_for_macrocycle(macro['id'])
        if not mesos:
            print(red(f"Error: No mesocycles found for Goal ID {goal_id}."))
            sys.exit(1)
        end_date = max(m['end_date'] for m in mesos)

    # Validate end_date format
    try:
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        print(red(f"Invalid date format for end date: '{end_date}'. Use YYYY-MM-DD."))
        sys.exit(1)

    # Determine start_date
    start_date = None
    if getattr(args, 'from_date', None) is not None:
        start_date = args.from_date
    elif getattr(args, 'from_meso', False):
        active_meso = db.get_active_mesocycle(today_str)
        if not active_meso:
            print(red("Error: No active mesocycle found to start from."))
            sys.exit(1)
        start_date = active_meso['start_date']
    elif getattr(args, 'meso_id', None) is not None:
        start_date = meso['start_date']
    elif getattr(args, 'goal_id', None) is not None:
        start_date = min(m['start_date'] for m in mesos)

    # If start_date is still not resolved, resolve it via days/weeks lookback from end_date
    if start_date is None:
        days = getattr(args, 'days', None)
        weeks = getattr(args, 'weeks', None)
        if days is not None:
            start_date_obj = end_date_obj - timedelta(days=days - 1)
        elif weeks is not None:
            ndays = max(1, round(weeks * 7))
            start_date_obj = end_date_obj - timedelta(days=ndays - 1)
        else:
            start_date_obj = end_date_obj - timedelta(days=default_days - 1)
        start_date = start_date_obj.strftime("%Y-%m-%d")

    # Validate start_date format
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        print(red(f"Invalid date format for start date: '{start_date}'. Use YYYY-MM-DD."))
        sys.exit(1)

    return start_date, end_date


def run_data_show_metrics(args: argparse.Namespace) -> None:
    """Displays athlete metrics over the resolved date range."""
    start_date, end_date = _resolve_historical_date_range(args, default_days=7)

    if not getattr(args, 'no_pull', False) and not getattr(args, 'all', False):
        try:
            garmin.ensure_data(start_date, end_date)
        except Exception as e:
            print(yellow(f"Warning: Could not ensure recent data: {e}"))

    metrics_history = db.get_metrics_cache(start_date=start_date, end_date=end_date)

    if getattr(args, 'csv', False):
        _show_metrics_csv(metrics_history)
        return

    range_str = f"{start_date} to {end_date}" if start_date and end_date else "All Time"
    print(bold(cyan(f"\n=== ATHLETE METRICS ({range_str}) ===")))
    if not metrics_history:
        print("No metrics cached in this range.")
        return

    print(bold(
        f"{'Date':<12} | {'HRV':<8} | {'HRV Base':<9} | {'RHR':<8} | {'RHR Base':<8} | "
        f"{'Sleep':<8} | {'Sleep Base':<10} | {'Stress':<6} | {'ACWR':<5} | "
        f"{'Acute':<7} | {'Chronic':<7}"
    ))
    print(gray("-" * 107))

    for m in metrics_history:
        base = db.get_baseline(m['date'])

        hrv_val = m['hrv']
        rhr_val = m['rhr']
        sleep_val = m['sleep_score']
        stress_val = m['stress']
        acwr_val = m['acwr']
        acute_val = m['acute_workload']
        chronic_val = m['chronic_workload']

        hrv_str = str(hrv_val) if hrv_val is not None else "N/A"
        rhr_str = str(rhr_val) if rhr_val is not None else "N/A"
        sleep_str = str(sleep_val) if sleep_val is not None else "N/A"
        stress_str = str(stress_val) if stress_val is not None else "N/A"
        acwr_str = color_acwr(acwr_val) if acwr_val is not None else "N/A"
        acute_str = f"{acute_val:.1f}" if acute_val is not None else "N/A"
        chronic_str = f"{chronic_val:.1f}" if chronic_val is not None else "N/A"

        hrv_base_str = "N/A"
        rhr_base_str = "N/A"
        sleep_base_str = "N/A"

        if base:
            if hrv_val is not None and base['hrv_baseline_mean'] is not None:
                sd = base['hrv_baseline_std'] or 1.0
                if hrv_val < (base['hrv_baseline_mean'] - sd):
                    hrv_str = red(f"{hrv_val} (v)")
                else:
                    hrv_str = green(str(hrv_val))
            if base['hrv_baseline_mean'] is not None:
                hrv_base_str = f"{base['hrv_baseline_mean']:.1f}"

            if rhr_val is not None and base['rhr_baseline_mean'] is not None:
                sd = base['rhr_baseline_std'] or 1.0
                if rhr_val > (base['rhr_baseline_mean'] + max(3.0, sd)):
                    rhr_str = red(f"{rhr_val} (^)")
                else:
                    rhr_str = green(str(rhr_val))
            if base['rhr_baseline_mean'] is not None:
                rhr_base_str = f"{base['rhr_baseline_mean']:.1f}"

            if sleep_val is not None:
                if sleep_val < 60:
                    sleep_str = red(f"{sleep_val} (v)")
                else:
                    sleep_str = green(str(sleep_val))
            if base['sleep_baseline_mean'] is not None:
                sleep_base_str = f"{base['sleep_baseline_mean']:.1f}"

        date_col = pad_visible(m['date'], 12)
        hrv_col = pad_visible(hrv_str, 8)
        hrv_base_col = pad_visible(hrv_base_str, 9)
        rhr_col = pad_visible(rhr_str, 8)
        rhr_base_col = pad_visible(rhr_base_str, 8)
        sleep_col = pad_visible(sleep_str, 8)
        sleep_base_col = pad_visible(sleep_base_str, 10)
        stress_col = pad_visible(stress_str, 6)
        acwr_col = pad_visible(acwr_str, 5)
        acute_col = pad_visible(acute_str, 7)
        chronic_col = pad_visible(chronic_str, 7)

        print(
            f"{date_col} | {hrv_col} | {hrv_base_col} | {rhr_col} | {rhr_base_col} | "
            f"{sleep_col} | {sleep_base_col} | {stress_col} | {acwr_col} | "
            f"{acute_col} | {chronic_col}"
        )
    print(gray("((v) suppressed/poor, (^) elevated compared to baseline)\n"))


def _show_metrics_csv(metrics_history: list) -> None:
    """Output metrics as CSV."""
    writer = csv_mod.writer(sys.stdout)
    writer.writerow([
        "date", "hrv", "hrv_baseline", "rhr", "rhr_baseline",
        "sleep_score", "sleep_baseline", "stress", "acwr",
        "acute_workload", "chronic_workload",
    ])
    for m in metrics_history:
        base = db.get_baseline(m['date'])
        hrv_base = None
        rhr_base = None
        sleep_base = None
        if base:
            hrv_base = base.get('hrv_baseline_mean')
            rhr_base = base.get('rhr_baseline_mean')
            sleep_base = base.get('sleep_baseline_mean')
        writer.writerow([
            m['date'], m['hrv'], hrv_base, m['rhr'], rhr_base,
            m['sleep_score'], sleep_base, m['stress'], m['acwr'],
            m['acute_workload'], m['chronic_workload'],
        ])


def run_data_show_activities(args: argparse.Namespace) -> None:
    """Displays completed activities over the resolved date range."""
    start_date, end_date = _resolve_historical_date_range(args, default_days=7)

    if not getattr(args, 'no_pull', False) and not getattr(args, 'all', False):
        try:
            garmin.ensure_data(start_date, end_date)
        except Exception as e:
            print(yellow(f"Warning: Could not ensure recent data: {e}"))

    activities = db.get_completed_activities(
        start_date=start_date, end_date=end_date
    )

    if getattr(args, 'sport_type', None) is not None:
        activities = [
            act for act in activities
            if act['activity_type'].lower() == args.sport_type.lower()
        ]

    if getattr(args, 'csv', False):
        _show_activities_csv(activities)
        return


    range_str = f"{start_date} to {end_date}" if start_date and end_date else "All Time"
    print(bold(cyan(f"\n=== COMPLETED ACTIVITIES ({range_str}) ===")))
    if not activities:
        print("No completed activities found in this range.")
        return

    print(bold(
        f"{'Date':<12} | {'Time':<8} | {'Type':<20} | {'Name':<25} | "
        f"{'Duration':<8} | {'Distance':<9} | {'Elev':<6} | {'Avg HR':<6} | "
        f"{'Max HR':<6} | {'Avg Watts':<9} | {'RPE':<4} | {'TSS':<6}"
    ))
    print(gray("-" * 131))

    for act in activities:
        dur_sec = act.get('duration_sec') or 0.0
        h = int(dur_sec // 3600)
        m = int((dur_sec % 3600) // 60)
        s = int(dur_sec % 60)
        dur_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

        name_str = act.get('activity_name') or ""
        if len(name_str) > 25:
            name_str = name_str[:22] + "..."

        dist_val = act.get('distance_km')
        dist_str = f"{dist_val:.1f} km" if dist_val is not None else "0.0 km"

        elev_val = act.get('elevation_gain_m')
        elev_str = f"{elev_val:.0f} m" if elev_val is not None else "0 m"

        avg_hr = act.get('avg_hr')
        avg_hr_str = str(avg_hr) if avg_hr is not None else "N/A"

        max_hr = act.get('max_hr')
        max_hr_str = str(max_hr) if max_hr is not None else "N/A"

        watts = act.get('bike_avg_watts')
        watts_str = f"{watts} W" if watts is not None else "N/A"

        rpe_val = act.get('rpe')
        rpe_str = str(rpe_val) if rpe_val is not None else "N/A"

        tss_val = act.get('tss')
        tss_str = f"{tss_val:.1f}" if tss_val is not None else "0.0"

        start_time = act.get('start_time') or ""
        if " " in start_time:
            time_str = start_time.split(" ", 1)[1]
        elif "T" in start_time:
            time_str = start_time.split("T", 1)[1]
            if "+" in time_str:
                time_str = time_str.split("+", 1)[0]
            elif "-" in time_str:
                time_str = time_str.split("-", 1)[0]
            elif "Z" in time_str:
                time_str = time_str.split("Z", 1)[0]
        else:
            time_str = start_time or "N/A"
        if len(time_str) > 8:
            time_str = time_str[:8]

        date_col = pad_visible(act['date'], 12)
        time_col = pad_visible(time_str, 8)
        type_col = pad_visible(act['activity_type'].upper(), 20)
        name_col = pad_visible(name_str, 25)
        dur_col = pad_visible(dur_str, 8)
        dist_col = pad_visible(dist_str, 9)
        elev_col = pad_visible(elev_str, 6)
        avg_hr_col = pad_visible(avg_hr_str, 6)
        max_hr_col = pad_visible(max_hr_str, 6)
        watts_col = pad_visible(watts_str, 9)
        rpe_col = pad_visible(rpe_str, 4)
        tss_col = pad_visible(tss_str, 6)

        print(
            f"{date_col} | {time_col} | {type_col} | {name_col} | "
            f"{dur_col} | {dist_col} | {elev_col} | {avg_hr_col} | "
            f"{max_hr_col} | {watts_col} | {rpe_col} | {tss_col}"
        )

    # Summary footer
    total_count = len(activities)
    total_duration_sec = sum(act.get('duration_sec') or 0.0 for act in activities)
    total_distance_km = sum(act.get('distance_km') or 0.0 for act in activities)
    total_elevation_m = sum(act.get('elevation_gain_m') or 0.0 for act in activities)
    total_tss = sum(act.get('tss') or 0.0 for act in activities)

    tot_h = int(total_duration_sec // 3600)
    tot_m = int((total_duration_sec % 3600) // 60)
    tot_dur_str = f"{tot_h}h {tot_m}m" if tot_h > 0 else f"{tot_m}m"

    print(gray("-" * 131))
    print(bold(
        f"Summary: {total_count} activities | Duration: {tot_dur_str} | "
        f"Distance: {total_distance_km:.1f} km | Elevation: {total_elevation_m:.0f} m | "
        f"TSS: {total_tss:.1f}"
    ))


def _show_activities_csv(activities: list) -> None:
    """Output activities as CSV."""
    writer = csv_mod.writer(sys.stdout)
    writer.writerow([
        "date", "start_time", "activity_type", "activity_name",
        "duration_sec", "distance_km", "elevation_gain_m",
        "avg_hr", "max_hr", "bike_avg_watts", "rpe", "tss",
    ])
    for act in activities:
        writer.writerow([
            act.get('date'), act.get('start_time'),
            act.get('activity_type'), act.get('activity_name'),
            act.get('duration_sec'), act.get('distance_km'),
            act.get('elevation_gain_m'), act.get('avg_hr'),
            act.get('max_hr'), act.get('bike_avg_watts'),
            act.get('rpe'), act.get('tss'),
        ])


def run_data_analyze(args: argparse.Namespace) -> None:
    """Runs data analyze to reverse-engineer training cycles."""
    try:
        result = coach_service.analyze_workouts(
            from_date_str=args.from_date,
            until_date_str=args.until_date,
            days=args.days,
            weeks=args.weeks,
            context=args.context,
            force=args.force,
            inspect=args.inspect,
            no_pull=args.no_pull,
        )

        print(bold(cyan("\n=== HISTORICAL WORKOUT ANALYSIS REPORT ===")))
        
        # Macrocycle Overview
        if "inferred_macrocycle" in result:
            im = result["inferred_macrocycle"]
            print(
                f"\n{bold('Macrocycle Focus')}: {cyan(im.get('overall_focus', 'N/A'))} "
                f"({magenta(im.get('start_date', ''))} to {magenta(im.get('end_date', ''))})"
            )
        
        if "macrocycle_summary" in result:
            print(format_labeled_block(f"{bold('Summary')}:", result["macrocycle_summary"]))

        # Inferred Mesocycles
        if "inferred_mesocycles" in result and result["inferred_mesocycles"]:
            print(bold(cyan("\nDetected Mesocycle Blocks:")))
            for meso in result["inferred_mesocycles"]:
                c_tag = meso.get("estimated_consistency", "Moderate")
                if c_tag == "High":
                    c_disp = green("[High Consistency]")
                elif c_tag == "Low":
                    c_disp = red("[Low Consistency]")
                else:
                    c_disp = yellow("[Moderate Consistency]")

                print(
                    f"  - {green(meso.get('name', 'Phase'))} "
                    f"({cyan(meso.get('start_date', ''))} to {cyan(meso.get('end_date', ''))}) "
                    f"{c_disp}"
                )
                print(f"    * Detected Focus: {meso.get('focus_detected', 'N/A')}")
                print(f"    * Avg Weekly TSS: {meso.get('average_weekly_tss', 'N/A')}")

        # Physiological Insights
        if "physiological_insights" in result and result["physiological_insights"]:
            print(bold(cyan("\nPhysiological Insights:")))
            for insight in result["physiological_insights"]:
                print(f"  - {insight}")

        # Coach learnings (incremental updates applied to learnings)
        updates = result.get("learning_updates")
        if updates:
            header = (
                "Coach Observations (NOT saved — inspect mode):" if args.inspect
                else "Coach Observations (Saved to learnings):"
            )
            print(bold(cyan("\n" + header)))
            try:
                learnings_map = {l['id']: l for l in db.get_learnings()}
            except Exception:
                learnings_map = {}
            for u in updates:
                op = u.get("op")
                meta = []
                if u.get("sports"):
                    meta.append(u["sports"])
                if u.get("confidence"):
                    meta.append(u["confidence"])
                suffix = f" ({', '.join(meta)})" if meta else ""
                if op == "add":
                    print(f"  + {u.get('text', '')}{suffix}")
                elif op == "revise":
                    print(f"  ~ [{u.get('id')}] {u.get('text', '')}{suffix}")
                elif op == "reinforce":
                    learning_id = u.get("id")
                    learning_text = ""
                    if learning_id is not None and learning_id in learnings_map:
                        learning_text = learnings_map[learning_id]['text']
                    
                    tag = f"  ↑ reinforced [{learning_id}]{suffix}"
                    if learning_text:
                        print(format_labeled_block(tag, learning_text))
                    else:
                        print(tag)
                elif op == "retire":
                    print(f"  - retired [{u.get('id')}]")

        print(bold(cyan("\n==========================================")))

    except Exception as e:
        print(red(f"Error running workout analysis: {e}"))


if __name__ == "__main__":
    main()
