import os
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from typing import Any, Dict, List
from trainmate import garmin
from trainmate.db import db
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.calendar_state import calendar_status
from trainmate.modification_state import modification_status
from trainmate.google_calendar import calendar_syncer
from trainmate.coach import coach_service
from trainmate.config import config
from trainmate.util import today_str

app = Flask(__name__, static_folder="static", static_url_path="")


# --- Static Routes ---
@app.route("/")
def index() -> Any:
    """Serves the front-end dashboard index.html page."""
    return send_from_directory("static", "index.html")


# --- Helpers ---

def _annotate_workout(workout: Dict[str, Any]) -> Dict[str, Any]:
    """Adds the *derived* calendar + modification facts the CLI shows as
    [SYNCED]/[STALE] and [ADAPTED]/[SWAPPED]/[REPLACED] markers (ARCHITECTURE.md
    §5). These are computed from the row, never stored — so the frontend renders
    them without re-deriving the rules (and risking drift from the writers)."""
    return {
        **workout,
        "calendar_status": calendar_status(workout),
        "modification_status": modification_status(workout),
    }


def _learnings_summary(learnings: List[Dict[str, Any]]) -> Dict[str, int]:
    """Computes the active/dormant/pending counts shown by `status` (status.py)."""
    active = [l for l in learnings if not l.get("dormant")]
    return {
        "active": len(active),
        "dormant": len(learnings) - len(active),
        "pending_demotion": sum(1 for l in learnings if l.get("proposed_confidence")),
        "total": len(learnings),
    }


# --- API Routes ---

@app.route("/api/status", methods=["GET"])
def get_status() -> Any:
    """API endpoint to retrieve overall athlete status, learnings, and metrics."""
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
            "learnings": learnings,
            "summary": _learnings_summary(learnings),
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

    # GET method — upcoming events (anchored on the machine-local day, like the CLI).
    return jsonify(db.get_lifeevents(start_after=today_str()))


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


# --- Workouts ---

@app.route("/api/workouts", methods=["GET", "POST"])
def manage_workouts() -> Any:
    """List workouts in a date range, or manually schedule a session.

    POST mirrors `workout add` (CoachService.workout_add): a deterministic,
    LLM-free manual schedule that replaces any same-sport session that day and
    syncs the calendar event in place (ARCHITECTURE.md §11)."""
    if request.method == "POST":
        data = request.json or {}
        date = data.get("date")
        sport_type = data.get("sport_type")
        title = data.get("title")
        description = data.get("description", "")
        if not date or not sport_type or not title:
            return jsonify({"error": "Missing date, sport_type, or title"}), 400

        def _opt_int(key):
            val = data.get(key)
            return int(val) if val not in (None, "") else None

        try:
            saved, replaced = coach_service.workout_add(
                date=date,
                sport_type=sport_type,
                title=title,
                description=description,
                duration_minutes=_opt_int("duration_minutes"),
                rpe=_opt_int("rpe"),
                tss=_opt_int("tss"),
                reason=data.get("reason"),
            )
            return jsonify({
                "message": "Workout scheduled.",
                "workout": saved,
                "replaced": replaced,
            }), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # GET method
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    include_removed = request.args.get("include_removed", "").lower() in ("1", "true", "yes")
    sport_type = request.args.get("sport_type") or None
    workouts = db.get_workouts(
        start_date=start_date, end_date=end_date,
        sport_type=sport_type, include_removed=include_removed,
    )
    return jsonify([_annotate_workout(w) for w in workouts])


@app.route("/api/workouts/<int:workout_id>/remove", methods=["POST"])
def remove_workout(workout_id: int) -> Any:
    """Soft-removes a workout (mirrors `workout rm`): marks it removed, keeps it in
    the DB, and updates the Calendar event to be marked deleted. `reason` optional."""
    data = request.json or {}
    workout = db.get_workout_by_id(workout_id)
    if not workout:
        return jsonify({"error": f"Workout {workout_id} not found."}), 404
    if workout.get("removed"):
        return jsonify({"message": "Workout is already removed."})

    db.mark_workout_removed(workout_id, reason=data.get("reason"))

    if workout.get("google_event_id"):
        updated = db.get_workout_by_id(workout_id)
        if updated is not None:
            try:
                calendar_syncer.sync_workout(updated)
            except Exception as e:
                return jsonify({
                    "message": "Workout removed locally; calendar update failed.",
                    "warning": str(e),
                })
    return jsonify({"message": "Workout removed."})


@app.route("/api/workouts/<int:workout_id>/restore", methods=["POST"])
def restore_workout(workout_id: int) -> Any:
    """Restores a soft-removed workout (mirrors `workout restore`) and refreshes the
    Calendar event to drop the deleted mark."""
    workout = db.get_workout_by_id(workout_id)
    if not workout:
        return jsonify({"error": f"Workout {workout_id} not found."}), 404
    if not workout.get("removed"):
        return jsonify({"message": "Workout is not removed."})

    db.restore_workout(workout_id)

    if workout.get("google_event_id"):
        updated = db.get_workout_by_id(workout_id)
        if updated is not None:
            try:
                calendar_syncer.sync_workout(updated)
            except Exception as e:
                return jsonify({
                    "message": "Workout restored locally; calendar update failed.",
                    "warning": str(e),
                })
    return jsonify({"message": "Workout restored."})


@app.route("/api/workouts/swap", methods=["POST"])
def swap_workouts() -> Any:
    """Swaps two workouts' dates (mirrors `workout swap`). Validates first: if the
    swap raises sports-science warnings and `force` is not set, returns them without
    applying so the caller can confirm. `reason` is required, as in the CLI."""
    data = request.json or {}
    ops = data.get("ops")
    reason = data.get("reason")
    force = bool(data.get("force"))
    no_sync = bool(data.get("no_sync"))

    if not ops or not isinstance(ops, list):
        return jsonify({"error": "Missing 'ops' (list of {id, new_date})."}), 400
    if not reason:
        return jsonify({"error": "A reason is required for a swap."}), 400
    try:
        ops = [{"id": int(op["id"]), "new_date": op["new_date"]} for op in ops]
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Each op must be {id, new_date}."}), 400

    try:
        warnings = coach_service.workout_swap_validate(ops)
        if warnings and not force:
            return jsonify({"applied": False, "warnings": warnings})

        updated = coach_service.workout_swap_apply(ops, no_sync, reason=reason)
        return jsonify({
            "applied": True,
            "warnings": warnings,
            "workouts": updated,
            "message": f"Swapped {len(updated)} workout(s).",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workouts/compare", methods=["GET"])
def compare_workouts() -> Any:
    """Plan-vs-actual adherence over a date range (mirrors `workout compare`,
    trainmate/cli/workouts.py). Pure reader: unlike the CLI it never calls
    `garmin.ensure_data` — the web app is a read-only consumer of cached data
    (ARCHITECTURE.md §8). Reuses `analyze_adherence`; returns a day-by-day
    structure so the frontend renders without re-deriving any logic.

    Query params: ?start_date=&end_date= (default 14-day lookback ending today,
    end capped at today), optional ?sport= filter."""
    today = today_str()
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    sport_filter = (request.args.get("sport") or "").lower() or None

    if not start_date:
        start_date = (
            datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=13)
        ).strftime("%Y-%m-%d")
    # We can only compare past/present activities.
    if not end_date or end_date > today:
        end_date = today

    start_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end_obj < start_obj:
        return jsonify({"error": "end_date is before start_date."}), 400
    history_days = (end_obj - start_obj).days + 1

    all_workouts = db.get_workouts(start_date=start_date, end_date=end_date)
    activities = db.get_completed_activities(start_date=start_date, end_date=end_date)
    covered_ranges = db.get_mesocycle_ranges(start_date, end_date)
    threshold = config.minor_activity_load_threshold

    discrepancies, matching_results, informational = analyze_adherence(
        planned_workouts=all_workouts,
        completed_activities=activities,
        start_date_obj=start_obj,
        history_days=history_days,
        minor_activity_load_threshold=threshold,
        covered_ranges=covered_ranges,
    )

    matched_act_ids = {
        r["completed"]["activity_id"] for r in matching_results if r["completed"]
    }
    acts_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for act in activities:
        acts_by_date.setdefault(act["date"], []).append(act)
    results_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for r in matching_results:
        results_by_date.setdefault(r["date"], []).append(r)

    days: List[Dict[str, Any]] = []
    for d in range(history_days):
        date_curr = (start_obj + timedelta(days=d)).strftime("%Y-%m-%d")
        day_results = results_by_date.get(date_curr, [])
        day_acts = acts_by_date.get(date_curr, [])
        unplanned = [a for a in day_acts if a["activity_id"] not in matched_act_ids]

        if sport_filter:
            day_results = [
                r for r in day_results
                if r["planned"]["sport_type"].lower() == sport_filter
            ]
            unplanned = [
                a for a in unplanned if sport_filter in a["activity_type"].lower()
            ]

        if not day_results and not unplanned:
            continue

        results_out = []
        for r in day_results:
            w = r["planned"]
            act = r["completed"]
            is_rest = w["sport_type"] == "rest"
            results_out.append({
                "planned": w,
                "completed": act,
                "is_rest": is_rest,
                "rest_violation": bool(is_rest and act),
            })

        unplanned_out = []
        for act in unplanned:
            load = garmin.activity_load(act)
            if load < threshold:
                kind = "minor"
            elif date_covered(date_curr, covered_ranges):
                kind = "unplanned"
            else:
                kind = "off_plan"
            unplanned_out.append({
                "activity": act,
                "load": round(load, 1),
                "rpe_divergence": garmin.rpe_divergence(act),
                "kind": kind,
            })

        days.append({
            "date": date_curr,
            "results": results_out,
            "unplanned": unplanned_out,
        })

    return jsonify({
        "filters": {
            "start_date": start_date,
            "end_date": end_date,
            "sport": sport_filter,
        },
        "days": days,
        "discrepancies": discrepancies,
        "informational": informational,
    })


# --- Plan & Workout Generation ---

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


def _resolve_goal_id(raw: Any) -> Any:
    """Resolves a goal id from a request payload/query, defaulting to the next active
    goal (earliest target date) — mirrors the CLI's plan-command goal resolution."""
    if raw is not None and raw != "":
        return int(raw)
    objectives = db.get_objectives(status='active')
    if not objectives:
        return None
    objectives.sort(key=lambda x: str(x['target_date']))
    return objectives[0]['id']


@app.route("/api/plan/versions", methods=["GET"])
def plan_versions() -> Any:
    """Lists every kept periodization plan version (active + superseded) for a goal,
    for the web equivalent of `plan versions` (see DESIGN_plan_rollback.md)."""
    goal_id = _resolve_goal_id(request.args.get("goal_id"))
    if goal_id is None:
        return jsonify({"goal": None, "versions": []})
    goal = db.get_objective(goal_id)
    versions = db.get_macrocycle_versions(goal_id)
    return jsonify({"goal": goal, "versions": versions})


@app.route("/api/plan/rollback", methods=["POST"])
def plan_rollback() -> Any:
    """Restores a superseded plan version and its workouts (web equivalent of
    `plan rollback`). Body: {goal_id?, version?}. Defaults to the previous version of
    the next active goal. See DESIGN_plan_rollback.md."""
    data = request.json or {}
    goal_id = _resolve_goal_id(data.get("goal_id"))
    if goal_id is None:
        return jsonify({"error": "No goal to roll back."}), 400
    version = data.get("version")
    if version is not None and version != "":
        version = int(version)
    else:
        version = None
    try:
        result = coach_service.plan_rollback(
            objective_id=goal_id, target_macrocycle_id=version
        )
        return jsonify({
            "message": (
                f"Rolled back to plan ID {result['to']['id']} "
                f"(was {result['from']['id']}). Restored "
                f"{result['restored_workouts']} workout(s), archived "
                f"{result['archived_workouts']}; Google Calendar updated."
            ),
            "to_id": result['to']['id'],
            "from_id": result['from']['id'],
            "restored_workouts": result['restored_workouts'],
            "archived_workouts": result['archived_workouts'],
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
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
            "message": "Workouts generated and pushed to Google Calendar.",
            "reasoning": reasoning,
            "workouts_count": len(workouts)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Adaptation ---

@app.route("/api/adapt", methods=["POST"])
def workout_adapt() -> Any:
    """Runs the daily adaptation check (read-only). Returns the proposed workouts;
    apply them via POST /api/adapt/apply. Mirrors the CLI `workout adapt` flow."""
    data = request.json or {}
    date_str = data.get("date") or today_str()
    try:
        reason, proposed = coach_service.workout_adapt(date_str)
        return jsonify({
            "message": "Daily adaptation check finished.",
            "reason": reason,
            "change_needed": bool(proposed),
            "workouts": proposed,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/adapt/apply", methods=["POST"])
def workout_adapt_apply() -> Any:
    """Applies proposed adaptations from POST /api/adapt: saves them, cleans up the
    overridden sessions, and syncs the affected range to Calendar."""
    data = request.json or {}
    proposed = data.get("workouts")
    reason = data.get("reason", "")
    if not proposed:
        return jsonify({"error": "No proposed workouts to apply."}), 400
    try:
        all_dates = [w["date"] for w in proposed]
        coach_service.workout_adapt_apply(
            proposed, reason, min(all_dates), max(all_dates)
        )
        return jsonify({
            "message": f"Applied {len(proposed)} adapted workout(s) and synced.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workouts/push", methods=["POST"])
def sync_calendar() -> Any:
    """API endpoint to synchronize planned and adapted workouts with Google Calendar."""
    planned_workouts = db.get_workouts(start_date=today_str())
    # Push eligibility = anything not currently in sync with Calendar (ARCHITECTURE.md §5).
    unsynced = [w for w in planned_workouts if calendar_status(w) != 'synced']

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


# --- Coach Learnings ---

@app.route("/api/learnings", methods=["GET"])
def get_learnings() -> Any:
    """Lists coach learnings (mirrors `learnings list`). Each carries its computed
    `dormant` flag, `sports`/`confidence`, and any `proposed_confidence` downgrade.
    Optional filters: ?sport=&confidence=&dormant=1."""
    learnings = db.get_learnings()

    if request.args.get("dormant", "").lower() in ("1", "true", "yes"):
        learnings = [l for l in learnings if l.get("dormant")]
    sport = request.args.get("sport")
    if sport:
        needle = sport.lower()
        learnings = [l for l in learnings if needle in (l.get("sports") or "general").lower()]
    confidence = request.args.get("confidence")
    if confidence:
        learnings = [l for l in learnings if (l.get("confidence") or "tentative") == confidence]

    return jsonify({
        "learnings": learnings,
        "summary": _learnings_summary(db.get_learnings()),
    })


@app.route("/api/learnings/<int:learning_id>/evidence", methods=["GET"])
def get_learning_evidence(learning_id: int) -> Any:
    """Returns the per-week evidence basis behind a learning's confidence
    (mirrors `learnings show`)."""
    learning = next((l for l in db.get_learnings() if l['id'] == learning_id), None)
    if not learning:
        return jsonify({"error": f"Learning {learning_id} not found."}), 404
    evidence = db.get_learning_evidence(learning_id)
    return jsonify({
        "learning": learning,
        "supporting": [e for e in evidence if e['polarity'] >= 0],
        "contradicting": [e for e in evidence if e['polarity'] < 0],
    })


@app.route("/api/learnings/<int:learning_id>", methods=["PUT", "DELETE"])
def single_learning(learning_id: int) -> Any:
    """Edit the text of, or delete, a single learning (mirrors `learnings edit`/`rm`)."""
    learning = next((l for l in db.get_learnings() if l['id'] == learning_id), None)
    if not learning:
        return jsonify({"error": f"Learning {learning_id} not found."}), 404

    if request.method == "DELETE":
        db.delete_learning(learning_id)
        return jsonify({"message": f"Learning {learning_id} removed."})

    data = request.json or {}
    text = data.get("text")
    if not text:
        return jsonify({"error": "Missing 'text'."}), 400
    db.update_learning(learning_id, text)
    return jsonify({"message": f"Learning {learning_id} updated."})


@app.route("/api/learnings/<int:learning_id>/demote", methods=["POST"])
def demote_learning(learning_id: int) -> Any:
    """Accepts a pending confidence downgrade (mirrors `learnings demote`)."""
    result = db.demote_learning(learning_id)
    if result is None:
        return jsonify({"message": "No pending demotion."})
    if result == "retired":
        return jsonify({"message": f"Learning {learning_id} retired."})
    return jsonify({"message": f"Learning {learning_id} demoted to '{result}'."})


@app.route("/api/learnings/<int:learning_id>/keep", methods=["POST"])
def keep_learning(learning_id: int) -> Any:
    """Dismisses + affirms a pending downgrade (mirrors `learnings keep`)."""
    learning = next((l for l in db.get_learnings() if l['id'] == learning_id), None)
    if not learning:
        return jsonify({"error": f"Learning {learning_id} not found."}), 404
    if not learning.get("proposed_confidence"):
        return jsonify({"message": "No pending demotion to dismiss."})
    db.keep_learning(learning_id)
    return jsonify({"message": f"Learning {learning_id} kept; pending demotion dismissed."})


# --- Activities, Metrics & Daily Context (read-only) ---

@app.route("/api/activities", methods=["GET"])
def get_activities() -> Any:
    """Completed activities over a date range (mirrors `data show-activities`)."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return jsonify(db.get_completed_activities(start_date=start_date, end_date=end_date))


@app.route("/api/daily-context", methods=["GET"])
def get_daily_context() -> Any:
    """External daily-context signals (alcohol/sleep/stress, ingested from Calendar)
    over a date range — see ARCHITECTURE.md §13."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return jsonify(db.get_daily_context(start_date=start_date, end_date=end_date))


@app.route("/api/metrics", methods=["GET"])
def get_metrics() -> Any:
    """Cached Garmin metrics. Date range via ?start_date=&end_date=, else last 30 days."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    if start_date or end_date:
        return jsonify(db.get_metrics_cache(start_date=start_date, end_date=end_date))
    metrics = db.get_metrics_cache()
    return jsonify(metrics[-30:] if metrics else [])


@app.route("/api/metrics/pull", methods=["POST"])
def sync_metrics() -> Any:
    """Garmin pulls are CLI-only (the web app is a pure reader, see
    DESIGN_garmin_direct_pull.md §11). Garmin login can require an interactive MFA
    prompt, so syncing must run in a terminal."""
    return jsonify({
        "error": "Garmin sync runs from the CLI, not the web app.",
        "command": "python trainmate_cli.py data pull"
    }), 409


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
