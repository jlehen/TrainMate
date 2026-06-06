import os
import unittest

from tests.helpers import clear_all_tables

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_db.db")

from trainmate.db import Database
import trainmate.db

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db


class TestDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_completed_activities_crud(self):
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
            tss=60.0,
        )

        activities = test_db.get_completed_activities(
            start_date="2026-06-01", end_date="2026-06-05"
        )
        self.assertEqual(len(activities), 1)
        act = activities[0]
        self.assertEqual(act["activity_id"], "act_123")
        self.assertEqual(act["activity_name"], "Morning Run")
        self.assertEqual(act["rpe"], 7)
        self.assertEqual(act["tss"], 60.0)

        # Upsert: same activity_id updates the row
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
            tss=65.0,
        )

        activities = test_db.get_completed_activities()
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["activity_name"], "Morning Run Updated")
        self.assertEqual(activities[0]["rpe"], 8)
        self.assertEqual(activities[0]["tss"], 65.0)

    def test_workout_workload_fields(self):
        w_id = test_db.save_workout(
            date="2026-06-03",
            sport_type="running",
            title="Tempo Run",
            description="30 mins at LTHR",
            duration_minutes=45,
            rpe=7,
            tss=50,
        )

        workout = test_db.get_workout_by_id(w_id)
        self.assertIsNotNone(workout)
        self.assertEqual(workout["duration_minutes"], 45)
        self.assertEqual(workout["rpe"], 7)
        self.assertEqual(workout["tss"], 50)

    def test_macrocycles_cascade_delete(self):
        obj_id = test_db.add_objective(
            title="Berlin Marathon",
            target_date="2026-09-27",
            sport_type="running",
            priority=1,
        )

        self.assertIsNone(test_db.get_macrocycle_for_objective(obj_id))

        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="aerobic focus plan",
            goals_hash="hashgoals123",
            lifeevents_hash="hashconstraints456",
            mesocycles=[
                {"name": "Base Building", "start_date": "2026-06-01",
                 "end_date": "2026-06-28", "focus": "Aerobic threshold"},
                {"name": "Specific prep", "start_date": "2026-06-29",
                 "end_date": "2026-07-26", "focus": "Intensity increase"},
            ],
        )
        self.assertIsNotNone(macro_id)

        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro["strategy"], "aerobic focus plan")

        mesocycles = test_db.get_mesocycles_for_macrocycle(macro["id"])
        self.assertEqual(len(mesocycles), 2)
        self.assertEqual(mesocycles[0]["name"], "Base Building")
        self.assertEqual(mesocycles[1]["name"], "Specific prep")

        test_db.delete_objective(obj_id)
        self.assertIsNone(test_db.get_macrocycle_for_objective(obj_id))
        self.assertEqual(len(test_db.get_mesocycles_for_macrocycle(macro_id)), 0)


if __name__ == "__main__":
    unittest.main()
