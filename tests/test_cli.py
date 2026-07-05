import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate_cli.db = test_db


class TestTrainMateCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate_cli.db = test_db

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

    def test_help_and_usage(self):
        exit_code, stdout, stderr = self.run_cli(["--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("TrainMate - Local Training Coach CLI", stdout)
        self.assertIn("goal", stdout)
        self.assertIn("constraint", stdout)
        self.assertIn("workout", stdout)
        self.assertIn("plan", stdout)
        self.assertIn("status", stdout)
        self.assertIn("data", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("push", stdout)

        exit_code, stdout, stderr = self.run_cli(["data", "--help"])
        self.assertEqual(exit_code, 0)
        self.assertIn("pull", stdout)

    def test_help_command_shows_full_command_tree(self):
        exit_code, stdout, stderr = self.run_cli(["help"])
        self.assertEqual(exit_code, 0)
        # top-level commands
        self.assertIn("goal", stdout)
        self.assertIn("workout", stdout)
        self.assertIn("data", stdout)
        # and their sub-commands, which plain --help doesn't show recursively
        self.assertIn("add", stdout)
        self.assertIn("pull", stdout)
        self.assertIn("push", stdout)

    def test_goal_commands(self):
        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== TRAINING OBJECTIVES / GOALS ===", stdout)
        self.assertNotIn("ID:", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "--title", "Zurich Marathon",
            "--date", "2026-10-15",
            "--sport", "running",
            "--desc", "Target sub 3:30",
            "--priority", "1",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal 'Zurich Marathon' added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "--title", "Morning Yoga Flow",
            "--date", "2026-10-20",
            "--sport", "yoga",
            "--desc", "Daily mindfulness and flexibility",
            "--priority", "2",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal 'Morning Yoga Flow' added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "goal", "add",
            "--title", "Hybrid Strength Endurance",
            "--date", "2026-11-30",
            "--sport", "road_biking", "strength_training",
            "--desc", "Aging well routine",
            "--priority", "3",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal 'Hybrid Strength Endurance' added successfully", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Zurich Marathon", stdout)
        self.assertIn("running", stdout)
        self.assertIn("2026-10-15", stdout)
        self.assertIn("Priority: 1", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertIn("Hybrid Strength Endurance", stdout)
        self.assertIn("road_biking,strength_training", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "rm", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Goal with ID 1 removed successfully", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Zurich Marathon", stdout)

    def test_constraint_commands(self):
        exit_code, stdout, stderr = self.run_cli(["constraint", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ATHLETE CONSTRAINTS ===", stdout)

        # Quick capture: positional title, flag-free (no prompts), soft by default.
        exit_code, stdout, stderr = self.run_cli([
            "constraint", "add", "Ibiza Vacation",
            "--start", "2026-07-01", "--end", "2026-07-08",
            "--type", "vacation", "--desc", "50% intensity",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Added constraint [1]: Ibiza Vacation", stdout)

        exit_code, stdout, stderr = self.run_cli(["cons", "list", "--all"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("vacation", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertNotIn("Details:", stdout)

        exit_code, stdout, stderr = self.run_cli(["cons", "list", "--all", "-v"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Details:\n    50% intensity", stdout)

        exit_code, stdout, stderr = self.run_cli(["constraint", "show", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ibiza Vacation", stdout)
        self.assertIn("ID: 1", stdout)
        self.assertIn("Details:\n    50% intensity", stdout)

        exit_code, stdout, stderr = self.run_cli(["constraint", "show", "999"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Constraint with ID 999 not found.", stdout)

        exit_code, stdout, stderr = self.run_cli(["constraint", "rm", "1"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Constraint [1] removed.", stdout)

        exit_code, stdout, stderr = self.run_cli(["cons", "list", "--all"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Ibiza Vacation", stdout)

    @patch("trainmate_cli.calendar_syncer")
    def test_context_add_and_list(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        ids = iter(["evt-1", "evt-2", "evt-3"])
        mock_calendar.add_context_event.side_effect = lambda *a, **k: next(ids)

        exit_code, stdout, _ = self.run_cli([
            "context", "add", "severe", "heatwave",
            "-m", "heat", "--value", "38",
            "--from", "2026-06-25", "--until", "2026-06-27",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("3 days", stdout)
        # One tagged event authored per day in the range.
        self.assertEqual(mock_calendar.add_context_event.call_count, 3)
        rows = test_db.get_daily_context("2026-06-25", "2026-06-27", metric="heat")
        self.assertEqual([r["date"] for r in rows],
                         ["2026-06-25", "2026-06-26", "2026-06-27"])
        self.assertTrue(all(r["value"] == 38.0 for r in rows))
        # A provided label gets the value appended in parentheses.
        self.assertTrue(all(r["text"] == "severe heatwave (38.0)" for r in rows))

        exit_code, stdout, _ = self.run_cli([
            "context", "list", "--from", "2026-06-25", "--until", "2026-06-27"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("heat", stdout)
        self.assertIn("2026-06-26", stdout)

        exit_code, stdout, _ = self.run_cli(["context", "lm"])
        self.assertEqual(exit_code, 0)
        self.assertIn("heat: 3 days", stdout)

    @patch("trainmate_cli.calendar_syncer")
    def test_context_add_no_label_uses_metric_value_form(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        mock_calendar.add_context_event.return_value = "evt-1"
        # No label + a value → "Metric: value" (matching ingested "Alcohol: 2.0").
        self.run_cli(["context", "add", "-m", "alcohol", "--value", "2",
                      "--from", "2026-06-25"], input_value="")
        rows = test_db.get_daily_context("2026-06-25", "2026-06-25", metric="alcohol")
        self.assertEqual(rows[0]["text"], "Alcohol: 2.0")
        # The calendar summary matches what we mirror locally.
        self.assertEqual(
            mock_calendar.add_context_event.call_args.args[3], "Alcohol: 2.0"
        )

    @patch("trainmate_cli.calendar_syncer")
    def test_context_add_label_flag(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        mock_calendar.add_context_event.return_value = "evt-1"
        # --label wins over (and is cleaner than) the positional text.
        exit_code, _, _ = self.run_cli([
            "context", "add", "-m", "heat",
            "--label", "severe heatwave, poor sleep", "--from", "2026-06-25",
        ])
        self.assertEqual(exit_code, 0)
        rows = test_db.get_daily_context("2026-06-25", "2026-06-25", metric="heat")
        self.assertEqual(rows[0]["text"], "severe heatwave, poor sleep")

    @patch("trainmate_cli.calendar_syncer")
    def test_context_add_idempotent_by_date_metric(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        mock_calendar.add_context_event.return_value = "evt-1"

        self.run_cli(["context", "add", "first", "-m", "heat", "--from", "2026-06-25"])
        self.run_cli(["context", "add", "second", "-m", "heat", "--from", "2026-06-25"])

        # Re-adding the same (date, metric) updates the existing event in place.
        last_call = mock_calendar.add_context_event.call_args
        self.assertEqual(last_call.args[-1], "evt-1")  # existing_event_id passed
        rows = test_db.get_daily_context("2026-06-25", "2026-06-25", metric="heat")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "second")

    @patch("trainmate_cli.calendar_syncer")
    def test_context_rm_deletes_calendar_event(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        test_db.upsert_daily_context_by_event("evt-9", "2026-06-25", "heat", None, "hot")
        row = test_db.get_daily_context("2026-06-25", "2026-06-25")[0]

        exit_code, stdout, _ = self.run_cli(["context", "rm", str(row["id"])])
        self.assertEqual(exit_code, 0)
        mock_calendar.delete_event.assert_called_once_with("evt-9")
        self.assertEqual(test_db.get_daily_context("2026-06-25", "2026-06-25"), [])

    @patch("trainmate_cli.calendar_syncer")
    def test_context_rm_refuses_unscoped(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        test_db.upsert_daily_context_by_event("evt-9", "2026-06-25", "heat", None, "hot")
        exit_code, stdout, _ = self.run_cli(["context", "rm"])
        self.assertEqual(exit_code, 1)
        mock_calendar.delete_event.assert_not_called()
        self.assertEqual(len(test_db.get_daily_context()), 1)

    def test_goal_edit_command(self):
        self.run_cli([
            "goal", "add",
            "--title", "Berlin Marathon",
            "--date", "2026-09-27",
            "--sport", "running",
            "--desc", "Sub 3:15 goal",
            "--priority", "2",
        ])

        goals = test_db.get_objectives()
        self.assertEqual(len(goals), 1)
        g_id = goals[0]["id"]

        exit_code, stdout, stderr = self.run_cli([
            "goal", "edit", str(g_id),
            "--title", "Berlin Marathon Elite",
            "--date", "2026-09-28",
            "--sport", "running", "strength_training",
            "--desc", "Sub 3:10 elite goal",
            "--priority", "1",
            "--status", "completed",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Goal with ID {g_id} updated successfully", stdout)

        edited = test_db.get_objective(g_id)
        self.assertEqual(edited["title"], "Berlin Marathon Elite")
        self.assertEqual(edited["target_date"], "2026-09-28")
        self.assertEqual(edited["sport_type"], "running,strength_training")
        self.assertEqual(edited["description"], "Sub 3:10 elite goal")
        self.assertEqual(edited["priority"], 1)
        self.assertEqual(edited["status"], "completed")

        exit_code, stdout, stderr = self.run_cli(["goal", "edit", "999", "--title", "Fail"])
        self.assertEqual(exit_code, 1)
        self.assertIn("Goal with ID 999 not found", stdout)

        exit_code, stdout, stderr = self.run_cli(["goal", "edit", str(g_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn("No fields to update", stdout)

    def test_constraint_edit_command(self):
        self.run_cli([
            "constraint", "add", "Summer Vacation",
            "--start", "2026-08-01", "--end", "2026-08-15",
            "--type", "vacation", "--desc", "No workouts",
        ])

        constraints = test_db.get_constraints()
        c_id = constraints[0]["id"]

        exit_code, stdout, stderr = self.run_cli([
            "constraint", "edit", str(c_id),
            "--title", "Summer Vacation Adapted",
            "--start", "2026-08-02", "--end", "2026-08-16",
            "--type", "trip", "--desc", "Light running only", "--hard",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"Constraint [{c_id}] updated.", stdout)

        edited = test_db.get_constraint(c_id)
        self.assertEqual(edited["title"], "Summer Vacation Adapted")
        self.assertEqual(edited["start_date"], "2026-08-02")
        self.assertEqual(edited["end_date"], "2026-08-16")
        self.assertEqual(edited["type"], "trip")
        self.assertEqual(edited["binding"], "hard")
        self.assertEqual(edited["description"], "Light running only")

        exit_code, stdout, stderr = self.run_cli(
            ["constraint", "edit", "999", "--title", "Fail"]
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("Constraint with ID 999 not found", stdout)

        exit_code, stdout, stderr = self.run_cli(["constraint", "edit", str(c_id)])
        self.assertEqual(exit_code, 0)
        self.assertIn("No fields to update", stdout)

    @patch("trainmate_cli.garmin")
    @patch("trainmate_cli.coach_service")
    def test_workout_commands(self, mock_coach, mock_garmin):
        mock_coach.workout_adapt.return_value = (
            "Metrics are green",
            [{
                "date": "2026-06-03",
                "sport_type": "running",
                "title": "Steady Ride",
                "duration_minutes": 60,
                "rpe": 5,
                "tss": 40.0,
            }],
            [],
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUT SCHEDULE ===", stdout)
        self.assertNotIn("Description:", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "adapt", "--auto"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Evaluating daily Garmin metrics adaptation", stdout)
        self.assertIn("Metrics are green", stdout)
        self.assertIn("Adaptations applied and synced to calendar successfully.", stdout)
        mock_coach.workout_adapt.assert_called_once()
        # With no -m, the athlete message threads through as None.
        self.assertIsNone(mock_coach.workout_adapt.call_args.kwargs.get("message"))

        # -m/--message is forwarded verbatim to the service for this run.
        mock_coach.workout_adapt.reset_mock()
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "adapt", "--auto", "-m", "knee is sore, keep impact low"]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            mock_coach.workout_adapt.call_args.kwargs.get("message"),
            "knee is sore, keep impact low",
        )

        w_id = test_db.save_workout(
            date="2026-06-02",
            sport_type="running",
            title="Interval Session",
            description="5x800m",
            
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "rm", str(w_id), "--reason", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn(
            f"Workout with ID {w_id} ('Interval Session') removed successfully", stdout
        )

    @patch("trainmate_cli.garmin")
    @patch("trainmate_cli.coach_service")
    def test_plan_commands(self, mock_coach, mock_garmin):
        mock_coach.plan_generate.return_value = (
            "Mock Strategy",
            [{"name": "Meso 1", "start_date": "2026-01-01", "end_date": "2026-01-28", "focus": "Base"}],
            False
        )
        mock_coach.workout_generate.return_value = (
            "Test workout reasoning",
            [{"title": "Test Workout"}],
        )

        exit_code, stdout, stderr = self.run_cli(["plan", "generate"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Plan discarded", stdout)
        mock_coach.plan_generate.assert_called_once_with(force=False, auto_apply=False)

        mock_coach.plan_generate.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["plan", "generate", "-f"])
        self.assertEqual(exit_code, 0)
        mock_coach.plan_generate.assert_called_once_with(force=True, auto_apply=False)

        mock_coach.plan_generate.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["plan", "generate", "--force"])
        self.assertEqual(exit_code, 0)
        mock_coach.plan_generate.assert_called_once_with(force=True, auto_apply=False)

        exit_code, stdout, stderr = self.run_cli(["workout", "generate"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUTS GENERATED BY COACH ===", stdout)
        self.assertIn("Test workout reasoning", stdout)
        mock_coach.workout_generate.assert_called_once()

        exit_code, stdout, stderr = self.run_cli(["plan", "show"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No active goals found", stdout)

        obj_id = test_db.add_objective(
            title="London Marathon",
            target_date="2026-09-20",
            sport_type="running",
            priority=1,
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
        self.assertIn("=== ACTIVE MACROCYCLE STRATEGY ===", stdout)
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

    def test_status_command(self):
        test_db.save_metric_cache(
            date="2026-05-31",
            rhr=48, hrv=82, sleep_score=90, stress=15,
            acute_workload=4.0, chronic_workload=3.5, acwr=1.14,
        )
        test_db.save_baseline(
            date="2026-05-31",
            rhr_mean=50.0, rhr_std=1.5,
            hrv_mean=78.0, hrv_std=4.0,
            sleep_mean=82.0, sleep_std=3.0,
        )
        test_db.add_objective(
            title="London Marathon", target_date="2026-09-20",
            sport_type="running", priority=1,
        )
        learning_id = test_db.add_learning("Rest well on Fridays")

        exit_code, stdout, stderr = self.run_cli(["status"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== TRAINMATE ATHLETE STATUS ===", stdout)
        self.assertIn("London Marathon", stdout)
        self.assertIn("Resting HR : 48 bpm", stdout)
        self.assertIn("Overnight HRV: 82 ms", stdout)
        self.assertIn("ACWR       : 1.14", stdout)
        self.assertIn("Baselines (28-day)", stdout)
        # Status shows only a one-line learnings summary; the full text lives under 'learnings'.
        self.assertIn("Coach Learnings:", stdout)
        self.assertIn("1 active", stdout)
        self.assertIn("see 'learnings list'", stdout)
        self.assertNotIn("Rest well on Fridays", stdout)

        # The full text is reachable via the dedicated 'learnings' command.
        exit_code, ln_stdout, _ = self.run_cli(["learnings", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"[{learning_id}|general|tentative]", ln_stdout)
        self.assertIn("Rest well on Fridays", ln_stdout)

        test_db.add_constraint(
            title="Ibiza Trip", start_date="2026-07-01", end_date="2026-08-08",
            binding="soft", type="vacation", description="Rest weeks",
        )

        goals = test_db.get_objectives()
        g_id = goals[0]["id"]
        events = test_db.get_constraints()
        e_id = events[0]["id"]

        exit_code_v, stdout_v, _ = self.run_cli(["status", "-v"])
        self.assertEqual(exit_code_v, 0)
        self.assertIn("Goals:", stdout_v)
        self.assertIn(
            f"- [ACTIVE] ID: {g_id} | London Marathon (running) on 2026-09-20 (Priority: 1)",
            stdout_v,
        )
        self.assertIn("Active Constraints:", stdout_v)
        self.assertIn(
            f"- ID: {e_id} | Ibiza Trip (vacation): 2026-07-01 to 2026-08-08",
            stdout_v,
        )
        self.assertIn("  Details:\n    Rest weeks", stdout_v)

        exit_code_vv, stdout_vv, _ = self.run_cli(["status", "--verbose"])
        self.assertEqual(exit_code_vv, 0)
        self.assertIn("Goals:", stdout_vv)
        self.assertIn(
            f"- [ACTIVE] ID: {g_id} | London Marathon (running) on 2026-09-20 (Priority: 1)",
            stdout_vv,
        )

    @patch("trainmate_cli.calendar_syncer")
    def test_workout_push_command(self, mock_calendar):
        exit_code, stdout, stderr = self.run_cli(["workout", "push"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No new or modified workouts to sync", stdout)
        mock_calendar.sync_multiple.assert_not_called()

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo Run",
            description="30 mins fast", 
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "push"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Syncing 1 workouts to Google Calendar", stdout)
        mock_calendar.sync_multiple.assert_called_once()

    @patch("trainmate_cli.coach_service")
    def test_workout_swap_by_date(self, mock_coach):
        today = datetime.now(timezone.utc).date()
        d1 = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (today + timedelta(days=3)).strftime("%Y-%m-%d")
        mock_coach.workout_swap_validate.return_value = []
        mock_coach.workout_swap_apply.return_value = [
            {"id": 1, "title": "Run A", "date": d2},
            {"id": 2, "title": "Ride B", "date": d1},
        ]
        a = test_db.save_workout(
            date=d1, sport_type="running", title="Run A",
            description="easy", rpe=4, tss=30,
        )
        b = test_db.save_workout(
            date=d2, sport_type="road_biking", title="Ride B",
            description="easy", rpe=4, tss=30,
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", d1, d2, "--reason", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Swapped 2 workout(s) successfully", stdout)
        # Each date's workout is moved to the other date.
        ops, no_sync = mock_coach.workout_swap_apply.call_args[0]
        self.assertCountEqual(ops, [
            {"id": a, "new_date": d2},
            {"id": b, "new_date": d1},
        ])
        self.assertFalse(no_sync)

    @patch("trainmate_cli.coach_service")
    def test_workout_swap_by_id_no_sync(self, mock_coach):
        today = datetime.now(timezone.utc).date()
        d1 = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (today + timedelta(days=3)).strftime("%Y-%m-%d")
        mock_coach.workout_swap_validate.return_value = []
        mock_coach.workout_swap_apply.return_value = []
        a = test_db.save_workout(
            date=d1, sport_type="running", title="Run A",
            description="easy", rpe=4, tss=30,
        )
        b = test_db.save_workout(
            date=d2, sport_type="road_biking", title="Ride B",
            description="easy", rpe=4, tss=30,
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", "--id1", str(a), "--id2", str(b), "--no-sync",
             "--reason", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Calendar sync skipped", stdout)
        ops, no_sync = mock_coach.workout_swap_apply.call_args[0]
        self.assertCountEqual(ops, [
            {"id": a, "new_date": d2},
            {"id": b, "new_date": d1},
        ])
        self.assertTrue(no_sync)

    @patch("trainmate_cli.coach_service")
    def test_workout_swap_warning_declined(self, mock_coach):
        today = datetime.now(timezone.utc).date()
        d1 = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (today + timedelta(days=3)).strftime("%Y-%m-%d")
        mock_coach.workout_swap_validate.return_value = ["Creates 3 consecutive high days"]
        a = test_db.save_workout(
            date=d1, sport_type="running", title="Run A",
            description="easy", rpe=8, tss=90,
        )
        b = test_db.save_workout(
            date=d2, sport_type="road_biking", title="Ride B",
            description="easy", rpe=8, tss=90,
        )
        # Default input is "n": the swap is cancelled and never applied.
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", d1, d2, "--reason", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Swap warnings", stdout)
        self.assertIn("Swap cancelled", stdout)
        mock_coach.workout_swap_apply.assert_not_called()

    @patch("trainmate_cli.coach_service")
    def test_workout_swap_past_date_rejected(self, mock_coach):
        today = datetime.now(timezone.utc).date()
        past = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        future = (today + timedelta(days=2)).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=past, sport_type="running", title="Run A",
            description="easy", rpe=4, tss=30,
        )
        test_db.save_workout(
            date=future, sport_type="road_biking", title="Ride B",
            description="easy", rpe=4, tss=30,
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", past, future, "--reason", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("in the past", stdout)
        mock_coach.workout_swap_apply.assert_not_called()

    def test_workout_swap_missing_args(self):
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", "2026-06-10", "--reason", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Specify two dates", stdout)

    @patch("trainmate_cli.garmin")
    def test_data_pull_command(self, mock_garmin):
        exit_code, stdout, stderr = self.run_cli(["data", "pull"])
        self.assertEqual(exit_code, 0)
        mock_garmin.pull.assert_called_once()

    @patch("trainmate.cli.data.mark_adherence_range")
    @patch("trainmate_cli.garmin")
    def test_data_pull_marks_adherence(self, mock_garmin, mock_mark):
        # A successful pull rides along into the adherence Calendar marking over
        # the pulled range, and reports how many past events were marked.
        mock_mark.return_value = 2
        exit_code, stdout, stderr = self.run_cli(["data", "pull", "--days", "7"])
        self.assertEqual(exit_code, 0)
        mock_garmin.pull.assert_called_once()
        mock_mark.assert_called_once()
        self.assertIn("Marked 2 past Calendar event(s)", stdout)

    @patch("trainmate.cli.data.mark_adherence_range")
    @patch("trainmate_cli.garmin")
    def test_data_pull_no_mark_skips_marking(self, mock_garmin, mock_mark):
        # --no-mark suppresses the ride-along even on a successful pull.
        exit_code, stdout, stderr = self.run_cli(["data", "pull", "--no-mark"])
        self.assertEqual(exit_code, 0)
        mock_garmin.pull.assert_called_once()
        mock_mark.assert_not_called()

    @patch("trainmate.cli.data.mark_adherence_range")
    @patch("trainmate_cli.garmin")
    def test_data_pull_skips_marking_on_failure(self, mock_garmin, mock_mark):
        # If the Garmin pull fails, the ride-along marking is not attempted.
        from trainmate.garmin import GarminAuthRequired
        mock_garmin.GarminAuthRequired = GarminAuthRequired
        mock_garmin.pull.side_effect = RuntimeError("boom")
        exit_code, stdout, stderr = self.run_cli(["data", "pull"])
        self.assertEqual(exit_code, 0)
        mock_mark.assert_not_called()

    def test_invalid_command(self):
        exit_code, stdout, stderr = self.run_cli(["invalidcmd"])
        self.assertEqual(exit_code, 2)
        self.assertIn("invalid choice: 'invalidcmd'", stderr)

    @patch("trainmate_cli.calendar_syncer")
    def test_workout_rm_synced(self, mock_calendar):
        w_id = test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Synced Run",
            description="30 mins", google_event_id="mock_event_123",
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "rm", str(w_id), "--reason", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn(
            "Workout is synced to Google Calendar. Updating calendar event",
            stdout,
        )
        self.assertIn(f"Workout with ID {w_id} ('Synced Run') removed successfully", stdout)
        mock_calendar.sync_workout.assert_called_once()
        synced_workout = mock_calendar.sync_workout.call_args[0][0]
        self.assertEqual(synced_workout['id'], w_id)
        self.assertTrue(synced_workout['removed'])

    @patch("trainmate_cli.calendar_syncer")
    def test_workout_rm_soft_deletes(self, mock_calendar):
        """`workout rm` marks the row removed (kept in DB), hides it from reads, and
        updates its calendar event — but it stays retrievable for the coach."""
        w_id = test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Interval Session",
            description="5x800m", google_event_id="evt-1",
        )
        exit_code, stdout, _ = self.run_cli(
            ["workout", "rm", str(w_id), "--reason", "Travelling for work"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Reason: Travelling for work", stdout)

        # Row is kept, flagged removed, reason stored, and retains calendar event reference.
        row = test_db.get_workout_by_id(w_id)
        self.assertIsNotNone(row)
        self.assertTrue(row["removed"])
        self.assertEqual(row["removed_reason"], "Travelling for work")
        self.assertEqual(row["google_event_id"], "evt-1")

        # Excluded from default reads, retrievable with include_removed=True.
        self.assertEqual(test_db.get_workouts(start_date="2026-06-02", end_date="2026-06-02"), [])
        self.assertEqual(
            len(test_db.get_workouts(
                start_date="2026-06-02", end_date="2026-06-02", include_removed=True
            )),
            1,
        )

        # Removing an already-removed workout is a no-op that does not re-hit the calendar.
        mock_calendar.sync_workout.reset_mock()
        exit_code, stdout, _ = self.run_cli(
            ["workout", "rm", str(w_id), "--reason", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("already removed", stdout)
        mock_calendar.sync_workout.assert_not_called()

        # Verify CLI list command excludes or includes the removed workout depending on --removed.
        exit_code, stdout, _ = self.run_cli(
            ["workout", "list", "--from", "2026-06-02", "--until", "2026-06-02"]
        )
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Interval Session", stdout)

        exit_code, stdout, _ = self.run_cli(
            ["workout", "list", "--from", "2026-06-02", "--until", "2026-06-02", "--removed"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Interval Session", stdout)
        self.assertIn("[REMOVED]", stdout)

    @patch("trainmate_cli.garmin")
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

    def test_wipe_commands_confirmation_flow(self):
        """goal/constraint/plan/data wipe share one confirmation flow: `n` cancels,
        `y` confirms, `-y` skips the prompt. (`workout wipe` is covered separately
        for its calendar side-effect.)"""
        def seed_goal():
            test_db.add_objective(
                title="Wipe Target", target_date="2026-10-15", sport_type="running"
            )
        def count_goal(_):
            return len(test_db.get_objectives())

        def seed_constraint():
            test_db.add_constraint(
                title="Wipe Constraint", start_date="2026-07-01",
                end_date="2026-07-02", binding="soft",
            )
        def count_constraint(_):
            return len(test_db.get_constraints())

        def seed_plan():
            obj_id = test_db.add_objective(
                title="Plan Wipe Obj", target_date="2026-10-15", sport_type="running"
            )
            test_db.save_macrocycle(
                objective_id=obj_id, strategy="Base", goals_hash="ghash",
                constraints_hash="lhash",
                mesocycles=[{
                    "name": "Meso1", "start_date": "2026-06-01",
                    "end_date": "2026-06-28", "focus": "Aerobic",
                }],
            )
            return obj_id
        def count_plan(obj_id):
            return 1 if test_db.get_macrocycle_for_objective(obj_id) else 0

        def seed_data():
            test_db.save_metric_cache(
                date="2026-05-31", rhr=48, hrv=82, sleep_score=90, stress=15
            )
        def count_data(_):
            return len(test_db.get_metrics_cache())

        cases = [
            (["goal", "wipe"], seed_goal, count_goal,
             "All training objectives wiped successfully."),
            (["constraint", "wipe"], seed_constraint, count_constraint,
             "All constraints wiped successfully."),
            (["plan", "wipe"], seed_plan, count_plan,
             "All periodization plans wiped successfully."),
            (["data", "wipe"], seed_data, count_data,
             "Wiped Garmin metrics, baselines, and activities and "
             "ingested daily-context signals."),
        ]

        for args, seed, count, message in cases:
            with self.subTest(command=args[0]):
                # `n` cancels the wipe.
                token = seed()
                self.assertEqual(count(token), 1)
                exit_code, stdout, _ = self.run_cli(args, input_value="n")
                self.assertEqual(exit_code, 0)
                self.assertIn("Wipe cancelled.", stdout)
                self.assertEqual(count(token), 1)

                # `y` confirms.
                exit_code, stdout, _ = self.run_cli(args, input_value="y")
                self.assertEqual(exit_code, 0)
                self.assertIn(message, stdout)
                self.assertEqual(count(token), 0)

                # `-y` skips the prompt entirely.
                token = seed()
                self.assertEqual(count(token), 1)
                exit_code, _, _ = self.run_cli(args + ["-y"])
                self.assertEqual(exit_code, 0)
                self.assertEqual(count(token), 0)

    @patch("trainmate_cli.calendar_syncer")
    def test_workout_wipe(self, mock_calendar):
        test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", google_event_id="ge_1",
        )
        test_db.save_workout(
            date="2026-06-03", sport_type="running", title="Run 2",
            description="30 mins", 
        )
        self.assertEqual(len(test_db.get_workouts()), 2)

        exit_code, stdout, stderr = self.run_cli(["workout", "wipe"], input_value="n")
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_workouts()), 2)
        mock_calendar.delete_workout_event.assert_not_called()

        exit_code, stdout, stderr = self.run_cli(["workout", "wipe"], input_value="y")
        self.assertEqual(exit_code, 0)
        self.assertIn("All workouts wiped successfully.", stdout)
        self.assertEqual(len(test_db.get_workouts()), 0)
        mock_calendar.delete_workout_event.assert_called_once_with("ge_1")

        mock_calendar.reset_mock()
        test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", google_event_id="ge_2",
        )
        exit_code, stdout, stderr = self.run_cli(["workout", "wipe", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_workouts()), 0)
        mock_calendar.delete_workout_event.assert_called_once_with("ge_2")

    def test_data_wipe_clears_all_evidence_tables(self):
        # `data wipe` spans three tables — metrics, baselines, and completed
        # activities — not just the metrics cache. (The confirm/`-y` flow itself is
        # covered by test_wipe_commands_confirmation_flow.)
        test_db.save_metric_cache(
            date="2026-05-31", rhr=48, hrv=82, sleep_score=90, stress=15
        )
        test_db.save_baseline(
            date="2026-05-31", rhr_mean=50.0, rhr_std=1.5,
            hrv_mean=78.0, hrv_std=4.0, sleep_mean=82.0, sleep_std=3.0,
        )
        test_db.save_completed_activity(
            activity_id="act_1", date="2026-05-31", start_time="10:00",
            activity_name="Run", activity_type="running", duration_sec=1800,
            distance_km=5.0, elevation_gain_m=50, avg_hr=150, max_hr=170,
            rpe=5, tss=30.0,
        )

        exit_code, stdout, stderr = self.run_cli(["data", "wipe", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_metrics_cache()), 0)
        self.assertIsNone(test_db.get_baseline("2026-05-31"))
        self.assertEqual(len(test_db.get_completed_activities()), 0)

    def test_workout_list_filters(self):
        today_date = datetime.now(timezone.utc).date()
        today_str = today_date.strftime("%Y-%m-%d")
        tomorrow_str = (today_date + timedelta(days=1)).strftime("%Y-%m-%d")
        past_str = (today_date - timedelta(days=5)).strftime("%Y-%m-%d")
        future_str = (today_date + timedelta(days=10)).strftime("%Y-%m-%d")

        test_db.save_workout(
            date=today_str, sport_type="running", title="Today Run",
            description="30 mins", 
        )
        test_db.save_workout(
            date=tomorrow_str, sport_type="road_biking", title="Tomorrow Ride",
            description="60 mins", 
        )
        test_db.save_workout(
            date=past_str, sport_type="yoga", title="Past Yoga",
            description="15 mins", 
        )
        test_db.save_workout(
            date=future_str, sport_type="strength_training", title="Future Lift",
            description="45 mins", 
        )

        goal_id = test_db.add_objective(
            title="Berlin Marathon",
            target_date=(today_date + timedelta(days=20)).strftime("%Y-%m-%d"),
            sport_type="running",
            priority=1,
            status="active",
        )
        test_db.save_macrocycle(
            objective_id=goal_id,
            strategy="Base strategy",
            goals_hash="ghash",
            constraints_hash="lhash",
            mesocycles=[{
                "name": "Base Building",
                "start_date": (today_date - timedelta(days=2)).strftime("%Y-%m-%d"),
                "end_date": (today_date + timedelta(days=5)).strftime("%Y-%m-%d"),
                "focus": "Aerobic conditioning",
            }],
        )

        macro = test_db.get_macrocycle_for_objective(goal_id)
        mesos = test_db.get_mesocycles_for_macrocycle(macro["id"])
        meso_id = mesos[0]["id"]

        exit_code, stdout, stderr = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "list", "--type", "running"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Today Run", stdout)
        self.assertNotIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "list", "--days", "2"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "--from", tomorrow_str, "--until", tomorrow_str
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "--mesocycle", str(meso_id)
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "--until-mesocycle", str(meso_id)
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "--goal", str(goal_id)
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "list", "--from-mesocycle"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertIn("Future Lift", stdout)

    def test_workout_list_shows_repeat_adapt_count(self):
        """A session eased once reads [ADAPTED]; eased again reads [ADAPTED ×2]."""
        today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo",
            description="orig", adaptation_summary="block too hard",
            modification_reason="eased", adapted_at="2026-06-17T08:00:00+00:00",
        )
        exit_code, stdout, _ = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("[ADAPTED]", stdout)
        self.assertNotIn("[ADAPTED ×", stdout)
        # Lifecycle line: creation stamp always shown, last-adapted stamp when eased.
        self.assertIn("Planned:", stdout)
        self.assertIn("Last adapted: 2026-06-17 08:00", stdout)

        # Second easing of the same slot bumps the count.
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo",
            description="easier", adaptation_summary="still fatigued",
            modification_reason="eased again", adapted_at="2026-06-18T08:00:00+00:00",
        )
        exit_code, stdout, _ = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("[ADAPTED ×2]", stdout)

    def test_workout_compare(self):
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        yesterday_str = yesterday.strftime("%Y-%m-%d")
        today_str = today.strftime("%Y-%m-%d")

        # Planned run yesterday (will be matched), planned run today (missed)
        test_db.save_workout(
            date=yesterday_str, sport_type="running", title="Easy Run",
            description="30 mins", duration_minutes=30, rpe=4, tss=20,
        )
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo Run",
            description="45 mins", duration_minutes=45, rpe=7, tss=50,
        )
        # Complete yesterday's run (matching load/duration — no discrepancy expected)
        test_db.save_completed_activity(
            activity_id="act_cmp_1",
            date=yesterday_str,
            start_time=f"{yesterday_str} 08:00:00",
            activity_name="Morning Run",
            activity_type="running",
            duration_sec=1800.0,
            distance_km=5.0,
            elevation_gain_m=50.0,
            avg_hr=140,
            max_hr=160,
            rpe=4,
            tss=20.0,
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "compare", "--days", "2"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUT COMPARE ===", stdout)
        self.assertIn("Easy Run", stdout)
        self.assertIn("Morning Run", stdout)
        self.assertIn("Tempo Run", stdout)
        self.assertIn("(none — missed)", stdout)
        self.assertIn("=== DISCREPANCIES ===", stdout)
        self.assertIn("Complete Miss", stdout)

        # Date range with no data → empty message
        exit_code, stdout, stderr = self.run_cli([
            "workout", "compare", "--from", "2020-01-01", "--until", "2020-01-02"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("No planned workouts or completed activities found", stdout)

    @patch("trainmate_cli.coach_service")
    def test_data_bootstrap_command(self, mock_coach):
        learning_id = test_db.add_learning("Athlete responds well to high sleep score")

        mock_coach.data_bootstrap.return_value = {
            "macrocycle_summary": "Simulated base building results",
            "inferred_macrocycle": {
                "overall_focus": "aerobic base building",
                "start_date": "2026-01-01",
                "end_date": "2026-03-31"
            },
            "inferred_mesocycles": [
                {
                    "name": "Base Building Phase",
                    "start_date": "2026-01-01",
                    "end_date": "2026-02-15",
                    "focus_detected": "Volume",
                    "average_weekly_tss": 300,
                    "estimated_consistency": "High"
                }
            ],
            "physiological_insights": [
                "HRV was stable during peak volume."
            ],
            "learning_updates": [
                {"op": "add", "text": "Responds well to volume"},
                {
                    "op": "reinforce",
                    "id": learning_id,
                    "confidence": "established"
                }
            ]
        }

        exit_code, stdout, stderr = self.run_cli([
            "data", "bootstrap", "--from", "2026-01-01", "--until", "2026-03-31",
            "--context", "Felt good"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== HISTORICAL WORKOUT ANALYSIS REPORT ===", stdout)
        self.assertIn("Macrocycle Focus: aerobic base building", stdout)
        self.assertIn("Base Building Phase", stdout)
        self.assertIn("HRV was stable during peak volume", stdout)
        self.assertIn("Coach Observations (Saved to learnings):", stdout)
        self.assertIn("Responds well to volume", stdout)
        self.assertIn("reinforced", stdout)
        self.assertIn("Athlete responds well to high sleep score", stdout)
        mock_coach.data_bootstrap.assert_called_once_with(
            from_date_str="2026-01-01",
            until_date_str="2026-03-31",
            days=None,
            weeks=None,
            context="Felt good",
            force=False,
            inspect_only=False,
            no_pull=False,
            force_pull=False,
            auto=False,
        )

        mock_coach.data_bootstrap.reset_mock()
        exit_code, stdout, stderr = self.run_cli([
            "data", "bootstrap", "--from", "2026-01-01", "--until", "2026-03-31",
            "--inspect-only"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Coach Observations (NOT saved — inspect mode):", stdout)
        mock_coach.data_bootstrap.assert_called_once_with(
            from_date_str="2026-01-01",
            until_date_str="2026-03-31",
            days=None,
            weeks=None,
            context=None,
            force=False,
            inspect_only=True,
            no_pull=False,
            force_pull=False,
            auto=False,
        )

    @patch("trainmate_cli.garmin")
    def test_data_show_metrics_command(self, mock_garmin):
        # garmin is mocked (to stub ensure_data); the PMC read helpers are pure DB reads,
        # so give them a real cutoff (None = no warm-up suppression) instead of a Mock.
        mock_garmin.pmc_warmup_cutoff.return_value = None
        test_db.save_metric_cache(
            date="2026-06-03", rhr=50, hrv=75, sleep_score=80, stress=20,
            acute_workload=4.0, chronic_workload=3.5, acwr=1.14
        )
        test_db.save_baseline(
            date="2026-06-03", rhr_mean=50.0, rhr_std=1.5,
            hrv_mean=78.0, hrv_std=4.0, sleep_mean=82.0, sleep_std=3.0
        )

        exit_code, stdout, stderr = self.run_cli([
            "data", "show-metrics", "--from", "2026-06-01", "--until", "2026-06-05"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ATHLETE METRICS", stdout)
        self.assertIn("2026-06-03", stdout)
        mock_garmin.ensure_data.assert_called_once_with(
            "2026-06-01", "2026-06-05", force=False
        )

        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli([
            "data", "show-metrics", "--days", "3", "--no-pull"
        ])
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_not_called()

        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli([
            "data", "show-metrics", "--all"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ATHLETE METRICS (All Time) ===", stdout)
        self.assertIn("2026-06-03", stdout)
        mock_garmin.ensure_data.assert_not_called()

    @patch("trainmate_cli.garmin")
    def test_data_show_activities_command(self, mock_garmin):
        test_db.save_completed_activity(
            activity_id="act_show_1", date="2026-06-03", start_time="09:00",
            activity_name="Morning Ride", activity_type="road_biking", duration_sec=3600,
            distance_km=30.0, elevation_gain_m=100.0, avg_hr=130, max_hr=150,
            rpe=4, tss=50.0
        )
        test_db.save_completed_activity(
            activity_id="act_show_2", date="2026-06-04", start_time="08:00",
            activity_name="Morning Run", activity_type="running", duration_sec=1800,
            distance_km=5.0, elevation_gain_m=30.0, avg_hr=140, max_hr=160,
            rpe=5, tss=20.0
        )

        exit_code, stdout, stderr = self.run_cli([
            "data", "show-activities", "--from", "2026-06-01", "--until", "2026-06-05"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== COMPLETED ACTIVITIES", stdout)
        self.assertIn("Morning Ride", stdout)
        self.assertIn("Morning Run", stdout)
        self.assertIn("Summary: 2 activities | Duration: 1h 30m | Distance: 35.0 km | "
                      "Elevation: 130 m | TSS: 70.0", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "data", "show-activities", "--from", "2026-06-01", "--until", "2026-06-05",
            "--type", "running"
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Morning Ride", stdout)
        self.assertIn("Morning Run", stdout)

        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli([
            "data", "show-activities", "-a"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== COMPLETED ACTIVITIES (All Time) ===", stdout)
        self.assertIn("Morning Ride", stdout)
        self.assertIn("Morning Run", stdout)
        mock_garmin.ensure_data.assert_not_called()

    @patch("trainmate_cli.garmin")
    @patch("trainmate_cli.coach_service")
    def test_no_pull_behavior_across_commands(self, mock_coach, mock_garmin):
        # 1. workout compare without --no-pull
        exit_code, stdout, stderr = self.run_cli(["workout", "compare", "--days", "3"])
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_called_once()

        # 2. workout compare with --no-pull
        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "compare", "--days", "3", "--no-pull"]
        )
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_not_called()

        # 3. status with --no-pull
        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["status", "--no-pull"])
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_not_called()

        # 4. plan generate with --no-pull
        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli(["plan", "generate", "--no-pull"])
        self.assertEqual(exit_code, 0)
        mock_garmin.ensure_data.assert_not_called()

        # 5. data bootstrap with --no-pull
        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli(
            ["data", "bootstrap", "--from", "2026-06-01", "--no-pull"]
        )
        self.assertEqual(exit_code, 0)
        mock_coach.data_bootstrap.assert_called_once()
        self.assertTrue(mock_coach.data_bootstrap.call_args[1].get("no_pull"))

    def test_llm_model_override(self):
        from trainmate.openrouter import openrouter_client
        original_model = openrouter_client.model
        try:
            exit_code, _, _ = self.run_cli([
                "--llm-model", "google/gemini-2.5-pro",
                "goal", "list"
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(openrouter_client.model, "google/gemini-2.5-pro")
        finally:
            openrouter_client.model = original_model


    def test_plan_versions_and_show_version(self):
        """`plan versions` lists active + superseded versions; `plan show --version`
        renders a specific superseded version (see DESIGN_plan_rollback.md)."""
        oid = test_db.add_objective(
            title="Versioned Goal", target_date="2026-12-15",
            sport_type="running", priority=1,
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
        self.assertIn(f"ID {v1}", stdout)
        self.assertIn(f"ID {v2}", stdout)
        self.assertIn("active", stdout)
        self.assertIn("superseded", stdout)

        # Showing the superseded version renders its strategy under a superseded header.
        exit_code, stdout, _ = self.run_cli(["plan", "show", "--version", str(v1)])
        self.assertEqual(exit_code, 0)
        self.assertIn("SUPERSEDED", stdout)
        self.assertIn("First strategy alpha", stdout)

        # A version id from another goal is rejected.
        other = test_db.add_objective(
            title="Other Goal", target_date="2027-01-15",
            sport_type="running", priority=1,
        )
        exit_code, stdout, _ = self.run_cli(
            ["plan", "show", "--goal", str(other), "--version", str(v1)]
        )
        self.assertIn("does not belong", stdout)


class TestDashlessOptionTranslator(unittest.TestCase):
    """Network-appliance-style dashless options (`workout adapt message "..." no-pull`)
    are rewritten back into `--flag` form by translate_dashless_argv before argparse,
    so both syntaxes share one parser definition."""

    def _parser(self):
        # A miniature tree mirroring the real shapes the translator must handle:
        # a sub-command level, a single-value option, a boolean flag, an
        # nargs="+" (comma-list) option, and an nargs="?" optional-value option.
        import argparse
        p = argparse.ArgumentParser()
        subs = p.add_subparsers(dest="command")
        w = subs.add_parser("workout", aliases=["w"])
        wsubs = w.add_subparsers(dest="subcommand")
        adapt = wsubs.add_parser("adapt", aliases=["a"])
        adapt.add_argument("-m", "--message", dest="message")
        adapt.add_argument("--no-pull", action="store_true", dest="no_pull")
        add = wsubs.add_parser("add")
        add.add_argument("--sport", nargs="+")
        add.add_argument("--title")
        lst = wsubs.add_parser("list")
        lst.add_argument("--mesocycle", type=int, nargs="?", const=-1, dest="meso_id")
        lst.add_argument("--type", dest="sport_type")
        return p

    def _xlate(self, tokens):
        import trainmate_cli
        return trainmate_cli.translate_dashless_argv(self._parser(), tokens)

    def test_flag_and_value_keywords(self):
        # `w a message "..." no-pull` → recurse aliases, expand value + boolean.
        self.assertEqual(
            self._xlate(["w", "a", "message", "feeling sluggish lately", "no-pull"]),
            ["w", "a", "--message", "feeling sluggish lately", "--no-pull"],
        )

    def test_multivalue_comma_split(self):
        self.assertEqual(
            self._xlate(["workout", "add", "sport", "running,hiking", "title", "Big Day"]),
            ["workout", "add", "--sport", "running", "hiking", "--title", "Big Day"],
        )

    def test_optional_value_peek(self):
        # Bare `mesocycle` keeps its const default (no value consumed)...
        self.assertEqual(
            self._xlate(["workout", "list", "mesocycle"]),
            ["workout", "list", "--mesocycle"],
        )
        # ...takes a following plain value...
        self.assertEqual(
            self._xlate(["workout", "list", "mesocycle", "5"]),
            ["workout", "list", "--mesocycle", "5"],
        )
        # ...but does NOT swallow a following keyword.
        self.assertEqual(
            self._xlate(["workout", "list", "mesocycle", "type", "running"]),
            ["workout", "list", "--mesocycle", "--type", "running"],
        )

    def test_single_value_binds_even_when_value_collides_with_keyword(self):
        # A value equal to a keyword name (a workout literally titled "message") is
        # still bound as the value, because single-value options consume unconditionally.
        self.assertEqual(
            self._xlate(["workout", "adapt", "message", "message"]),
            ["workout", "adapt", "--message", "message"],
        )

    def test_dashed_syntax_passes_through_untouched(self):
        # Classic --flag invocations are byte-identical after translation.
        tokens = ["workout", "adapt", "--message", "hi", "--no-pull"]
        self.assertEqual(self._xlate(tokens), tokens)
        self.assertEqual(
            self._xlate(["workout", "add", "--sport", "running", "hiking", "--title", "X"]),
            ["workout", "add", "--sport", "running", "hiking", "--title", "X"],
        )

    def test_canonical_option_uses_longest_spelling(self):
        # `message` resolves to the long form even though `-m` is also registered.
        self.assertEqual(
            self._xlate(["workout", "adapt", "m", "hello"]),
            ["workout", "adapt", "--message", "hello"],
        )


class TestDashlessEndToEnd(unittest.TestCase):
    """End-to-end: dashless argv flows through real main() and reaches the handlers."""

    @classmethod
    def setUpClass(cls):
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate_cli.db = test_db

    def setUp(self):
        clear_all_tables(test_db)

    def run_cli(self, args, input_value="n"):
        return run_cli(args, input_value)

    @patch("trainmate_cli.garmin")
    @patch("trainmate_cli.coach_service")
    def test_workout_adapt_message_and_no_pull(self, mock_coach, mock_garmin):
        mock_coach.workout_adapt.return_value = ("ok", [], [])
        exit_code, _, _ = self.run_cli(
            ["w", "a", "auto", "message", "feeling sluggish lately", "no-pull"]
        )
        self.assertEqual(exit_code, 0)
        # The athlete message threaded through verbatim...
        self.assertEqual(
            mock_coach.workout_adapt.call_args.kwargs.get("message"),
            "feeling sluggish lately",
        )
        # ...and `no-pull` reached ensure_recent_data → no Garmin pull.
        mock_garmin.ensure_data.assert_not_called()

    def test_goal_add_comma_list_and_collision(self):
        # Comma list expands to two sports.
        exit_code, stdout, _ = self.run_cli([
            "goal", "add", "title", "Marathon", "date", "2026-10-15",
            "sport", "running,strength_training", "priority", "1",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("added successfully", stdout)
        g = test_db.get_objectives()[0]
        self.assertEqual(g["sport_type"], "running,strength_training")
        self.assertEqual(g["title"], "Marathon")

        # A value colliding with a keyword name ("date") is still bound as the value.
        exit_code, _, _ = self.run_cli([
            "goal", "add", "title", "date", "date", "2026-11-01", "sport", "running",
        ])
        self.assertEqual(exit_code, 0)
        titled_date = [o for o in test_db.get_objectives() if o["title"] == "date"]
        self.assertEqual(len(titled_date), 1)
        self.assertEqual(titled_date[0]["target_date"], "2026-11-01")


if __name__ == "__main__":
    unittest.main()

