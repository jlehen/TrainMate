"""TrainMate CLI entry point: the argparse dispatcher (``main``) and its helpers.

The process-wide singletons live in ``trainmate.runtime`` and handlers read them as
``runtime.<name>`` at use time. Nothing under ``trainmate/`` imports this module any
more, so the self-alias into ``sys.modules`` that used to break the resulting import
cycle is gone with it.
"""
import argparse
import sys
from typing import Optional

from trainmate import runtime
from trainmate.prompt import PromptCancelled
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray, aside,
    visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, default_wrap_width,
    today_str as _today_str, today_date as _today_date,
)

from trainmate.cli.argparse_ext import (
    WrapAwareArgumentParser,
    _print_command_tree, translate_dashless_argv, _HelpAllAction,
    sort_command_tree,
)

# Help lists commands by usefulness, not argparse registration order
# (DESIGN_cli_noargs.md §c). One list per level, keyed by the parent's canonical
# name ("" = top level), each level's visible sub-commands most-useful first.
# Prefix matching leaves no trace in the listings, so both help surfaces say it out
# loud (DESIGN_cli_noargs.md §d).
PREFIX_HINT = "Any prefix that matches one command is that command: 'wo li' = 'workout list'."

COMMAND_ORDER = {
    "": ["status", "workout", "progress", "plan", "goal",
         "constraint", "benchmark", "signal", "learnings", "data", "settings",
         "shell", "help"],
    "settings": ["list", "set", "reset"],
    "goal": ["list", "add", "edit", "rm"],
    "constraint": ["list", "show", "add", "edit", "rm"],
    "benchmark": ["list", "record", "rm"],
    "signal": ["list", "list-metrics", "add", "rm"],
    "learnings": ["list", "show", "edit", "demote", "keep", "rm"],
    "plan": ["show", "generate", "feedback", "versions", "diff", "rollback"],
    "workout": ["list", "adapt", "compare", "generate", "swap", "add",
                "restore", "rm", "rollback", "batches"],
    "data": ["pull", "reflect", "show-metrics", "show-activities"],
}

from trainmate.cli.common import ensure_recent_data
from trainmate.cli.status import run_status
from trainmate.cli.progress import run_progress
from trainmate.cli.goals import (
    run_goal_add, run_goal_edit, run_goal_list, run_goal_rm, run_goal_wipe,
)
from trainmate.cli.constraints import (
    run_constraint_add, run_constraint_edit, run_constraint_list,
    run_constraint_show, run_constraint_rm, run_constraint_wipe,
)
from trainmate.cli.benchmarks import (
    run_benchmark_record, run_benchmark_list, run_benchmark_rm, run_benchmark_wipe,
)
from trainmate.cli.learnings import (
    run_learning_list, run_learning_show, run_learning_edit, run_learning_rm,
    run_learning_demote, run_learning_keep, run_learning_wipe,
)
from trainmate.cli.plans import (
    run_plan_generate, run_plan_show, run_plan_rm, run_plan_feedback, run_plan_wipe,
    run_plan_rollback, run_plan_versions, run_plan_diff,
)
from trainmate.cli.workouts import (
    run_workout_list, run_workout_compare, run_workout_generate, run_workout_rm,
    run_workout_restore, run_workout_adapt, run_workout_push, run_workout_swap,
    run_workout_add, run_workout_wipe, run_workout_batches, run_workout_rollback,
    run_workout_prune_calendar,
)
from trainmate.cli.data import (
    run_data_pull, run_data_bootstrap, run_data_reflect, run_data_backfill_tss,
    run_data_show_metrics, run_data_show_activities, run_data_wipe,
)
from trainmate.cli.signals import (
    run_signal_add, run_signal_rm, run_signal_list, run_signal_list_metrics,
)
from trainmate.cli.status import add_status_parser
from trainmate.cli.progress import add_progress_parser
from trainmate.cli.goals import add_goal_parser
from trainmate.cli.constraints import add_constraint_parser
from trainmate.cli.benchmarks import add_benchmark_parser
from trainmate.cli.signals import add_signal_parser
from trainmate.cli.learnings import add_learnings_parser
from trainmate.cli.plans import add_plan_parser
from trainmate.cli.workouts import add_workout_parser
from trainmate.cli.data import add_data_parser
from trainmate.cli.settings import add_settings_parser
from trainmate.cli.bot import (
    add_bot_parser, run_bot_constraints, run_bot_morning, run_bot_route,
)


def build_parser():
    """Construct the argparse tree and return ``(parser, named_subparsers)``.

    ``named_subparsers`` maps a top-level command to its sub-parser so the
    dispatcher can print per-group help. Split out from ``main`` so the REPL can
    build the tree once and reuse it across many lines of input.
    """
    parser = WrapAwareArgumentParser(
        # Not sys.argv[0]: nobody types 'trainmate_cli.py', and every wrapped usage line
        # is indented under it (DESIGN_cli_noargs.md §e).
        prog="tm",
        description="TrainMate - Local Training Coach CLI",
        epilog=PREFIX_HINT,
    )
    parser.add_argument(
        "--llm-model", dest="llm_model",
        help="Override the OpenRouter model identifier"
    )
    parser.add_argument(
        "--helpall", action=_HelpAllAction,
        help="Show every command including hidden maintenance ones (same as 'help --all')"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Re-raise on failure instead of printing a one-line error"
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
        "shell",
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

    # Date/mesocycle/macrocycle/goal filtering is no longer a shared parent parser: each
    # command calls trainmate.cli.selectors.add_selector_args with its own default window
    # and direction (DESIGN_cli_selectors.md §3).
    add_status_parser(subparsers, pull_bypass_parser)
    add_progress_parser(subparsers, pull_bypass_parser)
    goal_parser = add_goal_parser(subparsers)
    constraint_parser = add_constraint_parser(subparsers)
    benchmark_parser = add_benchmark_parser(subparsers)
    signal_parser = add_signal_parser(subparsers)
    learnings_parser = add_learnings_parser(subparsers)
    plan_parser = add_plan_parser(subparsers, pull_bypass_parser, llm_debug_parser)
    workout_parser = add_workout_parser(subparsers, pull_bypass_parser, llm_debug_parser)
    data_parser = add_data_parser(subparsers, pull_bypass_parser, llm_debug_parser)
    add_settings_parser(subparsers)
    add_bot_parser(subparsers)

    named_subparsers = {
        "goal": goal_parser,
        "constraint": constraint_parser,
        "benchmark": benchmark_parser,
        "signal": signal_parser,
        "learnings": learnings_parser,
        "plan": plan_parser,
        "workout": workout_parser,
        "data": data_parser,
    }
    sort_command_tree(parser, COMMAND_ORDER)
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

    # Aliases and prefixes are resolved to canonical names before argparse sees them
    # (DESIGN_cli_noargs.md §d), so `cmd` is always canonical.
    cmd = args.command.lower()

    # Two commands need the parser tree itself rather than the database, so they are
    # answered here instead of through a handler.
    if cmd == "help":
        print(bold(parser.description))
        print()
        _print_command_tree(parser, include_advanced=getattr(args, "show_all", False))
        aside(PREFIX_HINT)
        return
    if cmd == "shell":
        _repl(parser, named_subparsers)
        return

    # Every other command carries its handler, bound with set_defaults() next to the
    # sub-parser that defines its flags. The 200-line elif ladder this replaces had to
    # be edited in step with the parser definitions, and a branch that fell through
    # simply did nothing.
    handler = getattr(args, "func", None)
    if handler is None:
        # A command group invoked bare (`tm goal`): show what it offers.
        group = named_subparsers.get(cmd)
        (group or parser).print_help()
        sys.exit(1)

    handler(args)


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
    """Entry point. With no arguments, print help; use `shell` for the REPL.

    The single error boundary for a command run. Handlers used to wrap themselves in
    `except Exception` and print the message, which discarded the traceback, returned
    exit code 0 for a failed command, and hid real bugs behind a one-line summary — the
    `next_goal` NameError read as "Error during plan generation: name 'next_goal' is
    not defined" for as long as it existed. Failures now reach here, print in red, and
    exit non-zero; `--debug` re-raises so the traceback survives.
    """
    if argv is None:
        argv = sys.argv[1:]
    parser, named_subparsers = build_parser()
    debug = "--debug" in argv
    try:
        run_once(argv, parser, named_subparsers)
    except SystemExit:
        raise
    except PromptCancelled:
        # A deliberate abort (front-end /cancel or idle timeout), not a failure.
        print("Cancelled.")
        sys.exit(130)
    except Exception as e:
        if debug:
            raise
        print(red(f"Error: {e}"))
        aside("Re-run with --debug for the full traceback.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except PromptCancelled:
        # An interactive prompt was cancelled (front-end /cancel or idle timeout);
        # abort the command without the traceback an uncaught BaseException prints.
        print("Cancelled.")
