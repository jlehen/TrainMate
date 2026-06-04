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
        self.assertIn(f"Warning: Garmin metrics for the current date ({today_str}) are missing.", output)
