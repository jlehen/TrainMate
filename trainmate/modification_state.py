"""Derived modification state for workouts.

A workout's *modified?* fact is explicit — `modification_reason IS NOT NULL`. But the
*kind* of modification was, until now, inferred ad hoc from which columns were set
(`adaptation_summary IS NOT NULL` ⟺ from `workout adapt`). This centralizes that
inference into one named accessor so call sites stop reading raw columns, mirroring
`trainmate.calendar_state`.

Like the calendar axis, the kind is *derived*, not stored: a stored `modification_kind`
column would be a hand-maintained denormalization every write path had to keep correct —
the exact footgun the calendar rework removed. The information is fully recoverable from
columns that are already load-bearing, checked in this order:

    unmodified  modification_reason IS NULL
    adapted     adaptation_summary IS NOT NULL          (from `workout adapt`)
    swapped     date != original_date                   (a date move; `workout swap`)
    replaced    source == 'manual'                      (in-place manual replace)
    adapted     otherwise                               (legacy adapt: generated row
                                                         whose pre-split rationale lives
                                                         in modification_reason, no summary)

Two subtleties: `adapted` takes precedence over `swapped` — a session adapted and later
swapped keeps its `adaptation_summary`, so it still reads `adapted` (the swap text lives
in `modification_reason`). And the catch-all is `adapted`, not `replaced`: a manual
replace always stamps `source='manual'`, so a *generated* row that is modified without a
summary or a date move can only be a legacy adapt from before the rationale split (commit
4b2b053). See [[workout-calendar-state-redesign]].
"""

from typing import Literal

ModificationStatus = Literal["unmodified", "adapted", "swapped", "replaced"]


def modification_status(workout) -> ModificationStatus:
    """Classifies how a workout came to differ from its originally-planned form."""
    if not workout.get("modification_reason"):
        return "unmodified"
    if workout.get("adaptation_summary"):
        return "adapted"
    original_date = workout.get("original_date") or workout.get("date")
    if workout.get("date") != original_date:
        return "swapped"
    if workout.get("source") == "manual":
        return "replaced"
    return "adapted"  # legacy adapt: rationale in modification_reason, no summary
