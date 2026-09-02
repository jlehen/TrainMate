"""Progress timeline: past (measured) + future (planned) load on one continuous
series, and the CTL/ATL/TSB fitness/fatigue model run across the seam.

Pure functions, no singleton state — same shape as `trainmate/adherence.py` and
`coach/formatting.py`. Rows are passed in by the caller (CLI handler / web
endpoint); nothing here touches `db` directly, so the same computation is
shared verbatim by every front-end (DESIGN_progress_timeline.md §5).

The past half is **read, not recomputed** (§4): CTL/ATL/TSB for days before today
come verbatim from the stored `athlete_metrics_cache` rows that
`garmin.compute_pmc` already wrote, so `tm progress` and `tm status` never
disagree about the same day's fitness. The future half is an *anchored fold*: the
same recurrence folded forward from the latest stored row over the merged daily
loads — measured past → planned future — which is exactly the PMC design's
deferred Phase 2 projection, generalized to the full daily series.

One purity caveat, same as `adherence.py`'s: `garmin.activity_load` reads
`config` thresholds, and importing `trainmate.garmin` imports the `db`
singleton, so tests follow the patch-before-import pattern
(`tests/test_analysis.py` precedent) — the functions themselves are still
deterministic given rows + config.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from trainmate import garmin, intensity
from trainmate.garmin import activity_load
from trainmate.adherence import planned_load
from trainmate.sports import canonical_sport

DayPoint = Dict[str, Any]  # {date, load, source: 'actual'|'planned',
#                            ctl, atl, tsb: float | None}
#   ctl/atl/tsb are None inside the warm-up window, on days with no stored
#   metrics row, and everywhere when there is no anchor (§4).
#   tsb is day-ENTERING form: CTL_{d-1} - ATL_{d-1}


def _to_date(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _date_str(d) -> str:
    return d.strftime("%Y-%m-%d")


def _monday(d):
    return d - timedelta(days=d.weekday())


def _plural(n: int) -> str:
    return "s" if n != 1 else ""


def _days_between(start: str, end: str) -> int:
    """Whole days from `start` to `end`, negative when `end` precedes it. The same
    arithmetic as `util.days_between`, kept here so this module stays import-light."""
    return (_to_date(end) - _to_date(start)).days


def plan_end(workouts: List[Dict[str, Any]]) -> Optional[str]:
    """Last non-removed **generated** workout date (DESIGN_progress_timeline.md §3
    'Plan end'). The projection runs exactly to here and stops — no zero-fill ghost
    line past it.

    Manual rows (`workout add`) count toward daily loads *inside* the range but
    never *extend* it: one far-future race-day placeholder would otherwise drag the
    projection over months of assumed rest. Fallback for a fully-manual DB (no
    generated workouts at all): the last non-removed workout of any source. None
    when there are no non-removed workouts at all."""
    generated = [
        w["date"] for w in workouts
        if not w.get("removed") and w.get("source") == "generated"
    ]
    if generated:
        return max(generated)
    any_non_removed = [w["date"] for w in workouts if not w.get("removed")]
    return max(any_non_removed) if any_non_removed else None


def _window_end(workouts: List[Dict[str, Any]], today: str) -> str:
    """Last day the merged series should cover: `today` when there's no plan or the
    plan has already lapsed (plan end < today), else the plan-end date (§2's
    `max(today, plan end)`)."""
    end = plan_end(workouts)
    return end if (end and end > today) else today


def _series_start(
    activities: List[Dict[str, Any]], workouts: List[Dict[str, Any]]
) -> Optional[str]:
    """First day of the merged series: min(first activity, first planned workout)
    (§5/§6.0). With no activities but a plan already generated, "first activity" is
    undefined, so the plan's first workout seeds the series instead — the weekly
    bars still render the plan about to start (§3 empty state)."""
    dates = [a["date"] for a in activities]
    dates += [w["date"] for w in workouts if not w.get("removed")]
    return min(dates) if dates else None


def _history_start(
    activities: List[Dict[str, Any]], metrics_rows: List[Dict[str, Any]]
) -> Optional[str]:
    """Earliest Garmin evidence — min(first activity, first metrics row), mirroring
    `garmin.pmc_history_start` but over the rows the caller already handed in (so the
    young-DB caveat stays row-in/row-out, §4)."""
    dates = [a["date"] for a in activities] + [m["date"] for m in metrics_rows]
    return min(dates) if dates else None


def _group_by_date(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["date"], []).append(r)
    return grouped


def daily_loads(
    activities: List[Dict[str, Any]],
    workouts: List[Dict[str, Any]],
    today: str,
    *,
    window_end: Optional[str] = None,
) -> List[DayPoint]:
    """Merged per-day load series (DESIGN_progress_timeline.md §3): past days are
    measured load (`garmin.activity_load` over `completed_activities`), future days
    are planned load (`adherence.planned_load` over non-removed `workouts`), today is
    actual if any completed activity **with load > 0** exists, else planned. Runs from
    min(first activity, first planned workout) through plan end (or today, if there's
    no plan or it's already lapsed) with no gaps — zero-load days are included.

    Empty only when there is neither an activity nor a planned workout to seed a
    series from."""
    start = _series_start(activities, workouts)
    if start is None:
        return []
    end = window_end if window_end is not None else _window_end(workouts, today)

    acts_by_date = _group_by_date(activities)
    workouts_by_date = _group_by_date([w for w in workouts if not w.get("removed")])

    points: List[DayPoint] = []
    d = _to_date(start)
    end_d = _to_date(end)
    while d <= end_d:
        date_str = _date_str(d)
        day_acts = acts_by_date.get(date_str, [])
        # Measured load computed once (activity_load is non-negative, so >0 ≡ the
        # "any completed activity with load" test of the §3 today-rule).
        actual = sum(activity_load(a) for a in day_acts) if day_acts else 0.0
        if date_str < today or (date_str == today and actual > 0):
            load, source = actual, "actual"
        else:
            load = sum(planned_load(w) for w in workouts_by_date.get(date_str, []))
            source = "planned"
        points.append({"date": date_str, "load": load, "source": source})
        d += timedelta(days=1)
    return points


def _anchor(
    metrics_rows: List[Dict[str, Any]], today: str
) -> Optional[Tuple[str, float, float]]:
    """The projection anchor (§4): the latest stored metrics row dated **strictly
    before today** whose `ctl`/`atl` are both non-NULL, as `(date, ctl, atl)`.

    *Strictly before today* — a morning auto-ensure pull writes today's row (load 0)
    before the evening session; anchoring on it would drop today's planned session.
    *Non-NULL* — a pull that died before `recompute_derived()` leaves trailing NULL
    PMC rows; seeding from one is a TypeError, so skip back to the newest valid row.
    None when no such row exists (the no-anchor / young-DB state)."""
    best: Optional[Tuple[str, float, float]] = None
    for m in metrics_rows:  # sorted date ASC
        if m["date"] >= today:
            break
        if m.get("ctl") is not None and m.get("atl") is not None:
            best = (m["date"], float(m["ctl"]), float(m["atl"]))
    return best


def fitness_series(
    day_points: List[DayPoint],
    metrics_rows: List[Dict[str, Any]],
    today: str,
    ctl_days: int,
    atl_days: int,
    warmup_cutoff: Optional[str],
) -> List[DayPoint]:
    """Attaches CTL/ATL/TSB to each `daily_loads` point (DESIGN_progress_timeline.md
    §4). **Never recomputes the past.**

    - Past days (`date < today`): `ctl`/`atl`/`tsb` copied verbatim from the stored
      metrics row for that date, blanked to None before `warmup_cutoff`
      (`garmin.pmc_display_values` semantics). A past day with no stored row carries
      no PMC point (renderers join the line across the gap).
    - From the anchor (latest stored row strictly before today with non-NULL PMC):
      the same recurrence is folded forward via `garmin.compute_pmc(seed=(ctl_A,
      atl_A))` over the merged loads — actual for anchor+1..yesterday, the §3 rule for
      today, planned beyond — through plan end. Full-precision storage makes this fold
      reproduce the stored series bit-exactly, so today's fold equals the stored
      today-row whenever today's load has synced (§7.1 consistency contract).
    - No anchor → every point's PMC is None (the panel is suppressed by the renderer).

    Time constants come from config (`ctl_days`/`atl_days`), matching the stored past,
    so the folded future never kinks at the seam. The caller fetches `metrics_rows`,
    the config τs and the cutoff — this stays row-in/row-out."""
    metrics_by_date = {m["date"]: m for m in metrics_rows}
    anchor = _anchor(metrics_rows, today)
    folded: Dict[str, Tuple[float, float, float]] = {}
    if anchor and day_points:
        anchor_date, ctl_a, atl_a = anchor
        loads = {p["date"]: p["load"] for p in day_points}
        fold_start = _date_str(_to_date(anchor_date) + timedelta(days=1))
        fold_end = day_points[-1]["date"]
        if fold_start <= fold_end:
            folded = garmin.compute_pmc(
                loads, fold_start, fold_end, ctl_days, atl_days,
                seed=(ctl_a, atl_a),
            )

    out: List[DayPoint] = []
    for p in day_points:
        date = p["date"]
        ctl = atl = tsb = None
        if anchor and date > anchor[0]:
            # Anchor+1 onward (incl. today and the whole projection) — the fold.
            vals = folded.get(date)
            if vals is not None:
                ctl, atl, tsb = vals
        elif date < today:
            # Stored past, blanked inside the warm-up window; None if no row.
            m = metrics_by_date.get(date)
            if m is not None:
                ctl, atl, tsb = garmin.pmc_display_values(m, warmup_cutoff)
        out.append({**p, "ctl": ctl, "atl": atl, "tsb": tsb})
    return out


def zero_load_workout_count(workouts: List[Dict[str, Any]], today: str) -> int:
    """Count of non-removed, non-rest planned workouts **from today on** that value to
    0 load — no usable TSS and no RPE+duration, so `planned_load` falls back to 0 (§3).

    Dated from today because the warning is about rows feeding the *projection*:
    counting the whole table left the banner permanently lit by history nobody can
    fix (CODE_REVIEW finding, 'the stale-workout warning never heals')."""
    return sum(
        1 for w in workouts
        if not w.get("removed") and w.get("sport_type") != "rest"
        and w["date"] >= today and planned_load(w) == 0
    )


def _valid_span(start: Any, end: Any) -> bool:
    """Whether an inferred (LLM-authored) mesocycle block has parseable dates with
    start <= end — the guard for §6.1's 'unparseable dates are skipped'."""
    try:
        return bool(start) and bool(end) and _to_date(start) <= _to_date(end)
    except (ValueError, TypeError):
        return False


def _subtract_spans(
    span: Tuple[str, str], cutters: List[Tuple[str, str]]
) -> List[Tuple[str, str]]:
    """Date-interval subtraction (inclusive): removes each cutter from `span`,
    returning the remaining sub-intervals (0, 1, or more). Used to trim inferred
    bands where plan bands cover part of them (§6.1 band trimming)."""
    pieces = [span]
    for cs, ce in cutters:
        next_pieces: List[Tuple[str, str]] = []
        for s, e in pieces:
            if ce < s or cs > e:  # no overlap
                next_pieces.append((s, e))
                continue
            if s < cs:  # left remainder, up to the day before the cutter
                next_pieces.append((s, _date_str(_to_date(cs) - timedelta(days=1))))
            if ce < e:  # right remainder, from the day after the cutter
                next_pieces.append((_date_str(_to_date(ce) + timedelta(days=1)), e))
        pieces = next_pieces
    return pieces


def meso_bands(
    mesocycles: List[Dict[str, Any]],
    inferred_mesocycles: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The layered mesocycle band list (§6.1), most authoritative first, with no
    overlaps in the output:

    - **Plan bands** — the governing objective's mesocycles (the caller resolves
      which plan governs via `db.get_governing_macrocycle`), labelled by name.
    - **Inferred bands** — the bootstrap reconstruction's blocks, `~`-prefixed and
      `source='inferred'`. Blocks with unparseable dates are skipped; the survivors
      are trimmed to the parts no plan band covers, and dropped where fully covered
      (plan wins). The payload therefore never contains overlapping bands — renderers
      draw spans as given."""
    plan_bands = [
        {"label": m["name"], "source": "plan",
         "start_date": m["start_date"], "end_date": m["end_date"]}
        for m in sorted(mesocycles, key=lambda m: m["start_date"])
    ]
    plan_spans = [(b["start_date"], b["end_date"]) for b in plan_bands]

    inferred_bands: List[Dict[str, Any]] = []
    for m in sorted(
        (m for m in inferred_mesocycles if _valid_span(m.get("start_date"), m.get("end_date"))),
        key=lambda m: m["start_date"],
    ):
        for s, e in _subtract_spans((m["start_date"], m["end_date"]), plan_spans):
            inferred_bands.append({
                "label": f"~{m['name']}", "source": "inferred",
                "start_date": s, "end_date": e,
            })

    return sorted(plan_bands + inferred_bands, key=lambda b: b["start_date"])


def _load_by_sport(
    items: List[Dict[str, Any]], load_fn, sport_field: str
) -> Dict[str, float]:
    """Sums `load_fn(item)` per canonical sport (DESIGN_block_progress.md §3.3) — the
    same bucketing `intensity.sport_durations` uses, so a week's per-sport load and its
    per-sport duration never disagree about which sport an item belongs to.

    `sport_field` differs by row type and is the caller's job to get right: a
    completed activity's sport lives in `activity_type` (the raw `completed_activities`
    column), a planned workout's in `sport_type` — the same split `intensity.py`'s
    sport-keyed helpers already draw between the two row shapes."""
    by_sport: Dict[str, float] = {}
    for item in items:
        sport = canonical_sport(item.get(sport_field) or "unknown")
        by_sport[sport] = by_sport.get(sport, 0.0) + load_fn(item)
    return by_sport


def _week_meso(week_dates: List[str], meso_spans: List[Dict[str, Any]]):
    """Majority-overlap mesocycle label/source for one Monday-aligned week (§6.1):
    the block covering the most of the week's 7 days wins; a tie favors the later
    block (spans are given chronological, so a later match on an equal count
    overwrites the earlier one)."""
    best = None
    best_count = 0
    for span in meso_spans:
        count = sum(
            1 for d in week_dates if span["start_date"] <= d <= span["end_date"]
        )
        if count and count >= best_count:
            best_count = count
            best = span
    if best is None:
        return None, None
    return best["label"], best["source"]


def weekly_aggregates(
    activities: List[Dict[str, Any]],
    workouts: List[Dict[str, Any]],
    today: str,
    meso_spans: List[Dict[str, Any]],
    *,
    window_end: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Monday-commencing weekly planned-vs-actual load (§5/§6), one dict per week from
    the earliest activity/workout date through plan end (or today):

        {week_commencing, planned_load, planned_load_by_sport, planned_load_elapsed?,
         planned_load_elapsed_by_sport?, partial_plan?, in_progress, actual_load,
         actual_load_by_sport, meso_label, meso_source, zone_rows, sport_seconds,
         judged_sport_seconds, load_sparse, planned_zone_rows}

    The `_by_sport` dicts (canonical sport -> load) are the same totals bucketed by
    `sports.canonical_sport`, so a caller can tell WHICH sport drove a week's gap
    without re-deriving it from the activity/workout rows itself — the raw material
    for the gap annotation in DESIGN_block_progress.md §3.3. Bucketed on
    `activity_type` for `actual_load_by_sport` (the raw `completed_activities` column)
    and on `sport_type` for the two planned dicts — the same split `intensity.py`'s
    sport-keyed helpers already draw between the two row shapes.

    `planned_load` (Σ `adherence.planned_load` over non-removed workouts — the
    *adapted* plan, "what the plan asked at the time") is None for a week the plan
    never covered, matching `adherence.py`'s precedent that activity outside planned
    coverage is informational, not a deviation.

    `partial_plan` marks a week the plan covers only *part* of: it began or ended
    mid-week, so its planned total spans fewer days than its actual does and no
    honest percentage can be formed from the pair (§3 'comparable days'). The
    current (in-progress) week additionally carries `planned_load_elapsed`, the
    Monday-through-elapsed slice — today included only once its load has synced —
    so a partial week doesn't read as poor adherence every Monday."""
    non_removed_workouts = [w for w in workouts if not w.get("removed")]
    start = _series_start(activities, non_removed_workouts)
    if start is None:
        return []
    end = window_end if window_end is not None else _window_end(workouts, today)
    week_start = _monday(_to_date(start))
    end_d = _to_date(end)

    # The span the plan speaks for. A week only partly inside it compares a partial
    # planned total against a whole week of training (§3).
    plan_dates = [w["date"] for w in non_removed_workouts]
    plan_first = min(plan_dates) if plan_dates else None
    plan_last = plan_end(workouts)

    acts_by_date = _group_by_date(activities)
    workouts_by_date = _group_by_date(non_removed_workouts)

    weeks: List[Dict[str, Any]] = []
    w_start = week_start
    while w_start <= end_d:
        week_dates = [_date_str(w_start + timedelta(days=i)) for i in range(7)]
        week_mon, week_sun = week_dates[0], week_dates[-1]
        in_progress = week_mon <= today <= week_sun

        week_acts = [a for d in week_dates for a in acts_by_date.get(d, [])]
        actual_load = sum(activity_load(a) for a in week_acts)
        actual_load_by_sport = _load_by_sport(week_acts, activity_load, "activity_type")
        week_workouts = [w for d in week_dates for w in workouts_by_date.get(d, [])]

        meso_label, meso_source = _week_meso(week_dates, meso_spans)

        week: Dict[str, Any] = {
            "week_commencing": week_mon,
            "actual_load": actual_load,
            "actual_load_by_sport": actual_load_by_sport,
            "in_progress": in_progress,
            "meso_label": meso_label,
            "meso_source": meso_source,
            # The intensity half of the same rows (DESIGN_intensity_distribution.md §9.6).
            # Joined here rather than fetched again: this function already holds every
            # activity bucketed by week, and `render_progress` is handed one payload and
            # reads no database — the property the one-payload rule exists to protect.
            "zone_rows": intensity.zone_rows(week_acts),
            "sport_seconds": intensity.sport_durations(week_acts),
            # The same durations over sessions big enough to grade: what the "trained but
            # nothing recorded" `!` reads, so the floor applies there too (§11).
            "judged_sport_seconds": intensity.sport_durations(
                [a for a in week_acts if intensity.judgeable(a)]
            ),
            # The athlete trained normally, the strap died, and no RPE was entered — so
            # the week's own LOAD is undercounted and reads as an adherence miss the
            # coach will then adapt the plan around. A `progress` defect that predates
            # the zone tables (DESIGN_intensity_distribution.md §11). Only sessions big
            # enough to hide material load count: a 5-minute mobility session with a
            # cold strap lit this on two thirds of a real athlete's weeks.
            "load_sparse": any(
                garmin.load_method(a) == "hr_sparse" for a in week_acts
                if intensity.judgeable(a)
            ),
            # The future half of the zone table: what the plan PRESCRIBES per zone, ghost
            # rows under today exactly like the load table's ghost bars
            # (DESIGN_intensity_distribution.md §9.8). Empty for every week planned
            # before those columns existed — the rolling horizon rewrites the future on
            # each generation, so nothing needs backfilling.
            "planned_zone_rows": intensity.planned_zone_rows(week_workouts),
        }
        if week_workouts:
            week["planned_load"] = sum(planned_load(w) for w in week_workouts)
            week["planned_load_by_sport"] = _load_by_sport(
                week_workouts, planned_load, "sport_type"
            )
            # Elapsed = Mon..yesterday, plus today only once its load has synced
            # (today's §3 source is 'actual'). Including an unfinished today would
            # make an evening athlete read <100% all day (§3).
            if in_progress:
                today_synced = any(
                    activity_load(a) > 0 for a in acts_by_date.get(today, [])
                )
                elapsed_end = today if today_synced else (
                    _date_str(_to_date(today) - timedelta(days=1))
                )
                elapsed_workouts = [w for w in week_workouts if w["date"] <= elapsed_end]
                week["planned_load_elapsed"] = sum(
                    planned_load(w) for w in elapsed_workouts
                )
                week["planned_load_elapsed_by_sport"] = _load_by_sport(
                    elapsed_workouts, planned_load, "sport_type"
                )
            else:
                elapsed_end = week_sun
            # Comparable only if the plan speaks for every day already trained: the
            # week the plan *starts* otherwise divides three planned days by seven
            # trained ones and reads 477% (§3).
            if elapsed_end >= week_mon and not (
                plan_first is not None and plan_first <= week_mon
                and plan_last is not None and plan_last >= elapsed_end
            ):
                week["partial_plan"] = True
        else:
            week["planned_load"] = None

        weeks.append(week)
        w_start += timedelta(days=7)

    return weeks


def week_plan_denom(week: Dict[str, Any]) -> Optional[float]:
    """The planned figure a week's bar and percentage compare against (§3 'comparable
    days'): the elapsed slice for the in-progress week, the full planned total
    otherwise, None for a week no plan covered.

    Lives here rather than in one renderer because §3 is a payload rule, not a layout
    one: the CLI table and the PNG both read it, and the PNG reading `planned_load`
    directly is exactly how the two surfaces disagreed about the same week."""
    if week.get("planned_load") is None:
        return None
    if week.get("in_progress"):
        return week.get("planned_load_elapsed", 0.0)
    return week["planned_load"]


def week_plan_denom_by_sport(week: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """The per-sport counterpart of `week_plan_denom`: same elapsed-vs-full rule, but
    keyed by canonical sport. None for a week no plan covered, matching the scalar."""
    if week.get("planned_load") is None:
        return None
    if week.get("in_progress"):
        return week.get("planned_load_elapsed_by_sport", {})
    return week.get("planned_load_by_sport", {})


def plan_gap(
    objectives: List[Dict[str, Any]], plan_end_date: Optional[str]
) -> Optional[Tuple[Dict[str, Any], int]]:
    """The next active objective the plan doesn't yet reach, and how many whole weeks
    short of it the plan ends (§3), as `(objective, weeks_before)` — or None when there
    is no plan or every active objective is already reached.

    The single source of the plan-gap derivation: `assemble_timeline` puts it on the
    payload as the structured `plan_gap` field and each surface words it itself (§6.0).
    Computing it once here keeps the two surfaces from diverging on *when* the gap fires
    or *by how much* — the CODE_REVIEW #5 class of drift."""
    if plan_end_date is None:
        return None
    # Not-called-off is the only status question here: the `target_date > plan_end_date`
    # filter below already excludes everything behind the athlete (§12).
    live = sorted(
        (o for o in objectives if o.get("status") != "archived"),
        key=lambda o: str(o["target_date"]),
    )
    next_obj = next((o for o in live if o["target_date"] > plan_end_date), None)
    if next_obj is None:
        return None
    weeks_before = max(
        0, round((_to_date(next_obj["target_date"]) - _to_date(plan_end_date)).days / 7)
    )
    return next_obj, weeks_before


# --- End-of-runway detection (DESIGN_runway_nudge.md §2) ---
# The shapes the end of the schedule can take. Surfaces dispatch on these rather than on
# the wording, the same contract `_warning`'s `code` gives the payload banners.
RUNWAY_BLOCK = "block"
RUNWAY_SPAN = "span"
RUNWAY_PLAN_END_NEXT_GOAL = "plan_end_next_goal"
RUNWAY_PLAN_END_NO_GOAL = "plan_end_no_goal"


def coverage_end(workouts: List[Dict[str, Any]]) -> Optional[str]:
    """The last date the generated schedule covers — planned rest rows included, manual
    rows excluded (DESIGN_runway_nudge.md §2). None when nothing was ever generated.

    Deliberately not `plan_end`, which falls back to manual rows for a fully-manual
    database: a race put on the calendar by hand weeks out must not make the schedule
    look as if it reaches that far while the generated sessions end next Thursday.

    No margin and no guessing at a quiet tail: §2.1's coverage invariant makes every date
    of a generated span carry a row, so the last covered date is read straight off them."""
    dates = [
        w["date"] for w in workouts
        if not w.get("removed") and w.get("source") == "generated"
    ]
    return max(dates) if dates else None


def generation_span_end(
    span_start: str, block_end: Optional[str], span_days: int
) -> str:
    """Where an unselected generation span stops: the config cap, or the end of the block
    containing its start, whichever comes first (DESIGN_cli_selectors.md §8).

    Pure over the one block row the caller fetched, like `runway` over its rows, so the
    CLI and the unattended plan-then-generate path cannot clamp differently. With no block
    covering the start the cap stands alone."""
    cap_end = _date_str(_to_date(span_start) + timedelta(days=span_days - 1))
    return min(cap_end, block_end) if block_end else cap_end


def runway(
    workouts: List[Dict[str, Any]],
    mesocycles: List[Dict[str, Any]],
    objectives: List[Dict[str, Any]],
    today: str,
    warning_days: int,
) -> Optional[Dict[str, Any]]:
    """The end-of-schedule fact, or None when nothing fires (DESIGN_runway_nudge.md §2).

    Pure over rows the caller fetched, following `plan_gap`'s contract: every surface
    words the same structured answer itself, so `status`, `workout adapt` and the morning
    push cannot diverge on *when* the schedule runs out or on which command fixes it (§3).

    `mesocycles` are the blocks of the plan the current workouts implement, whichever
    goal it was drawn for. `warning_days` is `config.runway_warning_days`; it bounds both
    the run-up and the passed-state window (§7).

    Returns `{last_covered_date, days_left, kind, plan_end}`, plus `next_mesocycle` on a
    block cliff and `objective`/`weeks_before` on a plan cliff with a goal beyond it.
    `days_left` is negative once the cliff is behind the athlete."""
    last_covered = coverage_end(workouts)
    ends = [str(m["end_date"]) for m in mesocycles if m.get("end_date")]
    if last_covered is None or not ends:
        return None
    plan_end_date = max(ends)
    days_left = _days_between(today, last_covered)

    # The run-up window, then the passed state — still worth saying for `warning_days`
    # after the plan's own end, which is the morning the wrap-up matters most (§2).
    if days_left > warning_days:
        return None
    if (days_left < 0
            and _days_between(max(last_covered, plan_end_date), today) > warning_days):
        return None

    state: Dict[str, Any] = {
        "last_covered_date": last_covered,
        "days_left": days_left,
        "plan_end": plan_end_date,
    }

    # A periodization wholly behind today is a plan cliff whatever the sessions did:
    # there is nothing left to generate towards, which is the same judgement `workout
    # adapt` refuses on (§4). Otherwise the comparison is the exact one the coverage
    # invariant makes possible.
    if plan_end_date >= today and last_covered < plan_end_date:
        blocks = sorted(mesocycles, key=lambda m: str(m["start_date"]))
        next_block = next(
            (m for m in blocks if str(m["start_date"]) > last_covered), None
        )
        ends_a_block = any(str(m["end_date"]) == last_covered for m in mesocycles)
        if ends_a_block and next_block is not None:
            return {**state, "kind": RUNWAY_BLOCK, "next_mesocycle": next_block}
        return {**state, "kind": RUNWAY_SPAN}

    # Fed the mesocycle-derived plan end, not `plan_end`: the question here is whether the
    # PERIODIZATION reaches a goal, so a span cliff cannot read as a goal gap (§2).
    gap = plan_gap(objectives, plan_end_date)
    if gap is None:
        return {**state, "kind": RUNWAY_PLAN_END_NO_GOAL}
    return {
        **state, "kind": RUNWAY_PLAN_END_NEXT_GOAL,
        "objective": gap[0], "weeks_before": gap[1],
    }


def _warning(code: str, text: str, command: Optional[str] = None) -> Dict[str, Any]:
    """One payload warning. `code` is what renderers dispatch on and `command` is what
    a surface may style as a call to action — so nobody has to recognise a warning by
    matching its prose (§6.0)."""
    w: Dict[str, Any] = {"code": code, "text": text}
    if command:
        w["command"] = command
    return w


def assemble_timeline(
    activities: List[Dict[str, Any]],
    workouts: List[Dict[str, Any]],
    metrics_rows: List[Dict[str, Any]],
    mesocycles: List[Dict[str, Any]],
    inferred_mesocycles: List[Dict[str, Any]],
    objectives: List[Dict[str, Any]],
    today: str,
    ctl_days: int,
    atl_days: int,
    warmup_cutoff: Optional[str],
) -> Dict[str, Any]:
    """The ENTIRE §6.0 timeline payload — days, weeks, meso_bands, objectives,
    plan_gap, warnings — built here and ONLY here, from the §5 helpers plus the §6.1
    layered lookup. Callers do db reads and hand rows in; neither the CLI handler nor
    the web endpoint owns any assembly or warning-wording logic, so the two surfaces
    render one payload (the fix for the rev-4 divergence, CODE_REVIEW #5).

    `objectives` are ALL active AND completed objectives, unfiltered — a race weeks
    ago still gets its flag; renderers clip to their window. Stays row-in/row-out.
    """
    warnings: List[Dict[str, Any]] = []

    if not activities:
        # §3 empty state: no past series and no PMC, but the planned future still
        # renders (weekly bars); every surface shows this.
        warnings.append(_warning(
            "no_history", "no activity history yet — run `data pull` first",
            command="data pull",
        ))

    valid_inferred = [
        m for m in inferred_mesocycles
        if _valid_span(m.get("start_date"), m.get("end_date"))
    ]
    skipped = len(inferred_mesocycles) - len(valid_inferred)
    if skipped:
        warnings.append(_warning(
            "bootstrap_dates",
            f"{skipped} bootstrap mesocycle block{_plural(skipped)} skipped "
            f"— unparseable dates",
        ))
    bands = meso_bands(mesocycles, valid_inferred)

    # One plan-end scan for the whole payload: `end` (payload `plan_end`, or None) and
    # the merged-series window derived from it, threaded into both series builders so
    # they don't each re-walk the workouts (mirrors `_window_end`'s rule).
    end = plan_end(workouts)
    window_end = end if (end and end > today) else today

    day_points = daily_loads(activities, workouts, today, window_end=window_end)
    days = fitness_series(
        day_points, metrics_rows, today, ctl_days, atl_days, warmup_cutoff
    )

    weeks = weekly_aggregates(
        activities, workouts, today, bands, window_end=window_end
    )

    zero_count = zero_load_workout_count(workouts, today)
    if zero_count:
        warnings.append(_warning(
            "zero_load_workouts",
            f"{zero_count} planned workout{_plural(zero_count)} lack TSS/RPE "
            f"— count as 0",
        ))

    gap = None
    if end is not None:
        beyond = [
            w for w in workouts if not w.get("removed") and w["date"] > end
        ]
        if beyond:
            warnings.append(_warning(
                "beyond_plan_end",
                f"{len(beyond)} workout{_plural(len(beyond))} beyond plan end "
                f"— not projected",
            ))
        gap = plan_gap(objectives, end)

    history_start = _history_start(activities, metrics_rows)
    caveat = garmin.pmc_data_caveat(history_start, as_of=today)
    if caveat:
        warnings.append(_warning(
            "pmc_warming",
            f"PMC still warming: CTL based on {caveat['n_days']} days of history",
        ))

    plan_dates = [w["date"] for w in workouts if not w.get("removed")]
    return {
        "today": today,
        # Both edges: a plan that begins or ends mid-week leaves that week's planned
        # total covering fewer days than its actual, which the footnote names (§3).
        "plan_start": min(plan_dates) if plan_dates else None,
        "plan_end": end,
        "days": days,
        "weeks": weeks,
        "meso_bands": bands,
        "objectives": objectives,
        # Structured, not a `warnings` string: the CLI draws it as a three-line banner
        # and used to recognise it by prefix-matching the prose, then recompute it.
        "plan_gap": (
            {"objective": gap[0], "weeks_before": gap[1], "plan_end": end}
            if gap else None
        ),
        "warnings": warnings,
    }


def clip_payload(
    payload: Dict[str, Any], start_date: str, end_date: str
) -> Dict[str, Any]:
    """A window-clipped view of an `assemble_timeline` payload for one renderer
    (§6.0). `days` clip by date; `weeks` and `meso_bands` are returned **whole**
    whenever they overlap the window (reslicing a straddling week would corrupt its
    adherence percentage), never silently dropped. `objectives` pass through whole —
    the renderer clips flags to its own window. The stored series is full-history and
    the fold starts at the anchor, so clipping never changes any value inside the
    window."""
    def week_overlaps(w: Dict[str, Any]) -> bool:
        ws = w["week_commencing"]
        we = _date_str(_to_date(ws) + timedelta(days=6))
        return ws <= end_date and we >= start_date

    return {
        **payload,
        "days": [d for d in payload["days"] if start_date <= d["date"] <= end_date],
        "weeks": [w for w in payload["weeks"] if week_overlaps(w)],
        "meso_bands": [
            b for b in payload["meso_bands"]
            if b["start_date"] <= end_date and b["end_date"] >= start_date
        ],
    }


def select_weeks(
    weeks: List[Dict[str, Any]], weeks_window: Any, today: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """`(past, future, hidden)` — the weeks a surface shows for `--weeks`, and how many
    it drops. `weeks_window` is a positive int (that many either side of today) or
    ``'all'``.

    THE one answer to "which weeks", so the text table, the `--chart` window and the
    `--blocks` section cannot disagree about the span they are all describing (§7.1).
    `hidden` covers both sides: a default run over a long history drops far more past
    weeks than projected ones, and the legend names the total."""
    past = [w for w in weeks if w["week_commencing"] <= today]
    future = [w for w in weeks if w["week_commencing"] > today]
    if weeks_window == "all":
        return past, future, 0
    n = int(weeks_window)
    shown_past, shown_future = past[-n:], future[:n]
    hidden = (len(past) - len(shown_past)) + (len(future) - len(shown_future))
    return shown_past, shown_future, hidden


def clip_payload_for_weeks(
    payload: Dict[str, Any], weeks: Any, today: str, *, cap_future: bool = False
) -> Dict[str, Any]:
    """A payload windowed to what `select_weeks` shows (§6.0/§7.1) — the date form of
    the same decision, for the chart and the web endpoint.

    `cap_future` cuts the projection to the last projected week shown. The CLI passes
    it so `--chart` frames the same span its text table does; the web endpoint leaves
    it off — an `<img>` has no accompanying table to agree with, and the whole
    projection is what that panel is for (§7.3)."""
    past, future, _ = select_weeks(payload["weeks"], weeks, today)
    start_date = past[0]["week_commencing"] if past else (
        future[0]["week_commencing"] if future else today
    )
    end_date = payload["plan_end"] or today
    if end_date < today:
        end_date = today
    if cap_future and future:
        last_sunday = _to_date(future[-1]["week_commencing"]) + timedelta(days=6)
        end_date = min(end_date, _date_str(last_sunday))
    return clip_payload(payload, start_date, end_date)
