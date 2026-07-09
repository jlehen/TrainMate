"""Shared resolvers/formatters for the workout CLI handlers."""
import argparse
import sys
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.calendar_state import calendar_status
from trainmate.modification_state import modification_status
from trainmate.sports import canonical_sport
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    color_acwr, visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, render_table, today_str as _today_str,
    today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data, mark_adherence_from_results



def _fmt_ts(iso: Optional[str]) -> str:
    """Renders a stored UTC ISO timestamp as 'YYYY-MM-DD HH:MM' for the workout list.

    Falls back to the raw string if it isn't parseable (e.g. a date-only legacy value)."""
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso
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
        goal = next((o for o in cli.db.get_objectives(status='active') if o['id'] == goal_id), None)
        if goal is None:
            print(red(f"Active goal with ID {goal_id} not found."))
            sys.exit(1)
        return goal['target_date']

    if getattr(args, 'horizon_meso_id', None) is not None:
        meso = cli.db.get_mesocycle(args.horizon_meso_id)
        if meso is None:
            print(red(f"Mesocycle with ID {args.horizon_meso_id} not found."))
            sys.exit(1)
        return meso['end_date']

    return None  # fall back to config default in workout_generate()
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
        active_meso = cli.db.get_active_mesocycle(today_str)
        if not active_meso:
            print(red("Error: No active mesocycle found to start from."))
            sys.exit(1)
        start_date = active_meso['start_date']
    elif getattr(args, 'meso_id', None) is not None:
        meso_id = args.meso_id
        if meso_id == -1:
            active_meso = cli.db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = cli.db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        start_date = meso['start_date']
    elif getattr(args, 'until_meso_id', None) is not None:
        meso_id = args.until_meso_id
        if meso_id == -1:
            active_meso = cli.db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = cli.db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        # --until-mesocycle starts from today by default
        start_date = today_str
    elif getattr(args, 'goal_id', None) is not None:
        goal_id = args.goal_id
        if goal_id == -1:
            active_goal = cli.db.get_active_objective()
            if not active_goal:
                print(red("Error: No active goal found."))
                sys.exit(1)
            goal_id = active_goal['id']
        macro = cli.db.get_macrocycle_for_objective(goal_id)
        if not macro:
            print(red(f"Error: No plan exists for Goal ID {goal_id}."))
            sys.exit(1)
        mesos = cli.db.get_mesocycles_for_macrocycle(macro['id'])
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
        active_meso = cli.db.get_active_mesocycle(today_str)
        if not active_meso:
            print(red("Error: No active mesocycle found."))
            sys.exit(1)
        target_meso_id = active_meso['id']

    target_goal_id = None
    if getattr(args, 'goal_id', None) is not None:
        target_goal_id = args.goal_id
        if target_goal_id == -1:
            active_goal = cli.db.get_active_objective()
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

    next_goal = cli.db.get_active_objective()
    end_date = _resolve_workout_end_date(horizon_args, next_goal)

    return start_date, end_date
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
        w1 = cli.db.get_workout_by_id(args.id1)
        w2 = cli.db.get_workout_by_id(args.id2)
        if not w1:
            print(red(f"Workout with ID {args.id1} not found."))
            return None
        if not w2:
            print(red(f"Workout with ID {args.id2} not found."))
            return None
        if w1['date'] == w2['date']:
            print(yellow("Both workouts are already on the same date; nothing to swap."))
            return None
        today = _today_str()
        for w in (w1, w2):
            if w['date'] < today:
                print(red(
                    f"Cannot swap [{w['id']}] {w['title']} ({w['date']}): "
                    "it is in the past."
                ))
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
        today = _today_str()
        for d in (args.date1, args.date2):
            if d < today:
                print(red(f"Cannot swap {d}: it is in the past."))
                return None
        on_1 = cli.db.get_workouts(start_date=args.date1, end_date=args.date1)
        on_2 = cli.db.get_workouts(start_date=args.date2, end_date=args.date2)
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

    print(
        red("Specify two dates (e.g. ")
        + bold(green("'workout swap 2026-06-09 2026-06-11'"))
        + red(") or --id1 and --id2.")
    )
    return None
