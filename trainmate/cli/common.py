"""Shared helpers used across the CLI command modules."""
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.util import yellow, today_str as _today_str


def fmt_date(date_str: str) -> str:
    """Return 'YYYY-MM-DD Ddd' (e.g. '2026-06-05 Fri')."""
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d %a")


def ensure_recent_data(end_date: Optional[str] = None, no_pull: bool = False) -> None:
    """Ensures Garmin data covering the recent metrics window is present and fresh,
    auto-pulling small/recent gaps and surfacing large backfills as a command. Warns
    if today's metrics are still unavailable afterward."""
    if no_pull:
        return
    end_date = end_date or _today_str()
    history_days = config.metrics_lookback_days
    start_date = (
        datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(days=history_days - 1)
    ).strftime("%Y-%m-%d")
    cli.garmin.ensure_data(start_date, end_date)

    today = _today_str()
    if end_date == today:
        rows = cli.db.get_metrics_cache(start_date=today, end_date=today)
        present = bool(rows) and not (
            rows[0].get('rhr') is None and rows[0].get('hrv') is None
            and rows[0].get('sleep_score') is None and rows[0].get('stress') is None
        )
        if not present:
            print(yellow(f"Note: Garmin metrics for today ({today}) are not available yet."))
