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
from trainmate.coach.engine import CoachEngine
from trainmate.coach.formatting import format_baseline, _load_science_guidelines

# Fallback look-back for `reflect` when no watermark exists yet (bootstrap not run).
DEFAULT_REFLECT_WEEKS = 4


from trainmate.coach.service.context import PmcContextMixin
from trainmate.coach.service.prompt import PromptConfigMixin
from trainmate.coach.service.planning import PlanningMixin
from trainmate.coach.service.workouts import WorkoutGenMixin
from trainmate.coach.service.adaptation import AdaptationMixin
from trainmate.coach.service.editing import WorkoutEditMixin
from trainmate.coach.service.analysis import DataAnalysisMixin


class CoachService(PmcContextMixin, PromptConfigMixin, PlanningMixin, WorkoutGenMixin, AdaptationMixin, WorkoutEditMixin, DataAnalysisMixin):
    """Orchestrates sports science coaching by coordinating data I/O and business logic."""

    def __init__(
        self, db_instance=None, calendar_syncer_instance=None,
        engine: Optional[CoachEngine] = None
    ):
        self._db_instance = db_instance
        self._calendar_syncer_instance = calendar_syncer_instance
        self.engine = engine or CoachEngine()

    @property
    def _db(self):
        return self._db_instance or db

    @property
    def _calendar_syncer(self):
        return self._calendar_syncer_instance or calendar_syncer


coach_service = CoachService()
