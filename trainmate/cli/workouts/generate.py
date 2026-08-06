"""Workout CLI: adapt / generate / list / compare (LLM- and read-heavy)."""
import argparse
from datetime import datetime, timedelta
from typing import Optional
from trainmate import runtime
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered, format_discrepancies
from trainmate.google_calendar import event_url
from trainmate.sports import canonical_sport
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, magenta, gray, cmd, pad_visible, wrap_text,
    format_labeled_block, render_table, today_str as _today_str, today_date as _today_date,
    days_between,
)
from trainmate.cli.common import (
    fmt_date, ensure_recent_data, mark_adherence_from_results,
)

from trainmate.cli.selectors import has_selector as _has_selector, resolve_window, split_targets
from trainmate.cli.workouts._helpers import (_fmt_ts,
    _resolve_workout_end_date, workout_line)


def _print_block_boundary_hint(date_str: str) -> None:
    """Points at `workout generate` when the current block is about to end.

    Adapt cannot reach the next block, whose sessions may have been planned long ago against
    stale metrics. Prints on every run in the terminal window, not only when adaptations are
    proposed (DESIGN_block_boundary.md §4).
    """
    meso = runtime.db.get_active_mesocycle(date_str)
    if not meso:
        return
    days_left = days_between(date_str, meso['end_date'])
    if not 0 <= days_left <= config.adapt_terminal_window_days:
        return
    next_meso = runtime.db.get_next_mesocycle(meso['end_date'])
    if not next_meso:
        return

    when = "today" if days_left == 0 else f"in {days_left} day(s), on {meso['end_date']}"
    print(yellow(f"This block ({meso['name']}) ends {when}."))
    print(gray(wrap_text(
        f"Sessions in the next block ({next_meso['name']}) are outside this adaptation's "
        f"reach. To re-plan them against current metrics:"
    )))
    print(gray("    " + cmd(f"workout generate -m ..{next_meso['id']}", quote=False)))
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

    # The coach reads the full per-day trajectory (HRV/RHR/sleep/PMC) from the same
    # window; here we only tell the athlete how many days fed the decision, not the numbers.
    try:
        history_days = getattr(args, "lookback", None) or config.metrics_lookback_days
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_date = (date_obj - timedelta(days=history_days - 1)).strftime("%Y-%m-%d")
        metrics_history = runtime.db.get_metrics_cache(start_date=start_date, end_date=date_str)
        print(dim(f"\nUsing {len(metrics_history)} days of recovery metrics "
                  f"(past {history_days}-day window)."))
    except Exception as e:
        print(yellow(f"Warning: Could not load metrics trajectory: {e}"))

    _print_block_boundary_hint(date_str)

    print(f"Evaluating daily Garmin metrics adaptation for {date_str}...")
    try:
        proposal = runtime.coach_service.workout_adapt(
            date_str, message=getattr(args, 'message', None)
        )
        reason = proposal.reason
        proposed_workouts = proposal.workouts
        new_constraints = proposal.new_constraints

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
            if runtime.prompt.confirm(f"Add constraint: {title} ({span})?"):
                cid = runtime.coach_service.capture_message_constraint(candidate, date_str)
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

        def _stats(w: dict) -> str:
            return (
                f"{w.get('duration_minutes') or 0}m/"
                f"RPE{w.get('rpe') or 0}/"
                f"TSS{w.get('tss') or 0}"
            )

        # The proposal already says which planned session each change replaces; render
        # that rather than re-deriving it, so the preview cannot disagree with apply.
        for pair in proposal.pairs:
            pw, existing = pair.proposal, pair.original
            orig_title = existing['title'] if existing else "[None]"
            stats_diff = (
                f"{_stats(existing)} -> {_stats(pw)}" if existing else _stats(pw)
            )
            sport_label = (
                f"{existing['sport_type'].upper()}->{pw['sport_type'].upper()}"
                if pair.is_swap else pw['sport_type'].upper()
            )

            rows.append([
                cyan(pw['date']), magenta(sport_label), gray(orig_title),
                green(pw['title']), yellow(stats_diff),
            ])

        # Sessions being deleted outright (overridden with no replacement proposal).
        for ew in sorted(proposal.removals, key=lambda w: w['date']):
            rows.append([
                cyan(ew['date']), magenta(ew['sport_type'].upper()),
                gray(ew['title']), red("[Removed]"),
                yellow(f"{_stats(ew)} -> removed"),
            ])

        print(render_table(headers, rows))

        if args.auto:
            apply = True
        else:
            apply = runtime.prompt.confirm(
                "Apply these adaptations to your training plan and sync to Calendar?"
            )

        if apply:
            print("\nApplying adaptations...")
            runtime.coach_service.workout_adapt_apply(proposal)
            print(green("Adaptations applied and synced to calendar successfully."))
        else:
            print("\nAdaptations discarded.")

    except ValueError as e:
        # A domain refusal (no active plan to adapt towards), not a failure: say it
        # plainly. Anything else belongs to the entry point's error boundary.
        print(red(str(e)))


def _confirm_regeneration(end_date: Optional[str]) -> bool:
    """Gates the archive-and-rebuild: a regen replaces the upcoming plan, manual edits
    included, so name what is at stake before the LLM call (README §"Steering the plan").

    Returns True when there is nothing live to lose or the athlete confirmed."""
    live = runtime.db.get_workouts(start_date=_today_str())
    if not live:
        return True

    manual = sum(1 for w in live if w.get('source') == 'manual')
    hand_edited = f", {manual} added by hand" if manual else ""
    horizon = f" through {fmt_date(end_date)}" if end_date else ""
    return runtime.prompt.confirm(
        wrap_text(
            f"You already have {len(live)} upcoming workout(s) planned "
            f"({fmt_date(live[0]['date'])} → {fmt_date(live[-1]['date'])}{hand_edited}). "
            f"Regenerating archives all of them and rebuilds the plan{horizon}, at the "
            f"cost of one LLM call; {cmd('workout rollback')} restores them. Regenerate?"
        ),
        danger=True,
    )


def run_workout_generate(args: argparse.Namespace) -> None:
    """Executes the AI workout generation command based on active strategy."""
    force = getattr(args, 'force', False)
    ensure_recent_data(
        no_pull=args.no_pull, force_pull=getattr(args, 'force_pull', False)
    )

    next_goal = None
    objectives = runtime.db.upcoming_objectives()
    if objectives:
        if args.goal_id is not None:
            target_goals = [o for o in objectives if o['id'] == args.goal_id]
            next_goal = target_goals[0] if target_goals else None
        else:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]

        if next_goal:
            macro = runtime.db.get_macrocycle_for_objective(next_goal['id'])
            if macro:
                change_reason = runtime.coach_service.config_changed(macro)
                if change_reason:
                    warning = (
                        "Warning: a plan-shaping input has "
                        "changed since the active periodization plan was "
                        f"generated ({change_reason}).\n"
                        "Generating workouts using the out-of-date plan might "
                        "result in incorrect training targets.\n"
                        "It is highly recommended to run "
                        + cmd("plan generate") + " first."
                    )
                    if force:
                        print(yellow(warning + " Proceeding anyway (--force)."))
                    elif not runtime.prompt.confirm(yellow(warning + " Proceed anyway?")):
                        print(yellow(
                            "Workout generation cancelled. Please run "
                            + cmd("plan generate") + " first."
                        ))
                        return
                    else:
                        # Confirming accepts the out-of-date plan, so stamp the
                        # current config; --force only skips the question and leaves
                        # the warning live for the next run.
                        print("Proceeding. Updating configuration hash in database.")
                        runtime.db.update_macrocycle_config_hash(
                            macro['id'], runtime.coach_service._get_config_hash(),
                            runtime.coach_service._get_config_snapshot()
                        )

    workout_kwargs = {}
    if args.goal_id is not None:
        workout_kwargs['objective_id'] = args.goal_id

    end_date = _resolve_workout_end_date(args, next_goal)
    if end_date is not None:
        workout_kwargs['end_date'] = end_date

    if not force and not _confirm_regeneration(end_date):
        print(yellow("Workout generation cancelled — your current plan is unchanged."))
        return

    reasoning, workouts = runtime.coach_service.workout_generate(**workout_kwargs)
    print(bold(cyan("\n=== WORKOUTS GENERATED BY COACH ===")))
    print(f"{bold('Reasoning')}:\n{wrap_text(reasoning)}\n")
    print(green(
        f"Generated {len(workouts)} workouts starting from today and pushed them "
        "to Google Calendar."
    ))
    print(green(
        f"Run {cmd('workout rollback')} to undo this regeneration, or "
        f"{cmd('plan rollback')} to step the strategy back with it."
    ))
def _batch_line(index: int, batch: dict) -> str:
    """One `workout batches` row: '#N  <when>  <n> workouts · <span>  plan ID …'."""
    count = f"{batch['workouts']} workout(s)"
    restorable = batch.get('restorable')
    if restorable == 0:
        count += gray(" — all in the past")
    elif restorable is not None and restorable < batch['workouts']:
        count += gray(f" ({restorable} restorable)")
    macros = batch.get('macrocycle_ids') or []
    plan = f"plan ID {', '.join(str(m) for m in macros)}" if macros else "unversioned"
    span = f"{fmt_date(batch['first_date'])} → {fmt_date(batch['last_date'])}"
    return (
        f"{pad_visible(cyan(f'#{index}'), 5)} "
        f"{pad_visible(_fmt_ts(batch['archived_at']), 18)} "
        f"{pad_visible(count, 32)} {gray(span)}  {gray(plan)}"
    )


def run_workout_batches(args: argparse.Namespace) -> None:
    """Lists the archived workout batches a `workout rollback` can restore."""
    today = _today_str()
    batches = runtime.db.get_archived_batches(from_date=today)

    print(bold(cyan("\n=== ARCHIVED WORKOUT BATCHES ===")))
    if not batches:
        print(gray(
            "No archived workouts — nothing has displaced the current sessions yet."
        ))
        return
    print(gray(
        "Each batch is the set of upcoming sessions that was live when a regeneration "
        "or rollback replaced it, newest first.\n"
    ))
    for i, b in enumerate(batches, start=1):
        print(_batch_line(i, b))
    print()
    print(gray("Restore one with " + cmd("workout rollback [--batch N]")
               + " (defaults to #1). Numbering is positional and shifts after a "
                 "rollback."))


def run_workout_rollback(args: argparse.Namespace) -> None:
    """Restores an archived batch of workouts, undoing a `workout generate`."""
    today = _today_str()
    batches = runtime.db.get_archived_batches(from_date=today)
    if not batches:
        print(yellow(
            "No archived workouts to roll back to — nothing has displaced the current "
            "sessions yet."
        ))
        return

    index = getattr(args, 'batch', None) or 1
    if not 1 <= index <= len(batches):
        print(red(
            f"No archived batch #{index} — there {'is' if len(batches) == 1 else 'are'} "
            f"{len(batches)}."
        ))
        print(green(f"Run {cmd('workout batches')} to list them."))
        return
    target = batches[index - 1]

    if not target['restorable']:
        print(yellow(
            f"Batch #{index} covers {fmt_date(target['first_date'])} → "
            f"{fmt_date(target['last_date'])}, entirely in the past — there is nothing "
            "to restore."
        ))
        print(green(f"Run {cmd('workout batches')} to pick another."))
        return

    if not getattr(args, 'yes', False):
        live = len(runtime.db.get_workouts(start_date=today))
        if not runtime.prompt.confirm(
            f"Restore the {target['restorable']} upcoming workout(s) archived "
            f"{_fmt_ts(target['archived_at'])}?\nThis archives the {live} currently "
            "planned session(s) from today onward and updates Google Calendar. The "
            "active plan version is unchanged.",
            danger=True,
        ):
            print("Rollback cancelled.")
            return

    try:
        result = runtime.coach_service.workout_rollback(batch=target['archived_at'])
    except ValueError as e:
        print(red(str(e)))
        return

    span = (
        f" ({fmt_date(result['first_date'])} → {fmt_date(result['last_date'])})"
        if result['first_date'] else ""
    )
    print(green(
        f"\nRestored {result['restored_workouts']} workout(s){span} and archived "
        f"{result['archived_workouts']}; Google Calendar updated."
    ))
    print(green(f"Run {cmd('workout list')} to review the restored sessions."))
def _workouts_by_id(ids: list, sport_type: Optional[str], include_removed: bool) -> list:
    """Looks up the workout IDs named as positional targets, reporting the ones it can't."""
    found = []
    for workout_id in ids:
        w = runtime.db.get_workout_by_id(workout_id)
        if not w:
            print(yellow(f"No workout with ID {workout_id}."))
            continue
        if w.get('removed') and not include_removed:
            print(yellow(f"Workout {workout_id} is removed; pass --removed to show it."))
            continue
        if sport_type and w['sport_type'].lower() != sport_type.lower():
            continue
        found.append(w)
    return found


def run_workout_list(args: argparse.Namespace) -> None:
    """Lists stored workouts chronologically, by ID, date range, block, plan or sport."""
    ids, date_targets = split_targets(getattr(args, "targets", None))
    # Named dates narrow like any other selector; named IDs are looked up directly, since
    # an ID the athlete typed is not a window (DESIGN_cli_selectors.md §4).
    args._extra_windows = [(r.start, r.end) for r in date_targets]
    windowed = bool(date_targets) or _has_selector(args)

    include_removed = getattr(args, "removed", False)
    workouts = []
    if windowed or not ids:
        start_date, end_date = resolve_window(args)
        workouts = runtime.db.get_workouts(
            start_date=start_date,
            end_date=end_date,
            sport_type=args.sport_type,
            include_removed=include_removed,
        )
    else:
        start_date = end_date = None
    if ids:
        workouts += _workouts_by_id(ids, args.sport_type, include_removed)
        seen = set()
        workouts = [
            w for w in sorted(workouts, key=lambda w: (w['date'], w['id']))
            if not (w['id'] in seen or seen.add(w['id']))
        ]

    print(bold(cyan("=== WORKOUT SCHEDULE ===")))
    if ids:
        print(gray(f"Filters: IDs {', '.join(str(i) for i in ids)}"))
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
        print(workout_line(w))
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

    # The 14-day lookback and the backward reading of a bare span are declared on the
    # parser (direction="backward"), so this handler only caps the far end.
    start_date, end_date = resolve_window(args)

    # Cap end_date at today — we can only compare past/present activities
    if end_date is None or end_date > today_str:
        end_date = today_str

    if not getattr(args, 'no_pull', False):
        try:
            runtime.garmin.ensure_data(
                start_date, end_date, force=getattr(args, 'force_pull', False)
            )
        except Exception as e:
            print(yellow(f"Warning: Could not ensure recent data: {e}"))

    all_workouts = runtime.db.get_workouts(start_date=start_date, end_date=end_date)
    activities = runtime.db.get_completed_activities(start_date=start_date, end_date=end_date)

    start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
    history_days = (end_date_obj - start_date_obj).days + 1

    # Planned blocks overlapping the window: an activity on a date outside every block
    # is history no plan governed, shown as informational rather than "unplanned".
    covered_ranges = runtime.db.get_mesocycle_ranges(start_date, end_date)

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
        parts: list[str] = [dur, f"load {runtime.garmin.activity_load(act):.0f}"]
        if act.get('tss'):
            parts.append(f"TSS {act['tss']:.0f}")
        if act.get('rpe'):
            parts.append(f"RPE {act['rpe']}")
        s = f"[{act['activity_type']}] {act['activity_name']} ({', '.join(parts)})"
        div = runtime.garmin.rpe_divergence(act)
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
            act_load = runtime.garmin.activity_load(act)
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
        for line in format_discrepancies(discrepancies):
            print(yellow(line))
        print()
        n = len(discrepancies)
        print(bold(yellow(f"{n} discrepanc{'ies' if n != 1 else 'y'} found.")))
    else:
        print(bold(green("No discrepancies found. Great adherence!")))

    if informational:
        print()
        print(bold(gray("=== OUTSIDE ANY PLAN (informational) ===")))
        for act in informational:
            print(gray(f"- {act['date']}: {_fmt_act(act)}"))

    if not getattr(args, 'no_mark', False) and config.google_calendar_id:
        marked = mark_adherence_from_results(matching_results, today_str)
        print()
        if marked:
            print(green(f"Marked {marked} past event(s) on Calendar with adherence."))
        else:
            print(gray("Calendar adherence already up to date for this range."))
