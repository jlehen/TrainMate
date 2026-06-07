import os
import unittest
from datetime import date
from unittest.mock import patch

from tests.helpers import clear_all_tables
from trainmate.adherence import analyze_adherence

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_adaptation.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach
import trainmate.google_sheets

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.db = test_db
trainmate.google_sheets.db = test_db

from trainmate.coach import coach_service


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
        clear_all_tables(test_db)

    @patch("trainmate.coach.openrouter_client")
    def test_adaptation_matching_and_discrepancies(self, mock_client):
        test_profile = {"lthr": 165, "max_hr": 185}

        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "metrics_history_days": 3,
            "low_load_threshold": 10.0,
        }):
            mock_client.complete.return_value = {
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
                        "tss": 0.0,
                    }
                ],
            }

            test_db.save_metric_cache("2026-06-01", 50, 60, 80, 20, 10.0, 8.0, 1.2)
            test_db.save_metric_cache("2026-06-02", 52, 55, 75, 25, 12.0, 8.0, 1.5)
            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            test_db.save_workout(
                "2026-06-01", "running", "Easy Run", "30 mins",
                duration_minutes=30, rpe=4, tss=20,
            )
            test_db.save_workout(
                "2026-06-02", "road_biking", "Tempo Ride", "60 mins",
                duration_minutes=60, rpe=6, tss=40,
            )
            test_db.save_workout(
                "2026-06-03", "running", "Interval Session", "45 mins",
                duration_minutes=45, rpe=8, tss=60,
            )

            test_db.save_completed_activity(
                "act_1", "2026-06-01", "2026-06-01 08:00:00", "Easy Run",
                "running", 1800.0, 5.0, 50.0, 132, 150, 4, 20.0,
            )
            test_db.save_completed_activity(
                "act_2", "2026-06-02", "2026-06-02 08:00:00", "Short Cycling",
                "cycling", 1800.0, 12.0, 100.0, 132, 150, 4, 20.0,
            )

            reason, proposed = coach_service.adapt("2026-06-03")

            self.assertTrue(mock_client.complete.called)
            self.assertEqual(reason, "Fatigue detected, RHR is elevated and HRV is suppressed.")
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["title"], "Adapted Rest Day")
            self.assertEqual(proposed[0]["sport_type"], "rest")

            prompt_user_content = mock_client.complete.call_args[0][1]
            self.assertIn(
                "Complete Miss! Missed planned workout 'Interval Session'",
                prompt_user_content,
            )
            self.assertIn("duration mismatch", prompt_user_content)

    @patch("trainmate.coach.openrouter_client")
    def test_adapt_records_learning_updates(self, mock_client):
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "metrics_history_days": 3,
            "low_load_threshold": 10.0,
        }):
            # Pre-existing observation the model can reinforce by [id].
            lid = test_db.add_learning(
                "Elevated RHR after consecutive hard days", sports="running"
            )

            mock_client.complete.return_value = {
                "change_needed": False,
                "reason": "On track.",
                "adapted_workouts": [],
                "learning_updates": [
                    {"op": "reinforce", "id": lid, "confidence": "moderate"},
                    {"op": "add", "text": "Sleep score dips precede HRV suppression",
                     "sports": "general", "confidence": "tentative"},
                ],
            }

            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            reason, proposed = coach_service.adapt("2026-06-03")
            self.assertEqual(reason, "On track.")
            self.assertEqual(proposed, [])

            # The adapt prompt offers the learning_updates schema and shows the
            # existing observation by id (so it can reinforce instead of duplicating).
            system_prompt = mock_client.complete.call_args[0][0]
            self.assertIn("learning_updates", system_prompt)
            self.assertIn(f"[{lid}|running|", system_prompt)

            # Deltas were applied: existing reinforced to 'moderate', new one added.
            learnings = {l["id"]: l for l in test_db.get_learnings()}
            self.assertEqual(len(learnings), 2)
            self.assertEqual(learnings[lid]["confidence"], "moderate")
            self.assertTrue(any(
                l["text"] == "Sleep score dips precede HRV suppression"
                for l in learnings.values()
            ))

    def test_analyze_adherence_direct(self):
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
            # June 1: workload mismatch (planned load 27.5, actual load 64)
            {
                "date": "2026-06-01",
                "activity_id": "act1",
                "activity_name": "Hard Run",
                "activity_type": "running",
                "duration_sec": 1800,
                "rpe": 8,
                "tss": 60.0,
            },
            # June 2: rest day violation (workload 15.0 > threshold 10.0)
            {
                "date": "2026-06-02",
                "activity_id": "act2",
                "activity_name": "Lawn Mowing",
                "activity_type": "walking",
                "duration_sec": 3600,
                "rpe": 5,
                "tss": 10.0,
            },
            # June 4: unplanned activity (workload 30.0 > threshold 10.0)
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

        discrepancies, matching = analyze_adherence(
            planned_workouts=planned,
            completed_activities=completed,
            start_date_obj=date(2026, 6, 1),
            history_days=4,
            low_load_threshold=10.0,
        )

        self.assertEqual(len(discrepancies), 4)
        self.assertTrue(any("workload mismatch" in d for d in discrepancies))
        self.assertTrue(
            any("Rest Day Violation! Performed 'Lawn Mowing'" in d for d in discrepancies)
        )
        self.assertTrue(
            any("Complete Miss! Missed planned workout 'Ride'" in d for d in discrepancies)
        )
        self.assertTrue(
            any("Unplanned Activity! Performed 'Extra Run'" in d for d in discrepancies)
        )


if __name__ == "__main__":
    unittest.main()
