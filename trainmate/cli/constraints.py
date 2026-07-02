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
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, magenta, gray, format_labeled_block,
    today_str as _today_str,
)
from trainmate.cli.common import fmt_date


def _prompt_title(existing: Optional[str]) -> Optional[str]:
    """Returns the directive one-liner, prompting when it was omitted on the CLI."""
    if existing:
        return existing
    title = cli.prompt.ask_text(
        "Constraint (the directive, stated short — e.g. 'no run Thursday')"
    ).strip()
    return title or None


def _prompt_type(existing: Optional[str]) -> Optional[str]:
    """Returns the opaque `type` label. When omitted, offers the labels already in use
    (same distinct-values pattern as `context list-metrics`) to discourage vocabulary
    drift, but never requires one."""
    if existing is not None:
        return existing or None
    in_use = [t['type'] for t in cli.db.list_constraint_types()]
    hint = f" [in use: {', '.join(in_use)}]" if in_use else ""
    label = cli.prompt.ask_text(
        f"Type (optional opaque label, e.g. trip, injury{hint})", default=""
    ).strip()
    return label or None


def _resolve_dates(args: argparse.Namespace) -> tuple:
    """--start defaults to today; --end defaults to --start (single day)."""
    start = args.start or _today_str()
    end = args.end or start
    if end < start:
        print(red("--end is before --start."))
        sys.exit(1)
    return start, end


def _maybe_replan(constraint_id: int, title: str, replan_flag: Optional[bool]) -> None:
    """Handles the §7 plan-invalidation decision after an add/edit.

    `replan_flag` pre-answers the proposal: True (--replan) escalates and enters the
    regen flow; False (--no-replan) declines. When it is None, the directive's magnitude
    against the active plan decides whether to *ask*; only a human `y` sets replan = 1 and
    regenerates."""
    if replan_flag is False:
        cli.db.update_constraint(constraint_id, replan=0)
        return

    if replan_flag is True:
        cli.db.update_constraint(constraint_id, replan=1)
        _run_replan_flow(title)
        return

    # Undecided: derive magnitude and, if plan-shaping, propose.
    constraint = cli.db.get_constraint(constraint_id)
    if not constraint:
        return
    try:
        impact = cli.coach_service.constraint_plan_impact(constraint)
    except Exception:
        return
    if not cli.coach_service.constraint_is_plan_shaping(constraint, impact):
        return

    detail = f"displaces ~{impact['displaced_pct']:.0f}% of a typical week's planned load"
    print(yellow(f"This {impact['days']}-day constraint {detail}."))
    if cli.prompt.confirm("Replan around it?"):
        cli.db.update_constraint(constraint_id, replan=1)
        _run_replan_flow(title)
    else:
        cli.db.update_constraint(constraint_id, replan=0)
        print(dim("Left out of the plan; still honored by daily 'workout adapt'."))


def _run_replan_flow(title: str) -> None:
    """Escalates a directive to plan-shaping and runs the existing plan-generate confirm
    flow (each step of which still confirms before applying). Imported lazily to avoid a
    CLI import cycle."""
    from trainmate.cli.plans import run_plan_generate
    print(green(f"Marked '{title}' as plan-shaping. Regenerating the plan around it..."))
    ns = argparse.Namespace(
        no_pull=False, force_pull=False, auto=False, goal_id=None, force=False
    )
    run_plan_generate(ns)
    print(dim("If you applied the new plan, run 'workout generate' to schedule it."))


def run_constraint_add(args: argparse.Namespace) -> None:
    """Authors a directive over a day or range (DESIGN_constraints.md §4)."""
    # Quick capture stays flag- and prompt-free: `cons a "no run Thursday"`. Only the
    # guided path (bare `cons a`, where the title itself had to be prompted) walks the
    # optional type prompt.
    title_provided = bool(args.title)
    title = _prompt_title(args.title)
    if not title:
        print(red("A constraint needs a title (the directive itself)."))
        sys.exit(1)
    ctype = args.type if args.type is not None else (
        _prompt_type(None) if not title_provided else None
    )
    start, end = _resolve_dates(args)

    cid = cli.db.add_constraint(
        title=title, start_date=start, end_date=end,
        binding=args.binding, sport=args.sport, type=ctype,
        description=args.desc, replan=1 if args.replan is True else 0,
        source='manual',
    )
    span = start if start == end else f"{start}..{end}"
    print(green(
        f"Added constraint [{cid}]: {bold(title)} ({args.binding}) over {cyan(span)}"
        + (f" [{args.sport}]" if args.sport else "")
    ))
    _maybe_replan(cid, title, args.replan)


def run_constraint_edit(args: argparse.Namespace) -> None:
    """Adjusts scope / bindingness / text / replan of an existing directive."""
    constraint = cli.db.get_constraint(args.id)
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
    if args.binding is not None:
        kwargs['binding'] = args.binding
    if args.sport is not None:
        kwargs['sport'] = args.sport or None
    if args.type is not None:
        kwargs['type'] = args.type or None
    if args.desc is not None:
        kwargs['description'] = args.desc or None

    if not kwargs and args.replan is None:
        print(yellow("No fields to update. Provide at least one field to change."))
        return

    if kwargs:
        cli.db.update_constraint(args.id, **kwargs)
        print(green(f"Constraint [{args.id}] updated."))

    title = kwargs.get('title', constraint['title'])
    _maybe_replan(args.id, title, args.replan)


def _constraint_line(c: dict) -> str:
    """One-line rendering of a constraint for `list`."""
    ctype = c.get('type')
    type_str = f" ({magenta(ctype)})" if ctype else ""
    sport_str = f" [{c['sport']}]" if c.get('sport') else ""
    tags = c.get('binding', 'soft') + sport_str + (
        " · plan-shaping" if c.get('replan') else ""
    )
    return (
        f"ID: {c['id']} | {yellow(c['title'])}{type_str}: "
        f"{cyan(c['start_date'])} to {cyan(c['end_date'])} | {tags}"
    )


def run_constraint_list(args: argparse.Namespace) -> None:
    """Lists directives active within the last `config.metrics_lookback_days` days plus
    everything upcoming (open-ended), mirroring `context list`'s default window and
    override mechanism (§4): --from overrides the lower bound, --until bounds the upper
    end (default: open-ended). --all drops the lower bound entirely."""
    if getattr(args, 'all', False):
        start = None
    elif args.from_date:
        start = args.from_date
    else:
        window = config.metrics_lookback_days
        start = (
            datetime.strptime(_today_str(), "%Y-%m-%d").date() - timedelta(days=window - 1)
        ).strftime("%Y-%m-%d")
    end = args.until_date
    constraints = cli.db.get_constraints(start, end)
    if args.sport:
        from trainmate.sports import canonical_sport
        cs = canonical_sport(args.sport)
        constraints = [
            c for c in constraints
            if c.get('sport') is None or canonical_sport(c['sport']) == cs
        ]
    if args.type:
        constraints = [c for c in constraints if c.get('type') == args.type]

    print(bold(cyan("=== ATHLETE CONSTRAINTS ===")))
    if not constraints:
        print(dim("(none)"))
        return
    for c in constraints:
        print(_constraint_line(c))
        if args.verbose and c.get('description'):
            print(format_labeled_block("  Details:", c['description']))


def run_constraint_show(args: argparse.Namespace) -> None:
    """Displays a directive in detail, including whether it is plan-shaping (§7)."""
    constraint = cli.db.get_constraint(args.id)
    if not constraint:
        print(red(f"Constraint with ID {args.id} not found."))
        return
    print(_constraint_line(constraint))
    if constraint.get('description'):
        print(format_labeled_block("  Details:", constraint['description']))
    src = constraint.get('source')
    if src:
        print(gray(f"  Source: {src}"))
    if constraint.get('replan'):
        print(green("  Plan-shaping: built into the plan (replan)."))
    else:
        print(dim("  Plan-shaping: no — honored by daily 'workout adapt' only."))


def run_constraint_rm(args: argparse.Namespace) -> None:
    """Removes a directive by ID."""
    if not cli.db.get_constraint(args.id):
        print(yellow(f"No constraint with ID {args.id}."))
        return
    cli.db.delete_constraint(args.id)
    print(green(f"Constraint [{args.id}] removed."))


def run_constraint_wipe(args: argparse.Namespace) -> None:
    """Wipes all constraints after confirmation."""
    if not args.yes:
        if not cli.prompt.confirm(
            "Are you sure you want to wipe all constraints?", danger=True
        ):
            print("Wipe cancelled.")
            return
    cli.db.wipe_constraints()
    print(green("All constraints wiped successfully."))
