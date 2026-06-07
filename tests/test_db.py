import os
import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import clear_all_tables

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_db.db")

from trainmate.db import (
    Database, normalize_sports, valid_confidence, learning_is_dormant
)
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

    # --- Coach learnings (Phase 2 enrichment) ---

    def test_learning_enrichment_defaults_and_fields(self):
        lid = test_db.add_learning(
            "Handles long runs well", sports="running", confidence="moderate"
        )
        learnings = test_db.get_learnings()
        self.assertEqual(len(learnings), 1)
        learning = learnings[0]
        self.assertEqual(learning["id"], lid)
        self.assertEqual(learning["text"], "Handles long runs well")
        self.assertEqual(learning["sports"], "running")
        self.assertEqual(learning["confidence"], "moderate")
        self.assertFalse(learning["dormant"])
        # A fresh record's last reinforcement is its creation.
        self.assertEqual(learning["last_reinforced_at"], learning["created_at"])

        # Omitted scope/confidence fall back to general/tentative.
        test_db.add_learning("Generic note")
        generic = test_db.get_learnings()[-1]
        self.assertEqual(generic["sports"], "general")
        self.assertEqual(generic["confidence"], "tentative")

    def test_learning_deltas_enriched(self):
        test_db.apply_learning_deltas([
            {"op": "add", "text": "Recovers fast",
             "sports": "Running, Road_Biking", "confidence": "moderate"},
        ])
        learning = test_db.get_learnings()[0]
        lid = learning["id"]
        # Sport scope is normalized (lowercased, trimmed).
        self.assertEqual(learning["sports"], "running,road_biking")
        self.assertEqual(learning["confidence"], "moderate")

        # Revise with only confidence keeps the text but bumps recency.
        before = test_db.get_learnings()[0]["last_reinforced_at"]
        test_db.apply_learning_deltas([
            {"op": "revise", "id": lid, "confidence": "established"}
        ])
        revised = test_db.get_learnings()[0]
        self.assertEqual(revised["confidence"], "established")
        self.assertEqual(revised["text"], "Recovers fast")
        self.assertGreaterEqual(revised["last_reinforced_at"], before)

        # Invalid confidence is ignored, but a valid text change still lands.
        test_db.apply_learning_deltas([
            {"op": "revise", "id": lid, "confidence": "bogus",
             "text": "Recovers very fast"}
        ])
        learning = test_db.get_learnings()[0]
        self.assertEqual(learning["confidence"], "established")
        self.assertEqual(learning["text"], "Recovers very fast")

        # Retire deletes the record.
        test_db.apply_learning_deltas([{"op": "retire", "id": lid}])
        self.assertEqual(len(test_db.get_learnings()), 0)

    def test_learning_decay_and_reinforce_revival(self):
        lid = test_db.add_learning("Tentative observation")  # tentative: 21-day budget
        # Backdate its last reinforcement beyond the budget -> dormant.
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with test_db._get_connection() as conn:
            conn.execute(
                "UPDATE coach_learnings SET last_reinforced_at=? WHERE id=?", (old, lid)
            )
        self.assertTrue(test_db.get_learnings()[0]["dormant"])

        # Reinforcing refreshes recency and revives it.
        test_db.apply_learning_deltas([{"op": "reinforce", "id": lid}])
        revived = test_db.get_learnings()[0]
        self.assertFalse(revived["dormant"])

    def test_reinforcement_suppressed_on_unchanged_evidence(self):
        """The integrity invariant (DESIGN_backward_evaluation.md §8): on unchanged
        evidence, `reinforce` is a no-op and `revise` keeps content but not recency, while
        `add`/`retire` still apply."""
        lid = test_db.add_learning("Tentative observation")  # 21-day budget
        # Make it dormant so a (suppressed) reinforce would visibly revive it if applied.
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with test_db._get_connection() as conn:
            conn.execute(
                "UPDATE coach_learnings SET last_reinforced_at=? WHERE id=?", (old, lid)
            )

        # reinforce is dropped -> still dormant, recency unchanged.
        test_db.apply_learning_deltas(
            [{"op": "reinforce", "id": lid}], suppress_reinforcement=True
        )
        learning = test_db.get_learnings()[0]
        self.assertTrue(learning["dormant"])
        self.assertEqual(learning["last_reinforced_at"], old)

        # revise applies the text edit but does NOT refresh recency (still dormant).
        test_db.apply_learning_deltas(
            [{"op": "revise", "id": lid, "text": "Reworded observation"}],
            suppress_reinforcement=True,
        )
        learning = test_db.get_learnings()[0]
        self.assertEqual(learning["text"], "Reworded observation")
        self.assertEqual(learning["last_reinforced_at"], old)
        self.assertTrue(learning["dormant"])

        # add and retire are unaffected by suppression.
        test_db.apply_learning_deltas(
            [{"op": "add", "text": "Newly surfaced"}], suppress_reinforcement=True
        )
        texts = {l["text"] for l in test_db.get_learnings()}
        self.assertIn("Newly surfaced", texts)
        test_db.apply_learning_deltas(
            [{"op": "retire", "id": lid}], suppress_reinforcement=True
        )
        self.assertNotIn(lid, {l["id"] for l in test_db.get_learnings()})

    def test_analysis_cache_upsert_and_retention(self):
        """One row per horizon; saving again overwrites the slot. `reconstruction`
        round-trips through JSON (DESIGN_backward_evaluation.md §5.1)."""
        self.assertIsNone(test_db.get_analysis_cache("long"))

        recon = {"inferred_macrocycle": {"overall_focus": "Base"},
                 "physiological_insights": ["HRV stable"]}
        test_db.save_analysis_cache(
            "long", "fp-1", "2026-01-01", "2026-03-31", recon
        )
        row = test_db.get_analysis_cache("long")
        self.assertEqual(row["fingerprint"], "fp-1")
        self.assertEqual(row["window_start"], "2026-01-01")
        self.assertEqual(row["reconstruction"], recon)

        # Re-saving the same horizon overwrites (one-row-per-horizon retention).
        test_db.save_analysis_cache("long", "fp-2", "2026-01-01", "2026-04-30", {"x": 1})
        row = test_db.get_analysis_cache("long")
        self.assertEqual(row["fingerprint"], "fp-2")
        self.assertEqual(row["reconstruction"], {"x": 1})

        # Horizons are independent slots.
        test_db.save_analysis_cache("short", "fp-s", "2026-04-01", "2026-04-30", {"y": 2})
        self.assertEqual(test_db.get_analysis_cache("short")["fingerprint"], "fp-s")
        self.assertEqual(test_db.get_analysis_cache("long")["fingerprint"], "fp-2")

        # wipe_metrics also clears the reconstruction cache (evidence-derived).
        test_db.wipe_metrics()
        self.assertIsNone(test_db.get_analysis_cache("long"))
        self.assertIsNone(test_db.get_analysis_cache("short"))

    def test_learning_helpers(self):
        self.assertEqual(normalize_sports(None), "general")
        self.assertEqual(normalize_sports(""), "general")
        self.assertEqual(normalize_sports("Running, Cycling"), "running,cycling")
        self.assertEqual(normalize_sports(["Running", " Hiking "]), "running,hiking")

        self.assertEqual(valid_confidence("established"), "established")
        self.assertIsNone(valid_confidence("bogus"))

        base = datetime.now(timezone.utc)
        # Higher confidence survives longer: established budget is 180 days.
        fresh = {"confidence": "established",
                 "last_reinforced_at": (base - timedelta(days=100)).isoformat()}
        stale = {"confidence": "established",
                 "last_reinforced_at": (base - timedelta(days=200)).isoformat()}
        self.assertFalse(learning_is_dormant(fresh, base))
        self.assertTrue(learning_is_dormant(stale, base))


if __name__ == "__main__":
    unittest.main()
