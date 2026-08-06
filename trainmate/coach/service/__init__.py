from typing import Optional
from trainmate.config import config
from trainmate.db import db
from trainmate.google_calendar import calendar_syncer
from trainmate.util import today_str as _today_str, today_date as _today_date
from trainmate.coach.engine import CoachEngine

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
