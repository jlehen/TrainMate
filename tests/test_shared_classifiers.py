"""Rules that more than one surface shows must have exactly one implementation.

Each case here was written twice — once for the terminal and once for the dashboard —
and the copies had already begun to disagree.
"""
import unittest

from trainmate import intensity
from trainmate.baselines import (
    ELEVATED, NORMAL, SUPPRESSED, UNKNOWN, classify_metric, is_anomalous,
)
from trainmate.cli.progress import planned_week_cells, zone_week_cells


class TestClassifyMetric(unittest.TestCase):
    """`data show-metrics` and `status` hand-coded these thresholds separately."""

    BASE = {
        "hrv_baseline_mean": 60.0, "hrv_baseline_std": 5.0,
        "rhr_baseline_mean": 50.0, "rhr_baseline_std": 2.0,
        "sleep_baseline_mean": 80.0, "sleep_baseline_std": 5.0,
    }

    def test_hrv_below_one_standard_deviation_is_suppressed(self):
        self.assertEqual(classify_metric("hrv", 54.0, self.BASE), SUPPRESSED)
        self.assertEqual(classify_metric("hrv", 56.0, self.BASE), NORMAL)

    def test_resting_hr_needs_at_least_three_bpm_over_the_mean(self):
        """The floor stops a very steady athlete's tiny spread flagging ordinary noise:
        with std 2.0 the margin is 3.0, not 2.0."""
        self.assertEqual(classify_metric("rhr", 52.5, self.BASE), NORMAL)
        self.assertEqual(classify_metric("rhr", 53.5, self.BASE), ELEVATED)

    def test_a_wide_spread_raises_the_bar_above_the_floor(self):
        noisy = dict(self.BASE, rhr_baseline_std=6.0)
        self.assertEqual(classify_metric("rhr", 55.0, noisy), NORMAL)
        self.assertEqual(classify_metric("rhr", 57.0, noisy), ELEVATED)

    def test_sleep_is_absolute_not_relative_to_the_athlete(self):
        """A poor night reads as poor even for someone who habitually sleeps badly."""
        poor_sleeper = dict(self.BASE, sleep_baseline_mean=45.0)
        self.assertEqual(classify_metric("sleep", 55, poor_sleeper), SUPPRESSED)
        self.assertEqual(classify_metric("sleep", 65, poor_sleeper), NORMAL)

    def test_missing_readings_and_missing_baselines_are_unknown(self):
        self.assertEqual(classify_metric("hrv", None, self.BASE), UNKNOWN)
        self.assertEqual(classify_metric("hrv", 55.0, {}), UNKNOWN)
        self.assertEqual(classify_metric("rhr", 55.0, None), UNKNOWN)

    def test_a_zero_spread_falls_back_rather_than_flagging_everything(self):
        """With no usable spread the band is +/-1.0, so a reading a hair under the mean
        stays normal instead of every day reading as an anomaly."""
        flat = dict(self.BASE, hrv_baseline_std=0.0)
        self.assertEqual(classify_metric("hrv", 59.5, flat), NORMAL)
        self.assertEqual(classify_metric("hrv", 58.5, flat), SUPPRESSED)

    def test_only_the_two_anomalies_count_as_anomalous(self):
        self.assertTrue(is_anomalous(SUPPRESSED))
        self.assertTrue(is_anomalous(ELEVATED))
        self.assertFalse(is_anomalous(NORMAL))
        self.assertFalse(is_anomalous(UNKNOWN))


def _row(sport, currency, seconds, coverage=1.0, judged=None):
    return intensity.ZoneRow(
        sport=sport, currency=currency, seconds=tuple(seconds),
        coverage=coverage,
        judged_coverage=coverage if judged is None else judged,
    )


def _week(mon="2026-06-01", rows=(), seconds=None, judged=None, planned=()):
    return {
        "week_commencing": mon,
        "zone_rows": list(rows),
        "planned_zone_rows": list(planned),
        "sport_seconds": dict(seconds or {}),
        "judged_sport_seconds": dict(judged if judged is not None else (seconds or {})),
    }


class TestWeekZoneState(unittest.TestCase):
    """The CLI table and /api/zones re-derived this rule from the same design section,
    and already read different fields for "trained"."""

    def test_a_recorded_week_reports_its_seconds(self):
        week = _week(rows=[_row("running", "hr", [60, 120, 0, 0, 0])],
                     seconds={"running": 180})
        state = intensity.week_zone_state(week, "running", "hr")
        self.assertEqual(state.seconds, (60, 120, 0, 0, 0))
        self.assertTrue(state.trained)

    def test_duration_with_nothing_recorded_is_undercounted_not_untrained(self):
        """Saying "not trained" here would turn the design's meaning backwards."""
        week = _week(seconds={"running": 3600}, judged={"running": 3600})
        state = intensity.week_zone_state(week, "running", "hr")
        self.assertIsNone(state.seconds)
        self.assertTrue(state.trained)
        self.assertTrue(state.undercounted)

    def test_a_week_with_no_duration_is_simply_untrained(self):
        state = intensity.week_zone_state(_week(), "running", "hr")
        self.assertIsNone(state.seconds)
        self.assertFalse(state.trained)
        self.assertFalse(state.undercounted)

    def test_unjudgeable_duration_cannot_light_the_week(self):
        """Trained, but the only session was under the judging floor, so the claim that
        something went unrecorded is not supportable."""
        week = _week(seconds={"running": 300}, judged={})
        state = intensity.week_zone_state(week, "running", "hr")
        self.assertTrue(state.trained)
        self.assertFalse(state.undercounted)

    def test_a_future_week_planned_in_another_currency_is_a_mismatch(self):
        week = _week(planned=[_row("running", "power", [0] * 7)])
        state = intensity.week_zone_state(week, "running", "hr", is_future=True)
        self.assertIsNone(state.seconds)
        self.assertTrue(state.currency_mismatch)

    def test_a_future_week_is_never_undercounted(self):
        """Nothing has been recorded yet, so there is no measurement gap to report."""
        week = _week(seconds={"running": 3600}, planned=[])
        state = intensity.week_zone_state(week, "running", "hr", is_future=True)
        self.assertFalse(state.undercounted)


class TestTheCliRendersTheSharedState(unittest.TestCase):
    def test_zone_week_cells_agrees_with_week_zone_state(self):
        week = _week(seconds={"running": 3600}, judged={"running": 3600})
        cells, undercounted = zone_week_cells(week, "running", "hr", 5)
        state = intensity.week_zone_state(week, "running", "hr")
        self.assertEqual(undercounted, state.undercounted)
        self.assertEqual(len(cells), 5)

    def test_planned_week_cells_agrees_with_week_zone_state(self):
        week = _week(planned=[_row("running", "power", [0] * 7)])
        cells, mismatch = planned_week_cells(week, "running", "hr", 5)
        state = intensity.week_zone_state(week, "running", "hr", is_future=True)
        self.assertEqual(mismatch, state.currency_mismatch)
        self.assertEqual(len(cells), 5)


if __name__ == "__main__":
    unittest.main()
