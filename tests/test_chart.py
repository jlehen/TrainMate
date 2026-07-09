"""Pure-helper tests for the timeline chart (trainmate/chart.py).

The rendering itself is exercised through `GET /api/timeline.png` in test_web.py;
here we pin the label-fitting rules that keep mesocycle names from overprinting
each other (DESIGN_progress_timeline.md §6.1/§7.2).
"""
import unittest

from trainmate.chart import (
    _fit_label, _band_label_capacity, _MIN_BAND_LABEL_CHARS,
)

LONG_LABEL = "Specific Build II - Peak Specific Load & Fatigue Resistance"


class _FakeFig:
    dpi = 150

    def get_figwidth(self):
        return 10.0


class _FakeAx:
    figure = _FakeFig()


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


class TestBandLabelCapacity(unittest.TestCase):
    def test_capacity_scales_with_the_span(self):
        ax = _FakeAx()
        full = _band_label_capacity(ax, 1.0)
        half = _band_label_capacity(ax, 0.5)
        self.assertGreater(full, 0)
        self.assertAlmostEqual(half, full // 2, delta=1)

    def test_a_hairline_span_yields_no_room(self):
        self.assertLess(_band_label_capacity(_FakeAx(), 0.001), _MIN_BAND_LABEL_CHARS)

    def test_a_three_week_band_on_a_five_month_axis_cannot_hold_a_full_name(self):
        # The regression this guards: ~21/150 days of axis, a ~58-char name, and the
        # old code centred all 58 characters regardless.
        capacity = _band_label_capacity(_FakeAx(), 21 / 150)
        self.assertLess(capacity, len(LONG_LABEL))
        self.assertLessEqual(len(_fit_label(LONG_LABEL, capacity)), capacity)


if __name__ == "__main__":
    unittest.main()
