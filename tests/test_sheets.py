import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import io
import os

# Define test database path
TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_sheets.db")

from trainmate.db import Database
import trainmate.db
import trainmate.google_sheets

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.google_sheets.db = test_db

from trainmate.google_sheets import sheets_reader

class TestGarminSheetsReader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.google_sheets.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        with test_db._get_connection() as conn:
            conn.execute("DELETE FROM athlete_metrics_cache")
            conn.execute("DELETE FROM athlete_baselines")
            conn.execute("DELETE FROM completed_activities")
            conn.commit()

    @patch('trainmate.google_sheets.sheets_reader._get_sheet_values')
    def test_sync_data_today_present(self, mock_get_values):
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Mock daily metrics and activities
        mock_get_values.side_effect = [
            [
                ["Date", "Resting HR (RHR)", "Overnight HRV Average",
                 "Sleep Score", "Average Stress"],
                [today_str, "50", "70", "80", "20"]
            ],
            [
                ["Activity ID", "Date", "Start Time", "Activity Name", "Type",
                 "Duration (sec)"]
            ]
        ]
        
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            sheets_reader.sync_data()
            
        output = stdout.getvalue()
        self.assertNotIn("Warning: Garmin metrics for the current date", output)

    @patch('trainmate.google_sheets.sheets_reader._get_sheet_values')
    def test_sync_data_today_missing(self, mock_get_values):
        # Mock daily metrics and activities without today's date
        mock_get_values.side_effect = [
            [
                ["Date", "Resting HR (RHR)", "Overnight HRV Average",
                 "Sleep Score", "Average Stress"],
                ["2026-06-03", "50", "70", "80", "20"]
            ],
            [
                ["Activity ID", "Date", "Start Time", "Activity Name", "Type",
                 "Duration (sec)"]
            ]
        ]
        
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            sheets_reader.sync_data()
            
        output = stdout.getvalue()
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIn(
            f"Warning: Garmin metrics for the current date ({today_str}) are missing.",
            output
        )

    @patch('trainmate.google_sheets.sheets_reader._get_sheet_values')
    def test_sync_data_rpe_tss_from_sheet(self, mock_get_values):
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Mock daily metrics and activities, providing RPE and TSS directly in activities
        mock_get_values.side_effect = [
            [
                ["Date", "Resting HR (RHR)", "Overnight HRV Average",
                 "Sleep Score", "Average Stress"],
                [today_str, "50", "70", "80", "20"]
            ],
            [
                ["Activity ID", "Date", "Start Time", "Activity Name", "Type",
                 "Duration (sec)", "RPE", "TSS"],
                ["act_sheet_1", today_str, "08:00:00", "Morning Run", "running",
                 "3600", "8", "75.5"]
            ]
        ]

        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            sheets_reader.sync_data()

        # Check that the activity was saved with RPE and TSS from the sheet
        activities = test_db.get_completed_activities()
        # Find the activity we just inserted
        act = next((a for a in activities if a["activity_id"] == "act_sheet_1"), None)
        self.assertIsNotNone(act)
        self.assertEqual(act["rpe"], 8)
        self.assertEqual(act["tss"], 75.5)

    @patch('trainmate.google_sheets.sheets_reader._get_sheet_values')
    def test_sync_data_rpe_only_from_sheet(self, mock_get_values):
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_get_values.side_effect = [
            [
                ["Date", "Resting HR (RHR)", "Overnight HRV Average",
                 "Sleep Score", "Average Stress"],
                [today_str, "50", "70", "80", "20"]
            ],
            [
                ["Activity ID", "Date", "Start Time", "Activity Name", "Type",
                 "Duration (sec)", "RPE", "TSS"],
                ["act_sheet_2", today_str, "08:00:00", "Morning Run", "running",
                 "3600", "9", ""]
            ]
        ]

        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            sheets_reader.sync_data()

        activities = test_db.get_completed_activities()
        act = next((a for a in activities if a["activity_id"] == "act_sheet_2"), None)
        self.assertIsNotNone(act)
        # RPE should be 9 (provided)
        self.assertEqual(act["rpe"], 9)
        # TSS should be estimated (duration=1h, no HR so fallback estimated)
        # Let's verify what estimated TSS is for running 3600s with no HR:
        # avg_hr is None/0. lthr is 165. For sport running (not yoga/strength/rest),
        # fallback is duration_hours * 30.0 = 1.0 * 30.0 = 30.0.
        self.assertEqual(act["tss"], 30.0)
