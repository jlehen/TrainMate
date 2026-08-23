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

# TypedDict -> the table whose rows it annotates. Types with no table (PlanProposal,
# Workout) and tables with no TypedDict (settings, sync_state, ...) are deliberately
# absent.
TYPE_TABLES = {
    "Objective": "objectives",
    "Constraint": "constraints",
    "DailyContext": "daily_context",
    "CompletedActivity": "completed_activities",
    "AthleteMetric": "athlete_metrics_cache",
    "AthleteBaseline": "athlete_baselines",
    "Macrocycle": "macrocycles",
    "Mesocycle": "mesocycles",
}


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


if __name__ == "__main__":
    unittest.main()
