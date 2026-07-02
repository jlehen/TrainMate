"""SQLite persistence layer.

The single ``Database`` class is composed from domain-focused mixins (one module each)
to keep the file sizes navigable; behavior is identical to the former monolithic module.
Public names (``Database``, the ``db`` singleton, and the coach-learning helpers/constants)
are re-exported here so ``from trainmate.db import ...`` and ``trainmate.db.<name>`` keep
working unchanged.
"""

from trainmate.db.base import BaseDB
from trainmate.db.objectives import ObjectivesMixin
from trainmate.db.lifeevents import LifeEventsMixin
from trainmate.db.constraints import ConstraintsMixin
from trainmate.db.dailycontext import DailyContextMixin
from trainmate.db.workouts import WorkoutsMixin
from trainmate.db.activities import ActivitiesMixin
from trainmate.db.learnings import (
    LearningsMixin,
    normalize_sports,
    valid_confidence,
    confidence_rank,
    step_down,
    derive_confidence,
    learning_is_dormant,
    CONFIDENCE_LEVELS,
    LEARNING_STALENESS_DAYS,
    RETIRE_PROPOSAL,
)
from trainmate.db.analysis import AnalysisCacheMixin
from trainmate.db.periodization import PeriodizationMixin
from trainmate.db.wipes import WipesMixin


class Database(
    ObjectivesMixin,
    LifeEventsMixin,
    ConstraintsMixin,
    DailyContextMixin,
    WorkoutsMixin,
    ActivitiesMixin,
    LearningsMixin,
    AnalysisCacheMixin,
    PeriodizationMixin,
    WipesMixin,
    BaseDB,
):
    """Handles all database schema setups and operations using SQLite."""


# Singleton instance
db = Database()

__all__ = [
    "Database",
    "db",
    "normalize_sports",
    "valid_confidence",
    "confidence_rank",
    "step_down",
    "derive_confidence",
    "learning_is_dormant",
    "CONFIDENCE_LEVELS",
    "LEARNING_STALENESS_DAYS",
    "RETIRE_PROPOSAL",
]
