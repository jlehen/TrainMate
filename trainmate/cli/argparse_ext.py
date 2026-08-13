"""Argparse extensions for the CLI: the wrap-aware help/parser and the
network-appliance-style dashless-argv translator + command-tree printer.

Extracted from trainmate_cli.py; those names are re-imported there so existing
``trainmate_cli.<name>`` patch seams keep working."""
import argparse
import re
import sys
import textwrap
from typing import Optional

from trainmate.util import (
    bold, dim, red, yellow, cmd, default_wrap_width, format_labeled_block, wrap_text
)


def _fill(text: str, width: int, initial: str = "", subsequent: str = "") -> str:
    """Wrap ``text`` to ``width``, never splitting an option name at a hyphen
    (DESIGN_cli_noargs.md §e)."""
    return textwrap.fill(
        " ".join(text.split()), max(width, 20), initial_indent=initial,
        subsequent_indent=subsequent, break_on_hyphens=False, break_long_words=False,
    )


class WrapAwareHelpFormatter(argparse.HelpFormatter):
    """argparse help formatter that respects the prose wrap width.

    argparse keys its layout off an 80-col terminal: option help is indented to a
    fixed deep column (``max_help_position`` 24), which on a phone-width client
    wastes most of every line on the gap between an option and its help. When the
    Telegram bot drives the CLI it sets TRAINMATE_WRAP_WIDTH (~48); we pin the
    total width to that and, once narrow, collapse the help column so each option's
    help sits on the next line at a shallow indent instead of far to the right.
    On a real terminal (default width) we defer entirely to argparse's familiar
    two-column layout. The usage line, descriptions and flag names get wrapping
    argparse doesn't do for them either — DESIGN_cli_noargs.md §e."""

    def __init__(self, prog):
        width = default_wrap_width()
        if width >= 70:
            super().__init__(prog)
            self._narrow = False
        else:
            super().__init__(prog, max_help_position=4, width=width)
            self._narrow = True

    def _format_action(self, action):
        # In narrow mode argparse collapses help onto the next line at a single
        # global column (``max_help_position``). That column matches the *option*
        # indent, so options read nicely (name two columns above its help), but
        # sub-command entries are indented one level deeper — their name lands on
        # the very column the help text uses, so name and help collide. Pin the
        # help column to two past the current indent instead, so every entry's
        # help sits one level under its own name regardless of nesting depth.
        if self._narrow:
            saved = self._max_help_position
            self._max_help_position = self._current_indent + self._indent_increment
            try:
                return super()._format_action(action)
            finally:
                self._max_help_position = saved
        return super()._format_action(action)

    def _format_usage(self, usage, actions, groups, prefix):
        text = super()._format_usage(usage, actions, groups, prefix)
        if not self._narrow:
            return text
        # Aligning continuations under the program name leaves too few columns at chat
        # width, so re-flow the line at a flat indent (DESIGN_cli_noargs.md §e).
        head = prefix if prefix is not None else "usage: "
        body = " ".join(text.split()).removeprefix(head.strip()).strip()
        return _fill(body, self._width, head, "  ") + "\n\n"

    def _split_lines(self, text, width):
        # break_on_hyphens would split an option out of its own help ("--out-\nof-date").
        return _fill(text, width).splitlines()

    def _fill_text(self, text, width, indent):
        # Wrap a description/epilog one paragraph at a time, so prose re-flows but a
        # deliberate blank line survives (DESIGN_cli_noargs.md §e). Indented and bulleted
        # paragraphs are left exactly as authored.
        blocks = []
        for para in re.split(r"\n[ \t]*\n", text.strip("\n")):
            if any(re.match(r"[ \t]|[-*•]\s", line) for line in para.splitlines()):
                blocks.append(textwrap.indent(para, indent))
                continue
            blocks.append(_fill(para, width, indent, indent))
        return "\n\n".join(blocks)

class _DescFromHelpSubParsersAction(argparse._SubParsersAction):
    """Mirror help into description, and hide ``advanced=True`` sub-commands.

    Two behaviours:

    * ``add_parser(help=...)`` only feeds the *parent* listing, so a leaf sub-parser
      has no ``description`` and ``<cmd> -h`` prints usage+options but never says what
      the command does. Mirror help into description so both read the same summary.
    * ``add_parser(..., advanced=True)`` registers a maintenance/bootstrap command
      that still parses, dispatches, and answers ``<cmd> -h`` — but is kept out of
      the default ``-h`` listing and the ``help`` tree so the everyday surface stays
      uncluttered. It surfaces only under ``help --all`` (via ``advanced_choices``).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Not argparse's ``{a,b,c,…}``: one unbreakable token, past the edge, re-exposing
        # the hidden commands, saying nothing the listing below doesn't (§c).
        if self.metavar is None:
            self.metavar = "<command>"
        self.advanced_choices: list = []  # (name, help) for hidden sub-commands
        # Prefix resolution (DESIGN_cli_noargs.md §d) matches canonical names only;
        # aliases stay exact-match, which is why they survive at all.
        self.canonical_names: list = []
        self.alias_of: dict = {}

    def add_parser(self, name, advanced=False, aliases=(), **kwargs):
        if "help" in kwargs:
            kwargs.setdefault("description", kwargs["help"])
        self.canonical_names.append(name)
        for alias in aliases:
            self.alias_of[alias] = name
        if advanced:
            # Omit ``help`` so argparse builds no listing entry for this command
            # (leaving it out of both ``-h`` and the ``help`` tree), but keep the
            # description so ``<cmd> <name> -h`` still explains itself.
            self.advanced_choices.append((name, kwargs.get("help", "")))
            kwargs.pop("help", None)
        return self._with_helpall(super().add_parser(name, aliases=aliases, **kwargs))

    @staticmethod
    def _with_helpall(parser):
        """Give every sub-command its own ``--helpall`` (see :class:`_HelpAllAction`),
        so the full-including-hidden listing is reachable at any level, not just root."""
        parser.add_argument(
            "--helpall", action=_HelpAllAction,
            help="Show this command's full sub-tree, hidden maintenance commands included"
        )
        return parser


class WrapAwareArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that defaults to :class:`WrapAwareHelpFormatter`.

    Used for the root parser so every sub-parser created via ``add_subparsers`` /
    ``add_parser`` inherits the same formatter (argparse propagates the parser
    class but not ``formatter_class``), making all help — top-level and nested —
    wrap to the active client width. Also registers a sub-parsers action that
    defaults each sub-command's description from its help, so every ``<cmd> -h``
    states what the command does; the registration propagates to nested levels."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", WrapAwareHelpFormatter)
        super().__init__(*args, **kwargs)
        self.register("action", "parsers", _DescFromHelpSubParsersAction)

    def error(self, message):
        # A missing-required-argument error answers itself with the command's own help,
        # then names what is missing last, where the eye lands — except in chat, where
        # a help dump costs a screenful (DESIGN_cli_noargs.md §a).
        if message.startswith("the following arguments are required"):
            from trainmate.prompt import is_json_frontend
            if is_json_frontend():
                self.exit(2, red(f"{self.prog}: error: {message}\n")
                          + dim("Run " + cmd(f"{self.prog} -h") + " for usage.\n"))
            self.print_help(sys.stderr)
            self.exit(2, red(f"\n{self.prog}: error: {message}\n"))
        super().error(message)

def _weeks_arg(raw: str):
    """`--weeks N` must be a whole number >= 1, or the literal `all`
    (DESIGN_progress_timeline.md §7.1) — rejected at argparse, so a `0` can't silently
    fall through to the default. `all` matches the web endpoint's `?weeks=all`."""
    if raw == "all":
        return "all"
    try:
        n = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: '{raw}'")
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n

# A selector endpoint reaching back from today (`-7d`, `-2w..+1w`) starts with a dash, so
# argparse reads it as an option and reports a missing value. It is glued to its flag as
# `--date=-7d..` instead — the one form argparse takes verbatim (DESIGN_cli_selectors.md §6).
_NEGATIVE_SELECTOR_RE = re.compile(r"-\d+(\.\d+)?[dw](\.\.|$)")


def _needs_equals(value: Optional[str]) -> bool:
    return bool(value) and value.startswith("-") and bool(_NEGATIVE_SELECTOR_RE.match(value))


def _canonical_option(action: argparse.Action) -> str:
    """The most explicit spelling of an option (argparse accepts any registered one)."""
    return max(action.option_strings, key=len)

def _build_keyword_spec(parser: argparse.ArgumentParser) -> dict:
    """Map every dashless option spelling -> its action for one parser level.

    ``--from``/``--from-date`` both register (``from``, ``from-date``); ``-y`` registers
    ``y``. Positionals, the sub-parsers action, and ``-h/--help`` are excluded. Two
    distinct actions claiming one keyword in the same command is an authoring bug, so we
    warn rather than silently shadow.
    """
    spec: dict = {}
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if not action.option_strings:
            continue  # positional — bound by position, not by keyword
        for opt in action.option_strings:
            kw = opt.lstrip("-")
            if kw in spec and spec[kw] is not action:
                print(yellow(f"Warning: ambiguous dashless keyword '{kw}'"), file=sys.stderr)
            spec[kw] = action
    return spec

def _subparsers_action(parser: argparse.ArgumentParser):
    """This level's sub-parsers action, or None for a leaf command."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _subparser_choices(parser: argparse.ArgumentParser) -> dict:
    """The sub-command/alias -> sub-parser map for this level (empty for leaf commands)."""
    action = _subparsers_action(parser)
    return action.choices if action is not None else {}


def _resolve_subcommand(action, token: str, exact_only: bool = False) -> Optional[str]:
    """Map a typed sub-command token to its canonical name, or None if it matches nothing.

    Exact canonical name or registered alias first; failing that, any prefix that
    matches exactly one canonical name (DESIGN_cli_noargs.md §d). Hidden
    ``advanced=True`` commands take part in matching — they dispatch like any other.
    An ambiguous prefix is a user error, reported like argparse's own (exit 2).
    """
    names = getattr(action, "canonical_names", None) or list(action.choices)
    if token in names:
        return token
    canonical = getattr(action, "alias_of", {}).get(token)
    if canonical is not None:
        return canonical
    if exact_only:
        return None
    matches = [name for name in names if name.startswith(token)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        print(red(f"Ambiguous command '{token}' — matches: " + ", ".join(sorted(matches))),
              file=sys.stderr)
        sys.exit(2)
    return None

def _print_command_tree(
    parser: argparse.ArgumentParser, indent: int = 0, include_advanced: bool = False
) -> None:
    """Recursively prints every command/sub-command with its one-line help.

    argparse's own --help only renders one level (a command's immediate
    sub-commands); the 'help' command walks the whole sub-parser tree so the
    entire surface area is visible without drilling into each command.

    ``include_advanced`` (``help --all``) also lists the hidden maintenance
    commands, dimmed and tagged, right under their visible siblings.
    """
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for choice_action in action._choices_actions:
            label = "  " * indent + bold(choice_action.metavar)
            print(format_labeled_block(label, choice_action.help or ""))
            _print_command_tree(
                action.choices[choice_action.dest], indent + 1, include_advanced
            )
            if indent == 0:
                print()
        if include_advanced:
            for name, help_text in getattr(action, "advanced_choices", []):
                label = "  " * indent + yellow(name)
                print(format_labeled_block(label, yellow((help_text or "") + "  [maintenance]")))


def _reorder_subparsers_action(action: argparse._SubParsersAction, order: list) -> None:
    """Reorder one sub-parsers action's display to match ``order`` (canonical names).

    Sorts the help listing (``_choices_actions``, shared by ``-h`` and the ``help``
    tree). Names not in ``order`` sort stably to the end.
    """
    rank = {name: i for i, name in enumerate(order)}
    end = len(order)
    action._choices_actions.sort(key=lambda ca: rank.get(ca.dest, end))


def sort_command_tree(parser: argparse.ArgumentParser, order_map: dict, path: str = "") -> None:
    """Reorder the whole parser tree's help by usefulness (DESIGN_cli_noargs.md §c).

    ``order_map`` maps a level's key — ``""`` for the top level, else the parent
    command's canonical name — to that level's sub-commands most-useful first.
    """
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        order = order_map.get(path)
        if order:
            _reorder_subparsers_action(action, order)
        seen: set = set()
        for ca in action._choices_actions:
            child = action.choices[ca.dest]
            if id(child) in seen:
                continue
            seen.add(id(child))
            sort_command_tree(child, order_map, ca.dest)


class _HelpAllAction(argparse.Action):
    """A ``--helpall`` flag, on every command level, mirroring ``help --all``.

    ``help --all`` is the only way to see the hidden maintenance commands, but
    it's easy to miss; a real flag lands in each command's ``-h`` so it's
    discoverable everywhere. Prints the current command's full sub-tree (hidden
    included) and exits like ``-h``; on a leaf with no sub-commands it just falls
    back to that command's own ``-h``.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest,
                         default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        has_subcommands = any(
            isinstance(a, argparse._SubParsersAction) for a in parser._actions
        )
        if not has_subcommands:
            parser.print_help()
            parser.exit()
        print(bold(wrap_text(parser.description or "")))
        print()
        _print_command_tree(parser, include_advanced=True)
        parser.exit()


def translate_dashless_argv(parser: argparse.ArgumentParser, tokens: list) -> list:
    """Rewrite network-appliance-style dashless options back into ``--flag`` form.

    The dashless syntax (``workout adapt message "..." no-pull``) and the classic
    ``--flag`` syntax funnel through the *same* argparse tree: this preprocessor consults
    the tree itself (per-command option specs) to expand bare keywords, then hands the
    result to ``parse_args`` which still does all validation/help/choices. Both syntaxes —
    even mixed — therefore keep working, and the command handlers are untouched.

    Rules per token, at the current command level:
      * ``-…`` (already dashed) → passed through verbatim (classic syntax / its values).
      * a known boolean keyword (``nargs == 0``) → ``--flag``, consumes nothing.
      * a known multi-value keyword (``nargs`` in ``+``/``*``) → ``--flag`` then the next
        token split on commas (``sport running,hiking`` → ``--sport running hiking``).
      * a known optional-value keyword (``nargs == '?'``, e.g. ``mesocycle [ID]``) →
        ``--flag``, consuming the next token only if it isn't itself a keyword/option.
      * any other known keyword → ``--flag`` and binds the very next token as its value
        unconditionally (so a value colliding with a keyword name — a goal literally
        titled ``date`` — is still taken as the value).
      * a sub-command/alias → emitted *as its canonical name*, then the remainder is
        translated in that sub-parser's context (recursive descent mirroring the parser
        tree). This also covers the top-level ``help`` command (a real sub-command), so
        it takes priority over the next rule.
      * the bare word ``help`` (not a sub-command at this level) → ``--help``,
        argparse's own one-level help for the current command.
      * an unambiguous *prefix* of a sub-command → the same descent, tried only after
        the exact-keyword rules above so no existing spelling changes meaning
        (DESIGN_cli_noargs.md §d).
      * anything else → left as-is for argparse to bind positionally.

    Because every sub-command reaches argparse under its canonical name, the dispatcher
    in ``trainmate_cli`` compares canonical names only.
    """
    spec = _build_keyword_spec(parser)
    sub_action = _subparsers_action(parser)
    sub_choices = sub_action.choices if sub_action is not None else {}

    def descend(canonical: str, rest: list) -> list:
        return [canonical] + translate_dashless_argv(sub_choices[canonical], rest)

    out: list = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        tok_lower = tok.lower()
        if tok.startswith("-"):
            nxt = tokens[i + 1] if i + 1 < n else None
            if tok in parser._option_string_actions and _needs_equals(nxt):
                out.append(f"{tok}={nxt}")
                i += 2
                continue
            out.append(tok)
            i += 1
            continue
        if sub_action is not None:
            canonical = _resolve_subcommand(sub_action, tok_lower, exact_only=True)
            if canonical is not None:
                out.extend(descend(canonical, tokens[i + 1:]))
                return out
        if tok_lower == "help":
            out.append("--help")
            return out
        action = spec.get(tok_lower)
        if action is not None:
            out.append(_canonical_option(action))
            nargs = action.nargs
            nxt = tokens[i + 1] if i + 1 < n else None
            if nargs == 0:
                i += 1
            elif nargs in ("+", "*"):
                if nxt is not None:
                    out.extend(nxt.split(","))
                    i += 2
                else:
                    i += 1
            elif nargs == "?":
                takes = (
                    nxt is not None
                    and not nxt.startswith("-")
                    and nxt.lower() not in spec
                    and nxt.lower() not in sub_choices
                )
                if takes:
                    out.append(nxt)
                    i += 2
                else:
                    i += 1
            else:
                if nxt is not None and _needs_equals(nxt):
                    out[-1] = f"{out[-1]}={nxt}"
                    i += 2
                elif nxt is not None:
                    out.append(nxt)
                    i += 2
                else:
                    i += 1
            continue
        if sub_action is not None:
            canonical = _resolve_subcommand(sub_action, tok_lower)
            if canonical is not None:
                out.extend(descend(canonical, tokens[i + 1:]))
                return out
        out.append(tok)
        i += 1
    return out
