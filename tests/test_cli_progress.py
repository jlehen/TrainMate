"""Pure formatting-helper tests for `tm progress` (trainmate/cli/progress.py),
per the `coach/formatting.py` precedent: no DB, no CLI dispatch.
"""
import os
import unittest

os.environ.setdefault("NO_COLOR", "1")  # keep assertions ANSI-free

from trainmate.util import visible_len, wrap_text
from trainmate.cli.progress import (
    sparkline, render_bar, truncate_label, band_header, format_form_line,
    render_progress, format_weekly_table, table_rows, _week_row,
    BAND_LABEL_WIDTH, BAR_WIDTH, TABLE_WIDTH,
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
        self.assertEqual(render_bar(10, None, 0, False), "░" * BAR_WIDTH)

    def test_full_value_fills_the_bar(self):
        self.assertEqual(render_bar(100, None, 100, False), "▓" * BAR_WIDTH)

    def test_half_value_fills_about_half(self):
        self.assertEqual(render_bar(50, None, 100, False).count("▓"), BAR_WIDTH // 2)

    def test_ungoverned_week_draws_no_tick(self):
        self.assertNotIn("│", render_bar(50, None, 100, False))
        self.assertNotIn("│", render_bar(50, 0.0, 100, False))

    def test_tick_marks_the_plan_on_the_shared_scale(self):
        # plan 50/100 -> the tick sits on the first cell beyond half the bar.
        bar = render_bar(25, 50.0, 100, False)
        self.assertEqual(bar.index("│"), BAR_WIDTH // 2)

    def test_overshoot_puts_the_tick_inside_the_fill(self):
        # actual 90 > plan 50: the tick is surrounded by filled cells.
        bar = render_bar(90, 50.0, 100, False)
        tick = bar.index("│")
        self.assertEqual(bar[tick - 1], "▓")
        self.assertEqual(bar[tick + 1], "▓")

    def test_plan_at_full_scale_drops_the_tick_rather_than_overflowing(self):
        bar = render_bar(100, 100.0, 100, False)
        self.assertNotIn("│", bar)
        self.assertEqual(len(bar), BAR_WIDTH)

    def test_future_week_ghost_fills_to_plan(self):
        bar = render_bar(0.0, 50.0, 100, True)
        self.assertEqual(bar.count("▒"), BAR_WIDTH // 2)
        self.assertNotIn("▓", bar)
        self.assertNotIn("│", bar)

    def test_every_variant_is_exactly_bar_width(self):
        for args in [(10, None, 0, False), (100, 100.0, 100, False),
                     (25, 50.0, 100, False), (0.0, 50.0, 100, True),
                     (0.0, None, 100, True)]:
            self.assertEqual(visible_len(render_bar(*args)), BAR_WIDTH, msg=repr(args))


# A real mesocycle name from the shipped plan — the kind the old 8-column meso
# field rendered as `Specifi…` on five consecutive rows.
LONG_LABEL = "Specific Build II - Peak Specific Load & Fatigue Resistance"


class TestTruncateLabel(unittest.TestCase):
    def test_short_label_unchanged(self):
        self.assertEqual(truncate_label("Build 2"), "Build 2")

    def test_long_label_truncated_with_ellipsis(self):
        result = truncate_label(LONG_LABEL)
        self.assertEqual(len(result), BAND_LABEL_WIDTH)
        self.assertTrue(result.endswith("…"))

    def test_label_at_exactly_the_budget_is_not_truncated(self):
        exact = "x" * BAND_LABEL_WIDTH
        self.assertEqual(truncate_label(exact), exact)


class TestBandHeader(unittest.TestCase):
    def test_spans_the_table_width(self):
        self.assertEqual(visible_len(band_header("Build 2")), TABLE_WIDTH)

    def test_writes_the_label_in_full(self):
        # The whole point of the band rule: no more `Specifi…` on every row.
        self.assertIn("Specific Preparation", band_header("Specific Preparation"))

    def test_unlabelled_weeks_band_as_unplanned(self):
        self.assertIn("unplanned", band_header(None))

    def test_overlong_label_still_fits_the_width(self):
        header = band_header(LONG_LABEL)
        self.assertEqual(visible_len(header), TABLE_WIDTH)
        self.assertIn("…", header)

    def test_every_rule_closes_with_a_dash_so_the_right_edge_is_straight(self):
        # A label wide enough to consume the budget must still end in '─', not a
        # bare space — otherwise the band rules ended ragged against each other.
        for label in ["Build 2", LONG_LABEL, "x" * BAND_LABEL_WIDTH, None]:
            header = band_header(label)
            self.assertTrue(header.endswith("─"), msg=repr(header))
            self.assertEqual(visible_len(header), TABLE_WIDTH, msg=repr(header))


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

    def test_future_week_ghost_bars_the_plan_and_omits_actual_columns(self):
        row = _week_row(
            self._week(week_commencing="2026-07-13", actual_load=0.0),
            320.0, "2026-07-03", None,
        )
        self.assertIn("320", row)
        self.assertIn("▒", row)      # ghost-filled, so the series is continuous
        self.assertNotIn("▓", row)   # nothing done yet
        self.assertNotIn("%", row)   # no adherence to report

    def test_future_weeks_draw_so_they_cannot_silently_set_the_scale(self):
        # A big future plan and a small past actual on the same scale: the future row
        # must draw a bar, else it would compress the past bars while showing nothing.
        weeks = [
            {"week_commencing": "2026-06-22", "planned_load": 100.0,
             "actual_load": 100.0, "in_progress": False,
             "meso_label": "Build", "meso_source": "plan"},
            {"week_commencing": "2026-07-13", "planned_load": 400.0,
             "actual_load": 0.0, "in_progress": False,
             "meso_label": "Build", "meso_source": "plan"},
        ]
        rows = table_rows(weeks, "2026-07-03", None)
        future_row = [r for r in rows if "07-13" in r][0]
        self.assertEqual(future_row.count("▒"), BAR_WIDTH)  # 400 == scale max

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

    def test_one_band_rule_per_mesocycle_not_per_week(self):
        # Build 2 once, Build 3 once — the two Build 3 weeks share a rule.
        rows = table_rows(self._weeks(), "2026-06-30", None)
        bands = [r for r in rows if r.startswith("──")]
        self.assertEqual(len(bands), 2)
        self.assertIn("Build 2", bands[0])
        self.assertIn("Build 3", bands[1])

    def test_a_returning_mesocycle_opens_a_fresh_band(self):
        # Non-contiguous labels must not be collapsed — the rule tracks the previous
        # week's label, not the set of labels seen.
        weeks = self._weeks() + [
            {"week_commencing": "2026-07-13", "planned_load": 300.0,
             "actual_load": 0.0, "in_progress": False,
             "meso_label": "Build 2", "meso_source": "plan"},
        ]
        bands = [r for r in table_rows(weeks, "2026-06-30", None) if r.startswith("──")]
        self.assertEqual(len(bands), 3)

    def test_hidden_future_weeks_are_named_in_the_legend(self):
        lines = format_weekly_table(
            self._weeks(), "2026-06-30", None, False, None, hidden_future=12,
        )
        self.assertIn("+12 more (--weeks all)", lines[-1])

    def test_no_legend_note_when_nothing_is_hidden(self):
        lines = format_weekly_table(self._weeks(), "2026-06-30", None, False, None)
        self.assertNotIn("more (--weeks", lines[-1])


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

    def test_lag_note_hidden_by_default(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual")]
        text = "\n".join(render_progress(_payload(days, plan_end=None), 8))
        self.assertNotIn("TSB is CTL(yesterday)", text)

    def test_lag_note_carried_under_explain_when_tsb_shown(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual")]
        text = "\n".join(
            render_progress(_payload(days, plan_end=None), 8, explain=True))
        self.assertIn("TSB is CTL(yesterday)", text)

    def test_no_lag_note_under_explain_when_tsb_absent(self):
        text = "\n".join(
            render_progress(_payload([_day("2026-07-03")], plan_end=None), 8,
                            explain=True))
        self.assertNotIn("TSB is CTL(yesterday)", text)

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

    def _windowed_payload(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual")]
        weeks = [
            {"week_commencing": f"2026-06-{d:02d}", "planned_load": 100.0,
             "actual_load": 90.0, "in_progress": False,
             "meso_label": "Base", "meso_source": "plan"}
            for d in (1, 8, 15, 22, 29)
        ] + [
            {"week_commencing": f"2026-07-{d:02d}", "planned_load": 100.0,
             "actual_load": 0.0, "in_progress": False,
             "meso_label": "Build", "meso_source": "plan"}
            for d in (6, 13, 20, 27)
        ]
        return _payload(days, weeks=weeks, plan_end="2026-08-02")

    def test_future_weeks_are_windowed_like_the_past(self):
        # 4 future weeks, window of 2 -> 2 shown, 2 named as hidden.
        text = "\n".join(render_progress(self._windowed_payload(), 2))
        self.assertIn("w/c 07-06", text)
        self.assertIn("w/c 07-13", text)
        self.assertNotIn("w/c 07-20", text)
        self.assertIn("+2 more (--weeks all)", text)

    def test_weeks_all_shows_everything_and_hides_no_legend_note(self):
        text = "\n".join(render_progress(self._windowed_payload(), "all"))
        self.assertIn("w/c 06-01", text)
        self.assertIn("w/c 07-27", text)
        self.assertNotIn("more (--weeks", text)

    def test_weeks_all_sizes_the_sparkline_to_the_history_shown(self):
        text = "\n".join(render_progress(self._windowed_payload(), "all"))
        self.assertIn("CTL 5w", text)  # 5 past weeks, not the literal 'all'

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
