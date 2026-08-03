"""Tests for the canonical sport vocabulary (`trainmate/sports.py`): alias resolution,
the invariants the reverse index relies on, and the single-source-of-truth wiring that
feeds the CLI's `--sport` choices and the coach prompts' `sport_type` enum.
"""
import unittest

from trainmate.sports import (
    CANONICAL_SPORTS, SPORT_MAPPING, canonical_sport, sport_aliases,
)


class TestSportMappingInvariants(unittest.TestCase):
    def test_canonical_name_is_its_own_first_alias(self):
        for canonical, aliases in SPORT_MAPPING.items():
            self.assertEqual(aliases[0], canonical)

    def test_no_alias_is_shared_between_sports(self):
        seen = {}
        for canonical, aliases in SPORT_MAPPING.items():
            for alias in aliases:
                self.assertNotIn(
                    alias, seen, f"'{alias}' maps to both {seen.get(alias)} and {canonical}"
                )
                seen[alias] = canonical

    def test_aliases_are_lowercase(self):
        for aliases in SPORT_MAPPING.values():
            for alias in aliases:
                self.assertEqual(alias, alias.lower())

    def test_canonical_sports_matches_mapping_keys(self):
        self.assertEqual(CANONICAL_SPORTS, list(SPORT_MAPPING))


class TestRowing(unittest.TestCase):
    def test_garmin_types_resolve_to_rowing(self):
        for activity_type in ("rowing", "indoor_rowing", "rowing_v2", "indoor_rowing_v2"):
            self.assertEqual(canonical_sport(activity_type), "rowing")

    def test_common_aliases_resolve_to_rowing(self):
        for alias in ("erg", "ergometer", " Indoor_Rowing ", "ROWING"):
            self.assertEqual(canonical_sport(alias), "rowing")

    def test_aliases_round_trip(self):
        self.assertIn("indoor_rowing", sport_aliases("rowing"))
        self.assertIn("rowing", sport_aliases("indoor_rowing"))


class TestDownhillSkiing(unittest.TestCase):
    def test_garmin_types_resolve_to_downhill_skiing(self):
        for activity_type in ("resort_skiing", "resort_snowboarding",
                              "resort_skiing_snowboarding", "resort_skiing_snowboarding_ws"):
            self.assertEqual(canonical_sport(activity_type), "downhill_skiing")

    def test_common_aliases_resolve_to_downhill_skiing(self):
        for alias in ("alpine_skiing", "snowboarding", "Resort_Skiing"):
            self.assertEqual(canonical_sport(alias), "downhill_skiing")

    def test_lift_served_is_not_ski_touring(self):
        """The two share lifts with nothing else: conflating them would let a resort day
        satisfy a planned touring session (and vice versa)."""
        self.assertEqual(canonical_sport("backcountry_skiing"), "ski_touring")
        self.assertEqual(canonical_sport("nordic_skiing"), "ski_touring")
        self.assertNotIn("resort_skiing", sport_aliases("ski_touring"))
        self.assertNotIn("backcountry_skiing", sport_aliases("downhill_skiing"))


class TestPromptAndCliWiring(unittest.TestCase):
    def test_coach_prompt_enum_lists_every_canonical_sport_plus_rest(self):
        from trainmate.coach.engine.workouts import _SPORT_TYPE_ENUM
        for sport in CANONICAL_SPORTS + ["rest"]:
            self.assertIn(f'"{sport}"', _SPORT_TYPE_ENUM)
        for line in _SPORT_TYPE_ENUM.splitlines():
            self.assertLessEqual(len(line), 90)
        self.assertTrue(_SPORT_TYPE_ENUM.startswith('      "sport_type": '))
        self.assertTrue(_SPORT_TYPE_ENUM.endswith('"rest",\n'))

    def test_goal_cli_accepts_every_canonical_sport(self):
        import trainmate_cli
        parser = trainmate_cli.build_parser()[0]
        for sport in CANONICAL_SPORTS:
            args = parser.parse_args(["goal", "add", "T", "2026-12-01", sport])
            self.assertEqual(args.sport, [sport])


if __name__ == "__main__":
    unittest.main()
