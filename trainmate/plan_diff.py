"""Structural comparison of two periodization plan versions.

No formatting: `diff_plans` takes two macrocycle rows with their mesocycles and returns
what changed, which `cli/plans.py` renders as text and `trainmate_web.py` returns as JSON;
`resolve_versions` picks which two versions those are, over a database handle the caller
passes in. Also owns the parsing of a macrocycle's input snapshots (goals / constraints /
threshold anchors), since the diff is defined over them.
"""
import difflib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Abbreviations whose full stop does not end a sentence — the coach's prose is dense
# with "e.g." / "approx.", and splitting there fragments the diff mid-clause.
_SENTENCE_SPLIT = re.compile(
    r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)(?<!\bapprox)(?<!\bmin)(?<!\bDr)"
    r"(?<=[.!?])\s+"
)

# Below this sentence-level similarity a prose block was rewritten rather than edited, so
# a sentence diff conveys nothing a reader could not get from the two texts side by side.
REWRITE_RATIO = 0.5

# Above this name similarity two mesocycles in the same slot are the same phase renamed.
RENAME_RATIO = 0.4


def input_snapshots(
    macrocycle: Dict[str, Any]
) -> Tuple[Optional[list], Optional[list], Optional[list], Optional[dict]]:
    """The goals, plan-shaping constraints, all active constraints, and threshold anchors
    snapshotted when the plan was generated (see db.save_macrocycle), or None each when
    that plan predates the column.

    Snapshots reflect the inputs the plan was actually built on rather than the current
    live records, which may have since changed. Plans predating the constraints rename
    fall back to the legacy lifeevents snapshot. `all_events` is every constraint active
    at generation time (each tagged with a `replan` flag), a superset of `events` — the
    prompt is built from all of them, but only the `replan = 1` subset fingerprints the
    plan (DESIGN_constraints.md §7) — and is None on plans predating that column even when
    `events` is present."""
    raw_goals = macrocycle.get('goals_snapshot')
    raw_events = (
        macrocycle.get('constraints_snapshot')
        or macrocycle.get('lifeevents_snapshot')
    )
    raw_all_events = macrocycle.get('all_constraints_snapshot')
    raw_config = macrocycle.get('config_snapshot')
    return (
        json.loads(raw_goals) if raw_goals else None,
        json.loads(raw_events) if raw_events else None,
        json.loads(raw_all_events) if raw_all_events else None,
        json.loads(raw_config) if raw_config else None,
    )


def resolve_versions(
    dbh: Any, goal: Dict[str, Any], a: Optional[int], b: Optional[int]
) -> Tuple[Optional[dict], Optional[dict], Optional[Tuple[str, str]]]:
    """The two plan versions to compare: the given IDs, the given one against the active
    plan, or — with neither — the version before the active one against it.

    Returns `(old, new, None)`, or `(None, None, (code, message))` when the pair cannot be
    formed. The message is front-end-neutral; the code lets a caller add its own follow-up
    ('no_active_plan', 'single_version', 'same_version', 'not_found')."""
    if b is None:
        active = dbh.get_macrocycle_for_objective(goal['id'])
        if not active:
            return None, None, (
                "no_active_plan",
                f"Goal '{goal['title']}' has no active periodization plan.",
            )
        b = active['id']
    if a is None:
        prev = dbh.get_previous_macrocycle_version(goal['id'])
        if not prev:
            return None, None, (
                "single_version",
                f"Goal '{goal['title']}' has only one plan version — nothing to compare "
                "it against.",
            )
        a = prev['id']
    if a == b:
        return None, None, (
            "same_version", f"Plan version {a} cannot be compared against itself."
        )
    resolved = []
    for vid in (a, b):
        macro = dbh.get_macrocycle(vid)
        if not macro or macro.get('objective_id') != goal['id']:
            return None, None, (
                "not_found",
                f"Plan version {vid} does not belong to goal '{goal['title']}'.",
            )
        resolved.append(macro)
    return resolved[0], resolved[1], None


def split_sentences(text: Optional[str]) -> List[str]:
    """Splits prose into sentences so a diff lands on meaning-sized units rather than on
    wrap-width artefacts."""
    return [s.strip() for s in _SENTENCE_SPLIT.split((text or "").strip()) if s.strip()]


def diff_prose(old: Optional[str], new: Optional[str]) -> Dict[str, Any]:
    """Compares two prose blobs sentence by sentence.

    `blocks` keeps the edits in reading order, each pairing the sentences dropped with
    those that replaced them. `rewritten` marks a block the coach re-authored wholesale
    rather than edited — the sentence lists are still returned, but a caller rendering
    for a human is better off collapsing them to a note."""
    a, b = split_sentences(old), split_sentences(new)
    matcher = difflib.SequenceMatcher(None, a, b)
    blocks = [
        {"removed": a[i1:i2], "added": b[j1:j2]}
        for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != 'equal'
    ]
    return {
        "changed": bool(blocks),
        "rewritten": bool(a and b and blocks and matcher.ratio() < REWRITE_RATIO),
        "old_count": len(a),
        "new_count": len(b),
        "blocks": blocks,
    }


def _dates(m: Dict[str, Any]) -> Dict[str, str]:
    return {"start": m['start_date'], "end": m['end_date']}


def _diff_mesocycle_pair(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Compares two mesocycles taken to be the same phase across versions."""
    fields = [
        {"field": key, "from": old.get(key) or None, "to": new.get(key) or None}
        for key in ('phase',)
        if (old.get(key) or None) != (new.get(key) or None)
    ]
    dates = None
    if _dates(old) != _dates(new):
        dates = {"from": _dates(old), "to": _dates(new)}
    focus = diff_prose(old.get('focus'), new.get('focus'))
    renamed = old['name'] != new['name']
    changed = bool(fields or dates or focus['changed'] or renamed)
    return {
        "change": "changed" if changed else "unchanged",
        "renamed": renamed,
        "name": new['name'],
        "from_name": old['name'],
        "ids": {"from": old.get('id'), "to": new.get('id')},
        "dates": dates,
        "fields": fields,
        "focus": focus if focus['changed'] else None,
    }


def _added_or_removed(m: Dict[str, Any], change: str) -> Dict[str, Any]:
    return {
        "change": change,
        "name": m['name'],
        "id": m.get('id'),
        "dates": _dates(m),
        "focus_text": m.get('focus'),
    }


def diff_mesocycles(
    old: List[Dict[str, Any]], new: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Aligns two versions' mesocycle lists by name and reports each block's fate, in
    reading order. Unchanged blocks are included (`change: "unchanged"`) so a caller can
    show the whole timeline; the CLI filters them out.

    Within a replaced run the phases pair up positionally when their names still look
    related, so a rename reads as one change rather than an unrelated removal plus
    addition; a genuinely different phase in that slot stays a removal plus an addition."""
    matcher = difflib.SequenceMatcher(None, [m['name'] for m in old], [m['name'] for m in new])
    out: List[Dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            out.extend(_diff_mesocycle_pair(a, b) for a, b in zip(old[i1:i2], new[j1:j2]))
            continue
        pairs = min(i2 - i1, j2 - j1) if tag == 'replace' else 0
        for a, b in zip(old[i1:i1 + pairs], new[j1:j1 + pairs]):
            if difflib.SequenceMatcher(None, a['name'], b['name']).ratio() < RENAME_RATIO:
                out.append(_added_or_removed(a, "removed"))
                out.append(_added_or_removed(b, "added"))
                continue
            out.append(_diff_mesocycle_pair(a, b))
        out.extend(_added_or_removed(m, "removed") for m in old[i1 + pairs:i2])
        out.extend(_added_or_removed(m, "added") for m in new[j1 + pairs:j2])
    return out


def _missing_side(old: Any, new: Any) -> Optional[str]:
    """Which version lacks a snapshot entirely, so a plan predating the column is never
    read as everything having been added or removed."""
    if old is None and new is None:
        return "both"
    if old is None:
        return "old"
    if new is None:
        return "new"
    return None


def diff_records(old: Optional[list], new: Optional[list]) -> Dict[str, Any]:
    """Diffs two snapshotted input lists (goals / constraints) keyed by record ID."""
    missing = _missing_side(old, new)
    if missing:
        return {"missing": missing, "added": [], "removed": [], "changed": []}
    old_by_id = {r.get('id'): r for r in old}
    new_by_id = {r.get('id'): r for r in new}
    added, removed, changed = [], [], []
    for rid in sorted(set(old_by_id) | set(new_by_id), key=lambda x: (x is None, x)):
        a, b = old_by_id.get(rid), new_by_id.get(rid)
        if a is None:
            added.append(b)
        elif b is None:
            removed.append(a)
        else:
            fields = [
                {"field": k, "from": a.get(k), "to": b.get(k)}
                for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)
            ]
            if fields:
                changed.append({"id": rid, "title": b.get('title', ''), "fields": fields})
    return {"missing": None, "added": added, "removed": removed, "changed": changed}


def diff_thresholds(old: Optional[dict], new: Optional[dict]) -> Dict[str, Any]:
    """Diffs the threshold anchors the two versions were generated against, reporting
    relative drift — the quantity `CoachService.config_changed` judges staleness on."""
    missing = _missing_side(old, new)
    if missing:
        return {"missing": missing, "added": [], "removed": [], "changed": []}
    added, removed, changed = [], [], []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if a is None:
            added.append({"key": key, "value": b})
        elif b is None:
            removed.append({"key": key, "value": a})
        else:
            changed.append({
                "key": key, "from": a, "to": b,
                "pct": ((b - a) / a * 100.0) if a else None,
            })
    return {"missing": None, "added": added, "removed": removed, "changed": changed}


def diff_feedback(
    old_notes: Optional[list], new_notes: Optional[list]
) -> Dict[str, Any]:
    """The feedback notes attached to each version, side by side.

    Not a prose diff: the log is append-only, so a version's notes are a list rather than
    an edited blob — and for adjacent versions, A's notes are what drove B
    (DESIGN_plan_feedback.md §8)."""
    def note(n: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": n.get('id'),
            "date": str(n.get('created_at') or '')[:10],
            "filing": n.get('mesocycle_name'),
            "text": n.get('text'),
        }
    return {
        "from": [note(n) for n in old_notes or ()],
        "to": [note(n) for n in new_notes or ()],
    }


def diff_plans(
    old_macro: Dict[str, Any], new_macro: Dict[str, Any],
    old_mesos: List[Dict[str, Any]], new_mesos: List[Dict[str, Any]],
    old_notes: Optional[list] = None, new_notes: Optional[list] = None,
) -> Dict[str, Any]:
    """Everything that differs between two plan versions: the macrocycle's strategy, the
    feedback each carries, its mesocycle blocks, and the inputs each was generated from."""
    old_goals, old_events, _old_all_events, old_thresholds = input_snapshots(old_macro)
    new_goals, new_events, _new_all_events, new_thresholds = input_snapshots(new_macro)
    return {
        "from": {
            "id": old_macro['id'], "status": old_macro.get('status'),
            "created_at": old_macro.get('created_at'),
            "superseded_at": old_macro.get('superseded_at'),
        },
        "to": {
            "id": new_macro['id'], "status": new_macro.get('status'),
            "created_at": new_macro.get('created_at'),
            "superseded_at": new_macro.get('superseded_at'),
        },
        "strategy": diff_prose(old_macro.get('strategy'), new_macro.get('strategy')),
        "feedback": diff_feedback(old_notes, new_notes),
        "mesocycles": diff_mesocycles(old_mesos, new_mesos),
        "goals": diff_records(old_goals, new_goals),
        "constraints": diff_records(old_events, new_events),
        "thresholds": diff_thresholds(old_thresholds, new_thresholds),
    }
