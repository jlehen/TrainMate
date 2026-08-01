"""Benchmark (fitness-test) anchor-kind vocabulary — the single source of truth for
what threshold kinds exist, their units, and which direction is "better".

A benchmark result records a threshold *anchor* (FTP, LTHR, threshold pace, CSS,
e1RM, MAS) in the dated logbook (DESIGN_benchmark_workouts.md §3.2). The design's
core stance is **no privileged anchor kinds** (§3.5): every kind flows through the
same machinery — recorded via `benchmark record`, rendered generically into the
coaching prompt, drift-checked by the same snapshot rule, trended by `benchmark
list`. Adding a future kind is a vocabulary addition here (a row below), not new
machinery elsewhere.

Kept dependency-free (like ``trainmate.sports``) so the DB, service, CLI and engine
layers can all import it without an import cycle.
"""
from typing import Dict, List, NamedTuple, Optional


class AnchorKind(NamedTuple):
    """One threshold anchor kind. `key` doubles as the ``anchor_kind`` stored in the
    logbook AND the profile-dict key the coaching prompt reads (§3.3/§3.5), so the two
    can never drift. `lower_is_better` carries the per-kind sign a pace anchor needs so
    a faster runner is never shown a negative-looking progression (§3.2)."""
    key: str
    label: str
    unit: str
    lower_is_better: bool


# The full anchor vocabulary. `max_hr` is included for rendering/drift because it is a
# threshold the prompt prescribes from and the staleness check tracks — but it is
# quasi-fixed physiology kept in config, NOT a trainable logbook kind (§3.4), so it is
# excluded from LOGBOOK_KINDS below.
ANCHOR_KINDS: Dict[str, AnchorKind] = {
    a.key: a for a in (
        AnchorKind("ftp", "Functional Threshold Power (FTP)", "W", False),
        AnchorKind("lthr", "Lactate Threshold HR (LTHR)", "bpm", False),
        AnchorKind("threshold_pace", "Threshold Pace", "min/km", True),
        AnchorKind("css", "Critical Swim Speed (CSS)", "sec/100m", True),
        AnchorKind("e1rm", "Estimated 1RM (e1RM)", "kg", False),
        AnchorKind("mas", "Maximal Aerobic Speed (MAS)", "km/h", False),
        AnchorKind("max_hr", "Max Heart Rate", "bpm", False),
    )
}

# The kinds the logbook records (everything except the config-resident `max_hr`). These
# are the `--<kind>` value flags `benchmark record` accepts.
LOGBOOK_KINDS: List[str] = [k for k in ANCHOR_KINDS if k != "max_hr"]

# The natural anchor kind(s) each sport is tested on, used for `status` display and to
# suggest a kind from a bare sport. Keyed by both canonical and loose sport spellings
# so `record cycling` and `record road_biking` both resolve.
SPORT_ANCHORS: Dict[str, List[str]] = {
    "road_biking": ["ftp"],
    "cycling": ["ftp"],
    "running": ["threshold_pace", "lthr"],
    "swimming": ["css"],
    "strength_training": ["e1rm"],
    "strength": ["e1rm"],
    "rowing": ["threshold_pace"],
    "general": ["mas"],
}


def anchor_for_kind(kind: str) -> Optional[AnchorKind]:
    """The AnchorKind for `kind`, or None if unknown."""
    return ANCHOR_KINDS.get(kind)


def unit_for_kind(kind: str) -> str:
    """The canonical unit string for `kind` (empty if unknown)."""
    a = ANCHOR_KINDS.get(kind)
    return a.unit if a else ""


def anchors_for_sport(sport: str) -> List[str]:
    """The natural anchor kind(s) for a sport spelling (empty if none known)."""
    return SPORT_ANCHORS.get((sport or "").strip().lower(), [])


def _fmt_pace(value: float, per: str) -> str:
    """Renders a pace value (stored as a float of the unit's base) as mm:ss.

    `min/km` stores minutes (4.25 -> 4:15); `sec/100m` stores seconds (95 -> 1:35)."""
    total_seconds = value * 60 if per == "min/km" else value
    total_seconds = int(round(total_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def format_value(kind: str, value: float) -> str:
    """Human string for a threshold value, e.g. ``250 W``, ``165 bpm``, ``4:15 min/km``,
    ``1:35 sec/100m``. Pace kinds render as mm:ss so they read naturally."""
    a = ANCHOR_KINDS.get(kind)
    unit = a.unit if a else ""
    if unit in ("min/km", "sec/100m"):
        return f"{_fmt_pace(value, unit)} {unit}"
    # Show integers without a trailing .0; keep one decimal otherwise.
    num = f"{value:g}"
    return f"{num} {unit}".strip()


def format_delta(kind: str, new: float, old: float) -> str:
    """Signed, direction-aware delta between two values of the same kind, e.g.
    ``+6.4%`` for a raised FTP or a quickened pace. The sign reflects IMPROVEMENT, not
    raw arithmetic: for a lower-is-better anchor (pace/CSS) a drop in the number is a
    positive change, so a faster runner never sees a negative-looking progression (§3.2).
    Returns an empty string when `old` is zero (no baseline to compare)."""
    if not old:
        return ""
    raw_pct = (new - old) / abs(old) * 100.0
    a = ANCHOR_KINDS.get(kind)
    if a and a.lower_is_better:
        raw_pct = -raw_pct
    return f"{raw_pct:+.1f}%"


def is_improvement(kind: str, new: float, old: float) -> bool:
    """Whether `new` is better than `old` for this kind's direction."""
    a = ANCHOR_KINDS.get(kind)
    if a and a.lower_is_better:
        return new < old
    return new > old
