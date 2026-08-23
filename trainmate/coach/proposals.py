"""What the coach proposes, before the athlete has accepted it.

These records exist to stop the same rule being written twice on both sides of a
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
than recomputes. Records only: the logic that fills them in lives in `revisions.py`.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from trainmate.coach.revisions import RevisionPair


@dataclass(frozen=True)
class RevisionProposal:
    """An in-place rewrite of the sessions in a window, before it has been accepted.

    The shape `workout adapt` and `workout accommodate` share: rows edited where they
    stand rather than archived and rebuilt (DESIGN_constraint_reschedule.md §7). Both are
    applied by `workout_revision_apply`.

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
    # Everything below is defaulted, and that is the rule this record keeps: a producer
    # names only what it actually has. `workout adapt` extracts candidate directives from
    # the athlete's note and `workout accommodate` has no note to extract from, so the
    # second one says nothing rather than declaring an empty list — a field a producer has
    # to opt out of is how a shared record turns into a union of two.
    new_constraints: Tuple[Dict[str, Any], ...] = ()
    # Constraints this pass had authority over every remaining day of, so apply stamps
    # exactly the set decided at proposal time (§8). See `coach/honoring.py`.
    covered_constraint_ids: Tuple[int, ...] = ()
    # Which change kind apply writes this pass under. It is what tells an easing from a
    # reschedule: the adaptation tally counts `adapt` revisions only, so an accommodate
    # cannot raise the DO NOT COMPOUND bar for a session it never cut
    # (DESIGN_workout_revisions.md §7). Carried here rather than passed to apply so the
    # producer decides once and no call site can forget it.
    kind: str = "adapt"
    # Every session in the evaluated window, for a whole-window preview (§10). Carried so
    # the CLI renders the proposal rather than re-reading the rows behind it.
    window_workouts: Tuple[Dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AccommodationPass:
    """One §5 window and the directives it honors — the unit `workout accommodate` runs.

    Several constraints when their spill-widened windows overlapped and were merged: a
    second pass over shared days would preview a plan the first had not yet applied
    (DESIGN_constraint_reschedule.md §4.1).

    `blocks` are the governing blocks, resolved once at plan time (§4.2): the pass runs
    over its whole window whenever any of it is governed, so the propose call and the
    CLI's "plan runs out" warning both read this list rather than asking again.
    """
    range_start: str
    range_end: str
    constraints: Tuple[Dict[str, Any], ...] = ()
    blocks: Tuple[Dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AccommodationPlan:
    """What can and cannot be done about a set of directives, decided before any of it is.

    Every refusal is a populated field rather than a message the caller inferred. The CLI
    used to work these out for itself — a plan horizon from all blocks covering today, a
    `start_date > plan_end` test for "too far out" — while the service worked out its own
    from the blocks overlapping each window. Two computations of one fact, so a constraint
    landing in a GAP between blocks passed the caller's test, became a pass, and was
    refused mid-loop with "no active periodization strategy found" — which was not true
    (§4.1, §5).
    """
    plan_end: Optional[str]
    passes: Tuple[AccommodationPass, ...] = ()
    # No block covers their window, so there is nothing to reshuffle them towards: past
    # the plan's end, or inside a gap in it. One bucket, because the answer is the same.
    ungoverned: Tuple[Dict[str, Any], ...] = ()
    # Nothing left of their window but today, which belongs to `workout adapt` (§5).
    spent: Tuple[Dict[str, Any], ...] = ()


@dataclass(frozen=True)
class GenerateProposal:
    """A `workout generate` result, before anything has been written.

    Generation is archive-and-rebuild, so the athlete sees the sessions first and the
    write happens only on a `y` (ARCHITECTURE.md §"Workout Generation"). `workouts` are
    already tagged with the `macrocycle_id` governing their date, and `displaced` is the
    live plan this would archive — both decided here, so the preview and the apply cannot
    disagree about what appears and what disappears.
    """
    reasoning: str
    workouts: Tuple[Dict[str, Any], ...] = ()
    displaced: Tuple[Dict[str, Any], ...] = ()
    gen_start: str = ""
    # As on `RevisionProposal` (§8). Decided where `gen_start`/`gen_end` are both in
    # scope, so the proposal carries the resulting ids rather than a range to re-check.
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
