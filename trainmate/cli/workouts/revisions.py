"""Previewing an in-place revision before it is applied."""
import difflib
import re
from typing import List

from trainmate import runtime
from trainmate.util import (
    bold, green, red, yellow, cyan, magenta, gray, render_table, wrap_text,
)
from trainmate.coach.proposals import RevisionProposal

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _stats(w: dict) -> str:
    """One session's load, as the revision preview shows it."""
    return (
        f"{w.get('duration_minutes') or 0}m/"
        f"RPE{w.get('rpe') or 0}/"
        f"TSS{w.get('tss') or 0}"
    )


def _rewritten_text_only(proposal: dict, original: dict) -> bool:
    """True when the prescription the table can SHOW is identical and only the text moved.

    Its columns are title and load, so a session the coach revised in words alone renders
    as `X | X | 85m/RPE7/TSS84 -> 85m/RPE7/TSS84` and reads as a change made for no
    reason. Those are the rows the diff below exists for
    (DESIGN_workout_revisions.md §9.1)."""
    if not original:
        return False
    if proposal.get('title') != original.get('title') or _stats(proposal) != _stats(original):
        return False
    norm = lambda v: " ".join(str(v or "").split())  # noqa: E731
    return norm(proposal.get('description')) != norm(original.get('description'))


def _sentences(text) -> List[str]:
    """A description as sentences. Blank lines are layout, so the diff ignores them."""
    out: List[str] = []
    for line in str(text or "").splitlines():
        for sentence in _SENTENCE_END.split(line.strip()):
            if sentence.strip():
                out.append(sentence.strip())
    return out


def _wording_diff(proposal: dict, original: dict) -> List[str]:
    """The sentences that moved between two descriptions, as `-`/`+` lines."""
    return [
        line for line in difflib.unified_diff(
            _sentences(original.get('description')),
            _sentences(proposal.get('description')),
            lineterm="", n=0,
        )
        if not line.startswith(("---", "+++", "@@"))
    ]


def _print_wording_changes(proposal: RevisionProposal) -> None:
    """Shows what a revision changed when the table's columns cannot.

    The athlete reads the description, so revising it is a real adaptation — the coach
    makes them deliberately, e.g. rewriting a pacing cue to reference the session just
    executed. Printed rather than merely flagged: a preview that says a session changed
    but not how is what makes an honest text revision look like a bug (§9.1)."""
    reworded = [
        (pair.proposal, pair.original) for pair in proposal.pairs
        if _rewritten_text_only(pair.proposal, pair.original)
    ]
    if not reworded:
        return
    print(bold(yellow("\nTEXT REVISED (same load, so the columns above cannot show it):")))
    for pw, existing in reworded:
        print(f"\n  {cyan(pw['date'])} {magenta(pw['sport_type'].upper())} — {pw['title']}")
        for line in _wording_diff(pw, existing):
            paint = red if line.startswith("-") else green
            # "    - text": the space is what lets wrap_text see a list prefix and hang
            # continuation lines under it.
            print(paint(wrap_text(f"    {line[0]} {line[1:].strip()}", width=88)))


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
        proposed_label = green(pw['title'])
        if _rewritten_text_only(pw, existing):
            proposed_label += gray(" [text revised]")
        rows.append([
            cyan(pw['date']), magenta(sport_label), gray(orig_title),
            proposed_label, yellow(stats_diff),
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
    _print_wording_changes(proposal)
    return auto or runtime.prompt.confirm(question)
