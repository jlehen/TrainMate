import os
import json
from datetime import datetime, timedelta, timezone
from trainmate.config import config
from trainmate.db import db
from trainmate.openrouter import openrouter_client
from trainmate.google_calendar import calendar_syncer

class CoachEngine:
    def load_science_guidelines(self):
        """Loads and concatenates all text files in the science directory."""
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

    def get_coach_system_prompt(self, objectives, constraints, custom_task=""):
        """Constructs the static prefix system prompt including guidelines, goals, and memory."""
        science_guidelines = self.load_science_guidelines()
        
        # Load memory
        strategy = db.get_coach_memory("training_strategy") or (
            "Not established yet. Establish an endurance-focused training strategy "
            "based on goals."
        )
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

SPORTS SCIENCE GUIDELINES:
{science_guidelines}

COACH MEMORY (PREVIOUSLY LEARNED PHILSOPHY & OBSERVATIONS):
- Established Training Strategy for the current macro-cycle:
{strategy}
- Athlete-Specific Observations:
{learnings}

ACTIVE ATHLETE GOALS (CHRONOLOGICAL):
{obj_text if obj_text else "No active goals."}

UPCOMING CONSTRAINTS (LIFE EVENTS):
{c_text if c_text else "No upcoming constraints."}

{custom_task}
"""
        return system_prompt

    def replan(self):
        """Generates or adapts the training plan from today onwards based on goals and constraints."""
        objectives = db.get_objectives(status='active')
        if not objectives:
            return "No active goals found. TrainMate needs at least one objective to start planning.", []

        # Sort objectives by target date to identify the next goal
        objectives.sort(key=lambda x: x['target_date'])
        next_goal = objectives[0]
        
        # Get future constraints
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        constraints = db.get_constraints(start_after=today_str)

        # Custom replanning instructions
        custom_task = """
TASK:
Generate a training schedule for the next 4 weeks (28 days) starting from today. 
Identify high-level synergies between all active goals at the start, but focus the
daily/weekly scheduling exclusively on the next goal.
Incorporate deload weeks and schedule around constraints (injury = rest/cross-training,
vacation = maintain fitness, party = easy workouts next day).

You MUST respond with a JSON object containing:
{
  "reasoning": "Explain the plan, periodization block, goal synergies, and constraints.",
  "training_strategy": "Establish or update the overall training strategy philosophy text blob.",
  "athlete_learnings": "Update athlete observations text blob based on metrics or status if any.",
  "workouts": [
    {
      "date": "YYYY-MM-DD",
      "sport_type": "running" | "road_biking" | "hiking" | "strength_training" | "yoga" | "ski_touring" | "rest",
      "title": "Workout Title (e.g., Tempo Run, Long Ride, Rest Day)",
      "description": "Detailed description of intensity, duration, heart rate zones, and goals."
    }
  ]
}
"""
        system_prompt = self.get_coach_system_prompt(objectives, constraints, custom_task)
        user_content = f"Today's date is {today_str}. Please generate the training plan starting today."

        print("Querying OpenRouter to generate training plan...")
        plan_data = openrouter_client.complete(system_prompt, user_content)

        # Save strategy and learnings to memory
        if "training_strategy" in plan_data:
            db.save_coach_memory("training_strategy", plan_data["training_strategy"])
        if "athlete_learnings" in plan_data:
            db.save_coach_memory("athlete_learnings", plan_data["athlete_learnings"])

        # Save workouts to database
        workouts = plan_data.get("workouts", [])
        
        # Clear future unsynced workouts to prevent overlapping plans
        db.clear_future_workouts(today_str)
        
        for w in workouts:
            db.save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                status='planned'
            )

        print(f"Generated {len(workouts)} workouts.")
        return plan_data.get("reasoning", "Plan generated."), workouts

    def daily_adapt(self, target_date_str=None):
        """Runs the daily check to adapt today's planned workout based on Garmin metrics."""
        if not target_date_str:
            target_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Fetch workouts for this date
        workouts = db.get_workouts(start_date=target_date_str, end_date=target_date_str)
        if not workouts:
            return f"No workouts planned for {target_date_str}.", None

        # Fetch Garmin metrics and baseline for this date
        metrics = db.get_metrics_cache(start_date=target_date_str, end_date=target_date_str)
        if not metrics:
            return f"No Garmin metrics found for {target_date_str} to evaluate adaptation.", None
        
        today_metrics = metrics[0]
        baseline = db.get_baseline(target_date_str)
        if not baseline:
            # Fallback to no-baseline mode
            baseline_str = (
                "No baseline data available yet. Use absolute values (e.g. HRV, Sleep Score) "
                "to assess fatigue."
            )
        else:
            baseline_str = f"""
Resting HR baseline: Mean = {baseline['rhr_baseline_mean']:.1f}, StdDev = {baseline['rhr_baseline_std']:.2f}
HRV baseline: Mean = {baseline['hrv_baseline_mean']:.1f}, StdDev = {baseline['hrv_baseline_std']:.2f}
Sleep Score baseline: Mean = {baseline['sleep_baseline_mean']:.1f}, StdDev = {baseline['sleep_baseline_std']:.2f}
"""

        objectives = db.get_objectives(status='active')
        constraints = db.get_constraints(start_after=target_date_str)

        custom_task = """
TASK:
Evaluate today's Garmin metrics against the rolling baseline and determine if today's planned
workout needs to be adapted for safety, recovery, or overload.
If the athlete shows signs of high fatigue (e.g., elevated Resting HR, low Sleep Score, or dropped
HRV), modify the workout to be easier (recovery, reduced duration, lower intensity) or change it
to rest.
If you adjust the workout, generate the adapted title and description, explaining the sports
science reason.

You MUST respond with a JSON object containing:
{
  "change_needed": true | false,
  "reason": "Detail the sports science explanation comparing metrics to baseline.",
  "adapted_title": "Workout Title (only if change_needed is true)",
  "adapted_description": "Workout Description (only if change_needed is true)",
  "training_strategy": "Optionally update training strategy philosophy based on response.",
  "athlete_learnings": (
        "Optionally update athlete observations (e.g. athlete responds poorly to consecutive "
        "hard days)."
  )
}
"""
        system_prompt = self.get_coach_system_prompt(objectives, constraints, custom_task)

        # Build user message with daily data
        workout_text = ""
        for w in workouts:
            workout_text += f"- Sport: {w['sport_type']} | Title: {w['title']} | Description: {w['description']}\n"

        user_content = f"""
Today's Date: {target_date_str}

Athlete's Today Metrics:
- Resting Heart Rate: {today_metrics['rhr']} bpm
- HRV Overnight Average: {today_metrics['hrv']} ms
- Sleep Score: {today_metrics['sleep_score']} (0-100)
- Average Stress: {today_metrics['stress']}
- Acute Workload: {today_metrics['acute_workload']:.1f}
- Chronic Workload: {today_metrics['chronic_workload']:.1f}
- ACWR (Acute:Chronic Workload Ratio): {today_metrics['acwr']:.2f}

Athlete's Baseline Reference:
{baseline_str}

Planned Workout to Evaluate:
{workout_text}
"""
        print(f"Querying OpenRouter to evaluate daily adaptation for {target_date_str}...")
        decision = openrouter_client.complete(system_prompt, user_content)

        # Update memories
        if "training_strategy" in decision and decision["training_strategy"]:
            db.save_coach_memory("training_strategy", decision["training_strategy"])
        if "athlete_learnings" in decision and decision["athlete_learnings"]:
            db.save_coach_memory("athlete_learnings", decision["athlete_learnings"])

        adapted_workout = None
        if decision.get("change_needed"):
            print(f"Adaptation recommended for {target_date_str}: {decision.get('reason')}")
            for w in workouts:
                # Update workout in DB to modified
                db.save_workout(
                    date=target_date_str,
                    sport_type=w['sport_type'],
                    title=decision.get("adapted_title", w['title']),
                    description=decision.get("adapted_description", w['description']),
                    original_description=w['original_description'] or w['description'],
                    status='modified',
                    modification_reason=decision.get("reason"),
                    google_event_id=w.get('google_event_id')
                )
                
                # Fetch updated workout from DB
                adapted_workout = db.get_workout(target_date_str, w['sport_type'])
                
                # Automatically sync to Google Calendar!
                calendar_syncer.sync_workout(adapted_workout)
        else:
            print(f"No workout adaptation needed for {target_date_str}. Reason: {decision.get('reason')}")

        return decision.get("reason", "No adaptation needed."), adapted_workout

# Singleton instance
coach_engine = CoachEngine()
