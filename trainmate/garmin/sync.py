"""Ingestion + orchestration: pull, ensure_data, and the process-level ensure memo."""
import time
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

from trainmate import runtime
from trainmate.config import config
from trainmate.util import today_str, cmd, fail, step, warn
import trainmate.garmin as _g
from trainmate.garmin.client import (GarminAuthRequired, GarminClient, _date_range,
    _derivation_pad_days, _shift, _to_date)
from trainmate.garmin.load import _safe_round, compute_load, measured_tss
from trainmate.sports import canonical_sport
from trainmate.garmin.pmc import recompute_derived

def _ingest_activities(client: GarminClient, start: str, end: str, throttle: float) -> int:
    """Ingests the range's activities, returning how many Garmin returned — -1 on a
    failed fetch, so `pull` says "unavailable" rather than "0"."""
    try:
        activities = client.get_activities(start, end)
    except Exception as e:
        # Skip ingest AND deletion-reconcile on a failed fetch: an empty result
        # here would otherwise be read as "Garmin has no activities" and prune
        # the whole local range.
        fail(f"Garmin activity fetch {start}..{end} failed: {e}")
        return -1
    step(f"Found {len(activities)} activities in {start}..{end}.")
    underestimated_activities = []  # activities whose load is a weak estimate for lack of RPE
    fetched_ids = []  # everything Garmin still has in this range, for deletion reconcile
    for idx, act in enumerate(activities):
        activity_id = str(act.get("activityId"))
        fetched_ids.append(activity_id)
        start_time = act.get("startTimeLocal", "") or ""
        date_str = start_time.split(" ")[0] if start_time else ""
        if not date_str:
            continue
        type_key = (act.get("activityType") or {}).get("typeKey", "unknown")
        duration_sec = act.get("duration") or 0.0
        avg_hr = act.get("averageHR")

        # A classifier, not a pre-filter: Garmin reports avgPower for running too
        # (watch-/Stryd-derived), and running watts scored against a cycling FTP are
        # meaningless (DESIGN_intensity_distribution.md §6.1).
        bike_avg_watts = None
        if canonical_sport(type_key or "") == "cycling":
            power = act.get("avgPower") or act.get("averagePower")
            if power is not None:
                try:
                    bike_avg_watts = int(round(float(power)))
                except (ValueError, TypeError):
                    pass

        zones = client.get_activity_hr_zones(activity_id) if avg_hr is not None else {f"zone{i}_sec": 0 for i in range(1, 6)}
        # Power zones only exist when a power meter was recording (cycling); skip
        # the extra API call otherwise and leave the columns NULL.
        power_zones = (
            client.get_activity_power_zones(activity_id)
            if bike_avg_watts is not None
            else {f"power_zone{i}_sec": None for i in range(1, 8)}
        )

        # RPE is user-entered only; we never synthesise it from power or HR.
        rpe_raw = client.get_activity_rpe(activity_id)
        rpe = int(round(rpe_raw)) if rpe_raw else None

        # Store the objective measurement (power TSS or hrTSS; NULL if neither).
        # The training-load fallback — which may use RPE — is derived on the fly.
        tss = measured_tss(power_zones, zones)
        _load, _method, warning = compute_load(power_zones, zones, rpe, duration_sec)
        if warning:
            act_name = act.get("activityName", "Unknown Activity")
            underestimated_activities.append(f"{date_str} {act_name}")

        runtime.db.save_completed_activity(
            activity_id=activity_id, date=date_str, start_time=start_time,
            activity_name=act.get("activityName", "Unknown Activity"),
            activity_type=type_key, duration_sec=float(duration_sec),
            distance_km=_safe_round((act.get("distance") or 0) / 1000.0, 2),
            elevation_gain_m=_safe_round(act.get("elevationGain")),
            avg_hr=avg_hr, max_hr=act.get("maxHR"), rpe=rpe, tss=tss,
            bike_avg_watts=bike_avg_watts, **zones, **power_zones,
        )
        if throttle:
            time.sleep(throttle)

    if underestimated_activities:
        count = len(underestimated_activities)
        listing = "".join(f"\n    - {act}" for act in underestimated_activities)
        warn(
            f"{count} activit{'y' if count == 1 else 'ies'} "
            "had low HR-zone coverage and no RPE; their load is an underestimate. "
            f"Enter an RPE in Garmin for a better load value:{listing}"
        )

    # Reconcile deletions: drop local rows in this range that Garmin no longer
    # returns (e.g. a duplicate Zwift auto-upload the user deleted in Garmin
    # Connect). Without this they linger and surface as unplanned activities.
    pruned = runtime.db.prune_completed_activities(start, end, fetched_ids)
    if pruned:
        print(f"  Removed {pruned} activit{'y' if pruned == 1 else 'ies'} "
              "deleted in Garmin since the last sync.")
    return len(activities)
def _ingest_metrics(client: GarminClient, start: str, end: str, throttle: float) -> int:
    """Ingests one row per day in the range, returning how many days that was."""
    dates = _date_range(start, end)
    step(f"Fetching daily metrics for {len(dates)} day(s) {start}..{end}...")
    for date_str in dates:
        m = client.get_daily_metrics(date_str)
        # Write a row for EVERY day in range, even all-null, so the metrics-cache
        # date coverage records what has been pulled (gap detection). Derived
        # fields are left None here; recompute fills them via COALESCE.
        runtime.db.save_metric_cache(
            date=date_str,
            rhr=_int_or_none(m["rhr"]), hrv=_int_or_none(m["hrv"]),
            sleep_score=_int_or_none(m["sleep_score"]), stress=_int_or_none(m["stress"]),
        )
        if throttle:
            time.sleep(throttle)
    return len(dates)
def _int_or_none(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (ValueError, TypeError):
        return None
def pull(
    start_date: str, end_date: str, *,
    metrics: bool = True, activities: bool = True,
    throttle: Optional[float] = None, advance_watermark: bool = True,
) -> str:
    """Pulls a date range directly from Garmin into the DB, then recomputes derived
    metrics and (optionally) advances the watermark. Raises GarminAuthRequired if a
    non-interactive re-auth is needed.

    Returns a one-line summary of what landed: the step narration is an aside, but for
    `data pull` the sync IS the answer (DESIGN_output_verbosity.md §3.1)."""
    if throttle is None:
        throttle = config.garmin_throttle_seconds

    client = GarminClient(config.garmin_email, config.garmin_password, config.garmin_token_dir)
    step(f"Logging into Garmin Connect (tokens: {config.garmin_token_dir})...")
    client.login()

    landed = []
    if activities:
        count = _ingest_activities(client, start_date, end_date, throttle)
        landed.append(
            "activities unavailable" if count < 0
            else f"{count} activit{'y' if count == 1 else 'ies'}"
        )
    if metrics:
        days = _ingest_metrics(client, start_date, end_date, throttle)
        landed.append(f"{days} day{'' if days == 1 else 's'} of metrics")

    recompute_derived()

    if advance_watermark:
        state = runtime.db.get_sync_state()
        prev_through = state["through_date"] if state else None
        # through_date is a forward high-water mark; backfills never regress it.
        new_through = max([d for d in (prev_through, end_date) if d], default=end_date)
        runtime.db.set_sync_state(
            through_date=new_through,
            last_pull_utc=datetime.now(timezone.utc).isoformat(),
        )

    # An explicit pull also refreshes external calendar signals (best-effort).
    _sync_calendar_signals(force=True)
    return f"Garmin {start_date}..{end_date}: " + (", ".join(landed) or "nothing requested") + "."
# Process-level memo: the widest [start, end] window already ensured this run, so
# repeated reads (coach.py touches metrics/activities many times) cost nothing and
# we never log into Garmin twice per command.
_ensured: Optional[Tuple[str, str]] = None
def _sync_calendar_signals(force: bool) -> None:
    """Bridge to the calendar module's signal sync, lazily imported so a missing
    service-account file (calendar unconfigured) can never break a Garmin read. The
    gating, throttling, and error handling all live in google_calendar."""
    try:
        from trainmate import google_calendar
    except Exception:
        return  # Calendar not importable/configured — nothing to sync.
    google_calendar.sync_calendar_signals(force=force)
def _pull_command(start: str, end: str) -> str:
    return f"python trainmate_cli.py data pull -d {start}..{end}"
def _contiguous_regions(missing: List[str]) -> List[Tuple[str, str]]:
    """Groups a sorted list of YYYY-MM-DD dates into contiguous [start, end] regions."""
    regions: List[Tuple[str, str]] = []
    for d in missing:
        if regions and _shift(regions[-1][1], 1) == d:
            regions[-1] = (regions[-1][0], d)
        else:
            regions.append((d, d))
    return regions
def ensure_data(start_date: str, end_date: str, force: bool = False) -> None:
    """Ensures Garmin data covering [start_date, end_date] is present and fresh,
    pulling automatically where the gap is small and surfacing a copy-pastable
    command where it is large. Always continue-with-warning: never aborts, never
    blocks. Call once at command entry with the window the command will read.

    `force` (from --force-pull) bypasses the refresh-minutes throttle: the recent
    mutable zone is re-fetched and Calendar signals re-synced even if a refresh ran
    within the freshness window.
    """
    global _ensured
    # Refresh external calendar signals alongside the data read (independent of Garmin
    # auth; throttled + memoized inside, best-effort). Done first so it still runs even
    # when Garmin credentials are absent.
    _sync_calendar_signals(force=force)

    # No credentials → we can't pull anyway. Stay silent rather than warn on every
    # read; manual `data pull` reports the missing-credentials error explicitly.
    if not (config.garmin_email and config.garmin_password):
        return

    today = today_str()
    pad_start = _shift(start_date, -_derivation_pad_days())
    req_end = min(end_date, today)  # can't pull the future
    if req_end < pad_start:
        return  # window lies entirely in the future

    # Process memo: skip if already covered by a prior ensure this run. --force-pull
    # bypasses the memo so an explicit refresh actually re-fetches.
    if not force and _ensured and _ensured[0] <= pad_start and req_end <= _ensured[1]:
        return

    state = runtime.db.get_sync_state()
    present = set(runtime.db.get_metric_dates())
    refresh_minutes = config.data_refresh_minutes
    mutable_days = config.garmin_mutable_days
    prompt_days = config.garmin_backfill_prompt_days

    # Cold start: nothing pulled ever -> hand the user a backfill command.
    if not state and not present:
        sugg_start = min(pad_start, _shift(today, -config.garmin_initial_backfill_days))
        _warn_manual(sugg_start, today, cold=True)
        _remember(pad_start, req_end)
        return

    # Is the recent (mutable) zone stale? --force-pull treats it as stale unconditionally.
    stale = True
    last_pull_age_min: Optional[int] = None
    if state and state.get("last_pull_utc"):
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(state["last_pull_utc"])
            last_pull_age_min = int(age.total_seconds() // 60)
            stale = age > timedelta(minutes=refresh_minutes)
        except (ValueError, TypeError):
            stale = True
    if force:
        stale = True
    mutable_start = _shift(today, -(mutable_days - 1))

    # A needed day must be fetched if it has no row, or it's in the (stale) mutable
    # zone where values can still change.
    to_fetch = [
        d for d in _date_range(pad_start, req_end)
        if d not in present or (stale and d >= mutable_start)
    ]
    if not to_fetch:
        # Nothing to do. If the only reason we're not re-fetching the recent mutable zone
        # is the refresh-minutes throttle, say so — otherwise this silent reuse is opaque.
        if not stale and req_end >= mutable_start:
            age_note = (
                f"last pull {last_pull_age_min}m ago, < {refresh_minutes}m"
                if last_pull_age_min is not None else "recently pulled"
            )
            step(
                f"Garmin data is fresh ({age_note}); using cache. "
                "Pass --force-pull to refresh now."
            )
        _remember(pad_start, req_end)
        return

    auto_regions: List[Tuple[str, str]] = []
    surfaced_regions: List[Tuple[str, str]] = []
    pad_days = _derivation_pad_days()
    for region in _contiguous_regions(to_fetch):
        span = (_to_date(region[1]) - _to_date(region[0])).days + 1
        # Small gaps (incl. the cheap recent mutable-zone refresh, always <=
        # mutable_days) pull automatically; large ones are surfaced as a command.
        # Regions lying entirely BEFORE the requested window exist only to warm the
        # derivation pad — they are bounded by the pad itself and were never asked
        # for by the user, so they always auto-pull: without this, widening the pad
        # (28 -> 63 days for CTL) would leave every pre-existing install nagging
        # "run data pull -d ..." on each command instead of healing itself.
        limit = max(prompt_days, pad_days) if region[1] < start_date else prompt_days
        if span <= limit:
            auto_regions.append(region)
        else:
            surfaced_regions.append(region)

    for region in auto_regions:
        try:
            step(f"Auto-syncing Garmin {region[0]}..{region[1]}...")
            _g.pull(region[0], region[1], throttle=config.garmin_throttle_seconds)
        except GarminAuthRequired:
            warn(
                "Garmin re-auth required — run "
                + cmd("python trainmate_cli.py data pull")
                + " in a terminal. Continuing with cached data."
            )
            break
        except Exception as e:
            warn(f"Garmin sync failed ({e}). Continuing with cached data.")
            break

    if surfaced_regions:
        merged_start = min(r[0] for r in surfaced_regions)
        merged_end = max(r[1] for r in surfaced_regions)
        _warn_manual(merged_start, merged_end, cold=False)

    _remember(pad_start, req_end)
def _warn_manual(start: str, end: str, *, cold: bool) -> None:
    headline = "No Garmin data has been pulled yet. To get started, run:"
    if not cold:
        headline = (
            f"This view needs Garmin data back to {start}, which hasn't been pulled. "
            "Baselines may be incomplete. Fitness/fatigue (CTL/ATL/TSB) also "
            f"warm up over the first ~{config.pmc_ctl_days} days of history, so on a "
            "shallow backfill freshness can read artificially low. To backfill, run:"
        )
    warn(headline + "\n  " + cmd(_pull_command(start, end), quote=False))
def _remember(start: str, end: str) -> None:
    global _ensured
    if _ensured is None:
        _ensured = (start, end)
    else:
        _ensured = (min(_ensured[0], start), max(_ensured[1], end))
def reset_memo() -> None:
    """Clears the process-level ensure memo (used by tests)."""
    global _ensured
    _ensured = None
