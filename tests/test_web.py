import os
import unittest

from tests.helpers import clear_all_tables

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_web.db")

from trainmate.db import Database

import trainmate_web

# Point the web app's db singleton at an isolated test database. The handlers
# reference the module-level `trainmate_web.db`, so patching it here is enough
# for the read-only endpoints exercised below (the web app never pulls from
# Garmin — it is a pure reader, ARCHITECTURE.md §8).
test_db = Database(db_path=TEST_DB_PATH)
trainmate_web.db = test_db


def _save_activity(db, activity_id, date, activity_type, duration_sec, tss):
    db.save_completed_activity(
        activity_id=activity_id,
        date=date,
        start_time=f"{date} 08:00:00",
        activity_name=f"{activity_type} session",
        activity_type=activity_type,
        duration_sec=duration_sec,
        distance_km=10.0,
        elevation_gain_m=0.0,
        avg_hr=140,
        max_hr=160,
        rpe=None,
        tss=tss,
    )


class TestCompareEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate_web.db = test_db
        cls.client = trainmate_web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def _get(self, start, end):
        return self.client.get(
            f"/api/workouts/compare?start_date={start}&end_date={end}"
        )

    def test_matched_session_no_discrepancy(self):
        # Planned run with a closely-matching completed run -> matched, no discrepancy.
        test_db.save_workout(
            date="2026-06-10", sport_type="running", title="Tempo Run",
            description="40min tempo", duration_minutes=40, tss=50,
        )
        _save_activity(test_db, "a1", "2026-06-10", "running", 40 * 60, 50.0)

        res = self._get("2026-06-10", "2026-06-10")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data["days"]), 1)
        day = data["days"][0]
        self.assertEqual(day["date"], "2026-06-10")
        self.assertEqual(len(day["results"]), 1)
        self.assertIsNotNone(day["results"][0]["completed"])
        self.assertEqual(day["unplanned"], [])
        self.assertEqual(data["discrepancies"], [])

    def test_missed_session_is_discrepancy(self):
        # Planned run with no completed activity -> complete miss.
        test_db.save_workout(
            date="2026-06-10", sport_type="running", title="Long Run",
            description="90min easy", duration_minutes=90, tss=80,
        )

        res = self._get("2026-06-10", "2026-06-10")
        data = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(data["days"]), 1)
        self.assertIsNone(data["days"][0]["results"][0]["completed"])
        self.assertTrue(
            any("Complete Miss" in d for d in data["discrepancies"]),
            data["discrepancies"],
        )

    def test_unplanned_activity_is_flagged(self):
        # An activity on a day with no planned workout, inside a planned block
        # (mesocycle), surfaces as an unplanned deviation.
        obj_id = test_db.add_objective(
            title="Race", target_date="2026-09-01", sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="s", goals_hash="g",
            constraints_hash="l", config_hash="c",
            mesocycles=[{
                "name": "Base", "start_date": "2026-06-01",
                "end_date": "2026-06-30", "focus": "aerobic base",
            }],
        )

        _save_activity(test_db, "a2", "2026-06-10", "running", 60 * 60, 70.0)

        res = self._get("2026-06-10", "2026-06-10")
        data = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(data["days"]), 1)
        unplanned = data["days"][0]["unplanned"]
        self.assertEqual(len(unplanned), 1)
        self.assertEqual(unplanned[0]["kind"], "unplanned")

    def test_end_date_capped_and_default_range(self):
        # No params -> defaults to a 14-day lookback ending today; valid empty result.
        res = self.client.get("/api/workouts/compare")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("filters", data)
        self.assertIn("days", data)


class TestPlanVersionsEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate_web.db = test_db
        cls.client = trainmate_web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_plan_versions_lists_active_and_superseded(self):
        oid = test_db.add_objective(
            title="Web Goal", target_date="2026-12-15",
            sport_type="running", priority=1,
        )
        meso = [{
            "name": "Base", "start_date": "2026-06-01",
            "end_date": "2026-06-28", "focus": "Base",
        }]
        v1 = test_db.save_macrocycle(
            objective_id=oid, strategy="First", goals_hash="g",
            constraints_hash="l", mesocycles=meso,
        )
        v2 = test_db.save_macrocycle(
            objective_id=oid, strategy="Second", goals_hash="g",
            constraints_hash="l", mesocycles=meso,
        )

        res = self.client.get("/api/plan/versions")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["goal"]["id"], oid)
        versions = {v["id"]: v for v in data["versions"]}
        self.assertEqual(set(versions), {v1, v2})
        self.assertEqual(versions[v2]["status"], "active")
        self.assertEqual(versions[v1]["status"], "superseded")


if __name__ == "__main__":
    unittest.main()
