import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = 'service_account.json'
CALENDAR_ID = 'a05efe90aef057e16bf8f423500dda43244ae61b3bd1a9990f55cade1f4e8549@group.calendar.google.com'

scopes = ['https://www.googleapis.com/auth/calendar']
try:
    with open(SERVICE_ACCOUNT_FILE) as f:
        sa_data = json.load(f)
    print("Service Account Email:", sa_data.get("client_email"))
except Exception as e:
    print("Error reading service account file:", e)
    sa_data = None

if sa_data:
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=scopes)
        service = build('calendar', 'v3', credentials=creds)
        
        print(f"Attempting to fetch events from calendar: {CALENDAR_ID}...")
        events_result = service.events().list(calendarId=CALENDAR_ID, maxResults=10).execute()
        events = events_result.get('items', [])
        print(f"Successfully connected! Found {len(events)} events.")
        for event in events:
            print(f"- {event.get('summary')} ({event.get('start', {}).get('dateTime', event.get('start', {}).get('date'))})")
    except Exception as e:
        print(f"Error accessing calendar: {e}")
