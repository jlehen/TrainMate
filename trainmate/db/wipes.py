from typing import Optional


class WipesMixin:
    """Bulk deletion helpers used by the `wipe` CLI commands."""

    def wipe_objectives(self) -> None:
        """Deletes all objectives from the database."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM objectives")
            conn.commit()

    def wipe_constraints(self) -> None:
        """Deletes all constraints from the database."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM constraints")
            conn.commit()

    def wipe_benchmarks(self) -> None:
        """Deletes all benchmark-result logbook rows from the database."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM benchmark_results")
            conn.commit()

    def wipe_learnings(self) -> None:
        """Deletes all coach learnings (their evidence basis cascades)."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM coach_learnings")
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

    @staticmethod
    def _delete_by_date(cursor, table: str, start: Optional[str], end: Optional[str]) -> None:
        """Deletes rows from `table`, optionally bounded to the inclusive [start, end]
        date window. Every table this is used on keys its rows by a `date` column."""
        clauses, params = [], []
        if start is not None:
            clauses.append("date >= ?")
            params.append(start)
        if end is not None:
            clauses.append("date <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor.execute(f"DELETE FROM {table}{where}", params)

    def wipe_garmin_data(
        self, start: Optional[str] = None, end: Optional[str] = None
    ) -> None:
        """Deletes Garmin-sourced evidence: daily metrics, baselines, and completed
        activities — plus the analysis cache, whose reconstructions are derived from
        exactly this evidence and so are meaningless once any of it changes.

        With a [start, end] window only in-range rows go, and the sync watermarks are
        left intact: the next pull re-fetches the now-missing days, which it detects by
        row presence (`get_metric_dates`), not the watermark. With no window the whole
        dataset is cleared and every non-calendar sync watermark (garmin/reflect/
        bootstrap) is reset, so the next run is a true cold start.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            self._delete_by_date(cursor, "completed_activities", start, end)
            self._delete_by_date(cursor, "athlete_metrics_cache", start, end)
            self._delete_by_date(cursor, "athlete_baselines", start, end)
            cursor.execute("DELETE FROM analysis_cache")
            if start is None and end is None:
                # Full wipe: drop the Garmin watermark and the coach-analysis
                # watermarks that index this evidence. The calendar token is owned by
                # wipe_calendar_context and left alone here.
                cursor.execute("DELETE FROM sync_state WHERE key != 'calendar_context'")
            conn.commit()

    def wipe_calendar_context(
        self, start: Optional[str] = None, end: Optional[str] = None
    ) -> None:
        """Deletes ingested daily-context signals and resets the Calendar sync token so
        the next pull re-pulls context in full. The Calendar sync is incremental (it
        replays only events changed since the stored token), so deleted rows cannot
        otherwise return; resetting the token forces a full re-pull. A [start, end]
        window restricts which rows are deleted now, but that subsequent full re-pull
        still restores the whole history (DESIGN_calendar_context_ingest.md §6).
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            self._delete_by_date(cursor, "daily_context", start, end)
            cursor.execute("DELETE FROM sync_state WHERE key = 'calendar_context'")
            conn.commit()

    def wipe_metrics(self) -> None:
        """Full reset of all Garmin evidence and ingested daily context (and every sync
        watermark). Convenience wrapper over the scoped `wipe_garmin_data` /
        `wipe_calendar_context` for callers that want the whole lot gone."""
        self.wipe_garmin_data()
        self.wipe_calendar_context()
