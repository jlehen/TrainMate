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

from trainmate.cli.argparse_ext import (
    WrapAwareArgumentParser, _edit_text_in_editor,
    _print_command_tree, translate_dashless_argv, _HelpAllAction,
)

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
from trainmate.cli.status import add_status_parser
from trainmate.cli.progress import add_progress_parser
from trainmate.cli.goals import add_goal_parser
from trainmate.cli.constraints import add_constraint_parser
from trainmate.cli.context import add_context_parser
from trainmate.cli.learnings import add_learnings_parser
from trainmate.cli.plans import add_plan_parser
from trainmate.cli.workouts import add_workout_parser
from trainmate.cli.data import add_data_parser


def build_parser():
    """Construct the argparse tree and return ``(parser, named_subparsers)``.

    ``named_subparsers`` maps a top-level command to its sub-parser so the
    dispatcher can print per-group help. Split out from ``main`` so the REPL can
    build the tree once and reuse it across many lines of input.
    """
    parser = WrapAwareArgumentParser(
        description="TrainMate - Local Training Coach CLI",
    )
    parser.add_argument(
        "--llm-model", dest="llm_model",
        help="Override the OpenRouter model identifier"
    )
    parser.add_argument(
        "--helpall", action=_HelpAllAction,
        help="Show every command including hidden maintenance ones (same as 'help --all')"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # help command — prints every command and sub-command in one place. Registered
    # as a real sub-command (rather than just argparse's own --help) so it can
    # recurse through the whole sub-parser tree; see _print_command_tree.
    help_parser = subparsers.add_parser(
        "help", help="Show every command and sub-command in one place"
    )
    help_parser.add_argument(
        "--all", action="store_true", dest="show_all",
        help="Also list hidden maintenance commands (wipe, bootstrap, …)"
    )

    # shell command — drop into the interactive REPL. See _repl.
    subparsers.add_parser(
        "shell", aliases=["sh"],
        help="Start an interactive shell, dispatching each line like a command"
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

    add_status_parser(subparsers, pull_bypass_parser)
    add_progress_parser(subparsers, pull_bypass_parser)
    goal_parser = add_goal_parser(subparsers)
    constraint_parser = add_constraint_parser(subparsers)
    context_parser = add_context_parser(subparsers)
    learnings_parser = add_learnings_parser(subparsers)
    plan_parser = add_plan_parser(subparsers, pull_bypass_parser, llm_debug_parser)
    workout_parser = add_workout_parser(subparsers, pull_bypass_parser, llm_debug_parser, plan_date_parser, sport_type_parser)
    data_parser = add_data_parser(subparsers, pull_bypass_parser, llm_debug_parser, basic_date_parser, plan_date_parser, sport_type_parser)

    named_subparsers = {
        "goal": goal_parser,
        "constraint": constraint_parser,
        "context": context_parser,
        "learnings": learnings_parser,
        "plan": plan_parser,
        "workout": workout_parser,
        "data": data_parser,
    }
    return parser, named_subparsers


def run_once(argv, parser, named_subparsers) -> None:
    """Parse one command line and dispatch it. Shared by ``main`` and the REPL."""
    # Parse the arguments. Network-appliance-style dashless options
    # (e.g. `workout adapt message "..." no-pull`) are first rewritten back into
    # `--flag` form against the parser tree, so both syntaxes share one definition.
    args = parser.parse_args(translate_dashless_argv(parser, argv))

    if getattr(args, "llm_model", None):
        from trainmate.openrouter import openrouter_client
        openrouter_client.model = args.llm_model

    if getattr(args, "show_llm_prompt_only", False):
        from trainmate.openrouter import openrouter_client
        openrouter_client.show_prompt_only = True

    if not args.command:
        parser.print_help()
        sys.exit(1)

    goal_parser = named_subparsers["goal"]
    constraint_parser = named_subparsers["constraint"]
    context_parser = named_subparsers["context"]
    learnings_parser = named_subparsers["learnings"]
    plan_parser = named_subparsers["plan"]
    workout_parser = named_subparsers["workout"]
    data_parser = named_subparsers["data"]

    cmd = args.command.lower()

    if cmd == "help":
        print(bold(parser.description))
        print()
        _print_command_tree(parser, include_advanced=getattr(args, "show_all", False))
    elif cmd in ("shell", "sh"):
        _repl(parser, named_subparsers)
    elif cmd in ("status", "s"):
        run_status(verbose=args.verbose, no_pull=args.no_pull, force_pull=args.force_pull)
    elif cmd in ("progress", "pr"):
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
    elif cmd in ("plan", "pl"):
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


def _repl(parser, named_subparsers) -> None:
    """Read commands interactively until EOF/exit, dispatching each like a shell.

    Reached via the ``shell``/``sh`` command. Importing ``readline`` gives line
    editing and an in-session history for free.
    """
    import shlex
    try:
        import readline  # noqa: F401 -- registering it enables editing/history
    except ImportError:
        pass

    print(bold("TrainMate interactive shell") +
          dim(" — type a command, 'help' for the list, 'exit' or Ctrl-D to quit."))
    while True:
        try:
            line = input(cyan("tm> "))
        except EOFError:  # Ctrl-D
            print()
            break
        except KeyboardInterrupt:  # Ctrl-C at the prompt: abandon the line, stay in
            print()
            continue

        line = line.strip()
        if not line:
            continue
        if line.lower() in ("exit", "quit", "q"):
            break

        try:
            argv = shlex.split(line)
        except ValueError as exc:  # e.g. an unbalanced quote
            print(red(f"Parse error: {exc}"))
            continue

        try:
            run_once(argv, parser, named_subparsers)
        except SystemExit:
            # argparse errors, `-h`, and missing-subcommand paths call sys.exit;
            # swallow it so a bad line doesn't tear down the whole shell.
            pass
        except PromptCancelled:
            print("Cancelled.")
        except KeyboardInterrupt:  # Ctrl-C mid-command: cancel it, keep the shell
            print()
        except Exception as exc:  # a handler blew up; report and keep going
            print(red(f"Error: {exc}"))


def main(argv=None) -> None:
    """Entry point. With no arguments, print help; use `shell` for the REPL."""
    if argv is None:
        argv = sys.argv[1:]
    parser, named_subparsers = build_parser()
    run_once(argv, parser, named_subparsers)


if __name__ == "__main__":
    try:
        main()
    except PromptCancelled:
        # An interactive prompt was cancelled (front-end /cancel or idle timeout);
        # abort the command without the traceback an uncaught BaseException prints.
        print("Cancelled.")
