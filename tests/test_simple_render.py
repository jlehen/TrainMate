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
    """`telegram.ui`, `telegram.push.*` and `llm.router_model` — defaults and set
    values (§3, §4.3, §5.4)."""

    def _config(self, data):
        cfg = object.__new__(Config)
        cfg.data = data
        return cfg

    def test_defaults(self):
        cfg = self._config({})
        self.assertEqual(cfg.telegram_ui, "expert")
        self.assertTrue(cfg.telegram_push_enabled)
        self.assertEqual(cfg.telegram_push_morning_time, "08:00")
        self.assertEqual(cfg.telegram_push_morning_deadline, "15:00")
        self.assertFalse(cfg.telegram_push_adapt_first)
        self.assertIsNone(cfg.router_llm_model)

    def test_set_values(self):
        cfg = self._config({
            "telegram": {
                "ui": " Simple ",
                "push": {
                    "enabled": False,
                    "morning_time": "07:30",
                    "morning_deadline": "12:00",
                    "adapt_first": True,
                },
            },
            "llm": {"router_model": "cheap/model"},
        })
        self.assertEqual(cfg.telegram_ui, "simple")
        self.assertFalse(cfg.telegram_push_enabled)
        self.assertEqual(cfg.telegram_push_morning_time, "07:30")
        self.assertEqual(cfg.telegram_push_morning_deadline, "12:00")
        self.assertTrue(cfg.telegram_push_adapt_first)
        self.assertEqual(cfg.router_llm_model, "cheap/model")

    def test_blank_router_model_reads_as_absent(self):
        cfg = self._config({"llm": {"router_model": "   "}})
        self.assertIsNone(cfg.router_llm_model)


if __name__ == "__main__":
    unittest.main()
