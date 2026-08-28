from datetime import datetime, timedelta
from typing import Any, List, Optional, Dict, Tuple
from trainmate.config import config
from trainmate.adherence import analyze_adherence, format_discrepancies
from trainmate.sports import canonical_sport
from trainmate import intensity
from trainmate.util import yellow, cmd
from trainmate.coach.formatting import format_baseline
from trainmate.coach import honoring
from trainmate.coach.proposals import RevisionProposal
from trainmate.coach.revisions import (
    held_slots, normalize_load_fields, pair_revisions, structure_revision,
)
from trainmate.db.workouts import ATHLETE_VOID_KINDS
import trainmate.coach.service as _svc


class AdaptationMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

    @staticmethod
    def _is_keep_marker(proposal: Dict[str, Any]) -> bool:
        """True for `{"date", "sport_type", "keep": true}` — hold this session, don't
        rewrite it. Carries no prescription, so it cannot drift into a spurious
        adaptation the way a verbatim re-list does (DESIGN_workout_revisions.md §9.1)."""
        return bool(proposal.get('keep'))

    def _revision_is_change(self, proposal: Dict[str, Any]) -> bool:
        """True unless `proposal` exactly reproduces an existing same-sport session.

        Backstops the adaptation prompt's "return only changed sessions" rule: a verbatim
        (or cosmetic-only) re-list of an unchanged session is treated as a no-op so it is
        not re-stamped as adapted or re-synced. A sport swap (no same-sport original) or a
        proposal on a date with no current session is always a real change.
        """
        existing = self._db.get_workout(proposal['date'], proposal['sport_type'])
        if not existing:
            return True
        if canonical_sport(existing['sport_type']) != canonical_sport(proposal['sport_type']):
            return True

        def _norm_text(v: Any) -> str:
            return " ".join(str(v or "").split())

        def _norm_num(v: Any) -> Optional[float]:
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        if _norm_text(proposal.get('title')) != _norm_text(existing.get('title')):
            return True
        if _norm_text(proposal.get('description')) != _norm_text(existing.get('description')):
            return True
        for field in ('duration_minutes', 'rpe', 'tss'):
            if _norm_num(proposal.get(field)) != _norm_num(existing.get(field)):
                return True
        return False

    def workout_adapt(
        self, target_date_str: Optional[str] = None, message: Optional[str] = None
    ) -> RevisionProposal:
        """Evaluates metrics/activities over a rolling window and adapts mesocycle if needed.

        `message` is an optional free-text note from the athlete, passed to the SAME LLM
        call as advisory intent for today (DESIGN_constraints.md §8 — no separate
        classification pass). That one call may also extract constraint-shaped directives
        from the note; they come back as the third element, raw and UNCONFIRMED — the
        caller must confirm each with the athlete (echo + y/N) before persisting it via
        `capture_message_constraint`, per the two-confirmation flow. Nothing here creates
        a constraint row or triggers a plan regen on its own.
        """
        if not target_date_str:
            target_date_str = _svc._today_str()

        target_date_obj = datetime.strptime(target_date_str, "%Y-%m-%d").date()

        # Fetch metrics history window
        history_days = config.metrics_lookback_days
        start_date_obj = target_date_obj - timedelta(days=history_days - 1)
        start_date_str = start_date_obj.strftime("%Y-%m-%d")

        metrics = self._db.get_metrics_cache(
            start_date=start_date_str, end_date=target_date_str
        )
        completed_activities = self._db.get_completed_activities(
            start_date=start_date_str, end_date=target_date_str
        )

        # External daily signals over the window, so the adaptation can tell a
        # lifestyle-suppressed morning (alcohol/poor sleep the day before) from genuine
        # training fatigue and avoid cutting load on a non-training artifact. Recovery
        # lags the signal by a day, so reach one day before the metrics window to cover
        # the first morning's preceding-day signal.
        signal_start_str = (start_date_obj - timedelta(days=1)).strftime("%Y-%m-%d")
        daily_signals = self._db.get_daily_signals(
            start_date=signal_start_str, end_date=target_date_str
        )

        # Determine mesocycle end date for the adaptation range. The plan we adapt runs
        # FORWARD from the target date to here, so workouts must be fetched across the
        # whole span (lookback start -> mesocycle end), not just the backward window —
        # otherwise the LLM never sees already-scheduled future sessions and reinvents
        # them from scratch (losing their sport/title and overwriting the athlete's plan).
        # No plan, no adaptation: every judgement below is relative to the block — its
        # focus, its remaining days, what a cut can still rebound from — so without one
        # there is nothing to adapt *towards* (DESIGN_block_boundary.md §6).
        active_meso = self._db.get_active_mesocycle(target_date_str)
        if not active_meso:
            raise ValueError(
                "No active periodization strategy found. Run "
                + cmd("plan generate") + " first."
            )
        meso_end_date_str = active_meso['end_date']

        # The BACKWARD window — from the adherence lookback start, removed rows included —
        # not the forward one the proposal carries. Two different ranges, so two names.
        lookback_workouts = self._db.get_workouts(
            start_date=start_date_str, end_date=meso_end_date_str, include_removed=True
        )
        planned_workouts = [w for w in lookback_workouts if not w.get('removed')]
        # Only the cancellations the ATHLETE made. A day the plan simply stopped
        # scheduling is a void too, and telling the coach it was cancelled would put words
        # in the athlete's mouth (DESIGN_workout_revisions.md §3).
        removed_workouts = [
            w for w in lookback_workouts
            if w.get('removed') and w.get('change_kind') in ATHLETE_VOID_KINDS
        ]

        baseline = self._db.get_baseline(target_date_str)
        baseline_str = format_baseline(baseline)

        # Planned blocks overlapping the window. Activities on dates outside every block
        # are history the plan never governed (e.g. before tool adoption), so they are
        # reported as informational rather than as "unplanned" deviations.
        covered_ranges = self._db.get_mesocycle_ranges(start_date_str, target_date_str)

        # Match planned workouts vs completed activities and compute discrepancies.
        # analyze_adherence only inspects the `history_days` backward window, so the
        # future-dated workouts now in `planned_workouts` are ignored here (no false misses).
        # The window ENDS on the evaluation date, though, so today's not-yet-trained
        # sessions are pending, not missed — adapt runs in the morning.
        discrepancies, matching_results, informational = analyze_adherence(
            planned_workouts=planned_workouts,
            completed_activities=completed_activities,
            start_date_obj=start_date_obj,
            history_days=history_days,
            minor_activity_load_threshold=config.minor_activity_load_threshold,
            covered_ranges=covered_ranges,
            pending_from=target_date_str,
        )

        # Sessions that already have a matching completed Garmin activity are history and
        # cannot be adapted: the evaluation date is the first day of the adaptation range,
        # but the athlete may have already trained today. Without this lock the LLM
        # "adapts" a finished session (typically restating it to match the actual ride),
        # which is meaningless and, on apply, rewrites a past calendar event.
        completed_keys = {
            (m["date"], canonical_sport(m["planned"]["sport_type"]))
            for m in matching_results if m["completed"] is not None
        }

        objectives = self._db.upcoming_objectives()

        # Determine the fallback next_goal for passing to the prompt generator
        next_goal = None
        for obj in objectives:
            if obj['id'] is not None:
                macro = self._db.get_macrocycle_for_objective(obj['id'])
                if macro:
                    next_goal = obj
                    break
        if not next_goal and objectives:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]

        # Active constraints overlapping the adaptation window (target date → mesocycle
        # end), the single directive read path shared with generate (§6).
        constraints = self._db.get_constraints(target_date_str, meso_end_date_str)
        ctx = self._coach_context(
            constraints, objectives=objectives,
            objective_id=next_goal['id'] if next_goal else None,
        )
        guidelines, profile = ctx.guidelines, ctx.profile
        strategy, meso_text, learnings = ctx.strategy, ctx.meso_text, ctx.learnings

        # §8: the athlete's note is passed straight through as advisory intent — no
        # separate classification pass. The same LLM call also extracts any
        # constraint-shaped directives from it (see `new_constraints` below).
        pmc_cutoff, pmc_context = self._pmc_prompt_context(target_date_str)
        # The block's measured intensity distribution — adapt's primary view, since drift
        # caught in week 2 is correctable and drift diagnosed at plan-generation time is
        # history (DESIGN_intensity_distribution.md §9). Threaded as its own argument, NOT
        # folded into meso_text: that string is shared with plan generation, which §9.2
        # says must not grow this section.
        intensity_context = self._intensity_block_context(target_date_str)
        decision = self.engine._workout_adapt_logic(
            target_date_str=target_date_str,
            history_days=history_days,
            start_date_str=start_date_str,
            metrics=metrics,
            completed_activities=completed_activities,
            planned_workouts=planned_workouts,
            baseline_str=baseline_str,
            meso_end_date_str=meso_end_date_str,
            objectives=objectives,
            guidelines=guidelines,
            profile=profile,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            discrepancies=format_discrepancies(discrepancies),
            informational=informational,
            removed_workouts=removed_workouts,
            daily_signals=daily_signals,
            completed_keys=completed_keys,
            athlete_message=message,
            constraints=constraints,
            pmc_warmup_cutoff=pmc_cutoff,
            pmc_context=pmc_context,
            intensity_context=intensity_context,
            zone_currencies=self._planning_zone_currencies(target_date_str)
        )

        # NOTE: daily adaptation is read-only w.r.t. coach learnings
        # (DESIGN_evidence_based_confidence.md §2/§11). It consumes the rendered learnings as
        # context but authors none — durable, evidence-backed observations are written only by
        # the weekly history analysis (`data bootstrap` / `data reflect`), which can attribute
        # them to specific training weeks.

        reason = decision.get("reason", "No adaptation needed.")
        adapted = []
        if decision.get("change_needed"):
            adapted = decision.get("adapted_workouts", [])

        # Integers, before the no-op backstop and the preview both read these numbers.
        normalize_load_fields(adapted)

        # Drop any proposal dated past the adaptation range: the next block is out of reach
        # and was never shown to the model, so a post-boundary date is a hallucination
        # (DESIGN_block_boundary.md §1).
        adapted = [w for w in adapted if str(w.get("date", "")) <= meso_end_date_str]

        # Drop any proposal that targets an already-completed session — those are locked
        # history (see completed_keys above). This is the load-bearing guard: it holds even
        # if the model ignores the prompt instruction not to adapt finished sessions.
        if completed_keys:
            adapted = [
                w for w in adapted
                if (w.get("date"), canonical_sport(w.get("sport_type", "")))
                not in completed_keys
            ]

        # Held, not dropped (DESIGN_workout_revisions.md §9.1). A session named only to keep
        # it — a keep marker, or the verbatim re-list the no-op backstop catches — appends
        # no revision but stays SPOKEN FOR, because the same list decides what its date
        # keeps. Filtered out instead, it was deleted by the change it was protecting.
        held: List[Tuple[str, str]] = []
        changed: List[Dict[str, Any]] = []
        for w in adapted:
            if self._is_keep_marker(w) or not self._revision_is_change(w):
                held.append((w.get("date", ""), canonical_sport(w.get("sport_type", ""))))
            else:
                changed.append(w)
        adapted = changed

        # Deterministic rest-window pre-pass (§6): force rest onto any future,
        # not-yet-completed planned session that falls under a `rest` constraint, so the
        # guarantee holds regardless of what the model proposed.
        adapted = self._enforce_rest_windows_revision(
            adapted, planned_workouts, constraints, completed_keys, target_date_str
        )
        # A forced-rest day is cleared outright, so nothing on it is held: the constraint
        # outranks the coach's wish to keep the session (§6 over §9.1).
        forced_rest = self._forced_rest_days(
            planned_workouts, constraints, completed_keys, target_date_str
        )
        held = [(date, sport) for date, sport in held if date not in forced_rest]

        # §8: constraint-shaped directives the same LLM call extracted from the athlete's
        # note, if any — raw and UNCONFIRMED. The caller must confirm each with the
        # athlete before persisting it (via capture_message_constraint); nothing here
        # writes a row.
        new_constraints = (decision.get("new_constraints") or []) if message else []

        # The row shape both revision commands write, built in one place (§7).
        structured = structure_revision(adapted, reason)

        # Pair here, once, against the same range apply will act on — the CLI used to
        # re-derive this rule for its preview and rebuild a narrower range from the
        # proposal dates, so the two could disagree about which sessions disappear.
        window_workouts = self._db.get_workouts(
            start_date=target_date_str, end_date=meso_end_date_str
        )
        pairs, removals = pair_revisions(structured, window_workouts, held)
        return RevisionProposal(
            reason=reason,
            workouts=structured,
            new_constraints=tuple(new_constraints),
            range_start=target_date_str,
            range_end=meso_end_date_str,
            pairs=pairs,
            removals=removals,
            held=tuple(held),
            # Decided at proposal time so apply stamps this list rather than re-deriving
            # it from a set that may have been edited since (§8).
            covered_constraint_ids=honoring.covered_ids(
                constraints, target_date_str, meso_end_date_str
            ),
        )

    def workout_revision_apply(
        self, proposal: RevisionProposal
    ) -> None:
        """Appends a revision's sessions under one change, and lets Calendar follow.

        Takes the whole proposal so the range and the displacement decisions are the ones
        the coach actually made, not a reconstruction.

        A session the pass drops becomes a void revision rather than a `DELETE`, and one it
        substitutes cross-sport becomes a void at the source plus a revision at the
        destination carrying the same lineage — the swap shape, which is what keeps the
        adaptation tally following the session (DESIGN_workout_revisions.md §4/§11). The
        voids go first, so a moved session's newest revision is always the copy (§8).
        """
        proposed_workouts = proposal.workouts
        start_date, end_date = proposal.range_start, proposal.range_end
        # Read before the change opens, so every decision below is made against the plan
        # as it stood, not against rows this pass has already appended.
        existing_workouts = self._db.get_workouts(
            start_date=start_date, end_date=end_date
        )
        by_slot = {
            (w['date'], canonical_sport(w['sport_type'])): w for w in existing_workouts
        }

        proposed_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for pw in proposed_workouts:
            proposed_by_date.setdefault(pw['date'], []).append(pw)
        # Sessions the coach kept as planned. They append nothing, but they are spoken for,
        # so the displacement rule below must not read them as sessions it wants gone
        # (§9.1) — the same union the preview made in `pair_revisions`.
        held_by_date = held_slots(proposal.held)

        # The session displaced on each date, so a cross-sport substitution can carry its
        # lineage to the sport it becomes.
        displaced_by_date: Dict[str, Dict[str, Any]] = {}
        for ew in existing_workouts:
            if ew['date'] not in proposed_by_date:
                continue
            # Compare canonically so a proposal for 'strength_training' is recognized as
            # adapting an existing 'strength' session, not overriding it.
            proposed_sports = {
                canonical_sport(p['sport_type']) for p in proposed_by_date[ew['date']]
            }
            proposed_sports |= held_by_date.get(ew['date'], set())
            if canonical_sport(ew['sport_type']) not in proposed_sports:
                displaced_by_date.setdefault(ew['date'], ew)

        with self._db.workout_change(
            kind="adapt", summary=proposal.reason
        ) as change:
            for ew in displaced_by_date.values():
                print(yellow(
                    f"Removing overridden workout: {ew['title']} ({ew['sport_type']}) "
                    f"on {ew['date']}"
                ))
                change.void(
                    date=ew['date'], sport_type=ew['sport_type'],
                    reason=proposal.reason,
                )

            for w in proposed_workouts:
                slot = (w['date'], canonical_sport(w['sport_type']))
                existing = by_slot.get(slot)
                # No same-sport session to revise: this proposal swapped in a new sport.
                # It IS the displaced session, in a different sport, so it carries that
                # session's lineage — which is what keeps its originals and its tally (§4).
                # Popped, not read: a date's displaced session can only become ONE of the
                # sessions replacing it, and handing its lineage to two would leave one
                # session live in two slots (§10). A second new-sport proposal that day is
                # a session in its own right and starts its own lineage.
                displaced = None if existing else displaced_by_date.pop(w['date'], None)
                zone_currency, zone_sec = intensity.parse_planned_zones(w)
                # The flag belongs to the TEST, not to the slot: a returned change on a
                # benchmark's date that does not re-emit benchmark_type is the model saying
                # this session is no longer that test, so blank it rather than let the
                # carry-forward resurrect it onto a replacement
                # (DESIGN_benchmark_workouts.md §4.2).
                clear_benchmark = bool(
                    existing and existing['benchmark_type'] and not w.get('benchmark_type')
                )
                change.append(
                    date=w['date'],
                    sport_type=w['sport_type'],
                    title=w['title'],
                    description=w['description'],
                    duration_minutes=w.get('duration_minutes'),
                    rpe=w.get('rpe'),
                    tss=w.get('tss'),
                    reason=w.get('modification_reason'),
                    benchmark_type=w.get('benchmark_type'),
                    clear_benchmark=clear_benchmark,
                    lineage_id=displaced['id'] if displaced else None,
                    # A drift correction rewrites HOW a session is prescribed, so its zone
                    # target moves with it; omitted, the carry-forward preserves what the
                    # plan already held (DESIGN_intensity_distribution.md §9.8).
                    planned_zone_currency=zone_currency,
                    planned_zone_sec=zone_sec,
                )

        # Stamped on the athlete's `y`, never on a proposal they declined (§8).
        honoring.stamp(self._db, proposal.covered_constraint_ids)

    def workout_revision_record_no_change(self, proposal: RevisionProposal) -> None:
        """Records a pass that proposed nothing. Every revision command's no-change branch
        calls this, and it is the only write on that path.

        Two writes, neither of them a workout. The change row is written even though it
        appends nothing: an adapt that looked at the metrics and held is a real event, and
        `workout batches` reads it back as `(held)` — a run of them then says what it is
        rather than saying nothing (DESIGN_workout_revisions.md §3).

        And the pass still had the constraints in scope with authority over them, which is
        all `honored_at` claims — requiring a *change* would leave "no adaptation needed"
        flagged forever (§8). Separate from `workout_revision_apply` because there is
        nothing to apply, and outside the propose call because a propose writes nothing.
        """
        with self._db.workout_change(kind="adapt", summary=proposal.reason):
            pass
        honoring.stamp(self._db, proposal.covered_constraint_ids)
