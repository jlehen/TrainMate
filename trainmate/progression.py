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

from trainmate import garmin
from trainmate.garmin import activity_load
from trainmate.adherence import planned_load

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


def zero_load_workout_count(workouts: List[Dict[str, Any]]) -> int:
    """Count of non-removed, non-rest planned workouts that value to 0 load — no
    usable TSS and no RPE+duration, so `planned_load` falls back to 0 (§3). An
    explicit `tss = 0` counts here too (it values to 0); a `rest` row does not (it
    has no load by design)."""
    return sum(
        1 for w in workouts
        if not w.get("removed") and w.get("sport_type") != "rest"
        and planned_load(w) == 0
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


def _week_governed(
    week_mon: str, week_sun: str, macro_versions: List[Dict[str, Any]]
) -> bool:
    """Whether a week was governed by a plan (§6.1): per objective, the latest
    macrocycle version created before the week ended is the *version in force*; the
    week is governed iff any in-force version's mesocycle coverage overlaps it.

    Version history is consulted here (not the label) so a week whose plan was later
    superseded still counts as governed — and so governance can't be silently deleted
    by a labelling nit (CODE_REVIEW finding #3). Timestamp-vs-date pin: `created_at`
    is a UTC ISO timestamp, the week end is a date, so a version counts iff
    `created_at[:10] <= week_sunday`. A version with no usable `created_at` fails
    *closed* — excluded rather than treated as created-before-all-time, which would
    mark pre-plan weeks as governed (`created_at` is NOT NULL today, so this only
    guards a future migration)."""
    in_force: Dict[Any, Dict[str, Any]] = {}
    for mv in macro_versions:
        created = mv.get("created_at")
        if not created or created[:10] > week_sun:
            continue
        cur = in_force.get(mv["objective_id"])
        if cur is None or created > cur["created_at"]:
            in_force[mv["objective_id"]] = mv
    for mv in in_force.values():
        for s, e in mv["ranges"]:
            if s <= week_sun and e >= week_mon:
                return True
    return False


def weekly_aggregates(
    activities: List[Dict[str, Any]],
    workouts: List[Dict[str, Any]],
    today: str,
    meso_spans: List[Dict[str, Any]],
    macro_versions: List[Dict[str, Any]],
    *,
    window_end: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Monday-commencing weekly planned-vs-actual load (§5/§6), one dict per week from
    the earliest activity/workout date through plan end (or today):

        {week_commencing, planned_load, planned_load_elapsed?, in_progress,
         actual_load, meso_label, meso_source}

    `planned_load` (Σ `adherence.planned_load` over non-removed workouts — the
    *adapted* plan, "what the plan asked at the time") is None for an **ungoverned**
    week (no plan governed it, per the version-in-force rule) — matching
    `adherence.py`'s precedent that activity outside planned coverage is
    informational, not a deviation. Governance is decided by `macro_versions`, **not**
    by the meso label: a week can carry an `~inferred` label yet still be governed
    (its planned total and percentage render). The current (in-progress) week also
    carries `planned_load_elapsed`, the Monday-through-elapsed slice — today included
    only once its load has synced (§3), so a partial week doesn't read as poor
    adherence every Monday."""
    non_removed_workouts = [w for w in workouts if not w.get("removed")]
    start = _series_start(activities, non_removed_workouts)
    if start is None:
        return []
    end = window_end if window_end is not None else _window_end(workouts, today)
    week_start = _monday(_to_date(start))
    end_d = _to_date(end)

    acts_by_date = _group_by_date(activities)
    workouts_by_date = _group_by_date(non_removed_workouts)

    weeks: List[Dict[str, Any]] = []
    w_start = week_start
    while w_start <= end_d:
        week_dates = [_date_str(w_start + timedelta(days=i)) for i in range(7)]
        week_mon, week_sun = week_dates[0], week_dates[-1]
        in_progress = week_mon <= today <= week_sun

        actual_load = sum(
            activity_load(a) for d in week_dates for a in acts_by_date.get(d, [])
        )
        week_workouts = [w for d in week_dates for w in workouts_by_date.get(d, [])]

        meso_label, meso_source = _week_meso(week_dates, meso_spans)
        governed = _week_governed(week_mon, week_sun, macro_versions)

        week: Dict[str, Any] = {
            "week_commencing": week_mon,
            "actual_load": actual_load,
            "in_progress": in_progress,
            "meso_label": meso_label,
            "meso_source": meso_source,
        }
        if governed:
            week["planned_load"] = sum(planned_load(w) for w in week_workouts)
            if in_progress:
                # Elapsed = Mon..yesterday, plus today only once its load has synced
                # (today's §3 source is 'actual'). Including an unfinished today would
                # make an evening athlete read <100% all day (§3).
                today_synced = any(
                    activity_load(a) > 0 for a in acts_by_date.get(today, [])
                )
                elapsed_end = today if today_synced else (
                    _date_str(_to_date(today) - timedelta(days=1))
                )
                week["planned_load_elapsed"] = sum(
                    planned_load(w) for w in week_workouts if w["date"] <= elapsed_end
                )
        else:
            week["planned_load"] = None

        weeks.append(week)
        w_start += timedelta(days=7)

    return weeks


def plan_gap(
    objectives: List[Dict[str, Any]], plan_end_date: Optional[str]
) -> Optional[Tuple[Dict[str, Any], int]]:
    """The next active objective the plan doesn't yet reach, and how many whole weeks
    short of it the plan ends (§3), as `(objective, weeks_before)` — or None when there
    is no plan or every active objective is already reached.

    The single source of the plan-gap derivation: `assemble_timeline` words it into a
    payload `warnings` string, the CLI renders it as a rich banner (§7.1). Computing it
    once here keeps the two surfaces from diverging on *when* the gap fires or *by how
    much* — the CODE_REVIEW #5 class of drift."""
    if plan_end_date is None:
        return None
    active = sorted(
        (o for o in objectives if o.get("status") == "active"),
        key=lambda o: str(o["target_date"]),
    )
    next_obj = next((o for o in active if o["target_date"] > plan_end_date), None)
    if next_obj is None:
        return None
    weeks_before = max(
        0, round((_to_date(next_obj["target_date"]) - _to_date(plan_end_date)).days / 7)
    )
    return next_obj, weeks_before


def assemble_timeline(
    activities: List[Dict[str, Any]],
    workouts: List[Dict[str, Any]],
    metrics_rows: List[Dict[str, Any]],
    macro_versions: List[Dict[str, Any]],
    mesocycles: List[Dict[str, Any]],
    inferred_mesocycles: List[Dict[str, Any]],
    objectives: List[Dict[str, Any]],
    today: str,
    ctl_days: int,
    atl_days: int,
    warmup_cutoff: Optional[str],
) -> Dict[str, Any]:
    """The ENTIRE §6.0 timeline payload — days, weeks, meso_bands, objectives,
    warnings (exact strings) — built here and ONLY here, from the §5 helpers plus the
    §6.1 layered lookup. Callers do db reads and hand rows in; neither the CLI handler
    nor the web endpoint owns any assembly or warning-wording logic, so the two
    surfaces render one payload (the fix for the rev-4 divergence, CODE_REVIEW #5).

    `objectives` are ALL active AND completed objectives, unfiltered — a race weeks
    ago still gets its flag; renderers clip to their window. Stays row-in/row-out.
    """
    warnings: List[str] = []

    if not activities:
        # §3 empty state: no past series and no PMC, but the planned future still
        # renders (weekly bars); every surface shows this.
        warnings.append("no activity history yet — run `data pull` first")

    valid_inferred = [
        m for m in inferred_mesocycles
        if _valid_span(m.get("start_date"), m.get("end_date"))
    ]
    skipped = len(inferred_mesocycles) - len(valid_inferred)
    if skipped:
        warnings.append(
            f"{skipped} bootstrap mesocycle block{_plural(skipped)} skipped "
            f"— unparseable dates"
        )
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
        activities, workouts, today, bands, macro_versions, window_end=window_end
    )

    zero_count = zero_load_workout_count(workouts)
    if zero_count:
        warnings.append(
            f"{zero_count} planned workout{_plural(zero_count)} lack TSS/RPE "
            f"— count as 0"
        )

    if end is not None:
        beyond = [
            w for w in workouts if not w.get("removed") and w["date"] > end
        ]
        if beyond:
            warnings.append(
                f"{len(beyond)} workout{_plural(len(beyond))} beyond plan end "
                f"— not projected"
            )
        gap = plan_gap(objectives, end)
        if gap:
            next_obj, weeks_before = gap
            warnings.append(
                f"plan generated through {end} ({weeks_before} wks before "
                f"objective {next_obj['target_date']})"
            )

    history_start = _history_start(activities, metrics_rows)
    caveat = garmin.pmc_data_caveat(history_start, as_of=today)
    if caveat:
        warnings.append(
            f"PMC still warming: CTL based on {caveat['n_days']} days of history"
        )

    return {
        "today": today,
        "plan_end": end,
        "days": days,
        "weeks": weeks,
        "meso_bands": bands,
        "objectives": objectives,
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


def clip_payload_for_weeks(
    payload: Dict[str, Any], weeks: Any, today: str, *, cap_future: bool = False
) -> Dict[str, Any]:
    """Window a payload to the last `weeks` of past (§6.0/§7.1). `weeks` is a positive
    int (past edge = today − 7·weeks) or the string ``'all'`` for full history; the
    future edge is plan end, clamped up to today when the plan is absent or already
    lapsed. Shared by the CLI `--chart` path and the web endpoint so their windows
    can't drift.

    `cap_future` also cuts the projection to the next `weeks` whole weeks. The CLI
    passes it so `--chart` frames the same span its text table does — a PNG showing
    twenty projected weeks under a legend reading `+12 more` contradicts itself. The
    web endpoint leaves it off: an `<img>` has no accompanying table to agree with,
    and the whole projection is what that panel is for (§7.3)."""
    if weeks == "all":
        start_date = "0001-01-01"
    else:
        start_date = _date_str(_to_date(today) - timedelta(days=7 * int(weeks)))
    end_date = payload["plan_end"] or today
    if end_date < today:
        end_date = today
    if cap_future and weeks != "all":
        # Sunday of the Nth whole week after the one containing today — the last week
        # `render_progress` puts in the table.
        last_shown = _monday(_to_date(today)) + timedelta(days=7 * int(weeks) + 6)
        end_date = min(end_date, _date_str(last_shown))
    return clip_payload(payload, start_date, end_date)
