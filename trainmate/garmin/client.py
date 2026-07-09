"""Garmin Connect client: login, metric/activity fetch, and date helpers."""
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from trainmate.config import config
from trainmate.db import db
from trainmate.util import today_date, today_str, yellow, red, dim

def _derivation_pad_days() -> int:
    """Raw history needed *before* a displayed window so ACWR/chronic-load/baselines
    and the CTL EWMA are warm for the earliest displayed day. Read live from config.

    `max(acwr_chronic_days, 28, ceil(1.5*pmc_ctl_days))` (= 63 at defaults): the 28
    floor pins the pad to the hardcoded 28-day baseline lookback in recompute_derived()
    even if the chronic window is shrunk below it; the 1.5*τ_ctl term warms CTL to
    ~78% at the left edge (the §3.3(b) accuracy caveat carries the residual). See
    DESIGN_pmc_fitness_fatigue.md §3.4."""
    return max(
        config.acwr_chronic_days,
        28,
        math.ceil(1.5 * config.pmc_ctl_days),
    )
class GarminAuthRequired(Exception):
    """Raised when a fresh Garmin login (MFA) is needed but no TTY is available.

    Callers treat this as continue-with-warning: use cached data and tell the
    user to run `data pull` in a terminal to re-authenticate.
    """
def _to_date(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()
def _date_range(start: str, end: str) -> List[str]:
    """Inclusive list of YYYY-MM-DD strings from start to end."""
    out: List[str] = []
    cur, last = _to_date(start), _to_date(end)
    while cur <= last:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out
def _shift(date_str: str, days: int) -> str:
    return (_to_date(date_str) + timedelta(days=days)).isoformat()
class GarminClient:
    """Thin wrapper over `garminconnect` with token persistence and TTY-gated MFA."""

    def __init__(self, email: Optional[str], password: Optional[str], token_store_dir: str):
        self.email = email
        self.password = password
        self.token_store_dir = token_store_dir
        self.api: Any = None

    def login(self) -> None:
        """Logs into Garmin Connect, resuming from persisted tokens when possible.

        garminconnect persists a long-lived OAuth1 token, so MFA is only needed on
        the first login or a rare expiry. On a TTY we prompt for the code; off a
        TTY (web/cron) we raise GarminAuthRequired instead of blocking on input().
        """
        try:
            from garminconnect import Garmin  # lazy import: optional dependency
        except ImportError as e:
            raise RuntimeError(
                "The 'garminconnect' package is required for Garmin sync. "
                "Install it: pip install garminconnect"
            ) from e

        if not self.email or not self.password:
            raise RuntimeError(
                "Garmin credentials are not configured. Set garmin_email and "
                "garmin_password in config.yaml."
            )

        def prompt_mfa() -> str:
            if not sys.stdin.isatty():
                raise GarminAuthRequired(
                    "Garmin requires MFA but no interactive terminal is available."
                )
            print("\n[!] Garmin Multi-Factor Authentication (MFA) is required.")
            return input("Enter the MFA code sent to your email/phone: ").strip()

        self.api = Garmin(email=self.email, password=self.password, prompt_mfa=prompt_mfa)
        self.api.login(tokenstore=self.token_store_dir)

    def get_daily_metrics(self, date_str: str) -> Dict[str, Any]:
        """Fetches RHR, overnight HRV, sleep score, and average stress for a date."""
        metrics: Dict[str, Any] = {"rhr": None, "hrv": None, "sleep_score": None, "stress": None}

        try:
            stats = self.api.get_stats(date_str)
            if isinstance(stats, dict):
                metrics["rhr"] = stats.get("restingHeartRate")
                metrics["stress"] = stats.get("averageStressLevel")
        except Exception as e:
            print(dim(f"[{date_str}] daily stats unavailable: {e}"))

        try:
            sleep_data = self.api.get_sleep_data(date_str)
            if isinstance(sleep_data, dict):
                daily_sleep = sleep_data.get("dailySleepDTO")
                if isinstance(daily_sleep, dict):
                    scores = daily_sleep.get("sleepScores")
                    if isinstance(scores, dict):
                        metrics["sleep_score"] = scores.get("overall", {}).get("value")
                    if metrics["sleep_score"] is None:
                        q = daily_sleep.get("sleepQualityScore")
                        if isinstance(q, dict):
                            metrics["sleep_score"] = q.get("score")
        except Exception as e:
            print(dim(f"[{date_str}] sleep data unavailable: {e}"))

        try:
            hrv_data = self.api.get_hrv_data(date_str)
            if isinstance(hrv_data, dict):
                summary = hrv_data.get("hrvSummary")
                if isinstance(summary, dict):
                    metrics["hrv"] = summary.get("lastNightAvg")
                else:
                    metrics["hrv"] = hrv_data.get("lastNightAvg")
        except Exception:
            pass  # older/non-HRV devices

        return metrics

    def get_activities(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """Fetches the activity summary list within a date range (inclusive).

        Raises on API failure rather than masking it as an empty list: the caller
        reconciles deletions against this result, so a swallowed error would look
        like "Garmin has no activities here" and wipe the local range."""
        return self.api.get_activities_by_date(start_date, end_date) or []

    @staticmethod
    def _parse_zone_entries(data: Any, num_zones: int, prefix: str) -> Dict[str, int]:
        """Parses a Garmin *TimeInZones payload into {prefix}{n}_sec seconds.

        Both the HR and power endpoints return the same shape: a list (or a dict
        wrapping it under hrZoneDTO) of per-zone records with a zone number and a
        seconds-in-zone field under one of a couple of key spellings.
        """
        zones = {f"{prefix}{i}_sec": 0 for i in range(1, num_zones + 1)}
        entries = data if isinstance(data, list) else (
            data.get("hrZoneDTO") if isinstance(data, dict) else None)
        for z in entries or []:
            if not isinstance(z, dict):
                continue
            zid = z.get("zoneNumber") or z.get("zoneId")
            secs = z.get("secsInZone") or z.get("timeInZone", 0)
            if zid in range(1, num_zones + 1):
                zones[f"{prefix}{zid}_sec"] = int(float(secs))
        return zones

    def get_activity_hr_zones(self, activity_id: Any) -> Dict[str, int]:
        """Seconds spent in each HR zone (1-5) for an activity."""
        try:
            data = self.api.get_activity_hr_in_timezones(activity_id)
            return self._parse_zone_entries(data, 5, "zone")
        except Exception:
            return {f"zone{i}_sec": 0 for i in range(1, 6)}

    def get_activity_power_zones(self, activity_id: Any) -> Dict[str, Optional[int]]:
        """Seconds spent in each power zone (Garmin's 7-zone model) for an activity.

        Power zones only exist for activities recorded with a power meter (cycling).
        Returns all-None when no power-zone data is available, so the columns stay
        NULL rather than a misleading zero for non-power activities.
        """
        try:
            data = self.api.get_activity_power_in_timezones(activity_id)
            parsed = self._parse_zone_entries(data, 7, "power_zone")
            if any(v for v in parsed.values()):
                return parsed  # type: ignore[return-value]
        except Exception:
            pass
        return {f"power_zone{i}_sec": None for i in range(1, 8)}

    def get_activity_rpe(self, activity_id: Any) -> Optional[float]:
        """Fetches manually-entered RPE on a 1-10 scale, or None if unspecified."""
        try:
            act = self.api.get_activity(activity_id)
            if isinstance(act, dict):
                rpe = act.get("summaryDTO", {}).get("directWorkoutRpe")
                if rpe is not None:
                    return float(rpe) / 10.0
        except Exception:
            pass
        return None
