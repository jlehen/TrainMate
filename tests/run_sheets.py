import os
import sys
import traceback
from trainmate.google_sheets import sheets_reader
from trainmate.db import db

def main() -> None:
    """Manually runs the Google Sheets Garmin data sync and prints db results."""
    print("Starting manual Garmin Google Sheets metrics synchronization...")
    
    # Check service account credentials
    credentials_file = 'service_account.json'
    if not os.path.exists(credentials_file):
        print(f"Warning: '{credentials_file}' not found. Google integration may fail.")
        
    try:
        sheets_reader.sync_data()
        print("Testing database results...")
        
        # Check metrics cache
        cached = db.get_metrics_cache()
        print(f"Total cached days: {len(cached)}")
        if cached:
            print("Sample day:", cached[-1])
            
            # Check baselines
            last_baseline = db.get_baseline(cached[-1]['date'])
            print("Sample baseline:", last_baseline)
        else:
            print("No daily metrics cached in database.")
            
    except Exception as e:
        traceback.print_exc()
        print("Failed to sync sheets data:", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
