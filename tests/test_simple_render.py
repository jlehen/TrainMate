"""Simple-rendering helpers and the companion-mode config knobs
(DESIGN_bot_simple_frontend.md §3, §4.3, §6)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trainmate.cli import common
from trainmate.config import Config


class IsSimpleRenderTest(unittest.TestCase):
    def test_unset_is_expert(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRAINMATE_RENDER", None)
            self.assertFalse(common.is_simple_render())

    def test_simple_selects_the_companion_rendering(self):
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"}):
            self.assertTrue(common.is_simple_render())
        with patch.dict(os.environ, {"TRAINMATE_RENDER": " SIMPLE "}):
            self.assertTrue(common.is_simple_render())

    def test_other_values_stay_expert(self):
        with patch.dict(os.environ, {"TRAINMATE_RENDER": "fancy"}):
            self.assertFalse(common.is_simple_render())


class SessionLineTest(unittest.TestCase):
    def test_full_line_with_lead_and_duration(self):
        w = {"sport_type": "running", "title": "Easy run", "duration_minutes": 40}
        self.assertEqual(
            common.simple_session_line(w, lead="Today"), "🏃 Today: Easy run — 40 min"
        )

    def test_without_duration_or_lead(self):
        w = {"sport_type": "cycling", "title": "Spin"}
        self.assertEqual(common.simple_session_line(w), "🚴 Spin")

    def test_unknown_sport_gets_the_generic_emoji(self):
        w = {"sport_type": "curling", "title": "Sweep"}
        self.assertTrue(
            common.simple_session_line(w).startswith(common.DEFAULT_SPORT_EMOJI)
        )

    def test_every_canonical_sport_has_its_own_emoji(self):
        from trainmate.sports import CANONICAL_SPORTS
        for sport in CANONICAL_SPORTS:
            self.assertIn(sport, common.SPORT_EMOJI)


class DayLinesTest(unittest.TestCase):
    def test_empty_day_is_a_rest_day(self):
        self.assertEqual(
            common.simple_day_lines([], "2026-08-25"), [common.REST_DAY_LINE]
        )

    def test_session_day_includes_the_description(self):
        with patch("trainmate.cli.common._today_str", return_value="2026-08-25"):
            lines = common.simple_day_lines(
                [{"sport_type": "running", "title": "Easy run",
                  "duration_minutes": 40, "date": "2026-08-25",
                  "description": "Conversational pace."}],
                "2026-08-25",
            )
        self.assertEqual(lines[0], "🏃 Today: Easy run — 40 min")
        self.assertIn("Conversational pace.", lines[1])

    def test_another_day_is_named_not_called_today(self):
        with patch("trainmate.cli.common._today_str", return_value="2026-08-25"):
            lines = common.simple_day_lines(
                [{"sport_type": "running", "title": "Long run",
                  "date": "2026-08-27"}],
                "2026-08-27",
            )
        self.assertNotIn("Today", lines[0])
        self.assertIn("2026-08-27", lines[0])


    def _today(self, verdicts=None):
        with patch("trainmate.cli.common._today_str", return_value="2026-08-25"):
            return common.simple_day_lines(
                [{"id": 7, "sport_type": "running", "title": "Easy run",
                  "duration_minutes": 40, "date": "2026-08-25",
                  "description": "Conversational pace."}],
                "2026-08-25", verdicts,
            )

    def test_a_session_already_trained_is_acknowledged(self):
        """Asking for a day you have already trained should say so, not just re-read the
        prescription back (DESIGN_bot_simple_frontend.md §6)."""
        lines = self._today({7: {"status": "done", "label": "Done", "reasons": []}})
        self.assertEqual(lines[1], common.SIMPLE_DONE_LINE)

    def test_a_session_that_came_in_off_plan_still_counts_as_done(self):
        lines = self._today({7: {"status": "partial", "label": "Partial",
                                 "reasons": ["duration mismatch"]}})
        self.assertEqual(lines[1], common.SIMPLE_DONE_LINE)
        # The mismatch itself is expert detail — the companion never reads it out.
        self.assertFalse(any("mismatch" in line for line in lines))

    def test_a_session_still_ahead_or_missed_says_nothing(self):
        """The §6 tone rule: a gap is never the lead, and 'you have not done it yet' is
        not news to someone reading their own day."""
        for status, label in (("pending", "Not yet"), ("missed", "Missed")):
            lines = self._today({7: {"status": status, "label": label, "reasons": []}})
            self.assertNotIn(common.SIMPLE_DONE_LINE, lines)
            self.assertEqual(lines, self._today())


class WeekLinesTest(unittest.TestCase):
    def test_empty_window_is_a_break(self):
        lines = common.simple_week_lines([])
        self.assertEqual(len(lines), 1)
        self.assertIn("enjoy the break", lines[0])

    def test_sessions_are_dated_and_counted(self):
        lines = common.simple_week_lines([
            {"sport_type": "running", "title": "Easy run",
             "duration_minutes": 40, "date": "2026-08-25"},
            {"sport_type": "cycling", "title": "Endurance ride",
             "duration_minutes": 60, "date": "2026-08-27"},
        ])
        self.assertIn("🗓 Coming up:", lines[0])
        self.assertIn("Tue 25 · 🏃 Easy run — 40 min", lines[1])
        self.assertIn("2 sessions planned", lines[-1])

    def test_singular_session_word(self):
        lines = common.simple_week_lines(
            [{"sport_type": "running", "title": "Easy run", "date": "2026-08-25"}]
        )
        self.assertIn("1 session planned", lines[-1])

    def test_a_trained_session_gets_the_check_and_the_count(self):
        lines = common.simple_week_lines(
            [{"id": 1, "sport_type": "running", "title": "Easy run",
              "date": "2026-08-25"},
             {"id": 2, "sport_type": "cycling", "title": "Endurance ride",
              "date": "2026-08-27"}],
            verdicts={1: {"status": "done"}},
        )
        self.assertIn("✅", lines[1])
        self.assertNotIn("✅", lines[2])
        self.assertIn("1 of 2 sessions already done", lines[-1])

    def test_missed_and_pending_sessions_say_nothing(self):
        # §6 tone rule: a gap is never remarked on in the listing.
        lines = common.simple_week_lines(
            [{"id": 1, "sport_type": "running", "title": "Easy run",
              "date": "2026-08-25"}],
            verdicts={1: {"status": "missed"}},
        )
        self.assertNotIn("✅", lines[1])
        self.assertIn("1 session planned", lines[-1])


class WhenWordsTest(unittest.TestCase):
    """`simple_when` — the countdown vocabulary of the goal and plan views (§11)."""

    def test_the_near_words(self):
        self.assertEqual(common.simple_when("2026-08-25", "2026-08-25"), "today")
        self.assertEqual(common.simple_when("2026-08-26", "2026-08-25"), "tomorrow")
        self.assertEqual(common.simple_when("2026-08-30", "2026-08-25"), "in 5 days")

    def test_weeks_then_months(self):
        self.assertEqual(common.simple_when("2026-09-26", "2026-08-25"), "in 5 weeks")
        self.assertEqual(common.simple_when("2027-04-30", "2026-08-30"), "in 8 months")

    def test_a_past_date_reads_as_passed(self):
        self.assertEqual(common.simple_when("2026-08-20", "2026-08-25"), "passed")


class GoalLinesTest(unittest.TestCase):
    """`simple_goal_lines` — the companion goals view (§11): countdown words, no IDs
    or state tags, archived goals silent, completed ones one celebration line."""

    EVENT = {"id": 1, "title": "Marathon", "target_date": "2026-09-26",
             "sport_type": "running", "date_type": "event", "status": "active",
             "description": "Sub 4 hours."}
    HORIZON = {"id": 2, "title": "Climb faster", "target_date": "2026-09-30",
               "sport_type": "cycling", "date_type": "horizon", "status": "active",
               "description": ""}

    def test_event_goal_names_the_day_and_the_countdown(self):
        lines = common.simple_goal_lines([self.EVENT], "2026-08-25")
        self.assertIn("🎯 What you're training for:", lines[0])
        self.assertIn("🏃 Marathon — on Sat Sep 26 (in 5 weeks)", lines[1])
        self.assertIn("Sub 4 hours.", lines[2])

    def test_horizon_goal_reads_as_by_approximately(self):
        lines = common.simple_goal_lines([self.HORIZON], "2026-08-25")
        self.assertIn("🚴 Climb faster — by ~Wed Sep 30 (in 5 weeks)", lines[1])

    def test_no_expert_ids_or_tags_leak(self):
        for line in common.simple_goal_lines([self.EVENT], "2026-08-25"):
            self.assertNotIn("ID", line)
            self.assertNotIn("[UPCOMING]", line)

    def test_completed_goals_become_one_celebration_line(self):
        past = dict(self.EVENT, target_date="2026-05-01")
        lines = common.simple_goal_lines([past, self.EVENT], "2026-08-25")
        self.assertIn("Marathon — on Sat Sep 26", "\n".join(lines))
        self.assertIn("1 goal already behind you", lines[-1])

    def test_archived_goals_say_nothing(self):
        archived = dict(self.HORIZON, status="archived")
        lines = common.simple_goal_lines([self.EVENT, archived], "2026-08-25")
        self.assertNotIn("Climb faster", "\n".join(lines))

    def test_empty_is_an_invitation(self):
        lines = common.simple_goal_lines([], "2026-08-25")
        self.assertEqual(len(lines), 1)
        self.assertIn("No goal on the horizon", lines[0])


class PlanLinesTest(unittest.TestCase):
    """`simple_plan_lines` — the companion plan view (§11): done blocks checked, the
    active block located by week with its focus, future blocks dated, the goal day
    closing the road."""

    GOAL = {"id": 1, "title": "Marathon", "target_date": "2026-09-26",
            "sport_type": "running", "date_type": "event", "status": "active"}
    ACTIVE_MACRO = {"id": 6, "status": "active"}
    MESOCYCLES = [
        {"id": 1, "name": "Base", "start_date": "2026-07-27",
         "end_date": "2026-08-16", "focus": "Aerobic volume."},
        {"id": 2, "name": "Build", "start_date": "2026-08-17",
         "end_date": "2026-09-06", "focus": "Threshold work."},
        {"id": 3, "name": "Peak", "start_date": "2026-09-07",
         "end_date": "2026-09-16", "focus": "Race sharpening."},
    ]

    def _lines(self, today="2026-08-30"):
        return common.simple_plan_lines(
            self.GOAL, self.ACTIVE_MACRO, self.MESOCYCLES, today
        )

    def test_the_road_by_block(self):
        lines = self._lines()
        self.assertEqual(lines[0], "🧭 The road to Marathon:")
        self.assertIn("✅ Base — done", lines[1])
        self.assertIn("📍 Build — you're here, week 2 of 3", lines[2])
        self.assertIn("Threshold work.", lines[3])
        self.assertIn("⚪ Peak — starts Mon Sep 07, 10 days", lines[4])
        self.assertIn("🏁 The big day: Sat Sep 26 (in 4 weeks)", lines[-1])

    def test_exact_week_blocks_read_in_weeks(self):
        lines = common.simple_plan_lines(
            self.GOAL, self.ACTIVE_MACRO,
            [{"id": 3, "name": "Peak", "start_date": "2026-09-07",
              "end_date": "2026-09-20", "focus": "Race sharpening."}],
            "2026-08-30",
        )
        self.assertIn("⚪ Peak — starts Mon Sep 07, 2 weeks", lines[1])

    def test_a_long_focus_shrinks_to_its_first_sentence(self):
        wall = ("Three weeks: two loading microcycles plus a deload. LOADING WEEKS: "
                "1-2 threshold sessions accumulating 40+ min in zone, " + "x" * 300)
        mesocycles = [dict(self.MESOCYCLES[1], focus=wall)]
        lines = common.simple_plan_lines(
            self.GOAL, self.ACTIVE_MACRO, mesocycles, "2026-08-30"
        )
        self.assertEqual(
            lines[2], "Three weeks: two loading microcycles plus a deload."
        )

    def test_a_long_single_sentence_focus_is_cut_at_a_word(self):
        wall = "word " * 100
        mesocycles = [dict(self.MESOCYCLES[1], focus=wall)]
        lines = common.simple_plan_lines(
            self.GOAL, self.ACTIVE_MACRO, mesocycles, "2026-08-30"
        )
        self.assertTrue(lines[2].endswith("…"))
        self.assertLessEqual(len(lines[2]), 221)

    def test_only_the_active_block_carries_its_focus(self):
        joined = "\n".join(self._lines())
        self.assertNotIn("Aerobic volume.", joined)
        self.assertNotIn("Race sharpening.", joined)

    def test_a_horizon_goal_closes_without_a_big_day(self):
        goal = dict(self.GOAL, date_type="horizon")
        lines = common.simple_plan_lines(
            goal, self.ACTIVE_MACRO, self.MESOCYCLES, "2026-08-30"
        )
        self.assertIn("🏁 Building toward ~Sat Sep 26", lines[-1])

    def test_a_superseded_version_says_so(self):
        macro = dict(self.ACTIVE_MACRO, status="superseded")
        lines = common.simple_plan_lines(
            self.GOAL, macro, self.MESOCYCLES, "2026-08-30"
        )
        self.assertIn("older version", lines[1])

    def test_no_expert_ids_leak(self):
        for line in self._lines():
            self.assertNotIn("ID", line)
            self.assertNotIn("Macrocycle", line)

    def test_no_blocks_is_a_gentle_note(self):
        lines = common.simple_plan_lines(self.GOAL, self.ACTIVE_MACRO, [], "2026-08-30")
        self.assertIn("No training blocks drawn up yet", lines[-1])


class ProgressLinesTest(unittest.TestCase):
    """The §6 tone rule: every CTL branch reads as good news, and the trend line is
    always first (it doubles as the chart caption)."""

    TODAY = "2026-08-25"

    def _payload(self, start_ctl, end_ctl):
        return {"days": [
            {"date": "2026-07-25", "ctl": start_ctl},
            {"date": self.TODAY, "ctl": end_ctl},
        ]}

    def test_no_history_is_a_beginning(self):
        lines = common.simple_progress_lines({"days": []}, self.TODAY)
        self.assertIn("getting started", lines[0])
        self.assertEqual(len(lines), 2)

    def test_rising_ctl_is_climbing(self):
        lines = common.simple_progress_lines(self._payload(40.0, 50.0), self.TODAY)
        self.assertIn("climbing", lines[0])
        self.assertIn("25%", lines[0])

    def test_flat_ctl_is_steady(self):
        lines = common.simple_progress_lines(self._payload(50.0, 50.5), self.TODAY)
        self.assertIn("steady", lines[0])

    def test_falling_ctl_is_freshening_not_decay(self):
        lines = common.simple_progress_lines(self._payload(50.0, 40.0), self.TODAY)
        self.assertIn("freshening", lines[0])
        for word in ("down", "lost", "behind", "miss"):
            self.assertNotIn(word, lines[0].lower())

    def test_future_days_are_ignored(self):
        payload = {"days": [
            {"date": "2026-07-25", "ctl": 40.0},
            {"date": self.TODAY, "ctl": 50.0},
            {"date": "2026-09-25", "ctl": 90.0},
        ]}
        lines = common.simple_progress_lines(payload, self.TODAY)
        self.assertIn("25%", lines[0])


class ConstraintLinesTest(unittest.TestCase):
    """simple_constraint_lines — the §5.5 companion constraints view: day words, no
    IDs or tier tags, and an empty list that reads as a clean slate."""

    TODAY = "2026-08-25"

    def _line(self, **kw):
        c = {"title": "no run Thursday", "start_date": "2026-08-27",
             "end_date": "2026-08-27", "rest": 0}
        c.update(kw)
        return common.simple_constraint_lines([c], self.TODAY)[1]

    def test_empty_is_a_clean_slate(self):
        [line] = common.simple_constraint_lines([], self.TODAY)
        self.assertIn("Nothing on the list", line)

    def test_single_day_reads_as_the_day(self):
        self.assertEqual(self._line(), "• no run Thursday — Thu Aug 27")

    def test_today_reads_as_today(self):
        line = self._line(start_date=self.TODAY, end_date=self.TODAY)
        self.assertTrue(line.endswith("— today"), line)

    def test_range_names_both_ends(self):
        self.assertIn("Thu Aug 27 to Fri Sep 04", self._line(end_date="2026-09-04"))

    def test_rest_gets_the_sleep_bullet(self):
        self.assertTrue(self._line(rest=1).startswith("🛌"))

    def test_no_expert_ids_leak(self):
        line = self._line()
        self.assertNotIn("ID", line)
        self.assertNotIn("advisory", line)


class CompanionConfigKnobsTest(unittest.TestCase):
    """`telegram.ui` — the one companion knob config.yaml alone decides (§3). The push
    window and the router model resolve through the registry, and are covered against it
    in tests/test_cli_settings.py (DESIGN_settings.md §3)."""

    def _config(self, data):
        cfg = object.__new__(Config)
        cfg.data = data
        return cfg

    def test_ui_defaults_to_expert(self):
        self.assertEqual(self._config({}).telegram_ui, "expert")

    def test_ui_is_read_case_and_space_insensitively(self):
        cfg = self._config({"telegram": {"ui": " Simple "}})
        self.assertEqual(cfg.telegram_ui, "simple")

    def test_a_nested_key_path_reads_absent_levels_as_nothing(self):
        """What every registry entry seeded from config.yaml is built on (§3)."""
        cfg = self._config({"telegram": {"push": {"morning_time": "07:30"}}})
        self.assertEqual(cfg.raw("telegram", "push", "morning_time"), "07:30")
        self.assertIsNone(cfg.raw("telegram", "push", "enabled"))
        self.assertIsNone(cfg.raw("telegram", "nothing", "here"))
        self.assertIsNone(cfg.raw("absent"))


if __name__ == "__main__":
    unittest.main()
