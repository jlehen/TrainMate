class WipesMixin:
    """Bulk deletion helpers used by the `wipe` CLI commands."""

    def wipe_objectives(self) -> None:
        """Deletes all objectives from the database."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM objectives")
            conn.commit()

    def wipe_lifeevents(self) -> None:
        """Deletes all life events from the database."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM lifeevents")
            conn.commit()

    def wipe_plans(self) -> None:
        """Deletes all macrocycles and mesocycles from the database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM mesocycles")
            cursor.execute("DELETE FROM macrocycles")
            conn.commit()

    def wipe_workouts(self) -> None:
        """Deletes all workouts from the database."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM workouts")
            conn.commit()

    def wipe_metrics(self) -> None:
        """Deletes all metrics, baselines, and completed activities from the database.

        Also clears the analysis cache: its reconstructions are derived from exactly this
        evidence, so they are meaningless once the evidence is gone.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM completed_activities")
            cursor.execute("DELETE FROM athlete_metrics_cache")
            cursor.execute("DELETE FROM athlete_baselines")
            cursor.execute("DELETE FROM analysis_cache")
            cursor.execute("DELETE FROM sync_state")
            conn.commit()
