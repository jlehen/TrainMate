import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from trainmate.types import Workout, CompletedActivity
from trainmate.garmin import activity_load, rpe_divergence
from trainmate.sports import canonical_sport
from trainmate.modification_state import modification_status


def _adapt_recency_tag(workout: Workout, eval_date: Optional[str]) -> str:
    """Tags an already-adapted session with how recently/often it was eased.

    Returns "" unless the session reads as `adapted` (see trainmate.modification_state)
    AND carries an `adapted_at` stamp. The tag feeds the adaptation prompt a recency
    signal so a re-run holds the already-eased form instead of compounding the cut on
    still-lagging recovery metrics."""
    if modification_status(workout) != "adapted":
        return ""
    adapted_at = workout.get("adapted_at")
    if not adapted_at:
        return ""
    count = workout.get("adaptation_count") or 1
    times = "once" if count == 1 else f"{count}x"
    when = ""
    if eval_date:
        try:
            d0 = datetime.strptime(adapted_at[:10], "%Y-%m-%d").date()
            d1 = datetime.strptime(eval_date, "%Y-%m-%d").date()
            days = (d1 - d0).days
            if days <= 0:
                when = ", most recently TODAY"
            elif days == 1:
                when = ", most recently YESTERDAY"
            else:
                when = f", most recently {days} days ago"
        except (ValueError, TypeError):
            when = ""
    return (
        f" [ALREADY EASED by a prior adaptation ({times}{when}) — current form is the "
        f"reduced plan, not the original; do not compound]"
    )


# The printed CTL | ATL | TSB triple won't subtract to the shown TSB, because TSB is
# CTL(yesterday) - ATL(yesterday) (training_load.txt §2) while CTL/ATL are today's. This
# lag is correct (matching TrainingPeaks) but reads as an arithmetic error, so a one-line
# footnote states it wherever the triple is surfaced (per-day block, summary, tm status).
PMC_TSB_LAG_NOTE = (
    "(Note: TSB is CTL(yesterday) - ATL(yesterday), so it won't equal the shown "
    "same-day CTL - ATL; this ~1-day lag is expected, not an error.)"
)


def format_metrics_history(
    metrics: List[Dict[str, Any]], warmup_cutoff: Optional[str] = None
) -> str:
    """Formats metrics cache history to a readable block for LLM prompts.

    One None-omission convention for the whole line: every field (RHR, HRV, Sleep,
    Stress, ACWR, and the PMC triple CTL/ATL/TSB) is emitted only when present, so a
    NULL value is silently dropped rather than crashing a `:.2f`/rendering `Nonebpm`
    (DESIGN_pmc_fitness_fatigue.md §5.1). The PMC triple is additionally suppressed for
    rows dated before `warmup_cutoff` (garmin.pmc_warmup_cutoff), where the EWMAs are
    still leading-edge warm-up artifacts (§3.3a). The TSB-lag footnote is appended once
    when any row showed PMC."""
    metrics_lines = []
    shown_pmc = False
    for m in metrics:
        fields = []
        if m.get('rhr') is not None:
            fields.append(f"RHR={m['rhr']}bpm")
        if m.get('hrv') is not None:
            fields.append(f"HRV={m['hrv']}ms")
        if m.get('sleep_score') is not None:
            fields.append(f"Sleep={m['sleep_score']}")
        if m.get('stress') is not None:
            fields.append(f"Stress={m['stress']}")
        if m.get('acwr') is not None:
            fields.append(f"ACWR={m['acwr']:.2f}")
        in_warmup = bool(warmup_cutoff and m['date'] < warmup_cutoff)
        if not in_warmup:
            pmc_fields = [
                f"{label}={m[key]:.1f}"
                for label, key in (("CTL", "ctl"), ("ATL", "atl"), ("TSB", "tsb"))
                if m.get(key) is not None
            ]
            if pmc_fields:
                fields.extend(pmc_fields)
                shown_pmc = True   # any of the triple warrants the lag footnote
        # An all-null row (pulled, but Garmin had nothing) still gets a line — marked
        # explicitly rather than left dangling as "- 2026-07-02: ".
        metrics_lines.append(
            f"- {m['date']}: " + (", ".join(fields) if fields else "(no data)")
        )
    out = "\n".join(metrics_lines)
    if shown_pmc:
        out += "\n" + PMC_TSB_LAG_NOTE
    return out


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


def format_planned_workouts_detailed(
    planned_workouts: List[Workout],
    completed_keys: Optional[Set[Tuple[str, str]]] = None,
    eval_date: Optional[str] = None,
) -> str:
    """Like format_planned_workouts but includes each session's full description.

    Used by the adaptation prompt so the model can preserve interval structure,
    heart-rate zones, and rest/recovery durations it is not deliberately changing.
    The one-line summary alone omits these details, so adaptations otherwise lose
    them (the model reconstructs the session from title + duration + RPE + TSS).

    Sessions the athlete scheduled themselves (source == 'manual') are tagged
    "[athlete-added]" so the adaptation can treat them as deliberate intent.

    `completed_keys` is the set of `(date, canonical_sport)` pairs that already have a
    matching completed activity (computed by adherence analysis). Those sessions are
    tagged "[COMPLETED — locked history, not adaptable]" so the model has the
    authoritative done/locked signal instead of re-pairing the plan against the
    completed-activity list itself.

    Sessions a prior `workout adapt` already eased are tagged "[ALREADY EASED …]" with
    how recently and how many times (relative to `eval_date`), so a re-run does not stack
    a second reduction on a session whose current form is already the reduced plan.
    """
    completed_keys = completed_keys or set()
    blocks = []
    for w in planned_workouts:
        header = (
            f"- {w['date']} ({w['sport_type'].upper()}): {w['title']} | "
            f"Expected duration: {w.get('duration_minutes')}m, "
            f"RPE: {w.get('rpe')}, TSS: {w.get('tss')}"
        )
        if (w['date'], canonical_sport(w['sport_type'])) in completed_keys:
            header += " [COMPLETED — locked history, not adaptable]"
        if w.get('source') == 'manual':
            header += " [athlete-added]"
        header += _adapt_recency_tag(w, eval_date)
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
