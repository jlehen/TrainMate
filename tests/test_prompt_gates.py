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
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_prompt_gates.db")
test_db = bind_test_db(TEST_DB_PATH)

from trainmate.coach.engine import CoachEngine

# Sentinels for each region a gate controls, matched against the built prompt.
NOTE_INSTRUCTIONS = "### ATHLETE'S NOTE FOR TODAY"
NOTE_SCHEMA_MEMBER = '"new_constraints"'
NOTE_CLAUSE = "constraint from the athlete's note drove the change"
NOTE_DATA = "## ATHLETE'S NOTE FOR THIS ADAPTATION"
NOTE_SIGNAL_INSTRUCTIONS = "### RECORDING A DAILY SIGNAL FROM THE NOTE"
NOTE_SIGNAL_SCHEMA_MEMBER = '"new_signals"'

import trainmate.coach.engine.workouts as wk

VACATE_SECTION = "### RE-FILLING A DATE YOU VACATE"

DRIFT_INSTRUCTIONS = "### CORRECTING EXECUTION DRIFT"
DRIFT_BRANCH = "measured intensity distribution has diverged from its stated"
DRIFT_DATA = "## MEASURED INTENSITY DISTRIBUTION OF THE ACTIVE BLOCK"

STANDING_INSTRUCTIONS = "### THE SESSIONS THE ATHLETE IS ALREADY LOOKING AT"
STANDING_KEEP_MEMBER = '"keep": true'
STANDING_DROP_MEMBER = '"drop": true'
STANDING_REPLACES_MEMBER = '"replaces"'
STANDING_REASON_MEMBER = '"change_reason"'
STANDING_NOTE_MEMBER = '"athlete_note"'
STANDING_RULE = "unless it contradicts the athlete's profile, the plan, or a"
STANDING_DATA = "## SESSIONS ALREADY STANDING"
STANDING_SCOPE = "You must answer for every session listed here"

PAST_CONSTRAINTS_INSTRUCTIONS = "### WHAT ALREADY HAPPENED IN THIS BLOCK"
PAST_CONSTRAINTS_DATA = "## CONSTRAINTS EARLIER IN THIS BLOCK"

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


NOTE_REGIONS = (
    NOTE_INSTRUCTIONS, NOTE_SCHEMA_MEMBER, NOTE_CLAUSE, NOTE_DATA,
    NOTE_SIGNAL_INSTRUCTIONS, NOTE_SIGNAL_SCHEMA_MEMBER,
)


class TestAthleteNoteGate(unittest.TestCase):
    NOTE = "knee is sore, keep impact low"

    def test_a_note_reaches_every_region_it_governs(self):
        system, user = build_prompt(athlete_message=self.NOTE)
        whole = system + user
        for region in NOTE_REGIONS:
            with self.subTest(region=region):
                self.assertIn(region, whole)
        self.assertIn(self.NOTE, user, "the note itself must be in the data")

    def test_without_a_note_none_of_them_appear(self):
        system, user = build_prompt()
        whole = system + user
        for region in NOTE_REGIONS:
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
        # Its closing line — that an unchanged benchmark is not returned — is what
        # makes `workout_revision_apply`'s `clear_benchmark` inference sound, so the
        # section must reach the prompt intact rather than paraphrased.
        system, _user = build_prompt()
        self.assertIn(wk._benchmark_task(), system)


class TestStandingSessionsGate(unittest.TestCase):
    """`workout generate` must answer for the sessions the athlete has already been told
    about, so the instruction, the schema members that express an answer and the list
    itself are one gated region (DESIGN_plan_change_continuity.md §8)."""

    EASED = [{
        "date": "2026-06-05", "sport_type": "cycling", "title": "Easy Z2 Spin",
        "description": "[Easy Z2 Spin]\n40 min ERG-locked, no surges.",
        "duration_minutes": 40, "rpe": 3, "tss": 26,
        "planned_zone_currency": "power",
        "planned_zone1_sec": 600, "planned_zone2_sec": 1800,
        "adaptation_count": 1, "adapted_at": "2026-06-01",
        "original_duration_minutes": 90, "original_rpe": 7, "original_tss": 95,
        "modification_reason": "Cut to easy Z2 to shed intensity.",
    }]

    REGIONS_IN_SYSTEM = (
        STANDING_INSTRUCTIONS, STANDING_RULE, STANDING_KEEP_MEMBER,
        STANDING_DROP_MEMBER, STANDING_REPLACES_MEMBER, STANDING_REASON_MEMBER,
        STANDING_NOTE_MEMBER,
    )

    def test_a_standing_session_reaches_every_region_it_governs(self):
        system, user = build_generate_prompt(standing_workouts=self.EASED)
        for region in self.REGIONS_IN_SYSTEM:
            with self.subTest(region=region):
                self.assertIn(region, system)
        for region in (STANDING_DATA, STANDING_SCOPE):
            with self.subTest(region=region):
                self.assertIn(region, user)
        self.assertIn("Easy Z2 Spin", user, "the session itself must be in the data")

    def test_the_list_says_what_the_session_was_first_prescribed_as(self):
        """Load alone does not say the numbers are already reduced, and "do not compound"
        could not say from what. The first form can (§4.6)."""
        _system, user = build_generate_prompt(standing_workouts=self.EASED)
        self.assertIn(
            "[first prescribed as 90m, RPE 7, TSS 95 — eased once, most recently "
            "2 days ago]", user
        )
        self.assertIn("Cut to easy Z2 to shed intensity.", user)

    def test_the_committed_tag_follows_the_window(self):
        """A session inside the window is committed; one past it is in the list because
        the athlete added it, and must not claim to be committed (§4.6)."""
        _system, user = build_generate_prompt(
            standing_workouts=self.EASED, commitment_end="2026-06-05"
        )
        self.assertIn("[COMMITTED]", user)
        _system, user = build_generate_prompt(
            standing_workouts=self.EASED, commitment_end="2026-06-04"
        )
        self.assertNotIn("[COMMITTED]", user)

    def test_the_list_carries_the_intensity_target(self):
        """Duration and TSS fold intensity away — 40min steady and 12min hard inside 40min
        read the same, and whether a day still fits the week is a question about zones."""
        _system, user = build_generate_prompt(standing_workouts=self.EASED)
        self.assertIn("Target: ~10min recovery, ~30min endurance", user)

    def test_a_keep_is_told_not_to_restate_the_target(self):
        """Showing the target invites a revised one back on a KEEP. `_resolve_standing`
        discards it, so the only cost is tokens — say so rather than pay it."""
        system, _user = build_generate_prompt(standing_workouts=self.EASED)
        self.assertIn('carries no "planned_zone_sec"', system)

    def test_a_committed_session_ships_its_description(self):
        """A revision inside the window should be minimal rather than re-invented, which
        needs the prose the session already carries (§4.6)."""
        _system, user = build_generate_prompt(
            standing_workouts=self.EASED, commitment_end="2026-06-05"
        )
        self.assertIn("ERG-locked, no surges.", user)

    def test_a_session_past_the_window_does_not(self):
        """Past the window the coach answers for the session, it does not rewrite its
        interval structure — so the prose is not paid for."""
        _system, user = build_generate_prompt(standing_workouts=self.EASED)
        self.assertNotIn("ERG-locked, no surges.", user)

    def test_without_a_standing_session_none_of_them_appear(self):
        system, user = build_generate_prompt()
        whole = system + user
        for region in self.REGIONS_IN_SYSTEM + (STANDING_DATA, STANDING_SCOPE):
            with self.subTest(region=region):
                self.assertNotIn(region, whole)

    def test_an_empty_standing_list_counts_as_none(self):
        """A bare `workout generate` extends into empty days; it must not open a section
        that would then name no sessions."""
        system, user = build_generate_prompt(standing_workouts=[])
        self.assertNotIn(STANDING_INSTRUCTIONS, system + user)
        self.assertNotIn(STANDING_KEEP_MEMBER, system)

    def test_the_generate_schema_stays_well_formed_either_way(self):
        """The answer members are spliced ahead of `benchmark_type`, so their own trailing
        comma is what a bad splice loses and the block's last member runs on. (The schema
        mixes two annotation styles — comma after the value, or after the closing paren —
        so the member ahead of the splice is not asserted against one rule.)"""
        for standing in ([], self.EASED):
            with self.subTest(standing=bool(standing)):
                system, _user = build_generate_prompt(standing_workouts=standing)
                self.assertNotIn(",,", system)
                self.assertNotIn(",\n}", system)

        system, _user = build_generate_prompt(standing_workouts=self.EASED)
        block = system.split('"workouts": [')[1].split("\n    }")[0]
        member = block[block.index('"keep"'):].split('\n      "')[0]
        self.assertTrue(member.rstrip().endswith(","), msg=repr(member))
        self.assertGreater(
            block.index('"benchmark_type"'), block.rindex('"change_reason"'),
            "the answer members must sit ahead of the block's last member",
        )


class TestPastConstraintsGate(unittest.TestCase):
    """A constraint that ended earlier in the block is why a week went quiet, and the
    coach cannot see those days any other way (DESIGN_plan_change_continuity.md §6.1)."""

    PAST = [{
        "id": 4, "title": "Ill", "start_date": "2026-05-25", "end_date": "2026-05-29",
        "description": "Chest infection.", "rest": 1,
    }]

    def test_a_past_constraint_reaches_both_regions(self):
        system, user = build_generate_prompt(past_constraints=self.PAST)
        self.assertIn(PAST_CONSTRAINTS_INSTRUCTIONS, system)
        self.assertIn(PAST_CONSTRAINTS_DATA, user)
        self.assertIn("Ill", user)

    def test_without_one_neither_appears(self):
        system, user = build_generate_prompt()
        whole = system + user
        self.assertNotIn(PAST_CONSTRAINTS_INSTRUCTIONS, whole)
        self.assertNotIn(PAST_CONSTRAINTS_DATA, whole)


# The optional inputs whose regions are asserted above.
GATES_WITH_A_TEST = {
    "athlete_message", "intensity_context", "standing_workouts", "past_constraints",
}

# The rest of the two builders' optional inputs. Being here is not a claim that an input
# is harmless — only that nobody has written a gate test for it yet. It is a ledger, so
# `TestEveryOptionalPromptInputIsAccountedFor` can tell a *new* input from a known one.
GATES_WITHOUT_A_TEST = {
    # adapt
    "informational", "removed_workouts", "daily_signals", "performed",
    "pmc_warmup_cutoff", "pmc_context", "zone_currencies", "signal_vocabulary",
    "signal_earliest_date",
    # generate
    "num_days", "start_str", "metrics", "completed_activities", "baseline",
    "block_progress", "block_has_intensity", "anchor_history", "commitment_end",
}

BUILDERS = ("_workout_adapt_logic", "_workout_generate_logic")


def _optional_inputs():
    """Every optional parameter of the two prompt builders, read off their signatures."""
    import inspect

    names = set()
    for builder in BUILDERS:
        signature = inspect.signature(getattr(CoachEngine, builder))
        names.update(
            parameter.name for parameter in signature.parameters.values()
            if parameter.default is not inspect.Parameter.empty
        )
    return names


class TestEveryOptionalPromptInputIsAccountedFor(unittest.TestCase):
    """A twelfth gate added to a 380-line builder must not pass unnoticed.

    The sentinels above cover three inputs; the builders take twenty. Reading the
    parameter list off the signatures means a new one fails here until someone decides
    whether it governs prompt regions and needs a gate test of its own — which is the
    safe direction for a list to rot in.
    """

    def test_a_new_builder_input_must_be_classified(self):
        declared = GATES_WITH_A_TEST | GATES_WITHOUT_A_TEST
        unclassified = sorted(_optional_inputs() - declared)
        self.assertEqual(
            unclassified, [],
            "these optional prompt inputs are new since this ledger was written. If one "
            "switches whole prompt regions on and off, give it a gate test above and add "
            "it to GATES_WITH_A_TEST; otherwise record it in GATES_WITHOUT_A_TEST: "
            f"{unclassified}",
        )

    def test_the_ledger_does_not_name_inputs_that_are_gone(self):
        declared = GATES_WITH_A_TEST | GATES_WITHOUT_A_TEST
        departed = sorted(declared - _optional_inputs())
        self.assertEqual(
            departed, [],
            f"the builders no longer take these; drop them from the ledger: {departed}",
        )

    def test_the_two_halves_of_the_ledger_do_not_overlap(self):
        self.assertEqual(GATES_WITH_A_TEST & GATES_WITHOUT_A_TEST, set())


if __name__ == "__main__":
    unittest.main()
