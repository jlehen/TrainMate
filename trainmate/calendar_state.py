"""Derived calendar-sync state for workouts.

The freshness of a workout's Google Calendar event is *derived*, not stored: on
a successful push we record `pushed_signature` = a hash of exactly the fields
that determine the rendered event. The current state then falls out of a compare
against `calendar_signature(current_row)`:

    unpushed  google_event_id is NULL            (never sent to Calendar)
    synced    pushed_signature == current hash    (event matches local content)
    stale     pushed_signature != current hash    (edited since last push)

This replaces a hand-maintained `synced` boolean that every write path had to
remember to reset — miss it once and the calendar silently drifted. Now any edit
through any path leaves `pushed_signature` untouched and the row reads `stale`
automatically. `Workout` is a TypedDict, so these are free functions, not methods.
"""

import hashlib
import json
from typing import Literal

# Exactly the fields `google_calendar.sync_workout` renders into the event's
# summary/description. Deliberately EXCLUDES rpe (never reaches Calendar, so
# editing it must not mark a row stale) and the periodization footer (derives
# deterministically from the date and effectively never changes).
CALENDAR_FIELDS = (
    "date", "sport_type", "title", "description", "original_description",
    "modification_reason", "duration_minutes", "tss", "removed",
    "removed_reason", "source",
)

CalendarStatus = Literal["unpushed", "synced", "stale"]


def calendar_signature(workout) -> str:
    """Stable hash of the calendar-relevant fields of a workout."""
    payload = []
    for field in CALENDAR_FIELDS:
        value = workout.get(field)
        if field == "removed":
            value = bool(value)  # normalize 0/1/None/False to a stable bool
        payload.append(value)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def adherence_signature(workout, adherence) -> str:
    """Stable hash of a workout's calendar fields *plus* its backward-looking
    adherence verdict, as rendered by `workout compare --mark`.

    `mark_adherence_from_results` stores this on each adherence push and compares
    the prospective signature against it to skip a no-op Calendar update when the
    event already carries the same verdict. Kept distinct from `calendar_signature`
    (and stored in its own `marked_signature` column) because the adherence verdict
    isn't a workout field — folding it into the freshness hash would make every
    marked past row read `stale`. `adherence` is the dict built in
    `mark_adherence_from_results`: ``{"status", "actual", "reasons"}``.
    """
    payload = [calendar_signature(workout)]
    payload.append(adherence.get("status") if adherence else None)
    payload.append(adherence.get("actual") if adherence else None)
    payload.append(list(adherence.get("reasons") or []) if adherence else None)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def calendar_status(workout) -> CalendarStatus:
    """Derives {unpushed, synced, stale} from the row's stored signature."""
    if not workout.get("google_event_id"):
        return "unpushed"
    if workout.get("pushed_signature") == calendar_signature(workout):
        return "synced"
    return "stale"
