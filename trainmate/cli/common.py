"""Shared helpers used across the CLI command modules.

The companion-voice line builders used to live here too; they moved to `cli/render.py`
with the rest of that voice (DESIGN_render_persona.md §7).
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from trainmate.config import config
from trainmate.adherence import STATUS_LABELS, analyze_adherence, classify_adherence
from trainmate.util import (
    cyan, yellow, cmd, fmt_date, today_str as _today_str, notice, warn,
)

# `trainmate_cli` (the `db`/`garmin`/`calendar_syncer` facade) is imported lazily
# inside the functions below: it imports this module, so a module-level import here
# is a cycle that breaks whenever `common` is imported first (e.g. in isolation).


def resolve_cleanup_range(args) -> tuple[Optional[str], Optional[str]]:
    """Resolves the optional [start, end] window shared by the cleanup commands
    (`data wipe`, `workout prune-calendar`) from --from/--until/--days. No date flag
    at all -> (None, None), meaning the whole scope.

    Mirrors `data pull`: --days N anchors a trailing N-day window on --until (default
    today); --from / --until each bound their side, either open-ended on its own.
    Unlike the planning resolver, a lone --until does *not* imply a start of today —
    a cleanup reaches backwards by nature.
    """
    if not (args.from_date or args.until_date or args.days):
        return None, None
    start = args.from_date
    end = args.until_date
    if args.days and start is None:
        base = end or _today_str()
        start = (
            datetime.strptime(base, "%Y-%m-%d").date() - timedelta(days=args.days - 1)
        ).strftime("%Y-%m-%d")
        end = end or base
    return start, end


def pmc_warmup_cutoff(history_start: Optional[str] = None) -> Optional[str]:
    """The §3.3(a) leading-edge cutoff every CLI surface blanks PMC values against.

    Pass `history_start` when the caller already fetched it (it needs a DB hit),
    otherwise it is looked up here."""
    from trainmate import runtime
    if history_start is None:
        history_start = runtime.garmin.pmc_history_start(dbh=runtime.db)
    return runtime.garmin.pmc_warmup_cutoff_for(history_start, config.pmc_ctl_days)


def ensure_recent_data(
    end_date: Optional[str] = None, no_pull: bool = False, force_pull: bool = False
) -> None:
    """Ensures Garmin data covering the recent metrics window is present and fresh,
    auto-pulling small/recent gaps and surfacing large backfills as a command. Warns
    if today's metrics are still unavailable afterward. `force_pull` bypasses the
    refresh-minutes throttle."""
    from trainmate import runtime
    if no_pull:
        return
    end_date = end_date or _today_str()
    history_days = config.metrics_lookback_days
    start_date = (
        datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(days=history_days - 1)
    ).strftime("%Y-%m-%d")
    runtime.garmin.ensure_data(start_date, end_date, force=force_pull)

    today = _today_str()
    if end_date == today:
        rows = runtime.db.get_metrics_cache(start_date=today, end_date=today)
        present = bool(rows) and not (
            rows[0].get('rhr') is None and rows[0].get('hrv') is None
            and rows[0].get('sleep_score') is None and rows[0].get('stress') is None
        )
        if not present:
            notice(
                f"Note: Garmin metrics for today ({fmt_date(today)}) are not available yet.",
            )


def format_actual(act: Dict[str, Any], divergence: bool = False) -> str:
    """Compact 'actual effort' line for one completed activity, e.g.
    '[running] Morning Run (48min, load 62, TSS 58)'.

    One renderer for every surface that names the effort a planned session matched: the
    Calendar adherence header, `workout compare`'s ACTUAL row and `workout list -v`.
    `divergence` adds the "load from RPE" note — compare shows it, the Calendar header
    does not, and that is the only difference the three ever had."""
    from trainmate import runtime
    parts = [f"{act['duration_sec'] / 60:.0f}min", f"load {runtime.garmin.activity_load(act):.0f}"]
    if act.get('tss'):
        parts.append(f"TSS {act['tss']:.0f}")
    if act.get('rpe'):
        parts.append(f"RPE {act['rpe']}")
    line = f"[{act['activity_type']}] {act['activity_name']} ({', '.join(parts)})"
    if not divergence:
        return line
    div = runtime.garmin.rpe_divergence(act)
    if div is None:
        return line
    return f"{line} [load from RPE: HR under-counted {div:.1f}x]"


def adherence_results(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """`analyze_adherence`'s planned-vs-actual pairing over [start_date, end_date],
    read from the cache (this never pulls — the caller owns that).

    The one place a window becomes a pairing, and it is fed **every** planned workout in
    that window rather than the ones a caller means to show (ARCHITECTURE.md §5)."""
    from trainmate import runtime
    workouts = runtime.db.get_workouts(start_date=start_date, end_date=end_date)
    activities = runtime.db.get_completed_activities(start_date=start_date, end_date=end_date)
    start_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
    covered_ranges = runtime.db.get_mesocycle_ranges(start_date, end_date)
    _, matching_results, _ = analyze_adherence(
        planned_workouts=workouts,
        completed_activities=activities,
        start_date_obj=start_obj,
        history_days=(end_obj - start_obj).days + 1,
        minor_activity_load_threshold=config.minor_activity_load_threshold,
        covered_ranges=covered_ranges,
        pending_from=_today_str(),
        rejected_matches=runtime.db.get_rejected_matches(),
    )
    return matching_results


def adherence_verdicts(start_date: str, end_date: str) -> Dict[int, Dict[str, Any]]:
    """Per-workout-id verdict over [start_date, end_date], for the listings that show
    what became of a planned session (ARCHITECTURE.md §5, "Backward adherence marking").

    Each value is `classify_adherence`'s ``{"status", "reasons"}`` plus the athlete-facing
    `label` and the `completed` activity it graded against — so a caller can name the
    verdict and the effort without re-looking-up either."""
    threshold = config.minor_activity_load_threshold
    verdicts: Dict[int, Dict[str, Any]] = {}
    for r in adherence_results(start_date, end_date):
        w = r['planned']
        if w.get('id') is None:
            continue
        verdict = classify_adherence(
            w, r['completed'], threshold, pending=r.get('pending', False)
        )
        verdicts[w['id']] = {
            **verdict,
            "label": STATUS_LABELS.get(verdict["status"], verdict["status"]),
            "completed": r['completed'],
        }
    return verdicts


def mark_adherence_from_results(
    matching_results: List[Dict[str, Any]], today_str: Optional[str] = None
) -> int:
    """Stamps the adherence verdict onto past and same-day planned workout Calendar
    events (title tag + 'Adherence' header). Future events are always skipped.
    Today's event is skipped only when no activity was matched — marking an
    unmatched today's session would falsely read as missed. Workouts without an
    existing Calendar event are skipped, as is any event already carrying this
    exact verdict over unchanged content (matched via `adherence_pushed_signature`), so
    re-running compare over a settled range issues no redundant Calendar writes.
    Best-effort per event: a Calendar failure degrades to a warning. Returns the
    number of events actually (re)marked; the caller owns any summary line."""
    from trainmate.calendar_state import adherence_signature
    from trainmate import runtime
    today_str = today_str or _today_str()
    threshold = config.minor_activity_load_threshold
    marked = 0
    for r in matching_results:
        w = r['planned']
        if not w.get('google_event_id'):
            continue
        # Skip future dates always, and anything analyze_adherence flagged pending — a
        # not-yet-done session would falsely read as missed. The date test behind
        # `pending` is owned there; it is repeated here only for hand-built rows.
        if r['date'] > today_str or r.get('pending'):
            continue
        if r['date'] == today_str and not r['completed']:
            continue
        verdict = classify_adherence(w, r['completed'], threshold)
        actual = format_actual(r['completed']) if r['completed'] else None
        adherence = {
            "status": verdict["status"],
            "actual": actual,
            "reasons": verdict["reasons"],
        }
        # Skip a no-op Calendar write: if the event already carries this exact
        # verdict over unchanged content, re-pushing would just re-issue an
        # identical update. Re-running compare over a settled past range is the
        # common case, so this avoids a burst of pointless API writes.
        signature = adherence_signature(w, adherence)
        if w.get('adherence_pushed_signature') == signature:
            continue
        try:
            runtime.calendar_syncer.sync_workout(w, adherence=adherence)
            if w.get('id') is not None:
                runtime.db.mark_workout_adherence_pushed(w['id'], signature)
            marked += 1
        except Exception as e:
            warn(f"could not mark {fmt_date(w['date'])} on Calendar: {e}")
    return marked


def mark_adherence_range(start_date: str, end_date: str) -> int:
    """Computes adherence over [start_date, end_date] and marks strictly-past
    Calendar events with the verdict. No-op (returns 0) when no calendar is
    configured. Reads the shared `adherence_results` pairing; the `data pull` ride-along
    calls this once fresh activity data has landed."""
    if not config.google_calendar_id:
        return 0
    return mark_adherence_from_results(
        adherence_results(start_date, end_date), _today_str()
    )


def constraint_line(c: Dict[str, Any], needs_a_pass: bool = False) -> str:
    """One-line rendering of a constraint, for `constraint list`/`show`/`add` and `status`.

    Here rather than in `cli/constraints.py` because `status` also draws it, and its own
    hand-rolled copy had already drifted (DESIGN_constraint_honoring.md §4).

    `needs_a_pass` is `coach/honoring.py`'s answer, passed in rather than re-derived: this
    stays a renderer, and the one place that decides which tier owns a directive stays the
    one place. Deciding it here is how the tag came to contradict the sweep (§8).
    """
    tags = ("no training" if c.get('rest') else "advisory") + (
        " · plan-shaping" if c.get('replan') else ""
    )
    if c.get('honored_at'):
        tags += " · honored"
    elif needs_a_pass:
        tags += " · not yet in the plan"
    return (
        f"ID: {c['id']} | {yellow(c['title'])}: "
        f"{cyan(fmt_date(c['start_date']))} to {cyan(fmt_date(c['end_date']))} | {tags}"
    )


def report_unhonored(constraints: List[Dict[str, Any]]) -> None:
    """Names the constraints a rollback just un-honored (§8).

    The restored plan predates those honorings, so it cannot reflect them. Said after the
    fact, not before the `y`: the cost of the flag being cleared is one nudge and a cheap
    re-pass, which is not worth complicating a confirm over.
    """
    if not constraints:
        return
    names = ", ".join(f"[{c['id']}] {c['title']}" for c in constraints)
    notice(
        f"{len(constraints)} constraint(s) the restored plan predates are no longer "
        f"marked honored: {names}. Run " + cmd("workout generate") + " to build them "
        "back in.",
    )


def print_plan_cascade(objective_id: int) -> None:
    """The blast radius `goal rm --purge` and `plan rm` both print before asking — one
    renderer, so two copies cannot drift into disagreeing about one cascade
    (DESIGN_cli_noargs.md §b1)."""
    from trainmate import runtime
    versions = runtime.db.get_macrocycle_versions(objective_id)
    blocks = sum(
        len(runtime.db.get_mesocycles_for_macrocycle(m['id'])) for m in versions
    )
    notes = sum(len(runtime.db.list_plan_feedback(m['id'])) for m in versions)
    orphaned = runtime.db.count_future_workouts_for_macrocycles(
        [m['id'] for m in versions], _today_str()
    )
    print(f"  - {len(versions)} periodization plan version(s)")
    print(f"  - {blocks} mesocycle block(s)")
    print(f"  - {notes} plan feedback note(s)")
    if orphaned:
        notice(
            f"  and leaves {orphaned} upcoming session(s) with no plan to explain "
            "them.",
        )
