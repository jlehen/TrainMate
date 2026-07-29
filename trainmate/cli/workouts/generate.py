"""Workout CLI: adapt / generate / list / compare (LLM- and read-heavy)."""
import argparse
import sys
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.calendar_state import calendar_status
from trainmate.google_calendar import event_url
from trainmate.modification_state import modification_status
from trainmate.sports import canonical_sport
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    visible_len, pad_visible, wrap_text,
    format_labeled_text, format_labeled_block, render_table,
    today_str as _today_str, today_date as _today_date, days_between,
)
from trainmate.cli.common import (
    fmt_date, ensure_recent_data, mark_adherence_from_results,
)

from trainmate.cli.workouts._helpers import (_fmt_ts, _resolve_workout_date_range,
    _resolve_workout_end_date)


def _print_block_boundary_hint(date_str: str) -> None:
    """Points at `workout generate` when the current block is about to end.

    Adapt cannot reach the next block, whose sessions may have been planned long ago against
    stale metrics. Prints on every run in the terminal window, not only when adaptations are
    proposed (DESIGN_block_boundary.md §4).
    """
    meso = cli.db.get_active_mesocycle(date_str)
    if not meso:
        return
    days_left = days_between(date_str, meso['end_date'])
    if not 0 <= days_left <= config.adapt_terminal_window_days:
        return
    next_meso = cli.db.get_next_mesocycle(meso['end_date'])
    if not next_meso:
        return

    when = "today" if days_left == 0 else f"in {days_left} day(s), on {meso['end_date']}"
    print(yellow(f"This block ({meso['name']}) ends {when}."))
    print(gray(wrap_text(
        f"Sessions in the next block ({next_meso['name']}) are outside this adaptation's "
        f"reach. To re-plan them against current metrics:"
    )))
    print(gray(f"    workout generate --until-mesocycle {next_meso['id']}"))
    print()


def run_workout_adapt(args: argparse.Namespace) -> None:
    # Executes the daily workout Garmin adaptation checks command.
    date_str = args.date or _today_str()
    if not args.date:
        # Name the defaulted target so a bare `adapt` isn't silent (DESIGN_cli_noargs.md §b).
        print(dim(f"No date given — adapting today ({date_str})."))

    ensure_recent_data(
        date_str, no_pull=args.no_pull, force_pull=getattr(args, 'force_pull', False)
    )

    # The coach reads the full per-day trajectory (HRV/RHR/sleep/ACWR/PMC) from the same
    # window; here we only tell the athlete how many days fed the decision, not the numbers.
    try:
        history_days = getattr(args, "lookback", None) or config.metrics_lookback_days
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_date = (date_obj - timedelta(days=history_days - 1)).strftime("%Y-%m-%d")
        metrics_history = cli.db.get_metrics_cache(start_date=start_date, end_date=date_str)
        print(dim(f"\nUsing {len(metrics_history)} days of recovery metrics "
                  f"(past {history_days}-day window)."))
    except Exception as e:
        print(yellow(f"Warning: Could not load metrics trajectory: {e}"))

    _print_block_boundary_hint(date_str)

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
                    change_reason = cli.coach_service.config_changed(macro)
                    if change_reason:
                        message = (
                            yellow("Warning: a plan-shaping input has "
                                   "changed since the active periodization plan was "
                                   f"generated ({change_reason}).\n"
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
                            cli.db.update_macrocycle_config_hash(
                                macro['id'], cli.coach_service._get_config_hash(),
                                cli.coach_service._get_config_snapshot()
                            )

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
        # Benchmark identity is a stored column, orthogonal to the modification/sync/removed
        # axes (a benchmark can also be swapped), so it gets its own marker straight off the
        # column (DESIGN_benchmark_workouts.md §3.1/§6).
        bench_marker = bold(blue(" [BENCHMARK]")) if w.get('benchmark_type') else ""
        duration = w.get('duration_minutes')
        tss = w.get('tss')
        rpe = w.get('rpe')
        duration_str = f" | {duration}min" if duration else ""
        tss_str = f" | TSS {tss}" if tss is not None else ""
        rpe_str = f" | RPE {rpe}" if rpe is not None else ""
        print(
            f"ID: {w['id']} | {cyan(fmt_date(w['date']))} | {magenta(w['sport_type'].upper())} | "
            f"{bold(w['title'])}{bench_marker}{mod_marker}{sync_marker}{rem_marker}{src_marker}{duration_str}{tss_str}{rpe_str}"
        )
        # -l surfaces the Calendar event link (rebuilt from the stored event id) so it can
        # be opened without the sync commands having to print the URL every push.
        if getattr(args, "link", False):
            url = event_url(w.get('google_event_id'), config.google_calendar_id)
            print(gray(f"  Calendar: {url}") if url else gray("  Calendar: (not synced)"))
        # Default listing is one line per workout; -v adds the full per-workout detail.
        if not getattr(args, "verbose", False):
            continue
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
