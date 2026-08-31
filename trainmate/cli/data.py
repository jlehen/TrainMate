import csv as csv_mod
import argparse
import sys
from typing import Optional
from trainmate import runtime
from trainmate import intensity
from trainmate.garmin.load import activity_load, load_method
from trainmate.sports import sport_aliases
from trainmate.baselines import classify_metric, is_anomalous, UNKNOWN
from trainmate.util import (
    aside, bold, green, red, yellow, cyan, magenta, gray, cmd, color_load_ratio, pmc_cells,
    visible_len, wrap_text, format_labeled_text, format_labeled_block, render_table,
    is_narrow_client, default_wrap_width, fmt_date, fmt_span, notice,
)
from trainmate.cli.common import mark_adherence_range, pmc_warmup_cutoff
from trainmate.cli.selectors import add_selector_args, resolve_window


def run_data_pull(args: argparse.Namespace) -> None:
    """Pulls athlete metrics and activities directly from Garmin Connect.

    Explicit/manual pull: does exactly the range asked for (mirrors GarminScraper's
    options) and advances the watermark. The watermark/auto-ensure logic lives in
    runtime.garmin.ensure_data, which commands call when reading.
    """
    start_date, end_date = resolve_window(args)

    pulled = False
    try:
        # The sync's own step-by-step narration is side information; its summary is
        # this command's answer, so it prints here (DESIGN_output_verbosity.md §3.1).
        print(green(runtime.garmin.pull(
            start_date, end_date,
            metrics=not args.activities_only,
            activities=not args.metrics_only,
            throttle=args.sleep,
        )))
        pulled = True
    except runtime.garmin.GarminAuthRequired as e:
        notice(f"Garmin authentication required: {e}", red)
        notice("Run this command in an interactive terminal to complete MFA.")
    except Exception as e:
        notice(f"Error pulling from Garmin: {e}", red)

    # Ride-along: with fresh activity data in hand, stamp the adherence verdict
    # onto past Calendar events over the pulled range (best-effort — a Calendar
    # failure never breaks the pull; no-op when no calendar is configured).
    if pulled and not getattr(args, 'no_mark', False):
        try:
            marked = mark_adherence_range(start_date, end_date)
            if marked:
                print(green(f"Marked {marked} past Calendar event(s) with adherence."))
        except Exception as e:
            notice(f"Warning: adherence Calendar marking skipped: {e}")


def run_data_backfill_tss(args: argparse.Namespace) -> None:
    """Recomputes stored TSS for all cached activities under the current
    zone-based hierarchy, then refreshes the derived PMC."""
    start_date, end_date = resolve_window(args)
    changed = runtime.garmin.backfill_tss(
        start_date=start_date,
        end_date=end_date,
        verbose=getattr(args, "verbose", False),
    )
    print(green(f"Backfill complete. {changed} activities updated."))


def run_data_wipe(args: argparse.Namespace) -> None:
    """Wipes locally cached data after confirmation. --garmin / --calendar scope the
    wipe to Garmin evidence or ingested daily signals respectively (neither flag = both);
    -d restricts it to a date window."""
    garmin = getattr(args, "garmin", False)
    calendar = getattr(args, "calendar", False)
    # No scope flag means "everything" — keep the historical full-wipe behaviour.
    if not garmin and not calendar:
        garmin = calendar = True

    start, end = resolve_window(args)

    scope_parts = []
    if garmin:
        scope_parts.append("Garmin metrics, baselines, and activities")
    if calendar:
        scope_parts.append("ingested daily signals")
    scope = " and ".join(scope_parts)

    if start and end:
        window = f" dated {fmt_span(start, end)}"
    elif start:
        window = f" dated {fmt_date(start)} onward"
    elif end:
        window = f" dated up to {fmt_date(end)}"
    else:
        window = ""

    if not args.yes:
        if not runtime.prompt.confirm(
            f"Are you sure you want to wipe {scope}{window}?", danger=True
        ):
            print("Wipe cancelled.")
            return

    if garmin:
        # The post-wipe PMC sweep is part of wiping now, not something the caller has
        # to remember (see db/wipes.py).
        runtime.db.wipe_garmin_data(start, end)
    if calendar:
        runtime.db.wipe_calendar_signals(start, end)
    print(green(f"Wiped {scope}{window}."))


def run_data_show_metrics(args: argparse.Namespace) -> None:
    """Displays athlete metrics over the resolved date range."""
    start_date, end_date = (None, None) if args.all else resolve_window(args)

    if not getattr(args, 'no_pull', False) and not getattr(args, 'all', False):
        try:
            runtime.garmin.ensure_data(
                start_date, end_date, force=getattr(args, 'force_pull', False)
            )
        except Exception as e:
            notice(f"Warning: Could not ensure recent data: {e}")

    metrics_history = runtime.db.get_metrics_cache(start_date=start_date, end_date=end_date)

    if getattr(args, 'csv', False):
        _show_metrics_csv(metrics_history)
        return

    range_str = fmt_span(start_date, end_date) if start_date and end_date else "All Time"
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
        base = runtime.db.get_baseline(m['date'])

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
        ctl_v, atl_v, tsb_v = runtime.garmin.pmc_display_values(m, warmup_cutoff)
        ctl_str, atl_str, tsb_str = pmc_cells(ctl_v, atl_v, tsb_v)
        ratio_val = runtime.garmin.load_ratio(atl_v, ctl_v)
        ratio_str = color_load_ratio(ratio_val) if ratio_val is not None else "N/A"

        hrv_base_str = "N/A"
        rhr_base_str = "N/A"
        sleep_base_str = "N/A"

        if base:
            hrv_verdict = classify_metric('hrv', hrv_val, base)
            if hrv_verdict != UNKNOWN:
                hrv_str = (
                    red(f"{hrv_val} (v)") if is_anomalous(hrv_verdict)
                    else green(str(hrv_val))
                )
            if base['hrv_baseline_mean'] is not None:
                hrv_base_str = f"{base['hrv_baseline_mean']:.1f}"

            rhr_verdict = classify_metric('rhr', rhr_val, base)
            if rhr_verdict != UNKNOWN:
                rhr_str = (
                    red(f"{rhr_val} (^)") if is_anomalous(rhr_verdict)
                    else green(str(rhr_val))
                )
            if base['rhr_baseline_mean'] is not None:
                rhr_base_str = f"{base['rhr_baseline_mean']:.1f}"

            sleep_verdict = classify_metric('sleep', sleep_val)
            if sleep_verdict != UNKNOWN:
                sleep_str = (
                    red(f"{sleep_val} (v)") if is_anomalous(sleep_verdict)
                    else green(str(sleep_val))
                )
            if base['sleep_baseline_mean'] is not None:
                sleep_base_str = f"{base['sleep_baseline_mean']:.1f}"

        rows.append([
            fmt_date(m['date']), hrv_str, hrv_base_str, rhr_str, rhr_base_str,
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
        base = runtime.db.get_baseline(m['date'])
        hrv_base = None
        rhr_base = None
        sleep_base = None
        if base:
            hrv_base = base.get('hrv_baseline_mean')
            rhr_base = base.get('rhr_baseline_mean')
            sleep_base = base.get('sleep_baseline_mean')
        ctl_v, atl_v, tsb_v = runtime.garmin.pmc_display_values(m, warmup_cutoff)
        ratio_v = runtime.garmin.load_ratio(atl_v, ctl_v)
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
                [fmt_date(act["date"]), act["activity_type"].upper(),
                 intensity.fmt_duration(act.get("duration_sec") or 0.0), cur.tag]
                + cells
                + [f"{_zone_coverage(act, cur.prefix, n) * 100:.0f}%", _load_cell(act)]
            )
    if not rows:
        print("No zone data recorded for these activities.")
        return
    print(render_table(headers, rows))
    aside(wrap_text(intensity.NEVER_SUM_NOTE), color_fn=gray)


def run_data_show_activities(args: argparse.Namespace) -> None:
    """Displays completed activities over the resolved date range."""
    start_date, end_date = (None, None) if args.all else resolve_window(args)

    if not getattr(args, 'no_pull', False) and not getattr(args, 'all', False):
        try:
            runtime.garmin.ensure_data(
                start_date, end_date, force=getattr(args, 'force_pull', False)
            )
        except Exception as e:
            notice(f"Warning: Could not ensure recent data: {e}")

    activities = runtime.db.get_completed_activities(
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

    range_str = fmt_span(start_date, end_date) if start_date and end_date else "All Time"
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
            fmt_date(act['date']), time_str, act['activity_type'].upper(), name_str,
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

# The learning-delta ops db.apply_learning_deltas acts on; anything else was skipped there,
# so the report must not present it as saved (DESIGN_backward_evaluation.md §13).
_LEARNING_OPS = ("add", "revise", "reinforce", "contradict", "retire")


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
    window = resolve_window(args)
    result = runtime.coach_service.data_bootstrap(
        from_date_str=window[0],
        until_date_str=window[1],
        context=args.context,
        force=args.force,
        inspect_only=args.inspect_only,
        no_pull=args.no_pull,
        force_pull=args.force_pull,
        auto=getattr(args, "auto", False),
    )
    _render_analysis_report(result, args.inspect_only)
def run_data_reflect(args: argparse.Namespace) -> None:
    """Incremental reflection over evidence accrued since the last reflect watermark."""
    window = resolve_window(args)
    result = runtime.coach_service.data_reflect(
        from_date_str=window[0],
        until_date_str=window[1],
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
def run_data_show_analysis(args: argparse.Namespace) -> None:
    """Renders the stored reconstruction for one horizon slot; never recomputes it
    (DESIGN_backward_evaluation.md §5.1, forward consumer 3)."""
    horizon, source = ("short", "data reflect") if args.short else ("long", "data bootstrap")
    cached = runtime.db.get_analysis_cache(horizon)
    if not cached or not cached.get("reconstruction"):
        notice(f"No {horizon}-horizon reconstruction stored. Run "
               f"{cmd(source)} to build one.")
        return

    window = f"{cached.get('window_start')} to {cached.get('window_end')}"
    computed = (cached.get("created_at") or "")[:10]
    print()
    print(gray(wrap_text(f"From {cmd(source)} · window {window} · computed {computed}")))
    _render_analysis_report(cached["reconstruction"], inspect_only=False)

    # Training since the window closed is absent from the picture above, so say so rather
    # than let it read as current. Both slots point at reflect: bootstrap reads the backlog
    # once, so its slot falling behind is by design (DESIGN_backward_evaluation.md §5).
    window_end = cached.get("window_end")
    newer = [
        a for a in runtime.db.get_completed_activities(start_date=window_end)
        if a["date"] > window_end
    ] if window_end else []
    if not newer:
        return

    one = len(newer) == 1
    if horizon == "short":
        remedy = f"{cmd('data reflect')} folds {'it' if one else 'them'} in."
    else:
        remedy = (
            f"Bootstrap builds it once, so it will not catch up. {cmd('data reflect')} "
            f"follows the training since. Read it with {cmd('data show-analysis --short')}."
        )
    notice(
        f"{len(newer)} {'activity' if one else 'activities'} since {window_end} "
        f"{'post-dates' if one else 'post-date'} this analysis. {remedy}",
    )


def _render_analysis_report(result: dict, inspect_only: bool) -> None:
    """Renders a bootstrap/reflect reconstruction + applied coach learning deltas."""
    print(bold(cyan("\n=== HISTORICAL WORKOUT ANALYSIS REPORT ===")))
    
    # Macrocycle Overview. The reconstruction windows keep bare dates: labelled with
    # weekdays they outrun the bot's 48-column budget, and a window an analysis was run
    # over is read as a span, not as days to train on.
    if "inferred_macrocycle" in result:
        im = result["inferred_macrocycle"]
        print("\n" + format_labeled_block(
            f"{bold('Macrocycle Focus')} "
            f"({magenta(im.get('start_date', ''))} to {magenta(im.get('end_date', ''))}):",
            im.get('overall_focus', 'N/A'), color_fn=cyan
        ))
    
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

            print(format_labeled_text(
                "  - ",
                f"{green(meso.get('name', 'Phase'))} "
                f"({cyan(meso.get('start_date', ''))} to {cyan(meso.get('end_date', ''))}) "
                f"{c_disp}"
            ))
            print(format_labeled_block(
                "    * Detected Focus:", str(meso.get('focus_detected', 'N/A'))
            ))
            print(f"    * Avg Weekly TSS: {meso.get('average_weekly_tss', 'N/A')}")

    # Physiological Insights
    if "physiological_insights" in result and result["physiological_insights"]:
        print(bold(cyan("\nPhysiological Insights:")))
        for insight in result["physiological_insights"]:
            print(format_labeled_text("  - ", str(insight)))

    # Coach learnings (incremental updates applied to learnings)
    updates = result.get("learning_updates")
    if updates:
        # "Saved" is a claim, so it is only made when some delta named an op the app
        # acts on — a response whose every delta is unreadable saved nothing (§13).
        saved_any = any(
            isinstance(u, dict) and u.get("op") in _LEARNING_OPS for u in updates
        )
        if inspect_only:
            header = "Coach Observations (NOT saved — inspect mode):"
        elif saved_any:
            header = "Coach Observations (Saved to learnings):"
        else:
            header = "Coach Observations (none saved — see below):"
        print(bold(cyan("\n" + header)))
        try:
            learnings_map = {l['id']: l for l in runtime.db.get_learnings()}
        except Exception:
            learnings_map = {}
        for u in updates:
            if not isinstance(u, dict):
                notice(f"  ! unreadable update ({type(u).__name__}) — skipped")
                continue
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
                print(format_labeled_text("  + ", f"{u.get('text', '')}{suffix}"))
            elif op == "revise":
                print(format_labeled_text(
                    f"  ~ [{u.get('id')}] ", f"{u.get('text', '')}{suffix}"
                ))
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
            else:
                # Named no op the app knows, so nothing was saved for it. Printing the
                # skip keeps the block from rendering empty under a "Saved" header
                # (DESIGN_backward_evaluation.md §13).
                notice(f"  ! unreadable update (op={op!r}) — skipped")

    print(bold(cyan("\n==========================================")))

def add_data_parser(subparsers, pull_bypass_parser, llm_debug_parser):
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
        help="Fetch Garmin activities/metrics and Google Calendar signals",
        description=(
            "Fetch activities and daily metrics directly from Garmin Connect into the "
            "local cache, advancing the sync watermark. With no range, pulls the last "
            "2 days ending today (-d 7d for a different window, or -d A..B for an "
            "explicit range). Pulls both metrics and activities unless "
            "--metrics-only/--activities-only is given. Also syncs tagged daily-signal "
            "events (alcohol, sleep, stress, …) from Google Calendar into the local cache. "
            "Past Calendar events in the pulled range are stamped with the adherence "
            "verdict unless --no-mark is given."
        )
    )
    d_pull.set_defaults(func=run_data_pull)
    add_selector_args(d_pull, direction="backward", default="2d", span_days=2)
    d_pull.add_argument(
        "--no-mark", action="store_true", dest="no_mark",
        help="Skip stamping past Calendar events with the adherence verdict"
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
        parents=[pull_bypass_parser, llm_debug_parser],
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
    d_boot.set_defaults(func=run_data_bootstrap)
    # data reflect — incremental reflection over evidence since the last reflect
    d_reflect = data_subparsers.add_parser(
        "reflect",
        parents=[pull_bypass_parser, llm_debug_parser],
        help="Update coach learnings from how the athlete responded to training "
             "since the last reflect (incremental; no cycle inference)",
        description=(
            "Incremental: analyze only evidence accrued since the last reflect watermark "
            "(the day after the last reflected-through date), through the last COMPLETED "
            "week — the evidence basis counts whole weeks, so a run with nothing complete "
            "since the watermark reports nothing new and costs no LLM call. Its output is "
            "coach learnings and physiological insights; unlike 'data bootstrap' it does "
            "not reverse-engineer macro/mesocycles, which a few weeks cannot support. A "
            "date filter overrides both the watermark and the completed-week end. Because "
            "overlapping history is never re-counted, repeated runs no longer ratchet "
            "confidence to 'established'. Run 'data bootstrap' first to establish a "
            "baseline. --inspect-only renders without writing; --force bypasses the "
            "per-window cache."
        )
    )
    d_reflect.set_defaults(func=run_data_reflect)
    for d_an in (d_boot, d_reflect):
        # direction="none": an unbounded side stays unbounded, so the service keeps
        # auto-detecting the window it was never told (a bare span still looks back).
        add_selector_args(d_an, direction="none")
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
    d_btss.set_defaults(func=run_data_backfill_tss)
    add_selector_args(d_btss, direction="none")
    d_btss.add_argument(
        "-v", "--verbose", action="store_true",
        help="List activities with low HR-zone coverage that need an RPE"
    )

    # data show-metrics
    d_sm = data_subparsers.add_parser(
        "show-metrics", aliases=["sm"],
        parents=[pull_bypass_parser],
        help="Show athlete metrics over a date range",
        description=(
            "Show cached daily athlete metrics (RHR, HRV, sleep, stress) over a date "
            "range. With no date filter, looks back 7 days ending today; -a/--all "
            "shows every cached row. Freshens recent data from Garmin first unless "
            "--no-pull or --all is given. Use --csv for machine-readable output."
        )
    )
    d_sm.set_defaults(func=run_data_show_metrics)
    add_selector_args(d_sm, meso=True, macro=True, goal=True, direction="backward",
                      default="7d")
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
        parents=[pull_bypass_parser],
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
    d_sa.set_defaults(func=run_data_show_activities)
    add_selector_args(d_sa, meso=True, macro=True, goal=True, sport=True,
                      direction="backward", default="7d")
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

    # data show-analysis — the stored reconstruction, rendered without recomputing it
    d_san = data_subparsers.add_parser(
        "show-analysis", aliases=["san"],
        help="Show the macro/mesocycle reconstruction stored by the last "
             "'data bootstrap' (read-only: no LLM call, no Garmin pull)",
        description=(
            "Print the reconstruction cached by the last 'data bootstrap': the inferred "
            "macro focus, the mesocycle blocks 'tm progress' draws as '~' bands, and the "
            "physiological insights. Read-only in the strict sense — it renders what is "
            "stored and never calls the LLM, unlike 'data bootstrap --inspect-only' which "
            "recomputes as soon as the evidence has moved. --short shows the last "
            "'data reflect' instead — its recent-response read: summary and physiological "
            "insights, no cycles, because a few weeks cannot support a periodization "
            "claim. Only the latest of each is kept, so this is the current picture, not "
            "a history."
        )
    )
    d_san.set_defaults(func=run_data_show_analysis)
    d_san.add_argument(
        "--short", action="store_true",
        help="Show the short-horizon reconstruction from the last 'data reflect' instead "
             "of the bootstrap one"
    )

    # data wipe
    d_wipe = data_subparsers.add_parser(
        "wipe", advanced=True,
        help="Wipe locally cached Garmin data and/or daily signals from the database",
        description=(
            "Delete locally cached data. With no scope flag, wipes everything (Garmin "
            "metrics, baselines, activities, and ingested daily signals) and "
            "resets the sync watermarks. --garmin or --calendar narrow the scope; -d "
            "restricts it to a date window (the next 'data pull' re-fetches what was "
            "removed)."
        ),
    )
    d_wipe.set_defaults(func=run_data_wipe)
    d_wipe.add_argument(
        "--garmin", action="store_true",
        help="Wipe only Garmin evidence (metrics, baselines, activities, analysis cache)"
    )
    d_wipe.add_argument(
        "--calendar", "--signals", action="store_true", dest="calendar",
        help="Wipe only ingested daily signals and reset the Calendar sync token"
    )
    add_selector_args(d_wipe, direction="none")
    d_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    

    return data_parser
