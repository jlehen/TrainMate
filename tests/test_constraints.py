"""Tests for the unified `constraint` directive object (DESIGN_constraints.md):
DB windowing, the deterministic hard-rest pre-pass shared by generate/adapt, the §7
plan-magnitude heuristic, and §8 message classification into a constraint row.
"""
import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_constraints.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.service.db = test_db

from trainmate.coach import coach_service


class TestConstraintDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.service.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_get_constraints_window(self):
        a = test_db.add_constraint(title="A", start_date="2026-07-01", end_date="2026-07-03")
        b = test_db.add_constraint(title="B", start_date="2026-07-10", end_date="2026-07-12")
        # Open-ended (plan form): everything still active on/after the date.
        got = [c["id"] for c in test_db.get_constraints("2026-07-05")]
        self.assertEqual(got, [b])
        # Bounded window: only overlapping rows.
        got = [c["id"] for c in test_db.get_constraints("2026-07-02", "2026-07-04")]
        self.assertEqual(got, [a])
        # No bound: all rows, oldest first.
        self.assertEqual([c["id"] for c in test_db.get_constraints()], [a, b])

    def test_list_constraint_types(self):
        test_db.add_constraint(title="x", start_date="2026-07-01", end_date="2026-07-01",
                               type="trip")
        test_db.add_constraint(title="y", start_date="2026-07-02", end_date="2026-07-02",
                               type="trip")
        test_db.add_constraint(title="z", start_date="2026-07-03", end_date="2026-07-03")
        types = {t["type"]: t["count"] for t in test_db.list_constraint_types()}
        self.assertEqual(types, {"trip": 2})  # NULL type excluded


class TestHardConstraintPrePass(unittest.TestCase):
    def _c(self, binding, sport=None, start="2026-07-02", end="2026-07-02", title="c"):
        return {"binding": binding, "sport": sport, "start_date": start,
                "end_date": end, "title": title}

    def test_generate_hard_no_sport_forces_rest(self):
        workouts = [
            {"date": "2026-07-02", "sport_type": "running", "title": "Tempo",
             "duration_minutes": 60, "rpe": 7, "tss": 80},
            {"date": "2026-07-03", "sport_type": "running", "title": "Easy",
             "duration_minutes": 40, "rpe": 3, "tss": 30},
        ]
        out = coach_service._enforce_hard_constraints_generate(
            workouts, [self._c("hard")]
        )
        by_date = {w["date"]: w for w in out}
        self.assertEqual(by_date["2026-07-02"]["sport_type"], "rest")
        # Other dates untouched.
        self.assertEqual(by_date["2026-07-03"]["sport_type"], "running")

    def test_generate_hard_sport_is_advisory_not_enforced(self):
        """A `hard` constraint scoped to a sport is advisory — rendered into the prompt
        as a hard instruction, but left to the LLM to honor (§5/§6 rev3 narrowing). Only
        a blanket `hard` (no-sport) window is deterministically enforced."""
        workouts = [
            {"date": "2026-07-02", "sport_type": "running", "title": "Run"},
            {"date": "2026-07-02", "sport_type": "strength_training", "title": "Lift"},
        ]
        out = coach_service._enforce_hard_constraints_generate(
            workouts, [self._c("hard", sport="running")]
        )
        self.assertEqual(out, workouts)

    def test_generate_soft_is_untouched(self):
        workouts = [{"date": "2026-07-02", "sport_type": "running", "title": "Run"}]
        out = coach_service._enforce_hard_constraints_generate(
            workouts, [self._c("soft")]
        )
        self.assertEqual(out, workouts)

    def test_adapt_hard_no_sport_eases_planned_session_to_rest(self):
        planned = [{"date": "2026-07-02", "sport_type": "running", "title": "Tempo"}]
        # LLM proposed nothing; the pre-pass must still add a rest for the hard date.
        out = coach_service._enforce_hard_constraints_adapt(
            [], planned, [self._c("hard")], completed_keys=set(), from_date="2026-07-02"
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sport_type"], "rest")
        self.assertIn("change_reason", out[0])

    def test_adapt_hard_sport_is_advisory_not_enforced(self):
        """A `hard` constraint scoped to a sport does not touch the LLM's proposal —
        only a blanket hard (no-sport) window is eased to rest deterministically (§5)."""
        planned = [{"date": "2026-07-02", "sport_type": "running", "title": "Tempo"}]
        proposed = [{"date": "2026-07-02", "sport_type": "running", "title": "Easy Run"}]
        out = coach_service._enforce_hard_constraints_adapt(
            proposed, planned, [self._c("hard", sport="running")],
            completed_keys=set(), from_date="2026-07-02"
        )
        self.assertEqual(out, proposed)

    def test_adapt_skips_completed_and_past_sessions(self):
        from trainmate.sports import canonical_sport
        planned = [
            {"date": "2026-07-01", "sport_type": "running", "title": "Past"},   # before from
            {"date": "2026-07-02", "sport_type": "running", "title": "Done"},   # completed
        ]
        completed = {("2026-07-02", canonical_sport("running"))}
        out = coach_service._enforce_hard_constraints_adapt(
            [], planned, [self._c("hard", start="2026-07-01", end="2026-07-05")],
            completed_keys=completed, from_date="2026-07-02"
        )
        self.assertEqual(out, [])  # nothing to ease


class TestConstraintPlanImpact(unittest.TestCase):
    """The concrete §7 magnitude heuristic: relative displaced-load trigger (vs the
    plan's trailing weekly planned load) OR a hard-window floor — either firing proposes
    a replan. No per-session "importance" term (see DESIGN_constraints.md §7/§11)."""

    @classmethod
    def setUpClass(cls):
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.service.db = test_db

    def setUp(self):
        clear_all_tables(test_db)

    def test_displaced_load_trigger_fires_even_when_soft(self):
        # Trailing week (2026-07-25..2026-07-31, the 7 days before the constraint
        # starts) carries 200 TSS of planned load.
        test_db.save_workout(date="2026-07-28", sport_type="running",
                             title="Long Run", description="", tss=200)
        # The constraint's 3-day window overlaps 120 TSS — 60% of the trailing week,
        # over the default 50% threshold.
        test_db.save_workout(date="2026-08-02", sport_type="running",
                             title="Tempo", description="", tss=120)
        c = {"binding": "soft", "sport": None, "start_date": "2026-08-01",
             "end_date": "2026-08-03", "title": "big trip"}
        impact = coach_service.constraint_plan_impact(c)
        self.assertGreaterEqual(impact["displaced_pct"], 50)
        self.assertTrue(coach_service.constraint_is_plan_shaping(c, impact))

    def test_small_displaced_load_is_not_plan_shaping(self):
        test_db.save_workout(date="2026-07-28", sport_type="running",
                             title="Long Run", description="", tss=200)
        test_db.save_workout(date="2026-08-01", sport_type="running",
                             title="Easy", description="", tss=20)  # 10% of the trailing week
        c = {"binding": "soft", "sport": None, "start_date": "2026-08-01",
             "end_date": "2026-08-01", "title": "no run"}
        impact = coach_service.constraint_plan_impact(c)
        self.assertLess(impact["displaced_pct"], 50)
        self.assertFalse(coach_service.constraint_is_plan_shaping(c, impact))

    def test_hard_window_floor_fires_regardless_of_load(self):
        # No workouts at all — zero displaced load — but a 3-day hard window still
        # trips the independent hard-window floor (default replan_hard_span_days=3).
        c = {"binding": "hard", "sport": None, "start_date": "2026-08-01",
             "end_date": "2026-08-03", "title": "injury"}
        impact = coach_service.constraint_plan_impact(c)
        self.assertEqual(impact["displaced_pct"], 0.0)
        self.assertEqual(impact["days"], 3)
        self.assertTrue(coach_service.constraint_is_plan_shaping(c, impact))

    def test_short_hard_window_below_floor_is_not_plan_shaping(self):
        c = {"binding": "hard", "sport": None, "start_date": "2026-08-01",
             "end_date": "2026-08-01", "title": "no run"}
        impact = coach_service.constraint_plan_impact(c)
        self.assertFalse(coach_service.constraint_is_plan_shaping(c, impact))

    def test_single_soft_day_with_no_history_is_not_plan_shaping(self):
        c = {"binding": "soft", "sport": None, "start_date": "2026-08-01",
             "end_date": "2026-08-01", "title": "no run"}
        impact = coach_service.constraint_plan_impact(c)
        self.assertFalse(coach_service.constraint_is_plan_shaping(c, impact))


class TestMessageCapture(unittest.TestCase):
    """§8: a `workout adapt --message` note is classified by the SAME LLM call that
    evaluates the day (no separate pass) — `new_constraints` comes back raw and
    UNCONFIRMED; only `capture_message_constraint`, called after the CLI confirms with
    the athlete, ever writes a row."""

    @classmethod
    def setUpClass(cls):
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate.coach.service.db = test_db

    def setUp(self):
        clear_all_tables(test_db)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_new_constraints_returned_raw_and_unconfirmed(self, mock_client):
        """The single adapt LLM call may extract constraint candidates alongside the
        adaptation; workout_adapt returns them as-is without writing anything."""
        mock_client.complete.return_value = {
            "change_needed": False,
            "reason": "On track.",
            "adapted_workouts": [],
            "new_constraints": [{
                "title": "can't train Thursday", "start_date": "2026-07-09",
                "end_date": "2026-07-09", "sport": None, "type": None,
                "description": None,
            }],
        }
        test_db.save_metric_cache("2026-07-02", 56, 42, 60, 35, 14.0, 8.0, 1.0)
        test_db.save_baseline("2026-07-02", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

        reason, proposed, new_constraints = coach_service.workout_adapt(
            "2026-07-02", message="can't train Thursday"
        )
        self.assertEqual(len(new_constraints), 1)
        self.assertEqual(new_constraints[0]["title"], "can't train Thursday")
        # Nothing was persisted yet — that's the CLI's job after confirming with the
        # athlete (two-confirmation flow, §8).
        self.assertEqual(test_db.get_constraints(), [])

    @patch("trainmate.coach.engine.openrouter_client")
    def test_no_message_means_no_new_constraints(self, mock_client):
        mock_client.complete.return_value = {
            "change_needed": False,
            "reason": "On track.",
            "adapted_workouts": [],
            "new_constraints": [{"title": "should be ignored", "start_date": "2026-07-09",
                                 "end_date": "2026-07-09"}],
        }
        test_db.save_metric_cache("2026-07-02", 56, 42, 60, 35, 14.0, 8.0, 1.0)
        test_db.save_baseline("2026-07-02", 50.0, 2.0, 60.0, 5.0, 80.0, 5.0)

        _reason, _proposed, new_constraints = coach_service.workout_adapt("2026-07-02")
        self.assertEqual(new_constraints, [])

    def test_capture_creates_row_and_always_forces_soft(self):
        """Trust boundary (§8): the LLM can never mark an extracted constraint `hard` —
        capture_message_constraint ignores any `binding` the candidate might carry."""
        candidate = {
            "title": "can't train Thursday", "start_date": "2026-07-09",
            "end_date": "2026-07-09", "binding": "hard",  # must be ignored
        }
        cid = coach_service.capture_message_constraint(candidate, "2026-07-02")
        self.assertIsNotNone(cid)
        rows = test_db.get_constraints()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "message")
        self.assertEqual(rows[0]["binding"], "soft")
        self.assertEqual(rows[0]["replan"], 0)  # auto-capture never escalates

    def test_capture_defaults_dates_to_default_date(self):
        candidate = {"title": "only 45 min today"}
        cid = coach_service.capture_message_constraint(candidate, "2026-07-02")
        constraint = test_db.get_constraint(cid)
        self.assertEqual(constraint["start_date"], "2026-07-02")
        self.assertEqual(constraint["end_date"], "2026-07-02")

    def test_capture_returns_none_for_blank_title(self):
        cid = coach_service.capture_message_constraint({"title": "  "}, "2026-07-02")
        self.assertIsNone(cid)
        self.assertEqual(test_db.get_constraints(), [])


if __name__ == "__main__":
    unittest.main()
