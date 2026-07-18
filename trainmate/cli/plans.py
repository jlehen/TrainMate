import json
import textwrap
import argparse
import sys
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    color_acwr, visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, default_wrap_width, today_str as _today_str,
    today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data


def run_plan_generate(args: argparse.Namespace) -> None:
    """Executes the AI periodization strategy plan generation command."""
    # Make sure we have latest metrics cached
    ensure_recent_data(no_pull=args.no_pull, force_pull=getattr(args, 'force_pull', False))
    metrics = cli.db.get_metrics_cache()
    if not metrics:
        print(yellow("Warning: Metrics cache is empty. Proceeding without Garmin metrics."))
        
    try:
        # First-run nudge: no reflect watermark means `data bootstrap` has never run, so
        # there are no history-derived coach learnings to inform the plan. Offer to seed
        # them before generating (skipped in non-interactive --auto mode).
        if cli.db.get_sync_state("reflect") is None and not getattr(args, 'auto', False):
            if cli.prompt.confirm(
                "No training-history analysis found. Run 'data bootstrap' first to "
                "reconstruct past cycles and seed coach learnings?"
            ):
                cli.coach_service.data_bootstrap(
                    no_pull=args.no_pull, force_pull=getattr(args, 'force_pull', False)
                )

        objectives = cli.db.get_objectives(status='active')
        if objectives:
            if args.goal_id is not None:
                target_goals = [o for o in objectives if o['id'] == args.goal_id]
                next_goal = target_goals[0] if target_goals else None
            else:
                objectives.sort(key=lambda x: str(x['target_date']))
                next_goal = objectives[0]
                # Name the defaulted goal so a bare `plan generate` isn't silent
                # about which objective it planned for (DESIGN_cli_noargs.md §b).
                print(dim(
                    f"No goal given — planning for your next goal: "
                    f"{next_goal.get('title', '')} on {fmt_date(next_goal['target_date'])}."
                ))

            if next_goal:
                macro = cli.db.get_macrocycle_for_objective(next_goal['id'])
                if macro:
                    change_reason = cli.coach_service.config_changed(macro)
                    if change_reason and not args.force:
                        if cli.prompt.confirm(
                            "Plan-shaping configuration in config.yaml has changed "
                            f"since the last plan generation ({change_reason}).\n"
                            "Would you like to regenerate the periodization strategy?"
                        ):
                            args.force = True
                        else:
                            print("Keeping current periodization strategy. "
                                   "Updating configuration hash in database.")
                            cli.db.update_macrocycle_config_hash(
                                macro['id'], cli.coach_service._get_config_hash(),
                                cli.coach_service._get_config_snapshot()
                            )

        plan_kwargs = {'auto_apply': False}
        if args.goal_id is not None:
            plan_kwargs['objective_id'] = args.goal_id
        strategy, mesocycles, reused = cli.coach_service.plan_generate(
            force=bool(args.force), **plan_kwargs
        )
        
        if reused:
            print(green(f"\nActive plan is up to date ({len(mesocycles)} mesocycles)."))
            return

        if getattr(args, 'auto', False):
            apply = True
        else:
            apply = cli.prompt.confirm("Apply this new periodization strategy?")

        if apply:
            cli.coach_service.plan_apply(next_goal['id'], strategy, mesocycles)
            print(green(f"\nGenerated {len(mesocycles)} mesocycles. Save complete."))
            print(f"Run '{green('workout generate')}' to schedule workouts based on this plan.")
        else:
            print(yellow("\nPlan discarded."))

    except Exception as e:
        print(red(f"Error during plan generation: {e}"))


def _print_considered_inputs(macrocycle: dict) -> None:
    """Prints the goals and plan-shaping constraints snapshotted when the plan was generated.

    These are preserved on the macrocycle (see save_macrocycle), so they reflect the
    inputs the plan was actually built on rather than the current live records, which
    may have since changed. Older plans predate the snapshot and have nothing to show;
    plans that predate the constraints rename fall back to the legacy lifeevents snapshot.
    """
    raw_goals = macrocycle.get('goals_snapshot')
    raw_events = (
        macrocycle.get('constraints_snapshot')
        or macrocycle.get('lifeevents_snapshot')
    )
    if raw_goals is None and raw_events is None:
        print(gray("Inputs considered: not recorded (plan predates input snapshots)."))
        print()
        return

    goals = json.loads(raw_goals) if raw_goals else []
    events = json.loads(raw_events) if raw_events else []

    print(bold("Goals considered:"))
    if goals:
        for g in goals:
            sport = (g.get('sport_type') or '').upper()
            print(
                f"  - [ID: {g.get('id')}] {cyan(g.get('title', ''))} "
                f"({magenta(sport)}) on {cyan(fmt_date(g.get('target_date')))} "
                f"[priority {g.get('priority')}]"
            )
            if g.get('description'):
                for line in textwrap.wrap(g['description'], width=78):
                    print(f"      {gray(line)}")
    else:
        print(f"  {gray('None')}")

    print(bold("Constraints considered:"))
    if events:
        for e in events:
            # Snapshots are historical JSON, so tolerate three shapes: the current one
            # (a `rest` flag), the pre-rev-6 constraint (binding/sport/type), and the
            # original lifeevent (event_type/impact_description). Read whichever is present.
            if 'rest' in e:
                enforcement = "no training" if e.get('rest') else "advisory"
            else:
                enforcement = e.get('binding') or ''
            label = e.get('type') or e.get('event_type') or ''
            sport = e.get('sport')
            tags = " ".join(
                t for t in (
                    label,
                    enforcement,
                    (f"[{sport}]" if sport else ""),
                ) if t
            )
            print(
                f"  - [ID: {e.get('id')}] {cyan(e.get('title', ''))} "
                f"({tags}) "
                f"{fmt_date(e.get('start_date'))} -> {fmt_date(e.get('end_date'))}"
            )
            detail = e.get('description') or e.get('impact_description')
            if detail:
                for line in textwrap.wrap(detail, width=78):
                    print(f"      {gray(line)}")
    else:
        print(f"  {gray('None')}")
    print()


def run_plan_show(args: argparse.Namespace) -> None:
    """Displays the active training macrocycle and mesocycles periodization timeline."""
    objectives = cli.db.get_objectives(status='active')
    if not objectives:
        print(yellow("No active goals found. TrainMate needs at least one objective."))
        return
        
    if args.goal_id is not None:
        target_goals = [o for o in objectives if o['id'] == args.goal_id]
        if not target_goals:
            # Check if goal exists but is archived/completed
            goal = cli.db.get_objective(args.goal_id)
            if not goal:
                print(red(f"Goal with ID {args.goal_id} not found."))
                return
            next_goal = goal
        else:
            next_goal = target_goals[0]
    else:
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]
    
    version_id = getattr(args, 'version', None)
    if version_id is not None:
        macrocycle = cli.db.get_macrocycle(version_id)
        if not macrocycle or macrocycle.get('objective_id') != next_goal['id']:
            print(red(
                f"Plan version {version_id} does not belong to goal '{next_goal['title']}'."
            ))
            print(f"Run '{green('plan versions')}' to list this goal's plan versions.")
            return
    else:
        macrocycle = cli.db.get_macrocycle_for_objective(next_goal['id'])
    if not macrocycle:
        print(yellow(
            f"No active macrocycle strategy found for goal '{next_goal['title']}'."
        ))
        print(f"Run '{green('plan generate')}' to create one.")
        return

    mesocycles = cli.db.get_mesocycles_for_macrocycle(macrocycle['id'])

    is_superseded = macrocycle.get('status') == 'superseded'
    if is_superseded:
        superseded_on = str(macrocycle.get('superseded_at', ''))[:10]
        header = (
            f"=== SUPERSEDED MACROCYCLE STRATEGY (plan ID {macrocycle['id']}"
            + (f", superseded {superseded_on}" if superseded_on else "")
            + ") ==="
        )
        print(bold(yellow("\n" + header)))
        print(yellow(
            f"This is a past version, kept for rollback. Run "
        ) + green(f"'plan rollback --version {macrocycle['id']}'") + yellow(" to restore it."))
    else:
        print(bold(cyan("\n=== ACTIVE MACROCYCLE STRATEGY ===")))
    sport_str = next_goal['sport_type'].upper()
    print(
        f"{bold('Objective')} [ID: {next_goal['id']}]: "
        f"{cyan(next_goal['title'])} ({magenta(sport_str)}) "
        f"on {cyan(fmt_date(next_goal['target_date']))}"
    )
    print(format_labeled_block(f"{bold('Macrocycle Strategy')}:", macrocycle['strategy']))
    if macrocycle.get('feedback'):
        print(format_labeled_block(f"{bold('Macrocycle Feedback')}:", macrocycle['feedback']))
    print()
    _print_considered_inputs(macrocycle)
    print(bold("Mesocycle Timeline:"))
    
    today = _today_date()
    
    for m in mesocycles:
        start = datetime.strptime(m['start_date'], "%Y-%m-%d").date()
        end = datetime.strptime(m['end_date'], "%Y-%m-%d").date()
        
        total_days = (end - start).days + 1
        if total_days <= 0:
            total_days = 1
            
        bar_length = 20
        if end < today:
            status_str = gray("[DONE]  ")
            bar = gray("=" * bar_length)
            extra = ""
        elif start <= today <= end:
            status_str = green("[ACTIVE]")
            days_passed = (today - start).days + 1
            days_passed = max(1, min(days_passed, total_days))
            filled = round(bar_length * days_passed / total_days)
            filled = max(0, min(filled, bar_length))
            bar = green("=" * filled) + gray("." * (bar_length - filled))
            extra = green(f" (Day {days_passed}/{total_days})")
        else:
            status_str = blue("[FUTURE]")
            bar = gray("." * bar_length)
            extra = ""
            
        if total_days >= 7:
            weeks = total_days / 7
            if weeks.is_integer():
                duration_desc = f"({int(weeks)} weeks)"
            else:
                duration_desc = f"({weeks:.1f} weeks)"
        else:
            duration_desc = f"({total_days} days)"
            
        prefix = "|->" if start <= today <= end else "|--"
        if start <= today <= end:
            prefix = green(prefix)
            m_name_disp = green(m['name'])
        else:
            m_name_disp = m['name']
            
        print(
            f"{prefix} {status_str} [ID: {m['id']}] {pad_visible(m_name_disp, 15)} "
            f"({cyan(fmt_date(m['start_date']))} -> {cyan(fmt_date(m['end_date']))}) "
            f"[{bar}]{extra} {duration_desc}"
        )
              
        width = default_wrap_width()
        focus_lines = textwrap.wrap(m['focus'], width=max(20, width - 2))
        for line in focus_lines:
            print(f"  {line}")
        if m.get('feedback'):
            fb_lines = textwrap.wrap(m['feedback'], width=max(20, width - 4))
            print(f"  {bold('Mesocycle Feedback')}:")
            for line in fb_lines:
                print(f"    {line}")
        print("  " + gray("-" * 40))


def run_plan_versions(args: argparse.Namespace) -> None:
    """Lists every periodization plan version (active + superseded) for a goal."""
    if getattr(args, 'goal_id', None) is not None:
        goal = cli.db.get_objective(args.goal_id)
        if not goal:
            print(red(f"Goal with ID {args.goal_id} not found."))
            return
    else:
        objectives = cli.db.get_objectives(status='active')
        if not objectives:
            print(yellow("No active goals found. TrainMate needs at least one objective."))
            return
        objectives.sort(key=lambda x: str(x['target_date']))
        goal = objectives[0]

    versions = cli.db.get_macrocycle_versions(goal['id'])
    if not versions:
        print(yellow(f"No periodization plan exists for goal '{goal['title']}'."))
        print(f"Run '{green('plan generate')}' to create one.")
        return

    sport_str = goal['sport_type'].upper()
    print(bold(cyan("\n=== PLAN VERSIONS ===")))
    print(
        f"{bold('Objective')} [ID: {goal['id']}]: "
        f"{cyan(goal['title'])} ({magenta(sport_str)}) "
        f"on {cyan(fmt_date(goal['target_date']))}\n"
    )
    for v in versions:
        active = v.get('status') != 'superseded'
        created = str(v.get('created_at', ''))[:10]
        excerpt = " ".join((v.get('strategy') or "").split())
        if len(excerpt) > 70:
            excerpt = excerpt[:69] + "…"
        marker = green("●") if active else " "
        id_str = (green if active else str)(f"ID {v['id']}")
        if active:
            status = green("active")
        else:
            superseded = str(v.get('superseded_at', ''))[:10]
            status = gray("superseded" + (f" {superseded}" if superseded else ""))
        gen = f"generated {fmt_date(created)}" if created else ""
        print(
            f"{marker} {pad_visible(id_str, 8)} {pad_visible(status, 24)} {gray(gen)}"
        )
        if excerpt:
            print(f"    {gray(excerpt)}")
    print()
    print(gray(
        "Restore a version with "
    ) + green("'plan rollback --version <ID>'") + gray(", or inspect one with ")
        + green("'plan show --version <ID>'") + gray("."))


def run_plan_rm(args: argparse.Namespace) -> None:
    """Deletes the periodization plan for a specific goal."""
    goal = cli.db.get_objective(args.id)
    if not goal:
        print(red(f"Goal with ID {args.id} not found."))
        return

    macro = cli.db.get_macrocycle_for_objective(args.id)
    if not macro:
        print(yellow(f"No periodization plan exists for goal '{goal['title']}' (ID {args.id})."))
        return

    # Delete the plan
    cli.coach_service.plan_rm(args.id)
    print(green(f"Periodization plan for goal '{goal['title']}' removed successfully."))

    # Warn about subsequent plans
    objectives = cli.db.get_objectives(status='active')
    subsequent_goals_with_plans = []
    for obj in objectives:
        if str(obj['target_date']) > str(goal['target_date']):
            if obj['id'] is not None:
                sub_macro = cli.db.get_macrocycle_for_objective(obj['id'])
                if sub_macro:
                    subsequent_goals_with_plans.append(obj)

    if subsequent_goals_with_plans:
        print(yellow(
            "\nWarning: The following subsequent active goals have existing plans that\n"
            "were aligned with the plan you just deleted. You may need to regenerate them\n"
            "so their dates align correctly (e.g. running "
        ) + green("'plan generate --goal <ID> --force'") + yellow("):"))
        for sg in subsequent_goals_with_plans:
            print(yellow(f" - ID {sg['id']}: '{sg['title']}' (Target date: {sg['target_date']})"))


def run_plan_wipe(args: argparse.Namespace) -> None:
    """Wipes all plans from the database after confirmation."""
    if not args.yes:
        if not cli.prompt.confirm(
            "Are you sure you want to wipe all periodization plans?", danger=True
        ):
            print("Wipe cancelled.")
            return

    cli.db.wipe_plans()
    print(green("All periodization plans wiped successfully."))


def run_plan_rollback(args: argparse.Namespace) -> None:
    """Restores a superseded periodization plan version (and its workouts)."""
    # Resolve the target goal the same way generate/show do.
    if getattr(args, 'goal_id', None) is not None:
        goal = cli.db.get_objective(args.goal_id)
        if not goal:
            print(red(f"Goal with ID {args.goal_id} not found."))
            return
    else:
        objectives = cli.db.get_objectives(status='active')
        if not objectives:
            print(yellow("No active goals found."))
            return
        objectives.sort(key=lambda x: str(x['target_date']))
        goal = objectives[0]

    versions = cli.db.get_macrocycle_versions(goal['id'])
    superseded = [v for v in versions if v.get('status') == 'superseded']
    if not superseded:
        print(yellow(
            f"Goal '{goal['title']}' has no earlier plan version to roll back to."
        ))
        return

    # Determine the target version (default: chronologically previous).
    target_id = getattr(args, 'version', None)
    if target_id is None:
        prev = cli.db.get_previous_macrocycle(goal['id'])
        target_id = prev['id'] if prev else None
    if target_id is None:
        print(yellow(f"Goal '{goal['title']}' has no earlier plan version to roll back to."))
        return

    target = cli.db.get_macrocycle(target_id)
    if not target or target.get('objective_id') != goal['id']:
        print(red(f"Plan version {target_id} does not belong to goal '{goal['title']}'."))
        return

    if not getattr(args, 'yes', False):
        created = fmt_date(str(target.get('created_at', ''))[:10]) if target.get('created_at') else '?'
        if not cli.prompt.confirm(
            f"Roll back the plan for '{goal['title']}' to the version generated "
            f"{created} (plan ID {target_id})?\nThis archives the current plan's "
            f"upcoming workouts and restores that version's on Google Calendar.",
            danger=True,
        ):
            print("Rollback cancelled.")
            return

    try:
        result = cli.coach_service.plan_rollback(
            objective_id=goal['id'], target_macrocycle_id=target_id
        )
    except ValueError as e:
        print(red(str(e)))
        return

    print(green(
        f"\nRolled back '{goal['title']}' to plan ID {result['to']['id']} "
        f"(was {result['from']['id']})."
    ))
    print(
        f"Restored {result['restored_workouts']} workout(s) and archived "
        f"{result['archived_workouts']} from the superseded plan; Google Calendar updated."
    )
    print(f"Run '{green('plan show')}' to review the restored strategy.")


def _resolve_feedback_text(args: argparse.Namespace, current: Optional[str]) -> Optional[str]:
    """Returns the feedback text to save: either the editor result (--edit, seeded with the
    current value) or the positional `text`. Returns None to signal 'do not save' (aborted
    edit or empty input)."""
    if args.edit:
        new_text = cli._edit_text_in_editor(current or "")
        if new_text is None:
            return None
        if not new_text.strip():
            print(red("Error: Feedback is empty; nothing saved."))
            return None
        return new_text
    if not args.text:
        print(red("Error: Feedback text cannot be empty (or use --edit)."))
        sys.exit(1)
    return args.text


_FEEDBACK_REGEN_NOTE = (
    yellow("Note: You must regenerate the periodization plan to apply this feedback.\nRun ")
    + green("'plan generate --force'")
    + yellow(" (or with ")
    + green("'--goal <ID> --force'")
    + yellow(") to update the plan.")
)


def run_plan_feedback(args: argparse.Namespace) -> None:
    """Saves athlete feedback for a macrocycle or specific mesocycle.

    With --edit, opens $EDITOR seeded with the current feedback instead of taking text.
    """
    if not args.macro and not args.meso:
        print(red("Error: You must specify --macro or --meso <id>."))
        sys.exit(1)

    # 1. Handle mesocycle feedback directly if specified
    if args.meso:
        meso = cli.db.get_mesocycle(args.meso)
        if not meso:
            print(red(f"Mesocycle with ID {args.meso} not found."))
            sys.exit(1)
        text = _resolve_feedback_text(args, meso.get('feedback'))
        if text is None:
            return
        cli.db.update_mesocycle_feedback(args.meso, text)
        print(green(
            f"Feedback successfully saved for Mesocycle ID {args.meso} ('{meso['name']}')."
        ))
        print(_FEEDBACK_REGEN_NOTE)
        return

    # 2. Handle macrocycle feedback. Find target goal first.
    objectives = cli.db.get_objectives(status='active')
    if not objectives:
        print(yellow("No active goals found. TrainMate needs at least one objective."))
        sys.exit(1)

    if args.goal_id is not None:
        target_goals = [o for o in objectives if o['id'] == args.goal_id]
        if not target_goals:
            print(red(f"Active goal with ID {args.goal_id} not found."))
            sys.exit(1)
        next_goal = target_goals[0]
    else:
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]

    macro = cli.db.get_macrocycle_for_objective(next_goal['id'])
    if not macro:
        print(yellow(f"No active periodization plan exists for goal '{next_goal['title']}'."))
        sys.exit(1)

    text = _resolve_feedback_text(args, macro.get('feedback'))
    if text is None:
        return
    cli.db.update_macrocycle_feedback(macro['id'], text)
    print(green(
        f"Feedback successfully saved for Macrocycle ID {macro['id']} "
        f"(Goal: '{next_goal['title']}')."
    ))
    print(_FEEDBACK_REGEN_NOTE)


def add_plan_parser(subparsers, pull_bypass_parser, llm_debug_parser):
    # plan command & subparsers
    plan_parser = subparsers.add_parser(
        "plan",
        aliases=["pl"],
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
        help="Target goal ID to show the periodization plan for (defaults to the next "
             "active goal)"
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
        "rm", aliases=["d"], advanced=True,
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
    p_wipe = plan_subparsers.add_parser(
        "wipe", advanced=True, help="Wipe all periodization plans")
    p_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    

    return plan_parser
