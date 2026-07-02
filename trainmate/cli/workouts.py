import argparse
import sys
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.calendar_state import calendar_status
from trainmate.modification_state import modification_status
from trainmate.sports import canonical_sport
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    color_acwr, visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, render_table, today_str as _today_str,
    today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data, mark_adherence_from_results


def _fmt_ts(iso: Optional[str]) -> str:
    """Renders a stored UTC ISO timestamp as 'YYYY-MM-DD HH:MM' for the workout list.

    Falls back to the raw string if it isn't parseable (e.g. a date-only legacy value)."""
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def run_workout_adapt(args: argparse.Namespace) -> None:
    # Executes the daily workout Garmin adaptation checks command.
    date_str = args.date or _today_str()

    ensure_recent_data(
        date_str, no_pull=args.no_pull, force_pull=getattr(args, 'force_pull', False)
    )

    # Display rolling trajectory
    try:
        history_days = config.metrics_lookback_days
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_date = (date_obj - timedelta(days=history_days - 1)).strftime("%Y-%m-%d")
        metrics_history = cli.db.get_metrics_cache(start_date=start_date, end_date=date_str)
        
        print(bold(cyan(f"\n=== METRICS TRAJECTORY (PAST {history_days} DAYS) ===")))
        headers = ["Date", "HRV (ms)", "RHR (bpm)", "Sleep", "ACWR"]
        rows = []
        for m in metrics_history:
            base = cli.db.get_baseline(m['date'])
            
            hrv_val = m['hrv']
            rhr_val = m['rhr']
            sleep_val = m['sleep_score']
            acwr_val = m['acwr']
            
            hrv_str = f"{hrv_val or 'N/A'}"
            rhr_str = f"{rhr_val or 'N/A'}"
            sleep_str = f"{sleep_val or 'N/A'}"
            acwr_str = color_acwr(acwr_val) if acwr_val is not None else "N/A"
            
            if base:
                if hrv_val is not None and base['hrv_baseline_mean'] is not None:
                    sd = base['hrv_baseline_std'] or 1.0
                    if hrv_val < (base['hrv_baseline_mean'] - sd):
                        hrv_str = red(f"{hrv_val} (v)")
                    else:
                        hrv_str = green(str(hrv_val))
                if rhr_val is not None and base['rhr_baseline_mean'] is not None:
                    sd = base['rhr_baseline_std'] or 1.0
                    if rhr_val > (base['rhr_baseline_mean'] + max(3.0, sd)):
                        rhr_str = red(f"{rhr_val} (^)")
                    else:
                        rhr_str = green(str(rhr_val))
                if sleep_val is not None:
                    if sleep_val < 60:
                        sleep_str = red(f"{sleep_val} (v)")
                    else:
                        sleep_str = green(str(sleep_val))
            
            rows.append([m['date'], hrv_str, rhr_str, sleep_str, acwr_str])
        print(render_table(headers, rows))
        print(gray("((v) suppressed/poor, (^) elevated compared to baseline)\n"))
    except Exception as e:
        print(yellow(f"Warning: Could not display metrics trajectory: {e}"))

    print(f"Evaluating daily Garmin metrics adaptation for {date_str}...")
    try:
        reason, proposed_workouts, new_constraints = cli.coach_service.workout_adapt(
            date_str, message=getattr(args, 'message', None)
        )

        # §8 two-confirmation flow, step 1: confirm any constraint(s) extracted from the
        # athlete's note BEFORE the adaptation preview below — an independent commit that
        # runs even when no workout changes are proposed. Declining discards the
        # extraction; the note still informed this run's adaptation via the advisory text
        # (already baked into `reason`/`proposed_workouts` from the same LLM call).
        for candidate in new_constraints:
            title = (candidate.get('title') or '').strip()
            if not title:
                continue
            start = candidate.get('start_date') or date_str
            end = candidate.get('end_date') or start
            span = start if start == end else f"{start}..{end}"
            if cli.prompt.confirm(f"Add constraint: {title} ({span})?"):
                cid = cli.coach_service.capture_message_constraint(candidate, date_str)
                if cid is not None:
                    print(green(f"Captured constraint [{cid}]: {title} ({span})"))
            else:
                print("Discarded — not saved as a constraint.")

        print(f"\n{bold('Decision Summary')}:\n{wrap_text(reason)}")

        if not proposed_workouts:
            print(green(
                "\nAll metrics are green and workout plan is on track. No changes recommended."
            ))
            return

        print(bold(yellow("\nPROPOSED WORKOUT ADAPTATIONS:")))
        headers = [
            "Date", "Sport", "Original Workout", "Adapted Workout",
            "Duration/RPE/TSS",
        ]
        rows = []

        # Pair each proposal with the session it adapts. An in-place adapt matches by
        # (date, sport) — get_workout is alias-aware, so it always finds the original.
        # A sport SWAP (e.g. a strength session converted to REST) carries a new
        # sport_type with no same-sport original, so get_workout returns None and the row
        # would otherwise show "[None]", hiding the replaced session's data. Mirror the
        # apply-time rule (workout_adapt_apply): on a proposed date, an existing session
        # whose canonical sport isn't among that date's proposals is the one being
        # overridden, so pair it with that date's new-sport proposal.
        proposed_by_date: dict[str, list] = {}
        for pw in proposed_workouts:
            proposed_by_date.setdefault(pw['date'], []).append(pw)
        swap_original: dict[int, dict] = {}
        leftover_removed: list[dict] = []
        for date, proposals in proposed_by_date.items():
            existing_list = cli.db.get_workouts(start_date=date, end_date=date)
            proposed_canons = {canonical_sport(p['sport_type']) for p in proposals}
            existing_canons = {canonical_sport(e['sport_type']) for e in existing_list}
            overridden = [
                e for e in existing_list
                if canonical_sport(e['sport_type']) not in proposed_canons
            ]
            new_proposals = [
                p for p in proposals
                if canonical_sport(p['sport_type']) not in existing_canons
            ]
            # Common case: exactly one overridden session and one new-sport proposal (a
            # clean swap). Pair positionally; any overridden session left without a
            # new-sport proposal is a plain deletion — surface it as a "[Removed]" row so
            # the apply step never silently drops a session the preview didn't show.
            for p, orig in zip(new_proposals, overridden):
                swap_original[id(p)] = orig
            leftover_removed.extend(overridden[len(new_proposals):])

        def _stats(w: dict) -> str:
            return (
                f"{w.get('duration_minutes') or 0}m/"
                f"RPE{w.get('rpe') or 0}/"
                f"TSS{w.get('tss') or 0}"
            )

        for pw in proposed_workouts:
            existing = cli.db.get_workout(pw['date'], pw['sport_type'])
            if existing is None:
                existing = swap_original.get(id(pw))
            is_swap = existing is not None and (
                canonical_sport(existing['sport_type'])
                != canonical_sport(pw['sport_type'])
            )

            orig_title = existing['title'] if existing else "[None]"
            stats_diff = (
                f"{_stats(existing)} -> {_stats(pw)}" if existing else _stats(pw)
            )
            sport_label = (
                f"{existing['sport_type'].upper()}->{pw['sport_type'].upper()}"
                if is_swap else pw['sport_type'].upper()
            )

            rows.append([
                cyan(pw['date']), magenta(sport_label), gray(orig_title),
                green(pw['title']), yellow(stats_diff),
            ])

        # Sessions being deleted outright (overridden with no replacement proposal).
        for ew in sorted(leftover_removed, key=lambda w: w['date']):
            rows.append([
                cyan(ew['date']), magenta(ew['sport_type'].upper()),
                gray(ew['title']), red("[Removed]"),
                yellow(f"{_stats(ew)} -> removed"),
            ])

        print(render_table(headers, rows))

        if args.auto:
            apply = True
        else:
            apply = cli.prompt.confirm(
                "Apply these adaptations to your training plan and sync to Calendar?"
            )

        if apply:
            print("\nApplying adaptations...")
            all_dates = [pw['date'] for pw in proposed_workouts]
            start_date_adapt = min(all_dates)
            end_date_adapt = max(all_dates)
            cli.coach_service.workout_adapt_apply(
                proposed_workouts, reason, start_date_adapt, end_date_adapt
            )
            print(green("Adaptations applied and synced to calendar successfully."))
        else:
            print("\nAdaptations discarded.")

    except Exception as e:
        print(red(f"Error executing daily adaptation: {e}"))


def _resolve_workout_end_date(
    args: argparse.Namespace, resolved_goal: dict | None
) -> str | None:
    """Returns the end date string for workout generation based on CLI horizon flags."""
    today = _today_date()

    if getattr(args, 'horizon_days', None) is not None:
        return (today + timedelta(days=args.horizon_days)).strftime("%Y-%m-%d")

    if getattr(args, 'horizon_weeks', None) is not None:
        return (today + timedelta(days=round(args.horizon_weeks * 7))).strftime("%Y-%m-%d")

    if getattr(args, 'horizon_until', None) is not None:
        try:
            datetime.strptime(args.horizon_until, "%Y-%m-%d")
        except ValueError:
            print(red(f"Invalid date format for --until: '{args.horizon_until}'. Use YYYY-MM-DD."))
            sys.exit(1)
        return args.horizon_until

    if getattr(args, 'horizon_goal_id', None) is not None:
        goal_id = args.horizon_goal_id
        if goal_id == -1:
            # Sentinel: use the already-resolved goal for this generate run
            if resolved_goal is None:
                print(red("No active goal found for --until-goal."))
                sys.exit(1)
            return resolved_goal['target_date']
        goal = next((o for o in cli.db.get_objectives(status='active') if o['id'] == goal_id), None)
        if goal is None:
            print(red(f"Active goal with ID {goal_id} not found."))
            sys.exit(1)
        return goal['target_date']

    if getattr(args, 'horizon_meso_id', None) is not None:
        meso = cli.db.get_mesocycle(args.horizon_meso_id)
        if meso is None:
            print(red(f"Mesocycle with ID {args.horizon_meso_id} not found."))
            sys.exit(1)
        return meso['end_date']

    return None  # fall back to config default in workout_generate()


def run_workout_generate(args: argparse.Namespace) -> None:
    """Executes the AI workout generation command based on active strategy."""
    ensure_recent_data(
        no_pull=args.no_pull, force_pull=getattr(args, 'force_pull', False)
    )

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
                    if macro.get('config_hash') != current_hash:
                        message = (
                            yellow("Warning: config.yaml has changed since the active "
                                   "periodization plan was generated.\n"
                                   "Generating workouts using the out-of-date plan might "
                                   "result in incorrect training targets.\n"
                                   "It is highly recommended to run ")
                            + green("'plan generate'")
                            + yellow(" first. Proceed anyway?")
                        )
                        if not cli.prompt.confirm(message):
                            print(
                                yellow("Workout generation cancelled. Please run ")
                                + green("'plan generate'")
                                + yellow(" first.")
                            )
                            return
                        else:
                            print("Proceeding. Updating configuration hash in database.")
                            cli.db.update_macrocycle_config_hash(macro['id'], current_hash)

        workout_kwargs = {}
        if args.goal_id is not None:
            workout_kwargs['objective_id'] = args.goal_id

        end_date = _resolve_workout_end_date(args, next_goal if objectives else None)
        if end_date is not None:
            workout_kwargs['end_date'] = end_date

        reasoning, workouts = cli.coach_service.workout_generate(**workout_kwargs)
        print(bold(cyan("\n=== WORKOUTS GENERATED BY COACH ===")))
        print(f"{bold('Reasoning')}:\n{wrap_text(reasoning)}\n")
        print(green(
            f"Generated {len(workouts)} workouts starting from today and pushed them "
            "to Google Calendar."
        ))
        print(f"Run '{green('plan rollback')}' to undo this regeneration if needed.")
    except Exception as e:
        print(red(f"Error during workout generation: {e}"))



def _resolve_workout_date_range(
    args: argparse.Namespace,
) -> tuple[str | None, str | None]:
    """Resolves (start_date, end_date) from the shared date-filter CLI args."""
    today_str = _today_str()

    # Resolve start_date
    start_date = None
    if getattr(args, 'from_date', None) is not None:
        start_date = args.from_date
    elif getattr(args, 'from_meso', False):
        active_meso = cli.db.get_active_mesocycle(today_str)
        if not active_meso:
            print(red("Error: No active mesocycle found to start from."))
            sys.exit(1)
        start_date = active_meso['start_date']
    elif getattr(args, 'meso_id', None) is not None:
        meso_id = args.meso_id
        if meso_id == -1:
            active_meso = cli.db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = cli.db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        start_date = meso['start_date']
    elif getattr(args, 'until_meso_id', None) is not None:
        meso_id = args.until_meso_id
        if meso_id == -1:
            active_meso = cli.db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = cli.db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        # --until-mesocycle starts from today by default
        start_date = today_str
    elif getattr(args, 'goal_id', None) is not None:
        goal_id = args.goal_id
        if goal_id == -1:
            active_goal = cli.db.get_active_objective()
            if not active_goal:
                print(red("Error: No active goal found."))
                sys.exit(1)
            goal_id = active_goal['id']
        macro = cli.db.get_macrocycle_for_objective(goal_id)
        if not macro:
            print(red(f"Error: No plan exists for Goal ID {goal_id}."))
            sys.exit(1)
        mesos = cli.db.get_mesocycles_for_macrocycle(macro['id'])
        if not mesos:
            print(red(f"Error: No mesocycles found for Goal ID {goal_id}."))
            sys.exit(1)
        start_date = min(m['start_date'] for m in mesos)
    elif (
        getattr(args, 'days', None) is not None
        or getattr(args, 'weeks', None) is not None
        or getattr(args, 'until_date', None) is not None
    ):
        start_date = today_str

    # Resolve end_date — map list/push args to the horizon_* namespace expected
    # by _resolve_workout_end_date()
    target_meso_id = None
    if getattr(args, 'until_meso_id', None) is not None:
        target_meso_id = args.until_meso_id
    elif getattr(args, 'meso_id', None) is not None:
        target_meso_id = args.meso_id

    if target_meso_id == -1:
        active_meso = cli.db.get_active_mesocycle(today_str)
        if not active_meso:
            print(red("Error: No active mesocycle found."))
            sys.exit(1)
        target_meso_id = active_meso['id']

    target_goal_id = None
    if getattr(args, 'goal_id', None) is not None:
        target_goal_id = args.goal_id
        if target_goal_id == -1:
            active_goal = cli.db.get_active_objective()
            if not active_goal:
                print(red("Error: No active goal found."))
                sys.exit(1)
            target_goal_id = active_goal['id']

    horizon_args = argparse.Namespace(
        horizon_days=getattr(args, 'days', None),
        horizon_weeks=getattr(args, 'weeks', None),
        horizon_until=getattr(args, 'until_date', None),
        horizon_goal_id=target_goal_id,
        horizon_meso_id=target_meso_id,
    )

    next_goal = cli.db.get_active_objective()
    end_date = _resolve_workout_end_date(horizon_args, next_goal)

    return start_date, end_date


def run_workout_list(args: argparse.Namespace) -> None:
    """Lists stored workouts chronologically, with optional date, goal, or type filters."""
    start_date, end_date = _resolve_workout_date_range(args)

    # If no date options were provided...
    if start_date is None and end_date is None:
        today = _today_date()
        start_date = today.strftime("%Y-%m-%d")
        if getattr(args, "sport_type", None):
            # Only sport_type provided: from today onwards
            end_date = None
        else:
            # No options at all: from today for the next 7 days
            end_date = (today + timedelta(days=7)).strftime("%Y-%m-%d")

    # Fetch workouts using our extended cli.db.get_workouts
    workouts = cli.db.get_workouts(
        start_date=start_date,
        end_date=end_date,
        sport_type=args.sport_type,
        include_removed=getattr(args, "removed", False)
    )
    
    print(bold(cyan("=== WORKOUT SCHEDULE ===")))
    if start_date or end_date or args.sport_type:
        filter_parts = []
        if start_date:
            filter_parts.append(f"From: {start_date}")
        if end_date:
            filter_parts.append(f"Until: {end_date}")
        if args.sport_type:
            filter_parts.append(f"Type: {args.sport_type}")
        print(gray(f"Filters: {', '.join(filter_parts)}"))

    # The long batch rationale is stamped on every workout of an adapt run; show each
    # distinct summary only once across the listing so it doesn't dominate the output.
    seen_summaries: set = set()
    for w in workouts:
        mod_marker = ""
        mod_status = modification_status(w)
        if mod_status == 'adapted':
            # Surface repeat easings: a session adapted by more than one adapt run
            # reads [ADAPTED ×N], flagging load that has been walked down multiple times.
            count = w.get('adaptation_count') or 0
            label = f" [ADAPTED ×{count}]" if count > 1 else " [ADAPTED]"
            mod_marker = bold(yellow(label))
        elif mod_status == 'swapped':
            mod_marker = bold(yellow(" [SWAPPED]"))
        elif mod_status == 'replaced':
            mod_marker = bold(yellow(" [REPLACED]"))
        sync_marker = ""
        status = calendar_status(w)
        if status == 'synced':
            sync_marker = bold(green(" [SYNCED]"))
        elif status == 'stale':
            sync_marker = bold(yellow(" [STALE]"))
        rem_marker = ""
        if w.get('removed'):
            rem_marker = bold(red(" [REMOVED]"))
        src_marker = ""
        if w.get('source') == 'manual':
            src_marker = bold(magenta(" [MANUAL]"))
        duration = w.get('duration_minutes')
        tss = w.get('tss')
        rpe = w.get('rpe')
        duration_str = f" | {duration}min" if duration else ""
        tss_str = f" | TSS {tss}" if tss is not None else ""
        rpe_str = f" | RPE {rpe}" if rpe is not None else ""
        print(
            f"ID: {w['id']} | {cyan(fmt_date(w['date']))} | {magenta(w['sport_type'].upper())} | "
            f"{bold(w['title'])}{mod_marker}{sync_marker}{rem_marker}{src_marker}{duration_str}{tss_str}{rpe_str}"
        )
        # Lifecycle timestamps: when the session first entered the plan and, if ever
        # eased, when the most recent `workout adapt` run touched it. Both NULL on
        # rows predating these columns, so the line is omitted when neither is known.
        lifecycle_parts = []
        if w.get('created_at'):
            lifecycle_parts.append(f"Planned: {_fmt_ts(w['created_at'])}")
        if w.get('adapted_at'):
            lifecycle_parts.append(f"Last adapted: {_fmt_ts(w['adapted_at'])}")
        if lifecycle_parts:
            print(gray("  " + "  ·  ".join(lifecycle_parts)))
        print(format_labeled_block("  Description:", w['description']))
        summary = w.get('adaptation_summary')
        # Show the per-workout note inline, unless it's just the batch reason echoed
        # (the fallback when the model gave no per-workout change_reason) — that would
        # duplicate the Adapt summary printed below.
        if w.get('modification_reason') and w['modification_reason'] != summary:
            print(format_labeled_block("  Reason:", w['modification_reason'], color_fn=yellow))
        if summary and summary not in seen_summaries:
            seen_summaries.add(summary)
            print(format_labeled_block("  Adapt summary:", summary, color_fn=gray))
        print(gray("-" * 40))


def run_workout_compare(args: argparse.Namespace) -> None:
    """Compares planned workouts against completed activities for the given date range."""
    today_str = _today_str()
    today_obj = _today_date()

    start_date, end_date = _resolve_workout_date_range(args)

    # For compare, --days/--weeks mean "look back N days" instead of "look forward N days".
    # An explicit --from/--mesocycle/--goal already sets start_date to the right anchor.
    has_explicit_start = (
        getattr(args, 'from_date', None) is not None
        or getattr(args, 'from_meso', False)
        or getattr(args, 'meso_id', None) is not None
        or getattr(args, 'goal_id', None) is not None
    )
    if not has_explicit_start:
        days = getattr(args, 'days', None)
        weeks = getattr(args, 'weeks', None)
        if days is not None:
            start_date = (today_obj - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        elif weeks is not None:
            ndays = max(1, round(weeks * 7))
            start_date = (today_obj - timedelta(days=ndays - 1)).strftime("%Y-%m-%d")
        else:
            # Default or --until-only: 14-day lookback
            start_date = (today_obj - timedelta(days=13)).strftime("%Y-%m-%d")

    # Cap end_date at today — we can only compare past/present activities
    if end_date is None or end_date > today_str:
        end_date = today_str

    if not getattr(args, 'no_pull', False):
        try:
            cli.garmin.ensure_data(
                start_date, end_date, force=getattr(args, 'force_pull', False)
            )
        except Exception as e:
            print(yellow(f"Warning: Could not ensure recent data: {e}"))

    all_workouts = cli.db.get_workouts(start_date=start_date, end_date=end_date)
    activities = cli.db.get_completed_activities(start_date=start_date, end_date=end_date)

    start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
    history_days = (end_date_obj - start_date_obj).days + 1

    # Planned blocks overlapping the window: an activity on a date outside every block
    # is history no plan governed, shown as informational rather than "unplanned".
    covered_ranges = cli.db.get_mesocycle_ranges(start_date, end_date)

    discrepancies, matching_results, informational = analyze_adherence(
        planned_workouts=all_workouts,
        completed_activities=activities,
        start_date_obj=start_date_obj,
        history_days=history_days,
        minor_activity_load_threshold=config.minor_activity_load_threshold,
        covered_ranges=covered_ranges,
    )

    sport_filter = (getattr(args, 'sport_type', None) or "").lower() or None

    matched_act_ids = {
        r['completed']['activity_id'] for r in matching_results if r['completed']
    }

    acts_by_date: dict = {}
    for act in activities:
        acts_by_date.setdefault(act['date'], []).append(act)

    results_by_date: dict = {}
    for r in matching_results:
        results_by_date.setdefault(r['date'], []).append(r)

    print(bold(cyan("=== WORKOUT COMPARE ===")))
    filter_parts = [f"From: {start_date}", f"Until: {end_date}"]
    if sport_filter:
        filter_parts.append(f"Type: {sport_filter}")
    print(gray(f"Filters: {', '.join(filter_parts)}"))
    print()

    def _fmt_act(act: dict) -> str:
        dur = f"{act['duration_sec'] / 60:.0f}min"
        parts: list[str] = [dur, f"load {cli.garmin.activity_load(act):.0f}"]
        if act.get('tss'):
            parts.append(f"TSS {act['tss']:.0f}")
        if act.get('rpe'):
            parts.append(f"RPE {act['rpe']}")
        s = f"[{act['activity_type']}] {act['activity_name']} ({', '.join(parts)})"
        div = cli.garmin.rpe_divergence(act)
        if div is not None:
            s += f" [load from RPE: HR under-counted {div:.1f}x]"
        return s

    has_output = False
    for d in range(history_days):
        date_curr = (start_date_obj + timedelta(days=d)).strftime("%Y-%m-%d")
        day_results = results_by_date.get(date_curr, [])
        day_acts = acts_by_date.get(date_curr, [])
        unplanned = [a for a in day_acts if a['activity_id'] not in matched_act_ids]

        if sport_filter:
            day_results = [
                r for r in day_results
                if r['planned']['sport_type'].lower() == sport_filter
            ]
            unplanned = [
                a for a in unplanned
                if sport_filter in a['activity_type'].lower()
            ]

        if not day_results and not unplanned:
            continue

        has_output = True
        print(bold(cyan(fmt_date(date_curr))))

        for r in day_results:
            w = r['planned']
            act = r['completed']
            is_rest = w['sport_type'] == 'rest'

            if is_rest:
                print(f"  PLANNED:    [{magenta('REST')}]")
            else:
                parts = []
                if w.get('duration_minutes'):
                    parts.append(f"{w['duration_minutes']}min")
                if w.get('tss') is not None:
                    parts.append(f"TSS {w['tss']}")
                info = f" ({', '.join(parts)})" if parts else ""
                print(f"  PLANNED:    [{magenta(w['sport_type'].upper())}] {bold(w['title'])}{info}")

            if act:
                act_str = _fmt_act(act)
                if is_rest:
                    print(f"  ACTUAL:     {red(act_str)} {bold(red('[REST VIOLATION]'))}")
                else:
                    print(f"  ACTUAL:     {green(act_str)}")
            elif not is_rest:
                print(f"  ACTUAL:     {red('(none — missed)')}")

        for act in unplanned:
            act_load = cli.garmin.activity_load(act)
            act_str = _fmt_act(act)
            if act_load < config.minor_activity_load_threshold:
                print(gray(f"  (minor):    {act_str}"))
            elif date_covered(date_curr, covered_ranges):
                print(f"  UNPLANNED:  {yellow(act_str)}")
            else:
                print(gray(f"  (off-plan): {act_str}"))

        print(gray("-" * 40))

    if not has_output:
        print(gray("No planned workouts or completed activities found in this range."))
        return

    print()
    if discrepancies:
        print(bold(yellow("=== DISCREPANCIES ===")))
        for disc in discrepancies:
            print(yellow(disc))
        print()
        n = len(discrepancies)
        print(bold(yellow(f"{n} discrepanc{'ies' if n != 1 else 'y'} found.")))
    else:
        print(bold(green("No discrepancies found. Great adherence!")))

    if informational:
        print()
        print(bold(gray("=== OUTSIDE ANY PLAN (informational) ===")))
        for note in informational:
            print(gray(note))

    if not getattr(args, 'no_mark', False) and config.google_calendar_id:
        marked = mark_adherence_from_results(matching_results, today_str)
        print()
        if marked:
            print(green(f"Marked {marked} past event(s) on Calendar with adherence."))
        else:
            print(gray("Calendar adherence already up to date for this range."))


def run_workout_push(args: argparse.Namespace) -> None:
    """Synchronizes planned workouts with Google Calendar."""
    today_str = _today_str()
    force = getattr(args, 'force', False)

    start_date, end_date = _resolve_workout_date_range(args)
    # Default to today onwards when no date filter is given
    if start_date is None:
        start_date = today_str

    all_workouts = cli.db.get_workouts(
        start_date=start_date,
        end_date=end_date,
        sport_type=getattr(args, 'sport_type', None),
        include_removed=True,
    )

    to_push = []
    for w in all_workouts:
        is_fresh = calendar_status(w) == 'synced'
        if not w.get('removed'):
            if force or not is_fresh:
                to_push.append(w)
        else:
            # A removed workout only needs pushing if it has an event to update.
            if w.get('google_event_id') and (force or not is_fresh):
                to_push.append(w)

    if not to_push:
        if force:
            print("No workouts found in the specified range.")
        else:
            print(
                f"No new or modified workouts to sync. "
                f"Run '{green('workout generate')}' to generate a schedule, "
                f"or use -f to re-push already-synced workouts."
            )
        return

    print(f"Syncing {len(to_push)} workouts to Google Calendar...")
    try:
        cli.calendar_syncer.sync_multiple(to_push)
        print(green("Google Calendar synchronization completed."))
    except Exception as e:
        print(red(f"Error syncing to Google Calendar: {e}"))


def run_workout_rm(args: argparse.Namespace) -> None:
    """Soft-removes a planned workout: marks it removed (kept in the DB) and updates its
    Calendar event to be marked as deleted. Removed workouts are excluded from listings,
    comparisons, and the calendar push, but are still surfaced to the coach as a deliberate
    cancellation."""
    workout = cli.db.get_workout_by_id(args.id)
    if not workout:
        print(red(f"Workout with ID {args.id} not found."))
        return

    if workout.get('removed'):
        print(yellow(f"Workout with ID {args.id} ('{workout['title']}') is already removed."))
        return

    cli.db.mark_workout_removed(args.id, reason=args.reason)

    if workout.get('google_event_id'):
        print("Workout is synced to Google Calendar. Updating calendar event...")
        updated_workout = cli.db.get_workout_by_id(args.id)
        if updated_workout is not None:
            try:
                cli.calendar_syncer.sync_workout(updated_workout)
            except Exception as e:
                print(red(f"Error updating Google Calendar event: {e}"))

    print(green(
        f"Workout with ID {args.id} ('{workout['title']}') removed successfully."
    ))
    if args.reason:
        print(f"Reason: {args.reason}")

def run_workout_restore(args: argparse.Namespace) -> None:
    """Restores a soft-removed workout and updates its Calendar event to remove the
    deleted mark."""
    workout = cli.db.get_workout_by_id(args.id)
    if not workout:
        print(red(f"Workout with ID {args.id} not found."))
        return

    if not workout.get('removed'):
        print(yellow(f"Workout with ID {args.id} ('{workout['title']}') is not removed."))
        return

    cli.db.restore_workout(args.id)

    if workout.get('google_event_id'):
        print("Workout is synced to Google Calendar. Updating calendar event...")
        updated_workout = cli.db.get_workout_by_id(args.id)
        if updated_workout is not None:
            try:
                cli.calendar_syncer.sync_workout(updated_workout)
            except Exception as e:
                print(red(f"Warning: Failed to update Google Calendar: {e}"))
                print(yellow("The workout was restored locally but might still appear deleted on your calendar."))
                return

    print(green(f"Workout with ID {args.id} restored successfully."))


def _resolve_swap_ops(args: argparse.Namespace) -> list | None:
    """Turns CLI args into swap operations, or returns None on a usage/lookup error."""
    using_ids = args.id1 is not None or args.id2 is not None
    using_dates = bool(args.date1 or args.date2)

    if using_ids and using_dates:
        print(red("Provide either two dates or --id1/--id2, not both."))
        return None

    if using_ids:
        if args.id1 is None or args.id2 is None:
            print(red("Both --id1 and --id2 are required for an ID-based swap."))
            return None
        w1 = cli.db.get_workout_by_id(args.id1)
        w2 = cli.db.get_workout_by_id(args.id2)
        if not w1:
            print(red(f"Workout with ID {args.id1} not found."))
            return None
        if not w2:
            print(red(f"Workout with ID {args.id2} not found."))
            return None
        if w1['date'] == w2['date']:
            print(yellow("Both workouts are already on the same date; nothing to swap."))
            return None
        today = _today_str()
        for w in (w1, w2):
            if w['date'] < today:
                print(red(
                    f"Cannot swap [{w['id']}] {w['title']} ({w['date']}): "
                    "it is in the past."
                ))
                return None
        print(
            f"Swapping [{w1['id']}] {w1['title']} ({w1['date']}) <-> "
            f"[{w2['id']}] {w2['title']} ({w2['date']})"
        )
        return [
            {'id': w1['id'], 'new_date': w2['date']},
            {'id': w2['id'], 'new_date': w1['date']},
        ]

    if args.date1 and args.date2:
        for d in (args.date1, args.date2):
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                print(red(f"Invalid date format: '{d}'. Use YYYY-MM-DD."))
                return None
        if args.date1 == args.date2:
            print(yellow("The two dates are identical; nothing to swap."))
            return None
        today = _today_str()
        for d in (args.date1, args.date2):
            if d < today:
                print(red(f"Cannot swap {d}: it is in the past."))
                return None
        on_1 = cli.db.get_workouts(start_date=args.date1, end_date=args.date1)
        on_2 = cli.db.get_workouts(start_date=args.date2, end_date=args.date2)
        if not on_1 and not on_2:
            print(yellow(
                f"No workouts on either {args.date1} or {args.date2}; nothing to swap."
            ))
            return None
        desc_1 = ", ".join(w['title'] for w in on_1) or "(rest)"
        desc_2 = ", ".join(w['title'] for w in on_2) or "(rest)"
        print(f"Swapping {args.date1} [{desc_1}] <-> {args.date2} [{desc_2}]")
        return (
            [{'id': w['id'], 'new_date': args.date2} for w in on_1]
            + [{'id': w['id'], 'new_date': args.date1} for w in on_2]
        )

    print(
        red("Specify two dates (e.g. ")
        + bold(green("'workout swap 2026-06-09 2026-06-11'"))
        + red(") or --id1 and --id2.")
    )
    return None


def run_workout_swap(args: argparse.Namespace) -> None:
    """Exchanges workouts between two dates or two IDs, with recovery validation."""
    ops = _resolve_swap_ops(args)
    if not ops:
        return

    warnings = cli.coach_service.workout_swap_validate(ops)
    if warnings:
        print(bold(yellow("\nSwap warnings:")))
        for msg in warnings:
            print(yellow(f"  - {msg}"))
        if not args.force:
            if not cli.prompt.confirm("Proceed with the swap anyway?"):
                print("\nSwap cancelled.")
                return

    updated = cli.coach_service.workout_swap_apply(ops, args.no_sync, reason=args.reason)
    print(green(f"\nSwapped {len(updated)} workout(s) successfully."))
    for w in updated:
        print(f"  [{w['id']}] {w['title']} -> {w['date']}")
    if args.reason:
        print(f"Reason: {args.reason}")
    if args.no_sync:
        print(gray("Calendar sync skipped (--no-sync)."))


def run_workout_add(args: argparse.Namespace) -> None:
    """Manually schedules a workout on a date, replacing any same-sport session."""
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(red(f"Invalid date format: '{args.date}'. Use YYYY-MM-DD."))
        sys.exit(1)

    # Normalize to the coach's canonical sport vocabulary so the stored session and the
    # "Replacing existing ..." preview both match what generate/adapt will look up.
    args.sport_type = canonical_sport(args.sport_type)

    if args.replace_day:
        to_replace = cli.db.get_workouts(start_date=args.date, end_date=args.date)
    else:
        same = cli.db.get_workout(args.date, args.sport_type)
        to_replace = [same] if same else []
    for w in to_replace:
        print(yellow(
            f"Replacing existing {w['sport_type']} workout on {args.date}: "
            f"{w['title']}"
        ))

    saved, replaced = cli.coach_service.workout_add(
        date=args.date,
        sport_type=args.sport_type,
        title=args.title,
        description=args.description or "",
        duration_minutes=args.duration,
        rpe=args.rpe,
        tss=args.tss,
        reason=args.reason,
        replace_day=args.replace_day,
    )

    if not saved:
        print(red("Failed to save workout."))
        sys.exit(1)

    stat_parts = []
    if saved.get('duration_minutes') is not None:
        stat_parts.append(f"{saved['duration_minutes']}m")
    if saved.get('tss') is not None:
        stat_parts.append(f"TSS {saved['tss']}")
    if saved.get('rpe') is not None:
        stat_parts.append(f"RPE {saved['rpe']}")
    stats = f" ({', '.join(stat_parts)})" if stat_parts else ""
    verb = "Replaced with" if replaced else "Added"
    print(green(
        f"{verb} [{saved['id']}] {saved['title']}{stats} on {saved['date']} "
        f"({saved['sport_type']}) and synced to Calendar."
    ))


def run_workout_wipe(args: argparse.Namespace) -> None:
    """Wipes all workouts from the database and Google Calendar after confirmation."""
    if not args.yes:
        if not cli.prompt.confirm(
            "Are you sure you want to wipe all workouts "
            "(including Google Calendar events)?", danger=True
        ):
            print("Wipe cancelled.")
            return

    workouts = cli.db.get_workouts()
    synced_workouts = [w for w in workouts if w.get('google_event_id')]
    if synced_workouts:
        print(f"Deleting {len(synced_workouts)} events from Google Calendar...")
        for w in synced_workouts:
            ge_id = w['google_event_id']
            if ge_id:
                cli.calendar_syncer.delete_workout_event(ge_id)

    cli.db.wipe_workouts()
    print(green("All workouts wiped successfully."))
