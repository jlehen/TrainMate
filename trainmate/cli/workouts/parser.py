"""Argparse wiring for the `workout` command group.

Each sub-parser binds its handler with set_defaults(func=...), so the flags and the
function that reads them are defined together.
"""
from trainmate.config import config
from trainmate.util import green
from trainmate.cli.selectors import add_selector_args, add_single_date_arg, parse_target
from trainmate.cli.workouts.edit import (
    run_workout_add, run_workout_prune_calendar, run_workout_push,
    run_workout_restore, run_workout_rm, run_workout_swap, run_workout_wipe,
)
from trainmate.cli.workouts.generate import (
    run_workout_adapt, run_workout_batches, run_workout_compare,
    run_workout_generate, run_workout_list, run_workout_rollback, run_workout_show,
)


def _add_listing_args(parser):
    """The targets, selectors and filters `workout list` and `workout show` share: one
    listing, and only the per-workout detail block differs (`show` always prints it)."""
    parser.add_argument(
        "targets", nargs="*", metavar="TARGET", type=parse_target,
        help="Workout IDs and/or date selectors to show (e.g. '12 15', '2026-06-01..')"
    )
    add_selector_args(
        parser, meso=True, macro=True, goal=True, sport=True,
        direction="forward", default="7d",
    )
    parser.add_argument(
        "--removed", action="store_true",
        help="Include soft-removed workouts (e.g. to find their ID for restoring)"
    )
    parser.add_argument(
        "--link", "-l", action="store_true",
        help="Show each synced workout's Google Calendar event link"
    )


def add_workout_parser(subparsers, pull_bypass_parser, llm_debug_parser):
    # workout command & subparsers
    workout_parser = subparsers.add_parser(
        "workout",
        help="Manage workouts (microcycles)"
    )
    workout_subparsers = workout_parser.add_subparsers(
        dest="subcommand", help="Workout sub-commands"
    )

    # workout list
    w_list = workout_subparsers.add_parser(
        "list",
        parents=[pull_bypass_parser],
        help="Show all planned workouts",
        description=(
            "List planned workouts chronologically. With no filter at all, shows a 7-day "
            "window from today; with only --type, shows today onward. Name workout IDs or "
            "dates as arguments to show just those (handy with -v). Every listed session "
            "dated today or earlier also carries what became of it — "
            "[DONE]/[PARTIAL]/[MISSED]/[REST OK]/[REST BROKEN], or [NOT YET] for one still "
            "ahead of you today — and -v names the effort it matched and what a [PARTIAL] "
            "differed by. Garmin data is freshened over that past span first unless "
            "--no-pull is given; a listing entirely in the future never pulls."
        )
    )
    w_list.set_defaults(func=run_workout_list)
    _add_listing_args(w_list)
    w_list.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show full detail per workout (description, lifecycle, adapt notes) "
             "instead of one line each"
    )

    # workout show
    w_show = workout_subparsers.add_parser(
        "show",
        parents=[pull_bypass_parser],
        help="Show named workouts in full detail",
        description=(
            "Show workouts with the full detail block: the description, when the session "
            "was planned and last adapted, the effort a past session was graded against, "
            f"and any adapt notes. Same output as '{green('workout list')} -v', under a "
            "name that says what it does. Name workout IDs or dates as arguments "
            f"('{green('workout show')} 12'), and every filter '{green('workout list')}' "
            "takes works here too. With no argument at all, it details the same 7-day "
            "window that command lists."
        )
    )
    w_show.set_defaults(func=run_workout_show)
    _add_listing_args(w_show)

    # workout compare
    w_cmp = workout_subparsers.add_parser(
        "compare",
        parents=[pull_bypass_parser],
        help="Compare planned workouts against completed activities",
        description=(
            "Compare planned workouts against completed Garmin activities, flagging "
            "missed sessions, rest-day violations, and unplanned high-load efforts. "
            "With no date filter, looks back 14 days; here a bare span like -d 7d looks "
            "backward (not forward) and the end date is always capped at today. "
            "Freshens Garmin data for the range first unless --no-pull is given. "
            "Each past event's Calendar entry is stamped with the adherence verdict "
            "(a [Done]/[Missed]/[Partial]/... title tag and an 'Adherence' description "
            "header) unless --no-mark is given."
        )
    )
    w_cmp.set_defaults(func=run_workout_compare)
    add_selector_args(
        w_cmp, meso=True, macro=True, goal=True, sport=True,
        direction="backward", default="14d", span_days=14,
    )
    w_cmp.add_argument(
        "--no-mark", action="store_true", dest="no_mark",
        help=(
            "Skip writing the adherence verdict back to each past workout's Google "
            "Calendar event (title tag + description header)."
        )
    )

    # workout generate
    p_w_gen = workout_subparsers.add_parser(
        "generate",
        parents=[pull_bypass_parser, llm_debug_parser],
        help="Generate workouts (microcycles) based on the active strategy",
        description=(
            "Generate workouts (microcycles), driven by the periodization blocks covering "
            "the days being generated — which plan applies is read off the dates, so no "
            "goal has to be named. -d/-m/-M/-g pick the whole span to write, both ends of "
            "it: '-m 5' is block 5 from its first day to its last, '-g' is a goal's whole "
            "plan, '-d 4w' is the next four weeks. A span never opens before today. With "
            "no selector, generates config.workout_generation_span_days days from today "
            f"(28 by default). The proposed sessions are listed as '{green('workout list')}' "
            "shows them and nothing is written until you accept; on a yes the new sessions "
            "are appended, days inside the span the plan no longer holds are cancelled, "
            "sessions outside the span are left exactly as they are, and Google Calendar "
            f"is brought into line — undoable with '{green('workout rollback')}', or "
            f"'{green('plan rollback')}' to step the strategy back with it. This is a full "
            "rebuild of the span, not a fill-in: when it already holds sessions it also "
            "asks before spending the LLM call (-f skips both prompts)."
        )
    )
    p_w_gen.set_defaults(func=run_workout_generate)
    p_w_gen.add_argument(
        "-f", "--force", "-y", "--yes", action="store_true", dest="force",
        help="Skip the confirmation prompts (spending the LLM call, applying the "
             "proposed workouts, and the out-of-date-plan warning)"
    )
    p_w_gen.add_argument(
        "-v", "--verbose", action="store_true",
        help="Name each Calendar event as it is deleted and created, instead of the "
             "progress bar"
    )
    # The selectors name the span to write, both ends of it, and are grouped because a
    # span is one choice, not several (DESIGN_cli_selectors.md §8). `-M` doubles as the
    # tiebreaker when two plans cover the same days.
    p_w_gen_span = p_w_gen.add_mutually_exclusive_group()
    add_selector_args(
        p_w_gen, meso=True, macro=True, goal=True, direction="forward", default=None,
        group=p_w_gen_span, generates=True,
    )


    # workout rollback
    w_rollback = workout_subparsers.add_parser(
        "rollback", aliases=["rb"],
        help="Undo a workout change, and every change made after it",
        description=(
            "Put the sessions back the way they were the moment before a change ran. "
            "Any command that wrote workouts qualifies — a generation, an adapt, a swap, "
            "a manual edit — and undoing one also undoes everything after it, which is "
            "what stops a session ending up live on two days. Defaults to the newest "
            f"change; list them with '{green('workout batches')}' and pick one with "
            "--batch. Sessions dated before today are left alone. The active "
            f"periodization plan is left untouched — use '{green('plan rollback')}' to "
            f"step the strategy back as well. This is unrelated to "
            f"'{green('workout restore')}', which un-cancels a single session."
        )
    )
    w_rollback.set_defaults(func=run_workout_rollback)
    w_rollback.add_argument(
        "--batch", type=int, metavar="N",
        help="Which change to undo, as numbered by 'workout batches' "
             "(1 = the newest, the default)"
    )
    w_rollback.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )
    w_rollback.add_argument(
        "-v", "--verbose", action="store_true",
        help="Name each Calendar event as it is deleted and created, instead of the "
             "progress bar"
    )

    # workout batches
    _batches_parser = workout_subparsers.add_parser(
        "batches",
        help="List the workout changes that 'workout rollback' can undo",
        description=(
            "List every command that wrote workouts, newest first: when it ran, what "
            "kind of change it was, how many revisions it appended and over what dates. "
            f"'{green('workout rollback --batch N')}' undoes one, and everything after "
            "it. A pass that looked at the plan and changed nothing — an adapt that held "
            "— is listed too, marked '(held)'. Every entry is undoable, #1 included: it "
            "is the change that wrote the plan you are on now."
        )
    )
    _batches_parser.set_defaults(func=run_workout_batches)

    # workout add
    w_add = workout_subparsers.add_parser(
        "add",
        help="Manually schedule a workout on a date (replaces any same-sport session)",
        description=(
            "Manually add a workout on a specific date — driven by you rather than the "
            "coach. If a workout of the same sport already exists that day it is "
            "replaced (use --replace-day to instead replace every session that day "
            "regardless of sport), and each replaced session's description, duration, "
            "TSS and RPE are recorded on the new workout (and its Calendar event) so the "
            "change stays traceable. Saved and synced to Google Calendar immediately. To "
            f"have the coach re-balance surrounding load afterward, run '{green('workout adapt')}'."
        )
    )
    w_add.set_defaults(func=run_workout_add)
    w_add.add_argument("date", help="Workout date (YYYY-MM-DD)")
    w_add.add_argument("sport_type", help="Sport type (e.g. running, cycling)")
    w_add.add_argument("title", help="Workout title")
    w_add.add_argument(
        "--description", "--desc", dest="description", help="Workout description / details"
    )
    w_add.add_argument(
        "--duration", type=int, dest="duration", metavar="MIN",
        help="Planned duration in minutes"
    )
    w_add.add_argument("--rpe", type=int, help="Target RPE (1-10)")
    w_add.add_argument("--tss", type=int, help="Target training stress score")
    w_add.add_argument(
        "--reason",
        help="Why you're adding/replacing this session (recorded and shown to the coach)"
    )
    w_add.add_argument(
        "--replace-day", dest="replace_day", action="store_true",
        help="Replace every session that day, not just the same sport"
    )

    # workout rm
    w_rm = workout_subparsers.add_parser(
        "rm", help="Remove a workout by ID"
    )
    w_rm.set_defaults(func=run_workout_rm)
    w_rm.add_argument("id", type=int, help="Workout ID to remove")
    w_rm.add_argument(
        "reason",
        help="Why the workout is being removed (shown to the coach as a deliberate "
             "cancellation)"
    )

    # workout restore
    w_restore = workout_subparsers.add_parser(
        "restore", help="Bring a cancelled workout back by ID",
        description=(
            "Un-cancel a single session and put it back on the schedule, as it stood "
            f"before it was removed. This is unrelated to '{green('workout rollback')}', "
            "which undoes a whole change (DESIGN_workout_revisions.md §10)."
        )
    )
    w_restore.set_defaults(func=run_workout_restore)
    w_restore.add_argument("id", type=int, help="Workout ID to restore")
    
    # workout adapt
    w_adapt = workout_subparsers.add_parser(
        "adapt", aliases=["a"],
        parents=[pull_bypass_parser, llm_debug_parser],
        help="Run the daily Garmin check for today (syncs adapted workouts to Calendar)",
        description=(
            "Run the daily adaptation check: read recent recovery metrics and let the "
            "coach adjust upcoming workouts. Defaults to today (UTC); use --date for "
            "another day. Proposed changes are confirmed interactively unless -y/--auto "
            "is given, then synced to Google Calendar."
        )
    )
    w_adapt.set_defaults(func=run_workout_adapt)
    add_single_date_arg(
        w_adapt,
        "Day to adapt: YYYY-MM-DD, 'today' (the default) or an offset like -1d"
    )
    w_adapt.add_argument(
        "--lookback", type=int, metavar="DAYS",
        help="Days of recovery-metrics trajectory to summarize (default: config metrics_lookback_days)"
    )
    w_adapt.add_argument(
        "-m", "--message", dest="message",
        help=(
            "Ad-hoc, one-off signal to the coach for THIS adaptation run only (e.g. "
            "'knee is sore, keep impact low', 'no bike access Thursday'). Advisory: it "
            "won't override clear fatigue signals. The note itself isn't stored, but if "
            "it drives a session change its cause is recorded in that session's reason so "
            "a later run understands the tactical change; it stays a one-off and never "
            "becomes durable block evidence. For persistent signals (alcohol, sleep, "
            "stress) use 'signal add' instead."
        )
    )
    w_adapt.add_argument(
        "-y", "--yes", "--auto", action="store_true", dest="auto",
        help="Apply proposed adaptations automatically without prompting"
    )
    
    # workout push
    w_push = workout_subparsers.add_parser(
        "push", aliases=["p"], advanced=True,
        help="Commit local planned workouts to Google Calendar",
        description=(
            "Push planned workouts to Google Calendar. With no date filter, pushes "
            "from today onward. By default only new or modified (unsynced) workouts "
            "are sent; use -f/--force to re-push already-synced workouts, overwriting "
            "their calendar entries."
        )
    )
    w_push.set_defaults(func=run_workout_push)
    add_selector_args(
        w_push, meso=True, macro=True, goal=True, sport=True, direction="forward",
        default="today..",
    )
    w_push.add_argument(
        "-f", "--force", action="store_true",
        help="Re-push already-synced workouts, overwriting existing calendar entries"
    )

    # workout swap
    w_swap = workout_subparsers.add_parser(
        "swap",
        parents=[llm_debug_parser],
        help="Swap two workouts, given either two dates or two workout IDs",
        description=(
            "Swap two workouts, given either two dates (YYYY-MM-DD) or two workout "
            "IDs. Both targets must be the same kind - two dates or two IDs, not a "
            "mix. Runs recovery checks (consecutive hard days, weekly load spikes, "
            "mesocycle crossings) and prompts on warnings unless -f/--force. "
            "The swap is synced to Google Calendar unless --no-sync is given."
        )
    )
    w_swap.set_defaults(func=run_workout_swap)
    w_swap.add_argument(
        "target1",
        help="First workout to swap: a date (YYYY-MM-DD) or a workout ID"
    )
    w_swap.add_argument(
        "target2",
        help="Second workout to swap: a date (YYYY-MM-DD) or a workout ID"
    )
    w_swap.add_argument(
        "reason",
        help="Why the workouts are being swapped (recorded and shown to the coach)"
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
        "wipe", advanced=True,
        help="Wipe all workouts from the database and Google Calendar"
    )
    w_wipe.set_defaults(func=run_workout_wipe)
    w_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # workout prune-calendar
    w_prune = workout_subparsers.add_parser(
        "prune-calendar", advanced=True,
        help="Delete Google Calendar workout events no local workout references",
        description=(
            "Sweep the Google Calendar for TrainMate workout events that no workout in "
            "the database points at, and delete them. These orphans are what a fresh "
            "database, a restored backup, or a wipe that never reached Calendar leaves "
            "behind. Events belonging to cancelled workouts are kept (the session still "
            "claims them). With no date filter the whole calendar is swept; -d restricts "
            "it to a window, as on 'data wipe'. Use --dry-run to preview."
        )
    )
    w_prune.set_defaults(func=run_workout_prune_calendar)
    add_selector_args(w_prune, direction="none")
    w_prune.add_argument(
        "-n", "--dry-run", action="store_true", dest="dry_run",
        help="List the orphaned events without deleting anything"
    )
    w_prune.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    return workout_parser
