import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_periodization.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.service.db = test_db

from trainmate.coach import coach_service


class TestPeriodization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.service.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_hashing_helpers(self):
        hash1 = coach_service._get_goals_hash([])
        hash2 = coach_service._get_constraints_hash([])
        hash3 = coach_service._get_config_hash()
        self.assertIsNotNone(hash1)
        self.assertIsNotNone(hash2)
        self.assertIsNotNone(hash3)

        obj = {
            "id": 1, "title": "Test Goal", "target_date": "2026-10-15",
            "sport_type": "running", "description": "sub 3hr",
            "priority": 1, "status": "active",
        }
        hash1_with_obj = coach_service._get_goals_hash([obj])
        self.assertNotEqual(hash1, hash1_with_obj)

        obj["description"] = "sub 2:50"
        self.assertNotEqual(hash1_with_obj, coach_service._get_goals_hash([obj]))

        c = {
            "id": 1, "title": "Spain Trip", "start_date": "2026-07-01",
            "end_date": "2026-07-08", "rest": 0, "description": "easy",
        }
        self.assertNotEqual(hash2, coach_service._get_constraints_hash([c]))

    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_replan_logic_and_caching(self, mock_client, mock_today):
        mock_today.return_value = "2026-06-01"
        obj_id = test_db.add_objective(
            title="Berlin Marathon", target_date="2026-09-27",
            sport_type="running", priority=1,
        )

        mock_macro_response = {
            "strategy": "Simulated overall strategy",
            "mesocycles": [
                {"name": "Base Building", "start_date": "2026-06-01",
                 "end_date": "2026-06-28", "focus": "Endurance"},
                {"name": "Peak & Taper", "start_date": "2026-06-29",
                 "end_date": "2026-07-05", "focus": "Taper"},
            ],
        }
        mock_workouts_response = {
            "reasoning": "Microcycle generated reasoning",
            "learning_updates": [{"op": "add", "text": "Simulated learnings"}],
            "workouts": [{
                "date": "2026-06-01", "sport_type": "running",
                "title": "Base Run", "description": "45 mins zone 2",
            }],
        }

        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        reason, workouts = coach_service.replan(force=False)
        self.assertEqual(reason, "Microcycle generated reasoning")
        self.assertEqual(len(workouts), 1)
        self.assertEqual(workouts[0]["title"], "Base Run")
        self.assertEqual(mock_client.complete.call_count, 2)

        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertIsNotNone(macro)
        self.assertEqual(macro["strategy"], "Simulated overall strategy")
        self.assertEqual(len(test_db.get_mesocycles_for_macrocycle(macro["id"])), 2)

        # No changes + force=False → reuse macrocycle, generate workouts only
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = [mock_workouts_response]
        coach_service.replan(force=False)
        self.assertEqual(mock_client.complete.call_count, 1)

        # force=True → regenerate everything
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        coach_service.replan(force=True)
        self.assertEqual(mock_client.complete.call_count, 2)

        # New goal added → hash mismatch → regenerate everything
        test_db.add_objective(
            title="Mini Triathlon", target_date="2026-08-01",
            sport_type="road_biking", priority=2,
        )
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        coach_service.replan(force=False)
        self.assertEqual(mock_client.complete.call_count, 2)

    def test_system_prompt_inserts_periodization(self):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Run long and slow",
            goals_hash="hash1",
            constraints_hash="hash2",
            mesocycles=[
                {"name": "Base Building", "start_date": "2026-06-01",
                 "end_date": "2026-06-28", "focus": "Zone 2 runs"},
                {"name": "Peak & Taper", "start_date": "2026-06-29",
                 "end_date": "2026-07-05", "focus": "Tapering"},
            ],
        )

        objs = test_db.get_objectives(status="active")
        prompt = coach_service._get_coach_system_prompt(objs, [])

        self.assertIn("Run long and slow", prompt)
        self.assertIn("Base Building (2026-06-01 to 2026-06-28): Zone 2 runs", prompt)
        self.assertIn("Peak & Taper (2026-06-29 to 2026-07-05): Tapering", prompt)
        self.assertIn("COACH LEARNINGS & ACTIVE PERIODIZATION STRATEGY:", prompt)
        self.assertIn("START OF SPORTS SCIENCE GUIDELINES", prompt)
        self.assertIn("END OF SPORTS SCIENCE GUIDELINES", prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_replan_provides_previous_strategy_context_to_llm(self, mock_client):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="old_goals_hash",
            constraints_hash="old_constraints_hash",
            mesocycles=[{
                "name": "Base Building", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Aerobic conditioning",
            }],
        )

        mock_client.complete.side_effect = [
            {
                "strategy": "New strategy building on previous",
                "mesocycles": [{
                    "name": "Specific Prep", "start_date": "2026-06-01",
                    "end_date": "2026-06-28", "focus": "Faster runs",
                }],
            },
            {"reasoning": "Reasoning", "workouts": []},
        ]

        # A plan-shaping constraint triggers hash mismatch → replanning
        test_db.add_constraint(
            title="Business Trip", start_date="2026-06-10", end_date="2026-06-12",
            description="limited training time", replan=1,
        )

        coach_service.replan(force=False)

        self.assertEqual(mock_client.complete.call_count, 2)
        system_prompt = mock_client.complete.call_args_list[0][0][0]
        self.assertIn("PREVIOUS PERIODIZATION STRATEGY (FOR CONTEXT):", system_prompt)
        self.assertIn("Keep heart rate low", system_prompt)
        self.assertIn(
            "Base Building (2026-06-01 to 2026-06-28): Aerobic conditioning",
            system_prompt,
        )
        self.assertIn(
            "For context, the PREVIOUS periodization strategy that was in place",
            system_prompt,
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_generate_injects_planned_vs_actual(self, mock_client):
        # Option A (DESIGN_backward_evaluation.md §6): the prior plan's elapsed blocks are
        # compared against what was actually completed, and fed into the strategy prompt.
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="Old strategy",
            goals_hash="g", constraints_hash="l",
            mesocycles=[{
                "name": "Base Building", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Aerobic conditioning",
            }],
        )
        # A completed session inside that elapsed block.
        test_db.save_completed_activity(
            activity_id="a1", date="2026-06-01", start_time="08:00:00",
            activity_name="Base Run", activity_type="running",
            duration_sec=3600.0, distance_km=10.0, elevation_gain_m=50.0,
            avg_hr=140, max_hr=160, rpe=5, tss=60.0,
        )
        mock_client.complete.return_value = {
            "strategy": "New strategy", "mesocycles": [{
                "name": "Build", "start_date": "2026-06-08",
                "end_date": "2026-10-15", "focus": "Threshold",
            }],
        }
        coach_service.plan_generate(force=True, objective_id=obj_id)
        system_prompt = mock_client.complete.call_args[0][0]
        self.assertIn("PRIOR TRAINING REVIEW:", system_prompt)
        self.assertIn("PLANNED vs ACTUAL", system_prompt)
        self.assertIn("Aerobic conditioning", system_prompt)
        self.assertIn("1 sessions", system_prompt)

    def test_system_prompt_inserts_athlete_profile(self):
        test_profile = {
            "name": "Jane Doe",
            "birth_year": 1990,
            "max_hr": 190,
            "lthr": 170,
            "weekly_target_hours": 8.0,
            "sport_preferences": ["running", "yoga"],
            "chronic_injuries": "Tendency for runner's knee.",
            "preferences": "Enjoys morning runs.",
            "equipment": ["Garmin Watch", "Yoga Mat"],
            "weekly_schedule": {
                "Monday": {
                    "total_available_hours": 1.5,
                    "max_sessions": 2,
                    "certainty_percent": 95,
                    "equipment": ["treadmill"],
                },
                "Wednesday": 0.0,
            },
        }
        with patch.dict(trainmate.coach.config.data, {"user_profile": test_profile}):
            prompt = coach_service._get_coach_system_prompt([], [])
            self.assertIn("Jane Doe", prompt)
            self.assertIn("Birth Year: 1990", prompt)
            self.assertIn("Tendency for runner's knee.", prompt)
            self.assertIn("Enjoys morning runs.", prompt)
            self.assertIn("Garmin Watch, Yoga Mat", prompt)
            self.assertIn(
                "Monday: 1.5 hours | Max sessions: 2 | Certainty: 95% (Equipment: treadmill)",
                prompt,
            )
            self.assertIn("Wednesday: 0.0 hours", prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_snapshots_goals_and_constraints(self, mock_client):
        import json
        obj_id = test_db.add_objective(
            title="Berlin Marathon", target_date="2026-10-15",
            sport_type="running", description="sub-3 attempt", priority=1,
        )
        # A plan-shaping (replan=1) constraint the plan should snapshot. A tactical one
        # is excluded because only plan-shaping constraints fingerprint/snapshot the plan.
        test_db.add_constraint(
            title="Work trip", start_date="2026-08-01", end_date="2026-08-10",
            description="limited training time", replan=1,
        )
        test_db.add_constraint(
            title="no run Thursday", start_date="2026-08-06", end_date="2026-08-06",
            replan=0,
        )

        mock_client.complete.return_value = {
            "strategy": "Snapshot strategy",
            "mesocycles": [{
                "name": "Base Phase", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }

        coach_service.plan_generate(force=True, objective_id=obj_id)

        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertIsNotNone(macro["goals_snapshot"])
        self.assertIsNotNone(macro["constraints_snapshot"])

        goals = json.loads(macro["goals_snapshot"])
        events = json.loads(macro["constraints_snapshot"])
        self.assertEqual([g["title"] for g in goals], ["Berlin Marathon"])
        # Only the plan-shaping constraint is snapshotted, not the tactical one.
        self.assertEqual([e["title"] for e in events], ["Work trip"])

        # The snapshot must serialize exactly the data the hash fingerprints, so the
        # two never disagree about what the plan was built on.
        self.assertEqual(
            coach_service._get_goals_hash(goals), macro["goals_hash"]
        )
        self.assertEqual(
            coach_service._get_constraints_hash(events), macro["constraints_hash"]
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_plan_and_workouts_separately(self, mock_client):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )

        mock_client.complete.return_value = {
            "strategy": "Separate strategy philosophy",
            "mesocycles": [{
                "name": "Base Phase", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }

        with self.assertRaises(ValueError):
            coach_service.workout_generate()

        proposal = coach_service.plan_generate(force=False)
        strategy, mesos = proposal['strategy'], proposal['mesocycles']
        self.assertEqual(strategy, "Separate strategy philosophy")
        self.assertEqual(len(mesos), 1)
        mock_client.complete.assert_called_once()

        mock_client.complete.reset_mock()
        mock_client.complete.return_value = {
            "reasoning": "Separate workout reasoning",
            "workouts": [{
                "date": "2026-06-01", "sport_type": "running",
                "title": "Base Run", "description": "30 mins",
            }],
        }
        reason, workouts = coach_service.workout_generate()
        self.assertEqual(reason, "Separate workout reasoning")
        self.assertEqual(len(workouts), 1)
        self.assertEqual(workouts[0]["title"], "Base Run")
        mock_client.complete.assert_called_once()

    @patch("trainmate.coach.service.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_preserves_completed_today_workout(
        self, mock_client, mock_calendar
    ):
        """When today's planned session has a matching completed activity, regeneration
        must keep today's workout and start the new plan tomorrow."""
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        mock_client.complete.return_value = {
            "strategy": "Strategy", "mesocycles": [{
                "name": "Base", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }
        coach_service.plan_generate(force=False)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

        # Today's planned workout, already done (a matching Garmin run today).
        test_db.save_workout(
            date=today, sport_type="running", title="Today Done",
            description="completed session", google_event_id="evt-today",
        )
        test_db.save_completed_activity(
            activity_id="act-today", date=today, start_time=None,
            activity_name="Morning Run", activity_type="running",
            duration_sec=3600, distance_km=10.0, elevation_gain_m=50.0,
            avg_hr=150, max_hr=170, rpe=6, tss=50.0,
        )

        # The model is asked to start tomorrow; it (correctly) dates its workout tomorrow.
        mock_client.complete.reset_mock()
        mock_client.complete.return_value = {
            "reasoning": "New plan", "workouts": [{
                "date": tomorrow, "sport_type": "running",
                "title": "Tomorrow Run", "description": "fresh",
            }],
        }
        coach_service.workout_generate()

        # Today's completed workout survives; the new plan begins tomorrow.
        titles = [w["title"] for w in test_db.get_workouts(start_date=today)]
        self.assertEqual(titles, ["Today Done", "Tomorrow Run"])
        # Today's Calendar event was left untouched (only future days are torn down).
        for call in mock_calendar.delete_workout_event.call_args_list:
            self.assertNotEqual(call.args[0], "evt-today")

    @patch("trainmate.coach.service.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_workouts_clears_stale_synced_workouts(
        self, mock_client, mock_calendar
    ):
        """Regenerating workouts must wipe the previous plan's future workouts,
        including synced ones (and delete their Google Calendar events)."""
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )

        mock_client.complete.return_value = {
            "strategy": "Strategy", "mesocycles": [{
                "name": "Base", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }
        coach_service.plan_generate(force=False)

        # Simulate a stale workout from the old plan that was synced to Calendar.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=future, sport_type="running", title="Old Plan Run",
            description="stale", google_event_id="evt-old-123",
        )

        mock_client.complete.reset_mock()
        mock_client.complete.return_value = {
            "reasoning": "New plan", "workouts": [{
                "date": today, "sport_type": "running",
                "title": "New Run", "description": "fresh",
            }],
        }
        coach_service.workout_generate()

        # The stale synced workout is gone, and only the new workout remains.
        remaining = test_db.get_workouts(start_date=today)
        titles = [w["title"] for w in remaining]
        self.assertNotIn("Old Plan Run", titles)
        self.assertEqual(titles, ["New Run"])
        # Its Google Calendar event was deleted.
        mock_calendar.delete_workout_event.assert_called_once_with("evt-old-123")

    @patch("trainmate.coach.service.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_workouts_clears_stale_unsynced_calendar_workouts(
        self, mock_client, mock_calendar
    ):
        """A workout that was pushed then adapted/swapped (stale but with a
        google_event_id) must still have its Calendar event deleted on regenerate —
        i.e. cleanup keys on google_event_id, not the freshness state (orphan guard)."""
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )

        mock_client.complete.return_value = {
            "strategy": "Strategy", "mesocycles": [{
                "name": "Base", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }
        coach_service.plan_generate(force=False)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        # On the calendar (google_event_id) but pending re-push after an adaptation.
        test_db.save_workout(
            date=future, sport_type="running", title="Adapted Run",
            description="adapted", google_event_id="evt-stale-456",
            modification_reason="swapped",
        )

        mock_client.complete.reset_mock()
        mock_client.complete.return_value = {
            "reasoning": "New plan", "workouts": [{
                "date": today, "sport_type": "running",
                "title": "New Run", "description": "fresh",
            }],
        }
        coach_service.workout_generate()

        remaining = test_db.get_workouts(start_date=today)
        self.assertEqual([w["title"] for w in remaining], ["New Run"])
        mock_calendar.delete_workout_event.assert_called_once_with("evt-stale-456")

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_regenerate_supersedes_prior_version(self, mock_client):
        """Regenerating a plan keeps the prior macrocycle as a superseded version
        rather than deleting it (see DESIGN_plan_rollback.md)."""
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        meso = [{
            "name": "Base", "start_date": "2026-06-01",
            "end_date": "2026-06-28", "focus": "Base",
        }]
        mock_client.complete.return_value = {"strategy": "v1", "mesocycles": meso}
        coach_service.plan_generate(force=False)
        obj_id = test_db.get_active_objective()["id"]
        v1 = test_db.get_macrocycle_for_objective(obj_id)

        mock_client.complete.return_value = {"strategy": "v2", "mesocycles": meso}
        coach_service.plan_generate(force=True)

        versions = test_db.get_macrocycle_versions(obj_id)
        self.assertEqual(len(versions), 2)
        active = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(active["strategy"], "v2")
        self.assertNotEqual(active["id"], v1["id"])
        superseded = [v for v in versions if v["status"] == "superseded"]
        self.assertEqual([v["id"] for v in superseded], [v1["id"]])

    @patch("trainmate.coach.service.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_rollback_restores_plan_and_workouts(self, mock_client, mock_calendar):
        """`plan rollback` restores the previous plan version, resurrects its workouts,
        archives the current plan's, and reconciles Google Calendar symmetrically."""
        mock_calendar.sync_workout.return_value = "evt-new"
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        meso = [{
            "name": "Base", "start_date": "2026-06-01",
            "end_date": "2026-06-28", "focus": "Base",
        }]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        obj_id = None

        # --- Plan v1 + its workouts ---
        mock_client.complete.return_value = {"strategy": "v1", "mesocycles": meso}
        coach_service.plan_generate(force=False)
        obj_id = test_db.get_active_objective()["id"]
        v1_id = test_db.get_macrocycle_for_objective(obj_id)["id"]
        mock_client.complete.return_value = {
            "reasoning": "w1", "workouts": [{
                "date": today, "sport_type": "running",
                "title": "V1 Run", "description": "v1 session",
            }],
        }
        coach_service.workout_generate()

        # --- Plan v2 + its workouts (archives v1's) ---
        mock_client.complete.return_value = {"strategy": "v2", "mesocycles": meso}
        coach_service.plan_generate(force=True)
        v2_id = test_db.get_macrocycle_for_objective(obj_id)["id"]
        mock_client.complete.return_value = {
            "reasoning": "w2", "workouts": [{
                "date": today, "sport_type": "running",
                "title": "V2 Run", "description": "v2 session",
            }],
        }
        coach_service.workout_generate()
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=today)], ["V2 Run"]
        )

        # --- Roll back to v1 ---
        result = coach_service.plan_rollback(objective_id=obj_id)

        self.assertEqual(result["to"]["id"], v1_id)
        self.assertEqual(result["from"]["id"], v2_id)
        self.assertEqual(test_db.get_macrocycle_for_objective(obj_id)["strategy"], "v1")
        # V1's workout is live again; V2's is archived.
        live = test_db.get_workouts(start_date=today)
        self.assertEqual([w["title"] for w in live], ["V1 Run"])
        self.assertEqual(result["restored_workouts"], 1)
        self.assertEqual(result["archived_workouts"], 1)
        # The restored workout was re-pushed to Calendar.
        self.assertTrue(mock_calendar.sync_multiple.called)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_rollback_without_history_raises(self, mock_client):
        """Rolling back a plan with no earlier version is rejected."""
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        mock_client.complete.return_value = {"strategy": "v1", "mesocycles": [{
            "name": "Base", "start_date": "2026-06-01",
            "end_date": "2026-06-28", "focus": "Base",
        }]}
        coach_service.plan_generate(force=False)
        obj_id = test_db.get_active_objective()["id"]
        with self.assertRaises(ValueError):
            coach_service.plan_rollback(objective_id=obj_id)

    def _plan_v1(self, mock_client, strategy: str = "v1", force: bool = False) -> int:
        """Generates a periodization plan and returns the active macrocycle id."""
        mock_client.complete.return_value = {"strategy": strategy, "mesocycles": [{
            "name": "Base", "start_date": "2026-06-01",
            "end_date": "2026-06-28", "focus": "Base",
        }]}
        coach_service.plan_generate(force=force)
        obj_id = test_db.get_active_objective()["id"]
        return test_db.get_macrocycle_for_objective(obj_id)["id"]

    @staticmethod
    def _generate_workout(mock_client, today: str, title: str) -> None:
        """Runs `workout generate` with a single-session response."""
        mock_client.complete.return_value = {
            "reasoning": title, "workouts": [{
                "date": today, "sport_type": "running",
                "title": title, "description": f"{title} session",
            }],
        }
        coach_service.workout_generate()

    @patch("trainmate.coach.service.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_workout_rollback_restores_batch_leaving_plan_active(
        self, mock_client, mock_calendar
    ):
        """`workout rollback` restores the archived batch and re-pushes it, without
        touching the active plan version (see DESIGN_plan_rollback.md §9)."""
        mock_calendar.sync_workout.return_value = "evt-new"
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self._plan_v1(mock_client)
        self._generate_workout(mock_client, today, "V1 Run")
        v2_id = self._plan_v1(mock_client, strategy="v2", force=True)
        self._generate_workout(mock_client, today, "V2 Run")

        result = coach_service.workout_rollback()

        obj_id = test_db.get_active_objective()["id"]
        self.assertEqual(test_db.get_macrocycle_for_objective(obj_id)["id"], v2_id)
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=today)], ["V1 Run"]
        )
        self.assertEqual(result["restored_workouts"], 1)
        self.assertEqual(result["archived_workouts"], 1)
        self.assertTrue(mock_calendar.sync_multiple.called)

    @patch("trainmate.coach.service.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_workout_rollback_within_one_plan_version(self, mock_client, mock_calendar):
        """Two regenerations under the same plan are distinguished by their archive
        batch, so a rollback undoes the second one (the case `plan rollback`'s
        macrocycle-keyed restore cannot express)."""
        mock_calendar.sync_workout.return_value = "evt-new"
        test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        macro_id = self._plan_v1(mock_client)
        self._generate_workout(mock_client, today, "First Run")
        self._generate_workout(mock_client, today, "Second Run")

        coach_service.workout_rollback()

        live = test_db.get_workouts(start_date=today)
        self.assertEqual([w["title"] for w in live], ["First Run"])
        self.assertEqual(live[0]["macrocycle_id"], macro_id)

        # A second rollback steps forward again: the batch just archived is now newest.
        coach_service.workout_rollback()
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=today)], ["Second Run"]
        )

    def test_workout_rollback_without_archive_raises(self):
        """Rolling back workouts with nothing archived is rejected."""
        with self.assertRaises(ValueError):
            coach_service.workout_rollback()

    def test_restore_workout_batch_skips_past_dated_rows(self):
        """A batch reaching back before the restore floor keeps its past-dated rows
        archived — their slots are held by live rows the archive step left alone
        (see DESIGN_plan_rollback.md §9)."""
        test_db.save_workout(
            date="2026-06-01", sport_type="running", title="Old Mon", description="x"
        )
        test_db.save_workout(
            date="2026-06-05", sport_type="running", title="Old Fri", description="x"
        )
        test_db.archive_future_workouts("2026-06-01")
        # A later generation re-occupied the early slot; only that row is live now.
        test_db.save_workout(
            date="2026-06-01", sport_type="running", title="New Mon", description="x"
        )

        batches = test_db.get_archived_batches(from_date="2026-06-03")
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["workouts"], 2)
        self.assertEqual(batches[0]["restorable"], 1)

        restored = test_db.restore_workout_batch(batches[0]["archived_at"], "2026-06-03")
        self.assertEqual([w["title"] for w in restored], ["Old Fri"])
        # One live row per date+sport: the past-dated "Old Mon" stayed archived.
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date="2026-06-01")],
            ["New Mon", "Old Fri"],
        )

    @patch("trainmate.coach.service.config")
    def test_load_science_guidelines(self, mock_config):
        temp_app_dir = tempfile.mkdtemp()
        temp_user_dir = tempfile.mkdtemp()

        try:
            mock_config.app_science_dir = temp_app_dir
            mock_config.science_dir = temp_user_dir

            with open(os.path.join(temp_app_dir, "app_science.txt"), "w") as f:
                f.write("App guideline text")
            with open(os.path.join(temp_user_dir, "user_science.txt"), "w") as f:
                f.write("User guideline text")

            guidelines = coach_service._load_science_guidelines()

            self.assertIn("=== Guidelines from app_science.txt ===", guidelines)
            self.assertIn("App guideline text", guidelines)
            self.assertIn("=== Guidelines from user_science.txt ===", guidelines)
            self.assertIn("User guideline text", guidelines)
        finally:
            shutil.rmtree(temp_app_dir)
            shutil.rmtree(temp_user_dir)

    def test_config_hash_logic(self):
        initial_hash = coach_service._get_config_hash()
        self.assertIsNotNone(initial_hash)

        original_profile = dict(trainmate.coach.config.data["user_profile"])
        original_coach = dict(trainmate.coach.config.data.get("coach") or {})
        try:
            trainmate.coach.config.data["user_profile"]["weekly_target_hours"] = 20.0
            self.assertNotEqual(initial_hash, coach_service._get_config_hash())
            trainmate.coach.config.data["user_profile"] = dict(original_profile)

            # Physiological thresholds are tolerance-checked via the snapshot, not
            # fingerprinted — editing one must not shift the hash.
            trainmate.coach.config.data["user_profile"]["ftp"] = 999
            self.assertEqual(initial_hash, coach_service._get_config_hash())
            trainmate.coach.config.data["user_profile"] = dict(original_profile)

            # Prompt-context knobs are not plan-shaping.
            trainmate.coach.config.data["coach"] = dict(original_coach)
            trainmate.coach.config.data["coach"]["metrics_lookback_days"] = 99
            self.assertEqual(initial_hash, coach_service._get_config_hash())
        finally:
            trainmate.coach.config.data["user_profile"] = original_profile
            trainmate.coach.config.data["coach"] = original_coach

    def test_config_changed_threshold_tolerance(self):
        # FTP now lives in the benchmark logbook, not config (DESIGN_benchmark_workouts
        # §3.4); drift is driven by recording newer results (latest row wins).
        original_profile = dict(trainmate.coach.config.data["user_profile"])
        try:
            test_db.add_benchmark_result(
                date="2026-06-01", sport_type="road_biking",
                anchor_kind="ftp", value=220, unit="W",
            )
            macro = {
                "config_hash": coach_service._get_config_hash(),
                "config_snapshot": coach_service._get_config_snapshot(),
            }
            self.assertIsNone(coach_service.config_changed(macro))

            # Within the default 5% band: still current.
            test_db.add_benchmark_result(
                date="2026-06-15", sport_type="road_biking",
                anchor_kind="ftp", value=228, unit="W",
            )
            self.assertIsNone(coach_service.config_changed(macro))

            # Past the band: stale, with the threshold named in the reason.
            test_db.add_benchmark_result(
                date="2026-07-01", sport_type="road_biking",
                anchor_kind="ftp", value=250, unit="W",
            )
            reason = coach_service.config_changed(macro)
            self.assertIsNotNone(reason)
            self.assertIn("ftp", reason)

            # A newly recorded kind absent from the old snapshot is skipped (§3.5), not
            # read as instant drift — restore ftp to baseline first so it isn't the cause.
            test_db.add_benchmark_result(
                date="2026-07-02", sport_type="road_biking",
                anchor_kind="ftp", value=220, unit="W",
            )
            test_db.add_benchmark_result(
                date="2026-07-03", sport_type="swimming",
                anchor_kind="css", value=95, unit="sec/100m",
            )
            self.assertIsNone(coach_service.config_changed(macro))

            # Non-threshold profile edits still trip the fingerprint.
            trainmate.coach.config.data["user_profile"]["weekly_target_hours"] = 20.0
            self.assertEqual(
                coach_service.config_changed(macro), "athlete profile changed"
            )

            # Legacy macrocycle without a snapshot: fingerprint alone decides.
            trainmate.coach.config.data["user_profile"] = dict(original_profile)
            legacy = {"config_hash": coach_service._get_config_hash(),
                      "config_snapshot": None}
            self.assertIsNone(coach_service.config_changed(legacy))
        finally:
            trainmate.coach.config.data["user_profile"] = original_profile

    def test_db_config_hash_operations(self):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Long runs",
            goals_hash="ghash",
            constraints_hash="lehash",
            config_hash="confhash123",
            mesocycles=[],
        )

        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro["config_hash"], "confhash123")

        test_db.update_macrocycle_config_hash(macro_id, "newconfhash456")
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro["config_hash"], "newconfhash456")
        self.assertIsNone(macro["config_snapshot"])

        test_db.update_macrocycle_config_hash(
            macro_id, "confhash789", '{"ftp": 220.0}'
        )
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro["config_hash"], "confhash789")
        self.assertEqual(macro["config_snapshot"], '{"ftp": 220.0}')

    def test_validation_under_5_weeks(self):
        today = datetime.now(timezone.utc).date()
        test_db.add_objective(
            title="Short Goal",
            target_date=(today + timedelta(weeks=3)).strftime("%Y-%m-%d"),
            sport_type="running",
            priority=1,
        )

        with self.assertRaises(ValueError) as ctx:
            coach_service.plan_generate()
        self.assertIn("too close", str(ctx.exception))

    @patch("trainmate.coach.engine.openrouter_client")
    def test_splitting_over_24_weeks(self, mock_client):
        today = datetime.now(timezone.utc).date()
        target_date_str = (today + timedelta(weeks=30)).strftime("%Y-%m-%d")

        test_db.add_objective(
            title="Ultra Marathon", target_date=target_date_str,
            sport_type="running", priority=1,
        )

        phase_date_str = (today + timedelta(weeks=15)).strftime("%Y-%m-%d")
        # One call now: the milestones and the first leg's blocks come back together, with
        # the science guidelines and athlete history in scope for both.
        mock_client.complete.return_value = {
            "intermediate_goals": [{
                "title": "Ultra Marathon - Interim: Half Marathon Tune-Up",
                "target_date": phase_date_str,
                "sport_type": "running",
                "description": "Mid-way aerobic benchmark",
            }],
            "strategy": "Simulated base building strategy",
            "mesocycles": [{
                "name": "Base Building",
                "start_date": today.strftime("%Y-%m-%d"),
                "end_date": phase_date_str,
                "focus": "Aerobic conditioning",
            }],
        }

        coach_service.plan_generate(force=True)
        self.assertEqual(mock_client.complete.call_count, 1)
        split_prompt = mock_client.complete.call_args[0][0]
        self.assertIn("TASK — PART 1 (SPLIT THE TIMELINE):", split_prompt)
        self.assertIn("START OF SPORTS SCIENCE GUIDELINES", split_prompt)

        active_objs = sorted(
            test_db.get_objectives(status="active"), key=lambda x: str(x["target_date"])
        )
        self.assertEqual(len(active_objs), 2)

        intermediate = active_objs[0]
        self.assertEqual(intermediate["title"], "Ultra Marathon - Interim: Half Marathon Tune-Up")
        self.assertEqual(intermediate["target_date"], phase_date_str)
        # A stepping stone sits one level below the goal it leads to.
        self.assertEqual(intermediate["priority"], 2)

        macro = test_db.get_macrocycle_for_objective(intermediate["id"])
        self.assertIsNotNone(macro)
        self.assertEqual(macro["strategy"], "Simulated base building strategy")
        # The fingerprint covers the interim goals the plan itself created, so the next
        # run reuses the plan instead of seeing them as a change.
        self.assertEqual(
            macro["goals_hash"],
            coach_service._get_goals_hash(test_db.get_objectives(status="active")),
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_split_goals_are_not_written_until_applied(self, mock_client):
        # `plan generate` used to create the interim goals before showing the plan, so a
        # discarded proposal still left them behind in the athlete's goal list.
        today = datetime.now(timezone.utc).date()
        test_db.add_objective(
            title="Ultra Marathon",
            target_date=(today + timedelta(weeks=30)).strftime("%Y-%m-%d"),
            sport_type="running", priority=1,
        )
        phase_date_str = (today + timedelta(weeks=15)).strftime("%Y-%m-%d")
        mock_client.complete.return_value = {
            "intermediate_goals": [{
                "title": "Ultra Marathon - Interim: Half Marathon Tune-Up",
                "target_date": phase_date_str, "sport_type": "running",
                "description": "Mid-way aerobic benchmark",
            }],
            "strategy": "Simulated base building strategy",
            "mesocycles": [{
                "name": "Base Building", "start_date": today.strftime("%Y-%m-%d"),
                "end_date": phase_date_str, "focus": "Aerobic conditioning",
            }],
        }

        proposal = coach_service.plan_generate(force=True, auto_apply=False)

        self.assertEqual(len(proposal["pending_goals"]), 1)
        self.assertIsNone(proposal["goal"]["id"])
        self.assertEqual(len(test_db.get_objectives(status="active")), 1)
        self.assertIsNone(test_db.get_last_macrocycle())

        # Accepting is what writes them.
        planned_id = coach_service.plan_apply(
            None, proposal["strategy"], proposal["mesocycles"],
            pending_goals=proposal["pending_goals"],
        )
        self.assertEqual(len(test_db.get_objectives(status="active")), 2)
        self.assertEqual(
            test_db.get_objective(planned_id)["target_date"], phase_date_str
        )
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(planned_id))

    @patch("trainmate.coach.engine.openrouter_client")
    def test_split_rejects_mesocycles_running_past_the_first_leg(self, mock_client):
        today = datetime.now(timezone.utc).date()
        target_date_str = (today + timedelta(weeks=30)).strftime("%Y-%m-%d")
        test_db.add_objective(
            title="Ultra Marathon", target_date=target_date_str,
            sport_type="running", priority=1,
        )
        phase_date_str = (today + timedelta(weeks=15)).strftime("%Y-%m-%d")
        mock_client.complete.return_value = {
            "intermediate_goals": [{
                "title": "Ultra Marathon - Interim: Half Marathon Tune-Up",
                "target_date": phase_date_str, "sport_type": "running",
                "description": "Mid-way aerobic benchmark",
            }],
            "strategy": "Planned the whole horizon by mistake",
            "mesocycles": [{
                "name": "Base Building", "start_date": today.strftime("%Y-%m-%d"),
                "end_date": target_date_str, "focus": "Aerobic conditioning",
            }],
        }

        with self.assertRaises(ValueError) as ctx:
            coach_service.plan_generate(force=True)
        self.assertIn("past the first", str(ctx.exception))
        self.assertEqual(len(test_db.get_objectives(status="active")), 1)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_split_rejects_first_leg_outside_macrocycle_bounds(self, mock_client):
        # A milestone 28 weeks out leaves a first leg no longer than the goal it split.
        today = datetime.now(timezone.utc).date()
        test_db.add_objective(
            title="Ultra Marathon",
            target_date=(today + timedelta(weeks=40)).strftime("%Y-%m-%d"),
            sport_type="running", priority=1,
        )
        phase_date_str = (today + timedelta(weeks=28)).strftime("%Y-%m-%d")
        mock_client.complete.return_value = {
            "intermediate_goals": [{
                "title": "Ultra Marathon - Interim: Too Far Out",
                "target_date": phase_date_str, "sport_type": "running",
                "description": "",
            }],
            "strategy": "s",
            "mesocycles": [{"name": "Base", "start_date": today.strftime("%Y-%m-%d"),
                            "end_date": phase_date_str, "focus": "Aerobic"}],
        }

        with self.assertRaises(ValueError) as ctx:
            coach_service.plan_generate(force=True)
        self.assertIn("5-24 week range", str(ctx.exception))
        self.assertEqual(len(test_db.get_objectives(status="active")), 1)

    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_splitting_targets_first_interim_goal_not_earliest_active(
        self, mock_client, mock_today
    ):
        # A planned preceding goal pushes the plan start past its own date. Re-reading
        # "the next active goal" after the split used to snap the target back onto that
        # preceding goal, producing a plan window ending before it started.
        mock_today.return_value = "2026-07-31"
        prev_id = test_db.add_objective(
            title="Vertical Power", target_date="2026-09-30",
            sport_type="road_biking", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=prev_id, strategy="Preceding strategy",
            goals_hash="g", constraints_hash="c",
            mesocycles=[{
                "name": "Build", "start_date": "2026-07-31",
                "end_date": "2026-09-30", "focus": "Threshold",
            }],
        )
        far_id = test_db.add_objective(
            title="Ski Mountaineering", target_date="2027-04-30",
            sport_type="ski_touring", priority=1,
        )

        mock_client.complete.return_value = {
            "intermediate_goals": [
                {"title": "Ski Mountaineering - Interim: Base Check",
                 "target_date": "2026-12-10", "sport_type": "ski_touring",
                 "description": "Uphill endurance check"},
                {"title": "Ski Mountaineering - Interim: Long Tour",
                 "target_date": "2027-02-18", "sport_type": "ski_touring",
                 "description": "Steep skiing simulation"},
            ],
            "strategy": "First leg strategy", "mesocycles": [{
                "name": "Base", "start_date": "2026-10-01",
                "end_date": "2026-12-10", "focus": "Aerobic durability",
            }],
        }

        proposal = coach_service.plan_generate(force=True, objective_id=far_id)
        planned_goal = proposal["goal"]

        self.assertEqual(planned_goal["title"], "Ski Mountaineering - Interim: Base Check")
        # The plan window is the first leg, and it starts where the preceding plan ends.
        strategy_prompt = mock_client.complete.call_args[0][0]
        self.assertIn("(2026-10-01 to\n2027-04-30)", strategy_prompt)
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(planned_goal["id"]))
        # The preceding goal keeps its own plan.
        self.assertEqual(
            test_db.get_macrocycle_for_objective(prev_id)["strategy"], "Preceding strategy"
        )

    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_splitting_rejects_out_of_window_interim_goals(self, mock_client, mock_today):
        mock_today.return_value = "2026-07-31"
        far_id = test_db.add_objective(
            title="Ski Mountaineering", target_date="2027-04-30",
            sport_type="ski_touring", priority=1,
        )
        mock_client.complete.return_value = {
            "intermediate_goals": [
                {"title": "Bogus - before the plan start", "target_date": "2026-05-01",
                 "sport_type": "ski_touring", "description": ""},
                {"title": "Bogus - after the goal", "target_date": "2027-06-01",
                 "sport_type": "ski_touring", "description": ""},
            ],
            "strategy": "s", "mesocycles": [],
        }

        with self.assertRaises(ValueError) as ctx:
            coach_service.plan_generate(force=True, objective_id=far_id)
        self.assertIn("usable interim goals", str(ctx.exception))
        # Nothing bogus was written to the athlete's goal list.
        self.assertEqual(len(test_db.get_objectives(status="active")), 1)

    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_multi_goal_planning_and_deletion(self, mock_client, mock_today):
        mock_today.return_value = "2026-06-01"
        obj1_id = test_db.add_objective(
            title="Goal A", target_date="2026-08-01",
            sport_type="running", priority=1,
        )
        obj2_id = test_db.add_objective(
            title="Goal B", target_date="2026-11-01",
            sport_type="running", priority=2,
        )

        mock_client.complete.side_effect = [
            {
                "strategy": "Plan A strategy",
                "mesocycles": [{
                    "name": "Base Building A", "start_date": "2026-06-05",
                    "end_date": "2026-08-01", "focus": "Aerobic conditioning",
                }],
            },
            {
                "strategy": "Plan B strategy",
                "mesocycles": [{
                    "name": "Base Building B", "start_date": "2026-08-02",
                    "end_date": "2026-11-01", "focus": "Aerobic threshold",
                }],
            },
        ]

        proposal_a = coach_service.plan_generate(force=True, objective_id=obj1_id)
        strategy_a, mesos_a = proposal_a['strategy'], proposal_a['mesocycles']
        self.assertEqual(strategy_a, "Plan A strategy")
        self.assertEqual(mesos_a[0]["start_date"], "2026-06-05")

        proposal_b = coach_service.plan_generate(force=True, objective_id=obj2_id)
        strategy_b, mesos_b = proposal_b['strategy'], proposal_b['mesocycles']
        self.assertEqual(strategy_b, "Plan B strategy")
        self.assertIn("from 2026-08-02 until", mock_client.complete.call_args_list[1][0][0])

        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj1_id))
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj2_id))

        coach_service.plan_rm(obj1_id)
        self.assertIsNone(test_db.get_macrocycle_for_objective(obj1_id))
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj2_id))

    def test_config_user_profile_no_threshold_requirement(self):
        # Thresholds moved to the benchmark logbook (DESIGN_benchmark_workouts §3.4), so
        # user_profile no longer requires 'lthr'/'ftp' — a threshold-less profile is a
        # valid cold start (the coach nudges, never refuses).
        with patch.dict(trainmate.coach.config.data, {"user_profile": {"name": "Test Athlete"}}):
            profile = trainmate.coach.config.user_profile
            self.assertEqual(profile["name"], "Test Athlete")
            self.assertNotIn("ftp", profile)

    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_recent_history_summary_periodization_plan(self, mock_client, mock_today_str):
        mock_today_str.return_value = "2026-06-18"
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_completed_activity(
            activity_id="act_test_1", date="2026-06-04", start_time="08:00:00",
            activity_name="Morning Run", activity_type="running",
            duration_sec=3600.0, distance_km=10.0, elevation_gain_m=100.0,
            avg_hr=150, max_hr=170, rpe=6, tss=60.0,
        )
        test_db.save_metric_cache(
            date="2026-06-04", rhr=55, hrv=60, sleep_score=80, stress=25,
            ctl=58.0, atl=61.0, tsb=-3.0,
        )

        mock_client.complete.return_value = {
            "strategy": "Separate strategy philosophy",
            "mesocycles": [{
                "name": "Base Phase", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        }

        coach_service.plan_generate(force=True)

        system_prompt = mock_client.complete.call_args[0][0]
        self.assertIn("ATHLETE RECENT TRAINING SUMMARY (PAST 15 DAYS):", system_prompt)
        self.assertIn("Completed Workouts (Past 15 days):", system_prompt)
        self.assertIn("running: 1 sessions", system_prompt)
        self.assertIn("Resting Heart Rate: 55.0 bpm", system_prompt)

    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.coach.service.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_recent_history_workout_generation(self, mock_client, mock_calendar, mock_today_str):
        mock_today_str.return_value = "2026-06-18"
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash="hash1",
            constraints_hash="hash2",
            mesocycles=[{
                "name": "Base Phase", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Base",
            }],
        )
        test_db.save_metric_cache(
            date="2026-06-04", rhr=55, hrv=60, sleep_score=80, stress=25,
            ctl=58.0, atl=61.0, tsb=-3.0,
        )

        mock_client.complete.return_value = {
            "reasoning": "Separate workout reasoning",
            "workouts": [{
                "date": "2026-06-05", "sport_type": "running",
                "title": "Base Run", "description": "30 mins",
            }],
        }

        coach_service.workout_generate()

        user_content = mock_client.complete.call_args[0][1]
        self.assertIn("Athlete's Metrics History (Past 15 Days):", user_content)
        self.assertIn("RHR=55bpm, HRV=60ms", user_content)


if __name__ == "__main__":
    unittest.main()
