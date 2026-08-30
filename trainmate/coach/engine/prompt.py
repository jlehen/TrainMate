import json
import hashlib
from typing import Any, List, Optional, Dict
from trainmate.config import plan_config_hash, plan_profile
from trainmate.types import Objective, Constraint, CompletedActivity
from trainmate.util import today_date as _today_date
from trainmate.benchmarks import ANCHOR_KINDS, format_value


class PromptBuildMixin:
    """Part of :class:`CoachEngine` — see coach/engine/__init__.py."""

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
        if profile.get("gender"):
            lines.append(f"- Gender: {profile['gender']}")
        # Render whatever threshold anchors the (effective) profile carries, generically
        # with each kind's unit — no kind is privileged (DESIGN_benchmark_workouts.md §3.5),
        # so a first swim/strength test shows up here with zero further code. Ordered by the
        # vocabulary so output is stable. max_hr comes from config, the rest from the logbook.
        for kind, anchor in ANCHOR_KINDS.items():
            if profile.get(kind) is not None:
                lines.append(f"- {anchor.label}: {format_value(kind, profile[kind])}")
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
        §6): `title | dates | [enforcement] | description`. A `rest` directive is already
        enforced deterministically before the LLM runs (the rest pre-pass forces those
        dates to rest), so it appears here only as context; every other directive is
        advisory prose the coach honors via judgement — the title says what to work
        around, and the LLM is trusted to honor it and choose any substitute itself."""
        lines = ""
        for c in constraints:
            desc = c.get('description') or ''
            enforcement = "no training (rest enforced)" if c.get('rest') else "advisory"
            lines += (
                f"- Constraint: {c['title']} | Dates: {c['start_date']} to {c['end_date']} | "
                f"{enforcement}"
                + (f" | Details: {desc}" if desc else "") + "\n"
            )
        return lines

    def _render_goal_lines(self, objectives: List[Objective]) -> str:
        """One `- Goal:` line per objective, the same everywhere goals reach a prompt.

        A horizon goal's date is tagged so every prompt carries what the date means,
        not just the planning task (ARCHITECTURE.md §15 "Goal dates")."""
        lines = ""
        for o in objectives:
            details = o.get('description', '')
            if o.get('date_type') == 'horizon':
                date_txt = (
                    f"around {o['target_date']} (a training horizon — "
                    "nothing is scheduled on this date)"
                )
            else:
                date_txt = str(o['target_date'])
            lines += (
                f"- Goal: {o['title']} | Date: {date_txt} | "
                f"Sport: {o['sport_type']} | Details: {details}\n"
            )
        return lines

    def _build_system_prompt(
        self, objectives: List[Objective], constraints: List[Constraint],
        guidelines: str, strategy: str, meso_text: str, learnings: str,
        profile: Optional[Dict[str, Any]], custom_task: str = ""
    ) -> str:
        """Constructs the system prompt with sports science guidelines and athlete details.

        Sections are marked `## NAME`; `custom_task` supplies `## TASK` and everything under
        it. The one hierarchy every prompt in the app follows: DESIGN_prompt_structure.md §2.
        """
        obj_text = self._render_goal_lines(objectives)

        c_text = self._render_constraints(constraints)

        athlete_profile = self._format_athlete_profile(profile)
        system_prompt = f"""You are TrainMate Coach, an advanced AI sports science training coach.
You design and adapt personalized training plans for endurance athletes using sports science
principles.

## COACHING ROLE AND OBJECTIVES
1. Design periodized training plans (macro, meso, micro cycles) leading up to the target goals.
2. Focus scheduling on the NEXT CHRONOLOGICAL GOAL only. If there are multiple goals, identify
   synergies between them (e.g. general base or strength building phases).
3. Dynamically adjust training plans based on recent Garmin metrics (Resting HR, HRV, Sleep,
   and the PMC CTL/ATL/TSB) to optimize recovery and prevent injury.
4. Shift or scale training volume and intensity around the athlete's active constraints
   (travel, injury, capacity/intensity caps, preferences) to manage fatigue and respect
   what they've asked you to work around.
5. Adhere to the day-by-day weekly availability schedule and day-dependent equipment access
   (e.g., do not schedule gym workouts on home-only days; do not schedule workouts on rest days;
   do not exceed daily availability or max sessions). Respect certainty percentages (higher
   values indicate more rigid constraints; lower values allow flexibility).

{guidelines}

## COACH LEARNINGS & ACTIVE PERIODIZATION STRATEGY
- Established Training Strategy for the current macro-cycle:
{strategy}
- Mesocycles making up the macro-cycle:
{meso_text}
- Athlete-Specific Observations (reference by [id] when revising or retiring):
{learnings}

## ATHLETE PROFILE & PREFERENCES
{athlete_profile}

## ACTIVE ATHLETE GOALS (CHRONOLOGICAL)
{obj_text if obj_text else "No active goals."}

## ACTIVE CONSTRAINTS (athlete-declared directives to work around)
{c_text if c_text else "No active constraints."}

## WRITING FOR THE ATHLETE
Your prose is read on a phone, by one athlete who is already looking at the numbers and the
sessions this command prints beside your words. Write accordingly:
- Lead with the decision. No preamble, no restating the question, no summing up at the end.
- Name only the signals that actually drove it. Do not re-list metrics, dates, or session
  details that are already on the athlete's screen.
- Say a thing once. A rationale that reads well after deleting half its words was twice as
  long as it needed to be.
Any length limit stated on a field in RESPONSE FORMAT is a hard limit, not a target. This
section governs rationale and summary prose only: a workout "description" is the
prescription the athlete trains from, and stays as complete as the session requires.

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
            entry = {
                'id': o.get('id'),
                'title': o.get('title'),
                'target_date': o.get('target_date'),
                'sport_type': o.get('sport_type'),
                'description': o.get('description'),
                'status': o.get('status')
            }
            # Only when it departs from the default, so pre-field plans keep their
            # goals_hash; flipping a goal either way still changes the hash
            # (ARCHITECTURE.md §15 "Goal dates").
            if o.get('date_type') == 'horizon':
                entry['date_type'] = 'horizon'
            cleaned.append(entry)
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
                'rest': int(c.get('rest') or 0),
                'description': c.get('description'),
            })
        cleaned.sort(key=lambda x: (str(x['start_date']), x['id'] or 0))
        return cleaned

    def _clean_constraints_all(self, constraints: List[Constraint]) -> List[Dict[str, Any]]:
        """Every active constraint, tagged with its `replan` flag — display only.

        `_clean_constraints` above is fed only the `replan = 1` subset, so the plan's
        staleness fingerprint and snapshot never see a tactical directive. But the prompt
        (`_plan_generate_strategy`) renders *every* active constraint, so a `plan show`
        that reads only the fingerprinted subset can print "Constraints considered: None"
        while a tactical constraint plainly shaped the strategy. This is the same cleaning
        rule, just unfiltered and marked so a caller can tell plan-shaping from tactical.
        """
        cleaned = self._clean_constraints(constraints)
        replan_by_id = {c.get('id'): int(c.get('replan') or 0) for c in constraints}
        for entry in cleaned:
            entry['replan'] = replan_by_id.get(entry['id'], 0)
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
        """The user_profile fields that shape the periodization strategy — see
        `config.plan_profile`, which owns the rule."""
        return plan_profile()

    def _get_config_hash(self) -> str:
        """Computes a hash of the plan-shaping user config (see _clean_profile)."""
        return plan_config_hash()

    def _get_evidence_fingerprint(
        self, completed_activities: List[CompletedActivity],
        metrics: List[Dict[str, Any]], window_start: str, window_end: str,
        constraints: Optional[List[Constraint]] = None,
        daily_signals: Optional[List[Dict[str, Any]]] = None,
        signal_days: Optional[Dict[str, Any]] = None
    ) -> str:
        """Fingerprints the *evidence* a backward evaluation reconstructs from — the
        completed activities + daily metrics (+ overlapping constraints) within a window —
        so a re-run over unchanged data can be detected (see DESIGN_backward_evaluation.md
        §5, §8). `signal_days` is the *full-history* episode block, hashed as computed
        because it is built outside the window (DESIGN_quantitative_signal_impact.md §8).

        We hash the load-bearing fields (not just activity ids) so that a re-pull which
        *corrects* a value also shifts the fingerprint. Hashing the concrete activity-id
        set rather than only the date range narrows the overlapping/shrinking-window edge
        (§7). Constraints and `stress` are hashed because they now feed the analysis input
        as discounting context (DESIGN_richer_analysis_evidence.md §5, DESIGN_constraints.md §6).

        DELIBERATE OMISSIONS (§11 / richer-evidence §5): the prompt text and science/*.md
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
             m.get('stress'), m.get('ctl'), m.get('atl'), m.get('tsb'))
            for m in metrics
        )
        evt_digest = sorted(
            (c.get('id'), c.get('start_date'), c.get('end_date'),
             int(c.get('rest') or 0),
             c.get('title'), c.get('description'))
            for c in (constraints or [])
        )
        # Daily signals feed the analysis input, so an added/edited/deleted signal must
        # shift the fingerprint (DESIGN_calendar_signal_ingest.md §7).
        sig_digest = sorted(
            (c.get('date'), c.get('metric'), c.get('value'), c.get('text'))
            for c in (daily_signals or [])
        )
        serialized = json.dumps(
            {'window': [window_start, window_end],
             'activities': act_digest, 'metrics': met_digest, 'constraints': evt_digest,
             'daily_signals': sig_digest, 'signal_days': signal_days or {}},
            sort_keys=True
        )
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
