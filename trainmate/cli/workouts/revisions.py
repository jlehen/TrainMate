"""Previewing an in-place revision — shared by `workout adapt` and `workout accommodate`.

Its own module because it belongs to neither command file (AGENTS.md, CLI layout).
"""
from trainmate import runtime
from trainmate.sports import canonical_sport
from trainmate.util import (
    bold, green, red, yellow, cyan, magenta, gray, render_table,
)
from trainmate.coach.proposals import RevisionProposal


def _stats(w: dict) -> str:
    """One session's load, the way both revision previews show it."""
    return (
        f"{w.get('duration_minutes') or 0}m/"
        f"RPE{w.get('rpe') or 0}/"
        f"TSS{w.get('tss') or 0}"
    )


def preview_and_confirm_revision(
    proposal: RevisionProposal, heading: str, question: str, *,
    whole_window: bool = False, auto: bool = False,
) -> bool:
    """Renders a revision and asks whether to apply it.

    `whole_window` also lists the days the proposal does not touch, which is what makes a
    reschedule's confirm honest: a move that emits only its destination leaves the
    original standing on a date neither `pairs` nor `removals` mentions, so only a full
    render shows the session twice. Affordable because §5 bounds that window to a handful
    of rows; adapt's runs to the block's end, daily, so it keeps the changed-rows table
    (DESIGN_constraint_reschedule.md §10).

    Everything drawn comes off the proposal — the range it evaluated and the sessions it
    saw — so the preview cannot disagree with what apply will do.
    """
    print(bold(yellow(f"\n{heading}")))
    headers = ["Date", "Sport", "Original Workout", "Proposed Workout", "Duration/RPE/TSS"]
    rows = []
    # Every session the proposal accounts for, so the whole-window pass can tell an
    # untouched day from one it already rendered.
    touched = set()

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
        touched.add((pw['date'], canonical_sport(pw['sport_type'])))
        if existing:
            touched.add((existing['date'], canonical_sport(existing['sport_type'])))
        rows.append([
            cyan(pw['date']), magenta(sport_label), gray(orig_title),
            green(pw['title']), yellow(stats_diff),
        ])

    # Sessions being deleted outright (overridden with no replacement proposal).
    for ew in sorted(proposal.removals, key=lambda w: w['date']):
        touched.add((ew['date'], canonical_sport(ew['sport_type'])))
        rows.append([
            cyan(ew['date']), magenta(ew['sport_type'].upper()),
            gray(ew['title']), red("[Removed]"),
            yellow(f"{_stats(ew)} -> removed"),
        ])

    if whole_window:
        for w in proposal.window_workouts:
            if (w['date'], canonical_sport(w['sport_type'])) in touched:
                continue
            rows.append([
                cyan(w['date']), magenta(w['sport_type'].upper()), gray(w['title']),
                gray("(unchanged)"), gray(_stats(w)),
            ])

    rows.sort(key=lambda r: r[0])
    print(render_table(headers, rows))
    return auto or runtime.prompt.confirm(question)
