"""Deprecated `lifeevent` command — a thin forwarder to `constraint` (DESIGN_constraints.md
§9). A life event was, by definition, plan-shaping, so an `add` forwards to
`constraint add … --replan`; the other verbs map straight onto their constraint
equivalents. Retained for one release with a deprecation notice, then removed.
"""
import argparse
import sys
import trainmate_cli as cli
from trainmate.util import yellow
from trainmate.cli.constraints import (
    run_constraint_add, run_constraint_edit, run_constraint_list,
    run_constraint_show, run_constraint_rm, run_constraint_wipe,
)

_SUB_ALIASES = {
    "a": "add", "e": "edit", "l": "list", "s": "show", "r": "rm",
}


def run_lifeevent_forward(args: argparse.Namespace) -> None:
    """Maps a parsed `lifeevent` invocation onto the constraint command."""
    print(yellow(
        "Note: 'lifeevent' is deprecated. Use 'constraint' — a life event is just a "
        "plan-shaping constraint ('constraint add … --replan')."
    ))
    sub = _SUB_ALIASES.get(args.subcommand, args.subcommand)

    if sub == "add":
        # A life event was never code-enforced (§9) — it was advisory prompt context,
        # exactly like a `soft` constraint today. `soft` + `--replan` is the faithful
        # continuation, matching the migration's bindingness choice.
        run_constraint_add(argparse.Namespace(
            title=args.title, title_opt=None, start=args.start, end=args.end,
            sport=None, type=args.type, desc=args.desc, binding="soft", replan=True,
        ))
    elif sub == "edit":
        run_constraint_edit(argparse.Namespace(
            id=args.id, title=args.title, start=args.start, end=args.end,
            sport=None, type=args.type, desc=args.desc, binding=None, replan=None,
        ))
    elif sub == "list":
        run_constraint_list(argparse.Namespace(
            verbose=args.verbose, all=getattr(args, "all", False),
            sport=getattr(args, "sport", None), type=getattr(args, "type", None),
            from_date=None, until_date=None,
        ))
    elif sub == "show":
        run_constraint_show(argparse.Namespace(id=args.id))
    elif sub == "rm":
        run_constraint_rm(argparse.Namespace(id=args.id))
    elif sub == "wipe":
        run_constraint_wipe(argparse.Namespace(yes=args.yes))
    else:
        print(yellow("Unknown lifeevent subcommand."))
        sys.exit(1)
