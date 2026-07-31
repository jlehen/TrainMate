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
from trainmate import garmin
from trainmate.garmin import activity_load
from trainmate.util import (
    today_str as _today_str, today_date as _today_date,
    cyan, green, yellow, bold, red, gray, cmd, PMC_TSB_LAG_NOTE,
)
from trainmate.coach.engine import CoachEngine
from trainmate.coach.formatting import format_baseline, _load_science_guidelines
import trainmate.coach.service as _svc


class WorkoutGenMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

    def _today_workout_completed(
        self, today_str: str, completed_activities: List[Dict[str, Any]]
    ) -> bool:
        """Returns True when today's planned session has a matching completed activity.

        Used by `workout_generate` to decide whether to protect today's workout from a
        regeneration: a session already in the books should be kept as history rather
        than overwritten. Completion is decided by `analyze_adherence` over a one-day
        window so it uses the exact same planned-vs-completed sport matching as the rest
        of the app. A day counts as completed only when a non-rest planned workout for
        today is paired with an activity; rest days and empty days have nothing to protect.
        """
        planned = [
            w for w in self._db.get_workouts(start_date=today_str, end_date=today_str)
            if w['sport_type'] != 'rest'
        ]
        if not planned:
            return False
        today_date_obj = datetime.strptime(today_str, "%Y-%m-%d").date()
        _, matching, _ = analyze_adherence(
            planned_workouts=planned,
            completed_activities=completed_activities or [],
            start_date_obj=today_date_obj,
            history_days=1,
            minor_activity_load_threshold=config.minor_activity_load_threshold,
        )
        return any(r['completed'] for r in matching)

    @staticmethod
    def _rest_workout(date: str, cause: str) -> Dict[str, Any]:
        """A deterministic rest session the rest-window pre-pass places on a date the
        athlete has barred from training (DESIGN_constraints.md §6). The title/description
        are tagged "(forced constraint)" so it reads unmistakably as a code-enforced
        override rather than an ordinary planned/adapted rest day. The change_reason names
        the constraint so a later adaptation, which won't see the live constraint list in
        the same run, reads the cause back with the plan."""
        title = 'Rest (forced constraint)'
        return {
            'date': date,
            'sport_type': 'rest',
            'title': title,
            'description': f"[{title}]\nNo training — {cause}.",
            'duration_minutes': 0,
            'rpe': 0,
            'tss': 0,
            'change_reason': f"Rest — {cause}.",
        }

    @classmethod
    def _hard_rest_windows(
        cls, constraints: List[Constraint]
    ) -> List[Tuple[str, str, str]]:
        """The `rest` windows — the only edge that skips the LLM (§5): a full no-training
        window whose dates are forced to rest. Every other constraint is advisory prose
        the LLM honors itself, so it is deliberately excluded here. Returns
        [(start, end, title)]."""
        return [
            (c['start_date'], c['end_date'], c['title'])
            for c in constraints
            if c.get('rest')
        ]

    @classmethod
    def _enforce_rest_windows_generate(
        cls, workouts: List[Dict[str, Any]], constraints: List[Constraint]
    ) -> List[Dict[str, Any]]:
        """Forces `rest` constraints onto a freshly generated workout list (§6): a rest
        window replaces its dates with a single rest, deterministically, bypassing the LLM
        for that date entirely. Every other constraint is advisory only — left to the model
        via the prompt block, not enforced here (§5). Operates only on dates the model
        actually scheduled, so it never invents days beyond the generated span."""
        full_rest = cls._hard_rest_windows(constraints)
        if not full_rest:
            return workouts
        out: List[Dict[str, Any]] = []
        rested: set = set()
        for w in workouts:
            day = w.get('date', '')
            fr = next((t for (s, e, t) in full_rest if s <= day <= e), None)
            if fr is not None:
                if day not in rested:
                    rested.add(day)
                    out.append(cls._rest_workout(day, f"constraint '{fr}'"))
                continue
            out.append(w)
        return out

    @classmethod
    def _enforce_rest_windows_adapt(
        cls, adapted: List[Dict[str, Any]], planned_workouts: List[Workout],
        constraints: List[Constraint], completed_keys: Optional[set], from_date: str
    ) -> List[Dict[str, Any]]:
        """Eases planned sessions to rest on `rest` constraint dates (§6). Every other
        constraint is advisory only (§5) — left to the model, not enforced here. Only
        touches sessions on or after `from_date` that aren't already rest or completed."""
        full_rest = cls._hard_rest_windows(constraints)
        if not full_rest:
            return adapted
        completed = completed_keys or set()
        rest_sport = canonical_sport('rest')

        forced_rest: Dict[str, str] = {}          # date -> constraint title
        for w in planned_workouts:
            day = w.get('date', '')
            if day < from_date:
                continue
            sport = canonical_sport(w.get('sport_type', ''))
            if sport == rest_sport or (day, sport) in completed:
                continue
            fr = next((t for (s, e, t) in full_rest if s <= day <= e), None)
            if fr is not None:
                forced_rest[day] = fr

        if not forced_rest:
            return adapted

        cleaned = []
        for w in adapted:
            day = w.get('date', '')
            if day in forced_rest:
                continue  # whole day replaced with rest below
            cleaned.append(w)

        for day, title in sorted(forced_rest.items()):
            cleaned.append(cls._rest_workout(day, f"constraint '{title}'"))
        return cleaned

    @staticmethod
    def _drop_benchmark_collisions(
        workouts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Same-day collision rule (§4.1): save_workout keys on (date, sport), so a second
        same-sport session on a benchmark's date would silently overwrite the test. On a
        date holding a benchmark of sport X, drop any OTHER proposed sport-X session and
        warn. The benchmark is identified by its flag — no guessing."""
        # date -> {canonical sport with a benchmark that day}
        bench_sports: Dict[str, set] = {}
        for w in workouts:
            if w.get('benchmark_type'):
                bench_sports.setdefault(w.get('date', ''), set()).add(
                    canonical_sport(w.get('sport_type', ''))
                )
        if not bench_sports:
            return workouts
        out: List[Dict[str, Any]] = []
        for w in workouts:
            day = w.get('date', '')
            sport = canonical_sport(w.get('sport_type', ''))
            if (not w.get('benchmark_type')
                    and sport in bench_sports.get(day, set())):
                print(yellow(
                    f"Dropping {w.get('sport_type', '')} session on {day}: it collides "
                    f"with a scheduled benchmark of the same sport."
                ))
                continue
            out.append(w)
        return out

    def _warn_missing_boundary_benchmarks(
        self, workouts: List[Dict[str, Any]], constraints: List[Constraint],
        macrocycle_id: int, gen_start: str
    ) -> None:
        """Boundary-week post-check (§4.1): warn — don't auto-insert — when a covered
        mesocycle-boundary week ended up with no benchmark. Same spirit as the rest-window
        pass, but a surfaced warning the athlete can act on (regenerate), not a silent fix.
        Stays quiet when the boundary week sits under a `rest` constraint — rest wins."""
        if not workouts:
            return
        mesocycles = self._db.get_mesocycles_for_macrocycle(macrocycle_id)
        if not mesocycles:
            return
        dated = [w for w in workouts if w.get('date')]
        span_end = max(w['date'] for w in dated)
        rest_windows = self._hard_rest_windows(constraints)
        for m in mesocycles:
            end = m.get('end_date', '')
            # Only boundary weeks whose end falls inside the generated span.
            if not (gen_start <= end <= span_end):
                continue
            end_obj = datetime.strptime(end, "%Y-%m-%d").date()
            win_start = (end_obj - timedelta(days=6)).strftime("%Y-%m-%d")
            # Rest wins: a full-rest window overlapping the boundary week silences the check.
            if any(s <= end and e >= win_start for (s, e, _t) in rest_windows):
                continue
            has_benchmark = any(
                w.get('benchmark_type') and win_start <= w['date'] <= end
                for w in dated
            )
            if not has_benchmark:
                print(yellow(
                    f"No benchmark scheduled in the boundary week of '{m.get('name', '')}' "
                    f"({win_start} to {end}). Consider regenerating — a block-boundary "
                    f"fitness test keeps your zones calibrated."
                ))

    def _archive_and_teardown(self, from_date: str) -> List[Workout]:
        """Archives every live workout from `from_date` on and deletes their Calendar events.

        The displaced rows keep their `macrocycle_id` tag and share one `archived_at` batch
        stamp, so a later rollback can resurrect exactly this set (DESIGN_plan_rollback.md).
        Returns the rows as they were before archival."""
        archived = self._db.archive_future_workouts(from_date)
        for ew in archived:
            if ew.get('google_event_id'):
                try:
                    self._calendar_syncer.delete_workout_event(ew['google_event_id'])
                except Exception as e:
                    print(red(f"Error deleting Google Calendar event: {e}"))
        return archived

    def workout_rollback(self, batch: Optional[str] = None) -> Dict[str, Any]:
        """Restores a previously archived batch of workouts, undoing a regeneration.

        The sibling of `plan_rollback` on the workout axis: it swaps the live upcoming
        sessions for an archived batch — the most recent one by default, or the batch
        stamped `batch` — without touching the active plan version, so it also undoes a
        regeneration that never changed the strategy (see DESIGN_plan_rollback.md §9).
        The current sessions are archived (their Calendar events torn down) and the
        target batch is resurrected and re-pushed, from today onward.

        Returns {batch, restored_workouts, archived_workouts, first_date, last_date}.
        Raises ValueError when there is no batch to restore.
        """
        today_str = _svc._today_str()
        batches = self._db.get_archived_batches(from_date=today_str)
        if not batches:
            raise ValueError(
                "No archived workouts to roll back to — nothing has displaced the "
                "current sessions yet."
            )

        # Resolved before anything is archived: the archive below stamps a newer batch,
        # which would otherwise become the default target and restore what it just
        # displaced.
        if batch is not None:
            target = next((b for b in batches if b['archived_at'] == batch), None)
            if not target:
                raise ValueError(f"No archived workout batch stamped {batch}.")
        else:
            target = batches[0]

        if not target['restorable']:
            raise ValueError(
                f"Every workout in that batch ({target['first_date']}..."
                f"{target['last_date']}) is in the past — there is nothing to restore."
            )

        archived = self._archive_and_teardown(today_str)
        restored = self._db.restore_workout_batch(target['archived_at'], today_str)
        if restored:
            try:
                self._calendar_syncer.sync_multiple(restored)
            except Exception as e:
                print(red(f"Error syncing to Google Calendar: {e}"))

        return {
            'batch': target['archived_at'],
            'restored_workouts': len(restored),
            'archived_workouts': len(archived),
            'first_date': min((w['date'] for w in restored), default=None),
            'last_date': max((w['date'] for w in restored), default=None),
        }

    def workout_generate(
        self, objective_id: Optional[int] = None, end_date: Optional[str] = None
    ) -> Tuple[str, List[Workout]]:
        """Generates workouts (microcycles) based on the active strategy."""
        # Identify the target goal
        if objective_id is not None:
            next_goal = self._db.get_active_objective(objective_id)
            if not next_goal:
                next_goal = self._db.get_objective(objective_id)
                if not next_goal:
                    raise ValueError(f"Active goal with ID {objective_id} not found.")
        else:
            next_goal = self._db.get_active_objective()
            if not next_goal:
                return "No active goals found. TrainMate needs at least one objective.", []

        # Verify active periodization strategy exists
        macrocycle = self._db.get_macrocycle_for_objective(next_goal['id'])
        if not macrocycle:
            raise ValueError(
                "No active periodization strategy found. Run "
                + cmd("plan generate") + " first."
            )

        today_str = _svc._today_str()
        today_date_obj = datetime.strptime(today_str, "%Y-%m-%d").date()

        # Retrieve recent history context
        history_days = config.metrics_lookback_days
        start_date_obj = today_date_obj - timedelta(days=history_days - 1)
        start_date_str = start_date_obj.strftime("%Y-%m-%d")

        metrics = self._db.get_metrics_cache(
            start_date=start_date_str, end_date=today_str
        )
        completed_activities = self._db.get_completed_activities(
            start_date=start_date_str, end_date=today_str
        )
        baseline = self._db.get_baseline(today_str)

        # A regeneration normally replaces every workout from today onward, but a session
        # the athlete has already completed should be preserved as history rather than
        # overwritten. When today's planned workout is already in the books, start the
        # regenerated plan tomorrow and leave today's row (and its Calendar event) intact.
        gen_start_str = today_str
        if self._today_workout_completed(today_str, completed_activities):
            gen_start_str = (today_date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
            print(green(
                f"Today's workout is already completed — preserving it and regenerating "
                f"from {gen_start_str}."
            ))
        gen_start_obj = datetime.strptime(gen_start_str, "%Y-%m-%d").date()

        if end_date is not None:
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
            num_days = max(1, (end_date_obj - gen_start_obj).days)
        else:
            num_days = config.workout_generation_span_days

        constraints = self._db.get_constraints(gen_start_str)
        guidelines = self._load_science_guidelines()
        profile = self._effective_profile()
        self._maybe_nudge_no_threshold()

        # We need all objectives for _get_active_strategy_and_meso_text context
        objectives = self._db.get_objectives(status='active')
        strategy, meso_text = self._get_active_strategy_and_meso_text(
            objectives, objective_id=objective_id
        )
        learnings = self._get_learnings_text()

        pmc_cutoff, pmc_context = self._pmc_prompt_context(today_str)
        plan_data = self.engine._workout_generate_logic(
            objectives=objectives,
            constraints=constraints,
            today_str=today_str,
            start_str=gen_start_str,
            guidelines=guidelines,
            profile=profile,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            num_days=num_days,
            metrics=metrics,
            completed_activities=completed_activities,
            baseline=baseline,
            pmc_warmup_cutoff=pmc_cutoff,
            pmc_context=pmc_context
        )

        # NOTE: workout generation is read-only w.r.t. coach learnings (see
        # DESIGN_backward_evaluation.md §11) — it does not apply learning_updates. Durable
        # memory is authored only by `analyze` and `plan generate`.

        # Save workouts to database
        workouts = plan_data.get("workouts", [])

        # Guard the preserved day: when today's completed session is being kept, drop any
        # workout the model mistakenly dated before the generation start. save_workout
        # matches on date+sport, so a stray today-dated row would silently overwrite the
        # completed session we deliberately kept.
        if gen_start_str != today_str:
            workouts = [w for w in workouts if w.get('date', '') >= gen_start_str]

        # Same-day collision guard (§4.1): drop any non-benchmark session that shares a
        # date+sport with a scheduled benchmark, before the (date, sport)-keyed save can
        # let it overwrite the test.
        workouts = self._drop_benchmark_collisions(workouts)

        # Deterministic rest-window pre-pass (DESIGN_constraints.md §6): a `rest`
        # constraint forces its dates to rest regardless of what the LLM produced. Every
        # other constraint is advisory and left to the model. Applied after generation so
        # the guarantee holds even if the model ignores the constraint block it was shown.
        workouts = self._enforce_rest_windows_generate(workouts, constraints)

        # Boundary-week benchmark post-check (§4.1): warn (don't auto-insert) if a covered
        # block boundary lacks a fitness test. Runs after the rest pass so a rest-covered
        # boundary week is already silenced.
        self._warn_missing_boundary_benchmarks(
            workouts, constraints, macrocycle['id'], gen_start_str
        )

        # Archive (don't delete) future workouts from the previous plan so they can be
        # resurrected by `plan rollback` / `workout rollback` (see DESIGN_plan_rollback.md).
        self._archive_and_teardown(gen_start_str)

        saved_workouts: List[Workout] = []
        for w in workouts:
            wid = self._db.save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                duration_minutes=w.get('duration_minutes'),
                rpe=w.get('rpe'),
                tss=w.get('tss'),
                source='generated',
                benchmark_type=w.get('benchmark_type'),
                macrocycle_id=macrocycle['id']
            )
            # Sync from the persisted row, not a hand-built dict: the row's
            # calendar_signature is what freshness is later derived against, so any field
            # the dict omitted (e.g. source='generated') would make the push-time hash
            # disagree and read STALE forever. Re-fetching also lets the eager event carry
            # the same lifecycle footer a later re-push would (created_at, original load).
            saved_workouts.append(self._db.get_workout_by_id(wid))

        print(green(f"Generated {len(workouts)} workouts."))
        # Eager sync: push the new plan to Google Calendar straight away so the calendar
        # always mirrors the active plan (the old events were just torn down). Rollback is
        # the symmetric inverse (see DESIGN_plan_rollback.md).
        if saved_workouts:
            try:
                self._calendar_syncer.sync_multiple(saved_workouts)
                print(green("Synced new workouts to Google Calendar."))
            except Exception as e:
                print(red(f"Error syncing to Google Calendar: {e}"))
        return plan_data.get("reasoning", "Plan generated."), saved_workouts
