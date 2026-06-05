import os
import json
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.db import db
from trainmate.openrouter import openrouter_client
from trainmate.google_calendar import calendar_syncer
from trainmate.types import Objective, LifeEvent, Workout, CompletedActivity
from trainmate.adherence import analyze_adherence


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
        act_load = (
            (act.get('tss') or 0.0)
            + (act.get('rpe') or 0) * (act['duration_sec'] / 3600.0)
        )
        completed_list.append(
            f"- {act['date']} ({act['activity_type'].upper()}): "
            f"'{act['activity_name']}' | "
            f"Duration: {act['duration_sec']/60:.0f}m, Avg HR: {act['avg_hr']}, "
            f"Load: {act_load:.1f}"
        )
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


class CoachRepository:
    """Handles I/O operations for the coach, database access, and calendar syncing."""

    def __init__(self, db_instance=None, calendar_syncer_instance=None):
        self._db_instance = db_instance
        self._calendar_syncer_instance = calendar_syncer_instance

    @property
    def _db(self):
        return self._db_instance or db

    @property
    def _calendar_syncer(self):
        return self._calendar_syncer_instance or calendar_syncer

    def _get_active_objectives(self) -> List[Objective]:
        return self._db.get_objectives(status='active')

    def _add_objective(
        self, title: str, target_date: str, sport_type: str,
        description: str = "", priority: int = 1, status: str = 'active'
    ) -> int:
        return self._db.add_objective(
            title=title,
            target_date=target_date,
            sport_type=sport_type,
            description=description,
            priority=priority,
            status=status
        )

    def _get_objective(self, obj_id: int) -> Optional[Objective]:
        return self._db.get_objective(obj_id)

    def _update_objective(self, obj_id: int, **kwargs: Any) -> None:
        self._db.update_objective(obj_id, **kwargs)

    def _update_lifeevent(self, lifeevent_id: int, **kwargs: Any) -> None:
        self._db.update_lifeevent(lifeevent_id, **kwargs)



    def _get_upcoming_lifeevents(self, today_str: str) -> List[LifeEvent]:
        return self._db.get_lifeevents(start_after=today_str)

    def _get_macrocycle_for_objective(self, objective_id: int) -> Optional[Dict[str, Any]]:
        return self._db.get_macrocycle_for_objective(objective_id)

    def _get_mesocycles_for_macrocycle(self, macrocycle_id: int) -> List[Dict[str, Any]]:
        return self._db.get_mesocycles_for_macrocycle(macrocycle_id)

    def _get_last_macrocycle(self) -> Optional[Dict[str, Any]]:
        return self._db.get_last_macrocycle()

    def _get_coach_memory(self, key: str) -> Optional[str]:
        return self._db.get_coach_memory(key)

    def _save_coach_memory(self, key: str, value: str) -> None:
        self._db.save_coach_memory(key, value)

    def _save_macrocycle(
        self, objective_id: int, strategy: str, goals_hash: str,
        lifeevents_hash: str, config_hash: str, mesocycles: List[Dict[str, Any]]
    ) -> int:
        return self._db.save_macrocycle(
            objective_id=objective_id,
            strategy=strategy,
            goals_hash=goals_hash,
            lifeevents_hash=lifeevents_hash,
            config_hash=config_hash,
            mesocycles=mesocycles
        )

    def _update_macrocycle_config_hash(self, macrocycle_id: int, config_hash: str) -> None:
        self._db.update_macrocycle_config_hash(macrocycle_id, config_hash)

    def _update_macrocycle_feedback(self, macro_id: int, feedback: str) -> None:
        self._db.update_macrocycle_feedback(macro_id, feedback)

    def _update_mesocycle_feedback(self, meso_id: int, feedback: str) -> None:
        self._db.update_mesocycle_feedback(meso_id, feedback)

    def _delete_macrocycle_for_objective(self, objective_id: int) -> None:
        self._db.delete_macrocycle_for_objective(objective_id)


    def _clear_future_workouts(self, today_str: str) -> None:
        self._db.clear_future_workouts(today_str)

    def _save_workout(
        self, date: str, sport_type: str, title: str, description: str,
        status: str, original_description: Optional[str] = None,
        modification_reason: Optional[str] = None, google_event_id: Optional[str] = None,
        duration_minutes: Optional[int] = None, rpe: Optional[int] = None,
        tss: Optional[float] = None
    ) -> int:
        return self._db.save_workout(
            date=date,
            sport_type=sport_type,
            title=title,
            description=description,
            status=status,
            original_description=original_description,
            modification_reason=modification_reason,
            google_event_id=google_event_id,
            duration_minutes=duration_minutes,
            rpe=rpe,
            tss=tss
        )

    def _get_workout(self, date: str, sport_type: str) -> Optional[Workout]:
        return self._db.get_workout(date, sport_type)

    def _get_workouts(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> List[Workout]:
        return self._db.get_workouts(start_date=start_date, end_date=end_date)

    def _get_completed_activities(
        self, start_date: str, end_date: str
    ) -> List[CompletedActivity]:
        return self._db.get_completed_activities(start_date=start_date, end_date=end_date)

    def _get_metrics_cache(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return self._db.get_metrics_cache(start_date=start_date, end_date=end_date)

    def _get_baseline(self, date: str) -> Optional[Dict[str, Any]]:
        return self._db.get_baseline(date)

    def _delete_workout_by_id(self, workout_id: int) -> None:
        self._db.delete_workout_by_id(workout_id)

    def _delete_workout_calendar_event(self, google_event_id: str) -> None:
        self._calendar_syncer.delete_workout_event(google_event_id)

    def _sync_workout_to_calendar(self, workout: Workout) -> None:
        self._calendar_syncer.sync_workout(workout)

    def _load_science_guidelines(self, app_science_dir: str, science_dir: str) -> str:
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
            current_year = datetime.now(timezone.utc).year
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

COACH MEMORY & ACTIVE PERIODIZATION STRATEGY:
- Established Training Strategy for the current macro-cycle:
{strategy}
- Mesocycles making up the macro-cycle:
{meso_text}
- Athlete-Specific Observations:
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
            'metrics_history_days': config.metrics_history_days
        }
        serialized = json.dumps(data_to_hash, sort_keys=True)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _generate_macrocycle_strategy(
        self, next_goal: Objective, objectives: List[Objective],
        lifeevents: List[LifeEvent], today_str: str, guidelines: str,
        profile: Optional[Dict[str, Any]], previous_strategy_text: Optional[str] = None,
        plan_start_str: Optional[str] = None, athlete_feedback: Optional[str] = None,
        history_summary: Optional[str] = None
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
        metrics: Optional[List[Dict[str, Any]]] = None,
        completed_activities: Optional[List[CompletedActivity]] = None,
        baseline: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Queries LLM to generate the 4-week workouts based on active strategy details."""
        custom_task = """
TASK:
Generate a training schedule for the next 4 weeks (28 days) starting from today. 
Ensure the weekly schedules/microcycles are designed specifically to match the focus, target
volume, and intensity of the active mesocycle block(s) the athlete is in during this period, and
incorporate any deload weeks or exceptions for upcoming life events in accordance with the
science guidelines.

You MUST respond with a JSON object containing:
{
  "reasoning": "Explain the microcycle design, detailing how workouts align with the active
    mesocycle focus.",
  "athlete_learnings": "Update athlete observations text blob based on metrics or status
    if any.",
  "workouts": [
    {
      "date": "YYYY-MM-DD",
      "sport_type": "running" | "road_biking" | "hiking" | "strength_training" | "yoga" |
        "ski_touring" | "rest",
      "title": "Workout Title (e.g., Tempo Run, Long Ride, Rest Day)",
      "description": "Detailed description of intensity, duration, heart rate zones, and
        goals.",
      "duration_minutes": 60, (Estimated workout duration in minutes, integer. Use 0 for rest days)
      "rpe": 6, (Expected Rate of Perceived Exertion, integer 1-10. Use 0 for rest days)
      "tss": 45.0 (Expected Training Stress Score, float/integer. Use 0 for rest days)
    }
  ]
}
"""
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
            f"Please generate the 4-week microcycles (workouts) starting today."
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

You MUST respond with a JSON object containing:
{{
  "change_needed": true | false,
  "reason": "Swapping tempo run to rest.",
  "adapted_workouts": [
    {{
      "date": "YYYY-MM-DD",
      "sport_type": "running" | "road_biking" | "hiking" | "strength_training" |
        "yoga" | "ski_touring" | "rest",
      "title": "Adapted Workout Title",
      "description": "Adapted description of intensity, duration, heart rate zones, and goals.",
      "duration_minutes": 45,
      "rpe": 5,
      "tss": 30.0
    }}
  ]
}}
"""
        # Prepare system prompt
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

        # Build user content containing trajectory metrics and discrepancies
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


class CoachService:
    """Orchestrates sports science coaching by coordinating data I/O and business logic."""

    def __init__(self, repository: CoachRepository, engine: CoachEngine):
        self.repository = repository
        self.engine = engine

    def _get_recent_history_summary(self, today_str: str) -> str:
        """Retrieves and constructs a summary of the past 15 days of workouts/metrics."""
        today_date = datetime.strptime(today_str, "%Y-%m-%d").date()
        start_date_obj = today_date - timedelta(days=14)
        start_date_str = start_date_obj.strftime("%Y-%m-%d")

        metrics = self.repository._get_metrics_cache(
            start_date=start_date_str, end_date=today_str
        )
        completed_activities = self.repository._get_completed_activities(
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

    def _get_config_hash(self) -> str:
        return self.engine._get_config_hash()

    def _get_goals_hash(self, objectives: List[Objective]) -> str:
        return self.engine._get_goals_hash(objectives)

    def _get_lifeevents_hash(self, lifeevents: List[LifeEvent]) -> str:
        return self.engine._get_lifeevents_hash(lifeevents)

    def _load_science_guidelines(self) -> str:
        return self.repository._load_science_guidelines(
            config.app_science_dir, config.science_dir
        )

    def _get_active_strategy_and_meso_text(
        self, objectives: List[Objective], objective_id: Optional[int] = None
    ) -> Tuple[str, str]:
        strategy = None
        meso_text = ""
        if objectives:
            if objective_id is not None:
                target_goals = [o for o in objectives if o['id'] == objective_id]
                next_goal = target_goals[0] if target_goals else None
            else:
                sorted_objs = sorted(objectives, key=lambda x: str(x['target_date']))
                next_goal = sorted_objs[0] if sorted_objs else None

            if next_goal and next_goal['id'] is not None:
                macrocycle = self.repository._get_macrocycle_for_objective(next_goal['id'])
                if macrocycle:
                    strategy = macrocycle['strategy']
                    mesocycles = self.repository._get_mesocycles_for_macrocycle(macrocycle['id'])
                    for m in mesocycles:
                        meso_text += (
                            f"  - {m['name']} ({m['start_date']} to "
                            f"{m['end_date']}): {m['focus']}\n"
                        )
        if not strategy:
            strategy = self.repository._get_coach_memory("training_strategy") or (
                "Not established yet. Establish an endurance-focused training strategy "
                "based on goals."
            )
            meso_text = "  - Not established yet."
        return strategy, meso_text

    def _get_coach_system_prompt(
        self, objectives: List[Objective], lifeevents: List[LifeEvent],
        custom_task: str = "", objective_id: Optional[int] = None
    ) -> str:
        guidelines = self._load_science_guidelines()
        strategy, meso_text = self._get_active_strategy_and_meso_text(
            objectives, objective_id=objective_id
        )
        learnings = self.repository._get_coach_memory("athlete_learnings") or (
            "No observations yet. Over time, observe the athlete's responses to "
            "training volume and intensity."
        )
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
        self.repository._delete_macrocycle_for_objective(objective_id)

    def generate_periodization_plan(
        self, force: bool = False, objective_id: Optional[int] = None
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Determines the macrocycle strategy and mesocycle blocks."""
        objectives = self.repository._get_active_objectives()
        if not objectives:
            return "No active goals found. TrainMate needs at least one objective.", []

        # Identify the target goal
        if objective_id is not None:
            target_goals = [o for o in objectives if o['id'] == objective_id]
            if not target_goals:
                all_goals = self.repository._get_objective(objective_id)
                if all_goals:
                    next_goal = all_goals
                else:
                    raise ValueError(f"Goal with ID {objective_id} not found.")
            else:
                next_goal = target_goals[0]
        else:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]

        # Get future life events
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_date = datetime.strptime(today_str, "%Y-%m-%d").date()

        # Determine plan start date based on preceding goals with plans
        plan_start_date = today_date
        preceding_objs = [
            o for o in objectives
            if str(o['target_date']) < str(next_goal['target_date'])
        ]
        if preceding_objs:
            latest_preceding_target = None
            for po in preceding_objs:
                if po['id'] is not None:
                    po_macro = self.repository._get_macrocycle_for_objective(po['id'])
                    if po_macro:
                        po_target = datetime.strptime(po['target_date'], "%Y-%m-%d").date()
                        if (latest_preceding_target is None or
                                po_target > latest_preceding_target):
                            latest_preceding_target = po_target
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
                self.repository._add_objective(
                    title=pg['title'],
                    target_date=pg['target_date'],
                    sport_type=pg['sport_type'],
                    description=pg.get('description', ''),
                    priority=pg.get('priority', next_goal['priority']),
                    status='active'
                )

            # Re-fetch active objectives and update next_goal
            objectives = self.repository._get_active_objectives()
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]

        lifeevents = self.repository._get_upcoming_lifeevents(today_str)

        # Compute current hashes
        goals_hash = self.engine._get_goals_hash(objectives)
        lifeevents_hash = self.engine._get_lifeevents_hash(lifeevents)
        config_hash = self.engine._get_config_hash()

        # Try to retrieve existing macrocycle
        strategy = ""
        mesocycles: List[Dict[str, Any]] = []
        existing_macro = None
        if next_goal['id'] is not None:
            existing_macro = self.repository._get_macrocycle_for_objective(next_goal['id'])

        reused = False
        if existing_macro and not force:
            if (
                existing_macro['goals_hash'] == goals_hash
                and existing_macro['lifeevents_hash'] == lifeevents_hash
                and existing_macro.get('config_hash') == config_hash
            ):
                reused = True
                strategy = existing_macro['strategy']
                mesocycles = self.repository._get_mesocycles_for_macrocycle(existing_macro['id'])
                print("Reusing existing periodization strategy (macrocycle and mesocycles) "
                      "from database.")

        if not reused:
            # Get the previous strategy for context
            prev_macro = existing_macro
            if not prev_macro:
                prev_macro = self.repository._get_last_macrocycle()

            prev_strategy_text = None
            if prev_macro:
                prev_mesos = self.repository._get_mesocycles_for_macrocycle(prev_macro['id'])
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
                existing_mesos = self.repository._get_mesocycles_for_macrocycle(
                    existing_macro['id']
                )
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
                history_summary=history_summary
            )
            strategy = macro_data.get("strategy", "Endurance preparation strategy.")
            mesocycles = macro_data.get("mesocycles", [])

            # Save it
            if next_goal['id'] is not None:
                self.repository._save_macrocycle(
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
        self, objective_id: Optional[int] = None
    ) -> Tuple[str, List[Workout]]:
        """Generates the 4-week workouts (microcycles) based on the active strategy."""
        objectives = self.repository._get_active_objectives()
        if not objectives:
            return "No active goals found. TrainMate needs at least one objective.", []

        # Identify the target goal
        if objective_id is not None:
            target_goals = [o for o in objectives if o['id'] == objective_id]
            if not target_goals:
                raise ValueError(f"Active goal with ID {objective_id} not found.")
            next_goal = target_goals[0]
        else:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]

        # Verify active periodization strategy exists
        macrocycle = self.repository._get_macrocycle_for_objective(next_goal['id'])
        if not macrocycle:
            raise ValueError(
                "No active periodization strategy found. "
                "Please generate a periodization plan first."
            )

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lifeevents = self.repository._get_upcoming_lifeevents(today_str)
        guidelines = self._load_science_guidelines()
        profile = config.user_profile
        strategy, meso_text = self._get_active_strategy_and_meso_text(
            objectives, objective_id=objective_id
        )
        learnings = self.repository._get_coach_memory("athlete_learnings") or (
            "No observations yet. Over time, observe the athlete's responses to "
            "training volume and intensity."
        )

        # Retrieve recent history context
        history_days = config.metrics_history_days
        today_date_obj = datetime.strptime(today_str, "%Y-%m-%d").date()
        start_date_obj = today_date_obj - timedelta(days=history_days - 1)
        start_date_str = start_date_obj.strftime("%Y-%m-%d")

        metrics = self.repository._get_metrics_cache(
            start_date=start_date_str, end_date=today_str
        )
        completed_activities = self.repository._get_completed_activities(
            start_date=start_date_str, end_date=today_str
        )
        baseline = self.repository._get_baseline(today_str)

        plan_data = self.engine._generate_workouts_logic(
            objectives=objectives,
            lifeevents=lifeevents,
            today_str=today_str,
            guidelines=guidelines,
            profile=profile,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            metrics=metrics,
            completed_activities=completed_activities,
            baseline=baseline
        )

        # Save learnings to memory
        if "athlete_learnings" in plan_data:
            self.repository._save_coach_memory(
                "athlete_learnings", plan_data["athlete_learnings"]
            )

        # Save workouts to database
        workouts = plan_data.get("workouts", [])

        # Clear future unsynced workouts to prevent overlapping plans
        self.repository._clear_future_workouts(today_str)

        saved_workouts: List[Workout] = []
        for w in workouts:
            wid = self.repository._save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                status='planned',
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
                'status': 'planned',
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
        objectives = self.repository._get_active_objectives()
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
            target_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        target_date_obj = datetime.strptime(target_date_str, "%Y-%m-%d").date()

        # Fetch metrics history window
        history_days = config.metrics_history_days
        start_date_obj = target_date_obj - timedelta(days=history_days - 1)
        start_date_str = start_date_obj.strftime("%Y-%m-%d")

        # Fetch metrics and baselines in window via repository
        metrics = self.repository._get_metrics_cache(
            start_date=start_date_str, end_date=target_date_str
        )
        completed_activities = self.repository._get_completed_activities(
            start_date=start_date_str, end_date=target_date_str
        )
        planned_workouts = self.repository._get_workouts(
            start_date=start_date_str, end_date=target_date_str
        )

        # Retrieve baseline for reference
        baseline = self.repository._get_baseline(target_date_str)
        if not baseline:
            baseline_str = "No baseline data available."
        else:
            baseline_str = (
                f"Resting HR: Mean = {baseline['rhr_baseline_mean']:.1f}, "
                f"StdDev = {baseline['rhr_baseline_std']:.2f}\n"
                f"HRV: Mean = {baseline['hrv_baseline_mean']:.1f}, "
                f"StdDev = {baseline['hrv_baseline_std']:.2f}\n"
                f"Sleep Score: Mean = {baseline['sleep_baseline_mean']:.1f}, "
                f"StdDev = {baseline['sleep_baseline_std']:.2f}"
            )

        # Match planned workouts vs completed activities and compute discrepancies
        discrepancies, matching_results = analyze_adherence(
            planned_workouts=planned_workouts,
            completed_activities=completed_activities,
            start_date_obj=start_date_obj,
            history_days=history_days
        )

        # Determine mesocycle end date for adaptation range
        meso_end_date_str = (target_date_obj + timedelta(days=6)).strftime("%Y-%m-%d")
        active_meso = None
        next_goal = None
        objectives = self.repository._get_active_objectives()
        for obj in objectives:
            if obj['id'] is not None:
                macro = self.repository._get_macrocycle_for_objective(obj['id'])
                if macro:
                    mesos = self.repository._get_mesocycles_for_macrocycle(macro['id'])
                    for m in mesos:
                        start = datetime.strptime(m['start_date'], "%Y-%m-%d").date()
                        end = datetime.strptime(m['end_date'], "%Y-%m-%d").date()
                        if start <= target_date_obj <= end:
                            active_meso = m
                            meso_end_date_str = m['end_date']
                            next_goal = obj
                            break
            if active_meso:
                break

        # Fallback to objectives[0] if no active mesocycle covers target_date_obj
        if not next_goal and objectives:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]

        guidelines = self._load_science_guidelines()
        profile = config.user_profile
        objective_id = next_goal['id'] if next_goal else None
        strategy, meso_text = self._get_active_strategy_and_meso_text(
            objectives, objective_id=objective_id
        )
        learnings = self.repository._get_coach_memory("athlete_learnings") or (
            "No observations yet. Over time, observe the athlete's responses to "
            "training volume and intensity."
        )
        lifeevents = self.repository._get_upcoming_lifeevents(target_date_str)

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

        # Update memories if present
        if "training_strategy" in decision and decision["training_strategy"]:
            self.repository._save_coach_memory(
                "training_strategy", decision["training_strategy"]
            )
        if "athlete_learnings" in decision and decision["athlete_learnings"]:
            self.repository._save_coach_memory(
                "athlete_learnings", decision["athlete_learnings"]
            )

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
                'status': 'planned',
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
        existing_workouts = self.repository._get_workouts(
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
                    if ew.get('google_event_id') and ew['status'] == 'synced':
                        try:
                            self.repository._delete_workout_calendar_event(ew['google_event_id'])
                        except Exception as e:
                            print(f"Error deleting Google Calendar event: {e}")
                    self.repository._delete_workout_by_id(ew['id'])

        # 3. Save new adapted workouts and sync them
        for w in proposed_workouts:
            existing = self.repository._get_workout(w['date'], w['sport_type'])
            orig_desc = None
            ge_id = None
            if existing:
                orig_desc = existing['original_description'] or existing['description']
                ge_id = existing['google_event_id']

            self.repository._save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                original_description=orig_desc or w['description'],
                status='modified',
                modification_reason=reason,
                google_event_id=ge_id,
                duration_minutes=w.get('duration_minutes'),
                rpe=w.get('rpe'),
                tss=w.get('tss')
            )

            # Sync to Google Calendar
            updated = self.repository._get_workout(w['date'], w['sport_type'])
            if updated:
                try:
                    self.repository._sync_workout_to_calendar(updated)
                except Exception as e:
                    print(f"Error syncing {w['title']} to Google Calendar: {e}")


# Singleton instance matching original variable name for integration compatibility
coach_repository = CoachRepository()
coach_engine = CoachService(repository=coach_repository, engine=CoachEngine())
