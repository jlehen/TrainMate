import json
import hashlib
from typing import Any, List, Optional, Dict
from trainmate.config import config
from trainmate.openrouter import openrouter_client
from trainmate.types import Objective, Constraint, Workout, CompletedActivity
from trainmate.util import today_date as _today_date, cyan
from trainmate.coach.formatting import (
    format_metrics_history, format_completed_activities, format_baseline,
    format_planned_workouts, format_planned_workouts_detailed,
    format_removed_workouts, format_daily_context,
)

# Macrocycle duration bounds (weeks) enforced when turning a goal into a periodization
# plan: shorter than MIN can't be periodized; longer than MAX is split into sequential
# intermediate goals. These mirror the science guidelines (science/periodization.txt) and
# are shared by the control flow (service.py) and the split-prompt text below.
MIN_PLAN_WEEKS = 5
MAX_PLAN_WEEKS = 24


# Shared JSON-output instruction for incrementally updating coach learnings. The LLM emits
# only deltas; the app owns the merge so unchanged observations are never lost. The model
# does NOT set confidence — it attributes each observation to the training WEEK(S) that back
# (or contradict) it, and the app computes confidence from the accumulated distinct weeks
# (DESIGN_evidence_based_confidence.md §6).
LEARNING_UPDATES_FIELD = (
    '  "learning_updates": [\n'
    "    // Optional. Updates to athlete observations. Attribute each observation to the\n"
    "    //   specific week_commencing (Monday, YYYY-MM-DD) value(s) shown in the weekly\n"
    "    //   summaries that justify it. Each item is one of:\n"
    '    //   {"op": "add", "text": "New observation.", "sports": "running", "evidence": ["2026-05-04", "2026-05-11"]},\n'
    '    //   {"op": "revise", "id": 3, "text": "Reworded observation #3.", "evidence": ["2026-05-18"]},\n'
    '    //   {"op": "reinforce", "id": 4, "evidence": ["2026-05-25"]},\n'
    '    //   {"op": "contradict", "id": 5, "evidence": ["2026-06-01"]},\n'
    '    //   {"op": "retire", "id": 6}\n'
    '    // "sports": comma-separated sport(s) the observation applies to (e.g. "running,road_biking"),\n'
    '    //   or "general" if not sport-specific. Defaults to "general".\n'
    '    // "evidence": the week_commencing date(s) of training that SUPPORT the observation\n'
    "    //   (add/revise/reinforce) or CONTRADICT it (contradict). Do NOT set a confidence\n"
    "    //   level — the app derives it from how many distinct weeks back each observation.\n"
    '    // Use "reinforce" when an existing observation holds again in a new week; "contradict"\n'
    "    //   when a week shows the opposite (this can lower its confidence).\n"
    "    // Existing observations persist automatically; do NOT repeat unchanged ones.\n"
    "    // Reference existing observations by the [id] shown under COACH LEARNINGS.\n"
)


from trainmate.coach.engine.prompt import PromptBuildMixin
from trainmate.coach.engine.planning import PlanStrategyMixin
from trainmate.coach.engine.workouts import WorkoutLogicMixin
from trainmate.coach.engine.analysis import AnalysisLogicMixin


class CoachEngine(PromptBuildMixin, PlanStrategyMixin, WorkoutLogicMixin, AnalysisLogicMixin):
    """Pure business logic coach that builds prompts, computes hashes, and makes LLM calls."""
