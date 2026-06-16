import os
from typing import Any, Dict, List, Optional
from trainmate.types import Workout, CompletedActivity
from trainmate.garmin import activity_load, rpe_divergence


def format_metrics_history(metrics: List[Dict[str, Any]]) -> str:
    """Formats metrics cache history to a readable block for LLM prompts."""
    metrics_lines = []
    for m in metrics:
        metrics_lines.append(
            f"- {m['date']}: RHR={m['rhr']}bpm, HRV={m['hrv']}ms, "
            f"Sleep={m['sleep_score']}, Stress={m['stress']}, ACWR={m['acwr']:.2f}"
        )
    return "\n".join(metrics_lines)


def format_daily_context(daily_context: List[Dict[str, Any]]) -> str:
    """Formats externally-logged daily context signals (alcohol, poor sleep, etc.)
    to a readable block for LLM prompts. One line per logged signal-day."""
    lines = []
    for c in daily_context:
        line = f"- {c['date']}: {c['metric']}"
        if c.get('value') is not None:
            line += f"={c['value']:g}"
        if c.get('text'):
            line += f' — "{c["text"]}"'
        lines.append(line)
    return "\n".join(lines)


def format_completed_activities(completed_activities: List[CompletedActivity]) -> str:
    """Formats Garmin completed activities to a readable block for LLM prompts."""
    completed_list = []
    for act in completed_activities:
        line = (
            f"- {act['date']} ({act['activity_type'].upper()}): "
            f"'{act['activity_name']}' | "
            f"Duration: {act['duration_sec']/60:.0f}m, Avg HR: {act['avg_hr']}, "
            f"Load: {activity_load(act):.1f}"
        )
        divergence = rpe_divergence(act)
        if divergence is not None:
            line += (
                f" (load taken from RPE {act.get('rpe')}: ~{divergence:.1f}x the "
                f"measured TSS {act.get('tss'):.0f} — meters under-counted, e.g. "
                "resistance load or hidden fatigue from heat, sleep, muscular damage)"
            )
        extras = []
        if act.get('bike_avg_watts') is not None:
            extras.append(f"Avg Power: {act['bike_avg_watts']}W")
        zone_parts = [
            f"Z{i}={act[f'zone{i}_sec'] // 60}m"
            for i in range(1, 6)
            if act.get(f'zone{i}_sec') is not None
        ]
        if zone_parts:
            extras.append(f"HR Zones: {', '.join(zone_parts)}")
        power_zone_parts = [
            f"PZ{i}={act[f'power_zone{i}_sec'] // 60}m"
            for i in range(1, 8)
            if act.get(f'power_zone{i}_sec') is not None
        ]
        if power_zone_parts:
            extras.append(f"Power Zones: {', '.join(power_zone_parts)}")
        if extras:
            line += " | " + ", ".join(extras)
        completed_list.append(line)
    return "\n".join(completed_list)


def format_planned_workouts(planned_workouts: List[Workout]) -> str:
    """Formats planned workouts to a readable block for LLM prompts."""
    planned_list = []
    for w in planned_workouts:
        line = (
            f"- {w['date']} ({w['sport_type'].upper()}): {w['title']} | "
            f"Expected duration: {w.get('duration_minutes')}m, "
            f"RPE: {w.get('rpe')}, TSS: {w.get('tss')}"
        )
        mod_reason = w.get('modification_reason')
        if mod_reason:
            line += f" — {mod_reason}"
        planned_list.append(line)
    return "\n".join(planned_list)


def format_planned_workouts_detailed(planned_workouts: List[Workout]) -> str:
    """Like format_planned_workouts but includes each session's full description.

    Used by the adaptation prompt so the model can preserve interval structure,
    heart-rate zones, and rest/recovery durations it is not deliberately changing.
    The one-line summary alone omits these details, so adaptations otherwise lose
    them (the model reconstructs the session from title + duration + RPE + TSS).

    Sessions the athlete scheduled themselves (source == 'manual') are tagged
    "[athlete-added]" so the adaptation can treat them as deliberate intent.
    """
    blocks = []
    for w in planned_workouts:
        header = (
            f"- {w['date']} ({w['sport_type'].upper()}): {w['title']} | "
            f"Expected duration: {w.get('duration_minutes')}m, "
            f"RPE: {w.get('rpe')}, TSS: {w.get('tss')}"
        )
        if w.get('source') == 'manual':
            header += " [athlete-added]"
        mod_reason = w.get('modification_reason')
        if mod_reason:
            header += f" — {mod_reason}"
        desc = (w.get('description') or '').strip()
        if desc:
            indented = "\n".join("    " + ln for ln in desc.splitlines())
            blocks.append(f"{header}\n  Full description:\n{indented}")
        else:
            blocks.append(header)
    return "\n\n".join(blocks)


def format_removed_workouts(removed_workouts: List[Workout]) -> str:
    """Formats workouts the athlete deliberately removed, for the adaptation prompt."""
    lines = []
    for w in removed_workouts:
        line = (
            f"- {w['date']} ({w['sport_type'].upper()}): {w['title']} | "
            f"Expected duration: {w.get('duration_minutes')}m, "
            f"RPE: {w.get('rpe')}, TSS: {w.get('tss')}"
        )
        reason = w.get('removed_reason')
        if reason:
            line += f" — reason: {reason}"
        lines.append(line)
    return "\n".join(lines)


def format_baseline(baseline: Optional[Dict[str, Any]]) -> str:
    """Formats 28-day baseline reference to a readable block for LLM prompts."""
    if not baseline:
        return "No baseline data available."
    return (
        f"Resting HR: Mean = {baseline['rhr_baseline_mean']:.1f}, "
        f"StdDev = {baseline['rhr_baseline_std']:.2f}\n"
        f"HRV: Mean = {baseline['hrv_baseline_mean']:.1f}, "
        f"StdDev = {baseline['hrv_baseline_std']:.2f}\n"
        f"Sleep Score: Mean = {baseline['sleep_baseline_mean']:.1f}, "
        f"StdDev = {baseline['sleep_baseline_std']:.2f}"
    )


def _load_science_guidelines(app_science_dir: str, science_dir: str) -> str:
    """Loads and concatenates all text files in the app and user science directories."""
    directories = [app_science_dir, science_dir]
    texts = []
    for s_dir in directories:
        if not os.path.exists(s_dir):
            continue
        for filename in sorted(os.listdir(s_dir)):
            if not filename.endswith(".txt"):
                continue
            filepath = os.path.join(s_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    texts.append(f"=== Guidelines from {filename} ===\n" + f.read())
            except Exception as e:
                print(f"Error reading science guideline {filename}: {e}")
    return "\n\n".join(texts)
