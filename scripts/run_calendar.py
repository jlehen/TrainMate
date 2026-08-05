import os
import sys
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = 'service_account.json'
CALENDAR_ID = (
    'a05efe90aef057e16bf8f423500dda43244ae61b3bd1a9990f55cade1f4e8549'
    '@group.calendar.google.com'
)
SCOPES = ['https://www.googleapis.com/auth/calendar']

def main() -> None:
    """Manually checks Google Calendar credentials and fetches recent events."""
    print("Starting manual Google Calendar connection verification...")
    
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(f"Error: '{SERVICE_ACCOUNT_FILE}' file not found.")
        sys.exit(1)
        
    try:
        with open(SERVICE_ACCOUNT_FILE, encoding='utf-8') as f:
            sa_data = json.load(f)
        print("Service Account Email:", sa_data.get("client_email"))
    except Exception as e:
        print("Error reading service account file:", e)
        sys.exit(1)
        
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        service = build('calendar', 'v3', credentials=creds)
        
        print(f"Attempting to fetch events from calendar: {CALENDAR_ID}...")
        events_result = service.events().list(
            calendarId=CALENDAR_ID, maxResults=10
        ).execute()
        events = events_result.get('items', [])
        print(f"Successfully connected! Found {len(events)} events.")
        for event in events:
            start_info = event.get('start', {})
            date_val = start_info.get('dateTime', start_info.get('date'))
            print(f"- {event.get('summary')} ({date_val})")
    except Exception as e:
        print(f"Error accessing calendar: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
