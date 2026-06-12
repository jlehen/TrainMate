"""Sports-science coaching layer.

Split into three submodules, all re-exported here so ``from trainmate.coach import ...``
keeps working:
  - ``formatting`` — pure prompt-formatting helpers (no I/O, no LLM).
  - ``engine``     — ``CoachEngine``: prompt construction, hashing, and LLM calls.
                     Owns the ``openrouter_client`` binding (patch target for tests).
  - ``service``    — ``CoachService`` + the ``coach_service`` singleton: data I/O,
                     caching, and orchestration. Owns the ``db``/``calendar_syncer``
                     bindings (patch targets for tests).

NOTE for tests/monkeypatching: because the logic now lives in submodules, patch the name
where it is *used* — ``trainmate.coach.engine.openrouter_client``,
``trainmate.coach.service.db``, ``trainmate.coach.service.calendar_syncer``,
``trainmate.coach.service.config``. The ``config`` singleton re-exported here is the same
object every submodule imports, so in-place mutations of ``trainmate.coach.config.data``
remain visible everywhere.
"""

from trainmate.config import config
from trainmate.coach.formatting import (
    format_metrics_history,
    format_completed_activities,
    format_planned_workouts,
    format_removed_workouts,
    format_baseline,
    _load_science_guidelines,
)
from trainmate.coach.engine import CoachEngine, LEARNING_UPDATES_FIELD
from trainmate.coach.service import CoachService, coach_service

__all__ = [
    "config",
    "CoachEngine",
    "CoachService",
    "coach_service",
    "LEARNING_UPDATES_FIELD",
    "format_metrics_history",
    "format_completed_activities",
    "format_planned_workouts",
    "format_removed_workouts",
    "format_baseline",
    "_load_science_guidelines",
]
