from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from trainmate.types import Macrocycle, Mesocycle


class PeriodizationMixin:
    """Macrocycles & mesocycles: persistence, lookups, and feedback."""

    def get_macrocycle_for_objective(self, objective_id: int) -> Optional[Macrocycle]:
        """Fetches the latest macrocycle created for a specific objective."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM macrocycles WHERE objective_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (objective_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_last_macrocycle(self) -> Optional[Macrocycle]:
        """Fetches the absolute latest macrocycle created."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM macrocycles ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_mesocycles_for_macrocycle(self, macrocycle_id: int) -> List[Mesocycle]:
        """Fetches all mesocycles in chronological order belonging to a macrocycle."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM mesocycles WHERE macrocycle_id = ? ORDER BY start_date ASC",
                (macrocycle_id,)
            )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_mesocycle_ranges(self, start_date: str, end_date: str) -> List[Tuple[str, str]]:
        """Returns the (start_date, end_date) spans of all mesocycles overlapping the
        given window, across every objective regardless of status.

        Used to decide whether a date fell inside *any* planned block: an activity on a
        covered date with no matching workout is a genuine deviation, whereas one outside
        all coverage is just history the plan never governed (e.g. before tool adoption,
        or an unplanned off-season stretch). Status is intentionally not filtered — a
        since-completed objective still planned its dates."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT start_date, end_date FROM mesocycles
                WHERE start_date <= ? AND end_date >= ?
                ORDER BY start_date ASC
            """, (end_date, start_date))
            return [(row['start_date'], row['end_date']) for row in cursor.fetchall()]

    def get_periodization_ids_for_date(
        self, date: str
    ) -> Optional[Tuple[int, int, int]]:
        """Resolves the (objective_id, macrocycle_id, mesocycle_id) covering a date.

        Returns the mesocycle whose span contains the given date, along with its
        parent macrocycle and objective ids. When overlapping blocks exist across
        objectives, the most recently created macrocycle wins. Returns None if no
        mesocycle covers the date. Used to stamp calendar events with traceability
        back to the plan that produced them.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT mac.objective_id AS objective_id,
                       m.macrocycle_id AS macrocycle_id,
                       m.id AS mesocycle_id
                FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                WHERE m.start_date <= ? AND m.end_date >= ?
                ORDER BY mac.id DESC, m.start_date ASC
                LIMIT 1
            """, (date, date))
            row = cursor.fetchone()
            if not row:
                return None
            return (
                row['objective_id'],
                row['macrocycle_id'],
                row['mesocycle_id'],
            )

    def get_mesocycle(self, mesocycle_id: int) -> Optional[Mesocycle]:
        """Fetches a specific mesocycle by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM mesocycles WHERE id = ?", (mesocycle_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_active_mesocycle(self, target_date: str) -> Optional[Mesocycle]:
        """Finds the active mesocycle block for a given date.

        Falls back to the next future mesocycle, or the absolute first mesocycle
        if none contain or follow the target date. Only considers active objectives.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Primary: mesocycle containing target_date
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active'
                  AND m.start_date <= ? AND m.end_date >= ?
                ORDER BY m.start_date ASC LIMIT 1
            """, (target_date, target_date))
            row = cursor.fetchone()
            if row: return dict(row) # type: ignore

            # Fallback 1: first mesocycle that ends in the future
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active'
                  AND m.end_date >= ?
                ORDER BY m.start_date ASC LIMIT 1
            """, (target_date,))
            row = cursor.fetchone()
            if row: return dict(row) # type: ignore

            # Fallback 2: absolute first mesocycle
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active'
                ORDER BY m.start_date ASC LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None # type: ignore

    def update_macrocycle_feedback(self, macrocycle_id: int, feedback: str) -> None:
        """Saves user feedback for a specific macrocycle strategy."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "UPDATE macrocycles SET feedback = ? WHERE id = ?",
                (feedback, macrocycle_id)
            )
            conn.commit()

    def update_mesocycle_feedback(self, mesocycle_id: int, feedback: str) -> None:
        """Saves user feedback for a specific training phase/mesocycle block."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "UPDATE mesocycles SET feedback = ? WHERE id = ?",
                (feedback, mesocycle_id)
            )
            conn.commit()

    def save_macrocycle(
        self, objective_id: int, strategy: str, goals_hash: str,
        lifeevents_hash: str, mesocycles: List[Dict[str, Any]],
        config_hash: str = "", goals_snapshot: str = "",
        lifeevents_snapshot: str = ""
    ) -> int:
        """Saves a macrocycle and its nested mesocycles for the objective.

        goals_snapshot/lifeevents_snapshot are JSON of the goals and life events the plan
        was generated from (the same cleaned data the hashes fingerprint), preserved so
        the inputs can be shown later even after the live records change.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Delete any existing macrocycles for this objective (cascade deletes mesocycles)
            cursor.execute("DELETE FROM macrocycles WHERE objective_id = ?", (objective_id,))

            created_at = datetime.now(timezone.utc).isoformat()
            cursor.execute("""
                INSERT INTO macrocycles (
                    objective_id, strategy, goals_hash, lifeevents_hash, config_hash,
                    goals_snapshot, lifeevents_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (objective_id, strategy, goals_hash, lifeevents_hash, config_hash,
                  goals_snapshot or None, lifeevents_snapshot or None, created_at))
            macrocycle_id = cursor.lastrowid

            for meso in mesocycles:
                cursor.execute("""
                    INSERT INTO mesocycles (macrocycle_id, name, start_date, end_date, focus)
                    VALUES (?, ?, ?, ?, ?)
                """, (macrocycle_id, meso['name'], meso['start_date'], meso['end_date'],
                      meso['focus']))

            conn.commit()
            return int(macrocycle_id)

    def update_macrocycle_config_hash(self, macrocycle_id: int, config_hash: str) -> None:
        """Updates the config hash for a specific macrocycle."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE macrocycles SET config_hash = ? WHERE id = ?",
                (config_hash, macrocycle_id)
            )
            conn.commit()

    def delete_macrocycle_for_objective(self, objective_id: int) -> None:
        """Deletes the macrocycle and its nested mesocycles for a specific objective."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "DELETE FROM macrocycles WHERE objective_id = ?",
                (objective_id,)
            )
            conn.commit()
