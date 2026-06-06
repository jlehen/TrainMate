import unittest
from unittest.mock import patch, MagicMock
import os
from datetime import datetime, timedelta, timezone

# Define test database path
TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_adaptation.db")

# Override db singleton inside trainmate before anything else imports it
from trainmate.db import Database
import trainmate.db

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db

import trainmate.coach
import trainmate.google_sheets
trainmate.coach.db = test_db
trainmate.google_sheets.db = test_db

from trainmate.coach import coach_service
from trainmate.google_sheets import sheets_reader

class TestAdaptation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.db = test_db
        trainmate.google_sheets.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        # Clear tables
        with test_db._get_connection() as conn:
            conn.execute("DELETE FROM objectives")
            conn.execute("DELETE FROM lifeevents")
            conn.execute("DELETE FROM workouts")
            conn.execute("DELETE FROM completed_activities")
            conn.execute("DELETE FROM athlete_metrics_cache")
            conn.execute("DELETE FROM athlete_baselines")
            conn.execute("DELETE FROM coach_memory")
            conn.execute("DELETE FROM macrocycles")
            conn.execute("DELETE FROM mesocycles")
            conn.commit()

    def test_db_completed_activities_crud(self):
        # 1. Save completed activity
        test_db.save_completed_activity(
            activity_id="act_123",
            date="2026-06-03",
            start_time="2026-06-03 08:00:00",
            activity_name="Morning Run",
            activity_type="running",
            duration_sec=3600.0,
            distance_km=10.0,
            elevation_gain_m=100.0,
            avg_hr=150,
            max_hr=170,
            rpe=7,
            tss=60.0
        )

        # 2. Retrieve completed activities
        activities = test_db.get_completed_activities(
            start_date="2026-06-01", end_date="2026-06-05"
        )
        self.assertEqual(len(activities), 1)
        act = activities[0]
        self.assertEqual(act['activity_id'], "act_123")
        self.assertEqual(act['activity_name'], "Morning Run")
        self.assertEqual(act['rpe'], 7)
        self.assertEqual(act['tss'], 60.0)

        # 3. Conflict / Update scenario
        test_db.save_completed_activity(
            activity_id="act_123",
            date="2026-06-03",
            start_time="2026-06-03 08:00:00",
            activity_name="Morning Run Updated",
            activity_type="running",
            duration_sec=3600.0,
            distance_km=10.0,
            elevation_gain_m=100.0,
            avg_hr=150,
            max_hr=170,
            rpe=8,
            tss=65.0
        )

        activities = test_db.get_completed_activities()
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]['activity_name'], "Morning Run Updated")
        self.assertEqual(activities[0]['rpe'], 8)
        self.assertEqual(activities[0]['tss'], 65.0)

    def test_rpe_tss_estimation(self):
        # Setup config user profile
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.google_sheets.config.data, {"user_profile": test_profile}):
            # Test running with HR
            # Average HR = 132 (Intensity ratio = 132 / 165 = 0.8)
            # Duration = 1 hour (3600 sec)
            rpe, tss = sheets_reader._estimate_activity_metrics("running", 3600.0, 132)
            # Expected RPE = round(0.8 * 8) = 6
            # Expected TSS = 1.0 * (0.8^2) * 100 = 64.0
            self.assertEqual(rpe, 6)
            self.assertAlmostEqual(tss, 64.0)

            # Test strength without HR
            rpe_st, tss_st = sheets_reader._estimate_activity_metrics(
                "strength_training", 1800.0, None
            )
            self.assertEqual(rpe_st, 5)
            self.assertAlmostEqual(tss_st, 22.5)  # 0.5 hours * 45.0 = 22.5

            # Test yoga with HR
            # Yoga 1 hour, Average HR = 99 (Intensity ratio = 99 / 165 = 0.6)
            # Expected RPE = round(0.6 * 8) = 5
            # Expected TSS = 1.0 * (0.6^2) * 100 = 36.0
            rpe_yo, tss_yo = sheets_reader._estimate_activity_metrics("yoga", 3600.0, 99)
            self.assertEqual(rpe_yo, 5)
            self.assertAlmostEqual(tss_yo, 36.0)

    def test_planned_workout_workload_fields(self):
        w_id = test_db.save_workout(
            date="2026-06-03",
            sport_type="running",
            title="Tempo Run",
            description="30 mins at LTHR",
            duration_minutes=45,
            rpe=7,
            tss=50
        )
        
        workout = test_db.get_workout_by_id(w_id)
        self.assertIsNotNone(workout)
        self.assertEqual(workout['duration_minutes'], 45)
        self.assertEqual(workout['rpe'], 7)
        self.assertEqual(workout['tss'], 50)

    @patch('trainmate.coach.openrouter_client')
    def test_adaptation_matching_and_discrepancies(self, mock_client):
        # Set up a fake profile in config
        test_profile = {"lthr": 165, "max_hr": 185}
        
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "metrics_history_days": 3,
            "low_load_threshold": 10.0
        }):
            # Mock LLM adaptation decision
            mock_decision = {
                "change_needed": True,
                "reason": "Fatigue detected, RHR is elevated and HRV is suppressed.",
                "adapted_workouts": [
                    {
                        "date": "2026-06-03",
                        "sport_type": "rest",
                        "title": "Adapted Rest Day",
                        "description": "Swapped tempo run to rest.",
                        "duration_minutes": 0,
                        "rpe": 0,
                        "tss": 0.0
                    }
                ]
            }
            mock_client.complete.return_value = mock_decision

            # Seed metrics cache for past 3 days (June 1st, 2nd, 3rd)
            test_db.save_metric_cache("2026-06-01", 50, 60, 80, 20, 10.0, 8.0, 1.2)
            test_db.save_metric_cache("2026-06-02", 52, 55, 75, 25, 12.0, 8.0, 1.5)
            # Suppressed HRV & elevated RHR on June 3rd
            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)

            # Seed baseline
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            # June 1: running (On track)
            test_db.save_workout(
                "2026-06-01", "running", "Easy Run", "30 mins",
                duration_minutes=30, rpe=4, tss=20
            )
            # June 2: road_biking (Completed but duration discrepant: 60m planned, 30m actual)
            test_db.save_workout(
                "2026-06-02", "road_biking", "Tempo Ride", "60 mins",
                duration_minutes=60, rpe=6, tss=40
            )
            # June 3: running (Missed completely)
            test_db.save_workout(
                "2026-06-03", "running", "Interval Session", "45 mins",
                duration_minutes=45, rpe=8, tss=60
            )

            # Seed completed activities
            # June 1: running (On track)
            test_db.save_completed_activity(
                "act_1", "2026-06-01", "2026-06-01 08:00:00", "Easy Run",
                "running", 1800.0, 5.0, 50.0, 132, 150, 4, 20.0
            )
            # June 2: cycling (Short ride, duration discrepancy)
            test_db.save_completed_activity(
                "act_2", "2026-06-02", "2026-06-02 08:00:00", "Short Cycling",
                "cycling", 1800.0, 12.0, 100.0, 132, 150, 4, 20.0
            )

            # Call coach adapt
            reason, proposed = coach_service.adapt("2026-06-03")

            # Check that OpenRouter was called
            self.assertTrue(mock_client.complete.called)
            self.assertEqual(reason, "Fatigue detected, RHR is elevated and HRV is suppressed.")
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]['title'], "Adapted Rest Day")
            self.assertEqual(proposed[0]['sport_type'], "rest")

            # Ensure prompt contained discrepancies
            prompt_user_content = mock_client.complete.call_args[0][1]
            self.assertIn(
                "Complete Miss! Missed planned workout 'Interval Session'",
                prompt_user_content
            )
            self.assertIn("duration mismatch", prompt_user_content)

    def test_analyze_adherence_direct(self):
        from trainmate.adherence import analyze_adherence
        from datetime import date

        planned = [
            {
                "date": "2026-06-01",
                "sport_type": "running",
                "title": "Run",
                "duration_minutes": 30,
                "rpe": 5,
                "tss": 25,
            },
            {
                "date": "2026-06-02",
                "sport_type": "rest",
                "title": "Rest Day",
                "duration_minutes": 0,
                "rpe": 0,
                "tss": 0,
            },
            {
                "date": "2026-06-03",
                "sport_type": "road_biking",
                "title": "Ride",
                "duration_minutes": 60,
                "rpe": 6,
                "tss": 40,
            },
        ]

        completed = [
            # June 1: workload mismatch
            # planned load: 25 + 5 * 0.5 = 27.5
            # actual load: 60 + 8 * 0.5 = 64
            {
                "date": "2026-06-01",
                "activity_id": "act1",
                "activity_name": "Hard Run",
                "activity_type": "running",
                "duration_sec": 1800,
                "rpe": 8,
                "tss": 60.0,
            },
            # June 2: rest day violation (workload = 15.0 > 10.0)
            {
                "date": "2026-06-02",
                "activity_id": "act2",
                "activity_name": "Lawn Mowing",
                "activity_type": "walking",
                "duration_sec": 3600,
                "rpe": 5,
                "tss": 10.0,
            },
            # June 4: unplanned activity (workload = 30.0 > 10.0)
            {
                "date": "2026-06-04",
                "activity_id": "act4",
                "activity_name": "Extra Run",
                "activity_type": "running",
                "duration_sec": 1800,
                "rpe": 6,
                "tss": 27.0,
            },
        ]

        start_date = date(2026, 6, 1)
        discrepancies, matching = analyze_adherence(
            planned_workouts=planned,
            completed_activities=completed,
            start_date_obj=start_date,
            history_days=4,
            low_load_threshold=10.0
        )

        # Check discrepancies
        self.assertEqual(len(discrepancies), 4)

        # 1. June 1 Workload mismatch
        self.assertTrue(
            any("workload mismatch" in d for d in discrepancies)
        )
        # 2. June 2 Rest day violation
        self.assertTrue(
            any("Rest Day Violation! Performed 'Lawn Mowing'" in d for d in discrepancies)
        )
        # 3. June 3 Complete miss
        self.assertTrue(
            any("Complete Miss! Missed planned workout 'Ride'" in d for d in discrepancies)
        )
        # 4. June 4 Unplanned activity
        self.assertTrue(
            any("Unplanned Activity! Performed 'Extra Run'" in d for d in discrepancies)
        )

if __name__ == '__main__':
    unittest.main()

