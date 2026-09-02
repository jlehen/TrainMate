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


class _ScriptedPrompt:
    """Stands in for the prompt transport so a test can read what a capture ASKED.

    A confirm's text goes to `input()`, which `run_cli` mocks away, so the question never
    reaches the captured stdout — and for a capture the question IS the preview
    (DESIGN_bot_simple_frontend.md §12.2)."""

    def __init__(self, answers=True):
        self.asked = []
        self._script = list(answers) if isinstance(answers, (list, tuple)) else None
        self._always = None if self._script is not None else bool(answers)

    def confirm(self, message, *, default=False, danger=False):
        self.asked.append(message)
        if self._script is None:
            return self._always
        return self._script.pop(0) if self._script else False

    def choose(self, message, choices, *, default=None):
        self.asked.append(message)
        return default if default is not None else choices[0].value

    def ask_text(self, message, *, secret=False, default=None):
        self.asked.append(message)
        return default or ""

    @property
    def text(self):
        return "\n".join(self.asked)


class _CaptureCase(unittest.TestCase):
    """Shared rig for the §12 capture commands: the companion voice, a scripted prompt,
    and one mocked extraction per run."""

    def setUp(self):
        rebind_test_db(test_db)
        clear_all_tables(test_db)
        patcher = patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def prompt(self, answers=True):
        prompt = _ScriptedPrompt(answers)
        runtime.prompt = prompt
        # An assignment on `runtime` shadows the accessor for the whole process.
        self.addCleanup(runtime.reset, "prompt")
        return prompt

    def capture(self, argv, extraction, answers=True):
        """Runs one `bot capture …`; returns (exit code, stdout, the prompt)."""
        prompt = self.prompt(answers)
        with patch(
            "trainmate.openrouter.OpenRouterClient.complete",
            **({"side_effect": extraction} if isinstance(extraction, Exception)
               else {"return_value": extraction}),
        ):
            code, out, _ = run_cli(argv)
        return code, out, prompt

    def future(self, days):
        from datetime import date, timedelta
        return (date.fromisoformat(today_str()) + timedelta(days=days)).isoformat()


class GoalsViewTest(unittest.TestCase):
    """`bot goals` — §12.6: the companion list plus the call-off picker, whose leaves
    reach `goal rm <id>` and therefore archive, never purge."""

    def setUp(self):
        rebind_test_db(test_db)
        clear_all_tables(test_db)

    def _goal(self, title, days_out=30, status="active"):
        from datetime import date, timedelta
        target = (date.fromisoformat(today_str()) + timedelta(days=days_out)).isoformat()
        return test_db.add_objective(
            title=title, target_date=target, sport_type="running", status=status,
        )

    def test_empty_active_list_invites_and_offers_no_picker(self):
        code, out, _ = run_cli(["bot", "goals"])
        self.assertEqual(code, 0)
        self.assertIn("No goal on the horizon", out)
        self.assertNotIn(BUTTONS_SENTINEL, out)

    def test_lists_goals_and_offers_the_call_off_picker(self):
        gid = self._goal("Zurich Marathon")
        code, out, _ = run_cli(["bot", "goals"])
        self.assertEqual(code, 0)
        self.assertIn("Zurich Marathon", out)
        self.assertIn(BUTTONS_SENTINEL, out)
        self.assertIn(f"goal rm {gid}", out)
        # No IDs or state tags in the prose itself (§6).
        self.assertNotIn("[UPCOMING]", out)

    def test_a_called_off_goal_is_neither_shown_nor_offered(self):
        self._goal("Old 10k", status="archived")
        _, out, _ = run_cli(["bot", "goals"])
        self.assertNotIn("Old 10k", out)
        self.assertNotIn(BUTTONS_SENTINEL, out)

    def test_picker_leaves_reach_only_the_archiving_rm(self):
        from trainmate.cli.bot import goal_rm_buttons

        def leaves(buttons):
            for b in buttons:
                if b.get("menu"):
                    yield from leaves(b["menu"])
                else:
                    yield b

        buttons = goal_rm_buttons([{"id": 4, "title": "x" * 60}, {"id": 6, "title": "10k"}])
        sends = [b["send"] for b in leaves(buttons) if b.get("send")]
        self.assertEqual(sends, ["goal rm 4", "goal rm 6"])
        # `--purge` is unreachable from chat: the one goal mutation a tap fires is the
        # reversible call-off (§12.6).
        self.assertFalse(any("purge" in s for s in sends))

    def test_calling_a_goal_off_reads_as_prose_and_names_no_command(self):
        gid = self._goal("Spring 10k")
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(["goal", "rm", str(gid)])
        self.assertEqual(code, 0)
        self.assertIn("Spring 10k is off the list", out)
        self.assertNotIn("goal edit", out)
        self.assertNotIn("[ARCHIVED]", out)


class CaptureNoteTest(_CaptureCase):
    """`bot capture note` — §12.3: the note inbox that asks before storing, and offers
    the coach instead of riding it."""

    NOTE = "sore knee, no running for two weeks"

    def setUp(self):
        super().setUp()
        syncer = MagicMock()
        syncer.add_signal_event.side_effect = (
            lambda date, metric, value, text, existing=None:
            existing or f"evt-{date}-{metric}"
        )
        runtime.calendar_syncer = syncer
        self.addCleanup(runtime.reset, "calendar_syncer")

    def _run(self, extraction, answers=True):
        return self.capture(["bot", "capture", "note", self.NOTE], extraction, answers)

    def _constraint(self):
        return {"new_constraints": [
            {"title": "no running", "start_date": today_str(),
             "end_date": today_str()},
        ]}

    def test_a_confirmed_constraint_is_stored_and_the_coach_is_offered(self):
        code, out, _ = self._run(self._constraint())
        self.assertEqual(code, 0)
        self.assertEqual(
            [c["title"] for c in test_db.get_constraints(today_str(), None)],
            ["no running"],
        )
        # The coach is an offer, not a toll: nothing here ran an adaptation.
        self.assertIn(BUTTONS_SENTINEL, out)
        self.assertIn("Adjust the plan around it", out)
        self.assertIn("workout adapt", out)

    def test_a_confirmed_signal_gets_the_offer_too(self):
        """Amended 2026-09-02: the record points backward, but the athlete reporting one
        expects forward notice, so the gap is one visible tap wide (§12.3)."""
        code, out, _ = self._run({"new_signals": [
            {"metric": "disturbed_sleep", "date": today_str(), "end_date": today_str()},
        ]})
        self.assertEqual(code, 0)
        self.assertTrue(test_db.get_daily_signals(today_str(), today_str()))
        self.assertIn("Adjust the plan around it", out)

    def test_nothing_found_offers_the_message_as_written(self):
        code, out, _ = self._run({"new_constraints": [], "new_signals": []})
        self.assertEqual(code, 0)
        self.assertIn("Send it to your coach as written", out)
        # The exact words ride along — that is the lane this button exists for.
        self.assertIn(self.NOTE, out)
        self.assertEqual(test_db.get_constraints(today_str(), None), [])

    def test_an_extraction_failure_lands_where_a_no_find_does(self):
        code, out, _ = self._run(ValueError("no api key"))
        self.assertEqual(code, 0)
        self.assertIn("Send it to your coach as written", out)

    def test_declining_stores_nothing_and_presses_no_further(self):
        code, out, _ = self._run(self._constraint(), answers=False)
        self.assertEqual(code, 0)
        self.assertEqual(test_db.get_constraints(today_str(), None), [])
        self.assertNotIn(BUTTONS_SENTINEL, out)

    def test_the_confirm_and_the_outcome_speak_the_companion_voice(self):
        """The shared helper (§12.10) asks the same question `workout adapt -m` asks,
        worded by the active renderer — here the companion one (§6: no IDs, no ISO)."""
        code, out, prompt = self._run(self._constraint())
        self.assertIn("Shall I remember that?", prompt.text)
        self.assertNotIn("Add constraint:", prompt.text)
        self.assertNotIn(today_str(), prompt.text)
        self.assertIn("Noted — I'll work around that", out)
        self.assertNotIn("Captured constraint [", out)


class CaptureAddGoalTest(_CaptureCase):
    """`bot capture add_goal` — §12.5: the goal row is captured, the plan for it is not."""

    def _run(self, extraction, answers=True, text="I want to run a half marathon"):
        return self.capture(["bot", "capture", "add_goal", text], extraction, answers)

    def test_a_confirmed_goal_is_created_and_the_plan_is_left_to_the_operator(self):
        target = self.future(120)
        code, out, _ = self._run({
            "title": "Half marathon", "target_date": target,
            "sports": ["running"], "date_type": "event",
        })
        self.assertEqual(code, 0)
        goals = test_db.get_objectives()
        self.assertEqual([g["title"] for g in goals], ["Half marathon"])
        self.assertEqual(goals[0]["target_date"], target)
        self.assertIn("from the computer", out)
        self.assertNotIn("plan generate", out)

    def test_a_missing_date_asks_instead_of_guessing_one(self):
        """Extraction transcribes; it never fills — a guessed date is exactly what a
        confirm-tap sails past (§12.2)."""
        code, out, prompt = self._run({
            "title": "Half marathon", "target_date": None, "sports": ["running"],
        })
        self.assertEqual(code, 0)
        self.assertIn("When is it?", out)
        self.assertEqual(prompt.asked, [], "a missing field asks, it does not confirm")
        self.assertEqual(test_db.get_objectives(), [])

    def test_an_off_enum_sport_is_dropped_rather_than_free_typed(self):
        code, out, _ = self._run({
            "title": "Half marathon", "target_date": self.future(120),
            "sports": ["jogging"],
        })
        self.assertEqual(code, 0)
        self.assertIn("Which sport", out)
        self.assertEqual(test_db.get_objectives(), [])

    def test_the_preview_shows_the_resolved_date_and_the_date_type_reading(self):
        code, out, _ = self._run({
            "title": "Base building", "target_date": self.future(120),
            "sports": ["running"], "date_type": "horizon",
        }, answers=False)
        self.assertEqual(code, 0)
        # 'by ~' is the horizon reading — a wrong guess is visible in the first line (§12.5).
        self.assertIn("by ~", out)
        self.assertEqual(test_db.get_objectives(), [])


class CaptureEditTest(_CaptureCase):
    """`bot capture edit_goal` / `edit_constraint` — §12.4: nomination, and the four
    ways it can land."""

    def _goal(self, title="Zurich Marathon", days=60):
        return test_db.add_objective(
            title=title, target_date=self.future(days), sport_type="running",
        )

    def _run(self, intent, extraction, text, answers=True, pinned=None):
        argv = ["bot", "capture", intent, text]
        if pinned is not None:
            argv += ["--id", str(pinned)]
        return self.capture(argv, extraction, answers)

    def test_a_clean_nomination_previews_the_real_row_then_writes(self):
        gid = self._goal()
        moved = self.future(90)
        code, out, prompt = self._run("edit_goal", {
            "kind": "goal", "id": gid, "changes": {"target_date": moved},
        }, "move my marathon to October 12")
        self.assertEqual(code, 0)
        # The preview is drawn from the stored row, not from what the model believes it
        # says — which is what makes a wrong nomination die visibly (§12.4).
        self.assertIn("Your goal", out)
        self.assertIn("Zurich Marathon", out)
        self.assertIn("Shall I make that change?", prompt.text)
        self.assertEqual(test_db.get_objective(gid)["target_date"], moved)

    def test_declining_writes_nothing_and_offers_the_other_rows(self):
        gid = self._goal()
        other = self._goal("Spring 10k", days=20)
        before = test_db.get_objective(gid)["target_date"]
        code, out, _ = self._run("edit_goal", {
            "kind": "goal", "id": gid, "changes": {"target_date": self.future(90)},
        }, "move it", answers=False)
        self.assertEqual(code, 0)
        self.assertEqual(test_db.get_objective(gid)["target_date"], before)
        self.assertIn("Spring 10k", out)
        self.assertIn(f"--id {other}", out)

    def test_several_candidates_become_a_picker_that_re_captures_pinned(self):
        first = self._goal("Spring 10k", days=20)
        second = self._goal("Autumn 10k", days=200)
        code, out, prompt = self._run("edit_goal", {
            "kind": "goal", "id": first, "candidate_ids": [first, second],
            "changes": {"title": "10k"},
        }, "rename the 10k")
        self.assertEqual(code, 0)
        self.assertIn(BUTTONS_SENTINEL, out)
        self.assertEqual(prompt.asked, [], "the athlete picks before anything is asked")
        # A leaf does not execute the edit: it re-enters the capture with the row pinned.
        self.assertIn(f"bot capture edit_goal --id {first}", out)
        self.assertIn(f"bot capture edit_goal --id {second}", out)
        self.assertEqual(test_db.get_objective(first)["title"], "Spring 10k")

    def test_a_pinned_re_capture_goes_straight_to_the_preview(self):
        gid = self._goal("Spring 10k", days=20)
        code, out, _ = self._run("edit_goal", {
            "kind": "goal", "id": gid, "changes": {"title": "Spring 10k (fast)"},
        }, "rename the 10k", pinned=gid)
        self.assertEqual(code, 0)
        self.assertNotIn(BUTTONS_SENTINEL, out)
        self.assertEqual(test_db.get_objective(gid)["title"], "Spring 10k (fast)")

    def test_a_session_shaped_ask_is_handed_to_the_coach_not_to_a_picker(self):
        self._goal()
        save_workout(test_db, self.future(3), "running", "Long run", duration_minutes=90)
        session = test_db.get_workouts(
            start_date=self.future(3), end_date=self.future(3)
        )[0]
        prompt = self.prompt(True)
        with patch(
            "trainmate.openrouter.OpenRouterClient.complete",
            return_value={"kind": "session", "id": session["id"]},
        ), patch("trainmate.cli.bot._hand_off_to_coach") as handoff:
            code, out, _ = run_cli(
                ["bot", "capture", "edit_goal", "move my long run to Sunday"]
            )
        self.assertEqual(code, 0)
        # The wrong-domain picker — goals answering a question about a session — never
        # appears; the hand-off does (§12.4).
        self.assertIn("Long run", prompt.text)
        self.assertIn("pass it to your coach", prompt.text)
        self.assertNotIn(BUTTONS_SENTINEL, out)
        handoff.assert_called_once_with("move my long run to Sunday")

    def test_nothing_matching_falls_back_to_the_send_to_coach_offer(self):
        self._goal()
        code, out, _ = self._run(
            "edit_goal", {"kind": "none", "id": None}, "change the thing"
        )
        self.assertEqual(code, 0)
        self.assertIn("Send it to your coach as written", out)

    def test_a_constraint_edit_moves_its_window_and_names_no_command(self):
        cid = test_db.add_constraint(
            title="knee flare", start_date=today_str(), end_date=self.future(7),
            rest=0, description=None, replan=0, source="message",
        )
        moved = self.future(30)
        code, out, prompt = self._run("edit_constraint", {
            "kind": "constraint", "id": cid, "changes": {"end_date": moved},
        }, "the knee thing runs to the end of the month")
        self.assertEqual(code, 0)
        self.assertEqual(test_db.get_constraint(cid)["end_date"], moved)
        self.assertIn("Your rule", out + prompt.text)
        self.assertNotIn("constraint edit", out)
        self.assertNotIn("plan generate", out)

    def test_a_tier_only_change_is_refused_the_way_a_no_match_is(self):
        """`--rest` and `--replan` stay expert vocabulary, so an extraction naming only
        those has nothing this surface may write (§12.4)."""
        cid = test_db.add_constraint(
            title="knee flare", start_date=today_str(), end_date=today_str(),
            rest=0, description=None, replan=0, source="message",
        )
        code, out, _ = self._run("edit_constraint", {
            "kind": "constraint", "id": cid, "changes": {"rest": True, "replan": True},
        }, "make it a proper rest week")
        self.assertEqual(code, 0)
        self.assertEqual(test_db.get_constraint(cid)["rest"], 0)
        self.assertEqual(test_db.get_constraint(cid)["replan"], 0)
        self.assertIn("Send it to your coach as written", out)


class CaptureSettingTest(_CaptureCase):
    """`bot capture change_setting` — §12.7: an allowlist, not a surface."""

    def _run(self, extraction, answers=True, text="can you message me at 7 instead?"):
        return self.capture(
            ["bot", "capture", "change_setting", text], extraction, answers
        )

    def test_an_allowlisted_key_is_written_and_read_back_as_its_effect(self):
        from trainmate import settings
        code, out, prompt = self._run({"key": "morning-time", "value": "07:00"})
        self.assertEqual(code, 0)
        self.assertEqual(settings.morning_time(), "07:00")
        # The confirm names what she will notice, not the key (§12.7).
        self.assertIn("open your day at 07:00", prompt.text)
        self.assertNotIn("morning-time", prompt.text)
        self.assertNotIn("morning-time set to", out)

    def test_an_off_list_key_is_refused_honestly_and_names_the_operator(self):
        from trainmate import settings
        code, out, prompt = self._run({"key": "coach-model", "value": "3"},
                                      text="use a smarter model")
        self.assertEqual(code, 0)
        self.assertIn("not me", out)
        # A boundary, not the §5.3 unclear fallback, and nothing asked or written.
        self.assertNotIn("didn't quite get that", out)
        self.assertEqual(prompt.asked, [])
        self.assertIsNone(settings.stored(settings.COACH_MODEL))

    def test_an_operator_shaped_key_does_not_widen_the_guardrail(self):
        from trainmate import settings
        code, out, _ = self._run({"key": "adapt-first", "value": "on"})
        self.assertEqual(code, 0)
        self.assertIsNone(settings.stored(settings.ADAPT_FIRST))
        self.assertIn("not me", out)

    def test_a_value_the_setting_cannot_read_asks_rather_than_writing(self):
        from trainmate import settings
        code, out, _ = self._run({"key": "morning-time", "value": "breakfast"})
        self.assertEqual(code, 0)
        self.assertIsNone(settings.stored(settings.MORNING_TIME))
        self.assertIn("didn't catch what to set it to", out)

    def test_declining_leaves_the_setting_alone(self):
        from trainmate import settings
        code, _, _ = self._run({"key": "push", "value": "off"}, answers=False)
        self.assertEqual(code, 0)
        self.assertIsNone(settings.stored(settings.PUSH))


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
