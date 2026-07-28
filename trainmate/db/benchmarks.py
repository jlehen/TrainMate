from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class BenchmarksMixin:
    """Benchmark-results logbook CRUD (DESIGN_benchmark_workouts.md §3.2).

    A dated log of fitness-test outcomes — the single home for the athlete's trainable
    thresholds now that `ftp`/`lthr` have left config (§3.4). "Latest" is newest by
    `date`, `id` as tiebreak (so a backdated entry behaves), and the latest row per
    `anchor_kind` is what the effective-threshold accessor feeds the coaching prompt and
    the plan-staleness check.
    """

    def add_benchmark_result(
        self, date: str, sport_type: str, anchor_kind: str, value: float,
        unit: str, source: str = "test", workout_id: Optional[int] = None,
        note: Optional[str] = None
    ) -> int:
        """Records one benchmark result and returns its ID."""
        created = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO benchmark_results
                    (date, sport_type, anchor_kind, value, unit, source,
                     workout_id, note, created)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (date, sport_type, anchor_kind, float(value), unit, source,
                  workout_id, note, created))
            conn.commit()
            return int(cursor.lastrowid)

    def get_benchmark_results(
        self, sport_type: Optional[str] = None, anchor_kind: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetches logbook rows newest first (date desc, id desc), optionally filtered by
        sport or anchor kind."""
        query = "SELECT * FROM benchmark_results WHERE 1=1"
        params: List[Any] = []
        if sport_type:
            query += " AND LOWER(sport_type) = ?"
            params.append(sport_type.lower())
        if anchor_kind:
            query += " AND anchor_kind = ?"
            params.append(anchor_kind)
        query += " ORDER BY date DESC, id DESC"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_benchmark_result(self, result_id: int) -> Optional[Dict[str, Any]]:
        """Fetches a single logbook row by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM benchmark_results WHERE id = ?", (result_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_latest_benchmark(
        self, anchor_kind: str
    ) -> Optional[Dict[str, Any]]:
        """The most recent logbook row for an anchor kind (newest by date, id tiebreak),
        or None if the kind has never been recorded."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM benchmark_results WHERE anchor_kind = ? "
                "ORDER BY date DESC, id DESC LIMIT 1",
                (anchor_kind,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def latest_thresholds(self) -> Dict[str, float]:
        """The current effective value of every anchor kind on record — the latest row
        per `anchor_kind`, as {anchor_kind: value}. This is the logbook's contribution to
        the effective-threshold set the coach prescribes from (§3.3)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # For each kind, the row with the newest (date, id). A correlated subquery
            # keeps this a single statement and honours the date-then-id "latest" rule.
            cursor.execute("""
                SELECT b.anchor_kind, b.value
                FROM benchmark_results b
                WHERE b.id = (
                    SELECT b2.id FROM benchmark_results b2
                    WHERE b2.anchor_kind = b.anchor_kind
                    ORDER BY b2.date DESC, b2.id DESC
                    LIMIT 1
                )
            """)
            return {row["anchor_kind"]: float(row["value"]) for row in cursor.fetchall()}

    def delete_benchmark_result(self, result_id: int) -> None:
        """Deletes a logbook row by ID — the correction path (§6: latest-row-wins makes
        delete-and-re-record a sufficient editing story)."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "DELETE FROM benchmark_results WHERE id = ?", (result_id,)
            )
            conn.commit()
