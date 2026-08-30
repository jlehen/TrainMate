"""`PmcContextMixin._week_sport_gap_note`: the per-sport annotation on a block's
"Weeks already trained" prompt lines (coach/service/context.py). Pure function —
reads only its arguments, no `self._db` — so it is exercised directly here rather
than through a full CoachService/DB fixture."""
import unittest

from trainmate.coach.service.context import PmcContextMixin


class _Ctx(PmcContextMixin):
    pass


class TestWeekSportGapNote(unittest.TestCase):
    def setUp(self):
        self.ctx = _Ctx()

    def test_missed_strength_flags_the_split_even_though_blended_pct_looks_mild(self):
        # Cycling fully on plan (103%), strength entirely skipped (0%) — the
        # blended total (212/237, 89%) reads as an unremarkable soft week on its
        # own, but the per-sport split shows the shortfall is ALL strength.
        week = {
            "planned_load": 237.0,
            "planned_load_by_sport": {"cycling": 205.0, "strength_training": 32.0},
            "in_progress": False,
            "actual_load_by_sport": {"cycling": 212.0},
        }
        note = self.ctx._week_sport_gap_note(week, denom=237.0, actual=212.0)
        self.assertEqual(
            note, "of which cycling: 212/205 (103%), strength_training: 0/32 (0%)"
        )

    def test_stays_silent_when_sports_track_the_blended_rate_together(self):
        week = {
            "planned_load": 237.0,
            "planned_load_by_sport": {"cycling": 205.0, "strength_training": 32.0},
            "in_progress": False,
            "actual_load_by_sport": {"cycling": 205.0, "strength_training": 25.0},
        }
        self.assertIsNone(self.ctx._week_sport_gap_note(week, denom=237.0, actual=230.0))

    def test_stays_silent_for_a_single_sport_week(self):
        week = {
            "planned_load": 200.0,
            "planned_load_by_sport": {"cycling": 200.0},
            "in_progress": False,
            "actual_load_by_sport": {"cycling": 100.0},
        }
        self.assertIsNone(self.ctx._week_sport_gap_note(week, denom=200.0, actual=100.0))

    def test_stays_silent_when_the_only_planned_sport_is_on_target(self):
        # An unplanned bonus sport (yoga) pushes the blended total over 100%, but
        # the one sport that WAS planned hit its number exactly — nothing here
        # contradicts what the blended total already says.
        week = {
            "planned_load": 205.0,
            "planned_load_by_sport": {"cycling": 205.0},
            "in_progress": False,
            "actual_load_by_sport": {"cycling": 205.0, "yoga": 40.0},
        }
        self.assertIsNone(self.ctx._week_sport_gap_note(week, denom=205.0, actual=245.0))

    def test_in_progress_week_uses_the_elapsed_slice(self):
        week = {
            "planned_load": 300.0,
            "planned_load_by_sport": {"cycling": 260.0, "strength_training": 40.0},
            "in_progress": True,
            "planned_load_elapsed": 237.0,
            "planned_load_elapsed_by_sport": {"cycling": 205.0, "strength_training": 32.0},
            "actual_load_by_sport": {"cycling": 212.0},
        }
        note = self.ctx._week_sport_gap_note(week, denom=237.0, actual=212.0)
        self.assertEqual(
            note, "of which cycling: 212/205 (103%), strength_training: 0/32 (0%)"
        )

    def test_no_denom_or_no_actual_short_circuits(self):
        week = {"planned_load": None}
        self.assertIsNone(self.ctx._week_sport_gap_note(week, denom=None, actual=0.0))
        self.assertIsNone(self.ctx._week_sport_gap_note(week, denom=100.0, actual=0.0))


if __name__ == "__main__":
    unittest.main()
