import json
from datetime import datetime, timedelta, timezone
import math
from typing import Any, List, Optional, Tuple, Dict
from google.oauth2 import service_account
from googleapiclient.discovery import build
from trainmate.config import config
from trainmate.db import db

class GarminSheetsReader:
    """Reads Garmin daily metrics and activities data from Google Sheets."""

    def __init__(self) -> None:
        """Initializes sheets API service using service account credentials."""
        self.scopes: List[str] = ['https://www.googleapis.com/auth/spreadsheets.readonly']
        self.creds: service_account.Credentials = (
            service_account.Credentials.from_service_account_file(
                config.service_account_file, scopes=self.scopes
            )
        )
        self.service: Any = build('sheets', 'v4', credentials=self.creds)
        self.spreadsheet_id: Optional[str] = config.google_sheet_id

    def sync_data(self) -> None:
        """Fetches data from Google Sheets, caches it in DB, and computes baselines."""
        print("Fetching Garmin Daily Metrics and Activities from Google Sheets...")
        
        # 1. Fetch Daily Metrics
        daily_values = self._get_sheet_values("Daily Metrics!A1:Z5000")
        if not daily_values or len(daily_values) < 2:
            raise ValueError("No daily metrics found in Google Sheet.")
        
        daily_headers = [h.strip() for h in daily_values[0]]
        daily_rows = daily_values[1:]
        
        # 2. Fetch Activities
        activity_values = self._get_sheet_values("Activities!A1:Z5000")
        activity_rows = (
            activity_values[1:] if activity_values and len(activity_values) > 1 else []
        )
        activity_headers = (
            [h.strip() for h in activity_values[0]] if activity_values else []
        )

        print(f"Loaded {len(daily_rows)} daily metric rows and {len(activity_rows)} activities.")

        # Parse Daily Metrics
        # Headers: Date, Resting HR (RHR), Overnight HRV Average, Sleep Score,
        # Body Battery Start, Body Battery End, Steps, Calories Burned, Average Stress
        parsed_daily = []
        for row in daily_rows:
            if not row or not row[0]:
                continue
            
            # Map columns safely
            row_dict = {}
            for i, h in enumerate(daily_headers):
                if i < len(row):
                    row_dict[h] = row[i]
                else:
                    row_dict[h] = None
                    
            date_str = row_dict.get('Date')
            try:
                # Validate date format (YYYY-MM-DD)
                datetime.strptime(str(date_str), "%Y-%m-%d")
            except ValueError:
                continue  # Skip invalid date rows
                
            rhr = self._safe_int(row_dict.get('Resting HR (RHR)'))
            hrv = self._safe_int(row_dict.get('Overnight HRV Average'))
            sleep = self._safe_int(row_dict.get('Sleep Score'))
            stress = self._safe_int(row_dict.get('Average Stress'))
            
            parsed_daily.append({
                'date': date_str,
                'rhr': rhr,
                'hrv': hrv,
                'sleep_score': sleep,
                'stress': stress
            })

        # Parse Activities to compute training workload
        # Headers: Activity ID, Date, Start Time, Activity Name, Type,
        # Duration (sec), Duration (Formatted), Distance (km), Elevation Gain (m),
        # Avg HR, Max HR, RPE, TSS
        daily_activity_load: Dict[str, float] = {}  # Map date string -> total training load
        
        for row in activity_rows:
            if not row or not row[1]:
                continue
            row_dict = {}
            for i, h in enumerate(activity_headers):
                if i < len(row):
                    row_dict[h] = row[i]
                else:
                    row_dict[h] = None
                    
            date_str = row_dict.get('Date')
            try:
                datetime.strptime(str(date_str), "%Y-%m-%d")
            except ValueError:
                continue
                
            activity_id = str(row_dict.get('Activity ID'))
            start_time = row_dict.get('Start Time')
            activity_name = row_dict.get('Activity Name')
            activity_type = str(row_dict.get('Type'))
            
            duration_str = str(row_dict.get('Duration (sec)', '0')).replace(',', '')
            duration_sec = self._safe_float(duration_str)
            
            distance_str = str(row_dict.get('Distance (km)', '0')).replace(',', '')
            distance_km = self._safe_float(distance_str)
            
            elevation_str = str(row_dict.get('Elevation Gain (m)', '0')).replace(',', '')
            elevation_gain_m = self._safe_float(elevation_str)
            
            avg_hr = self._safe_int(row_dict.get('Avg HR'))
            max_hr = self._safe_int(row_dict.get('Max HR'))

            bike_avg_watts = self._safe_int(row_dict.get('Bike Avg Watts'))
            zone1_sec = self._safe_int(row_dict.get('Zone 1 Sec'))
            zone2_sec = self._safe_int(row_dict.get('Zone 2 Sec'))
            zone3_sec = self._safe_int(row_dict.get('Zone 3 Sec'))
            zone4_sec = self._safe_int(row_dict.get('Zone 4 Sec'))
            zone5_sec = self._safe_int(row_dict.get('Zone 5 Sec'))

            # Read RPE and TSS from sheet if present, fallback to estimation if missing/invalid
            rpe = self._safe_int(row_dict.get('RPE'))
            tss = self._safe_float_optional(row_dict.get('TSS'))

            if rpe is None or tss is None:
                est_rpe, est_tss = self._estimate_activity_metrics(
                    activity_type, duration_sec, avg_hr
                )
                if rpe is None:
                    rpe = est_rpe
                if tss is None:
                    tss = est_tss

            # Save completed activity to DB
            db.save_completed_activity(
                activity_id=activity_id,
                date=str(date_str),
                start_time=start_time,
                activity_name=activity_name,
                activity_type=activity_type,
                duration_sec=duration_sec,
                distance_km=distance_km,
                elevation_gain_m=elevation_gain_m,
                avg_hr=avg_hr,
                max_hr=max_hr,
                rpe=rpe,
                tss=tss,
                bike_avg_watts=bike_avg_watts,
                zone1_sec=zone1_sec,
                zone2_sec=zone2_sec,
                zone3_sec=zone3_sec,
                zone4_sec=zone4_sec,
                zone5_sec=zone5_sec
            )
            
            # Workload calculation: TSS + RPE * Duration (hours)
            workload = tss + rpe * (duration_sec / 3600.0)
            
            daily_activity_load[str(date_str)] = (
                daily_activity_load.get(str(date_str), 0.0) + workload
            )

        # Sort daily metrics chronologically
        parsed_daily.sort(key=lambda x: str(x['date']))
        
        # Save raw daily metrics and compute baselines
        for idx, day in enumerate(parsed_daily):
            date_str = str(day['date'])
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            
            # Calculate workloads for this date
            acute_load = 0.0
            chronic_load = 0.0
            
            # 7-day acute workload window: [date - 6, date]
            for d in range(7):
                check_date = (date_obj - timedelta(days=d)).strftime("%Y-%m-%d")
                acute_load += daily_activity_load.get(check_date, 0.0)
                
            # 28-day chronic workload window: [date - 27, date]
            total_28_day_load = 0.0
            for d in range(28):
                check_date = (date_obj - timedelta(days=d)).strftime("%Y-%m-%d")
                total_28_day_load += daily_activity_load.get(check_date, 0.0)
            chronic_load = total_28_day_load / 4.0  # Average weekly load
            
            acwr = 1.0
            if chronic_load > 0.0:
                acwr = acute_load / chronic_load
            elif acute_load > 0.0:
                acwr = 2.0  # Elevated ratio if acute exists but chronic is 0
 
            # Save metrics cache
            db.save_metric_cache(
                date=date_str,
                rhr=day['rhr'],
                hrv=day['hrv'],
                sleep_score=day['sleep_score'],
                stress=day['stress'],
                acute_workload=acute_load,
                chronic_workload=chronic_load,
                acwr=acwr
            )

            # Compute RHR, HRV, Sleep baseline over past 28 days: [date - 28, date - 1]
            rhr_values = []
            hrv_values = []
            sleep_values = []
            
            for d in range(1, 29):
                prev_date = (date_obj - timedelta(days=d)).strftime("%Y-%m-%d")
                # Look up in already parsed list (safer since sorted chronologically)
                prev_day = next((x for x in parsed_daily if x['date'] == prev_date), None)
                if prev_day:
                    if prev_day['rhr'] is not None:
                        rhr_values.append(prev_day['rhr'])
                    if prev_day['hrv'] is not None:
                        hrv_values.append(prev_day['hrv'])
                    if prev_day['sleep_score'] is not None:
                        sleep_values.append(prev_day['sleep_score'])
            
            # If we have enough data (at least 7 points in 28-day window)
            if len(rhr_values) >= 7 or len(hrv_values) >= 7 or len(sleep_values) >= 7:
                rhr_mean, rhr_std = self._mean_std(rhr_values)
                hrv_mean, hrv_std = self._mean_std(hrv_values)
                sleep_mean, sleep_std = self._mean_std(sleep_values)
                
                db.save_baseline(
                    date=date_str,
                    rhr_mean=rhr_mean,
                    rhr_std=rhr_std,
                    hrv_mean=hrv_mean,
                    hrv_std=hrv_std,
                    sleep_mean=sleep_mean,
                    sleep_std=sleep_std
                )

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_metrics = next((day for day in parsed_daily if day['date'] == today_str), None)
        if not today_metrics or (today_metrics['rhr'] is None and
                                 today_metrics['hrv'] is None and
                                 today_metrics['sleep_score'] is None and
                                 today_metrics['stress'] is None):
            print(f"Warning: Garmin metrics for the current date ({today_str}) are missing.")

        print("Google Sheets synchronization completed successfully.")

    def _get_sheet_values(self, sheet_range: str) -> List[List[Any]]:
        """Queries Google Sheets API for values in the given range.

        Args:
            sheet_range: The sheet range (e.g. 'Daily Metrics!A1:Z5000').

        Returns:
            A list of lists containing sheet cell values.
        """
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=sheet_range).execute()
            return result.get('values', [])  # type: ignore
        except Exception as e:
            print(f"Error fetching sheet range {sheet_range}: {e}")
            return []

    def _estimate_activity_metrics(
        self, sport_type: str, duration_sec: float, avg_hr: Optional[int]
    ) -> Tuple[int, float]:
        """Estimates RPE and TSS for a completed activity based on heart rate.

        Args:
            sport_type: Type of sport (e.g. running, road_biking).
            duration_sec: Duration of activity in seconds.
            avg_hr: Average heart rate during the activity.

        Returns:
            A tuple of (estimated_rpe, estimated_tss).
        """
        duration_hours = duration_sec / 3600.0
        profile = config.user_profile or {}
        lthr = profile.get("lthr")
        if not lthr:
            max_hr = profile.get("max_hr")
            lthr = int(round(max_hr * 0.85)) if max_hr else 165
        
        sport = (sport_type or "").lower().replace("_", " ")
        
        if not avg_hr or avg_hr <= 0:
            if "yoga" in sport:
                return 2, duration_hours * 15.0
            elif "strength" in sport:
                return 5, duration_hours * 45.0
            elif "rest" in sport:
                return 0, 0.0
            else:
                return 3, duration_hours * 30.0
                
        intensity_ratio = avg_hr / lthr
        
        # Estimate RPE on 2-10 scale
        rpe = int(round(intensity_ratio * 8.0))
        rpe = max(2, min(rpe, 10))
        
        # Estimate TSS: duration * intensity_ratio^2 * 100
        tss = duration_hours * (intensity_ratio ** 2) * 100.0
        return rpe, tss

    def _safe_int(self, val: Any) -> Optional[int]:
        """Safely parses a cell value to integer."""
        if val is None:
            return None
        try:
            return int(float(str(val).strip()))
        except ValueError:
            return None

    def _safe_float(self, val: Any) -> float:
        """Safely parses a cell value to float."""
        if val is None:
            return 0.0
        try:
            return float(str(val).strip())
        except ValueError:
            return 0.0

    def _safe_float_optional(self, val: Any) -> Optional[float]:
        """Safely parses a cell value to float, returning None if empty or invalid."""
        if val is None:
            return None
        try:
            return float(str(val).strip().replace(',', ''))
        except ValueError:
            return None

    def _mean_std(self, values: List[float]) -> Tuple[float, float]:
        """Calculates the mean and sample standard deviation of a list of floats.

        Args:
            values: A list of numeric values.

        Returns:
            A tuple of (mean, standard_deviation).
        """
        if not values:
            return 0.0, 0.0
        n = len(values)
        mean = sum(values) / n
        if n < 2:
            return mean, 0.0
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        std_dev = math.sqrt(variance)
        return mean, std_dev

# Singleton instance
sheets_reader = GarminSheetsReader()
