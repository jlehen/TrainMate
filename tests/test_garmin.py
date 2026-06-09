import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables
from trainmate.db import Database
import trainmate.db
import trainmate.garmin as garmin

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_garmin.db")
test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
garmin.db = test_db


def _d(offset: int) -> str:
    """A YYYY-MM-DD string `offset` days from today (local)."""
    return (date.today() + timedelta(days=offset)).isoformat()


def tearDownModule():
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except OSError:
            pass


class TestGarminTransforms(unittest.TestCase):
    def test_calculate_tss_prefers_power(self):
        act = {"duration": 3600, "avgPower": 200, "averageHR": 150}
        # IF = 200/200 = 1.0 -> 100 * 1h * 1.0^2 = 100
        self.assertEqual(garmin.calculate_tss(act, ftp=200, lthr=160), 100.0)

    def test_calculate_tss_falls_back_to_hr(self):
        act = {"duration": 3600, "averageHR": 160}
        # IF = 160/160 = 1.0 -> 100
        self.assertEqual(garmin.calculate_tss(act, ftp=None, lthr=160), 100.0)

    def test_calculate_tss_none_when_uncomputable(self):
        self.assertIsNone(garmin.calculate_tss({"duration": 0}, 200, 160))
        self.assertIsNone(garmin.calculate_tss({"duration": 3600}, None, None))

    def test_estimate_rpe_tss_uses_profile_lthr(self):
        with patch.dict(garmin.config.data, {"user_profile": {"lthr": 160}}):
            rpe, tss = garmin.estimate_rpe_tss("running", 3600.0, 160)
            self.assertEqual(rpe, 8)            # round(1.0 * 8)
            self.assertAlmostEqual(tss, 100.0)  # 1h * 1.0^2 * 100

    def test_estimate_rpe_tss_no_hr_defaults(self):
        with patch.dict(garmin.config.data, {"user_profile": {"lthr": 160}}):
            self.assertEqual(garmin.estimate_rpe_tss("yoga", 3600.0, None), (2, 15.0))
            self.assertEqual(garmin.estimate_rpe_tss("rest", 3600.0, None), (0, 0.0))


class TestRecomputeDerived(unittest.TestCase):
    def setUp(self):
        clear_all_tables(test_db)

    def test_recompute_fills_workload_and_baseline(self):
        # 30 days of metric rows + one activity in the acute window.
        for i in range(30, -1, -1):
            test_db.save_metric_cache(date=_d(-i), rhr=50, hrv=70, sleep_score=80, stress=20)
        test_db.save_completed_activity(
            activity_id="a1", date=_d(-1), start_time=None, activity_name="Run",
            activity_type="running", duration_sec=3600.0, distance_km=10.0,
            elevation_gain_m=0.0, avg_hr=150, max_hr=170, rpe=5, tss=60.0,
        )

        garmin.recompute_derived()

        today_row = test_db.get_metrics_cache(start_date=_d(0), end_date=_d(0))[0]
        # Activity yesterday contributes to today's 7-day acute window.
        self.assertGreater(today_row["acute_workload"], 0.0)
        self.assertIsNotNone(today_row["acwr"])
        # 28+ days of stable metrics -> a baseline exists for today.
        baseline = test_db.get_baseline(_d(0))
        self.assertIsNotNone(baseline)
        self.assertAlmostEqual(baseline["rhr_baseline_mean"], 50.0)


class TestEnsureData(unittest.TestCase):
    def setUp(self):
        clear_all_tables(test_db)
        garmin.reset_memo()
        self.env = patch.dict(
            os.environ, {"GARMIN_EMAIL": "a@b.c", "GARMIN_PASSWORD": "pw"}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        garmin.reset_memo()

    def test_no_credentials_skips_silently(self):
        self.env.stop()  # remove creds for this test
        with patch.dict(os.environ, {}, clear=True), patch.object(garmin, "pull") as mock_pull:
            with patch("builtins.print") as mock_print:
                garmin.ensure_data(_d(-5), _d(0))
            mock_pull.assert_not_called()
            mock_print.assert_not_called()
        self.env.start()  # restore for tearDown symmetry

    def test_cold_start_surfaces_command_without_pulling(self):
        with patch.object(garmin, "pull") as mock_pull:
            with patch("builtins.print") as mock_print:
                garmin.ensure_data(_d(-5), _d(0))
            mock_pull.assert_not_called()
            printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
            self.assertIn("data pull --start-date", printed)

    def test_small_recent_gap_auto_pulls(self):
        # Fully covered history except the recent mutable zone is stale (no watermark).
        for i in range(60, -1, -1):
            test_db.save_metric_cache(date=_d(-i), rhr=50, hrv=70, sleep_score=80, stress=20)
        with patch.object(garmin, "pull") as mock_pull:
            garmin.ensure_data(_d(-5), _d(0))
        mock_pull.assert_called()  # auto-pulled the small/recent region

    def test_large_backward_gap_surfaces_command(self):
        # Data only for the last few days; a read far back needs a large backfill.
        for i in range(3, -1, -1):
            test_db.save_metric_cache(date=_d(-i), rhr=50, hrv=70, sleep_score=80, stress=20)
        garmin.db.set_sync_state(through_date=_d(0), last_pull_utc=datetime.now(timezone.utc).isoformat())
        with patch.object(garmin, "pull") as mock_pull:
            with patch("builtins.print") as mock_print:
                garmin.ensure_data(_d(-120), _d(0))
            mock_pull.assert_not_called()
            printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
            self.assertIn("data pull --start-date", printed)

    def test_fresh_data_no_pull(self):
        for i in range(60, -1, -1):
            test_db.save_metric_cache(date=_d(-i), rhr=50, hrv=70, sleep_score=80, stress=20)
        # Recent watermark -> mutable zone considered fresh, nothing to fetch.
        garmin.db.set_sync_state(through_date=_d(0), last_pull_utc=datetime.now(timezone.utc).isoformat())
        with patch.object(garmin, "pull") as mock_pull:
            garmin.ensure_data(_d(-5), _d(0))
        mock_pull.assert_not_called()

    def test_interior_gap_present_rows_are_not_holes(self):
        # All-null rows still count as "pulled" -> not treated as a gap to refetch.
        for i in range(60, -1, -1):
            test_db.save_metric_cache(date=_d(-i), rhr=None, hrv=None, sleep_score=None, stress=None)
        garmin.db.set_sync_state(through_date=_d(0), last_pull_utc=datetime.now(timezone.utc).isoformat())
        with patch.object(garmin, "pull") as mock_pull:
            garmin.ensure_data(_d(-5), _d(0))
        mock_pull.assert_not_called()


class TestWatermarkForwardOnly(unittest.TestCase):
    def setUp(self):
        clear_all_tables(test_db)

    def test_through_date_never_regresses(self):
        test_db.set_sync_state(through_date=_d(0), last_pull_utc="2026-01-01T00:00:00+00:00")
        state = test_db.get_sync_state()
        self.assertEqual(state["through_date"], _d(0))
        # A backfill of older dates passes the existing high-water mark through.
        test_db.set_sync_state(through_date=_d(0), last_pull_utc="2026-02-01T00:00:00+00:00")
        self.assertEqual(test_db.get_sync_state()["through_date"], _d(0))


if __name__ == "__main__":
    unittest.main()
