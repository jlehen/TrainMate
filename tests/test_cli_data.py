import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_data.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class TestCliData(unittest.TestCase):
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
    def test_data_pull_command(self, mock_garmin):
        exit_code, stdout, stderr = self.run_cli(["data", "pull"])
        self.assertEqual(exit_code, 0)
        mock_garmin.pull.assert_called_once()

    @patch("trainmate.runtime.garmin")
    def test_data_pull_reports_what_landed_even_in_chat(self, mock_garmin):
        # `pull`'s step narration is a terminal-only aside, so the summary it RETURNS is
        # the whole answer here — without it a chat front-end would render "(no output)"
        # (DESIGN_output_verbosity.md §3.1).
        mock_garmin.pull.return_value = "Garmin 2026-06-01..2026-06-02: 3 activities, 2 days"
        os.environ["TRAINMATE_FRONTEND"] = "json"
        try:
            exit_code, stdout, stderr = self.run_cli(["data", "pull", "--no-mark"])
        finally:
            os.environ.pop("TRAINMATE_FRONTEND", None)
        self.assertEqual(exit_code, 0)
        self.assertIn("Garmin 2026-06-01..2026-06-02: 3 activities, 2 days", stdout)

    @patch("trainmate.cli.data.mark_adherence_range")
    @patch("trainmate.runtime.garmin")
    def test_data_pull_marks_adherence(self, mock_garmin, mock_mark):
        # A successful pull rides along into the adherence Calendar marking over
        # the pulled range, and reports how many past events were marked.
        mock_mark.return_value = 2
        exit_code, stdout, stderr = self.run_cli(["data", "pull", "-d", "7d"])
        self.assertEqual(exit_code, 0)
        mock_garmin.pull.assert_called_once()
        mock_mark.assert_called_once()
        self.assertIn("Marked 2 past Calendar event(s)", stdout)

    @patch("trainmate.cli.data.mark_adherence_range")
    @patch("trainmate.runtime.garmin")
    def test_data_pull_no_mark_skips_marking(self, mock_garmin, mock_mark):
        # --no-mark suppresses the ride-along even on a successful pull.
        exit_code, stdout, stderr = self.run_cli(["data", "pull", "--no-mark"])
        self.assertEqual(exit_code, 0)
        mock_garmin.pull.assert_called_once()
        mock_mark.assert_not_called()

    @patch("trainmate.cli.data.mark_adherence_range")
    @patch("trainmate.runtime.garmin")
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

    @patch("trainmate.runtime.coach_service")
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
            "data", "bootstrap", "-d", "2026-01-01..2026-03-31",
            "--context", "Felt good"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== HISTORICAL WORKOUT ANALYSIS REPORT ===", stdout)
        self.assertIn("Macrocycle Focus (2026-01-01 to 2026-03-31):", stdout)
        self.assertIn("aerobic base building", stdout)
        self.assertIn("Base Building Phase", stdout)
        self.assertIn("HRV was stable during peak volume", stdout)
        self.assertIn("Coach Observations (Saved to learnings):", stdout)
        self.assertIn("Responds well to volume", stdout)
        self.assertIn("reinforced", stdout)
        self.assertIn("Athlete responds well to high sleep score", stdout)
        mock_coach.data_bootstrap.assert_called_once_with(
            from_date_str="2026-01-01",
            until_date_str="2026-03-31",
            context="Felt good",
            force=False,
            inspect_only=False,
            no_pull=False,
            force_pull=False,
            auto=False,
        )

        mock_coach.data_bootstrap.reset_mock()
        exit_code, stdout, stderr = self.run_cli([
            "data", "bootstrap", "-d", "2026-01-01..2026-03-31",
            "--inspect-only"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Coach Observations (NOT saved — inspect mode):", stdout)
        mock_coach.data_bootstrap.assert_called_once_with(
            from_date_str="2026-01-01",
            until_date_str="2026-03-31",
            context=None,
            force=False,
            inspect_only=True,
            no_pull=False,
            force_pull=False,
            auto=False,
        )

    def test_analysis_report_wraps_llm_prose_to_the_client_width(self):
        # Every prose field in the bootstrap/reflect report comes from the LLM at
        # unbounded length; none may run past the client's wrap width (AGENTS.md).
        from trainmate.cli.data import _render_analysis_report
        from trainmate.util import visible_len

        learning_id = test_db.add_learning(
            "Long-run durability is the limiter: pace decays sharply beyond 90 minutes "
            "even at conversational effort, consistently across the last three blocks."
        )
        result = {
            "inferred_macrocycle": {
                "overall_focus": "Aerobic base rebuild with a late shift toward "
                                 "threshold-supported marathon specificity",
                "start_date": "2025-09-01",
                "end_date": "2026-08-01",
            },
            "macrocycle_summary": "Twelve months of steadily rising aerobic volume "
                                  "interrupted by two illness gaps.",
            "inferred_mesocycles": [{
                "name": "Extensive Endurance Accumulation (autumn)",
                "start_date": "2025-09-01",
                "end_date": "2025-11-15",
                "focus_detected": "High-volume low-intensity accumulation with weekly long "
                                  "runs progressing from 90 to 135 minutes and no structured "
                                  "threshold work",
                "average_weekly_tss": 410,
                "estimated_consistency": "High",
            }],
            "physiological_insights": [
                "Aerobic decoupling on long runs fell from 8% to 4% over the block, which "
                "points to genuine durability gains rather than pacing discipline alone.",
            ],
            "learning_updates": [
                {"op": "add",
                 "text": "Responds well to two quality sessions per week but shows elevated "
                         "fatigue whenever a third is added in the same seven days.",
                 "sports": "running", "evidence": [3, 4, 5]},
                {"op": "revise", "id": learning_id,
                 "text": "Durability limiter has shifted later: decay now appears beyond "
                         "110 minutes rather than 90.",
                 "sports": "running"},
                {"op": "reinforce", "id": learning_id, "evidence": [6]},
            ],
        }

        for width in ("48", "80"):
            os.environ["TRAINMATE_WRAP_WIDTH"] = width
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _render_analysis_report(result, False)
                out = buf.getvalue()
            finally:
                del os.environ["TRAINMATE_WRAP_WIDTH"]
            self.assertNotIn("Error rendering workout analysis", out)
            for line in out.split("\n"):
                self.assertLessEqual(visible_len(line), int(width), msg=repr(line))

    def test_unreadable_learning_deltas_are_shown_not_silently_dropped(self):
        """The kimi-k3 shape: every delta key prefixed, so no op is recognized. The block
        must not render empty under a 'Saved to learnings' header
        (DESIGN_backward_evaluation.md §13)."""
        from trainmate.cli.data import _render_analysis_report

        result = {
            "macrocycle_summary": "A real reconstruction.",
            "learning_updates": [
                {">op": "add", ">text": "Absorbs volume well"},
                "not even an object",
            ],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            _render_analysis_report(result, False)
        out = buf.getvalue()
        self.assertIn("none saved", out)
        self.assertNotIn("Saved to learnings", out)
        self.assertEqual(out.count("unreadable update"), 2)

    def test_a_readable_delta_still_reports_as_saved(self):
        from trainmate.cli.data import _render_analysis_report

        result = {
            "macrocycle_summary": "A real reconstruction.",
            "learning_updates": [{"op": "add", "text": "Absorbs volume well"}],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            _render_analysis_report(result, False)
        out = buf.getvalue()
        self.assertIn("Saved to learnings", out)
        self.assertNotIn("unreadable update", out)

    def _seed_reconstruction(self, horizon, name, window_start, window_end):
        test_db.save_analysis_cache(
            horizon=horizon, fingerprint=f"fp-{horizon}",
            window_start=window_start, window_end=window_end,
            reconstruction={
                "inferred_macrocycle": {
                    "overall_focus": f"{name} focus",
                    "start_date": window_start, "end_date": window_end,
                },
                "inferred_mesocycles": [{
                    "name": name, "start_date": window_start, "end_date": window_end,
                    "focus_detected": "Volume", "average_weekly_tss": 300,
                    "estimated_consistency": "High",
                }],
                "physiological_insights": [f"{name} insight"],
            },
        )

    def test_data_show_analysis_renders_the_stored_reconstruction(self):
        # Read-only: renders the stored slot, names where it came from, and never
        # reaches the coach service (no LLM call, unlike bootstrap --inspect-only).
        self._seed_reconstruction("long", "Base Block", "2026-01-01", "2026-03-31")
        with patch("trainmate.runtime.coach_service") as mock_coach:
            exit_code, stdout, stderr = self.run_cli(["data", "show-analysis"])
        self.assertEqual(exit_code, 0)
        self.assertIn("data bootstrap", stdout)
        self.assertIn("window 2026-01-01 to 2026-03-31", stdout)
        self.assertIn("Base Block", stdout)
        self.assertIn("Base Block insight", stdout)
        mock_coach.data_bootstrap.assert_not_called()

    def test_data_show_analysis_alias_and_short_horizon(self):
        # The two slots are separately addressable; `san` reaches the same command.
        self._seed_reconstruction("long", "Base Block", "2026-01-01", "2026-03-31")
        self._seed_reconstruction("short", "Recent Week", "2026-04-01", "2026-04-07")

        exit_code, stdout, stderr = self.run_cli(["data", "san"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Base Block", stdout)

        exit_code, stdout, stderr = self.run_cli(["data", "show-analysis", "--short"])
        self.assertEqual(exit_code, 0)
        self.assertIn("data reflect", stdout)
        self.assertIn("Recent Week", stdout)
        self.assertNotIn("Base Block", stdout)

    def test_data_show_analysis_flags_evidence_the_slot_predates(self):
        # The slot is only refreshed by a re-run, so activities past its window are
        # absent from the picture; silence there would read as "this is current".
        self._seed_reconstruction("long", "Base Block", "2026-01-01", "2026-03-31")
        test_db.save_completed_activity(
            activity_id="act_after", date="2026-04-02", start_time="10:00",
            activity_name="Ride", activity_type="cycling", duration_sec=3600,
            distance_km=30.0, elevation_gain_m=200, avg_hr=140, max_hr=165,
            rpe=5, tss=60.0,
        )
        exit_code, stdout, stderr = self.run_cli(["data", "show-analysis"])
        self.assertEqual(exit_code, 0)
        flat = " ".join(stdout.split())
        self.assertIn("1 activity since 2026-03-31 post-dates this analysis", flat)
        # Bootstrap reads the backlog once, so this slot falling behind is by design, not a
        # defect to repair: the nudge names the command that does follow training since.
        self.assertIn("data reflect", flat)
        self.assertIn("data show-analysis --short", flat)
        self.assertNotIn("data bootstrap --force", flat)

        # The provenance and staleness lines are this command's own, outside the shared
        # renderer the wrap test covers, and must hold the client width too (AGENTS.md).
        from trainmate.util import visible_len
        os.environ["TRAINMATE_WRAP_WIDTH"] = "48"
        try:
            exit_code, stdout, stderr = self.run_cli(["data", "show-analysis"])
        finally:
            del os.environ["TRAINMATE_WRAP_WIDTH"]
        for line in stdout.split("\n"):
            self.assertLessEqual(visible_len(line), 48, msg=repr(line))

    def test_data_show_analysis_short_slot_points_at_a_plain_reflect(self):
        # Reflect's window starts at its watermark, so a plain re-run picks the newer
        # activities up; --force only re-pays for evidence the slot already covers.
        self._seed_reconstruction("short", "Recent Week", "2026-04-01", "2026-04-07")
        test_db.save_completed_activity(
            activity_id="act_after_reflect", date="2026-04-09", start_time="10:00",
            activity_name="Ride", activity_type="cycling", duration_sec=3600,
            distance_km=30.0, elevation_gain_m=200, avg_hr=140, max_hr=165,
            rpe=5, tss=60.0,
        )
        exit_code, stdout, stderr = self.run_cli(["data", "show-analysis", "--short"])
        self.assertEqual(exit_code, 0)
        flat = " ".join(stdout.split())
        self.assertIn("1 activity since 2026-04-07 post-dates this analysis", flat)
        self.assertIn("data reflect", flat)
        self.assertNotIn("--force", flat)

    def test_data_show_analysis_without_a_stored_reconstruction(self):
        exit_code, stdout, stderr = self.run_cli(["data", "show-analysis"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No long-horizon reconstruction stored", stdout)
        self.assertIn("data bootstrap", stdout)

    @patch("trainmate.runtime.garmin")
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
            "data", "show-metrics", "-d", "2026-06-01..2026-06-05"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== ATHLETE METRICS", stdout)
        self.assertIn("2026-06-03", stdout)
        mock_garmin.ensure_data.assert_called_once_with(
            "2026-06-01", "2026-06-05", force=False
        )

        mock_garmin.ensure_data.reset_mock()
        exit_code, stdout, stderr = self.run_cli([
            "data", "show-metrics", "-d", "3d", "--no-pull"
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
            "-d", f"{base.isoformat()}..{(base + timedelta(days=20)).isoformat()}",
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

    @patch("trainmate.runtime.garmin")
    def test_data_show_activities_command(self, mock_garmin):
        test_db.save_completed_activity(
            activity_id="act_show_1", date="2026-06-03", start_time="09:00",
            activity_name="Morning Ride", activity_type="cycling", duration_sec=3600,
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
            "data", "show-activities", "-d", "2026-06-01..2026-06-05"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== COMPLETED ACTIVITIES", stdout)
        self.assertIn("Morning Ride", stdout)
        self.assertIn("Morning Run", stdout)
        self.assertIn("Summary: 2 activities | Duration: 1h 30m | Distance: 35.0 km | "
                      "Elevation: 130 m | TSS: 70.0", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "data", "show-activities", "-d", "2026-06-01..2026-06-05",
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

    # -------------------------------------- DESIGN_intensity_distribution.md §9.7

    def _zoned_activities(self):
        # A ride with both a meter and a strap, and a run with HR only.
        test_db.save_completed_activity(
            activity_id="z_ride", date="2026-06-03", start_time="09:00",
            activity_name="Gravel Ride", activity_type="gravel_cycling",
            duration_sec=3600, distance_km=30.0, elevation_gain_m=100.0,
            avg_hr=130, max_hr=150, rpe=None, tss=48.0,
            zone1_sec=600, zone2_sec=2400, zone3_sec=500, zone4_sec=0, zone5_sec=0,
            power_zone1_sec=500, power_zone2_sec=2200, power_zone3_sec=600,
            power_zone4_sec=100, power_zone5_sec=0, power_zone6_sec=0,
            power_zone7_sec=0,
        )
        test_db.save_completed_activity(
            activity_id="z_run", date="2026-06-04", start_time="08:00",
            activity_name="Morning Run", activity_type="running", duration_sec=1800,
            distance_km=5.0, elevation_gain_m=30.0, avg_hr=140, max_hr=160,
            rpe=None, tss=22.0,
            zone1_sec=200, zone2_sec=1300, zone3_sec=200, zone4_sec=0, zone5_sec=0,
        )

    @patch("trainmate.runtime.garmin")
    def test_load_column_carries_its_provenance(self, mock_garmin):
        """The highest-value fact missing from this view was not the breakdown, it was
        where the TSS came from — §9.6's `!` explained at source."""
        self._zoned_activities()
        _, stdout, _ = self.run_cli(["data", "show-activities", "-a"])
        self.assertIn("(pwr)", stdout)   # the ride: power zones win
        self.assertIn("(hr)", stdout)    # the run: hrTSS, coverage adequate

    @patch("trainmate.runtime.garmin")
    def test_type_filter_is_alias_aware(self, mock_garmin):
        """`--type cycling` used to miss every alias, so the athlete filtered for their
        cycling and saw a fraction of it."""
        self._zoned_activities()
        _, stdout, _ = self.run_cli([
            "data", "show-activities", "-a", "--type", "cycling"
        ])
        self.assertIn("Gravel Ride", stdout)
        self.assertNotIn("Morning Run", stdout)

    @patch("trainmate.runtime.garmin")
    def test_zones_renders_one_row_per_activity_and_currency(self, mock_garmin):
        """§6's prohibition kept structural: the two views of the same time are separate
        rows, never adjacent columns inviting addition."""
        self._zoned_activities()
        _, stdout, _ = self.run_cli(["data", "show-activities", "-a", "--zones"])
        self.assertIn("[pwr]", stdout)
        self.assertIn("[HR]", stdout)
        self.assertEqual(stdout.count("GRAVEL_CYCLING"), 2)  # one per currency
        self.assertEqual(stdout.count("RUNNING"), 1)         # HR only
        self.assertIn("Cov", stdout)
        self.assertIn("two views of the SAME time", stdout)  # NEVER_SUM_NOTE
        self.assertNotIn("Avg Watts", stdout)                # swapped, not widened

    @patch("trainmate.runtime.garmin")
    def test_csv_carries_every_zone_column_with_no_flag(self, mock_garmin):
        self._zoned_activities()
        _, stdout, _ = self.run_cli(["data", "show-activities", "-a", "--csv"])
        header = stdout.strip().splitlines()[0]
        for col in ["zone1_sec", "zone5_sec", "power_zone1_sec", "power_zone7_sec",
                    "hr_coverage", "power_coverage", "load", "load_method", "tss"]:
            self.assertIn(col, header)

    @patch("trainmate.runtime.garmin")
    def test_show_metrics_csv_empty_cells_for_null_pmc(self, mock_garmin):
        # §6.2: NULL/suppressed PMC values emit EMPTY CSV cells, never 0, so downstream
        # parsing can't read a zero as data.
        from trainmate import garmin as real_garmin
        mock_garmin.pmc_history_start.return_value = None
        mock_garmin.pmc_warmup_cutoff_for.side_effect = real_garmin.pmc_warmup_cutoff_for
        mock_garmin.pmc_display_values.side_effect = real_garmin.pmc_display_values
        mock_garmin.load_ratio.side_effect = real_garmin.load_ratio
        test_db.save_metric_cache(
            date="2026-06-03", rhr=50, hrv=75, sleep_score=80, stress=20,
        )
        exit_code, stdout, stderr = self.run_cli(["data", "show-metrics", "--all", "--csv"])
        self.assertEqual(exit_code, 0)
        header = stdout.splitlines()[0]
        self.assertTrue(header.endswith("ctl,atl,tsb,atl_ctl_ratio"))
        row = next(l for l in stdout.splitlines() if l.startswith("2026-06-03"))
        # Four empty cells, not zeros — the ratio is NULL whenever its inputs are.
        self.assertTrue(row.endswith(",,,,"), row)
