"""The one row-fetching path behind the progress timeline (DESIGN_progress_timeline.md
§5/§6). Both the CLI handler (`tm progress`) and the web endpoint
(`GET /api/timeline.png`) call `build_timeline_payload`, so the two surfaces fetch
the same rows and assemble the same §6.0 payload — the fix for the rev-4 divergence
where each caller assembled its own and the copies drifted (CODE_REVIEW finding #5).

Payload assembly itself lives in `progression.assemble_timeline` (pure, row-in);
this module is only the thin db-reads-plus-config-plumbing wrapper around it, kept
separate so the pure functions stay db-free and testable.
"""
from typing import Any, Dict

from trainmate import garmin, progression
from trainmate.config import config
from trainmate.db.objectives import ARCHIVED
from trainmate.util import today_str


def build_timeline_payload(dbh) -> Dict[str, Any]:
    """Reads every row the timeline needs from `dbh` and returns the assembled §6.0
    payload (unclipped — the renderer windows it). `dbh` is passed explicitly so the
    CLI's rebindable db and the web's singleton both resolve against the same handle
    the rest of their command used."""
    today = today_str()
    activities = dbh.get_completed_activities()
    workouts = dbh.get_workouts()
    metrics_rows = dbh.get_metrics_cache()

    # Everything the athlete has not called off: a goal already raced still belongs on the
    # timeline, and "completed" is now the date's verdict, not a stored one (§12).
    objectives = [o for o in dbh.get_objectives() if o["status"] != ARCHIVED]
    objectives.sort(key=lambda o: str(o["target_date"]))

    macro = dbh.get_governing_macrocycle()
    mesocycles = dbh.get_mesocycles_for_macrocycle(macro["id"]) if macro else []
    cache = dbh.get_analysis_cache("long")
    inferred = (
        (cache.get("reconstruction") or {}).get("inferred_mesocycles", [])
        if cache else []
    )

    history_start = garmin.pmc_history_start(dbh=dbh)
    warmup_cutoff = garmin.pmc_warmup_cutoff_for(history_start, config.pmc_ctl_days)

    return progression.assemble_timeline(
        activities, workouts, metrics_rows, mesocycles, inferred, objectives,
        today, config.pmc_ctl_days, config.pmc_atl_days, warmup_cutoff,
    )
