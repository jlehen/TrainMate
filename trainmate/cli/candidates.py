"""Turning the candidates a free-text note produced into rows — the questions asked
before anything is stored.

Two inboxes read the same message for the same two candidate kinds: `workout adapt -m`,
where extraction rides the coaching call, and `bot capture note`, where it is the whole
job (DESIGN_bot_simple_frontend.md §12.3). The confirms live here so both paths ask the
same questions in the same order, the signal ladder included (§12.10). Nothing here
writes: the service's `capture_message_*` do, and only after a `yes`.
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

from trainmate import runtime, signals


def _span(candidate: Dict[str, Any], start_key: str, default_date: str) -> Tuple[str, str]:
    """A candidate's window, defaulted and ordered the way the service will store it."""
    start = candidate.get(start_key) or default_date
    end = candidate.get("end_date") or start
    return (start, start) if end < start else (start, end)


def _stated_value(candidate: Dict[str, Any]) -> Optional[float]:
    """The number the note stated, or None. A bool is not a measurement, and neither is
    anything the model invented outside the numeric types (DESIGN_signal_extraction.md §3)."""
    value = candidate.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def confirm_new_constraints(
    candidates: Sequence[Dict[str, Any]], date_str: str
) -> List[int]:
    """Asks about each directive the note produced and stores the confirmed ones
    (DESIGN_constraints.md §8 two-confirmation flow, step 1).

    Returns the ids created. Declining discards the extraction — on the adapt path the
    note has already informed that run's adaptation regardless, since the same LLM call
    produced both."""
    captured: List[int] = []
    for candidate in candidates:
        title = (candidate.get('title') or '').strip()
        if not title:
            continue
        start, end = _span(candidate, 'start_date', date_str)
        if not runtime.prompt.confirm(
            runtime.render.constraint_candidate_question(title, start, end, date_str)
        ):
            runtime.render.constraint_candidate_discarded()
            continue
        cid = runtime.coach_service.capture_message_constraint(candidate, date_str)
        if cid is None:
            continue
        captured.append(cid)
        runtime.render.constraint_captured(cid, title, start, end, date_str)
    return captured


def confirm_new_signals(candidates: Sequence[Dict[str, Any]], date_str: str) -> int:
    """Asks about each daily signal the note produced, then writes the confirmed ones
    (DESIGN_signal_extraction.md §2). Returns how many were logged.

    A category close to one already in use is offered as a ladder of two y/N questions —
    the existing category first, the coined one second — so the reflexive `y` lands on
    reuse and coining a category takes a deliberate second answer (§6). Declining both
    logs nothing.
    """
    logged = 0
    for candidate in candidates:
        metric = signals.normalize_metric(candidate.get('metric'))
        if not metric:
            continue
        start, end = _span(candidate, 'date', date_str)
        value = _stated_value(candidate)

        known = runtime.coach_service.known_signal_metrics()
        near = None if metric in known else signals.nearest_known(metric, known)

        def _ask(name: str, new_category: bool) -> bool:
            return runtime.prompt.confirm(runtime.render.signal_candidate_question(
                name, value, start, end, date_str, new_category=new_category,
            ))

        # Reuse is offered first, so the reflexive `y` lands on the safe outcome and
        # coining a category needs a deliberate second answer (§6).
        chosen = None
        if near and _ask(near, False):
            chosen = near
        elif metric in known:
            if _ask(metric, False):
                chosen = metric
        elif _ask(metric, True):
            chosen = metric

        if not chosen:
            runtime.render.signal_candidate_discarded()
            continue
        rows = runtime.coach_service.capture_message_signal(candidate, date_str, chosen)
        if not rows:
            runtime.render.signal_not_logged(chosen)
            continue
        logged += 1
        runtime.render.signal_logged(chosen, len(rows), start, end, date_str)
    return logged
