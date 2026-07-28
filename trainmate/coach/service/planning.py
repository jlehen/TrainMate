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
    cyan, green, yellow, bold, red, gray, PMC_TSB_LAG_NOTE,
)
from trainmate.coach.engine import CoachEngine, MIN_PLAN_WEEKS, MAX_PLAN_WEEKS
from trainmate.coach.formatting import format_baseline, _load_science_guidelines
import trainmate.coach.service as _svc


class PlanningMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

    def plan_rm(self, objective_id: int) -> None:
        """Deletes the periodization plan for a specific objective."""
        self._db.delete_macrocycle_for_objective(objective_id)

    def constraint_plan_impact(self, constraint: Dict[str, Any]) -> Dict[str, Any]:
        """Magnitude of a directive against the active plan (DESIGN_constraints.md §7,
        concrete formula): the planned load it displaces, expressed as a percentage of the
        plan's trailing weekly planned load (a self-scaling ratio — no absolute TSS number
        rots as the athlete's fitness changes), plus its span in days. The trailing week is
        the 7 days immediately before the constraint's start, so the reference point isn't
        itself affected by the constraint being evaluated. Feeds the human-confirmed replan
        proposal only — never an automatic regen."""
        start, end = constraint['start_date'], constraint['end_date']
        window_start = max(start, _svc._today_str())
        rest = canonical_sport('rest')

        def _load(w_start: str, w_end: str) -> float:
            sessions = [
                w for w in self._db.get_workouts(start_date=w_start, end_date=w_end)
                if canonical_sport(w.get('sport_type', '')) != rest
            ]
            return sum(planned_load(w) for w in sessions)

        displaced_load = _load(window_start, end)
        days = (datetime.strptime(end, "%Y-%m-%d").date()
                - datetime.strptime(start, "%Y-%m-%d").date()).days + 1

        trailing_end_date = datetime.strptime(start, "%Y-%m-%d").date() - timedelta(days=1)
        trailing_start_date = trailing_end_date - timedelta(days=6)
        trailing_weekly_load = _load(
            trailing_start_date.strftime("%Y-%m-%d"), trailing_end_date.strftime("%Y-%m-%d")
        )
        displaced_pct = (
            (displaced_load / trailing_weekly_load * 100) if trailing_weekly_load > 0 else 0.0
        )
        return {
            'days': days,
            'displaced_load': displaced_load,
            'trailing_weekly_load': trailing_weekly_load,
            'displaced_pct': displaced_pct,
        }

    def capture_message_constraint(
        self, candidate: Dict[str, Any], default_date_str: str
    ) -> Optional[int]:
        """Creates one durable constraint from a `new_constraints` candidate the CLI has
        already confirmed with the athlete (DESIGN_constraints.md §8 two-confirmation
        flow, step 1). Always `source='message'`, always advisory (`rest=0`) — the LLM can
        never mark an extracted constraint as a deterministic rest window (trust boundary
        §8); a genuine rest escalation is a deliberate human action (`constraint edit <id>
        --rest`). Never sets `replan=1` either — a large capture only *surfaces a
        suggestion* to escalate, which the human acts on separately."""
        title = (candidate.get('title') or '').strip()
        if not title:
            return None
        start = candidate.get('start_date') or default_date_str
        end = candidate.get('end_date') or start
        if end < start:
            end = start
        cid = self._db.add_constraint(
            title=title, start_date=start, end_date=end, rest=0,
            description=candidate.get('description'), replan=0, source='message',
        )
        constraint = self._db.get_constraint(cid)
        impact = self.constraint_plan_impact(constraint)
        if self.constraint_is_plan_shaping(constraint, impact):
            print(yellow(
                f"  This looks plan-shaping ({impact['days']} days, displaces "
                f"~{impact['displaced_pct']:.0f}% of a typical week). To build it into "
                f"the plan, run 'constraint edit {cid} --replan' or 'plan generate'."
            ))
        return cid

    @staticmethod
    def constraint_is_plan_shaping(
        constraint: Dict[str, Any], impact: Dict[str, Any]
    ) -> bool:
        """Concrete §7 magnitude heuristic for whether to *propose* a replan. Two
        independent triggers, either firing proposes a replan; deliberately no
        per-session "importance" term (TrainMate has no per-workout priority field):

        1. Displaced-load trigger (relative): the constraint's overlapping planned load
           is >= config.replan_displaced_load_pct of the plan's trailing weekly planned
           load (default 50 — wipes out at least half a typical week).
        2. Rest-window floor: the constraint is a `rest` window spanning >= config.
           replan_rest_span_days days (default 3), regardless of load overlap (it may
           land in a light taper week yet still reshape everything after it).
        """
        if impact['displaced_pct'] >= config.replan_displaced_load_pct:
            return True
        if (
            constraint.get('rest')
            and impact['days'] >= config.replan_rest_span_days
        ):
            return True
        return False

    def plan_generate(
        self, force: bool = False, objective_id: Optional[int] = None, auto_apply: bool = True
    ) -> Tuple[str, List[Dict[str, Any]], bool]:
        """Determines the macrocycle strategy and mesocycle blocks."""
        # Identify the target goal
        if objective_id is not None:
            next_goal = self._db.get_active_objective(objective_id)
            if not next_goal:
                next_goal = self._db.get_objective(objective_id)
                if not next_goal:
                    raise ValueError(f"Goal with ID {objective_id} not found.")
        else:
            next_goal = self._db.get_active_objective()
            if not next_goal:
                return "No active goals found. TrainMate needs at least one objective.", []

        # Compute plan-window dates (constraints are fetched below with the hashes).
        today_str = _svc._today_str()
        today_date = datetime.strptime(today_str, "%Y-%m-%d").date()

        # Determine plan start date based on preceding goals with plans
        plan_start_date = today_date
        preceding_objs = self._db.get_preceding_objectives(next_goal['target_date'])

        latest_preceding_target = None
        prev_macro = None

        for po in preceding_objs:
            if po['id'] is not None:
                po_macro = self._db.get_macrocycle_for_objective(po['id'])
                if po_macro:
                    po_target = datetime.strptime(po['target_date'], "%Y-%m-%d").date()
                    latest_preceding_target = po_target
                    prev_macro = po_macro
                    break

        if latest_preceding_target is not None:
            plan_start_date = latest_preceding_target + timedelta(days=1)
            if plan_start_date < today_date:
                plan_start_date = today_date

        # Compute duration
        target_date = datetime.strptime(next_goal['target_date'], "%Y-%m-%d").date()
        duration_days = (target_date - plan_start_date).days
        duration_weeks = duration_days / 7.0

        if duration_weeks < MIN_PLAN_WEEKS:
            raise ValueError(
                f"Goal '{next_goal['title']}' is too close "
                f"({duration_weeks:.1f} weeks away from the start date "
                f"{plan_start_date.strftime('%Y-%m-%d')}). "
                f"TrainMate requires at least {MIN_PLAN_WEEKS} weeks to generate a periodization plan."
            )

        if duration_weeks > MAX_PLAN_WEEKS:
            print(cyan(f"Goal '{next_goal['title']}' is {duration_weeks:.1f} "
                  f"weeks away (> {MAX_PLAN_WEEKS} weeks)."))
            print("Querying LLM to generate intermediate objectives...")
            goals_data = self.engine._generate_intermediate_goals(
                next_goal=next_goal,
                today_str=plan_start_date.strftime("%Y-%m-%d"),
                duration_weeks=duration_weeks
            )
            proposed_goals = goals_data.get("goals", [])
            if not proposed_goals:
                raise ValueError(
                    "LLM did not return any intermediate goals to split "
                    "the timeline."
                )

            for pg in proposed_goals:
                self._db.add_objective(
                    title=pg['title'],
                    target_date=pg['target_date'],
                    sport_type=pg['sport_type'],
                    description=pg.get('description', ''),
                    priority=pg.get('priority', next_goal['priority']),
                    status='active'
                )

            # Re-fetch active objective and update next_goal
            next_goal = self._db.get_active_objective()
            if not next_goal:
                raise ValueError("No active objectives found after splitting.")
            target_date = datetime.strptime(next_goal['target_date'], "%Y-%m-%d").date()
            duration_days = (target_date - plan_start_date).days
            duration_weeks = duration_days / 7.0

        # Compute current hashes
        # We need to fetch active objectives for hash computation so the hash covers the whole landscape
        objectives = self._db.get_objectives(status='active')
        # All active constraints feed the plan prompt; only the plan-shaping (replan=1)
        # ones fingerprint the plan and are snapshotted, so a tactical "no run Thursday"
        # never trips the reuse-vs-regen decision (DESIGN_constraints.md §7).
        constraints = self._db.get_constraints(today_str)
        replan_constraints = [c for c in constraints if c.get('replan')]
        goals_hash = self.engine._get_goals_hash(objectives)
        constraints_hash = self.engine._get_constraints_hash(replan_constraints)
        config_hash = self.engine._get_config_hash()
        config_snapshot = self._get_config_snapshot()
        goals_snapshot = json.dumps(self.engine._clean_goals(objectives))
        constraints_snapshot = json.dumps(self.engine._clean_constraints(replan_constraints))

        # Try to retrieve existing macrocycle
        strategy = ""
        mesocycles: List[Dict[str, Any]] = []
        existing_macro = None
        if next_goal['id'] is not None:
            existing_macro = self._db.get_macrocycle_for_objective(next_goal['id'])

        reused = False
        if existing_macro and not force:
            if (
                existing_macro['goals_hash'] == goals_hash
                and existing_macro['constraints_hash'] == constraints_hash
                and self.config_changed(existing_macro) is None
            ):
                reused = True
                strategy = existing_macro['strategy']
                mesocycles = self._db.get_mesocycles_for_macrocycle(existing_macro['id'])
                print(cyan("Reusing existing periodization strategy (macrocycle and mesocycles) "
                      "from database."))

        if not reused:
            # Get the previous strategy for context
            if existing_macro:
                prev_macro = existing_macro

            prev_strategy_text = None
            if prev_macro:
                prev_mesos = self._db.get_mesocycles_for_macrocycle(prev_macro['id'])
                prev_meso_text = ""
                for m in prev_mesos:
                    prev_meso_text += (
                        f"  - {m['name']} ({m['start_date']} to {m['end_date']}): "
                        f"{m['focus']}\n"
                    )
                prev_strategy_text = (
                    "PREVIOUS PERIODIZATION STRATEGY (FOR CONTEXT):\n"
                    f"- Overall Strategy: {prev_macro['strategy']}\n"
                    f"- Mesocycles:\n{prev_meso_text or '  - None\n'}"
                )

            # Retrieve active feedback from existing plan
            feedback_text = None
            if existing_macro:
                fb_parts = []
                if existing_macro.get('feedback'):
                    fb_parts.append(
                        f"- Overall Strategy Feedback: \"{existing_macro['feedback']}\""
                    )
                existing_mesos = self._db.get_mesocycles_for_macrocycle(existing_macro['id'])
                for m in existing_mesos:
                    if m.get('feedback'):
                        fb_parts.append(f"- Phase \"{m['name']}\" Feedback: \"{m['feedback']}\"")
                if fb_parts:
                    feedback_text = "\n".join(fb_parts)

            # Generate new macrocycle strategy and mesocycles
            print(cyan("Goals or plan-shaping constraints have changed, or force generation "
                  "requested. Determining new overall periodization strategy..."))
            guidelines = self._load_science_guidelines()
            profile = self._effective_profile()
            history_summary = self._get_recent_history_summary(today_str)
            # Planned-vs-actual review of the prior plan (+ cached reconstruction) fed as
            # read-only context (Option A). `prev_macro` here is the existing plan being
            # replaced, or the preceding goal's plan when there is none.
            prior_training_text = self._build_prior_training_context(prev_macro, today_str)
            if prior_training_text:
                print(cyan(bold("\n=== PRIOR TRAINING REVIEW (planned vs actual) ===")))
                print(prior_training_text)
                print(cyan(bold("==================================================\n")))
            macro_data = self.engine._plan_generate_strategy(
                next_goal=next_goal,
                objectives=objectives,
                constraints=constraints,
                today_str=today_str,
                guidelines=guidelines,
                profile=profile,
                previous_strategy_text=prev_strategy_text,
                plan_start_str=plan_start_date.strftime("%Y-%m-%d"),
                athlete_feedback=feedback_text,
                history_summary=history_summary,
                prior_training_text=prior_training_text
            )
            strategy = macro_data.get("strategy", "Endurance preparation strategy.")
            mesocycles = macro_data.get("mesocycles", [])

            # Save it
            if next_goal['id'] is not None and auto_apply:
                self._db.save_macrocycle(
                    objective_id=next_goal['id'],
                    strategy=strategy,
                    goals_hash=goals_hash,
                    constraints_hash=constraints_hash,
                    config_hash=config_hash,
                    config_snapshot=config_snapshot,
                    goals_snapshot=goals_snapshot,
                    constraints_snapshot=constraints_snapshot,
                    mesocycles=mesocycles
                )

            print(cyan(bold("\n=== NEW PERIODIZATION STRATEGY (MACROCYCLE) ===")))
            print(f"{bold('Overall Strategy:')}\n{strategy}\n")
            print(bold("Mesocycle Blocks:"))
            for m in mesocycles:
                print(f"- {bold(m['name'])} ({m['start_date']} to {m['end_date']}): {m['focus']}")
            print(cyan(bold("==============================================\n")))

        self._maybe_nudge_bootstrap()
        return strategy, mesocycles, reused

    def plan_apply(
        self, objective_id: int, strategy: str, mesocycles: List[Dict[str, Any]]
    ) -> None:
        """Saves a generated periodization plan to the database."""
        today_str = _svc._today_str()
        objectives = self._db.get_objectives(status='active')
        replan_constraints = [
            c for c in self._db.get_constraints(today_str) if c.get('replan')
        ]
        goals_hash = self.engine._get_goals_hash(objectives)
        constraints_hash = self.engine._get_constraints_hash(replan_constraints)
        config_hash = self.engine._get_config_hash()

        self._db.save_macrocycle(
            objective_id=objective_id,
            strategy=strategy,
            goals_hash=goals_hash,
            constraints_hash=constraints_hash,
            config_hash=config_hash,
            config_snapshot=self._get_config_snapshot(),
            goals_snapshot=json.dumps(self.engine._clean_goals(objectives)),
            constraints_snapshot=json.dumps(self.engine._clean_constraints(replan_constraints)),
            mesocycles=mesocycles
        )

    def plan_rollback(
        self, objective_id: Optional[int] = None, target_macrocycle_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Restores an earlier periodization plan version and its workouts.

        Swaps the active macrocycle for `objective_id` (defaults to the next active goal)
        back to a superseded version — the chronologically previous one by default, or
        `target_macrocycle_id` when given — then reconciles workouts and Calendar
        symmetrically with eager generation: the current plan's future workouts are
        archived (their events torn down) and the restored version's archived workouts are
        resurrected and re-pushed (see DESIGN_plan_rollback.md).

        Returns a summary dict: {from, to, restored_workouts, archived_workouts}.
        Raises ValueError when there is nothing to roll back to.
        """
        if objective_id is not None:
            objective = self._db.get_objective(objective_id)
        else:
            objective = self._db.get_active_objective()
        if not objective:
            raise ValueError("No goal found to roll back.")

        current = self._db.get_macrocycle_for_objective(objective['id'])
        if not current:
            raise ValueError(
                f"Goal '{objective['title']}' has no active plan to roll back."
            )

        if target_macrocycle_id is not None:
            target = self._db.get_macrocycle(target_macrocycle_id)
            if not target or target.get('objective_id') != objective['id']:
                raise ValueError(
                    f"Plan version {target_macrocycle_id} does not belong to goal "
                    f"'{objective['title']}'."
                )
            if target_macrocycle_id == current['id']:
                raise ValueError("That plan version is already active.")
        else:
            target = self._db.get_previous_macrocycle(objective['id'])
            if not target:
                raise ValueError(
                    f"Goal '{objective['title']}' has no earlier plan version to roll "
                    "back to."
                )

        today_str = _svc._today_str()

        # 1. Archive the current plan's future workouts and tear down their events.
        archived = self._db.archive_future_workouts(today_str)
        for ew in archived:
            if ew.get('google_event_id'):
                try:
                    self._calendar_syncer.delete_workout_event(ew['google_event_id'])
                except Exception as e:
                    print(red(f"Error deleting Google Calendar event: {e}"))

        # 2. Flip the active version so date->plan lookups resolve to the restored plan.
        self._db.set_active_macrocycle(target['id'])

        # 3. Resurrect the restored version's workouts and re-push them.
        restored = self._db.restore_macrocycle_workouts(target['id'])
        if restored:
            try:
                self._calendar_syncer.sync_multiple(restored)
            except Exception as e:
                print(red(f"Error syncing to Google Calendar: {e}"))

        return {
            'objective': objective,
            'from': current,
            'to': target,
            'restored_workouts': len(restored),
            'archived_workouts': len(archived),
        }

    def replan(
        self, force: bool = False, objective_id: Optional[int] = None
    ) -> Tuple[str, List[Workout]]:
        """Generates or adapts the training plan from today onwards."""
        objectives = self._db.get_objectives(status='active')
        if not objectives:
            return (
                "No active goals found. TrainMate needs at least one objective to "
                "start planning.",
                []
            )

        self.plan_generate(force=force, objective_id=objective_id)
        return self.workout_generate(objective_id=objective_id)
