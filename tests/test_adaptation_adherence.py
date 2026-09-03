import os
import unittest
from datetime import date

from tests.helpers import clear_all_tables, rebind_test_db
from trainmate.adherence import analyze_adherence, format_discrepancies
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_adaptation_adherence.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service


class TestAdaptationAdherence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_analyze_adherence_direct(self):
        planned = [
            {
                "date": "2026-06-01",
                "sport_type": "running",
                "title": "Run",
                "duration_minutes": 30,
                "rpe": 5,
                "tss": 25,
            },
            {
                "date": "2026-06-02",
                "sport_type": "rest",
                "title": "Rest Day",
                "duration_minutes": 0,
                "rpe": 0,
                "tss": 0,
            },
            {
                "date": "2026-06-03",
                "sport_type": "cycling",
                "title": "Ride",
                "duration_minutes": 60,
                "rpe": 6,
                "tss": 40,
            },
        ]

        completed = [
            # June 1: workload mismatch (planned load 27.5, actual load 64)
            {
                "date": "2026-06-01",
                "activity_id": "act1",
                "activity_name": "Hard Run",
                "activity_type": "running",
                "duration_sec": 1800,
                "rpe": 8,
                "tss": 60.0,
            },
            # June 2: rest day violation (workload 15.0 > threshold 10.0)
            {
                "date": "2026-06-02",
                "activity_id": "act2",
                "activity_name": "Lawn Mowing",
                "activity_type": "walking",
                "duration_sec": 3600,
                "rpe": 5,
                "tss": 10.0,
            },
            # June 4: unplanned activity (workload 30.0 > threshold 10.0)
            {
                "date": "2026-06-04",
                "activity_id": "act4",
                "activity_name": "Extra Run",
                "activity_type": "running",
                "duration_sec": 1800,
                "rpe": 6,
                "tss": 27.0,
            },
        ]

        discrepancies, matching, _ = analyze_adherence(
            planned_workouts=planned,
            completed_activities=completed,
            start_date_obj=date(2026, 6, 1),
            history_days=4,
            minor_activity_load_threshold=10.0,
        )

        self.assertEqual(len(discrepancies), 4)
        # The facts, by kind — no sentence parsing needed.
        by_kind = {d.kind: d for d in discrepancies}
        self.assertEqual(set(by_kind), {"partial", "rest_violation", "missed", "unplanned"})
        self.assertIn("workload mismatch", " ".join(by_kind["partial"].reasons))
        self.assertEqual(by_kind["rest_violation"].completed["activity_name"], "Lawn Mowing")
        self.assertEqual(by_kind["missed"].planned["title"], "Ride")
        self.assertEqual(by_kind["unplanned"].completed["activity_name"], "Extra Run")

        # The wording the athlete and the coaching prompt still see.
        lines = format_discrepancies(discrepancies)
        self.assertTrue(any("Rest Day Violation! Performed 'Lawn Mowing'" in l for l in lines))
        self.assertTrue(
            any("Complete Miss! Missed planned workout 'Ride'" in l for l in lines)
        )
        self.assertTrue(any("Unplanned Activity! Performed 'Extra Run'" in l for l in lines))

    def test_classify_adherence_statuses(self):
        from trainmate.adherence import classify_adherence

        run = {"sport_type": "running", "title": "Run",
               "duration_minutes": 30, "rpe": 5, "tss": 25}
        rest = {"sport_type": "rest", "title": "Rest",
                "duration_minutes": 0, "rpe": 0, "tss": 0}

        def act(tss, dur_sec):
            return {"activity_id": "a", "activity_name": "X", "activity_type": "running",
                    "duration_sec": dur_sec, "rpe": 6, "tss": tss}

        # Non-rest, nothing matched -> missed.
        v = classify_adherence(run, None)
        self.assertEqual(v["status"], "missed")

        # Non-rest, matched within tolerance -> done (no reasons).
        v = classify_adherence(run, act(25.0, 1800))
        self.assertEqual(v["status"], "done")
        self.assertEqual(v["reasons"], [])

        # Non-rest, matched but workload way off -> partial (with reasons).
        v = classify_adherence(run, act(120.0, 1800))
        self.assertEqual(v["status"], "partial")
        self.assertTrue(v["reasons"])

        # Rest planned, no activity -> rest_ok.
        self.assertEqual(classify_adherence(rest, None)["status"], "rest_ok")

        # Rest planned, significant activity -> rest_violation.
        v = classify_adherence(rest, act(60.0, 3600), minor_activity_load_threshold=10.0)
        self.assertEqual(v["status"], "rest_violation")

    def test_analyze_adherence_coverage_gates_unplanned(self):
        """An activity with no planned workout is an 'Unplanned Activity!' deviation only
        when its date falls inside a planned block; outside all coverage it is softened
        to an informational note instead."""
        completed = [{
            "date": "2026-06-04",
            "activity_id": "act4",
            "activity_name": "Extra Run",
            "activity_type": "running",
            "duration_sec": 1800,
            "rpe": 6,
            "tss": 27.0,
        }]

        # Covered: 2026-06-04 sits inside the planned block -> deviation.
        disc, _, info = analyze_adherence(
            planned_workouts=[],
            completed_activities=completed,
            start_date_obj=date(2026, 6, 1),
            history_days=7,
            minor_activity_load_threshold=10.0,
            covered_ranges=[("2026-06-01", "2026-06-30")],
        )
        self.assertTrue(any(d.kind == "unplanned" and
                            d.completed["activity_name"] == "Extra Run" for d in disc))
        self.assertEqual(info, [])

        # Uncovered: the block starts after the activity -> informational, not a deviation.
        disc, _, info = analyze_adherence(
            planned_workouts=[],
            completed_activities=completed,
            start_date_obj=date(2026, 6, 1),
            history_days=7,
            minor_activity_load_threshold=10.0,
            covered_ranges=[("2026-06-10", "2026-06-30")],
        )
        self.assertEqual(disc, [])
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["activity_name"], "Extra Run")

        # No coverage at all (cold start) -> informational.
        disc, _, info = analyze_adherence(
            planned_workouts=[],
            completed_activities=completed,
            start_date_obj=date(2026, 6, 1),
            history_days=7,
            minor_activity_load_threshold=10.0,
            covered_ranges=[],
        )
        self.assertEqual(disc, [])
        self.assertEqual(len(info), 1)

    def test_analyze_adherence_variable_tolerance(self):
        # 1. Low expected load (exp_load = 10.0 <= 20.0, tolerance = 50%)
        planned_low = [{
            "date": "2026-06-01",
            "sport_type": "running",
            "title": "Easy Run",
            "duration_minutes": 30,
            "rpe": 2,
            "tss": 10,  # exp_load = planned tss = 10.0
        }]
        # 46.7% duration deviation (30 -> 44 minutes) is within 50%
        completed_low_ok = [{
            "date": "2026-06-01",
            "activity_id": "act_low_ok",
            "activity_name": "Easy Run Actual",
            "activity_type": "running",
            "duration_sec": 44 * 60,
            "rpe": 2,
            "tss": 10.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_low, completed_low_ok, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 0, f"Expected no discrepancies, got {disc}")

        # 53.3% duration deviation (30 -> 46 minutes) is outside 50%
        completed_low_err = [{
            "date": "2026-06-01",
            "activity_id": "act_low_err",
            "activity_name": "Easy Run Actual",
            "activity_type": "running",
            "duration_sec": 46 * 60,
            "rpe": 2,
            "tss": 10.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_low, completed_low_err, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 1)
        self.assertIn("duration mismatch", " ".join(disc[0].reasons))

        # 2. High expected load (exp_load = 110.0 >= 100.0, tolerance = 15%)
        planned_high = [{
            "date": "2026-06-01",
            "sport_type": "running",
            "title": "Long Run",
            "duration_minutes": 120,
            "rpe": 6,
            "tss": 110,  # exp_load = planned tss = 110.0
        }]
        # 16.7% duration deviation (120 -> 140 minutes) is outside 15%
        completed_high_err = [{
            "date": "2026-06-01",
            "activity_id": "act_high_err",
            "activity_name": "Long Run Actual",
            "activity_type": "running",
            "duration_sec": 140 * 60,
            "rpe": 6,
            "tss": 110.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_high, completed_high_err, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 1)
        self.assertIn("duration mismatch", " ".join(disc[0].reasons))

        # 3. Intermediate expected load (exp_load = 60.0, tolerance = 32.5%)
        planned_mid = [{
            "date": "2026-06-01",
            "sport_type": "running",
            "title": "Tempo",
            "duration_minutes": 60,
            "rpe": 5,
            "tss": 60,  # exp_load = planned tss = 60.0
        }]
        # 30% duration deviation (60 -> 78 minutes) is within 32.5%
        completed_mid_ok = [{
            "date": "2026-06-01",
            "activity_id": "act_mid_ok",
            "activity_name": "Tempo Actual",
            "activity_type": "running",
            "duration_sec": 78 * 60,
            "rpe": 5,
            "tss": 60.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_mid, completed_mid_ok, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 0, f"Expected no discrepancies, got {disc}")

        # 35% duration deviation (60 -> 81 minutes) is outside 32.5%
        completed_mid_err = [{
            "date": "2026-06-01",
            "activity_id": "act_mid_err",
            "activity_name": "Tempo Actual",
            "activity_type": "running",
            "duration_sec": 81 * 60,
            "rpe": 5,
            "tss": 60.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_mid, completed_mid_err, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 1)
        self.assertIn("duration mismatch", " ".join(disc[0].reasons))

        # 4. Zero expected load (exp_load = 0.0, tolerance = 50%)
        planned_zero = [{
            "date": "2026-06-01",
            "sport_type": "running",
            "title": "Untargeted Workout",
            "duration_minutes": 60,
            "rpe": 0,
            "tss": 0,
        }]
        # 46.7% duration deviation (60 -> 88 minutes) is within 50%
        completed_zero_ok = [{
            "date": "2026-06-01",
            "activity_id": "act_zero_ok",
            "activity_name": "Workout Actual",
            "activity_type": "running",
            "duration_sec": 88 * 60,
            "rpe": 0,
            "tss": 0.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_zero, completed_zero_ok, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 0, f"Expected no discrepancies, got {disc}")

        # 53.3% duration deviation (60 -> 92 minutes) is outside 50%
        completed_zero_err = [{
            "date": "2026-06-01",
            "activity_id": "act_zero_err",
            "activity_name": "Workout Actual",
            "activity_type": "running",
            "duration_sec": 92 * 60,
            "rpe": 0,
            "tss": 0.0,
        }]
        disc, _, _ = analyze_adherence(
            planned_zero, completed_zero_err, date(2026, 6, 1), 1
        )
        self.assertEqual(len(disc), 1)
        self.assertIn("duration mismatch", " ".join(disc[0].reasons))


class TestSportMatching(unittest.TestCase):
    """Pairing a planned session with its activity across sport-name spellings.

    A workout's `sport_type` is athlete- or LLM-authored, so it arrives in any casing
    and under any alias. Failing to canonicalize it reports a session the athlete
    actually completed as a Complete Miss."""

    @staticmethod
    def _pair(planned_sport: str, activity_type: str):
        planned = [{
            "date": "2026-06-01", "sport_type": planned_sport, "title": "Session",
            "duration_minutes": 60, "tss": 50.0, "rpe": 5,
        }]
        completed = [{
            "date": "2026-06-01", "activity_id": "act1", "activity_name": "Session",
            "activity_type": activity_type, "duration_sec": 3600, "tss": 50.0, "rpe": 5,
        }]
        return analyze_adherence(planned, completed, date(2026, 6, 1), 1)

    def test_exact_sport_matches(self):
        disc, matching, _ = self._pair("running", "running")
        self.assertIsNotNone(matching[0]["completed"])
        self.assertEqual(disc, [])

    def test_capitalized_sport_still_matches(self):
        # 'Running' is not a SPORT_MAPPING key, so uncanonicalized this read as a
        # Complete Miss.
        disc, matching, _ = self._pair("Running", "running")
        self.assertIsNotNone(matching[0]["completed"])
        self.assertEqual(disc, [])

    def test_alias_sport_matches_its_family(self):
        disc, matching, _ = self._pair("strength", "strength_training")
        self.assertIsNotNone(matching[0]["completed"])
        self.assertEqual(disc, [])

    def test_alias_on_the_activity_side_matches_too(self):
        # Garmin spells resort skiing several ways; all fold into downhill_skiing.
        disc, matching, _ = self._pair("downhill_skiing", "resort_skiing")
        self.assertIsNotNone(matching[0]["completed"])
        self.assertEqual(disc, [])

    def test_a_genuinely_different_sport_is_still_a_miss(self):
        # Canonicalizing must not make everything match.
        disc, matching, _ = self._pair("running", "cycling")
        self.assertIsNone(matching[0]["completed"])
        self.assertTrue(any(d.kind == "missed" for d in disc))

    def test_an_unknown_type_sharing_a_word_is_not_that_sport(self):
        # `e_bike_fitness` contains "fitness", an alias of strength_training, and the
        # old substring net paired a 34-minute e-bike ride with a 10-minute circuit.
        disc, matching, _ = self._pair("strength_training", "e_bike_fitness")
        self.assertIsNone(matching[0]["completed"])
        self.assertTrue(any(d.kind == "missed" for d in disc))

    def test_a_resort_day_is_not_a_ski_tour(self):
        # Every downhill_skiing alias ends in "skiing", which ski_touring also lists.
        # The two are kept apart on purpose (sports.py), and matching must respect that.
        disc, matching, _ = self._pair("ski_touring", "resort_skiing")
        self.assertIsNone(matching[0]["completed"])
        self.assertTrue(any(d.kind == "missed" for d in disc))

    def test_capitalized_rest_is_still_a_rest_day(self):
        planned = [{"date": "2026-06-01", "sport_type": "Rest", "title": "Rest"}]
        completed = [{
            "date": "2026-06-01", "activity_id": "a1", "activity_name": "Big Ride",
            "activity_type": "cycling", "duration_sec": 7200, "tss": 120.0, "rpe": 7,
        }]
        disc, _, _ = analyze_adherence(planned, completed, date(2026, 6, 1), 1)
        self.assertTrue(any(d.kind == "rest_violation" for d in disc))


class TestPendingSessions(unittest.TestCase):
    """A planned session on a day that is not over yet is PENDING, not missed.

    `workout adapt` runs in the morning, so the evaluation date's own sessions are
    still ahead of the athlete. Reporting them as misses told the coach work had been
    skipped and invited it to reschedule sessions nobody had skipped — that is how a
    benchmark ended up scheduled twice. Withdrawing a session is `workout remove`;
    saying it won't happen is the adapt note.
    """

    @staticmethod
    def _run(pending_from=None):
        planned = [
            {"date": "2026-06-01", "sport_type": "running", "title": "Yesterday Run",
             "duration_minutes": 60, "tss": 50.0, "rpe": 5},
            {"date": "2026-06-02", "sport_type": "running", "title": "Today Run",
             "duration_minutes": 60, "tss": 50.0, "rpe": 5},
        ]
        return analyze_adherence(
            planned, [], date(2026, 6, 1), 2, pending_from=pending_from
        )

    def test_without_pending_from_every_date_is_final(self):
        disc, matching, _ = self._run()
        self.assertEqual([d.date for d in disc if d.kind == "missed"],
                         ["2026-06-01", "2026-06-02"])
        self.assertFalse(any(r["pending"] for r in matching))

    def test_evaluation_date_is_pending_and_earlier_days_still_miss(self):
        disc, matching, _ = self._run(pending_from="2026-06-02")
        self.assertEqual([d.date for d in disc if d.kind == "missed"], ["2026-06-01"])
        self.assertEqual(
            {r["date"]: r["pending"] for r in matching},
            {"2026-06-01": False, "2026-06-02": True},
        )

    def test_a_completed_session_on_the_pending_date_is_not_pending(self):
        planned = [{"date": "2026-06-02", "sport_type": "running", "title": "Today Run",
                    "duration_minutes": 60, "tss": 50.0, "rpe": 5}]
        completed = [{"date": "2026-06-02", "activity_id": "a1", "activity_name": "Run",
                      "activity_type": "running", "duration_sec": 3600, "tss": 50.0,
                      "rpe": 5}]
        disc, matching, _ = analyze_adherence(
            planned, completed, date(2026, 6, 2), 1, pending_from="2026-06-02"
        )
        self.assertEqual(disc, [])
        self.assertFalse(matching[0]["pending"])
        self.assertIsNotNone(matching[0]["completed"])

    def test_pending_does_not_mask_a_rest_violation(self):
        # The athlete DID train — that is a fact about today, not a prediction.
        planned = [{"date": "2026-06-02", "sport_type": "rest", "title": "Rest"}]
        completed = [{"date": "2026-06-02", "activity_id": "a1", "activity_name": "Ride",
                      "activity_type": "cycling", "duration_sec": 7200, "tss": 120.0,
                      "rpe": 7}]
        disc, _, _ = analyze_adherence(
            planned, completed, date(2026, 6, 2), 1, pending_from="2026-06-02"
        )
        self.assertTrue(any(d.kind == "rest_violation" for d in disc))

    def test_a_partially_executed_session_today_still_reports(self):
        # Short of plan but done: real evidence, not a prediction about the rest of the day.
        planned = [{"date": "2026-06-02", "sport_type": "running", "title": "Long Run",
                    "duration_minutes": 120, "tss": 120.0, "rpe": 6}]
        completed = [{"date": "2026-06-02", "activity_id": "a1", "activity_name": "Run",
                      "activity_type": "running", "duration_sec": 900, "tss": 12.0,
                      "rpe": 3}]
        disc, matching, _ = analyze_adherence(
            planned, completed, date(2026, 6, 2), 1, pending_from="2026-06-02"
        )
        self.assertTrue(any(d.kind == "partial" for d in disc))
        self.assertFalse(matching[0]["pending"])


class TestClassifyPending(unittest.TestCase):
    def test_pending_flag_changes_the_verdict_only_when_unmatched(self):
        from trainmate.adherence import classify_adherence
        run = {"sport_type": "running", "title": "Run", "duration_minutes": 60,
               "tss": 50.0, "rpe": 5}
        self.assertEqual(classify_adherence(run, None)["status"], "missed")
        self.assertEqual(
            classify_adherence(run, None, pending=True)["status"], "pending"
        )
        act = {"activity_id": "a1", "activity_type": "running", "duration_sec": 3600,
               "tss": 50.0, "rpe": 5}
        self.assertEqual(
            classify_adherence(run, act, pending=True)["status"], "done"
        )

    def test_a_rest_day_is_never_pending(self):
        from trainmate.adherence import classify_adherence
        rest = {"sport_type": "rest", "title": "Rest"}
        self.assertEqual(
            classify_adherence(rest, None, pending=True)["status"], "rest_ok"
        )
