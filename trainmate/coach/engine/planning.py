import json
import hashlib
from typing import Any, List, Optional, Dict
from trainmate.config import config
from trainmate.openrouter import openrouter_client
from trainmate.types import Objective, Constraint, Workout, CompletedActivity
from trainmate.util import today_date as _today_date, cyan, wrap_text
from trainmate.coach.formatting import (
    format_metrics_history, format_completed_activities, format_baseline,
    format_planned_workouts, format_planned_workouts_detailed,
    format_removed_workouts, format_daily_context,
)
import trainmate.coach.engine as _eng
from trainmate.coach.engine import LEARNING_UPDATES_FIELD


class PlanStrategyMixin:
    """Part of :class:`CoachEngine` — see coach/engine/__init__.py."""

    def _plan_generate_strategy(
        self, next_goal: Objective, objectives: List[Objective],
        constraints: List[Constraint], today_str: str, guidelines: str,
        profile: Optional[Dict[str, Any]], previous_strategy_text: Optional[str] = None,
        plan_start_str: Optional[str] = None, athlete_feedback: Optional[str] = None,
        history_summary: Optional[str] = None, prior_training_text: Optional[str] = None,
        learnings: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queries LLM to determine the overall macrocycle strategy and mesocycle blocks.

        The plan always runs from the start date to the goal, however far out that is: how
        a long horizon gets structured is a question for the science guidelines, not for a
        duration threshold in the app.

        Builds its own system prompt rather than calling `_build_system_prompt`: that one
        states the ACTIVE strategy and blocks as settled fact, which is the very thing this
        call produces (DESIGN_backward_evaluation.md §10.1)."""
        plan_start = plan_start_str or today_str
        custom_task = f"""
TASK:
Determine the overall periodization strategy (macrocycle) from {plan_start} until the target
goal ({next_goal['target_date']}).

Divide this timeframe into contiguous, sequential mesocycles (determining the duration of each
block based on the periodization style guidelines provided in the science file). When planning
mesocycles, it is acceptable to shorten/extend a block by a few days to align its transition or
recovery boundaries with the athlete's active constraints (e.g. aligning a deload week or phase
change with a long travel block).
Make sure there are no gaps between the end date of one mesocycle and the start date of the next.
The first mesocycle must start on the start date ({plan_start}) and the last mesocycle must end
on or around the goal date ({next_goal['target_date']}).
"""

        if athlete_feedback:
            custom_task += f"""
ATHLETE FEEDBACK ON THE PREVIOUS PLAN:
The athlete has provided direct feedback on the previous periodization plan:
{athlete_feedback}
You MUST revise the macrocycle strategy and/or the duration, boundaries, and focuses of individual
mesocycles to directly address this feedback. Make adjustments (e.g. scheduling more rest,
changing block emphasis, extending/shortening specific cycles) while continuing to respect overall
sports science principles and guidelines.
"""

        if previous_strategy_text:
            custom_task += """
For context, the PREVIOUS periodization strategy that was in place before this
replanning is provided below. Please take it into account to ensure continuity
in the athlete's training, adapting or building on top of what has been planned
or done so far, rather than starting completely from scratch, unless a complete
reset is warranted by major changes.
"""

        custom_task += f"""
You MUST respond with a JSON object containing:
{{
  "strategy": "Explain the overall training strategy philosophy and periodization strategy
    until the goal date ({next_goal['target_date']}).",
  "mesocycles": [
    {{
      "name": "Phase Name (e.g., Base Building, Specific Preparation, Build,
        Peak & Taper, Race/Event)",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "focus": "Key focus and description of this block (e.g., volume progression,
        aerobic threshold, rest, peak load, etc.)"
    }}
  ]
}}
"""
        obj_text = ""
        for o in objectives:
            details = o.get('description', '')
            obj_text += (
                f"- Goal: {o['title']} | Date: {o['target_date']} | "
                f"Sport: {o['sport_type']} | Details: {details}\n"
            )

        c_text = self._render_constraints(constraints)

        athlete_profile = self._format_athlete_profile(profile)
        system_prompt = (
            "You are TrainMate Coach, an advanced AI sports science training coach.\n"
            "You design periodized training plans (macro, meso, micro cycles) leading up "
            "to target goals.\n\n"
            "================================================================================\n"
            "START OF SPORTS SCIENCE GUIDELINES\n"
            "================================================================================\n"
            f"{guidelines}\n"
            "================================================================================\n"
            "END OF SPORTS SCIENCE GUIDELINES\n"
            "================================================================================\n"
        )

        if previous_strategy_text:
            system_prompt += f"\n{previous_strategy_text}\n"

        system_prompt += (
            f"\nATHLETE PROFILE & PREFERENCES:\n{athlete_profile}\n"
        )
        if history_summary:
            system_prompt += (
                f"\nATHLETE RECENT TRAINING SUMMARY (PAST 15 DAYS):\n{history_summary}\n"
            )
        # Planned-vs-actual review of the prior plan + any inferred reconstruction, fed as
        # read-only context so the new plan is grounded in demonstrated reality rather than
        # an idealized template (DESIGN_backward_evaluation.md §6, Option A).
        if prior_training_text:
            system_prompt += f"\nPRIOR TRAINING REVIEW:\n{prior_training_text}\n"
        # The distilled half of what the analysis flow found; the reconstruction above is
        # its narrative half (DESIGN_backward_evaluation.md §10.1).
        if learnings:
            system_prompt += (
                "\nATHLETE-SPECIFIC OBSERVATIONS (accumulated by the training-history "
                "analysis, tagged\n[id|sports|confidence]):\n"
                f"{learnings}\n"
                "Weigh these when shaping the blocks — a higher confidence means more weeks "
                "of evidence\nbehind the observation. They are input only here: authoring "
                "and revising them belongs\nto the analysis flow.\n"
            )
        system_prompt += (
            f"\nACTIVE ATHLETE GOALS (CHRONOLOGICAL):\n"
            f"{obj_text if obj_text else 'No active goals.'}\n\n"
            f"ACTIVE CONSTRAINTS (athlete-declared directives to work around):\n"
            f"{c_text if c_text else 'No active constraints.'}\n\n"
            f"{custom_task}\n"
        )

        user_content = (
            f"Today's date is {today_str}. The target goal is "
            f"'{next_goal['title']}' on {next_goal['target_date']}. "
            "Please determine the macrocycle and mesocycle blocks starting from "
            f"{plan_start}."
        )

        print(cyan(wrap_text(
            "Querying OpenRouter to generate macrocycle and mesocycles "
            "periodization strategy..."
        )))
        result = _eng.openrouter_client.complete(
            system_prompt, user_content, label="plan_generate"
        )
        return result
