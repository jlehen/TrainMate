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
from typing import Any, Dict, List


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
    plan_end = payload.get("plan_end")
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
        _draw_objective_flags(ax_top, mdates, objectives, days)
        # Plan-end marker — only when the plan reaches today or beyond (a forward
        # projection to bound); a lapsed plan shows no marker, matching the CLI (§3).
        # Suppressed when an objective already marks that date — the common case,
        # and two rotated labels at one x overprint (§7.2).
        on_objective = any(o.get("target_date") == plan_end for o in objectives)
        if plan_end and _dt(plan_end) >= today_dt and not on_objective:
            _vline_label(ax_top, mdates.date2num(_dt(plan_end)),
                         f"plan ends {plan_end[5:]}", "gray", "--")
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
    _draw_meso_bands(ax_bottom, mdates, meso_bands)
    if weeks:
        # Anchored below the meso label strip, which owns the top of this axis.
        ax_bottom.legend(loc="upper left", bbox_to_anchor=(0, BAND_LABEL_FLOOR))
    ax_bottom.set_title("Weekly Load: Planned vs Actual")
    ax_bottom.grid(True, alpha=0.3)

    fig.autofmt_xdate()
    # Reserve a bottom band for the warnings footer so tight_layout lays the axes
    # above it — the chart's counterpart of the CLI warning banners (§3/§6.0).
    reserve = min(0.04 + 0.025 * len(warnings), 0.35) if warnings else 0.0
    fig.tight_layout(rect=(0, reserve, 1, 1))
    if warnings:
        fig.text(
            0.01, reserve / 2, "\n".join("• " + w for w in warnings),
            fontsize=6, va="center", ha="left", color="darkgoldenrod",
        )
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _vline_label(ax, x_num: float, text: str, color: str, linestyle: str) -> None:
    """A vertical marker with a small rotated label pinned to the top of the axis
    (x in data coords, y as an axes fraction). Labels are plain text, never emoji —
    matplotlib's default font can't render them (§7.2)."""
    ax.axvline(x_num, color=color, linestyle=linestyle, linewidth=1)
    ax.annotate(
        text, xy=(x_num, 1), xycoords=("data", "axes fraction"),
        xytext=(2, -2), textcoords="offset points",
        fontsize=7, rotation=90, va="top", ha="left", color=color, clip_on=True,
    )


def _draw_objective_flags(
    ax, mdates, objectives: List[Dict[str, Any]], days: List[Dict[str, Any]]
) -> None:
    """Objective target dates as labelled vertical markers, clipped to the day window
    (§2). The 🏁 of the CLI/mock becomes a plain-text date + title here (no emoji)."""
    if not days:
        return
    lo, hi = _dt(days[0]["date"]), _dt(days[-1]["date"])
    for obj in objectives:
        try:
            target_dt = _dt(obj["target_date"])
        except (ValueError, TypeError, KeyError):
            continue
        if lo <= target_dt <= hi:
            # Short MM-DD date (like the plan-end label), then the title, capped so a
            # long name can't run off the top as a rotated label.
            label = f"{obj['target_date'][5:]} {obj.get('title', '')}".strip()[:24]
            _vline_label(ax, mdates.date2num(target_dt), label, "black", "-.")


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
    # Headroom for the meso label strip and the legend above the bars (§7.2).
    peak = max(planned + actual, default=0)
    if peak > 0:
        ax.set_ylim(top=peak * 1.5)
    ax.xaxis_date()


BAND_FONTSIZE = 7
_MIN_BAND_LABEL_CHARS = 6  # below this a label is unreadable; draw the tint only
BAND_LABEL_TOP = 0.99      # axes fraction; the two staggered rows hang below it
BAND_LABEL_FLOOR = 0.84    # everything above this belongs to the labels


def _fit_label(text: str, max_chars: int) -> str:
    """`text` cut to `max_chars` with an ellipsis; '' means 'too narrow, draw no
    label' — a verdict the caller must honour rather than smear it over its
    neighbours (§7.2 label collision rules)."""
    if max_chars < _MIN_BAND_LABEL_CHARS:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _band_label_capacity(ax, span_fraction: float) -> int:
    """How many `BAND_FONTSIZE` characters fit across `span_fraction` of the axis.
    Estimated, not measured — an exact answer needs a renderer, and `tight_layout`
    moves the axes afterwards anyway (§7.2)."""
    fig = ax.figure
    axes_px = fig.get_figwidth() * fig.dpi * 0.85  # 0.85 discounts the axis margins
    char_px = BAND_FONTSIZE * fig.dpi / 72.0 * 0.6  # 0.6em ≈ mean proportional glyph
    return int(span_fraction * axes_px / char_px)


def _draw_meso_bands(ax, mdates, meso_bands: List[Dict[str, Any]]) -> None:
    """Mesocycle bands as tinted spans *with* their labels centred in each span (§6.1).
    Inferred (descriptive) bands render lighter; their labels already carry the `~`
    prefix from the payload. Labels are fitted to their span and staggered across two
    rows, else neighbouring names overprint (§7.2)."""
    trans = ax.get_xaxis_transform()  # x in data coords, y as an axes fraction
    x_lo, x_hi = ax.get_xlim()
    x_range = (x_hi - x_lo) or 1.0
    row = 0
    for span in meso_bands:
        try:
            start = _dt(span["start_date"])
            end = _dt(span["end_date"])
        except (ValueError, TypeError, KeyError):
            continue
        # Inferred (descriptive) bands render lighter than plan bands (§6.1).
        alpha = 0.05 if span.get("source") == "inferred" else 0.12
        ax.axvspan(start, end, color="tab:purple", alpha=alpha)

        lo, hi = mdates.date2num(start), mdates.date2num(end)
        label = _fit_label(
            span["label"], _band_label_capacity(ax, (hi - lo) / x_range)
        )
        if not label:
            continue
        # Stagger: two adjacent labels that each just fit still can't touch (§7.2).
        ax.text((lo + hi) / 2, BAND_LABEL_TOP - 0.07 * (row % 2), label,
                transform=trans, ha="center", va="top", fontsize=BAND_FONTSIZE,
                color="tab:purple", clip_on=True)
        row += 1
