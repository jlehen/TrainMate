import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray, cmd,
    color_load_ratio, color_ramp, pmc_cells, pmc_warming_note, visible_len, pad_visible,
    wrap_text, format_labeled_text, format_labeled_block, PMC_TSB_LAG_NOTE,
    today_str as _today_str, today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data, pmc_warmup_cutoff


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


def _days_ago_str(date_str: str) -> str:
    """Compact ' (Nd ago)' for a plain YYYY-MM-DD calendar date, or '' if unparseable
    or in the future. Distinct from `_ago`, which takes a UTC ISO instant."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return ""
    days = (_today_date() - d).days
    if days < 0:
        return ""
    if days == 0:
        return ", today"
    return f", {days}d ago"


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
            change_reason = cli.coach_service.config_changed(macro)
            if change_reason:
                print(yellow(
                    "\nWarning: a plan-shaping input has changed since the "
                    f"active periodization plan was generated ({change_reason}).\nRun "
                    + cmd("plan generate") + " to regenerate."))
            
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
                f"No periodization strategy established. Run {cmd('plan generate')} first."
            )
    else:
        print(
            f"\n{bold('Next Objective')}: None (TrainMate needs at least one goal to start planning)"
        )

    # Fitness thresholds (the effective anchors the coach prescribes from). Each is the
    # latest logbook row for its kind, with when it was tested (DESIGN_benchmark_workouts
    # §6). max_hr is quasi-fixed physiology from config, shown for completeness.
    from trainmate.benchmarks import ANCHOR_KINDS, LOGBOOK_KINDS, format_value
    threshold_lines = []
    for kind in LOGBOOK_KINDS:
        latest = cli.db.get_latest_benchmark(kind)
        if not latest:
            continue
        anchor = ANCHOR_KINDS[kind]
        tested = _days_ago_str(latest['date'])
        threshold_lines.append(
            f"- {anchor.label}: {cyan(format_value(kind, float(latest['value'])))} "
            f"({latest['sport_type']}, tested {cyan(fmt_date(latest['date']))}{tested})"
        )
    max_hr = config.user_profile.get('max_hr')
    if max_hr is not None:
        threshold_lines.append(
            f"- {ANCHOR_KINDS['max_hr'].label}: {cyan(format_value('max_hr', max_hr))} "
            f"{gray('(config)')}"
        )
    if threshold_lines:
        print(f"\n{bold('Fitness Thresholds')}:")
        for line in threshold_lines:
            print(line)
    else:
        print(
            f"\n{bold('Fitness Thresholds')}: {dim('none on record')} "
            + gray("— record one with 'benchmark record …'")
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
        
        # Fitness/Fatigue/Form (CTL/ATL/TSB) + ramp. The whole line is dropped when all
        # three are absent (DESIGN_pmc_fitness_fatigue.md §6.1); ramp comes from the full
        # stored CTL series. All garmin helpers read cli.db (dbh=), the same database the
        # metrics above came from.
        history_start = cli.garmin.pmc_history_start(dbh=cli.db)
        warmup_cutoff = pmc_warmup_cutoff(history_start)
        ctl_v, atl_v, tsb_v = cli.garmin.pmc_display_values(last_metrics, warmup_cutoff)
        if ctl_v is not None or atl_v is not None or tsb_v is not None:
            ctl_s, atl_s, tsb_s = pmc_cells(ctl_v, atl_v, tsb_v)
            ctl_by_date = {m['date']: m.get('ctl') for m in metrics}
            ramp_v = cli.garmin.pmc_ramp(
                ctl_by_date, last_metrics['date'], warmup_cutoff=warmup_cutoff
            )
            ramp_s = color_ramp(ramp_v) + "/wk" if ramp_v is not None else "—"
            ratio_v = cli.garmin.load_ratio(atl_v, ctl_v)
            ratio_s = color_load_ratio(ratio_v) if ratio_v is not None else "—"
            print(
                f"- Fitness    : CTL {ctl_s} | ATL {atl_s} | TSB {tsb_s} | "
                f"ATL:CTL {ratio_s} | Ramp {ramp_s}"
            )
            if tsb_v is not None:
                print(dim(f"  {PMC_TSB_LAG_NOTE}"))
        # Young/warming DB (§3.3b): say WHY freshness reads low — shown even while the
        # values themselves are warm-up-suppressed above (the suppression is the reason).
        caveat = cli.garmin.pmc_data_caveat(history_start)
        if caveat:
            print(dim("  " + pmc_warming_note(caveat['n_days'], config.pmc_ctl_days)))
        
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
            yellow("\nRecent Garmin Metrics: No cached metrics. Run "
                   + cmd("data pull") + " first.")
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
        summary += green(" — see " + cmd("learnings list"))
        print(summary)
    else:
        print(
            gray("  None yet. Run " + cmd("data bootstrap")
                 + " to reconstruct your training history and seed observations.")
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
        print(gray("  Last reflect: never — run " + cmd("data reflect")))
    if bootstrap_state and bootstrap_state.get("last_pull_utc"):
        line = f"  Bootstrap:    {fmt_date(bootstrap_state['last_pull_utc'][:10])}"
        if bootstrap_state.get("through_date"):
            line += gray(f" · through {fmt_date(bootstrap_state['through_date'])}")
        print(gray(line))

    # Which model will answer the next plan/adapt — the exchange logs only say so after the
    # fact (DESIGN_model_selection.md §5).
    from trainmate.openrouter import openrouter_client
    print(gray(f"  LLM model:    {openrouter_client.model} · change with "
               + cmd("model")))

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
            kind = "no training" if c.get('rest') else "advisory"
            print(
                f"- ID: {c['id']} | {yellow(c['title'])}: "
                f"{cyan(c['start_date'])} to {cyan(c['end_date'])} "
                f"| {kind}"
                + (" | plan-shaping" if c.get('replan') else "")
            )
            if c.get('description'):
                print(format_labeled_block("  Details:", c['description']))

    print(bold(cyan("\n================================")))


def add_status_parser(subparsers, pull_bypass_parser):
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

    return status_parser
