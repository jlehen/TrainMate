import os
import unittest
from datetime import date, timedelta

from trainmate.db import Database
import trainmate.db
import trainmate.garmin as garmin

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_progression.db")
test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
garmin.db = test_db

from trainmate import progression  # noqa: E402  (must follow the db patch above)


def tearDownModule():
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except OSError:
            pass


def _d(offset: int) -> str:
    """A YYYY-MM-DD string `offset` days from a fixed anchor (2026-07-03, a Friday)."""
    anchor = date(2026, 7, 3)
    return (anchor + timedelta(days=offset)).isoformat()


TODAY = _d(0)  # 2026-07-03, Friday


def _act(offset: int, tss=None, rpe=None, duration_sec=3600.0, activity_id=None):
    return {
        "activity_id": activity_id or f"act-{offset}-{tss}-{rpe}",
        "date": _d(offset),
        "tss": tss,
        "rpe": rpe,
        "duration_sec": duration_sec,
    }


def _w(offset: int, sport_type="running", tss=None, rpe=None, duration_minutes=None,
       removed=False):
    return {
        "date": _d(offset),
        "sport_type": sport_type,
        "tss": tss,
        "rpe": rpe,
        "duration_minutes": duration_minutes,
        "removed": removed,
    }


class TestDailyLoads(unittest.TestCase):
    def test_empty_without_any_activity(self):
        self.assertEqual(progression.daily_loads([], [_w(1, tss=50)], TODAY), [])

    def test_past_day_uses_measured_load_zero_gap_filled(self):
        # Activity on day -3 and day -1; day -2 has no activity and must still
        # appear as a zero-load, actual-source day (no gaps).
        activities = [_act(-3, tss=40.0), _act(-1, tss=60.0)]
        points = progression.daily_loads(activities, [], TODAY)
        by_date = {p["date"]: p for p in points}
        self.assertEqual(by_date[_d(-3)]["load"], 40.0)
        self.assertEqual(by_date[_d(-3)]["source"], "actual")
        self.assertEqual(by_date[_d(-2)]["load"], 0.0)
        self.assertEqual(by_date[_d(-2)]["source"], "actual")
        self.assertEqual(by_date[_d(-1)]["load"], 60.0)
        # Series runs through today (no plan) — window ends today.
        self.assertEqual(points[-1]["date"], TODAY)

    def test_future_day_uses_planned_load_srpe_fallback(self):
        activities = [_act(-1, tss=40.0)]
        workouts = [
            _w(1, tss=80),                       # coach TSS wins
            _w(2, rpe=6, duration_minutes=60),    # sRPE fallback: 6*10*1h = 60
        ]
        points = progression.daily_loads(activities, workouts, TODAY)
        by_date = {p["date"]: p for p in points}
        self.assertEqual(by_date[_d(1)]["load"], 80.0)
        self.assertEqual(by_date[_d(1)]["source"], "planned")
        self.assertEqual(by_date[_d(2)]["load"], 60.0)

    def test_removed_workout_excluded_from_planned_load(self):
        activities = [_act(-1, tss=40.0)]
        # A later non-removed workout establishes plan end; the removed one on
        # day 1 must not contribute load even though it falls inside the window.
        workouts = [_w(1, tss=80, removed=True), _w(3, tss=50)]
        points = progression.daily_loads(activities, workouts, TODAY)
        by_date = {p["date"]: p for p in points}
        self.assertEqual(by_date[_d(1)]["load"], 0.0)

    def test_today_is_actual_when_a_loaded_activity_exists(self):
        activities = [_act(0, tss=55.0)]
        workouts = [_w(0, tss=999)]
        points = progression.daily_loads(activities, workouts, TODAY)
        today_point = next(p for p in points if p["date"] == TODAY)
        self.assertEqual(today_point["source"], "actual")
        self.assertEqual(today_point["load"], 55.0)

    def test_today_falls_back_to_planned_when_activity_has_zero_load(self):
        # A zero-load activity (no power/HR/RPE) must not suppress a planned
        # session — today reads planned instead (§3).
        activities = [_act(0, tss=None, rpe=None)]
        workouts = [_w(0, tss=70)]
        points = progression.daily_loads(activities, workouts, TODAY)
        today_point = next(p for p in points if p["date"] == TODAY)
        self.assertEqual(today_point["source"], "planned")
        self.assertEqual(today_point["load"], 70.0)

    def test_plan_end_clamps_the_future_half(self):
        activities = [_act(-1, tss=40.0)]
        workouts = [_w(2, tss=50), _w(5, tss=50, removed=True)]  # plan end = day 2
        points = progression.daily_loads(activities, workouts, TODAY)
        self.assertEqual(points[-1]["date"], _d(2))

    def test_lapsed_plan_stops_at_today_not_plan_end(self):
        # plan end is in the past relative to today -> window still ends today.
        activities = [_act(-10, tss=40.0)]
        workouts = [_w(-5, tss=50)]
        points = progression.daily_loads(activities, workouts, TODAY)
        self.assertEqual(points[-1]["date"], TODAY)


class TestFitnessSeries(unittest.TestCase):
    def test_empty_in_empty_out(self):
        self.assertEqual(progression.fitness_series([]), [])

    def test_seeds_at_calendar_mean_when_history_shorter_than_windows(self):
        # 3 days of history: mean over what exists (not divided by 42/7).
        loads = [10.0, 0.0, 20.0]  # mean = 10.0
        points = [{"date": _d(i), "load": l, "source": "actual"} for i, l in enumerate(loads)]
        out = progression.fitness_series(points)
        # Day 0 TSB uses the seed for both CTL and ATL -> 0.
        self.assertAlmostEqual(out[0]["tsb"], 0.0)
        expected_ctl0 = 10.0 + (10.0 - 10.0) / 42
        expected_atl0 = 10.0 + (10.0 - 10.0) / 7
        self.assertAlmostEqual(out[0]["ctl"], expected_ctl0)
        self.assertAlmostEqual(out[0]["atl"], expected_atl0)

    def test_recursion_matches_hand_computation(self):
        loads = [50.0, 50.0, 100.0]
        points = [{"date": _d(i), "load": l, "source": "actual"} for i, l in enumerate(loads)]
        out = progression.fitness_series(points)

        ctl = sum(loads) / len(loads)  # seed uses full window since len < 42
        atl = sum(loads) / len(loads)  # len < 7 too
        expected = []
        for load in loads:
            tsb = ctl - atl
            ctl = ctl + (load - ctl) / 42
            atl = atl + (load - atl) / 7
            expected.append((ctl, atl, tsb))

        for point, (ctl_e, atl_e, tsb_e) in zip(out, expected):
            self.assertAlmostEqual(point["ctl"], ctl_e)
            self.assertAlmostEqual(point["atl"], atl_e)
            self.assertAlmostEqual(point["tsb"], tsb_e)

    def test_seed_window_capped_at_42_and_7_days(self):
        # 50 days of load=10 followed by one day of load=100: CTL seed must be
        # the mean of only the first 42 days (all 10s) => seed exactly 10, not
        # diluted/inflated by the day-50 spike, and ATL seed the first 7 (10s).
        loads = [10.0] * 50 + [100.0]
        points = [{"date": _d(i), "load": l, "source": "actual"} for i, l in enumerate(loads)]
        out = progression.fitness_series(points)
        self.assertAlmostEqual(out[0]["ctl"], 10.0 + (10.0 - 10.0) / 42)
        self.assertAlmostEqual(out[0]["atl"], 10.0 + (10.0 - 10.0) / 7)


class TestZeroLoadWorkoutCount(unittest.TestCase):
    def test_counts_only_non_rest_workouts_with_no_valuation(self):
        workouts = [
            _w(1, sport_type="rest"),                       # rest: excluded even though 0 load
            _w(2, tss=None, rpe=None, duration_minutes=None),  # no tss, no rpe/duration -> counted
            _w(3, tss=50),                                   # valued -> not counted
            _w(4, rpe=5, duration_minutes=30),                # sRPE valued -> not counted
            _w(5, tss=None, rpe=None, duration_minutes=None, removed=True),  # removed -> excluded
        ]
        self.assertEqual(progression.zero_load_workout_count(workouts), 1)


class TestWeeklyAggregates(unittest.TestCase):
    def _meso_span(self, start_offset, end_offset, label="Build 2", source="plan"):
        return {
            "label": label, "source": source,
            "start_date": _d(start_offset), "end_date": _d(end_offset),
        }

    def test_empty_without_activities_or_workouts(self):
        self.assertEqual(progression.weekly_aggregates([], [], TODAY, []), [])

    def test_monday_bucketing_and_actual_sum(self):
        # TODAY (_d(0)) is 2026-07-03, a Friday; the week commences 2026-06-29 (Monday).
        activities = [_act(-4, tss=30.0), _act(-3, tss=20.0)]  # Monday + Tuesday of that week
        weeks = progression.weekly_aggregates(activities, [], TODAY, [])
        self.assertEqual(weeks[0]["week_commencing"], "2026-06-29")
        self.assertEqual(weeks[0]["actual_load"], 50.0)

    def test_in_progress_week_carries_elapsed_planned_split(self):
        meso_spans = [self._meso_span(-4, 10)]
        # Full week: Mon(-4) .. Sun(2); today is Fri (offset 0).
        workouts = [
            _w(-4, tss=40), _w(-3, tss=40), _w(-2, tss=40), _w(-1, tss=40),  # Mon-Thu (past)
            _w(0, tss=40),   # Fri (today)
            _w(1, tss=40), _w(2, tss=40),  # Sat, Sun (not yet elapsed)
        ]
        weeks = progression.weekly_aggregates([], workouts, TODAY, meso_spans)
        this_week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertTrue(this_week["in_progress"])
        self.assertEqual(this_week["planned_load"], 40 * 7)
        self.assertEqual(this_week["planned_load_elapsed"], 40 * 5)  # Mon..Fri inclusive

    def test_ungoverned_week_has_no_planned_figure(self):
        # Actual load exists but no 'plan'-sourced meso span covers the week
        # (pre-adoption history) -> planned_load is None, not 0.
        activities = [_act(-4, tss=30.0)]
        weeks = progression.weekly_aggregates(activities, [], TODAY, [])
        week = weeks[0]
        self.assertIsNone(week["planned_load"])
        self.assertEqual(week["actual_load"], 30.0)

    def test_governed_week_sums_planned_load(self):
        meso_spans = [self._meso_span(-4, 2)]
        workouts = [_w(-4, tss=40), _w(-3, tss=60)]
        weeks = progression.weekly_aggregates([], workouts, TODAY, meso_spans)
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertEqual(week["planned_load"], 100.0)
        self.assertEqual(week["meso_source"], "plan")

    def test_majority_overlap_labeling_with_tiebreak_to_later_block(self):
        # Week 2026-06-29..07-05. Split evenly 3-and-a-half/3-and-a-half isn't
        # possible over 7 days, so use a clean majority case plus an exact tie.
        activities = [_act(-4, tss=10.0)]
        majority_span = self._meso_span(-4, -2, label="A")  # covers Mon-Wed (3 days)
        minority_span = {
            "label": "B", "source": "plan", "start_date": _d(-1), "end_date": _d(-1),
        }  # covers only Thu (1 day)
        weeks = progression.weekly_aggregates(
            activities, [], TODAY, [majority_span, minority_span]
        )
        week = weeks[0]
        self.assertEqual(week["meso_label"], "A")

        # Exact tie (3 vs 3 days out of the 6 spanned): later block (given
        # later in the chronological list) wins.
        tie_a = self._meso_span(-4, -2, label="A")     # Mon-Wed
        tie_b = self._meso_span(-1, 1, label="B")      # Thu-Sat
        weeks = progression.weekly_aggregates(activities, [], TODAY, [tie_a, tie_b])
        self.assertEqual(weeks[0]["meso_label"], "B")

    def test_no_matching_span_leaves_week_unlabeled(self):
        activities = [_act(-4, tss=10.0)]
        weeks = progression.weekly_aggregates(activities, [], TODAY, [])
        self.assertIsNone(weeks[0]["meso_label"])
        self.assertIsNone(weeks[0]["meso_source"])


class TestMesoBands(unittest.TestCase):
    def test_orders_inferred_before_plan_and_prefixes_label(self):
        inferred = [{"name": "Base", "start_date": _d(-60), "end_date": _d(-30)}]
        real = [{"name": "Build 2", "start_date": _d(-10), "end_date": _d(10)}]
        bands = progression.meso_bands(real, inferred)
        self.assertEqual(bands[0]["label"], "~Base")
        self.assertEqual(bands[0]["source"], "inferred")
        self.assertEqual(bands[1]["label"], "Build 2")
        self.assertEqual(bands[1]["source"], "plan")


if __name__ == "__main__":
    unittest.main()
