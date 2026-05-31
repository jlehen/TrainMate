import json
from datetime import datetime, timedelta
import math
from google.oauth2 import service_account
from googleapiclient.discovery import build
from trainmate.config import config
from trainmate.db import db

class GarminSheetsReader:
    def __init__(self):
        self.scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
        self.creds = service_account.Credentials.from_service_account_file(
            config.service_account_file, scopes=self.scopes)
        self.service = build('sheets', 'v4', credentials=self.creds)
        self.spreadsheet_id = config.google_sheet_id

    def sync_data(self):
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
        activity_rows = activity_values[1:] if activity_values and len(activity_values) > 1 else []
        activity_headers = [h.strip() for h in activity_values[0]] if activity_values else []

        print(f"Loaded {len(daily_rows)} daily metric rows and {len(activity_rows)} activities.")

        # Parse Daily Metrics
        # Headers: Date, Resting HR (RHR), Overnight HRV Average, Sleep Score, Body Battery Start, Body Battery End, Steps, Calories Burned, Average Stress
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
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue # Skip invalid date rows
                
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
        # Headers: Activity ID, Date, Start Time, Activity Name, Type, Duration (sec), Duration (Formatted), Distance (km), Elevation Gain (m), Avg HR, Max HR
        daily_activity_load = {} # Map date string -> total training load
        
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
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
                
            duration_str = row_dict.get('Duration (sec)', '0').replace(',', '')
            duration_sec = self._safe_float(duration_str)
            avg_hr = self._safe_int(row_dict.get('Avg HR'))
            
            # Default average heart rate if missing or zero
            if not avg_hr or avg_hr <= 0:
                avg_hr = 120
                
            # Workload calculation: Duration in hours * Avg HR
            workload = (duration_sec / 3600.0) * avg_hr
            
            daily_activity_load[date_str] = daily_activity_load.get(date_str, 0.0) + workload

        # Sort daily metrics chronologically
        parsed_daily.sort(key=lambda x: x['date'])
        
        # Save raw daily metrics and compute baselines
        for idx, day in enumerate(parsed_daily):
            date_str = day['date']
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
            chronic_load = total_28_day_load / 4.0 # Average weekly load
            
            acwr = 1.0
            if chronic_load > 0.0:
                acwr = acute_load / chronic_load
            elif acute_load > 0.0:
                acwr = 2.0 # Elevated ratio if acute exists but chronic is 0

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
                    if prev_day['rhr']: rhr_values.append(prev_day['rhr'])
                    if prev_day['hrv']: hrv_values.append(prev_day['hrv'])
                    if prev_day['sleep_score']: sleep_values.append(prev_day['sleep_score'])
            
            # If we have enough data (at least 7 points in the 28-day window to establish a baseline)
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

        print("Google Sheets synchronization completed successfully.")

    def _get_sheet_values(self, sheet_range):
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=sheet_range).execute()
            return result.get('values', [])
        except Exception as e:
            print(f"Error fetching sheet range {sheet_range}: {e}")
            return []

    def _safe_int(self, val):
        if val is None:
            return None
        try:
            return int(float(str(val).strip()))
        except ValueError:
            return None

    def _safe_float(self, val):
        if val is None:
            return 0.0
        try:
            return float(str(val).strip())
        except ValueError:
            return 0.0

    def _mean_std(self, values):
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
