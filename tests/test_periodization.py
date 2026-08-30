import io
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, pin_clock, rebind_test_db, save_workout

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_periodization.db")


def _days_out(n: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=n)).isoformat()


# Goal and constraint fixtures ride on today rather than on fixed dates: a plan window
# needs its goal in the future, so hardcoding one expires the tests the day it passes
# (same rot 2a7cd71 fixed in test_constraints.py).
GOAL_DATE = _days_out(71)

from trainmate.config import config
from trainmate.db import Database
from trainmate.db.periodization import repair_block_contiguity
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service


def _generate_workouts(**kwargs):
    """`workout generate` end to end: propose, then accept, as the CLI does on a `y`.

    Generation is two halves so the athlete reads the plan before it is written; tests
    exercising the write want both, and the proposal alone is called directly where only
    the refusal or the prompt is under test."""
    proposal = coach_service.workout_generate(**kwargs)
    return proposal.reasoning, coach_service.workout_generate_apply(proposal)


class TestPeriodization(unittest.TestCase):
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

    def test_hashing_helpers(self):
        hash1 = coach_service._get_goals_hash([])
        hash2 = coach_service._get_constraints_hash([])
        hash3 = coach_service._get_config_hash()
        self.assertIsNotNone(hash1)
        self.assertIsNotNone(hash2)
        self.assertIsNotNone(hash3)

        obj = {
            "id": 1, "title": "Test Goal", "target_date": "2026-10-15",
            "sport_type": "running", "description": "sub 3hr", "status": "active",
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

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_replan_logic_and_caching(self, mock_client, mock_calendar):
        # Every clock, not just the service's: whether a goal is still ahead is now a date
        # question the DB answers, so a half-pinned clock reads real "today" there and the
        # fixture's future-dated goals silently become past ones.
        pin_clock(self, "2026-06-01")
        obj_id = test_db.add_objective(
            title="Berlin Marathon", target_date="2026-09-27",
            sport_type="running",
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
            sport_type="cycling",
        )
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = [mock_macro_response, mock_workouts_response]
        coach_service.replan(force=False)
        self.assertEqual(mock_client.complete.call_count, 2)

    def test_system_prompt_inserts_periodization(self):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        self.assertIn("## COACH LEARNINGS & ACTIVE PERIODIZATION STRATEGY", prompt)
        self.assertIn("START OF TRAINMATE SPORTS SCIENCE GUIDELINES", prompt)
        self.assertIn("END OF TRAINMATE SPORTS SCIENCE GUIDELINES", prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_replan_provides_previous_strategy_context_to_llm(self, mock_client):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        self.assertIn("## PREVIOUS PERIODIZATION STRATEGY (FOR CONTEXT)", system_prompt)
        self.assertIn("Keep heart rate low", system_prompt)
        self.assertIn(
            "Base Building (2026-06-01 to 2026-06-28): Aerobic conditioning",
            system_prompt,
        )
        self.assertIn("### CONTINUITY WITH THE PREVIOUS PLAN", system_prompt)
        self.assertIn(
            "The PREVIOUS periodization strategy that was in place before this",
            system_prompt,
        )

    def _plan_with_block_under_way(self, target_date: str = "2026-11-01") -> int:
        """A goal whose active plan holds one block straddling the pinned today."""
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=target_date,
            sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Build aerobic base",
            goals_hash="old_goals_hash",
            constraints_hash="old_constraints_hash",
            mesocycles=[{
                "name": "Build", "start_date": "2026-08-03",
                "end_date": "2026-08-30", "focus": "Threshold work",
            }],
        )
        return obj_id

    @patch("trainmate.coach.engine.openrouter_client")
    def test_replan_offers_to_keep_the_block_under_way(self, mock_client):
        """Mid-block, the replan may let that block finish — which means repeating its
        ORIGINAL start date, since blocks own their sessions by date containment
        (DESIGN_block_progress.md §7)."""
        pin_clock(self, "2026-08-23")
        self._plan_with_block_under_way()
        mock_client.complete.return_value = {
            "strategy": "Kept the Build block; it still fits.",
            "mesocycles": [{
                "name": "Build", "start_date": "2026-08-03",
                "end_date": "2026-08-30", "focus": "Threshold work",
            }],
        }

        coach_service.plan_generate(force=True)

        prompt = mock_client.complete.call_args_list[0][0][0]
        self.assertIn("### THE BLOCK ALREADY UNDER WAY", prompt)
        self.assertIn('"Build" (2026-08-03 to 2026-08-30)', prompt)
        self.assertIn("Already trained: 20 days of it, starting 2026-08-03.", prompt)
        # The whole point of the change: keeping it means the original start date.
        self.assertIn("ORIGINAL start date", prompt)
        self.assertIn("(2026-08-03), its original end date (2026-08-30)", prompt)
        self.assertIn("Do NOT re-date it to 2026-08-23", prompt)
        # ...and the unconditional "start today" sentence is withdrawn while it applies.
        self.assertNotIn("The first mesocycle must start on the start date", prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_block_starting_today_is_not_offered(self, mock_client):
        """Nothing is under way yet, so there is nothing to let finish."""
        pin_clock(self, "2026-08-03")
        self._plan_with_block_under_way()
        mock_client.complete.return_value = {"strategy": "s", "mesocycles": []}

        coach_service.plan_generate(force=True)

        prompt = mock_client.complete.call_args_list[0][0][0]
        self.assertNotIn("### THE BLOCK ALREADY UNDER WAY", prompt)
        self.assertIn(
            "The first mesocycle must start on the start date (2026-08-03).", prompt
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_fresh_withholds_the_block_under_way(self, mock_client):
        """A clean slate is not asked to finish the block it is departing from."""
        pin_clock(self, "2026-08-23")
        self._plan_with_block_under_way()
        mock_client.complete.return_value = {"strategy": "s", "mesocycles": []}

        coach_service.plan_generate(fresh=True)

        prompt = mock_client.complete.call_args_list[0][0][0]
        self.assertNotIn("### THE BLOCK ALREADY UNDER WAY", prompt)
        self.assertIn(
            "The first mesocycle must start on the start date (2026-08-23).", prompt
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_block_not_offered_when_plan_start_is_pinned_past_today(self, mock_client):
        """A preceding goal's plan pins the start after today; reaching back past that
        would overlap that goal's season, so the keep option is withheld."""
        pin_clock(self, "2026-08-23")
        early_id = test_db.add_objective(
            title="Tune-up 10k", target_date="2026-09-15",
            sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=early_id, strategy="Sharpen",
            goals_hash="g", constraints_hash="c",
            mesocycles=[{
                "name": "Sharpen", "start_date": "2026-08-25",
                "end_date": "2026-09-15", "focus": "Speed",
            }],
        )
        obj_id = self._plan_with_block_under_way()
        mock_client.complete.return_value = {"strategy": "s", "mesocycles": []}

        # Named explicitly: the bare call would plan for the sooner tune-up goal.
        coach_service.plan_generate(force=True, objective_id=obj_id)

        prompt = mock_client.complete.call_args_list[0][0][0]
        self.assertNotIn("### THE BLOCK ALREADY UNDER WAY", prompt)
        self.assertIn(
            "The first mesocycle must start on the start date (2026-09-16).", prompt
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_a_named_goal_bounds_the_plan_to_its_own_span(self, mock_client):
        """`plan generate -g N` opens N's plan after the goal before it, even when that
        goal has no plan of its own yet — and says so, because the reading it replaces
        swallowed those days into N's plan (DESIGN_cli_selectors.md §9)."""
        pin_clock(self, "2026-08-23")
        test_db.add_objective(
            title="Tune-up 10k", target_date="2026-09-15", sport_type="running",
        )
        obj_id = test_db.add_objective(
            title="Autumn Marathon", target_date="2026-12-06", sport_type="running",
        )
        mock_client.complete.return_value = {"strategy": "s", "mesocycles": []}

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            coach_service.plan_generate(
                force=True, objective_id=obj_id, auto_apply=False,
                start_date="2026-09-16",
            )
        prompt = mock_client.complete.call_args_list[0][0][0]
        self.assertIn(
            "The first mesocycle must start on the start date (2026-09-16).", prompt
        )
        # The two readings differ here, so the run names both.
        said = " ".join(out.getvalue().split())
        self.assertIn("now plans this goal's own span", said)
        self.assertIn("2026-09-16", said)
        self.assertIn("2026-08-23", said)

        # Unbounded, the tune-up's days are still swallowed — the old reading, which
        # `-g ..N` keeps spellable.
        mock_client.complete.reset_mock()
        coach_service.plan_generate(
            force=True, objective_id=obj_id, auto_apply=False
        )
        prompt = mock_client.complete.call_args_list[0][0][0]
        self.assertIn(
            "The first mesocycle must start on the start date (2026-08-23).", prompt
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_a_bound_the_derivation_already_agrees_with_says_nothing(self, mock_client):
        """The notice is transitional, so it stays quiet when both readings pick the same
        first day."""
        pin_clock(self, "2026-08-23")
        obj_id = test_db.add_objective(
            title="Autumn Marathon", target_date="2026-12-06", sport_type="running",
        )
        mock_client.complete.return_value = {"strategy": "s", "mesocycles": []}

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            coach_service.plan_generate(
                force=True, objective_id=obj_id, auto_apply=False,
                start_date="2026-08-23",
            )
        self.assertNotIn("own span", out.getvalue())

    @patch("trainmate.coach.engine.openrouter_client")
    def test_fresh_withholds_the_plan_in_place(self, mock_client):
        """`--fresh`: the intent is withheld, the evidence is not
        (DESIGN_backward_evaluation.md §6.1)."""
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        test_db.save_completed_activity(
            activity_id="a1", date="2026-06-01", start_time="08:00:00",
            activity_name="Base Run", activity_type="running",
            duration_sec=3600.0, distance_km=10.0, elevation_gain_m=50.0,
            avg_hr=140, max_hr=160, rpe=5, tss=60.0,
            zone1_sec=300, zone2_sec=2700, zone3_sec=400, zone4_sec=200, zone5_sec=0,
        )
        mock_client.complete.return_value = {
            "strategy": "New strategy", "mesocycles": [{
                "name": "Build", "start_date": "2026-06-08",
                "end_date": "2026-10-15", "focus": "Threshold",
            }],
        }

        coach_service.plan_generate(fresh=True, objective_id=obj_id, auto_apply=False)

        system_prompt = mock_client.complete.call_args[0][0]
        self.assertNotIn("PREVIOUS PERIODIZATION STRATEGY", system_prompt)
        self.assertNotIn("CONTINUITY WITH THE PREVIOUS PLAN", system_prompt)
        self.assertNotIn("Keep heart rate low", system_prompt)
        # What the athlete trained under that plan still reaches the prompt.
        self.assertIn("PLANNED vs ACTUAL", system_prompt)
        self.assertIn("Aerobic conditioning", system_prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_fresh_implies_force(self, mock_client):
        """A clean slate is a regeneration: an up-to-date plan is not reused."""
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="Keep heart rate low",
            goals_hash=coach_service._get_goals_hash(test_db.upcoming_objectives()),
            constraints_hash=coach_service._get_constraints_hash([]),
            config_hash=coach_service._get_config_hash(),
            config_snapshot=coach_service._get_config_snapshot(),
            mesocycles=[{
                "name": "Base Building", "start_date": "2026-06-01",
                "end_date": "2026-06-28", "focus": "Aerobic conditioning",
            }],
        )
        mock_client.complete.return_value = {
            "strategy": "New strategy", "mesocycles": [{
                "name": "Build", "start_date": "2026-06-08",
                "end_date": "2026-10-15", "focus": "Threshold",
            }],
        }

        # Same inputs, so without `fresh` this would be reused without an LLM call.
        reused = coach_service.plan_generate(
            objective_id=obj_id, auto_apply=False
        )['reused']
        self.assertTrue(reused)

        proposal = coach_service.plan_generate(
            fresh=True, objective_id=obj_id, auto_apply=False
        )
        self.assertFalse(proposal['reused'])
        self.assertEqual(proposal['strategy'], "New strategy")

    def _prior_training_fixture(self, mock_client) -> int:
        """A goal whose plan has one elapsed block with a session trained inside it —
        the least that gives `_build_prior_training_context` something to say. Returns
        the goal's ID."""
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
            zone1_sec=300, zone2_sec=2700, zone3_sec=400, zone4_sec=200, zone5_sec=0,
        )
        mock_client.complete.return_value = {
            "strategy": "New strategy", "mesocycles": [{
                "name": "Build", "start_date": "2026-06-08",
                "end_date": "2026-10-15", "focus": "Threshold",
            }],
        }
        return obj_id

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_generate_injects_planned_vs_actual(self, mock_client):
        # Option A (DESIGN_backward_evaluation.md §6): the prior plan's elapsed blocks are
        # compared against what was actually completed, and fed into the strategy prompt.
        obj_id = self._prior_training_fixture(mock_client)
        coach_service.plan_generate(force=True, objective_id=obj_id)
        system_prompt = mock_client.complete.call_args[0][0]
        self.assertIn("## PRIOR TRAINING REVIEW", system_prompt)
        self.assertIn("PLANNED vs ACTUAL", system_prompt)
        self.assertIn("Aerobic conditioning", system_prompt)
        self.assertIn("1 session", system_prompt)
        # The review now carries the per-sport, per-zone distribution as a per-week rate
        # over completed weeks, not a Z1-2/Z3/Z4-5 rollup
        # (DESIGN_intensity_distribution.md §5/§9).
        self.assertIn("Intensity distribution, per week over 4 completed weeks",
                      " ".join(system_prompt.split()))
        self.assertIn("Z2 aerobic", system_prompt)
        self.assertNotIn("HR zones Z1-2/Z3/Z4-5", system_prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_the_review_is_not_echoed_to_the_screen_by_default(self, mock_client):
        # It is the longest thing this command prints, and it lands above the strategy
        # the athlete actually asked for (DESIGN_output_verbosity.md §7). What the model
        # is shown does not change — only what the screen is.
        obj_id = self._prior_training_fixture(mock_client)
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            coach_service.plan_generate(force=True, objective_id=obj_id)
        printed = out.getvalue()
        self.assertNotIn("PRIOR TRAINING REVIEW", printed)
        self.assertIn("--show-llm-context", printed)
        self.assertIn("## PRIOR TRAINING REVIEW", mock_client.complete.call_args[0][0])

    @patch("trainmate.coach.engine.openrouter_client")
    def test_show_context_echoes_the_review(self, mock_client):
        obj_id = self._prior_training_fixture(mock_client)
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            coach_service.plan_generate(
                force=True, objective_id=obj_id, show_context=True
            )
        printed = out.getvalue()
        self.assertIn("PRIOR TRAINING REVIEW", printed)
        self.assertIn("PLANNED vs ACTUAL", printed)
        # Pointing at a flag the athlete just used is noise.
        self.assertNotIn("--show-llm-context", printed)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_the_display_copy_is_built_only_when_it_is_shown(self, mock_client):
        # Showing it costs a second full pass over the same plans, re-laid-out at the
        # terminal's width. Off the flag that pass buys nothing (§7).
        obj_id = self._prior_training_fixture(mock_client)
        # auto_apply=False on both, as the CLI does: saving between the two runs would
        # change what the second one has to review.
        with patch.object(
            coach_service, "_build_prior_training_context",
            wraps=coach_service._build_prior_training_context,
        ) as spy, patch("sys.stdout", new_callable=io.StringIO):
            coach_service.plan_generate(
                force=True, objective_id=obj_id, auto_apply=False
            )
            self.assertEqual(spy.call_count, 1)
            spy.reset_mock()
            coach_service.plan_generate(
                force=True, objective_id=obj_id, auto_apply=False, show_context=True
            )
            self.assertEqual(spy.call_count, 2)

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

    def test_system_prompt_without_weekly_schedule(self):
        # weekly_schedule is optional: the prompt must say so explicitly and must not
        # instruct adherence to a day-by-day schedule that does not exist.
        test_profile = {
            "name": "Jane Doe",
            "weekly_target_hours": 8.0,
        }
        with patch.dict(trainmate.coach.config.data, {"user_profile": test_profile}):
            prompt = coach_service._get_coach_system_prompt([], [])
            self.assertIn("no day-by-day schedule configured", prompt)
            self.assertIn("No day-by-day availability schedule is configured", prompt)
            self.assertNotIn("Adhere to the day-by-day weekly availability schedule", prompt)
            self.assertNotIn("No availability configured", prompt)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_snapshots_goals_and_constraints(self, mock_client):
        import json
        obj_id = test_db.add_objective(
            title="Berlin Marathon", target_date=GOAL_DATE,
            sport_type="running", description="sub-3 attempt",
        )
        # A plan-shaping (replan=1) constraint the plan should snapshot. A tactical one
        # is excluded because only plan-shaping constraints fingerprint/snapshot the plan.
        test_db.add_constraint(
            title="Work trip", start_date=_days_out(1), end_date=_days_out(10),
            description="limited training time", replan=1,
        )
        test_db.add_constraint(
            title="no run Thursday", start_date=_days_out(6), end_date=_days_out(6),
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
        self.assertIsNotNone(macro["all_constraints_snapshot"])

        goals = json.loads(macro["goals_snapshot"])
        events = json.loads(macro["constraints_snapshot"])
        all_events = json.loads(macro["all_constraints_snapshot"])
        self.assertEqual([g["title"] for g in goals], ["Berlin Marathon"])
        # Only the plan-shaping constraint is snapshotted, not the tactical one.
        self.assertEqual([e["title"] for e in events], ["Work trip"])
        # But the display-only "all" snapshot carries both, tagged with `replan`, since
        # the prompt is built from every active constraint (DESIGN_constraints.md §7).
        self.assertEqual(
            sorted((e["title"], e["replan"]) for e in all_events),
            [("Work trip", 1), ("no run Thursday", 0)],
        )

        # The snapshot must serialize exactly the data the hash fingerprints, so the
        # two never disagree about what the plan was built on.
        self.assertEqual(
            coach_service._get_goals_hash(goals), macro["goals_hash"]
        )
        self.assertEqual(
            coach_service._get_constraints_hash(events), macro["constraints_hash"]
        )

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_plan_and_workouts_separately(self, mock_client, mock_calendar):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        reason, workouts = _generate_workouts()
        self.assertEqual(reason, "Separate workout reasoning")
        self.assertEqual(len(workouts), 1)
        self.assertEqual(workouts[0]["title"], "Base Run")
        mock_client.complete.assert_called_once()

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_preserves_completed_today_workout(
        self, mock_client, mock_calendar
    ):
        """When today's planned session has a matching completed activity, regeneration
        must keep today's workout and start the new plan tomorrow."""
        test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        save_workout(test_db,
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
        _generate_workouts()

        # Today's completed workout survives; the new plan begins tomorrow.
        titles = [w["title"] for w in test_db.get_workouts(start_date=today)]
        self.assertEqual(titles, ["Today Done", "Tomorrow Run"])
        # Today's Calendar event was left untouched (only future days are torn down).
        for call in mock_calendar.delete_workout_event.call_args_list:
            self.assertNotEqual(call.args[0], "evt-today")

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_workouts_clears_stale_synced_workouts(
        self, mock_client, mock_calendar
    ):
        """Regenerating workouts must wipe the previous plan's future workouts,
        including synced ones (and delete their Google Calendar events)."""
        test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        save_workout(test_db,
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
        _generate_workouts()

        # The stale synced workout is gone, and only the new workout remains.
        remaining = test_db.get_workouts(start_date=today)
        titles = [w["title"] for w in remaining]
        self.assertNotIn("Old Plan Run", titles)
        self.assertEqual(titles, ["New Run"])
        # Its Google Calendar event was deleted.
        mock_calendar.delete_workout_event.assert_called_once_with("evt-old-123")

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_generate_workouts_clears_stale_unsynced_calendar_workouts(
        self, mock_client, mock_calendar
    ):
        """A workout that was pushed then adapted/swapped (stale but with a
        google_event_id) must still have its Calendar event deleted on regenerate —
        i.e. cleanup keys on google_event_id, not the freshness state (orphan guard)."""
        test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        save_workout(test_db,
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
        _generate_workouts()

        remaining = test_db.get_workouts(start_date=today)
        self.assertEqual([w["title"] for w in remaining], ["New Run"])
        mock_calendar.delete_workout_event.assert_called_once_with("evt-stale-456")

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_regenerate_supersedes_prior_version(self, mock_client):
        """Regenerating a plan keeps the prior macrocycle as a superseded version
        rather than deleting it (see DESIGN_plan_rollback.md)."""
        test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_rollback_restores_plan_and_workouts(self, mock_client, mock_calendar):
        """`plan rollback` restores the previous plan version, resurrects its workouts,
        archives the current plan's, and reconciles Google Calendar symmetrically."""
        mock_calendar.sync_workout.return_value = "evt-new"
        test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        _generate_workouts()

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
        _generate_workouts()
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=today)], ["V2 Run"]
        )

        # --- Roll back to v1 ---
        result = coach_service.plan_rollback(objective_id=obj_id)

        self.assertEqual(result["to"]["id"], v1_id)
        self.assertEqual(result["from"]["id"], v2_id)
        self.assertEqual(test_db.get_macrocycle_for_objective(obj_id)["strategy"], "v1")
        # V1's workout is live again; V2's is an older sibling in the same slot.
        live = test_db.get_workouts(start_date=today)
        self.assertEqual([w["title"] for w in live], ["V1 Run"])
        self.assertEqual(result["restored_workouts"], 1)
        # The restored workout was re-pushed to Calendar.
        self.assertTrue(mock_calendar.sync_workout.called)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_rollback_without_history_raises(self, mock_client):
        """Rolling back a plan with no earlier version is rejected."""
        test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        # The block has to still be live for `workout generate` to have anything to
        # place into, so it rides on today like the goal does.
        mock_client.complete.return_value = {"strategy": strategy, "mesocycles": [{
            "name": "Base", "start_date": _days_out(-1),
            "end_date": _days_out(27), "focus": "Base",
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
        _generate_workouts()

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_workout_rollback_restores_batch_leaving_plan_active(
        self, mock_client, mock_calendar
    ):
        """`workout rollback` restores the archived batch and re-pushes it, without
        touching the active plan version (see DESIGN_plan_rollback.md §9)."""
        mock_calendar.sync_workout.return_value = "evt-new"
        test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        self.assertTrue(mock_calendar.sync_workout.called)

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_workout_rollback_within_one_plan_version(self, mock_client, mock_calendar):
        """Two regenerations under the same plan are separate changes, so a rollback
        undoes the second one (the case `plan rollback`'s version-keyed target cannot
        express)."""
        mock_calendar.sync_workout.return_value = "evt-new"
        test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        """Rolling back workouts with nothing written is rejected."""
        with self.assertRaises(ValueError):
            coach_service.workout_rollback()

    def test_rollback_leaves_slots_before_the_floor_alone(self):
        """A rollback restores nothing dated before the floor.

        Appending a copy into a past slot would silently make it the live session for a
        day already trained — the same rule the archive-and-restore model had, keyed to
        the change instead of the archive stamp (DESIGN_workout_revisions.md §10)."""
        save_workout(test_db,
            date="2026-06-01", sport_type="running", title="Old Mon", description="x"
        )
        save_workout(test_db,
            date="2026-06-05", sport_type="running", title="Old Fri", description="x"
        )
        changes = test_db.get_workout_changes()
        first_change = changes[-1]["id"]
        # A later generation rewrites both slots.
        with test_db.workout_change(kind="generate") as change:
            change.append(date="2026-06-01", sport_type="running",
                          title="New Mon", description="y")
            change.append(date="2026-06-05", sport_type="running",
                          title="New Fri", description="y")
        rewrite = test_db.get_workout_changes()[0]["id"]

        restored, _unhonored = test_db.rollback_to_change(rewrite, "2026-06-03")

        self.assertEqual([w["title"] for w in restored], ["Old Fri"])
        # One live session per slot: the past-dated Monday kept the newer session, because
        # the floor is what stops a rollback rewriting a day already trained.
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date="2026-06-01")],
            ["New Mon", "Old Fri"],
        )
        self.assertGreater(rewrite, first_change)

    @patch("trainmate.runtime.config")
    def test_load_science_guidelines(self, mock_config):
        temp_app_dir = tempfile.mkdtemp()
        temp_user_dir = tempfile.mkdtemp()

        try:
            mock_config.app_science_dir = temp_app_dir
            mock_config.science_dir = temp_user_dir

            with open(os.path.join(temp_app_dir, "app_science.md"), "w") as f:
                f.write("App guideline text")
            with open(os.path.join(temp_user_dir, "user_science.md"), "w") as f:
                f.write("User guideline text")
            with open(os.path.join(temp_user_dir, "notes.txt"), "w") as f:
                f.write("Ignored guideline text")

            guidelines = coach_service._load_science_guidelines()

            self.assertIn("--- app_science.md ---", guidelines)
            self.assertIn("App guideline text", guidelines)
            self.assertIn("--- user_science.md ---", guidelines)
            self.assertIn("User guideline text", guidelines)
            self.assertNotIn("Ignored guideline text", guidelines)
            # Each source gets its own banner, so the coach can tell whose material it is
            # reading (DESIGN_prompt_structure.md §3): the app's text must close out before
            # the athlete's banner opens.
            self.assertLess(
                guidelines.index("App guideline text"),
                guidelines.index("END OF TRAINMATE SPORTS SCIENCE GUIDELINES"),
            )
            self.assertLess(
                guidelines.index("END OF TRAINMATE SPORTS SCIENCE GUIDELINES"),
                guidelines.index("START OF ATHLETE-PROVIDED SPORTS SCIENCE GUIDELINES"),
            )
            self.assertLess(
                guidelines.index("START OF ATHLETE-PROVIDED SPORTS SCIENCE GUIDELINES"),
                guidelines.index("User guideline text"),
            )
        finally:
            shutil.rmtree(temp_app_dir)
            shutil.rmtree(temp_user_dir)

    @patch("trainmate.runtime.config")
    def test_science_guidelines_omit_banner_for_empty_source(self, mock_config):
        """A fresh install has no `science/` dir — it must produce no athlete banner at
        all, rather than an empty one (DESIGN_prompt_structure.md §3)."""
        temp_app_dir = tempfile.mkdtemp()
        try:
            mock_config.app_science_dir = temp_app_dir
            mock_config.science_dir = os.path.join(temp_app_dir, "does_not_exist")
            with open(os.path.join(temp_app_dir, "app_science.md"), "w") as f:
                f.write("App guideline text")

            guidelines = coach_service._load_science_guidelines()

            self.assertIn("START OF TRAINMATE SPORTS SCIENCE GUIDELINES", guidelines)
            self.assertNotIn("ATHLETE-PROVIDED", guidelines)
        finally:
            shutil.rmtree(temp_app_dir)

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

    def test_non_plan_shaping_profile_fields_do_not_flag_the_plan_stale(self):
        """`name` and `equipment` reach every prompt but cannot shape the periodization,
        so editing one must not propose a replan (DESIGN_plan_staleness.md §3)."""
        original_profile = dict(trainmate.coach.config.data["user_profile"])
        try:
            profile = trainmate.coach.config.data["user_profile"]
            profile["name"] = "Sam"
            profile["equipment"] = ["carbon road bike"]
            baseline = coach_service._get_config_hash()

            profile["name"] = "Alex"
            self.assertEqual(baseline, coach_service._get_config_hash())

            profile["equipment"] = ["carbon road bike", "rowing machine"]
            self.assertEqual(baseline, coach_service._get_config_hash())
        finally:
            trainmate.coach.config.data["user_profile"] = original_profile

    def test_weekly_schedule_is_fingerprinted_per_sub_key(self):
        """A day's kit shapes that day's session; its hours, session cap and certainty
        are load structure (DESIGN_plan_staleness.md §4)."""
        original_profile = dict(trainmate.coach.config.data["user_profile"])
        try:
            profile = trainmate.coach.config.data["user_profile"]
            profile["weekly_schedule"] = {
                "Monday": {
                    "total_available_hours": 1.5, "max_sessions": 1,
                    "certainty_percent": 100, "equipment": ["office gym"],
                }
            }
            monday = profile["weekly_schedule"]["Monday"]
            baseline = coach_service._get_config_hash()

            monday["equipment"] = ["rowing machine"]
            self.assertEqual(baseline, coach_service._get_config_hash())

            for key, value in (("total_available_hours", 2.5),
                               ("max_sessions", 2),
                               ("certainty_percent", 50)):
                with self.subTest(sub_key=key):
                    restore = monday[key]
                    monday[key] = value
                    self.assertNotEqual(baseline, coach_service._get_config_hash())
                    monday[key] = restore
        finally:
            trainmate.coach.config.data["user_profile"] = original_profile

    def test_plan_shaping_profile_fields_still_flag_the_plan_stale(self):
        """The other side of the partition — narrowing what triggers a replan must not
        have cost us the fields that genuinely reshape a periodization (§3)."""
        original_profile = dict(trainmate.coach.config.data["user_profile"])
        try:
            profile = trainmate.coach.config.data["user_profile"]
            profile.update({
                "birth_year": 1986, "weekly_target_hours": 8.0,
                "sport_preferences": ["cycling"], "chronic_injuries": "none",
                "preferences": "keep strength year-round",
            })
            baseline = coach_service._get_config_hash()

            for key, value in (("birth_year", 1956),
                               ("weekly_target_hours", 20.0),
                               ("sport_preferences", ["cycling", "swimming"]),
                               ("chronic_injuries", "left ACL reconstructed"),
                               ("preferences", "endurance only")):
                with self.subTest(field=key):
                    restore = profile[key]
                    profile[key] = value
                    self.assertNotEqual(baseline, coach_service._get_config_hash())
                    profile[key] = restore
        finally:
            trainmate.coach.config.data["user_profile"] = original_profile

    def test_stale_reason_names_the_profile_fields_that_moved(self):
        """config_hash answers "did something change", the snapshot answers "what" — so
        the athlete can judge the proposal without diffing config.yaml by hand (§5)."""
        original_profile = dict(trainmate.coach.config.data["user_profile"])
        try:
            profile = trainmate.coach.config.data["user_profile"]
            profile["sport_preferences"] = ["cycling"]
            profile["chronic_injuries"] = "none"
            profile["preferences"] = "keep strength year-round"
            macro = {
                "config_hash": coach_service._get_config_hash(),
                "config_snapshot": coach_service._get_config_snapshot(),
                "profile_snapshot": coach_service._get_profile_snapshot(),
            }
            self.assertIsNone(coach_service.config_changed(macro))

            profile["sport_preferences"] = ["cycling", "swimming"]
            self.assertEqual(
                coach_service.config_changed(macro),
                "athlete profile changed: sport_preferences",
            )

            # Several at once are all named, in a stable order.
            profile["preferences"] = "endurance only"
            self.assertEqual(
                coach_service.config_changed(macro),
                "athlete profile changed: preferences, sport_preferences",
            )

            # A field that disappears is named the same way as one that was edited —
            # the other two are put back so only the removal is left to report.
            profile["sport_preferences"] = ["cycling"]
            profile["preferences"] = "keep strength year-round"
            del profile["chronic_injuries"]
            self.assertEqual(
                coach_service.config_changed(macro),
                "athlete profile changed: chronic_injuries",
            )

            # A plan predating the snapshot column — or carrying an unreadable one —
            # cannot attribute the change, so it says only that there was one (§5).
            for snapshot in (None, "", "{not json"):
                with self.subTest(snapshot=snapshot):
                    self.assertEqual(
                        coach_service.config_changed(
                            dict(macro, profile_snapshot=snapshot)
                        ),
                        "athlete profile changed",
                    )
        finally:
            trainmate.coach.config.data["user_profile"] = original_profile

    def test_profile_snapshot_round_trips_through_the_database(self):
        """The reason can only name fields if the snapshot survives save and reload."""
        original_profile = dict(trainmate.coach.config.data["user_profile"])
        try:
            profile = trainmate.coach.config.data["user_profile"]
            profile["sport_preferences"] = ["cycling"]
            obj_id = test_db.add_objective(
                title="Snapshot round trip", target_date=GOAL_DATE,
                sport_type="cycling",
            )
            test_db.save_macrocycle(
                objective_id=obj_id, strategy="Build", goals_hash="g",
                constraints_hash="c", mesocycles=[],
                config_hash=coach_service._get_config_hash(),
                config_snapshot=coach_service._get_config_snapshot(),
                profile_snapshot=coach_service._get_profile_snapshot(),
            )
            macro = test_db.get_macrocycle_for_objective(obj_id)
            self.assertIsNone(coach_service.config_changed(macro))

            profile["sport_preferences"] = ["cycling", "hiking"]
            self.assertEqual(
                coach_service.config_changed(macro),
                "athlete profile changed: sport_preferences",
            )
        finally:
            trainmate.coach.config.data["user_profile"] = original_profile

    def test_config_changed_threshold_tolerance(self):
        # FTP now lives in the benchmark logbook, not config (DESIGN_benchmark_workouts
        # §3.4); drift is driven by recording newer results (latest row wins).
        original_profile = dict(trainmate.coach.config.data["user_profile"])
        try:
            test_db.add_benchmark_result(
                date="2026-06-01", sport_type="cycling",
                anchor_kind="ftp", value=220, unit="W",
            )
            macro = {
                "config_hash": coach_service._get_config_hash(),
                "config_snapshot": coach_service._get_config_snapshot(),
            }
            self.assertIsNone(coach_service.config_changed(macro))

            # Within the default 5% band: still current.
            test_db.add_benchmark_result(
                date="2026-06-15", sport_type="cycling",
                anchor_kind="ftp", value=228, unit="W",
            )
            self.assertIsNone(coach_service.config_changed(macro))

            # Past the band: stale, with the threshold named in the reason.
            test_db.add_benchmark_result(
                date="2026-07-01", sport_type="cycling",
                anchor_kind="ftp", value=250, unit="W",
            )
            reason = coach_service.config_changed(macro)
            self.assertIsNotNone(reason)
            self.assertIn("ftp", reason)

            # A newly recorded kind absent from the old snapshot is skipped (§3.5), not
            # read as instant drift — restore ftp to baseline first so it isn't the cause.
            test_db.add_benchmark_result(
                date="2026-07-02", sport_type="cycling",
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

    def test_e1rm_never_invalidates_a_periodization(self):
        """e1rm collides across lifts — the logbook has no per-exercise field, so a
        deadlift PR logged after a squat PR is one value jumping 70%. It must not trip a
        replan (DESIGN_intensity_distribution.md §10)."""
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="strength_training",
            anchor_kind="e1rm", value=102, unit="kg",
        )
        macro = {
            "config_hash": coach_service._get_config_hash(),
            "config_snapshot": coach_service._get_config_snapshot(),
        }
        self.assertIsNone(coach_service.config_changed(macro))

        # A different lift entirely — a 70% jump that would otherwise read as drift.
        test_db.add_benchmark_result(
            date="2026-07-01", sport_type="strength_training",
            anchor_kind="e1rm", value=175, unit="kg",
        )
        self.assertIsNone(coach_service.config_changed(macro))
        # It still reaches the coaching prompt; it just never invalidates the plan.
        self.assertEqual(coach_service.effective_thresholds()["e1rm"], 175.0)

    def test_db_config_hash_operations(self):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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

        test_db.update_macrocycle_config_hash(
            macro_id, "confhashABC", '{"ftp": 230.0}', '{"birth_year": 1986}'
        )
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro["profile_snapshot"], '{"birth_year": 1986}')

        # Re-stamping only the hash leaves both snapshots standing, so the plan never
        # loses what it was generated against.
        test_db.update_macrocycle_config_hash(macro_id, "confhashDEF")
        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro["config_hash"], "confhashDEF")
        self.assertEqual(macro["config_snapshot"], '{"ftp": 230.0}')
        self.assertEqual(macro["profile_snapshot"], '{"birth_year": 1986}')

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plans_a_goal_only_weeks_away(self, mock_client):
        # No lower bound on the plan window: a near goal gets a short macrocycle rather
        # than a refusal — how to periodize three weeks is the science docs' call.
        today = datetime.now(timezone.utc).date()
        target_date_str = (today + timedelta(weeks=3)).strftime("%Y-%m-%d")
        obj_id = test_db.add_objective(
            title="Short Goal", target_date=target_date_str,
            sport_type="running",
        )
        mock_client.complete.return_value = {
            "strategy": "Sharpen and taper",
            "mesocycles": [{
                "name": "Race Prep", "start_date": today.strftime("%Y-%m-%d"),
                "end_date": target_date_str, "focus": "Freshness",
            }],
        }

        proposal = coach_service.plan_generate(force=True)
        self.assertEqual(proposal["strategy"], "Sharpen and taper")
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj_id))

    def test_a_goal_whose_date_has_passed_is_no_longer_the_next_goal(self):
        """It used to stay "the next goal" forever, so `plan generate` refused with "no
        window to plan in" until the athlete marked it completed by hand. Completion is
        now the date's verdict (§12)."""
        pin_clock(self, "2026-07-31")
        past = test_db.add_objective(
            title="Yesterday's Race", target_date="2026-07-30",
            sport_type="running",
        )
        ahead = test_db.add_objective(
            title="Autumn Marathon", target_date="2026-10-30",
            sport_type="running",
        )

        self.assertEqual(test_db.get_active_objective()['id'], ahead)
        self.assertEqual([o['id'] for o in test_db.upcoming_objectives()], [ahead])
        # Still reachable by id — it just is not a planning target any more.
        self.assertIsNotNone(test_db.get_active_objective(past))

    def test_rejects_goal_on_or_before_plan_start(self):
        """Targeting the passed goal explicitly still gives the window error rather than a
        goal-not-found: the row is there, the window is not."""
        pin_clock(self, "2026-07-31")
        obj_id = test_db.add_objective(
            title="Yesterday's Race", target_date="2026-07-30",
            sport_type="running",
        )

        with self.assertRaises(ValueError) as ctx:
            coach_service.plan_generate(objective_id=obj_id)
        self.assertIn("no window to plan in", str(ctx.exception))

    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_far_goal_plans_one_macrocycle_to_the_goal(self, mock_client, mock_today):
        # A 30-week horizon is no longer split into interim goals: one macrocycle runs to
        # the goal itself, and the athlete's goal list is left alone.
        mock_today.return_value = "2026-07-31"
        obj_id = test_db.add_objective(
            title="Ultra Marathon", target_date="2027-02-26",
            sport_type="running",
        )
        mock_client.complete.return_value = {
            "strategy": "Long build",
            "mesocycles": [{
                "name": "Base Building", "start_date": "2026-07-31",
                "end_date": "2027-02-26", "focus": "Aerobic conditioning",
            }],
        }

        coach_service.plan_generate(force=True)
        prompt = mock_client.complete.call_args[0][0]
        self.assertIn("until the target\ngoal (2027-02-26)", prompt)
        self.assertNotIn("intermediate_goals", prompt)
        # No goals were invented along the way.
        self.assertEqual(len(test_db.get_objectives(status="active")), 1)
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj_id))

    @patch("trainmate.coach.engine.openrouter_client")
    def test_multi_goal_planning_and_deletion(self, mock_client):
        # Goal A must stay inside `goals_lookback_days` of today for B's plan to chain
        # off it, so both goals ride on today rather than on fixed dates.
        goal_a, goal_b = _days_out(61), _days_out(153)
        obj1_id = test_db.add_objective(
            title="Goal A", target_date=goal_a,
            sport_type="running",
        )
        obj2_id = test_db.add_objective(
            title="Goal B", target_date=goal_b,
            sport_type="running",
        )

        mock_client.complete.side_effect = [
            {
                "strategy": "Plan A strategy",
                "mesocycles": [{
                    "name": "Base Building A", "start_date": _days_out(4),
                    "end_date": goal_a, "focus": "Aerobic conditioning",
                }],
            },
            {
                "strategy": "Plan B strategy",
                "mesocycles": [{
                    "name": "Base Building B", "start_date": _days_out(62),
                    "end_date": goal_b, "focus": "Aerobic threshold",
                }],
            },
        ]

        proposal_a = coach_service.plan_generate(force=True, objective_id=obj1_id)
        strategy_a, mesos_a = proposal_a['strategy'], proposal_a['mesocycles']
        self.assertEqual(strategy_a, "Plan A strategy")
        self.assertEqual(mesos_a[0]["start_date"], _days_out(4))

        proposal_b = coach_service.plan_generate(force=True, objective_id=obj2_id)
        strategy_b, mesos_b = proposal_b['strategy'], proposal_b['mesocycles']
        self.assertEqual(strategy_b, "Plan B strategy")
        # Plan B starts the day after Goal A, not today: it chains off the earlier goal.
        self.assertIn(f"from {_days_out(62)} until",
                      mock_client.complete.call_args_list[1][0][0])

        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj1_id))
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj2_id))

        coach_service.plan_rm(obj1_id)
        self.assertIsNone(test_db.get_macrocycle_for_objective(obj1_id))
        self.assertIsNotNone(test_db.get_macrocycle_for_objective(obj2_id))

    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_recent_history_summary_periodization_plan(self, mock_client, mock_today_str):
        mock_today_str.return_value = "2026-06-18"
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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
        self.assertIn("## ATHLETE RECENT TRAINING SUMMARY (PAST 15 DAYS)", system_prompt)
        self.assertIn("Completed Workouts (Past 15 days):", system_prompt)
        self.assertIn("running: 1 sessions", system_prompt)
        self.assertIn("Resting Heart Rate: 55.0 bpm", system_prompt)

    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_recent_history_workout_generation(self, mock_client, mock_calendar, mock_today_str):
        mock_today_str.return_value = "2026-06-18"
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
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

        _generate_workouts()

        user_content = mock_client.complete.call_args[0][1]
        self.assertIn("## ATHLETE'S METRICS HISTORY (PAST 15 DAYS)", user_content)
        self.assertIn("RHR=55bpm, HRV=60ms", user_content)


class TestCompletedSeasonsReachTheReview(unittest.TestCase):
    """A goal whose date has passed keeps contributing its plan to the prior-training
    review. It used to be marked `completed` by hand — the one action that both unblocked
    planning and hid the season from it (DESIGN_backward_evaluation.md §12)."""

    TODAY = "2026-08-05"

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
        pin_clock(self, self.TODAY)

    def _last_season(self) -> None:
        """A goal raced on 2026-07-04, its plan, and the training that went into it."""
        obj_id = test_db.add_objective(
            title="Spring Hill Climb", target_date="2026-07-04",
            sport_type="cycling",
        )
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id, strategy="Spring build", goals_hash="g",
            constraints_hash="c",
            mesocycles=[{"name": "Spring Base", "start_date": "2026-06-08",
                         "end_date": "2026-07-04", "focus": "aerobic volume"}],
        )
        for i, day in enumerate(("2026-06-09", "2026-06-16")):
            save_workout(test_db,
                date=day, sport_type="cycling", title="Endurance",
                description="[Endurance]\n2h", duration_minutes=120, rpe=5, tss=100.0,
                source="generated", macrocycle_id=macro_id,
            )
            test_db.save_completed_activity(
                activity_id=f"s{i}", date=day, start_time=f"{day}T07:00:00",
                activity_name="Ride", activity_type="cycling", duration_sec=3600,
                distance_km=30.0, elevation_gain_m=200.0, avg_hr=140, max_hr=170,
                rpe=5, tss=100.0, zone1_sec=600, zone2_sec=3000,
            )

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_last_seasons_blocks_are_in_the_next_plans_prompt(
        self, mock_client, mock_calendar
    ):
        self._last_season()
        test_db.add_objective(
            title="Autumn Gran Fondo", target_date="2026-10-15",
            sport_type="cycling",
        )
        mock_client.complete.return_value = {
            "strategy": "Autumn build", "mesocycles": [
                {"name": "Base", "start_date": "2026-08-05", "end_date": "2026-09-01",
                 "focus": "volume"},
            ],
        }

        proposal = coach_service.plan_generate(force=True)

        self.assertEqual(proposal["goal"]["title"], "Autumn Gran Fondo")
        prompt = mock_client.complete.call_args_list[0][0][0]
        self.assertIn("Spring Base", prompt)
        self.assertIn('focus "aerobic volume"', prompt)
        self.assertIn("Weekly load", prompt)

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_an_archived_season_stays_out(self, mock_client, mock_calendar):
        """`archived` is the athlete saying it did not happen — the one thing the date
        cannot know, and the only reason the column still exists."""
        self._last_season()
        test_db.update_objective(1, status="archived")
        test_db.add_objective(
            title="Autumn Gran Fondo", target_date="2026-10-15",
            sport_type="cycling",
        )
        mock_client.complete.return_value = {
            "strategy": "Autumn build", "mesocycles": [
                {"name": "Base", "start_date": "2026-08-05", "end_date": "2026-09-01",
                 "focus": "volume"},
            ],
        }

        coach_service.plan_generate(force=True)

        self.assertNotIn("Spring Base", mock_client.complete.call_args_list[0][0][0])


class TestLearningsReachTheStrategyPrompt(unittest.TestCase):
    """`plan generate` builds its own system prompt rather than calling
    `_build_system_prompt`, and so was the one coach call that never saw the observations
    the analysis flow authors (DESIGN_backward_evaluation.md §10.1)."""

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

    @staticmethod
    def _strategy_prompt(mock_client) -> str:
        """The system prompt of the plan call — the first of the two `replan` makes."""
        return mock_client.complete.call_args_list[0][0][0]

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.service._today_str")
    @patch("trainmate.coach.engine.openrouter_client")
    def _generate(self, learning, mock_client, mock_today, mock_calendar):
        mock_today.return_value = "2026-06-01"
        test_db.add_objective(
            title="Berlin Marathon", target_date="2026-09-27", sport_type="running",
        )
        if learning:
            test_db.add_learning(**learning)
        mock_client.complete.return_value = {
            "strategy": "Simulated", "mesocycles": [
                {"name": "Base", "start_date": "2026-06-01", "end_date": "2026-06-28",
                 "focus": "Endurance"},
            ],
        }
        coach_service.plan_generate(force=True)
        return self._strategy_prompt(mock_client)

    def test_active_observations_are_rendered_with_their_tags(self):
        prompt = self._generate({
            "text": "Responds poorly to back-to-back threshold days",
            "sports": "running", "confidence": "established",
        })
        self.assertIn("ATHLETE-SPECIFIC OBSERVATIONS", prompt)
        self.assertIn("Responds poorly to back-to-back threshold days", prompt)
        self.assertIn("running", prompt)
        self.assertIn("established", prompt)

    def test_the_section_says_they_are_input_only(self):
        """Authoring stays with the analysis flow; nothing here may revise them (§11)."""
        prompt = self._generate({"text": "Sleeps badly after evening intensity"})
        self.assertIn("input only", prompt)

    def test_the_cold_start_placeholder_still_reaches_the_prompt(self):
        """No learnings yet renders the 'observe over time' placeholder, not an empty
        section — same text every other coach prompt gets."""
        prompt = self._generate(None)
        self.assertIn("ATHLETE-SPECIFIC OBSERVATIONS", prompt)
        self.assertIn("No observations yet", prompt)


class TestStaleAnalysisWarning(unittest.TestCase):
    """`plan generate` feeds the cached reconstruction to the strategy prompt but never
    recomputes it, so a stale cache shapes the plan silently unless it says so (§5)."""

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

    def _cache_ending(
        self, window_end: str, horizon: str = "long", window_start: str = "2026-01-01"
    ) -> None:
        # A reflect row must start past bootstrap's end to be read forward at all (§10.2),
        # so its start is explicit here rather than shared with bootstrap's.
        test_db.save_analysis_cache(
            horizon=horizon, fingerprint=f"fp-{horizon}", window_start=window_start,
            window_end=window_end, reconstruction={"macrocycle_summary": "Base build."},
        )

    def _warn(self, today: str) -> str:
        with patch("builtins.print") as mock_print:
            coach_service._maybe_warn_stale_analysis(today)
        return "\n".join(str(c[0][0]) for c in mock_print.call_args_list if c[0])

    def test_warns_once_the_lag_exceeds_the_configured_budget(self):
        self._cache_ending("2026-06-01")
        out = self._warn("2026-07-01")          # 30 days > the 14-day default
        self.assertIn("2026-06-01", out)
        self.assertIn("30 days ago", out)
        self.assertIn("data reflect", out)

    def test_quiet_while_the_cache_is_current(self):
        self._cache_ending("2026-06-25")
        self.assertEqual(self._warn("2026-07-01"), "")    # 6 days, inside the budget

    def test_quiet_when_there_is_nothing_cached(self):
        """A cold start is the bootstrap nudge's job, not this one's."""
        self.assertEqual(self._warn("2026-07-01"), "")

    def test_a_current_reflection_clears_a_stale_bootstrap(self):
        """The warning names `data reflect`, so it has to be judged over a window that
        `data reflect` can actually move — otherwise it repeats forever however diligently
        the athlete runs it (§10.2)."""
        self._cache_ending("2026-01-31")                     # bootstrap, months behind
        self._cache_ending("2026-06-25", horizon="short",    # reflect, caught up
                           window_start="2026-02-01")
        self.assertEqual(self._warn("2026-07-01"), "")

    def test_a_stale_reflection_still_warns_from_the_later_window(self):
        self._cache_ending("2026-01-31")
        self._cache_ending("2026-06-01", horizon="short", window_start="2026-02-01")
        out = self._warn("2026-07-01")
        self.assertIn("2026-06-01", out)                     # the later of the two
        self.assertNotIn("2026-01-31", out)


class TestPlanLineage(unittest.TestCase):
    """The retrospective block walk takes the previous *goal's* plan and never an earlier
    *version* of this goal's own (DESIGN_plan_rollback.md §6.1). `_blocks_in_window` used
    to call the version accessor, so `tm progress --blocks` reported every block twice
    after any regeneration — the two plans cover the same dates."""

    TODAY = "2026-08-05"

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
        pin_clock(self, self.TODAY)

    def _goal(self, title, target_date):
        return test_db.add_objective(
            title=title, target_date=target_date, sport_type="running",
        )

    def _plan(self, obj_id, strategy, blocks):
        return test_db.save_macrocycle(
            objective_id=obj_id, strategy=strategy, goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": name, "start_date": start, "end_date": end,
                         "focus": "aerobic"} for name, start, end in blocks],
        )

    def test_regenerating_does_not_duplicate_the_blocks(self):
        """The regression: v1 and v2 span the same dates, so walking both reports every
        calendar period twice and counts each activity into two blocks."""
        from trainmate.cli.progress import _blocks_in_window

        obj = self._goal("Autumn Marathon", "2026-09-20")
        self._plan(obj, "v1", [("V1 Base", "2026-06-01", "2026-06-28"),
                               ("V1 Build", "2026-06-29", "2026-07-26")])
        v2 = self._plan(obj, "v2", [("V2 Base", "2026-06-01", "2026-06-28"),
                                    ("V2 Build", "2026-06-29", "2026-07-26")])

        blocks = _blocks_in_window(test_db, "2026-06-01", self.TODAY)
        self.assertEqual([b["name"] for b in blocks], ["V2 Base", "V2 Build"])
        self.assertEqual({b["macrocycle_id"] for b in blocks}, {v2})

    def test_the_previous_goals_plan_is_walked(self):
        """The other half: a window reaching back past the current plan's first block
        lands in the previous goal's plan, which nothing else supplies."""
        from trainmate.cli.progress import _blocks_in_window

        spring = self._goal("Spring 10k", "2026-05-31")
        self._plan(spring, "spring", [("Spring Base", "2026-04-06", "2026-05-31")])
        autumn = self._goal("Autumn Marathon", "2026-09-20")
        self._plan(autumn, "autumn", [("Autumn Base", "2026-06-01", "2026-06-28")])

        blocks = _blocks_in_window(test_db, "2026-04-06", self.TODAY)
        self.assertEqual([b["name"] for b in blocks], ["Spring Base", "Autumn Base"])

    def test_preceding_macrocycle_skips_superseded_versions(self):
        """`get_preceding_macrocycle` reaches the previous goal's *active* plan, not
        whichever version of it happens to be newest."""
        spring = self._goal("Spring 10k", "2026-05-31")
        self._plan(spring, "spring v1", [("Old", "2026-04-06", "2026-05-31")])
        spring_v2 = self._plan(spring, "spring v2", [("New", "2026-04-06", "2026-05-31")])
        autumn = self._goal("Autumn Marathon", "2026-09-20")

        preceding = test_db.get_preceding_macrocycle(autumn)
        self.assertEqual(preceding["id"], spring_v2)
        self.assertEqual(preceding["strategy"], "spring v2")

    def test_preceding_macrocycle_is_none_for_the_earliest_goal(self):
        first = self._goal("Spring 10k", "2026-05-31")
        self._plan(first, "spring", [("Base", "2026-04-06", "2026-05-31")])
        self.assertIsNone(test_db.get_preceding_macrocycle(first))

    def test_the_two_previous_plan_accessors_disagree_on_purpose(self):
        """The naming fix, pinned: same goal, two accessors, opposite answers."""
        spring = self._goal("Spring 10k", "2026-05-31")
        spring_macro = self._plan(spring, "spring", [("S", "2026-04-06", "2026-05-31")])
        autumn = self._goal("Autumn Marathon", "2026-09-20")
        autumn_v1 = self._plan(autumn, "autumn v1", [("A1", "2026-06-01", "2026-06-28")])
        self._plan(autumn, "autumn v2", [("A2", "2026-06-01", "2026-06-28")])

        self.assertEqual(
            test_db.get_previous_macrocycle_version(autumn)["id"], autumn_v1
        )
        self.assertEqual(test_db.get_preceding_macrocycle(autumn)["id"], spring_macro)


class TestDateKeyedGeneration(unittest.TestCase):
    """`workout generate` reads its periodization off the dates it is writing, not off a
    goal the caller names (DESIGN_cli_selectors.md §8). The goal used to be an
    indirection to the macrocycle and nothing more, which meant the earliest goal's plan
    shaped the sessions even when a different plan governed the days in question."""

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

    def _goal(self, title, target_date):
        return test_db.add_objective(
            title=title, target_date=target_date, sport_type="running",
        )

    def _plan(self, obj_id, strategy, blocks):
        return test_db.save_macrocycle(
            objective_id=obj_id, strategy=strategy, goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": name, "start_date": start, "end_date": end,
                         "focus": f"focus of {name}"} for name, start, end in blocks],
        )

    @staticmethod
    def _one_session_response(date_str):
        return {
            "reasoning": "why",
            "workouts": [{
                "date": date_str, "sport_type": "running",
                "title": "Run", "description": "[Run]\n30 mins",
            }],
        }

    # --- the DB rule on its own ---------------------------------------------------

    def test_sequential_plans_both_govern_a_long_window(self):
        """A horizon can legitimately run out of one goal's last block into the next
        goal's first, so neither plan is dropped."""
        first = self._goal("Spring 10k", _days_out(30))
        self._plan(first, "spring", [("Base", _days_out(0), _days_out(30))])
        second = self._goal("Autumn Marathon", _days_out(90))
        self._plan(second, "autumn", [("Build", _days_out(31), _days_out(90))])

        blocks, dropped = test_db.get_governing_mesocycles(_days_out(0), _days_out(60))
        self.assertEqual([b["name"] for b in blocks], ["Base", "Build"])
        self.assertEqual(dropped, [])

    def test_plans_covering_the_same_days_are_settled_by_recency(self):
        """Two plans cannot both be followed on one day; the more recently generated one
        wins, the same tiebreak get_periodization_ids_for_date makes."""
        first = self._goal("Spring 10k", _days_out(40))
        old = self._plan(first, "spring", [("Base", _days_out(0), _days_out(40))])
        second = self._goal("Autumn Marathon", _days_out(90))
        new = self._plan(second, "autumn", [("Build", _days_out(0), _days_out(90))])

        blocks, dropped = test_db.get_governing_mesocycles(_days_out(0), _days_out(30))
        self.assertEqual([b["macrocycle_id"] for b in blocks], [new])
        self.assertEqual(dropped, [old])

        # Naming a plan settles it the other way instead.
        blocks, dropped = test_db.get_governing_mesocycles(
            _days_out(0), _days_out(30), prefer_macro_id=old
        )
        self.assertEqual([b["macrocycle_id"] for b in blocks], [old])
        self.assertEqual(dropped, [new])

    def test_a_window_no_block_covers_falls_back_rather_than_answering_empty(self):
        """A plan that has run out still answers, so generation reports "no strategy"
        only when there is genuinely none."""
        goal = self._goal("Spring 10k", _days_out(-5))
        self._plan(goal, "spring", [("Base", _days_out(-40), _days_out(-10))])

        blocks, dropped = test_db.get_governing_mesocycles(_days_out(0), _days_out(27))
        self.assertEqual([b["name"] for b in blocks], ["Base"])
        self.assertEqual(dropped, [])

        clear_all_tables(test_db)
        self.assertEqual(
            test_db.get_governing_mesocycles(_days_out(0), _days_out(27)), ([], [])
        )

    def test_the_strict_readers_answer_only_with_blocks_that_cover_their_target(self):
        """The strict question, for every caller that goes on to treat the answer as
        covering what it asked about — a block that does not contain the target is not
        something to name (DESIGN_constraint_honoring.md §4.1). The window form is
        internal to get_governing_mesocycles; the single-date form is the public reader
        the constraint messages use.
        """
        goal = self._goal("Spring 10k", _days_out(-5))
        self._plan(goal, "spring", [("Base", _days_out(-40), _days_out(-10))])

        self.assertEqual(
            test_db._get_covering_mesocycles(_days_out(0), _days_out(27)),
            ([], []),
        )
        self.assertIsNone(test_db.get_covering_mesocycle(_days_out(0)))
        # The governing readers still fall back, because `generate` depends on it.
        self.assertIsNotNone(test_db.get_active_mesocycle(_days_out(0)))
        # And a window a block really does cover answers with it through either reader.
        covered = _days_out(-20)
        self.assertEqual(
            [b["name"] for b in test_db.get_governing_mesocycles(covered, covered)[0]],
            ["Base"],
        )

    def test_single_date_readers_prefer_the_newest_plan_on_overlapping_days(self):
        """Overlapping plans are settled by recency in every reader, so the block a
        message names and the block a workout is stamped with are the same one."""
        first = self._goal("Spring 10k", _days_out(40))
        self._plan(first, "spring", [("Base", _days_out(0), _days_out(40))])
        second = self._goal("Autumn Marathon", _days_out(90))
        new = self._plan(second, "autumn", [("Build", _days_out(0), _days_out(90))])

        covering = test_db.get_covering_mesocycle(_days_out(10))
        self.assertEqual(covering["macrocycle_id"], new)
        self.assertEqual(test_db.get_active_mesocycle(_days_out(10))["id"], covering["id"])
        ids = test_db.get_periodization_ids_for_date(_days_out(10))
        self.assertEqual(ids[1], new)
        self.assertEqual(ids[2], covering["id"])

    # --- what generation actually does with it ------------------------------------

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_the_plan_covering_today_shapes_the_sessions_not_the_earliest_goal(
        self, mock_client, _mock_calendar
    ):
        """The regression the goal indirection caused: the nearest goal's plan was used
        even when its blocks were long finished and another plan covered today."""
        stale = self._goal("Club 10k", _days_out(20))
        self._plan(stale, "STALE STRATEGY", [("Old", _days_out(-60), _days_out(-30))])
        live = self._goal("Autumn Marathon", _days_out(90))
        self._plan(live, "LIVE STRATEGY", [("Build", _days_out(-1), _days_out(60))])

        mock_client.complete.return_value = self._one_session_response(_days_out(1))
        _generate_workouts()

        system_prompt = mock_client.complete.call_args.args[0]
        self.assertIn("LIVE STRATEGY", system_prompt)
        self.assertNotIn("STALE STRATEGY", system_prompt)
        # The covered block is marked, so the model no longer has to infer which blocks
        # the span falls in from the dates alone.
        self.assertIn("> Build", system_prompt)

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_a_span_crossing_two_plans_tags_each_session_with_its_own(
        self, mock_client, _mock_calendar
    ):
        """`plan rollback` accounting keys off macrocycle_id, so a session belongs to the
        plan governing its date — not to one id stamped across the whole batch."""
        first = self._goal("Spring 10k", _days_out(20))
        early = self._plan(first, "spring", [("Base", _days_out(0), _days_out(20))])
        second = self._goal("Autumn Marathon", _days_out(90))
        late = self._plan(second, "autumn", [("Build", _days_out(21), _days_out(90))])

        mock_client.complete.return_value = {
            "reasoning": "why",
            "workouts": [
                {"date": _days_out(5), "sport_type": "running",
                 "title": "Early", "description": "[Early]\n30 mins"},
                {"date": _days_out(40), "sport_type": "running",
                 "title": "Late", "description": "[Late]\n30 mins"},
            ],
        }
        _, workouts = _generate_workouts(end_date=_days_out(60))

        by_title = {w["title"]: w for w in workouts}
        self.assertEqual(by_title["Early"]["macrocycle_id"], early)
        self.assertEqual(by_title["Late"]["macrocycle_id"], late)
        # Both strategies reached the prompt: the span is genuinely governed by both.
        system_prompt = mock_client.complete.call_args.args[0]
        self.assertIn("spring", system_prompt)
        self.assertIn("autumn", system_prompt)

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_a_horizon_past_the_last_block_says_so(self, mock_client, _mock_calendar):
        """Days past the plan's end have no block to follow — a date-keyed view can see
        that and say it, where the goal-keyed one could not."""
        goal = self._goal("Autumn Marathon", _days_out(90))
        self._plan(goal, "autumn", [("Base", _days_out(0), _days_out(20))])

        mock_client.complete.return_value = self._one_session_response(_days_out(1))
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            coach_service.workout_generate(end_date=_days_out(60))
        self.assertIn("The plan runs out on", out.getvalue())
        self.assertIn(_days_out(20), out.getvalue())

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_no_plan_at_all_still_names_plan_generate(self, mock_client, _mock_calendar):
        self._goal("Autumn Marathon", _days_out(90))
        with self.assertRaises(ValueError) as ctx:
            coach_service.workout_generate()
        self.assertIn("plan generate", str(ctx.exception))
        mock_client.complete.assert_not_called()


class TestGenerationSpanIsBounded(unittest.TestCase):
    """The selectors pick the whole span `workout generate` writes, both ends of it
    (DESIGN_cli_selectors.md §8). A bounded regeneration rebuilds the days it was given
    and leaves every other day of the plan exactly as it was."""

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
        obj_id = test_db.add_objective(
            title="Autumn Marathon", target_date=_days_out(90), sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="build", goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": "Base", "start_date": _days_out(0),
                         "end_date": _days_out(90), "focus": "aerobic"}],
        )

    @staticmethod
    def _response(*dates):
        return {
            "reasoning": "why",
            "workouts": [
                {"date": d, "sport_type": "running", "title": f"Run {d}",
                 "description": f"[Run {d}]\n30 mins"}
                for d in dates
            ],
        }

    @staticmethod
    def _existing(date_str, title):
        save_workout(
            test_db, date=date_str, sport_type="running", title=title,
            description=f"[{title}]\n30 mins", duration_minutes=30,
        )

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_the_span_opens_where_the_caller_put_it(self, mock_client, _mock_calendar):
        """A block that starts next month is generated from its own first day: `-m` names
        a span, not a horizon reaching back to today."""
        mock_client.complete.return_value = self._response(_days_out(31))
        proposal = coach_service.workout_generate(
            start_date=_days_out(30), end_date=_days_out(40)
        )

        self.assertEqual(proposal.gen_start, _days_out(30))
        self.assertEqual(proposal.gen_end, _days_out(40))
        # The model is told which day to start on, so it does not lay sessions from today.
        prompt = "".join(str(a) for a in mock_client.complete.call_args.args)
        self.assertIn(f"starting from {_days_out(30)}", prompt)

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_the_span_never_opens_in_the_past(self, mock_client, _mock_calendar):
        """A block already under way is regenerated from today: yesterday is history."""
        mock_client.complete.return_value = self._response(_days_out(1))
        proposal = coach_service.workout_generate(
            start_date=_days_out(-10), end_date=_days_out(10)
        )
        self.assertEqual(proposal.gen_start, _days_out(0))
        self.assertEqual(proposal.gen_end, _days_out(10))

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_only_the_sessions_inside_the_span_are_displaced(
        self, mock_client, _mock_calendar
    ):
        """The days either side of the span keep the sessions they already have — before
        §8 the rebuild ran from its start with no end and cancelled all of them."""
        self._existing(_days_out(2), "Before")
        self._existing(_days_out(35), "Inside")
        self._existing(_days_out(50), "After")

        mock_client.complete.return_value = self._response(_days_out(31))
        proposal = coach_service.workout_generate(
            start_date=_days_out(30), end_date=_days_out(40)
        )
        self.assertEqual([w["title"] for w in proposal.displaced], ["Inside"])

        coach_service.workout_generate_apply(proposal)
        live = {w["title"] for w in test_db.get_workouts(start_date=_days_out(0))}
        self.assertIn("Before", live)
        self.assertIn("After", live)
        # A session the rebuilt span no longer holds is still cancelled inside it.
        self.assertNotIn("Inside", live)
        self.assertIn(f"Run {_days_out(31)}", live)

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_no_span_at_all_is_the_config_horizon_from_today(
        self, mock_client, _mock_calendar
    ):
        """With no selector the span is still bounded at both ends, so the default run
        behaves like every other one."""
        mock_client.complete.return_value = self._response(_days_out(1))
        proposal = coach_service.workout_generate()
        self.assertEqual(proposal.gen_start, _days_out(0))
        self.assertEqual(
            proposal.gen_end, _days_out(config.workout_generation_span_days - 1)
        )

class TestEasedSessionsAreCarriedIntoGeneration(unittest.TestCase):
    """A regeneration rewrites the horizon from scratch, so a session `workout adapt`
    already eased is handed back at full load unless it is carried into the prompt and
    the model is given a way to say "leave this one" (DESIGN_workout_revisions.md §7.1).
    Only the eased sessions are carried: the rest of the window is still the model's."""

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
        obj_id = test_db.add_objective(
            title="Autumn Marathon", target_date=_days_out(90), sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="build", goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": "Base", "start_date": _days_out(0),
                         "end_date": _days_out(60), "focus": "aerobic"}],
        )

    # --- fixtures -----------------------------------------------------------------

    @staticmethod
    def _eased(date_str, **extra):
        """A session whose live numbers are the reduced form of an earlier prescription —
        `original_*` is what makes the helper write the adapt revision over a generate
        one, which is what the tally reads."""
        save_workout(
            test_db, date=date_str, sport_type="cycling", title="Easy Z2 Spin",
            description="[Easy Z2 Spin]\n40 min ERG-locked, no surges.",
            duration_minutes=40, rpe=3, tss=26,
            planned_zone_currency="power", planned_zone_sec=[600, 1800, 0, 0, 0, 0, 0],
            original_duration_minutes=90, original_rpe=7, original_tss=110,
            modification_reason="Cut to easy Z2 to shed intensity.", **extra,
        )

    @staticmethod
    def _untouched(date_str):
        save_workout(
            test_db, date=date_str, sport_type="running", title="Steady Run",
            description="[Steady Run]\n50 min", duration_minutes=50, rpe=5, tss=45,
        )

    @staticmethod
    def _keep(date_str, sport="cycling"):
        return {"date": date_str, "sport_type": sport, "keep": True}

    @staticmethod
    def _written(date_str, sport="running", title="Steady Run"):
        return {
            "date": date_str, "sport_type": sport, "title": title,
            "description": f"[{title}]\n50 min", "duration_minutes": 50,
            "rpe": 5, "tss": 45,
        }

    def _run(self, *entries):
        """`workout generate` end to end against a fixed model response, as the CLI does
        on a `y`. Returns the proposal and the user content the model was sent."""
        with patch("trainmate.runtime.calendar_syncer"), \
                patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = {
                "reasoning": "why", "workouts": list(entries),
            }
            proposal = coach_service.workout_generate()
            user = client.complete.call_args.args[1]
            coach_service.workout_generate_apply(proposal)
        return proposal, user

    # --- what reaches the prompt --------------------------------------------------

    def test_an_eased_session_reaches_the_prompt_with_its_tag(self):
        self._eased(_days_out(3))
        _proposal, user = self._run()
        self.assertIn("## SESSIONS ALREADY EASED BY AN ADAPTATION", user)
        self.assertIn("Easy Z2 Spin", user)
        self.assertIn("[ALREADY EASED by a prior adaptation", user)
        self.assertIn("Cut to easy Z2 to shed intensity.", user)

    def test_the_intensity_target_reaches_the_prompt(self):
        """The zone columns are what let the model weigh a carried day against the week
        it is writing around it; duration and TSS cannot (§7.1)."""
        self._eased(_days_out(3))
        _proposal, user = self._run()
        self.assertIn("Target: ~10min recovery, ~30min endurance", user)

    def test_the_description_is_not_shipped(self):
        """A KEEP names the session rather than copying it, so the prompt pays for the
        metadata only."""
        self._eased(_days_out(3))
        _proposal, user = self._run()
        self.assertNotIn("ERG-locked, no surges.", user)

    def test_a_session_nothing_eased_is_not_carried(self):
        """The section is the easings, not the plan: carrying the untouched days would
        make generate a re-run of adapt."""
        self._eased(_days_out(3))
        self._untouched(_days_out(4))
        _proposal, user = self._run()
        self.assertIn("Easy Z2 Spin", user)
        self.assertNotIn("Steady Run", user)

    def test_nothing_eased_leaves_the_prompt_as_it_was(self):
        self._untouched(_days_out(4))
        _proposal, user = self._run()
        self.assertNotIn("SESSIONS ALREADY EASED", user)

    def test_a_cancelled_session_is_not_carried(self):
        """`workout rm` ended the session; an easing it carried is not a claim on the
        regenerated plan."""
        self._eased(_days_out(3), removed=True, removed_reason="travelling")
        _proposal, user = self._run()
        self.assertNotIn("SESSIONS ALREADY EASED", user)

    # --- what a KEEP does ---------------------------------------------------------

    def test_a_kept_session_is_left_exactly_as_it_stands(self):
        """The point of the action: no revision, so the prescription, the Calendar event
        and — via §7's tally — the session's standing as already-eased all survive. A
        rewrite that merely reproduced the load would reset the tally and the NEXT
        regeneration would restore it to full."""
        self._eased(_days_out(3))
        before = test_db.get_workout(_days_out(3), "cycling")
        self._run(self._keep(_days_out(3)))
        after = test_db.get_workout(_days_out(3), "cycling")
        self.assertEqual(after["revision_id"], before["revision_id"])
        self.assertEqual(after["title"], "Easy Z2 Spin")
        self.assertEqual(after["duration_minutes"], 40)
        self.assertEqual(after["planned_zone2_sec"], 1800)
        self.assertEqual(after["adaptation_count"], 1)

    def test_a_kept_session_survives_a_second_regeneration(self):
        """The durability the action buys, asserted rather than reasoned about."""
        self._eased(_days_out(3))
        before = test_db.get_workout(_days_out(3), "cycling")
        self._run(self._keep(_days_out(3)))
        _proposal, user = self._run(self._keep(_days_out(3)))
        self.assertIn("Easy Z2 Spin", user, "still carried on the second pass")
        after = test_db.get_workout(_days_out(3), "cycling")
        self.assertEqual(after["revision_id"], before["revision_id"])

    def test_the_rest_of_the_window_is_still_rewritten(self):
        """A KEEP claims one slot, not the horizon."""
        self._eased(_days_out(3))
        self._untouched(_days_out(4))
        self._run(self._keep(_days_out(3)), self._written(_days_out(5)))
        self.assertTrue(test_db.get_workout(_days_out(4), "running")["removed"])
        self.assertIsNotNone(test_db.get_workout(_days_out(5), "running"))

    def test_writing_the_day_out_beats_keeping_it(self):
        """The model may decide the easing has served its purpose; an explicit session
        for the same slot is that decision, and it wins."""
        self._eased(_days_out(3))
        before = test_db.get_workout(_days_out(3), "cycling")
        self._run(
            self._keep(_days_out(3)),
            self._written(_days_out(3), sport="cycling", title="Threshold 2x20"),
        )
        after = test_db.get_workout(_days_out(3), "cycling")
        self.assertEqual(after["title"], "Threshold 2x20")
        self.assertNotEqual(after["revision_id"], before["revision_id"])

    def test_a_keep_naming_a_session_that_was_not_carried_is_dropped(self):
        """It claims a slot nothing would then write. Dropping it archives the day like
        any other the plan does not fill — a silently blank day is the worse failure."""
        self._eased(_days_out(3))
        self._untouched(_days_out(4))
        self._run(self._keep(_days_out(4), sport="running"))
        self.assertTrue(test_db.get_workout(_days_out(4), "running")["removed"])

    def test_a_forced_rest_window_still_overrides_a_keep(self):
        """The deterministic rest pass runs after the KEEP is resolved, so a barred date
        is barred whatever the model asked to keep (DESIGN_constraints.md §6)."""
        self._eased(_days_out(3))
        test_db.add_constraint(
            title="Travel", start_date=_days_out(3), end_date=_days_out(3), rest=1,
        )
        self._run(self._keep(_days_out(3)))
        self.assertTrue(test_db.get_workout(_days_out(3), "cycling")["removed"])
        rest = test_db.get_workout(_days_out(3), "rest")
        self.assertIsNotNone(rest)
        self.assertFalse(rest["removed"])


class TestGoalArchivalStandsSessionsDown(unittest.TestCase):
    """Calling a goal off stands its future sessions down without destroying anything
    (DESIGN_backward_evaluation.md §14)."""

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

    def _goal_with_plan(self, title: str, target: str, start: str, end: str):
        """A goal plus a one-block plan, returned as (objective_id, macrocycle_id)."""
        oid = test_db.add_objective(title, target, "running", "", 1)
        mid = test_db.save_macrocycle(
            oid, f"{title} strategy", "gh", "ch",
            [{"name": "Base", "start_date": start, "end_date": end, "focus": "aerobic"}],
        )
        return oid, mid

    @patch("trainmate.runtime.calendar_syncer")
    def test_archiving_one_goal_spares_the_other_goals_sessions(self, _cal):
        """The sweep is scoped by plan version, not by date: both sessions sit in the
        same future window, and only the called-off goal's is stood down."""
        a_oid, a_mid = self._goal_with_plan(
            "Race A", _days_out(60), _days_out(0), _days_out(30)
        )
        _b_oid, b_mid = self._goal_with_plan(
            "Race B", _days_out(120), _days_out(31), _days_out(90)
        )
        save_workout(test_db, _days_out(3), "running", "A session", "x",
                             macrocycle_id=a_mid)
        save_workout(test_db, _days_out(5), "cycling", "B session", "x",
                             macrocycle_id=b_mid)

        result = coach_service.goal_archive(a_oid)

        self.assertEqual(result["archived_workouts"], 1)
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=_days_out(0))],
            ["B session"],
        )

    @patch("trainmate.runtime.calendar_syncer")
    def test_archiving_leaves_untagged_sessions_in_place_and_counts_them(self, _cal):
        """An untagged session belongs to no plan version, so no goal may claim it."""
        oid, mid = self._goal_with_plan(
            "Race A", _days_out(60), _days_out(0), _days_out(30)
        )
        save_workout(test_db, _days_out(3), "running", "Tagged", "x", macrocycle_id=mid)
        # Beyond every block, so save_workout finds no plan to tag it with.
        save_workout(test_db, _days_out(45), "running", "Untagged", "x")

        result = coach_service.goal_archive(oid)

        self.assertEqual(result["archived_workouts"], 1)
        self.assertEqual(result["untagged"], 1)
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=_days_out(0))],
            ["Untagged"],
        )

    @patch("trainmate.runtime.calendar_syncer")
    def test_archiving_leaves_the_training_already_done(self, _cal):
        """A called-off race does not un-train the work behind the athlete."""
        oid, mid = self._goal_with_plan(
            "Race A", _days_out(60), _days_out(-30), _days_out(30)
        )
        save_workout(test_db, _days_out(-5), "running", "Done", "x", macrocycle_id=mid)
        save_workout(test_db, _days_out(5), "running", "Ahead", "x", macrocycle_id=mid)

        result = coach_service.goal_archive(oid)

        self.assertEqual(result["archived_workouts"], 1)
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=_days_out(-30))],
            ["Done"],
        )

    @patch("trainmate.runtime.calendar_syncer")
    def test_archiving_keeps_the_plan_its_versions_and_its_feedback(self, _cal):
        """The whole point of archiving over deleting: the history survives."""
        oid, mid = self._goal_with_plan(
            "Race A", _days_out(60), _days_out(0), _days_out(30)
        )
        test_db.add_plan_feedback(mid, "too much volume in week 3")
        save_workout(test_db, _days_out(3), "running", "A session", "x",
                             macrocycle_id=mid)

        coach_service.goal_archive(oid)

        self.assertIsNotNone(test_db.get_macrocycle(mid))
        self.assertEqual(len(test_db.get_macrocycle_versions(oid)), 1)
        self.assertEqual(len(test_db.get_mesocycles_for_macrocycle(mid)), 1)
        self.assertEqual(len(test_db.list_plan_feedback(mid)), 1)

    @patch("trainmate.runtime.calendar_syncer")
    def test_reinstating_brings_the_stood_down_sessions_back(self, _cal):
        """`goal_reinstate` is the exact mirror of `goal_archive`."""
        oid, mid = self._goal_with_plan(
            "Race A", _days_out(60), _days_out(0), _days_out(30)
        )
        save_workout(test_db, _days_out(3), "running", "A session", "x",
                             macrocycle_id=mid)
        coach_service.goal_archive(oid)
        self.assertEqual(test_db.get_workouts(start_date=_days_out(0)), [])

        result = coach_service.goal_reinstate(oid)

        self.assertEqual(result["restored_workouts"], 1)
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=_days_out(0))],
            ["A session"],
        )

    @patch("trainmate.runtime.calendar_syncer")
    def test_reinstating_recovers_only_what_is_still_ahead(self, _cal):
        """A goal reinstated late gets back the sessions still in front of it, not the
        ones whose dates passed while it was called off (DESIGN_plan_rollback.md §9)."""
        oid, mid = self._goal_with_plan(
            "Race A", _days_out(60), _days_out(-30), _days_out(30)
        )
        save_workout(test_db, _days_out(2), "running", "Soon", "x", macrocycle_id=mid)
        save_workout(test_db, _days_out(9), "cycling", "Later", "x", macrocycle_id=mid)
        coach_service.goal_archive(oid)

        # The clock moves past the first session while the goal is archived.
        with patch("trainmate.coach.service._today_str", return_value=_days_out(5)):
            result = coach_service.goal_reinstate(oid)

        self.assertEqual(result["restored_workouts"], 1)
        self.assertEqual(
            [w["title"] for w in test_db.get_workouts(start_date=_days_out(-30))],
            ["Later"],
        )


class TestBlockContiguityRepair(unittest.TestCase):
    """Within one plan, blocks are contiguous by construction: save_macrocycle repairs
    model-authored dates instead of trusting them (DOMAIN_MODEL.md §4). End dates stay
    authoritative; starts are re-derived, and a swallowed block is dropped."""

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

    def _save(self, blocks):
        obj = test_db.add_objective(
            title="Goal", target_date=_days_out(90), sport_type="running",
        )
        macro = test_db.save_macrocycle(
            objective_id=obj, strategy="s", goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": name, "start_date": start, "end_date": end,
                         "focus": "f"} for name, start, end in blocks],
        )
        return test_db.get_mesocycles_for_macrocycle(macro)

    def test_contiguous_blocks_are_stored_untouched(self):
        stored = self._save([("Base", _days_out(0), _days_out(27)),
                             ("Build", _days_out(28), _days_out(55))])
        self.assertEqual([(b["start_date"], b["end_date"]) for b in stored],
                         [(_days_out(0), _days_out(27)), (_days_out(28), _days_out(55))])

    def test_a_gap_between_blocks_is_closed_at_save(self):
        stored = self._save([("Base", _days_out(0), _days_out(27)),
                             ("Build", _days_out(33), _days_out(55))])
        self.assertEqual(stored[1]["start_date"], _days_out(28))
        self.assertEqual(stored[1]["end_date"], _days_out(55))

    def test_overlapping_blocks_are_redated_at_save(self):
        stored = self._save([("Base", _days_out(0), _days_out(27)),
                             ("Build", _days_out(20), _days_out(55))])
        self.assertEqual(stored[1]["start_date"], _days_out(28))

    def test_a_block_its_predecessor_swallows_is_dropped(self):
        stored = self._save([("Base", _days_out(0), _days_out(27)),
                             ("Blip", _days_out(10), _days_out(20)),
                             ("Build", _days_out(28), _days_out(55))])
        self.assertEqual([b["name"] for b in stored], ["Base", "Build"])

    def test_repair_names_what_it_changed_and_is_idempotent(self):
        blocks = [
            {"name": "Base", "start_date": _days_out(0), "end_date": _days_out(27),
             "focus": "f"},
            {"name": "Build", "start_date": _days_out(33), "end_date": _days_out(55),
             "focus": "f"},
        ]
        repaired, notes = repair_block_contiguity(blocks)
        self.assertEqual(len(notes), 1)
        self.assertIn("Build", notes[0])
        self.assertIn("gap", notes[0])
        again, no_notes = repair_block_contiguity(repaired)
        self.assertEqual(no_notes, [])
        self.assertEqual(again, repaired)
        # The input list is not mutated: the caller may still display what the model said.
        self.assertEqual(blocks[1]["start_date"], _days_out(33))


if __name__ == "__main__":
    unittest.main()
