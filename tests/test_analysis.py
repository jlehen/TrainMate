import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables
from trainmate.db import Database
import trainmate.db
import trainmate.coach

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_analysis.db")
test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.db = test_db

from trainmate.coach import coach_service


class TestWorkoutAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    @patch("trainmate.coach.openrouter_client")
    def test_date_resolution_with_preceding_goal(self, mock_client):
        # Earliest objective: 2026-07-01
        test_db.add_objective(
            title="Goal A", target_date="2026-07-01",
            sport_type="running", priority=1, status="active"
        )
        # Preceding objective: 2026-06-01
        test_db.add_objective(
            title="Goal Preceding", target_date="2026-06-01",
            sport_type="running", priority=1, status="active"
        )

        mock_client.complete.return_value = {
            "macrocycle_summary": "Analysis summary",
            "inferred_macrocycle": {"overall_focus": "aerobic base"},
            "inferred_mesocycles": [],
            "physiological_insights": [],
            "learning_updates": []
        }

        # Analyze workouts without explicit range -> should start on 2026-06-02 (day after Goal Preceding)
        result = coach_service.analyze_workouts(until_date_str="2026-07-01")
        self.assertIsNotNone(result)

        # Inspect the start date passed to complete call
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-06-01", summaries) # Monday of that week is 2026-06-01 (Tuesday 2026-06-02 is in it)

    @patch("trainmate.coach.openrouter_client")
    def test_date_resolution_relative_days_and_weeks(self, mock_client):
        mock_client.complete.return_value = {
            "macrocycle_summary": "Analysis summary"
        }

        # Last 10 days relative to 2026-06-15
        coach_service.analyze_workouts(until_date_str="2026-06-15", days=10)
        # Start date should be 2026-06-06. The Monday of that week is 2026-06-01.
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-06-01", summaries)

        # Last 4 weeks relative to 2026-06-15
        coach_service.analyze_workouts(until_date_str="2026-06-15", weeks=4)
        # Start date should be 2026-05-19. The Monday of that week is 2026-05-18.
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-05-18", summaries)

    @patch("trainmate.coach.openrouter_client")
    def test_weekly_aggregation_logic(self, mock_client):
        # Setup completed activities in different weeks
        # Week commencing 2026-06-01
        test_db.save_completed_activity(
            activity_id="act_1", date="2026-06-03", start_time="08:00:00",
            activity_name="Base Ride", activity_type="road_biking",
            duration_sec=7200.0, distance_km=50.0, elevation_gain_m=300.0,
            avg_hr=130, max_hr=150, rpe=5, tss=90.0,
            zone1_sec=3600, zone2_sec=3600
        )
        # Highlight workout in the same week
        test_db.save_completed_activity(
            activity_id="act_2", date="2026-06-05", start_time="09:00:00",
            activity_name="FTP Race Test", activity_type="running",
            duration_sec=3600.0, distance_km=12.0, elevation_gain_m=50.0,
            avg_hr=165, max_hr=180, rpe=9, tss=130.0,
            zone4_sec=1800, zone5_sec=1800
        )
        # Metric cache for that week
        test_db.save_metric_cache(
            date="2026-06-03", rhr=50, hrv=75, sleep_score=85, stress=20,
            acute_workload=300.0, chronic_workload=280.0, acwr=1.07
        )

        mock_client.complete.return_value = {
            "macrocycle_summary": "Simulated aggregation summary",
            "learning_updates": [{"op": "add", "text": "Athlete responds well to FTP tests."}]
        }

        # Analyze the week
        result = coach_service.analyze_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07"
        )
        self.assertEqual(result["macrocycle_summary"], "Simulated aggregation summary")

        # Verify learnings updated in memory
        saved_learnings = test_db.get_learnings()
        self.assertEqual(len(saved_learnings), 1)
        self.assertEqual(saved_learnings[0]["text"], "Athlete responds well to FTP tests.")

        # Verify mock complete call payloads
        user_payload = mock_client.complete.call_args[0][1]
        self.assertIn("total_duration_hours\": 3.0", user_payload)
        self.assertIn("total_tss\": 220.0", user_payload)
        self.assertIn("FTP Race Test", user_payload)
        self.assertIn("avg_hrv\": 75.0", user_payload)


if __name__ == "__main__":
    unittest.main()
