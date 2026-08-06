import json
import os
import unittest
import unittest.mock
from datetime import date, timedelta

from tests.helpers import clear_all_tables

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_web.db")

from trainmate.db import Database
from trainmate import progression

import trainmate_web

# Point the web app's db singleton at an isolated test database. The handlers
# reference the module-level `trainmate_web.db`, so patching it here is enough
# for the read-only endpoints exercised below (the web app never pulls from
# Garmin — it is a pure reader, ARCHITECTURE.md §8).
test_db = Database(db_path=TEST_DB_PATH)
trainmate_web.db = test_db


def _save_activity(db, activity_id, date, activity_type, duration_sec, tss):
    db.save_completed_activity(
        activity_id=activity_id,
        date=date,
        start_time=f"{date} 08:00:00",
        activity_name=f"{activity_type} session",
        activity_type=activity_type,
        duration_sec=duration_sec,
        distance_km=10.0,
        elevation_gain_m=0.0,
        avg_hr=140,
        max_hr=160,
        rpe=None,
        tss=tss,
    )


class TestCompareEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate_web.db = test_db
        cls.client = trainmate_web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def _get(self, start, end):
        return self.client.get(
            f"/api/workouts/compare?start_date={start}&end_date={end}"
        )

    def test_matched_session_no_discrepancy(self):
        # Planned run with a closely-matching completed run -> matched, no discrepancy.
        test_db.save_workout(
            date="2026-06-10", sport_type="running", title="Tempo Run",
            description="40min tempo", duration_minutes=40, tss=50,
        )
        _save_activity(test_db, "a1", "2026-06-10", "running", 40 * 60, 50.0)

        res = self._get("2026-06-10", "2026-06-10")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data["days"]), 1)
        day = data["days"][0]
        self.assertEqual(day["date"], "2026-06-10")
        self.assertEqual(len(day["results"]), 1)
        self.assertIsNotNone(day["results"][0]["completed"])
        self.assertEqual(day["unplanned"], [])
        self.assertEqual(data["discrepancies"], [])

    def test_missed_session_is_discrepancy(self):
        # Planned run with no completed activity -> complete miss.
        test_db.save_workout(
            date="2026-06-10", sport_type="running", title="Long Run",
            description="90min easy", duration_minutes=90, tss=80,
        )

        res = self._get("2026-06-10", "2026-06-10")
        data = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(data["days"]), 1)
        self.assertIsNone(data["days"][0]["results"][0]["completed"])
        self.assertTrue(
            any("Complete Miss" in d for d in data["discrepancies"]),
            data["discrepancies"],
        )

    def test_unplanned_activity_is_flagged(self):
        # An activity on a day with no planned workout, inside a planned block
        # (mesocycle), surfaces as an unplanned deviation.
        obj_id = test_db.add_objective(
            title="Race", target_date="2026-09-01", sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="s", goals_hash="g",
            constraints_hash="l", config_hash="c",
            mesocycles=[{
                "name": "Base", "start_date": "2026-06-01",
                "end_date": "2026-06-30", "focus": "aerobic base",
            }],
        )

        _save_activity(test_db, "a2", "2026-06-10", "running", 60 * 60, 70.0)

        res = self._get("2026-06-10", "2026-06-10")
        data = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(data["days"]), 1)
        unplanned = data["days"][0]["unplanned"]
        self.assertEqual(len(unplanned), 1)
        self.assertEqual(unplanned[0]["kind"], "unplanned")

    def test_rest_day_verdict_matches_the_shared_classifier(self):
        """The endpoint used to re-derive rest/violation inline, skipping
        canonical_sport() and the load threshold — so a "Rest" workout read as a normal
        sport and a light stroll read as a violation on the dashboard only."""
        test_db.save_workout(
            date="2026-06-11", sport_type="Rest", title="Rest Day",
            description="full rest", duration_minutes=0, tss=0,
        )
        _save_activity(test_db, "a3", "2026-06-11", "running", 55 * 60, 70.0)

        result = self._get("2026-06-11", "2026-06-11").get_json()["days"][0]["results"][0]
        self.assertTrue(result["is_rest"])
        self.assertTrue(result["rest_violation"])
        self.assertEqual(result["status"], "rest_violation")

    def test_light_activity_on_a_rest_day_is_not_a_violation(self):
        test_db.save_workout(
            date="2026-06-12", sport_type="rest", title="Rest Day",
            description="full rest", duration_minutes=0, tss=0,
        )
        _save_activity(test_db, "a4", "2026-06-12", "walking", 12 * 60, 5.0)

        result = self._get("2026-06-12", "2026-06-12").get_json()["days"][0]["results"][0]
        self.assertTrue(result["is_rest"])
        self.assertFalse(result["rest_violation"])
        self.assertEqual(result["status"], "rest_ok")

    def test_end_date_capped_and_default_range(self):
        # No params -> defaults to a 14-day lookback ending today; valid empty result.
        res = self.client.get("/api/workouts/compare")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("filters", data)
        self.assertIn("days", data)


class TestPlanVersionsEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate_web.db = test_db
        cls.client = trainmate_web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_plan_versions_lists_active_and_superseded(self):
        oid = test_db.add_objective(
            title="Web Goal", target_date="2026-12-15",
            sport_type="running", priority=1,
        )
        meso = [{
            "name": "Base", "start_date": "2026-06-01",
            "end_date": "2026-06-28", "focus": "Base",
        }]
        v1 = test_db.save_macrocycle(
            objective_id=oid, strategy="First", goals_hash="g",
            constraints_hash="l", mesocycles=meso,
        )
        v2 = test_db.save_macrocycle(
            objective_id=oid, strategy="Second", goals_hash="g",
            constraints_hash="l", mesocycles=meso,
        )

        res = self.client.get("/api/plan/versions")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["goal"]["id"], oid)
        versions = {v["id"]: v for v in data["versions"]}
        self.assertEqual(set(versions), {v1, v2})
        self.assertEqual(versions[v2]["status"], "active")
        self.assertEqual(versions[v1]["status"], "superseded")


class TestWorkoutBatchesEndpoint(unittest.TestCase):
    """GET /api/workouts/batches — the web face of `workout batches`
    (see DESIGN_plan_rollback.md §9)."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate_web.db = test_db
        cls.client = trainmate_web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_batches_empty_when_nothing_archived(self):
        res = self.client.get("/api/workouts/batches")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["batches"], [])

    def test_batches_reports_span_and_restorable_count(self):
        today = date.today()
        past = (today - timedelta(days=3)).strftime("%Y-%m-%d")
        future = (today + timedelta(days=3)).strftime("%Y-%m-%d")
        for d in (past, future):
            test_db.save_workout(
                date=d, sport_type="running", title=f"Run {d}", description="x",
            )
        test_db.archive_future_workouts(past)

        res = self.client.get("/api/workouts/batches")
        self.assertEqual(res.status_code, 200)
        batches = res.get_json()["batches"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["workouts"], 2)
        # Only the future row would come back — the past one's slot may be occupied.
        self.assertEqual(batches[0]["restorable"], 1)
        self.assertEqual(batches[0]["first_date"], past)
        self.assertEqual(batches[0]["last_date"], future)


class TestPlanDiffEndpoint(unittest.TestCase):
    """GET /api/plan/diff — the web face of `plan diff`. Both front-ends render the same
    trainmate/plan_diff.py structure, so this asserts the payload, not the wording."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate_web.db = test_db
        cls.client = trainmate_web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def _seed(self):
        oid = test_db.add_objective(
            title="Diff Goal", target_date="2026-12-15",
            sport_type="running", priority=1,
        )
        v1 = test_db.save_macrocycle(
            objective_id=oid, strategy="Base first. Then sharpen.",
            goals_hash="g", constraints_hash="c",
            mesocycles=[
                {"name": "Base", "start_date": "2026-06-01",
                 "end_date": "2026-06-28", "focus": "Volume."},
                {"name": "Dropped", "start_date": "2026-06-29",
                 "end_date": "2026-07-12", "focus": "Filler."},
            ],
            config_snapshot=json.dumps({"ftp": 200.0}),
        )
        v2 = test_db.save_macrocycle(
            objective_id=oid, strategy="Base first. Then sharpen.",
            goals_hash="g", constraints_hash="c",
            mesocycles=[
                {"name": "Base", "start_date": "2026-06-01",
                 "end_date": "2026-07-05", "focus": "Volume."},
            ],
            config_snapshot=json.dumps({"ftp": 220.0}),
        )
        return oid, v1, v2

    def test_plan_diff_defaults_to_previous_vs_active(self):
        oid, v1, v2 = self._seed()

        res = self.client.get("/api/plan/diff")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["goal"]["id"], oid)
        diff = data["diff"]
        self.assertEqual(diff["from"]["id"], v1)
        self.assertEqual(diff["to"]["id"], v2)
        self.assertFalse(diff["strategy"]["changed"])

        blocks = {m["change"]: m for m in diff["mesocycles"]}
        self.assertEqual(blocks["changed"]["name"], "Base")
        self.assertEqual(
            blocks["changed"]["dates"],
            {"from": {"start": "2026-06-01", "end": "2026-06-28"},
             "to": {"start": "2026-06-01", "end": "2026-07-05"}},
        )
        self.assertEqual(blocks["removed"]["name"], "Dropped")

        (drift,) = diff["thresholds"]["changed"]
        self.assertEqual(drift["key"], "ftp")
        self.assertAlmostEqual(drift["pct"], 10.0)
        # Neither version snapshotted its goals, which must not read as a deletion.
        self.assertEqual(diff["goals"]["missing"], "both")
        self.assertEqual(diff["goals"]["removed"], [])

    def test_plan_diff_explicit_versions_and_errors(self):
        oid, v1, v2 = self._seed()

        res = self.client.get(f"/api/plan/diff?from_version={v2}&to_version={v1}")
        self.assertEqual(res.status_code, 200)
        diff = res.get_json()["diff"]
        self.assertEqual((diff["from"]["id"], diff["to"]["id"]), (v2, v1))
        self.assertAlmostEqual(diff["thresholds"]["changed"][0]["to"], 200.0)

        res = self.client.get(f"/api/plan/diff?from_version={v1}&to_version={v1}")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "same_version")

        res = self.client.get("/api/plan/diff?from_version=9999")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json()["code"], "not_found")

    def test_plan_diff_single_version(self):
        test_db.add_objective(
            title="Lonely", target_date="2026-12-15", sport_type="running",
        )
        objectives = test_db.get_objectives(status='active')
        test_db.save_macrocycle(
            objective_id=objectives[0]['id'], strategy="Only", goals_hash="g",
            constraints_hash="c", mesocycles=[],
        )
        res = self.client.get("/api/plan/diff")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "single_version")


class TestTimelinePngEndpoint(unittest.TestCase):
    """GET /api/timeline.png (DESIGN_progress_timeline.md §6). Pure reader — no
    Garmin/LLM mocks needed, which is itself part of the assertion. Pixels stay
    untested (visual output); only status/content-type/framing and the assembled
    payload are asserted."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate_web.db = test_db
        cls.client = trainmate_web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_returns_png_bytes(self):
        _save_activity(test_db, "a1", "2026-06-10", "running", 3600, 40.0)
        res = self.client.get("/api/timeline.png")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "image/png")
        self.assertTrue(res.data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_empty_db_still_renders_a_png(self):
        res = self.client.get("/api/timeline.png")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data.startswith(b"\x89PNG"))

    def test_weeks_validation(self):
        _save_activity(test_db, "a1", "2026-06-10", "running", 3600, 40.0)
        self.assertEqual(self.client.get("/api/timeline.png?weeks=0").status_code, 400)
        self.assertEqual(self.client.get("/api/timeline.png?weeks=all").status_code, 200)
        self.assertEqual(self.client.get("/api/timeline.png?weeks=x").status_code, 400)

    def test_matplotlib_absent_returns_503_with_hint(self):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "matplotlib" or name.startswith("matplotlib."):
                raise ImportError("no matplotlib")
            return real_import(name, *args, **kwargs)

        _save_activity(test_db, "a1", "2026-06-10", "running", 3600, 40.0)
        with unittest.mock.patch("builtins.__import__", side_effect=fake_import):
            res = self.client.get("/api/timeline.png")
        self.assertEqual(res.status_code, 503)
        self.assertIn("matplotlib", res.get_data(as_text=True))


class TestTimelinePayload(unittest.TestCase):
    """The assembled payload behind the PNG (via the shared builder), where the pixel
    output can't assert the numbers."""

    @classmethod
    def setUpClass(cls):
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate_web.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def _payload(self):
        from trainmate import timeline
        return timeline.build_timeline_payload(test_db)

    def test_no_activity_at_all_warns_and_empty_days(self):
        data = self._payload()
        self.assertEqual(data["days"], [])
        self.assertIsNone(data["plan_end"])
        self.assertIn("no_history", {w["code"] for w in data["warnings"]})

    def test_plan_end_is_last_non_removed_generated_workout(self):
        _save_activity(test_db, "a1", "2026-06-10", "running", 3600, 40.0)
        test_db.save_workout(date="2026-07-10", sport_type="running", title="Run",
                             description="d", duration_minutes=60, tss=50)
        later = test_db.save_workout(date="2026-07-20", sport_type="running",
                                     title="Run late", description="d",
                                     duration_minutes=60, tss=50)
        test_db.mark_workout_removed(later, reason="cancelled")
        self.assertEqual(self._payload()["plan_end"], "2026-07-10")

    def test_meso_bands_layers_inferred_and_plan(self):
        oid = test_db.add_objective(title="Race", target_date="2026-12-01",
                                    sport_type="running")
        test_db.save_macrocycle(
            objective_id=oid, strategy="s", goals_hash="g", constraints_hash="l",
            mesocycles=[{"name": "Build", "start_date": "2026-07-01",
                         "end_date": "2026-07-31", "focus": "build"}],
        )
        test_db.save_analysis_cache(
            horizon="long", fingerprint="fp", window_start="2026-05-01",
            window_end="2026-06-30",
            reconstruction={"inferred_mesocycles": [{
                "name": "Base", "start_date": "2026-05-01", "end_date": "2026-05-31",
                "focus": "base"}]},
        )
        _save_activity(test_db, "a1", "2026-05-05", "running", 3600, 30.0)
        sources = [b["source"] for b in self._payload()["meso_bands"]]
        self.assertIn("inferred", sources)
        self.assertIn("plan", sources)

    def test_clip_payload_keeps_full_history_seeding(self):
        for i in range(60):
            d = (date(2026, 5, 1) + timedelta(days=i)).isoformat()
            _save_activity(test_db, f"a{i}", d, "running", 3600, 50.0)
        payload = self._payload()
        full = progression.clip_payload(payload, "2026-05-01", "2026-06-29")
        narrow = progression.clip_payload(payload, "2026-06-25", "2026-06-29")
        full_point = next(d for d in full["days"] if d["date"] == "2026-06-25")
        narrow_point = next(d for d in narrow["days"] if d["date"] == "2026-06-25")
        self.assertAlmostEqual(full_point["ctl"], narrow_point["ctl"])
        self.assertEqual(len(narrow["days"]), 5)


class TestReadOnly(unittest.TestCase):
    """The dashboard reads and nothing else (ARCHITECTURE.md §8). These tests are the
    enforcement: a route added with a mutating verb fails here, which is the whole point
    of demoting the surface rather than merely documenting it as read-only."""

    @classmethod
    def setUpClass(cls):
        cls.client = trainmate_web.app.test_client()

    def test_every_route_is_get_only(self):
        for rule in trainmate_web.app.url_map.iter_rules():
            verbs = rule.methods - {"HEAD", "OPTIONS"}
            self.assertEqual(
                verbs, {"GET"},
                f"{rule} exposes {sorted(verbs)}; the dashboard must stay read-only",
            )

    def test_mutating_verbs_are_refused(self):
        for verb, path in (
            ("post", "/api/objectives"),
            ("post", "/api/workouts"),
            ("put", "/api/objectives/1"),
            ("delete", "/api/objectives/1"),
            ("post", "/api/plan"),
            ("post", "/api/adapt"),
        ):
            res = getattr(self.client, verb)(path)
            self.assertEqual(res.status_code, 405, f"{verb.upper()} {path}")
            self.assertIn("read-only", res.get_json()["error"])

    def test_no_google_or_llm_import_at_module_scope(self):
        # A reader needs no Calendar service-account credentials and no LLM client;
        # importing either would put write capability one call away.
        source = open(trainmate_web.__file__).read()
        self.assertNotIn("google_calendar", source)
        self.assertNotIn("coach_service", source)


class TestNewReadEndpoints(unittest.TestCase):
    """The CLI-only features the demotion brought over as views: the benchmark logbook,
    the daily-context vocabulary, the model menu, plan show and the zone tables."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate_web.db = test_db
        # `/api/models` delegates to `llm_models`, which resolves `trainmate.db.db`
        # lazily rather than taking the web app's handle — so that one has to be bound
        # too, and restored afterwards so it does not leak into later test modules.
        import trainmate.db
        cls._saved_db = trainmate.db.db
        trainmate.db.db = test_db
        cls.client = trainmate_web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        import trainmate.db
        trainmate.db.db = cls._saved_db
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)

    def test_benchmarks_carry_formatted_value_and_directional_delta(self):
        test_db.add_benchmark_result(
            date="2026-05-01", sport_type="cycling", anchor_kind="ftp",
            value=240.0, unit="W", source="test",
        )
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="cycling", anchor_kind="ftp",
            value=250.0, unit="W", source="test",
        )
        data = self.client.get("/api/benchmarks").get_json()
        # Newest first, and the newest compares against the older row of the same kind.
        self.assertEqual([r["value"] for r in data["results"]], [250.0, 240.0])
        newest = data["results"][0]
        self.assertTrue(newest["improvement"])
        self.assertTrue(newest["delta"].startswith("+"))
        self.assertIsNone(data["results"][1]["delta"])
        self.assertEqual(
            [t["anchor_kind"] for t in data["thresholds"]], ["ftp"]
        )
        self.assertEqual(data["thresholds"][0]["value"], 250.0)

    def test_benchmark_pace_delta_reads_positive_when_faster(self):
        # A lower threshold pace is an improvement; the sign must reflect that (§3.2).
        test_db.add_benchmark_result(
            date="2026-05-01", sport_type="running", anchor_kind="threshold_pace",
            value=300.0, unit="min/km", source="test",
        )
        test_db.add_benchmark_result(
            date="2026-06-01", sport_type="running", anchor_kind="threshold_pace",
            value=285.0, unit="min/km", source="test",
        )
        newest = self.client.get("/api/benchmarks").get_json()["results"][0]
        self.assertTrue(newest["improvement"])
        self.assertTrue(newest["delta"].startswith("+"))

    def test_benchmarks_filter_by_sport(self):
        test_db.add_benchmark_result(
            date="2026-05-01", sport_type="cycling", anchor_kind="ftp",
            value=240.0, unit="W", source="test",
        )
        test_db.add_benchmark_result(
            date="2026-05-02", sport_type="running", anchor_kind="threshold_pace",
            value=300.0, unit="min/km", source="test",
        )
        data = self.client.get("/api/benchmarks?sport=cycling").get_json()
        self.assertEqual([r["sport_type"] for r in data["results"]], ["cycling"])

    def test_context_metrics_vocabulary(self):
        for d, val in (("2026-06-01", 2), ("2026-06-03", 1)):
            test_db.upsert_daily_context_by_event(
                google_event_id=f"e{d}", date=d, metric="alcohol",
                value=val, text="drinks",
            )
        data = self.client.get("/api/daily-context/metrics").get_json()
        self.assertEqual(len(data["metrics"]), 1)
        row = data["metrics"][0]
        self.assertEqual(row["metric"], "alcohol")
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["first_date"], "2026-06-01")
        self.assertEqual(row["last_date"], "2026-06-03")

    def test_daily_context_filters_by_metric(self):
        test_db.upsert_daily_context_by_event(
            google_event_id="e1", date="2026-06-01", metric="alcohol", value=2, text="")
        test_db.upsert_daily_context_by_event(
            google_event_id="e2", date="2026-06-01", metric="stress", value=7, text="")
        rows = self.client.get("/api/daily-context?metric=stress").get_json()
        self.assertEqual([r["metric"] for r in rows], ["stress"])

    def test_models_menu_marks_the_active_entry(self):
        data = self.client.get("/api/models").get_json()
        self.assertIn("models", data)
        self.assertTrue(data["active"])
        actives = [m for m in data["models"] if m["active"]]
        self.assertEqual(len(actives), 1)
        self.assertEqual(actives[0]["model"], data["active"])

    def test_plan_show_returns_macro_and_mesocycles(self):
        goal_id = test_db.add_objective(
            title="A race", target_date="2026-09-01", sport_type="running",
            description="", priority=1, status="active",
        )
        test_db.save_macrocycle(
            objective_id=goal_id, strategy="build then peak", goals_hash="g",
            constraints_hash="l", config_hash="c",
            mesocycles=[{"name": "Base", "start_date": "2026-06-01",
                         "end_date": "2026-06-28", "focus": "aerobic"}],
        )
        data = self.client.get("/api/plan").get_json()
        self.assertEqual(data["goal"]["id"], goal_id)
        self.assertEqual(data["macrocycle"]["strategy"], "build then peak")
        self.assertEqual([m["name"] for m in data["mesocycles"]], ["Base"])

    def test_plan_show_without_a_goal_is_empty_not_an_error(self):
        res = self.client.get("/api/plan")
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.get_json()["goal"])

    def test_zones_rejects_a_bad_window_or_currency(self):
        self.assertEqual(self.client.get("/api/zones?weeks=0").status_code, 400)
        self.assertEqual(self.client.get("/api/zones?weeks=x").status_code, 400)
        self.assertEqual(self.client.get("/api/zones?currency=rpe").status_code, 400)

    def test_zones_empty_history_reports_no_sports(self):
        data = self.client.get("/api/zones").get_json()
        self.assertEqual(data["sports"], [])
        self.assertEqual(data["window"]["weeks"], 8)

    def test_zones_report_measured_seconds_for_a_recorded_sport(self):
        # One run with its HR zone seconds recorded, inside the default 8-week window.
        today = date.fromisoformat(trainmate_web.today_str())
        when = (today - timedelta(days=7)).isoformat()
        test_db.save_completed_activity(
            activity_id="z1", date=when, start_time=f"{when} 08:00:00",
            activity_name="Zone run", activity_type="running",
            duration_sec=3600, distance_km=10.0, elevation_gain_m=0.0,
            avg_hr=145, max_hr=170, rpe=None, tss=60.0,
            zone1_sec=600, zone2_sec=1800, zone3_sec=900, zone4_sec=300, zone5_sec=0,
        )
        # Naming the sport explicitly is the CLI's own override of the volume filter,
        # so the assertion does not depend on the machine's configured preferences.
        data = self.client.get("/api/zones?sport=running").get_json()

        running = next((s for s in data["sports"] if s["sport"] == "running"), None)
        self.assertIsNotNone(running, f"expected a running table, got {data['sports']}")
        self.assertEqual(running["currency"], "hr")
        self.assertEqual(len(running["zone_labels"]), 5)
        week = next(w for w in running["weeks"] if w["seconds"])
        self.assertEqual(sum(week["seconds"]), 3600)
        self.assertFalse(week["future"])


if __name__ == "__main__":
    unittest.main()
