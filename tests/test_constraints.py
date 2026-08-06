"""Tests for the unified `constraint` directive object (DESIGN_constraints.md):
DB windowing, the deterministic rest-window pre-pass shared by generate/adapt, the §7
plan-magnitude heuristic, §8 message classification into a constraint row, and the rev-6
schema migration that collapsed hard/soft × sport × type to a single `rest` flag.
"""
import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_constraints.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)
trainmate.coach.service.db = test_db

from trainmate.coach import coach_service


def tearDownModule():
    """Four of the five classes here recreate the file without removing it, so the
    cleanup belongs to the module rather than to any one class."""
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except OSError:
            pass


class TestConstraintDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)
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

    def test_add_defaults_to_advisory(self):
        cid = test_db.add_constraint(title="x", start_date="2026-07-01", end_date="2026-07-01")
        self.assertEqual(test_db.get_constraint(cid)["rest"], 0)

    def test_add_rest_flag_persists(self):
        cid = test_db.add_constraint(title="surgery", start_date="2026-07-01",
                                     end_date="2026-07-03", rest=1)
        self.assertEqual(test_db.get_constraint(cid)["rest"], 1)


class TestRestWindowPrePass(unittest.TestCase):
    def _c(self, rest=0, start="2026-07-02", end="2026-07-02", title="c"):
        return {"rest": rest, "start_date": start, "end_date": end, "title": title}

    def test_generate_rest_forces_rest(self):
        workouts = [
            {"date": "2026-07-02", "sport_type": "running", "title": "Tempo",
             "duration_minutes": 60, "rpe": 7, "tss": 80},
            {"date": "2026-07-03", "sport_type": "running", "title": "Easy",
             "duration_minutes": 40, "rpe": 3, "tss": 30},
        ]
        out = coach_service._enforce_rest_windows_generate(
            workouts, [self._c(rest=1)], "2026-07-02", "2026-07-08"
        )
        by_date = {w["date"]: w for w in out}
        self.assertEqual(by_date["2026-07-02"]["sport_type"], "rest")
        # Other dates untouched.
        self.assertEqual(by_date["2026-07-03"]["sport_type"], "running")

    def test_generate_fills_rest_dates_the_model_omitted(self):
        """The §6 guarantee: `generate` never leaves a rest date empty. The model is shown
        the window as "no training", so it usually returns nothing for those dates — the
        pre-pass must still emit a Rest row, since adherence reads a missing row as an
        unplanned gap rather than planned rest (this is what the adapt path already does)."""
        workouts = [
            {"date": "2026-07-01", "sport_type": "running", "title": "Easy"},
            {"date": "2026-07-05", "sport_type": "running", "title": "Long"},
        ]
        out = coach_service._enforce_rest_windows_generate(
            workouts, [self._c(rest=1, start="2026-07-02", end="2026-07-04")],
            "2026-07-01", "2026-07-07"
        )
        by_date = {w["date"]: w for w in out}
        for day in ("2026-07-02", "2026-07-03", "2026-07-04"):
            self.assertEqual(by_date[day]["sport_type"], "rest", day)
        self.assertEqual(by_date["2026-07-01"]["sport_type"], "running")
        self.assertEqual(by_date["2026-07-05"]["sport_type"], "running")

    def test_generate_fills_a_rest_window_at_the_tail_of_the_range(self):
        """The span is the *requested* range, not the model's last returned date. A rest
        window covering the final days is answered with silence, so bounding by what came
        back would leave exactly those days empty — the gap this pass exists to close."""
        workouts = [{"date": "2026-07-01", "sport_type": "running", "title": "Easy"}]
        out = coach_service._enforce_rest_windows_generate(
            workouts, [self._c(rest=1, start="2026-07-02", end="2026-07-03")],
            "2026-07-01", "2026-07-03"
        )
        by_date = {w["date"]: w for w in out}
        self.assertEqual(sorted(by_date), ["2026-07-01", "2026-07-02", "2026-07-03"])
        for day in ("2026-07-02", "2026-07-03"):
            self.assertEqual(by_date[day]["sport_type"], "rest", day)

    def test_generate_does_not_invent_days_outside_the_span(self):
        """Filling is bounded by the requested span: dates before `gen_start` or after
        `gen_end` get no row, however far the constraint itself runs (§6)."""
        workouts = [{"date": "2026-07-03", "sport_type": "running", "title": "Easy"}]
        out = coach_service._enforce_rest_windows_generate(
            workouts, [self._c(rest=1, start="2026-07-01", end="2026-07-10")],
            "2026-07-02", "2026-07-04"
        )
        self.assertEqual(
            sorted(w["date"] for w in out), ["2026-07-02", "2026-07-03", "2026-07-04"]
        )

    def test_generate_advisory_is_untouched(self):
        """An advisory constraint (rest=0) — which is every non-rest directive, including
        the old hard+sport case — is left entirely to the LLM; the pre-pass never rewrites
        the generated list for it (§5/§6)."""
        workouts = [
            {"date": "2026-07-02", "sport_type": "running", "title": "Run"},
            {"date": "2026-07-02", "sport_type": "strength_training", "title": "Lift"},
        ]
        out = coach_service._enforce_rest_windows_generate(
            workouts, [self._c(rest=0)], "2026-07-02", "2026-07-08"
        )
        self.assertEqual(out, workouts)

    def test_adapt_rest_eases_planned_session_to_rest(self):
        planned = [{"date": "2026-07-02", "sport_type": "running", "title": "Tempo"}]
        # LLM proposed nothing; the pre-pass must still add a rest for the rest date.
        out = coach_service._enforce_rest_windows_adapt(
            [], planned, [self._c(rest=1)], completed_keys=set(), from_date="2026-07-02"
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sport_type"], "rest")
        self.assertIn("change_reason", out[0])

    def test_adapt_advisory_is_untouched(self):
        """An advisory constraint does not touch the LLM's proposal — only a `rest`
        window is eased to rest deterministically (§5)."""
        planned = [{"date": "2026-07-02", "sport_type": "running", "title": "Tempo"}]
        proposed = [{"date": "2026-07-02", "sport_type": "running", "title": "Easy Run"}]
        out = coach_service._enforce_rest_windows_adapt(
            proposed, planned, [self._c(rest=0)],
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
        out = coach_service._enforce_rest_windows_adapt(
            [], planned, [self._c(rest=1, start="2026-07-01", end="2026-07-05")],
            completed_keys=completed, from_date="2026-07-02"
        )
        self.assertEqual(out, [])  # nothing to ease


class TestConstraintPlanImpact(unittest.TestCase):
    """The concrete §7 magnitude heuristic: relative displaced-load trigger (vs the
    plan's trailing weekly planned load) OR a rest-window floor — either firing proposes
    a replan. No per-session "importance" term (see DESIGN_constraints.md §7/§11)."""

    @classmethod
    def setUpClass(cls):
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)
        trainmate.coach.service.db = test_db

    def setUp(self):
        clear_all_tables(test_db)
        # `constraint_plan_impact` measures from max(start, today), so a fixture whose
        # window sits in the past displaces nothing. Pin the clock to the eve of these
        # dates rather than making them relative: the trailing-week arithmetic below is
        # only readable when the dates are literal.
        patcher = patch("trainmate.coach.service._today_str", return_value="2026-07-31")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_displaced_load_trigger_fires_even_when_advisory(self):
        # Trailing week (2026-07-25..2026-07-31, the 7 days before the constraint
        # starts) carries 200 TSS of planned load.
        test_db.save_workout(date="2026-07-28", sport_type="running",
                             title="Long Run", description="", tss=200)
        # The constraint's 3-day window overlaps 120 TSS — 60% of the trailing week,
        # over the default 50% threshold.
        test_db.save_workout(date="2026-08-02", sport_type="running",
                             title="Tempo", description="", tss=120)
        c = {"rest": 0, "start_date": "2026-08-01",
             "end_date": "2026-08-03", "title": "big trip"}
        impact = coach_service.constraint_plan_impact(c)
        self.assertGreaterEqual(impact["displaced_pct"], 50)
        self.assertTrue(coach_service.constraint_is_plan_shaping(c, impact))

    def test_small_displaced_load_is_not_plan_shaping(self):
        test_db.save_workout(date="2026-07-28", sport_type="running",
                             title="Long Run", description="", tss=200)
        test_db.save_workout(date="2026-08-01", sport_type="running",
                             title="Easy", description="", tss=20)  # 10% of the trailing week
        c = {"rest": 0, "start_date": "2026-08-01",
             "end_date": "2026-08-01", "title": "no run"}
        impact = coach_service.constraint_plan_impact(c)
        self.assertLess(impact["displaced_pct"], 50)
        self.assertFalse(coach_service.constraint_is_plan_shaping(c, impact))

    def test_rest_window_floor_fires_regardless_of_load(self):
        # No workouts at all — zero displaced load — but a 3-day rest window still
        # trips the independent rest-window floor (default replan_rest_span_days=3).
        c = {"rest": 1, "start_date": "2026-08-01",
             "end_date": "2026-08-03", "title": "injury"}
        impact = coach_service.constraint_plan_impact(c)
        self.assertEqual(impact["displaced_pct"], 0.0)
        self.assertEqual(impact["days"], 3)
        self.assertTrue(coach_service.constraint_is_plan_shaping(c, impact))

    def test_short_rest_window_below_floor_is_not_plan_shaping(self):
        c = {"rest": 1, "start_date": "2026-08-01",
             "end_date": "2026-08-01", "title": "no run"}
        impact = coach_service.constraint_plan_impact(c)
        self.assertFalse(coach_service.constraint_is_plan_shaping(c, impact))

    def test_single_advisory_day_with_no_history_is_not_plan_shaping(self):
        c = {"rest": 0, "start_date": "2026-08-01",
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
        rebind_test_db(test_db)
        trainmate.coach.service.db = test_db

    def setUp(self):
        clear_all_tables(test_db)
        # `adapt` refuses without a plan (DESIGN_block_boundary.md §6); these cases are
        # about the note, not the block, so give them one wide enough to ignore.
        obj_id = test_db.add_objective(
            title="Background goal", target_date="2026-12-31",
            sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="General preparation.",
            goals_hash="bg", constraints_hash="bg",
            mesocycles=[{
                "name": "Base", "start_date": "2026-01-01",
                "end_date": "2026-12-31", "focus": "Aerobic base",
            }],
        )

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
                "end_date": "2026-07-09", "description": None,
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

    def test_capture_creates_row_and_always_advisory(self):
        """Trust boundary (§8): the LLM can never mark an extracted constraint as a
        deterministic rest window — capture_message_constraint ignores any `rest` the
        candidate might carry and always writes rest=0."""
        candidate = {
            "title": "can't train Thursday", "start_date": "2026-07-09",
            "end_date": "2026-07-09", "rest": 1,  # must be ignored
        }
        cid = coach_service.capture_message_constraint(candidate, "2026-07-02")
        self.assertIsNotNone(cid)
        rows = test_db.get_constraints()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "message")
        self.assertEqual(rows[0]["rest"], 0)
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


class TestConstraintMigration(unittest.TestCase):
    """The rev-6 schema migration (db/base.py): a pre-rev-6 constraints table
    (binding/sport/type, no `rest`) is upgraded in place — `hard` + no sport becomes
    rest=1, every other row becomes advisory (rest=0), and the three legacy columns are
    dropped. Idempotent DDL, so it runs automatically in _init_db."""

    @classmethod
    def setUpClass(cls):
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)
        trainmate.coach.service.db = test_db

    def setUp(self):
        clear_all_tables(test_db)

    def _install_legacy_table(self, rows):
        with test_db._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS constraints")
            cur.execute("""
                CREATE TABLE constraints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                    binding TEXT NOT NULL, sport TEXT, type TEXT,
                    title TEXT NOT NULL, description TEXT,
                    replan INTEGER NOT NULL DEFAULT 0, source TEXT, created TEXT
                )
            """)
            for r in rows:
                cur.execute(
                    "INSERT INTO constraints "
                    "(start_date, end_date, binding, sport, type, title, replan) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    r
                )
            conn.commit()

    def test_migration_maps_binding_sport_to_rest_and_drops_columns(self):
        # (start, end, binding, sport, type, title, replan)
        self._install_legacy_table([
            ("2026-08-01", "2026-08-03", "hard", None, "injury", "broke ankle", 1),
            ("2026-08-05", "2026-08-05", "hard", "running", "trip", "no run today", 0),
            ("2026-08-06", "2026-08-06", "soft", None, None, "easy week", 0),
        ])
        # Re-run init (idempotent) to apply the rev-6 migration on the legacy table.
        test_db._init_db()

        rows = {r["title"]: r for r in test_db.get_constraints()}
        self.assertEqual(rows["broke ankle"]["rest"], 1)     # hard + no sport -> rest
        self.assertEqual(rows["no run today"]["rest"], 0)    # hard + sport   -> advisory
        self.assertEqual(rows["easy week"]["rest"], 0)       # soft           -> advisory
        # Other fields survive untouched.
        self.assertEqual(rows["broke ankle"]["replan"], 1)

        with test_db._get_connection() as conn:
            cols = [r["name"] for r in
                    conn.execute("PRAGMA table_info(constraints)").fetchall()]
        self.assertIn("rest", cols)
        for gone in ("binding", "sport", "type"):
            self.assertNotIn(gone, cols)

    def test_migration_is_idempotent(self):
        self._install_legacy_table([
            ("2026-08-01", "2026-08-03", "hard", None, None, "layoff", 0),
        ])
        test_db._init_db()
        test_db._init_db()  # second pass must be a no-op, not an error
        rows = test_db.get_constraints()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rest"], 1)


if __name__ == "__main__":
    unittest.main()
