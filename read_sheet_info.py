import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = 'service_account.json'
SPREADSHEET_ID = '17G0Rt9H-DR__f3miJNXIH7Ay0dE3FKv51HT9unkZrnc'

scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=scopes)

service = build('sheets', 'v4', credentials=creds)

# Call the Sheets API
sheet = service.spreadsheets()
result = sheet.get(spreadsheetId=SPREADSHEET_ID).execute()
print("Spreadsheet Title:", result.get('properties', {}).get('title'))
sheets = result.get('sheets', [])
print("Sheets (tabs) list:")
for s in sheets:
    props = s.get('properties', {})
    print(f"- {props.get('title')} (ID: {props.get('sheetId')})")

# Let's inspect the first few rows of each tab
for s in sheets:
    title = s.get('properties', {}).get('title')
    try:
        data_res = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=f"'{title}'!A1:K10").execute()
        values = data_res.get('values', [])
        print(f"\n--- Data sample for '{title}' (first 10 rows) ---")
        for row in values:
            print(row)
    except Exception as e:
        print(f"Error reading {title}: {e}")
