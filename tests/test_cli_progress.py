"""Pure formatting-helper tests for `tm progress` (trainmate/cli/progress.py),
per the `coach/formatting.py` precedent: no DB, no CLI dispatch.
"""
import os
import unittest

os.environ.setdefault("NO_COLOR", "1")  # keep assertions ANSI-free

from trainmate.util import visible_len, wrap_text
from trainmate.cli.progress import (
    sparkline, render_bar, truncate_label, format_form_line, render_progress,
    format_weekly_table, table_rows, _week_row, MESO_COL_WIDTH,
)


class TestSparkline(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(sparkline([]), "")

    def test_one_char_per_value(self):
        self.assertEqual(len(sparkline([1, 2, 3, 4])), 4)

    def test_flat_series_uses_the_floor_glyph(self):
        self.assertEqual(sparkline([5, 5, 5]), "▁▁▁")

    def test_none_cells_render_blank(self):
        spark = sparkline([None, 1, 2])
        self.assertEqual(spark[0], " ")
        self.assertEqual(len(spark), 3)

    def test_monotonic_increase_maps_to_monotonic_levels(self):
        spark = sparkline([1, 2, 3, 4, 5])
        self.assertEqual(spark[0], "▁")
        self.assertEqual(spark[-1], "█")


class TestRenderBar(unittest.TestCase):
    def test_zero_scale_max_is_empty(self):
        self.assertEqual(render_bar(10, 0), "░" * 9)

    def test_full_value_fills_the_bar(self):
        self.assertEqual(render_bar(100, 100), "▓" * 9)

    def test_half_value_fills_about_half(self):
        self.assertEqual(render_bar(50, 100).count("▓"), 4)


class TestTruncateLabel(unittest.TestCase):
    def test_none_becomes_dash(self):
        self.assertEqual(truncate_label(None), "—")

    def test_short_label_unchanged(self):
        self.assertEqual(truncate_label("Build 2"), "Build 2")

    def test_long_label_truncated_with_ellipsis(self):
        result = truncate_label("Base Building Phase")
        self.assertEqual(len(result), MESO_COL_WIDTH)
        self.assertTrue(result.endswith("…"))


class TestFormLine(unittest.TestCase):
    def test_tags_source_and_shows_ctl_atl_tsb(self):
        line = format_form_line("actual", 55.0, 61.0, -6.0)
        self.assertIn("FORM today (actual)", line)
        self.assertIn("CTL 55", line)
        self.assertIn("ATL 61", line)
        self.assertIn("TSB -6", line)

    def test_planned_tag_when_today_pending(self):
        self.assertIn("(planned)", format_form_line("planned", 55.0, 61.0, -6.0))

    def test_still_warming_when_no_pmc(self):
        self.assertIn("PMC still warming", format_form_line(None, None, None, None))


class TestWeekRow(unittest.TestCase):
    def _week(self, **overrides):
        week = {
            "week_commencing": "2026-06-22", "planned_load": 320.0,
            "actual_load": 214.0, "in_progress": False,
            "meso_label": "Build 2", "meso_source": "plan",
        }
        week.update(overrides)
        return week

    def test_past_governed_week_shows_bar_and_percentage(self):
        row = _week_row(self._week(), 320.0, "2026-07-03", None)
        self.assertIn("320", row)
        self.assertIn("214", row)
        self.assertIn("67%", row)

    def test_ungoverned_week_shows_dash_and_no_percentage(self):
        row = _week_row(
            self._week(planned_load=None, meso_source="inferred", meso_label="~Base"),
            320.0, "2026-07-03", None,
        )
        self.assertIn("—", row)
        self.assertNotIn("%", row)

    def test_future_week_shows_planned_only(self):
        row = _week_row(
            self._week(week_commencing="2026-07-13", actual_load=0.0),
            320.0, "2026-07-03", None,
        )
        self.assertIn("(planned)", row)

    def test_in_progress_week_uses_elapsed_planned(self):
        row = _week_row(
            self._week(week_commencing="2026-06-29", in_progress=True,
                       planned_load=340.0, planned_load_elapsed=150.0,
                       actual_load=138.0),
            360.0, "2026-07-03", None,
        )
        self.assertIn("*", row)
        self.assertIn("150", row)          # elapsed, not the full 340
        self.assertIn("92%", row)          # round(138/150*100)

    def test_partial_final_week_marked(self):
        # plan ends Wed 2026-07-08, mid-week -> the week is marked partial.
        row = _week_row(
            self._week(week_commencing="2026-07-06", actual_load=0.0),
            360.0, "2026-07-03", "2026-07-08",
        )
        self.assertIn("*", row)


class TestWeeklyTableWidth(unittest.TestCase):
    def _weeks(self):
        return [
            {"week_commencing": "2026-06-22", "planned_load": 320.0,
             "actual_load": 214.0, "in_progress": False,
             "meso_label": "Build 2", "meso_source": "plan"},
            {"week_commencing": "2026-06-29", "planned_load": 340.0,
             "planned_load_elapsed": 150.0, "actual_load": 138.0,
             "in_progress": True, "meso_label": "Build 3", "meso_source": "plan"},
            {"week_commencing": "2026-07-06", "planned_load": 360.0,
             "actual_load": 0.0, "in_progress": False,
             "meso_label": "Build 3", "meso_source": "plan"},
        ]

    def test_table_stays_within_the_48_column_budget(self):
        for line in table_rows(self._weeks(), "2026-06-30", None):
            self.assertLessEqual(visible_len(line), 48, msg=repr(line))

    def test_zero_max_scale_does_not_divide(self):
        weeks = [{"week_commencing": "2026-06-22", "planned_load": 0.0,
                  "actual_load": 0.0, "in_progress": False,
                  "meso_label": None, "meso_source": None}]
        rows = table_rows(weeks, "2026-07-03", None)  # must not raise
        self.assertTrue(rows)


def _day(date, ctl=None, atl=None, tsb=None, source="actual", load=0.0):
    return {"date": date, "load": load, "source": source,
            "ctl": ctl, "atl": atl, "tsb": tsb}


def _payload(days, weeks=None, plan_end=None, objectives=None, warnings=None,
             meso_bands=None, today="2026-07-03"):
    return {
        "today": today, "plan_end": plan_end, "days": days,
        "weeks": weeks or [], "objectives": objectives or [],
        "warnings": warnings or [], "meso_bands": meso_bands or [],
    }


class TestRenderProgress(unittest.TestCase):
    MARATHON = {"id": 1, "title": "Marathon", "target_date": "2026-09-30",
                "priority": 1, "status": "active"}

    def test_plan_gap_banner_when_objective_not_reached(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual"),
                _day("2026-07-31", 61, 60, 1, "planned")]
        lines = render_progress(
            _payload(days, plan_end="2026-07-31", objectives=[self.MARATHON]), 8)
        text = "\n".join(lines)
        self.assertIn("FORM today (actual)", text)
        self.assertIn("plan generated through", text)
        self.assertIn("workout generate --until-goal", text)

    def test_per_objective_projection_when_reached(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual"),
                _day("2026-09-30", 68, 56, 12, "planned")]
        lines = render_progress(
            _payload(days, plan_end="2026-09-30", objectives=[self.MARATHON]), 8)
        text = "\n".join(lines)
        self.assertIn("projected CTL 68", text)
        self.assertIn("TSB +12", text)
        self.assertNotIn("plan generated through", text)

    def test_reached_objective_and_later_gap_both_shown(self):
        # Plan reaches obj1 but a later obj2 is beyond plan end -> per-objective line
        # AND the gap banner (§3, multi-objective season).
        days = [_day("2026-07-03", 55, 61, -6, "actual"),
                _day("2026-09-30", 68, 56, 12, "planned")]
        objs = [self.MARATHON,
                {"id": 2, "title": "Ultra", "target_date": "2026-11-30",
                 "priority": 1, "status": "active"}]
        text = "\n".join(render_progress(
            _payload(days, plan_end="2026-09-30", objectives=objs), 8))
        self.assertIn("projected CTL 68", text)
        self.assertIn("plan generated through", text)

    def test_lapsed_plan_banner(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual")]
        text = "\n".join(render_progress(_payload(days, plan_end="2026-05-20"), 8))
        self.assertIn("plan lapsed", text)

    def test_no_plan_banner(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual")]
        text = "\n".join(render_progress(_payload(days, plan_end=None), 8))
        self.assertIn("no plan generated", text)

    def test_still_warming_state(self):
        days = [_day("2026-07-03")]  # no PMC today
        text = "\n".join(render_progress(_payload(days, plan_end=None), 8))
        self.assertIn("PMC still warming", text)

    def test_lag_note_carried_when_tsb_shown(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual")]
        text = "\n".join(render_progress(_payload(days, plan_end=None), 8))
        self.assertIn("TSB is CTL(yesterday)", text)

    def test_footer_warnings_except_plan_gap(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual"),
                _day("2026-07-31", 61, 60, 1, "planned")]
        warnings = ["plan generated through 2026-07-31 (9 wks before objective 2026-09-30)",
                    "2 planned workouts lack TSS/RPE — count as 0"]
        weeks = [{"week_commencing": "2026-06-29", "planned_load": 100.0,
                  "actual_load": 90.0, "in_progress": True,
                  "planned_load_elapsed": 80.0,
                  "meso_label": "Build", "meso_source": "plan"}]
        lines = render_progress(
            _payload(days, weeks=weeks, plan_end="2026-07-31",
                     objectives=[self.MARATHON], warnings=warnings), 8)
        # The plan-gap warning is rendered as the banner, not duplicated in the footer.
        footer = [ln for ln in lines if "lack TSS/RPE" in ln]
        self.assertEqual(len(footer), 1)
        self.assertEqual(sum(1 for ln in lines if "plan generated through" in ln), 1)

    def test_every_line_within_48_columns(self):
        os.environ["TRAINMATE_WRAP_WIDTH"] = "48"
        try:
            days = [_day("2026-07-03", 55, 61, -6, "actual"),
                    _day("2026-07-31", 61, 60, 1, "planned")]
            weeks = [{"week_commencing": "2026-06-22", "planned_load": 320.0,
                      "actual_load": 262.0, "in_progress": False,
                      "meso_label": "Build 2", "meso_source": "plan"}]
            lines = render_progress(
                _payload(days, weeks=weeks, plan_end="2026-07-31",
                         objectives=[self.MARATHON],
                         warnings=["2 planned workouts lack TSS/RPE — count as 0"]), 8)
            for line in lines:
                wrapped = wrap_text(line) if visible_len(line) > 48 else line
                for sub in wrapped.split("\n"):
                    self.assertLessEqual(visible_len(sub), 48, msg=repr(sub))
        finally:
            del os.environ["TRAINMATE_WRAP_WIDTH"]


if __name__ == "__main__":
    unittest.main()
