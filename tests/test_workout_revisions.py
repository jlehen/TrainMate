"""The append-only workouts log: what it guarantees, and what it exists to protect.

DESIGN_workout_revisions.md §14. Two kinds of test live here.

The structural ones read the source and name the offender at review time. The trigger
stops a stray write at runtime, but a rule that spans files needs a test that spans files
(AGENTS.md), and by the time SQLite raises, the author has already shipped the call.

The behaviour ones each pin something the design claims. The one that matters most is the
first: the DO NOT COMPOUND guard surviving a swap is the reason `lineage_id` exists at
all, and it fails on slot chains alone.
"""
import ast
import os
import pathlib
import sqlite3
import tempfile
import unittest

from tests.helpers import rebind_test_db, save_workout
from trainmate.coach.formatting import format_planned_workouts_detailed
from trainmate.db import Database

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB_DIR = ROOT / "trainmate/db"

# The one name allowed to write `workouts` outside the append path: the `data wipe` reset,
# which is a reset rather than a write path to convert (§14). The one-off rebuild is
# exempt by living in `scripts/`, outside the directory this glob walks.
EXEMPT_FUNCTIONS = ("wipe_workouts",)

# Where reads of the raw table are legitimate: the append path itself, which owns both
# the history surfaces and the slot lookup every append starts from.
APPEND_PATH_MODULE = "workouts.py"

# `_init_db` names `workouts` because it DEFINES the live view over it. That is the DDL
# that makes the rule enforceable, not a read that dodges it.
READ_EXEMPT_FUNCTIONS = ("_init_db",)


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _sql_strings(node):
    """Every string literal in `node`, joined the way an f-string or an implicit
    concatenation would read, so a statement split across lines is still seen."""
    parts = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    return " ".join(parts).lower()


class TestWorkoutsTableIsAppendOnly(unittest.TestCase):
    """`workouts` is never updated and never deleted from (§2).

    Keyed on a glob over `trainmate/db/`, not a list of filenames, so a module written
    tomorrow is covered tomorrow and not whenever someone remembers it."""

    def test_no_module_updates_or_deletes_workouts(self):
        offenders = []
        for path in sorted(DB_DIR.glob("*.py")):
            tree = ast.parse(path.read_text())
            for fn in _functions(tree):
                if fn.name in EXEMPT_FUNCTIONS:
                    continue
                sql = _sql_strings(fn)
                if "delete from workouts" in sql:
                    offenders.append(f"{path.name}:{fn.name} deletes from workouts")
                if "update workouts set" not in sql:
                    continue
                # The one exemption the §14 trigger also grants: seeding a first
                # revision's lineage with its own id, before commit.
                if "set lineage_id = id" in sql:
                    continue
                offenders.append(f"{path.name}:{fn.name} updates workouts")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_trigger_refuses_an_update_and_a_delete(self):
        db = _fresh_db(self)
        save_workout(db, "2026-09-01", "running", "Run", "30 min")
        with db._get_connection() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE workouts SET title = 'edited'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM workouts")

    def test_the_lineage_exemption_cannot_graft_a_row_onto_another_lineage(self):
        """The trigger's `WHEN` clause pins the value the one allowed UPDATE may write:
        the row's own id, and nothing else."""
        db = _fresh_db(self)
        save_workout(db, "2026-09-01", "running", "Run", "30 min")
        with db._get_connection() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE workouts SET lineage_id = 999")

    def test_reads_go_through_the_live_view(self):
        """Only the append path and the declared history readers touch the raw table;
        everything else reads `live_workouts`, or it would see dead revisions."""
        offenders = []
        for path in sorted(DB_DIR.glob("*.py")):
            if path.name == APPEND_PATH_MODULE:
                continue
            tree = ast.parse(path.read_text())
            for fn in _functions(tree):
                if fn.name in EXEMPT_FUNCTIONS + READ_EXEMPT_FUNCTIONS:
                    continue
                if "from workouts" in _sql_strings(fn):
                    offenders.append(f"{path.name}:{fn.name} selects from workouts")
        self.assertEqual(offenders, [], "\n".join(offenders))


def _fresh_db(testcase) -> Database:
    directory = tempfile.mkdtemp()
    db = Database(db_path=os.path.join(directory, "revisions.db"))
    rebind_test_db(db)
    return db


class TestTheGuardSurvivesASwap(unittest.TestCase):
    """The §4 scenario, end to end.

    Tuesday holds a long ride, Thursday an easy spin. Two bad mornings ease the ride
    twice, then a swap moves it to Thursday. Thursday's SLOT has seen one generate and one
    swap and no adapts at all, so slot chains alone would report a session never eased —
    and the coach would cut a session already cut twice, on the exact morning recovery is
    worst.
    """

    def setUp(self):
        self.db = _fresh_db(self)
        with self.db.workout_change(kind="generate", summary="v1") as change:
            change.append(date="2026-09-01", sport_type="cycling", title="Long ride",
                          description="90 min", duration_minutes=90, tss=110)
            change.append(date="2026-09-03", sport_type="cycling", title="Easy spin",
                          description="45 min", duration_minutes=45, tss=30)
        for minutes, load in ((75, 90), (65, 80)):
            with self.db.workout_change(kind="adapt", summary="HRV down") as change:
                change.append(date="2026-09-01", sport_type="cycling", title="Long ride",
                              description=f"{minutes} min", duration_minutes=minutes,
                              tss=load, reason="Eased")
        self._swap("2026-09-01", "2026-09-03")

    def _swap(self, date_a: str, date_b: str) -> None:
        a = self.db.get_workout(date_a, "cycling")
        b = self.db.get_workout(date_b, "cycling")
        with self.db.workout_change(kind="swap") as change:
            for session, destination in ((b, date_a), (a, date_b)):
                change.append(
                    date=destination, sport_type=session["sport_type"],
                    title=session["title"], description=session["description"],
                    duration_minutes=session["duration_minutes"], tss=session["tss"],
                    lineage_id=session["id"],
                    reason=f"Swapped from {session['date']} to {destination}",
                )

    def test_the_rendered_prompt_tag_still_says_already_eased(self):
        """The tag is the behaviour this design exists to protect, so the tag is what is
        asserted: checking `adaptation_count == 2` alone would pass at the db layer while
        a kind-gated `_adapt_recency_tag` still returned "" (§7)."""
        moved = self.db.get_workout("2026-09-03", "cycling")
        self.assertEqual(moved["title"], "Long ride")
        self.assertEqual(moved["adaptation_count"], 2)

        rendered = format_planned_workouts_detailed([moved], eval_date="2026-09-03")
        self.assertIn("ALREADY EASED", rendered)
        self.assertIn("2x", rendered)
        self.assertIn("do not compound", rendered)

    def test_the_marker_reflects_the_latest_change_and_the_count_stands_beside_it(self):
        """No precedence rule any more: a session eased twice and then swapped renders
        both facts (§12)."""
        from trainmate.cli.workouts._helpers import modification_markers
        moved = self.db.get_workout("2026-09-03", "cycling")
        self.assertEqual(modification_markers(moved), ["SWAPPED", "ADAPTED ×2"])


class TestRevisionBehaviour(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db(self)

    def _generate(self, *sessions, summary="v1"):
        with self.db.workout_change(kind="generate", summary=summary) as change:
            for date, sport, title, minutes in sessions:
                change.append(date=date, sport_type=sport, title=title,
                              description=f"{minutes} min", duration_minutes=minutes,
                              tss=minutes)
        return self.db.get_workout_changes()[0]["id"]

    def _revisions(self):
        return self.db.get_plan_revisions()

    def test_an_adapt_undone_by_rollback_stops_counting(self):
        """The `restored_from` jump: an adapt rolled back a minute later must not leave
        the guard shouting ALREADY EASED at a session running at full prescription (§7)."""
        self._generate(("2026-09-01", "cycling", "Long ride", 90))
        with self.db.workout_change(kind="adapt", summary="HRV down") as change:
            change.append(date="2026-09-01", sport_type="cycling", title="Long ride",
                          description="65 min", duration_minutes=65, tss=65,
                          reason="Eased")
        adapt_change = self.db.get_workout_changes()[0]["id"]
        self.assertEqual(
            self.db.get_workout("2026-09-01", "cycling")["adaptation_count"], 1
        )

        self.db.rollback_to_change(adapt_change, "2026-09-01")

        back = self.db.get_workout("2026-09-01", "cycling")
        self.assertEqual(back["duration_minutes"], 90)
        self.assertEqual(back["adaptation_count"], 0)
        self.assertEqual(format_planned_workouts_detailed([back], eval_date="2026-09-01")
                         .count("ALREADY EASED"), 0)

    def test_rollback_is_point_in_time(self):
        """Undoing a change reverts it AND everything after it. Undoing one change in
        isolation is unsound the moment a later one touched the same session: the "undo"
        would restore the pre-adapt copy on its old day while the swapped copy stayed
        live on the new one — one session, live twice (§10)."""
        self._generate(("2026-09-01", "cycling", "Long ride", 90),
                       ("2026-09-03", "cycling", "Easy spin", 45))
        with self.db.workout_change(kind="adapt", summary="HRV down") as change:
            change.append(date="2026-09-01", sport_type="cycling", title="Long ride",
                          description="65 min", duration_minutes=65, tss=65)
        adapt_change = self.db.get_workout_changes()[0]["id"]
        ride = self.db.get_workout("2026-09-01", "cycling")
        spin = self.db.get_workout("2026-09-03", "cycling")
        with self.db.workout_change(kind="swap") as change:
            for session, destination in ((spin, "2026-09-01"), (ride, "2026-09-03")):
                change.append(date=destination, sport_type="cycling",
                              title=session["title"], description=session["description"],
                              duration_minutes=session["duration_minutes"],
                              tss=session["tss"], lineage_id=session["id"])

        self.db.rollback_to_change(adapt_change, "2026-09-01")

        live = self.db.get_workouts()
        self.assertEqual([(w["date"], w["title"], w["duration_minutes"]) for w in live],
                         [("2026-09-01", "Long ride", 90), ("2026-09-03", "Easy spin", 45)])
        # No lineage is live in two slots.
        self.assertEqual(len({w["id"] for w in live}), len(live))

    def test_a_session_appended_over_a_void_starts_a_new_lineage(self):
        """Appending over a void is a new session, not a resurrection of the removed one —
        which would otherwise inherit its Calendar event, its originals and its tally (§4)."""
        self._generate(("2026-09-01", "cycling", "Long ride", 90))
        removed = self.db.get_workout("2026-09-01", "cycling")
        self.db.mark_workout_pushed(removed["id"], "evt-old", "sig")
        with self.db.workout_change(kind="rm") as change:
            change.void(date="2026-09-01", sport_type="cycling", reason="work trip")

        self._generate(("2026-09-01", "cycling", "Threshold", 60), summary="v2")

        fresh = self.db.get_workout("2026-09-01", "cycling")
        self.assertNotEqual(fresh["id"], removed["id"])
        self.assertEqual(fresh["adaptation_count"], 0)
        self.assertIsNone(fresh["google_event_id"])
        self.assertEqual(fresh["original_description"], "60 min")

    def test_a_generate_over_a_manual_session_replaces_it_and_says_so(self):
        """The plan owns the horizon, so the generate proceeds — under a new lineage, and
        naming what it replaced so the athlete can undo it (§4, §12)."""
        save_workout(self.db, "2026-09-01", "cycling", "My own ride", "60 min",
                     duration_minutes=60, source="manual")
        manual = self.db.get_workout("2026-09-01", "cycling")
        self.assertEqual(manual["source"], "manual")

        with self.db.workout_change(kind="generate", summary="v2") as change:
            change.append(date="2026-09-01", sport_type="cycling", title="Threshold",
                          description="75 min", duration_minutes=75, tss=90)
            replaced = list(change.replaced_manual)
            generate_change = change.id
        self.assertEqual([r["title"] for r in replaced], ["My own ride"])

        planned = self.db.get_workout("2026-09-01", "cycling")
        self.assertEqual(planned["source"], "generated")
        self.assertNotEqual(planned["id"], manual["id"])

        self.db.rollback_to_change(generate_change, "2026-09-01")
        back = self.db.get_workout("2026-09-01", "cycling")
        self.assertEqual(back["title"], "My own ride")
        self.assertEqual(back["source"], "manual")

    def test_regenerating_an_unchanged_horizon_appends_nothing(self):
        """Make regeneration churn the thing you read when you ask what happened to
        Tuesday, and the answer is six identical rows and one real change (§9)."""
        self._generate(("2026-09-01", "cycling", "Long ride", 90),
                       ("2026-09-03", "running", "Tempo", 45))
        before = len(self._revisions())

        self._generate(("2026-09-01", "cycling", "Long ride", 90),
                       ("2026-09-03", "running", "Tempo", 45), summary="v1 again")

        self.assertEqual(len(self._revisions()), before)
        # The pass is still recorded, flagged as what it was.
        self.assertTrue(self.db.get_workout_changes()[0]["held"])

    def test_restoring_leaves_the_revision_it_replaced_in_the_chain(self):
        self._generate(("2026-09-01", "cycling", "Long ride", 90))
        session = self.db.get_workout("2026-09-01", "cycling")
        with self.db.workout_change(kind="rm") as change:
            change.void(date="2026-09-01", sport_type="cycling", reason="ill")
        with self.db.workout_change(kind="restore") as change:
            change.restore(self.db.revision_before_live_void(session["id"]))

        back = self.db.get_workout("2026-09-01", "cycling")
        self.assertFalse(back["removed"])
        self.assertEqual(back["id"], session["id"])
        # Three revisions: the generate, the void, and the copy that outranks it.
        chain = [r for r in self._revisions() if r["lineage_id"] == session["id"]]
        self.assertEqual([bool(r["void"]) for r in chain], [False, True, False])

    def test_an_adapt_that_drops_a_session_leaves_a_void_a_rollback_can_undo(self):
        """The clearest correctness win here: this path used to `DELETE` the row, with no
        way back (§11)."""
        self._generate(("2026-09-01", "cycling", "Long ride", 90))
        with self.db.workout_change(kind="adapt", summary="Too much") as change:
            change.void(date="2026-09-01", sport_type="cycling", reason="Dropped")
        drop = self.db.get_workout_changes()[0]["id"]
        self.assertEqual(self.db.get_workouts(), [])

        self.db.rollback_to_change(drop, "2026-09-01")

        self.assertEqual([w["title"] for w in self.db.get_workouts()], ["Long ride"])

    def test_a_cross_sport_swap_voids_both_sources_and_lands_both_sessions(self):
        """Four appends where there used to be two date updates. The extra two are the
        honest cost of saying out loud that two slots became empty (§4)."""
        self._generate(("2026-09-01", "cycling", "Long ride", 90),
                       ("2026-09-03", "running", "Tempo", 45))
        ride = self.db.get_workout("2026-09-01", "cycling")
        run = self.db.get_workout("2026-09-03", "running")
        with self.db.workout_change(kind="swap") as change:
            change.void(date="2026-09-01", sport_type="cycling", reason="Swapped away")
            change.void(date="2026-09-03", sport_type="running", reason="Swapped away")
            change.append(date="2026-09-01", sport_type="running", title=run["title"],
                          description=run["description"], duration_minutes=45, tss=45,
                          lineage_id=run["id"])
            change.append(date="2026-09-03", sport_type="cycling", title=ride["title"],
                          description=ride["description"], duration_minutes=90, tss=90,
                          lineage_id=ride["id"])

        live = self.db.get_workouts()
        self.assertEqual([(w["date"], w["sport_type"]) for w in live],
                         [("2026-09-01", "running"), ("2026-09-03", "cycling")])
        # Each session kept its own identity across the move.
        self.assertEqual({w["id"] for w in live}, {ride["id"], run["id"]})
        # The lineage speaks through the copy, not the void it left behind.
        self.assertEqual(len(self.db.get_workouts(include_removed=True)), 2)

    def test_a_cross_sport_substitution_carries_the_session_across_sports(self):
        """An adapt that swaps a session's sport is a move, not a deletion and an
        insertion: the destination carries the source's lineage, so the tally follows
        it (§4/§11)."""
        from trainmate.coach.proposals import RevisionProposal
        import trainmate.coach

        self._generate(("2026-09-01", "strength_training", "Heavy lift", 60))
        with self.db.workout_change(kind="adapt", summary="Sore") as change:
            change.append(date="2026-09-01", sport_type="strength_training",
                          title="Heavy lift", description="45 min",
                          duration_minutes=45, tss=45)
        lift = self.db.get_workout("2026-09-01", "strength_training")
        self.assertEqual(lift["adaptation_count"], 1)

        service = trainmate.coach.CoachService(db_instance=self.db)
        service.workout_revision_apply(RevisionProposal(
            reason="Swap the lift for mobility.",
            # Same load, so the substitution is not itself an easing and the assertion
            # below is about identity alone.
            workouts=[{
                "date": "2026-09-01", "sport_type": "yoga", "title": "Mobility",
                "description": "45 min", "modification_reason": "Too sore to lift",
                "duration_minutes": 45, "rpe": 2, "tss": 45, "benchmark_type": None,
            }],
            range_start="2026-09-01", range_end="2026-09-07",
        ))

        live = self.db.get_workouts()
        self.assertEqual([(w["date"], w["sport_type"]) for w in live],
                         [("2026-09-01", "yoga")])
        # Same session, different sport: the id the athlete reads has not churned, and
        # the easing that already happened still describes it.
        self.assertEqual(live[0]["id"], lift["id"])
        self.assertEqual(live[0]["adaptation_count"], 1)

    def test_two_substitutions_on_one_day_do_not_share_a_lineage(self):
        """A displaced session can become ONE of the sessions replacing it. Handing its
        lineage to both would leave one session live in two slots (§10)."""
        from trainmate.coach.proposals import RevisionProposal
        import trainmate.coach

        self._generate(("2026-09-01", "strength_training", "Heavy lift", 60))
        service = trainmate.coach.CoachService(db_instance=self.db)
        service.workout_revision_apply(RevisionProposal(
            reason="Two easy sessions instead.",
            workouts=[
                {"date": "2026-09-01", "sport_type": "yoga", "title": "Mobility",
                 "description": "30 min", "modification_reason": "Sore",
                 "duration_minutes": 30, "rpe": 2, "tss": 10, "benchmark_type": None},
                {"date": "2026-09-01", "sport_type": "running", "title": "Shakeout",
                 "description": "20 min", "modification_reason": "Sore",
                 "duration_minutes": 20, "rpe": 3, "tss": 15, "benchmark_type": None},
            ],
            range_start="2026-09-01", range_end="2026-09-07",
        ))

        live = self.db.get_workouts()
        self.assertEqual(len(live), 2)
        self.assertEqual(len({w["id"] for w in live}), 2)

    def test_rm_by_lineage_id_acts_on_the_live_revision_after_an_adapt(self):
        """With raw revision ids, `workout rm 42` after an adapt would mark a dead
        revision and report success — a lie. The id the athlete reads is the id the
        command needs (§5)."""
        self._generate(("2026-09-01", "cycling", "Long ride", 90))
        printed_id = self.db.get_workouts()[0]["id"]
        with self.db.workout_change(kind="adapt", summary="HRV down") as change:
            change.append(date="2026-09-01", sport_type="cycling", title="Long ride",
                          description="65 min", duration_minutes=65, tss=65)

        live = self.db.get_workout_by_id(printed_id)
        self.assertEqual(live["id"], printed_id)
        self.assertEqual(live["duration_minutes"], 65)
        self.assertNotEqual(live["revision_id"], printed_id)


if __name__ == "__main__":
    unittest.main()
