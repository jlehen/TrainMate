"""Honoring a constraint the daily adapt cannot reach (DESIGN_constraint_reschedule.md).

A window-scoped reshuffle over a constraint's own dates rather than over the current
block. Reads no metrics, which is what lets it cross the block boundary `workout adapt`
may not (§2/§6); writes nothing, which `workout_revision_apply` does on a `y` (§7).
"""
from typing import Any, Dict, List, Optional, Tuple

from trainmate.util import cmd
from trainmate.coach import honoring
from trainmate.coach.proposals import (
    AccommodationPass, AccommodationPlan, RevisionProposal,
)
from trainmate.coach.revisions import (
    normalize_load_fields, pair_revisions, structure_revision,
)


class AccommodateMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

    def plan_horizon(self, today: str) -> Optional[str]:
        """The last day any block covers from `today` on, or None when no plan governs
        from here — the sweep's horizon (§4.1). The strict reader: a block that does not
        reach today is not a horizon (§5).
        """
        blocks, _dropped = self._db.get_covering_mesocycles(today, None)
        return max((b['end_date'] for b in blocks), default=None)

    def accommodation_plan(
        self, today: str, constraints: List[Dict[str, Any]]
    ) -> AccommodationPlan:
        """Sorts directives into the passes that can run and the reasons the rest cannot.

        Here rather than in the CLI because deciding how many coach calls to make, over
        which ranges, is not a rendering question — and because the caller answering it
        for itself is what let the two sides disagree about where the plan ends
        (`AccommodationPlan`, §4.1). The caller still chooses WHICH directives; this
        decides what can be done with them.
        """
        windowed, spent = [], []
        for c in constraints:
            window = honoring.constraint_window(c['start_date'], c['end_date'], today)
            if not window:
                spent.append(c)
                continue
            windowed.append((window, c))
        windowed.sort(key=lambda item: item[0])

        # Merge overlapping windows into one pass: a second pass over shared days would
        # preview a plan the first had not yet applied (§4.1).
        merged: List[Tuple[str, str, List[Dict[str, Any]]]] = []
        for (start, end), constraint in windowed:
            if merged and start <= merged[-1][1]:
                prev_start, prev_end, members = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end), members + [constraint])
                continue
            merged.append((start, end, [constraint]))

        passes, ungoverned = [], []
        for start, end, members in merged:
            # The single governance consult (§4.2): the blocks resolved here ride on the
            # pass, and `workout_accommodate` consumes them rather than asking again. A
            # pass with ANY governed day runs over its whole window (§5); only a wholly
            # ungoverned one — a gap in the plan, or past its end — has nothing to
            # reshuffle towards.
            blocks, _dropped = self._db.get_covering_mesocycles(start, end)
            if not blocks:
                ungoverned.extend(members)
                continue
            passes.append(AccommodationPass(start, end, tuple(members), tuple(blocks)))

        return AccommodationPlan(
            plan_end=self.plan_horizon(today),
            passes=tuple(passes),
            ungoverned=tuple(ungoverned),
            spent=tuple(spent),
        )

    def workout_accommodate(
        self, range_start: str, range_end: str,
        constraint_ids: Optional[List[int]] = None,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> RevisionProposal:
        """Proposes a reshuffle of the sessions in one already-resolved §5 window.

        The caller resolves the window via `honoring.constraint_window`, because merging
        two constraints whose windows overlap is a decision about the pass, not about the
        arithmetic. `constraint_ids` are the constraints this pass honors; they ride onto
        the proposal so apply stamps exactly the set decided here (§8). `blocks` are the
        governing blocks `accommodation_plan` already resolved — governance is decided
        once, there (§4.2); the self-fetch below is a backstop for direct callers only.
        """
        if blocks is None:
            # The strict reader: a block that does not cover the window is not something
            # to reshuffle towards, and the nearest-block fallback would hand us one (§5).
            blocks, _dropped = self._db.get_covering_mesocycles(range_start, range_end)
        if not blocks:
            raise ValueError(
                "No active periodization strategy found. Run "
                + cmd("plan generate") + " first."
            )

        # Every constraint overlapping the window, not only those being honored: a spill
        # day may belong to a neighbouring rest window (§5).
        constraints = self._db.get_constraints(range_start, range_end)
        honoring_now = [
            c for c in constraints
            if constraint_ids is None or c['id'] in constraint_ids
        ]
        if constraint_ids is not None and not honoring_now:
            raise ValueError(
                f"None of the named constraint(s) fall inside {range_start}..{range_end} "
                "— they may have been edited or removed since this pass was planned."
            )

        planned_workouts = self._db.get_workouts(
            start_date=range_start, end_date=range_end
        )

        ctx = self._coach_context(constraints, blocks=blocks)
        decision = self.engine._workout_accommodate_logic(
            range_start=range_start,
            range_end=range_end,
            constraint_titles=[c['title'] for c in honoring_now],
            planned_workouts=planned_workouts,
            objectives=ctx.objectives,
            constraints=constraints,
            guidelines=ctx.guidelines,
            profile=ctx.profile,
            strategy=ctx.strategy,
            meso_text=ctx.meso_text,
            learnings=ctx.learnings,
        )

        reason = decision.get("reason", "No change needed.")
        revised: List[Dict[str, Any]] = []
        if decision.get("change_needed"):
            revised = decision.get("revised_workouts") or []

        # Integers, before the no-op backstop and the preview both read these numbers.
        normalize_load_fields(revised)

        # Both sides, unlike adapt, whose window starts on the evaluation date: this one
        # starts TOMORROW, so a proposal dated today would otherwise sail through. §5
        # states the intent; this enforces it (§7).
        revised = [
            w for w in revised
            if range_start <= str(w.get("date", "")) <= range_end
        ]
        # A session re-listed verbatim is not a change.
        revised = [w for w in revised if self._revision_is_change(w)]
        # Deterministic backstop, not a fast path (§7): every `rest = 1` day in range
        # becomes rest whatever the model returned. The LLM still always runs — the
        # pre-pass guarantees the rest days, not where the load they displace goes.
        revised = self._enforce_rest_windows_revision(
            revised, planned_workouts, constraints, None, range_start
        )

        structured = structure_revision(revised, reason)
        pairs, removals = pair_revisions(structured, planned_workouts)
        return RevisionProposal(
            reason=reason,
            workouts=structured,
            range_start=range_start,
            range_end=range_end,
            pairs=pairs,
            removals=removals,
            covered_constraint_ids=honoring.covered_ids(
                honoring_now, range_start, range_end
            ),
            # A reschedule is not an easing, and its own change kind is what says so —
            # no flag to carry or forget (DESIGN_workout_revisions.md §7).
            kind="accommodate",
            window_workouts=tuple(planned_workouts),
        )
