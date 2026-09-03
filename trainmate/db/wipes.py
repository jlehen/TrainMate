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
        """Deletes all macrocycles, mesocycles and plan feedback from the database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM plan_feedback")
            cursor.execute("DELETE FROM mesocycles")
            cursor.execute("DELETE FROM macrocycles")
            conn.commit()

    def wipe_workouts(self) -> None:
        """Resets the whole workouts log: revisions, changes and Calendar state.

        The one whole-table deletion the append-only rule exempts, because it is a reset
        rather than a write path to convert (DESIGN_workout_revisions.md §14). It drops
        the immutability triggers, clears the three tables together — a revision without
        its change, or a Calendar handle without its lineage, would be worse than either
        gone — and puts the triggers back.
        """
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DROP TRIGGER IF EXISTS workouts_no_update")
            cursor.execute("DROP TRIGGER IF EXISTS workouts_no_delete")
            cursor.execute("DELETE FROM workouts")
            cursor.execute("DELETE FROM workout_changes")
            cursor.execute("DELETE FROM workout_calendar_state")
            cursor.execute("""
                CREATE TRIGGER workouts_no_update BEFORE UPDATE ON workouts
                WHEN NOT (OLD.lineage_id IS NULL AND NEW.lineage_id = NEW.id)
                BEGIN SELECT RAISE(ABORT, 'workouts is append-only: append a revision'); END
            """)
            cursor.execute("""
                CREATE TRIGGER workouts_no_delete BEFORE DELETE ON workouts
                BEGIN SELECT RAISE(ABORT, 'workouts is append-only: append a void revision'); END
            """)

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
        # One unit of work: the cache purge joins this transaction rather than opening a
        # second writer, which makes the whole wipe atomic (DESIGN_backward_evaluation.md
        # §5.1).
        with self.transaction() as conn:
            self.wipe_analysis_cache()
            cursor = conn.cursor()
            self._delete_by_date(cursor, "completed_activities", start, end)
            self._delete_by_date(cursor, "athlete_metrics_cache", start, end)
            self._delete_by_date(cursor, "athlete_baselines", start, end)
            if start is None and end is None:
                # Full wipe: drop the Garmin watermark and the coach-analysis
                # watermarks that index this evidence. The calendar token is owned by
                # wipe_calendar_signals and left alone here.
                cursor.execute("DELETE FROM sync_state WHERE key != 'calendar_signals'")

        # Deleted load stays baked into later days' CTL/ATL until the EWMAs are walked
        # again, so the sweep belongs to the wipe rather than to whoever remembers to
        # call it. It lived in cli/data.py to dodge a garmin<->db import cycle; the
        # lazy runtime singletons dissolved that, so it can live here now, where every
        # caller gets it. Imported inside the method to keep module import order free.
        from trainmate.garmin.pmc import recompute_derived
        recompute_derived(dbh=self)

    def wipe_calendar_signals(
        self, start: Optional[str] = None, end: Optional[str] = None
    ) -> None:
        """Deletes ingested daily signals and resets the Calendar sync token so
        the next pull re-pulls signals in full. The Calendar sync is incremental (it
        replays only events changed since the stored token), so deleted rows cannot
        otherwise return; resetting the token forces a full re-pull. A [start, end]
        window restricts which rows are deleted now, but that subsequent full re-pull
        still restores the whole history (DESIGN_calendar_signal_ingest.md §6).
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            self._delete_by_date(cursor, "daily_signals", start, end)
            cursor.execute("DELETE FROM sync_state WHERE key = 'calendar_signals'")
            conn.commit()

    def wipe_metrics(self) -> None:
        """Full reset of all Garmin evidence and ingested daily signals (and every sync
        watermark). Convenience wrapper over the scoped `wipe_garmin_data` /
        `wipe_calendar_signals` for callers that want the whole lot gone."""
        self.wipe_garmin_data()
        self.wipe_calendar_signals()
