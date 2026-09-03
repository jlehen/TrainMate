"""`plan feedback` as an append-only log (DESIGN_plan_feedback.md).

Capture, filing, listing, removal, the pending→consumed lifecycle, the one-off
migration off the two overwrite slots, and what the regeneration does with it all.
"""
import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db
from tests import test_db_path


def _days_out(n: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=n)).isoformat()


# A plan window needs its goal in the future, so a fixed date expires the tests the
# day it passes (same rot 2a7cd71 fixed in test_constraints.py).
GOAL_DATE = _days_out(71)

TEST_DB_PATH = test_db_path("test_trainmate_feedback.db")
LEGACY_DB_PATH = test_db_path("test_trainmate_fb_legacy.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service

# Three blocks around today, so a bare `-m`, a date atom and a name infix all have
# something to resolve to — and 'build' deliberately matches two of them.
BLOCKS = [
    {"name": "Base Building", "start_date": _days_out(-30), "end_date": _days_out(-1),
     "focus": "Zone 2"},
    {"name": "Build Specific", "start_date": _days_out(0), "end_date": _days_out(20),
     "focus": "Threshold"},
    {"name": "Climb-Specific Transmutation", "start_date": _days_out(21),
     "end_date": GOAL_DATE, "focus": "Vertical"},
]


class FeedbackTestCase(unittest.TestCase):
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

    def run_cli(self, args, input_value="n"):
        return run_cli(args, input_value)

    def _plan(self, blocks=None, strategy="Keep heart rate low"):
        """A goal with an active plan; returns (goal_id, macrocycle_id, mesocycles)."""
        obj_id = test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
        )
        macro_id = test_db.save_macrocycle(
            objective_id=obj_id, strategy=strategy, goals_hash="ghash",
            constraints_hash="chash", mesocycles=blocks or BLOCKS,
        )
        return obj_id, macro_id, test_db.get_mesocycles_for_macrocycle(macro_id)


class TestFeedbackCapture(FeedbackTestCase):
    """Append, list, remove — the selector-free capture path (§4)."""

    def test_bare_text_is_a_plan_level_note(self):
        obj_id, macro_id, _ = self._plan()
        exit_code, stdout, _ = self.run_cli(["plan", "feedback", "drop the second FTP test"])
        self.assertEqual(exit_code, 0)
        notes = test_db.list_plan_feedback(macro_id)
        self.assertEqual([n["text"] for n in notes], ["drop the second FTP test"])
        self.assertIsNone(notes[0]["mesocycle_id"])
        self.assertIn(f"Noted [id {notes[0]['id']}, plan-level]", stdout)
        self.assertIn("1 note pending", stdout)

    def test_notes_accumulate_instead_of_overwriting(self):
        obj_id, macro_id, _ = self._plan()
        self.run_cli(["plan", "feedback", "first thought"])
        exit_code, stdout, _ = self.run_cli(["plan", "feedback", "second thought"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [n["text"] for n in test_db.list_plan_feedback(macro_id)],
            ["first thought", "second thought"],
        )
        self.assertIn("2 notes pending", stdout)

    def test_bare_run_lists_oldest_first_and_writes_nothing(self):
        obj_id, macro_id, mesos = self._plan()
        test_db.add_plan_feedback(macro_id, "older note")
        test_db.add_plan_feedback(macro_id, "newer note", mesocycle_id=mesos[2]["id"])

        exit_code, stdout, _ = self.run_cli(["plan", "feedback"])
        self.assertEqual(exit_code, 0)
        self.assertLess(stdout.index("older note"), stdout.index("newer note"))
        self.assertIn("plan-level", stdout)
        self.assertIn("Climb-Specific Transmutation", stdout)
        self.assertEqual(len(test_db.list_plan_feedback(macro_id)), 2)

    def test_empty_listing_says_how_to_add_one(self):
        self._plan()
        exit_code, stdout, _ = self.run_cli(["plan", "feedback"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No feedback pending", stdout)
        self.assertIn("plan feedback", stdout)

    def test_rm_confirms_unless_yes(self):
        obj_id, macro_id, _ = self._plan()
        note_id = test_db.add_plan_feedback(macro_id, "a thought I take back")

        # Declined at the confirm: the note survives.
        exit_code, stdout, _ = self.run_cli(
            ["plan", "feedback", "--rm", str(note_id)], input_value="n"
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Removal cancelled", stdout)
        self.assertEqual(len(test_db.list_plan_feedback(macro_id)), 1)

        exit_code, stdout, _ = self.run_cli(
            ["plan", "feedback", "--rm", str(note_id), "-y"]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(test_db.list_plan_feedback(macro_id), [])

    def test_an_empty_note_is_refused_rather_than_stored(self):
        obj_id, macro_id, _ = self._plan()
        exit_code, stdout, _ = self.run_cli(["plan", "feedback", "   "])
        self.assertEqual(exit_code, 1)
        self.assertIn("the note is empty", stdout)
        self.assertEqual(test_db.list_plan_feedback(macro_id), [])

    def test_rm_of_an_unknown_note_fails(self):
        self._plan()
        exit_code, stdout, _ = self.run_cli(["plan", "feedback", "--rm", "999", "-y"])
        self.assertEqual(exit_code, 1)
        self.assertIn("No feedback note with ID 999", stdout)

    def test_rm_without_an_id_prints_the_help_and_the_missing_line(self):
        self._plan()
        exit_code, stdout, stderr = self.run_cli(["plan", "feedback", "--rm"])
        self.assertEqual(exit_code, 2)
        self.assertIn("--rm ID", stderr)
        self.assertIn("usage:", stderr)

    def test_no_plan_yet_is_an_error(self):
        exit_code, stdout, _ = self.run_cli(["plan", "feedback", "too hard"])
        self.assertEqual(exit_code, 1)
        self.assertIn("No active goals found", stdout)

        test_db.add_objective(
            title="Zurich Marathon", target_date=GOAL_DATE,
            sport_type="running",
        )
        exit_code, stdout, _ = self.run_cli(["plan", "feedback", "too hard"])
        self.assertEqual(exit_code, 1)
        self.assertIn("No active periodization plan exists", stdout)


class TestFeedbackFiling(FeedbackTestCase):
    """The `-m` atom, humanized (§5)."""

    def _file(self, *atom):
        return self.run_cli(["plan", "feedback", "a note", "-m", *atom])

    def _filed_block(self, macro_id):
        notes = test_db.list_plan_feedback(macro_id)
        return notes[-1]["mesocycle_name"] if notes else None

    def test_bare_m_files_to_the_block_containing_today(self):
        obj_id, macro_id, _ = self._plan()
        exit_code, stdout, _ = self.run_cli(["plan", "feedback", "a note", "-m"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(self._filed_block(macro_id), "Build Specific")
        self.assertIn("filed: Build Specific", stdout)

    def test_an_id_inside_the_plan_files_to_that_block(self):
        obj_id, macro_id, mesos = self._plan()
        exit_code, _, _ = self._file(str(mesos[0]["id"]))
        self.assertEqual(exit_code, 0)
        self.assertEqual(self._filed_block(macro_id), "Base Building")

    def test_an_id_outside_the_active_plan_is_refused(self):
        obj_id, macro_id, mesos = self._plan()
        exit_code, stdout, _ = self._file(str(max(m["id"] for m in mesos) + 50))
        self.assertEqual(exit_code, 1)
        self.assertIn("is not part of this plan", stdout)
        # The error lists the blocks to retry against, rather than a bare refusal.
        self.assertIn("Climb-Specific Transmutation", stdout)
        self.assertEqual(test_db.list_plan_feedback(macro_id), [])

    def test_an_id_from_another_goals_plan_names_that_goal(self):
        obj_id, macro_id, _ = self._plan()
        other_id = test_db.add_objective(
            title="Sierre-Zinal", target_date=_days_out(120),
            sport_type="running",
        )
        other_macro = test_db.save_macrocycle(
            objective_id=other_id, strategy="Other", goals_hash="g",
            constraints_hash="c",
            mesocycles=[{"name": "Their Base", "start_date": _days_out(72),
                         "end_date": _days_out(120), "focus": "Z2"}],
        )
        theirs = test_db.get_mesocycles_for_macrocycle(other_macro)[0]

        exit_code, stdout, _ = self._file(str(theirs["id"]))

        self.assertEqual(exit_code, 1)
        self.assertIn("belongs to the plan for 'Sierre-Zinal'", stdout)
        self.assertIn(f"-g {other_id}", stdout)

    def test_date_atoms_resolve_to_the_block_covering_that_day(self):
        obj_id, macro_id, _ = self._plan()
        for atom, expected in (
            ("-7d", "Base Building"),
            ("today", "Build Specific"),
            ("+4w", "Climb-Specific Transmutation"),
            (_days_out(25), "Climb-Specific Transmutation"),
        ):
            with self.subTest(atom=atom):
                clear_all_tables(test_db)
                obj_id, macro_id, _ = self._plan()
                exit_code, stdout, _ = self._file(atom)
                self.assertEqual(exit_code, 0, stdout)
                self.assertEqual(self._filed_block(macro_id), expected)

    def test_an_unsigned_span_is_refused_by_name(self):
        obj_id, macro_id, _ = self._plan()
        exit_code, stdout, _ = self._file("7d")
        self.assertEqual(exit_code, 1)
        self.assertIn("is a span, not a day", stdout)
        self.assertEqual(test_db.list_plan_feedback(macro_id), [])

    def test_a_range_is_refused_by_name(self):
        obj_id, macro_id, mesos = self._plan()
        exit_code, stdout, _ = self._file(f"{mesos[0]['id']}..{mesos[1]['id']}")
        self.assertEqual(exit_code, 1)
        self.assertIn("a note files to one block", stdout)

    def test_a_unique_name_infix_files_to_that_block(self):
        obj_id, macro_id, _ = self._plan()
        exit_code, stdout, _ = self._file("climb")
        self.assertEqual(exit_code, 0)
        self.assertEqual(self._filed_block(macro_id), "Climb-Specific Transmutation")

    def test_an_ambiguous_name_infix_lists_the_blocks(self):
        obj_id, macro_id, _ = self._plan()
        exit_code, stdout, _ = self._file("build")
        self.assertEqual(exit_code, 1)
        self.assertIn("matches several blocks", stdout)
        self.assertIn("Base Building", stdout)
        self.assertIn("Build Specific", stdout)
        self.assertEqual(test_db.list_plan_feedback(macro_id), [])

    def test_a_name_infix_matching_nothing_lists_the_blocks(self):
        obj_id, macro_id, _ = self._plan()
        exit_code, stdout, _ = self._file("taper")
        self.assertEqual(exit_code, 1)
        self.assertIn("No block name contains 'taper'", stdout)
        self.assertIn("Base Building", stdout)


class TestFeedbackLifecycle(FeedbackTestCase):
    """Pending := attached to the ACTIVE macrocycle; supersession consumes (§6)."""

    @patch("trainmate.coach.engine.openrouter_client")
    def test_regeneration_consumes_the_pending_notes(self, mock_client):
        obj_id, macro_id, mesos = self._plan()
        test_db.add_plan_feedback(macro_id, "drop the second FTP test")
        mock_client.complete.return_value = {
            "strategy": "New strategy",
            "mesocycles": [{"name": "Base", "start_date": _days_out(0),
                            "end_date": GOAL_DATE, "focus": "Aerobic"}],
        }

        coach_service.plan_generate(force=True)

        new_macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertNotEqual(new_macro["id"], macro_id)
        # Consumed means "the version they addressed is gone", not "deleted": the notes
        # stay attached to the now-superseded plan as history.
        self.assertEqual(test_db.list_plan_feedback(new_macro["id"]), [])
        self.assertEqual(len(test_db.list_plan_feedback(macro_id)), 1)

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_a_declined_preview_leaves_the_notes_pending(self, mock_coach, mock_garmin):
        obj_id, macro_id, _ = self._plan()
        test_db.add_plan_feedback(macro_id, "the Friday sessions should progress duration")
        mock_coach.config_changed.return_value = None
        mock_coach.plan_generate.return_value = {
            "strategy": "Proposed", "mesocycles": [], "reused": False,
            "goal": test_db.get_objective(obj_id),
        }

        exit_code, stdout, _ = self.run_cli(["plan", "generate"], input_value="n")

        self.assertEqual(exit_code, 0)
        self.assertIn("Plan discarded", stdout)
        mock_coach.plan_apply.assert_not_called()
        self.assertEqual(len(test_db.list_plan_feedback(macro_id)), 1)

    @patch("trainmate.runtime.calendar_syncer")
    @patch("trainmate.coach.engine.openrouter_client")
    def test_rollback_makes_the_notes_pending_again(self, mock_client, mock_calendar):
        obj_id, macro_id, _ = self._plan()
        test_db.add_plan_feedback(macro_id, "too much intensity")
        mock_client.complete.return_value = {
            "strategy": "New strategy",
            "mesocycles": [{"name": "Base", "start_date": _days_out(0),
                            "end_date": GOAL_DATE, "focus": "Aerobic"}],
        }
        coach_service.plan_generate(force=True)
        new_macro = test_db.get_macrocycle_for_objective(obj_id)

        coach_service.plan_rollback(objective_id=obj_id)

        # Rolling back abandons the version that addressed them, so their concerns reopen.
        active = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(active["id"], macro_id)
        self.assertEqual(
            [n["text"] for n in test_db.list_plan_feedback(active["id"])],
            ["too much intensity"],
        )
        self.assertEqual(test_db.list_plan_feedback(new_macro["id"]), [])


class TestFeedbackConsumption(FeedbackTestCase):
    """The regeneration gate and the prompt section it feeds (§7)."""

    @patch("trainmate.coach.engine.openrouter_client")
    def _generate(self, mock_client, **kwargs):
        mock_client.complete.return_value = {
            "strategy": "New strategy",
            "mesocycles": [{"name": "Base", "start_date": _days_out(0),
                            "end_date": GOAL_DATE, "focus": "Aerobic"}],
        }
        proposal = coach_service.plan_generate(**kwargs)
        prompt = (
            mock_client.complete.call_args[0][0]
            if mock_client.complete.call_args else ""
        )
        return proposal, prompt

    def test_pending_feedback_regenerates_without_force(self):
        obj_id, macro_id, _ = self._plan()
        # Same inputs as the plan in place: without feedback this reuses.
        with patch.object(
            coach_service.engine, "_get_goals_hash", return_value="ghash"
        ), patch.object(
            coach_service.engine, "_get_constraints_hash", return_value="chash"
        ), patch.object(coach_service, "config_changed", return_value=None):
            proposal, _ = self._generate()
            self.assertTrue(proposal["reused"])

            test_db.add_plan_feedback(macro_id, "drop the second FTP test")
            proposal, prompt = self._generate()

        self.assertFalse(proposal["reused"])
        self.assertIn("### ATHLETE FEEDBACK ON THE CURRENT PLAN", prompt)

    def test_the_prompt_lists_notes_oldest_first_with_phase_names(self):
        obj_id, macro_id, mesos = self._plan()
        test_db.add_plan_feedback(macro_id, "drop the second FTP test")
        test_db.add_plan_feedback(
            macro_id, "the Friday sessions should progress duration",
            mesocycle_id=mesos[2]["id"],
        )

        _, prompt = self._generate(force=True)

        self.assertIn("### ATHLETE FEEDBACK ON THE CURRENT PLAN", prompt)
        first = prompt.index("drop the second FTP test")
        second = prompt.index("the Friday sessions should progress duration")
        self.assertLess(first, second)
        self.assertIn('(phase: Climb-Specific Transmutation) "the Friday', prompt)
        # A plan-level note carries no phase marker.
        self.assertIn('] "drop the second FTP test"', prompt)

    def test_the_section_is_absent_when_nothing_is_pending(self):
        self._plan()
        _, prompt = self._generate(force=True)
        self.assertNotIn("ATHLETE FEEDBACK", prompt)


class TestFeedbackReplan(FeedbackTestCase):
    """`--replan` collapses append + regenerate, still behind the human `y` (§4)."""

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_replan_saves_then_reaches_the_confirm_gate(self, mock_coach, mock_garmin):
        obj_id, macro_id, _ = self._plan()
        mock_coach.config_changed.return_value = None
        mock_coach.plan_generate.return_value = {
            "strategy": "Proposed", "mesocycles": [], "reused": False,
            "goal": test_db.get_objective(obj_id),
        }

        exit_code, stdout, _ = self.run_cli(
            ["plan", "feedback", "ease the Fridays", "--replan"], input_value="n"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [n["text"] for n in test_db.list_plan_feedback(macro_id)],
            ["ease the Fridays"],
        )
        mock_coach.plan_generate.assert_called_once()
        # Nothing regenerates a plan without a `y`.
        mock_coach.plan_apply.assert_not_called()
        self.assertIn("Plan discarded", stdout)
        # --force is neither passed nor needed: pending feedback opens the gate (§7).
        self.assertFalse(mock_coach.plan_generate.call_args.kwargs["force"])


class TestFeedbackDisplay(FeedbackTestCase):
    """`plan show`, `plan diff` and `status` read the log (§8)."""

    def test_plan_show_lists_the_notes_after_the_strategy(self):
        obj_id, macro_id, mesos = self._plan()
        test_db.add_plan_feedback(macro_id, "overall too easy")
        test_db.add_plan_feedback(macro_id, "more vertical", mesocycle_id=mesos[2]["id"])

        exit_code, stdout, _ = self.run_cli(["plan", "show"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Athlete Feedback", stdout)
        self.assertIn("pending", stdout)
        self.assertIn("plan-level", stdout)
        self.assertIn("overall too easy", stdout)
        self.assertIn("more vertical", stdout)
        self.assertLess(
            stdout.index("Athlete Feedback"), stdout.index("Mesocycle Timeline")
        )

    @patch("trainmate.runtime.garmin")
    def test_status_counts_the_pending_notes(self, mock_garmin):
        obj_id, macro_id, _ = self._plan()
        test_db.add_plan_feedback(macro_id, "overall too easy")
        test_db.add_plan_feedback(macro_id, "and too long")

        exit_code, stdout, _ = self.run_cli(["status", "--no-pull"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Plan feedback: 2 pending", stdout)

    @patch("trainmate.coach.engine.openrouter_client")
    def test_plan_diff_lists_each_versions_notes(self, mock_client):
        obj_id, macro_id, _ = self._plan()
        test_db.add_plan_feedback(macro_id, "drop the second FTP test")
        mock_client.complete.return_value = {
            "strategy": "New strategy",
            "mesocycles": [{"name": "Base", "start_date": _days_out(0),
                            "end_date": GOAL_DATE, "focus": "Aerobic"}],
        }
        coach_service.plan_generate(force=True)

        exit_code, stdout, _ = self.run_cli(["plan", "diff"])

        self.assertEqual(exit_code, 0)
        panel = stdout.split("Athlete feedback:")[1].split("Mesocycles:")[0]
        # A: what drove the change; B: nothing pending on the new version yet.
        self.assertIn("drop the second FTP test", panel)
        self.assertIn("B:\n    none", panel)


class TestFeedbackMigration(unittest.TestCase):
    """The one-off move off the two overwrite slots (§6)."""

    def setUp(self):
        if os.path.exists(LEGACY_DB_PATH):
            os.remove(LEGACY_DB_PATH)
        self.addCleanup(
            lambda: os.path.exists(LEGACY_DB_PATH) and os.remove(LEGACY_DB_PATH)
        )

    def test_both_slot_kinds_are_backfilled_and_the_columns_dropped(self):
        conn = sqlite3.connect(LEGACY_DB_PATH)
        conn.executescript("""
            CREATE TABLE objectives (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                target_date TEXT NOT NULL, sport_type TEXT NOT NULL, description TEXT,
                priority INTEGER DEFAULT 1, status TEXT DEFAULT 'active');
            CREATE TABLE macrocycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, objective_id INTEGER NOT NULL,
                strategy TEXT NOT NULL, goals_hash TEXT NOT NULL,
                constraints_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                feedback TEXT DEFAULT NULL);
            CREATE TABLE mesocycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, macrocycle_id INTEGER NOT NULL,
                name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                focus TEXT NOT NULL, feedback TEXT DEFAULT NULL);
            INSERT INTO objectives (title, target_date, sport_type)
                VALUES ('Zurich Marathon', '2026-12-01', 'running');
            INSERT INTO macrocycles
                (objective_id, strategy, goals_hash, constraints_hash, created_at, feedback)
                VALUES (1, 'Keep it low', 'g', 'c', '2026-01-02T03:04:05+00:00',
                        'overall too easy');
            INSERT INTO mesocycles
                (macrocycle_id, name, start_date, end_date, focus, feedback)
                VALUES (1, 'Base', '2026-06-01', '2026-06-28', 'Z2', 'more speed');
            INSERT INTO mesocycles (macrocycle_id, name, start_date, end_date, focus)
                VALUES (1, 'Build', '2026-06-29', '2026-07-26', 'Threshold');
        """)
        conn.commit()
        conn.close()

        legacy = Database(db_path=LEGACY_DB_PATH)

        notes = legacy.list_plan_feedback(1)
        self.assertEqual(
            [(n["text"], n["mesocycle_name"]) for n in notes],
            [("overall too easy", None), ("more speed", "Base")],
        )
        # A mesocycle carries no timestamp; the parent macro's is the best available.
        self.assertTrue(all(n["created_at"] == "2026-01-02T03:04:05+00:00" for n in notes))

        with legacy._get_connection() as conn:
            for table in ("macrocycles", "mesocycles"):
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                self.assertNotIn("feedback", cols, table)


if __name__ == "__main__":
    unittest.main()
