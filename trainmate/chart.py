"""The progress-timeline chart renderer — the single implementation of the §2
two-panel picture (DESIGN_progress_timeline.md §7.2).

`render_timeline_png(payload)` takes the `progression.assemble_timeline` payload
(§6.0) and returns PNG bytes. It is the one drawing shared by the Telegram
`--chart` photo (§7.2) and the web `GET /api/timeline.png` endpoint (§6), so the
picture has exactly one implementation.

matplotlib is an optional dependency (`requirements.txt`, optional tier), imported
lazily inside the function: a missing install raises `ImportError`, which callers
turn into an install hint (the CLI prints it, the endpoint answers 503) rather
than breaking text mode too. Rendered at ~2x DPI with >=2px lines so Telegram's
photo recompression has little to smear.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional


def _dt(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def render_timeline_png(payload: Dict[str, Any]) -> bytes:
    """Renders the two-panel timeline (PMC lines over paired weekly-load bars) from a
    §6.0 payload and returns PNG bytes. Empty/warm-up states are drawn as a message
    inside the image so every surface shows the same thing the bot photo does."""
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    days = payload.get("days") or []
    weeks = payload.get("weeks") or []
    meso_bands = payload.get("meso_bands") or []
    objectives = payload.get("objectives") or []
    today = payload.get("today")
    warnings = payload.get("warnings") or []

    fig, (ax_top, ax_bottom) = plt.subplots(
        2, 1, figsize=(10, 6), dpi=150, gridspec_kw={"height_ratios": [2, 1]}
    )

    today_dt = _dt(today) if today else None
    has_pmc = any(p.get("ctl") is not None for p in days)

    if has_pmc and today_dt is not None:
        for label, key, color in (
            ("CTL", "ctl", "tab:blue"),
            ("ATL", "atl", "tab:red"),
            ("TSB", "tsb", "tab:green"),
        ):
            # Solid up to today, dashed beyond; None values break the line (a gap
            # renderers join across, §4).
            past = [
                (_dt(p["date"]), p[key]) for p in days
                if p.get(key) is not None and _dt(p["date"]) <= today_dt
            ]
            future = [
                (_dt(p["date"]), p[key]) for p in days
                if p.get(key) is not None and _dt(p["date"]) >= today_dt
            ]
            if past:
                ax_top.plot([d for d, _ in past], [v for _, v in past],
                            color=color, linewidth=2, label=label)
            if future:
                ax_top.plot([d for d, _ in future], [v for _, v in future],
                            color=color, linewidth=2, linestyle="--")
        ax_top.axvline(today_dt, color="gray", linestyle=":", linewidth=1)
        _draw_objective_flags(ax_top, objectives, days)
        ax_top.legend(loc="upper left")
        ax_top.grid(True, alpha=0.3)
    else:
        # No anchor / entire history inside the warm-up window (§4): suppress the PMC
        # panel and say why, rather than drawing a blank axis.
        msg = next(
            (w for w in warnings if "warming" in w or "no activity" in w),
            "Fitness/fatigue projection unavailable — not enough history yet.",
        )
        ax_top.text(0.5, 0.5, msg, ha="center", va="center", wrap=True,
                    transform=ax_top.transAxes)
        ax_top.set_xticks([])
        ax_top.set_yticks([])
    ax_top.set_title("Fitness / Fatigue / Form")

    _draw_weekly_bars(ax_bottom, mdates, weeks)
    _draw_meso_bands(ax_bottom, meso_bands)
    if weeks:
        ax_bottom.legend(loc="upper left")
    ax_bottom.set_title("Weekly Load: Planned vs Actual")
    ax_bottom.grid(True, alpha=0.3)

    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _draw_objective_flags(ax, objectives: List[Dict[str, Any]], days: List[Dict[str, Any]]) -> None:
    if not days:
        return
    lo, hi = _dt(days[0]["date"]), _dt(days[-1]["date"])
    for obj in objectives:
        try:
            target_dt = _dt(obj["target_date"])
        except (ValueError, TypeError, KeyError):
            continue
        if lo <= target_dt <= hi:
            ax.axvline(target_dt, color="black", linestyle="-.", linewidth=1)


def _draw_weekly_bars(ax, mdates, weeks: List[Dict[str, Any]]) -> None:
    if not weeks:
        return
    week_dates = [_dt(w["week_commencing"]) for w in weeks]
    planned = [
        w["planned_load"] if w.get("planned_load") is not None else 0 for w in weeks
    ]
    actual = [w["actual_load"] for w in weeks]
    x = mdates.date2num(week_dates)
    bar_w = 2.5
    ax.bar([xi - bar_w / 2 for xi in x], planned, width=bar_w,
           label="planned", color="tab:orange", alpha=0.6)
    ax.bar([xi + bar_w / 2 for xi in x], actual, width=bar_w,
           label="actual", color="tab:blue", alpha=0.8)
    ax.xaxis_date()


def _draw_meso_bands(ax, meso_bands: List[Dict[str, Any]]) -> None:
    for span in meso_bands:
        try:
            start = _dt(span["start_date"])
            end = _dt(span["end_date"])
        except (ValueError, TypeError, KeyError):
            continue
        # Inferred (descriptive) bands render lighter than plan bands (§6.1).
        alpha = 0.05 if span.get("source") == "inferred" else 0.12
        ax.axvspan(start, end, color="tab:purple", alpha=alpha)
