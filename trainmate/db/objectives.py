from typing import Any, List, Optional
from trainmate.config import config
from trainmate.util import today_date
from trainmate.types import Objective


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

    def get_active_objective(self, objective_id: Optional[int] = None) -> Optional[Objective]:
        """Fetches the target active goal, or the next upcoming one if no ID is provided."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if objective_id is not None:
                cursor.execute(
                    "SELECT * FROM objectives WHERE id = ? AND status = 'active'",
                    (objective_id,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM objectives WHERE status = 'active' "
                    "ORDER BY target_date ASC LIMIT 1"
                )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_preceding_objectives(
        self, target_date: str, history_days: Optional[int] = None
    ) -> List[Objective]:
        """Fetches active objectives strictly before target_date, up to history_days ago."""
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
                "WHERE status = 'active' AND target_date < ? AND target_date >= ? "
                "ORDER BY target_date DESC",
                (target_date, lower_bound_str)
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
