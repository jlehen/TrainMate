"""Direct Garmin Connect ingestion (see DESIGN_garmin_direct_pull.md).

Split into submodules — client/load/pmc/sync — all re-exported here so
``from trainmate.garmin import ...`` and ``patch.object(garmin, ...)`` keep working.
"""
from trainmate.config import config
from trainmate.db import db
from trainmate.garmin.client import (
    _derivation_pad_days, GarminAuthRequired, _to_date, _date_range, _shift, GarminClient
)
from trainmate.garmin.load import (
    POWER_ZONE_TSS_PER_SEC, HR_ZONE_TSS_PER_SEC, _zone_tss,
    _hr_zone_coverage, _rpe_tss, measured_tss, compute_load, _has_power_zones,
    _measurement_is_load, _divergence_ratio, _divergence_threshold, activity_load,
    rpe_divergence, _safe_round
)
from trainmate.garmin.pmc import (
    _mean_std, load_ratio, compute_pmc, pmc_warmup_cutoff_for, pmc_ramp,
    pmc_history_start, pmc_display_values, pmc_data_caveat, recompute_derived,
    backfill_tss
)
from trainmate.garmin.sync import (
    _ingest_activities, _ingest_metrics, _int_or_none, pull, _sync_calendar_context,
    _pull_command, _contiguous_regions, ensure_data, _warn_manual, _remember, reset_memo
)
