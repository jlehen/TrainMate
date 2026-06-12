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
    format_labeled_block, today_str as _today_str, today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data


def run_plan_generate(args: argparse.Namespace) -> None:
    """Executes the AI periodization strategy plan generation command."""
    # Make sure we have latest metrics cached
    ensure_recent_data(no_pull=args.no_pull)
    metrics = cli.db.get_metrics_cache()
    if not metrics:
        print(yellow("Warning: Metrics cache is empty. Proceeding without Garmin metrics."))
        
    try:
        objectives = cli.db.get_objectives(status='active')
        if objectives:
            if args.goal_id is not None:
                target_goals = [o for o in objectives if o['id'] == args.goal_id]
                next_goal = target_goals[0] if target_goals else None
            else:
                objectives.sort(key=lambda x: str(x['target_date']))
                next_goal = objectives[0]

            if next_goal:
                macro = cli.db.get_macrocycle_for_objective(next_goal['id'])
                if macro:
                    current_hash = cli.coach_service._get_config_hash()
                    if macro.get('config_hash') != current_hash and not args.force:
                        try:
                            confirm = input(
                                "\nConfiguration in config.yaml has changed since the last "
                                "plan generation.\n"
                                "Would you like to regenerate the periodization strategy? [y/N]: "
                            ).strip().lower()
                        except EOFError:
                            confirm = 'n'
                        if confirm in ('y', 'yes'):
                            args.force = True
                        else:
                            print("Keeping current periodization strategy. "
                                   "Updating configuration hash in database.")
                            cli.db.update_macrocycle_config_hash(macro['id'], current_hash)

        plan_kwargs = {'auto_apply': False}
        if args.goal_id is not None:
            plan_kwargs['objective_id'] = args.goal_id
        strategy, mesocycles, reused = cli.coach_service.generate_periodization_plan(
            force=bool(args.force), **plan_kwargs
        )
        
        if reused:
            print(green(f"\nActive plan is up to date ({len(mesocycles)} mesocycles)."))
            return

        if getattr(args, 'auto', False):
            confirm = 'y'
        else:
            confirm = input("\nApply this new periodization strategy? [y/N]: ").strip().lower()

        if confirm in ('y', 'yes'):
            cli.coach_service.apply_periodization_plan(next_goal['id'], strategy, mesocycles)
            print(green(f"\nGenerated {len(mesocycles)} mesocycles. Save complete."))
            print(f"Run '{green('workout generate')}' to schedule workouts based on this plan.")
        else:
            print(yellow("\nPlan discarded."))

    except Exception as e:
        print(red(f"Error during plan generation: {e}"))


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
    
    macrocycle = cli.db.get_macrocycle_for_objective(next_goal['id'])
    if not macrocycle:
        print(yellow(
            f"No active macrocycle strategy found for goal '{next_goal['title']}'."
        ))
        print(f"Run '{green('plan generate')}' to create one.")
        return
        
    mesocycles = cli.db.get_mesocycles_for_macrocycle(macrocycle['id'])
    
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
              
        focus_lines = textwrap.wrap(m['focus'], width=80)
        for line in focus_lines:
            print(f"  {line}")
        if m.get('feedback'):
            fb_lines = textwrap.wrap(m['feedback'], width=80)
            print(f"  {bold('Mesocycle Feedback')}:")
            for line in fb_lines:
                print(f"    {line}")
        print("  " + gray("-" * 40))


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
    cli.coach_service.delete_plan(args.id)
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
        try:
            confirm = input(
                "Are you sure you want to wipe all periodization plans? [y/N]: "
            ).strip().lower()
        except EOFError:
            confirm = 'n'
        if confirm not in ('y', 'yes'):
            print("Wipe cancelled.")
            return

    cli.db.wipe_plans()
    print(green("All periodization plans wiped successfully."))
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
