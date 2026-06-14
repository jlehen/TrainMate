import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables
from trainmate.db import Database
import trainmate.db
import trainmate.coach

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_analysis.db")
test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate.coach.service.db = test_db

from trainmate.coach import coach_service


class TestWorkoutAnalysis(unittest.TestCase):
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

    @patch("trainmate.coach.engine.openrouter_client")
    def test_date_resolution_with_preceding_goal(self, mock_client):
        # Earliest objective: 2026-07-01
        test_db.add_objective(
            title="Goal A", target_date="2026-07-01",
            sport_type="running", priority=1, status="active"
        )
        # Preceding objective: 2026-06-01
        test_db.add_objective(
            title="Goal Preceding", target_date="2026-06-01",
            sport_type="running", priority=1, status="active"
        )

        mock_client.complete.return_value = {
            "macrocycle_summary": "Analysis summary",
            "inferred_macrocycle": {"overall_focus": "aerobic base"},
            "inferred_mesocycles": [],
            "physiological_insights": [],
            "learning_updates": []
        }

        # Analyze workouts without explicit range -> should start on 2026-06-02 (day after Goal Preceding)
        result = coach_service.bootstrap_workouts(until_date_str="2026-07-01")
        self.assertIsNotNone(result)

        # Inspect the start date passed to complete call
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-06-01", summaries) # Monday of that week is 2026-06-01 (Tuesday 2026-06-02 is in it)

    @patch("builtins.input", return_value="y")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_date_resolution_relative_days_and_weeks(self, mock_client, _mock_input):
        mock_client.complete.return_value = {
            "macrocycle_summary": "Analysis summary"
        }

        # Last 10 days relative to 2026-06-15
        coach_service.bootstrap_workouts(until_date_str="2026-06-15", days=10)
        # Start date should be 2026-06-06. The Monday of that week is 2026-06-01.
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-06-01", summaries)

        # Last 4 weeks relative to 2026-06-15
        coach_service.bootstrap_workouts(until_date_str="2026-06-15", weeks=4)
        # Start date should be 2026-05-19. The Monday of that week is 2026-05-18.
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-05-18", summaries)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_weekly_aggregation_logic(self, mock_client):
        # Setup completed activities in different weeks
        # Week commencing 2026-06-01
        test_db.save_completed_activity(
            activity_id="act_1", date="2026-06-03", start_time="08:00:00",
            activity_name="Base Ride", activity_type="road_biking",
            duration_sec=7200.0, distance_km=50.0, elevation_gain_m=300.0,
            avg_hr=130, max_hr=150, rpe=5, tss=90.0,
            zone1_sec=3600, zone2_sec=3600
        )
        # Highlight workout in the same week
        test_db.save_completed_activity(
            activity_id="act_2", date="2026-06-05", start_time="09:00:00",
            activity_name="FTP Race Test", activity_type="running",
            duration_sec=3600.0, distance_km=12.0, elevation_gain_m=50.0,
            avg_hr=165, max_hr=180, rpe=9, tss=130.0,
            zone4_sec=1800, zone5_sec=1800
        )
        # Metric cache for that week
        test_db.save_metric_cache(
            date="2026-06-03", rhr=50, hrv=75, sleep_score=85, stress=20,
            acute_workload=300.0, chronic_workload=280.0, acwr=1.07
        )

        mock_client.complete.return_value = {
            "macrocycle_summary": "Simulated aggregation summary",
            "learning_updates": [{"op": "add", "text": "Athlete responds well to FTP tests."}]
        }

        # Analyze the week
        result = coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07"
        )
        self.assertEqual(result["macrocycle_summary"], "Simulated aggregation summary")

        # Verify learnings updated in learnings
        saved_learnings = test_db.get_learnings()
        self.assertEqual(len(saved_learnings), 1)
        self.assertEqual(saved_learnings[0]["text"], "Athlete responds well to FTP tests.")

        # Verify mock complete call payloads
        user_payload = mock_client.complete.call_args[0][1]
        self.assertIn("total_duration_hours\": 3.0", user_payload)
        self.assertIn("total_tss\": 220.0", user_payload)
        self.assertIn("FTP Race Test", user_payload)
        self.assertIn("avg_hrv\": 75.0", user_payload)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_bootstrap_derives_confidence_and_validates_weeks(self, mock_client):
        """The analysis flow passes the window's weeks to the merge layer: an observation
        cited across enough distinct in-window weeks is derived 'established', while a cited
        week outside the analysed window is dropped (DESIGN_evidence_based_confidence.md §6)."""
        # Window spans five Mondays: 2026-05-04 .. 2026-06-01 inclusive.
        in_window = ["2026-05-04", "2026-05-11", "2026-05-18", "2026-05-25", "2026-06-01"]
        mock_client.complete.return_value = {
            "macrocycle_summary": "s",
            "learning_updates": [
                {"op": "add", "text": "Strong aerobic base",
                 "evidence": in_window + ["2026-09-07"]},  # last week is outside the window
            ],
        }
        coach_service.bootstrap_workouts(
            from_date_str="2026-05-04", until_date_str="2026-06-07"
        )
        learning = test_db.get_learnings()[0]
        # Five in-window weeks -> established; the out-of-window week was dropped.
        self.assertEqual(learning["confidence"], "established")
        basis = test_db.get_learning_evidence(learning["id"])
        self.assertEqual(len(basis), 5)
        self.assertNotIn("2026-09-07", {e["week_commencing"] for e in basis})

    def _seed_activity(self):
        test_db.save_completed_activity(
            activity_id="a1", date="2026-06-03", start_time="08:00:00",
            activity_name="Ride", activity_type="road_biking",
            duration_sec=3600.0, distance_km=20.0, elevation_gain_m=100.0,
            avg_hr=130, max_hr=150, rpe=5, tss=60.0,
        )

    @patch("builtins.input", return_value="y")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_reuse_skips_llm_when_evidence_unchanged(self, mock_client, _mock_input):
        """A second bootstrap over unchanged evidence reuses the cached reconstruction
        instead of calling the LLM again (DESIGN_backward_evaluation.md §5). The repeat
        prompts (bootstrap already ran); confirming proceeds into the reuse path."""
        self._seed_activity()
        mock_client.complete.return_value = {
            "macrocycle_summary": "summary",
            "inferred_macrocycle": {"overall_focus": "base"},
            "learning_updates": [],
        }
        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07"
        )
        reused = coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07"
        )
        self.assertEqual(mock_client.complete.call_count, 1)
        self.assertEqual(reused["macrocycle_summary"], "summary")

    @patch("builtins.input", return_value="n")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_repeat_bootstrap_declined_is_a_noop(self, mock_client, _mock_input):
        """A second bootstrap detects the prior run and prompts; declining skips entirely —
        no extra LLM pass, no reflect-watermark reset."""
        self._seed_activity()
        mock_client.complete.return_value = {
            "macrocycle_summary": "summary", "learning_updates": [],
        }
        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07"
        )
        reflect_wm = test_db.get_sync_state("reflect")
        result = coach_service.bootstrap_workouts(
            from_date_str="2026-05-01", until_date_str="2026-05-07"
        )
        self.assertEqual(result, {})
        self.assertEqual(mock_client.complete.call_count, 1)  # second run never reached the LLM
        # The back-dated re-run did not rewind the reflect baseline.
        self.assertEqual(test_db.get_sync_state("reflect"), reflect_wm)

    @patch("builtins.input")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_repeat_bootstrap_under_auto_skips_without_prompting(self, mock_client, mock_input):
        """Under --auto (non-interactive) a repeat bootstrap skips silently rather than
        blocking on a prompt that can never be answered."""
        self._seed_activity()
        mock_client.complete.return_value = {
            "macrocycle_summary": "summary", "learning_updates": [],
        }
        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07", auto=True
        )
        result = coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07", auto=True
        )
        self.assertEqual(result, {})
        self.assertEqual(mock_client.complete.call_count, 1)
        mock_input.assert_not_called()

    @patch("trainmate.coach.engine.openrouter_client")
    def test_force_recompute_cannot_inflate_via_evidence_dedup(self, mock_client):
        """--force recomputes over unchanged evidence, but re-citing an already-counted week
        is a structural no-op: confidence is not ratcheted and recency is not refreshed. The
        per-learning basis owns this (the old suppress_reinforcement flag is gone; §6, §8)."""
        self._seed_activity()  # activity in week commencing 2026-06-01
        mock_client.complete.return_value = {
            "macrocycle_summary": "s",
            "learning_updates": [
                {"op": "add", "text": "Observation", "evidence": ["2026-06-01"]}
            ],
        }
        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07"
        )
        lid = test_db.get_learnings()[0]["id"]
        # Backdate recency; a forced re-run re-citing the SAME week must leave it untouched.
        sentinel = "2000-01-01T00:00:00+00:00"
        with test_db._get_connection() as conn:
            conn.execute(
                "UPDATE coach_learnings SET last_reinforced_at=? WHERE id=?",
                (sentinel, lid),
            )
        mock_client.complete.return_value = {
            "macrocycle_summary": "s",
            "learning_updates": [
                {"op": "reinforce", "id": lid, "evidence": ["2026-06-01"]}
            ],
        }
        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07", force=True
        )
        self.assertEqual(mock_client.complete.call_count, 2)  # force recomputed
        learning = test_db.get_learnings()[0]
        self.assertEqual(learning["last_reinforced_at"], sentinel)  # no new week -> no refresh
        self.assertEqual(learning["confidence"], "tentative")        # still one week

    @patch("trainmate.coach.engine.openrouter_client")
    def test_inspect_only_writes_nothing(self, mock_client):
        """--inspect-only renders but writes neither learnings nor the cache (§9)."""
        self._seed_activity()
        mock_client.complete.return_value = {
            "macrocycle_summary": "s",
            "learning_updates": [{"op": "add", "text": "New obs"}],
        }
        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07", inspect_only=True
        )
        self.assertEqual(len(test_db.get_learnings()), 0)
        self.assertIsNone(test_db.get_analysis_cache("long"))

    @patch("trainmate.coach.engine.openrouter_client")
    def test_existing_learnings_injected_into_prompt(self, mock_client):
        # Existing observations must appear in the analysis prompt (with ids) so the
        # model can revise/reinforce them instead of only re-adding duplicates.
        lid = test_db.add_learning(
            "Recovers slowly after back-to-back hard days",
            sports="running", confidence="moderate"
        )
        mock_client.complete.return_value = {
            "macrocycle_summary": "x", "learning_updates": []
        }

        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07"
        )

        system_prompt = mock_client.complete.call_args[0][0]
        self.assertIn("COACH LEARNINGS", system_prompt)
        self.assertIn(f"[{lid}|running|moderate]", system_prompt)
        self.assertIn("Recovers slowly after back-to-back hard days", system_prompt)


class TestReflectWatermark(unittest.TestCase):
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

    @patch("trainmate.coach.engine.openrouter_client")
    def test_bootstrap_establishes_watermark(self, mock_client):
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
        )
        wm = test_db.get_sync_state("reflect")
        self.assertEqual(wm["through_date"], "2026-06-07")

    @patch("trainmate.coach.engine.openrouter_client")
    def test_reflect_starts_after_watermark(self, mock_client):
        """Reflect ingests only evidence newer than the watermark, so overlapping history
        is never re-counted (the source of confidence converging to 'established')."""
        test_db.set_sync_state(
            through_date="2026-06-07", last_pull_utc="2026-06-07T00:00:00+00:00", key="reflect"
        )
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.reflect_workouts(until_date_str="2026-06-21", no_pull=True)
        # Window starts the day after the watermark: Monday of 2026-06-08's week is 2026-06-08.
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-06-08", summaries)
        self.assertNotIn("2026-06-01", summaries)
        # Watermark advanced to the new through-date.
        self.assertEqual(test_db.get_sync_state("reflect")["through_date"], "2026-06-21")

    @patch("trainmate.coach.engine.openrouter_client")
    def test_reflect_no_new_evidence_skips_llm(self, mock_client):
        """With nothing new since the watermark, reflect makes no LLM call."""
        test_db.set_sync_state(
            through_date="2026-06-21", last_pull_utc="2026-06-21T00:00:00+00:00", key="reflect"
        )
        result = coach_service.reflect_workouts(until_date_str="2026-06-21", no_pull=True)
        self.assertEqual(result, {})
        mock_client.complete.assert_not_called()

    @patch("trainmate.coach.engine.openrouter_client")
    def test_reflect_watermark_never_rewinds(self, mock_client):
        """A back-dated explicit window must not rewind the watermark."""
        test_db.set_sync_state(
            through_date="2026-06-21", last_pull_utc="2026-06-21T00:00:00+00:00", key="reflect"
        )
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.reflect_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
        )
        self.assertEqual(test_db.get_sync_state("reflect")["through_date"], "2026-06-21")


class TestWeekResponseFeatures(unittest.TestCase):
    """Pure unit tests for the deterministic per-week body-response features (no DB)."""

    BASELINE = {
        "rhr_baseline_mean": 50.0, "rhr_baseline_std": 4.0,
        "hrv_baseline_mean": 80.0, "hrv_baseline_std": 10.0,
        "sleep_baseline_mean": 70.0, "sleep_baseline_std": 8.0,
    }

    def test_z_scores_and_sign_convention(self):
        # Elevated RHR (worse) -> +z; suppressed HRV (worse) -> -z.
        metrics = [
            {"rhr": 54, "hrv": 70, "sleep_score": 66, "stress": 40},
            {"rhr": 58, "hrv": 70, "sleep_score": 66, "stress": 30},
        ]
        f = coach_service._week_response_features(metrics, self.BASELINE)
        self.assertEqual(f["avg_stress"], 35.0)
        self.assertEqual(f["avg_sleep_score"], 66.0)
        self.assertEqual(f["vs_baseline_z"]["rhr"], 1.5)    # ((54-50)+(58-50))/2 /4 = 1.5
        self.assertEqual(f["vs_baseline_z"]["hrv"], -1.0)   # (70-80)/10
        self.assertEqual(f["vs_baseline_z"]["sleep"], -0.5) # (66-70)/8

    def test_zero_std_and_missing_baseline_yield_none(self):
        metrics = [{"rhr": 54, "hrv": 70, "sleep_score": 66}]
        flat = dict(self.BASELINE, rhr_baseline_std=0.0)  # undefined -> None
        self.assertIsNone(
            coach_service._week_response_features(metrics, flat)["vs_baseline_z"]["rhr"]
        )
        # No baseline at all: vs_baseline_z omitted entirely, means still computed.
        f = coach_service._week_response_features(metrics, None)
        self.assertNotIn("vs_baseline_z", f)
        self.assertEqual(f["avg_sleep_score"], 66.0)

    def test_no_metric_days_means_none(self):
        f = coach_service._week_response_features([], self.BASELINE)
        self.assertIsNone(f["avg_sleep_score"])
        self.assertIsNone(f["avg_stress"])
        self.assertNotIn("vs_baseline_z", f)  # no values -> all None -> omitted


class TestWeekLifeEvents(unittest.TestCase):
    """Pure unit tests for per-week life-event bucketing (no DB)."""

    @staticmethod
    def _d(s):
        return datetime.strptime(s, "%Y-%m-%d").date()

    def test_full_partial_and_non_overlap(self):
        events = [
            {"title": "Flu", "start_date": "2026-06-01", "end_date": "2026-06-07",
             "event_type": "illness", "impact_description": "bed-bound"},
            {"title": "Trip", "start_date": "2026-06-05", "end_date": "2026-06-10",
             "event_type": "travel", "impact_description": ""},
            {"title": "Later", "start_date": "2026-06-20", "end_date": "2026-06-21",
             "event_type": "stress", "impact_description": ""},
        ]
        out = coach_service._week_life_events(
            events, self._d("2026-06-01"), self._d("2026-06-07")
        )
        titles = {e["title"]: e["coverage"] for e in out}
        self.assertEqual(titles, {"Flu": "full", "Trip": "partial"})  # "Later" excluded
        self.assertEqual(out[0]["type"], "illness")
        self.assertEqual(out[0]["impact"], "bed-bound")

    def test_multiweek_event_buckets_into_each_week(self):
        event = [{"title": "Long", "start_date": "2026-06-01", "end_date": "2026-06-14",
                  "event_type": "injury", "impact_description": ""}]
        wk1 = coach_service._week_life_events(
            event, self._d("2026-06-01"), self._d("2026-06-07")
        )
        wk2 = coach_service._week_life_events(
            event, self._d("2026-06-08"), self._d("2026-06-14")
        )
        self.assertEqual([e["coverage"] for e in wk1], ["full"])
        self.assertEqual([e["coverage"] for e in wk2], ["full"])


class TestRicherEvidenceIntegration(unittest.TestCase):
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

    def _seed_week(self):
        test_db.save_completed_activity(
            activity_id="a1", date="2026-06-03", start_time="08:00:00",
            activity_name="Ride", activity_type="road_biking",
            duration_sec=3600.0, distance_km=20.0, elevation_gain_m=100.0,
            avg_hr=130, max_hr=150, rpe=5, tss=60.0,
        )
        test_db.save_metric_cache("2026-06-03", rhr=58, hrv=68, sleep_score=60, stress=45)
        test_db.save_baseline(
            "2026-06-01", rhr_mean=50.0, rhr_std=4.0, hrv_mean=80.0,
            hrv_std=10.0, sleep_mean=70.0, sleep_std=8.0,
        )
        test_db.add_lifeevent(
            "Work crunch", "2026-06-01", "2026-06-07", "stress", "long hours, poor sleep"
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_features_and_events_reach_the_prompt(self, mock_client):
        self._seed_week()
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
        )
        user_content = mock_client.complete.call_args[0][1]
        self.assertIn("life_events", user_content)
        self.assertIn("Work crunch", user_content)
        self.assertIn("vs_baseline_z", user_content)
        self.assertIn("avg_stress", user_content)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_editing_a_life_event_invalidates_the_cache(self, mock_client):
        """A life-event change shifts the evidence fingerprint, so the next run recomputes
        rather than reusing the cached reconstruction."""
        self._seed_week()
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.bootstrap_workouts(
            from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
        )
        eid = test_db.get_lifeevents()[0]["id"]
        test_db.update_lifeevent(eid, impact_description="changed")
        # Confirm + recompute (bootstrap already ran).
        with patch("builtins.input", return_value="y"):
            coach_service.bootstrap_workouts(
                from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
            )
        self.assertEqual(mock_client.complete.call_count, 2)  # not reused


if __name__ == "__main__":
    unittest.main()
