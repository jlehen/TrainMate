from datetime import datetime, timezone
from trainmate import runtime
from trainmate import intensity
from trainmate.baselines import classify_metric, is_anomalous, UNKNOWN
from trainmate.config import config
from trainmate.util import (
    aside, asides_enabled, bold, dim, green, red, yellow, cyan, magenta, gray, cmd,
    color_load_ratio, color_ramp, pmc_cells, pmc_warming_note, format_labeled_block,
    default_wrap_width, PMC_TSB_LAG_NOTE, fmt_date, today_str as _today_str,
    today_date as _today_date, notice,
)
from trainmate.cli.common import (
    constraint_line, ensure_recent_data, pmc_warmup_cutoff,
)
from trainmate.cli import staleness
from trainmate.cli.runway import current_runway
from trainmate.coach import honoring
from trainmate.db.objectives import goal_state, GOAL_UPCOMING


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


def run_status(args) -> None:
    """Displays current athlete goals, Garmin metrics, baselines, and memories.

    Takes `args` like every other handler, so the dispatcher can call them uniformly.
    """
    verbose = getattr(args, "verbose", False)
    ensure_recent_data(
        no_pull=getattr(args, "no_pull", False),
        force_pull=getattr(args, "force_pull", False),
    )
    print(bold(cyan("=== TRAINMATE ATHLETE STATUS ===")))

    # Active Goal & Periodization Strategy
    objectives = runtime.db.upcoming_objectives()
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
            f"\n{bold('Next Goal')}: {cyan(next_goal['title'])} "
            f"({magenta(sport_str)})"
        )
        print(f"{bold('Target Date')}: {cyan(fmt_date(next_goal['target_date']))}"
              f"{gray(days_rem_str)}")
        
        print(format_labeled_block(f"{bold('Description')}:", next_goal.get('description', '')))
        
        # Query active mesocycle
        macro = runtime.db.get_macrocycle_for_objective(next_goal['id'])
        if macro:
            change_reason = staleness.reason(macro)
            if change_reason:
                # Names the fact and hands off. `status` is the athlete's overview, not
                # the place to weigh a replan: `plan show` prints the inputs this
                # contradicts and both routes out (DESIGN_plan_staleness.md §9).
                notice(
                    "\nCaution: a plan-shaping input has changed since the "
                    f"active periodization plan was generated ({change_reason}).\nRun "
                    + cmd("plan show") + " to see what changed and what to do about it.",
                )

            # Constraints the plan does not reflect yet are the same kind of fact — a
            # directive on record that nothing has acted on
            # (DESIGN_constraint_honoring.md §4), so it belongs beside the staleness
            # warning too. Counted through `honoring`, so this nag and the surfaces that
            # render the same question cannot disagree (§2).
            unhonored = honoring.constraints_needing_a_pass(runtime.db, _today_str())
            if unhonored:
                noun = "constraint" if len(unhonored) == 1 else "constraints"
                notice(
                    f"Constraints: {len(unhonored)} {noun} your plan does not reflect — "
                    + cmd("workout generate") + " builds them in.",
                )

            # Pending notes are a plan input too, so they belong beside the staleness
            # warning (DESIGN_plan_feedback.md §8).
            pending = runtime.db.list_plan_feedback(macro['id'])
            if pending:
                notice(
                    f"Plan feedback: {len(pending)} pending — " + cmd("plan generate")
                    + f" will address {'them' if len(pending) != 1 else 'it'}.",
                )

            mesos = runtime.db.get_mesocycles_for_macrocycle(macro['id'])
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
                    f"({cyan(fmt_date(active_meso['start_date']))} to "
                    f"{cyan(fmt_date(active_meso['end_date']))})"
                )
                print(format_labeled_block(f"{bold('Cycle Focus')}:", active_meso['focus']))
                # What the block ACTUALLY measured, beside what it was for
                # (DESIGN_intensity_distribution.md §9). Already wrapped to the target
                # width — never re-wrap it, the zone table is column-aligned.
                report = intensity.block_report(
                    active_meso, _today_str(), runtime.db.get_completed_activities,
                    current_week=True, benchmarks=runtime.db.get_benchmark_results(),
                    with_focus=False, indent="", width=default_wrap_width(),
                    # The measurement caveats are the same six lines every run — an
                    # aside here, though never in a prompt (DESIGN_output_verbosity.md §3.2).
                    notes=asides_enabled(),
                )
                if report:
                    print(f"\n{bold('Measured Intensity Distribution')}:")
                    print(report)
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
            f"\n{bold('Next Goal')}: None (TrainMate needs at least one goal to start planning)"
        )

    # Outside the `if objectives:` branch above, deliberately: the plan-cliff-with-no-goal
    # wording exists precisely because nothing is on record, so it would never be reached
    # from inside it (DESIGN_runway_nudge.md §4).
    runtime.render.runway_hint(current_runway(), _today_str())

    # Fitness thresholds (the effective anchors the coach prescribes from). Each is the
    # latest logbook row for its kind, with when it was tested (DESIGN_benchmark_workouts
    # §6). max_hr is quasi-fixed physiology from config, shown for completeness.
    from trainmate.benchmarks import ANCHOR_KINDS, LOGBOOK_KINDS, format_value
    threshold_lines = []
    for kind in LOGBOOK_KINDS:
        latest = runtime.db.get_latest_benchmark(kind)
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
    metrics = runtime.db.get_metrics_cache()
    if metrics:
        last_metrics = metrics[-1]
        print(f"\nRecent Garmin Metrics ({cyan(fmt_date(last_metrics['date']))}):")
        
        baseline = runtime.db.get_baseline(last_metrics['date'])
        rhr_val = last_metrics['rhr']
        hrv_val = last_metrics['hrv']
        sleep_val = last_metrics['sleep_score']
        stress_val = last_metrics['stress']
        
        rhr_display = f"{rhr_val} bpm"
        hrv_display = f"{hrv_val} ms"
        sleep_display = str(sleep_val)
        stress_display = str(stress_val)
        
        if baseline:
            rhr_verdict = classify_metric('rhr', rhr_val, baseline)
            if rhr_verdict != UNKNOWN:
                rhr_display = (
                    red(f"{rhr_val} bpm (^)") if is_anomalous(rhr_verdict)
                    else green(f"{rhr_val} bpm")
                )
            hrv_verdict = classify_metric('hrv', hrv_val, baseline)
            if hrv_verdict != UNKNOWN:
                hrv_display = (
                    red(f"{hrv_val} ms (v)") if is_anomalous(hrv_verdict)
                    else green(f"{hrv_val} ms")
                )
            sleep_verdict = classify_metric('sleep', sleep_val)
            if sleep_verdict != UNKNOWN:
                sleep_display = (
                    red(f"{sleep_val} (v)") if is_anomalous(sleep_verdict)
                    else green(str(sleep_val))
                )
                    
        print(f"- Resting HR : {rhr_display}")
        print(f"- Overnight HRV: {hrv_display}")
        print(f"- Sleep Score: {sleep_display}")
        print(f"- Stress     : {stress_display}")
        
        # Fitness/Fatigue/Form (CTL/ATL/TSB) + ramp. The whole line is dropped when all
        # three are absent (DESIGN_pmc_fitness_fatigue.md §6.1); ramp comes from the full
        # stored CTL series. All garmin helpers read runtime.db (dbh=), the same database the
        # metrics above came from.
        history_start = runtime.garmin.pmc_history_start(dbh=runtime.db)
        warmup_cutoff = pmc_warmup_cutoff(history_start)
        ctl_v, atl_v, tsb_v = runtime.garmin.pmc_display_values(last_metrics, warmup_cutoff)
        if ctl_v is not None or atl_v is not None or tsb_v is not None:
            ctl_s, atl_s, tsb_s = pmc_cells(ctl_v, atl_v, tsb_v)
            ctl_by_date = {m['date']: m.get('ctl') for m in metrics}
            ramp_v = runtime.garmin.pmc_ramp(
                ctl_by_date, last_metrics['date'], warmup_cutoff=warmup_cutoff
            )
            ramp_s = color_ramp(ramp_v) + "/wk" if ramp_v is not None else "—"
            ratio_v = runtime.garmin.load_ratio(atl_v, ctl_v)
            ratio_s = color_load_ratio(ratio_v) if ratio_v is not None else "—"
            print(
                f"- Fitness    : CTL {ctl_s} | ATL {atl_s} | TSB {tsb_s} | "
                f"ATL:CTL {ratio_s} | Ramp {ramp_s}"
            )
            if tsb_v is not None:
                aside(f"  {PMC_TSB_LAG_NOTE}")
        # Young/warming DB (§3.3b): say WHY freshness reads low — shown even while the
        # values themselves are warm-up-suppressed above (the suppression is the reason).
        caveat = runtime.garmin.pmc_data_caveat(history_start)
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
        notice("\nRecent Garmin Metrics: No cached metrics. Run "
               + cmd("data pull") + " first.")

    # Coach Learnings — one-line summary; the full list lives under 'learnings list'.
    learnings = runtime.db.get_learnings()
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
    reflect_state = runtime.db.get_sync_state("reflect")
    bootstrap_state = runtime.db.get_sync_state("bootstrap")
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
               + cmd("settings set coach-model")))

    if verbose:
        goals = runtime.db.get_objectives()
        print(bold(cyan("\nGoals:")))
        if not goals:
            print("- None")
        for g in goals:
            sport_str = g['sport_type']
            state = goal_state(g)          # derived, not stored (§12)
            status_tag = state.upper()
            if state == GOAL_UPCOMING:
                status_disp = green(f"[{status_tag}]")
                title_disp = cyan(g['title'])
            else:
                status_disp = gray(f"[{status_tag}]")
                title_disp = gray(g['title'])
            print(
                f"- {status_disp} ID: {g['id']} | {title_disp} "
                f"({sport_str}) on {cyan(fmt_date(g['target_date']))}"
            )
            if g.get('description'):
                print(format_labeled_block("  Description:", g['description']))

        today = _today_str()
        constraints = runtime.db.get_constraints(today)
        print(bold(cyan("\nActive Constraints:")))
        if not constraints:
            print("- None")
        for c in constraints:
            print(f"- {constraint_line(c, honoring.needs_a_pass(runtime.db, c, today))}")
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
        help="Show all goals and life events"
    )
    status_parser.set_defaults(func=run_status)

    return status_parser
