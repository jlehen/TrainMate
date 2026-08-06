"""What the coach proposes, before the athlete has accepted it.

These objects exist to stop the same rule being written twice on both sides of a
boundary. Two drift bugs came from that:

* The CLI preview re-derived which planned session a proposal displaces, and rebuilt
  the adaptation's date range from ``min``/``max`` of the proposal dates — a *narrower*
  range than adapt actually evaluated — then handed that to apply, which uses it to
  decide what to delete. Preview and apply could therefore disagree about what
  disappears.
* ``plan_generate`` fingerprinted its inputs at prompt time while ``plan_apply``
  re-read them from the database at accept time, so editing a goal in between
  persisted a fingerprint describing data the strategy was never generated against —
  silently defeating the staleness detector whose whole job is catching that.

So a proposal carries the facts it was computed from, and the consumer renders rather
than recomputes.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from trainmate.sports import canonical_sport


@dataclass(frozen=True)
class AdaptPair:
    """One proposed session and the planned session it stands in for.

    `original` is the session being replaced — the same-sport session for an in-place
    adaptation, or the displaced one for a sport swap. It is None for a session
    proposed on a date that had nothing planned.
    """
    proposal: Dict[str, Any]
    original: Optional[Dict[str, Any]]
    is_swap: bool


@dataclass(frozen=True)
class AdaptProposal:
    """A `workout adapt` result: the changes, and the range they were judged over.

    `range_start`/`range_end` are the window the coach actually evaluated
    (target date through the end of the mesocycle), not the span of the proposals it
    happened to return. Apply deletes overridden sessions across this range, so the
    two must be the same window or apply removes sessions the preview never showed.
    """
    reason: str
    workouts: List[Dict[str, Any]]
    new_constraints: List[Dict[str, Any]]
    range_start: str
    range_end: str
    pairs: Tuple[AdaptPair, ...] = ()
    removals: Tuple[Dict[str, Any], ...] = ()


def pair_adaptations(
    proposals: List[Dict[str, Any]], existing: List[Dict[str, Any]]
) -> Tuple[Tuple[AdaptPair, ...], Tuple[Dict[str, Any], ...]]:
    """Decides, per date, which planned session each proposal replaces.

    An in-place adaptation matches its original by (date, canonical sport). A sport
    swap carries a new sport_type and so has no same-sport original: on a proposed
    date, an existing session whose canonical sport is not among that date's proposals
    is the one being overridden, and is paired with that date's new-sport proposal.

    Returns `(pairs, removals)`, where removals are overridden sessions left without a
    replacement — plain deletions, which the preview must show so apply never drops a
    session silently.
    """
    proposed_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for proposal in proposals:
        proposed_by_date.setdefault(proposal["date"], []).append(proposal)

    existing_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for session in existing:
        existing_by_date.setdefault(session["date"], []).append(session)

    swap_original: Dict[int, Dict[str, Any]] = {}
    removals: List[Dict[str, Any]] = []

    for date, date_proposals in proposed_by_date.items():
        on_date = existing_by_date.get(date, [])
        proposed_sports = {canonical_sport(p["sport_type"]) for p in date_proposals}
        existing_sports = {canonical_sport(e["sport_type"]) for e in on_date}

        overridden = [
            e for e in on_date if canonical_sport(e["sport_type"]) not in proposed_sports
        ]
        new_sport_proposals = [
            p for p in date_proposals
            if canonical_sport(p["sport_type"]) not in existing_sports
        ]
        # The common case is one overridden session and one new-sport proposal — a clean
        # swap — so pair positionally and treat the surplus as deletions.
        for proposal, original in zip(new_sport_proposals, overridden):
            swap_original[id(proposal)] = original
        removals.extend(overridden[len(new_sport_proposals):])

    pairs = []
    for proposal in proposals:
        same_sport = next(
            (
                e for e in existing_by_date.get(proposal["date"], [])
                if canonical_sport(e["sport_type"]) == canonical_sport(proposal["sport_type"])
            ),
            None,
        )
        displaced = swap_original.get(id(proposal))
        pairs.append(
            AdaptPair(
                proposal=proposal,
                original=same_sport or displaced,
                is_swap=same_sport is None and displaced is not None,
            )
        )
    return tuple(pairs), tuple(removals)


@dataclass(frozen=True)
class CoachContext:
    """What the coach knows before it is asked anything in particular.

    These seven travel together into every prompt builder — planning, generation and
    adaptation — and were assembled by the same four calls copy-pasted into each of the
    three service methods. Threading them as one value means a new shared input is added
    in one place rather than three, and a call site cannot quietly omit one.

    Everything here is *shared* context. Per-command inputs (the target date, the
    athlete's note, the window being planned) stay as arguments, because they are what
    distinguishes one command from another.
    """
    objectives: List[Dict[str, Any]]
    constraints: List[Dict[str, Any]]
    guidelines: str
    profile: Optional[Dict[str, Any]]
    strategy: str
    meso_text: str
    learnings: str


@dataclass(frozen=True)
class PlanFingerprints:
    """The inputs a strategy was generated against, hashed once at prompt time.

    Persisted verbatim by `plan_apply`. Re-deriving them at accept time is what let an
    edit between generate and accept be recorded as if the strategy had seen it.
    """
    goals_hash: str
    constraints_hash: str
    config_hash: Optional[str] = None
    config_snapshot: Optional[str] = None
    goals_snapshot: Optional[str] = None
    constraints_snapshot: Optional[str] = None
