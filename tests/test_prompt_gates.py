"""An optional prompt feature must appear in every place it belongs, or in none.

`_workout_adapt_logic` has two gated features, and each controls three or four regions
that sit hundreds of lines apart in a 388-line builder: instructions telling the model
how to weigh the thing, the data block containing it, a schema member for what to say
about it, and a clause spliced into shared wording. That they move together was
guaranteed by comments. Half-applying one is the failure that matters — the model is
told to read a section that was never sent, or is sent data it was never told to use —
and it is silent, so it is asserted here.
"""
import os
import unittest
from unittest.mock import patch

from tests.helpers import bind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_prompt_gates.db")
test_db = bind_test_db(TEST_DB_PATH)

from trainmate.coach.engine import CoachEngine

# Sentinels for each region a gate controls, matched against the built prompt.
NOTE_INSTRUCTIONS = "### ATHLETE'S NOTE FOR TODAY"
NOTE_SCHEMA_MEMBER = '"new_constraints"'
NOTE_CLAUSE = "constraint from the athlete's note drove the change"
NOTE_DATA = "## ATHLETE'S NOTE FOR THIS ADAPTATION"

import trainmate.coach.engine.workouts as wk

VACATE_SECTION = "### RE-FILLING A DATE YOU VACATE"

DRIFT_INSTRUCTIONS = "### CORRECTING EXECUTION DRIFT"
DRIFT_BRANCH = "measured intensity distribution has diverged from its stated"
DRIFT_DATA = "## MEASURED INTENSITY DISTRIBUTION OF THE ACTIVE BLOCK"

BASE = dict(
    history_days=7,
    start_date_str="2026-05-28",
    constraints=[],
    metrics=[{"date": "2026-06-03", "rhr": 50, "hrv": 70,
              "sleep_score": 80, "stress": 20}],
    baseline_str="RHR 50 +/- 2",
    planned_workouts=[{"date": "2026-06-03", "sport_type": "running",
                       "title": "Tempo", "description": "40min",
                       "duration_minutes": 40, "rpe": 6, "tss": 45}],
    completed_activities=[],
    target_date_str="2026-06-03",
    meso_end_date_str="2026-06-28",
    objectives=[{"id": 1, "title": "Race", "target_date": "2026-09-01",
                 "sport_type": "running"}],
    guidelines="Guidelines text.",
    profile={"max_hr": 185},
    strategy="Build aerobic base.",
    meso_text="  - Base (2026-06-01 to 2026-06-28): Aerobic\n",
    learnings="No learnings.",
    discrepancies=[],
)


def build_prompt(**extra):
    """The (system, user) pair the adapt call would send."""
    engine = CoachEngine()
    with patch("trainmate.coach.engine.openrouter_client") as client:
        client.complete.return_value = {
            "change_needed": False, "reason": "ok", "adapted_workouts": [],
        }
        with patch("builtins.print"):
            engine._workout_adapt_logic(**BASE, **extra)
        system, user = client.complete.call_args[0][0], client.complete.call_args[0][1]
    return system, user


class TestAthleteNoteGate(unittest.TestCase):
    NOTE = "knee is sore, keep impact low"

    def test_a_note_reaches_every_region_it_governs(self):
        system, user = build_prompt(athlete_message=self.NOTE)
        whole = system + user
        for region in (NOTE_INSTRUCTIONS, NOTE_SCHEMA_MEMBER, NOTE_CLAUSE, NOTE_DATA):
            with self.subTest(region=region):
                self.assertIn(region, whole)
        self.assertIn(self.NOTE, user, "the note itself must be in the data")

    def test_without_a_note_none_of_them_appear(self):
        system, user = build_prompt()
        whole = system + user
        for region in (NOTE_INSTRUCTIONS, NOTE_SCHEMA_MEMBER, NOTE_CLAUSE, NOTE_DATA):
            with self.subTest(region=region):
                self.assertNotIn(region, whole)

    def test_a_blank_note_counts_as_no_note(self):
        """Whitespace is not intent; a blank note must not open the section."""
        system, user = build_prompt(athlete_message="   \n  ")
        self.assertNotIn(NOTE_INSTRUCTIONS, system + user)


class TestExecutionDriftGate(unittest.TestCase):
    CONTEXT = "Z1 2h00  Z2 3h00"

    def test_drift_context_reaches_every_region_it_governs(self):
        system, user = build_prompt(intensity_context=self.CONTEXT)
        whole = system + user
        for region in (DRIFT_INSTRUCTIONS, DRIFT_BRANCH, DRIFT_DATA):
            with self.subTest(region=region):
                self.assertIn(region, whole)
        self.assertIn(self.CONTEXT, user)

    def test_without_drift_context_none_of_them_appear(self):
        system, user = build_prompt()
        whole = system + user
        for region in (DRIFT_INSTRUCTIONS, DRIFT_BRANCH, DRIFT_DATA):
            with self.subTest(region=region):
                self.assertNotIn(region, whole)


class TestTheGatesAreIndependent(unittest.TestCase):
    def test_one_feature_does_not_switch_the_other_on(self):
        system, user = build_prompt(athlete_message="sore knee")
        self.assertNotIn(DRIFT_INSTRUCTIONS, system + user)

        system, user = build_prompt(intensity_context="Z1 2h00")
        self.assertNotIn(NOTE_INSTRUCTIONS, system + user)

    def test_both_together_produce_both(self):
        system, user = build_prompt(
            athlete_message="sore knee", intensity_context="Z1 2h00"
        )
        whole = system + user
        for region in (NOTE_INSTRUCTIONS, NOTE_DATA, DRIFT_INSTRUCTIONS, DRIFT_DATA):
            with self.subTest(region=region):
                self.assertIn(region, whole)


class TestTheSchemaStaysWellFormed(unittest.TestCase):
    """The response schema is assembled from string fragments joined with commas, so a
    gated member is exactly where punctuation desyncs."""

    def test_the_schema_block_has_no_double_or_trailing_comma(self):
        for extra in ({}, {"athlete_message": "sore knee"}):
            with self.subTest(extra=list(extra)):
                system, _ = build_prompt(**extra)
                self.assertNotIn(",,", system)
                self.assertNotIn(",\n}", system)


class TestAlwaysOnSections(unittest.TestCase):
    """Not every shared section is gated — the vacate rule fires on almost every pass,
    so its failure mode is being absent, not being half-applied
    (DESIGN_constraint_reschedule.md §9)."""

    def test_the_vacate_rule_is_always_on_in_the_adapt_task(self):
        for extra in ({}, {"athlete_message": "knee is sore"},
                      {"intensity_context": "Base 2 — focus \"volume\""}):
            with self.subTest(extra=sorted(extra)):
                system, _user = build_prompt(**extra)
                self.assertIn(VACATE_SECTION, system)

    def test_the_vacate_rule_is_always_on_in_the_accommodate_task(self):
        engine = CoachEngine()
        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = {}
            with patch("builtins.print"):
                engine._workout_accommodate_logic(
                    range_start="2026-06-08", range_end="2026-06-22",
                    constraint_titles=["Away"], planned_workouts=[],
                    objectives=[], constraints=[], guidelines="G", profile={},
                    strategy="S", meso_text="  - Base", learnings="L",
                )
            system = client.complete.call_args[0][0]
        self.assertIn(wk._vacate_task(), system)


class TestSharedSectionsAreParametrized(unittest.TestCase):
    """The two revision prompts share their benchmark and standing-rules text, so the
    thing worth pinning is that each renders ITS OWN scope (§9).

    Asserted against the constants the prompt is built from, not against quoted prose:
    rewording a rule should not break a test whose subject is which rules are present."""

    def _accommodate_prompt(self):
        engine = CoachEngine()
        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = {}
            with patch("builtins.print"):
                engine._workout_accommodate_logic(
                    range_start="2026-06-08", range_end="2026-06-22",
                    constraint_titles=["Away"], planned_workouts=[],
                    objectives=[], constraints=[], guidelines="G", profile={},
                    strategy="S", meso_text="  - Base", learnings="L",
                )
            return client.complete.call_args[0][0]

    def test_adapt_gets_five_standing_rules_including_the_two_shared_ones(self):
        system, _user = build_prompt()
        self.assertIn(wk.RULE_MOVE_FIRST, system)
        self.assertIn(
            wk.RULE_BLOCK_NOT_YOURS.format(signals=wk.ADAPT_BLOCK_SIGNALS), system
        )
        # Adapt's own three are written inline at its call site — counted, not quoted, so
        # rewording one does not break a test about how many rules the model is given.
        rules = system.split("### STANDING RULES")[1].split("###")[0]
        self.assertIn("\n5. ", rules)
        self.assertNotIn("\n6. ", rules)

    def test_the_window_pass_gets_only_the_two_that_bound_a_change(self):
        # Rule 3's specifics ride on the "change_reason" schema field, and rules 4-5 speak
        # to an adherence window and a metrics lag this pass is never given.
        system = self._accommodate_prompt()
        self.assertIn(wk.RULE_MOVE_FIRST, system)
        self.assertIn(
            wk.RULE_BLOCK_NOT_YOURS.format(signals=wk.WINDOW_BLOCK_SIGNALS), system
        )
        rules = system.split("### STANDING RULES")[1].split("###")[0]
        self.assertNotIn("\n3. ", rules)

    def test_each_prompt_gets_its_own_benchmark_scope(self):
        # Adapt's postponement escape promises a block boundary that a mid-block window
        # has no claim on, so the two wordings must not cross over.
        adapt, _user = build_prompt()
        window = self._accommodate_prompt()
        self.assertIn(wk._benchmark_task(in_block=True), adapt)
        self.assertIn(wk._benchmark_task(in_block=False), window)
        self.assertNotIn(wk._benchmark_task(in_block=False), adapt)
        self.assertNotIn(wk._benchmark_task(in_block=True), window)

    def test_the_window_pass_is_metric_blind(self):
        system = self._accommodate_prompt()
        for absent in ("METRICS HISTORY", "BASELINE REFERENCE", "PRESCRIBING INTENSITY",
                       "ADHERENCE DISCREPANCIES", "DO NOT COMPOUND"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, system)


if __name__ == "__main__":
    unittest.main()
