"""`tm progress` — the projected Performance Management Chart, numbers-first
(DESIGN_progress_timeline.md §7.1). All rendering is pure formatting helpers fed
the `assemble_timeline` payload (§6.0), so they are unit-testable without a DB,
per the `coach/formatting.py` precedent.

The whole table is laid out to the bot's 48-column budget and stays fixed-width,
so a TTY and Telegram render identically; width is measured with `visible_len`
(emoji are double-width), never `len`.
"""
import argparse
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from trainmate import progression, chart
from trainmate.util import (
    bold, green, red, yellow, gray, dim, pad_visible, visible_len, wrap_text,
    color_tsb, today_str as _today_str, PMC_TSB_LAG_NOTE,
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


def _to_date(date_str: str):
    """'2026-07-31' -> date(2026, 7, 31). The one ISO-date parser for this module, so
    the renderer doesn't sprinkle `datetime.strptime(...)` inline (mirrors
    `progression._to_date`)."""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _short_date(date_str: str) -> str:
    """'2026-07-31' -> '07-31'."""
    return date_str[5:]


def _weekday(date_str: str) -> str:
    """'2026-07-31' -> 'Wed'."""
    return _to_date(date_str).strftime("%a")


def sparkline(values: List[Optional[float]]) -> str:
    """Min-max-scaled 8-level sparkline, one char per value. `None` cells (warm-up
    edge inside the window, or no anchor) render as a blank space; a flat series
    (min == max) renders all present cells at the floor glyph. Range-stretching can
    make a small climb look steep — accepted (§7.1): the real numbers sit alongside."""
    present = [v for v in values if v is not None]
    if not present:
        return " " * len(values)
    lo, hi = min(present), max(present)
    chars = []
    for v in values:
        if v is None:
            chars.append(" ")
        elif hi == lo:
            chars.append(SPARK_CHARS[0])
        else:
            idx = round((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1))
            chars.append(SPARK_CHARS[idx])
    return "".join(chars)


def render_bar(value: float, scale_max: float, width: int = BAR_WIDTH) -> str:
    """Absolute-load bar on a shared scale (§7.1): `scale_max` is the maximum weekly
    load among the displayed rows (planned or actual); `value` is this row's own
    actual load on that scale. Not actual/planned — adherence is the pct column — so
    it works unchanged for ungoverned weeks. A zero scale_max renders empty (no
    division by the zero max)."""
    if scale_max <= 0:
        filled = 0
    else:
        filled = max(0, min(width, round(width * value / scale_max)))
    return "▓" * filled + "░" * (width - filled)


def truncate_label(label: Optional[str], width: int = MESO_COL_WIDTH) -> str:
    """Truncates a mesocycle label to the CLI column width with a trailing ellipsis;
    '—' when there is no label at all (§6.1 'no match')."""
    if not label:
        return "—"
    if len(label) <= width:
        return label
    return label[: width - 1] + "…"


def format_form_line(
    source: Optional[str], ctl: Optional[float], atl: Optional[float],
    tsb: Optional[float],
) -> str:
    """'FORM today (actual)  CTL 55  ATL 61  TSB -6' — the numbers-first answer to
    'am I on track', tagging where today's load came from (§7.1): `(actual)` once
    today's session has synced, `(planned)` while the fold counts the planned session
    in its place. TSB is coloured by `color_tsb`, reusing `tm status`'s conventions.
    Returns the still-warming message when today has no PMC value (§4)."""
    if ctl is None or atl is None or tsb is None:
        return dim("FORM today   PMC still warming — not enough history yet")
    tag = f" ({source})" if source else ""
    return (
        f"FORM today{tag}   CTL {ctl:.0f}   ATL {atl:.0f}   TSB {color_tsb(tsb)}"
    )


def format_sparkline_line(
    ctl_samples: List[Optional[float]], weeks_window: int,
    plan_end_proj: Optional[str] = None,
) -> str:
    """'CTL 8w ▁▂▂▃▃▅▅▆   plan end 07-31: CTL 61 TSB +1' — the CTL trend, one cell per
    displayed week, with the plan-end projection appended when the plan reaches no
    objective (else the per-objective lines carry it)."""
    spark = sparkline(ctl_samples)
    line = f"CTL {weeks_window}w {spark}"
    if plan_end_proj:
        line += f"   {plan_end_proj}"
    return line


def format_plan_end_proj(plan_end_date: str, ctl: float, tsb: float) -> str:
    return f"plan end {_short_date(plan_end_date)}: CTL {ctl:.0f} TSB {tsb:+.0f}"


def format_objective_projection_lines(
    objective: Dict[str, Any], ctl: float, tsb: float
) -> List[str]:
    """Per-objective projection, shown once the plan reaches that objective's target
    date (§7.1)."""
    return [
        f"\U0001F3C1 {objective['target_date']} {objective['title']}",
        f"   projected CTL {ctl:.0f}, TSB {tsb:+.0f}",
    ]


def format_plan_gap_banner(
    plan_end_date: str, next_objective: Dict[str, Any], weeks_before: int
) -> List[str]:
    """The plan-end gap banner (§3): plan generated through X, N weeks before the next
    objective it doesn't yet reach; names the fix (`workout generate --until-goal`). The
    gap itself (which objective, how many weeks) is computed once in
    `progression.plan_gap` and passed in — this is presentation only (§7.1)."""
    return [
        yellow(
            f"⚠ plan generated through {_short_date(plan_end_date)} "
            f"— {weeks_before} wks before"
        ),
        f"  \U0001F3C1 {next_objective['target_date']} {next_objective['title']}",
        f"  ({green('workout generate --until-goal')})",
    ]


def format_no_plan_banner(lapsed_date: Optional[str]) -> List[str]:
    """The 'no plan generated' / 'plan lapsed' empty states (§3): past-only PMC, name
    the fix. `lapsed_date` set → the plan was outrun by the rolling horizon."""
    if lapsed_date:
        return [
            yellow(f"⚠ plan lapsed {_short_date(lapsed_date)} — projection unavailable"),
            f"  Run {green('workout generate')} to project forward again.",
        ]
    return [
        yellow("⚠ no plan generated — projection unavailable"),
        f"  Run {green('plan generate')} "
        f"(after {green('data bootstrap')} if never run) to project forward.",
    ]


def _week_plan_denom(week: Dict[str, Any]) -> Optional[float]:
    """The planned figure a week's bar/percentage compares against (§3): the elapsed
    slice for the in-progress week (so a partial week doesn't read as poor adherence),
    the full planned total otherwise. None for an ungoverned week."""
    if week.get("planned_load") is None:
        return None
    if week.get("in_progress"):
        return week.get("planned_load_elapsed", 0.0)
    return week["planned_load"]


def _week_row(
    week: Dict[str, Any], scale_max: float, today: str, plan_end: Optional[str]
) -> str:
    meso = pad_visible(truncate_label(week.get("meso_label")), MESO_COL_WIDTH)
    week_label = f"w/c {_short_date(week['week_commencing'])}"
    week_sunday = (
        _to_date(week["week_commencing"]) + timedelta(days=6)
    ).strftime("%Y-%m-%d")
    partial = plan_end is not None and week["week_commencing"] <= plan_end < week_sunday
    if week.get("in_progress") or partial:
        week_label += "*"
    week_col = pad_visible(week_label, WEEK_COL_WIDTH)

    denom = _week_plan_denom(week)
    plan_col = pad_visible("—" if denom is None else f"{denom:.0f}", 4, align_left=False)

    is_future = week["week_commencing"] > today and not week.get("in_progress")
    if is_future:
        return f"{meso} {week_col} {plan_col}  (planned)"

    bar = render_bar(week["actual_load"], scale_max)
    actual_col = pad_visible(f"{week['actual_load']:.0f}", 4, align_left=False)
    if denom:
        pct = f"{round(week['actual_load'] / denom * 100)}%"
    else:
        pct = "—"
    pct_col = pad_visible(pct, 4, align_left=False)
    return f"{meso} {week_col} {plan_col}  {bar} {actual_col} {pct_col}"


def table_rows(
    weeks: List[Dict[str, Any]], today: str, plan_end: Optional[str]
) -> List[str]:
    """The fixed-width WEEKLY LOAD table rows only (header + one per week) — the part
    held to the 48-column budget (§7.1). Legend/warning lines are ordinary prose and
    wrap at the normal CLI width instead. Bar scale is the max weekly load among the
    displayed rows (planned-full or actual), so it doesn't jump when future weeks
    arrive."""
    if not weeks:
        return []
    scale_candidates = [
        w["planned_load"] if w.get("planned_load") is not None else 0.0 for w in weeks
    ] + [w["actual_load"] for w in weeks]
    scale_max = max(scale_candidates) if scale_candidates else 0.0

    header = pad_visible("WEEKLY LOAD", MESO_COL_WIDTH + 1 + WEEK_COL_WIDTH)
    lines = [bold(header + " plan  actual")]
    for week in weeks:
        lines.append(_week_row(week, scale_max, today, plan_end))
    return lines


def format_weekly_table(
    weeks: List[Dict[str, Any]], today: str, plan_end: Optional[str],
    has_inferred: bool, partial_note: Optional[str],
) -> List[str]:
    """The WEEKLY LOAD table (§7.1): fixed-width rows plus a legend footer. `weeks`
    must already be windowed by the caller."""
    lines = table_rows(weeks, today, plan_end)
    if not lines:
        return []
    legend_parts = []
    if has_inferred:
        legend_parts.append("~ inferred")
    legend_parts.append("* in progress")
    if partial_note:
        legend_parts.append(partial_note)
    lines.append(gray(" · ".join(legend_parts)))
    return lines


def render_progress(
    payload: Dict[str, Any], weeks_window: int
) -> List[str]:
    """The complete `tm progress` text output (§7.1) as a list of lines, built purely
    from the `assemble_timeline` payload — no DB, unit-testable. Width-agnostic here;
    `run_progress` wraps prose lines through `wrap_text` and the table is already
    fixed-width."""
    today = payload["today"]
    plan_end = payload["plan_end"]
    days = payload["days"]
    weeks = payload["weeks"]
    objectives = payload["objectives"]
    warnings = payload["warnings"]
    by_date = {p["date"]: p for p in days}

    # Displayed weeks: the last `weeks_window` past/current weeks + all future weeks.
    past_weeks = [w for w in weeks if w["week_commencing"] <= today][-weeks_window:]
    future_weeks = [w for w in weeks if w["week_commencing"] > today]
    display_weeks = past_weeks + future_weeks

    lines: List[str] = []

    today_point = by_date.get(today)
    tsb_shown = bool(today_point and today_point.get("tsb") is not None)
    lines.append(format_form_line(
        today_point.get("source") if today_point else None,
        today_point.get("ctl") if today_point else None,
        today_point.get("atl") if today_point else None,
        today_point.get("tsb") if today_point else None,
    ))

    # Sparkline: one CTL sample per displayed past week (its last day <= today).
    ctl_samples: List[Optional[float]] = []
    for w in past_weeks:
        w_start = _to_date(w["week_commencing"])
        sample: Optional[float] = None
        for offset in range(6, -1, -1):
            d = (w_start + timedelta(days=offset)).strftime("%Y-%m-%d")
            if d <= today and d in by_date:
                sample = by_date[d].get("ctl")
                break
        ctl_samples.append(sample)

    # Projection region: per-objective lines when the plan reaches objectives, else a
    # plan-end summary on the sparkline line plus the gap banner.
    reached = []
    if plan_end is not None:
        reached = [
            o for o in objectives
            if o.get("target_date") and o["target_date"] <= plan_end
            and o["target_date"] in by_date
            and by_date[o["target_date"]].get("ctl") is not None
        ]
        reached.sort(key=lambda o: str(o["target_date"]))

    plan_end_proj = None
    if plan_end is not None and not reached:
        end_pt = by_date.get(plan_end)
        if end_pt and end_pt.get("ctl") is not None:
            plan_end_proj = format_plan_end_proj(
                plan_end, end_pt["ctl"], end_pt["tsb"]
            )

    lines.append(format_sparkline_line(ctl_samples, weeks_window, plan_end_proj))

    if plan_end is None:
        lines += format_no_plan_banner(None)
    elif plan_end < today:
        # Plan lapsed: the rolling horizon was outrun, window ended at today (§3).
        lines += format_no_plan_banner(plan_end)
    else:
        # Per-objective projections for objectives the plan reaches, plus the gap
        # banner for the next active objective it doesn't yet reach (§3) — both can
        # apply in a multi-objective season.
        for o in reached:
            pt = by_date[o["target_date"]]
            lines += format_objective_projection_lines(o, pt["ctl"], pt["tsb"])
        gap = progression.plan_gap(objectives, plan_end)
        if gap:
            next_obj, weeks_before = gap
            lines += format_plan_gap_banner(plan_end, next_obj, weeks_before)

    if tsb_shown:
        lines.append(dim(PMC_TSB_LAG_NOTE))

    lines.append("")

    has_inferred = any(b["source"] == "inferred" for b in payload["meso_bands"])
    partial_note = None
    if plan_end is not None:
        # A plan ending on any day but Sunday leaves the final planned week partial.
        if _to_date(plan_end).weekday() != 6:
            partial_note = f"plan ends {_short_date(plan_end)} ({_weekday(plan_end)})"
    lines += format_weekly_table(
        display_weeks, today, plan_end, has_inferred, partial_note
    )

    # Footer warnings — everything except the plan-gap (rendered richly above).
    for w in warnings:
        if w.startswith("plan generated through"):
            continue
        lines.append(yellow(f"⚠ {w}"))

    return lines


DEFAULT_CHART_PATH = "./progress.png"


def _emit_chart(chart_arg: Any, payload: Dict[str, Any], caption: str) -> None:
    """Renders the §2 two-panel chart and delivers it per front-end (§7.2): a sentinel
    photo line under the bot's json frontend (temp file, bot-owned cleanup), else a
    plain overwritten file path printed to stdout.

    Rendering happens *before* any file is created, so a missing matplotlib fails with
    an install hint and leaves nothing behind — no orphaned temp file for the bot to
    clean up (it never learns of one, since no photo sentinel is emitted)."""
    try:
        png = chart.render_timeline_png(payload)
    except ImportError:
        print(red(
            "matplotlib is not installed — run: venv/bin/pip install -r requirements.txt"
        ))
        return

    if os.environ.get("TRAINMATE_FRONTEND", "").lower() == "json":
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(png)
            path = tmp.name
        from trainmate.prompt import emit_photo
        emit_photo(path, caption=caption)
    else:
        path = chart_arg if isinstance(chart_arg, str) else DEFAULT_CHART_PATH
        with open(path, "wb") as f:
            f.write(png)
        print(f"Chart written to {path}")


def run_progress(args: argparse.Namespace) -> None:
    """Displays the projected fitness/fatigue timeline: measured load to date, planned
    load through plan end, and the weekly planned-vs-actual bars (§7.1). Auto-ensures
    fresh Garmin data first (the seam would otherwise read yesterday's un-synced ride
    as a 0-load day)."""
    import trainmate_cli as cli
    from trainmate import timeline
    ensure_recent_data(
        no_pull=args.no_pull, force_pull=getattr(args, "force_pull", False)
    )
    today = _today_str()
    weeks_window = getattr(args, "weeks", None) or 8

    payload = timeline.build_timeline_payload(cli.db)

    lines = render_progress(payload, weeks_window)
    for line in lines:
        # Table rows are already fixed-width; prose (banners, footnotes) wraps.
        print(wrap_text(line) if visible_len(line) > 48 else line)

    chart_arg = getattr(args, "chart", False)
    if chart_arg:
        clipped = progression.clip_payload_for_weeks(payload, weeks_window, today)
        form_line = lines[0] if lines else ""
        _emit_chart(chart_arg, clipped, form_line)
