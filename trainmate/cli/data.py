import csv as csv_mod
import argparse
import sys
from datetime import datetime, timedelta
from typing import Optional
import trainmate_cli as cli
from trainmate.config import config
from trainmate.adherence import analyze_adherence, date_covered
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, magenta, gray,
    color_acwr, visible_len, pad_visible, wrap_text, format_labeled_text,
    format_labeled_block, today_str as _today_str, today_date as _today_date,
)
from trainmate.cli.common import fmt_date, ensure_recent_data


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

    try:
        cli.garmin.pull(
            start_date, end_date,
            metrics=not args.activities_only,
            activities=not args.metrics_only,
            throttle=args.sleep,
        )
    except cli.garmin.GarminAuthRequired as e:
        print(red(f"Garmin authentication required: {e}"))
        print(yellow("Run this command in an interactive terminal to complete MFA."))
    except Exception as e:
        print(red(f"Error pulling from Garmin: {e}"))


def run_data_backfill_tss(args: argparse.Namespace) -> None:
    """Recomputes stored TSS for all cached activities under the current
    zone-based hierarchy, then refreshes derived workload/ACWR."""
    changed = cli.garmin.backfill_tss(
        start_date=args.from_date,
        end_date=args.until_date,
        verbose=getattr(args, "verbose", False),
    )
    print(green(f"Backfill complete. {changed} activities updated."))


def run_data_wipe(args: argparse.Namespace) -> None:
    """Wipes all metrics, baselines, and activities after confirmation."""
    if not args.yes:
        try:
            msg = (
                "Are you sure you want to wipe all metrics, baselines, "
                "and completed activities? [y/N]: "
            )
            confirm = input(msg).strip().lower()
        except EOFError:
            confirm = 'n'
        if confirm not in ('y', 'yes'):
            print("Wipe cancelled.")
            return

    cli.db.wipe_metrics()
    print(green("All metrics, baselines, and completed activities wiped successfully."))


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
            cli.garmin.ensure_data(start_date, end_date)
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

    print(bold(
        f"{'Date':<12} | {'HRV':<8} | {'HRV Base':<9} | {'RHR':<8} | {'RHR Base':<8} | "
        f"{'Sleep':<8} | {'Sleep Base':<10} | {'Stress':<6} | {'ACWR':<5} | "
        f"{'Acute':<7} | {'Chronic':<7}"
    ))
    print(gray("-" * 107))

    for m in metrics_history:
        base = cli.db.get_baseline(m['date'])

        hrv_val = m['hrv']
        rhr_val = m['rhr']
        sleep_val = m['sleep_score']
        stress_val = m['stress']
        acwr_val = m['acwr']
        acute_val = m['acute_workload']
        chronic_val = m['chronic_workload']

        hrv_str = str(hrv_val) if hrv_val is not None else "N/A"
        rhr_str = str(rhr_val) if rhr_val is not None else "N/A"
        sleep_str = str(sleep_val) if sleep_val is not None else "N/A"
        stress_str = str(stress_val) if stress_val is not None else "N/A"
        acwr_str = color_acwr(acwr_val) if acwr_val is not None else "N/A"
        acute_str = f"{acute_val:.1f}" if acute_val is not None else "N/A"
        chronic_str = f"{chronic_val:.1f}" if chronic_val is not None else "N/A"

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

        date_col = pad_visible(m['date'], 12)
        hrv_col = pad_visible(hrv_str, 8)
        hrv_base_col = pad_visible(hrv_base_str, 9)
        rhr_col = pad_visible(rhr_str, 8)
        rhr_base_col = pad_visible(rhr_base_str, 8)
        sleep_col = pad_visible(sleep_str, 8)
        sleep_base_col = pad_visible(sleep_base_str, 10)
        stress_col = pad_visible(stress_str, 6)
        acwr_col = pad_visible(acwr_str, 5)
        acute_col = pad_visible(acute_str, 7)
        chronic_col = pad_visible(chronic_str, 7)

        print(
            f"{date_col} | {hrv_col} | {hrv_base_col} | {rhr_col} | {rhr_base_col} | "
            f"{sleep_col} | {sleep_base_col} | {stress_col} | {acwr_col} | "
            f"{acute_col} | {chronic_col}"
        )
    print(gray("((v) suppressed/poor, (^) elevated compared to baseline)\n"))


def _show_metrics_csv(metrics_history: list) -> None:
    """Output metrics as CSV."""
    writer = csv_mod.writer(sys.stdout)
    writer.writerow([
        "date", "hrv", "hrv_baseline", "rhr", "rhr_baseline",
        "sleep_score", "sleep_baseline", "stress", "acwr",
        "acute_workload", "chronic_workload",
    ])
    for m in metrics_history:
        base = cli.db.get_baseline(m['date'])
        hrv_base = None
        rhr_base = None
        sleep_base = None
        if base:
            hrv_base = base.get('hrv_baseline_mean')
            rhr_base = base.get('rhr_baseline_mean')
            sleep_base = base.get('sleep_baseline_mean')
        writer.writerow([
            m['date'], m['hrv'], hrv_base, m['rhr'], rhr_base,
            m['sleep_score'], sleep_base, m['stress'], m['acwr'],
            m['acute_workload'], m['chronic_workload'],
        ])


def run_data_show_activities(args: argparse.Namespace) -> None:
    """Displays completed activities over the resolved date range."""
    start_date, end_date = _resolve_historical_date_range(args, default_days=7)

    if not getattr(args, 'no_pull', False) and not getattr(args, 'all', False):
        try:
            cli.garmin.ensure_data(start_date, end_date)
        except Exception as e:
            print(yellow(f"Warning: Could not ensure recent data: {e}"))

    activities = cli.db.get_completed_activities(
        start_date=start_date, end_date=end_date
    )

    if getattr(args, 'sport_type', None) is not None:
        activities = [
            act for act in activities
            if act['activity_type'].lower() == args.sport_type.lower()
        ]

    if getattr(args, 'csv', False):
        _show_activities_csv(activities)
        return


    range_str = f"{start_date} to {end_date}" if start_date and end_date else "All Time"
    print(bold(cyan(f"\n=== COMPLETED ACTIVITIES ({range_str}) ===")))
    if not activities:
        print("No completed activities found in this range.")
        return

    print(bold(
        f"{'Date':<12} | {'Time':<8} | {'Type':<20} | {'Name':<25} | "
        f"{'Duration':<8} | {'Distance':<9} | {'Elev':<6} | {'Avg HR':<6} | "
        f"{'Max HR':<6} | {'Avg Watts':<9} | {'RPE':<4} | {'TSS':<6}"
    ))
    print(gray("-" * 131))

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

        tss_val = act.get('tss')
        tss_str = f"{tss_val:.1f}" if tss_val is not None else "0.0"

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

        date_col = pad_visible(act['date'], 12)
        time_col = pad_visible(time_str, 8)
        type_col = pad_visible(act['activity_type'].upper(), 20)
        name_col = pad_visible(name_str, 25)
        dur_col = pad_visible(dur_str, 8)
        dist_col = pad_visible(dist_str, 9)
        elev_col = pad_visible(elev_str, 6)
        avg_hr_col = pad_visible(avg_hr_str, 6)
        max_hr_col = pad_visible(max_hr_str, 6)
        watts_col = pad_visible(watts_str, 9)
        rpe_col = pad_visible(rpe_str, 4)
        tss_col = pad_visible(tss_str, 6)

        print(
            f"{date_col} | {time_col} | {type_col} | {name_col} | "
            f"{dur_col} | {dist_col} | {elev_col} | {avg_hr_col} | "
            f"{max_hr_col} | {watts_col} | {rpe_col} | {tss_col}"
        )

    # Summary footer
    total_count = len(activities)
    total_duration_sec = sum(act.get('duration_sec') or 0.0 for act in activities)
    total_distance_km = sum(act.get('distance_km') or 0.0 for act in activities)
    total_elevation_m = sum(act.get('elevation_gain_m') or 0.0 for act in activities)
    total_tss = sum(act.get('tss') or 0.0 for act in activities)

    tot_h = int(total_duration_sec // 3600)
    tot_m = int((total_duration_sec % 3600) // 60)
    tot_dur_str = f"{tot_h}h {tot_m}m" if tot_h > 0 else f"{tot_m}m"

    print(gray("-" * 131))
    print(bold(
        f"Summary: {total_count} activities | Duration: {tot_dur_str} | "
        f"Distance: {total_distance_km:.1f} km | Elevation: {total_elevation_m:.0f} m | "
        f"TSS: {total_tss:.1f}"
    ))


def _show_activities_csv(activities: list) -> None:
    """Output activities as CSV."""
    writer = csv_mod.writer(sys.stdout)
    writer.writerow([
        "date", "start_time", "activity_type", "activity_name",
        "duration_sec", "distance_km", "elevation_gain_m",
        "avg_hr", "max_hr", "bike_avg_watts", "rpe", "tss",
    ])
    for act in activities:
        writer.writerow([
            act.get('date'), act.get('start_time'),
            act.get('activity_type'), act.get('activity_name'),
            act.get('duration_sec'), act.get('distance_km'),
            act.get('elevation_gain_m'), act.get('avg_hr'),
            act.get('max_hr'), act.get('bike_avg_watts'),
            act.get('rpe'), act.get('tss'),
        ])


def run_data_bootstrap(args: argparse.Namespace) -> None:
    """Cold-start reconstruction over the full training backlog (seeds learnings,
    establishes the reflect watermark)."""
    try:
        result = cli.coach_service.bootstrap_workouts(
            from_date_str=args.from_date,
            until_date_str=args.until_date,
            days=args.days,
            weeks=args.weeks,
            context=args.context,
            force=args.force,
            inspect_only=args.inspect_only,
            no_pull=args.no_pull,
            auto=getattr(args, "auto", False),
        )
        _render_analysis_report(result, args.inspect_only)
    except Exception as e:
        print(red(f"Error running training history bootstrap: {e}"))


def run_data_reflect(args: argparse.Namespace) -> None:
    """Incremental reflection over evidence accrued since the last reflect watermark."""
    try:
        result = cli.coach_service.reflect_workouts(
            from_date_str=args.from_date,
            until_date_str=args.until_date,
            days=args.days,
            weeks=args.weeks,
            context=args.context,
            force=args.force,
            inspect_only=args.inspect_only,
            no_pull=args.no_pull,
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
