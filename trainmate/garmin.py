"""Direct Garmin Connect ingestion.

Replaces the former Google Sheets path: TrainMate logs into Garmin Connect,
fetches daily metrics and activities, stores the measured TSS (power TSS or
hrTSS) and HR/power-zone seconds plus any user-entered RPE, writes them to the
SQLite cache, recomputes the derived metrics (workload/ACWR/baselines), and
maintains a watermark so reads can auto-refresh. Training load is derived on
the fly (see compute_load/activity_load), not stored.

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

# Sports-science windows for the acute:chronic workload ratio (Gabbett/Banister
# lineage): a short acute load over a longer chronic load, the chronic expressed as
# a rolling weekly average (chronic/acute weeks). The PMC (CTL/ATL) EWMA time
# constants live alongside them. All four are config-backed under `garmin:`
# (config.acwr_acute_days / acwr_chronic_days / pmc_ctl_days / pmc_atl_days) so they
# can be experimented with, but the science file's interpretation bands and the
# CLI colors are calibrated to the defaults (7/28/42/7) — non-default constants
# change what the numbers *mean* while the bands keep judging them against the
# standard values, so the defaults are the supported configuration. Read live every
# sweep rather than frozen at import, so an edit can't drift derived values apart
# (e.g. a stored CHRONIC_WEEKS would go stale against a changed window).


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


# ==============================================================================
# Pure transforms (ported from GarminScraper/src/sync.py + sheets estimation)
# ==============================================================================

CYCLING_TERMS = (
    "cycling", "biking", "ride", "cyclocross", "bmx",
    "virtual_ride", "indoor_cycling", "gravel_cycling",
)


# --- Power-zone TSS weights (TSS per second in each zone) -------------------
# Derived from Dr. Andrew Coggan's power-zone model (Allen & Coggan, "Training
# and Racing with a Power Meter"). TSS over a steady effort is IF^2 * 100 per
# hour, so each zone's representative Intensity Factor (IF = fraction of FTP)
# gives a per-second rate of (IF^2 * 100) / 3600. Garmin records power in this
# native Coggan 7-zone model:
#     Z1 Active Recovery   <55% FTP   IF~0.50 -> 0.0069
#     Z2 Endurance       56-75%       IF~0.65 -> 0.0117
#     Z3 Tempo           76-90%       IF~0.80 -> 0.0178
#     Z4 Lactate Thresh  91-105%      IF~0.95 -> 0.0250
#     Z5 VO2max          106-120%     IF~1.10 -> 0.0333
#     Z6 Anaerobic       121-150%     IF~1.35 -> 0.0506
#     Z7 Neuromuscular   >150%        IF~1.60 -> 0.0711  (open-ended; IF capped
#                                                         at 1.60 as a sane max)
POWER_ZONE_TSS_PER_SEC = (0.0069, 0.0117, 0.0178, 0.0250, 0.0333, 0.0506, 0.0711)

# --- Heart-rate-zone TSS weights (TSS per second in each zone) --------------
# Based on Joe Friel's hrTSS methodology ("The Cyclist's Training Bible"):
# time spent in each HR zone, scaled to Lactate Threshold Heart Rate (LTHR),
# is assigned a baseline TSS/hour rate per zone. Expressed here per second
# across Garmin's 5-zone HR model (Z1 recovery -> Z5 above threshold).
HR_ZONE_TSS_PER_SEC = (0.0055, 0.0111, 0.0166, 0.0222, 0.0277)

# The minimum HR-zone coverage before hrTSS is trusted lives in config as
# `garmin.hr_zone_coverage_min` (default 0.5). Garmin's HR zone 1 has a non-zero
# lower bound, so time spent below it (easy walks, yoga, lift-served skiing) lands
# in no zone and hrTSS badly undercounts; below the threshold we defer to RPE.


def _zone_tss(
    zone_sec: Dict[str, Any], prefix: str, weights: Tuple[float, ...]
) -> Optional[float]:
    """Weighted sum of seconds-in-zone. None when no zone carries positive
    time (i.e. the data is absent), so callers can fall through the hierarchy."""
    total = 0.0
    have_data = False
    for i, weight in enumerate(weights, start=1):
        secs = zone_sec.get(f"{prefix}{i}_sec")
        if secs:
            total += float(secs) * weight
            have_data = True
    return round(total, 1) if have_data else None


def _hr_zone_coverage(hr_zone_sec: Dict[str, Any], duration_sec: float) -> float:
    """Fraction of the activity recorded inside any HR zone (0.0 when unknown)."""
    if not duration_sec:
        return 0.0
    total = sum(float(hr_zone_sec.get(f"zone{i}_sec") or 0) for i in range(1, 6))
    return total / duration_sec


def _rpe_tss(rpe: float, duration_sec: float) -> float:
    """Session-RPE (sRPE) TSS estimate. Foster (2001), "A new approach to
    monitoring exercise training", validated for resistance work by Sweet et
    al. (2004). Maps the Borg CR-10 1-10 scale to duration: (RPE*10) per hour."""
    return round((rpe * 10.0) * (duration_sec / 3600.0), 1)


def measured_tss(
    power_zone_sec: Dict[str, Any], hr_zone_sec: Dict[str, Any]
) -> Optional[float]:
    """The objective Training Stress Score actually recorded: power TSS when a
    power meter was present, else hrTSS. None when neither was recorded. This is
    a pure measurement (no coverage gate, no RPE) and is what we store in the
    `tss` column; the training-load fallback is computed separately on the fly."""
    power = _zone_tss(power_zone_sec, "power_zone", POWER_ZONE_TSS_PER_SEC)
    if power is not None:
        return power
    return _zone_tss(hr_zone_sec, "zone", HR_ZONE_TSS_PER_SEC)


def compute_load(
    power_zone_sec: Dict[str, Any],
    hr_zone_sec: Dict[str, Any],
    rpe: Optional[float],
    duration_sec: float,
) -> Tuple[float, str, Optional[str]]:
    """Training load via a best-available fallback (no estimated RPE, no max-ing):

        1. Power TSS                       (Coggan 7-zone)   -> method "power"
        2. hrTSS, if HR coverage >= MIN    (Friel 5-zone)    -> method "hr"
        3. Session RPE * duration          (Foster sRPE)     -> method "rpe"
           (used when power is absent and HR is missing or too sparse to trust)

    When method 3 should apply but the user entered no RPE, we keep the weak
    hrTSS (or 0) and return a warning string so callers can surface it.

    Returns (load, method, warning). `method` identifies the source; `warning`
    is a human reason when we fell back to an unreliable estimate for lack of a
    user RPE, else None. RPE is user-entered only and never synthesised.
    """
    power = _zone_tss(power_zone_sec, "power_zone", POWER_ZONE_TSS_PER_SEC)
    if power is not None:
        return power, "power", None

    hr = _zone_tss(hr_zone_sec, "zone", HR_ZONE_TSS_PER_SEC)
    if hr is not None:
        # Trust hrTSS only when the HR zones cover enough of the session; sparse
        # coverage means the effort sat below zone 1 (low-intensity work) and
        # hrTSS undercounts, so prefer the user's RPE when available.
        if _hr_zone_coverage(hr_zone_sec, duration_sec) >= config.hr_zone_coverage_min:
            return hr, "hr", None
        if rpe:
            return _rpe_tss(float(rpe), duration_sec), "rpe", None
        return hr, "hr_sparse", "low HR-zone coverage and no RPE entered"

    if rpe:
        return _rpe_tss(float(rpe), duration_sec), "rpe", None
    return 0.0, "none", "no power, HR, or RPE data"


def _has_power_zones(act: Dict[str, Any]) -> bool:
    return any(act.get(f"power_zone{i}_sec") for i in range(1, 8))


def _measurement_is_load(act: Dict[str, Any], duration_sec: float) -> bool:
    """True when the stored `tss` measurement is the load (came from power, or
    from HR with adequate coverage), rather than being overridden by RPE."""
    if _has_power_zones(act):
        return True
    has_hr = any(act.get(f"zone{i}_sec") for i in range(1, 6))
    if not has_hr:
        return True  # no zone data to second-guess the stored measurement
    return _hr_zone_coverage(act, duration_sec) >= config.hr_zone_coverage_min


def _divergence_ratio(act: Dict[str, Any], duration_sec: float) -> Optional[float]:
    """Raw sRPE / measured-TSS ratio when the stored `tss` is a trustworthy
    measurement (power, or adequately-covered HR) and the user entered an RPE;
    else None. Shared basis for both inflating the load and flagging divergence,
    so the two always agree on when the meters under-counted real strain."""
    rpe = act.get("rpe")
    tss = act.get("tss")
    if not rpe or not tss or float(tss) <= 0:
        return None
    if not _measurement_is_load(act, duration_sec):
        return None  # measurement isn't the load; RPE already wins, no divergence
    return _rpe_tss(float(rpe), duration_sec) / float(tss)


def _divergence_threshold() -> float:
    return config.rpe_divergence_ratio


def activity_load(act: Dict[str, Any]) -> float:
    """Training load for a stored activity row, derived on the fly. Reads the
    objective measurement from the `tss` column (power TSS or hrTSS) and applies
    the fallback: trust it when it came from power or adequately-covered HR;
    otherwise prefer the user's RPE (sRPE), keeping the weak measurement only
    when no RPE was entered. RPE-only when there is no measurement at all.

    When the measurement is trustworthy but the user's RPE implies a materially
    higher load (>= `rpe_divergence_ratio`), the load is taken from RPE instead:
    the meters under-counted real strain the body paid for (strength/resistance
    work, HIIT, heat, sleep debt). `rpe_divergence` flags the same activities so
    the bump can be explained to the user."""
    tss = act.get("tss")
    rpe = act.get("rpe")
    duration_sec = act.get("duration_sec") or 0.0
    if tss is not None:
        if not _measurement_is_load(act, duration_sec):
            return _rpe_tss(float(rpe), duration_sec) if rpe else float(tss)
        ratio = _divergence_ratio(act, duration_sec)
        if ratio is not None and ratio >= _divergence_threshold():
            return _rpe_tss(float(rpe), duration_sec)
        return float(tss)
    if rpe:
        return _rpe_tss(float(rpe), duration_sec)
    return 0.0


def rpe_divergence(act: Dict[str, Any]) -> Optional[float]:
    """If the load came from an objective measurement (power/HR) but the user's
    RPE implied a materially higher load, returns the ratio sRPE_load / measured;
    else None. Flags strain the meters miss (resistance work, heat, sleep debt,
    muscular damage), e.g. kettlebell HIIT. When this fires, `activity_load` has
    taken the load from RPE; the ratio explains by how much the meters fell short.
    The threshold is config `rpe_divergence_ratio`."""
    duration_sec = act.get("duration_sec") or 0.0
    ratio = _divergence_ratio(act, duration_sec)
    if ratio is None:
        return None
    return round(ratio, 2) if ratio >= _divergence_threshold() else None


def _safe_round(value: Any, ndigits: int = 1) -> float:
    try:
        return round(float(value), ndigits)
    except (ValueError, TypeError):
        return 0.0


# ==============================================================================
# Pull engine
# ==============================================================================

def _ingest_activities(client: GarminClient, start: str, end: str, throttle: float) -> None:
    try:
        activities = client.get_activities(start, end)
    except Exception as e:
        # Skip ingest AND deletion-reconcile on a failed fetch: an empty result
        # here would otherwise be read as "Garmin has no activities" and prune
        # the whole local range.
        print(red(f"Error fetching activities {start}..{end}: {e}"))
        return
    print(f"Found {len(activities)} activities in {start}..{end}.")
    underestimated_activities = []  # activities whose load is a weak estimate for lack of RPE
    fetched_ids = []  # everything Garmin still has in this range, for deletion reconcile
    for idx, act in enumerate(activities):
        activity_id = str(act.get("activityId"))
        fetched_ids.append(activity_id)
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

        zones = client.get_activity_hr_zones(activity_id) if avg_hr is not None else {f"zone{i}_sec": 0 for i in range(1, 6)}
        # Power zones only exist when a power meter was recording (cycling); skip
        # the extra API call otherwise and leave the columns NULL.
        power_zones = (
            client.get_activity_power_zones(activity_id)
            if bike_avg_watts is not None
            else {f"power_zone{i}_sec": None for i in range(1, 8)}
        )

        # RPE is user-entered only; we never synthesise it from power or HR.
        rpe_raw = client.get_activity_rpe(activity_id)
        rpe = int(round(rpe_raw)) if rpe_raw else None

        # Store the objective measurement (power TSS or hrTSS; NULL if neither).
        # The training-load fallback — which may use RPE — is derived on the fly.
        tss = measured_tss(power_zones, zones)
        _load, _method, warning = compute_load(power_zones, zones, rpe, duration_sec)
        if warning:
            act_name = act.get("activityName", "Unknown Activity")
            underestimated_activities.append(f"{date_str} {act_name}")

        db.save_completed_activity(
            activity_id=activity_id, date=date_str, start_time=start_time,
            activity_name=act.get("activityName", "Unknown Activity"),
            activity_type=type_key, duration_sec=float(duration_sec),
            distance_km=_safe_round((act.get("distance") or 0) / 1000.0, 2),
            elevation_gain_m=_safe_round(act.get("elevationGain")),
            avg_hr=avg_hr, max_hr=act.get("maxHR"), rpe=rpe, tss=tss,
            bike_avg_watts=bike_avg_watts, **zones, **power_zones,
        )
        if throttle:
            time.sleep(throttle)

    if underestimated_activities:
        count = len(underestimated_activities)
        print(yellow(
            f"  {count} activit{'y' if count == 1 else 'ies'} "
            "had low HR-zone coverage and no RPE; their load is an underestimate. "
            "Enter an RPE in Garmin for a better load value:"
        ))
        for act_str in underestimated_activities:
            print(yellow(f"    - {act_str}"))

    # Reconcile deletions: drop local rows in this range that Garmin no longer
    # returns (e.g. a duplicate Zwift auto-upload the user deleted in Garmin
    # Connect). Without this they linger and surface as unplanned activities.
    pruned = db.prune_completed_activities(start, end, fetched_ids)
    if pruned:
        print(f"  Removed {pruned} activit{'y' if pruned == 1 else 'ies'} "
              "deleted in Garmin since the last sync.")


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

    client = GarminClient(config.garmin_email, config.garmin_password, config.garmin_token_dir)
    print(f"Logging into Garmin Connect (tokens: {config.garmin_token_dir})...")
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

    # An explicit pull also refreshes external calendar context (best-effort).
    _sync_calendar_context(force=True)
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


# ==============================================================================
# Performance Management Chart — CTL (fitness) / ATL (fatigue) / TSB (form)
# All pure, unit-testable without a DB. See DESIGN_pmc_fitness_fatigue.md §3.
# ==============================================================================

# Trailing 7-day summed load below which the athlete counts as resting for
# effective-history gap detection (§3.3b). Judged per WEEK, not per day, so a token
# easy session inside a layoff (a ~20-TSS walk/jog) still reads as rest instead of
# splitting the layoff in two and defeating the comeback caveat; a genuine return to
# training (several sessions a week) clears it immediately.
_PMC_REST_WEEK_LOAD = 30.0


def compute_pmc(
    daily_load: Dict[str, float],
    start: str, end: str,
    ctl_days: int, atl_days: int,
) -> Dict[str, Tuple[float, float, float]]:
    """CTL/ATL/TSB per calendar day via the classic Coggan discrete 1/τ EWMA.

    Walks EVERY calendar day in [start, end] (not just days with load), so rest days
    and gaps decay the EWMAs with zero load. Both EWMAs seed at 0 at `start`.

        ctl_d = ctl_{d-1} + (load_d - ctl_{d-1}) / ctl_days
        atl_d = atl_{d-1} + (load_d - atl_{d-1}) / atl_days
        tsb_d = ctl_{d-1} - atl_{d-1}   # yesterday's values — the form you woke up with

    The TSB off-by-one is deliberate and load-bearing (training_load.txt §2): today's
    form must NOT include today's workout. Returns {ISO date -> (ctl, atl, tsb)},
    rounded to 1 dp; internal state stays full-precision. Empty {} on a degenerate span.
    """
    out: Dict[str, Tuple[float, float, float]] = {}
    if not start or not end:
        return out
    cur, last = _to_date(start), _to_date(end)
    if cur > last:
        return out
    ctl = atl = 0.0
    while cur <= last:
        ds = cur.isoformat()
        load = daily_load.get(ds, 0.0)
        tsb = ctl - atl                       # yesterday's (pre-update) balance
        ctl = ctl + (load - ctl) / ctl_days
        atl = atl + (load - atl) / atl_days
        out[ds] = (round(ctl, 1), round(atl, 1), round(tsb, 1))
        cur += timedelta(days=1)
    return out


def pmc_warmup_cutoff_for(start: Optional[str], ctl_days: int) -> Optional[str]:
    """ISO date at/after which PMC display values have cleared the leading-edge warm-up.
    A date d is a warm-up artifact (suppress it) iff d < cutoff = start + ctl_days.
    Returns None when there is no history start."""
    if not start:
        return None
    return (_to_date(start) + timedelta(days=ctl_days)).isoformat()


def pmc_effective_history(
    daily_load: Dict[str, float], start: str, end: str, ctl_days: int
) -> Tuple[int, Optional[str]]:
    """Effective history behind `end`: (n_days, effective_start_iso).

    The EWMA re-warms from a low floor not only at DB start but after any layoff long
    enough to decay CTL back toward zero (a run of >= ctl_days functionally-rested days).
    A day counts as rested when the trailing 7-day summed load is under
    _PMC_REST_WEEK_LOAD — a weekly criterion, so an isolated token session inside a
    layoff cannot split it into two sub-threshold runs. Effective history is measured
    from the first trained day after the most recent such gap, or the first trained day
    overall if there is none — so the §3.3(b) convergence caveat re-arms on a comeback
    even when total history is long. (0, None) if no load."""
    if not start or not end:
        return 0, None
    cur, last = _to_date(start), _to_date(end)
    eff_start: Optional[Any] = None
    zero_run = 0
    week: List[float] = []
    while cur <= last:
        week.append(daily_load.get(cur.isoformat(), 0.0))
        if len(week) > 7:
            week.pop(0)
        if sum(week) < _PMC_REST_WEEK_LOAD:
            zero_run += 1
        else:
            if eff_start is None or zero_run >= ctl_days:
                eff_start = cur
            zero_run = 0
        cur += timedelta(days=1)
    if eff_start is None:
        return 0, None
    return (last - eff_start).days + 1, eff_start.isoformat()


def pmc_convergence_pct(n_days: int, ctl_days: int) -> int:
    """CTL convergence proxy after `n_days` of effective history: 1 - e^{-n/τ}, as an
    integer percent (63% at τ, 78% at 1.5τ, 86% at 2τ, 95% at 3τ). A constant-load
    convergence figure, not a literal error bar — framed to the LLM as "~X% converged."""
    if ctl_days <= 0:
        return 100
    return round(100.0 * (1.0 - math.exp(-n_days / ctl_days)))


def pmc_ramp(
    ctl_by_date: Dict[str, Optional[float]], date_iso: str, window: int = 7,
    warmup_cutoff: Optional[str] = None,
) -> Optional[float]:
    """CTL ramp = ctl(date) - ctl(date - window days), in load units per week.

    Uses the exact d-window day, else the nearest EARLIER day carrying a CTL value
    (interior-gap rule, §3.1) — bounded at 2*window back, and scaled to a per-window
    rate when the baseline is older than `window` days, so a stored-series hole can
    never quietly report a multi-week delta as "/week". Returns None when `date` has
    no CTL, when no usable baseline exists at/under date-window (don't emit garbage),
    or when the baseline lands before `warmup_cutoff` — a ramp measured against a
    suppressed warm-up-artifact CTL would read as a phantom overload spike (the §5.4
    straddle guard, applied here so every surface gets it). Reads whatever CTL series
    it is given — callers pass the FULL stored series, never a short prompt window,
    so a small window never spuriously drops it."""
    today_ctl = ctl_by_date.get(date_iso)
    if today_ctl is None:
        return None
    target = _to_date(date_iso) - timedelta(days=window)
    oldest = _to_date(date_iso) - timedelta(days=2 * window)
    best_date = None
    best_val = None
    for ds, v in ctl_by_date.items():
        if v is None:
            continue
        d = _to_date(ds)
        if oldest <= d <= target and (best_date is None or d > best_date):
            best_date, best_val = d, v
    if best_val is None:
        return None
    if warmup_cutoff and best_date.isoformat() < warmup_cutoff:
        return None
    span = (_to_date(date_iso) - best_date).days
    return round((today_ctl - best_val) * window / span, 1)


def project_taper(
    anchor_ctl: float, anchor_atl: float, anchor_date: str, event_date: str,
    load_for_date, ctl_days: int, atl_days: int,
) -> Dict[str, float]:
    """Forward-projects event-day CTL/ATL/TSB from an anchor by walking the same §3.1
    recurrence day-by-day from anchor_date+1 through event_date. `load_for_date` maps an
    ISO date to that day's load (caller supplies actual completed load up to today and
    planned load after). Event-day TSB is CTL(event-1) - ATL(event-1), the form the
    athlete wakes up with on race day. Pure. Returns {'ctl','atl','tsb'} rounded 1 dp."""
    ctl, atl = float(anchor_ctl), float(anchor_atl)
    cur = _to_date(anchor_date) + timedelta(days=1)
    end = _to_date(event_date)
    tsb = ctl - atl
    while cur <= end:
        load = load_for_date(cur.isoformat())
        tsb = ctl - atl                       # form on `cur` = yesterday's balance
        ctl = ctl + (load - ctl) / ctl_days
        atl = atl + (load - atl) / atl_days
        cur += timedelta(days=1)
    return {'ctl': round(ctl, 1), 'atl': round(atl, 1), 'tsb': round(tsb, 1)}


def daily_load_by_date(
    activities: Optional[List[Dict[str, Any]]] = None, dbh=None
) -> Dict[str, float]:
    """Sums per-activity load (via the activity_load fallback hierarchy) per ISO date.
    Reads all completed activities from `dbh` (default: the module db) when none are
    passed. The same series recompute_derived() and the PMC surfaces build their EWMAs
    and caveats from."""
    if activities is None:
        activities = (dbh or db).get_completed_activities()
    daily: Dict[str, float] = {}
    for act in activities:
        d = act["date"]
        daily[d] = daily.get(d, 0.0) + activity_load(act)
    return daily


def pmc_history_start(dbh=None) -> Optional[str]:
    """Earliest ISO date with any Garmin evidence — min(first activity, first metrics
    row). The single source the warm-up cutoff and effective-history clock derive from.

    `dbh` defaults to the module db; callers holding their own handle (CoachService's
    injected db, the CLI's rebindable one) pass it so the cutoff is derived from the
    same database as the metrics it gates."""
    dbh = dbh or db
    firsts = []
    first_activity = dbh.get_first_activity_date()
    if first_activity:
        firsts.append(first_activity)
    metric_dates = dbh.get_metric_dates()
    if metric_dates:
        firsts.append(metric_dates[0])
    return min(firsts) if firsts else None


def pmc_warmup_cutoff(dbh=None) -> Optional[str]:
    """DB-backed single-source warm-up cutoff (§3.3a), passed to every surface — the
    per-day lines, weekly digest, and CLI — so none of them re-derives it differently."""
    return pmc_warmup_cutoff_for(pmc_history_start(dbh), config.pmc_ctl_days)


def pmc_display_values(
    m: Dict[str, Any], warmup_cutoff: Optional[str]
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(ctl, atl, tsb) of a metrics row for DISPLAY: all None inside the §3.3(a)
    warm-up window (stored values there are leading-edge artifacts), the stored
    values (each possibly None) otherwise. The one blanking rule every user surface
    (status line, show-metrics table, CSV) shares."""
    if warmup_cutoff and m["date"] < warmup_cutoff:
        return None, None, None
    return m.get("ctl"), m.get("atl"), m.get("tsb")


def pmc_data_caveat(
    as_of: Optional[str] = None,
    daily_load: Optional[Dict[str, float]] = None,
    dbh=None,
) -> Optional[Dict[str, Any]]:
    """Convergence caveat for when today's own PMC values are still warming (§3.3b).

    Returns {'n_days','pct','effective_start'} while effective history is short
    (< 3*τ_ctl ≈ the 95% mark), else None (the artifact is then negligible). `as_of`
    defaults to today. The caller renders the coach/user wording; the direction of any
    discount is the LLM's to judge from the athlete's pre-DB history — only the
    magnitude is ours."""
    ctl_days = config.pmc_ctl_days
    start = pmc_history_start(dbh)
    if not start:
        return None
    end = as_of or today_str()
    if daily_load is None:
        daily_load = daily_load_by_date(dbh=dbh)
    n_days, eff_start = pmc_effective_history(daily_load, start, end, ctl_days)
    if n_days <= 0 or n_days >= 3 * ctl_days:
        return None
    return {
        "n_days": n_days,
        "pct": pmc_convergence_pct(n_days, ctl_days),
        "effective_start": eff_start,
    }


def recompute_derived(dbh=None) -> None:
    """Recomputes acute/chronic workload, ACWR, and 28-day baselines for ALL cached
    days. A full sweep is trivially cheap on a local DB and avoids windowed-recompute
    bugs (an activity affects 28 days of derived values).

    `dbh` defaults to the module db; the post-wipe recompute (cli/data.py) passes the
    CLI's own handle so it sweeps the same database the wipe just ran against, even
    when the singleton has been rebound (tests, embeddings that inject a db)."""
    dbh = dbh or db
    # One unified load per activity via the fallback hierarchy (power TSS ->
    # hrTSS -> sRPE), not the old `tss + rpe*hours` blend.
    daily_load = daily_load_by_date(dbh=dbh)

    metrics = dbh.get_metrics_cache()  # sorted by date asc
    by_date = {m["date"]: m for m in metrics}

    # Window constants read live from config every sweep (never frozen at import), so an
    # edit can't drift derived values apart. CHRONIC_WEEKS is computed inline from the two
    # windows — never stored or a standalone param — so it can never disagree with them.
    acute_days = config.acwr_acute_days
    chronic_days = config.acwr_chronic_days
    chronic_weeks = chronic_days / acute_days

    # PMC (CTL/ATL/TSB) over EVERY calendar day so rest/gap days decay the EWMAs. The
    # span ends at max(last activity, last metrics): an activities-only pull can leave
    # trailing activity days past the last metrics row, and those carry load that must be
    # walked. Upserted onto existing metrics rows only (activity-only days feed the EWMA
    # but create no cache row).
    span_dates = list(daily_load.keys()) + [m["date"] for m in metrics]
    pmc: Dict[str, Tuple[float, float, float]] = {}
    if span_dates:
        pmc = compute_pmc(
            daily_load, min(span_dates), max(span_dates),
            config.pmc_ctl_days, config.pmc_atl_days,
        )

    for m in metrics:
        date_str = m["date"]
        date_obj = _to_date(date_str)

        acute = sum(daily_load.get((date_obj - timedelta(days=d)).isoformat(), 0.0) for d in range(acute_days))
        total_chronic = sum(daily_load.get((date_obj - timedelta(days=d)).isoformat(), 0.0) for d in range(chronic_days))
        chronic = total_chronic / chronic_weeks
        if chronic > 0.0:
            acwr = acute / chronic
        elif acute > 0.0:
            acwr = 2.0
        else:
            acwr = 1.0

        ctl_atl_tsb = pmc.get(date_str)
        ctl, atl, tsb = ctl_atl_tsb if ctl_atl_tsb else (None, None, None)

        dbh.save_metric_cache(
            date=date_str, rhr=m.get("rhr"), hrv=m.get("hrv"),
            sleep_score=m.get("sleep_score"), stress=m.get("stress"),
            acute_workload=acute, chronic_workload=chronic, acwr=acwr,
            ctl=ctl, atl=atl, tsb=tsb,
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
            dbh.save_baseline(
                date=date_str, rhr_mean=rhr_mean, rhr_std=rhr_std,
                hrv_mean=hrv_mean, hrv_std=hrv_std,
                sleep_mean=sleep_mean, sleep_std=sleep_std,
            )


def backfill_tss(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    verbose: bool = False,
) -> int:
    """Recomputes the measured TSS (power TSS or hrTSS) for every cached activity
    and rewrites the stored value. Activities keep their raw zone seconds, so
    this needs no Garmin calls. Returns the number of rows whose TSS changed, and
    refreshes derived workload/ACWR (which run off the on-the-fly load)."""
    activities = db.get_completed_activities(start_date=start_date, end_date=end_date)
    changed = 0
    sparse: List[Dict[str, Any]] = []
    for act in activities:
        new_tss = measured_tss(act, act)
        old_tss = act.get("tss")
        if (old_tss is None) != (new_tss is None) or (
            old_tss is not None and new_tss is not None
            and abs(float(old_tss) - new_tss) > 1e-6
        ):
            db.update_activity_tss(act["activity_id"], new_tss)
            changed += 1
        _load, _method, warning = compute_load(
            act, act, act.get("rpe"), act.get("duration_sec") or 0.0
        )
        if warning:
            sparse.append(act)
    print(f"Recomputed measured TSS for {len(activities)} activities "
          f"({changed} changed).")
    if sparse:
        print(yellow(
            f"  {len(sparse)} activities have low HR-zone coverage and no RPE; "
            "their load is an underestimate. Enter an RPE in Garmin for accuracy."
        ))
        if verbose:
            for act in sparse:
                date = act.get("date", "?")
                name = act.get("activity_name") or act.get("activity_type", "?")
                dur_sec = act.get("duration_sec") or 0.0
                dur_min = int(dur_sec // 60)
                coverage = _hr_zone_coverage(act, dur_sec)
                print(f"    {date}  {name}  ({dur_min} min, "
                      f"HR-zone coverage {coverage:.0%})")
    recompute_derived()
    return changed


# ==============================================================================
# Auto-ensure — the policy engine
# ==============================================================================

# Process-level memo: the widest [start, end] window already ensured this run, so
# repeated reads (coach.py touches metrics/activities many times) cost nothing and
# we never log into Garmin twice per command.
_ensured: Optional[Tuple[str, str]] = None


def _sync_calendar_context(force: bool) -> None:
    """Bridge to the calendar module's context sync, lazily imported so a missing
    service-account file (calendar unconfigured) can never break a Garmin read. The
    gating, throttling, and error handling all live in google_calendar."""
    try:
        from trainmate import google_calendar
    except Exception:
        return  # Calendar not importable/configured — nothing to sync.
    google_calendar.sync_calendar_context(force=force)


def _pull_command(start: str, end: str) -> str:
    return f"python trainmate_cli.py data pull --from {start} --until {end}"


def _contiguous_regions(missing: List[str]) -> List[Tuple[str, str]]:
    """Groups a sorted list of YYYY-MM-DD dates into contiguous [start, end] regions."""
    regions: List[Tuple[str, str]] = []
    for d in missing:
        if regions and _shift(regions[-1][1], 1) == d:
            regions[-1] = (regions[-1][0], d)
        else:
            regions.append((d, d))
    return regions


def ensure_data(start_date: str, end_date: str, force: bool = False) -> None:
    """Ensures Garmin data covering [start_date, end_date] is present and fresh,
    pulling automatically where the gap is small and surfacing a copy-pastable
    command where it is large. Always continue-with-warning: never aborts, never
    blocks. Call once at command entry with the window the command will read.

    `force` (from --force-pull) bypasses the refresh-minutes throttle: the recent
    mutable zone is re-fetched and Calendar context re-synced even if a refresh ran
    within the freshness window.
    """
    global _ensured
    # Refresh external calendar context alongside the data read (independent of Garmin
    # auth; throttled + memoized inside, best-effort). Done first so it still runs even
    # when Garmin credentials are absent.
    _sync_calendar_context(force=force)

    # No credentials → we can't pull anyway. Stay silent rather than warn on every
    # read; manual `data pull` reports the missing-credentials error explicitly.
    if not (config.garmin_email and config.garmin_password):
        return

    today = today_str()
    pad_start = _shift(start_date, -_derivation_pad_days())
    req_end = min(end_date, today)  # can't pull the future
    if req_end < pad_start:
        return  # window lies entirely in the future

    # Process memo: skip if already covered by a prior ensure this run. --force-pull
    # bypasses the memo so an explicit refresh actually re-fetches.
    if not force and _ensured and _ensured[0] <= pad_start and req_end <= _ensured[1]:
        return

    state = db.get_sync_state()
    present = set(db.get_metric_dates())
    refresh_minutes = config.data_refresh_minutes
    mutable_days = config.garmin_mutable_days
    prompt_days = config.garmin_backfill_prompt_days

    # Cold start: nothing pulled ever -> hand the user a backfill command.
    if not state and not present:
        sugg_start = min(pad_start, _shift(today, -config.garmin_initial_backfill_days))
        _warn_manual(sugg_start, today, cold=True)
        _remember(pad_start, req_end)
        return

    # Is the recent (mutable) zone stale? --force-pull treats it as stale unconditionally.
    stale = True
    last_pull_age_min: Optional[int] = None
    if state and state.get("last_pull_utc"):
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(state["last_pull_utc"])
            last_pull_age_min = int(age.total_seconds() // 60)
            stale = age > timedelta(minutes=refresh_minutes)
        except (ValueError, TypeError):
            stale = True
    if force:
        stale = True
    mutable_start = _shift(today, -(mutable_days - 1))

    # A needed day must be fetched if it has no row, or it's in the (stale) mutable
    # zone where values can still change.
    to_fetch = [
        d for d in _date_range(pad_start, req_end)
        if d not in present or (stale and d >= mutable_start)
    ]
    if not to_fetch:
        # Nothing to do. If the only reason we're not re-fetching the recent mutable zone
        # is the refresh-minutes throttle, say so — otherwise this silent reuse is opaque.
        if not stale and req_end >= mutable_start:
            age_note = (
                f"last pull {last_pull_age_min}m ago, < {refresh_minutes}m"
                if last_pull_age_min is not None else "recently pulled"
            )
            print(dim(
                f"Garmin data is fresh ({age_note}); using cache. "
                "Pass --force-pull to refresh now."
            ))
        _remember(pad_start, req_end)
        return

    auto_regions: List[Tuple[str, str]] = []
    surfaced_regions: List[Tuple[str, str]] = []
    pad_days = _derivation_pad_days()
    for region in _contiguous_regions(to_fetch):
        span = (_to_date(region[1]) - _to_date(region[0])).days + 1
        # Small gaps (incl. the cheap recent mutable-zone refresh, always <=
        # mutable_days) pull automatically; large ones are surfaced as a command.
        # Regions lying entirely BEFORE the requested window exist only to warm the
        # derivation pad — they are bounded by the pad itself and were never asked
        # for by the user, so they always auto-pull: without this, widening the pad
        # (28 -> 63 days for CTL) would leave every pre-existing install nagging
        # "run data pull --from ..." on each command instead of healing itself.
        limit = max(prompt_days, pad_days) if region[1] < start_date else prompt_days
        if span <= limit:
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
            "Baselines and ACWR may be incomplete. Fitness/fatigue (CTL/ATL/TSB) also "
            "warm up over ~6 weeks of history, so on a shallow backfill freshness can "
            "read artificially low. To backfill, run:"
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
