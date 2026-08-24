import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from trainmate.types import Workout, CompletedActivity
from trainmate.garmin import activity_load, load_ratio, rpe_divergence
from trainmate.util import PMC_TSB_LAG_NOTE
from trainmate.sports import canonical_sport
from trainmate import intensity


# The tag's closing clause names the risk the reading command runs, and the two commands
# run opposite ones: `adapt` may cut the session again, `generate` may write the day back
# at its original load (DESIGN_workout_revisions.md §7.1).
EASED_DO_NOT_COMPOUND = "do not compound"
EASED_DO_NOT_RESTORE = "do not silently restore it"


def _easing_recency_tag(
    workout: Workout, eval_date: Optional[str],
    closer: str = EASED_DO_NOT_COMPOUND,
) -> str:
    """Tags an already-eased session with how recently and how often it was eased.

    Gated on the derived tally, NOT on the kind of the latest change: under revisions the
    marker reflects the latest change, so an adapted-then-swapped session reads `swapped`,
    and a kind gate would silence this tag in exactly the scenario the lineage exists to
    protect (DESIGN_workout_revisions.md §7). Shared by the adaptation and generation
    prompts, which read the same recency signal against opposite risks — `closer` is the
    one clause that differs."""
    count = workout.get("adaptation_count") or 0
    if count < 1:
        return ""
    adapted_at = workout.get("adapted_at")
    if not adapted_at:
        return ""
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
        f"reduced plan, not the original; {closer}]"
    )


def format_metrics_history(
    metrics: List[Dict[str, Any]], warmup_cutoff: Optional[str] = None
) -> str:
    """Formats metrics cache history to a readable block for LLM prompts.

    One None-omission convention for the whole line: every field (RHR, HRV, Sleep,
    Stress, and the PMC triple CTL/ATL/TSB plus the ATL:CTL ratio) is emitted only when
    present, so a NULL value is silently dropped rather than crashing a `:.2f`/rendering
    `Nonebpm` (DESIGN_pmc_fitness_fatigue.md §5.1). The PMC block is additionally
    suppressed for rows dated before `warmup_cutoff` (garmin.pmc_warmup_cutoff_for),
    where the EWMAs are still leading-edge warm-up artifacts (§3.3a) — the ratio rides
    inside that gate since it divides two of them. The TSB-lag footnote is appended once
    when any row showed TSB (it explains the TSB lag; a CTL/ATL-only block has no lag
    to explain)."""
    metrics_lines = []
    shown_tsb = False
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
        in_warmup = bool(warmup_cutoff and m['date'] < warmup_cutoff)
        if not in_warmup:
            pmc_fields = [
                f"{label}={m[key]:.1f}"
                for label, key in (("CTL", "ctl"), ("ATL", "atl"), ("TSB", "tsb"))
                if m.get(key) is not None
            ]
            ratio = load_ratio(m.get('atl'), m.get('ctl'))
            if ratio is not None:
                pmc_fields.append(f"ATL:CTL={ratio:.2f}")
            if pmc_fields:
                fields.extend(pmc_fields)
                if m.get('tsb') is not None:
                    shown_tsb = True
        # An all-null row (pulled, but Garmin had nothing) still gets a line — marked
        # explicitly rather than left dangling as "- 2026-07-02: ".
        metrics_lines.append(
            f"- {m['date']}: " + (", ".join(fields) if fields else "(no data)")
        )
    out = "\n".join(metrics_lines)
    if shown_tsb:
        out += "\n" + PMC_TSB_LAG_NOTE
    return out


def format_daily_signals(daily_signals: List[Dict[str, Any]]) -> str:
    """Formats externally-logged daily signals (alcohol, poor sleep, etc.)
    to a readable block for LLM prompts. One line per logged signal-day."""
    lines = []
    for c in daily_signals:
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


def _planned_summary(
    w: Workout, eval_date: Optional[str], easing_closer: str, markers: str = "",
) -> str:
    """What both planned-workout renderings share: identity, load, why it was last
    changed, and the intensity target under it.

    `markers` are the caller's own bracketed tags, spliced between the load and the
    already-eased tag; :func:`format_planned_workouts_detailed` appends the description
    below what this returns.
    """
    line = (
        f"- {w['date']} ({w['sport_type'].upper()}): {w['title']} | "
        f"Expected duration: {w.get('duration_minutes')}m, "
        f"RPE: {w.get('rpe')}, TSS: {w.get('tss')}"
        f"{markers}"
    )
    line += _easing_recency_tag(w, eval_date, easing_closer)
    mod_reason = w.get('modification_reason')
    if mod_reason:
        line += f" — {mod_reason}"
    # The stated intensity target (DESIGN_intensity_distribution.md §9.8) — duration and
    # TSS fold intensity away, and both prompts are asked to weigh a session against the
    # block's hard/easy split (DESIGN_workout_revisions.md §7.1).
    target = intensity.format_planned_zones(w)
    if target:
        line += f"\n  {target}"
    return line


def format_planned_workouts(
    planned_workouts: List[Workout], eval_date: Optional[str] = None,
    easing_closer: str = EASED_DO_NOT_COMPOUND,
) -> str:
    """Formats planned workouts to a readable block for LLM prompts.

    `eval_date` adds the "[ALREADY EASED ...]" tag dated against it, closed by
    `easing_closer`. Used where the model is asked to weigh a session rather than rewrite
    it, so it carries the intensity target but not the description
    :func:`format_planned_workouts_detailed` adds.
    """
    return "\n".join(
        _planned_summary(w, eval_date, easing_closer) for w in planned_workouts
    )


def format_planned_workouts_detailed(
    planned_workouts: List[Workout],
    completed_keys: Optional[Set[Tuple[str, str]]] = None,
    eval_date: Optional[str] = None,
    easing_closer: str = EASED_DO_NOT_COMPOUND,
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
        markers = ""
        if (w['date'], canonical_sport(w['sport_type'])) in completed_keys:
            markers += " [COMPLETED — locked history, not adaptable]"
        if w.get('source') == 'manual':
            markers += " [athlete-added]"
        # A benchmark (fitness test) must be rescheduled intact, never softened — see the
        # adapt prompt's PROTECTING A BENCHMARK rule (DESIGN_benchmark_workouts.md §4.2).
        if w.get('benchmark_type'):
            markers += f" [BENCHMARK: {w['benchmark_type']} — reschedule intact, do not dilute]"
        header = _planned_summary(w, eval_date, easing_closer, markers)
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


_RULE = "=" * 80


def _science_block(s_dir: str, title: str, provenance: str) -> str:
    """One bannered block of quoted science documents, or "" when the directory holds none.

    The banner is the prompt's third marker (DESIGN_prompt_structure.md §3): everything
    inside it is verbatim source, so its own `#` headings are the document's and not the
    prompt's — which is why the frame never uses one.
    """
    if not os.path.exists(s_dir):
        return ""
    docs = []
    for filename in sorted(os.listdir(s_dir)):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(s_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                docs.append(f"--- {filename} ---\n" + f.read())
        except Exception as e:
            print(f"Error reading science guideline {filename}: {e}")
    if not docs:
        return ""
    return "\n".join([
        _RULE, f"START OF {title}", _RULE,
        provenance,
        "",
        "\n\n".join(docs),
        _RULE, f"END OF {title}", _RULE,
    ])


def _load_science_guidelines(app_science_dir: str, science_dir: str) -> str:
    """The sports-science reference documents, one banner per source.

    Split so the coach can tell whose material it is reading (§3): the app's own guidelines
    and the athlete's are different kinds of authority, and concatenating them under one
    banner hid that. Returns "" when neither directory has documents, so no caller emits an
    empty banner.
    """
    blocks = [
        _science_block(
            app_science_dir,
            "TRAINMATE SPORTS SCIENCE GUIDELINES",
            "TrainMate's own reference material, shipped with the app. It defines how to\n"
            "measure training and what the terms mean; the athlete's documents below decide\n"
            "what to prescribe. Where the two disagree, these yield — except for the rules\n"
            "each document marks as a floor, which never yield.",
        ),
        _science_block(
            science_dir,
            "ATHLETE-PROVIDED SPORTS SCIENCE GUIDELINES",
            "Reference material the athlete supplied themselves — the training philosophy\n"
            "and sources they want their coaching drawn from. These govern what is\n"
            "prescribed: volumes, durations, session counts, block order, taper depth.",
        ),
    ]
    return "\n\n".join(b for b in blocks if b)
