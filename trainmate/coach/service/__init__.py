from typing import Optional
from trainmate import runtime
from trainmate.config import config
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
        engine: Optional[CoachEngine] = None, prompt_instance=None
    ):
        self._db_instance = db_instance
        self._calendar_syncer_instance = calendar_syncer_instance
        self._prompt_instance = prompt_instance
        self.engine = engine or CoachEngine()

    @property
    def _db(self):
        return self._db_instance or runtime.db

    @property
    def _calendar_syncer(self):
        return self._calendar_syncer_instance or runtime.calendar_syncer

    @property
    def _prompt(self):
        """The question channel, injected so the service never imports the CLI.

        It used to reach up with `import trainmate_cli as cli` mid-method, which meant
        any non-terminal frontend calling these methods got a terminal conversation.
        Whoever constructs the service now supplies the transport.
        """
        return self._prompt_instance or runtime.prompt


coach_service = CoachService()
