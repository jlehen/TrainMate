"""The row TypedDicts must describe the tables they are annotations for.

They had drifted ~17 columns behind `workouts` and 5 behind `macrocycles`, which is why
DB reads are written `dict(row)  # type: ignore` — code that works is code the checker
would reject. Comparing against `PRAGMA table_info` freezes the alignment: adding a
column without declaring it fails here rather than silently widening the gap.

`Workout` is the exception, and deliberately so: it no longer describes a table. Under
DESIGN_workout_revisions.md §5 it describes the dict `get_workouts()` returns — a live
revision hydrated with what its lineage derives — so it is pinned against a session
written and read back instead. Same honesty property, checked against the thing the
TypedDict now claims to describe.
"""
import os
import tempfile
import unittest

from trainmate import types
from trainmate.db import Database

# TypedDict -> the table whose rows it annotates. Tables with no TypedDict (settings,
# sync_state, ...) are deliberately absent; a TypedDict missing from here has to appear
# in one of the two sets below instead, which `TestEveryTypedDictIsClassified` enforces.
TYPE_TABLES = {
    "Objective": "objectives",
    "Constraint": "constraints",
    "DailySignal": "daily_signals",
    "CompletedActivity": "completed_activities",
    "AthleteMetric": "athlete_metrics_cache",
    "AthleteBaseline": "athlete_baselines",
    "Macrocycle": "macrocycles",
    "Mesocycle": "mesocycles",
}

# Describe a hydrated read rather than a table, so each is pinned against the dict its
# accessor actually returns — see the two classes at the bottom of this file.
HYDRATED_TYPES = {"Workout", "PlanFeedback"}

# Not a database read at all: a proposal the coach hands back before anything is stored.
NON_ROW_TYPES = {"PlanProposal"}


def _typed_dict_names():
    """Every TypedDict `trainmate.types` declares, by the shape rather than by a list."""
    found = []
    for name in dir(types):
        member = getattr(types, name)
        if isinstance(member, type) and hasattr(member, "__total__") \
                and hasattr(member, "__annotations__"):
            found.append(name)
    return sorted(found)


class TestEveryTypedDictIsClassified(unittest.TestCase):
    """A TypedDict added tomorrow is checked tomorrow, not whenever someone remembers.

    `TYPE_TABLES` was a hand-kept map and had already fallen behind: `PlanFeedback` was
    added later and nothing in the suite checked it against anything.
    """

    def test_a_new_typed_dict_must_be_declared_a_row_a_hydrated_dict_or_neither(self):
        classified = set(TYPE_TABLES) | HYDRATED_TYPES | NON_ROW_TYPES
        unclassified = [n for n in _typed_dict_names() if n not in classified]
        self.assertEqual(
            unclassified, [],
            "these TypedDicts are checked by nothing — add each to TYPE_TABLES (it "
            "annotates a table), HYDRATED_TYPES (it describes what an accessor returns, "
            f"and pin it against that read), or NON_ROW_TYPES: {unclassified}",
        )

    def test_the_classifications_do_not_overlap_or_name_something_absent(self):
        every = set(_typed_dict_names())
        for label, group in (("TYPE_TABLES", set(TYPE_TABLES)),
                             ("HYDRATED_TYPES", HYDRATED_TYPES),
                             ("NON_ROW_TYPES", NON_ROW_TYPES)):
            with self.subTest(group=label):
                self.assertEqual(
                    group - every, set(),
                    f"{label} names types that no longer exist: {group - every}",
                )
        self.assertEqual(set(TYPE_TABLES) & HYDRATED_TYPES, set())
        self.assertEqual(HYDRATED_TYPES & NON_ROW_TYPES, set())


class TestTypedDictsMatchSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp()
        cls.db = Database(db_path=os.path.join(cls._dir, "schema_check.db"))

    def _columns(self, table):
        with self.db._get_connection() as conn:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]

    def test_every_column_is_declared_and_every_declaration_is_a_column(self):
        for type_name, table in TYPE_TABLES.items():
            with self.subTest(type=type_name, table=table):
                columns = self._columns(table)
                self.assertTrue(columns, f"table {table} does not exist")
                declared = list(getattr(types, type_name).__annotations__)

                undeclared = [c for c in columns if c not in declared]
                self.assertEqual(
                    undeclared, [],
                    f"{type_name} is missing columns present in {table}: {undeclared}",
                )
                phantom = [d for d in declared if d not in columns]
                self.assertEqual(
                    phantom, [],
                    f"{type_name} declares fields absent from {table}: {phantom}",
                )

    def test_declaration_order_follows_the_table(self):
        """Order is not correctness, but keeping it makes the two readable side by side
        and makes a missed column obvious in review."""
        for type_name, table in TYPE_TABLES.items():
            with self.subTest(type=type_name, table=table):
                self.assertEqual(
                    list(getattr(types, type_name).__annotations__),
                    self._columns(table),
                )


class TestWorkoutMatchesTheHydratedDict(unittest.TestCase):
    """`Workout` describes what `get_workouts()` returns, so that is what it is checked
    against (DESIGN_workout_revisions.md §5)."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp()
        cls.db = Database(db_path=os.path.join(cls._dir, "hydrated_check.db"))
        with cls.db.workout_change(kind="generate", summary="pin") as change:
            change.append(
                date="2026-07-01", sport_type="running", title="Run",
                description="easy", duration_minutes=45, rpe=4, tss=40,
            )
        cls.hydrated = cls.db.get_workouts()[0]

    def test_the_hydrated_dict_has_exactly_the_declared_keys(self):
        self.assertEqual(list(self.hydrated), list(types.Workout.__annotations__))

    def test_the_lineage_id_is_what_the_athlete_sees(self):
        """`id` is the lineage, `revision_id` the physical row. On a first revision they
        are equal, which is what makes an athlete-visible id stable from the start."""
        self.assertEqual(self.hydrated["id"], self.hydrated["revision_id"])


class TestPlanFeedbackMatchesTheHydratedDict(unittest.TestCase):
    """`PlanFeedback` describes what `list_plan_feedback()` returns, not the table:
    `mesocycle_name` is joined in, because a phase name survives the version churn its
    id does not (DESIGN_plan_feedback.md §7). So it is pinned against that read."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp()
        cls.db = Database(db_path=os.path.join(cls._dir, "feedback_check.db"))
        objective_id = cls.db.add_objective(
            title="Zurich Marathon", target_date="2027-04-18", sport_type="running",
        )
        cls.macro_id = cls.db.save_macrocycle(
            objective_id=objective_id, strategy="Build then sharpen",
            goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": "Base", "start_date": "2026-07-01",
                         "end_date": "2026-07-28", "focus": "Z2"}],
        )
        block = cls.db.get_mesocycles_for_macrocycle(cls.macro_id)[0]
        cls.db.add_plan_feedback(cls.macro_id, "plan-level note")
        cls.db.add_plan_feedback(cls.macro_id, "block note", mesocycle_id=block["id"])
        cls.notes = cls.db.list_plan_feedback(cls.macro_id)

    def test_the_hydrated_dict_has_exactly_the_declared_keys(self):
        for note in self.notes:
            with self.subTest(text=note["text"]):
                self.assertEqual(list(note), list(types.PlanFeedback.__annotations__))

    def test_the_joined_block_name_is_present_and_none_for_a_plan_level_note(self):
        by_text = {note["text"]: note for note in self.notes}
        self.assertIsNone(by_text["plan-level note"]["mesocycle_name"])
        self.assertEqual(by_text["block note"]["mesocycle_name"], "Base")


if __name__ == "__main__":
    unittest.main()
