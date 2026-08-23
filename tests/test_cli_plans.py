import json
import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db, save_workout
from trainmate.coach.proposals import GenerateProposal

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_plans.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class TestCliPlans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def run_cli(self, args, input_value="n"):
        return run_cli(args, input_value)

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_plan_generate_applies_to_the_goal_the_service_planned_for(
        self, mock_coach, mock_garmin
    ):
        far_id = test_db.add_objective(
            title="Ski Mountaineering", target_date="2027-04-30",
            sport_type="ski_touring",
        )
        mock_coach.plan_generate.return_value = {
            "strategy": "Long build strategy",
            "mesocycles": [{"name": "Base", "start_date": "2026-08-01",
                            "end_date": "2027-04-30", "focus": "Aerobic durability"}],
            "reused": False,
            "goal": test_db.get_objective(far_id),
        }
        mock_coach.plan_apply.return_value = far_id

        exit_code, stdout, stderr = self.run_cli(
            ["plan", "generate", "--goal", str(far_id)], input_value="y"
        )
        self.assertEqual(exit_code, 0)
        args, kwargs = mock_coach.plan_apply.call_args
        self.assertEqual(args[0], far_id)

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_plan_commands(self, mock_coach, mock_garmin):
        mock_coach.plan_generate.return_value = {
            "strategy": "Mock Strategy",
            "mesocycles": [
                {"name": "Meso 1", "start_date": "2026-01-01",
                 "end_date": "2026-01-28", "focus": "Base"}
            ],
            "reused": False,
            "goal": None,
        }
        mock_coach.workout_generate.return_value = GenerateProposal(
            reasoning="Test workout reasoning",
            workouts=({
                "date": "2026-01-02", "sport_type": "running",
                "title": "Test Workout", "description": "easy",
            },),
        )

        exit_code, stdout, stderr = self.run_cli(["plan", "generate"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Plan discarded", stdout)
        mock_coach.plan_generate.assert_called_once_with(
            force=False, fresh=False, auto_apply=False
        )

        mock_coach.plan_generate.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["plan", "generate", "-f"])
        self.assertEqual(exit_code, 0)
        mock_coach.plan_generate.assert_called_once_with(
            force=True, fresh=False, auto_apply=False
        )

        mock_coach.plan_generate.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["plan", "generate", "--force"])
        self.assertEqual(exit_code, 0)
        mock_coach.plan_generate.assert_called_once_with(
            force=True, fresh=False, auto_apply=False
        )

        # --fresh forces regeneration on its own, so the staleness prompt never runs.
        mock_coach.plan_generate.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["plan", "generate", "--fresh"])
        self.assertEqual(exit_code, 0)
        mock_coach.plan_generate.assert_called_once_with(
            force=True, fresh=True, auto_apply=False
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "generate"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUTS PROPOSED BY COACH ===", stdout)
        self.assertIn("Test workout reasoning", stdout)
        mock_coach.workout_generate.assert_called_once()

        exit_code, stdout, stderr = self.run_cli(["plan", "show"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No active goals found", stdout)

        obj_id = test_db.add_objective(
            title="London Marathon",
            target_date="2026-09-20",
            sport_type="running",
        )

        exit_code, stdout, stderr = self.run_cli(["plan", "show"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No active macrocycle strategy found", stdout)

        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Build base then taper",
            goals_hash="ghash",
            constraints_hash="chash",
            mesocycles=[{
                "name": "Base Building",
                "start_date": "2026-06-01",
                "end_date": "2026-06-28",
                "focus": "Aerobic threshold volume",
            }],
        )

        exit_code, stdout, stderr = self.run_cli(["plan", "show"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ACTIVE MACROCYCLE STRATEGY [Macrocycle ID:", stdout)
        self.assertIn("Build base then taper", stdout)
        self.assertIn("Base Building", stdout)

        mock_coach.plan_rm.reset_mock()
        obj_to_rm = test_db.add_objective(
            title="Goal to remove plan for",
            target_date="2026-09-22",
            sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_to_rm,
            strategy="Plan to delete",
            goals_hash="ghash",
            constraints_hash="lhash",
            mesocycles=[],
        )
        exit_code, stdout, stderr = self.run_cli(["plan", "rm", str(obj_to_rm)])
        self.assertEqual(exit_code, 0)
        self.assertIn("removed successfully", stdout)
        mock_coach.plan_rm.assert_called_once_with(obj_to_rm)

    @patch("trainmate.runtime.garmin")
    def test_plan_show_never_pulls(self, mock_garmin):
        # `plan show` is a pure read: it must never prompt or trigger a Garmin pull,
        # whether or not metrics exist (auto-ensure lives on the generating/adapting
        # commands, not on read-only views).
        test_db.save_metric_cache(
            date="2026-06-03", rhr=50, hrv=75, sleep_score=80, stress=20
        )
        with patch("builtins.input") as mock_input:
            exit_code, stdout, stderr = self.run_cli(["plan", "show"])
            self.assertEqual(exit_code, 0)
            mock_input.assert_not_called()
            mock_garmin.pull.assert_not_called()
            mock_garmin.ensure_data.assert_not_called()

        with test_db._get_connection() as conn:
            conn.execute("DELETE FROM athlete_metrics_cache")
            conn.commit()

        exit_code, stdout, stderr = self.run_cli(["plan", "show"])
        self.assertEqual(exit_code, 0)
        mock_garmin.pull.assert_not_called()

    def test_plan_versions_and_show_version(self):
        """`plan versions` lists active + superseded versions; `plan show --version`
        renders a specific superseded version (see DESIGN_plan_rollback.md)."""
        oid = test_db.add_objective(
            title="Versioned Goal", target_date="2026-12-15",
            sport_type="running",
        )
        meso = [{
            "name": "Base", "start_date": "2026-06-01",
            "end_date": "2026-06-28", "focus": "Base",
        }]
        v1 = test_db.save_macrocycle(
            objective_id=oid, strategy="First strategy alpha",
            goals_hash="g", constraints_hash="l", mesocycles=meso,
        )
        v2 = test_db.save_macrocycle(
            objective_id=oid, strategy="Second strategy beta",
            goals_hash="g", constraints_hash="l", mesocycles=meso,
        )

        exit_code, stdout, _ = self.run_cli(["plan", "versions"])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Macrocycle {v1}", stdout)
        self.assertIn(f"Macrocycle {v2}", stdout)
        self.assertIn("active", stdout)
        self.assertIn("superseded", stdout)

        # Showing the superseded version renders its strategy under a superseded header.
        exit_code, stdout, _ = self.run_cli(["plan", "show", "--macrocycle", str(v1)])
        self.assertEqual(exit_code, 0)
        self.assertIn("SUPERSEDED", stdout)
        self.assertIn("First strategy alpha", stdout)

        # A version id from another goal is rejected.
        other = test_db.add_objective(
            title="Other Goal", target_date="2027-01-15",
            sport_type="running",
        )
        exit_code, stdout, _ = self.run_cli(
            ["plan", "show", "--goal", str(other), "--macrocycle", str(v1)]
        )
        self.assertIn("does not belong", stdout)

    def _seed_two_versions(self) -> tuple:
        """A goal with two plan versions differing in strategy, mesocycle dates and inputs."""
        oid = test_db.add_objective(
            title="Diffable Goal", target_date="2026-12-15",
            sport_type="running",
        )
        v1 = test_db.save_macrocycle(
            objective_id=oid, strategy="Build a wide aerobic base. Then sharpen.",
            goals_hash="g", constraints_hash="c",
            mesocycles=[
                {"name": "Base", "start_date": "2026-06-01",
                 "end_date": "2026-06-28", "focus": "Aerobic volume."},
                {"name": "Dropped Block", "start_date": "2026-06-29",
                 "end_date": "2026-07-12", "focus": "Filler."},
            ],
            goals_snapshot=json.dumps(
                [{"id": oid, "title": "Diffable Goal", "target_date": "2026-08-01"}]
            ),
            constraints_snapshot=json.dumps([{"id": 7, "title": "Holiday", "rest": True}]),
            config_snapshot=json.dumps({"ftp": 200.0, "max_hr": 185.0}),
        )
        v2 = test_db.save_macrocycle(
            objective_id=oid, strategy="Build a wide aerobic base. Then sharpen.",
            goals_hash="g", constraints_hash="c",
            mesocycles=[
                {"name": "Base", "start_date": "2026-06-01",
                 "end_date": "2026-07-05", "focus": "Aerobic volume."},
            ],
            goals_snapshot=json.dumps(
                [{"id": oid, "title": "Diffable Goal", "target_date": "2026-08-15"}]
            ),
            constraints_snapshot=json.dumps([]),
            config_snapshot=json.dumps({"ftp": 220.0, "max_hr": 185.0}),
        )
        return oid, v1, v2

    def test_plan_diff(self):
        """`plan diff` reports what changed between two versions: mesocycle dates, dropped
        blocks, and the snapshotted goals/constraints/thresholds."""
        oid, v1, v2 = self._seed_two_versions()

        # No version given: previous vs active.
        exit_code, stdout, _ = self.run_cli(["plan", "diff", "--goal", str(oid)])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Macrocycle {v1}", stdout)
        self.assertIn(f"Macrocycle {v2}", stdout)
        # Identical strategy text is reported as such, not re-printed.
        self.assertIn("unchanged", stdout)
        # Mesocycle end date moved; the second block disappeared.
        self.assertIn("2026-06-28", stdout)
        self.assertIn("2026-07-05", stdout)
        self.assertIn("Dropped Block", stdout)
        # Inputs: the constraint went away, the goal's date moved, ftp moved.
        self.assertIn("Holiday", stdout)
        self.assertIn("target_date", stdout)
        self.assertIn("200  =>  220", stdout)
        self.assertIn("+10.0%", stdout)

        # Explicit versions in either order, and a version from another goal.
        exit_code, stdout, _ = self.run_cli(
            ["plan", "diff", str(v2), str(v1), "--goal", str(oid)]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("220  =>  200", stdout)

        exit_code, stdout, _ = self.run_cli(
            ["plan", "diff", str(v1), str(v1), "--goal", str(oid)]
        )
        self.assertIn("cannot be compared against itself", stdout)

        other = test_db.add_objective(
            title="Unrelated", target_date="2027-03-01", sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=other, strategy="s", goals_hash="g",
            constraints_hash="c", mesocycles=[],
        )
        exit_code, stdout, _ = self.run_cli(
            ["plan", "diff", str(v1), "--goal", str(other)]
        )
        self.assertIn("does not belong", stdout)

    def test_plan_diff_single_version(self):
        """A goal with only one plan version has nothing to compare against."""
        oid = test_db.add_objective(
            title="Lonely Goal", target_date="2026-12-15", sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=oid, strategy="Only strategy", goals_hash="g",
            constraints_hash="c", mesocycles=[],
        )
        exit_code, stdout, _ = self.run_cli(["plan", "diff", "--goal", str(oid)])
        self.assertEqual(exit_code, 0)
        self.assertIn("only one plan version", stdout)

    def test_plan_show_all_and_workouts(self):
        """`plan show --all` renders every planned goal; `--workouts` expands each
        mesocycle's sessions, which are otherwise summarised."""
        first = test_db.add_objective(
            title="First Goal", target_date="2026-08-01", sport_type="running",
        )
        second = test_db.add_objective(
            title="Second Goal", target_date="2026-11-01", sport_type="running",
        )
        unplanned = test_db.add_objective(
            title="Unplanned Goal", target_date="2026-09-01", sport_type="running",
        )
        macro = test_db.save_macrocycle(
            objective_id=first, strategy="First plan", goals_hash="g",
            constraints_hash="c",
            mesocycles=[{"name": "Base", "start_date": "2026-06-01",
                         "end_date": "2026-06-28", "focus": "Aerobic volume."}],
        )
        test_db.save_macrocycle(
            objective_id=second, strategy="Second plan", goals_hash="g",
            constraints_hash="c", mesocycles=[],
        )
        save_workout(test_db,
            date="2026-06-02", sport_type="running", title="Easy Run",
            description="Zone 2", duration_minutes=60, rpe=3, tss=45,
            macrocycle_id=macro,
        )

        exit_code, stdout, _ = self.run_cli(["plan", "show", "--all"])
        self.assertEqual(exit_code, 0)
        self.assertIn("First plan", stdout)
        self.assertIn("Second plan", stdout)
        self.assertNotIn("Unplanned Goal", stdout)
        # Summarised by default, expanded with --workouts.
        self.assertIn("1 workouts · 1h00 · load 45", stdout)
        self.assertNotIn("Easy Run", stdout)

        exit_code, stdout, _ = self.run_cli(
            ["plan", "show", "--goal", str(first), "--workouts"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Easy Run", stdout)

        exit_code, stdout, _ = self.run_cli(["plan", "show", "--all", "--goal", str(first)])
        self.assertIn("cannot be combined", stdout)

    def test_plan_show_thresholds_snapshot(self):
        """The threshold anchors a plan was generated against are shown among its inputs."""
        oid = test_db.add_objective(
            title="Threshold Goal", target_date="2026-12-15", sport_type="cycling",
        )
        test_db.save_macrocycle(
            objective_id=oid, strategy="Ride", goals_hash="g", constraints_hash="c",
            mesocycles=[], config_snapshot=json.dumps({"ftp": 220.0, "max_hr": 185.0}),
        )
        exit_code, stdout, _ = self.run_cli(["plan", "show", "--goal", str(oid)])
        self.assertEqual(exit_code, 0)
        self.assertIn("Thresholds considered:", stdout)
        self.assertIn("ftp: 220", stdout)
        self.assertIn("max_hr: 185", stdout)

    def test_plan_show_surfaces_tactical_constraints(self):
        """A tactical (replan=0) constraint reached the LLM prompt even though it never
        fingerprints the plan, so `plan show` must not render it as if nothing was active:
        the plan-shaping list is legitimately empty, but the tactical one still shows."""
        oid = test_db.add_objective(
            title="Tactical Goal", target_date="2026-12-15", sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=oid, strategy="Run", goals_hash="g", constraints_hash="c",
            mesocycles=[],
            constraints_snapshot=json.dumps([]),
            all_constraints_snapshot=json.dumps([{
                "id": 9, "title": "no run Thursday", "start_date": "2026-06-05",
                "end_date": "2026-06-05", "rest": 0, "description": None, "replan": 0,
            }]),
        )
        exit_code, stdout, _ = self.run_cli(["plan", "show", "--goal", str(oid)])
        self.assertEqual(exit_code, 0)
        self.assertIn("Constraints considered (plan-shaping):", stdout)
        self.assertIn("Also active (tactical", stdout)
        self.assertIn("no run Thursday", stdout)

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_accepting_a_plan_with_no_goal_reports_instead_of_crashing(
        self, mock_coach, mock_garmin
    ):
        """Accepting a proposal that names no goal, with nothing upcoming to fall back on,
        used to raise NameError — swallowed by the handler's blanket except and surfaced
        only as an error string."""
        mock_coach.plan_generate.return_value = {
            "strategy": "Unattached strategy",
            "mesocycles": [{"name": "Base", "start_date": "2026-08-01",
                            "end_date": "2026-08-28", "focus": "Aerobic"}],
            "reused": False,
            "goal": None,
        }
        mock_coach.plan_apply.return_value = None

        exit_code, stdout, _ = self.run_cli(["plan", "generate"], input_value="y")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("next_goal", stdout)
        self.assertNotIn("Error during plan generation", stdout)
        self.assertIn("No goal to attach", stdout)
        mock_coach.plan_apply.assert_called_once()
        self.assertIsNone(mock_coach.plan_apply.call_args[0][0])
