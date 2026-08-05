"""The mid-block progress section of the `workout generate` prompt
(DESIGN_block_progress.md)."""
import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_block_progress.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.service.db = test_db
trainmate_cli.db = test_db

from trainmate.coach import coach_service
from trainmate.coach.engine.workouts import _block_progress_task

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
        self.macro_id = self._block()

    @staticmethod
    def _block(start: str = BLOCK_START, end: str = BLOCK_END) -> int:
        obj_id = test_db.add_objective(
            title="Gran Fondo", target_date="2026-10-15",
            sport_type="cycling", priority=1,
        )
        return test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build", goals_hash="g", constraints_hash="c",
            mesocycles=[{
                "name": "Build 1", "start_date": start, "end_date": end,
                "focus": "threshold development",
            }],
        )

    def _planned(self, date: str, tss: float, **kwargs) -> int:
        return test_db.save_workout(
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
            coach_service._block_progress_context(BLOCK_START, BLOCK_START)
        )

    def test_none_when_today_falls_outside_every_block(self):
        """`get_active_mesocycle` falls back to a future/first block, which would describe
        training that never happened — so the guard must reject it."""
        self.assertIsNone(
            coach_service._block_progress_context("2026-06-01", "2026-06-01")
        )

    def test_none_when_the_elapsed_part_holds_no_data(self):
        """A block under way but with nothing recorded yet renders no section rather than a
        bare header."""
        self.assertIsNone(
            coach_service._block_progress_context("2026-07-22", "2026-07-22")
        )

    # ------------------------------------------------------------- week lines

    def test_elapsed_weeks_report_planned_against_actual(self):
        self._two_weeks_trained()
        text = coach_service._block_progress_context("2026-07-22", "2026-07-22")
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
        text = coach_service._block_progress_context("2026-07-22", "2026-07-22")
        self.assertIn("week of 2026-07-06", text)
        self.assertIn("[block covers only part of this week]", text)
        # The whole-week that follows is comparable and carries no flag.
        self.assertIn("week of 2026-07-13: planned 100, actual 100 (100%)", text)

    def test_the_in_progress_week_is_raw_and_never_extrapolated(self):
        """DESIGN_intensity_distribution.md §9.3: raw load beside the elapsed day count."""
        self._two_weeks_trained()
        self._planned("2026-07-20", 60.0)
        self._done("2026-07-20", 60.0, "w3-0")
        text = coach_service._block_progress_context("2026-07-22", "2026-07-22")
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
        text = coach_service._block_progress_context("2026-07-22", "2026-07-22")
        self.assertIn("no plan covered this week", text)

    def test_gen_start_after_today_counts_the_preserved_day_as_history(self):
        """When today's session is already completed generate starts tomorrow, so today is
        history and the window must include it (else the header and the weeks disagree)."""
        self._two_weeks_trained()
        self._planned("2026-07-22", 90.0)
        self._done("2026-07-22", 90.0, "today")
        text = coach_service._block_progress_context("2026-07-22", "2026-07-23")
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
        text = coach_service._block_progress_context("2026-07-22", "2026-07-22")
        self.assertIn("Fitness tests this block has already run:", text)
        self.assertIn("2026-07-15: ftp_20min (cycling)", text)
        self.assertIn("Functional Threshold Power (FTP) 271 W", text)

    def test_a_test_performed_but_never_recorded_still_counts(self):
        """De-duplication keys on the planned session, not the logbook: a test the athlete
        did without recording the result must not be scheduled twice."""
        self._two_weeks_trained()
        self._planned("2026-07-15", 70.0, title="FTP Test", benchmark_type="ftp_20min")
        text = coach_service._block_progress_context("2026-07-22", "2026-07-22")
        self.assertIn("2026-07-15: ftp_20min (cycling) — no result recorded", text)

    def test_an_ad_hoc_logbook_row_is_reported_too(self):
        """A result recorded with no planned test behind it — the athlete tested off-plan."""
        self._two_weeks_trained()
        test_db.add_benchmark_result(
            date="2026-07-17", sport_type="running", anchor_kind="lthr",
            value=168, unit="bpm",
        )
        text = coach_service._block_progress_context("2026-07-22", "2026-07-22")
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
        text = coach_service._block_progress_context("2026-07-22", "2026-07-22")
        self.assertNotIn("ftp_20min", text)


class TestBlockProgressReachesThePrompt(unittest.TestCase):
    """The threading end to end: service -> engine -> both halves of the LLM call."""

    @patch("trainmate.coach.engine.openrouter_client")
    def _prompts(self, block_progress, mock_client):
        mock_client.complete.return_value = {"reasoning": "ok", "workouts": []}
        coach_service.engine._workout_generate_logic(
            objectives=[], constraints=[], today_str="2026-07-22", guidelines="",
            profile={}, strategy="Build", meso_text="  - Build 1", learnings="",
            num_days=14, block_progress=block_progress,
        )
        return mock_client.complete.call_args[0]

    def test_both_halves_carry_it(self):
        system, user = self._prompts("Build 1 — 2 completed weeks of 4")
        # The instruction rides in the system prompt's TASK...
        self.assertIn("CONTINUING A BLOCK ALREADY UNDER WAY", system)
        # ...and the data in the user content, under the name the TASK refers to.
        self.assertIn("BLOCK PROGRESS SO FAR", user)
        self.assertIn("Build 1 — 2 completed weeks of 4", user)

    def test_a_clean_block_start_is_unchanged(self):
        """No block progress -> neither half mentions it, so starting a block cleanly
        produces the prompt it always did."""
        system, user = self._prompts(None)
        self.assertNotIn("CONTINUING A BLOCK", system)
        self.assertNotIn("BLOCK PROGRESS", user)


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


if __name__ == "__main__":
    unittest.main()
