import os
from flask import Flask, jsonify, request, send_from_directory
from datetime import datetime, timezone
from typing import Any, Dict
from trainmate.db import db
from trainmate.google_calendar import calendar_syncer
from trainmate.coach import coach_service
from trainmate.config import config

app = Flask(__name__, static_folder="static", static_url_path="")

# --- Static Routes ---
@app.route("/")
def index() -> Any:
    """Serves the front-end dashboard index.html page."""
    return send_from_directory("static", "index.html")

# --- API Routes ---

@app.route("/api/status", methods=["GET"])
def get_status() -> Any:
    """API endpoint to retrieve overall athlete status, memories, and metrics."""
    objectives = db.get_objectives(status='active')
    next_goal = None
    if objectives:
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]
        
    metrics = db.get_metrics_cache()
    last_metrics = metrics[-1] if metrics else None
    
    # Get last baseline
    last_baseline = db.get_baseline(last_metrics['date']) if last_metrics else None
    
    learnings = db.get_learnings()

    macrocycle = None
    mesocycles = []
    config_mismatch = False
    if next_goal and next_goal['id'] is not None:
        macrocycle = db.get_macrocycle_for_objective(next_goal['id'])
        if macrocycle:
            mesocycles = db.get_mesocycles_for_macrocycle(macrocycle['id'])
            current_hash = coach_service._get_config_hash()
            config_mismatch = macrocycle.get('config_hash') != current_hash
            
    # The web app is a pure reader — it never pulls from Garmin (see
    # DESIGN_garmin_direct_pull.md §11). Surface the watermark so the UI can show
    # how fresh the cached data is; the CLI (or a cron `data pull`) owns syncing.
    sync_state = db.get_sync_state()

    return jsonify({
        "next_goal": next_goal,
        "last_metrics": last_metrics,
        "last_baseline": last_baseline,
        "coach_learnings": {
            "learnings": learnings
        },
        "macrocycle": macrocycle,
        "mesocycles": mesocycles,
        "config_mismatch": config_mismatch,
        "sync_state": sync_state
    })

@app.route("/api/objectives", methods=["GET", "POST"])
def manage_objectives() -> Any:
    """API endpoint to list or create objectives."""
    if request.method == "POST":
        data = request.json
        if not data:
            return jsonify({"error": "Missing payload"}), 400
            
        title = data.get("title")
        t_date = data.get("target_date")
        s_type = data.get("sport_type")
        if not title or not t_date or not s_type:
            return jsonify({"error": "Missing title, target_date, or sport_type"}), 400
            
        if isinstance(s_type, list):
            s_type = ",".join(s_type)
            
        obj_id = db.add_objective(
            title=title,
            target_date=t_date,
            sport_type=s_type,
            description=data.get("description", ""),
            priority=int(data.get("priority", 1)),
            status=data.get("status", "active")
        )
        return jsonify({"id": obj_id, "message": "Objective added successfully."}), 201
        
    # GET method
    return jsonify(db.get_objectives())

@app.route("/api/objectives/<int:obj_id>", methods=["DELETE", "PUT"])
def single_objective(obj_id: int) -> Any:
    """API endpoint to update or delete a specific objective."""
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
def manage_life_events() -> Any:
    """API endpoint to list active life events or add a new life event."""
    if request.method == "POST":
        data = request.json
        if not data:
            return jsonify({"error": "Missing payload"}), 400
            
        title = data.get("title")
        start = data.get("start_date")
        end = data.get("end_date")
        e_type = data.get("event_type")
        if not title or not start or not end or not e_type:
            return jsonify({"error": "Missing title, start_date, end_date, or event_type"}), 400
            
        event_id = db.add_lifeevent(
            title=title,
            start_date=start,
            end_date=end,
            event_type=e_type,
            impact_description=data.get("impact_description", "")
        )
        return jsonify({"id": event_id, "message": "Life event logged successfully."}), 201
        
    # GET method
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return jsonify(db.get_lifeevents(start_after=today_str))

@app.route("/api/life-events/<int:event_id>", methods=["DELETE", "PUT"])
def single_life_event(event_id: int) -> Any:
    """API endpoint to update or delete a specific life event."""
    if request.method == "DELETE":
        db.delete_lifeevent(event_id)
        return jsonify({"message": "Life event deleted."})
    elif request.method == "PUT":
        data = request.json
        if not data:
            return jsonify({"error": "No update fields provided"}), 400
        db.update_lifeevent(event_id, **data)
        return jsonify({"message": "Life event updated."})

@app.route("/api/workouts", methods=["GET"])
def get_workouts() -> Any:
    """API endpoint to list workouts within an optional date range."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return jsonify(db.get_workouts(start_date=start_date, end_date=end_date))

@app.route("/api/plan", methods=["POST"])
def generate_plan() -> Any:
    """API endpoint to generate the periodization plan (macro/meso strategy)."""
    try:
        data = request.json or {}
        goal_id = data.get("goal_id")
        if goal_id is not None:
            goal_id = int(goal_id)
        strategy, mesocycles, _ = coach_service.plan_generate(
            objective_id=goal_id
        )
        return jsonify({
            "message": "Periodization plan generated and saved.",
            "strategy": strategy,
            "mesocycles_count": len(mesocycles)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/plan/<int:goal_id>", methods=["DELETE"])
def plan_rm(goal_id: int) -> Any:
    """API endpoint to delete the periodization plan for a specific goal."""
    try:
        coach_service.plan_rm(goal_id)
        msg = f"Periodization plan for goal {goal_id} deleted successfully."
        return jsonify({"message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/macrocycles/<int:macro_id>/feedback", methods=["POST"])
def save_macrocycle_feedback(macro_id: int) -> Any:
    """API endpoint to save athlete feedback for a specific macrocycle."""
    try:
        data = request.json or {}
        feedback = data.get("feedback", "")
        db.update_macrocycle_feedback(macro_id, feedback)
        return jsonify({"message": "Macrocycle feedback saved successfully."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/mesocycles/<int:meso_id>/feedback", methods=["POST"])
def save_mesocycle_feedback(meso_id: int) -> Any:
    """API endpoint to save athlete feedback for a specific mesocycle block."""
    try:
        data = request.json or {}
        feedback = data.get("feedback", "")
        db.update_mesocycle_feedback(meso_id, feedback)
        return jsonify({"message": "Mesocycle feedback saved successfully."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/workouts/generate", methods=["POST"])
def workout_generate() -> Any:
    """API endpoint to generate workouts (microcycles) based on active strategy."""
    try:
        data = request.json or {}
        goal_id = data.get("goal_id")
        if goal_id is not None:
            goal_id = int(goal_id)
        reasoning, workouts = coach_service.workout_generate(objective_id=goal_id)
        return jsonify({
            "message": "Workouts generated and saved.",
            "reasoning": reasoning,
            "workouts_count": len(workouts)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/adapt", methods=["POST"])
def workout_adapt() -> Any:
    """API endpoint to run daily Garmin fatigue checks and adapt workouts."""
    data = request.json or {}
    date_str = data.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        reason, adapted_workout = coach_service.workout_adapt(date_str)
        return jsonify({
            "message": "Daily adaptation check finished.",
            "reason": reason,
            "adapted": bool(adapted_workout),
            "workout": adapted_workout
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/workouts/push", methods=["POST"])
def sync_calendar() -> Any:
    """API endpoint to synchronize planned and adapted workouts with Google Calendar."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    planned_workouts = db.get_workouts(start_date=today_str)
    # Get unsynced workouts
    unsynced = [w for w in planned_workouts if not w['synced']]
    
    if not unsynced:
        return jsonify({
            "message": "No planned or modified workouts to sync.",
            "synced_count": 0
        })
        
    try:
        calendar_syncer.sync_multiple(unsynced)
        return jsonify({
            "message": "Google Calendar sync complete.",
            "synced_count": len(unsynced)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/metrics/pull", methods=["POST"])
def sync_metrics() -> Any:
    """Garmin pulls are CLI-only (the web app is a pure reader, see
    DESIGN_garmin_direct_pull.md §11). Garmin login can require an interactive MFA
    prompt, so syncing must run in a terminal."""
    return jsonify({
        "error": "Garmin sync runs from the CLI, not the web app.",
        "command": "python trainmate_cli.py data pull"
    }), 409

@app.route("/api/metrics", methods=["GET"])
def get_metrics() -> Any:
    """API endpoint to fetch the last 30 days of cached Garmin metrics."""
    # Return last 30 days of metrics for visualization
    metrics = db.get_metrics_cache()
    return jsonify(metrics[-30:] if metrics else [])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
