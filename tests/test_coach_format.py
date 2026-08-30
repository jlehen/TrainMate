"""Tests for trainmate/coach/formatting.py — the renderers that build the coach
prompt. Exact strings are the contract here: the string IS what the model reads."""
import unittest

from trainmate.coach import format_completed_activities
from trainmate.coach.formatting import format_metrics_history
from trainmate.util import PMC_TSB_LAG_NOTE
from trainmate.coach.engine.prompt import PromptBuildMixin


def _activity(**overrides):
    act = {
        "date": "2026-06-08", "activity_type": "cycling", "activity_name": "Ride",
        "duration_sec": 3600, "avg_hr": 140, "tss": 80, "rpe": 6,
        "bike_avg_watts": 210,
        "zone1_sec": 600, "zone2_sec": 1200, "zone3_sec": 600,
        "zone4_sec": 0, "zone5_sec": 0,
        "power_zone1_sec": 300, "power_zone2_sec": 900, "power_zone3_sec": 1200,
        "power_zone4_sec": 600, "power_zone5_sec": 300, "power_zone6_sec": 0,
        "power_zone7_sec": 0,
    }
    act.update(overrides)
    return act


class TestFormatCompletedActivities(unittest.TestCase):
    def test_power_zones_rendered_when_present(self):
        out = format_completed_activities([_activity()])
        self.assertIn("HR Zones: Z1=10m, Z2=20m, Z3=10m", out)
        self.assertIn("Power Zones: PZ1=5m, PZ2=15m, PZ3=20m, PZ4=10m, PZ5=5m", out)

    def test_power_zones_omitted_when_null(self):
        run = _activity(activity_type="running", bike_avg_watts=None)
        for i in range(1, 8):
            run[f"power_zone{i}_sec"] = None
        out = format_completed_activities([run])
        self.assertIn("HR Zones:", out)
        self.assertNotIn("Power Zones:", out)


if __name__ == "__main__":
    unittest.main()


# ==============================================================================
# format_metrics_history — None omission + warm-up suppression (§5.1)
# ==============================================================================

class TestFormatMetricsHistory(unittest.TestCase):
    def test_full_row_shows_pmc_and_footnote(self):
        rows = [{"date": "2026-07-02", "rhr": 52, "hrv": 61, "sleep_score": 78,
                 "stress": 31, "ctl": 62.4, "atl": 71.7, "tsb": -8.9}]
        out = format_metrics_history(rows)
        self.assertIn("CTL=62.4", out)
        self.assertIn("ATL=71.7", out)
        self.assertIn("TSB=-8.9", out)
        self.assertIn("ATL:CTL=1.15", out)
        self.assertIn(PMC_TSB_LAG_NOTE, out)

    def test_warmup_rows_suppress_pmc(self):
        rows = [{"date": "2026-01-05", "rhr": 50, "hrv": 60, "sleep_score": 80,
                 "stress": 20, "ctl": 20.0, "atl": 55.0, "tsb": -30.0}]
        out = format_metrics_history(rows, warmup_cutoff="2026-02-12")
        self.assertIn("RHR=50bpm", out)
        # "CTL" also catches a leaked ATL:CTL ratio, which rides inside the same gate.
        self.assertNotIn("CTL", out)
        self.assertNotIn(PMC_TSB_LAG_NOTE, out)

    def test_all_null_row_marked_no_data(self):
        # A pulled-but-empty day must not render a dangling "- 2026-06-01: " line.
        rows = [{"date": "2026-06-01", "rhr": None, "hrv": None, "sleep_score": None,
                 "stress": None, "ctl": None, "atl": None, "tsb": None}]
        out = format_metrics_history(rows)
        self.assertIn("- 2026-06-01: (no data)", out)

    def test_partial_pmc_row_still_gets_lag_footnote(self):
        # The footnote explains the TSB lag; it must appear whenever TSB is shown,
        # even when CTL happens to be NULL.
        rows = [{"date": "2026-07-02", "rhr": 52, "hrv": 61, "sleep_score": 78,
                 "stress": 31, "ctl": None, "atl": 71.7, "tsb": -8.9}]
        out = format_metrics_history(rows)
        self.assertIn("TSB=-8.9", out)
        self.assertIn(PMC_TSB_LAG_NOTE, out)

    def test_no_tsb_no_lag_footnote(self):
        # ...and conversely: a CTL/ATL-only block shows no TSB, so there is no lag
        # on display to explain and the footnote must be omitted.
        rows = [{"date": "2026-07-02", "rhr": 52, "hrv": 61, "sleep_score": 78,
                 "stress": 31, "ctl": 62.4, "atl": 71.7, "tsb": None}]
        out = format_metrics_history(rows)
        self.assertIn("CTL=62.4", out)
        self.assertNotIn(PMC_TSB_LAG_NOTE, out)


# ==============================================================================
# _format_athlete_profile — the prompt's who-you-are block
# ==============================================================================
class TestFormatAthleteProfile(unittest.TestCase):
    def _render(self, **profile):
        return PromptBuildMixin._format_athlete_profile(None, profile)

    def test_gender_rendered_when_set(self):
        self.assertIn("- Gender: female", self._render(name="Sam", gender="female"))

    def test_gender_omitted_when_absent_or_blank(self):
        self.assertNotIn("Gender", self._render(name="Sam"))
        self.assertNotIn("Gender", self._render(name="Sam", gender=""))
