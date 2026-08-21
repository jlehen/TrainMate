"""The shared selector grammar and the reserved-letter vocabulary
(DESIGN_cli_selectors.md)."""
import argparse
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from trainmate.cli import selectors
from trainmate.cli.selectors import (
    CURRENT, DateRange, IdRange, SelectorError, add_selector_args, parse_date_range,
    parse_id_range, parse_single_date, parse_target, resolve_window,
)

TODAY = date(2026, 6, 15)


def _iso(offset_days: int) -> str:
    return (TODAY + timedelta(days=offset_days)).strftime("%Y-%m-%d")


class TestDateGrammar(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(selectors, "_today_date", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher_str = patch.object(
            selectors, "_today_str", return_value=TODAY.strftime("%Y-%m-%d")
        )
        patcher_str.start()
        self.addCleanup(patcher_str.stop)

    def test_single_date_is_one_day(self):
        self.assertEqual(
            parse_date_range("2026-06-01"), DateRange(start="2026-06-01", end="2026-06-01")
        )

    def test_open_ended_ranges(self):
        self.assertEqual(parse_date_range("2026-06-01.."), DateRange(start="2026-06-01"))
        self.assertEqual(parse_date_range("..2026-06-01"), DateRange(end="2026-06-01"))
        self.assertEqual(
            parse_date_range("2026-06-01..2026-06-30"),
            DateRange(start="2026-06-01", end="2026-06-30"),
        )

    def test_today_and_signed_offsets_resolve_against_today(self):
        self.assertEqual(parse_date_range("today"), DateRange(start=_iso(0), end=_iso(0)))
        self.assertEqual(
            parse_date_range("-7d..+2w"), DateRange(start=_iso(-7), end=_iso(14))
        )

    def test_bare_span_carries_no_direction(self):
        self.assertEqual(parse_date_range("7d"), DateRange(span="7d"))
        self.assertEqual(parse_date_range("2w"), DateRange(span="2w"))

    def test_unsigned_offset_as_an_endpoint_is_refused(self):
        # The whole reason a span must carry its unit: '7d..' cannot say which way it runs.
        with self.assertRaises(SelectorError) as caught:
            parse_date_range("7d..")
        self.assertIn("needs a sign", str(caught.exception))

    def test_bare_integer_is_not_a_date(self):
        with self.assertRaises(SelectorError):
            parse_date_range("7")

    def test_malformed_selectors(self):
        for raw in ("", "..", "a..b", "2026-13-01", "1..2..3", "2026-06-30..2026-06-01"):
            with self.assertRaises(SelectorError, msg=raw):
                parse_date_range(raw)

    def test_single_date_arg_refuses_a_range(self):
        self.assertEqual(parse_single_date("2026-06-01"), "2026-06-01")
        self.assertEqual(parse_single_date("-1d"), _iso(-1))
        with self.assertRaises(SelectorError):
            parse_single_date("2026-06-01..2026-06-02")


class TestIdGrammar(unittest.TestCase):
    def test_ranges_over_ids(self):
        self.assertEqual(parse_id_range("3"), IdRange(start=3, end=3))
        self.assertEqual(parse_id_range("3.."), IdRange(start=3))
        self.assertEqual(parse_id_range("..5"), IdRange(end=5))
        self.assertEqual(parse_id_range("3..5"), IdRange(start=3, end=5))

    def test_empty_means_the_current_one(self):
        self.assertEqual(parse_id_range(""), CURRENT)

    def test_non_numeric_is_refused_by_name(self):
        with self.assertRaises(SelectorError) as caught:
            parse_id_range("base", "macrocycle")
        self.assertIn("macrocycle ID", str(caught.exception))


class TestTargets(unittest.TestCase):
    def test_integers_are_ids_and_everything_else_is_a_date(self):
        self.assertEqual(parse_target("12"), ("id", 12))
        kind, value = parse_target("2026-06-01..")
        self.assertEqual(kind, "date")
        self.assertEqual(value, DateRange(start="2026-06-01"))


class TestResolveWindow(unittest.TestCase):
    """The command's own policy fills whichever side no selector bounded."""

    def setUp(self):
        patcher = patch.object(selectors, "_today_date", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher_str = patch.object(
            selectors, "_today_str", return_value=TODAY.strftime("%Y-%m-%d")
        )
        patcher_str.start()
        self.addCleanup(patcher_str.stop)

    def _parse(self, argv, **policy):
        parser = argparse.ArgumentParser()
        add_selector_args(parser, **policy)
        return resolve_window(parser.parse_args(argv))

    def test_no_selector_uses_the_declared_default(self):
        self.assertEqual(
            self._parse([], direction="forward", default="7d"), (_iso(0), _iso(6))
        )
        self.assertEqual(
            self._parse([], direction="backward", default="14d"), (_iso(-13), _iso(0))
        )
        self.assertEqual(self._parse([], direction="none"), (None, None))

    def test_a_bare_span_runs_the_command_direction(self):
        self.assertEqual(
            self._parse(["-d", "3d"], direction="forward"), (_iso(0), _iso(2))
        )
        self.assertEqual(
            self._parse(["-d", "3d"], direction="backward"), (_iso(-2), _iso(0))
        )

    def test_forward_leaves_an_open_end_open_and_anchors_the_start_at_today(self):
        self.assertEqual(
            self._parse(["-d", "2026-06-01.."], direction="forward"), ("2026-06-01", None)
        )
        self.assertEqual(
            self._parse(["-d", "..2026-06-30"], direction="forward"),
            (_iso(0), "2026-06-30"),
        )

    def test_backward_closes_both_sides(self):
        self.assertEqual(
            self._parse(["-d", "..2026-06-30"], direction="backward", span_days=7),
            ("2026-06-24", "2026-06-30"),
        )
        self.assertEqual(
            self._parse(["-d", "2026-06-01.."], direction="backward"),
            ("2026-06-01", _iso(0)),
        )

    def test_cleanup_direction_leaves_both_sides_unbounded(self):
        self.assertEqual(
            self._parse(["-d", "2026-06-01.."], direction="none"), ("2026-06-01", None)
        )

    def test_dimensions_intersect(self):
        with patch.object(selectors, "_meso_bounds", return_value=("2026-06-01", "2026-06-28")):
            self.assertEqual(
                self._parse(["-m", "3", "-d", "2026-06-10.."], meso=True, direction="none"),
                ("2026-06-10", "2026-06-28"),
            )

    def test_an_empty_intersection_is_reported_not_silently_returned(self):
        with patch.object(selectors, "_meso_bounds", return_value=("2026-06-01", "2026-06-28")):
            with self.assertRaises(SystemExit):
                self._parse(["-m", "3", "-d", "2026-07-01.."], meso=True, direction="none")


class TestSelectorVocabularyInvariants(unittest.TestCase):
    """`-d`, `-m`, `-M`, `-g` and `-t` mean one thing across the whole tree.

    Every violation fails silently — the wrong flag simply resolves — so the real parser
    tree is walked here the same way TestCommandTreeInvariants walks it for commands."""

    # The two commands that act on a single day rather than a range, so a block or a plan
    # is not a thing they could mean (the athlete's call, DESIGN_cli_selectors.md §5).
    SINGLE_DAY = {"workout adapt", "benchmark record"}
    # `-m` there is --message / --metric, kept because neither command takes a block.
    LETTER_EXEMPT = {("workout adapt", "-m")}

    EXPECTED = {
        "-d": {"--date"},
        "-m": {"--mesocycle"},
        "-M": {"--macrocycle"},
        "-g": {"--goal"},
        "-t": {"--type"},
        "-c": {"--constraint"},
    }

    def _walk(self, parser, path=""):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                seen = set()
                for name, sub in action.choices.items():
                    if id(sub) in seen:
                        continue
                    seen.add(id(sub))
                    yield from self._walk(sub, f"{path} {name}".strip())
                continue
            yield path, action

    def test_short_letters_keep_their_meaning(self):
        import trainmate_cli
        parser, _ = trainmate_cli.build_parser()
        for path, action in self._walk(parser):
            shorts = [o for o in action.option_strings if len(o) == 2]
            longs = set(o for o in action.option_strings if len(o) > 2)
            for short in shorts:
                if short not in self.EXPECTED or (path, short) in self.LETTER_EXEMPT:
                    continue
                self.assertTrue(
                    longs & self.EXPECTED[short],
                    f"'{path} {short}' means {sorted(longs)}, not "
                    f"{sorted(self.EXPECTED[short])}",
                )

    def test_the_retired_range_flags_are_gone(self):
        import trainmate_cli
        parser, _ = trainmate_cli.build_parser()
        # `--until-goal` retired with the rest: `workout generate -g` now IS the horizon,
        # so a goal's target date reaches generation through the shared grammar rather
        # than through a flag of its own (DESIGN_cli_selectors.md §8).
        retired = {"--from", "--until", "--from-date", "--until-date", "--from-mesocycle",
                   "--until-mesocycle", "--days", "--until-goal"}
        for path, action in self._walk(parser):
            clash = retired & set(action.option_strings)
            self.assertFalse(clash, f"'{path}' still registers {sorted(clash)}")

    def test_accommodate_takes_the_reserved_vocabulary(self):
        """`workout accommodate` is a plain selector command (§4): -d/-m/-M/-g mean here
        exactly what they mean everywhere, and the tree walk above covers it for free —
        this pins that they are actually registered."""
        import trainmate_cli
        parser, _ = trainmate_cli.build_parser()
        registered = {
            o
            for path, action in self._walk(parser) if path == "workout accommodate"
            for o in action.option_strings
        }
        for flag in ("-d", "-m", "-M", "-g"):
            self.assertIn(flag, registered)

    def test_every_range_command_declares_a_policy(self):
        import trainmate_cli
        parser, _ = trainmate_cli.build_parser()
        for path, action in self._walk(parser):
            if "--date" not in action.option_strings or path in self.SINGLE_DAY:
                continue
            sub = parser
            for token in path.split():
                sub = next(
                    a.choices[token] for a in sub._actions
                    if isinstance(a, argparse._SubParsersAction) and token in a.choices
                )
            self.assertIn(
                "_selector_policy", sub._defaults,
                f"'{path}' takes -d but declares no direction/default",
            )


class TestOneDoorOntoTheAccommodationFlow(unittest.TestCase):
    """The window-scoped reshuffle has exactly one command
    (DESIGN_constraint_reschedule.md §4). A second door onto the same LLM call is what
    let `--show-llm-prompt-only` land on one and not the other."""

    def test_constraint_has_no_honor_subcommand(self):
        import trainmate_cli
        parser, _ = trainmate_cli.build_parser()
        constraint = next(
            a.choices["constraint"] for a in parser._actions
            if isinstance(a, argparse._SubParsersAction) and "constraint" in a.choices
        )
        verbs = {
            name for action in constraint._actions
            if isinstance(action, argparse._SubParsersAction) for name in action.choices
        }
        self.assertNotIn("honor", verbs)


if __name__ == "__main__":
    unittest.main()
