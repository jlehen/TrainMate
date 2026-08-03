import csv as csv_mod
import argparse
import sys
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate import intensity
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.garmin.load import activity_load, load_method
from trainmate.sports import sport_aliases
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    color_load_ratio, pmc_cells, visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, render_table, is_narrow_client, default_wrap_width,
    today_str as _today_str, today_date as _today_date,
)
from trainmate.cli.common import (
    fmt_date, ensure_recent_data, mark_adherence_range, pmc_warmup_cutoff,
    resolve_cleanup_range,
)


def run_data_pull(args: argparse.Namespace) -> None:
    """Pulls athlete metrics and activities directly from Garmin Connect.

    Explicit/manual pull: does exactly the range asked for (mirrors GarminScraper's
    options) and advances the watermark. The watermark/auto-ensure logic lives in
    cli.garmin.ensure_data, which commands call when reading.
    """
    end_date = args.until_date or _today_str()
    if args.from_date:
        start_date = args.from_date
    else:
        days = max(1, args.days)
        start_date = (
            datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(days=days - 1)
        ).strftime("%Y-%m-%d")

    pulled = False
    try:
        cli.garmin.pull(
            start_date, end_date,
            metrics=not args.activities_only,
            activities=not args.metrics_only,
            throttle=args.sleep,
        )
        pulled = True
    except cli.garmin.GarminAuthRequired as e:
        print(red(f"Garmin authentication required: {e}"))
        print(yellow("Run this command in an interactive terminal to complete MFA."))
    except Exception as e:
        print(red(f"Error pulling from Garmin: {e}"))

    # Ride-along: with fresh activity data in hand, stamp the adherence verdict
    # onto past Calendar events over the pulled range (best-effort — a Calendar
    # failure never breaks the pull; no-op when no calendar is configured).
    if pulled and not getattr(args, 'no_mark', False):
        try:
            marked = mark_adherence_range(start_date, end_date)
            if marked:
                print(green(f"Marked {marked} past Calendar event(s) with adherence."))
        except Exception as e:
            print(yellow(f"Warning: adherence Calendar marking skipped: {e}"))


def run_data_backfill_tss(args: argparse.Namespace) -> None:
    """Recomputes stored TSS for all cached activities under the current
    zone-based hierarchy, then refreshes the derived PMC."""
    changed = cli.garmin.backfill_tss(
        start_date=args.from_date,
        end_date=args.until_date,
        verbose=getattr(args, "verbose", False),
    )
    print(green(f"Backfill complete. {changed} activities updated."))


def run_data_wipe(args: argparse.Namespace) -> None:
    """Wipes locally cached data after confirmation. --garmin / --calendar scope the
    wipe to Garmin evidence or ingested daily context respectively (neither flag = both);
    --from/--until/--days restrict it to a date window."""
    garmin = getattr(args, "garmin", False)
    calendar = getattr(args, "calendar", False)
    # No scope flag means "everything" — keep the historical full-wipe behaviour.
    if not garmin and not calendar:
        garmin = calendar = True

    start, end = resolve_cleanup_range(args)

    scope_parts = []
    if garmin:
        scope_parts.append("Garmin metrics, baselines, and activities")
    if calendar:
        scope_parts.append("ingested daily-context signals")
    scope = " and ".join(scope_parts)

    if start and end:
        window = f" dated {start} to {end}"
    elif start:
        window = f" dated {start} onward"
    elif end:
        window = f" dated up to {end}"
    else:
        window = ""

    if not args.yes:
        if not cli.prompt.confirm(
            f"Are you sure you want to wipe {scope}{window}?", danger=True
        ):
            print("Wipe cancelled.")
            return

    if garmin:
        cli.db.wipe_garmin_data(start, end)
        # A ranged wipe leaves deleted load baked into later days' CTL/ATL EWMAs, so
        # recompute after the wipe commits. Done here at the command layer (not the db
        # method) to avoid a garmin<->db circular import, and pinned to cli.db so it
        # sweeps the same database the wipe ran against. See DESIGN_pmc_fitness_fatigue.md §4.
        cli.garmin.recompute_derived(dbh=cli.db)
    if calendar:
        cli.db.wipe_calendar_context(start, end)
    print(green(f"Wiped {scope}{window}."))


def _resolve_historical_date_range(
    args: argparse.Namespace, default_days: int = 7
) -> tuple[Optional[str], Optional[str]]:
    """Resolves (start_date, end_date) for historical queries, looking back by default."""
    if getattr(args, 'all', False):
        return None, None

    today_str = _today_str()

    # Determine end_date (default is today_str, capped/anchored by until/mesocycle/goal)
    end_date = today_str
    if getattr(args, 'until_date', None) is not None:
        end_date = args.until_date
    elif getattr(args, 'until_meso_id', None) is not None:
        meso_id = args.until_meso_id
        if meso_id == -1:
            active_meso = cli.db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = cli.db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        end_date = meso['end_date']
    elif getattr(args, 'meso_id', None) is not None:
        meso_id = args.meso_id
        if meso_id == -1:
            active_meso = cli.db.get_active_mesocycle(today_str)
            if not active_meso:
                print(red("Error: No active mesocycle found."))
                sys.exit(1)
            meso = active_meso
        else:
            meso = cli.db.get_mesocycle(meso_id)
            if not meso:
                print(red(f"Error: Mesocycle with ID {meso_id} not found."))
                sys.exit(1)
        end_date = meso['end_date']
    elif getattr(args, 'goal_id', None) is not None:
        goal_id = args.goal_id
        if goal_id == -1:
            active_goal = cli.db.get_active_objective()
            if not active_goal:
                print(red("Error: No active goal found."))
                sys.exit(1)
            goal_id = active_goal['id']
        macro = cli.db.get_macrocycle_for_objective(goal_id)
        if not macro:
            print(red(f"Error: No plan exists for Goal ID {goal_id}."))
            sys.exit(1)
        mesos = cli.db.get_mesocycles_for_macrocycle(macro['id'])
        if not mesos:
            print(red(f"Error: No mesocycles found for Goal ID {goal_id}."))
            sys.exit(1)
        end_date = max(m['end_date'] for m in mesos)

    # Validate end_date format
    try:
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        print(red(f"Invalid date format for end date: '{end_date}'. Use YYYY-MM-DD."))
        sys.exit(1)

    # Determine start_date
    start_date = None
    if getattr(args, 'from_date', None) is not None:
        start_date = args.from_date
    elif getattr(args, 'from_meso', False):
        active_meso = cli.db.get_active_mesocycle(today_str)
        if not active_meso:
            print(red("Error: No active mesocycle found to start from."))
            sys.exit(1)
        start_date = active_meso['start_date']
    elif getattr(args, 'meso_id', None) is not None:
        start_date = meso['start_date']
    elif getattr(args, 'goal_id', None) is not None:
        start_date = min(m['start_date'] for m in mesos)

    # If start_date is still not resolved, resolve it via days/weeks lookback from end_date
    if start_date is None:
        days = getattr(args, 'days', None)
        weeks = getattr(args, 'weeks', None)
        if days is not None:
            start_date_obj = end_date_obj - timedelta(days=days - 1)
        elif weeks is not None:
            ndays = max(1, round(weeks * 7))
            start_date_obj = end_date_obj - timedelta(days=ndays - 1)
        else:
            start_date_obj = end_date_obj - timedelta(days=default_days - 1)
        start_date = start_date_obj.strftime("%Y-%m-%d")

    # Validate start_date format
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        print(red(f"Invalid date format for start date: '{start_date}'. Use YYYY-MM-DD."))
        sys.exit(1)

    return start_date, end_date


def run_data_show_metrics(args: argparse.Namespace) -> None:
    """Displays athlete metrics over the resolved date range."""
    start_date, end_date = _resolve_historical_date_range(args, default_days=7)

    if not getattr(args, 'no_pull', False) and not getattr(args, 'all', False):
        try:
            cli.garmin.ensure_data(
                start_date, end_date, force=getattr(args, 'force_pull', False)
            )
        except Exception as e:
            print(yellow(f"Warning: Could not ensure recent data: {e}"))

    metrics_history = cli.db.get_metrics_cache(start_date=start_date, end_date=end_date)

    if getattr(args, 'csv', False):
        _show_metrics_csv(metrics_history)
        return

    range_str = f"{start_date} to {end_date}" if start_date and end_date else "All Time"
    print(bold(cyan(f"\n=== ATHLETE METRICS ({range_str}) ===")))
    if not metrics_history:
        print("No metrics cached in this range.")
        return

    headers = [
        "Date", "HRV", "HRV Base", "RHR", "RHR Base", "Sleep", "Sleep Base",
        "Stress", "CTL", "ATL", "TSB", "ATL:CTL",
    ]
    rows = []

    warmup_cutoff = pmc_warmup_cutoff()

    for m in metrics_history:
        base = cli.db.get_baseline(m['date'])

        hrv_val = m['hrv']
        rhr_val = m['rhr']
        sleep_val = m['sleep_score']
        stress_val = m['stress']

        hrv_str = str(hrv_val) if hrv_val is not None else "N/A"
        rhr_str = str(rhr_val) if rhr_val is not None else "N/A"
        sleep_str = str(sleep_val) if sleep_val is not None else "N/A"
        stress_str = str(stress_val) if stress_val is not None else "N/A"

        # Ratio off the *display* values, so a warm-up-suppressed CTL can't surface as a
        # spurious spike (DESIGN_pmc_fitness_fatigue.md §6.2).
        ctl_v, atl_v, tsb_v = cli.garmin.pmc_display_values(m, warmup_cutoff)
        ctl_str, atl_str, tsb_str = pmc_cells(ctl_v, atl_v, tsb_v)
        ratio_val = cli.garmin.load_ratio(atl_v, ctl_v)
        ratio_str = color_load_ratio(ratio_val) if ratio_val is not None else "N/A"

        hrv_base_str = "N/A"
        rhr_base_str = "N/A"
        sleep_base_str = "N/A"

        if base:
            if hrv_val is not None and base['hrv_baseline_mean'] is not None:
                sd = base['hrv_baseline_std'] or 1.0
                if hrv_val < (base['hrv_baseline_mean'] - sd):
                    hrv_str = red(f"{hrv_val} (v)")
                else:
                    hrv_str = green(str(hrv_val))
            if base['hrv_baseline_mean'] is not None:
                hrv_base_str = f"{base['hrv_baseline_mean']:.1f}"

            if rhr_val is not None and base['rhr_baseline_mean'] is not None:
                sd = base['rhr_baseline_std'] or 1.0
                if rhr_val > (base['rhr_baseline_mean'] + max(3.0, sd)):
                    rhr_str = red(f"{rhr_val} (^)")
                else:
                    rhr_str = green(str(rhr_val))
            if base['rhr_baseline_mean'] is not None:
                rhr_base_str = f"{base['rhr_baseline_mean']:.1f}"

            if sleep_val is not None:
                if sleep_val < 60:
                    sleep_str = red(f"{sleep_val} (v)")
                else:
                    sleep_str = green(str(sleep_val))
            if base['sleep_baseline_mean'] is not None:
                sleep_base_str = f"{base['sleep_baseline_mean']:.1f}"

        rows.append([
            m['date'], hrv_str, hrv_base_str, rhr_str, rhr_base_str,
            sleep_str, sleep_base_str, stress_str,
            ctl_str, atl_str, tsb_str, ratio_str,
        ])

    print(render_table(headers, rows))
    print(gray("((v) suppressed/poor, (^) elevated compared to baseline)\n"))


def _show_metrics_csv(metrics_history: list) -> None:
    """Output metrics as CSV."""
    writer = csv_mod.writer(sys.stdout)
    writer.writerow([
        "date", "hrv", "hrv_baseline", "rhr", "rhr_baseline",
        "sleep_score", "sleep_baseline", "stress", "ctl", "atl", "tsb",
        "atl_ctl_ratio",
    ])
    # Suppressed (warm-up) or NULL PMC values are emitted as empty cells, never 0, so
    # downstream parsing can't read a zero as data (DESIGN_pmc_fitness_fatigue.md §6.2).
    warmup_cutoff = pmc_warmup_cutoff()
    for m in metrics_history:
        base = cli.db.get_baseline(m['date'])
        hrv_base = None
        rhr_base = None
        sleep_base = None
        if base:
            hrv_base = base.get('hrv_baseline_mean')
            rhr_base = base.get('rhr_baseline_mean')
            sleep_base = base.get('sleep_baseline_mean')
        ctl_v, atl_v, tsb_v = cli.garmin.pmc_display_values(m, warmup_cutoff)
        ratio_v = cli.garmin.load_ratio(atl_v, ctl_v)
        writer.writerow([
            m['date'], m['hrv'], hrv_base, m['rhr'], rhr_base,
            m['sleep_score'], sleep_base, m['stress'],
            "" if ctl_v is None else ctl_v,
            "" if atl_v is None else atl_v,
            "" if tsb_v is None else tsb_v,
            "" if ratio_v is None else ratio_v,
        ])


# Where `activity_load` took a row's number from, as a tag beside it
# (DESIGN_intensity_distribution.md §9.7). The highest-value fact missing from this view
# is not the zone breakdown, it is the provenance of the TSS: every downstream confusion
# about the load half of `tm progress` disagreeing with the zone half traces back to a
# provenance that was invisible. `rpe+` is a path `compute_load` has no word for — the
# measurement was trustworthy but the athlete's RPE implied materially more strain, so
# the load came from RPE anyway (the kettlebell case §6 exists for).
LOAD_TAGS = {
    "power": "pwr", "hr": "hr", "rpe": "rpe", "hr_sparse": "sparse!",
    "rpe_divergence": "rpe+", "measured": "tss", "none": "—",
}


def _load_cell(act: dict) -> str:
    """`188 (pwr)` — the load and where it came from, about six characters.

    The figure is `activity_load()`, NOT the stored `tss`. `measured_tss` defines that
    column as a pure measurement — no coverage gate, never RPE — while `progress`, the
    PMC and every coaching path use `activity_load()`, so the two commands already
    disagreed about a session's load, silently. A tag on the measurement would name a
    provenance that is not the provenance of the number shown.
    """
    return f"{activity_load(act):.1f} ({LOAD_TAGS.get(load_method(act), '?')})"


def _zone_coverage(act: dict, prefix: str, n: int) -> float:
    duration = float(act.get("duration_sec") or 0.0)
    if not duration:
        return 0.0
    total = sum(float(act.get(f"{prefix}{i}_sec") or 0.0) for i in range(1, n + 1))
    return total / duration


def _show_activities_zones(activities: list) -> None:
    """`--zones`: one row per activity x currency, both currencies, nothing aggregated.

    It SWAPS columns rather than widening — Distance, Elev, Avg HR, Max HR and Avg Watts
    are not what the flag was reached for, and five HR zones plus seven power zones
    cannot join twelve existing columns. A ride with both a meter and a strap renders two
    rows, which keeps §6's one prohibition structural: the two views of the same time are
    separate rows, never adjacent columns inviting addition (§9.7).
    """
    headers = ["Date", "Type", "Duration", "Cur"] + [
        f"Z{i}" for i in range(1, 8)
    ] + ["Cov", "TSS"]
    rows = []
    for act in activities:
        for cur in intensity.CURRENCIES:
            n = len(cur.labels)
            secs = [
                float(act.get(f"{cur.prefix}{i}_sec") or 0.0) for i in range(1, n + 1)
            ]
            if not any(secs):
                continue
            cells = [intensity.fmt_duration(s) if s else "—" for s in secs]
            cells += [""] * (7 - n)  # HR rows leave Z6/Z7 blank
            rows.append(
                [act["date"], act["activity_type"].upper(),
                 intensity.fmt_duration(act.get("duration_sec") or 0.0), cur.tag]
                + cells
                + [f"{_zone_coverage(act, cur.prefix, n) * 100:.0f}%", _load_cell(act)]
            )
    if not rows:
        print("No zone data recorded for these activities.")
        return
    print(render_table(headers, rows))
    print(gray(wrap_text(intensity.NEVER_SUM_NOTE)))


def run_data_show_activities(args: argparse.Namespace) -> None:
    """Displays completed activities over the resolved date range."""
    start_date, end_date = _resolve_historical_date_range(args, default_days=7)

    if not getattr(args, 'no_pull', False) and not getattr(args, 'all', False):
        try:
            cli.garmin.ensure_data(
                start_date, end_date, force=getattr(args, 'force_pull', False)
            )
        except Exception as e:
            print(yellow(f"Warning: Could not ensure recent data: {e}"))

    activities = cli.db.get_completed_activities(
        start_date=start_date, end_date=end_date
    )

    if getattr(args, 'sport_type', None) is not None:
        # Alias-aware, so `--type cycling` stops missing `road_biking`,
        # `gravel_cycling`, `mountain_biking` and `indoor_cycling` — the athlete
        # filtering for their cycling and seeing a fraction of it (§9.7).
        wanted = set(sport_aliases(args.sport_type))
        activities = [
            act for act in activities
            if (act['activity_type'] or "").strip().lower() in wanted
        ]

    if getattr(args, 'csv', False):
        _show_activities_csv(activities)
        return

    range_str = f"{start_date} to {end_date}" if start_date and end_date else "All Time"
    print(bold(cyan(f"\n=== COMPLETED ACTIVITIES ({range_str}) ===")))
    if not activities:
        print("No completed activities found in this range.")
        return

    if getattr(args, 'zones', False):
        _show_activities_zones(activities)
        return

    headers = [
        "Date", "Time", "Type", "Name", "Duration", "Distance", "Elev",
        "Avg HR", "Max HR", "Avg Watts", "RPE", "TSS",
    ]
    rows = []

    for act in activities:
        dur_sec = act.get('duration_sec') or 0.0
        h = int(dur_sec // 3600)
        m = int((dur_sec % 3600) // 60)
        s = int(dur_sec % 60)
        dur_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

        name_str = act.get('activity_name') or ""
        if len(name_str) > 25:
            name_str = name_str[:22] + "..."

        dist_val = act.get('distance_km')
        dist_str = f"{dist_val:.1f} km" if dist_val is not None else "0.0 km"

        elev_val = act.get('elevation_gain_m')
        elev_str = f"{elev_val:.0f} m" if elev_val is not None else "0 m"

        avg_hr = act.get('avg_hr')
        avg_hr_str = str(avg_hr) if avg_hr is not None else "N/A"

        max_hr = act.get('max_hr')
        max_hr_str = str(max_hr) if max_hr is not None else "N/A"

        watts = act.get('bike_avg_watts')
        watts_str = f"{watts} W" if watts is not None else "N/A"

        rpe_val = act.get('rpe')
        rpe_str = str(rpe_val) if rpe_val is not None else "N/A"

        tss_str = _load_cell(act)

        start_time = act.get('start_time') or ""
        if " " in start_time:
            time_str = start_time.split(" ", 1)[1]
        elif "T" in start_time:
            time_str = start_time.split("T", 1)[1]
            if "+" in time_str:
                time_str = time_str.split("+", 1)[0]
            elif "-" in time_str:
                time_str = time_str.split("-", 1)[0]
            elif "Z" in time_str:
                time_str = time_str.split("Z", 1)[0]
        else:
            time_str = start_time or "N/A"
        if len(time_str) > 8:
            time_str = time_str[:8]

        rows.append([
            act['date'], time_str, act['activity_type'].upper(), name_str,
            dur_str, dist_str, elev_str, avg_hr_str, max_hr_str, watts_str,
            rpe_str, tss_str,
        ])

    table = render_table(headers, rows)
    print(table)

    # Summary footer
    total_count = len(activities)
    total_duration_sec = sum(act.get('duration_sec') or 0.0 for act in activities)
    total_distance_km = sum(act.get('distance_km') or 0.0 for act in activities)
    total_elevation_m = sum(act.get('elevation_gain_m') or 0.0 for act in activities)
    # The load, matching the column above — so this command and `progress` finally quote
    # one number for the same session (§9.7).
    total_tss = sum(activity_load(act) for act in activities)

    tot_h = int(total_duration_sec // 3600)
    tot_m = int((total_duration_sec % 3600) // 60)
    tot_dur_str = f"{tot_h}h {tot_m}m" if tot_h > 0 else f"{tot_m}m"

    narrow = is_narrow_client()
    sep = "\n" if narrow else " | "
    rule_width = (
        default_wrap_width() if narrow
        else max(visible_len(line) for line in table.splitlines())
    )
    print(gray("-" * rule_width))
    print(bold(sep.join([
        f"Summary: {total_count} activities",
        f"Duration: {tot_dur_str}",
        f"Distance: {total_distance_km:.1f} km",
        f"Elevation: {total_elevation_m:.0f} m",
        f"TSS: {total_tss:.1f}",
    ])))


_CSV_ZONE_COLUMNS = (
    [f"zone{i}_sec" for i in range(1, 6)]
    + [f"power_zone{i}_sec" for i in range(1, 8)]
)


def _show_activities_csv(activities: list) -> None:
    """Output activities as CSV — everything, with no flag gating it.

    No width constraint here and its consumers want completeness, so all twelve zone
    columns, both coverage fractions and the load provenance go in unconditionally
    (§9.7). `tss` stays the stored measurement; `load` and `load_method` carry the
    number every coaching path actually uses, so a script can see both and know which
    is which.
    """
    writer = csv_mod.writer(sys.stdout)
    writer.writerow([
        "date", "start_time", "activity_type", "activity_name",
        "duration_sec", "distance_km", "elevation_gain_m",
        "avg_hr", "max_hr", "bike_avg_watts", "rpe", "tss",
        "load", "load_method", "hr_coverage", "power_coverage",
    ] + _CSV_ZONE_COLUMNS)
    for act in activities:
        writer.writerow([
            act.get('date'), act.get('start_time'),
            act.get('activity_type'), act.get('activity_name'),
            act.get('duration_sec'), act.get('distance_km'),
            act.get('elevation_gain_m'), act.get('avg_hr'),
            act.get('max_hr'), act.get('bike_avg_watts'),
            act.get('rpe'), act.get('tss'),
            f"{activity_load(act):.1f}", load_method(act),
            f"{_zone_coverage(act, 'zone', 5):.3f}",
            f"{_zone_coverage(act, 'power_zone', 7):.3f}",
        ] + [act.get(col) for col in _CSV_ZONE_COLUMNS])


def run_data_bootstrap(args: argparse.Namespace) -> None:
    """Cold-start reconstruction over the full training backlog (seeds learnings,
    establishes the reflect watermark)."""
    try:
        result = cli.coach_service.data_bootstrap(
            from_date_str=args.from_date,
            until_date_str=args.until_date,
            days=args.days,
            weeks=args.weeks,
            context=args.context,
            force=args.force,
            inspect_only=args.inspect_only,
            no_pull=args.no_pull,
            force_pull=args.force_pull,
            auto=getattr(args, "auto", False),
        )
        _render_analysis_report(result, args.inspect_only)
    except Exception as e:
        print(red(f"Error running training history bootstrap: {e}"))


def run_data_reflect(args: argparse.Namespace) -> None:
    """Incremental reflection over evidence accrued since the last reflect watermark."""
    try:
        result = cli.coach_service.data_reflect(
            from_date_str=args.from_date,
            until_date_str=args.until_date,
            days=args.days,
            weeks=args.weeks,
            context=args.context,
            force=args.force,
            inspect_only=args.inspect_only,
            no_pull=args.no_pull,
            force_pull=args.force_pull,
            auto=getattr(args, "auto", False),
        )
        if not result:
            return  # Nothing new to reflect on; service already printed why.
        _render_analysis_report(result, args.inspect_only)
    except Exception as e:
        print(red(f"Error running training reflection: {e}"))


def _render_analysis_report(result: dict, inspect_only: bool) -> None:
    """Renders a bootstrap/reflect reconstruction + applied coach learning deltas."""
    try:
        print(bold(cyan("\n=== HISTORICAL WORKOUT ANALYSIS REPORT ===")))
        
        # Macrocycle Overview
        if "inferred_macrocycle" in result:
            im = result["inferred_macrocycle"]
            print(
                f"\n{bold('Macrocycle Focus')}: {cyan(im.get('overall_focus', 'N/A'))} "
                f"({magenta(im.get('start_date', ''))} to {magenta(im.get('end_date', ''))})"
            )
        
        if "macrocycle_summary" in result:
            print(format_labeled_block(f"{bold('Summary')}:", result["macrocycle_summary"]))

        # Inferred Mesocycles
        if "inferred_mesocycles" in result and result["inferred_mesocycles"]:
            print(bold(cyan("\nDetected Mesocycle Blocks:")))
            for meso in result["inferred_mesocycles"]:
                c_tag = meso.get("estimated_consistency", "Moderate")
                if c_tag == "High":
                    c_disp = green("[High Consistency]")
                elif c_tag == "Low":
                    c_disp = red("[Low Consistency]")
                else:
                    c_disp = yellow("[Moderate Consistency]")

                print(
                    f"  - {green(meso.get('name', 'Phase'))} "
                    f"({cyan(meso.get('start_date', ''))} to {cyan(meso.get('end_date', ''))}) "
                    f"{c_disp}"
                )
                print(f"    * Detected Focus: {meso.get('focus_detected', 'N/A')}")
                print(f"    * Avg Weekly TSS: {meso.get('average_weekly_tss', 'N/A')}")

        # Physiological Insights
        if "physiological_insights" in result and result["physiological_insights"]:
            print(bold(cyan("\nPhysiological Insights:")))
            for insight in result["physiological_insights"]:
                print(f"  - {insight}")

        # Coach learnings (incremental updates applied to learnings)
        updates = result.get("learning_updates")
        if updates:
            header = (
                "Coach Observations (NOT saved — inspect mode):"
                if inspect_only
                else "Coach Observations (Saved to learnings):"
            )
            print(bold(cyan("\n" + header)))
            try:
                learnings_map = {l['id']: l for l in cli.db.get_learnings()}
            except Exception:
                learnings_map = {}
            for u in updates:
                op = u.get("op")
                meta = []
                if u.get("sports"):
                    meta.append(u["sports"])
                ev = u.get("evidence")
                if isinstance(ev, (list, tuple)) and ev:
                    meta.append(f"weeks: {', '.join(str(w) for w in ev)}")
                suffix = f" ({'; '.join(meta)})" if meta else ""

                def _existing_text(uid):
                    if uid is not None and uid in learnings_map:
                        return learnings_map[uid]['text']
                    return ""

                if op == "add":
                    print(f"  + {u.get('text', '')}{suffix}")
                elif op == "revise":
                    print(f"  ~ [{u.get('id')}] {u.get('text', '')}{suffix}")
                elif op == "reinforce":
                    tag = f"  ↑ reinforced [{u.get('id')}]{suffix}"
                    text = _existing_text(u.get("id"))
                    print(format_labeled_block(tag, text) if text else tag)
                elif op == "contradict":
                    tag = f"  ↓ contradicted [{u.get('id')}]{suffix}"
                    text = _existing_text(u.get("id"))
                    print(format_labeled_block(tag, text) if text else tag)
                elif op == "retire":
                    print(f"  - retired [{u.get('id')}]")

        print(bold(cyan("\n==========================================")))

    except Exception as e:
        print(red(f"Error rendering workout analysis: {e}"))


def add_data_parser(subparsers, pull_bypass_parser, llm_debug_parser, basic_date_parser, plan_date_parser, sport_type_parser):
    # data command & subparsers
    data_parser = subparsers.add_parser(
        "data",
        help="Manage and sync athlete metrics and activities"
    )
    data_subparsers = data_parser.add_subparsers(
        dest="subcommand", help="Data sub-commands"
    )
    
    # data pull
    d_pull = data_subparsers.add_parser(
        "pull",
        help="Fetch Garmin activities/metrics and Google Calendar context",
        description=(
            "Fetch activities and daily metrics directly from Garmin Connect into the "
            "local cache, advancing the sync watermark. With no range, pulls the last "
            "2 days ending today (--days N for a different window, or --from/--until "
            "for an explicit range). Pulls both metrics and activities unless "
            "--metrics-only/--activities-only is given. Also syncs tagged daily-context "
            "events (alcohol, sleep, stress, …) from Google Calendar into the local cache. "
            "Past Calendar events in the pulled range are stamped with the adherence "
            "verdict unless --no-mark is given."
        )
    )
    d_pull.add_argument(
        "--days", type=int, default=2, metavar="N",
        help="Number of days to pull, ending today (default: 2)"
    )
    d_pull.add_argument(
        "--no-mark", action="store_true", dest="no_mark",
        help="Skip stamping past Calendar events with the adherence verdict"
    )
    d_pull.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Start date for an explicit range (overrides --days)"
    )
    d_pull.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="End date for an explicit range (defaults to today)"
    )
    d_pull.add_argument(
        "--sleep", type=float, dest="sleep", metavar="SECONDS",
        help="Throttle: seconds to sleep between Garmin calls (default from config)"
    )
    pull_group = d_pull.add_mutually_exclusive_group()
    pull_group.add_argument(
        "--metrics-only", action="store_true", help="Pull daily metrics only"
    )
    pull_group.add_argument(
        "--activities-only", action="store_true", help="Pull activities only"
    )

    # data bootstrap — cold-start backward reconstruction over the full backlog
    d_boot = data_subparsers.add_parser(
        "bootstrap", aliases=["b"], advanced=True,
        parents=[pull_bypass_parser, basic_date_parser, llm_debug_parser],
        help="Reconstruct macro/mesocycles from your full backlog (run once): "
             "seeds coach learnings + a cached reconstruction fed to 'plan generate'",
        description=(
            "Cold-start: reverse-engineer past training cycles from completed workouts "
            "and metrics. With no date filter, the window is auto-detected from the active "
            "goal (since the previous goal, else 12 weeks back). Two outputs: (1) coach "
            "learnings, delta-updated from the evidence; and (2) a cached reconstruction — "
            "the inferred macro focus, mesocycle blocks, and physiological insights — which "
            "'plan generate' replays read-only into its strategy prompt so the next plan "
            "builds on your demonstrated training arc. Also establishes the reflect "
            "watermark so later 'data reflect' runs only ingest newer evidence. Cached by "
            "evidence fingerprint: an unchanged re-run reuses the cache unless --force; "
            "--inspect-only renders the analysis without writing learnings or the cache."
        )
    )
    # data reflect — incremental reflection over evidence since the last reflect
    d_reflect = data_subparsers.add_parser(
        "reflect",
        parents=[pull_bypass_parser, basic_date_parser, llm_debug_parser],
        help="Update coach learnings from how the athlete responded to training "
             "since the last reflect (incremental; no reconstruction)",
        description=(
            "Incremental: analyze only evidence accrued since the last reflect watermark "
            "(the day after the last reflected-through date). Its output is coach learnings "
            "— delta-updated from the new evidence; unlike 'data bootstrap' it does not "
            "feed a reconstruction to 'plan generate'. A date filter overrides the "
            "watermark. Because overlapping history is never re-counted, repeated runs no "
            "longer ratchet confidence to 'established'. Run 'data bootstrap' first to "
            "establish a baseline. --inspect-only renders without writing; --force bypasses "
            "the per-window cache."
        )
    )
    for d_an in (d_boot, d_reflect):
        d_an.add_argument(
            "--context", dest="context",
            help="Optional text context detailing subjective athlete notes (travel, illness, etc.)"
        )
        d_an.add_argument(
            "-f", "--force", action="store_true",
            help="Recompute even if the evidence is unchanged (bypass the analysis cache)"
        )
        d_an.add_argument(
            "--inspect-only", action="store_true",
            help="Read-only: show the analysis without writing coach learnings or the cache"
        )
        d_an.add_argument(
            "--auto", action="store_true",
            help="Unattended: skip interactive demotion prompts. Staleness demotions apply "
                 "directly; contradiction demotions stay queued for the next interactive review."
        )

    # data backfill-tss
    d_btss = data_subparsers.add_parser(
        "backfill-tss", advanced=True,
        help="Recompute TSS for all stored activities using the current "
             "zone-based model (no Garmin calls needed)"
    )
    d_btss.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Only process activities on or after this date"
    )
    d_btss.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="Only process activities on or before this date"
    )
    d_btss.add_argument(
        "-v", "--verbose", action="store_true",
        help="List activities with low HR-zone coverage that need an RPE"
    )

    # data show-metrics
    d_sm = data_subparsers.add_parser(
        "show-metrics", aliases=["sm"],
        parents=[pull_bypass_parser, plan_date_parser],
        help="Show athlete metrics over a date range",
        description=(
            "Show cached daily athlete metrics (RHR, HRV, sleep, stress) over a date "
            "range. With no date filter, looks back 7 days ending today; -a/--all "
            "shows every cached row. Freshens recent data from Garmin first unless "
            "--no-pull or --all is given. Use --csv for machine-readable output."
        )
    )
    d_sm.add_argument(
        "--csv", action="store_true", dest="csv",
        help="Output data as CSV for script consumption"
    )
    d_sm.add_argument(
        "-a", "--all", action="store_true", dest="all",
        help="Show all cached athlete metrics"
    )

    # data show-activities
    d_sa = data_subparsers.add_parser(
        "show-activities", aliases=["sa"],
        parents=[pull_bypass_parser, plan_date_parser, sport_type_parser],
        help="Show completed activities over a date range",
        description=(
            "Show cached completed activities over a date range. With no date filter, "
            "looks back 7 days ending today; -a/--all shows every cached activity. "
            "Filter with --type (alias-aware: 'cycling' matches gravel, MTB and indoor "
            "rides too), freshen from Garmin unless --no-pull/--all, and use --csv for "
            "machine-readable output. The TSS column shows the training LOAD with where "
            "it came from — (pwr), (hr), (rpe), (rpe+) when your RPE outvoted a "
            "trustworthy meter, (sparse!) when the recording was too thin to trust and "
            "no RPE was entered."
        )
    )
    d_sa.add_argument(
         "--csv", action="store_true", dest="csv",
         help="Output data as CSV for script consumption (every zone column, both "
              "coverage fractions and the load provenance, unconditionally)"
     )
    d_sa.add_argument(
        "--zones", action="store_true", dest="zones",
        help="Show time in zone per activity instead of the distance/HR/watts columns: "
             "one row per activity and currency, so a ride with both a meter and a "
             "strap gets a [pwr] row and an [HR] row. Includes per-activity zone "
             "coverage — the column that turns 'the strap dropped out somewhere this "
             "week' into a named session."
    )
    d_sa.add_argument(
        "-a", "--all", action="store_true", dest="all",
        help="Show all cached completed activities"
    )

    # data wipe
    d_wipe = data_subparsers.add_parser(
        "wipe", advanced=True,
        help="Wipe locally cached Garmin data and/or daily context from the database",
        description=(
            "Delete locally cached data. With no scope flag, wipes everything (Garmin "
            "metrics, baselines, activities, and ingested daily-context signals) and "
            "resets the sync watermarks. --garmin or --calendar narrow the scope; "
            "--from/--until/--days restrict it to a date window (the next 'data pull' "
            "re-fetches what was removed)."
        ),
    )
    d_wipe.add_argument(
        "--garmin", action="store_true",
        help="Wipe only Garmin evidence (metrics, baselines, activities, analysis cache)"
    )
    d_wipe.add_argument(
        "--calendar", "--context", action="store_true", dest="calendar",
        help="Wipe only ingested daily-context signals and reset the Calendar sync token"
    )
    d_wipe.add_argument(
        "--days", type=int, metavar="N",
        help="Restrict to the trailing N days (ending --until, default today)"
    )
    d_wipe.add_argument(
        "--from", "--from-date", dest="from_date", metavar="YYYY-MM-DD",
        help="Restrict to rows on or after this date"
    )
    d_wipe.add_argument(
        "--until", "--until-date", dest="until_date", metavar="YYYY-MM-DD",
        help="Restrict to rows on or before this date"
    )
    d_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    

    return data_parser
