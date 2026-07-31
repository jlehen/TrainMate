import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_data.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate_cli.db = test_db


class TestCliData(unittest.TestCase):
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
        # so give them real behavior (None history start = no warm-up suppression)
        # instead of Mocks.
        from trainmate import garmin as real_garmin
        mock_garmin.pmc_history_start.return_value = None
        mock_garmin.pmc_warmup_cutoff_for.side_effect = real_garmin.pmc_warmup_cutoff_for
        mock_garmin.pmc_display_values.side_effect = real_garmin.pmc_display_values
        mock_garmin.load_ratio.side_effect = real_garmin.load_ratio
        test_db.save_metric_cache(
            date="2026-06-03", rhr=50, hrv=75, sleep_score=80, stress=20,
            ctl=60.0, atl=68.4, tsb=-8.4
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

    def test_data_wipe_recomputes_pmc_at_command_layer(self):
        # §4: a dated Garmin wipe must be followed by recompute_derived() at the command
        # layer — deleted load otherwise stays baked into every later day's CTL forever.
        # Exercises the real `data wipe` command, not the db method + recompute directly.
        from trainmate import garmin as real_garmin
        base = datetime(2026, 3, 1).date()
        for i in range(60):
            ds = (base + timedelta(days=i)).isoformat()
            test_db.save_metric_cache(
                date=ds, rhr=50, hrv=70, sleep_score=80, stress=20
            )
            test_db.save_completed_activity(
                activity_id=f"wipe{i}", date=ds, start_time=f"{ds} 09:00:00",
                activity_name="Run", activity_type="running", duration_sec=3600,
                distance_km=10.0, elevation_gain_m=0.0, avg_hr=150, max_hr=170,
                rpe=None, tss=70.0,
            )
        real_garmin.recompute_derived(dbh=test_db)
        later = (base + timedelta(days=59)).isoformat()
        ctl_before = next(
            m for m in test_db.get_metrics_cache() if m["date"] == later
        )["ctl"]

        exit_code, stdout, stderr = self.run_cli([
            "data", "wipe", "--garmin",
            "--from", base.isoformat(),
            "--until", (base + timedelta(days=20)).isoformat(),
            "-y",
        ])
        self.assertEqual(exit_code, 0)
        row = next(
            (m for m in test_db.get_metrics_cache() if m["date"] == later), None
        )
        self.assertIsNotNone(row)
        # Deleted early load no longer inflates a later surviving day's CTL.
        self.assertIsNotNone(row["ctl"])
        self.assertLess(row["ctl"], ctl_before)

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
