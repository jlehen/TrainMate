from typing import Any, Dict, List, Optional
from trainmate.types import AthleteMetric, AthleteBaseline, CompletedActivity


class ActivitiesMixin:
    """Completed Garmin activities, daily metrics cache, baselines, and sync watermark."""

    # --- Completed Activities ---
    def save_completed_activity(
        self, activity_id: str, date: str, start_time: Optional[str],
        activity_name: Optional[str], activity_type: str, duration_sec: float,
        distance_km: float, elevation_gain_m: float, avg_hr: Optional[int],
        max_hr: Optional[int], rpe: Optional[int], tss: Optional[float],
        bike_avg_watts: Optional[int] = None, zone1_sec: Optional[int] = None,
        zone2_sec: Optional[int] = None, zone3_sec: Optional[int] = None,
        zone4_sec: Optional[int] = None, zone5_sec: Optional[int] = None,
        power_zone1_sec: Optional[int] = None, power_zone2_sec: Optional[int] = None,
        power_zone3_sec: Optional[int] = None, power_zone4_sec: Optional[int] = None,
        power_zone5_sec: Optional[int] = None, power_zone6_sec: Optional[int] = None,
        power_zone7_sec: Optional[int] = None
    ) -> None:
        """Saves a completed Garmin activity, updating it if it already exists."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO completed_activities (
                    activity_id, date, start_time, activity_name, activity_type,
                    duration_sec, distance_km, elevation_gain_m, avg_hr, max_hr,
                    rpe, tss, bike_avg_watts, zone1_sec, zone2_sec, zone3_sec,
                    zone4_sec, zone5_sec, power_zone1_sec, power_zone2_sec,
                    power_zone3_sec, power_zone4_sec, power_zone5_sec,
                    power_zone6_sec, power_zone7_sec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    date=excluded.date,
                    start_time=excluded.start_time,
                    activity_name=excluded.activity_name,
                    activity_type=excluded.activity_type,
                    duration_sec=excluded.duration_sec,
                    distance_km=excluded.distance_km,
                    elevation_gain_m=excluded.elevation_gain_m,
                    avg_hr=excluded.avg_hr,
                    max_hr=excluded.max_hr,
                    rpe=excluded.rpe,
                    tss=excluded.tss,
                    bike_avg_watts=excluded.bike_avg_watts,
                    zone1_sec=excluded.zone1_sec,
                    zone2_sec=excluded.zone2_sec,
                    zone3_sec=excluded.zone3_sec,
                    zone4_sec=excluded.zone4_sec,
                    zone5_sec=excluded.zone5_sec,
                    power_zone1_sec=excluded.power_zone1_sec,
                    power_zone2_sec=excluded.power_zone2_sec,
                    power_zone3_sec=excluded.power_zone3_sec,
                    power_zone4_sec=excluded.power_zone4_sec,
                    power_zone5_sec=excluded.power_zone5_sec,
                    power_zone6_sec=excluded.power_zone6_sec,
                    power_zone7_sec=excluded.power_zone7_sec
            """, (activity_id, date, start_time, activity_name, activity_type,
                  duration_sec, distance_km, elevation_gain_m, avg_hr, max_hr,
                  rpe, tss, bike_avg_watts, zone1_sec, zone2_sec, zone3_sec,
                  zone4_sec, zone5_sec, power_zone1_sec, power_zone2_sec,
                  power_zone3_sec, power_zone4_sec, power_zone5_sec,
                  power_zone6_sec, power_zone7_sec))
            conn.commit()

    def update_activity_tss(self, activity_id: str, tss: Optional[float]) -> None:
        """Overwrites the stored measured TSS for one activity (backfill). None
        clears it (no power/HR data was recorded)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE completed_activities SET tss = ? WHERE activity_id = ?",
                (None if tss is None else float(tss), str(activity_id)),
            )
            conn.commit()

    def prune_completed_activities(
        self, start_date: str, end_date: str, keep_ids: List[str]
    ) -> int:
        """Deletes completed activities in [start_date, end_date] whose activity_id
        is not in `keep_ids`. Used to reconcile a fresh Garmin pull with local
        state: an activity removed upstream (e.g. a duplicate Zwift auto-upload the
        user deleted in Garmin Connect) would otherwise linger and show up as an
        unplanned activity. Returns the number of rows deleted.

        Scoped to the pulled date range so a narrow pull never touches activities
        outside the window it actually re-fetched."""
        keep = {str(i) for i in keep_ids}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT activity_id FROM completed_activities "
                "WHERE date >= ? AND date <= ?",
                (start_date, end_date),
            )
            stale = [
                row["activity_id"]
                for row in cursor.fetchall()
                if row["activity_id"] not in keep
            ]
            for activity_id in stale:
                cursor.execute(
                    "DELETE FROM completed_activities WHERE activity_id = ?",
                    (activity_id,),
                )
            conn.commit()
            return len(stale)

    def get_completed_activities(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> List[CompletedActivity]:
        """Fetches completed activities, optionally within a date range."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_date and end_date:
                cursor.execute(
                    "SELECT * FROM completed_activities WHERE date >= ? AND date <= ? "
                    "ORDER BY date ASC, start_time ASC",
                    (start_date, end_date)
                )
            elif start_date:
                cursor.execute(
                    "SELECT * FROM completed_activities WHERE date >= ? "
                    "ORDER BY date ASC, start_time ASC",
                    (start_date,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM completed_activities "
                    "ORDER BY date ASC, start_time ASC"
                )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_first_activity_date(self) -> Optional[str]:
        """MIN(date) over completed activities — the cheap history-start lookup, so
        callers don't fetch every row (all zone columns included) to read one date."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MIN(date) AS d FROM completed_activities")
            row = cursor.fetchone()
            return row["d"] if row else None

    def get_first_metric_date(self) -> Optional[str]:
        """MIN(date) over the metrics cache — same cheap lookup as
        get_first_activity_date, for the other half of pmc_history_start."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MIN(date) AS d FROM athlete_metrics_cache")
            row = cursor.fetchone()
            return row["d"] if row else None

    # --- Athlete Metrics Cache ---
    def save_metric_cache(
        self, date: str, rhr: Optional[int], hrv: Optional[int],
        sleep_score: Optional[int], stress: Optional[int],
        ctl: Optional[float] = None, atl: Optional[float] = None,
        tsb: Optional[float] = None
    ) -> None:
        """Caches daily athlete metrics in the database, updating on conflict.

        ctl/atl/tsb (the PMC triple) COALESCE-preserve: a metrics-only Garmin save (which
        passes them as None) never nulls out PMC values a prior recompute stored
        (DESIGN_pmc_fitness_fatigue.md §4)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO athlete_metrics_cache (
                    date, rhr, hrv, sleep_score, stress, ctl, atl, tsb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rhr=excluded.rhr,
                    hrv=excluded.hrv,
                    sleep_score=excluded.sleep_score,
                    stress=excluded.stress,
                    ctl=COALESCE(excluded.ctl, athlete_metrics_cache.ctl),
                    atl=COALESCE(excluded.atl, athlete_metrics_cache.atl),
                    tsb=COALESCE(excluded.tsb, athlete_metrics_cache.tsb)
            """, (date, rhr, hrv, sleep_score, stress, ctl, atl, tsb))
            conn.commit()

    def get_metrics_cache(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> List[AthleteMetric]:
        """Fetches cached metrics, optionally in a range."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_date and end_date:
                cursor.execute(
                    "SELECT * FROM athlete_metrics_cache "
                    "WHERE date >= ? AND date <= ? ORDER BY date ASC",
                    (start_date, end_date)
                )
            elif start_date:
                cursor.execute(
                    "SELECT * FROM athlete_metrics_cache WHERE date >= ? "
                    "ORDER BY date ASC", (start_date,)
                )
            else:
                cursor.execute("SELECT * FROM athlete_metrics_cache ORDER BY date ASC")
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    # --- Athlete Baselines ---
    def save_baseline(
        self, date: str, rhr_mean: float, rhr_std: float, hrv_mean: float,
        hrv_std: float, sleep_mean: float, sleep_std: float
    ) -> None:
        """Saves calculated athlete baseline values."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO athlete_baselines (
                    date, rhr_baseline_mean, rhr_baseline_std, hrv_baseline_mean,
                    hrv_baseline_std, sleep_baseline_mean, sleep_baseline_std
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rhr_baseline_mean=excluded.rhr_baseline_mean,
                    rhr_baseline_std=excluded.rhr_baseline_std,
                    hrv_baseline_mean=excluded.hrv_baseline_mean,
                    hrv_baseline_std=excluded.hrv_baseline_std,
                    sleep_baseline_mean=excluded.sleep_baseline_mean,
                    sleep_baseline_std=excluded.sleep_baseline_std
            """, (date, rhr_mean, rhr_std, hrv_mean, hrv_std, sleep_mean, sleep_std))
            conn.commit()

    def get_baseline(self, date: str) -> Optional[AthleteBaseline]:
        """Fetches baseline valid on or closest prior to the date."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM athlete_baselines WHERE date <= ? "
                "ORDER BY date DESC LIMIT 1", (date,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    # --- Sync watermark ---
    def get_sync_state(self, key: str = "garmin") -> Optional[Dict[str, Any]]:
        """Returns the {through_date, last_pull_utc, sync_token} state for a source,
        or None. sync_token is the opaque Calendar nextSyncToken (calendar_context row);
        through_date is the Garmin forward high-water mark."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key, through_date, last_pull_utc, sync_token "
                "FROM sync_state WHERE key = ?",
                (key,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def set_sync_state(
        self, through_date: Optional[str], last_pull_utc: str, key: str = "garmin",
        sync_token: Optional[str] = None
    ) -> None:
        """Upserts the watermark. through_date only ever advances (forward high-water
        mark); a backward backfill passes the existing value through unchanged.
        sync_token carries the Calendar nextSyncToken for the calendar_context row;
        rows that don't use it pass None. Each source owns a distinct key, so the
        unused columns simply stay NULL per row."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sync_state (key, through_date, last_pull_utc, sync_token)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    through_date=excluded.through_date,
                    last_pull_utc=excluded.last_pull_utc,
                    sync_token=excluded.sync_token
                """,
                (key, through_date, last_pull_utc, sync_token),
            )
            conn.commit()

    def get_metric_dates(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> List[str]:
        """Returns the sorted list of dates present in the metrics cache (optionally in a
        range). A row exists for every pulled day — even all-null ones — so this set is
        the record of what has been pulled, distinguishing 'pulled, empty' from 'never
        pulled' (see DESIGN_garmin_direct_pull.md §5)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if start_date and end_date:
                cursor.execute(
                    "SELECT date FROM athlete_metrics_cache "
                    "WHERE date >= ? AND date <= ? ORDER BY date ASC",
                    (start_date, end_date),
                )
            else:
                cursor.execute(
                    "SELECT date FROM athlete_metrics_cache ORDER BY date ASC"
                )
            return [row["date"] for row in cursor.fetchall()]
