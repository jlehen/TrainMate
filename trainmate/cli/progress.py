"""`tm progress` — the projected Performance Management Chart, numbers-first
(DESIGN_progress_timeline.md §7.1). Rendering is split into pure formatting
helpers (fed by `trainmate.progression` outputs) so they are unit-testable
without a DB, per the `coach/formatting.py` precedent.
"""
import argparse
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from trainmate import progression
from trainmate.util import (
    bold, green, red, yellow, gray, pad_visible, today_str as _today_str,
)
from trainmate.cli.common import ensure_recent_data

# `trainmate_cli` (the `db` facade) is imported lazily inside `run_progress`: it
# imports this module, so a module-level import here is a cycle that breaks
# whenever this module is imported in isolation (e.g. by its own formatting
# tests) — same reason `cli/common.py` defers the same import.

SPARK_CHARS = "▁▂▃▄▅▆▇█"  # ▁..█
BAR_WIDTH = 9
MESO_COL_WIDTH = 8
WEEK_COL_WIDTH = 10


def _short_date(date_str: str) -> str:
    """'2026-07-31' -> '07-31'."""
    return date_str[5:]


def sparkline(values: List[float]) -> str:
    """Min-max-scaled 8-level sparkline, one char per value. Range-stretching
    can make a small climb look steep — accepted (§7.1): the real numbers sit
    on the same line."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        mid = len(SPARK_CHARS) // 2
        return SPARK_CHARS[mid] * len(values)
    chars = []
    for v in values:
        idx = round((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1))
        chars.append(SPARK_CHARS[idx])
    return "".join(chars)


def render_bar(value: float, scale_max: float, width: int = BAR_WIDTH) -> str:
    """Absolute-load bar on a shared scale (§7.1): `scale_max` is the maximum
    weekly load among the displayed rows (planned or actual); `value` is this
    row's own actual load on that scale. Not actual/planned — adherence is
    already the pct column."""
    if scale_max <= 0:
        filled = 0
    else:
        filled = max(0, min(width, round(width * value / scale_max)))
    return "▓" * filled + "░" * (width - filled)


def truncate_label(label: Optional[str], width: int = MESO_COL_WIDTH) -> str:
    """Truncates a mesocycle label to the CLI column width with a trailing
    ellipsis; '-' when there is no label at all (§6.1 'no match')."""
    if not label:
        return "-"
    if len(label) <= width:
        return label
    return label[: width - 1] + "…"


def format_form_line(
    ctl: float, atl: float, tsb: float, weekly_ctl_samples: List[float], weeks_window: int
) -> str:
    """'FORM today   CTL 55   ATL 61   TSB -6     CTL <sparkline> (8w)'."""
    spark = sparkline(weekly_ctl_samples)
    return (
        f"FORM today   CTL {ctl:.0f}   ATL {atl:.0f}   TSB {tsb:+.0f}     "
        f"CTL {spark} ({weeks_window}w)"
    )


def format_projection_line(plan_end_date: str, ctl: float, tsb: float) -> str:
    """'Projected at plan end 07-31:  CTL 61  TSB +1' — shown when the plan
    doesn't yet reach any objective."""
    return f"Projected at plan end {_short_date(plan_end_date)}:  CTL {ctl:.0f}  TSB {tsb:+.0f}"


def format_objective_projection_line(objective: Dict[str, Any], ctl: float, tsb: float) -> str:
    """Per-objective projection, shown instead of the plan-end banner once the
    plan reaches that objective's target date."""
    return (
        f"  \U0001F3C1 {objective['target_date']} {objective['title']} "
        f"— projected CTL {ctl:.0f}, TSB {tsb:+.0f}"
    )


def format_plan_gap_warning(plan_end_date: str, next_objective: Optional[Dict[str, Any]]) -> List[str]:
    """The plan-end gap banner (§3): plan generated through X, N weeks before
    the next objective it doesn't yet reach; names the fix."""
    lines = [yellow(f"⚠ plan generated through {_short_date(plan_end_date)}")]
    if next_objective:
        end_d = datetime.strptime(plan_end_date, "%Y-%m-%d").date()
        target_d = datetime.strptime(next_objective["target_date"], "%Y-%m-%d").date()
        weeks_before = max(0, round((target_d - end_d).days / 7))
        lines[0] += yellow(f" ({weeks_before} wks before)")
        lines.append(
            f"  \U0001F3C1 {next_objective['target_date']} {next_objective['title']} "
            f"({green('workout generate --until-goal')})"
        )
    return lines


def format_no_plan_warning() -> List[str]:
    """The 'no plan generated' empty state (§3): past-only PMC, names the fix."""
    return [
        yellow("⚠ no plan generated — projection unavailable"),
        f"  Run {green('plan generate')} (after {green('data bootstrap')} if never run) to project forward.",
    ]


def _week_row(week: Dict[str, Any], scale_max: float, today: str) -> str:
    meso = pad_visible(truncate_label(week.get("meso_label")), MESO_COL_WIDTH)
    week_label = f"w/c {_short_date(week['week_commencing'])}"
    if week.get("in_progress"):
        week_label += "*"
    week_col = pad_visible(week_label, WEEK_COL_WIDTH)

    planned = week.get("planned_load")
    plan_col = pad_visible("-" if planned is None else f"{planned:.0f}", 4, align_left=False)

    is_future = week["week_commencing"] > today and not week.get("in_progress")
    if is_future:
        return f"{meso} {week_col} {plan_col}  (planned)"

    bar = render_bar(week["actual_load"], scale_max)
    actual_col = pad_visible(f"{week['actual_load']:.0f}", 4, align_left=False)
    if planned:
        pct = f"{round(week['actual_load'] / planned * 100)}%"
    else:
        pct = "-"
    pct_col = pad_visible(pct, 4, align_left=False)
    return f"{meso} {week_col} {plan_col}  {bar} {actual_col} {pct_col}"


def table_rows(weeks: List[Dict[str, Any]], today: str) -> List[str]:
    """The fixed-width WEEKLY LOAD table rows only (header + one per week) —
    the part held to the 48-column budget (§7.1). Legend/warning lines are
    ordinary prose and wrap at the normal CLI width instead."""
    if not weeks:
        return []
    scale_candidates = [
        w["planned_load"] if w["planned_load"] is not None else 0.0 for w in weeks
    ] + [w["actual_load"] for w in weeks]
    scale_max = max(scale_candidates) if scale_candidates else 0.0

    lines = [bold(pad_visible("WEEKLY LOAD", MESO_COL_WIDTH + 1 + WEEK_COL_WIDTH) + " plan  actual")]
    for week in weeks:
        lines.append(_week_row(week, scale_max, today))
    return lines


def format_weekly_table(weeks: List[Dict[str, Any]], today: str, zero_load_count: int) -> List[str]:
    """The WEEKLY LOAD table (§7.1): fixed-width rows, one per week, plus a
    legend/warning footer. `weeks` must already be windowed by the caller
    (`--weeks`, default 8, for the past half; the future half always runs to
    plan end)."""
    lines = table_rows(weeks, today)
    if not lines:
        return []
    lines.append(gray("~ inferred (bootstrap) · * in progress"))
    if zero_load_count:
        lines.append(yellow(
            f"⚠ {zero_load_count} planned workout"
            f"{'s' if zero_load_count != 1 else ''} have neither TSS nor RPE; count as 0"
        ))
    return lines


DEFAULT_CHART_PATH = "./progress.png"


def _render_chart_png(
    day_points: List[Dict[str, Any]],
    weeks: List[Dict[str, Any]],
    meso_spans: List[Dict[str, Any]],
    objectives: List[Dict[str, Any]],
    today: str,
    path: str,
) -> None:
    """Renders the §2 two-panel picture (PMC + weekly load) to a PNG.

    Imported lazily — matplotlib is an optional dependency (§7.2); a missing
    install surfaces as an ImportError the caller turns into an install hint,
    rather than failing text mode too. Rendered at ~2x DPI with >=2px lines so
    Telegram's photo recompression has little to smear (§7.2)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, (ax_top, ax_bottom) = plt.subplots(
        2, 1, figsize=(10, 6), dpi=150, gridspec_kw={"height_ratios": [2, 1]}
    )

    dates = [datetime.strptime(p["date"], "%Y-%m-%d") for p in day_points]
    today_dt = datetime.strptime(today, "%Y-%m-%d")

    for label, key, color in (("CTL", "ctl", "tab:blue"), ("ATL", "atl", "tab:red"),
                               ("TSB", "tsb", "tab:green")):
        past = [(d, p[key]) for d, p in zip(dates, day_points) if d <= today_dt]
        future = [(d, p[key]) for d, p in zip(dates, day_points) if d >= today_dt]
        if past:
            ax_top.plot([d for d, _ in past], [v for _, v in past], color=color,
                        linewidth=2, label=label)
        if future:
            ax_top.plot([d for d, _ in future], [v for _, v in future], color=color,
                        linewidth=2, linestyle="--")

    ax_top.axvline(today_dt, color="gray", linestyle=":", linewidth=1)
    for obj in objectives:
        try:
            target_dt = datetime.strptime(obj["target_date"], "%Y-%m-%d")
        except (ValueError, TypeError, KeyError):
            continue
        if dates and dates[0] <= target_dt <= dates[-1]:
            ax_top.axvline(target_dt, color="black", linestyle="-.", linewidth=1)
    ax_top.legend(loc="upper left")
    ax_top.set_title("Fitness / Fatigue / Form")
    ax_top.grid(True, alpha=0.3)

    week_dates = [datetime.strptime(w["week_commencing"], "%Y-%m-%d") for w in weeks]
    planned = [w["planned_load"] if w["planned_load"] is not None else 0 for w in weeks]
    actual = [w["actual_load"] for w in weeks]
    if week_dates:
        x = mdates.date2num(week_dates)
        bar_w = 2.5
        ax_bottom.bar([xi - bar_w / 2 for xi in x], planned, width=bar_w,
                      label="planned", color="tab:orange", alpha=0.6)
        ax_bottom.bar([xi + bar_w / 2 for xi in x], actual, width=bar_w,
                      label="actual", color="tab:blue", alpha=0.8)
        ax_bottom.xaxis_date()
    ax_bottom.legend(loc="upper left")
    ax_bottom.set_title("Weekly Load: Planned vs Actual")
    ax_bottom.grid(True, alpha=0.3)

    for span in meso_spans:
        try:
            start = datetime.strptime(span["start_date"], "%Y-%m-%d")
            end = datetime.strptime(span["end_date"], "%Y-%m-%d")
        except (ValueError, TypeError, KeyError):
            continue
        alpha = 0.05 if span.get("source") == "inferred" else 0.12
        ax_bottom.axvspan(start, end, color="tab:purple", alpha=alpha)

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _emit_chart(
    chart_arg: Any,
    day_points: List[Dict[str, Any]],
    weeks: List[Dict[str, Any]],
    meso_spans: List[Dict[str, Any]],
    objectives: List[Dict[str, Any]],
    today: str,
    caption: str,
) -> None:
    """Renders the chart and delivers it per front-end (§7.2): a sentinel photo
    line under the bot's json frontend (temp file, bot-owned cleanup), else a
    plain overwritten file path printed to stdout."""
    frontend = os.environ.get("TRAINMATE_FRONTEND", "")
    if frontend.lower() == "json":
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        path = tmp.name
        tmp.close()
    else:
        path = chart_arg if isinstance(chart_arg, str) else DEFAULT_CHART_PATH

    try:
        _render_chart_png(day_points, weeks, meso_spans, objectives, today, path)
    except ImportError:
        print(red(
            "matplotlib is not installed — run: venv/bin/pip install -r requirements.txt"
        ))
        return

    if frontend.lower() == "json":
        from trainmate.prompt import emit_photo
        emit_photo(path, caption=caption)
    else:
        print(f"Chart written to {path}")


def run_progress(args: argparse.Namespace) -> None:
    """Displays the projected fitness/fatigue timeline: measured load to date,
    planned load through plan end, and the weekly planned-vs-actual bars."""
    import trainmate_cli as cli
    ensure_recent_data(no_pull=args.no_pull, force_pull=getattr(args, 'force_pull', False))
    today = _today_str()
    weeks_window = getattr(args, 'weeks', None) or 8

    activities = cli.db.get_completed_activities()
    workouts = cli.db.get_workouts()

    if not activities:
        print(yellow("No activity history yet — run ") + green("'data pull'") + yellow(" first."))
        return

    objectives = cli.db.get_objectives(status='active')
    objectives.sort(key=lambda o: str(o['target_date']))

    governing_macro = None
    for obj in objectives:
        macro = cli.db.get_macrocycle_for_objective(obj['id'])
        if macro:
            governing_macro = macro
            break

    mesocycles = (
        cli.db.get_mesocycles_for_macrocycle(governing_macro['id']) if governing_macro else []
    )
    cache = cli.db.get_analysis_cache("long")
    inferred = (cache.get("reconstruction") or {}).get("inferred_mesocycles", []) if cache else []
    meso_spans = progression.meso_bands(mesocycles, inferred)

    day_points = progression.fitness_series(progression.daily_loads(activities, workouts, today))
    weeks = progression.weekly_aggregates(activities, workouts, today, meso_spans)
    end_date = progression.plan_end(workouts)
    zero_load_count = progression.zero_load_workout_count(workouts)

    today_point = next((p for p in day_points if p["date"] == today), day_points[-1])

    # Last-day-of-week CTL samples for the sparkline, one per displayed week.
    by_date = {p["date"]: p for p in day_points}
    past_weeks = [w for w in weeks if w["week_commencing"] <= today][-weeks_window:]
    weekly_ctl_samples = []
    for w in past_weeks:
        w_start = datetime.strptime(w["week_commencing"], "%Y-%m-%d").date()
        for offset in range(6, -1, -1):
            d = (w_start + timedelta(days=offset)).strftime("%Y-%m-%d")
            if d in by_date and d <= today:
                weekly_ctl_samples.append(by_date[d]["ctl"])
                break

    form_line = format_form_line(
        today_point["ctl"], today_point["atl"], today_point["tsb"],
        weekly_ctl_samples, weeks_window,
    )
    print(form_line)

    if end_date is None:
        for line in format_no_plan_warning():
            print(line)
    else:
        end_point = by_date.get(end_date, day_points[-1])
        reached = [o for o in objectives if o['target_date'] <= end_date]
        if reached:
            for obj in reached:
                obj_point = by_date.get(obj['target_date'], end_point)
                print(format_objective_projection_line(obj, obj_point["ctl"], obj_point["tsb"]))
        else:
            print(format_projection_line(end_date, end_point["ctl"], end_point["tsb"]))
            next_objective = objectives[0] if objectives else None
            for line in format_plan_gap_warning(end_date, next_objective):
                print(line)

    print()
    future_weeks = [w for w in weeks if w["week_commencing"] > today]
    display_weeks = past_weeks + future_weeks
    for line in format_weekly_table(display_weeks, today, zero_load_count):
        print(line)

    chart_arg = getattr(args, 'chart', False)
    if chart_arg:
        window_start = display_weeks[0]["week_commencing"] if display_weeks else today
        chart_points = [p for p in day_points if p["date"] >= window_start]
        _emit_chart(chart_arg, chart_points, display_weeks, meso_spans, objectives, today, form_line)
