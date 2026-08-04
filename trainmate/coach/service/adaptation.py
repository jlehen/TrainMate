import json
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.db import db
from trainmate.google_calendar import calendar_syncer
from trainmate.types import Objective, Constraint, Workout
from trainmate.adherence import analyze_adherence, planned_load
from trainmate.sports import canonical_sport
from trainmate.modification_state import SWAP_REASON_PREFIX, MANUAL_REPLACE_REASON_PREFIX
from trainmate import garmin, intensity
from trainmate.garmin import activity_load
from trainmate.util import (
    today_str as _today_str, today_date as _today_date,
    cyan, green, yellow, bold, red, gray, cmd, PMC_TSB_LAG_NOTE,
)
from trainmate.coach.engine import CoachEngine
from trainmate.coach.formatting import format_baseline, _load_science_guidelines
import trainmate.coach.service as _svc


class AdaptationMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

    def _adapt_is_change(self, proposal: Dict[str, Any]) -> bool:
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
    ) -> Tuple[str, List[Workout], List[Dict[str, Any]]]:
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

        # External daily-context signals over the window, so the adaptation can tell a
        # lifestyle-suppressed morning (alcohol/poor sleep the day before) from genuine
        # training fatigue and avoid cutting load on a non-training artifact. Recovery
        # lags the signal by a day, so reach one day before the metrics window to cover
        # the first morning's preceding-day signal.
        context_start_str = (start_date_obj - timedelta(days=1)).strftime("%Y-%m-%d")
        daily_context = self._db.get_daily_context(
            start_date=context_start_str, end_date=target_date_str
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

        window_workouts = self._db.get_workouts(
            start_date=start_date_str, end_date=meso_end_date_str, include_removed=True
        )
        planned_workouts = [w for w in window_workouts if not w.get('removed')]
        removed_workouts = [w for w in window_workouts if w.get('removed')]

        baseline = self._db.get_baseline(target_date_str)
        baseline_str = format_baseline(baseline)

        # Planned blocks overlapping the window. Activities on dates outside every block
        # are history the plan never governed (e.g. before tool adoption), so they are
        # reported as informational rather than as "unplanned" deviations.
        covered_ranges = self._db.get_mesocycle_ranges(start_date_str, target_date_str)

        # Match planned workouts vs completed activities and compute discrepancies.
        # analyze_adherence only inspects the `history_days` backward window, so the
        # future-dated workouts now in `planned_workouts` are ignored here (no false misses).
        discrepancies, matching_results, informational = analyze_adherence(
            planned_workouts=planned_workouts,
            completed_activities=completed_activities,
            start_date_obj=start_date_obj,
            history_days=history_days,
            minor_activity_load_threshold=config.minor_activity_load_threshold,
            covered_ranges=covered_ranges,
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

        objectives = self._db.get_objectives(status='active')

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

        guidelines = self._load_science_guidelines()
        profile = self._effective_profile()
        objective_id = next_goal['id'] if next_goal else None
        strategy, meso_text = self._get_active_strategy_and_meso_text(
            objectives, objective_id=objective_id
        )
        learnings = self._get_learnings_text()

        # Active constraints overlapping the adaptation window (target date → mesocycle
        # end), the single directive read path shared with generate (§6).
        constraints = self._db.get_constraints(target_date_str, meso_end_date_str)

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
            discrepancies=discrepancies,
            informational=informational,
            removed_workouts=removed_workouts,
            daily_context=daily_context,
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

        # No-op backstop: the prompt tells the model to return ONLY changed sessions, but
        # if it re-lists one verbatim (or with cosmetic-only churn) anyway, drop it here so
        # an unchanged session is never re-stamped as adapted or needlessly re-synced. A
        # proposal is a real change unless it matches an EXISTING same-sport session on every
        # meaningful field; a sport swap (no same-sport original) or a brand-new date always
        # counts as a change and is kept.
        adapted = [w for w in adapted if self._adapt_is_change(w)]

        # Deterministic rest-window pre-pass (§6): force rest onto any future,
        # not-yet-completed planned session that falls under a `rest` constraint, so the
        # guarantee holds regardless of what the model proposed.
        adapted = self._enforce_rest_windows_adapt(
            adapted, planned_workouts, constraints, completed_keys, target_date_str
        )

        # §8: constraint-shaped directives the same LLM call extracted from the athlete's
        # note, if any — raw and UNCONFIRMED. The caller must confirm each with the
        # athlete before persisting it (via capture_message_constraint); nothing here
        # writes a row.
        new_constraints = (decision.get("new_constraints") or []) if message else []

        # Filter and structure returned workouts
        return reason, [
            {
                'id': None,
                'date': w['date'],
                'sport_type': w['sport_type'],
                'title': w['title'],
                'description': w['description'],
                'original_description': w['description'],
                # Per-workout note; the long batch rationale travels separately as the
                # returned `reason` and is stamped onto adaptation_summary at apply time.
                # Fall back to the batch reason so an adapted session is never left with a
                # NULL modification_reason (the load-bearing "modified?" flag) if the model
                # omits a per-workout change_reason.
                'modification_reason': w.get('change_reason') or reason,
                'adaptation_summary': reason,
                'google_event_id': None,
                'duration_minutes': w.get('duration_minutes'),
                'rpe': w.get('rpe'),
                'tss': w.get('tss'),
                # Carry the benchmark flag through the rebuild: a moved test must stay a
                # test. The model owns its survival by re-emitting it (§3.1/§4.2); if it
                # omits it on a same-row change, save_workout's COALESCE preserves the
                # stored value. It is dropped only on a genuine sport swap, which is correct.
                'benchmark_type': w.get('benchmark_type')
            } for w in adapted
        ], new_constraints

    def workout_adapt_apply(
        self, proposed_workouts: List[Dict[str, Any]], reason: str,
        start_date: str, end_date: str
    ) -> None:
        """Saves proposed adapted workouts, cleans up overridden ones, and syncs to Calendar."""
        # 1. Fetch all existing workouts in the adaptation range
        existing_workouts = self._db.get_workouts(
            start_date=start_date, end_date=end_date
        )

        # One timestamp for the whole run, stamped on every session it EASES (see the
        # per-session `eased` test below). Lets a re-run see how recently (and how many
        # times) each session was already adapted and hold back from compounding the cut
        # (see save_workout / the adapt prompt).
        adapted_at = datetime.now(timezone.utc).isoformat()

        # Group proposed workouts by date
        proposed_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for pw in proposed_workouts:
            proposed_by_date.setdefault(pw['date'], []).append(pw)

        # 2. Find and delete existing workouts that are being replaced or removed.
        # A cross-sport swap (e.g. strength -> yoga) deletes the planned session and
        # inserts a fresh one, which would otherwise lose all trace of what was planned.
        # Remember the displaced session per date so its replacement can carry the
        # originally-planned description + load through to the Calendar event.
        displaced_by_date: Dict[str, Dict[str, Any]] = {}
        for ew in existing_workouts:
            ew_date = ew['date']
            if ew_date in proposed_by_date:
                # Compare canonically so a proposal for 'strength_training' is recognized
                # as adapting an existing 'strength' session (not overriding/deleting it).
                proposed_sports = {
                    canonical_sport(p['sport_type']) for p in proposed_by_date[ew_date]
                }
                if canonical_sport(ew['sport_type']) not in proposed_sports:
                    print(yellow(f"Removing overridden workout: {ew['title']} ({ew['sport_type']}) "
                          f"on {ew_date}"))
                    displaced_by_date.setdefault(ew_date, ew)
                    if ew.get('google_event_id'):
                        try:
                            self._calendar_syncer.delete_workout_event(ew['google_event_id'])
                        except Exception as e:
                            print(red(f"Error deleting Google Calendar event: {e}"))
                    self._db.delete_workout_by_id(ew['id'])

        # 3. Save new adapted workouts and sync them
        for w in proposed_workouts:
            existing = self._db.get_workout(w['date'], w['sport_type'])
            orig_desc = None
            orig_dur = orig_tss = orig_rpe = None
            ge_id = None
            if existing:
                orig_desc = existing['original_description'] or existing['description']
                ge_id = existing['google_event_id']
            else:
                # No same-sport session to adapt: this proposal swapped in a new sport.
                # If it displaced a planned session that day, inherit that session's
                # initially-planned description and load as this one's "original" snapshot,
                # so the Calendar event surfaces what was originally on the plan.
                displaced = displaced_by_date.get(w['date'])
                if displaced:
                    orig_desc = (
                        displaced.get('original_description') or displaced.get('description')
                    )
                    orig_dur = (
                        displaced.get('original_duration_minutes')
                        or displaced.get('duration_minutes')
                    )
                    orig_tss = displaced.get('original_tss') or displaced.get('tss')
                    orig_rpe = displaced.get('original_rpe') or displaced.get('rpe')
            # Origin is fixed at creation: preserve it when adapting an existing
            # session (None + COALESCE keeps 'manual'/'generated'); only a session
            # adapt newly introduces is coach-authored ('generated').
            source = None if existing else 'generated'

            # A drift correction rewrites the prescription without touching the load, so it
            # is not an easing — stamping it would raise the DO NOT COMPOUND bar for a
            # session that was never cut (DESIGN_intensity_distribution.md §9.5). Decided
            # here rather than in save_workout, which is a generic writer other callers
            # rely on. Compared against `existing`, not `original_*`: a session already cut
            # last week and merely re-worded today moved nothing now. A session adapt newly
            # introduced counts as eased — there is no prior form to compound.
            eased = not existing or (
                float(w.get('duration_minutes') or 0)
                != float(existing['duration_minutes'] or 0)
                or float(w.get('tss') or 0) != float(existing['tss'] or 0)
            )
            zone_currency, zone_sec = intensity.parse_planned_zones(w)

            self._db.save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                original_description=orig_desc or w['description'],
                original_duration_minutes=orig_dur,
                original_tss=orig_tss,
                original_rpe=orig_rpe,
                modification_reason=w.get('modification_reason'),
                adaptation_summary=reason,
                google_event_id=ge_id,
                duration_minutes=w.get('duration_minutes'),
                rpe=w.get('rpe'),
                tss=w.get('tss'),
                source=source,
                benchmark_type=w.get('benchmark_type'),
                adapted_at=adapted_at if eased else None,
                # A drift correction rewrites HOW a session is prescribed, so its zone
                # target moves with it; omitted, COALESCE preserves what the plan already
                # held (DESIGN_intensity_distribution.md §9.8). Note this leaves §9.5's
                # stopgap correct as written: `eased` compares duration and TSS only, so a
                # correction that rewrites the zones while holding both stays unstamped —
                # right, because nothing was cut.
                planned_zone_currency=zone_currency,
                planned_zone_sec=zone_sec
            )

            # Sync to Google Calendar
            updated = self._db.get_workout(w['date'], w['sport_type'])
            if updated:
                try:
                    self._calendar_syncer.sync_workout(updated)
                except Exception as e:
                    print(red(f"Error syncing {w['title']} to Google Calendar: {e}"))
