"""Argparse extensions for the CLI: wrap-aware help/parser, the $EDITOR helper,
and the network-appliance-style dashless-argv translator + command-tree printer.

Extracted from trainmate_cli.py; those names are re-imported there so existing
``trainmate_cli.<name>`` patch seams keep working."""
import argparse
import os
import subprocess
import sys
import tempfile
from typing import Optional

from trainmate.util import bold, red, yellow, default_wrap_width, format_labeled_block


class WrapAwareHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """argparse help formatter that respects the prose wrap width.

    argparse keys its layout off an 80-col terminal: option help is indented to a
    fixed deep column (``max_help_position`` 24), which on a phone-width client
    wastes most of every line on the gap between an option and its help. When the
    Telegram bot drives the CLI it sets TRAINMATE_WRAP_WIDTH (~48); we pin the
    total width to that and, once narrow, collapse the help column so each option's
    help sits on the next line at a shallow indent instead of far to the right.
    On a real terminal (default width) we defer entirely to argparse's familiar
    two-column layout."""

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

class WrapAwareArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that defaults to :class:`WrapAwareHelpFormatter`.

    Used for the root parser so every sub-parser created via ``add_subparsers`` /
    ``add_parser`` inherits the same formatter (argparse propagates the parser
    class but not ``formatter_class``), making all help — top-level and nested —
    wrap to the active client width."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", WrapAwareHelpFormatter)
        super().__init__(*args, **kwargs)

def _edit_text_in_editor(initial: str) -> Optional[str]:
    """Opens $EDITOR (falling back to vi) seeded with `initial`, returns the saved text.

    Returns None if the editor exits non-zero (treated as an abort). Trailing newlines are
    stripped. Used by `plan feedback --edit`.
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="trainmate-feedback-", delete=False
    ) as tf:
        tf.write(initial or "")
        path = tf.name
    try:
        result = subprocess.run([editor, path])
        if result.returncode != 0:
            print(red(f"Editor exited with status {result.returncode}; feedback unchanged."))
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read().rstrip("\n")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

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

def _subparser_choices(parser: argparse.ArgumentParser) -> dict:
    """The sub-command/alias -> sub-parser map for this level (empty for leaf commands)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}

def _print_command_tree(parser: argparse.ArgumentParser, indent: int = 0) -> None:
    """Recursively prints every command/sub-command with its one-line help.

    argparse's own --help only renders one level (a command's immediate
    sub-commands); the 'help' command walks the whole sub-parser tree so the
    entire surface area is visible without drilling into each command.
    """
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for choice_action in action._choices_actions:
            label = "  " * indent + bold(choice_action.metavar)
            print(format_labeled_block(label, choice_action.help or ""))
            _print_command_tree(action.choices[choice_action.dest], indent + 1)
            if indent == 0:
                print()

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
      * a sub-command/alias → emitted, then the remainder is translated in that
        sub-parser's context (recursive descent mirroring the parser tree). This
        also covers the top-level ``help`` command (a real sub-command), so it
        takes priority over the next rule.
      * the bare word ``help`` (not a sub-command at this level) → ``--help``,
        argparse's own one-level help for the current command.
      * anything else → left as-is for argparse to bind positionally.
    """
    spec = _build_keyword_spec(parser)
    sub_choices = _subparser_choices(parser)
    out: list = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        tok_lower = tok.lower()
        if tok.startswith("-"):
            out.append(tok)
            i += 1
            continue
        if tok_lower in sub_choices:
            out.append(tok_lower)
            out.extend(translate_dashless_argv(sub_choices[tok_lower], tokens[i + 1:]))
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
                if nxt is not None:
                    out.append(nxt)
                    i += 2
                else:
                    i += 1
            continue
        out.append(tok)
        i += 1
    return out
