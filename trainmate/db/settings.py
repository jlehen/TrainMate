from datetime import datetime, timezone
from typing import Any, Dict, Optional


class SettingsMixin:
    """App preferences kept as key/value rows.

    See DESIGN_model_selection.md §2. Not training data — a data wipe leaves these alone.
    """

    def get_setting(self, key: str) -> Optional[str]:
        """Returns the stored value for `key`, or None if it was never set."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else None

    def get_setting_row(self, key: str) -> Optional[Dict[str, Any]]:
        """Returns {key, value, updated_at} for `key`, or None. Callers that report *when*
        a preference was last changed need the timestamp too."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value, updated_at FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def set_setting(self, key: str, value: str) -> None:
        """Upserts `key`, stamping updated_at with the current UTC instant."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now)
            )
            conn.commit()

    def clear_setting(self, key: str) -> bool:
        """Deletes `key`. Returns True if a row was actually removed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM settings WHERE key = ?", (key,))
            conn.commit()
            return cursor.rowcount > 0
