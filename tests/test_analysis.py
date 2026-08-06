import os
import unittest
from datetime import datetime
from unittest.mock import patch

from tests.helpers import clear_all_tables, rebind_test_db
from trainmate.db import Database
import trainmate.db
import trainmate.coach

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_analysis.db")
test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service


class TestWorkoutAnalysis(unittest.TestCase):
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
        result = coach_service.data_bootstrap(until_date_str="2026-07-01")
        self.assertIsNotNone(result)

        # Inspect the start date passed to complete call
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-06-01", summaries) # Monday of that week is 2026-06-01 (Tuesday 2026-06-02 is in it)

    @patch("builtins.input", return_value="y")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_date_resolution_explicit_window(self, mock_client, _mock_input):
        """The service takes a resolved window; turning '10d'/'4w' into one is the CLI
        selector's job (tests/test_cli_selectors.py)."""
        mock_client.complete.return_value = {
            "macrocycle_summary": "Analysis summary"
        }

        # The window a `-d 10d` ending 2026-06-15 resolves to.
        coach_service.data_bootstrap(
            from_date_str="2026-06-06", until_date_str="2026-06-15"
        )
        # The Monday of the start week is 2026-06-01.
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-06-01", summaries)

        # The window a `-d 4w` ending 2026-06-15 resolves to.
        coach_service.data_bootstrap(
            from_date_str="2026-05-19", until_date_str="2026-06-15"
        )
        # The Monday of the start week is 2026-05-18.
        summaries = mock_client.complete.call_args[0][1]
        self.assertIn("2026-05-18", summaries)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_weekly_aggregation_logic(self, mock_client):
        # Setup completed activities in different weeks
        # Week commencing 2026-06-01
        test_db.save_completed_activity(
            activity_id="act_1", date="2026-06-03", start_time="08:00:00",
            activity_name="Base Ride", activity_type="cycling",
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
            ctl=60.0, atl=68.0, tsb=-8.0
        )

        mock_client.complete.return_value = {
            "macrocycle_summary": "Simulated aggregation summary",
            "learning_updates": [{"op": "add", "text": "Athlete responds well to FTP tests."}]
        }

        # Analyze the week
        result = coach_service.data_bootstrap(
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
        coach_service.data_bootstrap(
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
            activity_name="Ride", activity_type="cycling",
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
        coach_service.data_bootstrap(
            from_date_str="2026-06-01", until_date_str="2026-06-07"
        )
        reused = coach_service.data_bootstrap(
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
        coach_service.data_bootstrap(
            from_date_str="2026-06-01", until_date_str="2026-06-07"
        )
        reflect_wm = test_db.get_sync_state("reflect")
        result = coach_service.data_bootstrap(
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
        coach_service.data_bootstrap(
            from_date_str="2026-06-01", until_date_str="2026-06-07", auto=True
        )
        result = coach_service.data_bootstrap(
            from_date_str="2026-06-01", until_date_str="2026-06-07", auto=True
        )
        self.assertEqual(result, {})
        self.assertEqual(mock_client.complete.call_count, 1)
        mock_input.assert_not_called()

    @patch("trainmate.coach.engine.openrouter_client")
    @patch("builtins.input", return_value="s")
    def test_force_recompute_cannot_inflate_via_evidence_dedup(self, mock_input, mock_client):
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
        coach_service.data_bootstrap(
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
        coach_service.data_bootstrap(
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
        coach_service.data_bootstrap(
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

        coach_service.data_bootstrap(
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

    @patch("trainmate.coach.engine.openrouter_client")
    def test_bootstrap_establishes_watermark(self, mock_client):
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.data_bootstrap(
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
        coach_service.data_reflect(until_date_str="2026-06-21", no_pull=True)
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
        result = coach_service.data_reflect(until_date_str="2026-06-21", no_pull=True)
        self.assertEqual(result, {})
        mock_client.complete.assert_not_called()

    @patch("trainmate.coach.engine.openrouter_client")
    def test_reflect_watermark_never_rewinds(self, mock_client):
        """A back-dated explicit window must not rewind the watermark."""
        test_db.set_sync_state(
            through_date="2026-06-21", last_pull_utc="2026-06-21T00:00:00+00:00", key="reflect"
        )
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.data_reflect(
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


class TestContextDays(unittest.TestCase):
    """Pure unit tests for the quantitative context-impact alignment (no DB, no LLM):
    episode grouping, the bracketing morning strip, load attribution, channel exclusion,
    and the inclusion floor (DESIGN_quantitative_context_impact.md §3–§5)."""

    BASELINE = {
        "rhr_baseline_mean": 50.0, "rhr_baseline_std": 4.0,
        "hrv_baseline_mean": 80.0, "hrv_baseline_std": 10.0,
        "sleep_baseline_mean": 70.0, "sleep_baseline_std": 8.0,
    }

    def _baseline_for(self, _date):  # baseline is flat across the test window
        return self.BASELINE

    @staticmethod
    def _act(date, tss):
        # rpe=None makes activity_load return the raw tss (no fallback path), so load is
        # deterministic regardless of HR-coverage heuristics.
        return {"date": date, "activity_type": "Run", "tss": tss, "rpe": None,
                "duration_sec": 3600}

    def test_day_response_z_sign_convention(self):
        z = coach_service._day_response_z(
            {"rhr": 54, "hrv": 70, "sleep_score": 66}, self.BASELINE
        )
        self.assertEqual(z["rhr"], 1.0)    # (54-50)/4 elevated -> +z (worse)
        self.assertEqual(z["hrv"], -1.0)   # (70-80)/10 suppressed -> -z (worse)
        self.assertEqual(z["sleep"], -0.5)
        # Missing value / zero std / no baseline -> None.
        self.assertIsNone(coach_service._day_response_z({"hrv": 70}, self.BASELINE)["rhr"])
        flat = dict(self.BASELINE, hrv_baseline_std=0.0)
        self.assertIsNone(coach_service._day_response_z({"hrv": 70}, flat)["hrv"])
        self.assertIsNone(coach_service._day_response_z({"hrv": 70}, None)["hrv"])

    def test_single_signal_day_episode_shape(self):
        ctx = [{"date": "2026-05-10", "metric": "alcohol", "value": 4.0}]
        metrics = [{"date": "2026-05-11", "rhr": 58, "hrv": 70, "sleep_score": 62}]
        acts = [self._act("2026-05-10", 85)]
        out = coach_service._context_days(
            ctx, metrics, acts, self._baseline_for, k=3, min_signal_days=1
        )
        eps = out["alcohol"]
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["days"], [
            {"date": "2026-05-10", "value": 4, "load_tss": 85}  # 4.0 normalized to int 4
        ])
        # Strip spans (first-k+1)..(last+k) = 05-08 .. 05-13 -> 6 mornings.
        mornings = eps[0]["surrounding_mornings"]
        self.assertEqual([m["morning"] for m in mornings],
                         ["2026-05-08", "2026-05-09", "2026-05-10",
                          "2026-05-11", "2026-05-12", "2026-05-13"])
        # The morning AFTER the drink carries the drink-day's load as prev_day_load_tss.
        m11 = next(m for m in mornings if m["morning"] == "2026-05-11")
        self.assertEqual(m11["prev_day_load_tss"], 85)
        self.assertEqual(m11["vs_normal"], {"rhr": 2.0, "hrv": -1.0, "sleep": -1.0})
        # A morning with no metric row shows load context but an empty vs_normal (no data).
        m08 = next(m for m in mornings if m["morning"] == "2026-05-08")
        self.assertEqual(m08["vs_normal"], {})

    def test_consecutive_and_near_days_merge_one_episode(self):
        # 05-10, 05-11 (adjacent) and 05-14 (gap_free=2 < k=3) all merge into one episode;
        # the interior dry day 05-12/13 is NOT in days but its mornings still appear.
        ctx = [
            {"date": "2026-05-10", "metric": "alcohol", "value": 2},
            {"date": "2026-05-11", "metric": "alcohol", "value": 3},
            {"date": "2026-05-14", "metric": "alcohol", "value": 1},
        ]
        out = coach_service._context_days(
            ctx, [], [], self._baseline_for, k=3, min_signal_days=1
        )
        eps = out["alcohol"]
        self.assertEqual(len(eps), 1)
        self.assertEqual([d["date"] for d in eps[0]["days"]],
                         ["2026-05-10", "2026-05-11", "2026-05-14"])
        # Strip spans 05-08 .. 05-17, and 05-13 (a dry gap morning) is present.
        days_in_strip = {m["morning"] for m in eps[0]["surrounding_mornings"]}
        self.assertIn("2026-05-13", days_in_strip)
        self.assertNotIn("2026-05-13", {d["date"] for d in eps[0]["days"]})

    def test_large_gap_splits_into_two_episodes(self):
        # 4 days apart -> gap_free=3, not < k=3 -> separate episodes.
        ctx = [
            {"date": "2026-05-10", "metric": "alcohol", "value": 2},
            {"date": "2026-05-14", "metric": "alcohol", "value": 2},
        ]
        out = coach_service._context_days(
            ctx, [], [], self._baseline_for, k=3, min_signal_days=1
        )
        self.assertEqual(len(out["alcohol"]), 2)

    def test_min_signal_days_floor_omits_category(self):
        ctx = [{"date": "2026-05-10", "metric": "alcohol", "value": 2}]
        out = coach_service._context_days(
            ctx, [], [], self._baseline_for, k=3, min_signal_days=2
        )
        self.assertNotIn("alcohol", out)

    def test_sleep_construct_excludes_sleep_channel(self):
        ctx = [{"date": "2026-05-10", "metric": "poor_sleep", "value": 1}]
        metrics = [{"date": "2026-05-11", "rhr": 58, "hrv": 70, "sleep_score": 62}]
        out = coach_service._context_days(
            ctx, metrics, [], self._baseline_for, k=3, min_signal_days=1
        )
        m11 = next(
            m for m in out["poor_sleep"][0]["surrounding_mornings"]
            if m["morning"] == "2026-05-11"
        )
        self.assertNotIn("sleep", m11["vs_normal"])  # would be an echo, not an impact
        self.assertIn("hrv", m11["vs_normal"])
        self.assertIn("rhr", m11["vs_normal"])

    def test_presence_only_value_stays_none(self):
        ctx = [{"date": "2026-05-10", "metric": "big_meal", "value": None}]
        out = coach_service._context_days(
            ctx, [], [], self._baseline_for, k=3, min_signal_days=1
        )
        self.assertIsNone(out["big_meal"][0]["days"][0]["value"])

    def test_same_day_values_combine(self):
        ctx = [
            {"date": "2026-05-10", "metric": "alcohol", "value": 2},
            {"date": "2026-05-10", "metric": "alcohol", "value": 3},
        ]
        out = coach_service._context_days(
            ctx, [], [], self._baseline_for, k=3, min_signal_days=1
        )
        self.assertEqual(out["alcohol"][0]["days"][0]["value"], 5)


class TestWeekLifeEvents(unittest.TestCase):
    """Pure unit tests for per-week constraint bucketing (no DB)."""

    @staticmethod
    def _d(s):
        return datetime.strptime(s, "%Y-%m-%d").date()

    def test_full_partial_and_non_overlap(self):
        constraints = [
            {"title": "Flu", "start_date": "2026-06-01", "end_date": "2026-06-07",
             "type": "illness", "description": "bed-bound"},
            {"title": "Trip", "start_date": "2026-06-05", "end_date": "2026-06-10",
             "type": "travel", "description": ""},
            {"title": "Later", "start_date": "2026-06-20", "end_date": "2026-06-21",
             "type": "stress", "description": ""},
        ]
        out = coach_service._week_constraints(
            constraints, self._d("2026-06-01"), self._d("2026-06-07")
        )
        titles = {e["title"]: e["coverage"] for e in out}
        self.assertEqual(titles, {"Flu": "full", "Trip": "partial"})  # "Later" excluded
        self.assertEqual(out[0]["impact"], "bed-bound")

    def test_multiweek_event_buckets_into_each_week(self):
        constraint = [{"title": "Long", "start_date": "2026-06-01", "end_date": "2026-06-14",
                       "type": "injury", "description": ""}]
        wk1 = coach_service._week_constraints(
            constraint, self._d("2026-06-01"), self._d("2026-06-07")
        )
        wk2 = coach_service._week_constraints(
            constraint, self._d("2026-06-08"), self._d("2026-06-14")
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

    def _seed_week(self):
        test_db.save_completed_activity(
            activity_id="a1", date="2026-06-03", start_time="08:00:00",
            activity_name="Ride", activity_type="cycling",
            duration_sec=3600.0, distance_km=20.0, elevation_gain_m=100.0,
            avg_hr=130, max_hr=150, rpe=5, tss=60.0,
        )
        test_db.save_metric_cache("2026-06-03", rhr=58, hrv=68, sleep_score=60, stress=45)
        test_db.save_baseline(
            "2026-06-01", rhr_mean=50.0, rhr_std=4.0, hrv_mean=80.0,
            hrv_std=10.0, sleep_mean=70.0, sleep_std=8.0,
        )
        test_db.add_constraint(
            title="Work crunch", start_date="2026-06-01", end_date="2026-06-07",
            description="long hours, poor sleep",
        )

    @patch("trainmate.coach.engine.openrouter_client")
    def test_features_and_events_reach_the_prompt(self, mock_client):
        self._seed_week()
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.data_bootstrap(
            from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
        )
        user_content = mock_client.complete.call_args[0][1]
        self.assertIn("constraints", user_content)
        self.assertIn("Work crunch", user_content)
        self.assertIn("vs_baseline_z", user_content)
        self.assertIn("avg_stress", user_content)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_context_days_reaches_the_prompt(self, mock_client):
        """A logged external signal (alcohol) surfaces as an episode-aligned context_days
        block in the analysis user content (DESIGN_quantitative_context_impact.md §4)."""
        self._seed_week()
        test_db.upsert_daily_context_by_event(
            "evt1", "2026-06-02", "alcohol", value=4.0, text="Alcohol: 4 drinks"
        )
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.data_bootstrap(
            from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
        )
        user_content = mock_client.complete.call_args[0][1]
        self.assertIn("QUANTITATIVE CONTEXT IMPACT", user_content)
        self.assertIn("surrounding_mornings", user_content)
        self.assertIn("alcohol", user_content)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_editing_a_life_event_invalidates_the_cache(self, mock_client):
        """A constraint change shifts the evidence fingerprint, so the next run recomputes
        rather than reusing the cached reconstruction."""
        self._seed_week()
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.data_bootstrap(
            from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
        )
        eid = test_db.get_constraints()[0]["id"]
        test_db.update_constraint(eid, description="changed")
        # Confirm + recompute (bootstrap already ran).
        with patch("builtins.input", return_value="y"):
            coach_service.data_bootstrap(
                from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
            )
        self.assertEqual(mock_client.complete.call_count, 2)  # not reused

    @patch("trainmate.coach.engine.openrouter_client")
    def test_out_of_window_constraint_does_not_invalidate_the_cache(self, mock_client):
        """Constraints are fetched windowed, so one lying entirely outside [from,until]
        never reaches the fingerprint and the reconstruction stays reusable
        (DESIGN_richer_analysis_evidence.md §5, §8)."""
        self._seed_week()
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.data_bootstrap(
            from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
        )
        test_db.add_constraint(
            title="Later trip", start_date="2026-07-01", end_date="2026-07-05",
            description="out of window",
        )
        with patch("builtins.input", return_value="y"):
            coach_service.data_bootstrap(
                from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
            )
        self.assertEqual(mock_client.complete.call_count, 1)  # cached reconstruction reused

    @patch("trainmate.coach.engine.openrouter_client")
    def test_out_of_window_context_signal_invalidates_the_cache(self, mock_client):
        """`context_days` is built full-history, so a signal logged OUTSIDE [from,until]
        still changes the prompt — and must therefore shift the fingerprint
        (DESIGN_quantitative_context_impact.md §8)."""
        self._seed_week()
        mock_client.complete.return_value = {"macrocycle_summary": "s", "learning_updates": []}
        coach_service.data_bootstrap(
            from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
        )
        # Two months before the analysis window: invisible to the windowed evidence, but
        # it adds a whole episode to the context_days block the LLM is shown.
        test_db.upsert_daily_context_by_event(
            "evt-old", "2026-04-02", "alcohol", value=4.0, text="Alcohol: 4 drinks"
        )
        with patch("builtins.input", return_value="y"):
            coach_service.data_bootstrap(
                from_date_str="2026-06-01", until_date_str="2026-06-07", no_pull=True
            )
        self.assertEqual(mock_client.complete.call_count, 2)  # not reused
        self.assertIn("2026-04-02", mock_client.complete.call_args[0][1])


class TestPriorTrainingContext(unittest.TestCase):
    """The cached reconstruction is fed read-only into the plan-generate strategy prompt
    (DESIGN_backward_evaluation.md §6)."""

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

    def test_inferred_blocks_reach_the_context(self):
        """The reverse-engineered macro focus and mesocycle blocks from a bootstrap
        reconstruction are rendered into the prior-training context, not just the summary."""
        test_db.save_analysis_cache(
            "long", "fp", "2026-03-01", "2026-05-31",
            {
                "macrocycle_summary": "Built a solid aerobic base.",
                "inferred_macrocycle": {
                    "overall_focus": "Marathon base prep",
                    "start_date": "2026-03-01",
                    "end_date": "2026-05-31",
                },
                "inferred_mesocycles": [
                    {
                        "name": "Base Building",
                        "start_date": "2026-03-01",
                        "end_date": "2026-04-15",
                        "focus_detected": "Aerobic volume",
                        "average_weekly_tss": 380.0,
                        "estimated_consistency": "High",
                    },
                ],
                "physiological_insights": ["RHR trended down as volume rose."],
            },
        )

        text = coach_service._build_prior_training_context([], "2026-06-15")

        self.assertIsNotNone(text)
        self.assertIn("Reconstructed macrocycle focus", text)
        self.assertIn("Marathon base prep", text)
        self.assertIn("Base Building", text)
        self.assertIn("Aerobic volume", text)
        self.assertIn("380 TSS/wk", text)
        self.assertIn("High consistency", text)
        self.assertIn("RHR trended down", text)

    # ------------------------------------------------- which caches get replayed

    @staticmethod
    def _cache(horizon: str, start: str, end: str, summary: str) -> None:
        test_db.save_analysis_cache(
            horizon, f"fp-{horizon}", start, end, {"macrocycle_summary": summary}
        )

    def test_the_latest_reflection_joins_the_bootstrap_reconstruction(self):
        """`data reflect` used to write a cache slot nothing ever read, so everything it
        learned after onboarding was invisible to planning (§10.2)."""
        self._cache("long", "2026-01-01", "2026-05-31", "Winter base rebuilt.")
        self._cache("short", "2026-06-01", "2026-07-15", "Threshold work started.")

        text = coach_service._build_prior_training_context([], "2026-07-20")

        self.assertIn("full history reconstruction", text)
        self.assertIn("Winter base rebuilt.", text)
        self.assertIn("most recent reflection", text)
        self.assertIn("Threshold work started.", text)

    def test_a_reflection_no_later_than_bootstrap_is_not_replayed(self):
        """Ground bootstrap already covered: replaying it would put two accounts of the
        same weeks in front of the model."""
        self._cache("long", "2026-01-01", "2026-05-31", "Winter base rebuilt.")
        self._cache("short", "2026-04-01", "2026-05-31", "Same weeks, second opinion.")

        text = coach_service._build_prior_training_context([], "2026-07-20")

        self.assertIn("Winter base rebuilt.", text)
        self.assertNotIn("Same weeks, second opinion.", text)

    def test_reflection_alone_is_replayed_when_bootstrap_never_ran(self):
        self._cache("short", "2026-06-01", "2026-07-15", "Threshold work started.")

        text = coach_service._build_prior_training_context([], "2026-07-20")

        self.assertIn("most recent reflection", text)
        self.assertIn("Threshold work started.", text)

    # ------------------------------------------------------- planned vs actual load

    # Two whole Monday-weeks, so the week buckets line up with the block and no edge week
    # is partial by accident.
    BLOCK_START, BLOCK_END = "2026-06-08", "2026-06-21"
    ZONES = [600, 1800, 900, 300, 0]

    def _plan(
        self, name: str = "Base 3", start: str = BLOCK_START, end: str = BLOCK_END,
        goal: str = "Gran Fondo", target: str = "2026-10-15",
    ) -> int:
        obj_id = test_db.add_objective(
            title=goal, target_date=target, sport_type="cycling", priority=1
        )
        return test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build", goals_hash="g", constraints_hash="c",
            mesocycles=[{
                "name": name, "start_date": start, "end_date": end,
                "focus": "aerobic volume",
            }],
        )

    def _planned_session(self, macro_id: int, date: str, tss: float) -> None:
        test_db.save_workout(
            date=date, sport_type="cycling", title="Endurance",
            description="[Endurance]\n2h steady", duration_minutes=120, rpe=5, tss=tss,
            source="generated", macrocycle_id=macro_id,
            planned_zone_currency="hr", planned_zone_sec=self.ZONES,
        )

    @staticmethod
    def _completed(date: str, tss: float, activity_id: str) -> None:
        test_db.save_completed_activity(
            activity_id=activity_id, date=date, start_time=f"{date}T07:00:00",
            activity_name="Ride", activity_type="cycling", duration_sec=3600,
            distance_km=30.0, elevation_gain_m=200.0, avg_hr=140, max_hr=170, rpe=5,
            tss=tss, zone1_sec=600, zone2_sec=1800, zone3_sec=900, zone4_sec=300,
            zone5_sec=0,
        )

    def _half_missed_block(self) -> None:
        """Both weeks asked for 100; only the first was trained."""
        macro_id = self._plan()
        self._planned_session(macro_id, "2026-06-09", 100.0)
        self._planned_session(macro_id, "2026-06-16", 100.0)
        self._completed("2026-06-09", 100.0, "w1")

    def test_elapsed_blocks_report_each_week_planned_against_actual(self):
        """Without this a half-missed block reads exactly like a completed one, and the
        next macrocycle ramps from a load the athlete never reached."""
        self._half_missed_block()

        text = coach_service._build_prior_training_context([], "2026-07-20")

        self.assertIn("Weekly load (what the plan asked -> what was produced)", text)
        self.assertIn("week of 2026-06-08: planned 100, actual 100 (100%)", text)
        self.assertIn("week of 2026-06-15: planned 100, actual 0 (0%)", text)

    def test_elapsed_blocks_show_what_the_plan_prescribed(self):
        """Measured beside prescribed is what separates a mis-designed block from a
        mis-executed one (DESIGN_intensity_distribution.md §9.2a)."""
        self._half_missed_block()

        text = coach_service._build_prior_training_context([], "2026-07-20")

        self.assertIn("What the plan PRESCRIBED over the same weeks", text)

    # --------------------------------------------------- ordering across macrocycles

    def _two_plans(self):
        """An earlier macrocycle and a later one, each two whole weeks, both trained."""
        early = self._plan(
            name="Base 3", start="2026-06-08", end="2026-06-21",
            goal="Spring Hill Climb", target="2026-06-21",
        )
        late = self._plan(
            name="Build 1", start="2026-06-22", end="2026-07-05",
            goal="Gran Fondo", target="2026-10-15",
        )
        for i, day in enumerate(("2026-06-09", "2026-06-16")):
            self._planned_session(early, day, 100.0)
            self._completed(day, 100.0, f"e{i}")
        for i, day in enumerate(("2026-06-23", "2026-06-30")):
            self._planned_session(late, day, 100.0)
            self._completed(day, 100.0, f"l{i}")
        return (
            test_db.get_macrocycle(early), test_db.get_macrocycle(late)
        )

    def test_blocks_are_ordered_by_when_they_were_trained(self):
        """The caller's argument order is not chronological — the plan being replaced can
        be for a later goal than the governing one — and each block's delta baseline is
        the block before it in the flattened list (§6.1)."""
        early, late = self._two_plans()

        text = coach_service._build_prior_training_context([late, early], "2026-07-20")

        # Build 1 came after Base 3, so that is what it is compared against.
        self.assertIn("Change vs Base 3", text)
        self.assertNotIn("Change vs Build 1", text)
        self.assertLess(text.index("Base 3 —"), text.index("Build 1 —"))


if __name__ == "__main__":
    unittest.main()
