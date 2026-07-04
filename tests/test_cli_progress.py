"""Pure formatting-helper tests for `tm progress` (trainmate/cli/progress.py),
per the `coach/formatting.py` precedent: no DB, no CLI dispatch.
"""
import os
import unittest

os.environ.setdefault("NO_COLOR", "1")  # keep assertions ANSI-free

from trainmate.cli.progress import (
    sparkline, render_bar, truncate_label, format_form_line,
    format_weekly_table, table_rows, _week_row, MESO_COL_WIDTH, WEEK_COL_WIDTH,
)


class TestSparkline(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(sparkline([]), "")

    def test_one_char_per_value(self):
        self.assertEqual(len(sparkline([1, 2, 3, 4])), 4)

    def test_flat_series_uses_a_single_repeated_char(self):
        spark = sparkline([5, 5, 5])
        self.assertEqual(len(set(spark)), 1)

    def test_monotonic_increase_maps_to_monotonic_levels(self):
        spark = sparkline([1, 2, 3, 4, 5])
        self.assertEqual(spark[0], "▁")   # lowest level
        self.assertEqual(spark[-1], "█")  # highest level


class TestRenderBar(unittest.TestCase):
    def test_zero_scale_max_is_empty(self):
        self.assertEqual(render_bar(10, 0), "░" * 9)

    def test_full_value_fills_the_bar(self):
        self.assertEqual(render_bar(100, 100), "▓" * 9)

    def test_half_value_fills_about_half(self):
        bar = render_bar(50, 100)
        self.assertEqual(bar.count("▓"), 4)  # round(9 * 0.5) == 4 (banker's rounding)


class TestTruncateLabel(unittest.TestCase):
    def test_none_becomes_dash(self):
        self.assertEqual(truncate_label(None), "-")

    def test_short_label_unchanged(self):
        self.assertEqual(truncate_label("Build 2"), "Build 2")

    def test_long_label_truncated_with_ellipsis(self):
        result = truncate_label("Base Building Phase")
        self.assertEqual(len(result), MESO_COL_WIDTH)
        self.assertTrue(result.endswith("…"))


class TestFormLine(unittest.TestCase):
    def test_contains_ctl_atl_tsb_and_week_count(self):
        line = format_form_line(55.0, 61.0, -6.0, [50, 52, 55], 8)
        self.assertIn("CTL 55", line)
        self.assertIn("ATL 61", line)
        self.assertIn("TSB -6", line)
        self.assertIn("(8w)", line)

    def test_positive_tsb_gets_a_plus_sign(self):
        line = format_form_line(60.0, 55.0, 5.0, [], 8)
        self.assertIn("TSB +5", line)


class TestWeekRow(unittest.TestCase):
    def _week(self, **overrides):
        week = {
            "week_commencing": "2026-06-22",
            "planned_load": 320.0,
            "actual_load": 214.0,
            "in_progress": False,
            "meso_label": "Build 2",
            "meso_source": "plan",
        }
        week.update(overrides)
        return week

    def test_past_governed_week_shows_bar_and_percentage(self):
        row = _week_row(self._week(), scale_max=320.0, today="2026-07-03")
        self.assertIn("320", row)
        self.assertIn("214", row)
        self.assertIn("67%", row)  # round(214/320*100)

    def test_ungoverned_week_shows_dashes_not_zero(self):
        row = _week_row(
            self._week(planned_load=None, meso_source="inferred", meso_label="~Base"),
            scale_max=320.0, today="2026-07-03",
        )
        self.assertIn("-", row.split()[2])  # plan column
        self.assertNotIn("%", row)

    def test_future_week_shows_planned_only(self):
        row = _week_row(
            self._week(week_commencing="2026-07-13", actual_load=0.0),
            scale_max=320.0, today="2026-07-03",
        )
        self.assertIn("(planned)", row)

    def test_in_progress_week_marked_with_asterisk(self):
        row = _week_row(
            self._week(week_commencing="2026-06-29", in_progress=True),
            scale_max=320.0, today="2026-07-03",
        )
        self.assertIn("*", row)


class TestWeeklyTableWidth(unittest.TestCase):
    def test_table_stays_within_the_48_column_budget(self):
        weeks = [
            {
                "week_commencing": "2026-06-22", "planned_load": 320.0,
                "actual_load": 214.0, "in_progress": False,
                "meso_label": "Build 2", "meso_source": "plan",
            },
            {
                "week_commencing": "2026-06-29", "planned_load": 150.0,
                "actual_load": 138.0, "in_progress": True,
                "meso_label": "Build 3", "meso_source": "plan",
            },
            {
                "week_commencing": "2026-07-06", "planned_load": 360.0,
                "actual_load": 0.0, "in_progress": False,
                "meso_label": "Build 3", "meso_source": "plan",
            },
        ]
        lines = table_rows(weeks, today="2026-06-30")
        for line in lines:
            self.assertLessEqual(len(line), 48, msg=repr(line))


if __name__ == "__main__":
    unittest.main()
