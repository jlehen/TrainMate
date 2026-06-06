import io
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables

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
        clear_all_tables(test_db)

    @patch("trainmate.google_sheets.sheets_reader._get_sheet_values")
    def test_sync_data_today_present(self, mock_get_values):
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_get_values.side_effect = [
            [
                ["Date", "Resting HR (RHR)", "Overnight HRV Average",
                 "Sleep Score", "Average Stress"],
                [today_str, "50", "70", "80", "20"],
            ],
            [["Activity ID", "Date", "Start Time", "Activity Name", "Type", "Duration (sec)"]],
        ]

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            sheets_reader.sync_data()
        self.assertNotIn("Warning: Garmin metrics for the current date", stdout.getvalue())

    @patch("trainmate.google_sheets.sheets_reader._get_sheet_values")
    def test_sync_data_today_missing(self, mock_get_values):
        mock_get_values.side_effect = [
            [
                ["Date", "Resting HR (RHR)", "Overnight HRV Average",
                 "Sleep Score", "Average Stress"],
                ["2026-06-03", "50", "70", "80", "20"],
            ],
            [["Activity ID", "Date", "Start Time", "Activity Name", "Type", "Duration (sec)"]],
        ]

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            sheets_reader.sync_data()
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIn(
            f"Warning: Garmin metrics for the current date ({today_str}) are missing.",
            stdout.getvalue(),
        )

    @patch("trainmate.google_sheets.sheets_reader._get_sheet_values")
    def test_sync_data_rpe_tss_from_sheet(self, mock_get_values):
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_get_values.side_effect = [
            [
                ["Date", "Resting HR (RHR)", "Overnight HRV Average",
                 "Sleep Score", "Average Stress"],
                [today_str, "50", "70", "80", "20"],
            ],
            [
                ["Activity ID", "Date", "Start Time", "Activity Name", "Type",
                 "Duration (sec)", "RPE", "TSS"],
                ["act_sheet_1", today_str, "08:00:00", "Morning Run", "running",
                 "3600", "8", "75.5"],
            ],
        ]

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            sheets_reader.sync_data()

        activities = test_db.get_completed_activities()
        act = next((a for a in activities if a["activity_id"] == "act_sheet_1"), None)
        self.assertIsNotNone(act)
        self.assertEqual(act["rpe"], 8)
        self.assertEqual(act["tss"], 75.5)

    @patch("trainmate.google_sheets.sheets_reader._get_sheet_values")
    def test_sync_data_rpe_only_from_sheet(self, mock_get_values):
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_get_values.side_effect = [
            [
                ["Date", "Resting HR (RHR)", "Overnight HRV Average",
                 "Sleep Score", "Average Stress"],
                [today_str, "50", "70", "80", "20"],
            ],
            [
                ["Activity ID", "Date", "Start Time", "Activity Name", "Type",
                 "Duration (sec)", "RPE", "TSS"],
                ["act_sheet_2", today_str, "08:00:00", "Morning Run", "running",
                 "3600", "9", ""],
            ],
        ]

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            sheets_reader.sync_data()

        activities = test_db.get_completed_activities()
        act = next((a for a in activities if a["activity_id"] == "act_sheet_2"), None)
        self.assertIsNotNone(act)
        self.assertEqual(act["rpe"], 9)
        # TSS estimated: 1h running with no HR → duration_hours * 30.0 = 30.0
        self.assertEqual(act["tss"], 30.0)

    def test_rpe_tss_estimation(self):
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.google_sheets.config.data, {"user_profile": test_profile}):
            # running 1h at 80% LTHR: RPE=round(0.8*8)=6, TSS=1*(0.8^2)*100=64
            rpe, tss = sheets_reader._estimate_activity_metrics("running", 3600.0, 132)
            self.assertEqual(rpe, 6)
            self.assertAlmostEqual(tss, 64.0)

            # strength_training 30min no HR: fallback fixed RPE=5, TSS=0.5*45=22.5
            rpe_st, tss_st = sheets_reader._estimate_activity_metrics(
                "strength_training", 1800.0, None
            )
            self.assertEqual(rpe_st, 5)
            self.assertAlmostEqual(tss_st, 22.5)

            # yoga 1h at 60% LTHR: RPE=round(0.6*8)=5, TSS=1*(0.6^2)*100=36
            rpe_yo, tss_yo = sheets_reader._estimate_activity_metrics("yoga", 3600.0, 99)
            self.assertEqual(rpe_yo, 5)
            self.assertAlmostEqual(tss_yo, 36.0)


if __name__ == "__main__":
    unittest.main()
