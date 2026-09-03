"""Every command reachable from the parser tree must resolve to a handler.

Dispatch used to be a 200-line elif ladder kept in step with the parser definitions by
hand: registering a sub-command touched four places, and a branch that fell through
did nothing at all — silently, with exit code 0. Binding the handler with
set_defaults() next to the flags it reads makes that structural, and this test is what
keeps it honest.
"""
import unittest

import trainmate_cli


def leaf_parsers(parser, path=()):
    """Yields (path, parser) for every sub-parser with no sub-parsers of its own."""
    import argparse

    subactions = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    if not subactions:
        yield path, parser
        return
    for action in subactions:
        seen = set()
        for name, sub in action.choices.items():
            if id(sub) in seen:      # aliases point at the same parser
                continue
            seen.add(id(sub))
            yield from leaf_parsers(sub, path + (name,))


def command_groups(parser):
    """Top-level commands that own sub-commands and bind no handler of their own.

    A group with its own `func` is a read-only family answering bare by aliasing one of
    its children (`settings` = `settings list`, DESIGN_cli_noargs.md §a3); those print a
    listing rather than help, so they are not groups for this purpose.
    """
    import argparse

    names, seen = [], set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            if id(sub) in seen:      # aliases point at the same parser
                continue
            seen.add(id(sub))
            has_children = any(
                isinstance(a, argparse._SubParsersAction) for a in sub._actions
            )
            if has_children and sub.get_default("func") is None:
                names.append(name)
    return names


class TestEveryCommandHasAHandler(unittest.TestCase):
    # `help` and `shell` need the parser tree rather than the database, so the
    # dispatcher answers them directly.
    ANSWERED_BY_THE_DISPATCHER = {("help",), ("shell",)}

    def setUp(self):
        self.parser, self.named = trainmate_cli.build_parser()

    def test_every_leaf_command_binds_a_callable(self):
        missing = []
        for path, sub in leaf_parsers(self.parser):
            if path in self.ANSWERED_BY_THE_DISPATCHER:
                continue
            func = sub.get_default("func")
            if not callable(func):
                missing.append(" ".join(path))
        self.assertEqual(missing, [], f"commands with no handler: {missing}")

    def test_a_bare_command_group_prints_its_help_and_fails(self):
        """`tm goal` with no sub-command should say what it offers, not exit silently.

        The groups are read off the parser, not listed here: the list used to omit
        `bot`, which was hiding the fact that bare `tm bot` printed the *top-level*
        help. A group added tomorrow is covered tomorrow.
        """
        from tests.helpers import run_cli

        groups = command_groups(self.parser)
        self.assertGreater(len(groups), 5, "the parser walk found almost no groups")
        for name in groups:
            with self.subTest(group=name):
                exit_code, stdout, _ = run_cli([name])
                self.assertEqual(exit_code, 1, f"bare `{name}` should not report success")
                self.assertIn(f"tm {name}", stdout)

    def test_handlers_are_distinct_per_command(self):
        """A copy-paste that binds two sub-commands to one handler is a real bug and
        an easy one to make with this pattern."""
        bindings = {}
        for path, sub in leaf_parsers(self.parser):
            if path in self.ANSWERED_BY_THE_DISPATCHER:
                continue
            func = sub.get_default("func")
            bindings.setdefault(func, []).append(" ".join(path))

        # A read-only family answers bare by binding its own top-level parser to one of
        # its sub-commands (`settings` = `settings list`; DESIGN_cli_noargs.md §a3,
        # applied by DESIGN_settings.md §4). Keyed on that shape — a parent and one child —
        # so a family added later is covered without editing this test.
        def is_bare_alias(cmds):
            if len(cmds) != 2:
                return False
            parent, child = sorted(cmds, key=len)
            return child.startswith(parent + " ")

        duplicates = {
            f.__name__: cmds for f, cmds in bindings.items()
            if len(cmds) > 1 and not is_bare_alias(cmds)
        }
        self.assertEqual(duplicates, {}, f"one handler bound to several commands: {duplicates}")


if __name__ == "__main__":
    unittest.main()
