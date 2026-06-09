"""Direct Garmin Connect ingestion.

Replaces the former Google Sheets path: TrainMate logs into Garmin Connect,
fetches daily metrics and activities, computes TSS/RPE/HR-zones, writes them to
the SQLite cache, recomputes the derived metrics (workload/ACWR/baselines), and
maintains a watermark so reads can auto-refresh.

See DESIGN_garmin_direct_pull.md for the full design.
"""
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from trainmate.config import config
from trainmate.db import db
from trainmate.util import today_date, today_str, yellow, red, dim

# Raw history needed before a displayed window so ACWR/chronic-load/baselines
# (28-day lookbacks) are correct for the earliest displayed day.
DERIVATION_PAD_DAYS = 28


class GarminAuthRequired(Exception):
    """Raised when a fresh Garmin login (MFA) is needed but no TTY is available.

    Callers treat this as continue-with-warning: use cached data and tell the
    user to run `data pull` in a terminal to re-authenticate.
    """


# ==============================================================================
# Date helpers
# ==============================================================================

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


# ==============================================================================
# Garmin client (ported from GarminScraper/src/garmin_client.py)
# ==============================================================================

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
                "Garmin credentials are not configured. Set GARMIN_EMAIL and "
                "GARMIN_PASSWORD (env) or garmin_email/garmin_password in config.yaml."
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
        """Fetches the activity summary list within a date range (inclusive)."""
        try:
            return self.api.get_activities_by_date(start_date, end_date) or []
        except Exception as e:
            print(red(f"Error fetching activities {start_date}..{end_date}: {e}"))
            return []

    def get_activity_hr_zones(self, activity_id: Any) -> Dict[str, int]:
        """Seconds spent in each HR zone (1-5) for an activity."""
        zones = {f"zone{i}_sec": 0 for i in range(1, 6)}
        try:
            data = self.api.get_activity_hr_in_timezones(activity_id)
            entries = data if isinstance(data, list) else (data.get("hrZoneDTO") if isinstance(data, dict) else None)
            for z in entries or []:
                if not isinstance(z, dict):
                    continue
                zid = z.get("zoneNumber") or z.get("zoneId")
                secs = z.get("secsInZone") or z.get("timeInZone", 0)
                if zid in (1, 2, 3, 4, 5):
                    zones[f"zone{zid}_sec"] = int(float(secs))
        except Exception:
            pass
        return zones

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


# ==============================================================================
# Pure transforms (ported from GarminScraper/src/sync.py + sheets estimation)
# ==============================================================================

CYCLING_TERMS = (
    "cycling", "biking", "ride", "cyclocross", "bmx",
    "virtual_ride", "indoor_cycling", "gravel_cycling",
)


def calculate_tss(activity: Dict[str, Any], ftp: Optional[float], lthr: Optional[float]) -> Optional[float]:
    """Estimates TSS from power (preferred) or heart rate. None if not computable."""
    duration_sec = activity.get("duration") or 0
    if not duration_sec:
        return None
    hours = duration_sec / 3600.0

    avg_power = activity.get("avgPower") or activity.get("averagePower")
    if avg_power is not None and ftp:
        try:
            intensity = float(avg_power) / float(ftp)
            return round(100.0 * hours * (intensity ** 2), 1)
        except (ValueError, TypeError, ZeroDivisionError):
            pass

    avg_hr = activity.get("averageHR")
    if avg_hr is not None and lthr:
        try:
            intensity = float(avg_hr) / float(lthr)
            return round(100.0 * hours * (intensity ** 2), 1)
        except (ValueError, TypeError, ZeroDivisionError):
            pass

    return None


def estimate_rpe_tss(sport_type: str, duration_sec: float, avg_hr: Optional[int]) -> Tuple[int, float]:
    """Fallback RPE/TSS estimate from HR (or sport defaults). Mirrors the prior
    sheets-reader estimation so behaviour is unchanged when Garmin lacks data."""
    hours = duration_sec / 3600.0
    profile = config.user_profile or {}
    lthr = profile.get("lthr")
    if not lthr:
        max_hr = profile.get("max_hr")
        lthr = int(round(max_hr * 0.85)) if max_hr else 165

    sport = (sport_type or "").lower().replace("_", " ")
    if not avg_hr or avg_hr <= 0:
        if "yoga" in sport:
            return 2, hours * 15.0
        if "strength" in sport:
            return 5, hours * 45.0
        if "rest" in sport:
            return 0, 0.0
        return 3, hours * 30.0

    ratio = avg_hr / lthr
    rpe = max(2, min(int(round(ratio * 8.0)), 10))
    return rpe, hours * (ratio ** 2) * 100.0


def _safe_round(value: Any, ndigits: int = 1) -> float:
    try:
        return round(float(value), ndigits)
    except (ValueError, TypeError):
        return 0.0


# ==============================================================================
# Pull engine
# ==============================================================================

def _ingest_activities(client: GarminClient, start: str, end: str, throttle: float) -> None:
    profile = config.user_profile or {}
    ftp = profile.get("ftp")
    lthr = profile.get("lthr")

    activities = client.get_activities(start, end)
    print(f"Found {len(activities)} activities in {start}..{end}.")
    for idx, act in enumerate(activities):
        activity_id = str(act.get("activityId"))
        start_time = act.get("startTimeLocal", "") or ""
        date_str = start_time.split(" ")[0] if start_time else ""
        if not date_str:
            continue
        type_key = (act.get("activityType") or {}).get("typeKey", "unknown")
        duration_sec = act.get("duration") or 0.0
        avg_hr = act.get("averageHR")

        bike_avg_watts = None
        if any(term in (type_key or "").lower() for term in CYCLING_TERMS):
            power = act.get("avgPower") or act.get("averagePower")
            if power is not None:
                try:
                    bike_avg_watts = int(round(float(power)))
                except (ValueError, TypeError):
                    pass

        tss = calculate_tss(act, ftp, lthr)
        rpe_raw = client.get_activity_rpe(activity_id)
        rpe = int(round(rpe_raw)) if rpe_raw else None
        if rpe is None or tss is None:
            est_rpe, est_tss = estimate_rpe_tss(type_key, duration_sec, avg_hr)
            rpe = est_rpe if rpe is None else rpe
            tss = est_tss if tss is None else tss

        zones = client.get_activity_hr_zones(activity_id) if avg_hr is not None else {f"zone{i}_sec": 0 for i in range(1, 6)}

        db.save_completed_activity(
            activity_id=activity_id, date=date_str, start_time=start_time,
            activity_name=act.get("activityName", "Unknown Activity"),
            activity_type=type_key, duration_sec=float(duration_sec),
            distance_km=_safe_round((act.get("distance") or 0) / 1000.0, 2),
            elevation_gain_m=_safe_round(act.get("elevationGain")),
            avg_hr=avg_hr, max_hr=act.get("maxHR"), rpe=int(rpe), tss=float(tss),
            bike_avg_watts=bike_avg_watts, **zones,
        )
        if throttle:
            time.sleep(throttle)


def _ingest_metrics(client: GarminClient, start: str, end: str, throttle: float) -> None:
    dates = _date_range(start, end)
    print(f"Fetching daily metrics for {len(dates)} day(s) {start}..{end}...")
    for date_str in dates:
        m = client.get_daily_metrics(date_str)
        # Write a row for EVERY day in range, even all-null, so the metrics-cache
        # date coverage records what has been pulled (gap detection). Derived
        # fields are left None here; recompute fills them via COALESCE.
        db.save_metric_cache(
            date=date_str,
            rhr=_int_or_none(m["rhr"]), hrv=_int_or_none(m["hrv"]),
            sleep_score=_int_or_none(m["sleep_score"]), stress=_int_or_none(m["stress"]),
        )
        if throttle:
            time.sleep(throttle)


def _int_or_none(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (ValueError, TypeError):
        return None


def pull(
    start_date: str, end_date: str, *,
    metrics: bool = True, activities: bool = True,
    throttle: Optional[float] = None, advance_watermark: bool = True,
) -> None:
    """Pulls a date range directly from Garmin into the DB, then recomputes derived
    metrics and (optionally) advances the watermark. Raises GarminAuthRequired if a
    non-interactive re-auth is needed."""
    if throttle is None:
        throttle = config.garmin_throttle_seconds

    client = GarminClient(config.garmin_email, config.garmin_password, config.garmin_token_store)
    print(f"Logging into Garmin Connect (tokens: {config.garmin_token_store})...")
    client.login()

    if activities:
        _ingest_activities(client, start_date, end_date, throttle)
    if metrics:
        _ingest_metrics(client, start_date, end_date, throttle)

    recompute_derived()

    if advance_watermark:
        state = db.get_sync_state()
        prev_through = state["through_date"] if state else None
        # through_date is a forward high-water mark; backfills never regress it.
        new_through = max([d for d in (prev_through, end_date) if d], default=end_date)
        db.set_sync_state(
            through_date=new_through,
            last_pull_utc=datetime.now(timezone.utc).isoformat(),
        )
    print("Garmin sync completed.")


# ==============================================================================
# Derived recompute — full sweep (ported from former sheets_reader.sync_data)
# ==============================================================================

def _mean_std(values: List[float]) -> Tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, math.sqrt(variance)


def recompute_derived() -> None:
    """Recomputes acute/chronic workload, ACWR, and 28-day baselines for ALL cached
    days. A full sweep is trivially cheap on a local DB and avoids windowed-recompute
    bugs (an activity affects 28 days of derived values)."""
    activities = db.get_completed_activities()
    daily_load: Dict[str, float] = {}
    for act in activities:
        date_str = act["date"]
        tss = act.get("tss") or 0.0
        rpe = act.get("rpe") or 0
        duration_sec = act.get("duration_sec") or 0.0
        daily_load[date_str] = daily_load.get(date_str, 0.0) + tss + rpe * (duration_sec / 3600.0)

    metrics = db.get_metrics_cache()  # sorted by date asc
    by_date = {m["date"]: m for m in metrics}

    for m in metrics:
        date_str = m["date"]
        date_obj = _to_date(date_str)

        acute = sum(daily_load.get((date_obj - timedelta(days=d)).isoformat(), 0.0) for d in range(7))
        total_28 = sum(daily_load.get((date_obj - timedelta(days=d)).isoformat(), 0.0) for d in range(28))
        chronic = total_28 / 4.0
        if chronic > 0.0:
            acwr = acute / chronic
        elif acute > 0.0:
            acwr = 2.0
        else:
            acwr = 1.0

        db.save_metric_cache(
            date=date_str, rhr=m.get("rhr"), hrv=m.get("hrv"),
            sleep_score=m.get("sleep_score"), stress=m.get("stress"),
            acute_workload=acute, chronic_workload=chronic, acwr=acwr,
        )

        rhr_vals, hrv_vals, sleep_vals = [], [], []
        for d in range(1, 29):
            prev = by_date.get((date_obj - timedelta(days=d)).isoformat())
            if not prev:
                continue
            if prev.get("rhr") is not None:
                rhr_vals.append(prev["rhr"])
            if prev.get("hrv") is not None:
                hrv_vals.append(prev["hrv"])
            if prev.get("sleep_score") is not None:
                sleep_vals.append(prev["sleep_score"])

        if len(rhr_vals) >= 7 or len(hrv_vals) >= 7 or len(sleep_vals) >= 7:
            rhr_mean, rhr_std = _mean_std(rhr_vals)
            hrv_mean, hrv_std = _mean_std(hrv_vals)
            sleep_mean, sleep_std = _mean_std(sleep_vals)
            db.save_baseline(
                date=date_str, rhr_mean=rhr_mean, rhr_std=rhr_std,
                hrv_mean=hrv_mean, hrv_std=hrv_std,
                sleep_mean=sleep_mean, sleep_std=sleep_std,
            )


# ==============================================================================
# Auto-ensure — the policy engine
# ==============================================================================

# Process-level memo: the widest [start, end] window already ensured this run, so
# repeated reads (coach.py touches metrics/activities many times) cost nothing and
# we never log into Garmin twice per command.
_ensured: Optional[Tuple[str, str]] = None


def _pull_command(start: str, end: str) -> str:
    return f"python trainmate_cli.py data pull --start-date {start} --end-date {end}"


def _contiguous_regions(missing: List[str]) -> List[Tuple[str, str]]:
    """Groups a sorted list of YYYY-MM-DD dates into contiguous [start, end] regions."""
    regions: List[Tuple[str, str]] = []
    for d in missing:
        if regions and _shift(regions[-1][1], 1) == d:
            regions[-1] = (regions[-1][0], d)
        else:
            regions.append((d, d))
    return regions


def ensure_data(start_date: str, end_date: str) -> None:
    """Ensures Garmin data covering [start_date, end_date] is present and fresh,
    pulling automatically where the gap is small and surfacing a copy-pastable
    command where it is large. Always continue-with-warning: never aborts, never
    blocks. Call once at command entry with the window the command will read.
    """
    global _ensured
    # No credentials → we can't pull anyway. Stay silent rather than warn on every
    # read; manual `data pull` reports the missing-credentials error explicitly.
    if not (config.garmin_email and config.garmin_password):
        return

    today = today_str()
    pad_start = _shift(start_date, -DERIVATION_PAD_DAYS)
    req_end = min(end_date, today)  # can't pull the future
    if req_end < pad_start:
        return  # window lies entirely in the future

    # Process memo: skip if already covered by a prior ensure this run.
    if _ensured and _ensured[0] <= pad_start and req_end <= _ensured[1]:
        return

    state = db.get_sync_state()
    present = set(db.get_metric_dates())
    refresh_minutes = config.garmin_refresh_minutes
    mutable_days = config.garmin_mutable_days
    prompt_days = config.garmin_backfill_prompt_days

    # Cold start: nothing pulled ever -> hand the user a backfill command.
    if not state and not present:
        sugg_start = min(pad_start, _shift(today, -config.garmin_initial_backfill_days))
        _warn_manual(sugg_start, today, cold=True)
        _remember(pad_start, req_end)
        return

    # Is the recent (mutable) zone stale?
    stale = True
    if state and state.get("last_pull_utc"):
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(state["last_pull_utc"])
            stale = age > timedelta(minutes=refresh_minutes)
        except (ValueError, TypeError):
            stale = True
    mutable_start = _shift(today, -(mutable_days - 1))

    # A needed day must be fetched if it has no row, or it's in the (stale) mutable
    # zone where values can still change.
    to_fetch = [
        d for d in _date_range(pad_start, req_end)
        if d not in present or (stale and d >= mutable_start)
    ]
    if not to_fetch:
        _remember(pad_start, req_end)
        return

    auto_regions: List[Tuple[str, str]] = []
    surfaced_regions: List[Tuple[str, str]] = []
    for region in _contiguous_regions(to_fetch):
        span = (_to_date(region[1]) - _to_date(region[0])).days + 1
        # Small gaps (incl. the cheap recent mutable-zone refresh, always <=
        # mutable_days) pull automatically; large ones are surfaced as a command.
        if span <= prompt_days:
            auto_regions.append(region)
        else:
            surfaced_regions.append(region)

    for region in auto_regions:
        try:
            print(dim(f"Auto-syncing Garmin {region[0]}..{region[1]}..."))
            pull(region[0], region[1], throttle=config.garmin_throttle_seconds)
        except GarminAuthRequired:
            print(yellow(
                "Garmin re-auth required — run `python trainmate_cli.py data pull` "
                "in a terminal. Continuing with cached data."
            ))
            break
        except Exception as e:
            print(yellow(f"Garmin sync failed ({e}). Continuing with cached data."))
            break

    if surfaced_regions:
        merged_start = min(r[0] for r in surfaced_regions)
        merged_end = max(r[1] for r in surfaced_regions)
        _warn_manual(merged_start, merged_end, cold=False)

    _remember(pad_start, req_end)


def _warn_manual(start: str, end: str, *, cold: bool) -> None:
    if cold:
        print(yellow(
            "No Garmin data has been pulled yet. To get started, run:"
        ))
    else:
        print(yellow(
            f"This view needs Garmin data back to {start}, which hasn't been pulled. "
            "Baselines and ACWR may be incomplete. To backfill, run:"
        ))
    print(f"  {_pull_command(start, end)}")


def _remember(start: str, end: str) -> None:
    global _ensured
    if _ensured is None:
        _ensured = (start, end)
    else:
        _ensured = (min(_ensured[0], start), max(_ensured[1], end))


def reset_memo() -> None:
    """Clears the process-level ensure memo (used by tests)."""
    global _ensured
    _ensured = None
