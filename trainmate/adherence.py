from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Dict, Any, Tuple, Optional

from trainmate.config import config
from trainmate.garmin import activity_load, _rpe_tss
from trainmate.sports import canonical_sport, sport_aliases

REST_VIOLATION = "rest_violation"
MISSED = "missed"
PARTIAL = "partial"
UNPLANNED = "unplanned"

# A planned session on a day that has not finished yet is not a miss — the athlete may
# still train it. Withdrawing it is `workout remove`; saying so is the adapt note.
PENDING = "pending"


@dataclass(frozen=True)
class Discrepancy:
    """One way the week departed from the plan.

    Analysis returns these rather than sentences. The prose used to be built in here
    and shipped verbatim as JSON, so the dashboard spoke in the CLI's voice ("Complete
    Miss!") and no consumer could filter, count or restyle by kind without parsing
    English. `format_discrepancy` composes the wording at the edge that needs it.
    """
    kind: str
    date: str
    planned: Optional[Dict[str, Any]] = None
    completed: Optional[Dict[str, Any]] = None
    reasons: Tuple[str, ...] = ()
    load: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe form, with the rendered sentence alongside the facts."""
        return {
            "kind": self.kind,
            "date": self.date,
            "planned_title": (self.planned or {}).get("title"),
            "planned_sport": (self.planned or {}).get("sport_type"),
            "activity_name": (self.completed or {}).get("activity_name"),
            "activity_type": (self.completed or {}).get("activity_type"),
            "reasons": list(self.reasons),
            "load": self.load,
            "text": format_discrepancy(self),
        }


def format_discrepancy(d: "Discrepancy") -> str:
    """The athlete-facing sentence for one discrepancy.

    Kept identical to the wording analysis used to bake in, because it also reaches the
    coaching prompt — the LLM reads these lines as the week's evidence.
    """
    if d.kind == REST_VIOLATION:
        act = d.completed or {}
        return (
            f"- {d.date}: Rest Day Violation! Performed "
            f"'{act.get('activity_name')}' ({act.get('activity_type')}) with "
            f"workload {d.load:.1f} when Rest was planned."
        )
    if d.kind == MISSED:
        planned = d.planned or {}
        return (
            f"- {d.date}: Complete Miss! Missed planned workout "
            f"'{planned.get('title')}' ({planned.get('sport_type')})."
        )
    if d.kind == PARTIAL:
        planned, act = d.planned or {}, d.completed or {}
        return (
            f"- {d.date}: Discrepancy in '{planned.get('title')}' vs "
            f"'{act.get('activity_name')}': {', '.join(d.reasons)}."
        )
    if d.kind == UNPLANNED:
        act = d.completed or {}
        return (
            f"- {d.date}: Unplanned Activity! Performed "
            f"'{act.get('activity_name')}' ({act.get('activity_type')}) with "
            f"workload {d.load:.1f} on a day with no planned workouts."
        )
    raise ValueError(f"unknown discrepancy kind: {d.kind!r}")


def format_discrepancies(discrepancies: List["Discrepancy"]) -> List[str]:
    """The rendered lines, in order — for the CLI and the coaching prompt."""
    return [format_discrepancy(d) for d in discrepancies]


def date_covered(
    date_str: str, covered_ranges: Optional[List[Tuple[str, str]]]
) -> bool:
    """Whether `date_str` falls inside any planned block (mesocycle span).

    `covered_ranges` of None means "assume covered" — preserves the original
    behavior for callers that don't supply coverage. Dates are YYYY-MM-DD, so
    lexicographic comparison is chronological."""
    if covered_ranges is None:
        return True
    return any(start <= date_str <= end for start, end in covered_ranges)


def planned_load(w: Dict[str, Any]) -> float:
    """Expected load of a planned workout as a single value (mirrors the actual
    side): the coach's planned TSS, or sRPE (RPE x 10 x hours) when no TSS was
    assigned. Replaces the former `tss + rpe*hours` blend.

    An explicit ``tss = 0`` means zero, not "unset" — it is a real planned load
    and must not silently fall through to the sRPE estimate (that made the
    timeline's weekly bars disagree with the adherence percentages beside them,
    DESIGN_progress_timeline.md §3). Only a missing/None TSS triggers the fallback."""
    tss = w.get("tss")
    if tss is not None:
        return float(tss)
    rpe = w.get("rpe") or 0
    duration_min = w.get("duration_minutes") or 0
    return _rpe_tss(float(rpe), duration_min * 60.0)


def _adherence_tolerance(exp_load: float) -> float:
    """Dynamic +/- tolerance for the duration/workload comparison: looser for
    easy sessions (where small absolute swings are large in %), tighter for hard
    ones. Linearly interpolated between `easy_pct` at `low_load` and `hard_pct`
    at `high_load` (config.adherence_tolerance)."""
    t = config.adherence_tolerance
    easy_pct, hard_pct = t["easy_pct"], t["hard_pct"]
    low_load, high_load = t["low_load"], t["high_load"]
    if exp_load <= low_load:
        return easy_pct
    if exp_load >= high_load:
        return hard_pct
    fraction = (exp_load - low_load) / (high_load - low_load)
    return easy_pct - fraction * (easy_pct - hard_pct)


def _discrepancy_reasons(
    w: Dict[str, Any], matched_act: Dict[str, Any]
) -> List[str]:
    """Duration/workload mismatch notes for a planned workout vs the activity it
    matched. Empty list means the session was performed within tolerance. Single
    source of truth shared by `analyze_adherence` and `classify_adherence`."""
    act_duration_min = matched_act["duration_sec"] / 60.0
    act_load = activity_load(matched_act)

    p_duration = w.get("duration_minutes") or 0
    exp_load = planned_load(w)

    tolerance = _adherence_tolerance(exp_load)
    tol_pct = f"+/-{tolerance*100:.0f}%"

    reasons: List[str] = []
    if (
        p_duration > 0
        and (abs(act_duration_min - p_duration) / p_duration) > tolerance
    ):
        reasons.append(
            f"duration mismatch {tol_pct} (planned {p_duration:.0f}m, "
            f"actual {act_duration_min:.0f}m)"
        )
    if exp_load > 0 and (abs(act_load - exp_load) / exp_load) > tolerance:
        reasons.append(
            f"workload mismatch {tol_pct} (planned load {exp_load:.1f}, "
            f"actual load {act_load:.1f})"
        )
    return reasons


# The athlete-facing name of each `classify_adherence` status: one vocabulary for the
# Calendar title tag, the `workout list` marker and the web badge
# (ARCHITECTURE.md §15 "A session already behind us carries its verdict").
STATUS_LABELS = {
    "done": "Done",
    PARTIAL: "Partial",
    MISSED: "Missed",
    PENDING: "Not yet",
    "rest_ok": "Rest OK",
    "rest_violation": "Rest broken",
}


def classify_adherence(
    planned: Dict[str, Any],
    completed: Optional[Dict[str, Any]],
    minor_activity_load_threshold: float = 25.0,
    pending: bool = False,
) -> Dict[str, Any]:
    """Per-workout adherence verdict for a planned workout and the activity it
    matched (or None). `completed` is the *already-matched* activity from
    `analyze_adherence`'s pairing — for a rest day that is the violating effort,
    if any. Returns ``{"status": <one of below>, "reasons": [str, ...]}``:

        rest_ok          rest planned, no significant activity
        rest_violation   rest planned, a significant activity was performed
        pending          non-rest planned, nothing matched, day not over yet
        missed           non-rest planned, nothing matched
        done             non-rest planned, matched within tolerance
        partial          non-rest planned, matched but duration/load off

    `pending` is the caller's "this day has not finished" flag — carried on the
    `analyze_adherence` row that produced this pair, never re-derived here.
    """
    if canonical_sport(planned["sport_type"]) == "rest":
        if completed and activity_load(completed) >= minor_activity_load_threshold:
            return {"status": "rest_violation", "reasons": []}
        return {"status": "rest_ok", "reasons": []}

    if not completed:
        return {"status": PENDING if pending else "missed", "reasons": []}

    reasons = _discrepancy_reasons(planned, completed)
    return {"status": PARTIAL if reasons else "done", "reasons": reasons}


@dataclass(frozen=True)
class Performed:
    """What a planned session actually got, for the surfaces that must not call a
    fragment of a session "done" (ARCHITECTURE.md §15 "A session already behind us
    carries its verdict")."""
    status: str          # `classify_adherence` verdict: "done" or "partial"
    duration_min: float  # what the matched activity actually ran
    load: float          # ...and what it actually cost
    locked: bool         # history now: adapt may not rewrite it


def performed_sessions(
    matching_results: List[Dict[str, Any]],
    eval_date: str,
    minor_activity_load_threshold: float = 25.0,
) -> Dict[Tuple[str, str], Performed]:
    """`(date, canonical_sport) -> Performed` for every planned session that matched a
    completed activity. Unmatched sessions are absent (missed or pending — adapt's to
    shape), and so are rest days, whose verdict is about a violation, not a performance.

    `locked` is adapt's history lock: on the evaluation date a session becomes history
    only once its planned TIME was actually spent, so one abandoned after the warm-up
    stays adaptable for the rest of the day (ARCHITECTURE.md §15).
    """
    performed: Dict[Tuple[str, str], Performed] = {}
    for m in matching_results:
        act = m["completed"]
        if act is None:
            continue
        w = m["planned"]
        status = classify_adherence(
            w, act, minor_activity_load_threshold, pending=m.get("pending", False)
        )["status"]
        if status not in ("done", PARTIAL):
            continue
        performed[(m["date"], canonical_sport(w["sport_type"]))] = Performed(
            status=status,
            duration_min=act["duration_sec"] / 60.0,
            load=activity_load(act),
            locked=m["date"] < eval_date or not _duration_shortfall(w, act),
        )
    return performed


def _duration_shortfall(w: Dict[str, Any], matched_act: Dict[str, Any]) -> bool:
    """Whether the matched activity ran materially SHORTER than planned. Duration, not
    load: strength load read off HR is unreliable, but time in the gym is not."""
    p_duration = w.get("duration_minutes") or 0
    if p_duration <= 0:
        return False
    act_duration_min = matched_act["duration_sec"] / 60.0
    return (p_duration - act_duration_min) / p_duration > _adherence_tolerance(
        planned_load(w)
    )


def analyze_adherence(
    planned_workouts: List[Dict[str, Any]],
    completed_activities: List[Dict[str, Any]],
    start_date_obj: Any,
    history_days: int,
    minor_activity_load_threshold: float = 25.0,
    covered_ranges: Optional[List[Tuple[str, str]]] = None,
    pending_from: Optional[str] = None,
) -> Tuple[List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Evaluates planned workouts vs completed Garmin activities over a rolling window.

    Calculates adherence discrepancies (missed workouts, duration/workload mismatches, and
    rest violations).

    Args:
        planned_workouts: List of planned workouts in the window.
        completed_activities: List of completed activities in the window.
        start_date_obj: Start date of the evaluation window.
        history_days: Length of the window in days.
        covered_ranges: (start, end) spans of planned blocks. An activity on a day with
            no planned workout is reported as a deviation ("Unplanned Activity!") only if
            its date falls within one of these spans; outside all coverage it is softened
            to an informational note. None means treat every date as covered.
        pending_from: first date whose day is not over yet (normally today). An unmatched
            non-rest session on or after it is PENDING, not missed — the athlete is
            assumed to still be doing it. None means every date is final.

    Returns:
        A tuple containing:
            - List of text discrepancy messages (deviations from the plan).
            - List of mapping results with date, planned workout, completed activity, and
              the `pending` flag consumers must use rather than re-deriving the date rule.
            - List of completed activities that fall outside any planned block (informational).
    """
    matching_results = []
    discrepancies = []
    informational = []

    # Group completed activities by date
    activities_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for act in completed_activities:
        activities_by_date.setdefault(act["date"], []).append(act)

    # Group planned workouts by date
    workouts_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for w in planned_workouts:
        workouts_by_date.setdefault(w["date"], []).append(w)

    # Process each day in the window
    for d in range(history_days):
        date_curr = (start_date_obj + timedelta(days=d)).strftime("%Y-%m-%d")
        day_acts = activities_by_date.get(date_curr, [])
        day_workouts = workouts_by_date.get(date_curr, [])

        # Sort activities by load descending
        day_acts = sorted(day_acts, key=activity_load, reverse=True)

        used_act_ids = set()

        # Dates are YYYY-MM-DD, so lexicographic comparison is chronological.
        day_pending = bool(pending_from) and date_curr >= pending_from

        for w in day_workouts:
            # Canonicalize: a session stored under an alias ("strength", "Running") must
            # still match its activity, or it reads as a Complete Miss.
            w_sport = canonical_sport(w["sport_type"])
            matched_act = None
            pending = False

            if w_sport == "rest":
                # Check for rest day violation: any activity with significant workload
                for act in day_acts:
                    if act["activity_id"] in used_act_ids:
                        continue
                    act_load = activity_load(act)
                    if act_load < minor_activity_load_threshold:
                        continue
                    matched_act = act
                    used_act_ids.add(act["activity_id"])
                    discrepancies.append(Discrepancy(
                        kind=REST_VIOLATION, date=date_curr, planned=w,
                        completed=act, load=act_load,
                    ))
                    break
            else:
                # Find a matching completed activity
                allowed_types = sport_aliases(w_sport)
                for act in day_acts:
                    if act["activity_id"] in used_act_ids:
                        continue
                    act_type = act["activity_type"].lower()
                    if act_type in allowed_types or any(t in act_type for t in allowed_types):
                        matched_act = act
                        used_act_ids.add(act["activity_id"])
                        break

                if not matched_act:
                    # Still ahead of the athlete on an unfinished day, so not yet a miss.
                    # Reporting it as one told the adapt prompt the session was lost and
                    # invited it to reschedule work that was never skipped.
                    pending = day_pending
                    if not pending:
                        discrepancies.append(
                            Discrepancy(kind=MISSED, date=date_curr, planned=w)
                        )
                else:
                    disc_reasons = _discrepancy_reasons(w, matched_act)

                    if disc_reasons:
                        discrepancies.append(Discrepancy(
                            kind=PARTIAL, date=date_curr, planned=w,
                            completed=matched_act, reasons=tuple(disc_reasons),
                        ))

            matching_results.append({
                "date": date_curr,
                "planned": w,
                "completed": matched_act,
                "pending": pending,
            })

        # Check for completed activities when nothing was planned
        for act in day_acts:
            if act["activity_id"] in used_act_ids:
                continue
            act_load = activity_load(act)
            if act_load < minor_activity_load_threshold:
                continue
            if date_covered(date_curr, covered_ranges):
                discrepancies.append(Discrepancy(
                    kind=UNPLANNED, date=date_curr, completed=act, load=act_load,
                ))
            else:
                # No plan governs this date (e.g. before tool adoption, or an
                # unplanned off-season stretch). The load still feeds the PMC
                # via completed_activities — surface it as informational, not a deviation.
                informational.append(act)

    return discrepancies, matching_results, informational
