import os
import unittest
from datetime import date, timedelta

from tests.helpers import rebind_test_db
from trainmate.db import Database
import trainmate.db
import trainmate.garmin as garmin
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_trainmate_progression.db")
test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

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


TODAY = _d(0)  # 2026-07-03, Friday; its week commences 2026-06-29 (Monday)
CTL_DAYS, ATL_DAYS = 42, 7


def _act(offset, tss=None, rpe=None, duration_sec=3600.0, activity_id=None,
         activity_type="cycling"):
    return {
        "activity_id": activity_id or f"act-{offset}-{tss}-{rpe}",
        "date": _d(offset),
        "tss": tss,
        "rpe": rpe,
        "duration_sec": duration_sec,
        "activity_type": activity_type,
    }


def _w(offset, sport_type="running", tss=None, rpe=None, duration_minutes=None,
       removed=False, source="generated"):
    return {
        "date": _d(offset),
        "sport_type": sport_type,
        "tss": tss,
        "rpe": rpe,
        "duration_minutes": duration_minutes,
        "removed": removed,
        "source": source,
    }


def _m(offset, ctl, atl, tsb=0.0):
    return {"date": _d(offset), "ctl": ctl, "atl": atl, "tsb": tsb}


def _dp(offset, load, source="actual"):
    return {"date": _d(offset), "load": float(load), "source": source}


class TestPlanEnd(unittest.TestCase):
    def test_last_generated_workout_wins_over_far_future_manual(self):
        workouts = [_w(2, tss=50), _w(60, tss=50, source="manual")]
        self.assertEqual(progression.plan_end(workouts), _d(2))

    def test_fully_manual_db_falls_back_to_last_non_removed(self):
        workouts = [_w(5, tss=50, source="manual")]
        self.assertEqual(progression.plan_end(workouts), _d(5))

    def test_none_when_no_non_removed_workouts(self):
        self.assertIsNone(progression.plan_end([_w(2, tss=50, removed=True)]))


class TestDailyLoads(unittest.TestCase):
    def test_empty_only_without_activity_and_workouts(self):
        self.assertEqual(progression.daily_loads([], [], TODAY), [])

    def test_planned_future_renders_even_with_no_activity_history(self):
        # §3 empty state: no activities but a plan exists -> series still renders.
        points = progression.daily_loads([], [_w(1, tss=50)], TODAY)
        self.assertTrue(points)
        self.assertEqual(points[-1]["source"], "planned")

    def test_past_day_uses_measured_load_zero_gap_filled(self):
        activities = [_act(-3, tss=40.0), _act(-1, tss=60.0)]
        points = progression.daily_loads(activities, [], TODAY)
        by_date = {p["date"]: p for p in points}
        self.assertEqual(by_date[_d(-3)]["load"], 40.0)
        self.assertEqual(by_date[_d(-2)]["load"], 0.0)
        self.assertEqual(by_date[_d(-2)]["source"], "actual")
        self.assertEqual(points[-1]["date"], TODAY)

    def test_future_day_uses_planned_load_srpe_fallback(self):
        workouts = [_w(1, tss=80), _w(2, rpe=6, duration_minutes=60)]
        points = progression.daily_loads([_act(-1, tss=40.0)], workouts, TODAY)
        by_date = {p["date"]: p for p in points}
        self.assertEqual(by_date[_d(1)]["load"], 80.0)
        self.assertEqual(by_date[_d(1)]["source"], "planned")
        self.assertEqual(by_date[_d(2)]["load"], 60.0)

    def test_explicit_zero_tss_values_to_zero_not_srpe(self):
        # An explicit tss=0 with an rpe present must NOT fall through to sRPE (§3).
        points = progression.daily_loads([_act(-1, tss=40.0)],
                                         [_w(1, tss=0, rpe=6, duration_minutes=60)], TODAY)
        self.assertEqual(next(p for p in points if p["date"] == _d(1))["load"], 0.0)

    def test_today_is_actual_when_a_loaded_activity_exists(self):
        points = progression.daily_loads([_act(0, tss=55.0)], [_w(0, tss=999)], TODAY)
        today_point = next(p for p in points if p["date"] == TODAY)
        self.assertEqual(today_point["source"], "actual")
        self.assertEqual(today_point["load"], 55.0)

    def test_today_falls_back_to_planned_when_activity_has_zero_load(self):
        points = progression.daily_loads([_act(0, tss=None, rpe=None)],
                                         [_w(0, tss=70)], TODAY)
        today_point = next(p for p in points if p["date"] == TODAY)
        self.assertEqual(today_point["source"], "planned")
        self.assertEqual(today_point["load"], 70.0)

    def test_plan_end_clamps_the_future_half(self):
        workouts = [_w(2, tss=50), _w(5, tss=50, removed=True)]
        points = progression.daily_loads([_act(-1, tss=40.0)], workouts, TODAY)
        self.assertEqual(points[-1]["date"], _d(2))

    def test_lapsed_plan_stops_at_today(self):
        points = progression.daily_loads([_act(-10, tss=40.0)], [_w(-5, tss=50)], TODAY)
        self.assertEqual(points[-1]["date"], TODAY)


class TestFitnessSeries(unittest.TestCase):
    def _series(self, day_points, metrics, cutoff=None, ctl_days=CTL_DAYS):
        return progression.fitness_series(
            day_points, metrics, TODAY, ctl_days, ATL_DAYS, cutoff
        )

    def test_empty_in_empty_out(self):
        self.assertEqual(self._series([], []), [])

    def test_past_reads_stored_rows_and_blanks_warmup(self):
        day_points = [_dp(-3, 10), _dp(-2, 10), _dp(-1, 10), _dp(0, 0, "planned")]
        metrics = [_m(-3, 11.1, 22.2, 3.3), _m(-2, 12.0, 20.0, 4.0),
                   _m(-1, 30.0, 40.0, 5.0)]
        out = {p["date"]: p for p in self._series(day_points, metrics, cutoff=_d(-2))}
        # Before the cutoff -> blanked; at/after -> stored verbatim.
        self.assertIsNone(out[_d(-3)]["ctl"])
        self.assertEqual(out[_d(-2)]["ctl"], 12.0)
        self.assertEqual(out[_d(-1)]["tsb"], 5.0)

    def test_anchored_fold_zero_load_tail_matches_closed_form_decay(self):
        # Anchor = stored row at day -1 (ctl 100, atl 50); days 0..3 carry zero load.
        day_points = [_dp(i, 0, "planned") for i in range(0, 4)]
        metrics = [_m(-1, 100.0, 50.0, 7.0)]
        out = {p["date"]: p for p in self._series(day_points, metrics)}
        self.assertAlmostEqual(out[_d(0)]["ctl"], 100.0 * (1 - 1 / 42))
        self.assertAlmostEqual(out[_d(3)]["ctl"], 100.0 * (1 - 1 / 42) ** 4)
        # Day-entering TSB on the first folded day = ctl_A - atl_A.
        self.assertAlmostEqual(out[_d(0)]["tsb"], 50.0)

    def test_seam_continuity_under_non_default_tau(self):
        # τ_ctl = 10: the first folded day uses that τ, no kink at the anchor.
        out = self._series([_dp(0, 80, "planned")], [_m(-1, 40.0, 40.0, 0.0)],
                           ctl_days=10)
        self.assertAlmostEqual(out[0]["ctl"], 40.0 + (80.0 - 40.0) / 10)

    def test_morning_pull_today_row_ignored_as_anchor(self):
        # A stored load-0 row for today must not anchor the fold; today folds the
        # planned session per §3, raising tomorrow's ATL.
        day_points = [_dp(-1, 80), _dp(0, 50, "planned"), _dp(1, 60, "planned")]
        metrics = [_m(-1, 100.0, 50.0, 7.0), _m(0, 0.0, 0.0, 0.0)]
        out = {p["date"]: p for p in self._series(day_points, metrics)}
        self.assertAlmostEqual(out[_d(0)]["ctl"], 100.0 + (50.0 - 100.0) / 42)

    def test_trailing_null_pmc_rows_skipped_for_anchor(self):
        # The interrupted-pull NULL row at -1 is skipped; the anchor is -2, and the
        # fold decays over the actual load on -1.
        day_points = [_dp(-2, 0), _dp(-1, 90), _dp(0, 0, "planned")]
        metrics = [_m(-2, 100.0, 50.0, 7.0), _m(-1, None, None, None)]
        out = {p["date"]: p for p in self._series(day_points, metrics)}
        self.assertAlmostEqual(out[_d(-1)]["ctl"], 100.0 + (90.0 - 100.0) / 42)

    def test_no_anchor_yields_all_null(self):
        out = self._series([_dp(-1, 50), _dp(0, 0, "planned")], [])
        self.assertTrue(all(p["ctl"] is None and p["tsb"] is None for p in out))


class TestZeroLoadWorkoutCount(unittest.TestCase):
    def test_counts_only_non_rest_workouts_with_no_valuation(self):
        workouts = [
            _w(1, sport_type="rest"),
            _w(2, tss=None, rpe=None, duration_minutes=None),   # counted
            _w(3, tss=50),
            _w(4, rpe=5, duration_minutes=30),
            _w(5, tss=None, rpe=None, duration_minutes=None, removed=True),
        ]
        self.assertEqual(progression.zero_load_workout_count(workouts, TODAY), 1)


class TestWeeklyAggregates(unittest.TestCase):
    def _span(self, s, e, label="Build 2", source="plan"):
        return {"label": label, "source": source, "start_date": _d(s), "end_date": _d(e)}

    def test_empty_without_activities_or_workouts(self):
        self.assertEqual(progression.weekly_aggregates([], [], TODAY, []), [])

    def test_monday_bucketing_and_actual_sum(self):
        activities = [_act(-4, tss=30.0), _act(-3, tss=20.0)]  # Mon + Tue
        weeks = progression.weekly_aggregates(activities, [], TODAY, [])
        self.assertEqual(weeks[0]["week_commencing"], "2026-06-29")
        self.assertEqual(weeks[0]["actual_load"], 50.0)

    def test_actual_and_planned_load_split_by_canonical_sport(self):
        # A missed-strength week: cycling on plan, strength entirely skipped —
        # the blended total (105/140, 75%) should not hide that the shortfall is
        # ALL strength, not any cycling shortfall.
        activities = [_act(-4, tss=105.0, activity_type="indoor_cycling")]  # Mon
        workouts = [
            _w(-4, sport_type="cycling", tss=105),
            _w(-3, sport_type="strength", tss=35),  # alias for strength_training
        ]
        weeks = progression.weekly_aggregates(activities, workouts, TODAY, [])
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertEqual(week["actual_load_by_sport"], {"cycling": 105.0})
        self.assertEqual(
            week["planned_load_by_sport"], {"cycling": 105.0, "strength_training": 35.0}
        )

    def test_in_progress_elapsed_by_sport_matches_scalar_cutoff(self):
        workouts = [
            _w(o, sport_type="cycling", tss=40) for o in (-4, -3, -2, -1, 0, 1, 2)
        ]
        weeks = progression.weekly_aggregates([], workouts, TODAY, [])
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        # No activity today -> today not synced -> elapsed is Mon..Thu (yesterday).
        self.assertEqual(week["planned_load_elapsed_by_sport"], {"cycling": 40 * 4})

    def test_judged_sport_seconds_drops_the_sessions_under_the_floor(self):
        # The `!` marker's denominator: a 5-minute session keeps its duration in
        # `sport_seconds` and loses only its vote on the markers (§11).
        short = dict(_act(-4, duration_sec=300.0), activity_type="running")
        real = dict(_act(-3, duration_sec=3600.0), activity_type="cycling")
        week = progression.weekly_aggregates([short, real], [], TODAY, [])[0]
        self.assertEqual(week["sport_seconds"], {"running": 300.0, "cycling": 3600.0})
        self.assertEqual(week["judged_sport_seconds"], {"cycling": 3600.0})

    def test_week_the_plan_never_covered_has_no_planned_figure(self):
        activities = [_act(-4, tss=30.0)]
        weeks = progression.weekly_aggregates(activities, [], TODAY, [])
        self.assertIsNone(weeks[0]["planned_load"])
        self.assertEqual(weeks[0]["actual_load"], 30.0)

    def test_covered_week_sums_planned_load(self):
        workouts = [_w(-4, tss=40), _w(-3, tss=60)]
        weeks = progression.weekly_aggregates([], workouts, TODAY, [])
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertEqual(week["planned_load"], 100.0)

    def test_planned_load_is_label_independent(self):
        # An inferred label must not suppress the planned total (CODE_REVIEW #3).
        spans = [self._span(-4, 2, label="~Base", source="inferred")]
        weeks = progression.weekly_aggregates([], [_w(-4, tss=40)], TODAY, spans)
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertEqual(week["meso_source"], "inferred")
        self.assertEqual(week["planned_load"], 40.0)

    def test_all_removed_week_has_no_planned_figure(self):
        # An activity anchors the week in the series; its only planned row is removed,
        # so nothing survives to compare against — `—`, not a planned zero.
        activities = [_act(-4, tss=10.0)]
        weeks = progression.weekly_aggregates(
            activities, [_w(-4, tss=40, removed=True)], TODAY, []
        )
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertIsNone(week["planned_load"])

    def test_week_the_plan_starts_midway_through_is_partial(self):
        # The plan's first day is Thursday; the days trained before it sit outside the
        # plan entirely, so the pair is not comparable and must not be divided (§3).
        activities = [_act(o, tss=50.0) for o in (-4, -3, -2, -1)]
        workouts = [_w(o, tss=20) for o in (-1, 0, 1, 2)]
        weeks = progression.weekly_aggregates(activities, workouts, TODAY, [])
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertTrue(week["partial_plan"])

    def test_week_the_plan_fully_covers_is_not_partial(self):
        workouts = [_w(o, tss=40) for o in (-4, -3, -2, -1, 0, 1, 2)]  # Mon..Sun
        weeks = progression.weekly_aggregates([], workouts, TODAY, [])
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertNotIn("partial_plan", week)

    def test_in_progress_elapsed_excludes_unsynced_today(self):
        workouts = [_w(o, tss=40) for o in (-4, -3, -2, -1, 0, 1, 2)]  # Mon..Sun
        weeks = progression.weekly_aggregates([], workouts, TODAY, [])
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertTrue(week["in_progress"])
        self.assertEqual(week["planned_load"], 40 * 7)
        # No activity today -> today not synced -> elapsed is Mon..Thu (yesterday).
        self.assertEqual(week["planned_load_elapsed"], 40 * 4)

    def test_in_progress_elapsed_includes_synced_today(self):
        workouts = [_w(o, tss=40) for o in (-4, -3, -2, -1, 0, 1, 2)]
        activities = [_act(0, tss=25.0)]  # today's session has synced
        weeks = progression.weekly_aggregates(activities, workouts, TODAY, [])
        week = next(w for w in weeks if w["week_commencing"] == "2026-06-29")
        self.assertEqual(week["planned_load_elapsed"], 40 * 5)  # Mon..Fri

    def test_majority_overlap_labeling_with_tiebreak_to_later_block(self):
        activities = [_act(-4, tss=10.0)]
        majority = self._span(-4, -2, label="A")   # Mon-Wed
        minority = {"label": "B", "source": "plan",
                    "start_date": _d(-1), "end_date": _d(-1)}
        weeks = progression.weekly_aggregates(activities, [], TODAY, [majority, minority])
        self.assertEqual(weeks[0]["meso_label"], "A")

        tie_a = self._span(-4, -2, label="A")   # Mon-Wed (3)
        tie_b = self._span(-1, 1, label="B")    # Thu-Sat (3)
        weeks = progression.weekly_aggregates(activities, [], TODAY, [tie_a, tie_b])
        self.assertEqual(weeks[0]["meso_label"], "B")

    def test_no_matching_span_leaves_week_unlabeled(self):
        weeks = progression.weekly_aggregates([_act(-4, tss=10.0)], [], TODAY, [])
        self.assertIsNone(weeks[0]["meso_label"])


class TestWeekPlanDenomBySport(unittest.TestCase):
    def test_none_for_uncovered_week(self):
        self.assertIsNone(progression.week_plan_denom_by_sport({"planned_load": None}))

    def test_full_total_for_completed_week(self):
        week = {
            "planned_load": 140.0,
            "planned_load_by_sport": {"cycling": 105.0, "strength_training": 35.0},
            "in_progress": False,
        }
        self.assertEqual(
            progression.week_plan_denom_by_sport(week),
            {"cycling": 105.0, "strength_training": 35.0},
        )

    def test_elapsed_slice_for_in_progress_week(self):
        week = {
            "planned_load": 280.0,
            "planned_load_by_sport": {"cycling": 210.0, "strength_training": 70.0},
            "in_progress": True,
            "planned_load_elapsed_by_sport": {"cycling": 105.0, "strength_training": 35.0},
        }
        self.assertEqual(
            progression.week_plan_denom_by_sport(week),
            {"cycling": 105.0, "strength_training": 35.0},
        )


class TestMesoBands(unittest.TestCase):
    def test_orders_inferred_and_plan_and_prefixes_label(self):
        inferred = [{"name": "Base", "start_date": _d(-60), "end_date": _d(-30)}]
        real = [{"name": "Build 2", "start_date": _d(-10), "end_date": _d(10)}]
        bands = progression.meso_bands(real, inferred)
        labels = {b["label"]: b for b in bands}
        self.assertEqual(labels["~Base"]["source"], "inferred")
        self.assertEqual(labels["Build 2"]["source"], "plan")

    def test_inferred_band_trimmed_where_plan_covers(self):
        inferred = [{"name": "Base", "start_date": _d(-60), "end_date": _d(0)}]
        plan = [{"name": "Build", "start_date": _d(-10), "end_date": _d(10)}]
        bands = progression.meso_bands(plan, inferred)
        inf = next(b for b in bands if b["source"] == "inferred")
        self.assertEqual(inf["end_date"], _d(-11))  # trimmed to the day before plan
        # No two bands overlap.
        self.assertFalse(self._any_overlap(bands))

    def test_fully_covered_inferred_band_dropped(self):
        inferred = [{"name": "X", "start_date": _d(-5), "end_date": _d(5)}]
        plan = [{"name": "P", "start_date": _d(-10), "end_date": _d(10)}]
        bands = progression.meso_bands(plan, inferred)
        self.assertEqual([b["source"] for b in bands], ["plan"])

    @staticmethod
    def _any_overlap(bands):
        s = sorted(bands, key=lambda b: b["start_date"])
        return any(s[i]["end_date"] >= s[i + 1]["start_date"] for i in range(len(s) - 1))


class TestAssembleTimeline(unittest.TestCase):
    def _assemble(self, activities=None, workouts=None, metrics=None,
                  mesocycles=None, inferred=None, objectives=None, cutoff=None):
        return progression.assemble_timeline(
            activities or [], workouts or [], metrics or [], mesocycles or [],
            inferred or [], objectives or [], TODAY, CTL_DAYS, ATL_DAYS, cutoff,
        )

    @staticmethod
    def _codes(payload):
        return {w["code"] for w in payload["warnings"]}

    def test_payload_shape(self):
        p = self._assemble(activities=[_act(-2, tss=30.0)], workouts=[_w(2, tss=50)])
        self.assertEqual(
            set(p.keys()),
            {"today", "plan_start", "plan_end", "days", "weeks", "meso_bands",
             "objectives", "plan_gap", "warnings"},
        )
        self.assertEqual(p["plan_end"], _d(2))

    def test_warnings_carry_a_code_and_text(self):
        # Renderers dispatch on `code`, never on the prose (§6.0).
        p = self._assemble(workouts=[_w(1, tss=50)])
        w = next(w for w in p["warnings"] if w["code"] == "no_history")
        self.assertIn("no activity history", w["text"])
        self.assertEqual(w["command"], "data pull")

    def test_no_activity_state_warns_and_still_has_weeks(self):
        p = self._assemble(workouts=[_w(1, tss=50)])
        self.assertIn("no_history", self._codes(p))
        self.assertTrue(p["weeks"])  # planned bars still render

    def test_beyond_plan_end_warning(self):
        workouts = [_w(2, tss=50), _w(30, tss=50, source="manual")]
        p = self._assemble(activities=[_act(-1, tss=30.0)], workouts=workouts)
        self.assertIn("beyond_plan_end", self._codes(p))

    def test_plan_gap_is_structured_not_a_warning_string(self):
        objectives = [{"id": 1, "title": "Marathon", "target_date": _d(60), "status": "active"}]
        p = self._assemble(activities=[_act(-1, tss=30.0)],
                           workouts=[_w(2, tss=50)], objectives=objectives)
        self.assertEqual(p["plan_gap"]["objective"]["id"], 1)
        self.assertEqual(p["plan_gap"]["plan_end"], _d(2))
        self.assertGreater(p["plan_gap"]["weeks_before"], 0)
        self.assertNotIn("plan_gap", self._codes(p))

    def test_no_plan_gap_when_every_objective_is_reached(self):
        objectives = [{"id": 1, "title": "Marathon", "target_date": _d(1), "status": "active"}]
        p = self._assemble(activities=[_act(-1, tss=30.0)],
                           workouts=[_w(2, tss=50)], objectives=objectives)
        self.assertIsNone(p["plan_gap"])

    def test_zero_load_warning_ignores_past_workouts(self):
        # A past row nobody can fix must not keep the banner permanently lit.
        p = self._assemble(activities=[_act(-1, tss=30.0)],
                           workouts=[_w(-3, tss=None), _w(2, tss=50)])
        self.assertNotIn("zero_load_workouts", self._codes(p))
        p = self._assemble(activities=[_act(-1, tss=30.0)],
                           workouts=[_w(1, tss=None), _w(2, tss=50)])
        self.assertIn("zero_load_workouts", self._codes(p))

    def test_young_db_caveat_in_warnings(self):
        # History starts today -> n_days small -> caveat fires.
        p = self._assemble(activities=[_act(0, tss=30.0)], workouts=[_w(1, tss=50)])
        self.assertIn("pmc_warming", self._codes(p))

    def test_unparseable_inferred_block_skipped_and_warned(self):
        inferred = [{"name": "Bad", "start_date": "not-a-date", "end_date": _d(0)}]
        p = self._assemble(activities=[_act(-1, tss=30.0)], workouts=[_w(2, tss=50)],
                           inferred=inferred)
        self.assertIn("bootstrap_dates", self._codes(p))
        self.assertFalse(any(b["source"] == "inferred" for b in p["meso_bands"]))


class TestClipPayload(unittest.TestCase):
    def test_straddling_week_returned_whole(self):
        payload = {
            "days": [{"date": _d(-10), "load": 1}, {"date": _d(-2), "load": 1}],
            "weeks": [{"week_commencing": _d(-4)}],  # Mon..Sun spans the edge
            "meso_bands": [{"start_date": _d(-40), "end_date": _d(-1)}],
            "objectives": [],
        }
        clipped = progression.clip_payload(payload, _d(-3), _d(0))
        # The week starting _d(-4) straddles the _d(-3) window start -> kept whole.
        self.assertEqual(len(clipped["weeks"]), 1)
        # Days clip by date.
        self.assertEqual([d["date"] for d in clipped["days"]], [_d(-2)])
        # Bands overlapping the window are kept.
        self.assertEqual(len(clipped["meso_bands"]), 1)


class TestClipPayloadForWeeks(unittest.TestCase):
    """`cap_future` is what keeps `tm progress --chart` framing the same span its
    text table does (DESIGN_progress_timeline.md §7.1)."""

    def _payload(self, plan_end):
        days = [{"date": _d(offset), "load": 0.0, "source": "planned",
                 "ctl": 1.0, "atl": 1.0, "tsb": 0.0}
                for offset in range(-40, 90)]
        # Real weeks: the cap is derived from `select_weeks`, the same choice the text
        # table makes, so the fixture has to carry the weeks it chooses from.
        weeks = [{"week_commencing": _d(offset), "actual_load": 0.0,
                  "planned_load": None}
                 for offset in range(-39, 90, 7)]  # _d(-39) is a Monday
        return {"today": TODAY, "plan_end": plan_end, "days": days, "weeks": weeks,
                "objectives": [], "warnings": [], "meso_bands": []}

    def test_default_keeps_the_whole_projection(self):
        payload = self._payload(plan_end=_d(80))
        clipped = progression.clip_payload_for_weeks(payload, 2, TODAY)
        self.assertEqual(clipped["days"][-1]["date"], _d(80))

    def test_cap_future_cuts_the_projection_to_the_window(self):
        payload = self._payload(plan_end=_d(80))
        clipped = progression.clip_payload_for_weeks(
            payload, 2, TODAY, cap_future=True)
        # Sunday of the 2nd whole week after the one containing today.
        self.assertLess(clipped["days"][-1]["date"], _d(80))
        self.assertGreater(clipped["days"][-1]["date"], TODAY)

    def test_cap_future_never_extends_a_short_plan(self):
        payload = self._payload(plan_end=_d(3))
        clipped = progression.clip_payload_for_weeks(
            payload, 8, TODAY, cap_future=True)
        self.assertEqual(clipped["days"][-1]["date"], _d(3))

    def test_cap_future_is_a_no_op_under_weeks_all(self):
        payload = self._payload(plan_end=_d(80))
        capped = progression.clip_payload_for_weeks(
            payload, "all", TODAY, cap_future=True)
        self.assertEqual(capped["days"][-1]["date"], _d(80))


if __name__ == "__main__":
    unittest.main()
