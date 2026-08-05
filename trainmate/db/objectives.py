from typing import Any, List, Optional
from trainmate.config import config
from trainmate.util import today_date
from trainmate.types import Objective


ARCHIVED = 'archived'

# 'completed' is DERIVED, never stored: a goal the athlete has not archived and whose
# target date has passed is completed, by definition of the date. The column only records
# whether the goal was called off (DESIGN_backward_evaluation.md §12).
GOAL_UPCOMING, GOAL_COMPLETED, GOAL_ARCHIVED = 'upcoming', 'completed', 'archived'


def goal_state(objective: Objective, as_of: Optional[str] = None) -> str:
    """`'upcoming' | 'completed' | 'archived'` for one goal — the single derivation every
    surface asks, so no two of them can disagree about what a goal's state is."""
    if objective.get('status') == ARCHIVED:
        return GOAL_ARCHIVED
    today = as_of or today_date().strftime("%Y-%m-%d")
    return GOAL_COMPLETED if str(objective['target_date']) < today else GOAL_UPCOMING


class ObjectivesMixin:
    """Objectives CRUD."""

    def add_objective(
        self, title: str, target_date: str, sport_type: str,
        description: str = "", priority: int = 1, status: str = 'active'
    ) -> int:
        """Adds a new objective to the database and returns its ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO objectives (
                    title, target_date, sport_type, description, priority, status
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (title, target_date, sport_type, description, priority, status))
            conn.commit()
            return int(cursor.lastrowid)

    def get_objectives(self, status: Optional[str] = None) -> List[Objective]:
        """Fetches objectives from the database, filtered by status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    "SELECT * FROM objectives WHERE status = ? ORDER BY target_date ASC", (status,)
                )
            else:
                cursor.execute("SELECT * FROM objectives ORDER BY target_date ASC")
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def upcoming_objectives(self) -> List[Objective]:
        """Every goal still ahead of the athlete: not archived, target date not yet passed
        (§12). This is what "the goals that matter" means at every planning and picker
        site — a goal whose date is behind us is history, not a target."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM objectives WHERE status != ? AND target_date >= ? "
                "ORDER BY target_date ASC",
                (ARCHIVED, today_date().strftime("%Y-%m-%d")),
            )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_active_objective(self, objective_id: Optional[int] = None) -> Optional[Objective]:
        """The next goal still ahead, or the given one when an ID is passed.

        The no-ID form used to return the earliest goal merely marked `active`, with no
        date filter, so a goal whose date had passed stayed "the next goal" forever and
        `plan generate` refused with "there is no window to plan in" until the athlete
        marked it completed by hand (§12). The ID form resolves any goal that has not been
        archived, past or future — callers that mean "planning target" check the window
        themselves and give a better error than a missing row would.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if objective_id is not None:
                cursor.execute(
                    "SELECT * FROM objectives WHERE id = ? AND status != ?",
                    (objective_id, ARCHIVED)
                )
            else:
                cursor.execute(
                    "SELECT * FROM objectives WHERE status != ? AND target_date >= ? "
                    "ORDER BY target_date ASC LIMIT 1",
                    (ARCHIVED, today_date().strftime("%Y-%m-%d")),
                )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_preceding_objectives(
        self, target_date: str, history_days: Optional[int] = None
    ) -> List[Objective]:
        """Goals strictly before target_date, up to history_days ago — completed ones very
        much included, since they are the whole point of the lookup (§12).

        `history_days` (default `coach.goals_lookback_days`, 90) still bounds it: this
        feeds the plan-start computation, which wants the *recent* preceding goal.
        """
        if history_days is None:
            history_days = config.goals_lookback_days

        # Calculate the lower bound date
        from datetime import datetime, timedelta, timezone # imported locally to avoid modifying imports block
        target_date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
        today = today_date()
        reference_date = min(today, target_date_obj)
        lower_bound_obj = reference_date - timedelta(days=history_days)
        lower_bound_str = lower_bound_obj.strftime("%Y-%m-%d")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM objectives "
                "WHERE status != ? AND target_date < ? AND target_date >= ? "
                "ORDER BY target_date DESC",
                (ARCHIVED, target_date, lower_bound_str)
            )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_objective(self, obj_id: int) -> Optional[Objective]:
        """Fetches a specific objective by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM objectives WHERE id = ?", (obj_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def update_objective(self, obj_id: int, **kwargs: Any) -> None:
        """Updates objective properties in the database."""
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [obj_id]
        with self._get_connection() as conn:
            conn.cursor().execute(f"UPDATE objectives SET {fields} WHERE id = ?", values)
            conn.commit()

    def delete_objective(self, obj_id: int) -> None:
        """Deletes an objective by ID."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM objectives WHERE id = ?", (obj_id,))
            conn.commit()
