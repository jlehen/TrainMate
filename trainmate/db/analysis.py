import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class AnalysisCacheMixin:
    """Backward-evaluation reconstruction cache.

    See DESIGN_backward_evaluation.md §5.1. One row per `horizon`; callers compare the
    stored `fingerprint` against a freshly computed one to decide reuse vs recompute.
    """

    def save_analysis_cache(
        self, horizon: str, fingerprint: str, window_start: Optional[str],
        window_end: Optional[str], reconstruction: Dict[str, Any]
    ) -> None:
        """Upserts the reconstruction for a horizon slot ('long' | 'short').

        Overwrites whatever was cached for that horizon (one-row-per-horizon retention):
        a new fingerprint means the underlying evidence changed, and we only keep the
        current reconstruction. `reconstruction` is stored as a JSON blob.
        """
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(reconstruction)
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO analysis_cache (horizon, fingerprint, window_start, "
                "window_end, reconstruction, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(horizon) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, window_start=excluded.window_start, "
                "window_end=excluded.window_end, reconstruction=excluded.reconstruction, "
                "created_at=excluded.created_at",
                (horizon, fingerprint, window_start, window_end, payload, now)
            )
            conn.commit()

    def get_analysis_cache(self, horizon: str) -> Optional[Dict[str, Any]]:
        """Returns the cached row for a horizon (with `reconstruction` parsed back to a
        dict), or None. The caller decides reuse by matching `fingerprint`."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT horizon, fingerprint, window_start, window_end, reconstruction, "
                "created_at FROM analysis_cache WHERE horizon = ?", (horizon,)
            )
            row = cursor.fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["reconstruction"] = json.loads(result["reconstruction"])
        except (ValueError, TypeError):
            result["reconstruction"] = None
        return result

    def wipe_analysis_cache(self) -> None:
        """Deletes all cached reconstructions."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM analysis_cache")
            conn.commit()
