"""Previewing an in-place revision before it is applied."""
from trainmate import runtime
from trainmate.util import (
    bold, green, red, yellow, cyan, magenta, gray, render_table,
)
from trainmate.coach.proposals import RevisionProposal


def _stats(w: dict) -> str:
    """One session's load, as the revision preview shows it."""
    return (
        f"{w.get('duration_minutes') or 0}m/"
        f"RPE{w.get('rpe') or 0}/"
        f"TSS{w.get('tss') or 0}"
    )


def preview_and_confirm_revision(
    proposal: RevisionProposal, heading: str, question: str, *, auto: bool = False,
) -> bool:
    """Renders a revision and asks whether to apply it.

    Everything drawn comes off the proposal — the range it evaluated and the sessions it
    saw — so the preview cannot disagree with what apply will do.
    """
    print(bold(yellow(f"\n{heading}")))
    headers = ["Date", "Sport", "Original Workout", "Proposed Workout", "Duration/RPE/TSS"]
    rows = []

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

    rows.sort(key=lambda r: r[0])
    print(render_table(headers, rows))
    return auto or runtime.prompt.confirm(question)
