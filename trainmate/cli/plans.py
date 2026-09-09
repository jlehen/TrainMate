import textwrap
import argparse
import sys
from datetime import datetime, timedelta
from typing import List, Optional
from trainmate import runtime
from trainmate import plan_diff
from trainmate.adherence import planned_load
from trainmate.util import (
    aside, step, bold, green, red, yellow, cyan, blue, magenta, gray, cmd, visible_len,
    pad_visible, wrap_text, format_labeled_block, default_wrap_width, fmt_date, fmt_span,
    today_date as _today_date, notice, warn,
)
from trainmate.cli import staleness
from trainmate.cli.common import (
    ensure_recent_data, print_plan_cascade, report_unhonored,
)
from trainmate.cli.selectors import (
    CURRENT, IdRange, SelectorError, parse_id_range, resolve_meso_atom,
)


def _resolve_goal(goal_id: Optional[int]) -> Optional[dict]:
    """The goal a plan command targets: the given ID (whatever its status, so archived
    and completed goals stay reachable), else the next active goal by target date.
    Prints the reason and returns None when there is none."""
    if goal_id is not None:
        goal = runtime.db.get_objective(goal_id)
        if not goal:
            notice(f"Goal with ID {goal_id} not found.", red)
        return goal
    objectives = runtime.db.upcoming_objectives()
    if not objectives:
        notice("No active goals found. TrainMate needs at least one goal.")
        return None
    objectives.sort(key=lambda x: str(x['target_date']))
    return objectives[0]


def _goal_span_start(goal: Optional[dict]) -> Optional[str]:
    """The first day of a goal's OWN span: the day after the goal before it, never
    earlier than today (DESIGN_cli_selectors.md §9)."""
    if not goal:
        return None
    today = _today_date().strftime("%Y-%m-%d")
    preceding = runtime.db.get_preceding_objectives(goal['target_date'])
    if not preceding:
        return today
    day_after = (
        datetime.strptime(preceding[0]['target_date'], "%Y-%m-%d").date()
        + timedelta(days=1)
    ).strftime("%Y-%m-%d")
    return max(day_after, today)


def _goals_in_range(rng) -> Optional[list]:
    """Every upcoming goal a `-g` range covers, chronologically.

    The range runs over the goal TIMELINE rather than over row IDs, so `..2` is every goal
    falling on or before goal 2's target date (DESIGN_cli_selectors.md §9). None when an
    ID does not resolve — the reason is printed here."""
    bounds = []
    for goal_id in (rng.start, rng.end):
        if goal_id is None:
            bounds.append(None)
            continue
        goal = runtime.db.get_objective(goal_id)
        if not goal:
            notice(f"Goal with ID {goal_id} not found.", red)
            return None
        bounds.append(str(goal['target_date']))
    start, end = bounds
    goals = [
        g for g in runtime.db.upcoming_objectives()
        if (start is None or str(g['target_date']) >= start)
        and (end is None or str(g['target_date']) <= end)
    ]
    goals.sort(key=lambda g: (str(g['target_date']), g['id']))
    return goals


def goal_range_for_window(start: str, end: str) -> Optional[IdRange]:
    """The `-g` selector naming every upcoming goal whose own span overlaps [start, end].

    What a constraint-triggered replan targets: the plans that actually cover the
    disrupted days, never the next goal on the calendar (DESIGN_constraints.md §7).
    None when no goal's span holds any of them — a window wholly behind us, or one
    dated past the last goal — where there is no plan to reshape.

    Goals partition the timeline, so the overlap is contiguous and an IdRange over its
    two ends re-derives exactly this set through the shared grammar (§9).
    """
    overlapping = [
        g for g in runtime.db.upcoming_objectives()
        if str(g['target_date']) >= start and str(_goal_span_start(g)) <= end
    ]
    if not overlapping:
        return None
    overlapping.sort(key=lambda g: (str(g['target_date']), g['id']))
    return IdRange(start=overlapping[0]['id'], end=overlapping[-1]['id'])


def _plan_targets(args: argparse.Namespace) -> Optional[list]:
    """The goals `plan generate` plans for, chronologically, each paired with the day its
    own plan window opens.

    `-g` reads as the shared range grammar does (DESIGN_cli_selectors.md §9): one goal
    named plans that goal alone, a range plans every goal it covers — `-g ..2` is
    "everything through goal 2", which is one strategy call per goal falling in it. The
    no-flag default is `(None, None)`: the service picks the next goal itself. None when
    the selection resolves to nothing, with the reason already printed."""
    rng = getattr(args, "goal_range", None)
    if rng is None:
        return [(None, None)]
    if rng.current:
        goal = runtime.db.get_active_objective()
        return [((goal['id'] if goal else None), _goal_span_start(goal))]
    if rng.start is not None and rng.start == rng.end:
        # One ID reaches any non-archived goal, a past one included, as it always did —
        # only a range is restricted to what is still ahead.
        goal = runtime.db.get_objective(rng.start)
        if not goal:
            notice(f"Goal with ID {rng.start} not found.", red)
            return None
        return [(goal['id'], _goal_span_start(goal))]
    goals = _goals_in_range(rng)
    if goals is None:
        return None
    if not goals:
        notice("No upcoming goal falls in that range — nothing to plan.")
        return None
    return [(g['id'], _goal_span_start(g)) for g in goals]


def _announce_targets(targets: list) -> None:
    """Names the goals a range resolved to, and their order, before the first strategy
    call is spent (DESIGN_cli_selectors.md §9)."""
    named = []
    for goal_id, _ in targets:
        goal = runtime.db.get_objective(goal_id) if goal_id is not None else None
        if goal:
            named.append(f"{goal['title']} ({fmt_date(goal['target_date'])})")
    step(wrap_text(
        f"Planning {len(targets)} goals in date order, one strategy call each: "
        + "; ".join(named) + "."
    ))


def run_plan_generate(args: argparse.Namespace) -> None:
    """Executes the AI periodization strategy plan generation command."""
    # A clean slate is a regeneration by definition, so the staleness question below —
    # "an input changed, regenerate?" — is already answered.
    if args.fresh:
        args.force = True

    # Resolved before the Garmin pull, so an unknown goal ID fails without one.
    targets = _plan_targets(args)
    if targets is None:
        return

    # Make sure we have latest metrics cached
    ensure_recent_data(no_pull=args.no_pull, force_pull=getattr(args, 'force_pull', False))
    metrics = runtime.db.get_metrics_cache()
    if not metrics:
        warn("metrics cache is empty. Proceeding without Garmin metrics.")
        
    # First-run nudge: no reflect watermark means `data bootstrap` has never run, so
    # there are no history-derived coach learnings to inform the plan. Offer to seed
    # them before generating (skipped in non-interactive --auto mode).
    if runtime.db.get_sync_state("reflect") is None and not getattr(args, 'auto', False):
        if runtime.prompt.confirm(wrap_text(
            f"No training-history analysis found. Run {cmd('data bootstrap')} first "
            "to reconstruct past cycles and seed coach learnings?"
        )):
            runtime.coach_service.data_bootstrap(
                no_pull=args.no_pull, force_pull=getattr(args, 'force_pull', False)
            )

    if len(targets) == 1:
        _generate_one_plan(args, *targets[0])
        return

    # Each goal is its own strategy, its own preview and its own decision: declining one
    # does not stop the next, whose window is bounded by the goal dates either way (§9).
    _announce_targets(targets)
    for index, (goal_id, span_start) in enumerate(targets, start=1):
        goal = runtime.db.get_objective(goal_id) if goal_id is not None else None
        title = goal['title'] if goal else f"goal {goal_id}"
        print(bold(cyan(f"\n=== PLANNING {index}/{len(targets)}: {title} ===")))
        _generate_one_plan(args, goal_id, span_start)


def _generate_one_plan(
    args: argparse.Namespace, goal_id: Optional[int], span_start: Optional[str]
) -> None:
    """One goal's strategy: the staleness gate, the LLM call, the preview and the save.

    `force` is a local because the staleness gate raises it, and in a range that answer
    belongs to the goal it was asked about, not to the ones planned after it."""
    force = bool(args.force)

    # Bound before the branch: with no upcoming objectives the accept path below
    # still reads it, and an unbound name surfaced only as a NameError string.
    next_goal = None
    objectives = runtime.db.upcoming_objectives()
    if objectives:
        if goal_id is not None:
            target_goals = [o for o in objectives if o['id'] == goal_id]
            next_goal = target_goals[0] if target_goals else None
        else:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]
            # Name the defaulted goal so a bare `plan generate` isn't silent
            # about which objective it planned for (DESIGN_cli_noargs.md §b).
            step(wrap_text(
                f"No goal given — planning for your next goal: "
                f"{next_goal.get('title', '')} on {fmt_date(next_goal['target_date'])}."
            ))

        if next_goal:
            macro = runtime.db.get_macrocycle_for_objective(next_goal['id'])
            if macro:
                change_reason = staleness.reason(macro)
                if change_reason and not force:
                    if staleness.confirm_regenerate(change_reason, macro):
                        force = True
                    else:
                        print(wrap_text(staleness.kept_line()))
                        staleness.stamp(macro)

    plan_kwargs = {'auto_apply': False}
    if goal_id is not None:
        plan_kwargs['objective_id'] = goal_id
    if span_start is not None:
        plan_kwargs['start_date'] = span_start
    proposal = runtime.coach_service.plan_generate(
        force=force, fresh=bool(args.fresh),
        show_context=getattr(args, 'show_llm_context', False), **plan_kwargs
    )
    mesocycles = proposal['mesocycles']

    if proposal['reused']:
        print(green(f"\nActive plan is up to date ({len(mesocycles)} mesocycles)."))
        return

    if getattr(args, 'auto', False):
        apply = True
    else:
        apply = runtime.prompt.confirm("Apply this new periodization strategy?")

    if apply:
        goal = proposal['goal'] or next_goal
        # plan_apply already no-ops on a missing goal, so let it own that decision
        # rather than re-deciding here, and report what it actually saved.
        # Pass the fingerprints taken when the strategy was generated: the athlete
        # may have edited a goal while reading the proposal, and recording that edit
        # as part of this plan would mark a stale plan current.
        saved_id = runtime.coach_service.plan_apply(
            goal['id'] if goal else None, proposal['strategy'], mesocycles,
            fingerprints=proposal.get('fingerprints'),
        )
        if saved_id is None:
            notice("\nNo goal to attach this plan to — nothing was saved.")
            return
        print(green(f"\nGenerated {len(mesocycles)} mesocycles. Save complete."))
        print(green(f"Run {cmd('workout generate')} to schedule workouts "
                    "based on this plan."))
    else:
        notice("\nPlan discarded.")



def _print_hanging(head: str, text: str, width: int, color_fn=None) -> str:
    """Prints ``text`` after ``head``, wrapped with continuation lines aligned under it.

    Returns the indent string so the caller can align the block's follow-up lines
    (dates, progress bar, prose) to the same column. A narrow client gets a plain
    2-space indent instead, since aligning under a long head leaves no usable width."""
    pad_len = visible_len(head)
    if width - pad_len < 24:
        pad_len = 2
    pad = " " * pad_len
    lines = textwrap.wrap(text or "", width=max(20, width - pad_len)) or [""]
    for i, line in enumerate(lines):
        print((head if i == 0 else pad) + (color_fn(line) if color_fn else line))
    return pad


def _print_indented(text: str, pad: str, width: int, color_fn=None) -> None:
    """Prints wrapped prose at an existing block's indent."""
    for line in textwrap.wrap(text, width=max(20, width - len(pad))):
        print(pad + (color_fn(line) if color_fn else line))


def _print_segments(pad: str, segments: list, width: int) -> None:
    """Prints already-coloured metadata segments joined by ' · ', breaking onto a new
    indented line rather than letting the terminal wrap them mid-word."""
    avail = max(20, width - len(pad))
    line = ""
    for seg in segments:
        if not seg:
            continue
        candidate = f"{line} · {seg}" if line else seg
        if line and visible_len(candidate) > avail:
            print(pad + line)
            line = seg
        else:
            line = candidate
    if line:
        print(pad + line)


def _print_feedback_notes(notes: List[dict], width: int, indent: str = "") -> None:
    """The log's one rendering — '[id] date · plan-level|<block> · text', oldest first —
    shared by the listing, `plan show` and `plan diff` (DESIGN_plan_feedback.md §4)."""
    for n in notes:
        filing = n.get('mesocycle_name') or 'plan-level'
        date = str(n.get('created_at') or '')[:10]
        _print_hanging(
            f"{indent}{gray('[' + str(n['id']) + ']')} {cyan(fmt_date(date))} "
            f"· {magenta(filing)} · ",
            n['text'], width,
        )


def _print_plan_feedback(macrocycle: dict, width: int) -> None:
    """The notes attached to the shown plan version (DESIGN_plan_feedback.md §8)."""
    notes = runtime.db.list_plan_feedback(macrocycle['id'])
    if not notes:
        return
    state = (
        "consumed by the successor version"
        if macrocycle.get('status') == 'superseded'
        else "pending — feeds the next " + cmd('plan generate')
    )
    print(bold("Athlete Feedback") + f" ({gray(state)}):")
    _print_feedback_notes(notes, width, indent="  ")
    print()


def _print_constraint_entry(e: dict, width: int) -> None:
    """One constraint line under a "Constraints considered" heading.

    Snapshots are historical JSON, so tolerate three shapes: the current one (a `rest`
    flag), the pre-rev-6 constraint (binding/sport/type), and the original lifeevent
    (event_type/impact_description). Read whichever is present.
    """
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
    pad = _print_hanging(
        f"  - [Constraint ID: {e.get('id')}] ", e.get('title', ''), width, cyan
    )
    _print_segments(
        pad,
        [
            tags,
            f"{cyan(fmt_date(e.get('start_date')))} -> "
            f"{cyan(fmt_date(e.get('end_date')))}",
        ],
        width,
    )
    detail = e.get('description') or e.get('impact_description')
    if detail:
        _print_indented(detail, pad, width, gray)


def _print_considered_inputs(macrocycle: dict) -> None:
    """Prints the goals, constraints and threshold anchors the plan was generated from."""
    goals, events, all_events, thresholds = plan_diff.input_snapshots(macrocycle)
    if goals is None and events is None and thresholds is None:
        print(gray("Inputs considered: not recorded (plan predates input snapshots)."))
        print()
        return

    goals, events = goals or [], events or []
    width = default_wrap_width()

    print(bold("Goals considered:"))
    if goals:
        for g in goals:
            sport = (g.get('sport_type') or '').upper()
            pad = _print_hanging(
                f"  - [Goal ID: {g.get('id')}] ", g.get('title', ''), width, cyan
            )
            _print_segments(
                pad,
                [
                    magenta(sport),
                    cyan(fmt_date(g.get('target_date'))),
                ],
                width,
            )
            if g.get('description'):
                _print_indented(g['description'], pad, width, gray)
    else:
        print(f"  {gray('None')}")

    # `events` is only the replan=1 subset; `all_events` is every constraint the prompt
    # actually saw. Split the latter so an active advisory one is never silently dropped
    # from the render just because it didn't trigger a replan (DESIGN_constraints.md §7).
    tactical = [e for e in all_events if not e.get('replan')] if all_events is not None else None

    print(bold("Constraints considered (plan-shaping):"))
    if events:
        for e in events:
            _print_constraint_entry(e, width)
    else:
        print(f"  {gray('None')}")

    if tactical is not None:
        print(bold("Also active (tactical — did not trigger replan):"))
        if tactical:
            for e in tactical:
                _print_constraint_entry(e, width)
        else:
            print(f"  {gray('None')}")

    # The effective threshold anchors the plan prescribed against; drift past
    # `coach.threshold_replan_pct` is what makes it stale (ARCHITECTURE §5, macrocycles).
    if thresholds is not None:
        print(bold("Thresholds considered:"))
        if thresholds:
            for key in sorted(thresholds):
                print(f"  - {key}: {cyan(f'{thresholds[key]:g}')}")
        else:
            print(f"  {gray('None recorded')}")
    print()


def _fmt_duration(minutes: float) -> str:
    """'8h20' / '45min' for a workout-block total."""
    if minutes >= 60:
        return f"{int(minutes // 60)}h{int(minutes % 60):02d}"
    return f"{int(minutes)}min"


def _plan_workouts(macrocycle: dict) -> List[dict]:
    """Every revision the given plan version appended, live or since superseded.

    History rather than plan, so it reads the raw revisions instead of the live view: the
    question is what this version scheduled, including what a later one displaced
    (DESIGN_workout_revisions.md §5). Revisions carry the `macrocycle_id` of the version
    that created them; a database wholly predating that column has none, so its rows are
    matched on dates alone."""
    rows = runtime.db.get_plan_revisions()
    if any(w.get('macrocycle_id') is not None for w in rows):
        return [w for w in rows if w.get('macrocycle_id') == macrocycle['id']]
    return rows


def _print_mesocycle_workouts(
    meso: dict, workouts: List[dict], pad: str, width: int, detail: bool
) -> None:
    """Summarises (and with `detail`, lists) the workouts falling inside a mesocycle."""
    inside = [w for w in workouts if meso['start_date'] <= w['date'] <= meso['end_date']]
    if not inside:
        print(pad + gray("no workouts generated"))
        return
    minutes = sum(w.get('duration_minutes') or 0 for w in inside)
    load = sum(planned_load(w) for w in inside)
    print(pad + gray(
        f"{len(inside)} workouts · {_fmt_duration(minutes)} · load {load:.0f}"
    ))
    if not detail:
        return
    for w in inside:
        tail = [f"{w.get('duration_minutes') or 0:.0f}min", f"load {planned_load(w):.0f}"]
        if not w.get('live'):
            tail.append("superseded")
        elif w.get('void'):
            tail.append("cancelled")
        _print_hanging(
            f"{pad}  {cyan(fmt_date(w['date']))} ",
            f"[{w['sport_type']}] {w.get('title') or ''} ({' · '.join(tail)})", width, gray,
        )


def run_plan_show(args: argparse.Namespace) -> None:
    """Displays the training macrocycle(s) and mesocycles periodization timeline."""
    # The empty state `plan show` alone owns. `_resolve_goal` below would say the same
    # thing one line later, but it also serves `plan versions`, `plan diff`, `plan
    # rollback` and `plan feedback`, where the companion sentence is the wrong one
    # (DESIGN_render_persona.md §5).
    if (args.goal_id is None and not getattr(args, 'all', False)
            and not runtime.db.upcoming_objectives()):
        runtime.render.no_upcoming_goal()
        return
    if getattr(args, 'all', False):
        if args.goal_id is not None or getattr(args, 'macrocycle_id', None) is not None:
            notice("Error: --all cannot be combined with --goal or --macrocycle.", red)
            return
        goals = sorted(runtime.db.get_objectives(), key=lambda g: str(g['target_date']))
        planned = [(g, runtime.db.get_macrocycle_for_objective(g['id'])) for g in goals]
        planned = [(g, m) for g, m in planned if m]
        if not planned:
            notice("No goal has a periodization plan yet.")
            print(green(f"Run {cmd('plan generate')} to create one."))
            return
        for goal, macrocycle in planned:
            runtime.render.plan(goal, macrocycle, args)
        return

    next_goal = _resolve_goal(args.goal_id)
    if not next_goal:
        return

    version_id = getattr(args, 'macrocycle_id', None)
    if version_id is not None:
        macrocycle = runtime.db.get_macrocycle(version_id)
        if not macrocycle or macrocycle.get('objective_id') != next_goal['id']:
            notice(
                f"Plan version {version_id} does not belong to goal '{next_goal['title']}'.", red,
            )
            print(green(f"Run {cmd('plan versions')} to list this goal's plan versions."))
            return
    else:
        macrocycle = runtime.db.get_macrocycle_for_objective(next_goal['id'])
    if not macrocycle:
        runtime.render.no_plan_yet(next_goal)
        return

    runtime.render.plan(next_goal, macrocycle, args)


def print_plan(next_goal: dict, macrocycle: dict, args: argparse.Namespace) -> None:
    """Renders one plan version: header, strategy, snapshotted inputs, mesocycle timeline.

    The companion form of this is CompanionRenderer.plan (DESIGN_render_persona.md §5)."""
    mesocycles = runtime.db.get_mesocycles_for_macrocycle(macrocycle['id'])
    show_workouts = getattr(args, 'workouts', False)
    workouts = _plan_workouts(macrocycle)

    is_superseded = macrocycle.get('status') == 'superseded'
    if is_superseded:
        superseded_on = str(macrocycle.get('superseded_at', ''))[:10]
        header = (
            f"=== SUPERSEDED MACROCYCLE STRATEGY [Macrocycle ID: {macrocycle['id']}]"
            + (f" superseded {fmt_date(superseded_on)}" if superseded_on else "")
            + " ==="
        )
        print(bold(yellow("\n" + header)))
        notice(
            "This is a past version, kept for rollback. Run "
            + cmd(f"plan rollback --macrocycle {macrocycle['id']}") + " to restore it.",
        )
    else:
        print(bold(cyan(
            f"\n=== ACTIVE MACROCYCLE STRATEGY [Macrocycle ID: {macrocycle['id']}] ==="
        )))
    width = default_wrap_width()
    sport_str = next_goal['sport_type'].upper()
    obj_pad = _print_hanging(
        f"{bold('Planned for')} [Goal ID: {next_goal['id']}]: ",
        next_goal['title'], width, cyan,
    )
    _print_segments(
        obj_pad,
        [magenta(sport_str), cyan(fmt_date(next_goal['target_date']))],
        width,
    )
    print(format_labeled_block(f"{bold('Macrocycle Strategy')}:", macrocycle['strategy']))
    print()
    _print_plan_feedback(macrocycle, width)
    _print_considered_inputs(macrocycle)
    # Right under the inputs it contradicts, and only for the version in force: a
    # superseded one is out of date by definition (DESIGN_plan_staleness.md §9). Expert
    # only — the companion body is `simple_plan_lines`, and a replan is operator work.
    if not is_superseded:
        change_reason = staleness.reason(macrocycle)
        if change_reason:
            staleness.report(change_reason, macrocycle)
    print(bold("Mesocycle Timeline:"))
    
    today = _today_date()
    
    for m in mesocycles:
        start = datetime.strptime(m['start_date'], "%Y-%m-%d").date()
        end = datetime.strptime(m['end_date'], "%Y-%m-%d").date()
        
        total_days = (end - start).days + 1
        if total_days <= 0:
            total_days = 1
            
        # The bar shares its line with the day counter, so it has to shrink on a
        # narrow client rather than pushing the counter past the wrap width.
        bar_length = min(20, max(8, width - 30))
        is_active = start <= today <= end
        if end < today:
            status_str = gray("[DONE]  ")
            bar = gray("=" * bar_length)
            extra = ""
        elif is_active:
            status_str = green("[ACTIVE]")
            days_passed = (today - start).days + 1
            days_passed = max(1, min(days_passed, total_days))
            filled = round(bar_length * days_passed / total_days)
            filled = max(0, min(filled, bar_length))
            bar = green("=" * filled) + gray("." * (bar_length - filled))
            extra = green(f" Day {days_passed}/{total_days}")
        else:
            status_str = blue("[FUTURE]")
            bar = gray("." * bar_length)
            extra = ""

        if total_days >= 7:
            weeks = total_days / 7
            if weeks.is_integer():
                duration_desc = f"{int(weeks)} weeks"
            else:
                duration_desc = f"{weeks:.1f} weeks"
        else:
            duration_desc = f"{total_days} days"

        prefix = green("|->") if is_active else "|--"
        pad = _print_hanging(
            f"{prefix} {status_str} ", m['name'], width, green if is_active else None
        )
        _print_segments(
            pad,
            [
                f"[Mesocycle ID: {m['id']}]",
                f"{cyan(fmt_date(m['start_date']))} -> {cyan(fmt_date(m['end_date']))}",
                duration_desc,
                (f"phase {m['phase']}" if m.get('phase') else ""),
            ],
            width,
        )
        print(f"{pad}[{bar}]{extra}")
        _print_mesocycle_workouts(m, workouts, pad, width, show_workouts)
        _print_indented(m['focus'], pad, width)
        print(pad + gray("-" * min(40, max(10, width - len(pad)))))


def run_plan_keep(args: argparse.Namespace) -> None:
    """Records the current inputs against the active plan, without regenerating it: the
    "that was a wording tweak" answer, reachable without a strategy call
    (DESIGN_plan_staleness.md §9)."""
    goal = _resolve_goal(getattr(args, 'goal_id', None))
    if not goal:
        return

    macrocycle = runtime.db.get_macrocycle_for_objective(goal['id'])
    if not macrocycle:
        runtime.render.no_plan_yet(goal)
        return

    change_reason = staleness.reason(macrocycle)
    if not change_reason:
        notice("This plan already reflects your current inputs — nothing to keep.")
        return

    print(f"\n{bold('Changed since this plan was generated')}: {change_reason}")
    staleness.print_diff(macrocycle)
    print(green(wrap_text(staleness.kept_line())))
    staleness.stamp(macrocycle)
    print(gray(wrap_text(
        f"Your next {cmd('workout generate')} still picks the change up."
    )))


def run_plan_versions(args: argparse.Namespace) -> None:
    """Lists every periodization plan version (active + superseded) for a goal."""
    goal = _resolve_goal(getattr(args, 'goal_id', None))
    if not goal:
        return

    versions = runtime.db.get_macrocycle_versions(goal['id'])
    if not versions:
        notice(f"No periodization plan exists for goal '{goal['title']}'.")
        print(green(f"Run {cmd('plan generate')} to create one."))
        return

    sport_str = goal['sport_type'].upper()
    goal_tag = bold(f"[Goal ID: {goal['id']}]")
    print(bold(cyan("\n=== PLAN VERSIONS ===")))
    print(
        f"{goal_tag}: "
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
        id_str = (green if active else str)(f"Macrocycle {v['id']}")
        if active:
            status = green("active")
        else:
            superseded = str(v.get('superseded_at', ''))[:10]
            status = gray("superseded" + (f" {fmt_date(superseded)}" if superseded else ""))
        gen = f"generated {fmt_date(created)}" if created else ""
        print(
            f"{marker} {pad_visible(id_str, 16)} {pad_visible(status, 28)} {gray(gen)}"
        )
        if excerpt:
            print(f"    {gray(excerpt)}")
    print()
    aside(
        "Restore a version with " + cmd("plan rollback --macrocycle <ID>")
        + ", inspect one with " + cmd("plan show --macrocycle <ID>")
        + ", or compare two with " + cmd("plan diff <ID> <ID>") + ".",
        color_fn=gray,
    )


def _print_change(marker: str, text: str, width: int, color_fn, indent: str = "  ") -> None:
    """One '+'/'-'/'~' diff line, wrapped with its continuation aligned past the marker."""
    _print_hanging(f"{indent}{color_fn(marker)} ", text, width, color_fn)


def _print_prose_diff(
    prose: dict, width: int, full: bool, indent: str = "  "
) -> None:
    """Renders a `plan_diff.diff_prose` result. A block the coach rewrote wholesale
    collapses to a one-line note unless `full` — the sentence lists would otherwise just
    reprint both versions in their entirety."""
    if not prose['changed']:
        print(f"{indent}{gray('unchanged')}")
        return
    if prose['rewritten'] and not full:
        _print_change(
            "~", f"rewritten ({prose['old_count']} sentences -> {prose['new_count']}); "
            f"pass --full for the sentence-level diff", width, yellow, indent=indent,
        )
        return
    for block in prose['blocks']:
        for s in block['removed']:
            _print_change("-", s, width, red, indent=indent)
        for s in block['added']:
            _print_change("+", s, width, green, indent=indent)


def _print_mesocycles_diff(entries: list, width: int, full: bool) -> None:
    """Renders a `plan_diff.diff_mesocycles` result, skipping untouched blocks."""
    changed = [e for e in entries if e['change'] != 'unchanged']
    if not changed:
        print(f"  {gray('unchanged')}")
        return
    for e in changed:
        if e['change'] in ('added', 'removed'):
            added = e['change'] == 'added'
            label = (
                f"{e['name']} "
                f"({fmt_span(e['dates']['start'], e['dates']['end'], sep=' -> ')})"
            )
            _print_change("+" if added else "-", label, width, green if added else red)
            continue
        header = f"{e['from_name']}  =>  {e['name']}" if e['renamed'] else e['name']
        _print_change("~", header, width, yellow)
        if e['dates']:
            frm, to = e['dates']['from'], e['dates']['to']
            print(f"      dates {cyan(fmt_date(frm['start']))} -> {cyan(fmt_date(frm['end']))}"
                  f"\n         =>  {cyan(fmt_date(to['start']))} -> {cyan(fmt_date(to['end']))}")
        for f in e['fields']:
            print(f"      {f['field']}: {f['from'] or '—'}  =>  {f['to'] or '—'}")
        if e['focus']:
            print(f"      {gray('focus:')}")
            _print_prose_diff(e['focus'], width, full, indent="        ")


def _print_feedback_diff(entry: dict, width: int) -> None:
    """Renders a `plan_diff.diff_feedback` result: each version's own notes. Append-only
    logs are not prose-diffed — for adjacent versions, A's notes are what drove B (§8)."""
    for tag, notes in (("A", entry['from']), ("B", entry['to'])):
        print(f"  {bold(tag)}:")
        if not notes:
            print(f"    {gray('none')}")
            continue
        for n in notes:
            _print_hanging(
                f"    {gray('[' + str(n['id']) + ']')} {cyan(fmt_date(n['date']))} "
                f"· {magenta(n['filing'] or 'plan-level')} · ",
                n['text'], width,
            )


def _print_missing_snapshot(missing: str) -> None:
    """Says which side lacks the snapshot, so a plan predating the column is never read
    as everything having been added or removed."""
    if missing == "both":
        print(f"  {gray('not recorded on either version')}")
        return
    side = "A" if missing == "old" else "B"
    print(f"  {gray(f'not recorded on {side} — that plan predates the snapshot')}")


def _print_records_diff(diff: dict, width: int, kind: str) -> None:
    """Renders a `plan_diff.diff_records` result (snapshotted goals or constraints).

    `kind` names the entity in each ID tag — both kinds share one screen."""
    if diff['missing']:
        _print_missing_snapshot(diff['missing'])
        return
    if not (diff['added'] or diff['removed'] or diff['changed']):
        print(f"  {gray('unchanged')}")
        return
    for rec in diff['removed']:
        _print_change(
            "-", f"[{kind} ID: {rec.get('id')}] {rec.get('title', '')}", width, red
        )
    for rec in diff['added']:
        _print_change(
            "+", f"[{kind} ID: {rec.get('id')}] {rec.get('title', '')}", width, green
        )
    for rec in diff['changed']:
        _print_change("~", f"[{kind} ID: {rec['id']}] {rec['title']}", width, yellow)
        for f in rec['fields']:
            print(f"      {f['field']}: {f['from']!r}  =>  {f['to']!r}")


def _print_thresholds_diff(diff: dict, width: int) -> None:
    """Renders a `plan_diff.diff_thresholds` result."""
    if diff['missing']:
        _print_missing_snapshot(diff['missing'])
        return
    if not (diff['added'] or diff['removed'] or diff['changed']):
        print(f"  {gray('unchanged')}")
        return
    for t in diff['removed']:
        _print_change("-", f"{t['key']}: {t['value']:g}", width, red)
    for t in diff['added']:
        _print_change("+", f"{t['key']}: {t['value']:g}", width, green)
    for t in diff['changed']:
        pct = f" ({t['pct']:+.1f}%)" if t['pct'] is not None else ""
        _print_change("~", f"{t['key']}: {t['from']:g}  =>  {t['to']:g}{pct}", width, yellow)


def _version_line(tag: str, macro: dict) -> str:
    """'A  Macrocycle 12  generated 2026-07-29 Wed  superseded 2026-07-31 Fri', for a
    diff header."""
    created = str(macro.get('created_at', ''))[:10]
    if macro.get('status') == 'superseded':
        superseded = str(macro.get('superseded_at', ''))[:10]
        state = gray("superseded" + (f" {fmt_date(superseded)}" if superseded else ""))
    else:
        state = green("active")
    gen = f"generated {fmt_date(created)}" if created else ""
    label = pad_visible('Macrocycle ' + str(macro['id']), 16)
    return f"  {bold(tag)}  {label} {gray(gen)}  {state}"


# The command that gets the athlete unstuck, per failure the version resolver reports.
_DIFF_ERROR_HINTS = {
    "no_active_plan": ("plan generate", "to create one."),
    "not_found": ("plan versions", "to list this goal's plan versions."),
}


def run_plan_diff(args: argparse.Namespace) -> None:
    """Compares two periodization plan versions field by field."""
    goal = _resolve_goal(getattr(args, 'goal_id', None))
    if not goal:
        return
    old, new, error = plan_diff.resolve_versions(
        runtime.db, goal, args.version_a, args.version_b
    )
    if error:
        code, message = error
        print(red(message) if code in ("same_version", "not_found") else yellow(message))
        hint = _DIFF_ERROR_HINTS.get(code)
        if hint:
            print(green(f"Run {cmd(hint[0])} {hint[1]}"))
        return

    width = default_wrap_width()
    print(bold(cyan("\n=== PLAN DIFF ===")))
    goal_tag = bold(f"[Goal ID: {goal['id']}]")
    obj_pad = _print_hanging(
        f"{goal_tag}: ", goal['title'], width, cyan,
    )
    _print_segments(
        obj_pad,
        [magenta(goal['sport_type'].upper()), cyan(fmt_date(goal['target_date']))],
        width,
    )
    print(_version_line("A", old))
    print(_version_line("B", new))

    diff = plan_diff.diff_plans(
        old, new,
        runtime.db.get_mesocycles_for_macrocycle(old['id']),
        runtime.db.get_mesocycles_for_macrocycle(new['id']),
        runtime.db.list_plan_feedback(old['id']),
        runtime.db.list_plan_feedback(new['id']),
    )
    full = getattr(args, 'full', False)
    print(bold("\nStrategy:"))
    _print_prose_diff(diff['strategy'], width, full)
    print(bold("\nAthlete feedback:"))
    _print_feedback_diff(diff['feedback'], width)
    print(bold("\nMesocycles:"))
    _print_mesocycles_diff(diff['mesocycles'], width, full)
    print(bold("\nGoals considered:"))
    _print_records_diff(diff['goals'], width, "Goal")
    print(bold("\nConstraints considered:"))
    _print_records_diff(diff['constraints'], width, "Constraint")
    print(bold("\nThresholds considered:"))
    _print_thresholds_diff(diff['thresholds'], width)
    print()


def run_plan_rm(args: argparse.Namespace) -> None:
    """Deletes every periodization plan version a goal owns, superseded ones included.

    The inventory is printed first because the cascade reaches past the one version the
    athlete has in mind (DESIGN_cli_noargs.md §b1)."""
    goal = runtime.db.get_objective(args.id)
    if not goal:
        notice(f"Goal with ID {args.id} not found.", red)
        return

    macro = runtime.db.get_macrocycle_for_objective(args.id)
    if not macro:
        notice(f"No periodization plan exists for goal '{goal['title']}' (ID {args.id}).")
        return

    if not args.yes:
        notice(f"Removing the plan for goal '{goal['title']}' (ID {args.id}) deletes:")
        print_plan_cascade(args.id)
        print(gray(
            "To replace the plan reversibly instead, use "
            + cmd(f"plan generate --goal {args.id} --force") + "."
        ))
        if not runtime.prompt.confirm("Delete it anyway?", danger=True):
            print("Removal cancelled.")
            return

    runtime.coach_service.plan_rm(args.id)
    print(green(f"Periodization plan for goal '{goal['title']}' removed successfully."))

    # Warn about subsequent plans
    objectives = runtime.db.upcoming_objectives()
    subsequent_goals_with_plans = []
    for obj in objectives:
        if str(obj['target_date']) > str(goal['target_date']):
            if obj['id'] is not None:
                sub_macro = runtime.db.get_macrocycle_for_objective(obj['id'])
                if sub_macro:
                    subsequent_goals_with_plans.append(obj)

    if subsequent_goals_with_plans:
        notice(
            "\nCaution: The following subsequent active goals have existing plans that\n"
            "were aligned with the plan you just deleted. You may need to regenerate them\n"
            "so their dates align correctly (e.g. running "
            + cmd("plan generate --goal <ID> --force") + "):",
        )
        for sg in subsequent_goals_with_plans:
            notice(
                f" - ID {sg['id']}: '{sg['title']}' "
                f"(Target date: {fmt_date(sg['target_date'])})",
            )


def run_plan_wipe(args: argparse.Namespace) -> None:
    """Wipes all plans from the database after confirmation."""
    if not args.yes:
        if not runtime.prompt.confirm(
            "Are you sure you want to wipe all periodization plans?", danger=True
        ):
            print("Wipe cancelled.")
            return

    runtime.db.wipe_plans()
    print(green("All periodization plans wiped successfully."))


def run_plan_rollback(args: argparse.Namespace) -> None:
    """Restores a superseded periodization plan version (and its workouts)."""
    goal = _resolve_goal(getattr(args, 'goal_id', None))
    if not goal:
        return

    versions = runtime.db.get_macrocycle_versions(goal['id'])
    superseded = [v for v in versions if v.get('status') == 'superseded']
    if not superseded:
        notice(f"Goal '{goal['title']}' has no earlier plan version to roll back to.")
        return

    # Determine the target version (default: chronologically previous).
    target_id = getattr(args, 'macrocycle_id', None)
    if target_id is None:
        prev = runtime.db.get_previous_macrocycle_version(goal['id'])
        target_id = prev['id'] if prev else None
    if target_id is None:
        notice(f"Goal '{goal['title']}' has no earlier plan version to roll back to.")
        return

    target = runtime.db.get_macrocycle(target_id)
    if not target or target.get('objective_id') != goal['id']:
        notice(f"Plan version {target_id} does not belong to goal '{goal['title']}'.", red)
        return

    if not getattr(args, 'yes', False):
        created = fmt_date(str(target.get('created_at', ''))[:10]) if target.get('created_at') else '?'
        if not runtime.prompt.confirm(
            f"Roll back the plan for '{goal['title']}' to the version generated "
            f"{created} (plan ID {target_id})?\nThis archives the current plan's "
            f"upcoming workouts and restores that version's on Google Calendar.",
            danger=True,
        ):
            print("Rollback cancelled.")
            return

    try:
        result = runtime.coach_service.plan_rollback(
            objective_id=goal['id'], target_macrocycle_id=target_id
        )
    except ValueError as e:
        notice(str(e), red)
        return

    print(green(
        f"\nRolled back '{goal['title']}' to plan ID {result['to']['id']} "
        f"(was {result['from']['id']})."
    ))
    print(
        f"Restored {result['restored_workouts']} workout(s) from the superseded plan; "
        "Google Calendar updated."
    )
    report_unhonored(result['unhonored'])
    print(green(f"Run {cmd('plan show')} to review the restored strategy."))


# `--rm` given without an ID: argparse hands the const through untouched, so the handler
# can answer it with DESIGN_cli_noargs.md §a's missing-argument treatment rather than
# argparse's bare "expected one argument".
_RM_NO_ID = object()


def _feedback_list(goal: dict, macro: dict) -> None:
    """The bare run: the pending log, read-only (DESIGN_cli_noargs.md bucket 1)."""
    notes = runtime.db.list_plan_feedback(macro['id'])
    if not notes:
        print(gray(
            f"No feedback pending on the plan for '{goal['title']}'. Add a note with "
            + cmd('plan feedback "…"') + "."
        ))
        return
    print(bold(f"Plan feedback for '{goal['title']}'") + gray(
        f" ({len(notes)} pending — feeds the next {cmd('plan generate')})"
    ) + ":")
    _print_feedback_notes(notes, default_wrap_width())


def _feedback_rm(args: argparse.Namespace) -> None:
    """Deletes one note. Rewording is `--rm` + re-add, which is why there is no --edit."""
    if args.rm is _RM_NO_ID:
        args._parser.error("the following arguments are required: --rm ID")
    note = runtime.db.get_plan_feedback(args.rm)
    if not note:
        notice(f"No feedback note with ID {args.rm}.", red)
        sys.exit(1)
    _print_feedback_notes([note], default_wrap_width())
    if not args.yes and not runtime.prompt.confirm("Delete this note?", danger=True):
        print("Removal cancelled.")
        return
    runtime.db.rm_plan_feedback(args.rm)
    print(green(f"Removed feedback note {args.rm}."))


def _meso_owner_hint(atom, goal: dict) -> str:
    """Where a rejected mesocycle ID actually lives — another goal's plan, or a
    superseded version of this one (DESIGN_plan_feedback.md §4/§5)."""
    if not (isinstance(atom, str) and atom.strip().isdigit()):
        return ""
    meso = runtime.db.get_mesocycle(int(atom))
    macro = runtime.db.get_macrocycle(meso['macrocycle_id']) if meso else None
    if not macro:
        return ""
    if macro.get('status') == 'superseded':
        return (f"\nBlock {atom} ('{meso['name']}') belongs to a superseded version of "
                "this plan, and a note can only steer the active one.")
    owner = runtime.db.get_objective(macro['objective_id'])
    if not owner or owner['id'] == goal['id']:
        return ""
    return (f"\nBlock {atom} ('{meso['name']}') belongs to the plan for "
            f"'{owner['title']}' — reach it with " + cmd("-g " + str(owner['id'])) + ".")


def _feedback_replan(goal: dict) -> None:
    """Runs the regeneration flow right after saving, so feedback → new plan is one
    command. It does not imply --force and does not need to: pending notes are a plan
    input, so the gate lets the regeneration through (§7). The preview and its human `y`
    still stand, per the constraints precedent (trainmate/cli/constraints.py)."""
    print(green("Regenerating the periodization plan around your feedback..."))
    run_plan_generate(argparse.Namespace(
        no_pull=False, force_pull=False, auto=False,
        goal_range=IdRange(start=goal['id'], end=goal['id']), force=False, fresh=False,
    ))
    aside("If you applied the new plan, run " + cmd("workout generate")
          + " to schedule it.")


def run_plan_feedback(args: argparse.Namespace) -> None:
    """Appends to — or lists, or prunes — the plan's feedback log
    (DESIGN_plan_feedback.md §4).

    No LLM anywhere here: capture is an INSERT, and the one consumer of the result is the
    regeneration, which reads the whole log and does the understanding there (§2)."""
    goal = _resolve_goal(args.goal_id)
    if not goal:
        sys.exit(1)
    macro = runtime.db.get_macrocycle_for_objective(goal['id'])
    if not macro:
        notice(f"No active periodization plan exists for goal '{goal['title']}'.")
        print(green(f"Run {cmd('plan generate')} to create one."))
        sys.exit(1)

    if args.rm is not None:
        if args.text or args.meso is not None or args.replan:
            notice("Error: --rm deletes one note by ID; it takes nothing else.", red)
            sys.exit(1)
        _feedback_rm(args)
        return

    if args.text is not None and not args.text.strip():
        notice("Error: the note is empty; nothing was saved.", red)
        sys.exit(1)

    if args.text is None:
        if args.meso is not None or args.replan:
            notice("Error: give the note text — there is nothing to file yet.", red)
            sys.exit(1)
        _feedback_list(goal, macro)
        return

    # `-g` picks the plan, `-m` resolves inside it: filing to a superseded version cannot
    # steer the next one, so the atom only ever sees the active plan's blocks (§5).
    meso = None
    if args.meso is not None:
        try:
            meso = resolve_meso_atom(
                args.meso, runtime.db.get_mesocycles_for_macrocycle(macro['id'])
            )
        except SelectorError as e:
            print(red(f"Error: {e}") + _meso_owner_hint(args.meso, goal))
            sys.exit(1)

    note_id = runtime.db.add_plan_feedback(
        macro['id'], args.text.strip(), meso['id'] if meso else None
    )
    filing = f"filed: {meso['name']}" if meso else "plan-level"
    print(green(f"Noted [id {note_id}, {filing}]: ") + f"\"{args.text.strip()}\"")

    if args.replan:
        _feedback_replan(goal)
        return
    pending = len(runtime.db.list_plan_feedback(macro['id']))
    aside(
        f"{pending} note{'s' if pending != 1 else ''} pending — "
        f"{'they feed' if pending != 1 else 'it feeds'} the next {cmd('plan generate')} "
        f"({cmd('--replan')} runs it now)."
    )


def add_plan_parser(subparsers, pull_bypass_parser, llm_debug_parser):
    # plan command & subparsers
    plan_parser = subparsers.add_parser(
        "plan",
        help="Manage and consult the periodized training plan (macrocycles & mesocycles)"
    )
    plan_subparsers = plan_parser.add_subparsers(
        dest="subcommand", help="Plan sub-commands"
    )
    
    # plan generate
    p_gen = plan_subparsers.add_parser(
        "generate",
        parents=[pull_bypass_parser, llm_debug_parser],
        help=(
            "Generate or adapt the periodized training plan strategy "
            "(macrocycles & mesocycles)"
        )
    )
    p_gen.set_defaults(func=run_plan_generate)
    p_gen.add_argument(
        "-f", "--force", action="store_true",
        help="Force regeneration of the macrocycle/mesocycle strategy"
    )
    p_gen.add_argument(
        "--fresh", action="store_true",
        help=(
            "Clean slate: don't show the coach the plan currently in place, so the new "
            "strategy is not asked to continue it (implies --force). Your training "
            "history and plan feedback still feed in."
        )
    )
    p_gen.add_argument(
        "-y", "--yes", "--auto", action="store_true", dest="auto",
        help="Apply proposed plan updates automatically without prompting"
    )
    # Not -v: that letter already means "name each Calendar event" on three workout
    # sub-commands, and one letter meaning two things is what DESIGN_output_verbosity.md
    # §4 turned down a global --verbose for. This reads as the sibling it is of
    # --show-llm-prompt-only (§7).
    p_gen.add_argument(
        "--show-llm-context", action="store_true", dest="show_llm_context",
        help="Also print the planned-vs-actual review of your past plans that goes to "
             "the coach as prompt context. Off by default: it is long, and it pushes "
             "the new strategy below the fold"
    )
    p_gen.add_argument(
        "-g", "--goal", "--goal-id", dest="goal_range", nargs="?", const=CURRENT,
        type=lambda raw: parse_id_range(raw, "goal"), metavar="RANGE",
        help="Goal(s) to plan for, as the shared range grammar: ID, ID.., ..ID or "
             "ID..ID (bare -g is the active goal). One ID plans that goal alone, bounded "
             "to its own span — the day after the goal before it, through its target "
             "date. A range plans every upcoming goal it covers, in date order, one "
             "strategy call each: '-g ..2' is everything through goal 2"
    )
    
    # plan show
    p_show = plan_subparsers.add_parser(
        "show",
        help="Show a macrocycle and its mesocycles periodization strategy "
             "(--goal/--macrocycle/--all, -w for workouts)",
        description=(
            "Show a periodization plan: the macrocycle strategy, the inputs it was "
            "generated from (goals, constraints, threshold anchors) and its mesocycle "
            "timeline. Flags any of those inputs that have changed since, and what to do "
            "about it. Defaults to the active plan of the next active goal; --goal reaches "
            "any goal including completed/archived ones, --macrocycle an earlier plan version, "
            "and --all every goal that has a plan."
        )
    )
    p_show.set_defaults(func=run_plan_show)
    p_show.add_argument(
        "-g", "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID to show the periodization plan for (defaults to the next "
             "active goal)"
    )
    p_show.add_argument(
        "-M", "--macrocycle", type=int, dest="macrocycle_id", metavar="MACROCYCLE_ID",
        help="Show one macrocycle by ID — each plan version IS a macrocycle, so this is "
             "how you reach a superseded one ('plan versions' lists the IDs)"
    )
    p_show.add_argument(
        "-a", "--all", action="store_true",
        help="Show the active plan of every goal that has one (any status), oldest target "
             "date first"
    )
    p_show.add_argument(
        "-w", "--workouts", action="store_true",
        help="List each mesocycle's scheduled workouts, not just their count/load summary"
    )

    # plan keep
    p_keep = plan_subparsers.add_parser(
        "keep",
        help="Keep the current plan and stop flagging the inputs that changed",
        description=(
            "Record your current profile, goals and thresholds against the active plan "
            "without regenerating it. Use it when 'plan show' flags a changed input that "
            "would not have altered the periodization — a reworded preference, a "
            "corrected label — so the flag clears without spending a strategy call. The "
            "change still reaches your sessions at the next 'workout generate'."
        )
    )
    p_keep.set_defaults(func=run_plan_keep)
    p_keep.add_argument(
        "-g", "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID whose plan to keep (defaults to the next active goal)"
    )

    # plan diff
    p_diff = plan_subparsers.add_parser(
        "diff", aliases=["df"],
        help="Compare two plan versions (strategy, mesocycles, inputs)",
        description=(
            "Compare two periodization plan versions field by field: what changed in the "
            "macrocycle strategy, which feedback notes each version carries, which "
            "mesocycles were added, removed, "
            "renamed or re-dated, and how the snapshotted inputs (goals, constraints, "
            "threshold anchors) differ. With no version given, compares the previous "
            "version against the active one; with one, that version against the active one."
        )
    )
    p_diff.set_defaults(func=run_plan_diff)
    p_diff.add_argument(
        "version_a", type=int, nargs="?", metavar="PLAN_ID_A",
        help="Older plan version to compare from (defaults to the previous version)"
    )
    p_diff.add_argument(
        "version_b", type=int, nargs="?", metavar="PLAN_ID_B",
        help="Newer plan version to compare to (defaults to the active version)"
    )
    p_diff.add_argument(
        "-g", "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID whose plan versions to compare (defaults to the next active goal)"
    )
    p_diff.add_argument(
        "--full", action="store_true",
        help="Diff wholesale-rewritten prose sentence by sentence instead of collapsing it "
             "to a one-line note"
    )

    # plan versions
    p_versions = plan_subparsers.add_parser(
        "versions",
        help="List all plan versions (active + superseded) for a goal",
        description=(
            "List every periodization plan version kept for a goal — the active one and "
            "any superseded by later regenerations — with their IDs and dates, so you can "
            "inspect one ('plan show --macrocycle <ID>') or restore one "
            "('plan rollback --macrocycle <ID>')."
        )
    )
    p_versions.set_defaults(func=run_plan_versions)
    p_versions.add_argument(
        "-g", "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID whose plan versions to list (defaults to the next active goal)"
    )

    # plan rm
    p_rm = plan_subparsers.add_parser(
        "rm", advanced=True,
        help="Remove/delete a specific periodization plan by Goal ID",
        description=(
            "Delete every periodization plan version a goal owns — superseded ones "
            "included, so the goal is left with no plan history at all. Its mesocycle "
            "blocks and plan feedback go with them, and upcoming sessions are left "
            "behind with no plan to explain them. The inventory is shown before "
            f"anything is deleted. Use '{green('plan generate --force')}' to replace a "
            f"plan reversibly, or '{green('plan rollback')}' to step back one "
            "regeneration."
        )
    )
    p_rm.set_defaults(func=run_plan_rm)
    p_rm.add_argument(
        "id", type=int,
        help="Goal ID whose periodization plan should be removed"
    )
    p_rm.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )

    # plan rollback
    p_rollback = plan_subparsers.add_parser(
        "rollback", aliases=["rb"],
        help="Restore a superseded plan version and its workouts",
        description=(
            "Undo a plan regeneration: restore an earlier periodization plan version "
            "and the workouts that were live under it. Defaults to the chronologically "
            "previous version of the next active goal's plan; repeat to walk further "
            "back, or target a specific version with --macrocycle. The current plan's "
            "upcoming workouts are archived and the restored version's are re-pushed to "
            "Google Calendar (the symmetric inverse of generation)."
        )
    )
    p_rollback.set_defaults(func=run_plan_rollback)
    p_rollback.add_argument(
        "-g", "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID whose plan to roll back (defaults to the next active goal)"
    )
    p_rollback.add_argument(
        "-M", "--macrocycle", type=int, dest="macrocycle_id", metavar="MACROCYCLE_ID",
        help="Roll back to a specific plan version (macrocycle) ID instead of the previous one"
    )
    p_rollback.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )

    # plan feedback
    p_fb = plan_subparsers.add_parser(
        "feedback",
        help="Tell the coach what you think of the plan (bare run lists pending notes)",
        description=(
            "Leave a note about the plan for the next 'plan generate' to read. Notes "
            "accumulate against the active plan — a second thought adds to the first "
            "rather than replacing it — and are consumed when a new version supersedes "
            "the one they were written against. Bare text is plan-level; -m files the "
            "note to one block, by name, date or ID. A bare run lists what is pending; "
            "nothing here calls the LLM, so capture is instant."
        )
    )
    # `_parser` lets the handler route `--rm` with no ID back through argparse's own
    # missing-argument path (DESIGN_cli_noargs.md §a).
    p_fb.set_defaults(func=run_plan_feedback, _parser=p_fb)
    p_fb.add_argument(
        "text", nargs="?", default=None,
        help="The note to append (omit to list what is pending)"
    )
    p_fb.add_argument(
        "-m", "--mesocycle", dest="meso", nargs="?", const=CURRENT, metavar="ATOM",
        help="File the note to ONE block: its name (any part of it), a date it covers "
             "(YYYY-MM-DD, today, -7d, +2w) or its mesocycle ID. Bare -m is the current "
             "block; without -m the note is plan-level"
    )
    p_fb.add_argument(
        "-g", "--goal", "--goal-id", type=int, dest="goal_id",
        help=(
            "Target goal ID whose plan the feedback should attach to "
            "(defaults to the next active goal)"
        )
    )
    p_fb.add_argument(
        "--rm", nargs="?", type=int, const=_RM_NO_ID, default=None, metavar="ID",
        help="Delete one pending note by ID (the bare listing shows them)"
    )
    p_fb.add_argument(
        "-y", "--yes", action="store_true", help="Skip the --rm confirmation prompt"
    )
    p_fb.add_argument(
        "--replan", action="store_true",
        help="After saving, regenerate the plan straight away (same preview-and-confirm "
             "as 'plan generate'; no --force needed)"
    )

    # plan wipe
    p_wipe = plan_subparsers.add_parser(
        "wipe", advanced=True, help="Wipe all periodization plans")
    p_wipe.set_defaults(func=run_plan_wipe)
    p_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    

    return plan_parser
