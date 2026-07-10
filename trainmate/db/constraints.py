from datetime import datetime, timezone
from typing import Any, List, Optional
from trainmate.types import Constraint


class ConstraintsMixin:
    """Constraints CRUD — the single directive object (DESIGN_constraints.md §5).

    A constraint is anything the athlete asks the coach to work around, at any
    horizon. Deterministic code reads `start_date`/`end_date` (window queries),
    `rest` (the one enforced edge — a full no-training window, §6) and `replan`
    (plan snapshot/hash); `title`/`description` are advisory prose for display and
    the LLM.
    """

    def add_constraint(
        self, title: str, start_date: str, end_date: str,
        rest: int = 0, description: Optional[str] = None,
        replan: int = 0, source: str = "manual"
    ) -> int:
        """Adds a new constraint and returns its ID."""
        created = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO constraints
                    (start_date, end_date, rest, title,
                     description, replan, source, created)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (start_date, end_date, int(rest), title,
                  description, int(replan), source, created))
            conn.commit()
            return int(cursor.lastrowid)

    def get_constraints(
        self, start: Optional[str] = None, end: Optional[str] = None
    ) -> List[Constraint]:
        """Fetches constraints overlapping a window, oldest first.

        `start=None` means no lower bound; `end=None` means no upper bound (the `plan
        generate` form: every constraint still active on or after `start`). With both
        set, returns the constraints overlapping the inclusive `[start, end]` range.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start is None and end is None:
                cursor.execute("SELECT * FROM constraints ORDER BY start_date ASC")
            elif start is None:
                cursor.execute(
                    "SELECT * FROM constraints WHERE start_date <= ? ORDER BY start_date ASC",
                    (end,)
                )
            elif end is None:
                cursor.execute(
                    "SELECT * FROM constraints WHERE end_date >= ? ORDER BY start_date ASC",
                    (start,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM constraints WHERE start_date <= ? AND end_date >= ? "
                    "ORDER BY start_date ASC",
                    (end, start)
                )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_constraint(self, constraint_id: int) -> Optional[Constraint]:
        """Fetches a constraint by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM constraints WHERE id = ?", (constraint_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def update_constraint(self, constraint_id: int, **kwargs: Any) -> None:
        """Updates constraint properties in the database."""
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [constraint_id]
        with self._get_connection() as conn:
            conn.cursor().execute(
                f"UPDATE constraints SET {fields} WHERE id = ?", values
            )
            conn.commit()

    def delete_constraint(self, constraint_id: int) -> None:
        """Deletes a constraint by ID."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "DELETE FROM constraints WHERE id = ?", (constraint_id,)
            )
            conn.commit()
