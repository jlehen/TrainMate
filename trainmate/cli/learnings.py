import argparse
import sys

import trainmate_cli as cli
from trainmate.util import (
    bold, green, red, yellow, cyan, magenta, gray,
    format_labeled_block,
)


def _confidence_color(conf: str):
    """Maps a confidence level to its display color function."""
    if conf == 'established':
        return green
    if conf == 'moderate':
        return yellow
    return gray


def _print_learning(l: dict) -> None:
    """Renders a single learning as a coloured [id|sports|confidence] tag + wrapped text.

    Shared by `learnings list` and `learnings show`; mirrors the legacy status rendering
    so the move out of `status` doesn't change how an individual learning reads."""
    sports_str = l.get('sports') or 'general'
    conf_str = l.get('confidence') or 'tentative'

    if l.get("dormant"):
        # Decayed: kept on record but no longer fed to the coach until reaffirmed.
        tag = f"  [{l['id']}|{sports_str}|{conf_str}]"
        print(format_labeled_block(gray(tag), gray(f"{l['text']} (dormant)")))
    else:
        conf_disp = _confidence_color(conf_str)(conf_str)
        tag = f"  [{cyan(str(l['id']))}|{magenta(sports_str)}|{conf_disp}]"
        print(format_labeled_block(tag, l['text']))

    # Pending, human-confirmable confidence downgrade (resolve with 'learnings demote'/'keep'
    # here, or interactively on the next 'data reflect'/'data bootstrap' run).
    proposed = l.get("proposed_confidence")
    if proposed:
        target = "retire" if proposed == "retire" else proposed
        print(yellow(f"     ⚠ proposed demotion → {target} "
                     "(confirm with 'learnings demote'/'keep')"))


def run_learning_list(args: argparse.Namespace) -> None:
    """Lists coach learnings, optionally filtered by sport, confidence, or dormancy."""
    learnings = cli.db.get_learnings()

    if getattr(args, "dormant", False):
        learnings = [l for l in learnings if l.get("dormant")]
    if getattr(args, "sport", None):
        needle = args.sport.lower()
        learnings = [l for l in learnings if needle in (l.get("sports") or "general").lower()]
    if getattr(args, "confidence", None):
        learnings = [l for l in learnings if (l.get("confidence") or "tentative") == args.confidence]

    print(bold(cyan("=== COACH LEARNINGS ===")))
    if not learnings:
        print(gray("No learnings match." if (
            getattr(args, "dormant", False) or getattr(args, "sport", None)
            or getattr(args, "confidence", None)
        ) else "None yet."))
        if not cli.db.get_learnings():
            print(
                yellow("Run ") + green("'data bootstrap'")
                + yellow(" to reconstruct your training history and seed observations.")
            )
        return

    for l in learnings:
        _print_learning(l)


def run_learning_show(args: argparse.Namespace) -> None:
    """Displays one learning with its full evidence basis (the 'why' behind its confidence)."""
    learning = next((l for l in cli.db.get_learnings() if l['id'] == args.id), None)
    if not learning:
        print(red(f"Learning with ID {args.id} not found."))
        sys.exit(1)

    _print_learning(learning)
    print(f"  created   : {learning.get('created_at') or '-'}")
    print(f"  updated   : {learning.get('updated_at') or '-'}")
    print(f"  reinforced: {learning.get('last_reinforced_at') or '-'}")

    evidence = cli.db.get_learning_evidence(args.id)
    sup = [e for e in evidence if e['polarity'] >= 0]
    con = [e for e in evidence if e['polarity'] < 0]
    print(bold("\n  Evidence basis:"))
    if not evidence:
        print(gray("    (none — confidence will decay without supporting weeks)"))
        return

    def _weeks(rows):
        return ", ".join(f"{r['week_commencing']}({r['source']})" for r in rows)

    print(green(f"    supporting ({len(sup)} wk): ") + (_weeks(sup) or "-"))
    if con:
        print(red(f"    contradicting ({len(con)} wk): ") + _weeks(con))


def run_learning_edit(args: argparse.Namespace) -> None:
    """Revises the text of an existing learning."""
    learning = next((l for l in cli.db.get_learnings() if l['id'] == args.id), None)
    if not learning:
        print(red(f"Learning with ID {args.id} not found."))
        sys.exit(1)

    cli.db.update_learning(args.id, args.text)
    print(green(f"Learning with ID {args.id} updated successfully."))


def run_learning_rm(args: argparse.Namespace) -> None:
    """Deletes a learning by ID (its evidence basis cascades)."""
    learning = next((l for l in cli.db.get_learnings() if l['id'] == args.id), None)
    if not learning:
        print(red(f"Learning with ID {args.id} not found."))
        sys.exit(1)

    cli.db.delete_learning(args.id)
    print(green(f"Learning with ID {args.id} removed successfully."))


def run_learning_demote(args: argparse.Namespace) -> None:
    """Accepts a pending confidence downgrade for a learning."""
    result = cli.db.demote_learning(args.id)
    if result is None:
        print(yellow(f"Learning with ID {args.id} has no pending demotion."))
        return
    if result == "retired":
        print(green(f"Learning with ID {args.id} retired."))
    else:
        print(green(f"Learning with ID {args.id} demoted to '{result}'."))


def run_learning_keep(args: argparse.Namespace) -> None:
    """Dismisses + affirms a pending downgrade (the affirmation counts as reinforcement)."""
    learning = next((l for l in cli.db.get_learnings() if l['id'] == args.id), None)
    if not learning:
        print(red(f"Learning with ID {args.id} not found."))
        sys.exit(1)
    if not learning.get("proposed_confidence"):
        print(yellow(f"Learning with ID {args.id} has no pending demotion to dismiss."))
        return

    cli.db.keep_learning(args.id)
    print(green(f"Learning with ID {args.id} kept; pending demotion dismissed."))


def run_learning_wipe(args: argparse.Namespace) -> None:
    """Wipes all coach learnings from the database after confirmation."""
    if not args.yes:
        try:
            confirm = input(
                "Are you sure you want to wipe all coach learnings? [y/N]: "
            ).strip().lower()
        except EOFError:
            confirm = 'n'
        if confirm not in ('y', 'yes'):
            print("Wipe cancelled.")
            return

    cli.db.wipe_learnings()
    print(green("All coach learnings wiped successfully."))
