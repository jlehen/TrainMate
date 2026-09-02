"""`bot` command family tests: morning-push idempotency, adapt_first, and the
free-text router (DESIGN_bot_simple_frontend.md §4.2, §5.3)."""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db, save_workout

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_bot.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

from trainmate import runtime
from trainmate.cli.bot import MORNING_MARKER, PUSH_ALL_DONE_LINE
from trainmate.cli.render import SIMPLE_DONE_LINE
from trainmate.config import config
from trainmate.prompt import BUTTONS_SENTINEL
from trainmate.util import today_str

# One database for the whole module: the three test classes below share it and
# clear its tables per test, so its lifecycle is module-level, not per-class.
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


def tearDownModule():
    try:
        os.remove(TEST_DB_PATH)
    except OSError:
        pass


class MorningPushTest(unittest.TestCase):
    """`bot morning` — §4.1 rendering and the §4.2 settings-marker idempotency."""

    def setUp(self):
        rebind_test_db(test_db)  # an earlier module may have rebound the handles
        clear_all_tables(test_db)
        # The push grades today before it briefs it (§4.1), which freshens the cache
        # first; the activities each test wants are written to the db directly.
        garmin = patch.object(runtime, "garmin", MagicMock(), create=True)
        garmin.start()
        self.addCleanup(garmin.stop)

    def _trained(self, sport="running", duration_min=40):
        """One completed activity for today, the shape the Garmin pull would have left."""
        test_db.save_completed_activity(
            activity_id=f"a-{sport}", date=today_str(),
            start_time=f"{today_str()} 07:00:00", activity_name=f"Morning {sport}",
            activity_type=sport, duration_sec=duration_min * 60, distance_km=8.0,
            elevation_gain_m=0.0, avg_hr=140, max_hr=160, rpe=None, tss=45.0,
        )

    def test_rest_day_gets_one_line_and_no_buttons(self):
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn("Rest day", out)
        self.assertNotIn(BUTTONS_SENTINEL, out)
        self.assertEqual(test_db.get_setting(MORNING_MARKER), today_str())

    def test_session_day_renders_line_description_and_buttons(self):
        save_workout(
            test_db, today_str(), "running", "Easy run",
            description="Conversational pace, HR under 145.", duration_minutes=40,
        )
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn("🏃 Today: Easy run — 40 min", out)
        self.assertIn("Conversational pace", out)
        self.assertIn(BUTTONS_SENTINEL, out)

    def test_a_day_already_trained_is_congratulated_not_briefed(self):
        save_workout(
            test_db, today_str(), "running", "Easy run",
            description="Conversational pace, HR under 145.", duration_minutes=40,
        )
        self._trained()
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn(PUSH_ALL_DONE_LINE, out)
        self.assertNotIn("Easy run", out)
        self.assertNotIn("Conversational pace", out)
        self.assertNotIn(BUTTONS_SENTINEL, out)
        self.assertEqual(test_db.get_setting(MORNING_MARKER), today_str())

    def test_the_session_still_ahead_is_briefed_and_keeps_the_buttons(self):
        save_workout(test_db, today_str(), "running", "Easy run", duration_minutes=40)
        save_workout(test_db, today_str(), "strength", "Core work", duration_minutes=30)
        self._trained()
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertNotIn(PUSH_ALL_DONE_LINE, out)
        self.assertIn(SIMPLE_DONE_LINE, out)     # the run, acknowledged
        self.assertIn("Core work", out)          # the session left, still briefed
        self.assertIn(BUTTONS_SENTINEL, out)

    def test_an_ungraded_day_falls_back_to_the_briefing(self):
        save_workout(test_db, today_str(), "running", "Easy run", duration_minutes=40)
        self._trained()
        runtime.garmin.ensure_data.side_effect = RuntimeError("Garmin down")
        with patch.dict(os.environ, {"TRAINMATE_FRONTEND": "json"}):
            code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn("Easy run", out)
        self.assertNotIn("Garmin down", out)
        self.assertIn(BUTTONS_SENTINEL, out)

    def test_second_run_same_day_is_silent(self):
        save_workout(test_db, today_str(), "running", "Easy run")
        run_cli(["bot", "morning"])
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_force_resends_despite_the_marker(self):
        run_cli(["bot", "morning"])
        code, out, _ = run_cli(["bot", "morning", "--force"])
        self.assertEqual(code, 0)
        self.assertIn("Rest day", out)

    def test_adapt_first_off_never_touches_the_coach(self):
        coach = MagicMock()
        with patch.object(runtime, "coach_service", coach, create=True):
            run_cli(["bot", "morning"])
        coach.workout_adapt.assert_not_called()

    def _adapt_first_env(self, coach):
        return (
            patch.dict(config.data, {"telegram": {"push": {"adapt_first": True}}}),
            patch.object(runtime, "coach_service", coach, create=True),
            patch("trainmate.cli.bot.ensure_recent_data"),
        )

    def test_adapt_first_applies_and_surfaces_the_reason(self):
        save_workout(test_db, today_str(), "running", "Easy run")
        proposal = MagicMock(
            workouts=[{"date": today_str()}], reason="Eased today — rough night."
        )
        coach = MagicMock()
        coach.workout_adapt.return_value = proposal
        cfg, svc, pull = self._adapt_first_env(coach)
        with cfg, svc, pull:
            code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        coach.workout_revision_apply.assert_called_once_with(proposal)
        self.assertIn("Eased today", out)

    def test_adapt_first_no_change_records_and_adds_no_reason(self):
        proposal = MagicMock(workouts=[], reason="All green.")
        coach = MagicMock()
        coach.workout_adapt.return_value = proposal
        cfg, svc, pull = self._adapt_first_env(coach)
        with cfg, svc, pull:
            code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        coach.workout_revision_record_no_change.assert_called_once_with(proposal)
        coach.workout_revision_apply.assert_not_called()
        self.assertNotIn("All green.", out)

    def test_adapt_failure_does_not_sink_the_push(self):
        save_workout(test_db, today_str(), "running", "Easy run")
        coach = MagicMock()
        coach.workout_adapt.side_effect = RuntimeError("LLM down")
        cfg, svc, pull = self._adapt_first_env(coach)
        # The failure surfaces only as a terminal aside; under the bot's json
        # frontend (asides off) it must never reach the athlete's chat.
        with cfg, svc, pull, patch.dict(os.environ, {"TRAINMATE_FRONTEND": "json"}):
            code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn("Easy run", out)
        self.assertNotIn("LLM down", out)
        self.assertEqual(test_db.get_setting(MORNING_MARKER), today_str())


class RouteCommandTest(unittest.TestCase):
    """`bot route` — §5.3/§5.4: intent validation, graceful degradation, and the
    router-model role."""

    def setUp(self):
        rebind_test_db(test_db)  # an earlier module may have rebound the handles
        from trainmate.openrouter import openrouter_client
        clear_all_tables(test_db)
        openrouter_client.reset_model()
        self.addCleanup(openrouter_client.reset_model)

    def _intent(self, out):
        return json.loads(out.strip().splitlines()[-1])["intent"]

    def test_valid_intent_passes_through(self):
        with patch(
            "trainmate.openrouter.OpenRouterClient.complete",
            return_value={"intent": "show_week"},
        ):
            code, out, _ = run_cli(["bot", "route", "what's on this week?"])
        self.assertEqual(code, 0)
        self.assertEqual(self._intent(out), "show_week")

    def test_unknown_intent_degrades_to_unclear(self):
        with patch(
            "trainmate.openrouter.OpenRouterClient.complete",
            return_value={"intent": "rm -rf"},
        ):
            _, out, _ = run_cli(["bot", "route", "hello"])
        self.assertEqual(self._intent(out), "unclear")

    def test_llm_failure_degrades_to_unclear(self):
        with patch(
            "trainmate.openrouter.OpenRouterClient.complete",
            side_effect=ValueError("no api key"),
        ):
            code, out, _ = run_cli(["bot", "route", "hello"])
        self.assertEqual(code, 0)
        self.assertEqual(self._intent(out), "unclear")

    def test_router_model_role_pins_the_client(self):
        from trainmate.openrouter import openrouter_client
        # The router picks from the same menu the coach does — one allowlist
        # (DESIGN_settings.md §4), so the cheap model is listed under `llm.models` too.
        with patch.dict(
            config.data,
            {"llm": {"models": ["main/model", "cheap/model"],
                     "router_model": "cheap/model"}},
        ), patch(
            "trainmate.openrouter.OpenRouterClient.complete",
            return_value={"intent": "help"},
        ):
            run_cli(["bot", "route", "hello"])
            self.assertEqual(openrouter_client.model, "cheap/model")

    def test_an_off_menu_role_is_ignored_and_the_coach_model_routes(self):
        from trainmate.openrouter import openrouter_client
        with patch.dict(
            config.data,
            {"llm": {"models": ["main/model"], "router_model": "cheap/model"}},
        ), patch(
            "trainmate.openrouter.OpenRouterClient.complete",
            return_value={"intent": "help"},
        ):
            run_cli(["bot", "route", "hello"])
            self.assertEqual(openrouter_client.model, "main/model")

    def test_absent_role_leaves_the_active_model(self):
        from trainmate.openrouter import openrouter_client
        with patch.dict(
            config.data, {"llm": {"models": ["main/model"]}}
        ), patch(
            "trainmate.openrouter.OpenRouterClient.complete",
            return_value={"intent": "help"},
        ):
            run_cli(["bot", "route", "hello"])
            self.assertEqual(openrouter_client.model, "main/model")


class ConstraintsViewTest(unittest.TestCase):
    """`bot constraints` — §5.5: the companion list plus the remove picker, whose
    leaves stay pinned to single-ID `constraint rm` (the §7 guardrail's one
    routable-by-tap mutation)."""

    def setUp(self):
        rebind_test_db(test_db)  # an earlier module may have rebound the handles
        clear_all_tables(test_db)

    def _add(self, title, start=None, end=None, rest=0):
        start = start or today_str()
        return test_db.add_constraint(
            title=title, start_date=start, end_date=end or start,
            rest=rest, description=None, replan=0, source="manual",
        )

    def test_empty_list_is_a_clean_slate_without_buttons(self):
        code, out, _ = run_cli(["bot", "constraints"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing on the list", out)
        self.assertNotIn(BUTTONS_SENTINEL, out)

    def test_lists_titles_and_offers_the_picker(self):
        cid = self._add("no run Thursday")
        code, out, _ = run_cli(["bot", "constraints"])
        self.assertEqual(code, 0)
        self.assertIn("no run Thursday", out)
        self.assertIn(BUTTONS_SENTINEL, out)
        self.assertIn(f"constraint rm {cid}", out)

    def test_past_constraints_stay_out_of_the_view(self):
        self._add("old rule", start="2020-01-01", end="2020-01-02")
        _, out, _ = run_cli(["bot", "constraints"])
        self.assertNotIn("old rule", out)
        self.assertIn("Nothing on the list", out)

    def test_picker_leaves_reach_only_single_id_rm(self):
        from trainmate.cli.bot import constraint_rm_buttons

        def leaves(buttons):
            for b in buttons:
                if b.get("menu"):
                    yield from leaves(b["menu"])
                else:
                    yield b

        today = today_str()
        buttons = constraint_rm_buttons([
            {"id": 7, "title": "x" * 60, "start_date": today, "end_date": today},
            {"id": 9, "title": "short", "start_date": today, "end_date": today},
        ])
        sends = [b["send"] for b in leaves(buttons) if b.get("send")]
        self.assertEqual(sends, ["constraint rm 7", "constraint rm 9"])
        # Long titles shrink to a recognisable label, never a truncated utterance.
        self.assertTrue(all(len(b["label"]) <= 32 for b in leaves(buttons)))

    def test_rm_in_simple_render_stays_companion_prose(self):
        cid = self._add("no run Thursday")
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(["constraint", "rm", str(cid)])
        self.assertEqual(code, 0)
        self.assertIn("dropped", out)
        self.assertNotIn(f"Constraint [{cid}]", out)
        self.assertIsNone(test_db.get_constraint(cid))


class SimpleListRenderTest(unittest.TestCase):
    """`workout list` under TRAINMATE_RENDER=simple: companion prose, expert form
    untouched otherwise (§6)."""

    def setUp(self):
        rebind_test_db(test_db)  # an earlier module may have rebound the handles
        clear_all_tables(test_db)
        patcher = patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_today_view_reads_as_the_day(self):
        save_workout(
            test_db, today_str(), "cycling", "Endurance ride", duration_minutes=60
        )
        _, out, _ = run_cli(["workout", "list", "-d", "today"])
        self.assertIn("🚴 Today: Endurance ride — 60 min", out)
        self.assertNotIn("WORKOUT SCHEDULE", out)

    def test_empty_today_is_a_rest_day(self):
        _, out, _ = run_cli(["workout", "list", "-d", "today"])
        self.assertIn("Rest day", out)

    def test_week_view_lists_and_counts(self):
        save_workout(test_db, today_str(), "running", "Easy run", duration_minutes=40)
        _, out, _ = run_cli(["workout", "list"])
        self.assertIn("Coming up", out)
        self.assertIn("1 session planned", out)

    def test_expert_form_is_untouched_without_the_env(self):
        os.environ.pop("TRAINMATE_RENDER", None)
        save_workout(test_db, today_str(), "running", "Easy run")
        _, out, _ = run_cli(["workout", "list", "-d", "today"])
        self.assertIn("WORKOUT SCHEDULE", out)


class CompanionSurfaceRoutingTest(unittest.TestCase):
    """Every companion surface, driven through the real CLI (DESIGN_render_persona.md §5).

    The line builders have their own unit tests; what these pin is the *routing* — that
    the command reaches `runtime.render` and that the renderer built for this process is
    the companion one. A wrong argument order or a stale singleton is invisible to a
    builder test and changes every one of these."""

    def setUp(self):
        rebind_test_db(test_db)
        clear_all_tables(test_db)
        patcher = patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _goal_with_plan(self):
        """A goal 45 days out, with one block running from last week to the goal."""
        from datetime import date, timedelta
        today = date.fromisoformat(today_str())
        out = lambda days: (today + timedelta(days=days)).isoformat()  # noqa: E731
        goal_id = test_db.add_objective(
            title="Zurich Marathon", target_date=out(45), sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=goal_id, strategy="Build then sharpen.",
            goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": "Base", "start_date": out(-7), "end_date": out(45),
                         "focus": "Aerobic endurance, easy volume."}],
        )
        return goal_id

    def test_goal_list_reads_as_what_youre_training_for(self):
        self._goal_with_plan()
        code, out, _ = run_cli(["goal", "list"])
        self.assertEqual(code, 0)
        self.assertIn("What you're training for", out)
        self.assertIn("Zurich Marathon", out)
        # No IDs, no state tags, no expert header (§11).
        self.assertNotIn("=== GOALS ===", out)
        self.assertNotIn("[UPCOMING]", out)

    def test_plan_show_reads_as_the_road_to_the_goal(self):
        self._goal_with_plan()
        code, out, _ = run_cli(["plan", "show"])
        self.assertEqual(code, 0)
        self.assertIn("The road to Zurich Marathon", out)
        self.assertIn("you're here", out)
        self.assertNotIn("MACROCYCLE STRATEGY", out)
        self.assertNotIn("Macrocycle ID", out)

    def test_plan_show_without_a_goal_invites_instead_of_naming_a_command(self):
        """The athlete cannot run `plan generate`, so the empty state must not name it
        (DESIGN_bot_simple_frontend.md §11)."""
        code, out, _ = run_cli(["plan", "show"])
        self.assertEqual(code, 0)
        self.assertIn("once your goal is set up", out)
        self.assertNotIn("plan generate", out)

    def test_progress_reads_as_a_summary_not_a_table(self):
        code, out, _ = run_cli(["progress", "--no-pull"])
        self.assertEqual(code, 0)
        self.assertIn("The chart shows your fitness", out)
        self.assertNotIn("FORM today", out)

    def test_the_expert_voice_is_what_the_same_commands_speak_without_the_env(self):
        """The other half of the switch: nothing above may leak into the default voice."""
        os.environ.pop("TRAINMATE_RENDER", None)
        self._goal_with_plan()
        _, goals_out, _ = run_cli(["goal", "list"])
        _, plan_out, _ = run_cli(["plan", "show"])
        self.assertIn("=== GOALS ===", goals_out)
        self.assertIn("MACROCYCLE STRATEGY", plan_out)


if __name__ == "__main__":
    unittest.main()
