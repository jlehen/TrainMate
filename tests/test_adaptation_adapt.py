import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import Mock, patch

from tests.helpers import clear_all_tables
from trainmate.adherence import analyze_adherence

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_adaptation_adapt.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.service.db = test_db

from trainmate.coach import coach_service

# trainmate_cli re-exports names from trainmate.cli.workouts, so it must be imported first.
import trainmate_cli  # noqa: F401
from trainmate.cli.workouts import generate as workouts_cli


class TestAdaptationAdapt(unittest.TestCase):
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

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adaptation_matching_and_discrepancies(self, mock_client):
        test_profile = {"lthr": 165, "max_hr": 185}

        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            mock_client.complete.return_value = {
                "change_needed": True,
                "reason": "Fatigue detected, RHR is elevated and HRV is suppressed.",
                "adapted_workouts": [
                    {
                        "date": "2026-06-03",
                        "sport_type": "rest",
                        "title": "Adapted Rest Day",
                        "description": "Swapped tempo run to rest.",
                        "duration_minutes": 0,
                        "rpe": 0,
                        "tss": 0.0,
                    }
                ],
            }

            test_db.save_metric_cache("2026-06-01", 50, 60, 80, 20, 10.0, 8.0, 1.2)
            test_db.save_metric_cache("2026-06-02", 52, 55, 75, 25, 12.0, 8.0, 1.5)
            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            test_db.save_workout(
                "2026-06-01", "running", "Easy Run", "30 mins",
                duration_minutes=30, rpe=4, tss=20,
            )
            test_db.save_workout(
                "2026-06-02", "cycling", "Tempo Ride", "60 mins",
                duration_minutes=60, rpe=6, tss=40,
            )
            test_db.save_workout(
                "2026-06-03", "running", "Interval Session", "45 mins",
                duration_minutes=45, rpe=8, tss=60,
            )

            test_db.save_completed_activity(
                "act_1", "2026-06-01", "2026-06-01 08:00:00", "Easy Run",
                "running", 1800.0, 5.0, 50.0, 132, 150, 4, 20.0,
            )
            test_db.save_completed_activity(
                "act_2", "2026-06-02", "2026-06-02 08:00:00", "Short Cycling",
                "cycling", 1800.0, 12.0, 100.0, 132, 150, 4, 20.0,
            )

            reason, proposed, _new_constraints = coach_service.workout_adapt("2026-06-03")

            self.assertTrue(mock_client.complete.called)
            self.assertEqual(reason, "Fatigue detected, RHR is elevated and HRV is suppressed.")
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["title"], "Adapted Rest Day")
            self.assertEqual(proposed[0]["sport_type"], "rest")

            prompt_user_content = mock_client.complete.call_args[0][1]
            self.assertIn(
                "Complete Miss! Missed planned workout 'Interval Session'",
                prompt_user_content,
            )
            self.assertIn("duration mismatch", prompt_user_content)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_is_read_only_for_learnings(self, mock_client):
        """Daily adaptation consumes coach learnings as context but authors none — durable,
        evidence-backed observations are written only by the weekly history analysis
        (DESIGN_evidence_based_confidence.md §2/§11)."""
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            # Pre-existing observation shown to the model as context.
            lid = test_db.add_learning(
                "Elevated RHR after consecutive hard days", sports="running"
            )

            # Even if the model returns learning_updates, adapt must ignore them.
            mock_client.complete.return_value = {
                "change_needed": False,
                "reason": "On track.",
                "adapted_workouts": [],
                "learning_updates": [
                    {"op": "add", "text": "Should NOT be saved by adapt"},
                ],
            }

            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            reason, proposed, _new_constraints = coach_service.workout_adapt("2026-06-03")
            self.assertEqual(reason, "On track.")
            self.assertEqual(proposed, [])

            # The adapt prompt still surfaces the existing observation by id as context.
            system_prompt = mock_client.complete.call_args[0][0]
            self.assertIn(f"[{lid}|running|", system_prompt)

            # No learnings were written: only the pre-existing one remains.
            learnings = {l["id"]: l for l in test_db.get_learnings()}
            self.assertEqual(len(learnings), 1)
            self.assertIn(lid, learnings)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_message_surfaced_in_prompt(self, mock_client):
        """An athlete message for the run is rendered as a bounded section of the adapt
        prompt (advisory, ephemeral) and omitted entirely when no message is given."""
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            mock_client.complete.return_value = {
                "change_needed": False,
                "reason": "On track.",
                "adapted_workouts": [],
            }

            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            coach_service.workout_adapt(
                "2026-06-03", message="  knee is sore, keep impact low  "
            )
            user_content = mock_client.complete.call_args[0][1]
            system_prompt = mock_client.complete.call_args[0][0]
            self.assertIn("ATHLETE'S NOTE FOR THIS ADAPTATION", user_content)
            # Surrounding whitespace is trimmed before rendering.
            self.assertIn("knee is sore, keep impact low", user_content)
            self.assertNotIn("  knee is sore", user_content)
            self.assertIn("ATHLETE'S NOTE FOR TODAY", system_prompt)

            # No message → the section is absent (message-less run is unchanged).
            mock_client.complete.reset_mock()
            mock_client.complete.return_value = {
                "change_needed": False,
                "reason": "On track.",
                "adapted_workouts": [],
            }
            coach_service.workout_adapt("2026-06-03")
            self.assertNotIn(
                "ATHLETE'S NOTE FOR THIS ADAPTATION",
                mock_client.complete.call_args[0][1],
            )

    def _save_two_block_plan(self):
        """Saves a plan whose first block ends 2026-06-30 and whose second opens 2026-07-01."""
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date="2026-10-15",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Build aerobic base, then sharpen.",
            goals_hash="hash1",
            constraints_hash="hash2",
            mesocycles=[
                {"name": "Base Building", "start_date": "2026-06-01",
                 "end_date": "2026-06-30", "focus": "Zone 2 runs"},
                {"name": "Peak & Taper", "start_date": "2026-07-01",
                 "end_date": "2026-07-21", "focus": "Race pace"},
            ],
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_terminal_window_section_in_prompt(self, mock_client):
        """Near the block's end the prompt warns the model that an easing cannot rebound and
        the next block is out of reach; mid-block that section is absent entirely
        (DESIGN_block_boundary.md §3)."""
        self._save_two_block_plan()
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": {"lthr": 165, "max_hr": 185},
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            mock_client.complete.return_value = {
                "change_needed": False,
                "reason": "On track.",
                "adapted_workouts": [],
            }
            test_db.save_metric_cache("2026-06-28", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-28", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            # Two days before the block ends -> inside the default 3-day terminal window.
            coach_service.workout_adapt("2026-06-28")
            system_prompt = mock_client.complete.call_args[0][0]
            self.assertIn("THIS BLOCK IS ENDING", system_prompt)
            self.assertIn("ends in 2 day(s), on 2026-06-30", system_prompt)

            # Mid-block -> the section is omitted (prompt unchanged for the common case).
            mock_client.complete.reset_mock()
            mock_client.complete.return_value = {
                "change_needed": False,
                "reason": "On track.",
                "adapted_workouts": [],
            }
            coach_service.workout_adapt("2026-06-10")
            self.assertNotIn("THIS BLOCK IS ENDING", mock_client.complete.call_args[0][0])

    def test_block_boundary_hint_names_next_mesocycle(self):
        """Inside the terminal window the CLI names the ending block and the exact generate
        command for the next one; mid-block it stays silent (DESIGN_block_boundary.md §4)."""
        self._save_two_block_plan()
        next_meso = test_db.get_next_mesocycle("2026-06-30")
        self.assertEqual(next_meso["name"], "Peak & Taper")

        def hint_output(date_str: str) -> str:
            buf = io.StringIO()
            with patch("trainmate.cli.workouts.generate.cli") as mock_cli, redirect_stdout(buf):
                mock_cli.db = test_db
                workouts_cli._print_block_boundary_hint(date_str)
            return buf.getvalue()

        # One day before the block ends -> hint fires, even with no adaptation proposed.
        out = hint_output("2026-06-29")
        self.assertIn("Base Building", out)
        self.assertIn("in 1 day(s), on 2026-06-30", out)
        self.assertIn("Peak & Taper", out)
        self.assertIn(f"workout generate --until-mesocycle {next_meso['id']}", out)

        # Mid-block -> nothing printed.
        self.assertEqual(hint_output("2026-06-10"), "")

    def test_block_boundary_hint_silent_without_next_block(self):
        """The final block of a plan has nothing to regenerate, so the hint stays silent."""
        self._save_two_block_plan()
        buf = io.StringIO()
        with patch("trainmate.cli.workouts.generate.cli") as mock_cli, redirect_stdout(buf):
            mock_cli.db = test_db
            # 2026-07-20 is one day before the LAST block ends; get_next_mesocycle -> None.
            workouts_cli._print_block_boundary_hint("2026-07-20")
        self.assertEqual(buf.getvalue(), "")

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_drops_proposal_past_block_end(self, mock_client):
        """The next block is out of adapt's reach on the write side too: a proposal dated past
        the block's end is dropped, so the applied range can never stretch into the next block
        (DESIGN_block_boundary.md §1)."""
        self._save_two_block_plan()
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": {"lthr": 165, "max_hr": 185},
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            # The model invents a session in the NEXT block (starts 2026-07-01) alongside a
            # legitimate in-block one.
            mock_client.complete.return_value = {
                "change_needed": True,
                "reason": "Ease into the block's last days.",
                "adapted_workouts": [
                    {
                        "date": "2026-06-29", "sport_type": "running",
                        "title": "Easy Run", "description": "30 mins easy Z2",
                        "duration_minutes": 30, "rpe": 4, "tss": 20.0,
                    },
                    {
                        "date": "2026-07-02", "sport_type": "running",
                        "title": "Next-block Session (should be dropped)",
                        "description": "Past the boundary.",
                        "duration_minutes": 60, "rpe": 7, "tss": 55.0,
                    },
                ],
            }
            test_db.save_metric_cache("2026-06-28", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-28", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            _reason, proposed, _new_constraints = coach_service.workout_adapt("2026-06-28")

            self.assertEqual([p["date"] for p in proposed], ["2026-06-29"])

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_drops_already_completed_session(self, mock_client):
        """A session already performed (matched by a completed activity) is locked history:
        the guard drops any proposal targeting it, even if the model returns one — you cannot
        adapt a workout you have already finished today."""
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            # The model tries to "adapt" today's already-completed ride (date == eval date),
            # plus legitimately adapt a future session still ahead of the athlete.
            mock_client.complete.return_value = {
                "change_needed": True,
                "reason": "Mild fatigue; ease the upcoming interval session.",
                "adapted_workouts": [
                    {
                        "date": "2026-06-03",
                        "sport_type": "cycling",
                        "title": "Rewritten Ride (should be dropped)",
                        "description": "Restating the finished ride to match actual.",
                        "duration_minutes": 82,
                        "rpe": 4,
                        "tss": 52.0,
                    },
                    {
                        "date": "2026-06-04",
                        "sport_type": "running",
                        "title": "Eased Intervals",
                        "description": "Cut intensity for the future session.",
                        "duration_minutes": 40,
                        "rpe": 5,
                        "tss": 35.0,
                    },
                ],
            }

            test_db.save_metric_cache("2026-06-03", 56, 42, 60, 35, 14.0, 8.0, 1.75)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            # Today's planned ride and a future running session.
            test_db.save_workout(
                "2026-06-03", "cycling", "Aerobic Base Endurance", "70 mins",
                duration_minutes=70, rpe=4, tss=45,
            )
            test_db.save_workout(
                "2026-06-04", "running", "Interval Session", "45 mins",
                duration_minutes=45, rpe=8, tss=60,
            )

            # The ride was actually completed today — this is what locks it.
            test_db.save_completed_activity(
                "act_ride", "2026-06-03", "2026-06-03 08:00:00", "Zwift Ride",
                "cycling", 4920.0, 30.0, 250.0, 132, 150, 4, 52.0,
            )

            reason, proposed, _new_constraints = coach_service.workout_adapt("2026-06-03")

            dates = {p["date"] for p in proposed}
            self.assertNotIn("2026-06-03", dates)  # completed session dropped
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["date"], "2026-06-04")
            self.assertEqual(proposed[0]["title"], "Eased Intervals")

            # The completed session is surfaced to the model as locked history (the
            # authoritative signal), not left for it to re-derive from the activity list.
            prompt_user_content = mock_client.complete.call_args[0][1]
            self.assertIn(
                "Aerobic Base Endurance | Expected duration: 70m, RPE: 4, TSS: 45 "
                "[COMPLETED — locked history, not adaptable]",
                prompt_user_content,
            )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_drops_noop_relisted_session(self, mock_client):
        """No-op backstop: if the model re-lists a session unchanged (here verbatim, plus a
        cosmetic whitespace-only variant), it is dropped so an untouched session is never
        re-stamped as adapted. A genuinely changed session on the same run is kept."""
        test_profile = {"lthr": 165, "max_hr": 185}
        with patch.dict(trainmate.coach.config.data, {
            "user_profile": test_profile,
            "coach": {
                "metrics_lookback_days": 3,
                "minor_activity_load_threshold": 10.0,
            }
        }):
            mock_client.complete.return_value = {
                "change_needed": True,
                "reason": "Mostly on track; only the Friday tempo needs easing.",
                "adapted_workouts": [
                    # Verbatim re-list of the planned session — a no-op, must be dropped.
                    {
                        "date": "2026-06-04",
                        "sport_type": "running",
                        "title": "Easy Run",
                        "description": "30 mins easy Z2",
                        "duration_minutes": 30,
                        "rpe": 4,
                        "tss": 20.0,
                    },
                    # Same content but cosmetic whitespace churn + int/float tss — still a
                    # no-op, must be dropped.
                    {
                        "date": "2026-06-05",
                        "sport_type": "cycling",
                        "title": "Endurance Ride",
                        "description": "60 mins  aerobic   base",
                        "duration_minutes": 60,
                        "rpe": 5,
                        "tss": 40,
                    },
                    # Genuine change — must survive.
                    {
                        "date": "2026-06-06",
                        "sport_type": "running",
                        "title": "Eased Tempo",
                        "description": "Cut to easy Z2 to shed intensity.",
                        "duration_minutes": 35,
                        "rpe": 5,
                        "tss": 30.0,
                    },
                ],
            }

            test_db.save_metric_cache("2026-06-03", 50, 60, 80, 20, 10.0, 8.0, 1.1)
            test_db.save_baseline("2026-06-03", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

            test_db.save_workout(
                "2026-06-04", "running", "Easy Run", "30 mins easy Z2",
                duration_minutes=30, rpe=4, tss=20,
            )
            test_db.save_workout(
                "2026-06-05", "cycling", "Endurance Ride", "60 mins aerobic base",
                duration_minutes=60, rpe=5, tss=40,
            )
            test_db.save_workout(
                "2026-06-06", "running", "Friday Tempo", "45 mins w/ tempo blocks",
                duration_minutes=45, rpe=7, tss=55,
            )

            reason, proposed, _new_constraints = coach_service.workout_adapt("2026-06-03")

            dates = {p["date"] for p in proposed}
            self.assertEqual(dates, {"2026-06-06"})
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["title"], "Eased Tempo")

    def test_adapt_apply_stamps_recency_and_bumps_count(self):
        """Applying an adaptation that MOVES THE LOAD stamps `adapted_at` and bumps
        `adaptation_count`; a second load-moving adapt of the same session bumps it
        again. Non-adapt saves leave both untouched."""
        test_db.save_workout(
            "2026-06-20", "running", "Friday Tempo", "45 mins w/ tempo blocks",
            duration_minutes=45, rpe=7, tss=55,
        )
        # A plain save (no adapted_at) must not start the counter.
        row = test_db.get_workout("2026-06-20", "running")
        self.assertIsNone(row["adapted_at"])
        self.assertEqual(row["adaptation_count"], 0)

        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=Mock()
        )
        proposed = [{
            "date": "2026-06-20", "sport_type": "running", "title": "Easy Tempo",
            "description": "Cut to Z2", "modification_reason": "Eased for fatigue",
            "duration_minutes": 35, "rpe": 5, "tss": 30,
        }]
        service.workout_adapt_apply(proposed, "Block too hard", "2026-06-20", "2026-06-20")
        row = test_db.get_workout("2026-06-20", "running")
        self.assertIsNotNone(row["adapted_at"])
        self.assertEqual(row["adaptation_count"], 1)

        proposed[0]["description"] = "Cut further to easy walk"
        proposed[0]["duration_minutes"] = 25
        proposed[0]["tss"] = 18
        service.workout_adapt_apply(proposed, "Still fatigued", "2026-06-20", "2026-06-20")
        row = test_db.get_workout("2026-06-20", "running")
        self.assertEqual(row["adaptation_count"], 2)

    def _seed_block_with_drift(self):
        """A 3-week-elapsed block whose 'easy' running has drifted into Z3."""
        obj_id = test_db.add_objective(
            title="Autumn Half", target_date="2026-09-01",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="s", goals_hash="g", constraints_hash="c",
            mesocycles=[{
                "name": "Base 2", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "aerobic volume",
            }],
        )
        for i, day in enumerate(("2026-06-02", "2026-06-09", "2026-06-16")):
            test_db.save_completed_activity(
                f"drift_{i}", day, f"{day} 08:00:00", "Easy Run", "running",
                5400.0, 12.0, 80.0, 150, 172, None, 70.0,
                zone1_sec=300, zone2_sec=1800, zone3_sec=2700, zone4_sec=600,
                zone5_sec=0,
            )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_prompt_carries_the_measured_distribution_and_drift_branch(
        self, mock_client
    ):
        """§9.3/§9.4: the block's measured distribution reaches the adapt prompt as its
        own section, and it gates both the fourth TASK branch and CORRECTING EXECUTION
        DRIFT — the case no other branch covers, since the athlete showed up for
        everything and feels fine."""
        self._seed_block_with_drift()
        mock_client.complete.return_value = {"change_needed": False, "reason": "ok"}
        coach_service.workout_adapt("2026-06-24")

        system_prompt, user_content = mock_client.complete.call_args[0][:2]
        flat = " ".join(user_content.split())
        self.assertIn("MEASURED INTENSITY DISTRIBUTION OF THE ACTIVE BLOCK", flat)
        self.assertIn('Base 2 — focus "aerobic volume"', flat)
        self.assertIn("Z3 tempo", flat)
        self.assertIn("Current week so far", flat)
        self.assertIn("NOT extrapolated", flat)
        self.assertIn("CORRECTING EXECUTION DRIFT:", system_prompt)
        self.assertIn("even when\n  recovery metrics are fine", system_prompt)
        # §9.2: adapt may move a session's intensity, never the block's composition.
        self.assertIn("belongs to the next `workout generate`", system_prompt)
        # §4.1: the block-over-block delta is a generate view; adapt must not see it.
        self.assertNotIn("Change vs", user_content)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_adapt_prompt_omits_drift_instructions_without_a_block(self, mock_client):
        """No active block -> no table, so the instructions that reference it must go
        too (the has_message gating discipline)."""
        mock_client.complete.return_value = {"change_needed": False, "reason": "ok"}
        coach_service.workout_adapt("2026-06-24")
        system_prompt, user_content = mock_client.complete.call_args[0][:2]
        self.assertNotIn("CORRECTING EXECUTION DRIFT", system_prompt)
        self.assertNotIn("MEASURED INTENSITY DISTRIBUTION", user_content)

    def test_drift_correction_does_not_count_as_an_easing(self):
        """A drift correction rewrites the prescription and holds the load, so it must
        NOT be stamped as an easing (DESIGN_intensity_distribution.md §9.5). Stamping it
        would raise the DO NOT COMPOUND bar for a session that was never cut, blunting
        adapt's fatigue response the next time the athlete is genuinely wrecked."""
        test_db.save_workout(
            "2026-06-21", "running", "Easy Hour", "60 min conversational.",
            duration_minutes=60, rpe=4, tss=40,
        )
        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=Mock()
        )
        # Same duration, same TSS (as a float against a stored int) — only the
        # prescription's wording sharpens, with an explicit HR guard rail.
        proposed = [{
            "date": "2026-06-21", "sport_type": "running", "title": "Easy Hour",
            "description": "60 min conversational. HR ceiling 145 — hard cap.",
            "modification_reason": "Third block week where 'easy' runs averaged Z3.",
            "duration_minutes": 60, "rpe": 4, "tss": 40.0,
        }]
        service.workout_adapt_apply(
            proposed, "Correcting execution drift", "2026-06-21", "2026-06-21"
        )
        row = test_db.get_workout("2026-06-21", "running")
        self.assertIsNone(row["adapted_at"])
        self.assertEqual(row["adaptation_count"], 0)
        self.assertIn("HR ceiling 145", row["description"])

        # A genuine cut on the same session still stamps normally.
        proposed[0]["duration_minutes"] = 40
        proposed[0]["tss"] = 25
        service.workout_adapt_apply(
            proposed, "Fatigued", "2026-06-21", "2026-06-21"
        )
        row = test_db.get_workout("2026-06-21", "running")
        self.assertIsNotNone(row["adapted_at"])
        self.assertEqual(row["adaptation_count"], 1)

    def test_adapt_swap_inherits_displaced_session_as_original(self):
        """A cross-sport swap (strength -> yoga) deletes the planned strength session
        and inserts a yoga one. The new session should inherit the displaced strength
        session's description + load as its `original_*` snapshot, so the Calendar event
        can surface what was originally planned."""
        test_db.save_workout(
            "2026-07-02", "strength_training", "Heavy Legs",
            "5x5 back squat + accessories.",
            duration_minutes=60, rpe=7, tss=70,
        )

        service = trainmate.coach.CoachService(
            db_instance=test_db, calendar_syncer_instance=Mock()
        )
        proposed = [{
            "date": "2026-07-02", "sport_type": "yoga", "title": "Easy Mobility",
            "description": "20 min easy mobility flow.",
            "modification_reason": "Swapped from strength after a workload spike.",
            "duration_minutes": 30, "rpe": 1, "tss": 4,
        }]
        service.workout_adapt_apply(
            proposed, "Reduce load", "2026-07-02", "2026-07-02"
        )

        # The strength row is gone; the yoga row carries the strength session's
        # planned description and load as its original snapshot.
        self.assertIsNone(test_db.get_workout("2026-07-02", "strength_training"))
        row = test_db.get_workout("2026-07-02", "yoga")
        self.assertEqual(row["description"], "20 min easy mobility flow.")
        self.assertEqual(row["original_description"], "5x5 back squat + accessories.")
        self.assertEqual(row["original_duration_minutes"], 60)
        self.assertEqual(row["original_tss"], 70)
        self.assertEqual(row["original_rpe"], 7)
        # Current load reflects the swapped-in yoga session.
        self.assertEqual(row["duration_minutes"], 30)
        self.assertEqual(row["tss"], 4)
        self.assertEqual(row["rpe"], 1)

    def test_already_eased_tag_in_planned_prompt(self):
        """An already-eased session is tagged with its count + recency for the adapt
        prompt; an unadapted session is not."""
        from trainmate.coach.formatting import format_planned_workouts_detailed

        eased = {
            "date": "2026-06-18", "sport_type": "running", "title": "Easy Tempo",
            "description": "Z2", "duration_minutes": 35, "rpe": 5, "tss": 30,
            "modification_reason": "Eased", "adaptation_summary": "Block too hard",
            "adapted_at": "2026-06-17T08:00:00+00:00", "adaptation_count": 2,
        }
        untouched = {
            "date": "2026-06-19", "sport_type": "yoga", "title": "Mobility",
            "description": "easy", "duration_minutes": 20, "rpe": 2, "tss": 10,
        }
        text = format_planned_workouts_detailed(
            [eased, untouched], eval_date="2026-06-20"
        )
        self.assertIn("ALREADY EASED", text)
        self.assertIn("2x", text)
        self.assertIn("3 days ago", text)
        # The unadapted yoga line carries no such tag.
        yoga_line = [ln for ln in text.splitlines() if "(YOGA)" in ln][0]
        self.assertNotIn("ALREADY EASED", yoga_line)
