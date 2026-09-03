"""What the coach proposes, before the athlete has accepted it.

These records exist to stop the same rule being written twice on both sides of a
boundary. When the CLI preview re-derived a proposal's displaced session and date range,
preview and apply could disagree about what disappears; when ``plan_generate``
fingerprinted at prompt time and ``plan_apply`` re-read at accept time, an edit in
between silently defeated the staleness detector.

So a proposal carries the facts it was computed from, and the consumer renders rather
than recomputes. Records only: the logic that fills them in lives in `revisions.py`.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from trainmate.coach.revisions import RevisionPair


@dataclass(frozen=True)
class RevisionProposal:
    """An in-place rewrite of the sessions in a window, before it has been accepted.

    `workout adapt`'s shape: rows revised where they stand rather than rebuilt from a
    date onward, which is what separates it from `GenerateProposal` below. Applied by
    `workout_revision_apply`.

    `range_start`/`range_end` are the window the coach actually evaluated, not the span of
    the proposals it happened to return. Apply deletes overridden sessions across this
    range, so the two must be the same window or apply removes sessions the preview never
    showed — which is also why every caller renders these rather than its own request.
    """
    reason: str
    workouts: List[Dict[str, Any]]
    range_start: str
    range_end: str
    pairs: Tuple[RevisionPair, ...] = ()
    removals: Tuple[Dict[str, Any], ...] = ()
    # Candidate directives extracted from the athlete's note, raw and unconfirmed
    # (DESIGN_constraints.md §8).
    new_constraints: Tuple[Dict[str, Any], ...] = ()
    # Candidate daily signals extracted from the same note, raw and unconfirmed
    # (DESIGN_signal_extraction.md §2).
    new_signals: Tuple[Dict[str, Any], ...] = ()
    # Constraints this pass had authority over every remaining day of, so apply stamps
    # exactly the set decided at proposal time (DESIGN_constraint_honoring.md §3). See
    # `coach/honoring.py`.
    covered_constraint_ids: Tuple[int, ...] = ()
    # `(date, canonical sport)` the coach named only to hold — no revision, but the
    # displacement rule must still count them as spoken for
    # (DESIGN_workout_revisions.md §9.1).
    held: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class GenerateProposal:
    """A `workout generate` result, before anything has been written.

    Generation is archive-and-rebuild, so the athlete sees the sessions first and the
    write happens only on a `y` (ARCHITECTURE.md §"Workout Generation"). `workouts` are
    already tagged with the `macrocycle_id` governing their date, and `displaced` is the
    live plan this would archive — both decided here, so the preview and the apply cannot
    disagree about what appears and what disappears.

    `gen_start`/`gen_end` bound the rebuild at BOTH ends: the selectors pick a span, and
    days outside it keep the sessions they already have (DESIGN_cli_selectors.md §8).
    """
    reasoning: str
    workouts: Tuple[Dict[str, Any], ...] = ()
    displaced: Tuple[Dict[str, Any], ...] = ()
    gen_start: str = ""
    gen_end: str = ""
    # As on `RevisionProposal` (DESIGN_constraint_honoring.md §3). Decided where
    # `gen_start`/`gen_end` are both in scope, so the proposal carries the resulting ids
    # rather than a range to re-check.
    covered_constraint_ids: Tuple[int, ...] = ()


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
    profile_snapshot: Optional[str] = None
    goals_snapshot: Optional[str] = None
    constraints_snapshot: Optional[str] = None
    all_constraints_snapshot: Optional[str] = None
