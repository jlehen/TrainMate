"""Workout CLI: push / rm / restore / swap / add / wipe (deterministic edits)."""
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
    visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, render_table, today_str as _today_str,
    today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data, mark_adherence_from_results

from trainmate.cli.workouts._helpers import _resolve_workout_date_range, _resolve_swap_ops


def run_workout_push(args: argparse.Namespace) -> None:
    """Synchronizes planned workouts with Google Calendar."""
    today_str = _today_str()
    force = getattr(args, 'force', False)

    start_date, end_date = _resolve_workout_date_range(args)
    # Default to today onwards when no date filter is given
    if start_date is None:
        start_date = today_str

    all_workouts = cli.db.get_workouts(
        start_date=start_date,
        end_date=end_date,
        sport_type=getattr(args, 'sport_type', None),
        include_removed=True,
    )

    to_push = []
    for w in all_workouts:
        is_fresh = calendar_status(w) == 'synced'
        if not w.get('removed'):
            if force or not is_fresh:
                to_push.append(w)
        else:
            # A removed workout only needs pushing if it has an event to update.
            if w.get('google_event_id') and (force or not is_fresh):
                to_push.append(w)

    if not to_push:
        if force:
            print("No workouts found in the specified range.")
        else:
            print(
                f"No new or modified workouts to sync. "
                f"Run '{green('workout generate')}' to generate a schedule, "
                f"or use -f to re-push already-synced workouts."
            )
        return

    print(f"Syncing {len(to_push)} workouts to Google Calendar...")
    try:
        cli.calendar_syncer.sync_multiple(to_push)
        print(green("Google Calendar synchronization completed."))
    except Exception as e:
        print(red(f"Error syncing to Google Calendar: {e}"))
def run_workout_rm(args: argparse.Namespace) -> None:
    """Soft-removes a planned workout: marks it removed (kept in the DB) and updates its
    Calendar event to be marked as deleted. Removed workouts are excluded from listings,
    comparisons, and the calendar push, but are still surfaced to the coach as a deliberate
    cancellation."""
    workout = cli.db.get_workout_by_id(args.id)
    if not workout:
        print(red(f"Workout with ID {args.id} not found."))
        return

    if workout.get('removed'):
        print(yellow(f"Workout with ID {args.id} ('{workout['title']}') is already removed."))
        return

    cli.db.mark_workout_removed(args.id, reason=args.reason)

    if workout.get('google_event_id'):
        print("Workout is synced to Google Calendar. Updating calendar event...")
        updated_workout = cli.db.get_workout_by_id(args.id)
        if updated_workout is not None:
            try:
                cli.calendar_syncer.sync_workout(updated_workout)
            except Exception as e:
                print(red(f"Error updating Google Calendar event: {e}"))

    print(green(
        f"Workout with ID {args.id} ('{workout['title']}') removed successfully."
    ))
    if args.reason:
        print(f"Reason: {args.reason}")
def run_workout_restore(args: argparse.Namespace) -> None:
    """Restores a soft-removed workout and updates its Calendar event to remove the
    deleted mark."""
    workout = cli.db.get_workout_by_id(args.id)
    if not workout:
        print(red(f"Workout with ID {args.id} not found."))
        return

    if not workout.get('removed'):
        print(yellow(f"Workout with ID {args.id} ('{workout['title']}') is not removed."))
        return

    cli.db.restore_workout(args.id)

    if workout.get('google_event_id'):
        print("Workout is synced to Google Calendar. Updating calendar event...")
        updated_workout = cli.db.get_workout_by_id(args.id)
        if updated_workout is not None:
            try:
                cli.calendar_syncer.sync_workout(updated_workout)
            except Exception as e:
                print(red(f"Warning: Failed to update Google Calendar: {e}"))
                print(yellow("The workout was restored locally but might still appear deleted on your calendar."))
                return

    print(green(f"Workout with ID {args.id} restored successfully."))
def run_workout_swap(args: argparse.Namespace) -> None:
    """Exchanges workouts between two dates or two IDs, with recovery validation."""
    ops = _resolve_swap_ops(args)
    if not ops:
        return

    warnings = cli.coach_service.workout_swap_validate(ops)
    if warnings:
        print(bold(yellow("\nSwap warnings:")))
        for msg in warnings:
            print(yellow(f"  - {msg}"))
        if not args.force:
            if not cli.prompt.confirm("Proceed with the swap anyway?"):
                print("\nSwap cancelled.")
                return

    updated = cli.coach_service.workout_swap_apply(ops, args.no_sync, reason=args.reason)
    print(green(f"\nSwapped {len(updated)} workout(s) successfully."))
    for w in updated:
        print(f"  [{w['id']}] {w['title']} -> {w['date']}")
    if args.reason:
        print(f"Reason: {args.reason}")
    if args.no_sync:
        print(gray("Calendar sync skipped (--no-sync)."))
def run_workout_add(args: argparse.Namespace) -> None:
    """Manually schedules a workout on a date, replacing any same-sport session."""
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(red(f"Invalid date format: '{args.date}'. Use YYYY-MM-DD."))
        sys.exit(1)

    # Normalize to the coach's canonical sport vocabulary so the stored session and the
    # "Replacing existing ..." preview both match what generate/adapt will look up.
    args.sport_type = canonical_sport(args.sport_type)

    if args.replace_day:
        to_replace = cli.db.get_workouts(start_date=args.date, end_date=args.date)
    else:
        same = cli.db.get_workout(args.date, args.sport_type)
        to_replace = [same] if same else []
    for w in to_replace:
        print(yellow(
            f"Replacing existing {w['sport_type']} workout on {args.date}: "
            f"{w['title']}"
        ))

    saved, replaced = cli.coach_service.workout_add(
        date=args.date,
        sport_type=args.sport_type,
        title=args.title,
        description=args.description or "",
        duration_minutes=args.duration,
        rpe=args.rpe,
        tss=args.tss,
        reason=args.reason,
        replace_day=args.replace_day,
    )

    if not saved:
        print(red("Failed to save workout."))
        sys.exit(1)

    stat_parts = []
    if saved.get('duration_minutes') is not None:
        stat_parts.append(f"{saved['duration_minutes']}m")
    if saved.get('tss') is not None:
        stat_parts.append(f"TSS {saved['tss']}")
    if saved.get('rpe') is not None:
        stat_parts.append(f"RPE {saved['rpe']}")
    stats = f" ({', '.join(stat_parts)})" if stat_parts else ""
    verb = "Replaced with" if replaced else "Added"
    print(green(
        f"{verb} [{saved['id']}] {saved['title']}{stats} on {saved['date']} "
        f"({saved['sport_type']}) and synced to Calendar."
    ))
def run_workout_wipe(args: argparse.Namespace) -> None:
    """Wipes all workouts from the database and Google Calendar after confirmation."""
    if not args.yes:
        if not cli.prompt.confirm(
            "Are you sure you want to wipe all workouts "
            "(including Google Calendar events)?", danger=True
        ):
            print("Wipe cancelled.")
            return

    workouts = cli.db.get_workouts()
    synced_workouts = [w for w in workouts if w.get('google_event_id')]
    if synced_workouts:
        print(f"Deleting {len(synced_workouts)} events from Google Calendar...")
        for w in synced_workouts:
            ge_id = w['google_event_id']
            if ge_id:
                cli.calendar_syncer.delete_workout_event(ge_id)

    cli.db.wipe_workouts()
    print(green("All workouts wiped successfully."))
