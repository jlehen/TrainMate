"""How an in-place revision is computed — the pure helpers behind a `RevisionProposal`.

Kept apart from `proposals.py`, which holds only the frozen records the coach hands the
CLI: this is the logic that fills them in.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from trainmate.sports import canonical_sport


@dataclass(frozen=True)
class RevisionPair:
    """One proposed session and the planned session it stands in for.

    `original` is the session being replaced — the same-sport session for an in-place
    change, or the displaced one for a sport swap. None when nothing was planned that day.
    """
    proposal: Dict[str, Any]
    original: Optional[Dict[str, Any]]
    is_swap: bool


def structure_revision(
    revised: List[Dict[str, Any]], reason: str
) -> List[Dict[str, Any]]:
    """The row shape `workout_revision_apply` consumes, built from the model's response.

    One builder for every revision the coach proposes: the rules
    about how a returned change becomes a row — the `modification_reason` fallback below
    above all — are written once rather than copied into each propose method.
    """
    return [
        {
            'date': w['date'],
            'sport_type': w['sport_type'],
            'title': w['title'],
            'description': w['description'],
            # This revision's own note; the long batch rationale travels separately as
            # `reason` and lands on the change row (DESIGN_workout_revisions.md §3). Falls
            # back to the batch reason so a revised session is never left without one.
            'modification_reason': w.get('change_reason') or reason,
            'duration_minutes': w.get('duration_minutes'),
            'rpe': w.get('rpe'),
            'tss': w.get('tss'),
            # Carry the benchmark flag through the rebuild: a moved test must stay a test.
            # The model owns its survival by re-emitting it, and a returned change that
            # drops it clears the stored flag at apply time
            # (DESIGN_benchmark_workouts.md §3.1/§4.2).
            'benchmark_type': w.get('benchmark_type'),
        } for w in revised
    ]


def normalize_load_fields(workouts: List[Dict[str, Any]]) -> None:
    """Rounds the model's load fields to the integers the plan columns store.

    A JSON `24.0` is the same load as a stored `24`, but it renders `TSS24 -> TSS24.0`, so
    an unchanged number reads as a change. Fixed here rather than in the prompt so
    correctness does not rest on the model's formatting.
    """
    for w in workouts:
        for field in ('duration_minutes', 'rpe', 'tss'):
            value = w.get(field)
            if isinstance(value, float):
                w[field] = round(value)


def held_slots(held: Sequence[Tuple[str, str]]) -> Dict[str, set]:
    """`date -> {canonical sport}` for the sessions a revision holds rather than rewrites.

    A held session appends nothing, but it is still spoken for: the displacement rule
    below and in `workout_revision_apply` reads an unmentioned sport as one the coach
    wants gone, so omitting it here deletes it (§9.1).
    """
    by_date: Dict[str, set] = {}
    for date, sport in held:
        by_date.setdefault(date, set()).add(canonical_sport(sport))
    return by_date


def pair_revisions(
    proposals: List[Dict[str, Any]], existing: List[Dict[str, Any]],
    held: Sequence[Tuple[str, str]] = (),
) -> Tuple[Tuple[RevisionPair, ...], Tuple[Dict[str, Any], ...]]:
    """Decides, per date, which planned session each proposal replaces.

    An in-place change matches its original by (date, canonical sport). A sport swap
    carries a new sport_type and so has no same-sport original: on a proposed date, an
    existing session whose canonical sport is not among that date's proposals is the one
    being overridden, and is paired with that date's new-sport proposal.

    `held` names sessions the coach kept as planned. They produce no pair — there is
    nothing to show — but they count as proposed, so a date's other change cannot
    displace them (§9.1).

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

    held_by_date = held_slots(held)
    swap_original: Dict[int, Dict[str, Any]] = {}
    removals: List[Dict[str, Any]] = []

    for date, date_proposals in proposed_by_date.items():
        on_date = existing_by_date.get(date, [])
        proposed_sports = {canonical_sport(p["sport_type"]) for p in date_proposals}
        proposed_sports |= held_by_date.get(date, set())
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
            RevisionPair(
                proposal=proposal,
                original=same_sport or displaced,
                is_swap=same_sport is None and displaced is not None,
            )
        )
    return tuple(pairs), tuple(removals)
