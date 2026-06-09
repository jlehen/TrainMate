import unittest

from trainmate.coach import format_completed_activities


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
