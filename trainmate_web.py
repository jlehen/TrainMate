"""The read-only web dashboard (ARCHITECTURE.md §8).

The web app READS. It never writes a row, never calls Garmin, never calls the LLM and
never touches Google Calendar — every one of those is a CLI (or bot) action. The rule is
enforced by `_reject_writes` below rather than left to convention, because the previous
"tracks the CLI feature set" contract silently decayed the moment a feature landed
CLI-first: parity was true once, in June 2026, and nothing re-established it. A dashboard
that only reads has no parity to lose — new CLI commands add a view here when their data
is worth looking at, and cost nothing when it is not.

What that buys, concretely: no request can leave the database in a state the CLI did not
put it in, so the web app is safe to leave running, safe to expose on the LAN, and cannot
race the CLI or the bot over a workout row.
"""
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from typing import Any, Dict, List, Optional
from trainmate import runtime
from trainmate import benchmarks, garmin, intensity, llm_models, plan_diff, progression
from trainmate.adherence import analyze_adherence, classify_adherence, date_covered
from trainmate.calendar_state import calendar_status
from trainmate.cli.common import adherence_verdicts
from trainmate.cli.workouts._helpers import modification_markers
from trainmate.config import config, plan_config_hash
from trainmate.sports import canonical_sport
from trainmate.util import today_str

app = Flask(__name__, static_folder="static", static_url_path="")


# Every mutating verb, refused in one place. A route that wants to write has to delete
# this guard first — which is the point: the invariant is visible, not remembered.
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.before_request
def _reject_writes() -> Any:
    """405s any mutating verb. The dashboard is a reader (module docstring); scheduling,
    generating, adapting and syncing all run from the CLI."""
    if request.method in _WRITE_METHODS:
        return jsonify({
            "error": "The web dashboard is read-only; this action runs from the CLI.",
            "method": request.method,
            "path": request.path,
        }), 405
    return None


# --- Static Routes ---
@app.route("/")
def index() -> Any:
    """Serves the front-end dashboard index.html page."""
    return send_from_directory("static", "index.html")


# --- Helpers ---

def _annotate_workout(
    workout: Dict[str, Any], adherence: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Adds the *derived* calendar fact the CLI shows as [SYNCED]/[STALE], the
    modification markers it shows as [ADAPTED ×2]/[SWAPPED]/…, and — for a session
    today or earlier — the adherence verdict it shows as [DONE]/[MISSED]/…
    (ARCHITECTURE.md §5).

    All three are computed here, never stored, so the frontend renders them without
    re-deriving the rules (and risking drift from the writers). `modification_markers`
    is the CLI's own renderer, shared rather than copied
    (DESIGN_workout_revisions.md §7)."""
    return {
        **workout,
        "calendar_status": calendar_status(workout),
        "modification_markers": modification_markers(workout),
        "adherence": adherence,
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


def _resolve_goal_id(raw: Any) -> Any:
    """Resolves a goal id from a query string, defaulting to the next active goal
    (earliest target date) — mirrors the CLI's plan-command goal resolution."""
    if raw is not None and raw != "":
        return int(raw)
    objectives = runtime.db.upcoming_objectives()
    if not objectives:
        return None
    objectives.sort(key=lambda x: str(x['target_date']))
    return objectives[0]['id']


def _version_arg(raw: Any) -> Any:
    """A plan-version id from a query string, or None when omitted/blank."""
    return int(raw) if raw is not None and raw != "" else None


def _weeks_arg(raw: str) -> Any:
    """`?weeks=` as the CLI's `--weeks` takes it: a positive int, or 'all'. Raises
    ValueError with the message the caller should 400 with."""
    if raw == "all":
        return "all"
    try:
        weeks = int(raw)
    except ValueError:
        raise ValueError("weeks must be an integer or 'all'")
    if weeks < 1:
        raise ValueError("weeks must be >= 1")
    return weeks


# --- API Routes ---

@app.route("/api/status", methods=["GET"])
def get_status() -> Any:
    """API endpoint to retrieve overall athlete status, learnings, and metrics."""
    objectives = runtime.db.upcoming_objectives()
    next_goal = None
    if objectives:
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]

    metrics = runtime.db.get_metrics_cache()
    last_metrics = metrics[-1] if metrics else None

    # Get last baseline
    last_baseline = runtime.db.get_baseline(last_metrics['date']) if last_metrics else None

    learnings = runtime.db.get_learnings()

    macrocycle = None
    mesocycles = []
    plan_feedback = []
    config_mismatch = False
    if next_goal and next_goal['id'] is not None:
        macrocycle = runtime.db.get_macrocycle_for_objective(next_goal['id'])
        if macrocycle:
            mesocycles = runtime.db.get_mesocycles_for_macrocycle(macrocycle['id'])
            plan_feedback = runtime.db.list_plan_feedback(macrocycle['id'])
            config_mismatch = macrocycle.get('config_hash') != plan_config_hash()

    # The web app is a pure reader — it never pulls from Garmin (see
    # DESIGN_garmin_direct_pull.md §11). Surface the watermark so the UI can show
    # how fresh the cached data is; the CLI (or a cron `data pull`) owns syncing.
    sync_state = runtime.db.get_sync_state()

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
        # The pending feedback log, read-only like everything here — it is written with
        # `tm plan feedback` (DESIGN_plan_feedback.md §8).
        "plan_feedback": plan_feedback,
        "config_mismatch": config_mismatch,
        "sync_state": sync_state
    })


@app.route("/api/objectives", methods=["GET"])
def list_objectives() -> Any:
    """Every objective (mirrors `goal list`)."""
    return jsonify(runtime.db.get_objectives())


@app.route("/api/constraints", methods=["GET"])
def list_constraints() -> Any:
    """Active + upcoming directives — the read-only web view of `constraint list`
    (DESIGN_constraints.md §6). Its window is a rolling `metrics_lookback_days` plus
    everything upcoming, not the CLI's active-mesocycle anchor."""
    window = config.metrics_lookback_days
    start = (
        datetime.strptime(today_str(), "%Y-%m-%d").date() - timedelta(days=window - 1)
    ).strftime("%Y-%m-%d")
    return jsonify(runtime.db.get_constraints(start))


# --- Workouts ---

@app.route("/api/workouts", methods=["GET"])
def list_workouts() -> Any:
    """Workouts in a date range (mirrors `workout list`). Rows carry the derived
    `calendar_status` + `modification_markers` the CLI renders as markers, plus the
    `adherence` verdict for every row today or earlier."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    include_removed = request.args.get("include_removed", "").lower() in ("1", "true", "yes")
    sport_type = request.args.get("sport_type") or None
    workouts = runtime.db.get_workouts(
        start_date=start_date, end_date=end_date,
        sport_type=sport_type, include_removed=include_removed,
    )
    # Graded over the listing's own past span rather than the requested window: the
    # request may be open-ended on either side, and the verdicts are the CLI's
    # (`adherence_verdicts`), never a second implementation. No pull — §8.
    today = today_str()
    past = sorted(w["date"] for w in workouts if w["date"] <= today)
    verdicts = adherence_verdicts(past[0], past[-1]) if past else {}
    return jsonify([_annotate_workout(w, verdicts.get(w["id"])) for w in workouts])


@app.route("/api/workouts/compare", methods=["GET"])
def compare_workouts() -> Any:
    """Plan-vs-actual adherence over a date range (mirrors `workout compare`,
    trainmate/cli/workouts.py). Unlike the CLI it never calls `garmin.ensure_data` —
    the dashboard consumes cached data only (§8). Reuses `analyze_adherence`; returns a
    day-by-day structure so the frontend renders without re-deriving any logic.

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

    all_workouts = runtime.db.get_workouts(start_date=start_date, end_date=end_date)
    activities = runtime.db.get_completed_activities(start_date=start_date, end_date=end_date)
    covered_ranges = runtime.db.get_mesocycle_ranges(start_date, end_date)
    threshold = config.minor_activity_load_threshold

    discrepancies, matching_results, informational = analyze_adherence(
        planned_workouts=all_workouts,
        completed_activities=activities,
        start_date_obj=start_obj,
        history_days=history_days,
        minor_activity_load_threshold=threshold,
        covered_ranges=covered_ranges,
        pending_from=today,
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
            # Ask the shared classifier rather than re-deriving: the inline version
            # skipped canonical_sport() and the load threshold, so a "Rest" workout
            # read as a normal sport and a light stroll read as a violation here only.
            verdict = classify_adherence(w, act, threshold, pending=r.get("pending", False))
            results_out.append({
                "planned": w,
                "completed": act,
                "is_rest": verdict["status"] in ("rest_ok", "rest_violation"),
                "rest_violation": verdict["status"] == "rest_violation",
                "pending": verdict["status"] == "pending",
                "status": verdict["status"],
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
        # Structured facts plus the rendered sentence, rather than CLI-voiced prose the
        # dashboard would have to parse to filter or restyle by kind.
        "discrepancies": [d.to_dict() for d in discrepancies],
        "informational": informational,
    })


@app.route("/api/timeline.png", methods=["GET"])
def get_timeline_png() -> Any:
    """Progress timeline as a PNG image (DESIGN_progress_timeline.md §6): measured
    load to date + planned load through plan end, with the CTL/ATL/TSB model run
    across the seam. The same §7.2 renderer draws the Telegram photo, so bot and web
    show the identical picture.

    Never calls `garmin.ensure_data`; freshness is surfaced via `sync_state` on the
    Dashboard. Not cached: recomputed per request so the projection moves the instant a
    CLI `adapt`/`generate`/`swap`/`remove` rewrites future workouts.

    `?weeks=N` re-windows the past half exactly like the CLI's `--weeks` (default 8,
    must be >= 1 else 400; `all` extends to full history). The future half always runs
    to plan end. matplotlib absent (optional tier, §7.2) -> 503 with the install hint
    the tab shows verbatim."""
    today = today_str()
    try:
        weeks_arg = _weeks_arg(request.args.get("weeks", "8"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    from trainmate import timeline
    payload = timeline.build_timeline_payload(runtime.db)
    clipped = progression.clip_payload_for_weeks(payload, weeks_arg, today)

    try:
        from trainmate import chart
        png = chart.render_timeline_png(clipped)
    except ImportError:
        return (
            "matplotlib is not installed — run: "
            "venv/bin/pip install -r requirements.txt",
            503,
            {"Content-Type": "text/plain"},
        )
    return app.response_class(png, mimetype="image/png")


# --- Intensity distribution (DESIGN_intensity_distribution.md) ---

@app.route("/api/zones", methods=["GET"])
def get_zones() -> Any:
    """Per-sport, per-zone time in zone over the displayed window — the structured form
    of the `tm progress -z` tables (DESIGN_intensity_distribution.md §9.6/§9.8).

    Which sports qualify, and the currency each is drawn in, come from
    `intensity.window_sport_stats`/`select_zone_sports`/`zone_currency` — the same three
    functions the CLI tables call, so a sport the terminal omits is omitted here for the
    same reason and named in `omitted` rather than silently dropped.

    Weeks at or before today carry what was MEASURED; weeks beyond it carry what the plan
    PRESCRIBES (`future: true`), the data behind the CLI's ghost rows. A future week whose
    plan was written in the other currency reports `currency_mismatch` instead of
    converting — collapsing 7 power zones onto 5 HR ones would be banding by the back
    door (§5).

    Query: ?weeks=N|all (default 8), ?sport= (repeatable; overrides the volume filter),
    ?currency=hr|power (the CLI's --hr/--power)."""
    today = today_str()
    try:
        weeks_arg = _weeks_arg(request.args.get("weeks", "8"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    forced = request.args.get("currency") or None
    if forced and forced not in intensity.CURRENCY_BY_KEY:
        return jsonify({"error": "currency must be 'hr' or 'power'"}), 400

    explicit = [s for s in request.args.getlist("sport") if s]

    from trainmate import timeline
    payload = timeline.build_timeline_payload(runtime.db)
    past, future, hidden = progression.select_weeks(payload["weeks"], weeks_arg, today)
    weeks = past + future

    stats = intensity.window_sport_stats(weeks)
    selected, low_volume, no_zone_data = intensity.select_zone_sports(
        explicit, list(config.user_profile.get("sport_preferences") or []), stats
    )
    window_seconds = sum(agg["seconds"] for agg in stats.values())

    sports_out: List[Dict[str, Any]] = []
    for sport in selected:
        currency = intensity.zone_currency(stats, sport, forced)
        if not currency:
            continue
        spec = intensity.CURRENCY_BY_KEY[currency]
        agg = stats.get(sport) or {"seconds": 0.0, "coverage": {}}

        weeks_out = []
        for week in weeks:
            is_future = week["week_commencing"] > today
            # One derivation of the three-state rule, shared with the CLI table.
            state = intensity.week_zone_state(week, sport, currency, is_future=is_future)
            entry: Dict[str, Any] = {
                "week_commencing": week["week_commencing"],
                "meso_label": week.get("meso_label"),
                "in_progress": bool(week.get("in_progress")),
                "future": is_future,
                "seconds": list(state.seconds) if state.seconds is not None else None,
                "trained": state.trained,
                "undercounted": state.undercounted,
            }
            if is_future and state.seconds is None:
                # Planned in the other currency: offered as a fact, never converted (§9.8).
                entry["currency_mismatch"] = state.currency_mismatch
            weeks_out.append(entry)

        sports_out.append({
            "sport": sport,
            "currency": currency,
            "tag": spec.tag,
            "zone_labels": list(spec.labels),
            "coverage": (agg.get("coverage") or {}).get(currency, 0.0),
            "sport_seconds": agg["seconds"],
            "weeks": weeks_out,
        })

    return jsonify({
        "window": {
            "weeks": weeks_arg,
            "hidden_weeks": hidden,
            "window_seconds": window_seconds,
            "today": today,
        },
        "sports": sports_out,
        "omitted": {
            "low_volume": low_volume,
            "no_zone_data": no_zone_data,
            "min_share": intensity.ZONE_SPORT_MIN_SHARE,
        },
    })


# --- Plan ---

@app.route("/api/plan", methods=["GET"])
def get_plan() -> Any:
    """The active periodization plan for a goal (mirrors `plan show`): the governing
    macrocycle plus its mesocycle blocks. `?goal_id=` defaults to the next active goal."""
    goal_id = _resolve_goal_id(request.args.get("goal_id"))
    if goal_id is None:
        return jsonify({"goal": None, "macrocycle": None, "mesocycles": []})
    goal = runtime.db.get_objective(goal_id)
    if not goal:
        return jsonify({"error": f"Goal with ID {goal_id} not found."}), 404
    macrocycle = runtime.db.get_macrocycle_for_objective(goal_id)
    mesocycles = (
        runtime.db.get_mesocycles_for_macrocycle(macrocycle['id']) if macrocycle else []
    )
    plan_feedback = (
        runtime.db.list_plan_feedback(macrocycle['id']) if macrocycle else []
    )
    return jsonify({
        "goal": goal,
        "macrocycle": macrocycle,
        "mesocycles": mesocycles,
        "plan_feedback": plan_feedback,
    })


@app.route("/api/plan/versions", methods=["GET"])
def plan_versions() -> Any:
    """Lists every kept periodization plan version (active + superseded) for a goal,
    for the web equivalent of `plan versions` (see DESIGN_plan_rollback.md)."""
    goal_id = _resolve_goal_id(request.args.get("goal_id"))
    if goal_id is None:
        return jsonify({"goal": None, "versions": []})
    goal = runtime.db.get_objective(goal_id)
    versions = runtime.db.get_macrocycle_versions(goal_id)
    return jsonify({"goal": goal, "versions": versions})


@app.route("/api/plan/diff", methods=["GET"])
def plan_diff_versions() -> Any:
    """Compares two periodization plan versions (web equivalent of `plan diff`).

    Query: {goal_id?, from_version?, to_version?}. Defaults to the version before the
    active one vs the active one, for the next active goal. The comparison itself lives
    in trainmate/plan_diff.py — the CLI renders the very same structure as text."""
    goal_id = _resolve_goal_id(request.args.get("goal_id"))
    if goal_id is None:
        return jsonify({"error": "No goal to compare plan versions for."}), 400
    goal = runtime.db.get_objective(goal_id)
    if not goal:
        return jsonify({"error": f"Goal with ID {goal_id} not found."}), 404
    old, new, error = plan_diff.resolve_versions(
        runtime.db, goal,
        _version_arg(request.args.get("from_version")),
        _version_arg(request.args.get("to_version")),
    )
    if error:
        code, message = error
        return jsonify({"error": message, "code": code}), 404 if code == "not_found" else 400
    diff = plan_diff.diff_plans(
        old, new,
        runtime.db.get_mesocycles_for_macrocycle(old['id']),
        runtime.db.get_mesocycles_for_macrocycle(new['id']),
        runtime.db.list_plan_feedback(old['id']),
        runtime.db.list_plan_feedback(new['id']),
    )
    return jsonify({"goal": goal, "diff": diff})


@app.route("/api/workouts/batches", methods=["GET"])
def workout_batches() -> Any:
    """Lists the workout changes a `workout rollback` could undo (web equivalent of
    `workout batches`, see DESIGN_workout_revisions.md §10). Undoing one is a CLI
    action."""
    return jsonify({"batches": runtime.db.get_workout_changes(from_date=today_str())})


# --- Coach Learnings ---

@app.route("/api/learnings", methods=["GET"])
def get_learnings() -> Any:
    """Lists coach learnings (mirrors `learnings list`). Each carries its computed
    `dormant` flag, `sports`/`confidence`, and any `proposed_confidence` downgrade.
    Optional filters: ?sport=&confidence=&dormant=1."""
    learnings = runtime.db.get_learnings()

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
        "summary": _learnings_summary(runtime.db.get_learnings()),
    })


@app.route("/api/learnings/<int:learning_id>/evidence", methods=["GET"])
def get_learning_evidence(learning_id: int) -> Any:
    """Returns the per-week evidence basis behind a learning's confidence
    (mirrors `learnings show`)."""
    learning = next((l for l in runtime.db.get_learnings() if l['id'] == learning_id), None)
    if not learning:
        return jsonify({"error": f"Learning {learning_id} not found."}), 404
    evidence = runtime.db.get_learning_evidence(learning_id)
    return jsonify({
        "learning": learning,
        "supporting": [e for e in evidence if e['polarity'] >= 0],
        "contradicting": [e for e in evidence if e['polarity'] < 0],
    })


# --- Benchmarks (DESIGN_benchmark_workouts.md) ---

@app.route("/api/benchmarks", methods=["GET"])
def get_benchmarks() -> Any:
    """The benchmark logbook, newest first (mirrors `benchmark list`), plus the effective
    threshold set those rows currently imply (§3.3).

    Each row carries its display `formatted` value and the signed, direction-aware
    `delta` against the next-older row of the same kind — via `benchmarks.with_previous`
    and `format_delta`, so a quickened pace reads positive here exactly as it does in the
    terminal (§3.2). Optional filters: ?sport=&kind=."""
    sport = request.args.get("sport")
    rows = runtime.db.get_benchmark_results(
        sport_type=canonical_sport(sport) if sport else None,
        anchor_kind=request.args.get("kind") or None,
    )

    out = []
    for row, prev in benchmarks.with_previous(rows):
        kind = row["anchor_kind"]
        value = float(row["value"])
        anchor = benchmarks.anchor_for_kind(kind)
        out.append({
            **row,
            "label": anchor.label if anchor else kind,
            "formatted": benchmarks.format_value(kind, value),
            "delta": benchmarks.format_delta(kind, value, prev) if prev else None,
            "improvement": (
                benchmarks.is_improvement(kind, value, prev) if prev else None
            ),
        })

    thresholds = runtime.db.latest_thresholds()
    return jsonify({
        "results": out,
        "thresholds": [
            {
                "anchor_kind": kind,
                "label": (
                    benchmarks.anchor_for_kind(kind).label
                    if benchmarks.anchor_for_kind(kind) else kind
                ),
                "value": value,
                "formatted": benchmarks.format_value(kind, value),
                "unit": benchmarks.unit_for_kind(kind),
            }
            for kind, value in sorted(thresholds.items())
        ],
    })


# --- Activities, Metrics & Daily Signals ---

@app.route("/api/activities", methods=["GET"])
def get_activities() -> Any:
    """Completed activities over a date range (mirrors `data show-activities`)."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return jsonify(runtime.db.get_completed_activities(start_date=start_date, end_date=end_date))


@app.route("/api/daily-signals", methods=["GET"])
def get_daily_signals() -> Any:
    """External daily signals (alcohol/sleep/stress, ingested from Calendar or
    authored with `signal add`) over a date range — mirrors `signal list`, see
    ARCHITECTURE.md §13. Optional ?metric= restricts to one category."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return jsonify(runtime.db.get_daily_signals(
        start_date=start_date, end_date=end_date,
        metric=request.args.get("metric") or None,
    ))


@app.route("/api/daily-signals/metrics", methods=["GET"])
def get_signal_metrics() -> Any:
    """The distinct signal metrics in use, with row counts and first/last dates
    (mirrors `signal list-metrics`) — the vocabulary behind the signal charts."""
    return jsonify({"metrics": runtime.db.list_signal_metrics()})


@app.route("/api/metrics", methods=["GET"])
def get_metrics() -> Any:
    """Cached Garmin metrics. Date range via ?start_date=&end_date=, else last 30 days."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    if start_date or end_date:
        return jsonify(runtime.db.get_metrics_cache(start_date=start_date, end_date=end_date))
    metrics = runtime.db.get_metrics_cache()
    return jsonify(metrics[-30:] if metrics else [])


# --- Configuration (read-only) ---

@app.route("/api/models", methods=["GET"])
def get_models() -> Any:
    """The configured LLM menu with the active entry marked (mirrors `model list`,
    DESIGN_model_selection.md §3.3). Choosing one is `settings set coach-model`, a CLI action."""
    return jsonify({
        "models": llm_models.list_models(),
        "active": llm_models.active_model(),
        "source": llm_models.active_source(),
        "set_at": llm_models.stored_at(),
    })


if __name__ == "__main__":
    app.run(host=config.web_host, port=config.web_port, debug=config.web_debug)
