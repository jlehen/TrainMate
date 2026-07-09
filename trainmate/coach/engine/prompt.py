import json
import hashlib
from typing import Any, List, Optional, Dict
from trainmate.config import config
from trainmate.openrouter import openrouter_client
from trainmate.types import Objective, Constraint, Workout, CompletedActivity
from trainmate.util import today_date as _today_date, cyan
from trainmate.coach.formatting import (
    format_metrics_history, format_completed_activities, format_baseline,
    format_planned_workouts, format_planned_workouts_detailed,
    format_removed_workouts, format_daily_context,
)
import trainmate.coach.engine as _eng
from trainmate.coach.engine import MIN_PLAN_WEEKS, MAX_PLAN_WEEKS, LEARNING_UPDATES_FIELD


class PromptBuildMixin:
    """Part of :class:`CoachEngine` — see coach/engine/__init__.py."""

    # Physiological thresholds live in user_profile but are excluded from the config
    # fingerprint: they anchor per-workout zone targets (recomputed from live config at
    # every workout generation), not the phase structure. They are instead snapshotted
    # on the macrocycle and only flag the plan stale past a relative drift tolerance
    # (`coach.threshold_replan_pct`) — see service.config_changed().
    PROFILE_THRESHOLD_FIELDS = ('max_hr', 'lthr', 'ftp')

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

    @staticmethod
    def _render_constraints(constraints: List[Constraint]) -> str:
        """Renders active directives for the prompt, one per line (DESIGN_constraints.md
        §6): `title | dates | binding | sport | type | description`. A blanket `hard`
        (no sport) directive is already enforced deterministically before the LLM runs
        (hard-rest pre-pass), so it appears here only as context; a `hard` directive
        scoped to one sport is advisory — the LLM is trusted to honor it and choose any
        substitute itself. `soft` ones are preferences the coach honors via judgement."""
        lines = ""
        for c in constraints:
            sport = c.get('sport') or 'all sports'
            ctype = c.get('type') or '—'
            desc = c.get('description') or ''
            lines += (
                f"- Constraint: {c['title']} | Dates: {c['start_date']} to {c['end_date']} | "
                f"Binding: {c.get('binding', 'soft')} | Sport: {sport} | Type: {ctype}"
                + (f" | Details: {desc}" if desc else "") + "\n"
            )
        return lines

    def _build_system_prompt(
        self, objectives: List[Objective], constraints: List[Constraint],
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

        c_text = self._render_constraints(constraints)

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
4. Shift or scale training volume and intensity around the athlete's active constraints
   (travel, injury, capacity/intensity caps, preferences) to manage fatigue and respect
   what they've asked you to work around.
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

ACTIVE CONSTRAINTS (athlete-declared directives to work around):
{c_text if c_text else "No active constraints."}

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

    def _clean_constraints(self, constraints: List[Constraint]) -> List[Dict[str, Any]]:
        """The constraint fields that matter for planning, normalized and stably ordered.

        Single source of truth for both the constraints_hash fingerprint and the snapshot
        persisted on the macrocycle (see _clean_goals). Fed only the plan-shaping
        (`replan = 1`) constraints by the caller, so tactical directives don't flag the
        plan stale (DESIGN_constraints.md §7).
        """
        cleaned = []
        for c in constraints:
            cleaned.append({
                'id': c.get('id'),
                'title': c.get('title'),
                'start_date': c.get('start_date'),
                'end_date': c.get('end_date'),
                'binding': c.get('binding'),
                'sport': c.get('sport'),
                'type': c.get('type'),
                'description': c.get('description'),
            })
        cleaned.sort(key=lambda x: (str(x['start_date']), x['id'] or 0))
        return cleaned

    def _get_goals_hash(self, objectives: List[Objective]) -> str:
        """Computes a hash representation of objectives list to check for updates."""
        serialized = json.dumps(self._clean_goals(objectives), sort_keys=True)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _get_constraints_hash(self, constraints: List[Constraint]) -> str:
        """Computes a hash representation of the plan-shaping constraints to check for
        updates (DESIGN_constraints.md §7 — computed over `replan = 1` constraints only)."""
        serialized = json.dumps(self._clean_constraints(constraints), sort_keys=True)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _clean_profile(self) -> Dict[str, Any]:
        """The user_profile fields that shape the periodization strategy.

        Single source of truth for the config_hash fingerprint. Excludes the
        physiological thresholds (tolerance-checked separately, see above); every
        other profile field — availability, target hours, preferences, injuries,
        equipment — is plan-shaping. Deliberately NOT fingerprinted: prompt-context
        knobs such as `coach.metrics_lookback_days`, which change what the coach
        *sees*, not what the plan should be.
        """
        return {
            k: v for k, v in config.user_profile.items()
            if k not in self.PROFILE_THRESHOLD_FIELDS
        }

    def _get_config_thresholds(self) -> Dict[str, float]:
        """The current physiological thresholds, normalized for the macrocycle
        snapshot and the drift comparison in service.config_changed()."""
        profile = config.user_profile
        return {
            k: float(profile[k]) for k in self.PROFILE_THRESHOLD_FIELDS
            if profile.get(k) is not None
        }

    def _get_config_hash(self) -> str:
        """Computes a hash of the plan-shaping user config (see _clean_profile)."""
        data_to_hash = {'user_profile': self._clean_profile()}
        serialized = json.dumps(data_to_hash, sort_keys=True)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _get_evidence_fingerprint(
        self, completed_activities: List[CompletedActivity],
        metrics: List[Dict[str, Any]], window_start: str, window_end: str,
        constraints: Optional[List[Constraint]] = None,
        daily_context: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Fingerprints the *evidence* a backward evaluation reconstructs from — the
        completed activities + daily metrics (+ overlapping constraints) within a window —
        so a re-run over unchanged data can be detected (see DESIGN_backward_evaluation.md
        §5, §8).

        We hash the load-bearing fields (not just activity ids) so that a re-pull which
        *corrects* a value also shifts the fingerprint. Hashing the concrete activity-id
        set rather than only the date range narrows the overlapping/shrinking-window edge
        (§7). Constraints and `stress` are hashed because they now feed the analysis input
        as discounting context (DESIGN_richer_analysis_evidence.md §5, DESIGN_constraints.md §6).

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
             m.get('stress'), m.get('acwr'),
             m.get('ctl'), m.get('atl'), m.get('tsb'))
            for m in metrics
        )
        evt_digest = sorted(
            (c.get('id'), c.get('start_date'), c.get('end_date'),
             c.get('binding'), c.get('sport'), c.get('type'),
             c.get('title'), c.get('description'))
            for c in (constraints or [])
        )
        # Daily context feeds the analysis input, so an added/edited/deleted signal must
        # shift the fingerprint (DESIGN_calendar_context_ingest.md §7).
        ctx_digest = sorted(
            (c.get('date'), c.get('metric'), c.get('value'), c.get('text'))
            for c in (daily_context or [])
        )
        serialized = json.dumps(
            {'window': [window_start, window_end],
             'activities': act_digest, 'metrics': met_digest, 'constraints': evt_digest,
             'daily_context': ctx_digest},
            sort_keys=True
        )
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
