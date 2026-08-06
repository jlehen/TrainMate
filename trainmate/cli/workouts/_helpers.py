"""Shared resolvers/formatters for the workout CLI handlers."""
import argparse
import re
import sys
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.calendar_state import calendar_status
from trainmate.modification_state import modification_status
from trainmate.util import (
    bold, green, red, yellow, cyan, blue, magenta, cmd, today_str as _today_str,
)
from trainmate.cli.common import fmt_date
from trainmate.cli.selectors import resolve_window



def _fmt_ts(iso: Optional[str]) -> str:
    """Renders a stored UTC ISO timestamp as 'YYYY-MM-DD HH:MM' for the workout list.

    Falls back to the raw string if it isn't parseable (e.g. a date-only legacy value)."""
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso
def workout_line(w: dict) -> str:
    """One-line rendering of a workout for `list` (and the `add` echo)."""
    mod_marker = ""
    mod_status = modification_status(w)
    if mod_status == 'adapted':
        # Surface repeat easings: a session adapted by more than one adapt run
        # reads [ADAPTED ×N], flagging load that has been walked down multiple times.
        count = w.get('adaptation_count') or 0
        label = f" [ADAPTED ×{count}]" if count > 1 else " [ADAPTED]"
        mod_marker = bold(yellow(label))
    elif mod_status == 'swapped':
        mod_marker = bold(yellow(" [SWAPPED]"))
    elif mod_status == 'replaced':
        mod_marker = bold(yellow(" [REPLACED]"))
    sync_marker = ""
    status = calendar_status(w)
    if status == 'synced':
        sync_marker = bold(green(" [SYNCED]"))
    elif status == 'stale':
        sync_marker = bold(yellow(" [STALE]"))
    rem_marker = ""
    if w.get('removed'):
        rem_marker = bold(red(" [REMOVED]"))
    src_marker = ""
    if w.get('source') == 'manual':
        src_marker = bold(magenta(" [MANUAL]"))
    # Benchmark identity is a stored column, orthogonal to the modification/sync/removed
    # axes (a benchmark can also be swapped), so it gets its own marker straight off the
    # column (DESIGN_benchmark_workouts.md §3.1/§6).
    bench_marker = bold(blue(" [BENCHMARK]")) if w.get('benchmark_type') else ""
    duration = w.get('duration_minutes')
    tss = w.get('tss')
    rpe = w.get('rpe')
    duration_str = f" | {duration}min" if duration else ""
    tss_str = f" | TSS {tss}" if tss is not None else ""
    rpe_str = f" | RPE {rpe}" if rpe is not None else ""
    return (
        f"ID: {w['id']} | {cyan(fmt_date(w['date']))} | {magenta(w['sport_type'].upper())} | "
        f"{bold(w['title'])}{bench_marker}{mod_marker}{sync_marker}{rem_marker}"
        f"{src_marker}{duration_str}{tss_str}{rpe_str}"
    )
def warn_stale_before(start_date: str) -> None:
    """Flags workouts left `stale` on days earlier than the window just pushed.

    `workout push` defaults to today onward, so a row that went stale in the past —
    realistically a push that failed while offline — has nothing that would ever
    re-push it. Freshness is derived, not stored (see trainmate.calendar_state), so
    the marker is durable; this just makes it visible outside the pushed range."""
    try:
        cutoff = (
            datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=1)
        ).strftime("%Y-%m-%d")
    except ValueError:
        return
    earlier = cli.db.get_workouts(end_date=cutoff, include_removed=True)
    stale = [w for w in earlier if calendar_status(w) == 'stale']
    if not stale:
        return
    label = "workout" if len(stale) == 1 else "workouts"
    earliest = min(w['date'] for w in stale)
    print(yellow(
        f"Note: {len(stale)} {label} before {start_date} still read [STALE] — their "
        f"calendar events are out of date and this push did not cover them. "
        f"Run {cmd(f'workout push -d {earliest}..')} to update them."
    ))
def _resolve_workout_end_date(
    args: argparse.Namespace, resolved_goal: dict | None
) -> str | None:
    """The horizon for `workout generate`: a goal's target date, or the END of whatever the
    selectors cover. Generation always starts today, so a selector's start is not used."""
    if getattr(args, 'horizon_goal_id', None) is not None:
        goal_id = args.horizon_goal_id
        if goal_id == -1:
            # Sentinel: use the already-resolved goal for this generate run
            if resolved_goal is None:
                print(red("No active goal found for --until-goal."))
                sys.exit(1)
            return resolved_goal['target_date']
        goal = next((o for o in cli.db.upcoming_objectives() if o['id'] == goal_id), None)
        if goal is None:
            print(red(f"Active goal with ID {goal_id} not found."))
            sys.exit(1)
        return goal['target_date']

    return resolve_window(args)[1]  # None falls back to the config default span


def _classify_swap_target(value: str) -> str | None:
    """Classifies a swap positional as 'date' (YYYY-MM-DD) or 'id' (bare integer)."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "date"
    if value.isdigit():
        return "id"
    return None


def _resolve_swap_ops(args: argparse.Namespace) -> list | None:
    """Turns CLI args into swap operations, or returns None on a usage/lookup error."""
    kind1 = _classify_swap_target(args.target1)
    kind2 = _classify_swap_target(args.target2)
    for target, kind in ((args.target1, kind1), (args.target2, kind2)):
        if kind is None:
            print(red(
                f"'{target}' is neither a date (YYYY-MM-DD) nor a workout ID. Swap "
                "two dates (e.g. "
                + cmd("workout swap 2026-06-09 2026-06-11 'travelling'")
                + ") or two workout IDs (e.g. "
                + cmd("workout swap 5 8 'travelling'") + ")."
            ))
            return None
    if kind1 != kind2:
        print(red("Swap two dates or two workout IDs, not one of each."))
        return None

    if kind1 == "id":
        id1, id2 = int(args.target1), int(args.target2)
        w1 = cli.db.get_workout_by_id(id1)
        w2 = cli.db.get_workout_by_id(id2)
        if not w1:
            print(red(f"Workout with ID {id1} not found."))
            return None
        if not w2:
            print(red(f"Workout with ID {id2} not found."))
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

    date1, date2 = args.target1, args.target2
    for d in (date1, date2):
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            print(red(f"Invalid date format: '{d}'. Use YYYY-MM-DD."))
            return None
    if date1 == date2:
        print(yellow("The two dates are identical; nothing to swap."))
        return None
    today = _today_str()
    for d in (date1, date2):
        if d < today:
            print(red(f"Cannot swap {d}: it is in the past."))
            return None
    on_1 = cli.db.get_workouts(start_date=date1, end_date=date1)
    on_2 = cli.db.get_workouts(start_date=date2, end_date=date2)
    if not on_1 and not on_2:
        print(yellow(
            f"No workouts on either {date1} or {date2}; nothing to swap."
        ))
        return None
    desc_1 = ", ".join(w['title'] for w in on_1) or "(rest)"
    desc_2 = ", ".join(w['title'] for w in on_2) or "(rest)"
    print(f"Swapping {date1} [{desc_1}] <-> {date2} [{desc_2}]")
    return (
        [{'id': w['id'], 'new_date': date2} for w in on_1]
        + [{'id': w['id'], 'new_date': date1} for w in on_2]
    )
