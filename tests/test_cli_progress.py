"""Pure formatting-helper tests for `tm progress` (trainmate/cli/progress.py),
per the `coach/formatting.py` precedent: no DB, no CLI dispatch.
"""
import os
import unittest

os.environ.setdefault("NO_COLOR", "1")  # keep assertions ANSI-free

from tests.helpers import _hr, _m, _pwr, _zweek

from trainmate.plan_lineage import delta_baseline
from trainmate.util import visible_len, wrap_text, pmc_cells
from trainmate import garmin, progression
from trainmate.garmin import pmc_display_values
from trainmate.cli.progress import (
    sparkline, render_bar, truncate_label, band_header, format_form_line,
    render_progress, format_weekly_table, table_rows, _week_row,
    fmt_zone_cell, window_sport_stats, zone_currency,
    zone_week_cells, zone_table, zone_section, unknown_sport_preferences,
    _orphan_week_note, render_block_section, _warning_line,
    BAND_LABEL_WIDTH, BAR_WIDTH, TABLE_WIDTH, WEEK_COL_WIDTH,
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

    def test_the_week_that_sets_the_scale_still_gets_its_tick(self):
        # plan == scale_max maps to p == BAR_WIDTH; clamped into the bar rather than
        # dropped, because that is the peak week whose tick matters most.
        bar = render_bar(80, 100.0, 100, False)
        self.assertIn("│", bar)
        self.assertEqual(bar.index("│"), BAR_WIDTH - 1)
        self.assertEqual(len(bar), BAR_WIDTH)

    def test_a_fully_met_peak_week_reads_as_on_plan(self):
        bar = render_bar(100, 100.0, 100, False)
        self.assertEqual(len(bar), BAR_WIDTH)
        self.assertEqual(bar, "▓" * (BAR_WIDTH - 1) + "│")

    def test_future_week_ghost_fills_to_plan(self):
        bar = render_bar(0.0, 50.0, 100, True)
        self.assertEqual(bar.count("▒"), BAR_WIDTH // 2)
        self.assertNotIn("▓", bar)
        self.assertNotIn("│", bar)

    def test_every_variant_is_exactly_bar_width(self):
        for args in [(10, None, 0, False), (100, 100.0, 100, False),
                     (25, 50.0, 100, False), (0.0, 50.0, 100, True),
                     (0.0, None, 100, True), (0.0, 100.0, 100, False),
                     (100, 1.0, 100, False), (100.0, 100.0, 100, True)]:
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
        row = _week_row(self._week(), 320.0, "2026-07-03")
        self.assertIn("320", row)
        self.assertIn("214", row)
        self.assertIn("67%", row)

    def test_week_with_no_plan_shows_dash_and_no_percentage(self):
        row = _week_row(
            self._week(planned_load=None, meso_source="inferred", meso_label="~Base"),
            320.0, "2026-07-03",
        )
        self.assertIn("—", row)
        self.assertNotIn("%", row)

    def test_future_week_ghost_bars_the_plan_and_omits_actual_columns(self):
        row = _week_row(
            self._week(week_commencing="2026-07-13", actual_load=0.0),
            320.0, "2026-07-03",
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
        rows = table_rows(weeks, "2026-07-03")
        future_row = [r for r in rows if "07-13" in r][0]
        self.assertEqual(future_row.count("▒"), BAR_WIDTH)  # 400 == scale max

    def test_in_progress_week_uses_elapsed_planned(self):
        row = _week_row(
            self._week(week_commencing="2026-06-29", in_progress=True,
                       planned_load=340.0, planned_load_elapsed=150.0,
                       actual_load=138.0),
            360.0, "2026-07-03",
        )
        self.assertIn("*", row)
        self.assertIn("150", row)          # elapsed, not the full 340
        self.assertIn("92%", row)          # round(138/150*100)

    def test_part_week_plan_gets_no_adherence_figure(self):
        # The plan covers three of the week's seven trained days: dividing them was the
        # 477% this rule exists to stop (§3). The plan total still shows.
        row = _week_row(
            self._week(planned_load=72.0, actual_load=344.0, partial_plan=True),
            400.0, "2026-07-03",
        )
        self.assertIn("72", row)
        self.assertIn("344", row)
        self.assertNotIn("%", row)


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
        for line in table_rows(self._weeks(), "2026-06-30"):
            self.assertLessEqual(visible_len(line), 48, msg=repr(line))

    def test_zero_max_scale_does_not_divide(self):
        weeks = [{"week_commencing": "2026-06-22", "planned_load": 0.0,
                  "actual_load": 0.0, "in_progress": False,
                  "meso_label": None, "meso_source": None}]
        rows = table_rows(weeks, "2026-07-03")  # must not raise
        self.assertTrue(rows)

    def test_one_band_rule_per_mesocycle_not_per_week(self):
        # Build 2 once, Build 3 once — the two Build 3 weeks share a rule.
        rows = table_rows(self._weeks(), "2026-06-30")
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
        bands = [r for r in table_rows(weeks, "2026-06-30") if r.startswith("──")]
        self.assertEqual(len(bands), 3)

    def test_hidden_weeks_are_named_in_the_legend(self):
        lines = format_weekly_table(
            self._weeks(), "2026-06-30", False, None, hidden_weeks=12,
        )
        self.assertIn("+12 more (--weeks all)", lines[-1])

    def test_no_legend_note_when_nothing_is_hidden(self):
        lines = format_weekly_table(self._weeks(), "2026-06-30", False, None)
        self.assertNotIn("more (--weeks", lines[-1])


def _day(date, ctl=None, atl=None, tsb=None, source="actual", load=0.0):
    return {"date": date, "load": load, "source": source,
            "ctl": ctl, "atl": atl, "tsb": tsb}


def _warn(code, text, command=None):
    w = {"code": code, "text": text}
    if command:
        w["command"] = command
    return w


def _payload(days, weeks=None, plan_end=None, objectives=None, warnings=None,
             meso_bands=None, today="2026-07-03", plan_gap=None):
    return {
        "today": today, "plan_start": None, "plan_end": plan_end, "days": days,
        "weeks": weeks or [], "objectives": objectives or [],
        "plan_gap": plan_gap, "warnings": warnings or [],
        "meso_bands": meso_bands or [],
    }


class TestRenderProgress(unittest.TestCase):
    MARATHON = {"id": 1, "title": "Marathon", "target_date": "2026-09-30",
                "priority": 1, "status": "active"}
    GAP = {"objective": MARATHON, "weeks_before": 9, "plan_end": "2026-07-31"}

    def test_plan_gap_banner_when_objective_not_reached(self):
        days = [_day("2026-07-03", 55, 61, -6, "actual"),
                _day("2026-07-31", 61, 60, 1, "planned")]
        lines = render_progress(
            _payload(days, plan_end="2026-07-31", objectives=[self.MARATHON],
                     plan_gap=self.GAP), 8)
        text = "\n".join(lines)
        self.assertIn("FORM today (actual)", text)
        self.assertIn("plan generated through", text)
        self.assertIn("workout generate -g", text)

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
        gap = {"objective": objs[1], "weeks_before": 8, "plan_end": "2026-09-30"}
        text = "\n".join(render_progress(
            _payload(days, plan_end="2026-09-30", objectives=objs, plan_gap=gap), 8))
        self.assertIn("projected CTL 68", text)
        self.assertIn("plan generated through", text)

    def test_a_completed_objective_behind_today_is_not_projected(self):
        # The payload carries completed objectives (the chart flags them), but a race
        # already run has no projection to show — and printing one used to displace the
        # plan-end figure, the command's headline number (§7.1/§11).
        days = [_day("2026-05-10", 48, 40, 8, "actual"),
                _day("2026-07-03", 55, 61, -6, "actual"),
                _day("2026-07-31", 61, 60, 1, "planned")]
        old = {"id": 3, "title": "Old 10k", "target_date": "2026-05-10",
               "priority": 2, "status": "completed"}
        text = "\n".join(render_progress(
            _payload(days, plan_end="2026-07-31", objectives=[old]), 8))
        self.assertNotIn("Old 10k", text)
        self.assertNotIn("projected CTL", text)
        self.assertIn("plan end 07-31", text)

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

    def test_plan_gap_is_drawn_once_from_the_structured_field(self):
        # It is not a `warnings` string, so no renderer has to recognise it by prose
        # and then recompute it (§6.0).
        days = [_day("2026-07-03", 55, 61, -6, "actual"),
                _day("2026-07-31", 61, 60, 1, "planned")]
        warnings = [_warn("zero_load_workouts",
                          "2 planned workouts lack TSS/RPE — count as 0")]
        weeks = [{"week_commencing": "2026-06-29", "planned_load": 100.0,
                  "actual_load": 90.0, "in_progress": True,
                  "planned_load_elapsed": 80.0,
                  "meso_label": "Build", "meso_source": "plan"}]
        lines = render_progress(
            _payload(days, weeks=weeks, plan_end="2026-07-31",
                     objectives=[self.MARATHON], warnings=warnings,
                     plan_gap=self.GAP), 8)
        self.assertEqual(sum(1 for ln in lines if "lack TSS/RPE" in ln), 1)
        self.assertEqual(sum(1 for ln in lines if "plan generated through" in ln), 1)

    def test_warning_command_is_styled_from_the_payload_field(self):
        w = _warn("no_history", "no activity history yet — run `data pull` first",
                  command="data pull")
        line = _warning_line(w)
        self.assertIn("data pull", line)
        self.assertNotIn("`data pull`", line)  # replaced by the styled command

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
        # 5 past + 4 future, window of 2 -> 2 + 2 shown; 3 past + 2 future hidden.
        text = "\n".join(render_progress(self._windowed_payload(), 2))
        self.assertIn("w/c 07-06", text)
        self.assertIn("w/c 07-13", text)
        self.assertNotIn("w/c 07-20", text)
        self.assertNotIn("w/c 06-01", text)
        self.assertIn("+5 more (--weeks all)", text)

    def test_hidden_past_weeks_are_counted_too_not_just_future(self):
        # 5 past + 4 future, window of 4 -> 1 past hidden, 0 future hidden. A legend
        # that counted only the future side would print no note at all here.
        text = "\n".join(render_progress(self._windowed_payload(), 4))
        self.assertIn("+1 more (--weeks all)", text)

    def test_weeks_all_shows_everything_and_hides_no_legend_note(self):
        text = "\n".join(render_progress(self._windowed_payload(), "all"))
        self.assertIn("w/c 06-01", text)
        self.assertIn("w/c 07-27", text)
        self.assertNotIn("more (--weeks", text)

    def test_sparkline_label_counts_cells_drawn_not_the_window_asked_for(self):
        # Young DB: 5 weeks of history under a --weeks 8 request must not say '8w'.
        text = "\n".join(render_progress(self._windowed_payload(), 8))
        self.assertIn("CTL 5w", text)
        self.assertNotIn("CTL 8w", text)

    def test_sparkline_label_matches_the_window_when_history_is_long_enough(self):
        text = "\n".join(render_progress(self._windowed_payload(), 3))
        self.assertIn("CTL 3w", text)

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
                         objectives=[self.MARATHON], plan_gap=self.GAP,
                         warnings=[_warn(
                             "zero_load_workouts",
                             "2 planned workouts lack TSS/RPE — count as 0")]), 8)
            for line in lines:
                wrapped = wrap_text(line) if visible_len(line) > 48 else line
                for sub in wrapped.split("\n"):
                    self.assertLessEqual(visible_len(sub), 48, msg=repr(sub))
        finally:
            del os.environ["TRAINMATE_WRAP_WIDTH"]


class TestStatusConsistencyContract(unittest.TestCase):
    """§7.1's contract with `tm status`, pinned rather than argued.

    `tm status` renders the latest stored metrics row through
    `garmin.pmc_display_values` + `util.pmc_cells`; `tm progress` renders the §4 fold of
    the same series through `format_form_line`. Both sides are built here from one set
    of loads so the two really are the same day's numbers.
    """
    TODAY = "2026-07-03"
    LOADS = {f"2026-06-{d:02d}": 40.0 + d for d in range(1, 31)}

    def _stored_rows(self, today_load):
        """The rows `recompute_derived()` would write — the series `tm status` reads."""
        loads = dict(self.LOADS, **{self.TODAY: today_load})
        pmc = garmin.compute_pmc(loads, "2026-06-01", self.TODAY, 42, 7)
        return [{"date": d, "ctl": c, "atl": a, "tsb": t}
                for d, (c, a, t) in sorted(pmc.items())]

    def _day_points(self, today_load, today_source):
        points = [{"date": d, "load": self.LOADS[d], "source": "actual"}
                  for d in sorted(self.LOADS)]
        return points + [{"date": self.TODAY, "load": today_load,
                          "source": today_source}]

    def _both(self, stored_today_load, folded_today_load, today_source):
        rows = self._stored_rows(stored_today_load)
        series = progression.fitness_series(
            self._day_points(folded_today_load, today_source), rows,
            self.TODAY, 42, 7, None,
        )
        today = series[-1]
        form = format_form_line(
            today["source"], today["ctl"], today["atl"], today["tsb"]
        )
        return form, today, pmc_cells(*pmc_display_values(rows[-1], None))

    def test_today_synced_reads_verbatim_the_same_as_status(self):
        # Full-precision storage + the same recurrence: the fold reproduces the stored
        # row bit-exactly, so the two commands print one set of numbers (§4/§7.1).
        form, _, (ctl_s, atl_s, tsb_s) = self._both(90.0, 90.0, "actual")
        self.assertIn("(actual)", form)
        self.assertIn(f"CTL {ctl_s} ATL {atl_s} TSB {tsb_s}", form)

    def test_today_pending_keeps_tsb_and_diverges_on_ctl_atl(self):
        # The morning pull stored a load-0 row; the fold counts tonight's planned
        # session instead. TSB is day-entering, so it is identical either way; CTL/ATL
        # deliberately differ and the `(planned)` tag is what says so (§7.1).
        form, today, (ctl_s, atl_s, tsb_s) = self._both(0.0, 100.0, "planned")
        self.assertIn("(planned)", form)
        self.assertIn(f"TSB {tsb_s}", form)
        self.assertNotIn(f"CTL {ctl_s} ", form)
        self.assertNotIn(f"ATL {atl_s} ", form)
        self.assertGreater(today["ctl"], float(ctl_s))
        self.assertGreater(today["atl"], float(atl_s))


# ---------------------------------------------------------------- zone tables
# DESIGN_intensity_distribution.md §9.6.


class TestZoneCell(unittest.TestCase):
    def test_under_an_hour_is_three_characters(self):
        self.assertEqual(fmt_zone_cell(_m(55)), "55m")

    def test_hours_render_four_characters(self):
        self.assertEqual(fmt_zone_cell(_m(300)), "5h00")

    def test_ten_hours_and_up_drop_the_minutes_to_stay_inside_the_budget(self):
        self.assertEqual(fmt_zone_cell(_m(12 * 60 + 30)), "12h")
        self.assertLessEqual(len(fmt_zone_cell(_m(99 * 60))), 4)

    def test_no_seconds_is_a_dash_not_a_zero(self):
        self.assertEqual(fmt_zone_cell(0), "—")


class TestZoneWeekCells(unittest.TestCase):
    """`—`, `!` and a plain row are three different facts (§9.6)."""

    def test_sport_not_trained_renders_dashes_and_takes_no_marker(self):
        week = _zweek("2026-06-29", rows=[], seconds={"cycling": _m(120)})
        cells, undercounted = zone_week_cells(week, "running", "hr", 5)
        self.assertEqual(cells, ["—"] * 5)
        self.assertFalse(undercounted)

    def test_trained_but_unrecorded_renders_dashes_and_takes_the_marker(self):
        week = _zweek("2026-06-29", rows=[], seconds={"running": _m(120)})
        cells, undercounted = zone_week_cells(week, "running", "hr", 5)
        self.assertEqual(cells, ["—"] * 5)
        self.assertTrue(undercounted)

    def test_a_too_short_unrecorded_session_does_not_light_the_week(self):
        # §11's floor applies to this branch too: 5 minutes of unrecorded training is
        # not evidence that the week's zone minutes are undercounted.
        week = _zweek("2026-06-29", rows=[], seconds={"running": _m(5)}, judged={})
        cells, undercounted = zone_week_cells(week, "running", "hr", 5)
        self.assertEqual(cells, ["—"] * 5)
        self.assertFalse(undercounted)

    def test_no_data_in_the_chosen_currency_does_not_fall_back_to_the_other(self):
        week = _zweek(
            "2026-06-29", rows=[_hr("cycling", [10, 60, 5, 2, 1])],
            seconds={"cycling": _m(78)},
        )
        cells, _ = zone_week_cells(week, "cycling", "power", 7)
        self.assertEqual(cells, ["—"] * 7)

    def test_marker_fires_at_the_display_bar_not_the_load_bar(self):
        # 0.55 clears `hr_zone_coverage_min` (0.5) and still misses nearly half the
        # recorded time, so the row must admit it.
        week = _zweek(
            "2026-06-29", rows=[_hr("running", [10, 60, 5, 2, 1], coverage=0.55)],
            seconds={"running": _m(140)},
        )
        _, undercounted = zone_week_cells(week, "running", "hr", 5)
        self.assertTrue(undercounted)

class TestZoneTableWidth(unittest.TestCase):
    def test_seven_zone_power_table_fits_48_columns_with_a_ten_hour_z2(self):
        weeks = [_zweek(
            "2026-06-29",
            rows=[_pwr("cycling", [90, 11 * 60, 70, 35, 15, 5, 3])],
            seconds={"cycling": _m(878)},
        )]
        lines, _ = zone_table(weeks, "cycling", "power", _m(878), _m(878), 0.9)
        for line in lines:
            self.assertLessEqual(visible_len(line), TABLE_WIDTH, msg=repr(line))
        row = next(l for l in lines if l.startswith("w/c"))
        self.assertEqual(visible_len(row), TABLE_WIDTH)
        self.assertIn("11h", row)

    def test_five_zone_hr_table_is_38_columns(self):
        weeks = [_zweek(
            "2026-06-29", rows=[_hr("running", [50, 300, 35, 15, 5])],
            seconds={"running": _m(405)},
        )]
        lines, _ = zone_table(weeks, "running", "hr", _m(405), _m(405), 0.94)
        row = next(l for l in lines if l.startswith("w/c"))
        self.assertEqual(visible_len(row), 38)

    def test_in_progress_and_undercounted_both_fit_the_week_column(self):
        weeks = [_zweek(
            "2026-07-06", rows=[_hr("running", [22, 82, 38, 7, 3], coverage=0.4)],
            seconds={"running": _m(400)}, in_progress=True,
        )]
        lines, markers = zone_table(weeks, "running", "hr", _m(400), _m(400), 0.4)
        row = next(l for l in lines if l.startswith("w/c"))
        self.assertTrue(row.startswith("w/c 07-06*!"))
        self.assertEqual(len("w/c 07-06*!"), WEEK_COL_WIDTH)
        self.assertIn("!", markers)


class TestZoneSection(unittest.TestCase):
    def _weeks(self):
        return [
            _zweek("2026-06-22",
                   rows=[_hr("running", [55, 258, 62, 17, 7]),
                         _pwr("cycling", [45, 140, 30, 15, 7, 3, 1])],
                   seconds={"running": _m(399), "cycling": _m(241)}),
            _zweek("2026-06-29",
                   rows=[_pwr("cycling", [90, 570, 70, 35, 15, 5, 3])],
                   seconds={"cycling": _m(788)}, label="Base 2"),
        ]

    def test_one_table_per_sport_so_a_swap_reads_as_a_swap(self):
        lines = zone_section(self._weeks(), ["running", "cycling"])
        text = "\n".join(lines)
        self.assertIn("ZONES running", text)
        self.assertIn("ZONES cycling", text)
        # Running absent in 06-29 while cycling Z2 climbs: one table alone would have
        # called that a collapsed aerobic base.
        self.assertIn("—", text)

    def test_header_carries_the_sport_share_of_the_window(self):
        lines = zone_section(self._weeks(), ["running"])
        header = next(l for l in lines if l.startswith("ZONES"))
        self.assertIn(" of ", header)
        self.assertIn("[HR", header)

    def test_hidden_weeks_are_named_in_the_footer(self):
        lines = zone_section(self._weeks(), ["running"], hidden_weeks=3)
        self.assertIn("+3 more weeks (--weeks all)", "\n".join(lines))

    def test_a_named_sport_with_no_rows_lists_the_sports_that_have_them(self):
        lines = zone_section(self._weeks(), [], explicit=["swimming"])
        text = "\n".join(lines)
        self.assertIn("No zone data for swimming", text)
        self.assertIn("cycling", text)
        self.assertIn("running", text)

    def test_every_line_stays_inside_the_column_budget(self):
        for line in zone_section(self._weeks(), ["running", "cycling"], hidden_weeks=3):
            self.assertLessEqual(visible_len(line), TABLE_WIDTH, msg=repr(line))


class TestZoneTableFutureHalf(unittest.TestCase):
    """§9.8: ghost rows under today, exactly like the load table's ghost bars — which
    closes §9.6's one asymmetry."""

    TODAY = "2026-06-24"

    def _weeks(self):
        past = _zweek("2026-06-22",
                      rows=[_hr("running", [55, 258, 62, 17, 7])],
                      seconds={"running": _m(399)}, in_progress=True)
        future = _zweek("2026-06-29", seconds={})
        future["planned_zone_rows"] = [_hr("running", [20, 240, 30, 20, 5], 1.0)]
        return [past, future]

    def test_future_weeks_render_the_plan_and_are_marked(self):
        lines, markers = zone_table(
            self._weeks(), "running", "hr", _m(399), _m(399), 0.94, today=self.TODAY
        )
        rows = [l for l in lines if l.startswith("w/c")]
        self.assertTrue(rows[0].startswith("w/c 06-22*"))   # measured, in progress
        self.assertTrue(rows[1].startswith("w/c 06-29+"))   # prescribed
        self.assertIn("4h00", rows[1])                      # 240 min of planned Z2
        self.assertIn("+", markers)

    def test_a_future_week_with_no_plan_renders_dashes_not_the_past(self):
        weeks = self._weeks()
        weeks[1]["planned_zone_rows"] = []
        lines, _ = zone_table(
            weeks, "running", "hr", _m(399), _m(399), 0.94, today=self.TODAY
        )
        future = [l for l in lines if l.startswith("w/c 06-29")][0]
        self.assertNotIn("+", future)
        self.assertEqual(future.count("—"), 5)

    def test_a_plan_in_the_other_currency_is_declared_never_converted(self):
        # Power Z6/Z7 have no HR equivalent, so collapsing seven onto five would be
        # banding by the back door and §5 forbids it.
        weeks = self._weeks()
        weeks[1]["planned_zone_rows"] = [_pwr("running", [20, 240, 30, 20, 5, 2, 1], 1.0)]
        lines, markers = zone_table(
            weeks, "running", "hr", _m(399), _m(399), 0.94, today=self.TODAY
        )
        future = [l for l in lines if l.startswith("w/c 06-29")][0]
        self.assertEqual(future.count("—"), 5)
        self.assertIn("~mismatch", markers)

    def test_the_mismatch_is_explained_in_the_section_footer(self):
        weeks = self._weeks()
        weeks[1]["planned_zone_rows"] = [_pwr("running", [20, 240, 30, 20, 5, 2, 1], 1.0)]
        text = " ".join(
            l.strip() for l in
            zone_section(weeks, ["running"], today=self.TODAY, stats_weeks=weeks[:1])
        )
        self.assertIn("written in the other currency", text)

    def test_the_currency_choice_still_reads_measured_coverage_only(self):
        # A future week's planned rows must not vote on which column the table is drawn
        # in — coverage is a property of recordings.
        weeks = self._weeks()
        stats = window_sport_stats(weeks[:1])
        self.assertEqual(zone_currency(stats, "running"), "hr")

class TestUnknownSportPreferences(unittest.TestCase):
    def test_a_canonical_sport_warns_about_nothing(self):
        self.assertEqual(unknown_sport_preferences(["cycling", "running"]), [])

    def test_a_free_text_entry_warns_and_suggests(self):
        [warning] = unknown_sport_preferences(["Road cycling"])
        self.assertIn("'Road cycling' is not a known sport", warning)
        self.assertIn("cycling", warning)

    def test_a_genuinely_new_sport_warns_without_a_suggestion(self):
        [warning] = unknown_sport_preferences(["swimming"])
        self.assertIn("not a known sport", warning)
        self.assertNotIn("Did you mean", warning)

    def test_an_alias_is_not_a_warning(self):
        self.assertEqual(unknown_sport_preferences(["road_biking"]), [])


class TestOrphanWeekNote(unittest.TestCase):
    BLOCKS = [{"start_date": "2026-05-25", "end_date": "2026-06-14"},
              {"start_date": "2026-06-29", "end_date": "2026-07-26"}]

    def test_weeks_belonging_to_no_block_are_named(self):
        weeks = [_zweek(d) for d in
                 ("2026-06-01", "2026-06-15", "2026-06-22", "2026-06-29")]
        lines = _orphan_week_note(weeks, self.BLOCKS)
        note = " ".join(l.strip() for l in lines)
        self.assertIn("2 weeks", note)
        self.assertIn("06-15", note)
        self.assertIn("06-22", note)
        self.assertIn("partial week is excluded", note)
        self.assertIn("without --blocks", note)
        for line in lines:
            self.assertLessEqual(visible_len(line), TABLE_WIDTH, msg=repr(line))

    def test_a_long_orphan_list_is_capped(self):
        weeks = [_zweek(f"2026-06-{d:02d}") for d in (15, 22)] + [
            _zweek("2026-08-03"), _zweek("2026-08-10")
        ]
        note = "\n".join(_orphan_week_note(weeks, self.BLOCKS))
        self.assertIn("+2 more", note)

    def test_full_coverage_says_nothing(self):
        self.assertEqual(_orphan_week_note([_zweek("2026-06-29")], self.BLOCKS), [])


class _StubDb:
    """The five accessors `render_block_section` reads, and nothing else — it takes a
    `dbh` precisely so the block walk stays testable without a database.

    `preceding` is `(macro, blocks)` for the previous *goal's* plan, the only other
    lineage the walk pulls in; there is deliberately no way to hand it a superseded
    version of the governing plan (DESIGN_plan_rollback.md §6.1)."""

    def __init__(self, mesos, activities, preceding=None, governing_id=1):
        self._mesos, self._activities = mesos, activities
        self._governing_id = governing_id
        self._preceding_macro, self._preceding_mesos = preceding or (None, [])

    def get_governing_macrocycle(self):
        return {"id": self._governing_id, "objective_id": 1}

    def get_preceding_macrocycle(self, objective_id):
        return self._preceding_macro

    def get_mesocycles_for_macrocycle(self, macrocycle_id):
        if self._preceding_macro and macrocycle_id == self._preceding_macro["id"]:
            return self._preceding_mesos
        return self._mesos

    def get_benchmark_results(self):
        return []

    def get_completed_activities(self, start_date=None, end_date=None):
        return [a for a in self._activities if start_date <= a["date"] <= end_date]


def _run_act(date, minutes, z2_minutes):
    row = {"date": date, "activity_type": "running", "duration_sec": minutes * 60,
           "rpe": None, "tss": None}
    for i in range(1, 6):
        row[f"zone{i}_sec"] = z2_minutes * 60 if i == 2 else 0
    for i in range(1, 8):
        row[f"power_zone{i}_sec"] = 0
    return row


class TestBlockSection(unittest.TestCase):
    """`--blocks` reproduces the very loss §9.6 exists to prevent, by more than one
    route, so it must name both."""

    TODAY = "2026-07-09"
    MESOS = [
        {"id": 1, "macrocycle_id": 1, "name": "Base 1", "focus": "aerobic volume",
         "start_date": "2026-05-25", "end_date": "2026-06-14"},
        {"id": 2, "macrocycle_id": 1, "name": "Base 2",
         "focus": "aerobic consolidation",
         "start_date": "2026-06-29", "end_date": "2026-07-26"},
    ]

    def _payload_weeks(self):
        mondays = ["2026-06-01", "2026-06-08", "2026-06-15", "2026-06-22",
                   "2026-06-29", "2026-07-06"]
        return [_zweek(m, seconds={"running": _m(300)},
                       rows=[_hr("running", [10, 250, 30, 8, 2])]) for m in mondays]

    def _section(self):
        acts = [_run_act(d, 300, 250) for d in
                ("2026-06-02", "2026-06-09", "2026-06-16", "2026-06-23",
                 "2026-06-30", "2026-07-07")]
        return render_block_section(
            _StubDb(self.MESOS, acts), {"weeks": self._payload_weeks()},
            8, self.TODAY, [], ["running"],
        )

    def test_reports_each_block_overlapping_the_window(self):
        text = "\n".join(self._section())
        self.assertIn("ZONES BY BLOCK — running", text)
        self.assertIn("Base 1", text)
        self.assertIn("Base 2", text)
        self.assertIn("aerobic volume", text)  # the stated focus, to be graded against

    def test_names_the_weeks_that_belong_to_no_block(self):
        text = " ".join(l.strip() for l in self._section())
        self.assertIn("belong to no block", text)
        self.assertIn("06-15", text)
        self.assertIn("06-22", text)

    def test_names_the_excluded_partial_tails(self):
        text = " ".join(l.strip() for l in self._section())
        self.assertIn("final partial week is excluded", text)

    def test_the_caveats_are_emitted_once_not_once_per_block(self):
        text = "\n".join(self._section())
        self.assertEqual(text.count("interval work with rest"), 1)

    def test_every_line_stays_inside_the_column_budget(self):
        for line in self._section():
            self.assertLessEqual(visible_len(line), TABLE_WIDTH, msg=repr(line))


class TestDeltaStopsAtThePlanBoundary(unittest.TestCase):
    """A long window reaches back into the previous goal's plan. Those blocks are worth
    reporting; the change *against* them is not — it spans a taper, a race and an
    off-season (DESIGN_plan_rollback.md §6.1)."""

    TODAY = "2026-07-09"
    # The governing plan (macro 2), and last season's (macro 1) before it.
    THIS_SEASON = [
        {"id": 3, "macrocycle_id": 2, "name": "Base 1", "focus": "aerobic volume",
         "start_date": "2026-05-25", "end_date": "2026-06-21"},
        {"id": 4, "macrocycle_id": 2, "name": "Base 2",
         "focus": "aerobic consolidation",
         "start_date": "2026-06-22", "end_date": "2026-07-19"},
    ]
    LAST_SEASON = [
        {"id": 1, "macrocycle_id": 1, "name": "Spring Peak", "focus": "race sharpening",
         "start_date": "2026-04-06", "end_date": "2026-05-03"},
    ]

    def _section(self):
        mondays = ["2026-04-06", "2026-04-13", "2026-04-20", "2026-04-27",
                   "2026-05-25", "2026-06-01", "2026-06-08", "2026-06-15",
                   "2026-06-22", "2026-06-29", "2026-07-06"]
        weeks = [_zweek(m, seconds={"running": _m(300)},
                        rows=[_hr("running", [10, 250, 30, 8, 2])]) for m in mondays]
        acts = [_run_act(f"{m[:8]}{int(m[8:]) + 1:02d}", 300, 250) for m in mondays]
        db = _StubDb(self.THIS_SEASON, acts, governing_id=2,
                     preceding=({"id": 1, "objective_id": 1}, self.LAST_SEASON))
        return "\n".join(render_block_section(
            db, {"weeks": weeks}, "all", self.TODAY, [], ["running"],
        ))

    def test_last_seasons_block_is_still_reported(self):
        self.assertIn("Spring Peak", self._section())

    def test_no_delta_is_drawn_across_the_boundary(self):
        self.assertNotIn("Change vs Spring Peak", self._section())

    def test_the_within_plan_delta_survives(self):
        self.assertIn("Change vs Base 1", self._section())


class TestDeltaBaseline(unittest.TestCase):
    """`delta_baseline` on its own — the rule both the CLI section and the strategy
    prompt read it from."""

    BLOCKS = [
        {"name": "Spring Peak", "macrocycle_id": 1},
        {"name": "Base 1", "macrocycle_id": 2},
        {"name": "Base 2", "macrocycle_id": 2},
    ]

    def test_the_first_block_has_no_baseline(self):
        self.assertIsNone(delta_baseline(self.BLOCKS, 0))

    def test_a_plan_boundary_has_no_baseline(self):
        self.assertIsNone(delta_baseline(self.BLOCKS, 1))

    def test_within_a_plan_the_baseline_is_the_block_before(self):
        self.assertEqual(delta_baseline(self.BLOCKS, 2)["name"], "Base 1")

    def test_blocks_predating_the_column_still_compare(self):
        """Legacy rows carry no `macrocycle_id`; None == None, so they keep their delta
        rather than silently losing it."""
        legacy = [{"name": "A"}, {"name": "B"}]
        self.assertEqual(delta_baseline(legacy, 1)["name"], "A")


class TestLoadSparseWeek(unittest.TestCase):
    """§11: the strap died, no RPE was entered, and the week reads as an adherence
    miss the coach will then adapt the plan around."""

    def _week(self, sparse):
        return {"week_commencing": "2026-06-29", "planned_load": 320.0,
                "actual_load": 262.0, "in_progress": False, "meso_label": "Build 2",
                "meso_source": "plan", "load_sparse": sparse}

    def test_marked_in_the_week_column(self):
        row = _week_row(self._week(True), 400.0, "2026-07-03")
        self.assertTrue(row.startswith("w/c 06-29?"))

    def test_legend_explains_it_only_when_it_fires(self):
        marked = "\n".join(
            format_weekly_table([self._week(True)], "2026-07-03", False, None)
        )
        self.assertIn("? load undercounted", marked)
        clean = "\n".join(
            format_weekly_table([self._week(False)], "2026-07-03", False, None)
        )
        self.assertNotIn("? load undercounted", clean)


if __name__ == "__main__":
    unittest.main()
