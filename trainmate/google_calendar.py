import json
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from trainmate.config import config
from trainmate.db import db
from trainmate.types import Workout
from trainmate.util import yellow


class CalendarSyncer:
    """Synchronizes planned and adapted workouts to Google Calendar as all-day events."""

    def __init__(self) -> None:
        """Initializes Google API calendar service using configured service account."""
        self.scopes: List[str] = ['https://www.googleapis.com/auth/calendar']
        self.creds: service_account.Credentials = (
            service_account.Credentials.from_service_account_file(
                config.service_account_file, scopes=self.scopes
            )
        )
        self.service: Any = build('calendar', 'v3', credentials=self.creds)
        self.calendar_id: Optional[str] = config.google_calendar_id

    def sync_workout(self, workout: Workout) -> Optional[str]:
        """Syncs a single workout to Google Calendar (creating or updating).

        Args:
            workout: The Workout details to synchronize.

        Returns:
            The Google Calendar event ID if sync was successful, or None.

        Raises:
            HttpError: If API call fails.
        """
        date_str = workout['date']
        sport_type = workout['sport_type']
        title = workout['title']
        description = workout['description']
        orig_description = workout.get('original_description') or description
        mod_reason = workout.get('modification_reason')
        google_event_id = workout.get('google_event_id')

        # Calculate end date (exclusive for all-day events: start_date + 1 day)
        start_date = datetime.strptime(date_str, "%Y-%m-%d")
        end_date = start_date + timedelta(days=1)
        end_date_str = end_date.strftime("%Y-%m-%d")

        # Format Summary and Description
        is_modified = bool(mod_reason)
        content_changed = is_modified and orig_description != description
        is_manual = workout.get('source') == 'manual'
        if workout.get('removed'):
            summary = f"[Deleted] {title}"
            event_description = description or ""
            removed_reason = workout.get('removed_reason')
            if removed_reason:
                event_description = f"{event_description}\n\nReason:\n{removed_reason}"
        elif content_changed:
            summary = f"[Adapted] {title}"
            event_description = (
                f"Adapted:\n{description}\n\n"
                f"Originally:\n{orig_description}\n\n"
                f"Reason:\n{mod_reason}"
            )
        elif is_modified:
            # Date-only change (e.g. a swap): the content is unchanged, so showing
            # "Adapted"/"Originally" with identical text is redundant. Show it once.
            summary = f"[Adapted] {title}"
            event_description = f"{description or ''}\n\nReason:\n{mod_reason}"
        else:
            summary = title
            event_description = description or ""

        # Flag manually-added sessions so they're distinguishable at a glance from
        # coach-generated ones. The marker composes with any "[Adapted]" tag above
        # (a manual add can replace an existing session); a deleted event keeps the
        # "[Deleted]" framing alone since its removal is the salient state.
        if is_manual and not workout.get('removed'):
            summary = f"[Manual] {summary}"

        # Prepend duration and tss to description if available
        duration = workout.get('duration_minutes')
        tss = workout.get('tss')
        prefix_parts = []
        if duration is not None:
            prefix_parts.append(f"Duration: {duration}m")
        if tss is not None:
            prefix_parts.append(f"TSS: {tss}")
        prefix = " | ".join(prefix_parts)
        if prefix:
            if event_description:
                event_description = f"{prefix}\n\n{event_description}"
            else:
                event_description = prefix

        # Append an identifier footer so each event stays traceable back to the plan
        # that produced it: goal/macro/meso are resolved from the workout's date, the
        # workout id comes from the row itself.
        id_parts = []
        ids = db.get_periodization_ids_for_date(date_str)
        if ids:
            objective_id, macrocycle_id, mesocycle_id = ids
            id_parts.append(f"Goal: {objective_id}")
            id_parts.append(f"Macro: {macrocycle_id}")
            id_parts.append(f"Meso: {mesocycle_id}")
        workout_id = workout.get('id')
        if workout_id is not None:
            id_parts.append(f"Workout: {workout_id}")
        if id_parts:
            footer = " | ".join(id_parts)
            if event_description:
                event_description = f"{event_description}\n\n{footer}"
            else:
                event_description = footer

        event_body = {
            'summary': summary,
            'description': event_description,
            'start': {
                'date': date_str,
            },
            'end': {
                'date': end_date_str,
            },
            # Add metadata tag to identify TrainMate events
            'extendedProperties': {
                'private': {
                    'source': 'TrainMate',
                    'sport_type': sport_type
                }
            }
        }

        # If we have a saved google_event_id, try updating it
        if google_event_id:
            try:
                updated_event = self.service.events().update(
                    calendarId=self.calendar_id,
                    eventId=google_event_id,
                    body=event_body
                ).execute()
                print(
                    f"Updated existing calendar event for {date_str} ({sport_type}): "
                    f"{updated_event.get('htmlLink')}"
                )
                
                # Mark as synced in DB
                db.save_workout(
                    date=date_str,
                    sport_type=sport_type,
                    title=title,
                    description=description or "",
                    original_description=orig_description,
                    synced=True,
                    modification_reason=mod_reason,
                    google_event_id=google_event_id,
                    duration_minutes=duration,
                    rpe=workout.get('rpe'),
                    tss=tss,
                    original_date=workout.get('original_date'),
                    removed=workout.get('removed', False),
                    removed_reason=workout.get('removed_reason')
                )
                return google_event_id
            except HttpError as e:
                if e.resp.status in (404, 410):
                    print(
                        f"Warning: Calendar event {google_event_id} was deleted on Google "
                        f"Calendar. Re-creating a new one..."
                    )
                    # Fall through to insert new event
                else:
                    print(f"Error updating Google Calendar event: {e}")
                    raise e
            except Exception as e:
                print(f"Error updating Google Calendar event: {e}")
                raise e

        # Create a new event
        try:
            created_event = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event_body
            ).execute()
            new_event_id = created_event.get('id')
            print(
                f"Created new calendar event for {date_str} ({sport_type}): "
                f"{created_event.get('htmlLink')}"
            )
            
            # Save the new event ID and mark as synced in DB
            db.save_workout(
                date=date_str,
                sport_type=sport_type,
                title=title,
                description=description or "",
                original_description=orig_description,
                synced=True,
                modification_reason=mod_reason,
                google_event_id=new_event_id,
                duration_minutes=duration,
                rpe=workout.get('rpe'),
                tss=tss,
                original_date=workout.get('original_date'),
                removed=workout.get('removed', False),
                removed_reason=workout.get('removed_reason')
            )
            return str(new_event_id)
        except Exception as e:
            print(f"Error inserting event to Google Calendar: {e}")
            raise e

    def sync_multiple(self, workouts: List[Workout]) -> List[Optional[str]]:
        """Syncs a list of workouts sequentially.

        Args:
            workouts: List of Workout objects to sync.

        Returns:
            A list of event IDs synced.
        """
        synced_ids = []
        for w in workouts:
            eid = self.sync_workout(w)
            synced_ids.append(eid)
        return synced_ids

    # ------------------------------------------------------------------
    # Inbound: external daily-context signals (alcohol, sleep, stress, …)
    # ------------------------------------------------------------------
    def sync_context(self) -> int:
        """Pulls tagged daily-context events from the calendar into `daily_context`.

        Incremental via `syncToken` (edit and delete detection come for free); falls
        back to a full pull of *all* tagged events on first run or when the token has
        expired (HTTP 410). The server-side `privateExtendedProperty` filter means only
        context events ever enter the stream — workouts and private appointments don't.
        Returns the number of rows upserted or deleted
        (DESIGN_calendar_context_ingest.md §6).
        """
        if not self.calendar_id:
            return 0

        state = db.get_sync_state(key="calendar_context")
        use_token: Optional[str] = state.get("sync_token") if state else None

        changed = 0
        page_token: Optional[str] = None
        next_sync_token: Optional[str] = None
        while True:
            params: dict = {
                'calendarId': self.calendar_id,
                'showDeleted': True,
                'singleEvents': True,
                'maxResults': 250,
            }
            # syncToken and the initial-sync params are mutually exclusive beyond
            # pageToken; reuse the *same* base params so the token stays valid.
            if use_token:
                params['syncToken'] = use_token
            else:
                params['privateExtendedProperty'] = f"source={config.calendar_context_tag}"

            if page_token:
                params['pageToken'] = page_token
            try:
                resp = self.service.events().list(**params).execute()
            except HttpError as e:
                if e.resp.status == 410 and use_token:
                    # Expired token: discard it and restart with a full pull.
                    use_token = None
                    page_token = None
                    changed = 0
                    continue
                raise
            for event in resp.get('items', []):
                changed += self._ingest_context_event(event)
            page_token = resp.get('nextPageToken')
            if not page_token:
                next_sync_token = resp.get('nextSyncToken')
                break

        db.set_sync_state(
            through_date=None,
            last_pull_utc=datetime.now(timezone.utc).isoformat(),
            key="calendar_context",
            sync_token=next_sync_token,
        )
        return changed

    def _ingest_context_event(self, event: dict) -> int:
        """Reconciles a single context event into `daily_context`. A cancelled event
        deletes its row; otherwise the row is upserted by event id. Returns 1 if the DB
        changed, else 0."""
        event_id = event.get('id')
        if not event_id:
            return 0
        if event.get('status') == 'cancelled':
            db.delete_daily_context_by_event(event_id)
            return 1

        private = (event.get('extendedProperties', {}) or {}).get('private', {}) or {}
        # The server-side filter should guarantee this, but a shared calendar or a
        # token stream can still surprise us — skip anything not actually ours.
        if private.get('source') != config.calendar_context_tag:
            return 0

        start = event.get('start', {}) or {}
        date = start.get('date') or (start.get('dateTime') or "")[:10]
        if not date:
            return 0

        db.upsert_daily_context_by_event(
            google_event_id=event_id,
            date=date,
            metric=private.get('metric') or 'context',
            value=self._parse_float(private.get('value')),
            text=self._context_text(event),
            updated=event.get('updated'),
        )
        return 1

    @staticmethod
    def _parse_float(raw: Any) -> Optional[float]:
        """Best-effort parse of the optional numeric `value` tag; None when absent/bad."""
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _context_text(event: dict) -> Optional[str]:
        """The human blurb handed to the coach: summary then description, trimmed."""
        parts = [event.get('summary'), event.get('description')]
        text = "\n".join(p.strip() for p in parts if p and p.strip())
        return text or None

    def delete_workout_event(self, google_event_id: str) -> None:
        """Deletes a workout event from Google Calendar.

        Args:
            google_event_id: The event ID to delete.
        """
        try:
            self.service.events().delete(
                calendarId=self.calendar_id,
                eventId=google_event_id
            ).execute()
            print(f"Deleted Google Calendar event {google_event_id}.")
        except Exception as e:
            print(f"Warning: Failed to delete Google Calendar event {google_event_id}: {e}")

# Singleton instance
calendar_syncer = CalendarSyncer()


# Per-process memo: a single CLI command syncs context at most once.
_context_synced: bool = False


def sync_calendar_context(force: bool = False) -> None:
    """Refreshes daily context from the calendar, gating/throttling the actual sync.

    Rides along with `data pull` (force=True) and the auto-ensure-before-read path
    (force=False, where it runs at most once per process and skips entirely while the
    last sync is still fresh). Best-effort: no calendar configured is a silent no-op,
    and any Calendar error is swallowed with a warning so a data read never blocks
    (DESIGN_calendar_context_ingest.md §6).
    """
    global _context_synced
    if not config.google_calendar_id:
        return
    if not force:
        if _context_synced:
            return
        # Skip if a recent sync already covers the freshness window (shared with the
        # Garmin metric-refresh cadence — "how fresh is fresh enough").
        state = db.get_sync_state(key="calendar_context")
        if state and state.get("last_pull_utc"):
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(
                    state["last_pull_utc"]
                )
                if age <= timedelta(minutes=config.garmin_refresh_minutes):
                    _context_synced = True
                    return
            except (ValueError, TypeError):
                pass
    try:
        calendar_syncer.sync_context()
        _context_synced = True
    except Exception as e:
        print(yellow(f"Warning: calendar context sync skipped: {e}"))
