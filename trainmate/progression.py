"""Progress timeline: past (measured) + future (planned) load on one continuous
series, and the CTL/ATL/TSB fitness/fatigue model run across the seam.

Pure functions, no singleton state — same shape as `trainmate/adherence.py` and
`coach/formatting.py`. Rows are passed in by the caller (CLI handler / web
endpoint); nothing here touches `db` directly, so the same computation is
shared verbatim by every front-end (DESIGN_progress_timeline.md §5).

One purity caveat, same as `adherence.py`'s: `garmin.activity_load` reads
`config` thresholds, and importing `trainmate.garmin` imports the `db`
singleton, so tests follow the patch-before-import pattern
(`tests/test_analysis.py` precedent) — the functions themselves are still
deterministic given rows + config.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from trainmate.garmin import activity_load
from trainmate.adherence import planned_load

# Standard impulse-response PMC time constants (Banister via Coggan's
# simplification). Module constants, not config — nobody tunes these in
# practice (DESIGN_progress_timeline.md §4).
CTL_DAYS = 42
ATL_DAYS = 7

DayPoint = Dict[str, Any]  # {date, load, source: 'actual'|'planned', ctl?, atl?, tsb?}
#                            tsb is day-ENTERING form: CTL_{d-1} - ATL_{d-1}


def _to_date(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _date_str(d) -> str:
    return d.strftime("%Y-%m-%d")


def _monday(d):
    return d - timedelta(days=d.weekday())


def plan_end(workouts: List[Dict[str, Any]]) -> Optional[str]:
    """Last non-removed workout date, or None if there are no workouts at all
    (DESIGN_progress_timeline.md §3 'Plan end'). The projection never runs past
    this date — no zero-fill ghost line."""
    dates = [w["date"] for w in workouts if not w.get("removed")]
    return max(dates) if dates else None


def _window_end(workouts: List[Dict[str, Any]], today: str) -> str:
    """Last day the merged series should cover: `today` when there's no plan
    or the plan has already lapsed, else the plan-end date."""
    end = plan_end(workouts)
    return end if (end and end > today) else today


def _group_by_date(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["date"], []).append(r)
    return grouped


def daily_loads(
    activities: List[Dict[str, Any]],
    workouts: List[Dict[str, Any]],
    today: str,
) -> List[DayPoint]:
    """Merged per-day load series (DESIGN_progress_timeline.md §3): past days
    are measured load (`garmin.activity_load` over `completed_activities`),
    future days are planned load (`adherence.planned_load` over non-removed
    `workouts`), today is actual if any completed activity with load > 0
    exists, else planned. Runs from the first activity date through plan end
    (or today, if there's no plan or it's already lapsed) with no gaps —
    zero-load days are included, not skipped.

    Empty when there are no completed activities at all (nothing to seed a
    series from) — the "no activity history yet" empty state."""
    if not activities:
        return []

    start = min(a["date"] for a in activities)
    end = _window_end(workouts, today)

    acts_by_date = _group_by_date(activities)
    workouts_by_date = _group_by_date([w for w in workouts if not w.get("removed")])

    points: List[DayPoint] = []
    d = _to_date(start)
    end_d = _to_date(end)
    while d <= end_d:
        date_str = _date_str(d)
        day_acts = acts_by_date.get(date_str, [])
        if date_str < today:
            load = sum(activity_load(a) for a in day_acts)
            source = "actual"
        elif date_str == today and any(activity_load(a) > 0 for a in day_acts):
            load = sum(activity_load(a) for a in day_acts)
            source = "actual"
        else:
            load = sum(planned_load(w) for w in workouts_by_date.get(date_str, []))
            source = "planned"
        points.append({"date": date_str, "load": load, "source": source})
        d += timedelta(days=1)
    return points


def fitness_series(day_points: List[DayPoint]) -> List[DayPoint]:
    """Folds the CTL/ATL/TSB recursion (§4) over `daily_loads`' output.

    Seeded at the calendar mean daily load of the first 42 (CTL) / 7 (ATL)
    days of the series — zero-load days count in the numerator and stay in
    the denominator (less history than that: mean over what exists). This is
    the value the recursion itself converges to; seeding at 0 would assume an
    untrained athlete at data start and take ~42 days to decay away."""
    if not day_points:
        return []

    ctl_window = day_points[:CTL_DAYS]
    atl_window = day_points[:ATL_DAYS]
    ctl = sum(p["load"] for p in ctl_window) / len(ctl_window)
    atl = sum(p["load"] for p in atl_window) / len(atl_window)

    out: List[DayPoint] = []
    for p in day_points:
        tsb = ctl - atl  # form entering this day, before today's load lands
        ctl = ctl + (p["load"] - ctl) / CTL_DAYS
        atl = atl + (p["load"] - atl) / ATL_DAYS
        out.append({**p, "ctl": ctl, "atl": atl, "tsb": tsb})
    return out


def zero_load_workout_count(workouts: List[Dict[str, Any]]) -> int:
    """Count of non-removed, non-rest planned workouts with neither `tss` nor
    `rpe`+`duration_minutes` set, so `planned_load` falls back to 0 (§3). The
    caller folds this into a payload/CLI warning ("N planned workouts have
    neither TSS nor RPE and count as 0 load") — a legitimate rest day is
    excluded since it has no load by design, not by data gap."""
    return sum(
        1 for w in workouts
        if not w.get("removed") and w.get("sport_type") != "rest"
        and planned_load(w) == 0
    )


def _week_meso(week_start, week_dates: List[str], meso_spans: List[Dict[str, Any]]):
    """Majority-overlap mesocycle label/source for one Monday-aligned week
    (§6.1): the block covering the most of the week's 7 days wins; a tie
    favors the later block (spans must be given in chronological order, so a
    later match on an equal count overwrites the earlier one)."""
    best = None
    best_count = 0
    for span in meso_spans:
        count = sum(1 for d in week_dates if span["start_date"] <= d <= span["end_date"])
        if count and count >= best_count:
            best_count = count
            best = span
    if best is None:
        return None, None
    return best["label"], best["source"]


def meso_bands(
    mesocycles: List[Dict[str, Any]],
    inferred_mesocycles: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Builds the layered mesocycle band list (§6.1) from already-resolved
    rows: `mesocycles` is the plan's mesocycles for the weeks they actually
    governed (the caller resolves which macrocycle *version* governed a given
    past week — that needs db + the governing-objective choice, so it isn't
    done here); `inferred_mesocycles` are the bootstrap reconstruction's
    blocks from `analysis_cache["long"]`, rendered `~`-prefixed/`inferred`."""
    bands = []
    for m in sorted(inferred_mesocycles, key=lambda m: m["start_date"]):
        bands.append({
            "label": f"~{m['name']}",
            "source": "inferred",
            "start_date": m["start_date"],
            "end_date": m["end_date"],
        })
    for m in sorted(mesocycles, key=lambda m: m["start_date"]):
        bands.append({
            "label": m["name"],
            "source": "plan",
            "start_date": m["start_date"],
            "end_date": m["end_date"],
        })
    return bands


def weekly_aggregates(
    activities: List[Dict[str, Any]],
    workouts: List[Dict[str, Any]],
    today: str,
    meso_spans: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Monday-commencing weekly planned-vs-actual load (§5/§6), one dict per
    week from the earliest activity/workout date through plan end (or today):

        {week_commencing, planned_load, actual_load, in_progress,
         planned_load_elapsed?, meso_label, meso_source}

    `planned_load` (Sigma `adherence.planned_load` over non-removed workouts —
    the *adapted* plan, "what the plan asked at the time") is None for a week
    with no governing plan mesocycle (`meso_source != 'plan'`) — matching
    `adherence.py`'s covered_ranges precedent: activity outside planned
    coverage is informational, not a deviation, so no planned figure/percentage
    is shown for it. The in-progress week (current week) additionally carries
    `planned_load_elapsed`, the Monday-through-today slice, so a partial week
    doesn't read as poor adherence every Monday (§3)."""
    non_removed_workouts = [w for w in workouts if not w.get("removed")]
    dates = [a["date"] for a in activities] + [w["date"] for w in non_removed_workouts]
    if not dates:
        return []

    start = min(dates)
    end = _window_end(workouts, today)
    week_start = _monday(_to_date(start))
    end_d = _to_date(end)

    acts_by_date = _group_by_date(activities)
    workouts_by_date = _group_by_date(non_removed_workouts)

    weeks: List[Dict[str, Any]] = []
    w_start = week_start
    while w_start <= end_d:
        week_dates = [_date_str(w_start + timedelta(days=i)) for i in range(7)]
        in_progress = week_dates[0] <= today <= week_dates[-1]

        actual_load = sum(
            activity_load(a) for d in week_dates for a in acts_by_date.get(d, [])
        )
        week_workouts = [w for d in week_dates for w in workouts_by_date.get(d, [])]

        meso_label, meso_source = _week_meso(w_start, week_dates, meso_spans)
        governed = meso_source == "plan"

        week: Dict[str, Any] = {
            "week_commencing": week_dates[0],
            "actual_load": actual_load,
            "in_progress": in_progress,
            "meso_label": meso_label,
            "meso_source": meso_source,
        }
        if governed:
            week["planned_load"] = sum(planned_load(w) for w in week_workouts)
            if in_progress:
                week["planned_load_elapsed"] = sum(
                    planned_load(w) for w in week_workouts if w["date"] <= today
                )
        else:
            week["planned_load"] = None

        weeks.append(week)
        w_start += timedelta(days=7)

    return weeks
