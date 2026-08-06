"""SQLite persistence layer.

The single ``Database`` class is composed from domain-focused mixins (one module each)
to keep the file sizes navigable; behavior is identical to the former monolithic module.
Public names (``Database``, the ``db`` singleton, and the coach-learning helpers/constants)
are re-exported here so ``from trainmate.db import ...`` and ``trainmate.db.<name>`` keep
working unchanged.
"""

from trainmate.db.base import BaseDB
from trainmate.db.objectives import ObjectivesMixin
from trainmate.db.constraints import ConstraintsMixin
from trainmate.db.dailycontext import DailyContextMixin
from trainmate.db.benchmarks import BenchmarksMixin
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
    RETIRE_PROPOSAL,
)
from trainmate.db.analysis import AnalysisCacheMixin
from trainmate.db.periodization import PeriodizationMixin
from trainmate.db.settings import SettingsMixin
from trainmate.db.wipes import WipesMixin


class Database(
    ObjectivesMixin,
    ConstraintsMixin,
    DailyContextMixin,
    BenchmarksMixin,
    WorkoutsMixin,
    ActivitiesMixin,
    LearningsMixin,
    AnalysisCacheMixin,
    PeriodizationMixin,
    SettingsMixin,
    WipesMixin,
    BaseDB,
):
    """Handles all database schema setups and operations using SQLite."""


def __getattr__(name):
    """Builds the module-level ``db`` singleton on first use, not on import.

    Constructing a Database runs the schema migrations and writes to the file, so
    binding it at import meant that merely importing this package — for ``--help``, for
    a unit test of a pure function — opened and mutated the athlete's real database.
    Prefer ``trainmate.runtime.db``, which owns the process-wide handle; this accessor
    keeps ``from trainmate.db import db`` working for callers that still want one.
    """
    if name != "db":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from trainmate import runtime
    globals()["db"] = runtime.db
    return globals()["db"]

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
    "RETIRE_PROPOSAL",
]
