import os
import unittest

from tests.helpers import clear_all_tables, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_benchmarks.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.service.db = test_db
trainmate_cli.db = test_db

from trainmate.coach import coach_service
from trainmate import benchmarks as bm


class TestBenchmarkVocabulary(unittest.TestCase):
    def test_format_value_units(self):
        self.assertEqual(bm.format_value("ftp", 250), "250 W")
        self.assertEqual(bm.format_value("lthr", 165), "165 bpm")
        # Pace kinds render mm:ss.
        self.assertEqual(bm.format_value("threshold_pace", 4.25), "4:15 min/km")
        self.assertEqual(bm.format_value("css", 95), "1:35 sec/100m")

    def test_delta_sign_is_direction_aware(self):
        # Higher FTP is better -> positive.
        self.assertEqual(bm.format_delta("ftp", 250, 235), "+6.4%")
        # A quicker (lower) pace is an improvement -> shown positive, not negative.
        d = bm.format_delta("threshold_pace", 4.0, 4.4)
        self.assertTrue(d.startswith("+"), d)
        self.assertTrue(bm.is_improvement("threshold_pace", 4.0, 4.4))
        self.assertFalse(bm.is_improvement("ftp", 230, 235))

    def test_anchors_for_sport(self):
        self.assertEqual(bm.anchors_for_sport("cycling"), ["ftp"])
        self.assertIn("lthr", bm.anchors_for_sport("running"))
        self.assertEqual(bm.anchors_for_sport("swimming"), ["css"])
        self.assertEqual(bm.anchors_for_sport("unknown-sport"), [])


class TestBenchmarkDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.service.db = test_db
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

    def test_latest_is_newest_by_date_then_id(self):
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="road_biking",
            anchor_kind="ftp", value=235, unit="W",
        )
        test_db.add_benchmark_result(
            date="2026-08-01", sport_type="road_biking",
            anchor_kind="ftp", value=250, unit="W",
        )
        # A backdated entry does not become "latest".
        test_db.add_benchmark_result(
            date="2026-05-01", sport_type="road_biking",
            anchor_kind="ftp", value=200, unit="W",
        )
        self.assertEqual(test_db.get_latest_benchmark("ftp")["value"], 250.0)

    def test_latest_thresholds_one_per_kind(self):
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="road_biking",
            anchor_kind="ftp", value=235, unit="W",
        )
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="running",
            anchor_kind="lthr", value=165, unit="bpm",
        )
        test_db.add_benchmark_result(
            date="2026-07-01", sport_type="road_biking",
            anchor_kind="ftp", value=250, unit="W",
        )
        self.assertEqual(
            test_db.latest_thresholds(), {"ftp": 250.0, "lthr": 165.0}
        )

    def test_delete(self):
        rid = test_db.add_benchmark_result(
            date="2026-06-01", sport_type="road_biking",
            anchor_kind="ftp", value=235, unit="W",
        )
        test_db.delete_benchmark_result(rid)
        self.assertIsNone(test_db.get_latest_benchmark("ftp"))

    def test_workout_benchmark_type_persists_and_preserves(self):
        wid = test_db.save_workout(
            date="2026-08-05", sport_type="road_biking", title="FTP Test",
            description="[FTP Test]", benchmark_type="ftp_20min", source="generated",
        )
        self.assertEqual(
            test_db.get_workout_by_id(wid)["benchmark_type"], "ftp_20min"
        )
        # A later same-row save that omits benchmark_type must preserve it (COALESCE).
        test_db.save_workout(
            date="2026-08-05", sport_type="road_biking", title="FTP Test v2",
            description="[FTP Test]",
        )
        self.assertEqual(
            test_db.get_workout_by_id(wid)["benchmark_type"], "ftp_20min"
        )


class TestEffectiveThresholds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.service.db = test_db
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

    def test_effective_overlays_logbook_on_config(self):
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="road_biking",
            anchor_kind="ftp", value=242, unit="W",
        )
        eff = coach_service.effective_thresholds()
        self.assertEqual(eff["ftp"], 242.0)
        # max_hr comes from config (quasi-fixed physiology).
        self.assertIn("max_hr", eff)

    def test_config_ftp_lthr_are_inert_not_special(self):
        from unittest.mock import patch
        # The code makes no assumption about ftp/lthr in the profile: a value lingering in
        # config is neither required, forbidden, nor stripped — it rides through as ordinary
        # profile data, and a logbook value simply overlays it.
        with patch.dict(
            trainmate.coach.config.data,
            {"user_profile": {"max_hr": 185, "ftp": 999, "lthr": 199}},
        ):
            prof = coach_service._effective_profile()
            self.assertEqual(prof["lthr"], 199)  # rides through, untouched
            # A logbook value overrides the same-named profile key.
            test_db.add_benchmark_result(
                date="2026-06-01", sport_type="road_biking",
                anchor_kind="ftp", value=242, unit="W",
            )
            self.assertEqual(coach_service._effective_profile()["ftp"], 242.0)

    def test_profile_renders_logbook_threshold(self):
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="swimming",
            anchor_kind="css", value=95, unit="sec/100m",
        )
        rendered = coach_service.engine._format_athlete_profile(
            coach_service._effective_profile()
        )
        self.assertIn("Critical Swim Speed", rendered)
        self.assertIn("1:35 sec/100m", rendered)

    def test_cold_start_nudge_only_when_no_threshold(self):
        import io
        from unittest.mock import patch
        # No trainable threshold: nudge fires (max_hr from config does not count).
        out = io.StringIO()
        with patch("sys.stdout", out):
            coach_service._maybe_nudge_no_threshold()
        self.assertIn("No fitness thresholds on record", out.getvalue())
        # Once one is recorded, it goes quiet.
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="road_biking",
            anchor_kind="ftp", value=235, unit="W",
        )
        out = io.StringIO()
        with patch("sys.stdout", out):
            coach_service._maybe_nudge_no_threshold()
        self.assertEqual(out.getvalue(), "")


class TestBenchmarkCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.service.db = test_db
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

    def test_record_confirm_and_cancel(self):
        # Decline: nothing recorded.
        code, out, _ = run_cli(
            ["benchmark", "record", "cycling", "--ftp", "250"],
            input_value="n",
        )
        self.assertEqual(code, 0)
        self.assertIn("Not recorded.", out)
        self.assertEqual(test_db.get_benchmark_results(), [])

        # Accept: recorded, canonicalized sport.
        code, out, _ = run_cli(
            ["benchmark", "record", "cycling", "--ftp", "250"],
            input_value="y",
        )
        self.assertEqual(code, 0)
        # Echoed in the 'benchmark list' format, then the confirmation.
        self.assertIn("ROAD_BIKING | Functional Threshold Power (FTP): 250 W", out)
        self.assertIn("Benchmark result recorded successfully", out)
        rows = test_db.get_benchmark_results()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["anchor_kind"], "ftp")
        self.assertEqual(rows[0]["sport_type"], "road_biking")

    def test_record_yes_skips_prompt_and_flags_replan(self):
        run_cli(["benchmark", "record", "cycling", "--ftp", "235", "-y"])
        code, out, _ = run_cli(
            ["benchmark", "record", "cycling", "--ftp", "260", "-y"]
        )
        self.assertEqual(code, 0)
        # >5% jump surfaces the replan hint.
        self.assertIn("replan band", out)

    def test_record_echo_matches_the_list_line(self):
        """The line `record` echoes is exactly the one `list` shows for that row,
        signed delta against the previous latest included."""
        run_cli(["benchmark", "record", "cycling", "--ftp", "250", "-y"])
        _, rec_out, _ = run_cli(
            ["benchmark", "record", "cycling", "--ftp", "262", "-y"]
        )
        _, list_out, _ = run_cli(["benchmark", "list"])

        echoed = next(l for l in rec_out.splitlines() if l.startswith("ID: "))
        self.assertIn("(+4.8%)", echoed)
        self.assertIn(echoed, list_out)

    def test_record_pace_parses_mmss(self):
        run_cli(
            ["benchmark", "record", "running",
             "--threshold-pace", "4:15", "-y"]
        )
        latest = test_db.get_latest_benchmark("threshold_pace")
        self.assertAlmostEqual(latest["value"], 4.25)

    def test_record_rejects_two_values(self):
        code, out, _ = run_cli(
            ["benchmark", "record", "cycling",
             "--ftp", "250", "--lthr", "165", "-y"]
        )
        self.assertEqual(code, 1)
        self.assertIn("exactly one anchor value", out)

    def test_list_shows_signed_delta(self):
        run_cli(["benchmark", "record", "cycling", "--ftp", "235", "-y"])
        run_cli(["benchmark", "record", "cycling", "--ftp", "250", "-y"])
        code, out, _ = run_cli(["benchmark", "list"])
        self.assertEqual(code, 0)
        self.assertIn("BENCHMARK LOGBOOK", out)
        self.assertIn("+6.4%", out)

    def test_rm(self):
        run_cli(["benchmark", "record", "cycling", "--ftp", "235", "-y"])
        rid = test_db.get_benchmark_results()[0]["id"]
        code, out, _ = run_cli(["benchmark", "rm", str(rid)])
        self.assertEqual(code, 0)
        self.assertIn(f"Removed benchmark result ID {rid}", out)
        self.assertEqual(test_db.get_benchmark_results(), [])

    def test_rm_missing(self):
        code, out, _ = run_cli(["benchmark", "rm", "999"])
        self.assertEqual(code, 1)
        self.assertIn("not found", out)


if __name__ == "__main__":
    unittest.main()
