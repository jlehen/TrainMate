import os
import sqlite3
from trainmate.google_sheets import sheets_reader
from trainmate.db import db

try:
    sheets_reader.sync_data()
    print("Testing database results...")
    
    # Check metrics cache
    cached = db.get_metrics_cache()
    print(f"Total cached days: {len(cached)}")
    if cached:
        print("Sample day:", cached[-1])
        
    # Check baselines
    last_baseline = db.get_baseline(cached[-1]['date']) if cached else None
    print("Sample baseline:", last_baseline)
    
except Exception as e:
    import traceback
    traceback.print_exc()
    print("Failed to sync sheets data:", e)
