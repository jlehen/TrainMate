import os
import json
import hashlib
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.db import db
from trainmate.openrouter import openrouter_client
from trainmate.google_calendar import calendar_syncer
from trainmate.types import Objective, LifeEvent, Workout, CompletedActivity
from trainmate.adherence import analyze_adherence
from trainmate.garmin import activity_load, rpe_divergence
from trainmate.util import today_str as _today_str, today_date as _today_date


# Shared JSON-output instruction for incrementally updating coach learnings. The LLM
# emits only deltas; the app owns the merge so unchanged observations are never lost.
LEARNING_UPDATES_FIELD = (
    '  "learning_updates": [\n'
    "    // Optional. Incremental updates to athlete observations; each item is one of:\n"
    '    //   {"op": "add", "text": "New observation.", "sports": "running", "confidence": "tentative"},\n'
    '    //   {"op": "revise", "id": 3, "text": "Reworded observation #3.", "confidence": "moderate"},\n'
    '    //   {"op": "reinforce", "id": 4, "confidence": "established"},\n'
    '    //   {"op": "retire", "id": 5}\n'
    '    // "sports": comma-separated sport(s) the observation applies to (e.g. "running,road_biking"),\n'
    '    //   or "general" if not sport-specific. Defaults to "general".\n'
    '    // "confidence": how well-established the observation is — "tentative" | "moderate" |\n'
    '    //   "established". Defaults to "tentative". Raise it as repeated evidence accumulates.\n'
    "    // Use \"reinforce\" when you still see evidence for an existing observation but its\n"
    "    //   wording needs no change — this keeps it fresh (unreinforced observations fade over time).\n"
    "    // Existing observations persist automatically; do NOT repeat unchanged ones.\n"
    "    // Reference existing observations by the [id] shown under COACH LEARNINGS.\n"
)


def format_metrics_history(metrics: List[Dict[str, Any]]) -> str:
    """Formats metrics cache history to a readable block for LLM prompts."""
    metrics_lines = []
    for m in metrics:
        metrics_lines.append(
            f"- {m['date']}: RHR={m['rhr']}bpm, HRV={m['hrv']}ms, "
            f"Sleep={m['sleep_score']}, Stress={m['stress']}, ACWR={m['acwr']:.2f}"
        )
    return "\n".join(metrics_lines)


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
                f" (RPE {act.get('rpe')} implies ~{divergence:.1f}x the measured "
                "load: possible hidden fatigue — heat, sleep, muscular damage)"
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
        planned_list.append(
            f"- {w['date']} ({w['sport_type'].upper()}): {w['title']} | "
            f"Expected duration: {w.get('duration_minutes')}m, "
            f"RPE: {w.get('rpe')}, TSS: {w.get('tss')}"
        )
    return "\n".join(planned_list)


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


class CoachEngine:
    """Pure business logic coach that builds prompts, computes hashes, and makes LLM calls."""

    def _format_athlete_profile(self, profile: Optional[Dict[str, Any]]) -> str:
        """Formats the athlete's user profile into a readable prompt segment."""
        if not profile:
            return "No athlete profile configured."

        lines = []
        if "name" in profile:
            lines.append(f"- Name: {profile['name']}")
        if "birth_year" in profile:
            current_year = _today_date().year
            age = current_year - profile['birth_year']
            lines.append(f"- Birth Year: {profile['birth_year']} (Age: {age})")
        if "max_hr" in profile:
            lines.append(f"- Max Heart Rate: {profile['max_hr']} bpm")
        if "lthr" in profile:
            lines.append(f"- Lactate Threshold HR (LTHR): {profile['lthr']} bpm")
        if "ftp" in profile:
            lines.append(f"- Functional Threshold Power (FTP): {profile['ftp']} W")
        if "weekly_target_hours" in profile:
            lines.append(f"- Weekly Target Hours: {profile['weekly_target_hours']} hours")
        if "sport_preferences" in profile:
            lines.append(f"- Sport Preferences: {', '.join(profile['sport_preferences'])}")

        chronic_injuries = profile.get("chronic_injuries")
        if chronic_injuries:
            if isinstance(chronic_injuries, list):
                lines.append(f"- Chronic Injuries: {', '.join(chronic_injuries)}")
            else:
                lines.append(f"- Chronic Injuries: {chronic_injuries}")

        preferences = profile.get("preferences")
        if preferences:
            if isinstance(preferences, list):
                lines.append(f"- Preferences / Static Constraints: {', '.join(preferences)}")
            else:
                lines.append(f"- Preferences / Static Constraints: {preferences}")

        general_equipment = profile.get("equipment")
        if general_equipment:
            if isinstance(general_equipment, list):
                lines.append(
                    f"- General Equipment (always available): {', '.join(general_equipment)}"
                )
            else:
                lines.append(f"- General Equipment (always available): {general_equipment}")

        weekly_schedule = profile.get("weekly_schedule")
        if weekly_schedule:
            lines.append("- Weekly Availability & Equipment:")
            days_order = [
                "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
            ]
            for day in days_order:
                day_key = next((k for k in weekly_schedule if k.lower() == day.lower()), None)
                if not day_key:
                    lines.append(f"  * {day}: No availability configured")
                    continue
                day_data = weekly_schedule[day_key]
                if isinstance(day_data, dict):
                    hours = day_data.get("total_available_hours", 0.0)
                    max_sessions = day_data.get("max_sessions", 1)
                    cert = day_data.get("certainty_percent", 100)
                    equip = day_data.get("equipment", [])
                    equip_str = f" (Equipment: {', '.join(equip)})" if equip else ""
                    lines.append(
                        f"  * {day}: {hours} hours | "
                        f"Max sessions: {max_sessions} | "
                        f"Certainty: {cert}%{equip_str}"
                    )
                else:
                    lines.append(f"  * {day}: {day_data} hours")

        return "\n".join(lines)

    def _build_system_prompt(
        self, objectives: List[Objective], lifeevents: List[LifeEvent],
        guidelines: str, strategy: str, meso_text: str, learnings: str,
        profile: Optional[Dict[str, Any]], custom_task: str = ""
    ) -> str:
        """Constructs the system prompt with sports science guidelines and athlete details."""
        obj_text = ""
        for o in objectives:
            details = o.get('description', '')
            obj_text += (
                f"- Goal: {o['title']} | Date: {o['target_date']} | "
                f"Sport: {o['sport_type']} | Details: {details}\n"
            )

        c_text = ""
        for c in lifeevents:
            impact = c.get('impact_description', '')
            c_text += (
                f"- Life Event: {c['title']} | Start: {c['start_date']} | "
                f"End: {c['end_date']} | Type: {c['event_type']} | Impact: {impact}\n"
            )

        athlete_profile = self._format_athlete_profile(profile)
        system_prompt = f"""You are TrainMate Coach, an advanced AI sports science training coach.
You design and adapt personalized training plans for endurance athletes using sports science
principles.

COACHING ROLE AND OBJECTIVES:
1. Design periodized training plans (macro, meso, micro cycles) leading up to the target goals.
2. Focus scheduling on the NEXT CHRONOLOGICAL GOAL only. If there are multiple goals, identify
   synergies between them (e.g. general base or strength building phases).
3. Dynamically adjust training plans based on recent Garmin metrics (Resting HR, HRV, Sleep,
   ACWR) to optimize recovery and prevent injury.
4. Shift or scale training volume and intensity around life events (business trip, vacation,
   parties) to manage fatigue.
5. Adhere to the day-by-day weekly availability schedule and day-dependent equipment access
   (e.g., do not schedule gym workouts on home-only days; do not schedule workouts on rest days;
   do not exceed daily availability or max sessions). Respect certainty percentages (higher
   values indicate more rigid constraints; lower values allow flexibility).

================================================================================
START OF SPORTS SCIENCE GUIDELINES
================================================================================
{guidelines}
================================================================================
END OF SPORTS SCIENCE GUIDELINES
================================================================================

COACH LEARNINGS & ACTIVE PERIODIZATION STRATEGY:
- Established Training Strategy for the current macro-cycle:
{strategy}
- Mesocycles making up the macro-cycle:
{meso_text}
- Athlete-Specific Observations (reference by [id] when revising or retiring):
{learnings}

ATHLETE PROFILE & PREFERENCES:
{athlete_profile}

ACTIVE ATHLETE GOALS (CHRONOLOGICAL):
{obj_text if obj_text else "No active goals."}

UPCOMING LIFE EVENTS:
{c_text if c_text else "No upcoming life events."}

{custom_task}
"""
        return system_prompt

    def _get_goals_hash(self, objectives: List[Objective]) -> str:
        """Computes a hash representation of objectives list to check for updates."""
        cleaned = []
        for o in objectives:
            cleaned.append({
                'id': o.get('id'),
                'title': o.get('title'),
                'target_date': o.get('target_date'),
                'sport_type': o.get('sport_type'),
                'description': o.get('description'),
                'priority': o.get('priority'),
                'status': o.get('status')
            })
        cleaned.sort(key=lambda x: (str(x['target_date']), x['id'] or 0))
        serialized = json.dumps(cleaned, sort_keys=True)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _get_lifeevents_hash(self, lifeevents: List[LifeEvent]) -> str:
        """Computes a hash representation of life events list to check for updates."""
        cleaned = []
        for c in lifeevents:
            cleaned.append({
                'id': c.get('id'),
                'title': c.get('title'),
                'start_date': c.get('start_date'),
                'end_date': c.get('end_date'),
                'event_type': c.get('event_type'),
                'impact_description': c.get('impact_description')
            })
        cleaned.sort(key=lambda x: (str(x['start_date']), x['id'] or 0))
        serialized = json.dumps(cleaned, sort_keys=True)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _get_config_hash(self) -> str:
        """Computes a hash representation of the relevant user config to check for updates."""
        data_to_hash = {
            'user_profile': config.user_profile,
            'metrics_lookback_days': config.metrics_lookback_days
        }
        serialized = json.dumps(data_to_hash, sort_keys=True)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _get_evidence_fingerprint(
        self, completed_activities: List[CompletedActivity],
        metrics: List[Dict[str, Any]], window_start: str, window_end: str
    ) -> str:
        """Fingerprints the *evidence* a backward evaluation reconstructs from — the
        completed activities + daily metrics within a window — so a re-run over unchanged
        data can be detected (see DESIGN_backward_evaluation.md §5, §8).

        We hash the load-bearing fields (not just activity ids) so that a re-pull which
        *corrects* a value also shifts the fingerprint. Hashing the concrete activity-id
        set rather than only the date range narrows the overlapping/shrinking-window edge
        (§7).

        DELIBERATE OMISSION (§11): the prompt text and science/*.txt files are NOT hashed.
        Editing a prompt or guideline will therefore reuse a stale reconstruction until the
        underlying data changes; `--force` is the manual escape hatch. This is a chosen
        trade-off, not an oversight — revisit if prompt iteration becomes common.
        """
        act_digest = sorted(
            {
                (
                    a.get('activity_id'), a.get('date'), a.get('activity_type'),
                    a.get('duration_sec'), a.get('tss'), a.get('rpe'),
                    a.get('zone1_sec'), a.get('zone2_sec'), a.get('zone3_sec'),
                    a.get('zone4_sec'), a.get('zone5_sec'),
                )
                for a in completed_activities
            }
        )
        met_digest = sorted(
            (m.get('date'), m.get('rhr'), m.get('hrv'), m.get('sleep_score'),
             m.get('acwr'))
            for m in metrics
        )
        serialized = json.dumps(
            {'window': [window_start, window_end],
             'activities': act_digest, 'metrics': met_digest},
            sort_keys=True
        )
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _generate_macrocycle_strategy(
        self, next_goal: Objective, objectives: List[Objective],
        lifeevents: List[LifeEvent], today_str: str, guidelines: str,
        profile: Optional[Dict[str, Any]], previous_strategy_text: Optional[str] = None,
        plan_start_str: Optional[str] = None, athlete_feedback: Optional[str] = None,
        history_summary: Optional[str] = None, prior_training_text: Optional[str] = None
    ) -> Dict[str, Any]:
        """Queries LLM to determine the overall macrocycle strategy and mesocycle blocks."""
        plan_start = plan_start_str or today_str
        custom_task = f"""
TASK:
Determine the overall periodization strategy (macrocycle) from {plan_start} until the target
goal ({next_goal['target_date']}).
Divide this timeframe into contiguous, sequential mesocycles (determining the duration of each
block based on the periodization style guidelines provided in the science file). When planning
mesocycles, it is acceptable to shorten/extend a block by a few days to align transition or
recovery periods with upcoming life events, and we should also try to align transition
boundaries with long life events (e.g. aligning a deload week or phase change with a vacation).
Make sure there are no gaps between the end date of one mesocycle and the start date of the next.
The first mesocycle must start on the start date ({plan_start}) and the last mesocycle must end
on or around the goal date ({next_goal['target_date']}).
"""

        if athlete_feedback:
            custom_task += f"""
ATHLETE FEEDBACK ON THE PREVIOUS PLAN:
The athlete has provided direct feedback on the previous periodization plan:
{athlete_feedback}
You MUST revise the macrocycle strategy and/or the duration, boundaries, and focuses of individual
mesocycles to directly address this feedback. Make adjustments (e.g. scheduling more rest,
changing block emphasis, extending/shortening specific cycles) while continuing to respect overall
sports science principles and guidelines.
"""

        if previous_strategy_text:
            custom_task += """
For context, the PREVIOUS periodization strategy that was in place before this
replanning is provided below. Please take it into account to ensure continuity
in the athlete's training, adapting or building on top of what has been planned
or done so far, rather than starting completely from scratch, unless a complete
reset is warranted by major changes.
"""

        custom_task += f"""
You MUST respond with a JSON object containing:
{{
  "strategy": "Explain the overall training strategy philosophy and periodization strategy
    until the goal.",
  "mesocycles": [
    {{
      "name": "Phase Name (e.g., Base Building, Specific Preparation, Build,
        Peak & Taper, Race/Event)",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "focus": "Key focus and description of this block (e.g., volume progression,
        aerobic threshold, rest, peak load, etc.)"
    }}
  ]
}}
"""
        obj_text = ""
        for o in objectives:
            details = o.get('description', '')
            obj_text += (
                f"- Goal: {o['title']} | Date: {o['target_date']} | "
                f"Sport: {o['sport_type']} | Details: {details}\n"
            )

        c_text = ""
        for c in lifeevents:
            impact = c.get('impact_description', '')
            c_text += (
                f"- Life Event: {c['title']} | Start: {c['start_date']} | "
                f"End: {c['end_date']} | Type: {c['event_type']} | Impact: {impact}\n"
            )

        athlete_profile = self._format_athlete_profile(profile)
        system_prompt = (
            "You are TrainMate Coach, an advanced AI sports science training coach.\n"
            "You design periodized training plans (macro, meso, micro cycles) leading up "
            "to target goals.\n\n"
            "================================================================================\n"
            "START OF SPORTS SCIENCE GUIDELINES\n"
            "================================================================================\n"
            f"{guidelines}\n"
            "================================================================================\n"
            "END OF SPORTS SCIENCE GUIDELINES\n"
            "================================================================================\n"
        )

        if previous_strategy_text:
            system_prompt += f"\n{previous_strategy_text}\n"

        system_prompt += (
            f"\nATHLETE PROFILE & PREFERENCES:\n{athlete_profile}\n"
        )
        if history_summary:
            system_prompt += (
                f"\nATHLETE RECENT TRAINING SUMMARY (PAST 15 DAYS):\n{history_summary}\n"
            )
        # Planned-vs-actual review of the prior plan + any inferred reconstruction, fed as
        # read-only context so the new plan is grounded in demonstrated reality rather than
        # an idealized template (DESIGN_backward_evaluation.md §6, Option A).
        if prior_training_text:
            system_prompt += f"\nPRIOR TRAINING REVIEW:\n{prior_training_text}\n"
        system_prompt += (
            f"\nACTIVE ATHLETE GOALS (CHRONOLOGICAL):\n"
            f"{obj_text if obj_text else 'No active goals.'}\n\n"
            f"UPCOMING LIFE EVENTS:\n"
            f"{c_text if c_text else 'No upcoming life events.'}\n\n"
            f"{custom_task}\n"
        )

        user_content = (
            f"Today's date is {today_str}. The target goal is "
            f"'{next_goal['title']}' on {next_goal['target_date']}. "
            f"Please determine the macrocycle and mesocycle blocks starting from {plan_start}."
        )

        print("Querying OpenRouter to generate macrocycle and mesocycles periodization strategy...")
        result = openrouter_client.complete(
            system_prompt, user_content, label="periodization_plan"
        )
        return result

    def _generate_workouts_logic(
        self, objectives: List[Objective], lifeevents: List[LifeEvent],
        today_str: str, guidelines: str, profile: Optional[Dict[str, Any]],
        strategy: str, meso_text: str, learnings: str,
        num_days: int = 28,
        metrics: Optional[List[Dict[str, Any]]] = None,
        completed_activities: Optional[List[CompletedActivity]] = None,
        baseline: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Queries LLM to generate workouts for a given number of days based on active strategy."""
        weeks = num_days / 7
        if weeks == int(weeks):
            duration_desc = f"{int(weeks)} week{'s' if weeks != 1 else ''} ({num_days} days)"
        else:
            duration_desc = f"{num_days} day{'s' if num_days != 1 else ''}"
        custom_task = (
            f"TASK:\nGenerate a training schedule for the next {duration_desc} starting from today.\n"
            "Ensure the weekly schedules/microcycles are designed specifically to match the focus, target\n"
            "volume, and intensity of the active mesocycle block(s) the athlete is in during this period, and\n"
            "incorporate any deload weeks or exceptions for upcoming life events in accordance with the\n"
            "science guidelines.\n"
            "\n"
            "You MUST respond with a JSON object containing:\n"
            "{\n"
            '  "reasoning": "Explain the microcycle design, detailing how workouts align with the active\n'
            '    mesocycle focus.",\n'
            # Workout generation is read-only w.r.t. coach learnings (see
            # DESIGN_backward_evaluation.md §11): it consumes the rendered learnings in the
            # system prompt but authors none. Tactical/recent observations are better
            # captured by `adapt`, durable ones by `analyze`. Hence no learning_updates here.
            '  "workouts": [\n'
            "    {\n"
            '      "date": "YYYY-MM-DD",\n'
            '      "sport_type": "running" | "road_biking" | "hiking" | "strength_training" | "yoga" |\n'
            '        "ski_touring" | "rest",\n'
            '      "title": "Workout Title (e.g., Tempo Run, Long Ride, Rest Day)",\n'
            '      "description": "Detailed description of intensity, duration, heart rate zones, and\n'
            '        goals.",\n'
            "      \"duration_minutes\": 60, (Estimated workout duration in minutes, integer. Use 0 for rest days)\n"
            "      \"rpe\": 6, (Expected Rate of Perceived Exertion, integer 1-10. Use 0 for rest days)\n"
            "      \"tss\": 45.0 (Expected Training Stress Score, float/integer. Use 0 for rest days)\n"
            "    }\n"
            "  ]\n"
            "}\n"
        )
        system_prompt = self._build_system_prompt(
            objectives=objectives,
            lifeevents=lifeevents,
            guidelines=guidelines,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            profile=profile,
            custom_task=custom_task
        )
        user_content = (
            f"Today's date is {today_str}. "
            f"Please generate the microcycles (workouts) for the next {duration_desc} starting today."
        )

        history_text_parts = []
        if metrics:
            metrics_text = format_metrics_history(metrics)
            history_text_parts.append(
                f"Athlete's Metrics History (Past 15 Days):\n{metrics_text}"
            )
        if baseline:
            baseline_str = format_baseline(baseline)
            history_text_parts.append(
                f"Baseline Reference:\n{baseline_str}"
            )
        if completed_activities:
            completed_text = format_completed_activities(completed_activities)
            history_text_parts.append(
                f"Actual Completed Garmin Activities in Window:\n{completed_text}"
            )

        if history_text_parts:
            user_content += "\n\n" + "\n\n".join(history_text_parts)

        print("Querying OpenRouter to generate training workouts (microcycles)...")
        plan_data = openrouter_client.complete(
            system_prompt, user_content, label="workout_generation"
        )
        return plan_data

    def _adapt_logic(
        self, target_date_str: str, history_days: int, start_date_str: str,
        metrics: List[Dict[str, Any]], completed_activities: List[CompletedActivity],
        planned_workouts: List[Workout], baseline_str: str,
        meso_end_date_str: str, objectives: List[Objective], lifeevents: List[LifeEvent],
        guidelines: str, profile: Optional[Dict[str, Any]], strategy: str,
        meso_text: str, learnings: str, discrepancies: List[str]
    ) -> Dict[str, Any]:
        """Queries LLM to evaluate metrics/activities and adapt workouts if needed."""
        custom_task = f"""
TASK:
Analyze the athlete's actual workout adherence and physiological metrics trajectory
over the past {history_days} days.
Review the list of completed activities compared to planned workouts and any
calculated discrepancies (misses, workload/duration differences, rest violations).
Also inspect the rolling baseline reference and the daily metrics sequence to see
if the athlete shows signs of accumulated fatigue.

Based on this, determine if we need to adapt the training plan for the remainder of
the active mesocycle block (from {target_date_str} to {meso_end_date_str}).
- If they are showing high fatigue or injury risk (e.g. elevated RHR, depressed HRV,
  poor sleep, or ACWR > 1.3), replace hard workouts with recovery or rest.
- If they have missed key workouts, adjust the remaining workouts to safely build back
  volume without spiking the acute load too fast.
- If they are fully recovered and on track, keep the plan as scheduled or make minor
  optimal adjustments.

If this window reveals a durable insight about how the athlete responds to training
(recovery patterns, load tolerance, recurring adherence/injury signals), record it via
learning_updates — prefer reinforcing or revising an existing observation by [id] over
adding a near-duplicate. Do not record one-off, day-specific noise.
""" + (
            "You MUST respond with a JSON object containing:\n"
            "{\n"
            '  "change_needed": true | false,\n'
            '  "reason": "Swapping tempo run to rest.",\n'
            + LEARNING_UPDATES_FIELD +
            "  ],\n"
            '  "adapted_workouts": [\n'
            "    {\n"
            '      "date": "YYYY-MM-DD",\n'
            '      "sport_type": "running" | "road_biking" | "hiking" | "strength_training" |\n'
            '        "yoga" | "ski_touring" | "rest",\n'
            '      "title": "Adapted Workout Title",\n'
            '      "description": "Adapted description of intensity, duration, heart rate zones, and goals.",\n'
            '      "duration_minutes": 45,\n'
            '      "rpe": 5,\n'
            '      "tss": 30.0\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )
        system_prompt = self._build_system_prompt(
            objectives=objectives,
            lifeevents=lifeevents,
            guidelines=guidelines,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            profile=profile,
            custom_task=custom_task
        )

        metrics_text = format_metrics_history(metrics)
        discrepancy_text = (
            "\n".join(discrepancies) if discrepancies
            else "No discrepancies detected (athlete fully on track)."
        )
        planned_text = format_planned_workouts(planned_workouts)
        completed_text = format_completed_activities(completed_activities)

        user_content = f"""
Evaluation Date: {target_date_str}
Adaptation Range: {target_date_str} to {meso_end_date_str}

Athlete's Metrics History (Past {history_days} Days):
{metrics_text}

Baseline Reference:
{baseline_str}

Planned Workouts in Window:
{planned_text}

Actual Completed Garmin Activities in Window:
{completed_text}

Adherence Discrepancies & Violations:
{discrepancy_text}
"""
        print(f"Querying OpenRouter to evaluate adaptation for the remainder of the mesocycle "
              f"({target_date_str} -> {meso_end_date_str})...")
        decision = openrouter_client.complete(
            system_prompt, user_content, label="workout_adaptation"
        )
        return decision

    def _generate_intermediate_goals(
        self, next_goal: Objective, today_str: str, duration_weeks: float
    ) -> Dict[str, Any]:
        """Queries the LLM to generate intermediate objectives to split a >24w timeline."""
        orig_title = next_goal['title']
        target_date = next_goal['target_date']
        sport_type = next_goal['sport_type']
        desc = next_goal.get('description', '')
        priority = next_goal['priority']

        system_prompt = (
            "You are TrainMate Coach, an advanced AI sports science training coach.\n"
            "You help split long-term training timelines (exceeding 24 weeks) into "
            "multiple sequential macrocycles.\n"
            "You do this by proposing sports-science-sensible intermediate training goals "
            "(e.g., a base fitness check, a 10K tune-up, or a half marathon test) "
            "that anchor each macrocycle container.\n\n"
            "GUIDELINES FOR INTERMEDIATE GOALS:\n"
            "1. Each macrocycle container leading to a goal must respect the duration constraints\n"
            "   detailed in the science guidelines (5 to 24 weeks).\n"
            f"2. The target dates for all proposed goals must be sequential, start after "
            f"today ({today_str}), and lead chronologically up to the final event date "
            f"({target_date}).\n"
            f"3. The sport type of the intermediate goals must be: {sport_type}.\n"
            "4. Provide a clear description explaining why this is a sensible sports "
            "science milestone for the athlete's progression.\n"
            "5. The intermediate milestones must be named clearly so that they can be "
            "easily linked to the original long-term goal. Format their titles as: "
            f"'{orig_title} - Interim: <milestone_name>'. For example, if the original goal "
            f"is '{orig_title}', an intermediate goal title could be "
            f"'{orig_title} - Interim: Half Marathon Tune-Up'.\n"
        )

        user_content = (
            f"Today's date is {today_str}.\n"
            f"The final objective is '{orig_title}' on {target_date}.\n"
            f"Sport type: {sport_type}\n"
            f"Description: {desc}\n\n"
            f"Please split this {duration_weeks:.1f}-week timeline by proposing intermediate "
            "goals.\n"
            "You MUST respond with a JSON object containing:\n"
            "{\n"
            '  "goals": [\n'
            "    {\n"
            f'      "title": "{orig_title} - Interim: <name>",\n'
            '      "target_date": "YYYY-MM-DD",\n'
            f'      "sport_type": "{sport_type}",\n'
            '      "description": "Sports science rationale for this milestone.",\n'
            f'      "priority": {priority}\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )
        print("Querying OpenRouter to generate intermediate objectives...")
        result = openrouter_client.complete(
            system_prompt, user_content, label="generate_intermediate_goals"
        )
        return result

    def _analyze_workouts_logic(
        self, objectives: List[Objective], guidelines: str,
        profile: Optional[Dict[str, Any]],
        weekly_summaries: List[Dict[str, Any]],
        learnings: str,
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Queries LLM to reverse-engineer training cycles from weekly summaries."""
        custom_task = (
            "TASK:\n"
            "Analyze the athlete's completed training load, zone distributions, and\n"
            "physiological metrics week-by-week. Reverse-engineer this data to identify\n"
            "the underlying training phases (macrocycle & mesocycles) that occurred.\n"
            "\n"
            "You MUST respond with a JSON object containing:\n"
            "{\n"
            '  "macrocycle_summary": "High-level summary of the training period.",\n'
            '  "inferred_macrocycle": {\n'
            '    "overall_focus": "e.g. Marathon base prep",\n'
            '    "start_date": "YYYY-MM-DD",\n'
            '    "end_date": "YYYY-MM-DD"\n'
            "  },\n"
            '  "inferred_mesocycles": [\n'
            "    {\n"
            '      "name": "Phase Name (e.g. Base Building, Build, Recovery, etc.)",\n'
            '      "start_date": "YYYY-MM-DD",\n'
            '      "end_date": "YYYY-MM-DD",\n'
            '      "focus_detected": "Key detected focus of this block",\n'
            '      "average_weekly_tss": 380.0,\n'
            '      "estimated_consistency": "High" | "Moderate" | "Low"\n'
            "    }\n"
            "  ],\n"
            '  "physiological_insights": [\n'
            '    "Physiological response observations (e.g., HRV/RHR trends vs load)."\n'
            "  ],\n"
            + LEARNING_UPDATES_FIELD +
            "  ]\n"
            "}\n"
        )

        system_prompt = (
            "You are TrainMate Coach, an advanced AI sports science training coach.\n"
            "You analyze historical activities and physiological metrics to identify\n"
            "training periodization phases (macro and mesocycles).\n\n"
            "================================================================================\n"
            "START OF SPORTS SCIENCE GUIDELINES\n"
            "================================================================================\n"
            f"{guidelines}\n"
            "================================================================================\n"
            "END OF SPORTS SCIENCE GUIDELINES\n"
            "================================================================================\n"
        )

        athlete_profile = self._format_athlete_profile(profile)
        system_prompt += f"\nATHLETE PROFILE & PREFERENCES:\n{athlete_profile}\n"

        obj_text = ""
        for o in objectives:
            details = o.get('description', '')
            obj_text += (
                f"- Goal: {o['title']} | Date: {o['target_date']} | "
                f"Sport: {o['sport_type']} | Details: {details}\n"
            )
        system_prompt += (
            f"\nATHLETE GOALS IN OR AFTER THIS PERIOD:\n"
            f"{obj_text if obj_text else 'No objectives.'}\n"
        )

        # Show existing observations so the model can revise/reinforce/retire them by
        # [id] rather than only re-adding near-duplicates on every run.
        system_prompt += (
            "\nCOACH LEARNINGS — existing athlete observations "
            "(reference by [id] when revising, reinforcing, or retiring):\n"
            f"{learnings}\n"
        )

        system_prompt += f"\n{custom_task}\n"

        user_content = "Please analyze the following weekly training summaries:\n\n"
        user_content += json.dumps(weekly_summaries, indent=2)

        if context:
            user_content += f"\n\nATHLETE SUBJECTIVE CONTEXT FOR THIS PERIOD:\n{context}\n"

        print("Querying OpenRouter to perform training history analysis...")
        result = openrouter_client.complete(
            system_prompt, user_content, label="workout_analysis"
        )
        return result




class CoachService:
    """Orchestrates sports science coaching by coordinating data I/O and business logic."""

    def __init__(
        self, db_instance=None, calendar_syncer_instance=None,
        engine: Optional[CoachEngine] = None
    ):
        self._db_instance = db_instance
        self._calendar_syncer_instance = calendar_syncer_instance
        self.engine = engine or CoachEngine()

    @property
    def _db(self):
        return self._db_instance or db

    @property
    def _calendar_syncer(self):
        return self._calendar_syncer_instance or calendar_syncer

    def _get_recent_history_summary(self, today_str: str) -> str:
        """Retrieves and constructs a summary of the past 15 days of workouts/metrics."""
        today_date = datetime.strptime(today_str, "%Y-%m-%d").date()
        start_date_obj = today_date - timedelta(days=14)
        start_date_str = start_date_obj.strftime("%Y-%m-%d")

        metrics = self._db.get_metrics_cache(
            start_date=start_date_str, end_date=today_str
        )
        completed_activities = self._db.get_completed_activities(
            start_date=start_date_str, end_date=today_str
        )

        lines = []

        # 1. Activities summary
        if completed_activities:
            sport_durations: Dict[str, float] = {}
            sport_counts: Dict[str, int] = {}
            for act in completed_activities:
                sport = act.get('activity_type', 'unknown').lower()
                dur_min = act.get('duration_sec', 0.0) / 60.0
                sport_durations[sport] = sport_durations.get(sport, 0.0) + dur_min
                sport_counts[sport] = sport_counts.get(sport, 0) + 1

            lines.append("Completed Workouts (Past 15 days):")
            total_duration_hours = 0.0
            for sport, count in sport_counts.items():
                dur_hours = sport_durations[sport] / 60.0
                total_duration_hours += dur_hours
                lines.append(
                    f"  - {sport}: {count} sessions, "
                    f"total duration {dur_hours:.1f} hours"
                )

            weekly_avg_hours = (total_duration_hours / 15.0) * 7.0
            lines.append(
                f"  - Total training volume: {total_duration_hours:.1f} hours "
                f"(~{weekly_avg_hours:.1f} hours/week)"
            )
        else:
            lines.append("Completed Workouts (Past 15 days):\n  - No completed workouts found.")

        # 2. Metrics summary
        if metrics:
            rhrs = [m['rhr'] for m in metrics if m.get('rhr') is not None]
            hrvs = [m['hrv'] for m in metrics if m.get('hrv') is not None]
            sleeps = [m['sleep_score'] for m in metrics if m.get('sleep_score') is not None]
            acwrs = [m['acwr'] for m in metrics if m.get('acwr') is not None]

            lines.append("Physiological Metrics (15-day average):")
            if rhrs:
                lines.append(f"  - Resting Heart Rate: {sum(rhrs)/len(rhrs):.1f} bpm")
            if hrvs:
                lines.append(f"  - Heart Rate Variability (HRV): {sum(hrvs)/len(hrvs):.1f} ms")
            if sleeps:
                lines.append(f"  - Sleep Score: {sum(sleeps)/len(sleeps):.1f}/100")
            if acwrs:
                lines.append(
                    f"  - Current ACWR (Acute:Chronic Workload Ratio): "
                    f"{acwrs[-1]:.2f} (latest)"
                )
        else:
            lines.append("Physiological Metrics (Past 15 days):\n  - No metrics found.")

        return "\n".join(lines)

    def _build_prior_training_context(
        self, prior_macro: Optional[Dict[str, Any]], today_str: str
    ) -> Optional[str]:
        """Builds a read-only "planned vs actual" review for the strategy prompt
        (DESIGN_backward_evaluation.md §6, Option A).

        Anchored on the prior plan's *elapsed* mesocycle windows (§6): each planned block's
        focus is shown beside what the athlete actually did in that window (sessions,
        volume, TSS, zone split) so the model can judge whether the block's intent
        materialized. If a cached backward-evaluation reconstruction exists (from `data
        analyze`), its summary + physiological insights are appended — reused without
        another LLM call (§10). Returns None if there is nothing to report.

        This does NOT write to any `feedback` field: under Option A the assessment is
        prompt context only, sidestepping the feedback-lifecycle collision (§11).
        """
        sections: List[str] = []

        if prior_macro:
            block_lines = []
            for m in self._db.get_mesocycles_for_macrocycle(prior_macro['id']):
                if m['start_date'] > today_str:
                    continue  # future block; nothing actual to compare yet
                win_end = min(m['end_date'], today_str)
                acts = self._db.get_completed_activities(m['start_date'], win_end)
                if not acts:
                    block_lines.append(
                        f"- {m['name']} ({m['start_date']}..{win_end}): planned focus "
                        f"\"{m['focus']}\" — no completed activities recorded."
                    )
                    continue
                hours = sum((a.get('duration_sec') or 0.0) for a in acts) / 3600.0
                tss = sum(activity_load(a) for a in acts)
                z12 = sum(
                    (a.get('zone1_sec') or 0) + (a.get('zone2_sec') or 0) for a in acts
                )
                z3 = sum((a.get('zone3_sec') or 0) for a in acts)
                z45 = sum(
                    (a.get('zone4_sec') or 0) + (a.get('zone5_sec') or 0) for a in acts
                )
                # Power zones use Garmin's 7-zone model, grouped polarized like HR:
                # Z1-2 easy / Z3-4 threshold / Z5-7 hard.
                pz12 = sum(
                    (a.get('power_zone1_sec') or 0) + (a.get('power_zone2_sec') or 0)
                    for a in acts
                )
                pz34 = sum(
                    (a.get('power_zone3_sec') or 0) + (a.get('power_zone4_sec') or 0)
                    for a in acts
                )
                pz567 = sum(
                    (a.get('power_zone5_sec') or 0) + (a.get('power_zone6_sec') or 0)
                    + (a.get('power_zone7_sec') or 0) for a in acts
                )
                zone_note = ""
                if (z12 + z3 + z45) > 0:
                    zone_note += (
                        f", HR zones Z1-2/Z3/Z4-5 = {z12 // 60}/{z3 // 60}/{z45 // 60} min"
                    )
                if (pz12 + pz34 + pz567) > 0:
                    zone_note += (
                        f", power zones Z1-2/Z3-4/Z5-7 = "
                        f"{pz12 // 60}/{pz34 // 60}/{pz567 // 60} min"
                    )
                block_lines.append(
                    f"- {m['name']} ({m['start_date']}..{win_end}): planned focus "
                    f"\"{m['focus']}\" — actual: {len(acts)} sessions, {hours:.1f}h, "
                    f"{tss:.0f} TSS{zone_note}."
                )
            if block_lines:
                sections.append(
                    "PLANNED vs ACTUAL (elapsed blocks of the prior plan — judge whether "
                    "each block's intent materialized):\n" + "\n".join(block_lines)
                )

        cached = self._db.get_analysis_cache("long")
        recon = cached.get("reconstruction") if cached else None
        if recon:
            recon_lines = []
            if recon.get("macrocycle_summary"):
                recon_lines.append(f"Summary: {recon['macrocycle_summary']}")
            for ins in (recon.get("physiological_insights") or []):
                recon_lines.append(f"- {ins}")
            if recon_lines:
                window = ""
                if cached.get("window_start") and cached.get("window_end"):
                    window = f" ({cached['window_start']}..{cached['window_end']})"
                sections.append(
                    f"INFERRED FROM PAST TRAINING{window} (latest data analysis):\n"
                    + "\n".join(recon_lines)
                )

        return "\n\n".join(sections) if sections else None

    def _get_config_hash(self) -> str:
        return self.engine._get_config_hash()

    def _get_goals_hash(self, objectives: List[Objective]) -> str:
        return self.engine._get_goals_hash(objectives)

    def _get_lifeevents_hash(self, lifeevents: List[LifeEvent]) -> str:
        return self.engine._get_lifeevents_hash(lifeevents)

    def _load_science_guidelines(self) -> str:
        return _load_science_guidelines(config.app_science_dir, config.science_dir)

    def _get_active_strategy_and_meso_text(
        self, objectives: List[Objective], objective_id: Optional[int] = None
    ) -> Tuple[str, str]:
        strategy = None
        meso_text = ""
        
        next_goal = self._db.get_active_objective(objective_id)
        if not next_goal and objective_id is not None:
            next_goal = self._db.get_objective(objective_id)

        if next_goal and next_goal['id'] is not None:
            macrocycle = self._db.get_macrocycle_for_objective(next_goal['id'])
            if macrocycle:
                strategy = macrocycle['strategy']
                mesocycles = self._db.get_mesocycles_for_macrocycle(macrocycle['id'])
                for m in mesocycles:
                    meso_text += (
                        f"  - {m['name']} ({m['start_date']} to "
                        f"{m['end_date']}): {m['focus']}\n"
                    )
        if not strategy:
            strategy = (
                "Not established yet. Establish an endurance-focused training strategy "
                "based on goals."
            )
            meso_text = "  - Not established yet."
        return strategy, meso_text

    def _get_learnings_text(self) -> str:
        """Renders active athlete observations as a tagged block for prompts. Each line is
        `[id|sports|confidence] text`. Dormant (decayed) observations are omitted so stale
        notes stop influencing planning until reaffirmed."""
        learnings = [l for l in self._db.get_learnings() if not l.get("dormant")]
        if not learnings:
            return (
                "No observations yet. Over time, observe the athlete's responses to "
                "training volume and intensity."
            )
        return "\n".join(
            f"  [{l['id']}|{l.get('sports') or 'general'}|"
            f"{l.get('confidence') or 'tentative'}] {l['text']}"
            for l in learnings
        )

    def _apply_learning_updates(
        self, data: Dict[str, Any], suppress_reinforcement: bool = False
    ) -> None:
        """Applies incremental learning deltas returned by the LLM, if any.

        `suppress_reinforcement` is forwarded to the merge layer: pass True when the deltas
        were derived from unchanged evidence so re-reading cannot ratchet confidence/recency
        (see db.apply_learning_deltas and DESIGN_backward_evaluation.md §8)."""
        self._db.apply_learning_deltas(
            data.get("learning_updates") or [],
            suppress_reinforcement=suppress_reinforcement,
        )

    def _get_coach_system_prompt(
        self, objectives: List[Objective], lifeevents: List[LifeEvent],
        custom_task: str = "", objective_id: Optional[int] = None
    ) -> str:
        guidelines = self._load_science_guidelines()
        strategy, meso_text = self._get_active_strategy_and_meso_text(
            objectives, objective_id=objective_id
        )
        learnings = self._get_learnings_text()
        profile = config.user_profile
        return self.engine._build_system_prompt(
            objectives=objectives,
            lifeevents=lifeevents,
            guidelines=guidelines,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            profile=profile,
            custom_task=custom_task
        )

    def delete_plan(self, objective_id: int) -> None:
        """Deletes the periodization plan for a specific objective."""
        self._db.delete_macrocycle_for_objective(objective_id)

    def generate_periodization_plan(
        self, force: bool = False, objective_id: Optional[int] = None
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Determines the macrocycle strategy and mesocycle blocks."""
        # Identify the target goal
        if objective_id is not None:
            next_goal = self._db.get_active_objective(objective_id)
            if not next_goal:
                next_goal = self._db.get_objective(objective_id)
                if not next_goal:
                    raise ValueError(f"Goal with ID {objective_id} not found.")
        else:
            next_goal = self._db.get_active_objective()
            if not next_goal:
                return "No active goals found. TrainMate needs at least one objective.", []

        # Get future life events
        today_str = _today_str()
        today_date = datetime.strptime(today_str, "%Y-%m-%d").date()

        # Determine plan start date based on preceding goals with plans
        plan_start_date = today_date
        preceding_objs = self._db.get_preceding_objectives(next_goal['target_date'])
        
        latest_preceding_target = None
        prev_macro = None
        
        for po in preceding_objs:
            if po['id'] is not None:
                po_macro = self._db.get_macrocycle_for_objective(po['id'])
                if po_macro:
                    po_target = datetime.strptime(po['target_date'], "%Y-%m-%d").date()
                    latest_preceding_target = po_target
                    prev_macro = po_macro
                    break

        if latest_preceding_target is not None:
            plan_start_date = latest_preceding_target + timedelta(days=1)
            if plan_start_date < today_date:
                plan_start_date = today_date

        # Compute duration
        target_date = datetime.strptime(next_goal['target_date'], "%Y-%m-%d").date()
        duration_days = (target_date - plan_start_date).days
        duration_weeks = duration_days / 7.0

        if duration_weeks < 5:
            raise ValueError(
                f"Goal '{next_goal['title']}' is too close "
                f"({duration_weeks:.1f} weeks away from the start date "
                f"{plan_start_date.strftime('%Y-%m-%d')}). "
                f"TrainMate requires at least 5 weeks to generate a periodization plan."
            )

        if duration_weeks > 24:
            print(f"Goal '{next_goal['title']}' is {duration_weeks:.1f} "
                  "weeks away (> 24 weeks).")
            print("Querying LLM to generate intermediate objectives...")
            goals_data = self.engine._generate_intermediate_goals(
                next_goal=next_goal,
                today_str=plan_start_date.strftime("%Y-%m-%d"),
                duration_weeks=duration_weeks
            )
            proposed_goals = goals_data.get("goals", [])
            if not proposed_goals:
                raise ValueError(
                    "LLM did not return any intermediate goals to split "
                    "the timeline."
                )

            for pg in proposed_goals:
                self._db.add_objective(
                    title=pg['title'],
                    target_date=pg['target_date'],
                    sport_type=pg['sport_type'],
                    description=pg.get('description', ''),
                    priority=pg.get('priority', next_goal['priority']),
                    status='active'
                )

            # Re-fetch active objective and update next_goal
            next_goal = self._db.get_active_objective()
            if not next_goal:
                raise ValueError("No active objectives found after splitting.")
            target_date = datetime.strptime(next_goal['target_date'], "%Y-%m-%d").date()
            duration_days = (target_date - plan_start_date).days
            duration_weeks = duration_days / 7.0

        # Compute current hashes
        # We need to fetch active objectives for hash computation so the hash covers the whole landscape
        objectives = self._db.get_objectives(status='active')
        lifeevents = self._db.get_lifeevents(start_after=today_str)
        goals_hash = self.engine._get_goals_hash(objectives)
        lifeevents_hash = self.engine._get_lifeevents_hash(lifeevents)
        config_hash = self.engine._get_config_hash()

        # Try to retrieve existing macrocycle
        strategy = ""
        mesocycles: List[Dict[str, Any]] = []
        existing_macro = None
        if next_goal['id'] is not None:
            existing_macro = self._db.get_macrocycle_for_objective(next_goal['id'])

        reused = False
        if existing_macro and not force:
            if (
                existing_macro['goals_hash'] == goals_hash
                and existing_macro['lifeevents_hash'] == lifeevents_hash
                and existing_macro.get('config_hash') == config_hash
            ):
                reused = True
                strategy = existing_macro['strategy']
                mesocycles = self._db.get_mesocycles_for_macrocycle(existing_macro['id'])
                print("Reusing existing periodization strategy (macrocycle and mesocycles) "
                      "from database.")

        if not reused:
            # Get the previous strategy for context
            if existing_macro:
                prev_macro = existing_macro

            prev_strategy_text = None
            if prev_macro:
                prev_mesos = self._db.get_mesocycles_for_macrocycle(prev_macro['id'])
                prev_meso_text = ""
                for m in prev_mesos:
                    prev_meso_text += (
                        f"  - {m['name']} ({m['start_date']} to {m['end_date']}): "
                        f"{m['focus']}\n"
                    )
                prev_strategy_text = (
                    "PREVIOUS PERIODIZATION STRATEGY (FOR CONTEXT):\n"
                    f"- Overall Strategy: {prev_macro['strategy']}\n"
                    f"- Mesocycles:\n{prev_meso_text or '  - None\n'}"
                )

            # Retrieve active feedback from existing plan
            feedback_text = None
            if existing_macro:
                fb_parts = []
                if existing_macro.get('feedback'):
                    fb_parts.append(
                        f"- Overall Strategy Feedback: \"{existing_macro['feedback']}\""
                    )
                existing_mesos = self._db.get_mesocycles_for_macrocycle(existing_macro['id'])
                for m in existing_mesos:
                    if m.get('feedback'):
                        fb_parts.append(f"- Phase \"{m['name']}\" Feedback: \"{m['feedback']}\"")
                if fb_parts:
                    feedback_text = "\n".join(fb_parts)

            # Generate new macrocycle strategy and mesocycles
            print("Goals or life events have changed, or force generation requested. "
                  "Determining new overall periodization strategy...")
            guidelines = self._load_science_guidelines()
            profile = config.user_profile
            history_summary = self._get_recent_history_summary(today_str)
            # Planned-vs-actual review of the prior plan (+ cached reconstruction) fed as
            # read-only context (Option A). `prev_macro` here is the existing plan being
            # replaced, or the preceding goal's plan when there is none.
            prior_training_text = self._build_prior_training_context(prev_macro, today_str)
            if prior_training_text:
                print("\n=== PRIOR TRAINING REVIEW (planned vs actual) ===")
                print(prior_training_text)
                print("==================================================\n")
            macro_data = self.engine._generate_macrocycle_strategy(
                next_goal=next_goal,
                objectives=objectives,
                lifeevents=lifeevents,
                today_str=today_str,
                guidelines=guidelines,
                profile=profile,
                previous_strategy_text=prev_strategy_text,
                plan_start_str=plan_start_date.strftime("%Y-%m-%d"),
                athlete_feedback=feedback_text,
                history_summary=history_summary,
                prior_training_text=prior_training_text
            )
            strategy = macro_data.get("strategy", "Endurance preparation strategy.")
            mesocycles = macro_data.get("mesocycles", [])

            # Save it
            if next_goal['id'] is not None:
                self._db.save_macrocycle(
                    objective_id=next_goal['id'],
                    strategy=strategy,
                    goals_hash=goals_hash,
                    lifeevents_hash=lifeevents_hash,
                    config_hash=config_hash,
                    mesocycles=mesocycles
                )
            print("\n=== NEW PERIODIZATION STRATEGY (MACROCYCLE) ===")
            print(f"Overall Strategy:\n{strategy}\n")
            print("Mesocycle Blocks:")
            for m in mesocycles:
                print(f"- {m['name']} ({m['start_date']} to {m['end_date']}): {m['focus']}")
            print("==============================================\n")

        return strategy, mesocycles

    def generate_workouts(
        self, objective_id: Optional[int] = None, end_date: Optional[str] = None
    ) -> Tuple[str, List[Workout]]:
        """Generates workouts (microcycles) based on the active strategy."""
        # Identify the target goal
        if objective_id is not None:
            next_goal = self._db.get_active_objective(objective_id)
            if not next_goal:
                next_goal = self._db.get_objective(objective_id)
                if not next_goal:
                    raise ValueError(f"Active goal with ID {objective_id} not found.")
        else:
            next_goal = self._db.get_active_objective()
            if not next_goal:
                return "No active goals found. TrainMate needs at least one objective.", []

        # Verify active periodization strategy exists
        macrocycle = self._db.get_macrocycle_for_objective(next_goal['id'])
        if not macrocycle:
            raise ValueError(
                "No active periodization strategy found. "
                "Please generate a periodization plan first."
            )

        today_str = _today_str()
        today_date_obj = datetime.strptime(today_str, "%Y-%m-%d").date()

        if end_date is not None:
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
            num_days = max(1, (end_date_obj - today_date_obj).days)
        else:
            num_days = config.workout_generate_days

        lifeevents = self._db.get_lifeevents(start_after=today_str)
        guidelines = self._load_science_guidelines()
        profile = config.user_profile
        
        # We need all objectives for _get_active_strategy_and_meso_text context
        objectives = self._db.get_objectives(status='active')
        strategy, meso_text = self._get_active_strategy_and_meso_text(
            objectives, objective_id=objective_id
        )
        learnings = self._get_learnings_text()

        # Retrieve recent history context
        history_days = config.metrics_lookback_days
        start_date_obj = today_date_obj - timedelta(days=history_days - 1)
        start_date_str = start_date_obj.strftime("%Y-%m-%d")

        metrics = self._db.get_metrics_cache(
            start_date=start_date_str, end_date=today_str
        )
        completed_activities = self._db.get_completed_activities(
            start_date=start_date_str, end_date=today_str
        )
        baseline = self._db.get_baseline(today_str)

        plan_data = self.engine._generate_workouts_logic(
            objectives=objectives,
            lifeevents=lifeevents,
            today_str=today_str,
            guidelines=guidelines,
            profile=profile,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            num_days=num_days,
            metrics=metrics,
            completed_activities=completed_activities,
            baseline=baseline
        )

        # NOTE: workout generation is read-only w.r.t. coach learnings (see
        # DESIGN_backward_evaluation.md §11) — it does not apply learning_updates. Durable
        # memory is authored only by `analyze` and `plan generate`.

        # Save workouts to database
        workouts = plan_data.get("workouts", [])

        # Clear future workouts from the previous plan to prevent overlap. This includes
        # synced workouts: their Google Calendar events are deleted first so the old plan
        # doesn't linger on the calendar.
        for ew in self._db.get_workouts(start_date=today_str):
            if ew.get('google_event_id'):
                try:
                    self._calendar_syncer.delete_workout_event(ew['google_event_id'])
                except Exception as e:
                    print(f"Error deleting Google Calendar event: {e}")
        self._db.clear_future_workouts(today_str, include_calendar_events=True)

        saved_workouts: List[Workout] = []
        for w in workouts:
            wid = self._db.save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                synced=False,
                duration_minutes=w.get('duration_minutes'),
                rpe=w.get('rpe'),
                tss=w.get('tss')
            )
            saved_workouts.append({
                'id': wid,
                'date': w['date'],
                'sport_type': w['sport_type'],
                'title': w['title'],
                'description': w['description'],
                'original_description': w['description'],
                'synced': False,
                'modification_reason': None,
                'google_event_id': None,
                'duration_minutes': w.get('duration_minutes'),
                'rpe': w.get('rpe'),
                'tss': w.get('tss')
            })

        print(f"Generated {len(workouts)} workouts.")
        return plan_data.get("reasoning", "Plan generated."), saved_workouts

    def replan(
        self, force: bool = False, objective_id: Optional[int] = None
    ) -> Tuple[str, List[Workout]]:
        """Generates or adapts the training plan from today onwards."""
        objectives = self._db.get_objectives(status='active')
        if not objectives:
            return (
                "No active goals found. TrainMate needs at least one objective to "
                "start planning.",
                []
            )

        self.generate_periodization_plan(force=force, objective_id=objective_id)
        return self.generate_workouts(objective_id=objective_id)

    def adapt(self, target_date_str: Optional[str] = None) -> Tuple[str, List[Workout]]:
        """Evaluates metrics/activities over a rolling window and adapts mesocycle if needed."""
        if not target_date_str:
            target_date_str = _today_str()

        target_date_obj = datetime.strptime(target_date_str, "%Y-%m-%d").date()

        # Fetch metrics history window
        history_days = config.metrics_lookback_days
        start_date_obj = target_date_obj - timedelta(days=history_days - 1)
        start_date_str = start_date_obj.strftime("%Y-%m-%d")

        metrics = self._db.get_metrics_cache(
            start_date=start_date_str, end_date=target_date_str
        )
        completed_activities = self._db.get_completed_activities(
            start_date=start_date_str, end_date=target_date_str
        )
        planned_workouts = self._db.get_workouts(
            start_date=start_date_str, end_date=target_date_str
        )

        baseline = self._db.get_baseline(target_date_str)
        baseline_str = format_baseline(baseline)

        # Match planned workouts vs completed activities and compute discrepancies
        discrepancies, matching_results = analyze_adherence(
            planned_workouts=planned_workouts,
            completed_activities=completed_activities,
            start_date_obj=start_date_obj,
            history_days=history_days,
            low_load_threshold=config.low_load_threshold,
        )

        # Determine mesocycle end date for adaptation range
        active_meso = self._db.get_active_mesocycle(target_date_str)
        if active_meso:
            meso_end_date_str = active_meso['end_date']
        else:
            meso_end_date_str = (target_date_obj + timedelta(days=6)).strftime("%Y-%m-%d")

        objectives = self._db.get_objectives(status='active')
        
        # Determine the fallback next_goal for passing to the prompt generator
        next_goal = None
        for obj in objectives:
            if obj['id'] is not None:
                macro = self._db.get_macrocycle_for_objective(obj['id'])
                if macro:
                    next_goal = obj
                    break
        if not next_goal and objectives:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]

        guidelines = self._load_science_guidelines()
        profile = config.user_profile
        objective_id = next_goal['id'] if next_goal else None
        strategy, meso_text = self._get_active_strategy_and_meso_text(
            objectives, objective_id=objective_id
        )
        learnings = self._get_learnings_text()
        lifeevents = self._db.get_lifeevents(start_after=target_date_str)

        decision = self.engine._adapt_logic(
            target_date_str=target_date_str,
            history_days=history_days,
            start_date_str=start_date_str,
            metrics=metrics,
            completed_activities=completed_activities,
            planned_workouts=planned_workouts,
            baseline_str=baseline_str,
            meso_end_date_str=meso_end_date_str,
            objectives=objectives,
            lifeevents=lifeevents,
            guidelines=guidelines,
            profile=profile,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            discrepancies=discrepancies
        )

        # Record any durable observations the adaptation surfaced. Done at evaluation
        # time (not apply time) since the insight stands regardless of whether the
        # proposed workout changes are ultimately applied.
        self._apply_learning_updates(decision)

        reason = decision.get("reason", "No adaptation needed.")
        adapted = []
        if decision.get("change_needed"):
            adapted = decision.get("adapted_workouts", [])

        # Filter and structure returned workouts
        return reason, [
            {
                'id': None,
                'date': w['date'],
                'sport_type': w['sport_type'],
                'title': w['title'],
                'description': w['description'],
                'original_description': w['description'],
                'synced': False,
                'modification_reason': reason,
                'google_event_id': None,
                'duration_minutes': w.get('duration_minutes'),
                'rpe': w.get('rpe'),
                'tss': w.get('tss')
            } for w in adapted
        ]

    def apply_adaptations(
        self, proposed_workouts: List[Dict[str, Any]], reason: str,
        start_date: str, end_date: str
    ) -> None:
        """Saves proposed adapted workouts, cleans up overridden ones, and syncs to Calendar."""
        # 1. Fetch all existing workouts in the adaptation range
        existing_workouts = self._db.get_workouts(
            start_date=start_date, end_date=end_date
        )

        # Group proposed workouts by date
        proposed_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for pw in proposed_workouts:
            proposed_by_date.setdefault(pw['date'], []).append(pw)

        # 2. Find and delete existing workouts that are being replaced or removed
        for ew in existing_workouts:
            ew_date = ew['date']
            if ew_date in proposed_by_date:
                proposed_sports = [p['sport_type'] for p in proposed_by_date[ew_date]]
                if ew['sport_type'] not in proposed_sports:
                    print(f"Removing overridden workout: {ew['title']} ({ew['sport_type']}) "
                          f"on {ew_date}")
                    if ew.get('google_event_id'):
                        try:
                            self._calendar_syncer.delete_workout_event(ew['google_event_id'])
                        except Exception as e:
                            print(f"Error deleting Google Calendar event: {e}")
                    self._db.delete_workout_by_id(ew['id'])

        # 3. Save new adapted workouts and sync them
        for w in proposed_workouts:
            existing = self._db.get_workout(w['date'], w['sport_type'])
            orig_desc = None
            ge_id = None
            if existing:
                orig_desc = existing['original_description'] or existing['description']
                ge_id = existing['google_event_id']

            self._db.save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                original_description=orig_desc or w['description'],
                synced=False,
                modification_reason=reason,
                google_event_id=ge_id,
                duration_minutes=w.get('duration_minutes'),
                rpe=w.get('rpe'),
                tss=w.get('tss')
            )

            # Sync to Google Calendar
            updated = self._db.get_workout(w['date'], w['sport_type'])
            if updated:
                try:
                    self._calendar_syncer.sync_workout(updated)
                except Exception as e:
                    print(f"Error syncing {w['title']} to Google Calendar: {e}")


    # --- Workout swapping ---
    @staticmethod
    def _is_high_intensity(workout: Dict[str, Any]) -> bool:
        """A day is taxing if its session hits RPE >= 7 or TSS >= 100."""
        return (workout.get('rpe') or 0) >= 7 or (workout.get('tss') or 0) >= 100

    @staticmethod
    def _consecutive_runs(dates: set) -> List[List[str]]:
        """Groups a set of YYYY-MM-DD strings into runs of consecutive calendar days."""
        runs: List[List[str]] = []
        current: List[str] = []
        prev = None
        for ds in sorted(dates):
            d = datetime.strptime(ds, "%Y-%m-%d").date()
            if prev is not None and (d - prev).days == 1:
                current.append(ds)
            else:
                if current:
                    runs.append(current)
                current = [ds]
            prev = d
        if current:
            runs.append(current)
        return runs

    @staticmethod
    def _week_of(date_str: str) -> str:
        """Returns the Monday (ISO week start) for a date, as YYYY-MM-DD."""
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")

    def validate_swap(self, swap_ops: List[Dict[str, Any]]) -> List[str]:
        """Simulates a proposed swap and returns sports-science warnings.

        Each op is ``{'id': <workout id>, 'new_date': 'YYYY-MM-DD'}``. Checks for
        newly-created stretches of >2 consecutive high-intensity days, weekly load
        spikes (an ACWR proxy), and mesocycle-boundary crossings. An empty list means
        the swap looks safe.
        """
        warnings: List[str] = []
        if not swap_ops:
            return warnings

        # Resolve the workouts actually being moved.
        moves = []  # (workout, new_date)
        for op in swap_ops:
            w = self._db.get_workout_by_id(op['id'])
            if w:
                moves.append((w, op['new_date']))
        if not moves:
            return warnings

        affected = set()
        for w, new_date in moves:
            affected.add(w['date'])
            affected.add(new_date)

        # Pull a window wide enough to see the days surrounding the swap.
        dmin = datetime.strptime(min(affected), "%Y-%m-%d").date()
        dmax = datetime.strptime(max(affected), "%Y-%m-%d").date()
        win_start = (dmin - timedelta(days=7)).strftime("%Y-%m-%d")
        win_end = (dmax + timedelta(days=7)).strftime("%Y-%m-%d")
        existing = self._db.get_workouts(start_date=win_start, end_date=win_end)

        override = {op['id']: op['new_date'] for op in swap_ops}
        post = []
        for w in existing:
            wc = dict(w)
            if wc['id'] in override:
                wc['date'] = override[wc['id']]
            post.append(wc)

        # 1. Consecutive high-intensity days created by the swap.
        pre_high = {w['date'] for w in existing if self._is_high_intensity(w)}
        post_high = {w['date'] for w in post if self._is_high_intensity(w)}
        pre_max = max((len(r) for r in self._consecutive_runs(pre_high)), default=0)
        for run in self._consecutive_runs(post_high):
            if len(run) >= 3 and len(run) > pre_max and affected & set(run):
                warnings.append(
                    f"Creates {len(run)} consecutive high-intensity days "
                    f"({run[0]} to {run[-1]}); consider spacing hard sessions out."
                )
                break

        # 2. Weekly load spike (ACWR proxy). Only cross-week swaps shift weekly totals.
        pre_weekly: Dict[str, float] = {}
        post_weekly: Dict[str, float] = {}
        for w in existing:
            pre_weekly[self._week_of(w['date'])] = (
                pre_weekly.get(self._week_of(w['date']), 0.0) + (w.get('tss') or 0)
            )
        for w in post:
            post_weekly[self._week_of(w['date'])] = (
                post_weekly.get(self._week_of(w['date']), 0.0) + (w.get('tss') or 0)
            )
        for week in sorted(set(pre_weekly) | set(post_weekly)):
            before = pre_weekly.get(week, 0.0)
            after = post_weekly.get(week, 0.0)
            delta = after - before
            if before > 0 and delta > 0 and delta / before > 0.30 and delta >= 50:
                warnings.append(
                    f"Week of {week}: planned load rises {before:.0f} -> {after:.0f} "
                    f"TSS (+{delta / before * 100:.0f}%), which may spike your ACWR."
                )

        # 3. Mesocycle boundary crossings.
        for w, new_date in moves:
            if w['date'] == new_date:
                continue
            old_meso = self._db.get_active_mesocycle(w['date'])
            new_meso = self._db.get_active_mesocycle(new_date)
            old_id = old_meso['id'] if old_meso else None
            new_id = new_meso['id'] if new_meso else None
            if old_id != new_id:
                warnings.append(
                    f"Moving '{w['title']}' from {w['date']} to {new_date} crosses a "
                    f"mesocycle boundary; it may no longer match the block's focus."
                )

        return warnings

    def apply_swap(
        self, swap_ops: List[Dict[str, Any]], no_sync: bool = False
    ) -> List[Dict[str, Any]]:
        """Applies the date changes for a swap and syncs the moved workouts.

        Reads each workout's original date before moving it (ops reference distinct ids,
        so reads stay correct across the loop). Returns the updated workout records.
        """
        updated_workouts = []
        for op in swap_ops:
            workout = self._db.get_workout_by_id(op['id'])
            if not workout:
                continue
            reason = f"Swapped from {workout['date']} to {op['new_date']}"
            self._db.update_workout_date(op['id'], op['new_date'], reason)
            moved = self._db.get_workout_by_id(op['id'])
            if moved:
                updated_workouts.append(moved)

        if not no_sync:
            for moved in updated_workouts:
                try:
                    self._calendar_syncer.sync_workout(moved)
                except Exception as e:
                    print(f"Error syncing {moved['title']} to Google Calendar: {e}")

        return updated_workouts


    def analyze_workouts(
        self, from_date_str: Optional[str] = None, until_date_str: Optional[str] = None,
        days: Optional[int] = None, weeks: Optional[int] = None,
        context: Optional[str] = None, force: bool = False, inspect: bool = False
    ) -> Dict[str, Any]:
        """Analyzes historical workouts and physiological metrics using LLM.

        Backward-evaluation reuse (DESIGN_backward_evaluation.md §5, §8, §9):
        - The reconstruction is cached under the 'long' horizon, keyed by an evidence
          fingerprint. If the evidence is unchanged since the last run and `force` is
          False, the cached reconstruction is returned without an LLM call.
        - `force` bypasses *reuse* only (recompute even if unchanged); it never bypasses
          the reinforcement integrity invariant — a forced re-run over unchanged evidence
          still suppresses the confidence/recency ratchet.
        - `inspect` is read-only: it renders the reconstruction but writes neither coach
          learnings nor the cache.
        """
        until_date = _today_date()
        if until_date_str:
            until_date = datetime.strptime(until_date_str, "%Y-%m-%d").date()

        from_date = None
        if from_date_str:
            from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()
        elif days:
            from_date = until_date - timedelta(days=days - 1)
        elif weeks:
            from_date = until_date - timedelta(weeks=weeks) + timedelta(days=1)
        else:
            # Auto-timeline detection based on active goals
            earliest_goal = self._db.get_active_objective()
            if earliest_goal:
                target_date_str = earliest_goal['target_date']
                preceding = self._db.get_preceding_objectives(target_date_str)
                if preceding:
                    last_goal_date = datetime.strptime(
                        preceding[0]['target_date'], "%Y-%m-%d"
                    ).date()
                    from_date = last_goal_date + timedelta(days=1)
                else:
                    # No preceding goal. Assume the athlete was training for it.
                    # Default to 12 weeks lookback from today/until_date, capped at today
                    from_date = until_date - timedelta(weeks=12)
            else:
                # No active goals found. Default to 12 weeks lookback.
                from_date = until_date - timedelta(weeks=12)

        if from_date > until_date:
            raise ValueError(f"Start date {from_date} is after end date {until_date}.")

        from_str = from_date.strftime("%Y-%m-%d")
        until_str = until_date.strftime("%Y-%m-%d")

        print(f"Analyzing activities from {from_str} to {until_str}...")

        # Ensure Garmin data covers the analysis window (auto-pull recent/small gaps,
        # surface a command for large backfills) before reading it.
        from trainmate import garmin
        garmin.ensure_data(from_str, until_str)

        metrics = self._db.get_metrics_cache(start_date=from_str, end_date=until_str)
        completed_activities = self._db.get_completed_activities(
            start_date=from_str, end_date=until_str
        )

        # Reuse path: if the evidence is unchanged since the last analysis, return the
        # cached reconstruction instead of paying for another LLM pass (unless --force).
        fingerprint = self.engine._get_evidence_fingerprint(
            completed_activities, metrics, from_str, until_str
        )
        cached = self._db.get_analysis_cache("long")
        evidence_unchanged = bool(cached and cached.get("fingerprint") == fingerprint)
        if evidence_unchanged and not force and cached.get("reconstruction"):
            print("Evidence unchanged since last analysis; reusing cached reconstruction "
                  "(use --force to recompute).")
            return cached["reconstruction"]

        # Group by ISO week (Monday date string)
        weeks_data: Dict[str, Dict[str, Any]] = {}
        current_day = from_date
        while current_day <= until_date:
            monday = current_day - timedelta(days=current_day.weekday())
            monday_str = monday.strftime("%Y-%m-%d")

            if monday_str not in weeks_data:
                weeks_data[monday_str] = {
                    "days": [],
                    "metrics": [],
                    "activities": []
                }
            weeks_data[monday_str]["days"].append(current_day)
            current_day += timedelta(days=1)

        # Distribute metrics and activities into the weeks
        for m in metrics:
            m_date = datetime.strptime(m['date'], "%Y-%m-%d").date()
            monday = m_date - timedelta(days=m_date.weekday())
            monday_str = monday.strftime("%Y-%m-%d")
            if monday_str in weeks_data:
                weeks_data[monday_str]["metrics"].append(m)

        for act in completed_activities:
            act_date = datetime.strptime(act['date'], "%Y-%m-%d").date()
            monday = act_date - timedelta(days=act_date.weekday())
            monday_str = monday.strftime("%Y-%m-%d")
            if monday_str in weeks_data:
                weeks_data[monday_str]["activities"].append(act)

        # Build summaries per week
        weekly_summaries = []
        for monday_str in sorted(weeks_data.keys()):
            w_info = weeks_data[monday_str]
            days_in_week = w_info["days"]
            w_metrics = w_info["metrics"]
            w_activities = w_info["activities"]

            total_duration_hours = sum(
                (act.get('duration_sec') or 0.0) / 3600.0 for act in w_activities
            )
            total_tss = sum(activity_load(act) for act in w_activities)

            sports: Dict[str, int] = {}
            for act in w_activities:
                st = act['activity_type'].lower()
                sports[st] = sports.get(st, 0) + 1

            z1_z2_sec = sum(
                (act.get('zone1_sec') or 0) + (act.get('zone2_sec') or 0)
                for act in w_activities
            )
            z3_sec = sum(act.get('zone3_sec') or 0 for act in w_activities)
            z4_z5_sec = sum(
                (act.get('zone4_sec') or 0) + (act.get('zone5_sec') or 0)
                for act in w_activities
            )

            # Power zones (Garmin 7-zone model), grouped polarized like HR.
            pz1_pz2_sec = sum(
                (act.get('power_zone1_sec') or 0) + (act.get('power_zone2_sec') or 0)
                for act in w_activities
            )
            pz3_pz4_sec = sum(
                (act.get('power_zone3_sec') or 0) + (act.get('power_zone4_sec') or 0)
                for act in w_activities
            )
            pz5_pz7_sec = sum(
                (act.get('power_zone5_sec') or 0) + (act.get('power_zone6_sec') or 0)
                + (act.get('power_zone7_sec') or 0) for act in w_activities
            )

            avg_rpe = 0.0
            rpes = [act['rpe'] for act in w_activities if act.get('rpe') is not None]
            if rpes:
                avg_rpe = sum(rpes) / len(rpes)

            avg_rhr = None
            rhrs = [m['rhr'] for m in w_metrics if m.get('rhr') is not None]
            if rhrs:
                avg_rhr = sum(rhrs) / len(rhrs)

            avg_hrv = None
            hrvs = [m['hrv'] for m in w_metrics if m.get('hrv') is not None]
            if hrvs:
                avg_hrv = sum(hrvs) / len(hrvs)

            max_acwr = None
            acwrs = [m['acwr'] for m in w_metrics if m.get('acwr') is not None]
            if acwrs:
                max_acwr = max(acwrs)

            active_dates = {act['date'] for act in w_activities}
            rest_days = len(days_in_week) - len(active_dates)

            highlights = []
            for act in w_activities:
                is_hi = (
                    (act.get('tss') and act['tss'] >= 120) or
                    (act.get('rpe') and act['rpe'] >= 8) or
                    any(
                        kw in (act.get('activity_name') or "").lower()
                        for kw in ["race", "test", "ftp", "marathon"]
                    )
                )
                if is_hi:
                    highlights.append({
                        "date": act['date'],
                        "type": act['activity_type'],
                        "name": act.get('activity_name') or "Workout",
                        "duration_min": int((act.get('duration_sec') or 0.0) / 60.0),
                        "tss": act.get('tss'),
                        "rpe": act.get('rpe')
                    })

            weekly_summaries.append({
                "week_commencing": monday_str,
                "total_duration_hours": round(total_duration_hours, 1),
                "total_tss": round(total_tss, 1),
                "average_rpe": round(avg_rpe, 1) if avg_rpe > 0 else 0.0,
                "sports": sports,
                "zone_distribution_sec": {
                    "Z1_Z2": z1_z2_sec,
                    "Z3": z3_sec,
                    "Z4_Z5": z4_z5_sec
                },
                # Only emitted when some activity recorded power-zone data, so its
                # absence means "no power meter" rather than "no hard riding".
                "power_zone_distribution_sec": {
                    "Z1_Z2": pz1_pz2_sec,
                    "Z3_Z4": pz3_pz4_sec,
                    "Z5_Z7": pz5_pz7_sec
                } if (pz1_pz2_sec + pz3_pz4_sec + pz5_pz7_sec) > 0 else None,
                "avg_rhr": round(avg_rhr, 1) if avg_rhr is not None else None,
                "avg_hrv": round(avg_hrv, 1) if avg_hrv is not None else None,
                "max_acwr": round(max_acwr, 2) if max_acwr is not None else None,
                "rest_days": rest_days,
                "highlights": highlights
            })

        # Fetch relevant objectives (occurring on or after from_date)
        all_objectives = self._db.get_objectives()
        objectives = [
            obj for obj in all_objectives
            if datetime.strptime(obj['target_date'], "%Y-%m-%d").date() >= from_date
        ]

        guidelines = self._load_science_guidelines()
        profile = config.user_profile

        decision = self.engine._analyze_workouts_logic(
            objectives=objectives,
            guidelines=guidelines,
            profile=profile,
            weekly_summaries=weekly_summaries,
            learnings=self._get_learnings_text(),
            context=context
        )

        if not inspect:
            # Apply learning deltas. On a forced re-run over unchanged evidence, honour the
            # integrity invariant (§8): suppress the reinforcement ratchet so re-reading the
            # same data cannot inflate confidence or reset decay.
            self._apply_learning_updates(
                decision, suppress_reinforcement=evidence_unchanged
            )
            # Cache the reconstruction (everything but the point-in-time deltas) so future
            # runs — and `plan generate` — can reuse it without another LLM call.
            reconstruction = {
                k: v for k, v in decision.items() if k != "learning_updates"
            }
            self._db.save_analysis_cache(
                "long", fingerprint, from_str, until_str, reconstruction
            )

        return decision


# Singleton instance
coach_service = CoachService()

