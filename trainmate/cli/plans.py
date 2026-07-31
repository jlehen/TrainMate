import textwrap
import argparse
import sys
from datetime import datetime, timedelta
from typing import List, Optional
import trainmate_cli as cli
from trainmate import plan_diff
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered, planned_load
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, default_wrap_width, today_str as _today_str,
    today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data


def _resolve_goal(goal_id: Optional[int]) -> Optional[dict]:
    """The goal a plan command targets: the given ID (whatever its status, so archived
    and completed goals stay reachable), else the next active goal by target date.
    Prints the reason and returns None when there is none."""
    if goal_id is not None:
        goal = cli.db.get_objective(goal_id)
        if not goal:
            print(red(f"Goal with ID {goal_id} not found."))
        return goal
    objectives = cli.db.get_objectives(status='active')
    if not objectives:
        print(yellow("No active goals found. TrainMate needs at least one objective."))
        return None
    objectives.sort(key=lambda x: str(x['target_date']))
    return objectives[0]


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
            if cli.prompt.confirm(wrap_text(
                "No training-history analysis found. Run 'data bootstrap' first to "
                "reconstruct past cycles and seed coach learnings?"
            )):
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
                print(dim(wrap_text(
                    f"No goal given — planning for your next goal: "
                    f"{next_goal.get('title', '')} on {fmt_date(next_goal['target_date'])}."
                )))

            if next_goal:
                macro = cli.db.get_macrocycle_for_objective(next_goal['id'])
                if macro:
                    change_reason = cli.coach_service.config_changed(macro)
                    if change_reason and not args.force:
                        if cli.prompt.confirm(wrap_text(
                            "A plan-shaping input has changed since the last plan "
                            f"generation ({change_reason}).\n"
                            "Would you like to regenerate the periodization strategy?"
                        )):
                            args.force = True
                        else:
                            print(wrap_text(
                                "Keeping current periodization strategy. "
                                "Updating configuration hash in database."
                            ))
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


def _print_considered_inputs(macrocycle: dict) -> None:
    """Prints the goals, constraints and threshold anchors the plan was generated from."""
    goals, events, thresholds = plan_diff.input_snapshots(macrocycle)
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
                f"  - [ID: {g.get('id')}] ", g.get('title', ''), width, cyan
            )
            _print_segments(
                pad,
                [
                    magenta(sport),
                    cyan(fmt_date(g.get('target_date'))),
                    f"priority {g.get('priority')}",
                ],
                width,
            )
            if g.get('description'):
                _print_indented(g['description'], pad, width, gray)
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
            pad = _print_hanging(
                f"  - [ID: {e.get('id')}] ", e.get('title', ''), width, cyan
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
    """Every workout (live or archived) the given plan version scheduled.

    Rows carry the `macrocycle_id` of the version that created them. A database wholly
    predating that column has none, so its rows are matched on dates alone; once any row
    is stamped, an unstamped one is nobody's rather than everybody's."""
    rows = cli.db.get_workouts(include_archived=True)
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
        if w.get('archived_at'):
            tail.append("archived")
        _print_hanging(
            f"{pad}  {cyan(fmt_date(w['date']))} ",
            f"[{w['sport_type']}] {w.get('title') or ''} ({' · '.join(tail)})", width, gray,
        )


def run_plan_show(args: argparse.Namespace) -> None:
    """Displays the training macrocycle(s) and mesocycles periodization timeline."""
    if getattr(args, 'all', False):
        if args.goal_id is not None or getattr(args, 'version', None) is not None:
            print(red("Error: --all cannot be combined with --goal or --version."))
            return
        goals = sorted(cli.db.get_objectives(), key=lambda g: str(g['target_date']))
        planned = [(g, cli.db.get_macrocycle_for_objective(g['id'])) for g in goals]
        planned = [(g, m) for g, m in planned if m]
        if not planned:
            print(yellow("No goal has a periodization plan yet."))
            print(f"Run '{green('plan generate')}' to create one.")
            return
        for goal, macrocycle in planned:
            _print_plan(goal, macrocycle, args)
        return

    next_goal = _resolve_goal(args.goal_id)
    if not next_goal:
        return

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

    _print_plan(next_goal, macrocycle, args)


def _print_plan(next_goal: dict, macrocycle: dict, args: argparse.Namespace) -> None:
    """Renders one plan version: header, strategy, snapshotted inputs, mesocycle timeline."""
    mesocycles = cli.db.get_mesocycles_for_macrocycle(macrocycle['id'])
    show_workouts = getattr(args, 'workouts', False)
    workouts = _plan_workouts(macrocycle)

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
    width = default_wrap_width()
    sport_str = next_goal['sport_type'].upper()
    obj_pad = _print_hanging(
        f"{bold('Objective')} [ID: {next_goal['id']}]: ",
        next_goal['title'], width, cyan,
    )
    _print_segments(
        obj_pad,
        [magenta(sport_str), cyan(fmt_date(next_goal['target_date']))],
        width,
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
                f"[ID: {m['id']}]",
                f"{cyan(fmt_date(m['start_date']))} -> {cyan(fmt_date(m['end_date']))}",
                duration_desc,
                (f"phase {m['phase']}" if m.get('phase') else ""),
            ],
            width,
        )
        print(f"{pad}[{bar}]{extra}")
        _print_mesocycle_workouts(m, workouts, pad, width, show_workouts)
        _print_indented(m['focus'], pad, width)
        if m.get('feedback'):
            print(f"{pad}{bold('Mesocycle Feedback')}:")
            _print_indented(m['feedback'], pad + "  ", width)
        print(pad + gray("-" * min(40, max(10, width - len(pad)))))


def run_plan_versions(args: argparse.Namespace) -> None:
    """Lists every periodization plan version (active + superseded) for a goal."""
    goal = _resolve_goal(getattr(args, 'goal_id', None))
    if not goal:
        return

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
    ) + green("'plan rollback --version <ID>'") + gray(", inspect one with ")
        + green("'plan show --version <ID>'") + gray(", or compare two with ")
        + green("'plan diff <ID> <ID>'") + gray("."))


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
            label = f"{e['name']} ({e['dates']['start']} -> {e['dates']['end']})"
            _print_change("+" if added else "-", label, width, green if added else red)
            continue
        header = f"{e['from_name']}  =>  {e['name']}" if e['renamed'] else e['name']
        _print_change("~", header, width, yellow)
        if e['dates']:
            frm, to = e['dates']['from'], e['dates']['to']
            print(f"      dates {cyan(frm['start'])} -> {cyan(frm['end'])}  =>  "
                  f"{cyan(to['start'])} -> {cyan(to['end'])}")
        for f in e['fields']:
            print(f"      {f['field']}: {f['from'] or '—'}  =>  {f['to'] or '—'}")
        if e['focus']:
            print(f"      {gray('focus:')}")
            _print_prose_diff(e['focus'], width, full, indent="        ")


def _print_missing_snapshot(missing: str) -> None:
    """Says which side lacks the snapshot, so a plan predating the column is never read
    as everything having been added or removed."""
    if missing == "both":
        print(f"  {gray('not recorded on either version')}")
        return
    side = "A" if missing == "old" else "B"
    print(f"  {gray(f'not recorded on {side} — that plan predates the snapshot')}")


def _print_records_diff(diff: dict, width: int) -> None:
    """Renders a `plan_diff.diff_records` result (snapshotted goals or constraints)."""
    if diff['missing']:
        _print_missing_snapshot(diff['missing'])
        return
    if not (diff['added'] or diff['removed'] or diff['changed']):
        print(f"  {gray('unchanged')}")
        return
    for rec in diff['removed']:
        _print_change("-", f"[ID: {rec.get('id')}] {rec.get('title', '')}", width, red)
    for rec in diff['added']:
        _print_change("+", f"[ID: {rec.get('id')}] {rec.get('title', '')}", width, green)
    for rec in diff['changed']:
        _print_change("~", f"[ID: {rec['id']}] {rec['title']}", width, yellow)
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
    """'A  ID 12  generated 2026-07-29  superseded 2026-07-31' for a diff header."""
    created = str(macro.get('created_at', ''))[:10]
    if macro.get('status') == 'superseded':
        superseded = str(macro.get('superseded_at', ''))[:10]
        state = gray("superseded" + (f" {superseded}" if superseded else ""))
    else:
        state = green("active")
    gen = f"generated {fmt_date(created)}" if created else ""
    return f"  {bold(tag)}  {pad_visible('ID ' + str(macro['id']), 8)} {gray(gen)}  {state}"


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
        cli.db, goal, args.version_a, args.version_b
    )
    if error:
        code, message = error
        print(red(message) if code in ("same_version", "not_found") else yellow(message))
        hint = _DIFF_ERROR_HINTS.get(code)
        if hint:
            print(f"Run '{green(hint[0])}' {hint[1]}")
        return

    width = default_wrap_width()
    print(bold(cyan("\n=== PLAN DIFF ===")))
    obj_pad = _print_hanging(
        f"{bold('Objective')} [ID: {goal['id']}]: ", goal['title'], width, cyan,
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
        cli.db.get_mesocycles_for_macrocycle(old['id']),
        cli.db.get_mesocycles_for_macrocycle(new['id']),
    )
    full = getattr(args, 'full', False)
    print(bold("\nStrategy:"))
    _print_prose_diff(diff['strategy'], width, full)
    print(bold("\nMacrocycle feedback:"))
    _print_prose_diff(diff['feedback'], width, full)
    print(bold("\nMesocycles:"))
    _print_mesocycles_diff(diff['mesocycles'], width, full)
    print(bold("\nGoals considered:"))
    _print_records_diff(diff['goals'], width)
    print(bold("\nConstraints considered:"))
    _print_records_diff(diff['constraints'], width)
    print(bold("\nThresholds considered:"))
    _print_thresholds_diff(diff['thresholds'], width)
    print()


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
    goal = _resolve_goal(getattr(args, 'goal_id', None))
    if not goal:
        return

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
        help="Show a macrocycle and its mesocycles periodization strategy "
             "(--goal/--version/--all, -w for workouts)",
        description=(
            "Show a periodization plan: the macrocycle strategy, the inputs it was "
            "generated from (goals, constraints, threshold anchors) and its mesocycle "
            "timeline. Defaults to the active plan of the next active goal; --goal reaches "
            "any goal including completed/archived ones, --version an earlier plan version, "
            "and --all every goal that has a plan."
        )
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
    p_show.add_argument(
        "-a", "--all", action="store_true",
        help="Show the active plan of every goal that has one (any status), oldest target "
             "date first"
    )
    p_show.add_argument(
        "-w", "--workouts", action="store_true",
        help="List each mesocycle's scheduled workouts, not just their count/load summary"
    )

    # plan diff
    p_diff = plan_subparsers.add_parser(
        "diff", aliases=["df"],
        help="Compare two plan versions (strategy, mesocycles, inputs)",
        description=(
            "Compare two periodization plan versions field by field: what changed in the "
            "macrocycle strategy and feedback, which mesocycles were added, removed, "
            "renamed or re-dated, and how the snapshotted inputs (goals, constraints, "
            "threshold anchors) differ. With no version given, compares the previous "
            "version against the active one; with one, that version against the active one."
        )
    )
    p_diff.add_argument(
        "version_a", type=int, nargs="?", metavar="PLAN_ID_A",
        help="Older plan version to compare from (defaults to the previous version)"
    )
    p_diff.add_argument(
        "version_b", type=int, nargs="?", metavar="PLAN_ID_B",
        help="Newer plan version to compare to (defaults to the active version)"
    )
    p_diff.add_argument(
        "--goal", "--goal-id", type=int, dest="goal_id",
        help="Target goal ID whose plan versions to compare (defaults to the next active goal)"
    )
    p_diff.add_argument(
        "--full", action="store_true",
        help="Diff wholesale-rewritten prose sentence by sentence instead of collapsing it "
             "to a one-line note"
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
