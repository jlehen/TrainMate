"""Shared resolvers/formatters for the workout CLI handlers."""
import argparse
import re
from datetime import datetime, timedelta
from typing import Optional
from trainmate import runtime
from trainmate.calendar_state import calendar_status
from trainmate.util import (
    bold, gray, green, red, yellow, cyan, blue, magenta, cmd, fmt_date, fmt_span,
    today_str as _today_str, notice,
)


# The change kind of a session's live revision, as the athlete reads it
# (DESIGN_workout_revisions.md §7). A `generate` is the plan saying what it says, so it
# gets no marker, and a void carries [REMOVED] instead.
_KIND_MARKERS = {
    'adapt': 'ADAPTED',
    'swap': 'SWAPPED',
    'add': 'REPLACED',
}


def modification_markers(w: dict) -> list:
    """What has happened to a session: the kind of its latest change, plus the easings
    that still stand.

    Two facts rather than one, which is why there is no precedence rule any more: a
    session eased twice and then swapped reads `[SWAPPED, ADAPTED ×2]` (§12)."""
    kind = w.get('change_kind')
    count = w.get('adaptation_count') or 0
    eased = f"ADAPTED ×{count}" if count > 1 else ("ADAPTED" if count else "")
    if kind == 'adapt':
        # The tally is the whole story here; an adapt that eased nothing still reads
        # [ADAPTED], because the prescription did change.
        return [eased or "ADAPTED"]
    return [m for m in (_KIND_MARKERS.get(kind), eased) if m]


# How each `adherence.classify_adherence` status is coloured. The word itself rides on
# the verdict, stamped from the shared STATUS_LABELS (ARCHITECTURE.md §5).
_ADHERENCE_COLORS = {
    'done': green,
    'partial': yellow,
    'missed': red,
    'pending': gray,
    'rest_ok': green,
    'rest_violation': red,
}


def adherence_marker(adherence: Optional[dict]) -> str:
    """The `[DONE]`/`[MISSED]`/`[PARTIAL]`/… marker for a session already behind us.

    Empty for anything with no verdict — a future session, a proposal, a removed row —
    which is why the listing can carry it unconditionally."""
    label = (adherence or {}).get('label')
    if not label:
        return ""
    color = _ADHERENCE_COLORS.get(adherence.get('status'), yellow)
    return bold(color(f" [{label.upper()}]"))


def workout_line(w: dict, adherence: Optional[dict] = None) -> str:
    """One-line rendering of a workout for `list` (and the `add` echo).

    Also renders a *proposed* session — a `workout generate` preview, which has no row and
    so no ID — so the plan being accepted reads exactly like the plan `list` will show.

    `adherence` is `cli.common.adherence_verdicts`' entry for this session when the day is
    behind us; None leaves the line exactly as it was."""
    markers = modification_markers(w)
    mod_marker = bold(yellow(f" [{', '.join(markers)}]")) if markers else ""
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
    # Only a `workout generate` proposal carries this: the day is being left alone rather
    # than rewritten, which the rest of the line cannot show
    # (DESIGN_workout_revisions.md §7.1).
    keep_marker = bold(green(" [KEPT]")) if w.get('keep') else ""
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
    ident = f"ID: {w['id']} | " if w.get('id') is not None else ""
    # First of the markers: for a day already behind us, what became of the session is
    # the salient state, the way [SYNCED] is for a day ahead.
    adh_marker = adherence_marker(adherence)
    return (
        f"{ident}{cyan(fmt_date(w['date']))} | {magenta(w['sport_type'].upper())} | "
        f"{bold(w['title'])}{adh_marker}{bench_marker}{mod_marker}{sync_marker}"
        f"{rem_marker}{src_marker}{keep_marker}{duration_str}{tss_str}{rpe_str}"
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
    earlier = runtime.db.get_workouts(end_date=cutoff, include_removed=True)
    stale = [w for w in earlier if calendar_status(w) == 'stale']
    if not stale:
        return
    label = "workout" if len(stale) == 1 else "workouts"
    earliest = min(w['date'] for w in stale)
    notice(
        f"Note: {len(stale)} {label} before {fmt_date(start_date)} still read [STALE] "
        f"— their calendar events are out of date and this push did not cover them. "
        f"Run {cmd(f'workout push -d {earliest}..')} to update them.",
    )

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
            notice(
                f"'{target}' is neither a date (YYYY-MM-DD) nor a workout ID. Swap "
                "two dates (e.g. "
                + cmd("workout swap 2026-06-09 2026-06-11 'travelling'")
                + ") or two workout IDs (e.g. "
                + cmd("workout swap 5 8 'travelling'") + ").", red,
            )
            return None
    if kind1 != kind2:
        print(red("Swap two dates or two workout IDs, not one of each."))
        return None

    if kind1 == "id":
        id1, id2 = int(args.target1), int(args.target2)
        w1 = runtime.db.get_workout_by_id(id1)
        w2 = runtime.db.get_workout_by_id(id2)
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
                    f"Cannot swap [{w['id']}] {w['title']} ({fmt_date(w['date'])}): "
                    "it is in the past."
                ))
                return None
        print(
            f"Swapping [{w1['id']}] {w1['title']} ({fmt_date(w1['date'])}) <-> "
            f"[{w2['id']}] {w2['title']} ({fmt_date(w2['date'])})"
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
            print(red(f"Cannot swap {fmt_date(d)}: it is in the past."))
            return None
    on_1 = runtime.db.get_workouts(start_date=date1, end_date=date1)
    on_2 = runtime.db.get_workouts(start_date=date2, end_date=date2)
    if not on_1 and not on_2:
        print(yellow(
            f"No workouts on either {fmt_date(date1)} or {fmt_date(date2)}; "
            f"nothing to swap."
        ))
        return None
    desc_1 = ", ".join(w['title'] for w in on_1) or "(rest)"
    desc_2 = ", ".join(w['title'] for w in on_2) or "(rest)"
    print(f"Swapping {fmt_date(date1)} [{desc_1}] <-> {fmt_date(date2)} [{desc_2}]")
    return (
        [{'id': w['id'], 'new_date': date2} for w in on_1]
        + [{'id': w['id'], 'new_date': date1} for w in on_2]
    )
