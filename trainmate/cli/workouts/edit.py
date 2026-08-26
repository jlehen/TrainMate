"""Workout CLI: push / rm / restore / swap / add / wipe (deterministic edits)."""
import argparse
import sys
from datetime import datetime
from typing import Optional
from trainmate import runtime
from trainmate.calendar_state import calendar_status
from trainmate.sports import canonical_sport
from trainmate.util import (
    aside, bold, dim, green, red, yellow, cyan, gray, cmd, fmt_date, fmt_span,
    today_str as _today_str,
)
from trainmate.cli.selectors import resolve_window

from trainmate.cli.workouts._helpers import (_resolve_swap_ops,
    workout_line, warn_stale_before)


def run_workout_push(args: argparse.Namespace) -> None:
    """Synchronizes planned workouts with Google Calendar."""
    today_str = _today_str()
    force = getattr(args, 'force', False)

    start_date, end_date = resolve_window(args)

    all_workouts = runtime.db.get_workouts(
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
            print(green(
                "No new or modified workouts to sync. "
                f"Run {cmd('workout generate')} to generate a schedule, "
                "or use -f to re-push already-synced workouts."
            ))
        warn_stale_before(start_date)
        return

    aside(f"Syncing {len(to_push)} workouts to Google Calendar...")
    try:
        runtime.calendar_syncer.sync_multiple(to_push)
        print(green("Google Calendar synchronization completed."))
    except Exception as e:
        print(red(f"Error syncing to Google Calendar: {e}"))
    warn_stale_before(start_date)
def run_workout_rm(args: argparse.Namespace) -> None:
    """Cancels a planned session by appending a void revision.

    The session is not deleted: the slot now says "no session here", and everything before
    that is still in the log (DESIGN_workout_revisions.md §3). Cancelled sessions are
    excluded from listings, comparisons and the calendar push, but are still surfaced to
    the coach as a deliberate cancellation — and their Calendar event is kept, retitled
    "[Deleted]", by the reconcile the change schedules (§8)."""
    workout = runtime.db.get_workout_by_id(args.id)
    if not workout:
        print(red(f"Workout with ID {args.id} not found."))
        return

    if workout.get('removed'):
        print(yellow(f"Workout with ID {args.id} ('{workout['title']}') is already removed."))
        return

    with runtime.db.workout_change(kind="rm", summary=args.reason) as change:
        change.void(
            date=workout['date'], sport_type=workout['sport_type'], reason=args.reason
        )

    print(green(
        f"Workout with ID {args.id} ('{workout['title']}') removed successfully."
    ))
    if args.reason:
        print(f"Reason: {args.reason}")
def run_workout_restore(args: argparse.Namespace) -> None:
    """Brings a cancelled session back by appending a copy of the revision its void ended.

    A restore is a duplicate, not an un-flag: the copy gets a new, higher id and becomes
    live by the same rule as everything else (§5)."""
    workout = runtime.db.get_workout_by_id(args.id)
    if not workout:
        print(red(f"Workout with ID {args.id} not found."))
        return

    if not workout.get('removed'):
        print(yellow(f"Workout with ID {args.id} ('{workout['title']}') is not removed."))
        return

    revision = runtime.db.revision_before_live_void(args.id)
    if revision is None:
        print(red(
            f"Workout with ID {args.id} has no earlier version to restore — it was "
            "cancelled before it was ever scheduled."
        ))
        return

    with runtime.db.workout_change(kind="restore") as change:
        change.restore(revision)

    print(green(f"Workout with ID {args.id} restored successfully."))
def run_workout_swap(args: argparse.Namespace) -> None:
    """Exchanges workouts between two dates or two IDs, with recovery validation."""
    ops = _resolve_swap_ops(args)
    if not ops:
        return

    warnings = runtime.coach_service.workout_swap_validate(ops)
    if warnings:
        print(bold(yellow("\nSwap warnings:")))
        for msg in warnings:
            print(yellow(f"  - {msg}"))
        if not args.force:
            if not runtime.prompt.confirm("Proceed with the swap anyway?"):
                print("\nSwap cancelled.")
                return

    updated = runtime.coach_service.workout_swap_apply(ops, args.no_sync, reason=args.reason)
    print()
    for w in updated:
        print(workout_line(w))
    print(green(f"Swapped {len(updated)} workout(s) successfully."))
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
        to_replace = runtime.db.get_workouts(start_date=args.date, end_date=args.date)
    else:
        same = runtime.db.get_workout(args.date, args.sport_type)
        to_replace = [same] if same else []
    for w in to_replace:
        print(yellow(
            f"Replacing existing {w['sport_type']} workout on {fmt_date(args.date)}: "
            f"{w['title']}"
        ))

    saved, replaced = runtime.coach_service.workout_add(
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

    print(workout_line(saved))
    verb = "replaced" if replaced else "added"
    print(green(f"Workout {verb} successfully and synced to Calendar."))
def run_workout_wipe(args: argparse.Namespace) -> None:
    """Wipes all workouts from the database and Google Calendar after confirmation."""
    if not args.yes:
        if not runtime.prompt.confirm(
            "Are you sure you want to wipe all workouts "
            "(including Google Calendar events)?", danger=True
        ):
            print("Wipe cancelled.")
            return

    workouts = runtime.db.get_workouts(include_removed=True)
    synced_workouts = [w for w in workouts if w.get('google_event_id')]
    if synced_workouts:
        aside(f"Deleting {len(synced_workouts)} events from Google Calendar...")
        for w in synced_workouts:
            ge_id = w['google_event_id']
            if ge_id:
                runtime.calendar_syncer.delete_workout_event(ge_id)

    runtime.db.wipe_workouts()
    print(green("All workouts wiped successfully."))


def _event_day(event: dict) -> Optional[str]:
    """The day an event sits on. Workout events are all-day (`start.date`); a timed
    start is tolerated in case one was hand-edited in Google Calendar."""
    start = event.get('start') or {}
    return start.get('date') or (start.get('dateTime') or "")[:10] or None


def run_workout_prune_calendar(args: argparse.Namespace) -> None:
    """Deletes workout events on the calendar that no local workout row references.

    Ownership is read from the calendar side (the `source=TrainMate` tag), because the
    orphans this cleans up are exactly the ones the database can no longer name — a
    fresh DB, a restored backup, or a wipe that never reached Calendar.
    """
    start_date, end_date = resolve_window(args)

    try:
        events = runtime.calendar_syncer.list_workout_events()
    except Exception as e:
        print(red(f"Error reading Google Calendar: {e}"))
        sys.exit(1)

    # Every id any row still claims, removed rows included: a soft-removed workout
    # keeps its "[Deleted]" event on purpose, and the window must not orphan it.
    # Read *after* the calendar, so a workout pushed mid-command lands in `known`
    # rather than in a stale event list — the race then errs towards keeping.
    known = {
        w['google_event_id']
        for w in runtime.db.get_workouts(include_removed=True)
        if w.get('google_event_id')
    }

    orphans = []
    for event in events:
        if event.get('id') in known:
            continue
        day = _event_day(event)
        if start_date and (day is None or day < start_date):
            continue
        if end_date and (day is None or day > end_date):
            continue
        orphans.append((day or "?", event))
    orphans.sort(key=lambda pair: pair[0])

    if start_date and end_date:
        window = f" dated {fmt_span(start_date, end_date)}"
    elif start_date:
        window = f" dated {fmt_date(start_date)} onward"
    elif end_date:
        window = f" dated up to {fmt_date(end_date)}"
    else:
        window = ""

    if not orphans:
        print(green(
            f"No orphaned Calendar events{window}. "
            f"{len(events)} workout event(s) all match a local workout."
        ))
        return

    for day, event in orphans:
        print(f"{cyan(fmt_date(day))}  {event.get('summary') or dim('(no title)')}")
    print(dim(
        f"{len(orphans)} of {len(events)} workout event(s) on the calendar "
        f"match no local workout{window}."
    ))

    if getattr(args, 'dry_run', False):
        print(yellow(f"Dry run: nothing deleted. Re-run without --dry-run to prune."))
        return

    if not args.yes:
        if not runtime.prompt.confirm(
            f"Delete these {len(orphans)} Google Calendar event(s)?", danger=True
        ):
            print("Prune cancelled.")
            return

    deleted = sum(
        1 for _, event in orphans if runtime.calendar_syncer.delete_event(event['id'])
    )
    print(green(f"Pruned {deleted} orphaned Calendar event{'s' if deleted != 1 else ''}."))
    if deleted != len(orphans):
        print(yellow(f"{len(orphans) - deleted} event(s) could not be deleted (see above)."))
