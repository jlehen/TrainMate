"""End-of-runway nudges (DESIGN_runway_nudge.md): the pure detector, the wordings each
surface draws, and the surfaces themselves.

The detector half needs no database — `progression.runway` is row-in, like `plan_gap`.
The surface half drives the real CLI against a temp database, because the point of the
design is that `workout adapt`, `status`, `workout list` and the morning push cannot
answer the same morning differently (§3).
"""
import argparse
import os
import re
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.helpers import clear_all_tables, rebind_test_db, run_cli, save_workout
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_trainmate_runway.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli  # noqa: F401  (re-exports the workout handlers)

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate import progression, runtime  # noqa: E402
from trainmate.cli import render as render_cli, runway as runway_cli  # noqa: E402
from trainmate.cli.workouts import generate as generate_cli  # noqa: E402
from trainmate.cli.bot import MORNING_MARKER  # noqa: E402
from trainmate.config import config  # noqa: E402
from trainmate.cli.render import SPORT_EMOJI, SIMPLE_PASSED_LINE  # noqa: E402
from trainmate.cli.runway import RUNWAY_BUTTON_LABEL  # noqa: E402
from trainmate.prompt import BUTTONS_SENTINEL  # noqa: E402
from trainmate.progression import (  # noqa: E402
    RUNWAY_BLOCK, RUNWAY_PLAN_END_NEXT_GOAL, RUNWAY_PLAN_END_NO_GOAL, RUNWAY_SPAN,
)
from trainmate.util import today_str  # noqa: E402

TODAY = "2026-08-31"
WARN = 7


def tearDownModule():
    try:
        os.remove(TEST_DB_PATH)
    except OSError:
        pass


def _d(offset: int, anchor: str = TODAY) -> str:
    return (date.fromisoformat(anchor) + timedelta(days=offset)).isoformat()


def _out(offset: int) -> str:
    """A date `offset` days from the athlete's today — the surface tests drive the real
    CLI, so their fixtures have to sit against the clock it reads."""
    return _d(offset, today_str())


def _w(offset: int, sport_type="running", source="generated", removed=False):
    return {
        "date": _d(offset), "sport_type": sport_type,
        "source": source, "removed": removed,
    }


def _meso(start: int, end: int, meso_id: int = 1, name="Block"):
    return {
        "id": meso_id, "name": name,
        "start_date": _d(start), "end_date": _d(end),
    }


def _goal(offset: int, title="Klausenpass", status="active"):
    return {"id": 9, "title": title, "target_date": _d(offset), "status": status}


class RunwayDetectorTest(unittest.TestCase):
    """`progression.runway` — when it fires and what it classifies (§2)."""

    def _run(self, workouts, mesocycles, objectives=(), today=TODAY, warn=WARN):
        return progression.runway(
            list(workouts), list(mesocycles), list(objectives), today, warn
        )

    def test_silent_while_the_schedule_still_reaches_past_the_window(self):
        state = self._run([_w(20)], [_meso(-30, 60)])
        self.assertIsNone(state)

    def test_span_cliff_when_the_schedule_stops_mid_block(self):
        state = self._run([_w(4)], [_meso(-30, 45)])
        self.assertEqual(state["kind"], RUNWAY_SPAN)
        self.assertEqual(state["days_left"], 4)
        self.assertEqual(state["last_covered_date"], _d(4))
        self.assertEqual(state["plan_end"], _d(45))
        self.assertNotIn("next_mesocycle", state)

    def test_block_cliff_when_the_schedule_stops_on_a_block_boundary(self):
        blocks = [_meso(-30, 4, meso_id=6, name="Base"),
                  _meso(5, 45, meso_id=7, name="Build")]
        state = self._run([_w(4)], blocks)
        self.assertEqual(state["kind"], RUNWAY_BLOCK)
        self.assertEqual(state["next_mesocycle"]["id"], 7)

    def test_a_block_boundary_with_no_block_after_it_is_a_span_cliff(self):
        """A gap inside the plan, not the boundary the athlete should re-plan against:
        with no block starting after it there is no `-m ..<id>` to name."""
        blocks = [_meso(-30, 4, meso_id=6), _meso(-60, 45, meso_id=7)]
        state = self._run([_w(4)], blocks)
        self.assertEqual(state["kind"], RUNWAY_SPAN)

    def test_plan_cliff_with_no_goal_beyond_it(self):
        state = self._run([_w(5)], [_meso(-30, 5)], objectives=[_goal(5)])
        self.assertEqual(state["kind"], RUNWAY_PLAN_END_NO_GOAL)

    def test_plan_cliff_naming_the_goal_the_plan_does_not_reach(self):
        state = self._run(
            [_w(5)], [_meso(-30, 5)], objectives=[_goal(5), _goal(60, title="Klausen")]
        )
        self.assertEqual(state["kind"], RUNWAY_PLAN_END_NEXT_GOAL)
        self.assertEqual(state["objective"]["title"], "Klausen")

    def test_an_archived_goal_beyond_the_plan_is_not_what_comes_next(self):
        state = self._run(
            [_w(5)], [_meso(-30, 5)],
            objectives=[_goal(60, title="Called off", status="archived")],
        )
        self.assertEqual(state["kind"], RUNWAY_PLAN_END_NO_GOAL)

    def test_a_manual_row_does_not_extend_the_schedule(self):
        """The race put on the calendar by hand must not make the schedule look as if it
        reaches it while the generated sessions end next Thursday (§2)."""
        state = self._run(
            [_w(4), _w(40, source="manual")], [_meso(-30, 45)]
        )
        self.assertEqual(state["last_covered_date"], _d(4))
        self.assertEqual(state["kind"], RUNWAY_SPAN)

    def test_a_removed_row_does_not_extend_the_schedule(self):
        state = self._run([_w(4), _w(9, removed=True)], [_meso(-30, 45)])
        self.assertEqual(state["last_covered_date"], _d(4))

    def test_a_planned_rest_row_covers_its_date_like_any_other(self):
        """The coverage invariant's whole point: no margin, no guessing whether a quiet
        tail is a taper or a hole (§2.1)."""
        state = self._run([_w(2), _w(6, sport_type="rest")], [_meso(-30, 45)])
        self.assertEqual(state["last_covered_date"], _d(6))
        self.assertEqual(state["days_left"], 6)

    def test_day_zero_fires_with_days_left_zero(self):
        state = self._run([_w(0)], [_meso(-30, 45)])
        self.assertEqual(state["days_left"], 0)
        self.assertEqual(state["kind"], RUNWAY_SPAN)

    def test_a_passed_cliff_keeps_firing_while_the_plan_runs_on(self):
        state = self._run([_w(-20)], [_meso(-60, 45)])
        self.assertEqual(state["kind"], RUNWAY_SPAN)
        self.assertEqual(state["days_left"], -20)

    def test_a_finished_plan_speaks_for_one_window_then_goes_quiet(self):
        blocks = [_meso(-60, -3)]
        self.assertEqual(
            self._run([_w(-3)], blocks, objectives=[_goal(-3)])["kind"],
            RUNWAY_PLAN_END_NO_GOAL,
        )
        self.assertIsNone(self._run([_w(-8)], [_meso(-60, -8)], objectives=[_goal(-8)]))

    def test_a_goal_whose_date_passed_yesterday_still_has_a_plan_to_wrap_up(self):
        """`upcoming_objectives` would have dropped it this morning — the one morning the
        wrap-up matters most (§2)."""
        state = self._run([_w(-1)], [_meso(-60, -1)], objectives=[_goal(-1)])
        self.assertEqual(state["kind"], RUNWAY_PLAN_END_NO_GOAL)

    def test_a_periodization_wholly_behind_today_is_a_plan_cliff_not_a_span_one(self):
        """The schedule stopped before the plan did, and then the plan ended too. There is
        nothing left to generate towards, which is what `workout adapt` refuses on (§4)."""
        state = self._run([_w(-6)], [_meso(-60, -2)], objectives=[_goal(-2)])
        self.assertEqual(state["kind"], RUNWAY_PLAN_END_NO_GOAL)

    def test_silent_without_a_plan_on_record(self):
        self.assertIsNone(self._run([_w(2)], []))

    def test_silent_without_a_generated_row_on_record(self):
        self.assertIsNone(self._run([_w(2, source="manual")], [_meso(-30, 45)]))

    def test_the_window_is_the_configured_width(self):
        self.assertIsNone(self._run([_w(10)], [_meso(-30, 45)], warn=7))
        self.assertEqual(self._run([_w(10)], [_meso(-30, 45)], warn=14)["days_left"], 10)


class RunwayWordingTest(unittest.TestCase):
    """The §4 hint lines: the fact, then the exact command."""

    def _lines(self, state, today=TODAY):
        return [
            re.sub(r"\x1b\[[0-9;]*m", "", line)
            for line in runway_cli.runway_hint_lines(state, today)
        ]

    def test_block_cliff_names_the_next_block_by_id(self):
        state = progression.runway(
            [_w(4)],
            [_meso(-30, 4, meso_id=6), _meso(5, 45, meso_id=7)],
            [], TODAY, WARN,
        )
        first, second = self._lines(state)
        self.assertIn("This block ends in 4 day(s), on 2026-09-04 Fri", first)
        self.assertIn("the next one has no fresh sessions", first)
        self.assertIn("workout generate -m ..7", second)

    def test_day_zero_never_reads_in_zero_days(self):
        state = progression.runway([_w(0)], [_meso(-30, 45)], [], TODAY, WARN)
        first, _ = self._lines(state)
        self.assertIn("Scheduled workouts run out today.", first)
        self.assertNotIn("0 day(s)", first)

    def test_a_passed_cliff_reads_in_the_past_tense(self):
        state = progression.runway([_w(-3)], [_meso(-60, 45)], [], TODAY, WARN)
        first, _ = self._lines(state)
        self.assertIn("Scheduled workouts ran out 3 day(s) ago", first)

    def test_span_cliff_says_how_much_plan_is_left(self):
        state = progression.runway([_w(4)], [_meso(-30, 45)], [], TODAY, WARN)
        _, second = self._lines(state)
        self.assertIn("Your plan covers 6 more weeks", second)
        self.assertIn("'workout generate'", second)

    def test_plan_cliff_with_a_goal_names_it_and_the_goal_flag(self):
        state = progression.runway(
            [_w(5)], [_meso(-30, 5)], [_goal(60, title="Klausen")], TODAY, WARN
        )
        first, second = self._lines(state)
        self.assertIn("Your plan ends with its last session on 2026-09-05 Sat", first)
        self.assertIn("Next up: Klausen (2026-10-30 Fri)", second)
        self.assertIn("'workout generate -g'", second)

    def test_plan_cliff_with_nothing_after_it_asks_for_a_goal_first(self):
        state = progression.runway([_w(5)], [_meso(-30, 5)], [], TODAY, WARN)
        first, second = self._lines(state)
        self.assertIn("nothing is planned beyond it", first)
        self.assertIn("'goal add'", second)
        self.assertIn("'plan generate'", second)


class SimpleRunwayWordingTest(unittest.TestCase):
    """The companion wordings the morning push draws instead of the hint (§6)."""

    def test_a_span_cliff_offers_to_plan_the_next_weeks(self):
        state = progression.runway([_w(4)], [_meso(-30, 45)], [], TODAY, WARN)
        line = render_cli.simple_runway_lines(state, TODAY)[0]
        self.assertIn("your schedule runs out in 4 days", line)
        self.assertIn("Want me to plan the next few weeks?", line)

    def test_a_plan_cliff_celebrates_and_points_at_the_computer(self):
        state = progression.runway([_w(5)], [_meso(-30, 5)], [], TODAY, WARN)
        line = render_cli.simple_runway_lines(state, TODAY)[0]
        self.assertIn("that's the goal you've been training toward", line)
        self.assertIn("happens from the computer", line)

    def test_only_span_and_block_cliffs_earn_a_button(self):
        span = progression.runway([_w(4)], [_meso(-30, 45)], [], TODAY, WARN)
        block = progression.runway(
            [_w(4)], [_meso(-30, 4, meso_id=6), _meso(5, 45, meso_id=7)], [], TODAY, WARN
        )
        plan = progression.runway([_w(5)], [_meso(-30, 5)], [], TODAY, WARN)
        self.assertEqual(runway_cli.runway_argv(span), "workout generate")
        self.assertEqual(runway_cli.runway_argv(block), "workout generate -m ..7")
        self.assertIsNone(runway_cli.runway_argv(plan))
        self.assertEqual(runway_cli.runway_buttons(plan), [])
        self.assertIsNone(runway_cli.runway_argv(None))


class RunwayHintIsNotReDerivedTest(unittest.TestCase):
    """§3's whole point: one printer, one fact, one command. The hint used to live on
    `workout adapt` alone, which is how `status` came to answer differently on the same
    morning — so nothing outside `cli/runway.py` may build the wording again.

    `-m ..<id>` is the runway hint's own spelling: the block's whole span, not the
    `-m <id>` a constraint hint names. Keyed on a directory glob rather than a list of
    files, so a CLI module written tomorrow is covered tomorrow."""

    def test_only_the_runway_module_builds_the_next_block_command(self):
        cli_dir = Path(runway_cli.__file__).parent
        offenders = [
            path.relative_to(cli_dir.parent).as_posix()
            for path in sorted(cli_dir.rglob("*.py"))
            if path.name != "runway.py" and "workout generate -m .." in path.read_text()
        ]
        self.assertEqual(offenders, [])


class RunwaySurfaceTest(unittest.TestCase):
    """The CLI surfaces, driven end to end against a real database."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

    def setUp(self):
        rebind_test_db(test_db)
        clear_all_tables(test_db)
        garmin = patch.object(runtime, "garmin", MagicMock(), create=True)
        garmin.start()
        self.addCleanup(garmin.stop)

    def _plan(self, blocks, goal_offset=45, title="Zurich Marathon"):
        """A goal with a periodization whose blocks are `(start offset, end offset)`."""
        obj_id = test_db.add_objective(
            title=title, target_date=_out(goal_offset), sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build then sharpen.",
            goals_hash="g", constraints_hash="c",
            mesocycles=[
                {"name": f"Block {i}", "start_date": _out(s), "end_date": _out(e),
                 "focus": "Endurance"}
                for i, (s, e) in enumerate(blocks, start=1)
            ],
        )
        return obj_id

    def _sessions(self, *offsets, sport_type="running"):
        for offset in offsets:
            save_workout(
                test_db, _out(offset), sport_type, f"Session {offset}",
                description="Easy.", duration_minutes=40,
            )

    def test_status_names_the_cliff_and_the_command(self):
        self._plan([(-30, 45)])
        self._sessions(0, 3)
        with patch("trainmate.cli.status.ensure_recent_data"):
            code, out, _ = run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertIn("Scheduled workouts run out in 3 day(s)", out)
        self.assertIn("workout generate", out)

    def test_status_speaks_even_with_no_goal_on_record(self):
        """The plan-cliff-no-goal wording exists because nothing is on record, so it must
        print outside `status`'s `if objectives:` branch (§4)."""
        self._plan([(-30, -1)], goal_offset=-1)
        self._sessions(-1)
        with patch("trainmate.cli.status.ensure_recent_data"):
            code, out, _ = run_cli(["status"])
        self.assertEqual(code, 0)
        flat = " ".join(out.split())
        self.assertIn("Next Goal: None", flat)
        self.assertIn("nothing is planned beyond it", flat)
        self.assertIn("goal add", flat)

    def test_adapt_refuses_over_a_plan_wholly_behind_today(self):
        """It used to adapt against `get_active_mesocycle`'s absolute-first-block fallback
        and close with an all-clear over an empty calendar (§4)."""
        self._plan([(-60, -2)], goal_offset=-2)
        self._sessions(-2)
        coach = MagicMock()
        with patch.object(runtime, "coach_service", coach, create=True), \
                patch("trainmate.cli.workouts.generate.ensure_recent_data"):
            code, out, _ = run_cli(["workout", "adapt", "-y"])
        self.assertEqual(code, 0)
        coach.workout_adapt.assert_not_called()
        self.assertIn("nothing is planned beyond it", " ".join(out.split()))
        self.assertNotIn("All metrics are green", out)

    def test_list_marks_where_the_schedule_stops(self):
        self._plan([(-30, 45)])
        self._sessions(0, 2)
        code, out, _ = run_cli(["workout", "list"])
        self.assertEqual(code, 0)
        self.assertIn("end of scheduled workouts", out)
        self.assertIn("plan continues to", out)

    def test_list_says_end_of_plan_when_the_schedule_reaches_it(self):
        self._plan([(-30, 2)], goal_offset=2)
        self._sessions(0, 2)
        code, out, _ = run_cli(["workout", "list"])
        self.assertIn("end of scheduled workouts (end of plan)", out)

    def test_an_empty_listing_renders_the_marker_alone(self):
        """Today `run_workout_list` prints a header and then nothing, which is exactly the
        passed state where the athlete most needs the gap named (§4)."""
        self._plan([(-30, 45)])
        self._sessions(-3)
        code, out, _ = run_cli(["workout", "list"])
        self.assertEqual(code, 0)
        self.assertIn("end of scheduled workouts", out)

    def test_a_listing_that_stops_short_of_the_cliff_says_nothing(self):
        self._plan([(-30, 45)])
        self._sessions(0, 2, 5)
        code, out, _ = run_cli(["workout", "list", "-d", f"{_out(0)}..{_out(2)}"])
        self.assertEqual(code, 0)
        self.assertNotIn("end of scheduled workouts", out)

    def test_the_simple_surface_suppresses_the_expert_hint(self):
        """One fact gets one wording per message: under simple render the bot words it
        itself, so `workout adapt`'s hint stays out of the way (§6)."""
        self._plan([(-30, 45)])
        self._sessions(0, 2)
        coach = MagicMock()
        coach.workout_adapt.return_value.workouts = ()
        coach.workout_adapt.return_value.new_constraints = ()
        coach.workout_adapt.return_value.reason = "All good."
        with patch.object(runtime, "coach_service", coach, create=True), \
                patch("trainmate.cli.workouts.generate.ensure_recent_data"), \
                patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(["workout", "adapt", "-y"])
        self.assertEqual(code, 0)
        coach.workout_adapt.assert_called_once()
        self.assertNotIn("Scheduled workouts run out", out)

    def test_the_refusal_still_speaks_on_the_simple_surface(self):
        """The hint is suppressed there, but a refusing adapt must still say why — in
        words the athlete can act on. The expert refusal names `goal add` and `plan
        generate`, two commands companion mode cannot type, so the companion form reuses
        the morning push's plan-cliff wording (DESIGN_render_persona.md §5)."""
        self._plan([(-60, -2)], goal_offset=-2)
        self._sessions(-2)
        coach = MagicMock()
        with patch.object(runtime, "coach_service", coach, create=True), \
                patch("trainmate.cli.workouts.generate.ensure_recent_data"), \
                patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(["workout", "adapt", "-y"])
        self.assertEqual(code, 0)
        coach.workout_adapt.assert_not_called()
        flowed = " ".join(out.split())
        self.assertIn("setting up a new goal happens from the computer", flowed)
        self.assertNotIn("goal add", flowed)

    def test_the_nudge_is_answerable_by_the_command_it_names(self):
        """A cliff must not survive the run that answers it. The generation span opens the
        day after the covered days, so acting on the nudge adds days rather than rewriting
        the ones already on record (DESIGN_cli_selectors.md §8)."""
        self._plan([(-30, 3), (4, 45)])
        self._sessions(0, 3)
        self.assertEqual(runway_cli.current_runway()["kind"], RUNWAY_BLOCK)
        span = generate_cli._resolve_span(argparse.Namespace())
        self.assertEqual(span[0], _out(4))

    def test_the_companion_week_view_names_the_end_of_the_schedule(self):
        self._plan([(-30, 45)])
        self._sessions(0, 2)
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(["workout", "list"])
        self.assertEqual(code, 0)
        self.assertIn("That's the end of the current schedule", out)

    def test_the_companion_week_view_offers_the_one_tap_fix(self):
        """The week view is where she looks at the schedule, so it is where noticing that
        it stops should turn into acting on it — the push's offer, without the wait."""
        self._plan([(-30, 45)])
        self._sessions(0, 2)
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(["workout", "list"])
        self.assertEqual(code, 0)
        self.assertIn(RUNWAY_BUTTON_LABEL.split(" ", 1)[1], out)
        self.assertIn("workout generate", out)

    def test_the_week_view_button_carries_the_block_flagged_command(self):
        self._plan([(-30, 2), (3, 45)])
        self._sessions(0, 2)
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(["workout", "list"])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"workout generate -m \.\.\d+")

    def test_the_week_view_states_a_plan_cliff_and_offers_nothing(self):
        """Periodization stays operator work in companion mode, so the end of the plan is
        named and no button follows it."""
        self._plan([(-30, 2)], goal_offset=2)
        self._sessions(0, 2)
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(["workout", "list"])
        self.assertEqual(code, 0)
        self.assertIn("That's the end of the current schedule", out)
        self.assertNotIn(RUNWAY_BUTTON_LABEL.split(" ", 1)[1], out)

    def test_the_end_note_draws_alone_while_the_cliff_is_still_far_off(self):
        """The note is a fact of the listing, the button an offer worth making — so a
        schedule that stops next month names its end without pressing for action."""
        self._plan([(-30, 45)])
        self._sessions(0, 20)
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(
                ["workout", "list", "-d", f"{_out(0)}..{_out(30)}"])
        self.assertEqual(code, 0)
        self.assertIn("That's the end of the current schedule", out)
        self.assertNotIn(RUNWAY_BUTTON_LABEL.split(" ", 1)[1], out)

    def test_a_week_view_stopping_short_of_the_cliff_offers_nothing(self):
        """The crossing gate stands on its own: the nudge is live, but this listing never
        reaches the end of the schedule, so there is nothing for a button to sit under."""
        self._plan([(-30, 45)])
        self._sessions(0, 2, 5)
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            code, out, _ = run_cli(["workout", "list", "-d", f"{_out(0)}..{_out(2)}"])
        self.assertEqual(code, 0)
        self.assertNotIn(RUNWAY_BUTTON_LABEL.split(" ", 1)[1], out)


class MorningPushRunwayTest(unittest.TestCase):
    """`bot morning` — the §6 line, its button, and the silence past the window."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

    def setUp(self):
        rebind_test_db(test_db)
        clear_all_tables(test_db)
        garmin = patch.object(runtime, "garmin", MagicMock(), create=True)
        garmin.start()
        self.addCleanup(garmin.stop)

    def _plan(self, blocks, goal_offset=45):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=_out(goal_offset), sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build.", goals_hash="g", constraints_hash="c",
            mesocycles=[
                {"name": f"Block {i}", "start_date": _out(s), "end_date": _out(e),
                 "focus": "Endurance"}
                for i, (s, e) in enumerate(blocks, start=1)
            ],
        )

    def _sessions(self, *offsets):
        for offset in offsets:
            save_workout(
                test_db, _out(offset), "running", f"Session {offset}",
                description="Easy.", duration_minutes=40,
            )

    def test_a_span_cliff_adds_a_line_and_a_one_tap_fix(self):
        self._plan([(-30, 45)])
        self._sessions(0, 3)
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn("your schedule runs out", out)
        self.assertIn(RUNWAY_BUTTON_LABEL.split(" ", 1)[1], out)
        self.assertIn("workout generate", out)

    def test_a_block_cliff_sends_the_block_flagged_command(self):
        self._plan([(-30, 3), (4, 45)])
        self._sessions(0, 3)
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn(RUNWAY_BUTTON_LABEL.split(" ", 1)[1], out)
        self.assertRegex(out, r"workout generate -m \.\.\d+")

    def test_a_plan_cliff_is_prose_with_no_button(self):
        self._plan([(-30, 3)], goal_offset=3)
        self._sessions(0, 3)
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn("that's the goal you've been training toward", out)
        self.assertNotIn(RUNWAY_BUTTON_LABEL.split(" ", 1)[1], out)

    def test_the_passed_state_replaces_the_rest_day_line(self):
        """"Rest day — enjoy it 🎉" describes an exhausted schedule as a coaching
        decision, which it is not (§6)."""
        self._plan([(-30, 45)])
        self._sessions(-2)
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn(SIMPLE_PASSED_LINE, out)
        self.assertNotIn("Rest day", out)
        self.assertIn(RUNWAY_BUTTON_LABEL.split(" ", 1)[1], out)

    def test_past_the_window_the_push_sends_nothing_at_all(self):
        """Silence is the honest state for "no plan covers today" — not the rest-day lie,
        not a stale celebration (§6)."""
        self._plan([(-60, -20)], goal_offset=-20)
        self._sessions(-20)
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")
        self.assertEqual(test_db.get_setting(MORNING_MARKER), today_str())

    def test_a_silent_morning_spends_no_adaptation(self):
        """The silence is decided before `adapt-first` runs, so a dead plan does not burn
        an LLM call every morning for a message nobody sends."""
        from trainmate.config import config
        self._plan([(-60, -20)], goal_offset=-20)
        self._sessions(-20)
        coach = MagicMock()
        with patch.dict(config.data, {"telegram": {"push": {"adapt_first": True}}}), \
                patch.object(runtime, "coach_service", coach, create=True), \
                patch("trainmate.cli.bot.ensure_recent_data"):
            code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")
        coach.workout_adapt.assert_not_called()

    def test_a_healthy_schedule_keeps_its_ordinary_rest_day(self):
        """An empty day inside a schedule that runs on is a rest day the coach chose."""
        self._plan([(-30, 45)])
        self._sessions(20)
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn("Rest day", out)
        self.assertNotIn(BUTTONS_SENTINEL, out)

    def test_a_fresh_instance_with_nothing_generated_still_greets(self):
        """No schedule ever generated is a cold start, not an exhausted plan."""
        code, out, _ = run_cli(["bot", "morning"])
        self.assertEqual(code, 0)
        self.assertIn("Rest day", out)


class CoverageInvariantTest(unittest.TestCase):
    """§2.1 — generation covers every date of its span, by construction."""

    def test_the_task_tells_the_model_to_cover_every_date(self):
        from trainmate.coach.engine import CoachEngine
        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = {"reasoning": "r", "workouts": []}
            CoachEngine()._workout_generate_logic(
                objectives=[], constraints=[], today_str=TODAY, start_str=TODAY,
                guidelines="", profile="", strategy="", meso_text="", learnings="",
                num_days=7, metrics=[], completed_activities=[], baseline=None,
                pmc_warmup_cutoff=None, pmc_context="", block_progress=None,
                block_has_intensity=False, zone_currencies={}, anchor_history="",
                standing_workouts=[],
            )
            system_prompt = client.complete.call_args[0][0]
        self.assertIn("Cover EVERY date of the span", system_prompt)

    def test_the_backstop_fills_the_dates_the_model_left_out(self):
        from trainmate.coach.service.workouts import WorkoutGenMixin
        proposed = [{"date": _d(0), "sport_type": "running", "title": "Long run"}]
        filled = WorkoutGenMixin._fill_coverage_gaps(proposed, _d(0), _d(3))
        self.assertEqual(len(filled), 4)
        rest = [w for w in filled if w["sport_type"] == "rest"]
        self.assertEqual([w["date"] for w in rest], [_d(1), _d(2), _d(3)])
        self.assertEqual(rest[0]["title"], "Rest Day")
        self.assertNotIn("forced constraint", rest[0]["description"])

    def test_an_empty_proposal_is_not_salvaged_into_a_span_of_rest(self):
        from trainmate.coach.service.workouts import WorkoutGenMixin
        self.assertEqual(WorkoutGenMixin._fill_coverage_gaps([], _d(0), _d(3)), [])


class SimpleGeneratePreviewTest(unittest.TestCase):
    """The runway button makes `workout generate` reachable by tap, so its preview has to
    read as her week rather than as a `<pre>` table (§6)."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

    def setUp(self):
        rebind_test_db(test_db)
        clear_all_tables(test_db)
        garmin = patch.object(runtime, "garmin", MagicMock(), create=True)
        garmin.start()
        self.addCleanup(garmin.stop)
        env = patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"})
        env.start()
        self.addCleanup(env.stop)

    def test_the_proposed_weeks_render_as_dated_lines(self):
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=_out(45), sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build.", goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": "Base", "start_date": _out(-30), "end_date": _out(45),
                         "focus": "Endurance"}],
        )
        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = {
                "reasoning": "A steady week to open the block.",
                "workouts": [
                    {"date": _out(0), "sport_type": "running", "title": "Easy run",
                     "description": "Conversational.", "duration_minutes": 40},
                    {"date": _out(1), "sport_type": "rest", "title": "Rest Day",
                     "description": "Off.", "duration_minutes": 0},
                ],
            }
            # `y` all the way through: the tapped flow confirms the staleness question,
            # the regeneration and the apply, each rendered as buttons by the bot.
            code, out, _ = run_cli(
                ["workout", "generate", "-d", f"{_out(0)}..{_out(1)}"], input_value="y"
            )
        self.assertEqual(code, 0)
        self.assertNotIn("WORKOUTS PROPOSED BY COACH", out)
        self.assertIn("A steady week to open the block.", out)
        self.assertIn("🏃", out)
        # Rest reads as intended rest, not as a generic session (§6).
        self.assertIn(SPORT_EMOJI["rest"], out)


class AddGoalIntentTest(unittest.TestCase):
    """`add_goal` supersedes the reply-only `new_goal` this design added
    (DESIGN_bot_simple_frontend.md §12.5): the goal row is now captured for real, and
    what stays behind the §7 line is the periodization built on it."""

    def test_the_intent_reaches_the_capture_and_not_a_reply(self):
        from trainmate.cli.bot import ROUTER_INTENTS
        import trainmate_bot
        self.assertIn("add_goal", ROUTER_INTENTS)
        self.assertNotIn("new_goal", ROUTER_INTENTS)
        # A capture, so it carries the athlete's text rather than running fixed argv.
        self.assertNotIn("add_goal", trainmate_bot.ROUTER_INTENT_ARGV)
        self.assertEqual(trainmate_bot.ROUTER_CAPTURE_INTENTS["add_goal"], "add_goal")
        self.assertFalse(hasattr(trainmate_bot, "new_goal_reply"))

    def test_the_plan_for_it_is_still_named_as_the_operators_work(self):
        from trainmate.cli.render import simple_plan_setup_line, simple_plan_wrapped_line
        for line in (simple_plan_setup_line(), simple_plan_wrapped_line()):
            self.assertIn("from the computer", line)
            # "coach" formally means the app, so the human who runs `plan generate` is
            # named (DESIGN_render_persona.md §5).
            self.assertNotIn("your coach", line)
            self.assertIn(config.telegram_operator_name, line)


if __name__ == "__main__":
    unittest.main()
