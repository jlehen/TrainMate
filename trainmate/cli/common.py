"""Shared helpers used across the CLI command modules."""
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from trainmate.config import config
from trainmate.adherence import analyze_adherence, classify_adherence
from trainmate.util import cyan, yellow, cmd, wrap_text, today_str as _today_str

# `trainmate_cli` (the `db`/`garmin`/`calendar_syncer` facade) is imported lazily
# inside the functions below: it imports this module, so a module-level import here
# is a cycle that breaks whenever `common` is imported first (e.g. in isolation).


def fmt_date(date_str: str) -> str:
    """Return 'YYYY-MM-DD Ddd' (e.g. '2026-06-05 Fri')."""
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d %a")


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
            print(yellow(f"Note: Garmin metrics for today ({today}) are not available yet."))


def _format_actual(act: Dict[str, Any]) -> str:
    """Compact 'actual effort' line for a Calendar adherence header, e.g.
    '[running] Morning Run (48min, load 62, TSS 58)'."""
    from trainmate import runtime
    parts = [f"{act['duration_sec'] / 60:.0f}min", f"load {runtime.garmin.activity_load(act):.0f}"]
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
        if w.get('adherence_pushed_signature') == signature:
            continue
        try:
            runtime.calendar_syncer.sync_workout(w, adherence=adherence)
            if w.get('id') is not None:
                runtime.db.mark_workout_adherence_pushed(w['id'], signature)
            marked += 1
        except Exception as e:
            print(yellow(f"Warning: could not mark {w['date']} on Calendar: {e}"))
    return marked


def mark_adherence_range(start_date: str, end_date: str) -> int:
    """Computes adherence over [start_date, end_date] and marks strictly-past
    Calendar events with the verdict. No-op (returns 0) when no calendar is
    configured. Reuses `analyze_adherence`'s pairing; the `data pull` ride-along
    calls this once fresh activity data has landed."""
    from trainmate import runtime
    if not config.google_calendar_id:
        return 0
    workouts = runtime.db.get_workouts(start_date=start_date, end_date=end_date)
    activities = runtime.db.get_completed_activities(start_date=start_date, end_date=end_date)
    start_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
    history_days = (end_obj - start_obj).days + 1
    covered_ranges = runtime.db.get_mesocycle_ranges(start_date, end_date)
    today = _today_str()
    _, matching_results, _ = analyze_adherence(
        planned_workouts=workouts,
        completed_activities=activities,
        start_date_obj=start_obj,
        history_days=history_days,
        minor_activity_load_threshold=config.minor_activity_load_threshold,
        covered_ranges=covered_ranges,
        pending_from=today,
    )
    return mark_adherence_from_results(matching_results, today)


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
        f"{cyan(c['start_date'])} to {cyan(c['end_date'])} | {tags}"
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
    print(yellow(wrap_text(
        f"{len(constraints)} constraint(s) the restored plan predates are no longer "
        f"marked honored: {names}. Run " + cmd("workout generate") + " to build them "
        "back in."
    )))


# --- Simple rendering (DESIGN_bot_simple_frontend.md §6) ---
# Commands opt in one at a time; anything that hasn't opted in falls back to the
# expert form. Tone rule: lead with what was done and what is next, state gaps as
# neutral facts after the lead, never open with a miss.

REST_DAY_LINE = "Rest day — enjoy it 🎉"

# Emoji per canonical sport for the simple session lines; unknown sports get the
# generic one rather than nothing, so a new sport never renders bare.
SPORT_EMOJI = {
    "running": "🏃",
    "cycling": "🚴",
    "swimming": "🏊",
    "strength": "🏋️",
    "hiking": "🥾",
    "rowing": "🚣",
    "yoga": "🧘",
}
DEFAULT_SPORT_EMOJI = "🎽"


def is_simple_render() -> bool:
    """The one place the TRAINMATE_RENDER env var is interpreted (mirrors
    `is_json_frontend` for TRAINMATE_FRONTEND): 'simple' selects the companion
    rendering for commands that opted in (DESIGN_bot_simple_frontend.md §6)."""
    return os.environ.get("TRAINMATE_RENDER", "").strip().lower() == "simple"


def sport_emoji(sport_type: Optional[str]) -> str:
    """The emoji standing in for the sport column in simple session lines."""
    return SPORT_EMOJI.get((sport_type or "").strip().lower(), DEFAULT_SPORT_EMOJI)


def simple_session_line(w: Dict[str, Any], lead: Optional[str] = None) -> str:
    """One simple-mode line for a session: '🏃 Today: Easy run — 40 min'.

    `lead` is the day word ('Today', '2026-08-25 Tue'); omitted for a bare line."""
    duration = w.get("duration_minutes")
    duration_str = f" — {duration} min" if duration else ""
    prefix = f"{sport_emoji(w.get('sport_type'))} "
    if lead:
        prefix += f"{lead}: "
    return f"{prefix}{w.get('title') or w.get('sport_type', 'Session')}{duration_str}"


def simple_day_lines(workouts: List[Dict[str, Any]], date_str: str) -> List[str]:
    """Simple rendering of one day's schedule: session line(s) plus the wrapped
    description (the coach's actual prescription), or the one-line rest message.
    Any empty day gets the rest line, whatever the reason it is empty
    (DESIGN_bot_simple_frontend.md §10)."""
    if not workouts:
        return [REST_DAY_LINE]
    day_word = "Today" if date_str == _today_str() else fmt_date(date_str)
    lines: List[str] = []
    for w in workouts:
        lines.append(simple_session_line(w, lead=day_word))
        description = (w.get("description") or "").strip()
        if description:
            lines.append(wrap_text(description))
    return lines


def simple_week_lines(workouts: List[Dict[str, Any]]) -> List[str]:
    """Simple rendering of a multi-day window: one dated line per session, no
    descriptions, ending on an encouraging count. An empty window is a break, not
    a gap (§6 tone rule)."""
    if not workouts:
        return ["Nothing on the schedule — enjoy the break 🎉"]
    lines = ["🗓 Coming up:"]
    for w in workouts:
        day = datetime.strptime(w["date"], "%Y-%m-%d").strftime("%a %d")
        lines.append(f"{day} · {simple_session_line(w)}")
    count = len(workouts)
    session_word = "session" if count == 1 else "sessions"
    lines.append(f"\n{count} {session_word} planned — you've got this 💪")
    return lines


def simple_constraint_lines(constraints: List[Dict[str, Any]], today: str) -> List[str]:
    """Simple rendering of the directives the coach works around: one bullet per
    constraint, dates as day words, no IDs or tier tags (the expert `constraint list`
    keeps those). Empty reads as a clean slate, not a gap (§6 tone rule)."""
    if not constraints:
        return ["Nothing on the list — no rules to work around right now. "
                "Just tell me when something comes up 💬"]

    def day_word(date_str: str) -> str:
        if date_str == today:
            return "today"
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %b %d")

    lines = ["📌 I'm working around:"]
    for c in constraints:
        span = day_word(c["start_date"])
        if c["end_date"] != c["start_date"]:
            span = f"{span} to {day_word(c['end_date'])}"
        bullet = "🛌" if c.get("rest") else "•"
        lines.append(f"{bullet} {c['title']} — {span}")
    return lines


def simple_progress_lines(payload: Dict[str, Any], today: str) -> List[str]:
    """The two-line simple `progress` summary: a fitness-trend sentence (from the
    CTL series, ~28 days back) and a chart legend. Every branch keeps the §6 tone
    rule — a falling CTL reads as freshening up, not as decay."""
    days = payload.get("days") or []
    dated = [(d["date"], d.get("ctl")) for d in days
             if d.get("ctl") is not None and d["date"] <= today]
    trend = "Your training story is just getting started 🌱"
    if dated:
        now_date, now_ctl = dated[-1]
        base_cutoff = (
            datetime.strptime(now_date, "%Y-%m-%d") - timedelta(days=28)
        ).strftime("%Y-%m-%d")
        # Baseline: the newest sample at or before the cutoff; a shorter history
        # falls back to its earliest sample.
        older = [ctl for date, ctl in dated if date <= base_cutoff]
        base = older[-1] if older else dated[0][1]
        if base and base > 0 and len(dated) > 1:
            delta_pct = (now_ctl - base) / base * 100
            if delta_pct > 3:
                trend = f"Fitness is climbing — up {delta_pct:.0f}% this month 📈"
            elif delta_pct < -3:
                trend = "You're freshening up — recent rest is banking energy 🔋"
            else:
                trend = "Fitness is holding steady — consistency is doing its job 👍"
    return [
        trend,
        "The chart shows your fitness building up top, and week-by-week training "
        "below — keep stacking those weeks 💪",
    ]
