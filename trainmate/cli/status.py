import argparse
import sys
from datetime import datetime, timedelta, timezone
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


def _ago(iso_utc: str) -> str:
    """Return a compact 'Nm/Nh/Nd ago' for a UTC ISO timestamp, or '' if unparseable."""
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso_utc)
    except (ValueError, TypeError):
        return ""
    secs = int(delta.total_seconds())
    if secs < 0:
        return ""
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def run_status(
    verbose: bool = False, no_pull: bool = False, force_pull: bool = False
) -> None:
    """Displays current athlete goals, Garmin metrics, baselines, and memories."""
    ensure_recent_data(no_pull=no_pull, force_pull=force_pull)
    print(bold(cyan("=== TRAINMATE ATHLETE STATUS ===")))
    
    # Active Goal & Periodization Strategy
    objectives = cli.db.get_objectives(status='active')
    if objectives:
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]
        sport_str = next_goal['sport_type'].upper()
        
        # Calculate days remaining
        today = _today_date()
        target_date = datetime.strptime(next_goal['target_date'], "%Y-%m-%d").date()
        days_rem = (target_date - today).days
        days_rem_str = f" ({days_rem} days remaining)" if days_rem >= 0 else ""
        
        print(
            f"\n{bold('Next Objective')}: {cyan(next_goal['title'])} "
            f"({magenta(sport_str)})"
        )
        print(f"{bold('Target Date')}: {cyan(next_goal['target_date'])}{gray(days_rem_str)}")
        
        print(format_labeled_block(f"{bold('Description')}:", next_goal.get('description', '')))
        
        # Query active mesocycle
        macro = cli.db.get_macrocycle_for_objective(next_goal['id'])
        if macro:
            current_hash = cli.coach_service._get_config_hash()
            if macro.get('config_hash') != current_hash:
                print(yellow(
                    "\nWarning: config.yaml has changed since the active periodization plan "
                    "was generated.\nRun "
                ) + green("'plan generate'") + yellow(" to regenerate."))
            
            mesos = cli.db.get_mesocycles_for_macrocycle(macro['id'])
            active_meso = None
            for m in mesos:
                start = datetime.strptime(m['start_date'], "%Y-%m-%d").date()
                end = datetime.strptime(m['end_date'], "%Y-%m-%d").date()
                if start <= today <= end:
                    active_meso = m
                    break
            
            if active_meso:
                print(
                    f"{bold('Active Mesocycle')}: {green(active_meso['name'])} "
                    f"({cyan(active_meso['start_date'])} to {cyan(active_meso['end_date'])})"
                )
                print(format_labeled_block(f"{bold('Cycle Focus')}:", active_meso['focus']))
            else:
                print(
                    f"{bold('Active Mesocycle')}: "
                    f"None active today (outside mesocycle boundaries)"
                )
        else:
            print(
                f"{bold('Active Mesocycle')}: "
                f"No periodization strategy established. Run '{green('plan generate')}' first."
            )
    else:
        print(
            f"\n{bold('Next Objective')}: None (TrainMate needs at least one goal to start planning)"
        )

    # Recent Garmin metrics
    metrics = cli.db.get_metrics_cache()
    if metrics:
        last_metrics = metrics[-1]
        print(f"\nRecent Garmin Metrics ({cyan(last_metrics['date'])}):")
        
        baseline = cli.db.get_baseline(last_metrics['date'])
        rhr_val = last_metrics['rhr']
        hrv_val = last_metrics['hrv']
        sleep_val = last_metrics['sleep_score']
        stress_val = last_metrics['stress']
        
        rhr_display = f"{rhr_val} bpm"
        hrv_display = f"{hrv_val} ms"
        sleep_display = str(sleep_val)
        stress_display = str(stress_val)
        
        if baseline:
            if rhr_val is not None and baseline['rhr_baseline_mean'] is not None:
                sd = baseline['rhr_baseline_std'] or 1.0
                if rhr_val > (baseline['rhr_baseline_mean'] + max(3.0, sd)):
                    rhr_display = red(f"{rhr_val} bpm (^)")
                else:
                    rhr_display = green(f"{rhr_val} bpm")
            if hrv_val is not None and baseline['hrv_baseline_mean'] is not None:
                sd = baseline['hrv_baseline_std'] or 1.0
                if hrv_val < (baseline['hrv_baseline_mean'] - sd):
                    hrv_display = red(f"{hrv_val} ms (v)")
                else:
                    hrv_display = green(f"{hrv_val} ms")
            if sleep_val is not None:
                if sleep_val < 60:
                    sleep_display = red(f"{sleep_val} (v)")
                else:
                    sleep_display = green(str(sleep_val))
                    
        print(f"- Resting HR : {rhr_display}")
        print(f"- Overnight HRV: {hrv_display}")
        print(f"- Sleep Score: {sleep_display}")
        print(f"- Stress     : {stress_display}")
        
        acute = last_metrics['acute_workload'] or 0.0
        chronic = last_metrics['chronic_workload'] or 0.0
        acwr = last_metrics['acwr'] or 0.0
        print(
            f"- ACWR       : {color_acwr(acwr)} "
            f"(Acute: {acute:.1f}, Chronic: {chronic:.1f})"
        )
        
        # Baselines
        if baseline:
            print(bold("Baselines (28-day):"))
            print(
                f"- RHR Mean   : {baseline['rhr_baseline_mean']:.1f} "
                f"(std: {baseline['rhr_baseline_std']:.2f})"
            )
            print(
                f"- HRV Mean   : {baseline['hrv_baseline_mean']:.1f} "
                f"(std: {baseline['hrv_baseline_std']:.2f})"
            )
            print(
                f"- Sleep Mean : {baseline['sleep_baseline_mean']:.1f} "
                f"(std: {baseline['sleep_baseline_std']:.2f})"
            )
    else:
        print(
            yellow("\nRecent Garmin Metrics: No cached metrics. Run ")
            + green("'data pull'")
            + yellow(" first.")
        )

    # Coach Learnings — one-line summary; the full list lives under 'learnings list'.
    learnings = cli.db.get_learnings()
    print(bold("\nCoach Learnings:"))
    if learnings:
        active = [l for l in learnings if not l.get("dormant")]
        dormant = len(learnings) - len(active)
        proposed = sum(1 for l in learnings if l.get("proposed_confidence"))
        summary = f"  {len(active)} active"
        if dormant:
            summary += gray(f", {dormant} dormant")
        if proposed:
            summary += yellow(f", {proposed} pending demotion")
        summary += green(" — see 'learnings list'")
        print(summary)
    else:
        print(
            gray("  None yet. Run ") + green("'data bootstrap'")
            + gray(" to reconstruct your training history and seed observations.")
        )

    # When the learnings were last updated — reflect/bootstrap run watermarks.
    reflect_state = cli.db.get_sync_state("reflect")
    bootstrap_state = cli.db.get_sync_state("bootstrap")
    if reflect_state and reflect_state.get("last_pull_utc"):
        line = f"  Last reflect: {fmt_date(reflect_state['last_pull_utc'][:10])}"
        ago = _ago(reflect_state["last_pull_utc"])
        if ago:
            line += gray(f" ({ago})")
        if reflect_state.get("through_date"):
            line += gray(f" · through {fmt_date(reflect_state['through_date'])}")
        print(line)
    elif learnings:
        print(gray("  Last reflect: never — run ") + green("'data reflect'"))
    if bootstrap_state and bootstrap_state.get("last_pull_utc"):
        line = f"  Bootstrap:    {fmt_date(bootstrap_state['last_pull_utc'][:10])}"
        if bootstrap_state.get("through_date"):
            line += gray(f" · through {fmt_date(bootstrap_state['through_date'])}")
        print(gray(line))

    if verbose:
        goals = cli.db.get_objectives()
        print(bold(cyan("\nGoals:")))
        if not goals:
            print("- None")
        for g in goals:
            sport_str = g['sport_type']
            status_tag = g['status'].upper()
            if g['status'] == 'active':
                status_disp = green(f"[{status_tag}]")
                title_disp = cyan(g['title'])
            else:
                status_disp = gray(f"[{status_tag}]")
                title_disp = gray(g['title'])
            print(
                f"- {status_disp} ID: {g['id']} | {title_disp} "
                f"({sport_str}) on {cyan(g['target_date'])} (Priority: {g['priority']})"
            )
            if g.get('description'):
                print(format_labeled_block("  Description:", g['description']))

        constraints = cli.db.get_constraints(_today_str())
        print(bold(cyan("\nActive Constraints:")))
        if not constraints:
            print("- None")
        for c in constraints:
            ctype = c.get('type')
            type_str = f" ({magenta(ctype)})" if ctype else ""
            sport_str = f" [{c['sport']}]" if c.get('sport') else ""
            print(
                f"- ID: {c['id']} | {yellow(c['title'])}{type_str}: "
                f"{cyan(c['start_date'])} to {cyan(c['end_date'])} "
                f"| {c.get('binding', 'soft')}{sport_str}"
                + (" | plan-shaping" if c.get('replan') else "")
            )
            if c.get('description'):
                print(format_labeled_block("  Details:", c['description']))

    print(bold(cyan("\n================================")))
