"""Derived modification state for workouts.

A workout's *modified?* fact is explicit — `modification_reason IS NOT NULL`. But the
*kind* of modification was, until now, inferred ad hoc from which columns were set
(`adaptation_summary IS NOT NULL` ⟺ from `workout adapt`). This centralizes that
inference into one named accessor so call sites stop reading raw columns, mirroring
`trainmate.calendar_state`.

Like the calendar axis, the kind is *derived*, not stored: a stored `modification_kind`
column would be a hand-maintained denormalization every write path had to keep correct —
the exact footgun the calendar rework removed.

Classification (checked in order):

    unmodified  modification_reason IS NULL
    adapted     adaptation_summary IS NOT NULL          (post-split `workout adapt`)
    swapped     reason starts SWAP_REASON_PREFIX, or date != original_date
    replaced    reason starts MANUAL_REPLACE_REASON_PREFIX, or source == 'manual'
    adapted     otherwise                               (legacy adapt; see below)

**Why the deterministic reason prefixes, not just the columns.** `workout swap` and
`workout add` write a fixed, machine-readable prefix into `modification_reason`
(`SWAP_REASON_PREFIX` / `MANUAL_REPLACE_REASON_PREFIX` below) — `workout adapt` instead
writes a free-form LLM rationale. The column-only signals each have a blind spot the
prefix covers:
  - *swap:* `date != original_date` is the clean signal, but the `original_date` backfill
    migration set `original_date = date` on legacy rows, erasing the move for any swap
    done before that column existed. Such a row would otherwise fall to the catch-all and
    misread as `adapted`. The prefix recovers it.
  - *replace:* `source == 'manual'` is reliable today, but the prefix is belt-and-braces
    if `source` is ever missing.
The prefixes are shared constants imported by the writer (`coach/service.py`) so the two
sides cannot drift; changing the user-facing wording there must keep the prefix intact
(there is a test asserting the writer's output still starts with each prefix).

**The catch-all is `adapted`, not `replaced`.** A generated row that is modified with no
summary, no swap/replace prefix, no date move, and not `source='manual'` can only be a
*legacy adapt* — one made before commit 4b2b053 split the rationale into
`modification_reason` (short note) + `adaptation_summary` (batch reason); before that an
adapt put the whole rationale in `modification_reason` and left the summary NULL.

`adapted` takes precedence over `swapped`: a session adapted and later swapped keeps its
`adaptation_summary`, so it still reads `adapted` (the swap text lives in
`modification_reason`). See [[workout-calendar-state-redesign]].
"""

from typing import Literal

# Deterministic prefixes the writers stamp onto `modification_reason`. Imported by
# coach/service.py so the writer and this classifier share one source of truth.
SWAP_REASON_PREFIX = "Swapped from "
MANUAL_REPLACE_REASON_PREFIX = "Manually replaced previous "

ModificationStatus = Literal["unmodified", "adapted", "swapped", "replaced"]


def modification_status(workout) -> ModificationStatus:
    """Classifies how a workout came to differ from its originally-planned form."""
    reason = workout.get("modification_reason")
    if not reason:
        return "unmodified"
    if workout.get("adaptation_summary"):
        return "adapted"
    original_date = workout.get("original_date") or workout.get("date")
    if reason.startswith(SWAP_REASON_PREFIX) or workout.get("date") != original_date:
        return "swapped"
    if reason.startswith(MANUAL_REPLACE_REASON_PREFIX) or workout.get("source") == "manual":
        return "replaced"
    return "adapted"  # legacy adapt: rationale in modification_reason, no summary
