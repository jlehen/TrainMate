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

CARRY_INSTRUCTIONS = "### CARRYING OVER AN ALREADY-EASED SESSION"
CARRY_SCHEMA_MEMBER = '"keep": true'
CARRY_DATA = "## SESSIONS ALREADY EASED BY AN ADAPTATION"
CARRY_SCOPE = "These are the only sessions you should try to carry over"

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


GENERATE_BASE = dict(
    objectives=[{"id": 1, "title": "Race", "target_date": "2026-09-01",
                 "sport_type": "running"}],
    constraints=[],
    today_str="2026-06-03",
    guidelines="Guidelines text.",
    profile={"max_hr": 185},
    strategy="Build aerobic base.",
    meso_text="  - Base (2026-06-01 to 2026-06-28): Aerobic\n",
    learnings="No learnings.",
)


def build_generate_prompt(**extra):
    """The (system, user) pair the generate call would send."""
    engine = CoachEngine()
    with patch("trainmate.coach.engine.openrouter_client") as client:
        client.complete.return_value = {"reasoning": "ok", "workouts": []}
        with patch("builtins.print"):
            engine._workout_generate_logic(**GENERATE_BASE, **extra)
        return client.complete.call_args[0][0], client.complete.call_args[0][1]


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
    (DESIGN_adapt_task_prompt.md §2)."""

    def test_the_vacate_rule_is_always_on_in_the_adapt_task(self):
        for extra in ({}, {"athlete_message": "knee is sore"},
                      {"intensity_context": "Base 2 — focus \"volume\""}):
            with self.subTest(extra=sorted(extra)):
                system, _user = build_prompt(**extra)
                self.assertIn(VACATE_SECTION, system)


class TestTheStandingRules(unittest.TestCase):
    """Which rules the adapt TASK is given, asserted against the constants it is built
    from rather than against quoted prose: rewording a rule should not break a test
    whose subject is which rules are present."""

    def test_adapt_gets_five_standing_rules_including_the_two_named_ones(self):
        system, _user = build_prompt()
        self.assertIn(wk.RULE_MOVE_FIRST, system)
        self.assertIn(wk.RULE_BLOCK_NOT_YOURS, system)
        # Adapt's other three are written inline at its call site — counted, not quoted,
        # so rewording one does not break a test about how many rules the model is given.
        rules = system.split("### STANDING RULES")[1].split("###")[0]
        self.assertIn("\n5. ", rules)
        self.assertNotIn("\n6. ", rules)

    def test_the_benchmark_section_is_present_whole(self):
        # Its closing line — that an unchanged benchmark need not be returned — is what
        # makes `workout_revision_apply`'s `clear_benchmark` inference sound, so the
        # section must reach the prompt intact rather than paraphrased.
        system, _user = build_prompt()
        self.assertIn(wk._benchmark_task(), system)


class TestCarriedAdaptationsGate(unittest.TestCase):
    """`workout generate` rewrites the horizon from scratch, so a session a prior adapt
    eased survives only if it is carried in — and the instruction telling the model what
    to do with the list must arrive with the list."""

    EASED = [{
        "date": "2026-06-05", "sport_type": "cycling", "title": "Easy Z2 Spin",
        "description": "[Easy Z2 Spin]\n40 min ERG-locked, no surges.",
        "duration_minutes": 40, "rpe": 3, "tss": 26,
        "adaptation_count": 1, "adapted_at": "2026-06-01",
        "modification_reason": "Cut to easy Z2 to shed intensity.",
    }]

    def test_a_carried_session_reaches_every_region_it_governs(self):
        system, user = build_generate_prompt(carried_workouts=self.EASED)
        for region in (CARRY_INSTRUCTIONS, CARRY_SCHEMA_MEMBER):
            with self.subTest(region=region):
                self.assertIn(region, system)
        for region in (CARRY_DATA, CARRY_SCOPE):
            with self.subTest(region=region):
                self.assertIn(region, user)
        self.assertIn("Easy Z2 Spin", user, "the session itself must be in the data")

    def test_the_list_carries_the_easing_tag_and_its_reason(self):
        """Load alone does not say the numbers are already reduced — the tag does, and
        the reason is what lets the model judge whether the easing still applies."""
        _system, user = build_generate_prompt(carried_workouts=self.EASED)
        self.assertIn(
            "[ALREADY EASED by a prior adaptation (once, most recently 2 days ago)", user
        )
        self.assertIn("Cut to easy Z2 to shed intensity.", user)

    def test_the_description_is_not_shipped(self):
        """A KEEP identifies the session rather than copying it, so the description stays
        out — sending it would be paying for text the model is told not to reproduce."""
        _system, user = build_generate_prompt(carried_workouts=self.EASED)
        self.assertNotIn("ERG-locked, no surges.", user)

    def test_without_a_carried_session_none_of_them_appear(self):
        system, user = build_generate_prompt()
        whole = system + user
        for region in (CARRY_INSTRUCTIONS, CARRY_SCHEMA_MEMBER, CARRY_DATA, CARRY_SCOPE):
            with self.subTest(region=region):
                self.assertNotIn(region, whole)

    def test_an_empty_carry_list_counts_as_none(self):
        """Nothing eased in the horizon is the ordinary case; it must not open a section
        that would then name no sessions."""
        system, user = build_generate_prompt(carried_workouts=[])
        self.assertNotIn(CARRY_INSTRUCTIONS, system + user)
        self.assertNotIn(CARRY_SCHEMA_MEMBER, system)

    def test_the_generate_schema_stays_well_formed_either_way(self):
        """`keep` is spliced ahead of `benchmark_type`, so its own trailing comma is what
        a bad splice loses and the block's last member runs on. (The schema mixes two
        annotation styles — comma after the value, or after the closing paren — so the
        member ahead of the splice is not asserted against one rule.)"""
        for carried in ([], self.EASED):
            with self.subTest(carried=bool(carried)):
                system, _user = build_generate_prompt(carried_workouts=carried)
                self.assertNotIn(",,", system)
                self.assertNotIn(",\n}", system)

        system, _user = build_generate_prompt(carried_workouts=self.EASED)
        block = system.split('"workouts": [')[1].split("\n    }")[0]
        member = block[block.index('"keep"'):].split('\n      "')[0]
        self.assertTrue(member.rstrip().endswith(","), msg=repr(member))
        self.assertIn('"benchmark_type"', block.split('"keep"')[1],
                      "keep must sit ahead of the block's last member")


if __name__ == "__main__":
    unittest.main()
