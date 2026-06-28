from typing import List, Optional
from trainmate.types import DailyContext


class DailyContextMixin:
    """CRUD for external daily context signals ingested from tagged Calendar events.

    Ingestion is an upsert keyed on the Google Calendar event id, so editing a
    signal updates its row in place and a cancelled event deletes it
    (DESIGN_calendar_context_ingest.md §5–§6).
    """

    def upsert_daily_context_by_event(
        self, google_event_id: str, date: str, metric: str,
        value: Optional[float], text: Optional[str], updated: Optional[str] = None
    ) -> None:
        """Inserts or updates a context signal, reconciled by its Calendar event id."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                """
                INSERT INTO daily_context
                    (google_event_id, date, metric, value, text, updated)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(google_event_id) DO UPDATE SET
                    date=excluded.date,
                    metric=excluded.metric,
                    value=excluded.value,
                    text=excluded.text,
                    updated=excluded.updated
                """,
                (google_event_id, date, metric, value, text, updated),
            )
            conn.commit()

    def delete_daily_context_by_event(self, google_event_id: str) -> None:
        """Removes the context signal for a (cancelled/deleted) Calendar event id."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "DELETE FROM daily_context WHERE google_event_id = ?", (google_event_id,)
            )
            conn.commit()

    def get_daily_context(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None,
        metric: Optional[str] = None
    ) -> List[DailyContext]:
        """Returns context signals within [start_date, end_date] (inclusive), ordered
        by date then metric. Either bound may be omitted for an open-ended range; an
        optional `metric` restricts to a single category."""
        clauses = []
        params: list = []
        if start_date is not None:
            clauses.append("date >= ?")
            params.append(start_date)
        if end_date is not None:
            clauses.append("date <= ?")
            params.append(end_date)
        if metric is not None:
            clauses.append("metric = ?")
            params.append(metric)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT * FROM daily_context {where} ORDER BY date ASC, metric ASC",
                params,
            )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_daily_context_by_id(self, context_id: int) -> Optional[DailyContext]:
        """Returns a single context signal by its local row id, or None."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM daily_context WHERE id = ?", (context_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def delete_daily_context(self, context_id: int) -> None:
        """Removes a context signal by its local row id (the calendar event must be
        deleted separately so a full re-pull can't resurrect it)."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "DELETE FROM daily_context WHERE id = ?", (context_id,)
            )
            conn.commit()

    def list_context_metrics(self) -> List[dict]:
        """Returns the distinct metrics in use with a row count and first/last date,
        ordered by most-recently-seen. Powers `context list-metrics`."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT metric,
                       COUNT(*)  AS count,
                       MIN(date) AS first_date,
                       MAX(date) AS last_date
                FROM daily_context
                GROUP BY metric
                ORDER BY last_date DESC, metric ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]
