"""TrainMate CLI entry point.

The argparse dispatcher (``main``) and the patchable singletons/helpers live here;
the per-command handlers live in the ``trainmate.cli`` package and reference these
names via ``import trainmate_cli as cli`` so that test seams patching
``trainmate_cli.<name>`` continue to take effect.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from typing import Optional
from datetime import datetime, timedelta

# When launched as a script (``python trainmate_cli.py``) this module is named
# ``__main__``; the trainmate.cli.* handlers, however, ``import trainmate_cli`` to reach
# the singletons/helpers below. Alias the two names to one module object so that import
# resolves to *this* module instead of re-executing the file (which would deadlock on a
# circular import) and so both see the same — patchable — bindings.
sys.modules.setdefault("trainmate_cli", sys.modules[__name__])

from trainmate.db import db
from trainmate import garmin
from trainmate.google_calendar import calendar_syncer
from trainmate.coach import coach_service
from trainmate.config import config
from trainmate.prompt import make_prompt, Choice, PromptCancelled

# The active prompt transport (TtyPrompt on a terminal, JsonPrompt under the bot,
# selected via TRAINMATE_FRONTEND). A patchable singleton like db/coach_service:
# handlers reach it as ``cli.prompt`` to ask yes/no, one-of-N, or text questions.
prompt = make_prompt()
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    color_acwr, visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, default_wrap_width,
    today_str as _today_str, today_date as _today_date,
)


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


# Handlers live in the trainmate.cli package; imported here so main() can dispatch to
# them and so they remain attributes of this module (test compatibility).
from trainmate.cli.common import fmt_date, ensure_recent_data
from trainmate.cli.status import run_status
from trainmate.cli.progress import run_progress
from trainmate.cli.goals import (
    run_goal_add, run_goal_edit, run_goal_list, run_goal_rm, run_goal_wipe,
)
from trainmate.cli.constraints import (
    run_constraint_add, run_constraint_edit, run_constraint_list,
    run_constraint_show, run_constraint_rm, run_constraint_wipe,
)
from trainmate.cli.learnings import (
    run_learning_list, run_learning_show, run_learning_edit, run_learning_rm,
    run_learning_demote, run_learning_keep, run_learning_wipe,
)
from trainmate.cli.plans import (
    run_plan_generate, run_plan_show, run_plan_rm, run_plan_feedback, run_plan_wipe,
    run_plan_rollback, run_plan_versions,
)
from trainmate.cli.workouts import (
    run_workout_list, run_workout_compare, run_workout_generate, run_workout_rm,
    run_workout_restore, run_workout_adapt, run_workout_push, run_workout_swap,
    run_workout_add, run_workout_wipe,
)
from trainmate.cli.data import (
    run_data_pull, run_data_bootstrap, run_data_reflect, run_data_backfill_tss,
    run_data_show_metrics, run_data_show_activities, run_data_wipe,
)
from trainmate.cli.context import (
    run_context_add, run_context_rm, run_context_list, run_context_list_metrics,
)


def _weeks_arg(raw: str) -> int:
    """`--weeks N` must be a whole number >= 1 (DESIGN_progress_timeline.md §7.1) —
    rejected at argparse, so a `0` can't silently fall through to the default."""
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


def main() -> None:
    """Entry point for the TrainMate Command Line Interface."""
    parser = WrapAwareArgumentParser(
        description="TrainMate - Local Training Coach CLI",
    )
    parser.add_argument(
        "--llm-model", dest="llm_model",
        help="Override the OpenRouter model identifier"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # help command — prints every command and sub-command in one place. Registered
    # as a real sub-command (rather than just argparse's own --help) so it can
    # recurse through the whole sub-parser tree; see _print_command_tree.
    subparsers.add_parser(
        "help", help="Show every command and sub-command in one place"
    )

    # Common parser for commands that support bypassing or forcing the auto-pull.
    # --no-pull and --force-pull are opposite ends of the same throttle, so they're
    # mutually exclusive.
    pull_bypass_parser = argparse.ArgumentParser(add_help=False)
    _pull_group = pull_bypass_parser.add_mutually_exclusive_group()
    _pull_group.add_argument(
        "--no-pull", action="store_true", dest="no_pull",
        help="Skip the Garmin/Calendar pull check, reading purely from the SQLite cache"
    )
    _pull_group.add_argument(
        "--force-pull", action="store_true", dest="force_pull",
        help="Force a Garmin/Calendar refresh even within the refresh-minutes window, "
             "bypassing the cache-reuse throttle"
    )

    # Common parser for debugging LLM prompts
    llm_debug_parser = argparse.ArgumentParser(add_help=False)
    llm_debug_parser.add_argument(
        "--show-llm-prompt-only", action="store_true", dest="show_llm_prompt_only",
        help="Print the prompt that would be sent to the LLM and exit without sending"
    )

    # Basic date parser containing base date-filtering options
    basic_date_parser = argparse.ArgumentParser(add_help=False)
    basic_date_parser.add_argument(
        "--days", type=int, dest="days", metavar="N",
        help="Show/process data for N days"
    )
    basic_date_parser.add_argument(
        "--weeks", type=float, dest="weeks", metavar="N",
        help="Show/process data for N weeks"
    )
    basic_date_parser.add_argument(
        "--from", "--from-date", dest="from_date",
        help="Start from DATE (YYYY-MM-DD)"
    )
    basic_date_parser.add_argument(
        "--until", "--until-date", dest="until_date",
        help="End at DATE (YYYY-MM-DD)"
    )

    # Extended plan date parser that includes goal and mesocycle level filters
    plan_date_parser = argparse.ArgumentParser(add_help=False, parents=[basic_date_parser])
    plan_date_parser.add_argument(
        "--from-mesocycle", action="store_true", dest="from_meso",
        help="Start from the beginning of the current mesocycle"
    )
    plan_date_parser.add_argument(
        "--until-mesocycle", type=int, nargs="?", const=-1, dest="until_meso_id",
        metavar="ID", help="End at the end of a mesocycle (uses current if ID omitted)"
    )
    plan_date_parser.add_argument(
        "--mesocycle", type=int, nargs="?", const=-1, dest="meso_id", metavar="ID",
        help="Filter within a mesocycle (uses current if ID omitted)"
    )
    plan_date_parser.add_argument(
        "--goal", "--goal-id", type=int, nargs="?", const=-1, dest="goal_id", metavar="ID",
        help="Filter by a specific goal's plan duration (uses active goal if ID omitted)"
    )

    # Common parser for sport type filtering
    sport_type_parser = argparse.ArgumentParser(add_help=False)
    sport_type_parser.add_argument(
        "--type", "--sport-type", dest="sport_type",
        help="Filter by sport type"
    )

    # status command
    status_parser = subparsers.add_parser(
        "status",
        aliases=["s"],
        parents=[pull_bypass_parser],
        help="Show current athlete status, active goals, recent metrics, and memories",
        description=(
            "Show current athlete status: the next active goal and its plan, recent "
            "Garmin metrics, and coach learnings. By default freshens the recent "
            "metrics window from Garmin first; pass --no-pull to read only the cache."
        )
    )
    status_parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show all training objectives/goals and life events"
    )

    # progress command — the projected Performance Management Chart
    progress_parser = subparsers.add_parser(
        "progress",
        aliases=["p"],
        parents=[pull_bypass_parser],
        help="Show the training progress timeline: measured load to date, projected forward",
        description=(
            "Show a single continuous timeline of training load: past days measured "
            "from completed activities, future days planned from the current plan, one "
            "fitness/fatigue model (CTL/ATL/TSB) run across the seam. Projects to plan "
            "end (or each objective the plan reaches) so you can see whether the plan "
            "as written delivers peak fitness with positive form on race day."
        )
    )
    progress_parser.add_argument(
        "--weeks", type=_weeks_arg, default=8, metavar="N",
        help="Weeks of past weekly load to show (default: 8, must be >= 1). The "
             "future half always runs to plan end."
    )
    progress_parser.add_argument(
        "--chart", nargs="?", const=True, default=False, metavar="PATH",
        help="Also render the two-panel chart (PMC + weekly load) to a PNG "
             "(default: ./progress.png; requires matplotlib). Additive — the "
             "text output above still prints."
    )

    # goal command & subparsers
    goal_parser = subparsers.add_parser(
        "goal",
        aliases=["g"],
        help="Manage training objectives / goals of your training plan"
    )
    goal_subparsers = goal_parser.add_subparsers(dest="subcommand", help="Goal sub-commands")
    
    # goal add
    g_add = goal_subparsers.add_parser(
        "add", aliases=["a"], help="Add a new training objective/goal"
    )
    g_add.add_argument("--title", required=True, help="Goal title (e.g. Marathon)")
    g_add.add_argument("--date", required=True, help="Target event date (YYYY-MM-DD)")
    g_add.add_argument(
        "--sport", required=True, nargs="+",
        choices=["running", "road_biking", "hiking", "strength_training", "yoga", "ski_touring"],
        help="Sport types (one or more)"
    )
    g_add.add_argument("--desc", default="", help="Description")
    g_add.add_argument("--priority", type=int, default=1, help="Goal priority (1 = highest)")

    # goal edit
    g_edit = goal_subparsers.add_parser(
        "edit", aliases=["e"], help="Edit an existing goal/objective"
    )
    g_edit.add_argument("id", type=int, help="Goal ID to edit")
    g_edit.add_argument("--title", help="New goal title")
    g_edit.add_argument("--date", help="New target event date (YYYY-MM-DD)")
    g_edit.add_argument(
        "--sport", nargs="+",
        choices=["running", "road_biking", "hiking", "strength_training", "yoga", "ski_touring"],
        help="New sport types (one or more)"
    )
    g_edit.add_argument("--desc", help="New description")
    g_edit.add_argument("--priority", type=int, help="New priority (1 = highest)")
    g_edit.add_argument(
        "--status", choices=["active", "completed", "archived"],
        help="New status ('active', 'completed', 'archived')"
    )
    
    # goal rm
    g_rm = goal_subparsers.add_parser("rm", aliases=["r"], help="Remove a goal by ID")
    g_rm.add_argument("id", type=int, help="Goal ID to remove")
    
    # goal list
    goal_subparsers.add_parser("list", aliases=["l"], help="Show all training objectives")

    # goal wipe
    g_wipe = goal_subparsers.add_parser("wipe", help="Wipe all training objectives")
    g_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # constraint command & subparsers — the single directive object
    # (DESIGN_constraints.md). Everything the athlete asks the coach to work around, at
    # any horizon; supersedes `lifeevent`.
    constraint_parser = subparsers.add_parser(
        "constraint",
        aliases=["cons"],
        help="Author/list directives the coach works around (availability, caps, "
             "preferences, disruptions)",
        description=(
            "Manage constraints — anything you ask the coach to work around, at any "
            "horizon ('no run Thursday', 'only 45 min today', '3-week injury layoff'). "
            "A blanket 'hard' constraint (no --sport) deterministically forces rest in "
            "generate/adapt; a 'hard' constraint scoped to a sport, and every 'soft' one, "
            "is advisory — the coach honors it by judgement. Whether a constraint reshapes "
            "the plan is derived from its magnitude and human-confirmed, not picked up front."
        ),
    )
    constraint_subparsers = constraint_parser.add_subparsers(
        dest="subcommand", help="Constraint sub-commands"
    )

    def _add_binding_flags(p, default):
        grp = p.add_mutually_exclusive_group()
        grp.add_argument("--hard", dest="binding", action="store_const", const="hard",
                         help="No training those dates (enforced); with --sport, that "
                              "sport is unavailable (advisory)")
        grp.add_argument("--soft", dest="binding", action="store_const", const="soft",
                         help="Advisory preference/capacity hint (default)")
        p.set_defaults(binding=default)

    def _add_replan_flags(p):
        grp = p.add_mutually_exclusive_group()
        grp.add_argument("--replan", dest="replan", action="store_const", const=True,
                         help="Escalate to plan-shaping and regenerate around it")
        grp.add_argument("--no-replan", dest="replan", action="store_const", const=False,
                         help="Keep out of the plan (honored by daily adapt only)")
        p.set_defaults(replan=None)

    # constraint add
    cons_add = constraint_subparsers.add_parser(
        "add", aliases=["a"], help="Author a directive over a day or range"
    )
    cons_add.add_argument("title", nargs="?", help="The directive, stated short "
                          "(e.g. 'no run Thursday'); prompted if omitted")
    cons_add.add_argument("--title", dest="title_opt", help=argparse.SUPPRESS)
    cons_add.add_argument("--start", help="Start date (YYYY-MM-DD; default: today)")
    cons_add.add_argument("--end", help="End date (YYYY-MM-DD; default: --start)")
    cons_add.add_argument("--sport", help="Scope to one sport (default: all sports)")
    cons_add.add_argument("--type", help="Optional opaque label (e.g. trip, injury)")
    cons_add.add_argument("--desc", "--description", dest="desc",
                          help="Optional richer context for the coach")
    _add_binding_flags(cons_add, default="soft")
    _add_replan_flags(cons_add)

    # constraint edit
    cons_edit = constraint_subparsers.add_parser(
        "edit", aliases=["e"], help="Adjust scope / bindingness / text / replan"
    )
    cons_edit.add_argument("id", type=int, help="Constraint ID to edit")
    cons_edit.add_argument("--title", help="New directive title")
    cons_edit.add_argument("--start", help="New start date (YYYY-MM-DD)")
    cons_edit.add_argument("--end", help="New end date (YYYY-MM-DD)")
    cons_edit.add_argument("--sport", help="New sport scope ('' to clear)")
    cons_edit.add_argument("--type", help="New opaque label ('' to clear)")
    cons_edit.add_argument("--desc", "--description", dest="desc",
                           help="New richer context ('' to clear)")
    _add_binding_flags(cons_edit, default=None)
    _add_replan_flags(cons_edit)

    # constraint list
    cons_list = constraint_subparsers.add_parser(
        "list", aliases=["l"], help="List active/upcoming directives"
    )
    cons_list.add_argument("-v", "--verbose", action="store_true",
                           help="Show details for each directive")
    cons_list.add_argument("--all", action="store_true",
                           help="Include past (expired) directives too")
    cons_list.add_argument("--sport", help="Filter to directives affecting this sport")
    cons_list.add_argument("--type", help="Filter to this opaque type label")
    cons_list.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Override the default lower bound (config.metrics_lookback_days back)"
    )
    cons_list.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="Bound the upper end too (default: open-ended/upcoming)"
    )

    # constraint show
    cons_show = constraint_subparsers.add_parser(
        "show", aliases=["s"], help="Show one directive in detail (incl. plan-shaping)"
    )
    cons_show.add_argument("id", type=int, help="Constraint ID to display")

    # constraint rm
    cons_rm = constraint_subparsers.add_parser(
        "rm", aliases=["r"], help="Remove a directive by ID"
    )
    cons_rm.add_argument("id", type=int, help="Constraint ID to remove")

    # constraint wipe
    cons_wipe = constraint_subparsers.add_parser("wipe", help="Wipe all constraints")
    cons_wipe.add_argument("-y", "--yes", action="store_true",
                           help="Skip confirmation prompt")

    # context command & subparsers — first-party daily-context authoring
    context_parser = subparsers.add_parser(
        "context",
        aliases=["ctx"],
        help="Author/list/remove daily-context signals (heat, sleep, stress, …)",
        description=(
            "Manage external daily-context signals — the same tagged Google Calendar "
            "events that 'data pull' ingests into the coach's view. 'add' authors them "
            "(one all-day event per day), 'rm' removes them (calendar event + local row), "
            "'list' and 'list-metrics' inspect what's recorded. TrainMate stays "
            "domain-agnostic: a metric is opaque free text."
        )
    )
    context_subparsers = context_parser.add_subparsers(
        dest="subcommand", help="Context sub-commands"
    )

    # context add
    ctx_add = context_subparsers.add_parser(
        "add", aliases=["a"],
        help="Author a context signal over a day or date range",
        description=(
            "Write a tagged all-day context event per day in the range and mirror it "
            "locally. Defaults to today; the label is optional. Re-adding "
            "the same (date, metric) updates in place rather than duplicating."
        )
    )
    ctx_add.add_argument(
        "text", nargs="*", help="Human label/summary (e.g. severe heatwave)"
    )
    ctx_add.add_argument(
        "-l", "--label",
        help="Human label/summary (alternative to the positional text; takes "
             "precedence, and avoids word-splitting for multi-word labels)"
    )
    ctx_add.add_argument(
        "-m", "--metric", required=True,
        help="Opaque category, e.g. heat, sleep, stress"
    )
    ctx_add.add_argument(
        "--value", type=float, metavar="N",
        help="Optional free numeric magnitude (severity, °C, count — uninterpreted)"
    )
    ctx_add.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Start date (default: today)"
    )
    ctx_add.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="End date (default: --from)"
    )

    # context rm
    ctx_rm = context_subparsers.add_parser(
        "rm", aliases=["r"],
        help="Remove signal(s) by ID, or by date range + metric",
        description=(
            "Delete context signal(s). Pass row IDs, or narrow with "
            "--from/--until/--metric. The calendar event is deleted too, so a full "
            "re-pull cannot resurrect it."
        )
    )
    ctx_rm.add_argument("ids", nargs="*", type=int, help="Context row IDs to remove")
    ctx_rm.add_argument("-m", "--metric", help="Restrict range removal to this metric")
    ctx_rm.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Start date for range removal"
    )
    ctx_rm.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="End date for range removal"
    )
    ctx_rm.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation for multi-row removal"
    )

    # context list
    ctx_list = context_subparsers.add_parser(
        "list", aliases=["l"],
        help="List context signals (default window: the coach's metrics lookback)"
    )
    ctx_list.add_argument("-m", "--metric", help="Filter to a single metric")
    ctx_list.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Start date (default: metrics_lookback_days before --until)"
    )
    ctx_list.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="End date (default: today)"
    )

    # context list-metrics
    context_subparsers.add_parser(
        "list-metrics", aliases=["lm"],
        help="Show distinct metrics in use with counts and date span"
    )

    # learnings command & subparsers
    learnings_parser = subparsers.add_parser(
        "learnings",
        aliases=["l", "learn"],
        help="View and curate coach learnings (LLM observations from your history)"
    )
    learnings_subparsers = learnings_parser.add_subparsers(
        dest="subcommand", help="Learnings sub-commands"
    )

    # learnings list
    ln_list = learnings_subparsers.add_parser(
        "list", aliases=["l"], help="Show coach learnings"
    )
    ln_list.add_argument(
        "--dormant", action="store_true", help="Show only dormant (decayed) learnings"
    )
    ln_list.add_argument("--sport", help="Filter by sport (substring match)")
    ln_list.add_argument(
        "--confidence", choices=["tentative", "moderate", "established"],
        help="Filter by confidence level"
    )
    ln_list.add_argument(
        "-v", "--verbose", action="store_true",
        help="Also show created/updated/reinforced timestamps"
    )

    # learnings show
    ln_show = learnings_subparsers.add_parser(
        "show", aliases=["s"], help="Show a learning and its evidence basis by ID"
    )
    ln_show.add_argument("id", type=int, help="Learning ID to display")

    # learnings edit
    ln_edit = learnings_subparsers.add_parser(
        "edit", aliases=["e"], help="Revise the text of a learning"
    )
    ln_edit.add_argument("id", type=int, help="Learning ID to edit")
    ln_edit.add_argument("--text", required=True, help="New learning text")

    # learnings rm
    ln_rm = learnings_subparsers.add_parser("rm", aliases=["r"], help="Remove a learning by ID")
    ln_rm.add_argument("id", type=int, help="Learning ID to remove")

    # learnings demote
    ln_demote = learnings_subparsers.add_parser(
        "demote", aliases=["d"], help="Accept a pending confidence demotion"
    )
    ln_demote.add_argument("id", type=int, help="Learning ID to demote")

    # learnings keep
    ln_keep = learnings_subparsers.add_parser(
        "keep", aliases=["k"], help="Dismiss a pending demotion (affirms the learning)"
    )
    ln_keep.add_argument("id", type=int, help="Learning ID to keep")

    # learnings wipe
    ln_wipe = learnings_subparsers.add_parser("wipe", help="Wipe all coach learnings")
    ln_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # plan command & subparsers
    plan_parser = subparsers.add_parser(
        "plan",
        help="Manage and consult the periodized training plan (macrocycles & mesocycles)"
    )
    plan_subparsers = plan_parser.add_subparsers(
        dest="subcommand", help="Plan sub-commands"
    )
    
    # plan generate
    p_gen = plan_subparsers.add_parser(
        "generate", aliases=["g"],
        parents=[pull_bypass_parser, llm_debug_parser],
        help=(
            "Generate or adapt the periodized training plan strategy "
            "(macrocycles & mesocycles)"
        )
    )
    p_gen.add_argument(
        "-f", "--force", action="store_true",
        help="Force regeneration of the macrocycle/mesocycle strategy"
    )
    p_gen.add_argument(
        "-y", "--yes", "--auto", action="store_true", dest="auto",
        help="Apply proposed plan updates automatically without prompting"
    )
    p_gen.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID to generate the periodization plan for"
    )
    
    # plan show
    p_show = plan_subparsers.add_parser(
        "show", aliases=["s"],
        help="Show the active macrocycle and mesocycles periodization strategy"
    )
    p_show.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID to show the periodization plan for"
    )
    p_show.add_argument(
        "--version", type=int, dest="version", metavar="PLAN_ID",
        help="Show a specific (e.g. superseded) plan version by ID instead of the active one"
    )

    # plan versions
    p_versions = plan_subparsers.add_parser(
        "versions", aliases=["v"],
        help="List all plan versions (active + superseded) for a goal",
        description=(
            "List every periodization plan version kept for a goal — the active one and "
            "any superseded by later regenerations — with their IDs and dates, so you can "
            "inspect one ('plan show --version <ID>') or restore one "
            "('plan rollback --version <ID>')."
        )
    )
    p_versions.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID whose plan versions to list (defaults to the next active goal)"
    )

    # plan rm
    p_rm = plan_subparsers.add_parser(
        "rm", aliases=["d"],
        help="Remove/delete a specific periodization plan by Goal ID"
    )
    p_rm.add_argument(
        "id", type=int,
        help="Goal ID whose periodization plan should be removed"
    )

    # plan rollback
    p_rollback = plan_subparsers.add_parser(
        "rollback", aliases=["rb"],
        help="Restore a superseded plan version and its workouts",
        description=(
            "Undo a plan regeneration: restore an earlier periodization plan version "
            "and the workouts that were live under it. Defaults to the chronologically "
            "previous version of the next active goal's plan; repeat to walk further "
            "back, or target a specific version with --version. The current plan's "
            "upcoming workouts are archived and the restored version's are re-pushed to "
            "Google Calendar (the symmetric inverse of generation)."
        )
    )
    p_rollback.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID whose plan to roll back (defaults to the next active goal)"
    )
    p_rollback.add_argument(
        "--version", type=int, dest="version", metavar="PLAN_ID",
        help="Roll back to a specific plan version (macrocycle) ID instead of the previous one"
    )
    p_rollback.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )

    # plan feedback
    p_fb = plan_subparsers.add_parser(
        "feedback",
        aliases=["f"],
        description="Add athlete feedback (either --macro or --meso is mandatory).",
        help="Add athlete feedback (either --macro or --meso is mandatory)"
    )
    p_fb.add_argument(
        "--macro", action="store_true",
        help="Provide general feedback on the overall macrocycle strategy"
    )
    p_fb.add_argument(
        "--meso", type=int,
        help="Provide feedback on a specific mesocycle ID"
    )
    p_fb.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help=(
            "Target goal ID whose plan the feedback should attach to "
            "(default to the current active goal)"
        )
    )
    p_fb.add_argument(
        "--edit", action="store_true",
        help="Open $EDITOR seeded with the current feedback (takes no text argument)"
    )
    p_fb.add_argument(
        "text", nargs="?", default=None,
        help="Feedback content string (omit when using --edit)"
    )

    # plan wipe
    p_wipe = plan_subparsers.add_parser("wipe", help="Wipe all periodization plans")
    p_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # workout command & subparsers
    workout_parser = subparsers.add_parser(
        "workout",
        aliases=["w"],
        help="Manage workouts (microcycles)"
    )
    workout_subparsers = workout_parser.add_subparsers(
        dest="subcommand", help="Workout sub-commands"
    )
    
    # workout list
    w_list = workout_subparsers.add_parser(
        "list", aliases=["l"],
        parents=[plan_date_parser, sport_type_parser],
        help="Show all planned workouts",
        description=(
            "List planned workouts chronologically. With no date filter, shows today "
            "through the next 7 days; with only --type, shows today onward. Reads the "
            "local database only (no Garmin pull)."
        )
    )
    w_list.add_argument(
        "--removed", action="store_true",
        help="Include soft-removed workouts (e.g. to find their ID for restoring)"
    )
    
    # workout compare
    w_cmp = workout_subparsers.add_parser(
        "compare", aliases=["c"],
        parents=[pull_bypass_parser, plan_date_parser, sport_type_parser],
        help="Compare planned workouts against completed activities",
        description=(
            "Compare planned workouts against completed Garmin activities, flagging "
            "missed sessions, rest-day violations, and unplanned high-load efforts. "
            "With no date filter, looks back 14 days; here --days/--weeks look "
            "backward (not forward) and the end date is always capped at today. "
            "Freshens Garmin data for the range first unless --no-pull is given. "
            "Each past event's Calendar entry is stamped with the adherence verdict "
            "(a [Done]/[Missed]/[Partial]/... title tag and an 'Adherence' description "
            "header) unless --no-mark is given."
        )
    )
    w_cmp.add_argument(
        "--no-mark", action="store_true", dest="no_mark",
        help=(
            "Skip writing the adherence verdict back to each past workout's Google "
            "Calendar event (title tag + description header)."
        )
    )

    # workout generate
    p_w_gen = workout_subparsers.add_parser(
        "generate", aliases=["g"],
        parents=[pull_bypass_parser, llm_debug_parser],
        help="Generate workouts (microcycles) based on the active strategy",
        description=(
            "Generate workouts (microcycles) from today, driven by the active "
            "periodization strategy. With no horizon flag, generates "
            "config.workout_generation_span_days days ahead (28 by default). The new plan "
            "is pushed to Google Calendar straight away (the previous plan's upcoming "
            f"workouts are archived first); use '{green('plan rollback')}' to undo a "
            "regeneration."
        )
    )
    p_w_gen.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID to generate workouts for"
    )
    p_w_gen_horizon = p_w_gen.add_mutually_exclusive_group()
    p_w_gen_horizon.add_argument(
        "--days", type=int, dest="horizon_days", metavar="N",
        help="Generate workouts for N days from today"
    )
    p_w_gen_horizon.add_argument(
        "--weeks", type=float, dest="horizon_weeks", metavar="N",
        help="Generate workouts for N weeks from today"
    )
    p_w_gen_horizon.add_argument(
        "--until", dest="horizon_until", metavar="DATE",
        help="Generate workouts until DATE (YYYY-MM-DD)"
    )
    p_w_gen_horizon.add_argument(
        "--until-goal", type=int, nargs="?", const=-1, dest="horizon_goal_id", metavar="ID",
        help="Generate workouts until the target date of a goal (uses current goal if ID omitted)"
    )
    p_w_gen_horizon.add_argument(
        "--until-mesocycle", type=int, dest="horizon_meso_id", metavar="ID",
        help="Generate workouts until the end date of a mesocycle"
    )
    
    # workout add
    w_add = workout_subparsers.add_parser(
        "add",
        help="Manually schedule a workout on a date (replaces any same-sport session)",
        description=(
            "Manually add a workout on a specific date — driven by you rather than the "
            "coach. If a workout of the same sport already exists that day it is "
            "replaced (use --replace-day to instead replace every session that day "
            "regardless of sport), and each replaced session's description, duration, "
            "TSS and RPE are recorded on the new workout (and its Calendar event) so the "
            "change stays traceable. Saved and synced to Google Calendar immediately. To "
            f"have the coach re-balance surrounding load afterward, run '{green('workout adapt')}'."
        )
    )
    w_add.add_argument("date", help="Workout date (YYYY-MM-DD)")
    w_add.add_argument("sport_type", help="Sport type (e.g. running, road_biking)")
    w_add.add_argument("--title", required=True, help="Workout title")
    w_add.add_argument(
        "--description", "--desc", dest="description", help="Workout description / details"
    )
    w_add.add_argument(
        "--duration", type=int, dest="duration", metavar="MIN",
        help="Planned duration in minutes"
    )
    w_add.add_argument("--rpe", type=int, help="Target RPE (1-10)")
    w_add.add_argument("--tss", type=int, help="Target training stress score")
    w_add.add_argument(
        "--reason",
        help="Why you're adding/replacing this session (recorded and shown to the coach)"
    )
    w_add.add_argument(
        "--replace-day", dest="replace_day", action="store_true",
        help="Replace every session that day, not just the same sport"
    )

    # workout rm
    w_rm = workout_subparsers.add_parser(
        "rm", aliases=["r"], help="Remove a workout by ID"
    )
    w_rm.add_argument("id", type=int, help="Workout ID to remove")
    w_rm.add_argument(
        "--reason", required=True,
        help="Why the workout is being removed (shown to the coach as a deliberate "
             "cancellation)"
    )

    # workout restore
    w_restore = workout_subparsers.add_parser(
        "restore", aliases=["res"], help="Restore a soft-removed workout by ID"
    )
    w_restore.add_argument("id", type=int, help="Workout ID to restore")
    
    # workout adapt
    w_adapt = workout_subparsers.add_parser(
        "adapt", aliases=["a"],
        parents=[pull_bypass_parser, llm_debug_parser],
        help="Run the daily Garmin check for today (syncs adapted workouts to Calendar)",
        description=(
            "Run the daily adaptation check: read recent recovery metrics and let the "
            "coach adjust upcoming workouts. Defaults to today (UTC); use --date for "
            "another day. Proposed changes are confirmed interactively unless -y/--auto "
            "is given, then synced to Google Calendar."
        )
    )
    w_adapt.add_argument("--date", help="Date in YYYY-MM-DD format (defaults to UTC today)")
    w_adapt.add_argument(
        "--lookback", type=int, metavar="DAYS",
        help="Days of recovery-metrics trajectory to display (default: config metrics_lookback_days)"
    )
    w_adapt.add_argument(
        "-m", "--message", dest="message",
        help=(
            "Ad-hoc, one-off signal to the coach for THIS adaptation run only (e.g. "
            "'knee is sore, keep impact low', 'no bike access Thursday'). Advisory: it "
            "won't override clear fatigue signals. The note itself isn't stored, but if "
            "it drives a session change its cause is recorded in that session's reason so "
            "a later run understands the tactical change; it stays a one-off and never "
            "becomes durable block evidence. For persistent context (alcohol, sleep, "
            "stress) use 'context add' instead."
        )
    )
    w_adapt.add_argument(
        "-y", "--yes", "--auto", action="store_true", dest="auto",
        help="Apply proposed adaptations automatically without prompting"
    )
    
    # workout push
    w_push = workout_subparsers.add_parser(
        "push", aliases=["p"],
        parents=[plan_date_parser, sport_type_parser],
        help="Commit local planned workouts to Google Calendar",
        description=(
            "Push planned workouts to Google Calendar. With no date filter, pushes "
            "from today onward. By default only new or modified (unsynced) workouts "
            "are sent; use -f/--force to re-push already-synced workouts, overwriting "
            "their calendar entries."
        )
    )
    w_push.add_argument(
        "-f", "--force", action="store_true",
        help="Re-push already-synced workouts, overwriting existing calendar entries"
    )

    # workout swap
    w_swap = workout_subparsers.add_parser(
        "swap", aliases=["s"],
        parents=[llm_debug_parser],
        help="Swap workouts between two dates (or two IDs), with recovery checks",
        description=(
            "Swap two workouts, given either two dates (date1 date2) or two IDs "
            "(--id1/--id2). Runs recovery checks (consecutive hard days, weekly load "
            "spikes, mesocycle crossings) and prompts on warnings unless -f/--force. "
            "The swap is synced to Google Calendar unless --no-sync is given."
        )
    )
    w_swap.add_argument(
        "date1", nargs="?", help="First date to swap (YYYY-MM-DD)"
    )
    w_swap.add_argument(
        "date2", nargs="?", help="Second date to swap (YYYY-MM-DD)"
    )
    w_swap.add_argument(
        "--id1", type=int, help="First workout ID (use together with --id2)"
    )
    w_swap.add_argument(
        "--id2", type=int, help="Second workout ID (use together with --id1)"
    )
    w_swap.add_argument(
        "--no-sync", action="store_true", dest="no_sync",
        help="Do not sync the swapped workouts to Google Calendar"
    )
    w_swap.add_argument(
        "-f", "--force", action="store_true", dest="force",
        help="Apply the swap without prompting, even if warnings are raised"
    )
    w_swap.add_argument(
        "--reason", required=True,
        help="Why the workouts are being swapped (recorded and shown to the coach)"
    )



    # workout wipe
    w_wipe = workout_subparsers.add_parser(
        "wipe",
        help="Wipe all workouts from the database and Google Calendar"
    )
    w_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # data command & subparsers
    data_parser = subparsers.add_parser(
        "data",
        aliases=["d"],
        help="Manage and sync athlete metrics and activities"
    )
    data_subparsers = data_parser.add_subparsers(
        dest="subcommand", help="Data sub-commands"
    )
    
    # data pull
    d_pull = data_subparsers.add_parser(
        "pull",
        aliases=["p"],
        help="Fetch Garmin activities/metrics and Google Calendar context",
        description=(
            "Fetch activities and daily metrics directly from Garmin Connect into the "
            "local cache, advancing the sync watermark. With no range, pulls the last "
            "2 days ending today (--days N for a different window, or --from/--until "
            "for an explicit range). Pulls both metrics and activities unless "
            "--metrics-only/--activities-only is given. Also syncs tagged daily-context "
            "events (alcohol, sleep, stress, …) from Google Calendar into the local cache. "
            "Past Calendar events in the pulled range are stamped with the adherence "
            "verdict unless --no-mark is given."
        )
    )
    d_pull.add_argument(
        "--days", type=int, default=2, metavar="N",
        help="Number of days to pull, ending today (default: 2)"
    )
    d_pull.add_argument(
        "--no-mark", action="store_true", dest="no_mark",
        help="Skip stamping past Calendar events with the adherence verdict"
    )
    d_pull.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Start date for an explicit range (overrides --days)"
    )
    d_pull.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="End date for an explicit range (defaults to today)"
    )
    d_pull.add_argument(
        "--sleep", type=float, dest="sleep", metavar="SECONDS",
        help="Throttle: seconds to sleep between Garmin calls (default from config)"
    )
    pull_group = d_pull.add_mutually_exclusive_group()
    pull_group.add_argument(
        "--metrics-only", action="store_true", help="Pull daily metrics only"
    )
    pull_group.add_argument(
        "--activities-only", action="store_true", help="Pull activities only"
    )

    # data bootstrap — cold-start backward reconstruction over the full backlog
    d_boot = data_subparsers.add_parser(
        "bootstrap", aliases=["b"],
        parents=[pull_bypass_parser, basic_date_parser, llm_debug_parser],
        help="Reconstruct macro/mesocycles from your full backlog (run once): "
             "seeds coach learnings + a cached reconstruction fed to 'plan generate'",
        description=(
            "Cold-start: reverse-engineer past training cycles from completed workouts "
            "and metrics. With no date filter, the window is auto-detected from the active "
            "goal (since the previous goal, else 12 weeks back). Two outputs: (1) coach "
            "learnings, delta-updated from the evidence; and (2) a cached reconstruction — "
            "the inferred macro focus, mesocycle blocks, and physiological insights — which "
            "'plan generate' replays read-only into its strategy prompt so the next plan "
            "builds on your demonstrated training arc. Also establishes the reflect "
            "watermark so later 'data reflect' runs only ingest newer evidence. Cached by "
            "evidence fingerprint: an unchanged re-run reuses the cache unless --force; "
            "--inspect-only renders the analysis without writing learnings or the cache."
        )
    )
    # data reflect — incremental reflection over evidence since the last reflect
    d_reflect = data_subparsers.add_parser(
        "reflect", aliases=["r"],
        parents=[pull_bypass_parser, basic_date_parser, llm_debug_parser],
        help="Update coach learnings from how the athlete responded to training "
             "since the last reflect (incremental; no reconstruction)",
        description=(
            "Incremental: analyze only evidence accrued since the last reflect watermark "
            "(the day after the last reflected-through date). Its output is coach learnings "
            "— delta-updated from the new evidence; unlike 'data bootstrap' it does not "
            "feed a reconstruction to 'plan generate'. A date filter overrides the "
            "watermark. Because overlapping history is never re-counted, repeated runs no "
            "longer ratchet confidence to 'established'. Run 'data bootstrap' first to "
            "establish a baseline. --inspect-only renders without writing; --force bypasses "
            "the per-window cache."
        )
    )
    for d_an in (d_boot, d_reflect):
        d_an.add_argument(
            "--context", dest="context",
            help="Optional text context detailing subjective athlete notes (travel, illness, etc.)"
        )
        d_an.add_argument(
            "-f", "--force", action="store_true",
            help="Recompute even if the evidence is unchanged (bypass the analysis cache)"
        )
        d_an.add_argument(
            "--inspect-only", action="store_true",
            help="Read-only: show the analysis without writing coach learnings or the cache"
        )
        d_an.add_argument(
            "--auto", action="store_true",
            help="Unattended: skip interactive demotion prompts. Staleness demotions apply "
                 "directly; contradiction demotions stay queued for the next interactive review."
        )

    # data backfill-tss
    d_btss = data_subparsers.add_parser(
        "backfill-tss",
        help="Recompute TSS for all stored activities using the current "
             "zone-based model (no Garmin calls needed)"
    )
    d_btss.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Only process activities on or after this date"
    )
    d_btss.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="Only process activities on or before this date"
    )
    d_btss.add_argument(
        "-v", "--verbose", action="store_true",
        help="List activities with low HR-zone coverage that need an RPE"
    )

    # data show-metrics
    d_sm = data_subparsers.add_parser(
        "show-metrics", aliases=["sm"],
        parents=[pull_bypass_parser, plan_date_parser],
        help="Show athlete metrics over a date range",
        description=(
            "Show cached daily athlete metrics (RHR, HRV, sleep, stress) over a date "
            "range. With no date filter, looks back 7 days ending today; -a/--all "
            "shows every cached row. Freshens recent data from Garmin first unless "
            "--no-pull or --all is given. Use --csv for machine-readable output."
        )
    )
    d_sm.add_argument(
        "--csv", action="store_true", dest="csv",
        help="Output data as CSV for script consumption"
    )
    d_sm.add_argument(
        "-a", "--all", action="store_true", dest="all",
        help="Show all cached athlete metrics"
    )

    # data show-activities
    d_sa = data_subparsers.add_parser(
        "show-activities", aliases=["sa"],
        parents=[pull_bypass_parser, plan_date_parser, sport_type_parser],
        help="Show completed activities over a date range",
        description=(
            "Show cached completed activities over a date range. With no date filter, "
            "looks back 7 days ending today; -a/--all shows every cached activity. "
            "Filter with --type, freshen from Garmin unless --no-pull/--all, and use "
            "--csv for machine-readable output."
        )
    )
    d_sa.add_argument(
         "--csv", action="store_true", dest="csv",
         help="Output data as CSV for script consumption"
     )
    d_sa.add_argument(
        "-a", "--all", action="store_true", dest="all",
        help="Show all cached completed activities"
    )

    # data wipe
    d_wipe = data_subparsers.add_parser(
        "wipe",
        help="Wipe locally cached Garmin data and/or daily context from the database",
        description=(
            "Delete locally cached data. With no scope flag, wipes everything (Garmin "
            "metrics, baselines, activities, and ingested daily-context signals) and "
            "resets the sync watermarks. --garmin or --calendar narrow the scope; "
            "--from/--until/--days restrict it to a date window (the next 'data pull' "
            "re-fetches what was removed)."
        ),
    )
    d_wipe.add_argument(
        "--garmin", action="store_true",
        help="Wipe only Garmin evidence (metrics, baselines, activities, analysis cache)"
    )
    d_wipe.add_argument(
        "--calendar", "--context", action="store_true", dest="calendar",
        help="Wipe only ingested daily-context signals and reset the Calendar sync token"
    )
    d_wipe.add_argument(
        "--days", type=int, metavar="N",
        help="Restrict to the trailing N days (ending --until, default today)"
    )
    d_wipe.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Restrict to rows on or after this date"
    )
    d_wipe.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="Restrict to rows on or before this date"
    )
    d_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    # Parse the arguments. Network-appliance-style dashless options
    # (e.g. `workout adapt message "..." no-pull`) are first rewritten back into
    # `--flag` form against the parser tree, so both syntaxes share one definition.
    args = parser.parse_args(translate_dashless_argv(parser, sys.argv[1:]))
    
    if getattr(args, "llm_model", None):
        from trainmate.openrouter import openrouter_client
        openrouter_client.model = args.llm_model

    if getattr(args, "show_llm_prompt_only", False):
        from trainmate.openrouter import openrouter_client
        openrouter_client.show_prompt_only = True

    if not args.command:
        parser.print_help()
        sys.exit(1)
        
    cmd = args.command.lower()

    if cmd == "help":
        print(bold(parser.description))
        print()
        _print_command_tree(parser)
    elif cmd in ("status", "s"):
        run_status(verbose=args.verbose, no_pull=args.no_pull, force_pull=args.force_pull)
    elif cmd in ("progress", "p"):
        run_progress(args)
    elif cmd in ("goal", "g"):
        if not args.subcommand:
            goal_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("add", "a"):
            run_goal_add(args)
        elif sub in ("edit", "e"):
            run_goal_edit(args)
        elif sub in ("rm", "r"):
            run_goal_rm(args)
        elif sub in ("list", "l"):
            run_goal_list()
        elif sub == "wipe":
            run_goal_wipe(args)
    elif cmd in ("constraint", "cons"):
        if not args.subcommand:
            constraint_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("add", "a"):
            # The positional TITLE and the hidden --title alias both land here; prefer
            # the flag form when supplied.
            if getattr(args, "title_opt", None):
                args.title = args.title_opt
            run_constraint_add(args)
        elif sub in ("edit", "e"):
            run_constraint_edit(args)
        elif sub in ("rm", "r"):
            run_constraint_rm(args)
        elif sub in ("list", "l"):
            run_constraint_list(args)
        elif sub in ("show", "s"):
            run_constraint_show(args)
        elif sub == "wipe":
            run_constraint_wipe(args)
    elif cmd in ("context", "ctx"):
        if not args.subcommand:
            context_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("add", "a"):
            run_context_add(args)
        elif sub in ("rm", "r"):
            run_context_rm(args)
        elif sub in ("list", "l"):
            run_context_list(args)
        elif sub in ("list-metrics", "lm"):
            run_context_list_metrics(args)
    elif cmd in ("learnings", "learn", "l"):
        if not args.subcommand:
            learnings_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("list", "l"):
            run_learning_list(args)
        elif sub in ("show", "s"):
            run_learning_show(args)
        elif sub in ("edit", "e"):
            run_learning_edit(args)
        elif sub in ("rm", "r"):
            run_learning_rm(args)
        elif sub in ("demote", "d"):
            run_learning_demote(args)
        elif sub in ("keep", "k"):
            run_learning_keep(args)
        elif sub == "wipe":
            run_learning_wipe(args)
    elif cmd in ("workout", "w"):
        if not args.subcommand:
            workout_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("list", "l"):
            run_workout_list(args)
        elif sub in ("compare", "c"):
            run_workout_compare(args)
        elif sub in ("generate", "g"):
            run_workout_generate(args)
        elif sub in ("rm", "r"):
            run_workout_rm(args)
        elif sub in ("restore", "res"):
            run_workout_restore(args)
        elif sub in ("adapt", "a"):
            run_workout_adapt(args)
        elif sub in ("push", "p"):
            run_workout_push(args)
        elif sub in ("swap", "s"):
            run_workout_swap(args)
        elif sub == "add":
            run_workout_add(args)
        elif sub == "wipe":
            run_workout_wipe(args)
    elif cmd in ("data", "d"):
        if not args.subcommand:
            data_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("pull", "p"):
            run_data_pull(args)
        elif sub in ("bootstrap", "b"):
            run_data_bootstrap(args)
        elif sub in ("reflect", "r"):
            run_data_reflect(args)
        elif sub == "backfill-tss":
            run_data_backfill_tss(args)
        elif sub in ("show-metrics", "sm"):
            run_data_show_metrics(args)
        elif sub in ("show-activities", "sa"):
            run_data_show_activities(args)
        elif sub == "wipe":
            run_data_wipe(args)
    elif cmd == "plan":
        if not args.subcommand:
            plan_parser.print_help()
            sys.exit(1)
        sub = args.subcommand.lower()
        if sub in ("generate", "g"):
            run_plan_generate(args)
        elif sub in ("show", "s"):
            run_plan_show(args)
        elif sub in ("versions", "v"):
            run_plan_versions(args)
        elif sub in ("rm", "d"):
            run_plan_rm(args)
        elif sub in ("rollback", "rb"):
            run_plan_rollback(args)
        elif sub in ("feedback", "f"):
            run_plan_feedback(args)
        elif sub == "wipe":
            run_plan_wipe(args)
    else:
        print(f"Unknown command: '{cmd}'")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except PromptCancelled:
        # An interactive prompt was cancelled (front-end /cancel or idle timeout);
        # abort the command without the traceback an uncaught BaseException prints.
        print("Cancelled.")
