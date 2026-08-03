"""`tm progress` — the projected Performance Management Chart, numbers-first
(DESIGN_progress_timeline.md §7.1). All rendering is pure formatting helpers fed
the `assemble_timeline` payload (§6.0), so they are unit-testable without a DB,
per the `coach/formatting.py` precedent.

The whole table is laid out to the bot's 48-column budget and stays fixed-width,
so a TTY and Telegram render identically; width is measured with `visible_len`
(emoji are double-width), never `len`.
"""
import argparse
import textwrap

from trainmate.cli.argparse_ext import _weeks_arg
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from trainmate import progression, chart, intensity
from trainmate.sports import SPORT_MAPPING, canonical_sport
from trainmate.util import (
    bold, green, red, yellow, gray, dim, cmd, pad_visible, visible_len, wrap_text,
    color_tsb, today_str as _today_str, PMC_TSB_LAG_NOTE,
)
from trainmate.cli.common import ensure_recent_data

# `trainmate_cli` (the `db` facade) is imported lazily inside `run_progress`: it
# imports this module, so a module-level import here is a cycle that breaks
# whenever this module is imported in isolation (e.g. by its own formatting
# tests) — same reason `cli/common.py` defers the same import.

SPARK_CHARS = "▁▂▃▄▅▆▇█"  # ▁..█
BAR_WIDTH = 12
WEEK_COL_WIDTH = 11
NUM_COL_WIDTH = 4
TABLE_WIDTH = 48  # the bot's column budget; the band rule may use all of it
BAND_LABEL_WIDTH = TABLE_WIDTH - 5  # '── ' + label + ' ' + at least one closing '─'

# Zone-table columns (DESIGN_intensity_distribution.md §9.6). Z1 and Z2 get a sixth
# character so they keep their minutes past ten hours — the only two zones that ever get
# there, and they get there on exactly the hiking, ski-touring and high-volume cycling
# weeks where the aerobic base is the whole question. 11 + 6 + 6 + 5x5 = 48 for the
# 7-zone power table, 11 + 6 + 6 + 3x5 = 38 for the 5-zone HR one.
ZONE_WIDE_COL = 6
ZONE_COL = 5
# A sport must hold this share of the window's duration to earn a table by default; the
# rest are named in the footer, never silently dropped.
ZONE_SPORT_MIN_SHARE = 0.10
NOT_TRAINED = "—"
UNDERCOUNTED = "!"
# The load table's own marker, and a different claim from the zone table's `!`: the week
# contains a session whose HR recording was too sparse to trust AND carried no RPE, so
# the LOAD is undercounted too and the week reads as an adherence miss it never was
# (DESIGN_intensity_distribution.md §11).
LOAD_SPARSE = "?"

_NO_BAND = object()  # sentinel: no band emitted yet (a real meso_label may be None)


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


def _cells(value: Optional[float], scale_max: float, width: int) -> int:
    """`value` as a whole number of bar cells on the shared scale, clamped to the bar.
    A zero scale_max yields 0 (no division by the zero max)."""
    if scale_max <= 0 or not value:
        return 0
    return max(0, min(width, round(width * value / scale_max)))


def render_bar(
    actual: float, plan: Optional[float], scale_max: float, is_future: bool,
    width: int = BAR_WIDTH,
) -> str:
    """Bullet bar on the shared absolute-load scale `scale_max` (§7.1): past weeks
    fill `▓` to actual and tick `│` at plan, future weeks ghost-fill `▒` to plan."""
    if is_future:
        n = _cells(plan, scale_max, width)
        return "▒" * n + "░" * (width - n)

    n = _cells(actual, scale_max, width)
    bar = ["▓"] * n + ["░"] * (width - n)
    if plan:
        # First cell *beyond* plan: filled-up-to-the-tick reads as on-plan. Clamped
        # into the bar, because the week whose plan *is* scale_max maps to p == width
        # and is precisely the week whose tick matters most (§7.1).
        p = min(_cells(plan, scale_max, width), width - 1)
        bar[p] = "│"
    return "".join(bar)


def truncate_label(label: str, width: int = BAND_LABEL_WIDTH) -> str:
    """Truncates a mesocycle label to `width` with a trailing ellipsis."""
    if len(label) <= width:
        return label
    return label[: width - 1] + "…"


def band_header(label: Optional[str], width: int = TABLE_WIDTH) -> str:
    """A mesocycle band rule spanning the table — `── Base Consolidation ─────────`
    (§7.1). Weeks the plan never governed band under 'unplanned' (§6.1 'no match')."""
    text = truncate_label(label) if label else "unplanned"
    prefix = f"── {text} "
    # At least one closing dash, so the table's right edge stays straight (§7.1);
    # BAND_LABEL_WIDTH reserves the room.
    return prefix + "─" * max(1, width - visible_len(prefix))


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
    ctl_samples: List[Optional[float]], weeks_shown: int,
    plan_end_proj: Optional[str] = None,
) -> str:
    """'CTL 8w ▁▂▂▃▃▅▅▆   plan end 07-31: CTL 61 TSB +1' — the CTL trend, one cell per
    displayed week, with the plan-end projection appended when the plan reaches no
    objective (else the per-objective lines carry it). `weeks_shown` counts the cells
    drawn, which on a short history is fewer than `--weeks` asked for (§7.1)."""
    spark = sparkline(ctl_samples)
    line = f"CTL {weeks_shown}w {spark}"
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
        yellow(f"  ({cmd('workout generate --until-goal', quote=False)})"),
    ]


# `progression` emits its warnings as plain text so the web payload stays ANSI-free, so
# the one that names a command gets highlighted here instead, at render time.
_WARNING_COMMANDS = ("data pull",)


def _warning_line(w: str) -> str:
    """One footer warning in yellow, with any command it names rendered as a call to
    action."""
    for name in _WARNING_COMMANDS:
        marker = f"`{name}`"
        if marker in w:
            head, _, tail = w.partition(marker)
            return yellow(f"⚠ {head}" + cmd(name) + tail)
    return yellow(f"⚠ {w}")


def format_no_plan_banner(lapsed_date: Optional[str]) -> List[str]:
    """The 'no plan generated' / 'plan lapsed' empty states (§3): past-only PMC, name
    the fix. `lapsed_date` set → the plan was outrun by the rolling horizon."""
    if lapsed_date:
        return [
            yellow(f"⚠ plan lapsed {_short_date(lapsed_date)} — projection unavailable"),
            green(f"  Run {cmd('workout generate')} to project forward again."),
        ]
    return [
        yellow("⚠ no plan generated — projection unavailable"),
        green(f"  Run {cmd('plan generate')} "
              f"(after {cmd('data bootstrap')} if never run) to project forward."),
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
    week_label = f"w/c {_short_date(week['week_commencing'])}"
    week_sunday = (
        _to_date(week["week_commencing"]) + timedelta(days=6)
    ).strftime("%Y-%m-%d")
    partial = plan_end is not None and week["week_commencing"] <= plan_end < week_sunday
    if week.get("in_progress") or partial:
        week_label += "*"
    if week.get("load_sparse"):
        week_label += LOAD_SPARSE
    week_col = pad_visible(week_label, WEEK_COL_WIDTH)

    denom = _week_plan_denom(week)
    plan_col = pad_visible(
        "—" if denom is None else f"{denom:.0f}", NUM_COL_WIDTH, align_left=False
    )

    is_future = week["week_commencing"] > today and not week.get("in_progress")
    bar = render_bar(week["actual_load"], denom, scale_max, is_future)
    if is_future:
        # No actual and no adherence yet — leave the columns off rather than filling
        # them with em-dashes the eye has to skip.
        return f"{week_col} {plan_col}  {bar}"

    actual_col = pad_visible(
        f"{week['actual_load']:.0f}", NUM_COL_WIDTH, align_left=False
    )
    if denom:
        pct = f"{round(week['actual_load'] / denom * 100)}%"
    else:
        pct = "—"
    pct_col = pad_visible(pct, NUM_COL_WIDTH, align_left=False)
    return f"{week_col} {plan_col}  {bar} {actual_col} {pct_col}"


def table_rows(
    weeks: List[Dict[str, Any]], today: str, plan_end: Optional[str]
) -> List[str]:
    """The fixed-width WEEKLY LOAD table rows only (header, band rules, one row per
    week) — the part held to the 48-column budget (§7.1). Legend/warning lines are
    ordinary prose and wrap at the normal CLI width instead. Bar scale is the max
    weekly load among the displayed rows, so it doesn't jump when future weeks
    arrive."""
    if not weeks:
        return []
    scale_candidates = [
        w["planned_load"] if w.get("planned_load") is not None else 0.0 for w in weeks
    ] + [w["actual_load"] for w in weeks]
    scale_max = max(scale_candidates) if scale_candidates else 0.0

    header = (
        f"{pad_visible('WEEKLY LOAD', WEEK_COL_WIDTH)} "
        f"{pad_visible('plan', NUM_COL_WIDTH, align_left=False)}  "
        f"{pad_visible('▓done ▒plan', BAR_WIDTH)} "
        f"{pad_visible('done', NUM_COL_WIDTH, align_left=False)} "
        f"{pad_visible('adh', NUM_COL_WIDTH, align_left=False)}"
    )
    lines = [bold(header)]

    # A band rule wherever the mesocycle changes, so each label is written once, in
    # full, instead of truncated onto every row.
    current_label: Any = _NO_BAND
    for week in weeks:
        label = week.get("meso_label")
        if label != current_label:
            lines.append(gray(band_header(label)))
            current_label = label
        lines.append(_week_row(week, scale_max, today, plan_end))
    return lines


def format_weekly_table(
    weeks: List[Dict[str, Any]], today: str, plan_end: Optional[str],
    has_inferred: bool, partial_note: Optional[str], hidden_weeks: int = 0,
) -> List[str]:
    """The WEEKLY LOAD table (§7.1): fixed-width rows plus a legend footer. `weeks`
    must already be windowed by the caller; `hidden_weeks` counts every week that
    window dropped — past *and* projected — named in the legend so the truncation is
    never silent."""
    lines = table_rows(weeks, today, plan_end)
    if not lines:
        return []
    legend_parts = []
    if has_inferred:
        legend_parts.append("~ inferred")
    legend_parts.append("* in progress")
    if any(w.get("load_sparse") for w in weeks):
        legend_parts.append(f"{LOAD_SPARSE} load undercounted — recording gap, no RPE")
    if partial_note:
        legend_parts.append(partial_note)
    if hidden_weeks:
        legend_parts.append(f"+{hidden_weeks} more (--weeks all)")
    lines.append(gray(" · ".join(legend_parts)))
    return lines


# ------------------------------------------------------------------ zone tables
# The intensity half of the screen (DESIGN_intensity_distribution.md §9.6). It lives here
# rather than in `intensity.py` because it aligns row for row with the load table above
# and shares that table's week column, band walk and 48-column budget; `intensity.py`
# keeps the aggregation and the prompt-width table the coach reads.


def fmt_zone_cell(seconds: float) -> str:
    """A zone's time capped to four characters — `55m`, `5h00`, `12h`, `—` for none.

    `intensity.fmt_duration` renders `12h30` at five characters, and a 7-zone power table
    of five-character cells is 55 columns — it overruns the budget precisely for the
    high-volume cyclist the power table exists to serve. Z3 and above never reach ten
    hours in a week, so nothing above tempo loses precision anywhere (§9.6).
    """
    minutes = int(round((seconds or 0.0) / 60.0))
    if minutes <= 0:
        return NOT_TRAINED
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h" if hours >= 10 else f"{hours}h{minutes % 60:02d}"


def zone_col_widths(n_zones: int) -> List[int]:
    return [ZONE_WIDE_COL, ZONE_WIDE_COL][:n_zones] + [ZONE_COL] * max(0, n_zones - 2)


def _zone_cells_row(label: str, cells: Sequence[str]) -> str:
    widths = zone_col_widths(len(cells))
    body = "".join(
        pad_visible(c, w, align_left=False) for c, w in zip(cells, widths)
    )
    return pad_visible(label, WEEK_COL_WIDTH) + body


def window_sport_stats(weeks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per canonical sport over the displayed window: total duration and total recorded
    seconds per currency — everything the sport filter, the 10% floor and the currency
    choice read.

    All three are computed over the DISPLAYED window, which is what the header's share
    figure means. One consequence to expect rather than to fix: `--weeks all` can qualify
    a different set of sports than the default does, and can draw one of them in a
    different currency, because both rules read the window they are given (§9.6).
    """
    stats: Dict[str, Dict[str, Any]] = {}

    def entry(sport: str) -> Dict[str, Any]:
        return stats.setdefault(sport, {"seconds": 0.0, "zone_seconds": {}})

    for week in weeks:
        for sport, secs in (week.get("sport_seconds") or {}).items():
            entry(sport)["seconds"] += secs
        for row in week.get("zone_rows") or []:
            zones = entry(row.sport)["zone_seconds"]
            zones[row.currency] = zones.get(row.currency, 0.0) + row.total
    for agg in stats.values():
        agg["coverage"] = {
            cur: (secs / agg["seconds"] if agg["seconds"] else 0.0)
            for cur, secs in agg["zone_seconds"].items()
        }
    return stats


def select_zone_sports(
    explicit: Optional[Sequence[str]], preferences: Sequence[str],
    stats: Dict[str, Dict[str, Any]],
) -> Tuple[List[str], List[str], List[str]]:
    """`(sports, omitted_low_volume, omitted_no_zone_data)` — which sports get a table.

    Naming sports explicitly overrides all three filters; the default is every
    `sport_preferences` entry, **in config order** (the athlete's own priority list, and
    stable across invocations — a screen someone checks daily must not reshuffle its rows
    because last week's volume moved) that has zone data in the window and holds at least
    `ZONE_SPORT_MIN_SHARE` of its duration.
    """
    if explicit:
        return [canonical_sport(s) for s in explicit], [], []
    total = sum(agg["seconds"] for agg in stats.values())
    sports, low, no_data = [], [], []
    for name in preferences:
        sport = canonical_sport(name)
        agg = stats.get(sport)
        if not agg or not agg["seconds"]:
            continue
        if not any(agg["zone_seconds"].values()):
            no_data.append(sport)
        elif total and agg["seconds"] / total < ZONE_SPORT_MIN_SHARE:
            low.append(sport)
        else:
            sports.append(sport)
    return sports, low, no_data


def zone_currency(
    stats: Dict[str, Dict[str, Any]], sport: str, forced: Optional[str] = None
) -> Optional[str]:
    """The currency one sport's table is drawn in. `forced` (`--power`/`--hr`) wins where
    that currency has data, and has no effect on a sport that has only the other."""
    agg = stats.get(sport)
    if not agg:
        return None
    if forced and agg["zone_seconds"].get(forced):
        return forced
    return intensity.pick_currency(agg["coverage"])


def zone_week_cells(
    week: Dict[str, Any], sport: str, currency: str, n_zones: int
) -> Tuple[List[str], bool]:
    """`(cells, undercounted)` for one week of one sport's table.

    Three states, not two (§9.6): no duration is not-trained and renders `—` unmarked;
    duration with nothing recorded in this currency renders `—` and takes a `!`, because
    asserting the athlete simply did not train would be §7's meaning turned exactly
    backwards; a real row renders its minutes and takes a `!` below the display bar.
    """
    row = next(
        (r for r in (week.get("zone_rows") or [])
         if r.sport == sport and r.currency == currency),
        None,
    )
    if row is None:
        trained = bool((week.get("sport_seconds") or {}).get(sport))
        return [NOT_TRAINED] * n_zones, trained
    return (
        [fmt_zone_cell(s) for s in row.seconds],
        row.coverage < intensity.ZONE_COVERAGE_DISPLAY_MIN,
    )


def _legend(text: str) -> List[str]:
    """One legend paragraph wrapped into the table's budget, continuations indented so
    they read as the same line rather than as a new one."""
    return textwrap.wrap(
        text, width=TABLE_WIDTH, subsequent_indent="  ", break_on_hyphens=False
    ) or [text]


def zone_table(
    weeks: List[Dict[str, Any]], sport: str, currency: str,
    sport_seconds: float, window_seconds: float, coverage: float,
) -> Tuple[List[str], set]:
    """One sport's weekly zone table plus the set of glyphs it actually used: header,
    column row, band rules, one row per week.

    The tables cover past and in-progress weeks only — the future half of the load table
    is planned TSS, and until planned zones land there is no intensity target to put under
    it (§9.6/§9.8). The current week sits inline with full weeks, marked `*`: the marker
    is what §4's argument asks for at weekly grain, and a separate section for one week
    would cost more than it saves.
    """
    spec = intensity.CURRENCY_BY_KEY[currency]
    n_zones = len(spec.labels)
    tag = "pwr" if currency == "power" else "HR"
    lines = _legend(
        f"ZONES {sport} [{tag} {coverage * 100:.0f}%] — "
        f"{intensity.fmt_duration(sport_seconds)} of "
        f"{intensity.fmt_duration(window_seconds)} total"
    )
    lines = [bold(lines[0])] + lines[1:]
    lines.append(_zone_cells_row(
        "week", [f"Z{i}" for i in range(1, n_zones + 1)]
    ))

    used: set = set()
    current_label: Any = _NO_BAND
    for week in weeks:
        label = week.get("meso_label")
        if label != current_label:
            lines.append(gray(band_header(label)))
            current_label = label
        cells, undercounted = zone_week_cells(week, sport, currency, n_zones)
        used.update(c for c in cells if c == NOT_TRAINED)
        week_label = f"w/c {_short_date(week['week_commencing'])}"
        if week.get("in_progress"):
            week_label += "*"
        if undercounted:
            week_label += UNDERCOUNTED
            used.add(UNDERCOUNTED)
        lines.append(_zone_cells_row(week_label, cells))

    names = " · ".join(
        f"Z{i} {label}" for i, label in enumerate(spec.labels, start=1)
    )
    lines.extend(gray(l) for l in _legend(f"{names} · * in progress"))
    return lines, used


def zone_section(
    weeks: List[Dict[str, Any]], preferences: Sequence[str],
    explicit: Optional[Sequence[str]] = None, forced_currency: Optional[str] = None,
    hidden_weeks: int = 0,
) -> List[str]:
    """Every zone table plus the shared footer, or the empty-state line.

    `weeks` are the displayed PAST/in-progress weeks only. The default stacks one table
    per qualifying sport, because fixing the grain to a single sport buys legibility at
    the price of a new lie: an athlete who swapped two planned runs for two rides of equal
    TSS reads a running-only table as whole-athlete load held flat beside a collapsed
    aerobic base — the exact signature of intensity creep, on a week where nothing went
    wrong. The cycling table rising as the running table falls makes "they rode instead"
    self-evident (§9.6).
    """
    stats = window_sport_stats(weeks)
    window_seconds = sum(agg["seconds"] for agg in stats.values())
    sports, low, no_data = select_zone_sports(explicit, preferences, stats)

    lines: List[str] = []
    drawn: List[str] = []
    used_markers: set = set()
    for sport in sports:
        agg = stats.get(sport)
        currency = zone_currency(stats, sport, forced_currency)
        if not agg or currency is None:
            continue
        if lines:
            lines.append("")
        table, markers = zone_table(
            weeks, sport, currency, agg["seconds"], window_seconds,
            agg["coverage"].get(currency, 0.0),
        )
        lines.extend(table)
        drawn.append(sport)
        used_markers |= markers

    if not drawn:
        # Both failures key on *no rows in the window*, not on *not a known sport*:
        # `canonical_sport` passes unknown values through stripped and lowercased, so
        # nothing is unrecognised at that layer and the list must come from the data.
        have = sorted(
            s for s, agg in stats.items() if any(agg["zone_seconds"].values())
        )
        asked = ", ".join(canonical_sport(s) for s in explicit) if explicit else None
        if asked:
            lines.extend(yellow(l) for l in _legend(
                f"No zone data for {asked} in this window."
            ))
        else:
            lines.extend(yellow(l) for l in _legend(
                "No zone data in this window."
            ))
        if have:
            lines.extend(gray(l) for l in _legend(
                f"Sports with zone data here: {', '.join(have)}"
            ))
        return lines

    footer: List[str] = []
    parts = []
    if NOT_TRAINED in used_markers:
        parts.append(f"{NOT_TRAINED} not trained")
    if UNDERCOUNTED in used_markers:
        # Says nothing about the TSS beside it: the load fallback swaps at
        # `hr_zone_coverage_min`, not at the display bar, so across most of the 0.5-0.8
        # band the week's TSS is still hrTSS computed from these very seconds (§9.6).
        parts.append(
            f"{UNDERCOUNTED} zone minutes undercounted — the recording missed time"
        )
    if parts:
        footer.extend(_legend(" · ".join(parts)))
    if no_data:
        footer.extend(_legend(
            f"{', '.join(no_data)}: sessions but no zone recording in this window"
        ))
    if low:
        footer.extend(_legend(
            f"{', '.join(low)} omitted (under {ZONE_SPORT_MIN_SHARE * 100:.0f}% of "
            f"volume) — name them to see: tm progress {low[0]}"
        ))
    if hidden_weeks:
        footer.append(f"+{hidden_weeks} more weeks (--weeks all)")
    lines.extend(gray(l) for l in footer)
    return lines


def unknown_sport_preferences(preferences: Sequence[str]) -> List[str]:
    """Warnings for `sport_preferences` entries that are not canonical sports (§9.6).

    A warning and not an error: `SPORT_MAPPING` has no `swimming` or `rowing` entry and a
    genuinely new sport must still round-trip. Emitted here, where the list is consumed,
    and not in `Config.__init__` — `config.py` has no validation pass and is imported in
    every process, so a warning there would greet `tm --help`, the Telegram bot and the
    web app alike, none of which read this list.
    """
    import difflib
    out = []
    for name in preferences:
        key = canonical_sport(name)
        if key in SPORT_MAPPING:
            continue
        close = difflib.get_close_matches(key, list(SPORT_MAPPING), n=1, cutoff=0.6)
        hint = f" Did you mean '{close[0]}'?" if close else ""
        out.append(
            f"sport_preferences: '{name}' is not a known sport — it will be matched "
            f"literally against activity types.{hint}"
        )
    return out


def render_progress(
    payload: Dict[str, Any], weeks_window: Any, explain: bool = False,
    zone_opts: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """The complete `tm progress` text output (§7.1) as a list of lines, built purely
    from the `assemble_timeline` payload — no DB, unit-testable. Width-agnostic here;
    `run_progress` wraps prose lines through `wrap_text` and the table is already
    fixed-width. `weeks_window` is a positive int or `'all'` and windows *both*
    halves (§7.1); `explain` adds the PMC footnotes.

    `zone_opts` (`{preferences, sports, currency}`) adds the per-sport intensity tables
    under the load table (DESIGN_intensity_distribution.md §9.6). It scopes the intensity
    content ONLY: CTL, ATL, TSB, the projection and the WEEKLY LOAD table stay
    whole-athlete, because a running-only CTL is not a quantity — the fitness model
    integrates every session the body paid for — and adherence is measured against the
    whole plan. Every line it emits is inside the 48-column budget, so `run_progress`'s
    re-wrap never fires on one."""
    today = payload["today"]
    plan_end = payload["plan_end"]
    days = payload["days"]
    weeks = payload["weeks"]
    objectives = payload["objectives"]
    warnings = payload["warnings"]
    by_date = {p["date"]: p for p in days}

    # Displayed weeks: the last `weeks_window` past/current weeks + the next
    # `weeks_window` projected ones (all of both under `--weeks all`).
    past_all = [w for w in weeks if w["week_commencing"] <= today]
    future_all = [w for w in weeks if w["week_commencing"] > today]
    if weeks_window == "all":
        past_weeks, future_weeks = past_all, future_all
    else:
        past_weeks = past_all[-weeks_window:]
        future_weeks = future_all[:weeks_window]
    # Both sides: a default run over a long history hides far more past than future.
    hidden_weeks = (len(past_all) - len(past_weeks)) + (
        len(future_all) - len(future_weeks)
    )
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

    # Label the cells actually drawn, not the window asked for: on a young DB
    # `--weeks 8` over one week of history used to render 'CTL 8w ▁' (§7.1).
    lines.append(format_sparkline_line(ctl_samples, len(past_weeks), plan_end_proj))

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

    # The lag note is a standing caveat, not news: printing it on every invocation
    # trained the eye to skip it. Behind `--explain` (§7.1).
    if tsb_shown and explain:
        lines.append(dim(PMC_TSB_LAG_NOTE))

    lines.append("")

    has_inferred = any(b["source"] == "inferred" for b in payload["meso_bands"])
    partial_note = None
    if plan_end is not None:
        # A plan ending on any day but Sunday leaves the final planned week partial.
        if _to_date(plan_end).weekday() != 6:
            partial_note = f"plan ends {_short_date(plan_end)} ({_weekday(plan_end)})"
    lines += format_weekly_table(
        display_weeks, today, plan_end, has_inferred, partial_note, hidden_weeks
    )

    if zone_opts is not None:
        # Past and in-progress weeks only: the load table's ghost bars have no intensity
        # target under them until §9.8 lands, so this is the one place the two halves do
        # not align row for row (§9.6).
        zone_lines = zone_section(
            past_weeks, zone_opts.get("preferences") or [],
            explicit=zone_opts.get("sports"),
            forced_currency=zone_opts.get("currency"),
            hidden_weeks=len(past_all) - len(past_weeks),
        )
        if zone_lines:
            lines.append("")
            lines += zone_lines

    # Footer warnings — everything except the plan-gap (rendered richly above).
    for w in warnings:
        if w.startswith("plan generated through"):
            continue
        lines.append(_warning_line(w))

    return lines


def _blocks_in_window(dbh, start: str, end: str) -> List[Dict[str, Any]]:
    """Every mesocycle overlapping `start..end`, plus the block preceding the first of
    them so §4.1's delta has a left-hand side.

    Walked macrocycle-first, never by a date-ordered mesocycle query: every mesocycle
    accessor filters `mac.status = 'active'`, which hides the cross-plan case, and
    dropping that filter drags in superseded rollback versions whose blocks overlap the
    live ones and describe training that never happened (§4.1).
    """
    blocks: List[Dict[str, Any]] = []
    seen = set()
    governing = dbh.get_governing_macrocycle()
    macros = [dbh.get_previous_macrocycle(), governing]
    for macro in macros:
        if not macro or macro["id"] in seen:
            continue
        seen.add(macro["id"])
        blocks.extend(dbh.get_mesocycles_for_macrocycle(macro["id"]))
    return blocks


def _orphan_week_note(
    weeks: List[Dict[str, Any]], reported: List[Dict[str, Any]]
) -> List[str]:
    """The weeks in the window that belong to no reported block, named.

    `--blocks` reproduces the very loss §9.6 exists to prevent, and by more than one
    route: weeks belonging to no mesocycle are silently absent, and `rate_window`
    additionally excludes each block's partial tail from both sides of its division —
    correctly, and invisibly, dropping up to six more days per block. Silence would be
    the block-grained blindness this section was written about, reintroduced by the flag
    that opts into block grain.
    """
    orphans = []
    for week in weeks:
        mon = week["week_commencing"]
        sun = _to_date(mon) + timedelta(days=6)
        sun_s = sun.strftime("%Y-%m-%d")
        if not any(
            b["start_date"] <= sun_s and b["end_date"] >= mon for b in reported
        ):
            orphans.append(_short_date(mon))
    if not orphans:
        return []
    shown = orphans[:2]
    tail = f", +{len(orphans) - len(shown)} more" if len(orphans) > len(shown) else ""
    return _legend(
        f"{len(orphans)} week{'s' if len(orphans) != 1 else ''} in this window "
        f"belong to no block ({', '.join(shown)}{tail}), and each block's final partial "
        f"week is excluded from its rate — run without --blocks for the weekly view"
    )


def render_block_section(
    dbh, payload: Dict[str, Any], weeks_window: Any, today: str,
    explicit: Sequence[str], preferences: Sequence[str],
) -> List[str]:
    """`--blocks`: the graded view, per mesocycle instead of per week (§9.6).

    Two grains, two questions — the week table answers *when did it change*, the block
    table answers *did the block do what it said*. Only the block has a stated intent to
    be graded against, which is why the weekly table carries no verdict and no focus. It
    REPLACES the weekly zone table rather than appending to it: the flag is a choice of
    grain, not an extra section.

    Single-sport, because N sports x M blocks is not a view. And it trades brevity for
    grain rather than the other way round — a single block runs about 25 lines at phone
    width — so the help text says so.
    """
    weeks = [w for w in payload["weeks"] if w["week_commencing"] <= today]
    if weeks_window != "all":
        weeks = weeks[-int(weeks_window):]
    if not weeks:
        return []
    window_start = weeks[0]["week_commencing"]

    stats = window_sport_stats(weeks)
    sports, _, _ = select_zone_sports(explicit, preferences, stats)
    if not sports:
        return [yellow("No sport with zone data in this window.")]
    sport = sports[0]

    def fetch(start: str, end: str) -> List[Dict[str, Any]]:
        return [
            a for a in dbh.get_completed_activities(start_date=start, end_date=end)
            if canonical_sport(a.get("activity_type") or "unknown") == sport
        ]

    blocks = _blocks_in_window(dbh, window_start, today)
    benchmarks = dbh.get_benchmark_results()
    lines: List[str] = [bold(f"ZONES BY BLOCK — {sport}")]
    reported: List[Dict[str, Any]] = []
    for i, meso in enumerate(blocks):
        if meso["end_date"] < window_start or meso["start_date"] > today:
            continue
        text = intensity.block_report(
            meso, today, fetch,
            current_week=meso["start_date"] <= today <= meso["end_date"],
            previous=blocks[i - 1] if i else None,
            benchmarks=benchmarks, notes=False, indent="", width=TABLE_WIDTH,
        )
        if text:
            lines.append("")
            lines.extend(text.split("\n"))
            reported.append(meso)

    if not reported:
        return [yellow("No mesocycle overlaps this window.")]

    # Once per section, under the last block — `block_report` printing its own would
    # render the same two caveats three times over three blocks (§9.6).
    rows = intensity.zone_rows(fetch(window_start, today))
    notes = intensity.format_notes(rows, width=TABLE_WIDTH)
    if notes:
        lines.append("")
        lines.extend(gray(n) for n in notes)
    orphan = _orphan_week_note(weeks, reported)
    if orphan:
        lines.append("")
        lines.extend(gray(o) for o in orphan)
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
        print(red("matplotlib is not installed — run: "
                  + cmd("venv/bin/pip install -r requirements.txt", quote=False)))
        return

    from trainmate.prompt import emit_photo, is_json_frontend
    if is_json_frontend():
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(png)
            path = tmp.name
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
    from trainmate.config import config
    ensure_recent_data(
        no_pull=args.no_pull, force_pull=getattr(args, "force_pull", False)
    )
    today = _today_str()
    weeks_window = getattr(args, "weeks", None) or 8

    payload = timeline.build_timeline_payload(cli.db)

    sports = list(getattr(args, "sports", None) or [])
    forced = "power" if getattr(args, "power", False) else (
        "hr" if getattr(args, "hr", False) else None
    )
    blocks = getattr(args, "blocks", False)
    preferences: List[str] = []
    if not sports:
        preferences = list(config.user_profile.get("sport_preferences") or [])
        for warning in unknown_sport_preferences(preferences):
            print(yellow(f"Warning: {warning}"))

    zone_opts = None if blocks else {
        "preferences": preferences, "sports": sports, "currency": forced,
    }
    lines = render_progress(
        payload, weeks_window, getattr(args, "explain", False), zone_opts
    )
    for line in lines:
        # Table rows are already fixed-width; prose (banners, footnotes) wraps.
        print(wrap_text(line) if visible_len(line) > 48 else line)

    if blocks:
        # Printed outside the loop above: `block_report` lays one zone cell per line at
        # phone width, and a screen-width re-wrap would shred those columns (§9.6).
        for line in render_block_section(
            cli.db, payload, weeks_window, today, sports, preferences
        ):
            print(line)

    chart_arg = getattr(args, "chart", False)
    if chart_arg:
        clipped = progression.clip_payload_for_weeks(
            payload, weeks_window, today, cap_future=True
        )
        form_line = lines[0] if lines else ""
        _emit_chart(chart_arg, clipped, form_line)


def add_progress_parser(subparsers, pull_bypass_parser):
    # progress command — the projected Performance Management Chart
    progress_parser = subparsers.add_parser(
        "progress",
        parents=[pull_bypass_parser],
        help="Show the training progress timeline: measured load to date, projected forward",
        description=(
            "Show a single continuous timeline of training load: past days measured "
            "from completed activities, future days planned from the current plan, one "
            "fitness/fatigue model (CTL/ATL/TSB) run across the seam. Projects to plan "
            "end (or each objective the plan reaches) so you can see whether the plan "
            "as written delivers peak fitness with positive form on race day. Under the "
            "load table, one time-in-zone table per sport: TSS folds volume and "
            "intensity into one number, so easy days drifting to tempo read as flat "
            "weekly load and flat adherence. The zone tables cover past and in-progress "
            "weeks only — planned sessions carry no intensity target yet. Naming sports "
            "scopes those tables ONLY: CTL/ATL/TSB, the projection and the weekly load "
            "table stay whole-athlete."
        )
    )
    progress_parser.add_argument(
        "sports", nargs="*", metavar="SPORT",
        help="Canonical sports to report intensity for, one table each in the order "
             "given (e.g. 'tm progress running cycling'). Default: every sport "
             "preference with zone data in the window that holds at least 10%% of its "
             "duration, in config order; the rest are named in the footer."
    )
    progress_parser.add_argument(
        "--weeks", type=_weeks_arg, default=8, metavar="N",
        help="Weeks of weekly load to show either side of today (default: 8, must be "
             ">= 1, or 'all' for the whole plan). Not symmetric: the zone tables cover "
             "the past half only. The projection lines above the table always run to "
             "plan end regardless."
    )
    progress_parser.add_argument(
        "--blocks", action="store_true",
        help="Report intensity per mesocycle instead of per week, single-sport: "
             "per-week rates over each block's completed weeks beside its stated focus, "
             "the block-over-block delta and the structural rows. Replaces the weekly "
             "zone table (the load table stays) and is LONGER than what it replaces."
    )
    currency = progress_parser.add_mutually_exclusive_group()
    currency.add_argument(
        "--power", action="store_true",
        help="Draw the zone tables in power zones instead of choosing by coverage. "
             "No effect on a sport that has only HR."
    )
    currency.add_argument(
        "--hr", action="store_true",
        help="Draw the zone tables in HR zones instead of choosing by coverage. "
             "No effect on a sport that has only power."
    )
    progress_parser.add_argument(
        "--explain", action="store_true",
        help="Append the PMC footnotes (e.g. why TSB lags same-day CTL - ATL)."
    )
    progress_parser.add_argument(
        "--chart", nargs="?", const=True, default=False, metavar="PATH",
        help="Also render the two-panel chart (PMC + weekly load) to a PNG "
             "(default: ./progress.png; requires matplotlib). Additive — the "
             "text output above still prints."
    )

    return progress_parser
