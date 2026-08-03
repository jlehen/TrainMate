"""Canonical sport-type vocabulary shared across planning, matching and storage.

A single planned/canonical sport maps to the family of Garmin activity types (and
historical free-form spellings) that count as the same sport. The coach's prompts
and generated/adapted workouts speak the canonical names (e.g. ``strength_training``);
manually-added or legacy rows may use an alias (e.g. ``strength``). Normalizing at
write time and matching alias-aware on read keeps the two in agreement so a session
is never duplicated or lost just because two layers spelled its sport differently.

Kept dependency-free so both the DB layer and the adherence/analysis layer can import
it without an import cycle (adherence -> garmin -> trainmate.db).
"""
from typing import List

# Maps planned (canonical) workout types to the Garmin activity types and accepted
# spellings that count as the same sport. The canonical name appears first in its own
# alias list, so it round-trips to itself.
SPORT_MAPPING = {
    "running": ["running", "indoor_running", "trail_running", "treadmill_running"],
    # Road, gravel, cyclocross, MTB and BMX share one set of Garmin cycling zone
    # boundaries, which is what qualifies them for a single row
    # (DESIGN_intensity_distribution.md §6.1).
    "cycling": [
        "cycling", "road_cycling", "road_biking", "gravel_cycling",
        "mountain_biking", "cyclocross", "bmx", "indoor_cycling",
        "virtual_ride", "biking",
    ],
    "hiking": ["hiking", "walking"],
    "strength_training": ["strength_training", "strength", "indoor_cardio", "fitness"],
    "yoga": ["yoga", "stretching", "pilates"],
    "ski_touring": ["ski_touring", "backcountry_skiing", "nordic_skiing", "skiing"],
    "rowing": ["rowing", "indoor_rowing", "rowing_v2", "indoor_rowing_v2", "erg", "ergometer"],
    # Lift-served descending, kept apart from `ski_touring`: no sustained climb, so the
    # load profile and the coach's prescriptions differ. Garmin files skiing and
    # snowboarding under one resort type, and both ride the same lifts, so they share a
    # canonical sport here too.
    "downhill_skiing": [
        "downhill_skiing", "resort_skiing", "resort_snowboarding",
        "resort_skiing_snowboarding", "resort_skiing_snowboarding_ws",
        "alpine_skiing", "snowboarding",
    ],
}

# The planned/canonical sports, in declaration order. Single source of truth for the CLI's
# `--sport` choices and the coach prompts' `sport_type` enum, so a new sport above reaches
# both without a second edit.
CANONICAL_SPORTS: List[str] = list(SPORT_MAPPING)

# Reverse index: every alias (and each canonical name) -> canonical name.
_ALIAS_TO_CANONICAL = {
    alias: canonical
    for canonical, aliases in SPORT_MAPPING.items()
    for alias in aliases
}


# Sports whose HR trace is dominated by rest between sets rather than by effort. The
# caveat travelling with their zone rows is suppressed when none of them is on screen
# (DESIGN_intensity_distribution.md §9.6); the interval-work half of that caveat is not,
# because interval work with rest happens in running, cycling and rowing alike.
STRENGTH_SPORTS = frozenset({"strength_training"})


def is_strength_sport(value: str) -> bool:
    """Whether `value` names a sport whose HR rows need the rest-between-sets caveat."""
    return canonical_sport(value) in STRENGTH_SPORTS


def canonical_sport(value: str) -> str:
    """Maps any sport-type spelling (canonical or alias, any case) to its canonical
    name. Unknown sports pass through stripped + lowercased so new sports still
    round-trip consistently."""
    if not value:
        return value
    key = value.strip().lower()
    return _ALIAS_TO_CANONICAL.get(key, key)


def sport_aliases(value: str) -> List[str]:
    """All sport-type spellings (lowercased) equivalent to `value`, including itself.
    Unknown sports return just their own normalized form."""
    canon = canonical_sport(value)
    return SPORT_MAPPING.get(canon, [canon])
