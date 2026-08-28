"""Proposals carry the facts they were computed from.

Both cases here are drift bugs the old shape allowed: a preview that re-derived the
apply-time rule, and a fingerprint recomputed after the athlete had a chance to edit
the inputs. See trainmate/coach/proposals.py.
"""
import json
import os
import unittest

from tests.helpers import clear_all_tables, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_proposals.db")

from trainmate.db import Database
from trainmate.coach.proposals import RevisionProposal, PlanFingerprints
from trainmate.coach.revisions import normalize_load_fields, pair_revisions

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service


class TestPairAdaptations(unittest.TestCase):
    """The one implementation of "which session does this proposal replace"."""

    @staticmethod
    def _planned(date, sport, title):
        return {"date": date, "sport_type": sport, "title": title}

    def test_same_sport_proposal_pairs_with_its_original(self):
        proposal = self._planned("2026-06-10", "running", "Easy Run")
        existing = self._planned("2026-06-10", "running", "Interval Session")

        pairs, removals = pair_revisions([proposal], [existing])

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].original["title"], "Interval Session")
        self.assertFalse(pairs[0].is_swap)
        self.assertEqual(removals, ())

    def test_sport_swap_pairs_with_the_session_it_displaces(self):
        """A swap carries a new sport_type, so there is no same-sport original — the
        row would otherwise show "[None]" and hide what was replaced."""
        proposal = self._planned("2026-06-10", "yoga", "Restorative Yoga")
        existing = self._planned("2026-06-10", "strength", "Heavy Lower")

        pairs, removals = pair_revisions([proposal], [existing])

        self.assertEqual(pairs[0].original["title"], "Heavy Lower")
        self.assertTrue(pairs[0].is_swap)
        self.assertEqual(removals, ())

    def test_alias_spellings_are_matched_canonically(self):
        proposal = self._planned("2026-06-10", "strength_training", "Lighter Lift")
        existing = self._planned("2026-06-10", "strength", "Heavy Lower")

        pairs, removals = pair_revisions([proposal], [existing])

        self.assertFalse(pairs[0].is_swap, "an alias is the same sport, not a swap")
        self.assertEqual(pairs[0].original["title"], "Heavy Lower")
        self.assertEqual(removals, ())

    def test_an_overridden_session_with_no_replacement_is_a_removal(self):
        """Apply deletes it, so the preview has to show it."""
        proposal = self._planned("2026-06-10", "yoga", "Restorative Yoga")
        existing = [
            self._planned("2026-06-10", "strength", "Heavy Lower"),
            self._planned("2026-06-10", "cycling", "Long Ride"),
        ]

        pairs, removals = pair_revisions([proposal], existing)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(len(removals), 1)
        self.assertIn(removals[0]["title"], {"Heavy Lower", "Long Ride"})
        self.assertNotEqual(removals[0]["title"], pairs[0].original["title"])


    def test_a_held_session_is_neither_paired_nor_removed(self):
        """A session the coach kept is spoken for. Left out of the list, the swap rule
        below reads it as overridden and apply voids it — the bug that deleted a lift
        the model had explicitly asked to keep (DESIGN_workout_revisions.md §9.1)."""
        proposal = self._planned("2026-06-10", "cycling", "Climb Threshold — Indoors")
        existing = [
            self._planned("2026-06-10", "cycling", "Climb Threshold — Outdoors"),
            self._planned("2026-06-10", "strength", "Kettlebell Full-Body"),
        ]

        pairs, removals = pair_revisions(
            [proposal], existing, held=[("2026-06-10", "strength_training")]
        )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].original["title"], "Climb Threshold — Outdoors")
        self.assertFalse(pairs[0].is_swap)
        self.assertEqual(removals, (), "the held lift was offered up for deletion")

    def test_without_the_hold_the_same_day_session_is_a_removal(self):
        """The other half of the case above: nothing named means nothing kept."""
        proposal = self._planned("2026-06-10", "cycling", "Climb Threshold — Indoors")
        existing = [
            self._planned("2026-06-10", "cycling", "Climb Threshold — Outdoors"),
            self._planned("2026-06-10", "strength", "Kettlebell Full-Body"),
        ]

        _pairs, removals = pair_revisions([proposal], existing)

        self.assertEqual([r["title"] for r in removals], ["Kettlebell Full-Body"])

    def test_a_proposal_on_an_empty_date_has_no_original(self):
        pairs, removals = pair_revisions(
            [self._planned("2026-06-11", "running", "Extra Run")], []
        )
        self.assertIsNone(pairs[0].original)
        self.assertFalse(pairs[0].is_swap)


class TestLoadFieldsAreIntegers(unittest.TestCase):
    """A model that answers `"tss": 24.0` proposes the same load as a stored `24`, but the
    preview rendered `TSS24 -> TSS24.0` and an unchanged number read as a change."""

    def test_a_float_load_becomes_the_integer_the_column_stores(self):
        workouts = [{"duration_minutes": 45.0, "rpe": 5.0, "tss": 24.0}]

        normalize_load_fields(workouts)

        self.assertEqual(workouts[0], {"duration_minutes": 45, "rpe": 5, "tss": 24})
        for value in workouts[0].values():
            self.assertIsInstance(value, int)

    def test_a_fractional_load_rounds_rather_than_truncating(self):
        workouts = [{"tss": 24.6}]
        normalize_load_fields(workouts)
        self.assertEqual(workouts[0]["tss"], 25)

    def test_integers_and_missing_fields_are_left_alone(self):
        workouts = [{"duration_minutes": 45, "tss": None}]
        normalize_load_fields(workouts)
        self.assertEqual(workouts[0], {"duration_minutes": 45, "tss": None})


class TestRevisionProposalCarriesItsRange(unittest.TestCase):
    def test_the_range_is_the_window_evaluated_not_the_proposal_span(self):
        """The CLI used to rebuild the range from min/max of the proposal dates and hand
        that to apply, which deletes overridden sessions across it. A single proposal
        therefore produced a one-day range, so a session displaced later in the block
        was never removed — preview and apply disagreeing about what disappears."""
        proposal = RevisionProposal(
            reason="Ease the week",
            workouts=[{"date": "2026-06-10", "sport_type": "running", "title": "Easy"}],
            new_constraints=[],
            range_start="2026-06-10",
            range_end="2026-06-30",
        )

        spans = [w["date"] for w in proposal.workouts]
        self.assertEqual(proposal.range_end, "2026-06-30")
        self.assertNotEqual(proposal.range_end, max(spans))


class TestPlanFingerprintsSurviveTheAcceptStep(unittest.TestCase):
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

    def test_editing_a_goal_after_generating_does_not_rewrite_the_fingerprint(self):
        """Persisting a hash of the *current* goals would describe data the strategy was
        never generated against, marking a stale plan current."""
        goal_id = test_db.add_objective(
            title="Autumn Marathon", target_date="2026-11-15",
            sport_type="running",
        )
        generated = PlanFingerprints(
            goals_hash="hash-at-generate-time",
            constraints_hash="constraints-at-generate-time",
            config_hash="config-at-generate-time",
            config_snapshot=json.dumps({"ftp": 250}),
            goals_snapshot=json.dumps([{"title": "Autumn Marathon"}]),
            constraints_snapshot=json.dumps([]),
        )

        # The athlete edits the goal while reading the proposal.
        test_db.update_objective(goal_id, title="Autumn Marathon (moved)")

        coach_service.plan_apply(
            goal_id, "Build then taper", [], fingerprints=generated
        )

        macro = test_db.get_macrocycle_for_objective(goal_id)
        self.assertEqual(macro["goals_hash"], "hash-at-generate-time")
        self.assertEqual(macro["constraints_hash"], "constraints-at-generate-time")
        self.assertEqual(macro["config_hash"], "config-at-generate-time")
        self.assertEqual(
            json.loads(macro["goals_snapshot"]), [{"title": "Autumn Marathon"}]
        )

    def test_recomputing_at_accept_time_is_what_loses_the_edit(self):
        """The mechanism of the old bug, kept as documentation.

        Applying without fingerprints re-reads the goals as they are *now*, so the
        stored hash describes the edited goal rather than the one the strategy was
        generated from — which is exactly the state the staleness detector treats as
        "plan is current"."""
        goal_id = test_db.add_objective(
            title="Spring Race", target_date="2027-04-01",
            sport_type="running",
        )
        at_generate_time = coach_service.engine._get_goals_hash(
            test_db.upcoming_objectives()
        )

        test_db.update_objective(goal_id, title="Spring Race (different course)")
        coach_service.plan_apply(goal_id, "Base then build", [])

        macro = test_db.get_macrocycle_for_objective(goal_id)
        self.assertNotEqual(
            macro["goals_hash"], at_generate_time,
            "recomputing at accept time silently absorbs the edit",
        )

        # Passing the generate-time fingerprints is what preserves it.
        second_goal = test_db.add_objective(
            title="Summer Race", target_date="2027-07-01",
            sport_type="running",
        )
        before_edit = coach_service.engine._get_goals_hash(test_db.upcoming_objectives())
        test_db.update_objective(second_goal, title="Summer Race (moved)")
        coach_service.plan_apply(
            second_goal, "Build", [],
            fingerprints=PlanFingerprints(
                goals_hash=before_edit, constraints_hash="c",
            ),
        )
        self.assertEqual(
            test_db.get_macrocycle_for_objective(second_goal)["goals_hash"], before_edit
        )


if __name__ == "__main__":
    unittest.main()
