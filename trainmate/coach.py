import os
import json
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.db import db
from trainmate.openrouter import openrouter_client
from trainmate.google_calendar import calendar_syncer
from trainmate.types import Objective, Constraint, Workout, CompletedActivity

class CoachEngine:
    """Orchestrates sports science coaching, macro/meso planning, and daily adaptations."""

    def _load_science_guidelines(self) -> str:
        """Loads and concatenates all text files in the science directory.

        Returns:
            A string containing all training science guidelines.
        """
        science_dir = config.science_dir
        texts = []
        if os.path.exists(science_dir):
            for filename in sorted(os.listdir(science_dir)):
                if filename.endswith(".txt"):
                    filepath = os.path.join(science_dir, filename)
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            texts.append(f"=== Guidelines from {filename} ===\n" + f.read())
                    except Exception as e:
                        print(f"Error reading science guideline {filename}: {e}")
        return "\n\n".join(texts)

    def _format_athlete_profile(self) -> str:
        """Formats the athlete's user profile (from config) into a readable prompt segment."""
        profile = config.user_profile
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
                lines.append(f"- General Equipment (always available): {', '.join(general_equipment)}")
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
                    hours = day_data.get("available_hours", 0.0)
                    cert = day_data.get("certainty_percent", 100)
                    equip = day_data.get("equipment", [])
                    equip_str = f" (Equipment: {', '.join(equip)})" if equip else ""
                    lines.append(f"  * {day}: {hours} hours | Certainty: {cert}%{equip_str}")
                else:
                    lines.append(f"  * {day}: {day_data} hours")

        return "\n".join(lines)

    def _get_coach_system_prompt(
        self, objectives: List[Objective], constraints: List[Constraint],
        custom_task: str = ""
    ) -> str:
        """Constructs the static prefix system prompt.

        Includes guidelines, goals, and coach memory.

        Args:
            objectives: List of active athlete objectives.
            constraints: List of upcoming logged constraints.
            custom_task: Specific task context to append.

        Returns:
            The constructed system prompt string.
        """
        science_guidelines = self._load_science_guidelines()
        
        # Load active macrocycle and mesocycles if available
        strategy = None
        meso_text = ""
        if objectives:
            # Sort objectives to find the next goal
            sorted_objs = sorted(objectives, key=lambda x: str(x['target_date']))
            next_goal = sorted_objs[0]
            if next_goal['id'] is not None:
                macrocycle = db.get_macrocycle_for_objective(next_goal['id'])
                if macrocycle:
                    strategy = macrocycle['strategy']
                    mesocycles = db.get_mesocycles_for_macrocycle(macrocycle['id'])
                    for m in mesocycles:
                        meso_text += (
                            f"  - {m['name']} ({m['start_date']} to "
                            f"{m['end_date']}): {m['focus']}\n"
                        )

        if not strategy:
            strategy = db.get_coach_memory("training_strategy") or (
                "Not established yet. Establish an endurance-focused training strategy "
                "based on goals."
            )
            meso_text = "  - Not established yet."

        learnings = db.get_coach_memory("athlete_learnings") or (
            "No observations yet. Over time, observe the athlete's responses to "
            "training volume and intensity."
        )

        # Serialize objectives
        obj_text = ""
        for o in objectives:
            details = o.get('description', '')
            obj_text += (
                f"- Goal: {o['title']} | Date: {o['target_date']} | "
                f"Sport: {o['sport_type']} | Details: {details}\n"
            )

        # Serialize constraints
        c_text = ""
        for c in constraints:
            impact = c.get('impact_description', '')
            c_text += (
                f"- Constraint: {c['title']} | Start: {c['start_date']} | "
                f"End: {c['end_date']} | Type: {c['event_type']} | Impact: {impact}\n"
            )

        athlete_profile = self._format_athlete_profile()
        system_prompt = f"""You are TrainMate Coach, an advanced AI sports science training coach.
You design and adapt personalized training plans for endurance athletes using sports science
principles.

COACHING ROLE AND OBJECTIVES:
1. Design periodized training plans (macro, meso, micro cycles) leading up to the target goals.
2. Focus scheduling on the NEXT CHRONOLOGICAL GOAL only. If there are multiple goals, identify
   synergies between them (e.g. general base or strength building phases).
3. Dynamically adjust training plans based on recent Garmin metrics (Resting HR, HRV, Sleep,
   ACWR) to optimize recovery and prevent injury.
4. Shift or scale training volume and intensity around constraints (injury, vacation, parties)
   to manage fatigue.
5. Adhere to the day-by-day weekly availability schedule and day-dependent equipment access
   (e.g., do not schedule gym workouts on home-only days; do not schedule workouts on rest days;
   do not exceed daily availability). Respect certainty percentages (higher values indicate more
   rigid constraints; lower values allow flexibility).

SPORTS SCIENCE GUIDELINES:
{science_guidelines}

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

UPCOMING CONSTRAINTS (LIFE EVENTS):
{c_text if c_text else "No upcoming constraints."}

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

    def _get_constraints_hash(self, constraints: List[Constraint]) -> str:
        """Computes a hash representation of constraints list to check for updates."""
        cleaned = []
        for c in constraints:
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

    def _generate_macrocycle_strategy(
        self, next_goal: Objective, objectives: List[Objective],
        constraints: List[Constraint], today_str: str,
        previous_strategy_text: Optional[str] = None
    ) -> Dict[str, Any]:
        """Queries LLM to determine the overall macrocycle strategy and mesocycle blocks."""
        custom_task = f"""
TASK:
Determine the overall periodization strategy (macrocycle) from today ({today_str}) until the next
chronological goal ({next_goal['target_date']}).
Divide this timeframe into contiguous, sequential mesocycles (typically blocks of 3-4 weeks,
though the final peak/taper/race block or very short periods can be shorter).
Make sure there are no gaps between the end date of one mesocycle and the start date of the next.
The first mesocycle must start on today's date ({today_str}) and the last mesocycle must end on or
around the goal date ({next_goal['target_date']}).
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
        science_guidelines = self._load_science_guidelines()
        
        obj_text = ""
        for o in objectives:
            details = o.get('description', '')
            obj_text += (
                f"- Goal: {o['title']} | Date: {o['target_date']} | "
                f"Sport: {o['sport_type']} | Details: {details}\n"
            )

        c_text = ""
        for c in constraints:
            impact = c.get('impact_description', '')
            c_text += (
                f"- Constraint: {c['title']} | Start: {c['start_date']} | "
                f"End: {c['end_date']} | Type: {c['event_type']} | Impact: {impact}\n"
            )

        athlete_profile = self._format_athlete_profile()
        system_prompt = (
            "You are TrainMate Coach, an advanced AI sports science training coach.\n"
            "You design periodized training plans (macro, meso, micro cycles) leading up "
            "to target goals.\n\n"
            f"SPORTS SCIENCE GUIDELINES:\n{science_guidelines}\n"
        )
        
        if previous_strategy_text:
            system_prompt += f"\n{previous_strategy_text}\n"
            
        system_prompt += (
            f"\nATHLETE PROFILE & PREFERENCES:\n{athlete_profile}\n"
            f"\nACTIVE ATHLETE GOALS (CHRONOLOGICAL):\n"
            f"{obj_text if obj_text else 'No active goals.'}\n\n"
            f"UPCOMING CONSTRAINTS (LIFE EVENTS):\n"
            f"{c_text if c_text else 'No upcoming constraints.'}\n\n"
            f"{custom_task}\n"
        )

        user_content = (
            f"Today's date is {today_str}. The next chronological goal is "
            f"'{next_goal['title']}' on {next_goal['target_date']}. "
            f"Please determine the macrocycle and mesocycle blocks starting from {today_str}."
        )

        print("Querying OpenRouter to generate macrocycle and mesocycles periodization strategy...")
        result = openrouter_client.complete(system_prompt, user_content)
        return result

    def generate_periodization_plan(self, force: bool = False) -> Tuple[str, List[Dict[str, Any]]]:
        """Determines the macrocycle strategy and mesocycle blocks.

        Args:
            force: Force regeneration of the periodization plan.

        Returns:
            A tuple of (strategy text, list of mesocycles).
        """
        objectives = db.get_objectives(status='active')
        if not objectives:
            return "No active goals found. TrainMate needs at least one objective.", []

        # Sort objectives by target date to identify the next goal
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]

        # Get future constraints
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        constraints = db.get_constraints(start_after=today_str)

        # Compute current hashes
        goals_hash = self._get_goals_hash(objectives)
        constraints_hash = self._get_constraints_hash(constraints)

        # Try to retrieve existing macrocycle
        strategy = ""
        mesocycles: List[Dict[str, Any]] = []
        existing_macro = None
        if next_goal['id'] is not None:
            existing_macro = db.get_macrocycle_for_objective(next_goal['id'])

        reused = False
        if existing_macro and not force:
            if (
                existing_macro['goals_hash'] == goals_hash
                and existing_macro['constraints_hash'] == constraints_hash
            ):
                reused = True
                strategy = existing_macro['strategy']
                mesocycles = db.get_mesocycles_for_macrocycle(existing_macro['id'])  # type: ignore
                print("Reusing existing periodization strategy (macrocycle and mesocycles) "
                      "from database.")

        if not reused:
            # Get the previous strategy for context
            prev_macro = existing_macro
            if not prev_macro:
                prev_macro = db.get_last_macrocycle()

            prev_strategy_text = None
            if prev_macro:
                prev_mesos = db.get_mesocycles_for_macrocycle(prev_macro['id'])
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

            # Generate new macrocycle strategy and mesocycles
            print("Goals or constraints have changed, or force generation requested. "
                  "Determining new overall periodization strategy...")
            macro_data = self._generate_macrocycle_strategy(
                next_goal,
                objectives,
                constraints,
                today_str,
                previous_strategy_text=prev_strategy_text
            )
            strategy = macro_data.get("strategy", "Endurance preparation strategy.")
            mesocycles = macro_data.get("mesocycles", [])

            # Save it
            if next_goal['id'] is not None:
                db.save_macrocycle(
                    objective_id=next_goal['id'],
                    strategy=strategy,
                    goals_hash=goals_hash,
                    constraints_hash=constraints_hash,
                    mesocycles=mesocycles
                )
            print("\n=== NEW PERIODIZATION STRATEGY (MACROCYCLE) ===")
            print(f"Overall Strategy:\n{strategy}\n")
            print("Mesocycle Blocks:")
            for m in mesocycles:
                print(f"- {m['name']} ({m['start_date']} to {m['end_date']}): {m['focus']}")
            print("==============================================\n")

        return strategy, mesocycles

    def generate_workouts(self) -> Tuple[str, List[Workout]]:
        """Generates the 4-week workouts (microcycles) based on the active strategy.

        Returns:
            A tuple of (reasoning text, list of generated workouts).
        """
        objectives = db.get_objectives(status='active')
        if not objectives:
            return "No active goals found. TrainMate needs at least one objective.", []

        # Sort objectives by target date to identify the next goal
        objectives.sort(key=lambda x: str(x['target_date']))
        next_goal = objectives[0]

        # Verify active periodization strategy exists
        macrocycle = db.get_macrocycle_for_objective(next_goal['id'])
        if not macrocycle:
            raise ValueError(
                "No active periodization strategy found. "
                "Please generate a periodization plan first."
            )

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        constraints = db.get_constraints(start_after=today_str)

        custom_task = """
TASK:
Generate a training schedule for the next 4 weeks (28 days) starting from today. 
Ensure the weekly schedules/microcycles are designed specifically to match the focus, target
volume, and intensity of the active mesocycle block(s) the athlete is in during this period.
Incorporate deload weeks and schedule around constraints (injury = rest/cross-training,
vacation = maintain fitness, party = easy workouts next day).

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
        system_prompt = self._get_coach_system_prompt(objectives, constraints, custom_task)
        user_content = (
            f"Today's date is {today_str}. "
            "Please generate the 4-week microcycles (workouts) starting today."
        )

        print("Querying OpenRouter to generate training workouts (microcycles)...")
        plan_data = openrouter_client.complete(system_prompt, user_content)

        # Save learnings to memory
        if "athlete_learnings" in plan_data:
            db.save_coach_memory("athlete_learnings", plan_data["athlete_learnings"])

        # Save workouts to database
        workouts = plan_data.get("workouts", [])

        # Clear future unsynced workouts to prevent overlapping plans
        db.clear_future_workouts(today_str)

        saved_workouts: List[Workout] = []
        for w in workouts:
            wid = db.save_workout(
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

    def replan(self, force: bool = False) -> Tuple[str, List[Workout]]:
        """Generates or adapts the training plan from today onwards.

        Args:
            force: Force regeneration of macro/meso plan.

        Returns:
            A tuple of (reasoning string, list of generated Workouts).
        """
        objectives = db.get_objectives(status='active')
        if not objectives:
            return (
                "No active goals found. TrainMate needs at least one objective to "
                "start planning.",
                []
            )

        self.generate_periodization_plan(force=force)
        return self.generate_workouts()

    def adapt(self, target_date_str: Optional[str] = None) -> Tuple[str, List[Workout]]:
        """Evaluates metrics/activities over a rolling window and adapts mesocycle if needed."""
        if not target_date_str:
            target_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        target_date_obj = datetime.strptime(target_date_str, "%Y-%m-%d").date()

        # 1. Fetch metrics history window
        history_days = config.metrics_history_days
        start_date_obj = target_date_obj - timedelta(days=history_days - 1)
        start_date_str = start_date_obj.strftime("%Y-%m-%d")

        # Fetch metrics and baselines in window
        metrics = db.get_metrics_cache(start_date=start_date_str, end_date=target_date_str)
        completed_activities = db.get_completed_activities(
            start_date=start_date_str, end_date=target_date_str
        )
        planned_workouts = db.get_workouts(start_date=start_date_str, end_date=target_date_str)

        # Retrieve baseline for reference
        baseline = db.get_baseline(target_date_str)
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

        # 2. Match planned workouts vs completed activities and compute discrepancies
        SPORT_MAPPING = {
            "running": ["running", "indoor_running", "trail_running", "treadmill_running"],
            "road_biking": [
                "road_biking", "indoor_cycling", "cycling", "virtual_cycling", "biking"
            ],
            "hiking": ["hiking", "walking"],
            "strength_training": ["strength_training", "strength", "indoor_cardio", "fitness"],
            "yoga": ["yoga", "stretching", "pilates"],
            "ski_touring": ["ski_touring", "backcountry_skiing", "nordic_skiing", "skiing"]
        }

        matching_results = []
        discrepancies = []
        
        # Group by date
        activities_by_date: Dict[str, List[Any]] = {}
        for act in completed_activities:
            activities_by_date.setdefault(act['date'], []).append(act)

        workouts_by_date: Dict[str, List[Any]] = {}
        for w in planned_workouts:
            workouts_by_date.setdefault(w['date'], []).append(w)

        # Process each day in the window
        for d in range(history_days):
            date_curr = (start_date_obj + timedelta(days=d)).strftime("%Y-%m-%d")
            day_acts = activities_by_date.get(date_curr, [])
            day_workouts = workouts_by_date.get(date_curr, [])

            # Sort activities by workload descending
            day_acts = sorted(
                day_acts,
                key=lambda x: (
                    (x.get('tss') or 0.0)
                    + (x.get('rpe') or 0) * ((x.get('duration_sec') or 0.0) / 3600.0)
                ),
                reverse=True
            )

            used_act_ids = set()

            for w in day_workouts:
                w_sport = w['sport_type']
                matched_act = None

                if w_sport == "rest":
                    # Check for rest day violation: any activity with significant workload
                    for act in day_acts:
                        act_load = (
                            (act.get('tss') or 0.0)
                            + (act.get('rpe') or 0) * (act['duration_sec'] / 3600.0)
                        )
                        if act_load > 10.0 and act['activity_id'] not in used_act_ids:
                            matched_act = act
                            used_act_ids.add(act['activity_id'])
                            discrepancies.append(
                                f"- {date_curr}: Rest Day Violation! Performed "
                                f"'{act['activity_name']}' ({act['activity_type']}) with "
                                f"workload {act_load:.1f} when Rest was planned."
                            )
                            break
                else:
                    # Find a matching completed activity
                    allowed_types = SPORT_MAPPING.get(w_sport, [w_sport])
                    for act in day_acts:
                        if act['activity_id'] in used_act_ids:
                            continue
                        act_type = act['activity_type'].lower()
                        if act_type in allowed_types or any(t in act_type for t in allowed_types):
                            matched_act = act
                            used_act_ids.add(act['activity_id'])
                            break

                    if matched_act:
                        act_duration_min = matched_act['duration_sec'] / 60.0
                        act_load = (
                            (matched_act.get('tss') or 0.0)
                            + (matched_act.get('rpe') or 0) * (matched_act['duration_sec'] / 3600.0)
                        )
                        
                        p_duration = w.get('duration_minutes') or 0
                        p_rpe = w.get('rpe') or 0
                        p_tss = w.get('tss') or 0
                        exp_load = p_tss + p_rpe * (p_duration / 60.0)

                        disc_reasons = []
                        if p_duration > 0 and (abs(act_duration_min - p_duration) / p_duration) > 0.30:
                            disc_reasons.append(
                                f"duration mismatch +/-30% (planned {p_duration:.0f}m, "
                                f"actual {act_duration_min:.0f}m)"
                            )
                        
                        if exp_load > 0 and (abs(act_load - exp_load) / exp_load) > 0.30:
                            disc_reasons.append(
                                f"workload mismatch +/-30% (planned load {exp_load:.1f}, "
                                f"actual load {act_load:.1f})"
                            )

                        if disc_reasons:
                            discrepancies.append(
                                f"- {date_curr}: Discrepancy in '{w['title']}' vs "
                                f"'{matched_act['activity_name']}': {', '.join(disc_reasons)}."
                            )
                    else:
                        # Complete miss
                        discrepancies.append(
                            f"- {date_curr}: Complete Miss! Missed planned workout '{w['title']}' "
                            f"({w['sport_type']})."
                        )

                matching_results.append({
                    "date": date_curr,
                    "planned": w,
                    "completed": matched_act
                })

            # Check for completed activities when nothing was planned
            for act in day_acts:
                if act['activity_id'] not in used_act_ids:
                    act_load = (
                        (act.get('tss') or 0.0)
                        + (act.get('rpe') or 0) * (act['duration_sec'] / 3600.0)
                    )
                    if act_load > 10.0:
                        discrepancies.append(
                            f"- {date_curr}: Unplanned Activity! Performed "
                            f"'{act['activity_name']}' ({act['activity_type']}) with "
                            f"workload {act_load:.1f} on a day with no planned workouts."
                        )

        # 3. Determine mesocycle end date for adaptation range
        meso_end_date_str = (target_date_obj + timedelta(days=6)).strftime("%Y-%m-%d")
        active_meso = None
        objectives = db.get_objectives(status='active')
        if objectives:
            objectives.sort(key=lambda x: str(x['target_date']))
            next_goal = objectives[0]
            macro = db.get_macrocycle_for_objective(next_goal['id'])
            if macro:
                mesos = db.get_mesocycles_for_macrocycle(macro['id'])
                for m in mesos:
                    start = datetime.strptime(m['start_date'], "%Y-%m-%d").date()
                    end = datetime.strptime(m['end_date'], "%Y-%m-%d").date()
                    if start <= target_date_obj <= end:
                        active_meso = m
                        meso_end_date_str = m['end_date']
                        break

        # 4. Formulate LLM Prompt
        custom_task = f"""
TASK:
Analyze the athlete's actual workout adherence and physiological metrics trajectory over the past {history_days} days.
Review the list of completed activities compared to planned workouts and any calculated discrepancies (misses, workload/duration differences, rest violations).
Also inspect the rolling baseline reference and the daily metrics sequence to see if the athlete shows signs of accumulated fatigue.

Based on this, determine if we need to adapt the training plan for the remainder of the active mesocycle block (from {target_date_str} to {meso_end_date_str}).
- If they are showing high fatigue or injury risk (e.g. elevated RHR, depressed HRV, poor sleep, or ACWR > 1.3), replace hard workouts with recovery or rest.
- If they have missed key workouts, adjust the remaining workouts to safely build back volume without spiking the acute load too fast.
- If they are fully recovered and on track, keep the plan as scheduled or make minor optimal adjustments.

You MUST respond with a JSON object containing:
{{
  "change_needed": true | false,
  "reason": "Explain the physiological justification based on metrics trends and workout discrepancies.",
  "adapted_workouts": [
    {{
      "date": "YYYY-MM-DD",
      "sport_type": "running" | "road_biking" | "hiking" | "strength_training" | "yoga" | "ski_touring" | "rest",
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
        system_prompt = self._get_coach_system_prompt(
            objectives, db.get_constraints(start_after=target_date_str), custom_task
        )

        # Build user content containing trajectory metrics and discrepancies
        metrics_lines = []
        for m in metrics:
            metrics_lines.append(
                f"- {m['date']}: RHR={m['rhr']}bpm, HRV={m['hrv']}ms, Sleep={m['sleep_score']}, "
                f"Stress={m['stress']}, ACWR={m['acwr']:.2f}"
            )
        metrics_text = "\n".join(metrics_lines)

        discrepancy_text = (
            "\n".join(discrepancies) if discrepancies
            else "No discrepancies detected (athlete fully on track)."
        )

        planned_list = []
        for w in planned_workouts:
            planned_list.append(
                f"- {w['date']} ({w['sport_type'].upper()}): {w['title']} | "
                f"Expected duration: {w.get('duration_minutes')}m, RPE: {w.get('rpe')}, "
                f"TSS: {w.get('tss')}"
            )
        planned_text = "\n".join(planned_list)

        completed_list = []
        for act in completed_activities:
            act_load = (
                (act.get('tss') or 0.0)
                + (act.get('rpe') or 0) * (act['duration_sec'] / 3600.0)
            )
            completed_list.append(
                f"- {act['date']} ({act['activity_type'].upper()}): '{act['activity_name']}' | "
                f"Duration: {act['duration_sec']/60:.0f}m, Avg HR: {act['avg_hr']}, "
                f"Load: {act_load:.1f}"
            )
        completed_text = "\n".join(completed_list)

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
        decision = openrouter_client.complete(system_prompt, user_content)

        # Update memories if present
        if "training_strategy" in decision and decision["training_strategy"]:
            db.save_coach_memory("training_strategy", decision["training_strategy"])
        if "athlete_learnings" in decision and decision["athlete_learnings"]:
            db.save_coach_memory("athlete_learnings", decision["athlete_learnings"])

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
        self, proposed_workouts: List[Dict[str, Any]], reason: str, start_date: str, end_date: str
    ) -> None:
        """Saves proposed adapted workouts, cleans up overridden ones, and syncs to Calendar."""
        # 1. Fetch all existing workouts in the adaptation range
        existing_workouts = db.get_workouts(start_date=start_date, end_date=end_date)
        
        # Group proposed workouts by date
        proposed_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for pw in proposed_workouts:
            proposed_by_date.setdefault(pw['date'], []).append(pw)
            
        # 2. Find and delete existing workouts that are being replaced or removed
        for ew in existing_workouts:
            ew_date = ew['date']
            # If we have proposed workouts for this date
            if ew_date in proposed_by_date:
                # Check if this sport type is preserved in the proposed workouts
                proposed_sports = [p['sport_type'] for p in proposed_by_date[ew_date]]
                if ew['sport_type'] not in proposed_sports:
                    print(f"Removing overridden workout: {ew['title']} ({ew['sport_type']}) "
                          f"on {ew_date}")
                    if ew.get('google_event_id') and ew['status'] == 'synced':
                        try:
                            calendar_syncer.delete_workout_event(ew['google_event_id'])
                        except Exception as e:
                            print(f"Error deleting Google Calendar event: {e}")
                    db.delete_workout_by_id(ew['id'])
                    
        # 3. Save new adapted workouts and sync them
        for w in proposed_workouts:
            existing = db.get_workout(w['date'], w['sport_type'])
            orig_desc = None
            ge_id = None
            if existing:
                orig_desc = existing['original_description'] or existing['description']
                ge_id = existing['google_event_id']
                
            db.save_workout(
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
            updated = db.get_workout(w['date'], w['sport_type'])
            if updated:
                try:
                    calendar_syncer.sync_workout(updated)
                except Exception as e:
                    print(f"Error syncing {w['title']} to Google Calendar: {e}")

# Singleton instance
coach_engine = CoachEngine()
