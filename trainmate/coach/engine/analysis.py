import json
from typing import Any, List, Optional, Dict
from trainmate.types import Objective
from trainmate.util import cyan
import trainmate.coach.engine as _eng
from trainmate.coach.engine import LEARNING_UPDATES_FIELD


class AnalysisLogicMixin:
    """Part of :class:`CoachEngine` — see coach/engine/__init__.py."""

    def _data_analyze_logic(
        self, objectives: List[Objective], guidelines: str,
        profile: Optional[Dict[str, Any]],
        weekly_summaries: List[Dict[str, Any]],
        learnings: str,
        context: Optional[str] = None,
        context_days: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        label: str = "workout_analysis",
        horizon: str = "long"
    ) -> Dict[str, Any]:
        """Queries the LLM to read a window of completed training.

        `horizon` selects the question, not just the window: `long` reverse-engineers the
        periodization structure, `short` reads the recent response only — a periodization
        structure cannot be inferred from a few weeks
        (DESIGN_backward_evaluation.md §3, §10.3).
        """
        cycles = horizon == "long"
        custom_task = (
            "## TASK\n"
            "Analyze the athlete's completed training load, zone distributions, and\n"
            "physiological metrics week-by-week. "
            + ("Reverse-engineer this data to identify\n"
               "the underlying training phases (macrocycle & mesocycles) that occurred.\n"
               if cycles else
               "Read how the athlete RESPONDED to the\n"
               "training in this window: recovery, tolerance, and what the evidence supports\n"
               "as a durable observation about them. This window is too short to support a\n"
               "claim about periodization structure — do not infer macro/mesocycles.\n")
            + "\n"
            "### READING THE PER-WEEK CONTEXT FIELDS\n"
            "- 'constraints': athlete-declared directives overlapping the week (illness,\n"
            "  travel, work crunch, capacity caps, etc.). Consider them as a possible\n"
            "  explanation for load, performance, or recovery anomalies before attributing\n"
            "  those to training adaptation; avoid authoring a training learning from a week\n"
            "  whose anomaly a constraint already explains. A constraint may only explain an\n"
            "  anomaly away — never cite one as supporting evidence for a learning.\n"
            "- 'daily_context': externally-logged daily signals (e.g. alcohol, poor sleep,\n"
            "  high stress), each with a 'metric', an optional numeric 'value', and free\n"
            "  'text'. Present only on days one was logged. Treat these the same way as\n"
            "  constraints: a signal the day before (recovery lags) is a likely\n"
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
        )

        # Gate this reading guide on the same context_days that gates the DATA block below,
        # so the guide never describes a section the model wasn't given.
        if context_days:
            custom_task += (
                "### READING 'context_days' (quantitative context impact, full history)\n"
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
            )

        custom_task += (
            "## RESPONSE FORMAT\n"
            "You MUST respond with a JSON object containing:\n"
            "{\n"
            '  "macrocycle_summary": "High-level summary of the training period.",\n'
            + ('  "inferred_macrocycle": {\n'
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
               "  ],\n" if cycles else "")
            + '  "physiological_insights": [\n'
            '    "Physiological response observations (e.g., HRV/RHR trends vs load)."\n'
            "  ],\n"
            + LEARNING_UPDATES_FIELD +
            "  ]\n"
            "}\n"
        )

        system_prompt = (
            "You are TrainMate Coach, an advanced AI sports science training coach.\n"
            "You analyze historical activities and physiological metrics to "
            + ("identify\ntraining periodization phases (macro and mesocycles).\n\n"
               if cycles else
               "read how an\nathlete is responding to their training.\n\n")
            + f"{guidelines}\n"
        )

        athlete_profile = self._format_athlete_profile(profile)
        system_prompt += f"\n## ATHLETE PROFILE & PREFERENCES\n{athlete_profile}\n"

        obj_text = self._render_goal_lines(objectives)
        system_prompt += (
            f"\n## ATHLETE GOALS IN OR AFTER THIS PERIOD\n"
            f"{obj_text if obj_text else 'No objectives.'}\n"
        )

        # Show existing observations so the model can revise/reinforce/retire them by
        # [id] rather than only re-adding near-duplicates on every run.
        system_prompt += (
            "\n## COACH LEARNINGS\n"
            "Existing athlete observations — reference by [id] when revising, "
            "reinforcing, or retiring.\n"
            f"{learnings}\n"
        )

        system_prompt += f"\n{custom_task}\n"

        user_content = "Please analyze the weekly training summaries below.\n\n"
        user_content += "## WEEKLY TRAINING SUMMARIES\n"
        user_content += json.dumps(weekly_summaries, indent=2)

        # Quantitative context-impact rows ride beside the weekly summaries, covering the
        # athlete's full signal-day history (DESIGN_quantitative_context_impact.md §4, §6).
        # Emitted only when some category has rows, so its absence reads as "nothing logged".
        if context_days:
            user_content += (
                "\n\n## QUANTITATIVE CONTEXT IMPACT (full signal-day history, episode-aligned)\n"
            )
            user_content += json.dumps(context_days, indent=2)

        if context:
            user_content += f"\n\n## ATHLETE SUBJECTIVE CONTEXT FOR THIS PERIOD\n{context}\n"

        print(cyan("Querying OpenRouter to perform training history analysis..."))
        result = _eng.openrouter_client.complete(
            system_prompt, user_content, label=label
        )
        return result
