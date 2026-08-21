"""`workout accommodate` — honoring a constraint the daily adapt cannot reach
(DESIGN_constraint_reschedule.md).

Names which constraints to honor, then renders what the service says can be done with
them. Its own file under the workout family, per AGENTS.md's one-command-per-file rule.
"""
import argparse
from typing import List, Optional

from trainmate import runtime
from trainmate.util import (
    aside, bold, green, red, yellow, cmd, wrap_text, today_str as _today_str,
)
from trainmate.coach import honoring
from trainmate.coach.proposals import AccommodationPass
from trainmate.cli.common import fmt_date
from trainmate.cli.selectors import has_selector as _has_selector, resolve_window
from trainmate.cli.workouts.revisions import preview_and_confirm_revision


def _names(constraints) -> str:
    return ", ".join(f"[{c['id']}] {c['title']}" for c in constraints)


def run_accommodation_pass(pass_: AccommodationPass, auto: bool) -> None:
    """One window: propose, preview the whole of it, apply on a `y`.

    Sequential by construction — the caller runs these one at a time so every pass is
    judged against the plan as the previous apply left it (§4.1).
    """
    range_start, range_end = pass_.range_start, pass_.range_end
    aside(f"Honoring {_names(pass_.constraints)} over {range_start} to {range_end}...")
    # The whole window is evaluated even when the plan ends inside it (§5): a session in
    # conflict past the plan's end is still cleared or capped — but nothing governs what
    # is re-placed there, so say so before the call is spent.
    governed_end = max((b['end_date'] for b in pass_.blocks), default=None)
    if governed_end and governed_end < range_end:
        print(yellow(wrap_text(
            f"The plan runs out on {governed_end}, before this window ends "
            f"({range_end}). Sessions after it are still reshuffled around the "
            "constraint, but no block guides them; run " + cmd("plan generate")
            + " to extend the periodization."
        )))
    proposal = runtime.coach_service.workout_accommodate(
        range_start, range_end,
        constraint_ids=[c['id'] for c in pass_.constraints],
        blocks=list(pass_.blocks) or None,
    )

    print(f"\n{bold('Decision Summary')}:\n{wrap_text(proposal.reason)}")
    if not proposal.workouts:
        print(green(
            f"\nThe sessions in {fmt_date(proposal.range_start)} → "
            f"{fmt_date(proposal.range_end)} already work around this — no changes "
            "proposed."
        ))
        # The pass still had them in scope with authority over them, which is all
        # `honored_at` claims — without this the sweep re-offers them forever (§8).
        runtime.coach_service.workout_revision_record_no_change(proposal)
        return

    if not preview_and_confirm_revision(
        proposal,
        f"PROPOSED SCHEDULE FOR {proposal.range_start} TO {proposal.range_end}:",
        "Apply this reshuffle to your training plan and sync to Calendar?",
        whole_window=True, auto=auto,
    ):
        print("\nReshuffle discarded.")
        return

    aside("\nApplying...")
    runtime.coach_service.workout_revision_apply(proposal)
    print(green("Schedule updated and synced to calendar successfully."))


def _named_constraints(ids: List[int]) -> Optional[List[dict]]:
    """`-c`: exactly these, `honored_at` or not. None when one of them does not exist.

    Naming a constraint is an explicit imperative on it, so the §8 filter does not apply —
    re-honoring one after hand-editing its window's sessions is a legitimate ask (§4).
    """
    found, missing = [], []
    for constraint_id in ids:
        constraint = runtime.db.get_constraint(constraint_id)
        if not constraint:
            missing.append(str(constraint_id))
            continue
        found.append(constraint)
    if missing:
        print(red("No constraint with ID " + ", ".join(missing) + "."))
        return None
    return found


def _subject(
    args: argparse.Namespace, today: str, plan_end: str, bare: bool
) -> Optional[List[dict]]:
    """The constraints this invocation is about — the one question that stays here,
    because two of its three answers are argparse-shaped (§4).

    What can then be DONE with them is the service's call: whether a block governs each
    window, and how they group into passes.
    """
    if args.constraint:
        return _named_constraints(args.constraint)

    # One owner for "is the window tier the right answer for this directive?" — the same
    # call the `status` line and `constraint list`/`show` make (§8).
    needs_a_pass = honoring.constraints_needing_a_pass(runtime.db, today)
    if bare:
        return needs_a_pass
    start, end = resolve_window(args)
    start, end = start or today, end or plan_end
    return [c for c in needs_a_pass if c['start_date'] <= end and c['end_date'] >= start]


def run_workout_accommodate(args: argparse.Namespace) -> None:
    """Honors the constraints the daily adapt cannot reach, in their own windows.

    Three ways to name the subject: `-c` on the constraints themselves, a selector from
    the reserved vocabulary, or nothing — which sweeps every constraint the plan does not
    yet reflect (§4.1), the case that matters, because the athlete does not know which
    block the constraint landed in.
    """
    if args.constraint and _has_selector(args):
        print(red(
            "-c names the constraints to honor; -d/-m/-M/-g name a window to find them "
            "in. Use one or the other."
        ))
        return

    today = _today_str()
    plan_end = runtime.coach_service.plan_horizon(today)
    if not plan_end:
        print(red(
            "No active periodization strategy found. Run " + cmd("plan generate") + " first."
        ))
        return

    # No subject named at all — neither a constraint nor a window: the §4.1 sweep.
    bare = not args.constraint and not _has_selector(args)
    subject = _subject(args, today, plan_end, bare)
    if subject is None:
        return

    plan = runtime.coach_service.accommodation_plan(today, subject)
    if bare:
        # Name the defaulted target rather than assuming it (DESIGN_cli_noargs.md §b).
        aside(
            f"No window given — checking {len(subject)} constraint(s) your plan does "
            f"not reflect, through {plan.plan_end}."
        )

    # Every refusal is a bucket the service filled, named rather than skipped, so
    # "checked through <plan end>" is never read as "checked everything" (§4.1).
    if plan.ungoverned:
        print(yellow(wrap_text(
            f"{len(plan.ungoverned)} constraint(s) have no block in your plan to "
            f"reshuffle towards (it runs to {plan.plan_end}); run "
            + cmd("plan generate") + " to cover their dates first: "
            + _names(plan.ungoverned)
        )))

    # Only reachable via `-c`: the sweep's own filter already excludes these.
    if plan.spent:
        print(yellow(wrap_text(
            f"{len(plan.spent)} constraint(s) end today or earlier — only today is left "
            "of them, which belongs to " + cmd("workout adapt") + ": "
            + _names(plan.spent)
        )))

    if not plan.passes:
        if not plan.ungoverned and not plan.spent:
            print(green("Your plan already reflects every constraint in range."))
        return

    # Say how much work was just asked for before spending it: one LLM call per window,
    # and with -y each one writes (DESIGN_constraint_reschedule.md §4.1).
    if len(plan.passes) > 1:
        aside(f"{len(plan.passes)} windows to work through, one coach call each.")

    for pass_ in plan.passes:
        try:
            run_accommodation_pass(pass_, args.auto)
        except ValueError as e:
            # A domain refusal the plan could not foresee — the world changed under it.
            print(red(str(e)))
