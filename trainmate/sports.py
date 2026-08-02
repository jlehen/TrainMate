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
}

# Reverse index: every alias (and each canonical name) -> canonical name.
_ALIAS_TO_CANONICAL = {
    alias: canonical
    for canonical, aliases in SPORT_MAPPING.items()
    for alias in aliases
}


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
