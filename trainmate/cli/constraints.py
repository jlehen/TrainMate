"""Handlers for the `constraint` command — the single directive object
(DESIGN_constraints.md). A constraint is anything the athlete asks the coach to work
around, at any horizon: hard availability, capacity/intensity caps, soft preferences,
or big disruptions. It replaces `lifeevent` and gives `workout adapt --message` a typed
home to land in.

Plan-invalidation is never an authoring-time category: `add`/`edit` derive a directive's
magnitude against the active plan and, when it crosses a conservative threshold, *propose*
a human-confirmed replan (§7). Nothing here regenerates a plan without a `y`.
"""
import argparse
import sys
from typing import Optional
from trainmate import runtime
from trainmate.util import (
    aside, bold, dim, green, red, yellow, cyan, gray, cmd, format_labeled_block,
    wrap_text, today_str as _today_str,
)
from trainmate.cli.selectors import add_selector_args, has_selector, resolve_window
from trainmate.cli.common import constraint_line
from trainmate.coach import honoring


def _needs_a_pass(c: dict) -> bool:
    """`coach/honoring.py`'s tier question, at the four places this file renders it."""
    return honoring.needs_a_pass(runtime.db, c, _today_str())


def _resolve_dates(args: argparse.Namespace) -> tuple:
    """--start defaults to today; --end defaults to --start (single day)."""
    start = args.start or _today_str()
    end = args.end or start
    if end < start:
        print(red("--end is before --start."))
        sys.exit(1)
    return start, end


def _maybe_point_at_honor(constraint_id: int) -> None:
    """Names the block a constraint lands in when it is past daily adapt's reach, and
    says what would build it into the plan now (DESIGN_constraint_honoring.md §4).

    Its own function called AFTER `_maybe_replan`, not a branch appended to it: that
    function returns early on a magnitude below the threshold, which is exactly the case
    this message exists for, so anything at its foot would be dead code here.
    """
    constraint = runtime.db.get_constraint(constraint_id)
    if not constraint:
        return
    # The one owner of "does the plan not reflect this yet?" — plan-shaping tier, nothing
    # scheduled in the window, and the rest of §2's terms, decided in one place.
    if not honoring.needs_a_pass(runtime.db, constraint, _today_str()):
        return
    active_meso = runtime.db.get_active_mesocycle(_today_str())
    if not active_meso or constraint['end_date'] <= active_meso['end_date']:
        return

    # The days out of reach are the ones past the current block, so it is the block
    # holding the constraint's END that names them. Asked of its START instead, a
    # constraint straddling the boundary names the current block — the one adapt reaches
    # today. The strict reader: a block that does not contain the date is not the answer.
    landing = runtime.db.get_covering_mesocycle(constraint['end_date'])
    if not landing:
        _report_past_plan_end(constraint)
        return

    # Generation runs from today to the end of the block it is given, so the block
    # holding the last day is the one to name — it covers every block before it too,
    # which is what makes one run enough for a straddling constraint.
    build = cmd(f"workout generate -m {landing['id']}")

    # Straddling the boundary: adapt honors the near days from this block and the rest
    # only once its window rolls on, so no single run ever sees the whole of it.
    if constraint['start_date'] <= active_meso['end_date']:
        print(yellow(wrap_text(
            f"Straddles the end of {active_meso['name']} ({active_meso['end_date']}): "
            f"daily adapt honors the days up to there, {landing['name']} holds the rest, "
            "and no one run sees both."
        )))
        print(yellow(wrap_text(
            f"Build the whole of it in with {build} — that rebuilds the plan from today "
            f"through {landing['end_date']}."
        )))
        return

    print(yellow(wrap_text(
        f"Lands in {landing['name']} ({landing['start_date']} — {landing['end_date']}), "
        "outside daily adapt's reach."
    )))
    # Adapt at date D reaches from D to the end of D's block, so it sees this constraint
    # once its window rolls onto the landing block — i.e. on that block's first day.
    print(yellow(wrap_text(
        f"Leave it — adapt reaches it on {landing['start_date']} — or build it in now "
        f"with {build}, which rebuilds the plan from today through that block's end."
    )))


def _report_past_plan_end(constraint: dict) -> None:
    """No block holds the constraint's last day. Whether anything can be done now turns
    on its FIRST day: a plan covering part of the window can still be built around it,
    one covering none of it cannot."""
    if runtime.db.get_covering_mesocycle(constraint['start_date']):
        print(yellow(wrap_text(
            f"Runs to {constraint['end_date']}, past the end of your plan. "
            + cmd("workout generate") + " builds the days your plan covers around it; "
            "run " + cmd("plan generate")
            + " to extend the periodization over the rest."
        )))
        return
    print(yellow(wrap_text(
        f"Starts {constraint['start_date']}, past the end of the plan — no block "
        "governs it yet, so nothing can schedule around it."
    )))
    print(yellow("Extend the periodization with " + cmd("plan generate") + " first."))


def _maybe_replan(constraint_id: int, title: str, replan_flag: Optional[bool]) -> None:
    """Handles the §7 plan-invalidation decision after an add/edit.

    `replan_flag` pre-answers the proposal: True (--replan) escalates and enters the
    regen flow; False (--no-replan) declines. When it is None, the directive's magnitude
    against the active plan decides whether to *ask*; only a human `y` sets replan = 1 and
    regenerates."""
    if replan_flag is False:
        runtime.db.update_constraint(constraint_id, replan=0)
        return

    if replan_flag is True:
        runtime.db.update_constraint(constraint_id, replan=1)
        _run_replan_flow(title)
        return

    # Undecided: derive magnitude and, if plan-shaping, propose.
    constraint = runtime.db.get_constraint(constraint_id)
    if not constraint:
        return
    try:
        impact = runtime.coach_service.constraint_plan_impact(constraint)
    except Exception:
        return
    if not runtime.coach_service.constraint_is_plan_shaping(constraint, impact):
        return

    detail = f"displaces ~{impact['displaced_pct']:.0f}% of a typical week's planned load"
    print(yellow(f"This {impact['days']}-day constraint {detail}."))
    if runtime.prompt.confirm("Replan around it?"):
        runtime.db.update_constraint(constraint_id, replan=1)
        _run_replan_flow(title)
    else:
        runtime.db.update_constraint(constraint_id, replan=0)
        print(dim("Left out of the plan; still honored by daily 'workout adapt'."))


def _run_replan_flow(title: str) -> None:
    """Escalates a directive to plan-shaping and runs the existing plan-generate confirm
    flow (each step of which still confirms before applying). Imported lazily to avoid a
    CLI import cycle."""
    from trainmate.cli.plans import run_plan_generate
    print(green(f"Marked '{title}' as plan-shaping. Regenerating the plan around it..."))
    ns = argparse.Namespace(
        no_pull=False, force_pull=False, auto=False, goal_id=None, force=False, fresh=False
    )
    run_plan_generate(ns)
    aside("If you applied the new plan, run " + cmd("workout generate")
          + " to schedule it.")


def run_constraint_add(args: argparse.Namespace) -> None:
    """Authors a directive over a day or range (DESIGN_constraints.md §4)."""
    title = args.title
    start, end = _resolve_dates(args)

    cid = runtime.db.add_constraint(
        title=title, start_date=start, end_date=end,
        rest=1 if args.rest else 0,
        description=args.desc, replan=1 if args.replan is True else 0,
        source='manual',
    )
    constraint = runtime.db.get_constraint(cid)
    if constraint:
        print(constraint_line(constraint, _needs_a_pass(constraint)))
    print(green("Constraint added successfully."))
    _maybe_replan(cid, title, args.replan)
    _maybe_point_at_honor(cid)


def run_constraint_edit(args: argparse.Namespace) -> None:
    """Adjusts scope / rest / text / replan of an existing directive."""
    constraint = runtime.db.get_constraint(args.id)
    if not constraint:
        print(red(f"Constraint with ID {args.id} not found."))
        sys.exit(1)

    kwargs = {}
    if args.title is not None:
        kwargs['title'] = args.title
    if args.start is not None:
        kwargs['start_date'] = args.start
    if args.end is not None:
        kwargs['end_date'] = args.end
    if args.rest is not None:
        kwargs['rest'] = 1 if args.rest else 0
    if args.desc is not None:
        kwargs['description'] = args.desc or None

    if not kwargs and args.replan is None:
        print(yellow("No fields to update. Provide at least one field to change."))
        return

    # An explicit --replan/--no-replan lands in the same write, so the echo below shows
    # the final state; _maybe_replan still owns the regen flow and the undecided case.
    if args.replan is not None:
        kwargs['replan'] = 1 if args.replan else 0

    runtime.db.update_constraint(args.id, **kwargs)
    # The window moved, or the directive itself changed — and for an advisory constraint
    # the prose IS the enforcement mechanism (DESIGN_constraints.md §5), so new words are a
    # new directive a previous honoring says nothing about (§8). `replan` alone does not
    # clear: it escalates the tier, it does not restate what to work around.
    if any(k in kwargs for k in ('start_date', 'end_date', 'rest', 'title', 'description')):
        runtime.db.clear_honored(args.id)

    updated = runtime.db.get_constraint(args.id)
    if updated:
        print(constraint_line(updated, _needs_a_pass(updated)))
    print(green("Constraint updated successfully."))

    title = kwargs.get('title', constraint['title'])
    _maybe_replan(args.id, title, args.replan)
    _maybe_point_at_honor(args.id)


def run_constraint_list(args: argparse.Namespace) -> None:
    """Lists directives from the start of the current mesocycle onward — the training block
    being planned — plus everything upcoming (open-ended). --all drops the lower bound and
    shows every directive, past included; a selector (-d/-m/-M/-g) sets the window
    explicitly (§4). With no active mesocycle to anchor on (no plan yet), every constraint
    is shown."""
    if getattr(args, 'all', False):
        start = end = None
    elif has_selector(args):
        start, end = resolve_window(args)
    else:
        # Anchor on the current training block; with no plan yet, there is nothing to
        # anchor on, so show everything (a fresh user has only a handful of constraints).
        active_meso = runtime.db.get_active_mesocycle(_today_str())
        start = active_meso['start_date'] if active_meso else None
        end = None
    constraints = runtime.db.get_constraints(start, end)

    print(bold(cyan("=== ATHLETE CONSTRAINTS ===")))
    if not constraints:
        print(dim("(none)"))
        return
    for c in constraints:
        print(constraint_line(c, _needs_a_pass(c)))
        if args.verbose and c.get('description'):
            print(format_labeled_block("  Details:", c['description']))


def run_constraint_show(args: argparse.Namespace) -> None:
    """Displays a directive in detail, including whether it is plan-shaping (§7)."""
    constraint = runtime.db.get_constraint(args.id)
    if not constraint:
        print(red(f"Constraint with ID {args.id} not found."))
        return
    needs_a_pass = _needs_a_pass(constraint)
    print(constraint_line(constraint, needs_a_pass))
    if constraint.get('description'):
        print(format_labeled_block("  Details:", constraint['description']))
    src = constraint.get('source')
    if src:
        print(gray(f"  Source: {src}"))
    if constraint.get('replan'):
        print(green("  Plan-shaping: built into the plan (replan)."))
    else:
        print(dim("  Plan-shaping: no — honored by daily 'workout adapt' only."))
    # What `honored_at` means, stated the way §8 states it: a coach pass had this in scope
    # with authority over every day of it still ahead. Not "the plan definitely changed".
    # Labelled "Coach pass", not "Honored": the line above already uses that word for the
    # plan-shaping tier, and this axis is a weaker claim than either — a pass had it in
    # scope, which is not the same as the plan having changed.
    if constraint.get('honored_at'):
        print(gray(f"  Coach pass: covered it on "
                   f"{str(constraint['honored_at'])[:10]}."))
    elif needs_a_pass:
        print(yellow("  Coach pass: none yet — run "
                     + cmd("workout generate") + " to build it into the plan."))


def run_constraint_rm(args: argparse.Namespace) -> None:
    """Removes a directive by ID."""
    if not runtime.db.get_constraint(args.id):
        print(yellow(f"No constraint with ID {args.id}."))
        return
    runtime.db.delete_constraint(args.id)
    print(green(f"Constraint [{args.id}] removed."))


def run_constraint_wipe(args: argparse.Namespace) -> None:
    """Wipes all constraints after confirmation."""
    if not args.yes:
        if not runtime.prompt.confirm(
            "Are you sure you want to wipe all constraints?", danger=True
        ):
            print("Wipe cancelled.")
            return
    runtime.db.wipe_constraints()
    print(green("All constraints wiped successfully."))


def add_constraint_parser(subparsers):
    # constraint command & subparsers — the single directive object
    # (DESIGN_constraints.md). Everything the athlete asks the coach to work around, at
    # any horizon; supersedes `lifeevent`.
    constraint_parser = subparsers.add_parser(
        "constraint",
        help="Author/list directives the coach works around (availability, caps, "
             "preferences, disruptions)",
        description=(
            "Manage constraints — anything you ask the coach to work around, at any "
            "horizon ('no run Thursday', 'only 45 min today', 'easy ride with a friend "
            "Saturday', 'knee flare, no running ~2 weeks'). A constraint is advisory prose "
            "the coach honors by judgement — say what you mean in the title and the LLM "
            "works around it. The one exception is --rest, which deterministically forces a "
            "full no-training window (surgery, no-gym travel), placing an explicit rest day "
            "without asking the LLM. Whether a constraint reshapes the plan is derived from "
            "its magnitude and human-confirmed, not picked up front."
        ),
    )
    constraint_subparsers = constraint_parser.add_subparsers(
        dest="subcommand", help="Constraint sub-commands"
    )

    def _add_rest_flag(p, edit=False):
        # --rest is the single deterministic edge: a full no-training window. On `edit`,
        # the pair lets you toggle a constraint back to advisory (--no-rest); on `add`,
        # absence just means advisory (the default), so only --rest is offered.
        if edit:
            grp = p.add_mutually_exclusive_group()
            grp.add_argument("--rest", dest="rest", action="store_const", const=True,
                             help="Force a full no-training window (deterministic rest)")
            grp.add_argument("--no-rest", dest="rest", action="store_const", const=False,
                             help="Make it advisory instead (clear the rest flag)")
            p.set_defaults(rest=None)
        else:
            p.add_argument("--rest", action="store_true",
                           help="Force a full no-training window (deterministic rest, no "
                                "LLM); omit for an advisory constraint the coach works around")

    def _add_replan_flags(p):
        grp = p.add_mutually_exclusive_group()
        grp.add_argument("--replan", dest="replan", action="store_const", const=True,
                         help="Escalate to plan-shaping and regenerate around it")
        grp.add_argument("--no-replan", dest="replan", action="store_const", const=False,
                         help="Keep out of the plan (honored by daily adapt only)")
        p.set_defaults(replan=None)

    # constraint add
    cons_add = constraint_subparsers.add_parser(
        "add", help="Author a directive over a day or range"
    )
    cons_add.set_defaults(func=run_constraint_add)
    cons_add.add_argument("title", help="The directive, stated short "
                          "(e.g. 'no run Thursday')")
    cons_add.add_argument("--start", help="Start date (YYYY-MM-DD; default: today)")
    cons_add.add_argument("--end", help="End date (YYYY-MM-DD; default: --start)")
    cons_add.add_argument("--desc", "--description", dest="desc",
                          help="Optional richer context for the coach")
    _add_rest_flag(cons_add)
    _add_replan_flags(cons_add)

    # constraint edit
    cons_edit = constraint_subparsers.add_parser(
        "edit", help="Adjust scope / rest / text / replan"
    )
    cons_edit.set_defaults(func=run_constraint_edit)
    cons_edit.add_argument("id", type=int, help="Constraint ID to edit")
    cons_edit.add_argument("--title", help="New directive title")
    cons_edit.add_argument("--start", help="New start date (YYYY-MM-DD)")
    cons_edit.add_argument("--end", help="New end date (YYYY-MM-DD)")
    cons_edit.add_argument("--desc", "--description", dest="desc",
                           help="New richer context ('' to clear)")
    _add_rest_flag(cons_edit, edit=True)
    _add_replan_flags(cons_edit)

    # constraint list
    cons_list = constraint_subparsers.add_parser(
        "list",
        help="List directives from the current mesocycle onward",
        description=(
            "List directives the coach works around. By default, shows everything from the "
            "start of the current mesocycle (the training block being planned) onward, plus "
            "all upcoming directives. With no active mesocycle to anchor on (no plan yet), "
            "shows every constraint. Use --all to include past directives too, or "
            "-d/-m/-M/-g to set the window explicitly."
        ),
    )
    cons_list.set_defaults(func=run_constraint_list)
    cons_list.add_argument("-v", "--verbose", action="store_true",
                           help="Show details for each directive")
    cons_list.add_argument("-a", "--all", action="store_true",
                           help="Show every directive, past ones included (drop the "
                                "mesocycle lower bound)")
    # No default window here: with no selector at all the handler anchors on the current
    # block, which is not a date the parser could name (DESIGN_constraints.md §4).
    add_selector_args(cons_list, meso=True, macro=True, goal=True, direction="forward")

    # constraint show
    cons_show = constraint_subparsers.add_parser(
        "show", help="Show one directive in detail (incl. plan-shaping)"
    )
    cons_show.set_defaults(func=run_constraint_show)
    cons_show.add_argument("id", type=int, help="Constraint ID to display")

    # constraint rm
    cons_rm = constraint_subparsers.add_parser(
        "rm", help="Remove a directive by ID"
    )
    cons_rm.set_defaults(func=run_constraint_rm)
    cons_rm.add_argument("id", type=int, help="Constraint ID to remove")

    # constraint wipe
    cons_wipe = constraint_subparsers.add_parser(
        "wipe", advanced=True, help="Wipe all constraints")
    cons_wipe.set_defaults(func=run_constraint_wipe)
    cons_wipe.add_argument("-y", "--yes", action="store_true",
                           help="Skip confirmation prompt")

    return constraint_parser
