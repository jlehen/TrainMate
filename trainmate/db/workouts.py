from typing import List, Optional
from trainmate.types import Workout
from trainmate.sports import sport_aliases


class WorkoutsMixin:
    """Workouts CRUD and soft-delete / reschedule operations."""

    def save_workout(
        self, date: str, sport_type: str, title: str, description: str,
        original_description: Optional[str] = None,
        modification_reason: Optional[str] = None, google_event_id: Optional[str] = None,
        duration_minutes: Optional[int] = None, rpe: Optional[int] = None,
        tss: Optional[int] = None, original_date: Optional[str] = None,
        removed: bool = False, removed_reason: Optional[str] = None,
        source: Optional[str] = None, adaptation_summary: Optional[str] = None
    ) -> int:
        """Saves a workout, updating it if one already exists that day for the same
        sport. Existence is alias-aware (see trainmate.sports), so adapting/regenerating
        a canonical ``strength_training`` updates an existing ``strength`` row in place
        instead of inserting a duplicate; the stored row keeps its original spelling.

        Calendar freshness is *not* touched here: `pushed_signature` is left as-is, so any
        content change made through this method automatically reads as `stale` (see
        trainmate.calendar_state). Only a successful push, via `mark_workout_pushed`,
        records a new signature."""
        aliases = sport_aliases(sport_type)
        placeholders = ",".join("?" * len(aliases))
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, google_event_id FROM workouts WHERE date = ? "
                f"AND LOWER(sport_type) IN ({placeholders})",
                (date, *aliases)
            )
            row = cursor.fetchone()
            if row:
                workout_id = row['id']
                ge_id = google_event_id if google_event_id is not None else row['google_event_id']
                cursor.execute("""
                    UPDATE workouts
                    SET title = ?, description = ?,
                        original_description = COALESCE(?, original_description),
                        original_date = COALESCE(?, original_date, date),
                        modification_reason = ?,
                        adaptation_summary = COALESCE(?, adaptation_summary),
                        google_event_id = ?,
                        duration_minutes = COALESCE(?, duration_minutes),
                        rpe = COALESCE(?, rpe),
                        tss = COALESCE(?, tss),
                        removed = ?, removed_reason = ?,
                        source = COALESCE(?, source)
                    WHERE id = ?
                """, (title, description, original_description,
                      original_date,
                      modification_reason, adaptation_summary, ge_id,
                      duration_minutes, rpe, tss,
                      int(removed), removed_reason, source,
                      workout_id))
            else:
                cursor.execute("""
                    INSERT INTO workouts (
                        date, sport_type, title, description, original_description,
                        original_date, modification_reason, adaptation_summary,
                        google_event_id, duration_minutes, rpe, tss,
                        removed, removed_reason, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (date, sport_type, title, description,
                      original_description or description,
                      original_date or date,
                      modification_reason, adaptation_summary,
                      google_event_id, duration_minutes,
                      rpe, tss, int(removed), removed_reason, source))
                workout_id = cursor.lastrowid
            conn.commit()
            return int(workout_id)

    def get_workout(self, date: str, sport_type: str) -> Optional[Workout]:
        """Fetches a workout by date and sport type.

        Matching is alias-aware (see trainmate.sports): a lookup for the canonical
        ``strength_training`` finds a row stored under an alias like ``strength`` (and
        vice-versa), case-insensitively, so the coach's canonical vocabulary lines up
        with manually-added or legacy spellings."""
        aliases = sport_aliases(sport_type)
        placeholders = ",".join("?" * len(aliases))
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM workouts WHERE date = ? "
                f"AND LOWER(sport_type) IN ({placeholders})",
                (date, *aliases)
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_workouts(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        sport_type: Optional[str] = None,
        include_removed: bool = False
    ) -> List[Workout]:
        """Fetches workouts ordered by date, optionally within a range or by sport type.

        Soft-removed workouts (`removed = 1`, set by `workout rm`) are excluded by
        default so they never appear in listings, comparisons, adaptation inputs, or
        the calendar push. Pass include_removed=True to retrieve them (e.g. to tell
        the coach a session was deliberately cancelled)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM workouts WHERE 1=1"
            params = []
            if not include_removed:
                query += " AND COALESCE(removed, 0) = 0"
            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND date <= ?"
                params.append(end_date)
            if sport_type:
                query += " AND LOWER(sport_type) = ?"
                params.append(sport_type.lower())
            query += " ORDER BY date ASC"
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def clear_future_workouts(
        self, from_date: str, include_calendar_events: bool = False
    ) -> None:
        """Deletes future workouts from the database.

        By default workouts that have a Google Calendar event (google_event_id set) are
        spared, so their events are not orphaned — this is the true "on calendar" signal,
        independent of whether the event is currently in sync. Pass
        include_calendar_events=True to remove them too; callers doing this are responsible
        for deleting the corresponding Calendar events first.
        """
        with self._get_connection() as conn:
            if include_calendar_events:
                conn.cursor().execute(
                    "DELETE FROM workouts WHERE date >= ?", (from_date,)
                )
            else:
                conn.cursor().execute(
                    "DELETE FROM workouts WHERE date >= ? AND google_event_id IS NULL",
                    (from_date,)
                )
            conn.commit()

    def get_workout_by_id(self, workout_id: int) -> Optional[Workout]:
        """Fetches a workout by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def delete_workout_by_id(self, workout_id: int) -> None:
        """Deletes a workout by ID."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
            conn.commit()

    def mark_workout_pushed(
        self, workout_id: int, google_event_id: str, signature: str
    ) -> None:
        """Records a successful Google Calendar push: stores the event handle and the
        signature of the content that was pushed. Freshness is derived from comparing
        this signature against the live content (see trainmate.calendar_state), so this
        is the *only* method that marks a workout `synced`."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET google_event_id = ?, pushed_signature = ? WHERE id = ?",
                (google_event_id, signature, workout_id)
            )
            conn.commit()

    def mark_workout_removed(
        self, workout_id: int, reason: Optional[str] = None
    ) -> None:
        """Soft-deletes a workout: flags it `removed`.

        The row is kept (excluded from reads by default) so the coach can still be told
        the session was deliberately cancelled, optionally with the athlete's `reason`.
        `google_event_id` is preserved so the calendar event can be updated to be marked
        as deleted on sync; changing `removed` shifts the content hash, so the workout
        reads as `stale` and the next push updates the event (see trainmate.calendar_state)."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET removed = 1, removed_reason = ? WHERE id = ?",
                (reason, workout_id)
            )
            conn.commit()

    def restore_workout(self, workout_id: int) -> None:
        """Restores a soft-deleted workout.

        Clears the `removed` and `removed_reason` flags; the resulting content change
        reads as `stale` so the next push un-deletes the Google Calendar event."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET removed = 0, removed_reason = NULL WHERE id = ?",
                (workout_id,)
            )
            conn.commit()

    def update_workout_date(
        self, workout_id: int, new_date: str, modification_reason: str
    ) -> None:
        """Moves a workout to a new date; the date change reads as `stale` so the next
        push updates the Calendar event (see trainmate.calendar_state).

        If the workout lands back on its original_date the modification_reason
        is cleared — the workout is no longer considered adapted/swapped.
        """
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET date = ?, "
                "modification_reason = CASE "
                "  WHEN COALESCE(original_date, date) = ? THEN NULL "
                "  ELSE ? "
                "END "
                "WHERE id = ?",
                (new_date, new_date, modification_reason, workout_id)
            )
            conn.commit()
