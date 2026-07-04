from datetime import timedelta
from typing import List, Dict, Any, Tuple, Optional

from trainmate.config import config
from trainmate.garmin import activity_load, _rpe_tss
from trainmate.sports import SPORT_MAPPING


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
    assigned. Replaces the former `tss + rpe*hours` blend."""
    tss = w.get("tss")
    if tss:
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


def classify_adherence(
    planned: Dict[str, Any],
    completed: Optional[Dict[str, Any]],
    minor_activity_load_threshold: float = 25.0,
) -> Dict[str, Any]:
    """Per-workout adherence verdict for a planned workout and the activity it
    matched (or None). `completed` is the *already-matched* activity from
    `analyze_adherence`'s pairing — for a rest day that is the violating effort,
    if any. Returns ``{"status": <one of below>, "reasons": [str, ...]}``:

        rest_ok          rest planned, no significant activity
        rest_violation   rest planned, a significant activity was performed
        missed           non-rest planned, nothing matched
        done             non-rest planned, matched within tolerance
        partial          non-rest planned, matched but duration/load off
    """
    if planned["sport_type"] == "rest":
        if completed and activity_load(completed) >= minor_activity_load_threshold:
            return {"status": "rest_violation", "reasons": []}
        return {"status": "rest_ok", "reasons": []}

    if not completed:
        return {"status": "missed", "reasons": []}

    reasons = _discrepancy_reasons(planned, completed)
    return {"status": "partial" if reasons else "done", "reasons": reasons}


def analyze_adherence(
    planned_workouts: List[Dict[str, Any]],
    completed_activities: List[Dict[str, Any]],
    start_date_obj: Any,
    history_days: int,
    minor_activity_load_threshold: float = 25.0,
    covered_ranges: Optional[List[Tuple[str, str]]] = None,
) -> Tuple[List[str], List[Dict[str, Any]], List[str]]:
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

    Returns:
        A tuple containing:
            - List of text discrepancy messages (deviations from the plan).
            - List of mapping results with date, planned workout, and completed activity.
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

        for w in day_workouts:
            w_sport = w["sport_type"]
            matched_act = None

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
                    discrepancies.append(
                        f"- {date_curr}: Rest Day Violation! Performed "
                        f"'{act['activity_name']}' ({act['activity_type']}) with "
                        f"workload {act_load:.1f} when Rest was planned."
                    )
                    break
            else:
                # Find a matching completed activity
                allowed_types = SPORT_MAPPING.get(w_sport, [w_sport])
                for act in day_acts:
                    if act["activity_id"] in used_act_ids:
                        continue
                    act_type = act["activity_type"].lower()
                    if act_type in allowed_types or any(t in act_type for t in allowed_types):
                        matched_act = act
                        used_act_ids.add(act["activity_id"])
                        break

                if not matched_act:
                    # Complete miss
                    discrepancies.append(
                        f"- {date_curr}: Complete Miss! Missed planned workout '{w['title']}' "
                        f"({w['sport_type']})."
                    )
                else:
                    disc_reasons = _discrepancy_reasons(w, matched_act)

                    if disc_reasons:
                        discrepancies.append(
                            f"- {date_curr}: Discrepancy in '{w['title']}' vs "
                            f"'{matched_act['activity_name']}': {', '.join(disc_reasons)}."
                        )

            matching_results.append({
                "date": date_curr,
                "planned": w,
                "completed": matched_act
            })

        # Check for completed activities when nothing was planned
        for act in day_acts:
            if act["activity_id"] in used_act_ids:
                continue
            act_load = activity_load(act)
            if act_load < minor_activity_load_threshold:
                continue
            if date_covered(date_curr, covered_ranges):
                discrepancies.append(
                    f"- {date_curr}: Unplanned Activity! Performed "
                    f"'{act['activity_name']}' ({act['activity_type']}) with "
                    f"workload {act_load:.1f} on a day with no planned workouts."
                )
            else:
                # No plan governs this date (e.g. before tool adoption, or an
                # unplanned off-season stretch). The load still feeds fatigue/ACWR
                # via completed_activities — surface it as informational, not a deviation.
                informational.append(act)

    return discrepancies, matching_results, informational
