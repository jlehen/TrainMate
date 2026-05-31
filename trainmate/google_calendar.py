import json
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from trainmate.config import config
from trainmate.db import db

class CalendarSyncer:
    def __init__(self):
        self.scopes = ['https://www.googleapis.com/auth/calendar']
        self.creds = service_account.Credentials.from_service_account_file(
            config.service_account_file, scopes=self.scopes)
        self.service = build('calendar', 'v3', credentials=self.creds)
        self.calendar_id = config.google_calendar_id

    def sync_workout(self, workout):
        """Syncs a single workout to Google Calendar (creating or updating)."""
        date_str = workout['date']
        sport_type = workout['sport_type']
        title = workout['title']
        description = workout['description']
        orig_description = workout.get('original_description') or description
        status = workout.get('status', 'planned')
        mod_reason = workout.get('modification_reason')
        google_event_id = workout.get('google_event_id')

        # Calculate end date (exclusive for all-day events: start_date + 1 day)
        start_date = datetime.strptime(date_str, "%Y-%m-%d")
        end_date = start_date + timedelta(days=1)
        end_date_str = end_date.strftime("%Y-%m-%d")

        # Format Summary and Description
        is_modified = status == 'modified' or bool(mod_reason)
        if is_modified:
            summary = f"[Adapted] {title}"
            event_description = f"Originally:\n{orig_description}\n\nAdapted:\n{description}\n\nReason:\n{mod_reason}"
        else:
            summary = title
            event_description = description

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
                print(f"Updated existing calendar event for {date_str} ({sport_type}): {updated_event.get('htmlLink')}")
                
                # Mark as synced in DB
                db.save_workout(
                    date=date_str,
                    sport_type=sport_type,
                    title=title,
                    description=description,
                    original_description=orig_description,
                    status='synced',
                    modification_reason=mod_reason,
                    google_event_id=google_event_id
                )
                return google_event_id
            except Exception as e:
                print(f"Warning: Failed to update event {google_event_id}, creating a new one: {e}")
                # Fall through to insert new event if update failed (e.g. event deleted on calendar)

        # Create a new event
        try:
            created_event = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event_body
            ).execute()
            new_event_id = created_event.get('id')
            print(f"Created new calendar event for {date_str} ({sport_type}): {created_event.get('htmlLink')}")
            
            # Save the new event ID and mark as synced in DB
            db.save_workout(
                date=date_str,
                sport_type=sport_type,
                title=title,
                description=description,
                original_description=orig_description,
                status='synced',
                modification_reason=mod_reason,
                google_event_id=new_event_id
            )
            return new_event_id
        except Exception as e:
            print(f"Error inserting event to Google Calendar: {e}")
            raise e

    def sync_multiple(self, workouts):
        """Syncs a list of workouts sequentially."""
        synced_ids = []
        for w in workouts:
            eid = self.sync_workout(w)
            synced_ids.append(eid)
        return synced_ids

    def delete_workout_event(self, google_event_id):
        """Deletes a workout event from Google Calendar."""
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
