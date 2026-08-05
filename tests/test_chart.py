"""Pure-helper tests for the timeline chart (trainmate/chart.py).

Full rendering is exercised through `GET /api/timeline.png` in test_web.py; here we
pin the label-fitting rules that keep mesocycle names from overprinting each other
(DESIGN_progress_timeline.md §6.1/§7.2).
"""
import unittest

import matplotlib
matplotlib.use("Agg")

from trainmate.chart import (  # noqa: E402  (must follow the backend selection)
    _fit_label, _chars_per_axis, _span_dates, _MIN_BAND_LABEL_CHARS,
)

LONG_LABEL = "Specific Build II - Peak Specific Load & Fatigue Resistance"


class TestFitLabel(unittest.TestCase):
    def test_short_label_passes_through(self):
        self.assertEqual(_fit_label("Build 2", 20), "Build 2")

    def test_long_label_is_cut_to_capacity_with_an_ellipsis(self):
        out = _fit_label(LONG_LABEL, 12)
        self.assertEqual(len(out), 12)
        self.assertTrue(out.endswith("…"))

    def test_label_exactly_at_capacity_is_untouched(self):
        self.assertEqual(_fit_label("x" * 12, 12), "x" * 12)

    def test_a_span_too_narrow_to_read_gets_no_label_at_all(self):
        # The tint still draws; the name would be an unreadable stub over its
        # neighbour, so it is dropped rather than truncated to noise.
        self.assertEqual(_fit_label(LONG_LABEL, _MIN_BAND_LABEL_CHARS - 1), "")

    def test_zero_and_negative_capacity_are_safe(self):
        self.assertEqual(_fit_label(LONG_LABEL, 0), "")
        self.assertEqual(_fit_label(LONG_LABEL, -5), "")


class TestSpanDates(unittest.TestCase):
    def test_valid_span_parses(self):
        start, end = _span_dates({"start_date": "2026-05-01", "end_date": "2026-06-01"})
        self.assertEqual(start.month, 5)
        self.assertEqual(end.month, 6)

    def test_unusable_spans_are_reported_as_none(self):
        for bad in [{}, {"start_date": "nope", "end_date": "2026-06-01"},
                    {"start_date": None, "end_date": None}]:
            self.assertIsNone(_span_dates(bad), msg=repr(bad))


class TestCharsPerAxis(unittest.TestCase):
    def _ax(self, figwidth=10.0, dpi=150):
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(figwidth, 4), dpi=dpi)
        fig.canvas.draw()  # geometry must be final before measuring
        self.addCleanup(plt.close, fig)
        return ax

    def test_a_wider_figure_holds_more_characters(self):
        self.assertGreater(_chars_per_axis(self._ax(figwidth=20.0)),
                           _chars_per_axis(self._ax(figwidth=10.0)))

    def test_measures_the_axes_box_not_the_whole_figure(self):
        # The axes are inset by margins, so the budget must be short of the naive
        # full-figure estimate (10in * 150dpi / 8.75px-per-char ≈ 171).
        self.assertLess(_chars_per_axis(self._ax()), 171)

    def test_a_three_week_band_on_a_five_month_axis_cannot_hold_a_full_name(self):
        # The regression this guards: the old code centred all 58 characters of a
        # mesocycle name in a span a few dozen pixels wide.
        capacity = int(21 / 150 * _chars_per_axis(self._ax()))
        self.assertLess(capacity, len(LONG_LABEL))
        self.assertLessEqual(len(_fit_label(LONG_LABEL, capacity)), capacity)


class TestPlanEndMarkerStaysInsideTheWindow(unittest.TestCase):
    """`--chart` may cap the projection short of plan end (§7.1); an axvline drawn
    past the last day drags the x-axis out with it, leaving a wide empty margin."""

    def _payload(self, plan_end, objectives=()):
        days = [
            {"date": f"2026-06-{d:02d}", "load": 10.0, "source": "actual",
             "ctl": 40.0, "atl": 30.0, "tsb": 10.0}
            for d in range(1, 21)
        ]
        return {"today": "2026-06-10", "plan_end": plan_end, "days": days,
                "weeks": [], "objectives": list(objectives), "warnings": [],
                "meso_bands": []}

    def _top_axis_right_edge(self, payload):
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from trainmate import chart

        captured = []
        original = plt.subplots

        def spy(*args, **kwargs):
            fig, axes = original(*args, **kwargs)
            captured.append(axes)
            return fig, axes

        plt.subplots = spy
        try:
            chart.render_timeline_png(payload)
        finally:
            plt.subplots = original
        top = captured[0][0]
        return mdates.num2date(top.get_xlim()[1]).date().isoformat()

    def test_plan_end_past_the_window_does_not_stretch_the_axis(self):
        edge = self._top_axis_right_edge(self._payload(plan_end="2026-09-30"))
        self.assertLess(edge, "2026-07-15")

    def test_plan_end_inside_the_window_is_still_marked(self):
        payload = self._payload(plan_end="2026-06-18")
        edge = self._top_axis_right_edge(payload)
        self.assertLess(edge, "2026-07-15")  # in-window: no stretch either way

    def test_an_objective_on_plan_end_suppresses_the_duplicate_marker(self):
        # Both would be rotated labels on the same x. The objective wins.
        objectives = [{"target_date": "2026-06-18", "title": "Race"}]
        payload = self._payload(plan_end="2026-06-18", objectives=objectives)
        self.assertLess(self._top_axis_right_edge(payload), "2026-07-15")


class TestWeeklyBarsUseTheComparableSlice(unittest.TestCase):
    """§3's comparable-days rule holds on every surface: the in-progress week bars the
    ELAPSED planned figure the CLI table divides by, not the full week's."""

    def _planned_heights(self, weeks):
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from trainmate import chart

        fig, ax = plt.subplots()
        self.addCleanup(plt.close, fig)
        chart._draw_weekly_bars(ax, mdates, weeks)
        return [bar.get_height() for bar in ax.containers[0]]

    def test_in_progress_week_bars_the_elapsed_plan(self):
        weeks = [
            {"week_commencing": "2026-06-22", "planned_load": 320.0,
             "actual_load": 300.0},
            {"week_commencing": "2026-06-29", "planned_load": 340.0,
             "planned_load_elapsed": 150.0, "actual_load": 138.0,
             "in_progress": True},
        ]
        self.assertEqual(self._planned_heights(weeks), [320.0, 150.0])

    def test_ungoverned_week_bars_zero_rather_than_failing(self):
        weeks = [{"week_commencing": "2026-06-22", "planned_load": None,
                  "actual_load": 262.0}]
        self.assertEqual(self._planned_heights(weeks), [0])


if __name__ == "__main__":
    unittest.main()
