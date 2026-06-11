import json
from datetime import datetime, timedelta
from typing import Any, List, Optional
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from trainmate.config import config
from trainmate.db import db
from trainmate.types import Workout

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
