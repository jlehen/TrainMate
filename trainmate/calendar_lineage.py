"""The revision history a Calendar event carries (DESIGN_calendar_lineage.md).

`workouts` is an append-only log, so every form a session ever had is still a row and the
lineage links them (DESIGN_workout_revisions.md §4). This renders those rows as the
`History` block at the bottom of the event: every revision except the one the event is
showing, newest first.

The renderer is pure text over raw revision dicts — what `db.get_lineage_revisions`
returns, with the change's `kind`, `created_at` and `summary` joined on. `for_workout` is
the one function that reaches for the database.
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

from trainmate import intensity
from trainmate import runtime
from trainmate.util import fmt_date, fmt_timestamp

# Google's hard ceiling on an event description. The history is rendered last, into
# whatever the rest of the event leaves, so a long prescription is never what gets cut.
MAX_DESCRIPTION = 8192

# And a softer ceiling of its own: the event is a thing to read on a phone, not an
# archive, so the history yields well before the API would make it (§7).
HISTORY_BUDGET = 4000

SEPARATOR = "─" * 29

# Continuation lines sit under the entry's `[3/4] ` marker.
_INDENT = " " * 6

# What happened to the session at this revision, as a past-tense verb (§3).
_KIND_LABELS = {
    "generate": "Planned",
    "adapt": "Adapted",
    "swap": "Moved",
    "add": "Added by hand",
    "rm": "Cancelled",
    "restore": "Restored",
    "rollback": "Rolled back",
    "stand-down": "Stood down",
    "reinstate": "Reinstated",
}

# A void needs its own word: the same emptying reads as "cancelled" when the athlete asked
# for it and as "dropped" when a regeneration stopped scheduling the day (§3).
_VOID_LABELS = {
    "generate": "Dropped from the plan",
    "adapt": "Dropped by the adaptation",
    "swap": "Moved away",
    "rm": "Cancelled",
    "rollback": "Undone",
    "stand-down": "Goal stood down",
}


def _label(revision: Dict[str, Any]) -> str:
    kind = revision.get("kind") or "?"
    table = _VOID_LABELS if revision.get("void") else _KIND_LABELS
    return table.get(kind, kind)


def _load(revision: Dict[str, Any]) -> Optional[str]:
    """The load line, in the same shape as the one at the top of the event, so the two
    compare by eye."""
    parts = []
    for field, label, suffix in (
        ("duration_minutes", "Duration", "m"), ("tss", "TSS", ""), ("rpe", "RPE", ""),
    ):
        value = revision.get(field)
        if value is not None:
            parts.append(f"{label}: {value}{suffix}")
    return " | ".join(parts) if parts else None


def _entry(revision: Dict[str, Any], position: int, total: int) -> str:
    """One revision as a labelled block (§3).

    A void is rendered lean — it carries the departing session's columns forward, and
    printing them would state a prescription for a day that holds no session."""
    void = bool(revision.get("void"))
    header = (
        f"[{position}/{total}] {_label(revision)} · "
        f"{fmt_timestamp(revision.get('change_created_at'))}"
    )
    lines: List[str] = [f"{fmt_date(revision.get('date'))} · {revision.get('title') or ''}"]
    if not void:
        for line in (_load(revision), intensity.format_planned_zones(revision)):
            if line:
                lines.append(line)
    reason = (revision.get("reason") or "").strip()
    if reason:
        lines.append(f"Reason: {reason}")
    summary = (revision.get("change_summary") or "").strip()
    if summary and summary != reason:
        lines.append(f"Change: {summary}")
    body = (revision.get("description") or "").strip()
    if body and not void:
        lines.append(body)
    indented = [_INDENT + part for line in lines for part in line.split("\n")]
    return "\n".join([header] + indented)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _earlier(
    revisions: Sequence[Dict[str, Any]], head_revision_id: int
) -> List[Tuple[int, Dict[str, Any]]]:
    """Every revision but the one being rendered, newest first, each with its 1-based
    position in the lineage."""
    numbered = [(i + 1, r) for i, r in enumerate(revisions) if r["id"] != head_revision_id]
    return list(reversed(numbered))


def history_block(
    revisions: Sequence[Dict[str, Any]], head_revision_id: int,
    budget: Optional[int] = None,
) -> Optional[str]:
    """The `History` section of an event description, or None when there is no history.

    Entries are laid down newest first until the §7 budget runs out; what does not fit is
    stated as a count rather than dropped silently. `budget` is the room the rest of the
    event left, and never raises the standing ceiling."""
    earlier = _earlier(revisions, head_revision_id)
    if not earlier:
        return None
    header = f"History · {_plural(len(earlier), 'earlier revision')}, newest first"
    room = min(HISTORY_BUDGET, budget if budget is not None else HISTORY_BUDGET)
    room -= len(SEPARATOR) + len(header) + 2
    total = len(revisions)
    entries: List[str] = []
    used = 0
    for position, revision in earlier:
        text = _entry(revision, position, total)
        if used + len(text) + 2 > room:
            break
        entries.append(text)
        used += len(text) + 2
    # The count of what was dropped is worth more than one more entry: an event that
    # quietly stops at revision 6 of 20 reads as a complete history. It has to fit too,
    # so entries give way to it rather than the other way round.
    while len(entries) < len(earlier):
        note = f"… {_plural(len(earlier) - len(entries), 'earlier revision')} not shown."
        if used + len(note) + 2 <= room:
            entries.append(note)
            break
        if not entries:
            return None
        used -= len(entries.pop()) + 2
    if not entries:
        return None
    return "\n\n".join([f"{SEPARATOR}\n{header}"] + entries)


def for_workout(
    workout: Dict[str, Any], budget: Optional[int] = None
) -> Optional[str]:
    """The history block for a hydrated session, read from its lineage.

    `id` is the lineage and `revision_id` the live row it speaks through
    (DESIGN_workout_revisions.md §5); a dict carrying neither — a synthetic one in a test,
    or a session already torn out of the log — simply has no history."""
    lineage_id = workout.get("id")
    revision_id = workout.get("revision_id")
    if lineage_id is None or revision_id is None:
        return None
    return history_block(
        runtime.db.get_lineage_revisions(lineage_id), revision_id, budget
    )
