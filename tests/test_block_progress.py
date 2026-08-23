"""The mid-block progress section of the `workout generate` prompt
(DESIGN_block_progress.md)."""
import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables, rebind_test_db, save_workout

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_block_progress.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)
rebind_test_db(test_db)

from trainmate import intensity
from trainmate.coach import coach_service
from trainmate.coach.engine.workouts import (
    _block_composition_task, _block_progress_task,
)

# Build 1 runs Monday 2026-07-06 through Sunday 2026-08-02 — four whole weeks, so the
# Monday-week buckets line up with the block and no edge week is partial by accident.
BLOCK_START, BLOCK_END = "2026-07-06", "2026-08-02"


class TestBlockProgressContext(unittest.TestCase):
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
        self.macro_id = self._block()

    @staticmethod
    def _block(start: str = BLOCK_START, end: str = BLOCK_END) -> int:
        obj_id = test_db.add_objective(
            title="Gran Fondo", target_date="2026-10-15",
            sport_type="cycling",
        )
        return test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build", goals_hash="g", constraints_hash="c",
            mesocycles=[{
                "name": "Build 1", "start_date": start, "end_date": end,
                "focus": "threshold development",
            }],
        )

    @staticmethod
    def _context(as_of: str, gen_start: str):
        """Just the rendered text — the `has_intensity` gate has its own tests below."""
        return coach_service._block_progress_context(as_of, gen_start)[0]

    def _planned(self, date: str, tss: float, **kwargs) -> int:
        return save_workout(test_db,
            date=date, sport_type=kwargs.pop("sport_type", "cycling"),
            title=kwargs.pop("title", "Threshold"),
            description="[Threshold]\n4x8min", duration_minutes=60, rpe=7, tss=tss,
            source="generated", macrocycle_id=self.macro_id, **kwargs,
        )

    def _done(self, date: str, tss: float, activity_id: str) -> None:
        test_db.save_completed_activity(
            activity_id=activity_id, date=date, start_time=f"{date}T07:00:00",
            activity_name="Ride", activity_type="cycling", duration_sec=3600,
            distance_km=30.0, elevation_gain_m=200.0, avg_hr=140, max_hr=170,
            rpe=7, tss=tss,
        )

    def _two_weeks_trained(self) -> None:
        """Weeks 1 and 2 planned at 200 load each; week 1 fully done, week 2 half-done."""
        for i, date in enumerate(("2026-07-07", "2026-07-09")):
            self._planned(date, 100.0)
            self._done(date, 100.0, f"w1-{i}")
        self._planned("2026-07-14", 100.0)
        self._planned("2026-07-16", 100.0)
        self._done("2026-07-14", 100.0, "w2-0")

    # ------------------------------------------------------------------ gating

    def test_none_when_the_block_has_not_started(self):
        """gen_start on the block's first day: generate is writing the whole block, so
        there is no fulfilled part to continue from."""
        self.assertIsNone(
            self._context(BLOCK_START, BLOCK_START)
        )

    def test_none_when_today_falls_outside_every_block(self):
        """`get_active_mesocycle` falls back to a future/first block, which would describe
        training that never happened — so the guard must reject it."""
        self.assertIsNone(
            self._context("2026-06-01", "2026-06-01")
        )

    def test_none_when_the_elapsed_part_holds_no_data(self):
        """A block under way but with nothing recorded yet renders no section rather than a
        bare header."""
        self.assertIsNone(
            self._context("2026-07-22", "2026-07-22")
        )

    # ------------------------------------------------------------- week lines

    def test_elapsed_weeks_report_planned_against_actual(self):
        self._two_weeks_trained()
        text = self._context("2026-07-22", "2026-07-22")
        self.assertIsNotNone(text)
        # The header states what divided the numbers, reusing intensity.format_header.
        self.assertIn("Build 1", text)
        self.assertIn('focus "threshold development"', text)
        self.assertIn("2 completed weeks of 4", text)
        # Week 1 fully executed, week 2 half — the adherence gap generate could not see.
        self.assertIn("week of 2026-07-06: planned 200, actual 200 (100%)", text)
        self.assertIn("week of 2026-07-13: planned 200, actual 100 (50%)", text)
        # A Monday with no session is not the plan starting mid-week.
        self.assertNotIn("only part of this week", text)
        # Prompt literals wrap at 100 (AGENTS.md), tables included.
        self.assertTrue(
            all(len(line) <= 100 for line in text.splitlines()),
            max(text.splitlines(), key=len),
        )

    def test_a_week_the_block_straddles_is_flagged_incomparable(self):
        """A block starting mid-week compares a part-week of plan against a whole week of
        training, and no honest percentage comes from that pair."""
        clear_all_tables(test_db)
        # Block starts on a Wednesday, so its first Monday-week is only partly its own.
        self.macro_id = self._block(start="2026-07-08", end="2026-08-02")
        self._planned("2026-07-09", 100.0)
        self._done("2026-07-09", 100.0, "a")
        self._done("2026-07-06", 90.0, "before-the-block")
        self._planned("2026-07-14", 100.0)
        self._done("2026-07-14", 100.0, "b")
        text = self._context("2026-07-22", "2026-07-22")
        self.assertIn("week of 2026-07-06", text)
        self.assertIn("[block covers only part of this week]", text)
        # The whole-week that follows is comparable and carries no flag.
        self.assertIn("week of 2026-07-13: planned 100, actual 100 (100%)", text)

    def test_the_in_progress_week_is_raw_and_never_extrapolated(self):
        """DESIGN_intensity_distribution.md §9.3: raw load beside the elapsed day count."""
        self._two_weeks_trained()
        self._planned("2026-07-20", 60.0)
        self._done("2026-07-20", 60.0, "w3-0")
        text = self._context("2026-07-22", "2026-07-22")
        self.assertIn(
            "week of 2026-07-20 (in progress, 2 of 7 days): "
            "planned 60 so far, actual 60",
            text,
        )
        # No projection of the part-week onto a full week.
        self.assertNotIn("210", text)

    def test_a_week_the_plan_never_covered_says_so(self):
        """Training inside the block on days no plan spoke for is informational, matching
        `adherence.py`'s precedent — it must not read as 0% adherence."""
        self._two_weeks_trained()
        # An unplanned week 3 session, with no planned row anywhere in that week.
        self._done("2026-07-20", 80.0, "w3-unplanned")
        text = self._context("2026-07-22", "2026-07-22")
        self.assertIn("no plan covered this week", text)

    def test_gen_start_after_today_counts_the_preserved_day_as_history(self):
        """When today's session is already completed generate starts tomorrow, so today is
        history and the window must include it (else the header and the weeks disagree)."""
        self._two_weeks_trained()
        self._planned("2026-07-22", 90.0)
        self._done("2026-07-22", 90.0, "today")
        text = self._context("2026-07-22", "2026-07-23")
        self.assertIn("3 of 7 days", text)
        self.assertIn("actual 90", text)
        # The header counts the same days the week lines do.
        self.assertIn("plus 3 days", text)

    # -------------------------------------------------------- benchmark lines

    def test_a_completed_test_is_named_with_what_it_measured(self):
        self._two_weeks_trained()
        wid = self._planned(
            "2026-07-15", 70.0, title="FTP Test", benchmark_type="ftp_20min"
        )
        test_db.add_benchmark_result(
            date="2026-07-15", sport_type="cycling", anchor_kind="ftp",
            value=271, unit="W", workout_id=wid,
        )
        text = self._context("2026-07-22", "2026-07-22")
        self.assertIn("Fitness tests this block has already run:", text)
        self.assertIn("2026-07-15: ftp_20min (cycling)", text)
        self.assertIn("Functional Threshold Power (FTP) 271 W", text)

    def test_a_test_performed_but_never_recorded_still_counts(self):
        """De-duplication keys on the planned session, not the logbook: a test the athlete
        did without recording the result must not be scheduled twice."""
        self._two_weeks_trained()
        self._planned("2026-07-15", 70.0, title="FTP Test", benchmark_type="ftp_20min")
        text = self._context("2026-07-22", "2026-07-22")
        self.assertIn("2026-07-15: ftp_20min (cycling) — no result recorded", text)

    def test_an_ad_hoc_logbook_row_is_reported_too(self):
        """A result recorded with no planned test behind it — the athlete tested off-plan."""
        self._two_weeks_trained()
        test_db.add_benchmark_result(
            date="2026-07-17", sport_type="running", anchor_kind="lthr",
            value=168, unit="bpm",
        )
        text = self._context("2026-07-22", "2026-07-22")
        self.assertIn(
            "2026-07-17: Lactate Threshold HR (LTHR) 168 bpm recorded (no planned test)",
            text,
        )

    def test_a_test_still_ahead_is_not_reported_as_run(self):
        """The window stops at gen_start: a benchmark in the part being re-planned has not
        happened, and claiming it had would suppress the very test generate must place."""
        self._two_weeks_trained()
        self._planned(
            "2026-07-30", 70.0, title="FTP Test", benchmark_type="ftp_20min"
        )
        text = self._context("2026-07-22", "2026-07-22")
        self.assertNotIn("ftp_20min", text)


class TestBlockCompositionContext(unittest.TestCase):
    """The intensity half — §9.2a's amendment: generate is the periodization consumer, so
    it gets the measured distribution, what was prescribed, and the block-over-block delta."""

    setUpClass = TestBlockProgressContext.setUpClass
    tearDownClass = TestBlockProgressContext.tearDownClass
    _done = TestBlockProgressContext._done

    def setUp(self):
        clear_all_tables(test_db)
        obj_id = test_db.add_objective(
            title="Gran Fondo", target_date="2026-10-15", sport_type="cycling",
        )
        self.macro_id = test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build", goals_hash="g", constraints_hash="c",
            mesocycles=[
                {"name": "Base 3", "start_date": "2026-06-08", "end_date": "2026-07-05",
                 "focus": "aerobic volume"},
                {"name": "Build 1", "start_date": BLOCK_START, "end_date": BLOCK_END,
                 "focus": "threshold development"},
            ],
        )

    def _zoned(self, date: str, zones, activity_id: str) -> None:
        test_db.save_completed_activity(
            activity_id=activity_id, date=date, start_time=f"{date}T07:00:00",
            activity_name="Ride", activity_type="cycling", duration_sec=sum(zones),
            distance_km=40.0, elevation_gain_m=300.0, avg_hr=140, max_hr=175, rpe=7,
            tss=100.0,
            zone1_sec=zones[0], zone2_sec=zones[1], zone3_sec=zones[2],
            zone4_sec=zones[3], zone5_sec=zones[4],
        )

    def _prescribed(self, date: str, zones) -> int:
        return save_workout(test_db,
            date=date, sport_type="cycling", title="Threshold",
            description="[Threshold]\n4x8min", duration_minutes=int(sum(zones) / 60),
            rpe=7, tss=100.0, source="generated", macrocycle_id=self.macro_id,
            planned_zone_currency="hr", planned_zone_sec=list(zones) + [None, None],
        )

    def _two_weeks_zoned(self) -> None:
        """Two whole weeks of Build 1: prescribed mostly Z2 with 20 min Z4, executed with
        the easy work drifted up into Z3."""
        for i, date in enumerate(
            ("2026-07-07", "2026-07-09", "2026-07-14", "2026-07-16")
        ):
            self._prescribed(date, [600, 2400, 300, 1200, 0])
            self._zoned(date, [400, 1500, 1800, 900, 60], f"z{i}")

    def test_measured_and_prescribed_tables_sit_side_by_side(self):
        self._two_weeks_zoned()
        text, has_intensity = coach_service._block_progress_context(
            "2026-07-22", "2026-07-22"
        )
        self.assertTrue(has_intensity)
        self.assertIn("Intensity distribution, per week over 2 completed weeks", text)
        self.assertIn("What the plan PRESCRIBED over the same weeks", text)
        # Both halves of the section are present: the zone tables and the week loads.
        self.assertIn("Weeks already trained", text)
        # Exactly one header — block_report's stands in for the section's own.
        self.assertEqual(text.count('focus "threshold development"'), 1)

    def test_the_preceding_block_supplies_the_periodization_delta(self):
        self._two_weeks_zoned()
        # Four whole weeks of easy work in the block before this one.
        for i, date in enumerate(
            ("2026-06-09", "2026-06-11", "2026-06-16", "2026-06-18",
             "2026-06-23", "2026-06-25", "2026-06-30", "2026-07-02")
        ):
            self._zoned(date, [900, 4200, 600, 120, 0], f"p{i}")
        text, _ = coach_service._block_progress_context("2026-07-22", "2026-07-22")
        self.assertIn("Change vs Base 3", text)

    def test_no_delta_when_the_block_is_the_first_of_its_plan(self):
        clear_all_tables(test_db)
        obj_id = test_db.add_objective(
            title="Gran Fondo", target_date="2026-10-15", sport_type="cycling",
        )
        self.macro_id = test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build", goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": "Build 1", "start_date": BLOCK_START,
                         "end_date": BLOCK_END, "focus": "threshold development"}],
        )
        self._two_weeks_zoned()
        text, _ = coach_service._block_progress_context("2026-07-22", "2026-07-22")
        self.assertNotIn("Change vs", text)

    def test_has_intensity_is_false_without_zone_recordings(self):
        """The gate must follow the tables: an athlete with no HR/power data gets the
        volume half of the section and none of the composition instructions."""
        for i, date in enumerate(("2026-07-07", "2026-07-14")):
            self._prescribed(date, [600, 2400, 300, 1200, 0])
            self._done(date, 100.0, f"bare{i}")
        text, has_intensity = coach_service._block_progress_context(
            "2026-07-22", "2026-07-22"
        )
        self.assertIsNotNone(text)
        self.assertIn("Weeks already trained", text)
        self.assertFalse(has_intensity)

    def test_prompt_width_is_respected_across_the_whole_section(self):
        self._two_weeks_zoned()
        text, _ = coach_service._block_progress_context("2026-07-22", "2026-07-22")
        for line in text.splitlines():
            self.assertLessEqual(len(line), intensity.PROMPT_WIDTH, msg=repr(line))


class TestBlockProgressReachesThePrompt(unittest.TestCase):
    """The threading end to end: service -> engine -> both halves of the LLM call."""

    @patch("trainmate.coach.engine.openrouter_client")
    def _prompts(self, block_progress, has_intensity, mock_client):
        mock_client.complete.return_value = {"reasoning": "ok", "workouts": []}
        coach_service.engine._workout_generate_logic(
            objectives=[], constraints=[], today_str="2026-07-22", guidelines="",
            profile={}, strategy="Build", meso_text="  - Build 1", learnings="",
            num_days=14, block_progress=block_progress,
            block_has_intensity=has_intensity,
        )
        return mock_client.complete.call_args[0]

    def test_both_halves_carry_it(self):
        system, user = self._prompts("Build 1 — 2 completed weeks of 4", False)
        # The instruction rides in the system prompt's TASK...
        self.assertIn("CONTINUING A BLOCK ALREADY UNDER WAY", system)
        # ...and the data in the user content, under the name the TASK refers to.
        self.assertIn("BLOCK PROGRESS SO FAR", user)
        self.assertIn("Build 1 — 2 completed weeks of 4", user)

    def test_a_clean_block_start_is_unchanged(self):
        """No block progress -> neither half mentions it, so starting a block cleanly
        produces the prompt it always did."""
        system, user = self._prompts(None, False)
        self.assertNotIn("CONTINUING A BLOCK", system)
        self.assertNotIn("BLOCK PROGRESS", user)

    def test_composition_section_follows_the_zone_tables(self):
        """Gate discipline: the composition instructions quote the zone tables, so they
        appear only when those tables have rows."""
        without, _ = self._prompts("Build 1 — ...", False)
        self.assertNotIn("JUDGING THE BLOCK'S COMPOSITION", without)
        with_zones, _ = self._prompts("Build 1 — ...", True)
        self.assertIn("JUDGING THE BLOCK'S COMPOSITION", with_zones)
        # Both progress sections coexist — continuity and composition are separate calls.
        self.assertIn("CONTINUING A BLOCK ALREADY UNDER WAY", with_zones)

    def test_the_flag_alone_cannot_conjure_the_section(self):
        """has_intensity is meaningless without the data it describes."""
        system, _ = self._prompts(None, True)
        self.assertNotIn("JUDGING THE BLOCK'S COMPOSITION", system)


class TestBlockProgressTask(unittest.TestCase):
    """The prompt section is gated on the data, so a clean block start is unchanged."""

    def test_absent_without_data(self):
        self.assertEqual(_block_progress_task(None), "")
        self.assertEqual(_block_progress_task(""), "")

    def test_names_the_data_section_and_bounds_benchmark_placement(self):
        task = _block_progress_task("Build 1 — ...")
        self.assertIn("BLOCK PROGRESS SO FAR", task)
        self.assertIn("BENCHMARK PLACEMENT", task)
        self.assertIn("deload", task)
        # Wraps at 100 like every other prompt literal (AGENTS.md).
        self.assertTrue(all(len(line) <= 100 for line in task.splitlines()))

    def test_composition_task_needs_both_the_data_and_the_tables(self):
        self.assertEqual(_block_composition_task(None, True), "")
        self.assertEqual(_block_composition_task("Build 1", False), "")
        self.assertNotEqual(_block_composition_task("Build 1", True), "")

    def test_composition_task_states_the_attribution_rule(self):
        """The whole point of §9.2a: a block measuring off-focus has two opposite causes,
        and only one of them is generate's to fix."""
        task = _block_composition_task("Build 1", True)
        self.assertIn("PRESCRIBED", task)
        # Names adapt's half, so generate does not also start rewriting prescriptions.
        self.assertIn("workout adapt", task)
        # And refuses to reshape the block around the athlete's deviation.
        self.assertIn("rewards the drift", task)
        # The HR caveat, so a power-less block is not judged too soft on heart rate alone.
        self.assertIn("HR-only", task)
        self.assertTrue(all(len(line) <= 100 for line in task.splitlines()))


if __name__ == "__main__":
    unittest.main()
