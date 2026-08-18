"""PMC (fitness/fatigue) and derived-metric computation. See DESIGN_pmc_fitness_fatigue.md."""
import math
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from trainmate import runtime
from trainmate.config import config
from trainmate.util import today_str, yellow
import trainmate.garmin as _g
from trainmate.garmin.client import _to_date
from trainmate.garmin.load import _hr_zone_coverage, activity_load, compute_load, measured_tss

# The PMC CTL/ATL EWMA time constants are config-backed under `garmin:` and read live
# every sweep (not frozen at import) so an edit can't drift derived values apart.
# Non-default windows are experimental — calibration caveat in config_template.yaml and
# DESIGN_pmc_fitness_fatigue.md §3.4.

def load_ratio(atl: Optional[float], ctl: Optional[float]) -> Optional[float]:
    """ATL/CTL — fatigue relative to the athlete's own fitness base, the scale-invariant
    companion to TSB's absolute difference (training_load.md §3).

    None when either EWMA is NULL (pre-recompute row) or CTL has not warmed above zero:
    there is no base to divide by, and a ratio against ~0 is noise, not a spike."""
    if atl is None or ctl is None or ctl <= 0.0:
        return None
    return atl / ctl
def _mean_std(values: List[float]) -> Tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, math.sqrt(variance)
def compute_pmc(
    daily_load: Dict[str, float],
    start: str, end: str,
    ctl_days: int, atl_days: int,
    seed: Tuple[float, float] = (0.0, 0.0),
) -> Dict[str, Tuple[float, float, float]]:
    """CTL/ATL/TSB per calendar day via the classic Coggan discrete 1/τ EWMA.

    Walks EVERY calendar day in [start, end] (not just days with load), so rest days
    and gaps decay the EWMAs with zero load. Both EWMAs seed from `seed` (the state
    at end of `start - 1`; default (0.0, 0.0) reproduces the from-zero full-history
    sweep). A non-zero seed is the progression fold's anchor — the last stored row's
    (CTL, ATL) — so a fold from any day reproduces the unbroken series bit-exactly
    (DESIGN_progress_timeline.md §4).

        ctl_d = ctl_{d-1} + (load_d - ctl_{d-1}) / ctl_days
        atl_d = atl_{d-1} + (load_d - atl_{d-1}) / atl_days
        tsb_d = ctl_{d-1} - atl_{d-1}   # yesterday's values — the form you woke up with

    The TSB off-by-one is deliberate and load-bearing (training_load.md §1): today's
    form must NOT include today's workout. Returns {ISO date -> (ctl, atl, tsb)} at
    full precision — rounding moves to display, so the seed stays exact
    (DESIGN_progress_timeline.md §4). Empty {} on a degenerate span.
    """
    out: Dict[str, Tuple[float, float, float]] = {}
    if not start or not end:
        return out
    cur, last = _to_date(start), _to_date(end)
    if cur > last:
        return out
    ctl, atl = seed
    while cur <= last:
        ds = cur.isoformat()
        load = daily_load.get(ds, 0.0)
        tsb = ctl - atl                       # yesterday's (pre-update) balance
        ctl = ctl + (load - ctl) / ctl_days
        atl = atl + (load - atl) / atl_days
        out[ds] = (ctl, atl, tsb)
        cur += timedelta(days=1)
    return out
def pmc_warmup_cutoff_for(start: Optional[str], ctl_days: int) -> Optional[str]:
    """ISO date at/after which PMC display values have cleared the leading-edge warm-up.
    A date d is a warm-up artifact (suppress it) iff d < cutoff = start + ctl_days.
    Returns None when there is no history start."""
    if not start:
        return None
    return (_to_date(start) + timedelta(days=ctl_days)).isoformat()
def pmc_ramp(
    ctl_by_date: Dict[str, Optional[float]], date_iso: str, window: int = 7,
    warmup_cutoff: Optional[str] = None,
) -> Optional[float]:
    """CTL ramp = ctl(date) - ctl(date - window days), in load units per week.

    Uses the exact d-window day, else the nearest EARLIER day carrying a CTL value
    (interior-gap rule, §3.1) — bounded at 2*window back, and scaled to a per-window
    rate when the baseline is older than `window` days, so a stored-series hole can
    never quietly report a multi-week delta as "/week". Returns None when `date` has
    no CTL, when no usable baseline exists at/under date-window (don't emit garbage),
    or when the baseline lands before `warmup_cutoff` — a ramp measured against a
    suppressed warm-up-artifact CTL would read as a phantom overload spike (the §5.4
    straddle guard, applied here so every surface gets it). Callers pass the FULL
    stored series, never a short prompt window, so a small window never spuriously
    drops it."""
    today_ctl = ctl_by_date.get(date_iso)
    if today_ctl is None:
        return None
    # The baseline can only live on one of the `window` days in [d-2w, d-w]; look
    # those up directly (nearest to d-w first) instead of scanning the whole series.
    d0 = _to_date(date_iso)
    for offset in range(window, 2 * window + 1):
        base_date = (d0 - timedelta(days=offset)).isoformat()
        val = ctl_by_date.get(base_date)
        if val is None:
            continue
        if warmup_cutoff and base_date < warmup_cutoff:
            return None
        return round((today_ctl - val) * window / offset, 1)
    return None
def pmc_history_start(dbh=None) -> Optional[str]:
    """Earliest ISO date with any Garmin evidence — min(first activity, first metrics
    row); two MIN() queries. The single source the warm-up cutoff and the
    still-warming-up flag derive from — callers fetch it ONCE per command and pass it
    (or the cutoff derived from it) down, so no two surfaces can compute it differently.

    `dbh` defaults to the module db; callers holding their own handle (CoachService's
    injected db, the CLI's rebindable one) pass it so the cutoff is derived from the
    same database as the metrics it gates."""
    dbh = dbh or runtime.db
    firsts = [
        d for d in (dbh.get_first_activity_date(), dbh.get_first_metric_date()) if d
    ]
    return min(firsts) if firsts else None
def pmc_display_values(
    m: Dict[str, Any], warmup_cutoff: Optional[str]
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(ctl, atl, tsb) of a metrics row for DISPLAY: all None inside the §3.3(a)
    warm-up window (stored values there are leading-edge artifacts), the stored
    values (each possibly None) otherwise. The one blanking rule every user surface
    (status line, show-metrics table, CSV) shares."""
    if warmup_cutoff and m["date"] < warmup_cutoff:
        return None, None, None
    return m.get("ctl"), m.get("atl"), m.get("tsb")
def pmc_data_caveat(
    history_start: Optional[str],
    as_of: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Static "still warming up" flag for when today's own PMC values are short on
    history (§3.3b). A τ_ctl-day CTL EWMA needs months to settle, so while total history
    behind today is short the latest value is warm-up grade even though the leading-edge
    blanking (§3.3a) can't suppress *today*.

    Pure: takes the pmc_history_start() the caller already fetched. Returns
    {'n_days','history_start'} while N = today − history_start is under 3·τ_ctl
    (≈126 days), else None (above that the artifact is negligible). This is a flat flag,
    not a computed accuracy figure: a young/just-returned athlete simply sees low numbers
    under a plain flag. The caller renders the coach/user wording; the *direction* of any
    discount (is a low CTL an artifact or a real beginner?) is the LLM's to judge from the
    athlete's pre-DB history — the app only flags that the number is young."""
    if not history_start:
        return None
    end = as_of or today_str()
    n_days = (_to_date(end) - _to_date(history_start)).days
    if n_days < 0 or n_days >= 3 * config.pmc_ctl_days:
        return None
    return {"n_days": n_days, "history_start": history_start}
def recompute_derived(dbh=None) -> None:
    """Recomputes the PMC (CTL/ATL/TSB) and 28-day baselines for ALL cached days. A full
    sweep is trivially cheap on a local DB and avoids windowed-recompute bugs (an
    activity affects 28 days of derived values).

    `dbh` defaults to the module db; the post-wipe recompute (cli/data.py) passes the
    CLI's own handle so it sweeps the same database the wipe just ran against, even
    when the singleton has been rebound (tests, embeddings that inject a db)."""
    dbh = dbh or runtime.db
    # One unified load per activity via the fallback hierarchy (power TSS ->
    # hrTSS -> sRPE), not the old `tss + rpe*hours` blend.
    daily_load: Dict[str, float] = {}
    for act in dbh.get_completed_activities():
        date_str = act["date"]
        daily_load[date_str] = daily_load.get(date_str, 0.0) + activity_load(act)

    metrics = dbh.get_metrics_cache()  # sorted by date asc
    by_date = {m["date"]: m for m in metrics}

    # PMC (CTL/ATL/TSB) over EVERY calendar day so rest/gap days decay the EWMAs. The
    # span ends at max(last activity, last metrics): an activities-only pull can leave
    # trailing activity days past the last metrics row, and those carry load that must be
    # walked. Upserted onto existing metrics rows only (activity-only days feed the EWMA
    # but create no cache row).
    span_dates = list(daily_load.keys()) + [m["date"] for m in metrics]
    pmc: Dict[str, Tuple[float, float, float]] = {}
    if span_dates:
        pmc = compute_pmc(
            daily_load, min(span_dates), max(span_dates),
            config.pmc_ctl_days, config.pmc_atl_days,
        )

    # One connection and one commit for the whole sweep. Each per-day write used to
    # open, commit and close its own — roughly one such cycle per day of history, after
    # every pull. The sweep is also all-or-nothing now, so an interruption cannot leave
    # half the history carrying refreshed CTL/ATL and half the old values.
    with dbh.transaction():
        _write_derived(dbh, metrics, by_date, pmc)


def _write_derived(dbh, metrics, by_date, pmc) -> None:
    """Upserts the PMC triple and the 28-day baselines for every cached day."""
    for m in metrics:
        date_str = m["date"]
        date_obj = _to_date(date_str)

        ctl_atl_tsb = pmc.get(date_str)
        ctl, atl, tsb = ctl_atl_tsb if ctl_atl_tsb else (None, None, None)

        dbh.save_metric_cache(
            date=date_str, rhr=m.get("rhr"), hrv=m.get("hrv"),
            sleep_score=m.get("sleep_score"), stress=m.get("stress"),
            ctl=ctl, atl=atl, tsb=tsb,
        )

        rhr_vals, hrv_vals, sleep_vals = [], [], []
        for d in range(1, 29):
            prev = by_date.get((date_obj - timedelta(days=d)).isoformat())
            if not prev:
                continue
            if prev.get("rhr") is not None:
                rhr_vals.append(prev["rhr"])
            if prev.get("hrv") is not None:
                hrv_vals.append(prev["hrv"])
            if prev.get("sleep_score") is not None:
                sleep_vals.append(prev["sleep_score"])

        if len(rhr_vals) >= 7 or len(hrv_vals) >= 7 or len(sleep_vals) >= 7:
            rhr_mean, rhr_std = _mean_std(rhr_vals)
            hrv_mean, hrv_std = _mean_std(hrv_vals)
            sleep_mean, sleep_std = _mean_std(sleep_vals)
            dbh.save_baseline(
                date=date_str, rhr_mean=rhr_mean, rhr_std=rhr_std,
                hrv_mean=hrv_mean, hrv_std=hrv_std,
                sleep_mean=sleep_mean, sleep_std=sleep_std,
            )
def backfill_tss(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    verbose: bool = False,
) -> int:
    """Recomputes the measured TSS (power TSS or hrTSS) for every cached activity
    and rewrites the stored value. Activities keep their raw zone seconds, so
    this needs no Garmin calls. Returns the number of rows whose TSS changed, and
    refreshes the derived PMC (which runs off the on-the-fly load)."""
    activities = runtime.db.get_completed_activities(start_date=start_date, end_date=end_date)
    changed = 0
    sparse: List[Dict[str, Any]] = []
    for act in activities:
        new_tss = measured_tss(act, act)
        old_tss = act.get("tss")
        if (old_tss is None) != (new_tss is None) or (
            old_tss is not None and new_tss is not None
            and abs(float(old_tss) - new_tss) > 1e-6
        ):
            runtime.db.update_activity_tss(act["activity_id"], new_tss)
            changed += 1
        _load, _method, warning = compute_load(
            act, act, act.get("rpe"), act.get("duration_sec") or 0.0
        )
        if warning:
            sparse.append(act)
    print(f"Recomputed measured TSS for {len(activities)} activities "
          f"({changed} changed).")
    if sparse:
        print(yellow(
            f"  {len(sparse)} activities have low HR-zone coverage and no RPE; "
            "their load is an underestimate. Enter an RPE in Garmin for accuracy."
        ))
        if verbose:
            for act in sparse:
                date = act.get("date", "?")
                name = act.get("activity_name") or act.get("activity_type", "?")
                dur_sec = act.get("duration_sec") or 0.0
                dur_min = int(dur_sec // 60)
                coverage = _hr_zone_coverage(act, dur_sec)
                print(f"    {date}  {name}  ({dur_min} min, "
                      f"HR-zone coverage {coverage:.0%})")
    recompute_derived()
    return changed
