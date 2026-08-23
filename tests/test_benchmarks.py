import os
import unittest

from tests.helpers import clear_all_tables, run_cli, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_benchmarks.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)
rebind_test_db(test_db)

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
        self.assertIn("ftp", bm.anchors_for_sport("cycling"))
        self.assertIn("lthr", bm.anchors_for_sport("running"))
        self.assertIn("css", bm.anchors_for_sport("swimming"))
        # Aliases resolve through canonical_sport, so a Garmin spelling works too.
        self.assertIn("ftp", bm.anchors_for_sport("road_biking"))
        self.assertIn("e1rm", bm.anchors_for_sport("strength"))
        # An unknown sport carries no opinion — callers must not read [] as "invalid".
        self.assertEqual(bm.anchors_for_sport("unknown-sport"), [])

class TestBenchmarkDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)
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

    def test_latest_is_newest_by_date_then_id(self):
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="cycling",
            anchor_kind="ftp", value=235, unit="W",
        )
        test_db.add_benchmark_result(
            date="2026-08-01", sport_type="cycling",
            anchor_kind="ftp", value=250, unit="W",
        )
        # A backdated entry does not become "latest".
        test_db.add_benchmark_result(
            date="2026-05-01", sport_type="cycling",
            anchor_kind="ftp", value=200, unit="W",
        )
        self.assertEqual(test_db.get_latest_benchmark("ftp")["value"], 250.0)

    def test_latest_thresholds_one_per_kind(self):
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="cycling",
            anchor_kind="ftp", value=235, unit="W",
        )
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="running",
            anchor_kind="lthr", value=165, unit="bpm",
        )
        test_db.add_benchmark_result(
            date="2026-07-01", sport_type="cycling",
            anchor_kind="ftp", value=250, unit="W",
        )
        self.assertEqual(
            test_db.latest_thresholds(), {"ftp": 250.0, "lthr": 165.0}
        )

    def test_delete(self):
        rid = test_db.add_benchmark_result(
            date="2026-06-01", sport_type="cycling",
            anchor_kind="ftp", value=235, unit="W",
        )
        test_db.delete_benchmark_result(rid)
        self.assertIsNone(test_db.get_latest_benchmark("ftp"))

    def test_workout_benchmark_type_persists_and_preserves(self):
        wid = test_db.save_workout(
            date="2026-08-05", sport_type="cycling", title="FTP Test",
            description="[FTP Test]", benchmark_type="ftp_20min", source="generated",
        )
        self.assertEqual(
            test_db.get_workout_by_id(wid)["benchmark_type"], "ftp_20min"
        )
        # A later same-row save that omits benchmark_type must preserve it (COALESCE).
        test_db.save_workout(
            date="2026-08-05", sport_type="cycling", title="FTP Test v2",
            description="[FTP Test]",
        )
        self.assertEqual(
            test_db.get_workout_by_id(wid)["benchmark_type"], "ftp_20min"
        )

    def test_clear_benchmark_blanks_the_flag_in_place(self):
        """COALESCE must not make the flag unclearable: §4.2's POSTPONE fallback replaces a
        test with an ordinary session on the same row, and that row must stop being a test
        (DESIGN_benchmark_workouts.md §3.1)."""
        wid = test_db.save_workout(
            date="2026-08-05", sport_type="cycling", title="FTP Test",
            description="[FTP Test]", benchmark_type="ftp_20min", source="generated",
        )
        test_db.save_workout(
            date="2026-08-05", sport_type="cycling", title="Easy Spin",
            description="[Easy Spin]", clear_benchmark=True,
        )
        self.assertIsNone(test_db.get_workout_by_id(wid)["benchmark_type"])


class TestEffectiveThresholds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)
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

    def test_effective_overlays_logbook_on_config(self):
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="cycling",
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
                date="2026-06-01", sport_type="cycling",
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
            date="2026-06-01", sport_type="cycling",
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
        rebind_test_db(test_db)
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
        self.assertIn("CYCLING | Functional Threshold Power (FTP): 250 W", out)
        self.assertIn("Benchmark result recorded successfully", out)
        rows = test_db.get_benchmark_results()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["anchor_kind"], "ftp")
        self.assertEqual(rows[0]["sport_type"], "cycling")

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

    def test_record_rejects_implausible_sport_kind(self):
        """`record swimming --ftp 250` is a slip, and a rejected record costs one retyped
        command where a wrong one silently pollutes the logbook (§3.2)."""
        code, out, _ = run_cli(
            ["benchmark", "record", "swimming", "--ftp", "250", "-y"]
        )
        self.assertEqual(code, 1)
        self.assertIn("is not an anchor swimming is tested on", out)
        self.assertIsNone(test_db.get_latest_benchmark("ftp"))

    def test_record_is_quiet_for_plausible_pairs(self):
        pairs = [
            ("cycling", "--ftp", "250"),
            ("cycling", "--lthr", "158"),      # a cyclist's LTHR is real
            ("running", "--mas", "18"),        # so is a runner's MAS
            ("strength", "--e1rm", "120"),     # alias resolves to strength_training
            ("kitesurfing", "--ftp", "200"),   # unknown sport -> no opinion, no warning
        ]
        for sport, flag, value in pairs:
            with self.subTest(sport=sport, flag=flag):
                code, out, _ = run_cli(
                    ["benchmark", "record", sport, flag, value, "-y"]
                )
                # Match the refusal the code actually prints, and confirm the row
                # landed — the point is that a plausible pair is recorded, not refused.
                self.assertEqual(code, 0)
                self.assertNotIn("is not an anchor", out)
                self.assertIsNotNone(test_db.get_latest_benchmark(flag.lstrip("-")))


class TestBenchmarkPlacementGuards(unittest.TestCase):
    """The two deterministic passes around generation (§4.1)."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)
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

    @staticmethod
    def _capture(fn, *args):
        import io
        from unittest.mock import patch
        out = io.StringIO()
        with patch("sys.stdout", out):
            result = fn(*args)
        return result, out.getvalue()

    def test_collision_drops_the_same_sport_session_not_the_benchmark(self):
        workouts = [
            {"date": "2026-08-05", "sport_type": "cycling", "title": "FTP Test",
             "benchmark_type": "ftp_20min"},
            {"date": "2026-08-05", "sport_type": "road_biking", "title": "Z2 spin"},
            {"date": "2026-08-05", "sport_type": "running", "title": "Easy run"},
            {"date": "2026-08-06", "sport_type": "cycling", "title": "Intervals"},
        ]
        kept, out = self._capture(
            coach_service._drop_benchmark_collisions, workouts
        )
        titles = [w["title"] for w in kept]
        # The alias-spelled ride collides and goes; the other sport and the next day stay.
        self.assertEqual(titles, ["FTP Test", "Easy run", "Intervals"])
        self.assertIn("Dropping road_biking session on 2026-08-05", out)

    def test_collision_pass_is_a_no_op_without_benchmarks(self):
        workouts = [
            {"date": "2026-08-05", "sport_type": "cycling", "title": "Z2 spin"},
            {"date": "2026-08-05", "sport_type": "cycling", "title": "Openers"},
        ]
        kept, out = self._capture(
            coach_service._drop_benchmark_collisions, workouts
        )
        self.assertEqual(len(kept), 2)
        self.assertEqual(out, "")

    def _macrocycle_with_boundary(self):
        obj_id = test_db.add_objective(
            title="Gran Fondo", target_date="2026-10-15",
            sport_type="cycling",
        )
        return test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build", goals_hash="g",
            constraints_hash="c",
            mesocycles=[{
                "name": "Base 1", "start_date": "2026-08-03",
                "end_date": "2026-08-30", "focus": "Aerobic",
            }],
        )

    def test_boundary_week_without_a_benchmark_warns(self):
        macro_id = self._macrocycle_with_boundary()
        workouts = [
            {"date": "2026-08-26", "sport_type": "cycling", "title": "Z2"},
            {"date": "2026-08-30", "sport_type": "running", "title": "Long run"},
        ]
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], test_db.get_mesocycles_for_macrocycle(macro_id), "2026-08-03",
        )
        self.assertIn("No benchmark scheduled in the boundary week of 'Base 1'", out)
        self.assertIn("2026-08-24 to 2026-08-30", out)

    def test_boundary_week_with_a_benchmark_is_silent(self):
        macro_id = self._macrocycle_with_boundary()
        workouts = [
            {"date": "2026-08-26", "sport_type": "cycling", "title": "FTP Test",
             "benchmark_type": "ftp_20min"},
            {"date": "2026-08-30", "sport_type": "running", "title": "Long run"},
        ]
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], test_db.get_mesocycles_for_macrocycle(macro_id), "2026-08-03",
        )
        self.assertEqual(out, "")

    def test_rest_constraint_over_the_boundary_week_wins(self):
        macro_id = self._macrocycle_with_boundary()
        workouts = [{"date": "2026-08-26", "sport_type": "cycling", "title": "Z2"}]
        rest = [{
            "title": "Family holiday", "start_date": "2026-08-25",
            "end_date": "2026-08-31", "rest": 1,
        }]
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, rest, test_db.get_mesocycles_for_macrocycle(macro_id), "2026-08-03",
        )
        self.assertEqual(out, "")

    def test_boundary_week_reaching_the_goal_week_is_not_checked(self):
        """A goal-directed macrocycle's last block tapers into the event, so its boundary
        week is the goal's own: no test is asked for there (§4.1)."""
        obj_id = test_db.add_objective(
            title="Hill climb", target_date="2026-09-30",
            sport_type="cycling",
        )
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build", goals_hash="g",
            constraints_hash="c",
            mesocycles=[
                {"name": "Specific", "start_date": "2026-08-31",
                 "end_date": "2026-09-20", "focus": "Climb"},
                {"name": "Taper", "start_date": "2026-09-21",
                 "end_date": "2026-09-30", "focus": "Peak"},
            ],
        )
        # Only the Specific block's boundary (2026-09-20) should be asked about; the Taper
        # block ends on race day itself.
        workouts = [{"date": d, "sport_type": "cycling", "title": "Z2"}
                    for d in ("2026-09-18", "2026-09-25", "2026-09-30")]
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], test_db.get_mesocycles_for_macrocycle(macro_id), "2026-08-31",
        )
        self.assertIn("boundary week of 'Specific'", out)
        self.assertNotIn("Taper", out)

    def test_boundary_week_with_an_already_completed_benchmark_is_silent(self):
        """Regenerating mid-boundary-week must not advise regenerating to recover a test
        the athlete has already done (DESIGN_block_progress.md §4.1)."""
        macro_id = self._macrocycle_with_boundary()
        test_db.save_workout(
            date="2026-08-25", sport_type="cycling", title="FTP Test",
            description="[FTP Test]\n20-min test", benchmark_type="ftp_20min",
            macrocycle_id=macro_id,
        )
        # Regenerating on the 27th: the freshly generated span reaches the block's end (so
        # the boundary week IS checked) but holds no benchmark, the test being two days
        # behind it.
        workouts = [
            {"date": "2026-08-28", "sport_type": "cycling", "title": "Z2"},
            {"date": "2026-08-30", "sport_type": "running", "title": "Long run"},
        ]
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], test_db.get_mesocycles_for_macrocycle(macro_id), "2026-08-27",
        )
        self.assertEqual(out, "")

    def test_a_displaced_future_benchmark_does_not_satisfy_the_boundary_week(self):
        """The stored-workout lookup is bounded below gen_start: the previous plan's future
        rows are still live when this check runs and must not answer for the new plan."""
        macro_id = self._macrocycle_with_boundary()
        test_db.save_workout(
            date="2026-08-28", sport_type="cycling", title="FTP Test",
            description="[FTP Test]\n20-min test", benchmark_type="ftp_20min",
            macrocycle_id=macro_id,
        )
        workouts = [
            {"date": "2026-08-28", "sport_type": "cycling", "title": "Z2"},
            {"date": "2026-08-30", "sport_type": "running", "title": "Long run"},
        ]
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], test_db.get_mesocycles_for_macrocycle(macro_id), "2026-08-27",
        )
        self.assertIn("No benchmark scheduled in the boundary week of 'Base 1'", out)

    def test_recent_planned_test_before_the_span_silences_the_warning(self):
        """A test within MIN_RETEST_DAYS of the boundary means none is due there
        (benchmarks.md §1 floor) — warning would nag toward violating it."""
        macro_id = self._macrocycle_with_boundary()
        test_db.save_workout(
            date="2026-08-10", sport_type="cycling", title="FTP Test",
            description="[FTP Test]\n20-min test", benchmark_type="ftp_20min",
            macrocycle_id=macro_id,
        )
        workouts = [{"date": "2026-08-28", "sport_type": "cycling", "title": "Z2"},
                    {"date": "2026-08-30", "sport_type": "running", "title": "Long run"}]
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], test_db.get_mesocycles_for_macrocycle(macro_id), "2026-08-14",
        )
        self.assertEqual(out, "")

    def test_recent_test_in_the_same_batch_silences_the_warning(self):
        """The floor counts tests this run itself is placing, not only history."""
        macro_id = self._macrocycle_with_boundary()
        workouts = [
            {"date": "2026-08-20", "sport_type": "cycling", "title": "FTP Test",
             "benchmark_type": "ftp_20min"},
            {"date": "2026-08-30", "sport_type": "running", "title": "Long run"},
        ]
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], test_db.get_mesocycles_for_macrocycle(macro_id), "2026-08-03",
        )
        self.assertEqual(out, "")

    def test_measured_logbook_result_silences_but_a_manual_seed_does_not(self):
        """Only a measurement starts the interval clock — a seeded value is a guess."""
        macro_id = self._macrocycle_with_boundary()
        workouts = [{"date": "2026-08-28", "sport_type": "cycling", "title": "Z2"},
                    {"date": "2026-08-30", "sport_type": "running", "title": "Long run"}]
        mesos = test_db.get_mesocycles_for_macrocycle(macro_id)
        seed = test_db.add_benchmark_result(
            date="2026-08-15", sport_type="cycling", anchor_kind="ftp",
            value=220.0, unit="W", source="manual",
        )
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], mesos, "2026-08-14",
        )
        self.assertIn("No benchmark scheduled in the boundary week of 'Base 1'", out)
        test_db.delete_benchmark_result(seed)
        test_db.add_benchmark_result(
            date="2026-08-15", sport_type="cycling", anchor_kind="ftp",
            value=235.0, unit="W", source="test",
        )
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], mesos, "2026-08-14",
        )
        self.assertEqual(out, "")

    def test_boundary_outside_the_generated_span_is_not_checked(self):
        macro_id = self._macrocycle_with_boundary()
        # The span stops well before the 2026-08-30 boundary, so there is nothing to warn
        # about yet — the test simply has not been generated into view.
        workouts = [{"date": "2026-08-05", "sport_type": "cycling", "title": "Z2"}]
        _, out = self._capture(
            coach_service._warn_missing_boundary_benchmarks,
            workouts, [], test_db.get_mesocycles_for_macrocycle(macro_id), "2026-08-03",
        )
        self.assertEqual(out, "")

    def test_anchor_history_names_provenance_and_planned_tests(self):
        """ANCHORS ON RECORD: a manual seed reads as never measured, and a planned test
        before the span is listed so the interval floor can count it (§4.1)."""
        macro_id = self._macrocycle_with_boundary()
        test_db.add_benchmark_result(
            date="2026-07-31", sport_type="cycling", anchor_kind="ftp",
            value=220.0, unit="W", source="manual",
        )
        test_db.save_workout(
            date="2026-08-10", sport_type="cycling", title="FTP Test",
            description="[FTP Test]\n20-min test", benchmark_type="ftp_20min",
            macrocycle_id=macro_id,
        )
        text = coach_service._anchor_history_text("2026-08-14")
        self.assertIn("220 W", text)
        self.assertIn("never measured by a test", text)
        self.assertIn("2026-08-10 ftp_20min", text)


if __name__ == "__main__":
    unittest.main()
