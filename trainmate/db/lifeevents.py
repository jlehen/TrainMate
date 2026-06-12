from typing import Any, List, Optional
from trainmate.types import LifeEvent


class LifeEventsMixin:
    """LifeEvents CRUD."""

    def add_lifeevent(
        self, title: str, start_date: str, end_date: str, event_type: str,
        impact_description: str = ""
    ) -> int:
        """Adds a new life event and returns its ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO lifeevents (title, start_date, end_date, event_type, impact_description)
                VALUES (?, ?, ?, ?, ?)
            """, (title, start_date, end_date, event_type, impact_description))
            conn.commit()
            return int(cursor.lastrowid)

    def get_lifeevents(self, start_after: Optional[str] = None) -> List[LifeEvent]:
        """Fetches all life events, optionally active on or after a date."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_after:
                cursor.execute(
                    "SELECT * FROM lifeevents WHERE end_date >= ? ORDER BY start_date ASC",
                    (start_after,)
                )
            else:
                cursor.execute("SELECT * FROM lifeevents ORDER BY start_date ASC")
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_lifeevent(self, lifeevent_id: int) -> Optional[LifeEvent]:
        """Fetches a life event by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM lifeevents WHERE id = ?", (lifeevent_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def update_lifeevent(self, lifeevent_id: int, **kwargs: Any) -> None:
        """Updates life event properties in the database."""
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [lifeevent_id]
        with self._get_connection() as conn:
            conn.cursor().execute(f"UPDATE lifeevents SET {fields} WHERE id = ?", values)
            conn.commit()

    def delete_lifeevent(self, lifeevent_id: int) -> None:
        """Deletes a life event by ID."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM lifeevents WHERE id = ?", (lifeevent_id,))
            conn.commit()
