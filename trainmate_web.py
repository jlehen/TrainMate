import os
from flask import Flask, jsonify, request, send_from_directory
from datetime import datetime, timezone
from trainmate.db import db
from trainmate.google_sheets import sheets_reader
from trainmate.google_calendar import calendar_syncer
from trainmate.coach import coach_engine
from trainmate.config import config

app = Flask(__name__, static_folder="static", static_url_path="")

# --- Static Routes ---
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# --- API Routes ---

@app.route("/api/status", methods=["GET"])
def get_status():
    objectives = db.get_objectives(status='active')
    next_goal = None
    if objectives:
        objectives.sort(key=lambda x: x['target_date'])
        next_goal = objectives[0]
        
    metrics = db.get_metrics_cache()
    last_metrics = metrics[-1] if metrics else None
    
    # Get last baseline
    last_baseline = db.get_baseline(last_metrics['date']) if last_metrics else None
    
    strategy = db.get_coach_memory("training_strategy")
    learnings = db.get_coach_memory("athlete_learnings")
    
    return jsonify({
        "next_goal": next_goal,
        "last_metrics": last_metrics,
        "last_baseline": last_baseline,
        "coach_memory": {
            "strategy": strategy,
            "learnings": learnings
        }
    })

@app.route("/api/objectives", methods=["GET", "POST"])
def manage_objectives():
    if request.method == "POST":
        data = request.json
        if not data or not data.get("title") or not data.get("target_date") or not data.get("sport_type"):
            return jsonify({"error": "Missing title, target_date, or sport_type"}), 400
            
        obj_id = db.add_objective(
            title=data["title"],
            target_date=data["target_date"],
            sport_type=data["sport_type"],
            description=data.get("description", ""),
            priority=int(data.get("priority", 1)),
            status=data.get("status", "active")
        )
        return jsonify({"id": obj_id, "message": "Objective added successfully."}), 201
        
    # GET method
    return jsonify(db.get_objectives())

@app.route("/api/objectives/<int:obj_id>", methods=["DELETE", "PUT"])
def single_objective(obj_id):
    if request.method == "DELETE":
        db.delete_objective(obj_id)
        return jsonify({"message": "Objective deleted."})
    elif request.method == "PUT":
        data = request.json
        if not data:
            return jsonify({"error": "No update fields provided"}), 400
        db.update_objective(obj_id, **data)
        return jsonify({"message": "Objective updated."})

@app.route("/api/life-events", methods=["GET", "POST"])
def manage_life_events():
    if request.method == "POST":
        data = request.json
        if not data or not data.get("title") or not data.get("start_date") or not data.get("end_date") or not data.get("event_type"):
            return jsonify({"error": "Missing title, start_date, end_date, or event_type"}), 400
            
        event_id = db.add_constraint(
            title=data["title"],
            start_date=data["start_date"],
            end_date=data["end_date"],
            event_type=data["event_type"],
            impact_description=data.get("impact_description", "")
        )
        return jsonify({"id": event_id, "message": "Constraint logged successfully."}), 201
        
    # GET method
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return jsonify(db.get_constraints(start_after=today_str))

@app.route("/api/life-events/<int:event_id>", methods=["DELETE"])
def delete_life_event(event_id):
    db.delete_constraint(event_id)
    return jsonify({"message": "Constraint deleted."})

@app.route("/api/workouts", methods=["GET"])
def get_workouts():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return jsonify(db.get_workouts(start_date=start_date, end_date=end_date))

@app.route("/api/plan", methods=["POST"])
def generate_plan():
    try:
        reasoning, workouts = coach_engine.replan()
        return jsonify({
            "message": "Plan generated and saved.",
            "reasoning": reasoning,
            "workouts_count": len(workouts)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/adapt", methods=["POST"])
def adapt():
    data = request.json or {}
    date_str = data.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        reason, adapted_workout = coach_engine.adapt(date_str)
        return jsonify({
            "message": "Daily adaptation check finished.",
            "reason": reason,
            "adapted": bool(adapted_workout),
            "workout": adapted_workout
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sync", methods=["POST"])
def sync_calendar():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    planned_workouts = db.get_workouts(start_date=today_str)
    # Get unsynced workouts
    unsynced = [w for w in planned_workouts if w['status'] in ('planned', 'modified')]
    
    if not unsynced:
        return jsonify({"message": "No planned or modified workouts to sync.", "synced_count": 0})
        
    try:
        calendar_syncer.sync_multiple(unsynced)
        return jsonify({
            "message": "Google Calendar sync complete.",
            "synced_count": len(unsynced)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sync-sheets", methods=["POST"])
def sync_sheets():
    try:
        sheets_reader.sync_data()
        return jsonify({"message": "Garmin metrics synchronized from Google Sheets."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/metrics", methods=["GET"])
def get_metrics():
    # Return last 30 days of metrics for visualization
    metrics = db.get_metrics_cache()
    return jsonify(metrics[-30:] if metrics else [])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
