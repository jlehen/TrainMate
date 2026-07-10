import os
import sys
from datetime import datetime, timedelta, timezone
from trainmate.db import db
from trainmate.google_calendar import calendar_syncer

def main() -> None:
    """Manually runs the calendar sync integration test with mocked/simulated database workouts."""
    print("=== STARTING INTEGRATION TESTS ===")
    
    # 1. Reset/Add mock objectives
    db.add_objective(
        title="Zurich Marathon",
        target_date="2026-10-15",
        sport_type="running",
        description="Target time: under 3:30:00",
        priority=1
    )
    goals = db.get_objectives()
    print(f"Goal database test: Found {len(goals)} objectives.")
    for g in goals:
        print(f"- Objective: {g['title']} on {g['target_date']}")

    # 2. Add mock constraint (a plan-shaping directive)
    db.add_constraint(
        title="Ibiza Vacation",
        start_date="2026-07-01",
        end_date="2026-07-08",
        description="Reduce volume by 50%",
        replan=1,
    )
    constraints = db.get_constraints()
    print(f"Constraints test: Found {len(constraints)} constraints.")

    # 3. Create simulated workouts (one normal, one adapted)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Workout 1: Standard Planned session
    db.save_workout(
        date=today_str,
        sport_type="running",
        title="Aerobic Base Run",
        description="45 minutes in HR Zone 2 (130-145 bpm). Steady flat pace.",
    )
    
    # Workout 2: Modified session
    db.save_workout(
        date=tomorrow_str,
        sport_type="road_biking",
        title="Active Recovery Spin",
        description="30 minutes of light cycling. Keep heart rate below 110 bpm.",
        original_description="90 minutes endurance road cycling with hill climbs.",
        modification_reason="HRV average dropped 1.2 SD below chronic baseline."
    )
    
    print("Workouts created in local SQLite database.")

    # 4. Sync workouts to Google Calendar
    print("Attempting to sync workouts to Google Calendar...")
    workouts_to_sync = db.get_workouts(start_date=today_str, end_date=tomorrow_str)
    
    try:
        synced_ids = calendar_syncer.sync_multiple(workouts_to_sync)
        print(f"Successfully synced {len(synced_ids)} events to Google Calendar.")
        print(f"Event IDs: {synced_ids}")
        
        # Verify sync status in local database
        synced_workouts = db.get_workouts(start_date=today_str, end_date=tomorrow_str)
        print("Database verify sync status:")
        for w in synced_workouts:
            print(
                f"- Date: {w['date']} | Synced: {w['synced']} | "
                f"Adapted: {bool(w['modification_reason'])} | "
                f"Google Event ID: {w['google_event_id']}"
            )
            
    except Exception as e:
        print(f"Google Calendar sync test FAILED: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
