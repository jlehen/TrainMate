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
    @staticmethod
    def _no_power():
        return {f"power_zone{i}_sec": None for i in range(1, 8)}

    @staticmethod
    def _no_hr():
        return {f"zone{i}_sec": 0 for i in range(1, 6)}

    # --- measured_tss: the stored objective measurement -------------------
    def test_measured_tss_prefers_power(self):
        power = self._no_power()
        power["power_zone2_sec"] = 1800   # 1800 * 0.0117 = 21.06
        power["power_zone3_sec"] = 1800   # 1800 * 0.0178 = 32.04
        hr = {f"zone{i}_sec": 720 for i in range(1, 6)}  # present but power wins
        self.assertEqual(garmin.measured_tss(power, hr), 53.1)

    def test_measured_tss_falls_back_to_hr(self):
        hr = self._no_hr()
        hr["zone2_sec"] = 3600            # 3600 * 0.0111 = 39.96
        self.assertEqual(garmin.measured_tss(self._no_power(), hr), 40.0)

    def test_measured_tss_none_without_zones(self):
        # Pure measurement: no coverage gate, no RPE — None when no zones.
        self.assertIsNone(garmin.measured_tss(self._no_power(), self._no_hr()))

    # --- compute_load: the fallback hierarchy -----------------------------
    def test_load_uses_power(self):
        power = self._no_power()
        power["power_zone4_sec"] = 3600   # 3600 * 0.0250 = 90.0
        load, method, warning = garmin.compute_load(
            power, {f"zone{i}_sec": 720 for i in range(1, 6)}, rpe=6,
            duration_sec=3600)
        self.assertEqual((load, method, warning), (90.0, "power", None))

    def test_load_uses_hr_with_good_coverage(self):
        hr = self._no_hr()
        hr["zone2_sec"] = 3600            # coverage 1.0
        load, method, warning = garmin.compute_load(
            self._no_power(), hr, rpe=4, duration_sec=3600)
        self.assertEqual((load, method, warning), (40.0, "hr", None))

    def test_load_diverts_sparse_hr_to_user_rpe(self):
        hr = self._no_hr()
        hr["zone1_sec"] = 300            # coverage 0.083 < 0.5
        load, method, warning = garmin.compute_load(
            self._no_power(), hr, rpe=3, duration_sec=3600)
        self.assertEqual((load, method, warning), (30.0, "rpe", None))

    def test_load_sparse_hr_no_rpe_warns_and_keeps_hr(self):
        hr = self._no_hr()
        hr["zone1_sec"] = 300            # coverage 0.083, hrTSS = 1.65 -> 1.6/1.7
        load, method, warning = garmin.compute_load(
            self._no_power(), hr, rpe=None, duration_sec=3600)
        self.assertEqual(method, "hr_sparse")
        self.assertIsNotNone(warning)
        self.assertAlmostEqual(load, round(300 * 0.0055, 1))

    def test_load_no_data_no_rpe_warns_zero(self):
        load, method, warning = garmin.compute_load(
            self._no_power(), self._no_hr(), rpe=None, duration_sec=3600)
        self.assertEqual(load, 0.0)
        self.assertEqual(method, "none")
        self.assertIsNotNone(warning)

    def test_load_no_zones_uses_rpe(self):
        load, method, warning = garmin.compute_load(
            self._no_power(), self._no_hr(), rpe=5, duration_sec=3600)
        self.assertEqual((load, method, warning), (50.0, "rpe", None))

    # --- rpe_divergence: hidden-fatigue flag ------------------------------
    @staticmethod
    def _hr_activity(**overrides):
        act = {f"zone{i}_sec": 0 for i in range(1, 6)}
        act.update({f"power_zone{i}_sec": None for i in range(1, 8)})
        act.update({"duration_sec": 3600, "rpe": None, "tss": None})
        act.update(overrides)
        return act

    def test_rpe_divergence_flags_when_rpe_exceeds_measured(self):
        # Measured hrTSS 40 (coverage 1.0); RPE 9 -> sRPE 90 -> ratio 2.25 >= 1.5.
        act = self._hr_activity(zone2_sec=3600, tss=40.0, rpe=9)
        self.assertEqual(garmin.rpe_divergence(act), 2.25)

    def test_rpe_divergence_none_without_rpe(self):
        act = self._hr_activity(zone2_sec=3600, tss=40.0, rpe=None)
        self.assertIsNone(garmin.rpe_divergence(act))

    def test_rpe_divergence_none_when_load_is_rpe_based(self):
        # Sparse HR -> load already came from RPE, so there is no divergence.
        act = self._hr_activity(zone1_sec=300, tss=1.7, rpe=8)  # coverage 0.083
        self.assertIsNone(garmin.rpe_divergence(act))

    def test_activity_load_takes_rpe_when_divergent(self):
        # Trustworthy hrTSS 15 but RPE 4 -> sRPE 40 (ratio 2.67 >= 1.5): the
        # meters under-counted real strain (e.g. strength work), so load = sRPE.
        act = self._hr_activity(zone2_sec=3600, tss=15.0, rpe=4)
        self.assertAlmostEqual(garmin.activity_load(act), 40.0)

    def test_activity_load_keeps_measured_when_rpe_agrees(self):
        # hrTSS 40 and RPE 4 -> sRPE 40 (ratio 1.0 < 1.5): no divergence, keep it.
        act = self._hr_activity(zone2_sec=3600, tss=40.0, rpe=4)
        self.assertAlmostEqual(garmin.activity_load(act), 40.0)


class TestZoneParsing(unittest.TestCase):
    def _client(self):
        return garmin.GarminClient(email="x", password="y", token_store_dir="/tmp")

    def test_parse_zone_entries_handles_key_spellings(self):
        data = [
            {"zoneNumber": 1, "secsInZone": 60},
            {"zoneId": 3, "timeInZone": 120},  # alternate spellings
            {"zoneNumber": 8, "secsInZone": 99},  # out of range -> ignored
        ]
        zones = garmin.GarminClient._parse_zone_entries(data, 7, "power_zone")
        self.assertEqual(zones["power_zone1_sec"], 60)
        self.assertEqual(zones["power_zone3_sec"], 120)
        self.assertEqual(zones["power_zone7_sec"], 0)
        self.assertNotIn("power_zone8_sec", zones)

    def test_get_activity_power_zones_returns_seconds(self):
        client = self._client()
        client.api = type("Api", (), {})()
        client.api.get_activity_power_in_timezones = lambda aid: [
            {"zoneNumber": 2, "secsInZone": 300},
            {"zoneNumber": 4, "secsInZone": 150},
        ]
        zones = client.get_activity_power_zones("123")
        self.assertEqual(zones["power_zone2_sec"], 300)
        self.assertEqual(zones["power_zone4_sec"], 150)
        self.assertEqual(zones["power_zone1_sec"], 0)

    def test_get_activity_power_zones_all_none_when_empty(self):
        client = self._client()
        client.api = type("Api", (), {})()
        client.api.get_activity_power_in_timezones = lambda aid: []
        zones = client.get_activity_power_zones("123")
        self.assertTrue(all(v is None for v in zones.values()))
        self.assertEqual(set(zones), {f"power_zone{i}_sec" for i in range(1, 8)})

    def test_get_activity_power_zones_all_none_on_error(self):
        client = self._client()
        client.api = type("Api", (), {})()

        def boom(aid):
            raise RuntimeError("no power data")

        client.api.get_activity_power_in_timezones = boom
        zones = client.get_activity_power_zones("123")
        self.assertTrue(all(v is None for v in zones.values()))


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


class TestBackfillTss(unittest.TestCase):
    def setUp(self):
        clear_all_tables(test_db)

    def test_backfill_rewrites_tss_from_zones(self):
        # Stored TSS is stale (old avg-power model); zone seconds say otherwise.
        test_db.save_completed_activity(
            activity_id="a1", date=_d(-1), start_time=None, activity_name="Ride",
            activity_type="cycling", duration_sec=3600.0, distance_km=30.0,
            elevation_gain_m=0.0, avg_hr=150, max_hr=170, rpe=6, tss=999.0,
            power_zone2_sec=1800, power_zone3_sec=1800,  # 21.06 + 32.04 = 53.1
        )

        changed = garmin.backfill_tss()

        self.assertEqual(changed, 1)
        row = test_db.get_completed_activities()[0]
        self.assertEqual(row["tss"], 53.1)

    def test_backfill_stores_measurement_not_rpe(self):
        # No power, sparse HR, no user RPE. `tss` is the pure measurement, so it
        # becomes the (low) hrTSS — we never synthesise RPE to inflate it.
        test_db.save_completed_activity(
            activity_id="y1", date=_d(-1), start_time=None, activity_name="Yoga",
            activity_type="yoga", duration_sec=3600.0, distance_km=0.0,
            elevation_gain_m=0.0, avg_hr=90, max_hr=110, rpe=None, tss=17.2,
            zone1_sec=120,  # hrTSS = 120 * 0.0055 = 0.66 -> 0.7
        )

        garmin.backfill_tss()

        row = test_db.get_completed_activities()[0]
        self.assertEqual(row["tss"], 0.7)

    def test_backfill_clears_tss_without_zones(self):
        # No zones at all -> measurement is NULL (load comes from RPE on the fly).
        test_db.save_completed_activity(
            activity_id="s1", date=_d(-1), start_time=None, activity_name="Lift",
            activity_type="strength_training", duration_sec=1800.0, distance_km=0.0,
            elevation_gain_m=0.0, avg_hr=None, max_hr=None, rpe=7, tss=42.0,
        )

        garmin.backfill_tss()

        row = test_db.get_completed_activities()[0]
        self.assertIsNone(row["tss"])


class _FakeClient:
    """Minimal GarminClient stand-in for _ingest_activities. `summaries` is the
    activity list get_activities returns; set `raise_on_fetch` to simulate an API
    failure. No HR/power so the zone/RPE lookups stay trivial."""

    def __init__(self, summaries, raise_on_fetch=False):
        self.summaries = summaries
        self.raise_on_fetch = raise_on_fetch

    def get_activities(self, start, end):
        if self.raise_on_fetch:
            raise RuntimeError("garmin down")
        return self.summaries

    def get_activity_hr_zones(self, activity_id):
        return {f"zone{i}_sec": 0 for i in range(1, 6)}

    def get_activity_power_zones(self, activity_id):
        return {f"power_zone{i}_sec": None for i in range(1, 8)}

    def get_activity_rpe(self, activity_id):
        return None


def _summary(activity_id, day, type_key="indoor_cycling"):
    return {
        "activityId": activity_id,
        "startTimeLocal": f"{day} 08:00:00",
        "activityType": {"typeKey": type_key},
        "duration": 3600.0,
        "averageHR": None,
    }


class TestIngestReconcilesDeletions(unittest.TestCase):
    def setUp(self):
        clear_all_tables(test_db)

    def _seed(self, activity_id, day, type_key="indoor_cycling"):
        test_db.save_completed_activity(
            activity_id=activity_id, date=day, start_time=f"{day} 08:00:00",
            activity_name=type_key, activity_type=type_key, duration_sec=3600.0,
            distance_km=0.0, elevation_gain_m=0.0, avg_hr=None, max_hr=None,
            rpe=None, tss=None,
        )

    def _ids(self):
        return {a["activity_id"] for a in test_db.get_completed_activities()}

    def test_drops_activity_deleted_upstream(self):
        # The Zwift case: a duplicate auto-upload and the watch's own recording
        # both stored locally; the user deletes the Zwift one in Garmin Connect.
        self._seed("zwift_dup", _d(0), "virtual_ride")
        self._seed("watch", _d(0), "indoor_cycling")

        # Garmin now only returns the watch activity for today.
        client = _FakeClient([_summary("watch", _d(0))])
        with patch("builtins.print"):
            garmin._ingest_activities(client, _d(0), _d(0), throttle=0)

        self.assertEqual(self._ids(), {"watch"})

    def test_failed_fetch_leaves_local_rows_intact(self):
        # A swallowed error must NOT be read as "Garmin has no activities" and
        # wipe the range.
        self._seed("a1", _d(0))
        client = _FakeClient([], raise_on_fetch=True)
        with patch("builtins.print"):
            garmin._ingest_activities(client, _d(0), _d(0), throttle=0)

        self.assertEqual(self._ids(), {"a1"})

    def test_prune_is_scoped_to_pulled_range(self):
        # An activity outside the pulled window is untouched even when absent
        # from the fetch result.
        self._seed("today", _d(0))
        self._seed("last_week", _d(-7))

        client = _FakeClient([_summary("today", _d(0))])
        with patch("builtins.print"):
            garmin._ingest_activities(client, _d(0), _d(0), throttle=0)

        self.assertEqual(self._ids(), {"today", "last_week"})


class TestEnsureData(unittest.TestCase):
    def setUp(self):
        clear_all_tables(test_db)
        garmin.reset_memo()
        # Credentials come from config.yaml only; inject test creds into config.data.
        self.creds = patch.dict(
            garmin.config.data, {"garmin": {"email": "a@b.c", "password": "pw"}}
        )
        self.creds.start()

    def tearDown(self):
        self.creds.stop()
        garmin.reset_memo()

    def test_no_credentials_skips_silently(self):
        self.creds.stop()  # remove creds for this test
        with patch.dict(garmin.config.data, {}, clear=True), patch.object(garmin, "pull") as mock_pull:
            with patch("builtins.print") as mock_print:
                garmin.ensure_data(_d(-5), _d(0))
            mock_pull.assert_not_called()
            mock_print.assert_not_called()
        self.creds.start()  # restore for tearDown symmetry

    def test_cold_start_surfaces_command_without_pulling(self):
        with patch.object(garmin, "pull") as mock_pull:
            with patch("builtins.print") as mock_print:
                garmin.ensure_data(_d(-5), _d(0))
            mock_pull.assert_not_called()
            printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
            self.assertIn("data pull --from", printed)

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
            self.assertIn("data pull --from", printed)

    def test_pad_only_gap_auto_pulls(self):
        # Upgrade path: a DB whose history satisfied the old 28-day pad now has a
        # ~35-day hole that exists only to warm the wider 63-day derivation pad. It
        # lies entirely BEFORE the requested window, is bounded by the pad, and was
        # never user-requested — so it must auto-pull, not nag "run data pull --from"
        # on every command.
        for i in range(33, -1, -1):   # covers window + the old 28-day pad
            test_db.save_metric_cache(date=_d(-i), rhr=50, hrv=70, sleep_score=80, stress=20)
        garmin.db.set_sync_state(through_date=_d(0), last_pull_utc=datetime.now(timezone.utc).isoformat())
        with patch.object(garmin, "pull") as mock_pull:
            with patch("builtins.print") as mock_print:
                garmin.ensure_data(_d(-5), _d(0))
            mock_pull.assert_called()   # pad region pulled automatically
            printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
            self.assertNotIn("data pull --from", printed)

    def test_fresh_data_no_pull(self):
        # Cover the full derivation pad (now max(chronic, 28, 1.5*ctl)=63 days) so the
        # padded window has no missing days to fetch.
        for i in range(75, -1, -1):
            test_db.save_metric_cache(date=_d(-i), rhr=50, hrv=70, sleep_score=80, stress=20)
        # Recent watermark -> mutable zone considered fresh, nothing to fetch.
        garmin.db.set_sync_state(through_date=_d(0), last_pull_utc=datetime.now(timezone.utc).isoformat())
        with patch.object(garmin, "pull") as mock_pull:
            garmin.ensure_data(_d(-5), _d(0))
        mock_pull.assert_not_called()

    def test_interior_gap_present_rows_are_not_holes(self):
        # All-null rows still count as "pulled" -> not treated as a gap to refetch.
        # Cover the full 63-day derivation pad so the padded window has no real hole.
        for i in range(75, -1, -1):
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
