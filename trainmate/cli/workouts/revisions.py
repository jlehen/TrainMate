"""Previewing an in-place revision before it is applied.

The expert table lives here; the companion prose form of the same proposal is
`render.simple_revision_lines`, which reuses the wording-diff helpers below
(DESIGN_render_persona.md §7).
"""
import difflib
import re
from typing import List, Tuple

from trainmate.util import (
    bold, green, red, yellow, cyan, magenta, gray, render_table, format_labeled_text,
)
from trainmate.coach.proposals import RevisionProposal

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# A block of the wording diff: the sentences dropped and the sentences that took their
# place, in reading order. Either side may be empty.
WordingBlock = Tuple[List[str], List[str]]


def _stats(w: dict) -> str:
    """One session's load, as the revision preview shows it."""
    return (
        f"{w.get('duration_minutes') or 0}m/"
        f"RPE{w.get('rpe') or 0}/"
        f"TSS{w.get('tss') or 0}"
    )


def rewritten_text_only(proposal: dict, original: dict) -> bool:
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


def wording_blocks(proposal: dict, original: dict) -> List[WordingBlock]:
    """What moved between two descriptions, as (dropped, replacement) sentence blocks.

    Blocks rather than a line-per-sentence `-`/`+` listing: a reader wants "this passage
    became that passage", and sign-prefixed lines lose their sign the moment a phone
    re-flows them (DESIGN_workout_revisions.md §9.1)."""
    a = _sentences(original.get('description'))
    b = _sentences(proposal.get('description'))
    return [
        (a[i1:i2], b[j1:j2])
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes()
        if tag != 'equal'
    ]


def wording_block_lines(block: WordingBlock, indent: str = "") -> List[str]:
    """One block as a labelled 'Was:' / 'Now:' pair (or 'Dropped:' / 'Added:' when one
    side is empty), wrapped at the client's width with the text hanging under its label."""
    dropped, added = block
    lines: List[str] = []
    if dropped:
        label = "Was: " if added else "Dropped: "
        lines.append(format_labeled_text(indent + label, " ".join(dropped), color_fn=red))
    if added:
        label = "Now: " if dropped else "Added: "
        lines.append(format_labeled_text(indent + label, " ".join(added), color_fn=green))
    return lines


def _print_wording_changes(proposal: RevisionProposal) -> None:
    """Shows what a revision changed when the table's columns cannot.

    The athlete reads the description, so revising it is a real adaptation — the coach
    makes them deliberately, e.g. rewriting a pacing cue to reference the session just
    executed. Printed rather than merely flagged: a preview that says a session changed
    but not how is what makes an honest text revision look like a bug (§9.1)."""
    reworded = [
        (pair.proposal, pair.original) for pair in proposal.pairs
        if rewritten_text_only(pair.proposal, pair.original)
    ]
    if not reworded:
        return
    print(bold(yellow("\nTEXT REVISED (same load, so the columns above cannot show it):")))
    for pw, existing in reworded:
        print(f"\n  {cyan(pw['date'])} {magenta(pw['sport_type'].upper())} — {pw['title']}")
        for block in wording_blocks(pw, existing):
            for line in wording_block_lines(block, indent="    "):
                print(line)


def print_revision_preview(proposal: RevisionProposal, heading: str) -> None:
    """Renders a revision as the expert table, plus the wording diff its columns cannot
    show.

    Everything drawn comes off the proposal — the range it evaluated and the sessions it
    saw — so the preview cannot disagree with what apply will do. Drawing only: the
    caller asks the question, through `runtime.prompt` (DESIGN_render_persona.md §4).
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
        if rewritten_text_only(pw, existing):
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
