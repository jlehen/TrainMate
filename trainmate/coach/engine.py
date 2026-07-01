import json
import hashlib
from typing import Any, List, Optional, Dict
from trainmate.config import config
from trainmate.openrouter import openrouter_client
from trainmate.types import Objective, LifeEvent, Workout, CompletedActivity
from trainmate.util import today_date as _today_date, cyan
from trainmate.coach.formatting import (
    format_metrics_history, format_completed_activities, format_baseline,
    format_planned_workouts, format_planned_workouts_detailed,
    format_removed_workouts, format_daily_context,
)


# Shared JSON-output instruction for incrementally updating coach learnings. The LLM emits
# only deltas; the app owns the merge so unchanged observations are never lost. The model
# does NOT set confidence — it attributes each observation to the training WEEK(S) that back
# (or contradict) it, and the app computes confidence from the accumulated distinct weeks
# (DESIGN_evidence_based_confidence.md §6).
LEARNING_UPDATES_FIELD = (
    '  "learning_updates": [\n'
    "    // Optional. Updates to athlete observations. Attribute each observation to the\n"
    "    //   specific week_commencing (Monday, YYYY-MM-DD) value(s) shown in the weekly\n"
    "    //   summaries that justify it. Each item is one of:\n"
    '    //   {"op": "add", "text": "New observation.", "sports": "running", "evidence": ["2026-05-04", "2026-05-11"]},\n'
    '    //   {"op": "revise", "id": 3, "text": "Reworded observation #3.", "evidence": ["2026-05-18"]},\n'
    '    //   {"op": "reinforce", "id": 4, "evidence": ["2026-05-25"]},\n'
    '    //   {"op": "contradict", "id": 5, "evidence": ["2026-06-01"]},\n'
    '    //   {"op": "retire", "id": 6}\n'
    '    // "sports": comma-separated sport(s) the observation applies to (e.g. "running,road_biking"),\n'
    '    //   or "general" if not sport-specific. Defaults to "general".\n'
    '    // "evidence": the week_commencing date(s) of training that SUPPORT the observation\n'
    "    //   (add/revise/reinforce) or CONTRADICT it (contradict). Do NOT set a confidence\n"
    "    //   level — the app derives it from how many distinct weeks back each observation.\n"
    '    // Use "reinforce" when an existing observation holds again in a new week; "contradict"\n'
    "    //   when a week shows the opposite (this can lower its confidence).\n"
    "    // Existing observations persist automatically; do NOT repeat unchanged ones.\n"
    "    // Reference existing observations by the [id] shown under COACH LEARNINGS.\n"
)


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

    def _clean_goals(self, objectives: List[Objective]) -> List[Dict[str, Any]]:
        """The goal fields that matter for planning, normalized and stably ordered.

        Single source of truth for both the goals_hash fingerprint and the snapshot
        persisted on the macrocycle, so the two can never drift apart.
        """
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
        return cleaned

    def _clean_lifeevents(self, lifeevents: List[LifeEvent]) -> List[Dict[str, Any]]:
        """The life-event fields that matter for planning, normalized and stably ordered.

        Single source of truth for both the lifeevents_hash fingerprint and the snapshot
        persisted on the macrocycle (see _clean_goals).
        """
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
        return cleaned

    def _get_goals_hash(self, objectives: List[Objective]) -> str:
        """Computes a hash representation of objectives list to check for updates."""
        serialized = json.dumps(self._clean_goals(objectives), sort_keys=True)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _get_lifeevents_hash(self, lifeevents: List[LifeEvent]) -> str:
        """Computes a hash representation of life events list to check for updates."""
        serialized = json.dumps(self._clean_lifeevents(lifeevents), sort_keys=True)
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
        metrics: List[Dict[str, Any]], window_start: str, window_end: str,
        lifeevents: Optional[List[LifeEvent]] = None,
        daily_context: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Fingerprints the *evidence* a backward evaluation reconstructs from — the
        completed activities + daily metrics (+ overlapping life events) within a window —
        so a re-run over unchanged data can be detected (see DESIGN_backward_evaluation.md
        §5, §8).

        We hash the load-bearing fields (not just activity ids) so that a re-pull which
        *corrects* a value also shifts the fingerprint. Hashing the concrete activity-id
        set rather than only the date range narrows the overlapping/shrinking-window edge
        (§7). Life events and `stress` are hashed because they now feed the analysis input
        (DESIGN_richer_analysis_evidence.md §5).

        DELIBERATE OMISSIONS (§11 / richer-evidence §5): the prompt text and science/*.txt
        files are NOT hashed; neither is *baseline recomputation* that shifts a deviation
        without any in-window metric changing (baselines track the metrics, so they move
        together in practice). Editing a prompt/guideline or a bare baseline recompute will
        therefore reuse a stale reconstruction until the underlying data changes; `--force`
        is the manual escape hatch. Chosen trade-offs, not oversights.
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
             m.get('stress'), m.get('acwr'))
            for m in metrics
        )
        evt_digest = sorted(
            (c.get('id'), c.get('start_date'), c.get('end_date'),
             c.get('event_type'), c.get('impact_description'))
            for c in (lifeevents or [])
        )
        # Daily context feeds the analysis input, so an added/edited/deleted signal must
        # shift the fingerprint (DESIGN_calendar_context_ingest.md §7).
        ctx_digest = sorted(
            (c.get('date'), c.get('metric'), c.get('value'), c.get('text'))
            for c in (daily_context or [])
        )
        serialized = json.dumps(
            {'window': [window_start, window_end],
             'activities': act_digest, 'metrics': met_digest, 'lifeevents': evt_digest,
             'daily_context': ctx_digest},
            sort_keys=True
        )
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _plan_generate_strategy(
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

        print(cyan("Querying OpenRouter to generate macrocycle and mesocycles periodization strategy..."))
        result = openrouter_client.complete(
            system_prompt, user_content, label="plan_generate"
        )
        return result

    def _workout_generate_logic(
        self, objectives: List[Objective], lifeevents: List[LifeEvent],
        today_str: str, guidelines: str, profile: Optional[Dict[str, Any]],
        strategy: str, meso_text: str, learnings: str,
        num_days: int = 28,
        start_str: Optional[str] = None,
        metrics: Optional[List[Dict[str, Any]]] = None,
        completed_activities: Optional[List[CompletedActivity]] = None,
        baseline: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Queries LLM to generate workouts for a given number of days based on active strategy.

        `start_str` is the first day to schedule (defaults to today). It differs from today
        only when today's session is already completed and must be preserved — generation
        then begins tomorrow so the finished workout isn't overwritten.
        """
        start_str = start_str or today_str
        starting_phrase = "today" if start_str == today_str else start_str
        weeks = num_days / 7
        if weeks == int(weeks):
            duration_desc = f"{int(weeks)} week{'s' if weeks != 1 else ''} ({num_days} days)"
        else:
            duration_desc = f"{num_days} day{'s' if num_days != 1 else ''}"
        custom_task = (
            f"TASK:\nGenerate a training schedule for the next {duration_desc} starting from {starting_phrase}.\n"
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
            '      "description": "Start with the title on its own line in brackets followed by a\n'
            '        newline, e.g. \"[Tempo Run]\\n\", then a detailed description of intensity,\n'
            '        duration, heart rate zones, and goals.",\n'
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
            f"Please generate the microcycles (workouts) for the next {duration_desc} "
            f"starting from {starting_phrase}."
        )
        if start_str != today_str:
            user_content += (
                f" Today's ({today_str}) session is already completed and must NOT be "
                f"regenerated — the first workout you schedule must be dated {start_str}."
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

        print(cyan("Querying OpenRouter to generate training workouts (microcycles)..."))
        plan_data = openrouter_client.complete(
            system_prompt, user_content, label="workout_generate"
        )
        return plan_data

    def _workout_adapt_logic(
        self, target_date_str: str, history_days: int, start_date_str: str,
        metrics: List[Dict[str, Any]], completed_activities: List[CompletedActivity],
        planned_workouts: List[Workout], baseline_str: str,
        meso_end_date_str: str, objectives: List[Objective], lifeevents: List[LifeEvent],
        guidelines: str, profile: Optional[Dict[str, Any]], strategy: str,
        meso_text: str, learnings: str, discrepancies: List[str],
        informational: Optional[List[CompletedActivity]] = None,
        removed_workouts: Optional[List[Workout]] = None,
        daily_context: Optional[List[Dict[str, Any]]] = None,
        completed_keys: Optional[set] = None,
        athlete_message: Optional[str] = None
    ) -> Dict[str, Any]:
        """Queries LLM to evaluate metrics/activities and adapt workouts if needed.

        `athlete_message` is an optional free-text note for THIS adaptation only; when
        present it is surfaced as a clearly-bounded section of the user content and the
        model is told to weigh it as today's intent without treating it as a durable
        signal about the block.
        """
        custom_task = f"""
TASK:
Analyze the athlete's actual workout adherence and physiological metrics trajectory
over the past {history_days} days.
Review the list of completed activities compared to planned workouts and any
calculated discrepancies (misses, workload/duration differences, rest violations).
Activities listed as informational fell on dates no plan governed (e.g. before the
plan began) — count their load when judging fatigue, but do NOT treat them as
adherence failures or unplanned deviations.
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

ATTRIBUTING A DEPRESSED MORNING — TRAINING FATIGUE vs LIFESTYLE NOISE:
When recovery looks bad, separate WHY it is depressed from WHAT to do today — they are
different decisions. If an externally-logged daily-context signal (e.g. alcohol, a bad
night, high stress — recovery lags, so look at the signal the DAY BEFORE the depressed
morning) explains the dip, treat that suppression as transient lifestyle noise, NOT
accumulated training fatigue.
- Today's readiness still counts: a suppressed body trains a hard session poorly and
  with more risk regardless of cause, so easing today, or better RESCHEDULING the hard
  session a day or two later (preserving the planned work rather than deleting it), is
  a reasonable call. Use your judgement on acute readiness.
- But do NOT read a lifestyle-suppressed morning as evidence the BLOCK is too hard:
  don't permanently cut the mesocycle's planned volume/intensity on its account, and
  don't treat it as accumulated training fatigue. Reserve genuine load REDUCTIONS for
  fatigue the TRAINING actually caused (a depressed morning following genuinely hard
  days, with no lifestyle signal to explain it).
When a hard day AND a lifestyle signal coincide, both may contribute — weigh them
rather than blaming training alone.

Some sessions may be listed as deliberately removed by the athlete. These are
intentional plan edits, NOT adherence failures — do not treat them as missed workouts.
You may, however, consider them when judging the athlete's intent and remaining load.

Planned sessions tagged "[COMPLETED — locked history, not adaptable]" have already
been performed (a matching activity was recorded), including any session the athlete
trained earlier on the evaluation date. They are history: do NOT adapt them, and never
restate a finished session to match what was actually done. Adapt only sessions still
ahead of the athlete.

Sessions tagged "[athlete-added]" were scheduled by the athlete themselves, not
generated by you — treat them as deliberate intent. Preserve them as planned unless
fatigue or injury risk clearly warrants easing, and prefer rescheduling a day or two
over deleting them. If you must reduce one, say why in the reason.

DO NOT COMPOUND A PRIOR ADAPTATION:
Sessions tagged "[ALREADY EASED by a prior adaptation ...]" are NOT the original plan —
their current numbers are the reduced form a previous adaptation already produced.
Recovery metrics LAG, so the morning after an easing often still looks depressed from
the very fatigue you already acted on; reading that as "still too hard" and cutting
again would spiral the load down without ever letting it rebound. Default to HOLDING the
already-eased form. Only cut it further if the metrics have clearly WORSENED since it was
eased, or a genuinely NEW signal (a hard completed session, a fresh life/context event)
warrants it — and the more recently and more times it was already eased (see the tag),
the higher your bar for touching it again. Restoring load toward the original as the
athlete recovers is encouraged; deepening an already-fresh cut is not.

ATHLETE'S NOTE FOR TODAY:
If the user content includes a section titled "ATHLETE'S NOTE FOR THIS ADAPTATION", it is
a free-text note the athlete attached to THIS run — extra intent or constraints the
metrics can't show (e.g. a niggle to protect, no access to a sport/venue on a given day,
or how they feel). Weigh it as today's intent alongside the data: honour stated
constraints, and let it tip a judgement call. It is advisory, not an override — do NOT
schedule clearly unsafe load just because the athlete asks (if recovery signals warrant
easing, ease and say why). Treat it as a one-off for this adaptation only: do NOT read it
as durable evidence about the block, and do NOT permanently re-shape the mesocycle on its
account.
BUT record its FOOTPRINT: the note is gone on the next run, yet the sessions it changed
persist. So when the note is what drives a session change (e.g. "no training access
Thursday" -> that day set to rest/eased), name that external cause in the session's
"change_reason" — e.g. "Rest — athlete away, no training access this day." A later
adaptation, which will NOT see this note, reads that reason back with the plan and so
won't blindly undo the tactical change (e.g. re-add a session on a day the athlete can't
train). This footprint is the tactical session note only; it is still NOT durable block
evidence and must not reshape the mesocycle.

This daily adaptation is READ-ONLY with respect to the coach's durable observations:
use the COACH LEARNINGS as context, but do NOT emit any learning updates here — durable,
evidence-backed observations are authored only by the weekly history analysis
(`data bootstrap` / `data reflect`).
""" + (
            "You MUST respond with a JSON object containing:\n"
            "{\n"
            '  "change_needed": true | false,\n'
            '  "reason": "Overall rationale for the whole adaptation: the readiness/load\n'
            '    picture and the strategy applied across the block. This is the batch-level\n'
            '    summary, shared by every adapted workout below — do NOT repeat it per\n'
            '    workout; keep per-workout notes in "change_reason".",\n'
            '  "adapted_workouts": [\n'
            "    // Include ONLY sessions you are actually changing. Omit any session that\n"
            "    // stays exactly as planned — it is preserved automatically, so re-listing\n"
            "    // an unchanged session (even verbatim) is wrong and counts as a spurious\n"
            "    // adaptation. EXCEPTION: if you change one session on a date that holds\n"
            "    // ANOTHER session of a different sport you are keeping, include BOTH that\n"
            "    // day so the kept one is not dropped.\n"
            "    {\n"
            '      "date": "YYYY-MM-DD",\n'
            '      "sport_type": "running" | "road_biking" | "hiking" | "strength_training" |\n'
            '        "yoga" | "ski_touring" | "rest",\n'
            '      "title": "Adapted Workout Title",\n'
            '      "change_reason": "One short sentence on why THIS specific session changed,\n'
            '        e.g. \"Cut to easy Z2 to shed intensity.\" If an external constraint from\n'
            '        the athlete\'s note drove the change rather than the metrics, name that\n'
            '        cause here so a future run without the note understands it, e.g. \"Rest —\n'
            '        athlete away, no training access this day.\" Keep it to a single sentence;\n'
            '        do not restate the overall reason.",\n'
            '      "description": "Start with the title on its own line in brackets followed by a\n'
            '        newline, e.g. \"[Tempo Run]\\n\", then an adapted description of intensity,\n'
            '        duration, heart rate zones, and goals.",\n'
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
        context_text = (
            format_daily_context(daily_context) if daily_context
            else "No external daily-context signals logged in this window."
        )
        discrepancy_text = (
            "\n".join(discrepancies) if discrepancies
            else "No discrepancies detected (athlete fully on track)."
        )
        planned_text = format_planned_workouts_detailed(
            planned_workouts, completed_keys, eval_date=target_date_str
        )
        completed_text = format_completed_activities(completed_activities)

        removed_section = ""
        if removed_workouts:
            removed_section = (
                "\nWorkouts Removed by Athlete (deliberately cancelled — not misses):\n"
                + format_removed_workouts(removed_workouts) + "\n"
            )

        informational_section = ""
        if informational:
            informational_section = (
                "\nActivities Outside Any Plan (informational — load counts, "
                "but not adherence failures):\n" + format_completed_activities(informational) + "\n"
            )

        # Ephemeral, this-run-only note from the athlete (see custom_task guidance). Omitted
        # entirely when absent so a message-less run is byte-for-byte the prior behaviour.
        message_section = ""
        if athlete_message and athlete_message.strip():
            message_section = (
                "\nATHLETE'S NOTE FOR THIS ADAPTATION (free-text intent/constraints for "
                "today only — advisory, not an override; do not treat as durable evidence "
                f"about the block):\n{athlete_message.strip()}\n"
            )

        user_content = f"""
Evaluation Date: {target_date_str}
Adaptation Range: {target_date_str} to {meso_end_date_str}
{message_section}

Athlete's Metrics History (Past {history_days} Days):
{metrics_text}

Externally-Logged Daily Context (alcohol, poor sleep, stress, etc. — a signal the day
before a depressed morning is a likely non-training explanation; recovery lags):
{context_text}

Baseline Reference:
{baseline_str}

Planned Workouts (recent window for adherence + already-scheduled sessions through
the adaptation range). This is the full forward plan for CONTEXT — most of it will
usually be fine and should be left untouched. Return a session in "adapted_workouts"
ONLY if you are genuinely changing it; sessions you omit stay exactly as planned (they
are NOT dropped). Do not re-list a session just to keep it, and do not reword a session
you don't mean to change — that registers as a spurious adaptation.
When you DO change a session, modify it in place: preserve its date and sport_type
unless deliberately swapping the sport. Only invent a brand-new session for a date that
currently has none.
Each session below includes its full description so you can reuse its specifics —
interval structure, heart-rate zones, rest/recovery durations — when you carry a changed
session over largely as-is. Adapt as boldly as the athlete's state warrants, but only
where their state actually warrants it; the descriptions are here only so detail you are
keeping isn't lost for lack of being restated:
{planned_text}
{removed_section}
Actual Completed Garmin Activities in Window:
{completed_text}

Adherence Discrepancies & Violations:
{discrepancy_text}
{informational_section}"""
        print(cyan(f"Querying OpenRouter to evaluate adaptation for the remainder of the mesocycle "
              f"({target_date_str} -> {meso_end_date_str})..."))
        decision = openrouter_client.complete(
            system_prompt, user_content, label="workout_adapt"
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
        print(cyan("Querying OpenRouter to generate intermediate objectives..."))
        result = openrouter_client.complete(
            system_prompt, user_content, label="plan_generate"
        )
        return result

    def _data_analyze_logic(
        self, objectives: List[Objective], guidelines: str,
        profile: Optional[Dict[str, Any]],
        weekly_summaries: List[Dict[str, Any]],
        learnings: str,
        context: Optional[str] = None,
        context_days: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        label: str = "workout_analysis"
    ) -> Dict[str, Any]:
        """Queries LLM to reverse-engineer training cycles from weekly summaries."""
        custom_task = (
            "TASK:\n"
            "Analyze the athlete's completed training load, zone distributions, and\n"
            "physiological metrics week-by-week. Reverse-engineer this data to identify\n"
            "the underlying training phases (macrocycle & mesocycles) that occurred.\n"
            "\n"
            "READING THE PER-WEEK CONTEXT FIELDS:\n"
            "- 'life_events': non-training events overlapping the week (illness, travel,\n"
            "  work crunch, etc.). Consider them as a possible explanation for load,\n"
            "  performance, or recovery anomalies before attributing those to training\n"
            "  adaptation; avoid authoring a training learning from a week whose anomaly a\n"
            "  life event already explains.\n"
            "- 'daily_context': externally-logged daily signals (e.g. alcohol, poor sleep,\n"
            "  high stress), each with a 'metric', an optional numeric 'value', and free\n"
            "  'text'. Present only on days one was logged. Treat these the same way as\n"
            "  life events: a signal the day before (recovery lags) is a likely\n"
            "  non-training explanation for a depressed next-morning metric, so weigh it\n"
            "  before attributing the dip to training load.\n"
            "- 'vs_baseline_z': how the week's morning metrics sat versus the athlete's\n"
            "  rolling baseline, in standard deviations. Sign convention: +rhr = elevated\n"
            "  (worse), +hrv = higher (better), +sleep = better. Use these (with\n"
            "  'avg_sleep_score'/'avg_stress') as the evidence for 'physiological_insights'\n"
            "  and any recovery/overreaching observation. Recovery response LAGS load by\n"
            "  roughly a week, so read a high-load week together with the NEXT week's\n"
            "  'vs_baseline_z'. A null component means 'no data' — never treat it as zero.\n"
            "\n"
            "READING 'context_days' (quantitative context impact, full history):\n"
            "- A separate block, per external signal category (e.g. alcohol), of aligned\n"
            "  EPISODES. An episode is a run of one or more signal-days; each has a 'days'\n"
            "  dose sequence ({date, value, load_tss} — the signal magnitude and that day's\n"
            "  training load) and a 'surrounding_mornings' strip bracketing it: k mornings\n"
            "  before (drink-free, the local 'normal'), the run during, and k after.\n"
            "- Each morning carries 'prev_day_load_tss' and 'vs_normal' (baseline-relative\n"
            "  z per recovery channel, same sign convention as 'vs_baseline_z'). Read a\n"
            "  morning's PRECEDING-day dose (match the morning's prior date against 'days')\n"
            "  AND its 'prev_day_load_tss' TOGETHER before blaming a low morning on the\n"
            "  signal — a hard training day the day before is the competing explanation.\n"
            "- Read the before -> during -> after arc to judge how LARGE the effect is, how\n"
            "  many days it PERSISTS, and whether back-to-back signal-days STACK (cumulative\n"
            "  cost). Compare high- vs low-dose days at similar load (the signal's share)\n"
            "  and high- vs low-load days at similar dose (training's share).\n"
            "- Weigh the NUMBER of distinct episodes and the spread of doses: a handful is\n"
            "  weak evidence; do not over-read. 'value' may be a true count, a subjective\n"
            "  rank, or absent (presence-only) — calibrate how much to read into it.\n"
            "- A missing channel or morning is 'no data', never zero. When you author a\n"
            "  durable conclusion from this, cite the in-window week(s) the signal-days\n"
            "  fall in via the learning evidence protocol, like any other observation.\n"
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

        # Quantitative context-impact rows ride beside the weekly summaries, covering the
        # athlete's full signal-day history (DESIGN_quantitative_context_impact.md §4, §6).
        # Emitted only when some category has rows, so its absence reads as "nothing logged".
        if context_days:
            user_content += (
                "\n\nQUANTITATIVE CONTEXT IMPACT (full signal-day history, episode-aligned):\n"
            )
            user_content += json.dumps(context_days, indent=2)

        if context:
            user_content += f"\n\nATHLETE SUBJECTIVE CONTEXT FOR THIS PERIOD:\n{context}\n"

        print(cyan("Querying OpenRouter to perform training history analysis..."))
        result = openrouter_client.complete(
            system_prompt, user_content, label=label
        )
        return result
