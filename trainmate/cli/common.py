"""Shared helpers used across the CLI command modules."""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from trainmate.config import config
from trainmate.adherence import analyze_adherence, classify_adherence
from trainmate.util import yellow, today_str as _today_str

# `trainmate_cli` (the `db`/`garmin`/`calendar_syncer` facade) is imported lazily
# inside the functions below: it imports this module, so a module-level import here
# is a cycle that breaks whenever `common` is imported first (e.g. in isolation).


def fmt_date(date_str: str) -> str:
    """Return 'YYYY-MM-DD Ddd' (e.g. '2026-06-05 Fri')."""
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d %a")


def ensure_recent_data(
    end_date: Optional[str] = None, no_pull: bool = False, force_pull: bool = False
) -> None:
    """Ensures Garmin data covering the recent metrics window is present and fresh,
    auto-pulling small/recent gaps and surfacing large backfills as a command. Warns
    if today's metrics are still unavailable afterward. `force_pull` bypasses the
    refresh-minutes throttle."""
    import trainmate_cli as cli
    if no_pull:
        return
    end_date = end_date or _today_str()
    history_days = config.metrics_lookback_days
    start_date = (
        datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(days=history_days - 1)
    ).strftime("%Y-%m-%d")
    cli.garmin.ensure_data(start_date, end_date, force=force_pull)

    today = _today_str()
    if end_date == today:
        rows = cli.db.get_metrics_cache(start_date=today, end_date=today)
        present = bool(rows) and not (
            rows[0].get('rhr') is None and rows[0].get('hrv') is None
            and rows[0].get('sleep_score') is None and rows[0].get('stress') is None
        )
        if not present:
            print(yellow(f"Note: Garmin metrics for today ({today}) are not available yet."))


def _format_actual(act: Dict[str, Any]) -> str:
    """Compact 'actual effort' line for a Calendar adherence header, e.g.
    '[running] Morning Run (48min, load 62, TSS 58)'."""
    import trainmate_cli as cli
    parts = [f"{act['duration_sec'] / 60:.0f}min", f"load {cli.garmin.activity_load(act):.0f}"]
    if act.get('tss'):
        parts.append(f"TSS {act['tss']:.0f}")
    if act.get('rpe'):
        parts.append(f"RPE {act['rpe']}")
    return f"[{act['activity_type']}] {act['activity_name']} ({', '.join(parts)})"


def mark_adherence_from_results(
    matching_results: List[Dict[str, Any]], today_str: Optional[str] = None
) -> int:
    """Stamps the adherence verdict onto past and same-day planned workout Calendar
    events (title tag + 'Adherence' header). Future events are always skipped.
    Today's event is skipped only when no activity was matched — marking an
    unmatched today's session would falsely read as missed. Workouts without an
    existing Calendar event are skipped, as is any event already carrying this
    exact verdict over unchanged content (matched via `marked_signature`), so
    re-running compare over a settled range issues no redundant Calendar writes.
    Best-effort per event: a Calendar failure degrades to a warning. Returns the
    number of events actually (re)marked; the caller owns any summary line."""
    from trainmate.calendar_state import adherence_signature
    import trainmate_cli as cli
    today_str = today_str or _today_str()
    threshold = config.minor_activity_load_threshold
    marked = 0
    for r in matching_results:
        w = r['planned']
        if not w.get('google_event_id'):
            continue
        # Skip future dates always. Skip today only when unmatched — a not-yet-done
        # session would falsely read as missed.
        if r['date'] > today_str:
            continue
        if r['date'] == today_str and not r['completed']:
            continue
        verdict = classify_adherence(w, r['completed'], threshold)
        actual = _format_actual(r['completed']) if r['completed'] else None
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
        if w.get('marked_signature') == signature:
            continue
        try:
            cli.calendar_syncer.sync_workout(w, adherence=adherence)
            if w.get('id') is not None:
                cli.db.mark_workout_adherence_pushed(w['id'], signature)
            marked += 1
        except Exception as e:
            print(yellow(f"Warning: could not mark {w['date']} on Calendar: {e}"))
    return marked


def mark_adherence_range(start_date: str, end_date: str) -> int:
    """Computes adherence over [start_date, end_date] and marks strictly-past
    Calendar events with the verdict. No-op (returns 0) when no calendar is
    configured. Reuses `analyze_adherence`'s pairing; the `data pull` ride-along
    calls this once fresh activity data has landed."""
    import trainmate_cli as cli
    if not config.google_calendar_id:
        return 0
    workouts = cli.db.get_workouts(start_date=start_date, end_date=end_date)
    activities = cli.db.get_completed_activities(start_date=start_date, end_date=end_date)
    start_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
    history_days = (end_obj - start_obj).days + 1
    covered_ranges = cli.db.get_mesocycle_ranges(start_date, end_date)
    _, matching_results, _ = analyze_adherence(
        planned_workouts=workouts,
        completed_activities=activities,
        start_date_obj=start_obj,
        history_days=history_days,
        minor_activity_load_threshold=config.minor_activity_load_threshold,
        covered_ranges=covered_ranges,
    )
    return mark_adherence_from_results(matching_results, _today_str())
