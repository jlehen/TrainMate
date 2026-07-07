"""PMC (CTL/ATL/TSB fitness-fatigue-form) tests — see DESIGN_pmc_fitness_fatigue.md §8.

Split into pure-math (compute_pmc / warm-up / ramp / color), formatting
(format_metrics_history + status/CSV None handling), and a couple of DB integration
checks (recompute upsert, COALESCE preservation, wipe-then-recompute).
"""
import os
import unittest
import unittest.mock
from datetime import date, timedelta

from tests.helpers import clear_all_tables
from trainmate.db import Database
import trainmate.db
import trainmate.garmin as garmin
from trainmate.garmin import compute_pmc, pmc_warmup_cutoff_for, pmc_ramp
from trainmate.coach.formatting import format_metrics_history, PMC_TSB_LAG_NOTE
from trainmate.util import color_tsb, color_ramp


TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_pmc.db")
test_db = Database(db_path=TEST_DB_PATH)


class _DBBackedTest(unittest.TestCase):
    """Base for the DB-integration classes: point the module-global db handles at this
    file's test DB for the duration of each test, then restore them.

    Done per-test with restore (not at import) so this module never leaves the shared
    `garmin.db` / `trainmate.db.db` singletons rebound for whatever test module runs next
    — the suite rebinds them per file at import (see test_garmin.py), and a lingering
    rebind here would break another file regardless of collection order."""

    def _use_test_db(self):
        prev_gdb, prev_tdb = garmin.db, trainmate.db.db
        garmin.db = test_db
        trainmate.db.db = test_db

        def _restore():
            garmin.db = prev_gdb
            trainmate.db.db = prev_tdb
        self.addCleanup(_restore)


def _d(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def tearDownModule():
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except OSError:
            pass


# ==============================================================================
# compute_pmc — the pure EWMA (§8 "compute_pmc unit tests")
# ==============================================================================

class TestComputePMC(unittest.TestCase):
    def _series(self, load_by_day, start="2026-01-01", days=200, ctl=42, atl=7):
        end = (date.fromisoformat(start) + timedelta(days=days - 1)).isoformat()
        return compute_pmc(load_by_day, start, end, ctl, atl)

    def test_constant_load_converges(self):
        # Steady L: CTL->L, ATL->L, TSB->0, with ATL leading CTL early (smaller tau).
        L = 60.0
        daily = {(date(2026, 1, 1) + timedelta(days=i)).isoformat(): L for i in range(200)}
        pmc = self._series(daily)
        last = pmc[(date(2026, 1, 1) + timedelta(days=199)).isoformat()]
        ctl, atl, tsb = last
        self.assertAlmostEqual(ctl, L, delta=1.0)
        self.assertAlmostEqual(atl, L, delta=0.5)
        self.assertAlmostEqual(tsb, 0.0, delta=1.0)
        # Early on, ATL (7d) climbs faster than CTL (42d) -> TSB negative.
        early = pmc[(date(2026, 1, 1) + timedelta(days=10)).isoformat()]
        self.assertLess(early[2], 0.0)

    def test_tsb_off_by_one(self):
        # A single big day raises ATL ON that day, but TSB must only drop the NEXT day.
        daily = {"2026-01-10": 300.0}
        pmc = compute_pmc(daily, "2026-01-01", "2026-01-20", 42, 7)
        d10 = pmc["2026-01-10"]
        d11 = pmc["2026-01-11"]
        # ATL jumps on the 10th; its TSB is still ~0 (yesterday's balance, pre-spike).
        self.assertGreater(d10[1], 0.0)
        self.assertAlmostEqual(d10[2], 0.0, delta=0.01)
        # The drop shows up on the 11th.
        self.assertLess(d11[2], d10[2])

    def test_calendar_gaps_decay(self):
        # A rest span with no rows must still decay both EWMAs (regression vs a
        # rows-only iteration that would skip the empty days).
        daily = {(date(2026, 1, 1) + timedelta(days=i)).isoformat(): 80.0 for i in range(30)}
        pmc = compute_pmc(daily, "2026-01-01", "2026-02-28", 42, 7)  # 30 loaded + rest
        loaded_end = pmc["2026-01-30"]
        rest_end = pmc["2026-02-28"]
        self.assertLess(rest_end[0], loaded_end[0])   # CTL decayed
        self.assertLess(rest_end[1], loaded_end[1])   # ATL decayed (faster)

    def test_first_day_tsb_zero(self):
        pmc = compute_pmc({"2026-01-01": 100.0}, "2026-01-01", "2026-01-03", 42, 7)
        self.assertEqual(pmc["2026-01-01"][2], 0.0)   # both EWMAs seed equal at 0

    def test_time_constants_from_params(self):
        daily = {(date(2026, 1, 1) + timedelta(days=i)).isoformat(): 50.0 for i in range(100)}
        # tau_ctl == tau_atl -> CTL identical to ATL every day -> TSB pinned at 0.
        equal = compute_pmc(daily, "2026-01-01", "2026-04-10", 20, 20)
        for ctl, atl, tsb in equal.values():
            self.assertEqual(ctl, atl)
            self.assertEqual(tsb, 0.0)
        # A shorter tau_ctl converges faster (higher CTL at a fixed early day).
        fast = compute_pmc(daily, "2026-01-01", "2026-01-15", 20, 7)
        slow = compute_pmc(daily, "2026-01-01", "2026-01-15", 42, 7)
        self.assertGreater(fast["2026-01-15"][0], slow["2026-01-15"][0])

    def test_empty_and_degenerate_span(self):
        self.assertEqual(compute_pmc({}, "", "", 42, 7), {})
        # end < start must not throw and returns {}.
        self.assertEqual(compute_pmc({}, "2026-02-01", "2026-01-01", 42, 7), {})


# ==============================================================================
# Warm-up cutoff (§3.3a) + static still-warming-up flag (§3.3b)
# ==============================================================================

class TestWarmup(unittest.TestCase):
    def test_cutoff_is_start_plus_tau(self):
        self.assertEqual(pmc_warmup_cutoff_for("2026-01-01", 42), "2026-02-12")
        self.assertIsNone(pmc_warmup_cutoff_for(None, 42))


class TestStaticFlag(_DBBackedTest):
    """garmin.pmc_data_caveat — the static "still warming up" flag (§3.3b): fires with a
    plain N=today-history_start while N < 3*tau_ctl, then drops. No convergence-% figure."""

    def setUp(self):
        self._use_test_db()
        clear_all_tables(test_db)

    def test_flag_fires_for_young_db_with_right_n(self):
        # 30 days of history behind today -> flag fires; N is today - history_start.
        base = date.today() - timedelta(days=30)
        for i in range(31):
            test_db.save_metric_cache(
                date=(base + timedelta(days=i)).isoformat(),
                rhr=50, hrv=70, sleep_score=80, stress=20,
            )
        cav = garmin.pmc_data_caveat(date.today().isoformat(), dbh=test_db)
        self.assertIsNotNone(cav)
        self.assertEqual(cav["n_days"], 30)
        self.assertEqual(cav["history_start"], base.isoformat())
        self.assertNotIn("pct", cav)              # static flag — no convergence figure

    def test_flag_fires_on_first_pull_day(self):
        # N = 0 (history starts today) is the youngest possible DB — the flag must
        # fire, not be excluded by an off-by-one at the boundary.
        test_db.save_metric_cache(
            date=date.today().isoformat(),
            rhr=50, hrv=70, sleep_score=80, stress=20,
        )
        cav = garmin.pmc_data_caveat(date.today().isoformat(), dbh=test_db)
        self.assertIsNotNone(cav)
        self.assertEqual(cav["n_days"], 0)

    def test_flag_drops_once_history_exceeds_three_tau(self):
        # >= 3*tau_ctl (126 days) of history -> artifact negligible, no flag.
        base = date.today() - timedelta(days=130)
        for i in range(131):
            test_db.save_metric_cache(
                date=(base + timedelta(days=i)).isoformat(),
                rhr=50, hrv=70, sleep_score=80, stress=20,
            )
        self.assertIsNone(
            garmin.pmc_data_caveat(date.today().isoformat(), dbh=test_db)
        )

    def test_no_history_no_flag(self):
        self.assertIsNone(garmin.pmc_data_caveat(date.today().isoformat(), dbh=test_db))


# ==============================================================================
# Ramp lookup (§3.1 interior-gap / young-DB rules)
# ==============================================================================

class TestRamp(unittest.TestCase):
    def test_exact_seven_day_delta(self):
        ctl_by_date = {
            (date(2026, 1, 1) + timedelta(days=i)).isoformat(): float(i)
            for i in range(30)
        }
        # ctl on day 20 is 20.0, day 13 is 13.0 -> ramp +7.0.
        self.assertEqual(pmc_ramp(ctl_by_date, "2026-01-21"), 7.0)

    def test_nearest_earlier_on_interior_gap(self):
        # d-7 day missing -> use the nearest earlier day with a value.
        ctl_by_date = {"2026-01-01": 10.0, "2026-01-05": 12.0, "2026-01-12": 20.0}
        # For 2026-01-12, d-7 = 2026-01-05 (present) -> 20-12 = 8.0.
        self.assertEqual(pmc_ramp(ctl_by_date, "2026-01-12"), 8.0)

    def test_omit_when_fewer_than_seven_days(self):
        ctl_by_date = {"2026-01-10": 5.0, "2026-01-11": 6.0}
        # No value at/under 2026-01-04 -> omit rather than emit garbage.
        self.assertIsNone(pmc_ramp(ctl_by_date, "2026-01-11"))

    def test_none_when_date_absent(self):
        self.assertIsNone(pmc_ramp({"2026-01-01": 5.0}, "2026-02-01"))

    def test_older_baseline_is_scaled_per_week(self):
        # Baseline 10 days back (interior hole at d-7): the delta is normalized to a
        # per-7-day rate, so a multi-week span can't masquerade as "/week".
        ctl_by_date = {"2026-01-02": 10.0, "2026-01-12": 20.0}
        self.assertEqual(pmc_ramp(ctl_by_date, "2026-01-12"), 7.0)  # (20-10)*7/10

    def test_fallback_is_bounded_at_two_windows(self):
        # Nearest earlier value is 23 days old -> beyond the 2*window bound: omit
        # rather than report a month-scale delta as a weekly ramp.
        ctl_by_date = {"2025-12-20": 10.0, "2026-01-12": 20.0}
        self.assertIsNone(pmc_ramp(ctl_by_date, "2026-01-12"))

    def test_straddle_guard_suppresses_warmup_baseline(self):
        # The -7d baseline lands before the warm-up cutoff -> the ramp would be
        # measured against a suppressed warm-up artifact: omit (§5.4 straddle guard).
        ctl_by_date = {"2026-01-05": 10.0, "2026-01-12": 20.0}
        self.assertIsNone(
            pmc_ramp(ctl_by_date, "2026-01-12", warmup_cutoff="2026-01-06")
        )
        # At/after the cutoff the same baseline is fine.
        self.assertEqual(
            pmc_ramp(ctl_by_date, "2026-01-12", warmup_cutoff="2026-01-05"), 10.0
        )


# ==============================================================================
# Color bands (§6.1) — strip ANSI to assert which band claimed a value
# ==============================================================================

class TestColors(unittest.TestCase):
    def setUp(self):
        # Force color on regardless of TTY so the band is observable.
        self._patch = unittest.mock.patch("trainmate.util.is_color_enabled", return_value=True)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _has_color(self, s, code):
        return f"\033[{code}m" in s

    def test_tsb_risk_ends_only(self):
        # Phase-blind: only the two risk ends color. < -30 red, > +25 yellow.
        self.assertTrue(self._has_color(color_tsb(-31.0), "31"))    # red
        self.assertTrue(self._has_color(color_tsb(26.0), "33"))     # yellow

    def test_tsb_midrange_uncolored_no_green(self):
        # No green band and no phase argument: the -30..+25 middle is always plain,
        # including the old race-ready +5..+25 region.
        for v in (12.0, 5.0, 0.0, -10.0, -30.0, 25.0):
            s = color_tsb(v)
            self.assertFalse(self._has_color(s, "32"))   # never green
            self.assertFalse(self._has_color(s, "31"))   # not red
            self.assertFalse(self._has_color(s, "33"))   # not yellow

    def test_ramp_bands_touch(self):
        self.assertTrue(self._has_color(color_ramp(9.0), "31"))    # >=8 red
        self.assertTrue(self._has_color(color_ramp(7.5), "33"))    # 5..<8 yellow (no hole)
        self.assertTrue(self._has_color(color_ramp(6.0), "33"))
        self.assertFalse(self._has_color(color_ramp(4.0), "31"))
        self.assertFalse(self._has_color(color_ramp(4.0), "33"))


# ==============================================================================
# format_metrics_history — None omission + warm-up suppression (§5.1)
# ==============================================================================

class TestFormatMetricsHistory(unittest.TestCase):
    def test_null_fields_omitted_no_crash(self):
        # Regression: NULL acwr used to crash `:.2f`; NULL rhr rendered `Nonebpm`.
        rows = [{"date": "2026-06-01", "rhr": None, "hrv": None, "sleep_score": None,
                 "stress": None, "acwr": None, "ctl": None, "atl": None, "tsb": None}]
        out = format_metrics_history(rows)
        self.assertIn("2026-06-01", out)
        self.assertNotIn("None", out)
        self.assertNotIn("ACWR", out)

    def test_full_row_shows_pmc_and_footnote(self):
        rows = [{"date": "2026-07-02", "rhr": 52, "hrv": 61, "sleep_score": 78,
                 "stress": 31, "acwr": 1.12, "ctl": 62.4, "atl": 71.7, "tsb": -8.9}]
        out = format_metrics_history(rows)
        self.assertIn("CTL=62.4", out)
        self.assertIn("ATL=71.7", out)
        self.assertIn("TSB=-8.9", out)
        self.assertIn(PMC_TSB_LAG_NOTE, out)

    def test_warmup_rows_suppress_pmc(self):
        rows = [{"date": "2026-01-05", "rhr": 50, "hrv": 60, "sleep_score": 80,
                 "stress": 20, "acwr": 1.0, "ctl": 20.0, "atl": 55.0, "tsb": -30.0}]
        out = format_metrics_history(rows, warmup_cutoff="2026-02-12")
        self.assertIn("ACWR=1.00", out)
        self.assertNotIn("CTL", out)
        self.assertNotIn(PMC_TSB_LAG_NOTE, out)

    def test_all_null_row_marked_no_data(self):
        # A pulled-but-empty day must not render a dangling "- 2026-06-01: " line.
        rows = [{"date": "2026-06-01", "rhr": None, "hrv": None, "sleep_score": None,
                 "stress": None, "acwr": None, "ctl": None, "atl": None, "tsb": None}]
        out = format_metrics_history(rows)
        self.assertIn("- 2026-06-01: (no data)", out)

    def test_partial_pmc_row_still_gets_lag_footnote(self):
        # The footnote explains the TSB lag; it must appear whenever TSB is shown,
        # even when CTL happens to be NULL.
        rows = [{"date": "2026-07-02", "rhr": 52, "hrv": 61, "sleep_score": 78,
                 "stress": 31, "acwr": 1.1, "ctl": None, "atl": 71.7, "tsb": -8.9}]
        out = format_metrics_history(rows)
        self.assertIn("TSB=-8.9", out)
        self.assertIn(PMC_TSB_LAG_NOTE, out)

    def test_no_tsb_no_lag_footnote(self):
        # ...and conversely: a CTL/ATL-only block shows no TSB, so there is no lag
        # on display to explain and the footnote must be omitted.
        rows = [{"date": "2026-07-02", "rhr": 52, "hrv": 61, "sleep_score": 78,
                 "stress": 31, "acwr": 1.1, "ctl": 62.4, "atl": 71.7, "tsb": None}]
        out = format_metrics_history(rows)
        self.assertIn("CTL=62.4", out)
        self.assertNotIn(PMC_TSB_LAG_NOTE, out)


class TestDisplayValues(unittest.TestCase):
    """garmin.pmc_display_values — the one warm-up blanking rule the status line,
    show-metrics table, and CSV all share."""

    def test_warmup_row_blanks_all_three(self):
        row = {"date": "2026-01-05", "ctl": 20.0, "atl": 55.0, "tsb": -30.0}
        self.assertEqual(
            garmin.pmc_display_values(row, "2026-02-12"), (None, None, None)
        )

    def test_past_cutoff_returns_stored_values(self):
        row = {"date": "2026-03-01", "ctl": 62.4, "atl": 71.7, "tsb": None}
        self.assertEqual(
            garmin.pmc_display_values(row, "2026-02-12"), (62.4, 71.7, None)
        )
        self.assertEqual(
            garmin.pmc_display_values(row, None), (62.4, 71.7, None)
        )


# ==============================================================================
# DB integration — recompute upsert, COALESCE, wipe-then-recompute
# ==============================================================================

class TestPMCIntegration(_DBBackedTest):
    def setUp(self):
        self._use_test_db()
        clear_all_tables(test_db)

    def _add_activity(self, aid, date_str, tss):
        test_db.save_completed_activity(
            activity_id=aid, date=date_str, start_time=f"{date_str} 09:00:00",
            activity_name="Run", activity_type="running", duration_sec=3600.0,
            distance_km=10.0, elevation_gain_m=0.0, avg_hr=150, max_hr=170,
            rpe=None, tss=tss,
            zone1_sec=0, zone2_sec=1800, zone3_sec=1800, zone4_sec=0, zone5_sec=0,
        )

    def test_recompute_populates_and_activity_only_days_create_no_row(self):
        base = date(2026, 3, 1)
        for i in range(60):
            d = (base + timedelta(days=i)).isoformat()
            test_db.save_metric_cache(date=d, rhr=50, hrv=70, sleep_score=80, stress=20)
            self._add_activity(f"a{i}", d, 60.0)
        # An activity on a day with NO metrics row must feed the EWMA but create no row.
        orphan = (base + timedelta(days=80)).isoformat()
        self._add_activity("orphan", orphan, 90.0)

        garmin.recompute_derived()

        rows = {m["date"]: m for m in test_db.get_metrics_cache()}
        self.assertNotIn(orphan, rows)                       # no row widened
        late = rows[(base + timedelta(days=59)).isoformat()]
        self.assertIsNotNone(late["ctl"])
        self.assertIsNotNone(late["tsb"])

    def test_coalesce_preserves_pmc_on_metrics_only_repull(self):
        base = date(2026, 3, 1)
        for i in range(50):
            d = (base + timedelta(days=i)).isoformat()
            test_db.save_metric_cache(date=d, rhr=50, hrv=70, sleep_score=80, stress=20)
            self._add_activity(f"a{i}", d, 55.0)
        garmin.recompute_derived()
        d40 = (base + timedelta(days=40)).isoformat()
        before = next(m for m in test_db.get_metrics_cache() if m["date"] == d40)["ctl"]
        self.assertIsNotNone(before)
        # A metrics-only re-save (ctl/atl/tsb default None) must NOT null the stored PMC.
        test_db.save_metric_cache(date=d40, rhr=51, hrv=69, sleep_score=79, stress=21)
        after = next(m for m in test_db.get_metrics_cache() if m["date"] == d40)["ctl"]
        self.assertEqual(before, after)

    def test_config_windows_move_normalized_chronic_in_same_sweep(self):
        # chronic_weeks is computed INLINE from the live windows (never a frozen
        # CHRONIC_WEEKS): with acute=14/chronic=28 and steady 60/day, chronic
        # normalizes by 2 (not the default 4), so chronic == acute and ACWR == 1.0.
        base = date(2026, 3, 1)
        for i in range(60):
            d = (base + timedelta(days=i)).isoformat()
            test_db.save_metric_cache(date=d, rhr=50, hrv=70, sleep_score=80, stress=20)
            self._add_activity(f"a{i}", d, 60.0)
        with unittest.mock.patch.dict(
            garmin.config.data,
            {"garmin": {"acwr_acute_days": 14, "acwr_chronic_days": 28}},
        ):
            garmin.recompute_derived()
        late = next(
            m for m in test_db.get_metrics_cache()
            if m["date"] == (base + timedelta(days=59)).isoformat()
        )
        self.assertAlmostEqual(late["acute_workload"], 840.0, delta=0.1)
        self.assertAlmostEqual(late["chronic_workload"], 840.0, delta=0.1)
        self.assertAlmostEqual(late["acwr"], 1.0, delta=0.01)

    def test_nonpositive_config_window_fails_loud(self):
        # The windows are EWMA/averaging divisors; a zero must be rejected at read
        # time with a clear message, not crash recompute mid-sweep (or store
        # negative-window nonsense).
        for key in ("acwr_acute_days", "acwr_chronic_days", "pmc_ctl_days", "pmc_atl_days"):
            with unittest.mock.patch.dict(garmin.config.data, {"garmin": {key: 0}}):
                with self.assertRaises(ValueError):
                    getattr(garmin.config, key)

    def test_wipe_then_recompute_clears_load_through_the_gap(self):
        base = date(2026, 3, 1)
        for i in range(60):
            d = (base + timedelta(days=i)).isoformat()
            test_db.save_metric_cache(date=d, rhr=50, hrv=70, sleep_score=80, stress=20)
            self._add_activity(f"a{i}", d, 70.0)
        garmin.recompute_derived()
        later = (base + timedelta(days=59)).isoformat()
        ctl_before = next(m for m in test_db.get_metrics_cache() if m["date"] == later)["ctl"]

        # Wipe an early activity range, then recompute (the command-layer contract).
        wipe_end = (base + timedelta(days=20)).isoformat()
        test_db.wipe_garmin_data(start=base.isoformat(), end=wipe_end)
        garmin.recompute_derived()

        row = next((m for m in test_db.get_metrics_cache() if m["date"] == later), None)
        self.assertIsNotNone(row)
        # Deleted early load no longer inflates CTL on a later surviving day.
        self.assertLess(row["ctl"], ctl_before)


class TestPMCService(_DBBackedTest):
    """Service-level PMC surfacing: the single CTL ramp line from full history."""

    def setUp(self):
        self._use_test_db()
        clear_all_tables(test_db)
        from trainmate.coach.service import CoachService
        self.svc = CoachService(db_instance=test_db)
        # 60 days of steady load + metrics rows, recomputed so CTL/ATL are populated and
        # the latest row is well past the warm-up cutoff.
        base = date.today() - timedelta(days=59)
        for i in range(60):
            d = (base + timedelta(days=i)).isoformat()
            test_db.save_metric_cache(date=d, rhr=50, hrv=70, sleep_score=80, stress=20)
            self._add_activity(f"a{i}", d, 60.0)
        garmin.recompute_derived()

    def _add_activity(self, aid, date_str, tss):
        test_db.save_completed_activity(
            activity_id=aid, date=date_str, start_time=f"{date_str} 09:00:00",
            activity_name="Run", activity_type="running", duration_sec=3600.0,
            distance_km=10.0, elevation_gain_m=0.0, avg_hr=150, max_hr=170,
            rpe=None, tss=tss,
            zone1_sec=0, zone2_sec=1800, zone3_sec=1800, zone4_sec=0, zone5_sec=0,
        )

    def test_ramp_line_from_full_history(self):
        # Steady 60/day -> CTL still climbing over 60 days, so ramp is positive & present.
        line = self.svc._pmc_ramp_line()
        self.assertIsNotNone(line)
        self.assertIn("CTL ramp rate:", line)


if __name__ == "__main__":
    unittest.main()
