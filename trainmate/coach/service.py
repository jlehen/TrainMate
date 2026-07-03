import json
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.db import db
from trainmate.google_calendar import calendar_syncer
from trainmate.types import Objective, Constraint, Workout
from trainmate.adherence import analyze_adherence, _planned_load
from trainmate.sports import canonical_sport
from trainmate.modification_state import SWAP_REASON_PREFIX, MANUAL_REPLACE_REASON_PREFIX
from trainmate.garmin import activity_load
from trainmate.util import today_str as _today_str, today_date as _today_date, cyan, green, yellow, bold, red, gray
from trainmate.coach.engine import CoachEngine, MIN_PLAN_WEEKS, MAX_PLAN_WEEKS
from trainmate.coach.formatting import format_baseline, _load_science_guidelines

# Fallback look-back for `reflect` when no watermark exists yet (bootstrap not run).
DEFAULT_REFLECT_WEEKS = 4


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
        bootstrap`), its summary, reverse-engineered macro/mesocycle structure, and
        physiological insights are appended — reused without another LLM call (§10).
        Returns None if there is nothing to report.

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
            # Reverse-engineered periodization structure: the overall focus and the
            # mesocycle blocks the athlete actually moved through. Fed so the new plan can
            # build on the real prior arc (where base/build/recovery fell, how consistent
            # each block was) rather than re-deriving it (DESIGN_backward_evaluation.md §10).
            im = recon.get("inferred_macrocycle") or {}
            if im.get("overall_focus"):
                span = ""
                if im.get("start_date") and im.get("end_date"):
                    span = f" ({im['start_date']}..{im['end_date']})"
                recon_lines.append(f"Reconstructed macrocycle focus{span}: {im['overall_focus']}")
            for meso in (recon.get("inferred_mesocycles") or []):
                name = meso.get("name", "Phase")
                m_span = ""
                if meso.get("start_date") and meso.get("end_date"):
                    m_span = f" ({meso['start_date']}..{meso['end_date']})"
                detail = []
                if meso.get("focus_detected"):
                    detail.append(f"focus \"{meso['focus_detected']}\"")
                if meso.get("average_weekly_tss") is not None:
                    detail.append(f"~{float(meso['average_weekly_tss']):.0f} TSS/wk")
                if meso.get("estimated_consistency"):
                    detail.append(f"{meso['estimated_consistency']} consistency")
                detail_txt = f": {', '.join(detail)}" if detail else ""
                recon_lines.append(f"- {name}{m_span}{detail_txt}")
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

    def _get_config_snapshot(self) -> str:
        """JSON of the current physiological thresholds, persisted on the macrocycle
        so config_changed() can judge later drift against real values."""
        return json.dumps(self.engine._get_config_thresholds(), sort_keys=True)

    def config_changed(self, macro: Dict[str, Any]) -> Optional[str]:
        """Whether config.yaml has drifted plan-shapingly since `macro` was generated.

        Returns a human-readable reason, or None when the plan is still current. Two
        axes (see engine._clean_profile): the fingerprint over plan-shaping profile
        fields, and the physiological thresholds, which only count as drift past
        `coach.threshold_replan_pct` relative change — a small FTP/LTHR retest
        correction feeds the next workout generation without invalidating the
        periodization strategy. Macrocycles predating the threshold snapshot judge on
        the fingerprint alone.
        """
        if macro.get('config_hash') != self.engine._get_config_hash():
            return "athlete profile changed"

        snapshot_raw = macro.get('config_snapshot')
        if not snapshot_raw:
            return None
        try:
            old_thresholds = json.loads(snapshot_raw)
        except (ValueError, TypeError):
            return None

        current = self.engine._get_config_thresholds()
        tolerance = config.threshold_replan_pct / 100.0
        for key in sorted(set(old_thresholds) | set(current)):
            old_val, new_val = old_thresholds.get(key), current.get(key)
            if old_val is None or new_val is None:
                return f"{key} was {'added' if old_val is None else 'removed'}"
            if old_val and abs(new_val - old_val) / abs(old_val) > tolerance:
                pct = (new_val - old_val) / old_val * 100.0
                return f"{key} changed {old_val:g} → {new_val:g} ({pct:+.1f}%)"
        return None

    def _get_goals_hash(self, objectives: List[Objective]) -> str:
        return self.engine._get_goals_hash(objectives)

    def _get_constraints_hash(self, constraints: List[Constraint]) -> str:
        return self.engine._get_constraints_hash(constraints)

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
        self, data: Dict[str, Any], available_weeks: List[str], source: str
    ) -> None:
        """Applies evidence-cited learning deltas returned by the LLM, if any.

        `available_weeks` is the set of week_commencing (Monday) dates under analysis; cited
        weeks outside it are dropped. `source` tags the evidence rows ('bootstrap'/'reflect').
        Confidence is derived by the app from the accumulated basis — re-citing counted weeks
        cannot inflate it (see db.apply_learning_deltas and
        DESIGN_evidence_based_confidence.md §6)."""
        self._db.apply_learning_deltas(
            data.get("learning_updates") or [],
            available_weeks=available_weeks,
            source=source,
        )

    def _review_learning_proposals(self, auto: bool = False) -> None:
        """Resolves pending learning-confidence downgrades (§7).

        First sweeps for staleness demotions (dormant learnings): under `auto` these apply
        directly, otherwise they are queued as proposals. Then, when interactive, prompts the
        human about each pending proposal (contradiction- or staleness-driven) — accept
        (demote), keep (dismiss + affirm), or skip (leave pending). Under `auto` contradiction
        proposals stay queued for the next interactive review; no prompts are shown."""
        self._db.derive_staleness_proposals(auto=auto)
        if auto:
            return
        pending = [l for l in self._db.get_learnings() if l.get("proposed_confidence")]
        if not pending:
            return
        print(yellow(bold("\nPending coach-learning demotion proposals:")))
        for l in pending:
            target = l["proposed_confidence"]
            target_disp = "retire" if target == "retire" else target
            print(
                f"  [{l['id']}|{l.get('sports') or 'general'}|{l['confidence']}] {l['text']}"
            )
            print(yellow(f"    proposed demotion → {target_disp}"))
            import trainmate_cli as cli
            ans = cli.prompt.choose(
                f"Apply proposed demotion of learning [{l['id']}] → {target_disp}?",
                [
                    cli.Choice("demote", f"Demote → {target_disp}"),
                    cli.Choice("keep", "Keep (dismiss + affirm)"),
                    cli.Choice("skip", "Skip (leave pending)"),
                ],
                default="skip",
            )
            if ans == "demote":
                result = self._db.demote_learning(l["id"])
                if result == "retired":
                    print(red(f"    Retired learning [{l['id']}]."))
                else:
                    print(green(f"    Demoted [{l['id']}] → {result}."))
            elif ans == "skip":
                print(gray(f"    Left [{l['id']}] pending."))
            else:
                self._db.keep_learning(l["id"])
                print(cyan(f"    Kept [{l['id']}] at {l['confidence']}."))

    def _maybe_nudge_bootstrap(self) -> None:
        """Prints a cold-start hint to run `data bootstrap` when there are no active coach
        learnings yet — durable observations are authored only by the history analysis."""
        if any(not l.get("dormant") for l in self._db.get_learnings()):
            return
        print(yellow(
            "No coach learnings yet. Run "
        ) + green("'data bootstrap'") + yellow(
            " to reconstruct your training history and seed evidence-based observations."
        ))

    def _get_coach_system_prompt(
        self, objectives: List[Objective], constraints: List[Constraint],
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
            constraints=constraints,
            guidelines=guidelines,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            profile=profile,
            custom_task=custom_task
        )

    def plan_rm(self, objective_id: int) -> None:
        """Deletes the periodization plan for a specific objective."""
        self._db.delete_macrocycle_for_objective(objective_id)

    def constraint_plan_impact(self, constraint: Dict[str, Any]) -> Dict[str, Any]:
        """Magnitude of a directive against the active plan (DESIGN_constraints.md §7,
        concrete formula): the planned load it displaces, expressed as a percentage of the
        plan's trailing weekly planned load (a self-scaling ratio — no absolute TSS number
        rots as the athlete's fitness changes), plus its span in days. The trailing week is
        the 7 days immediately before the constraint's start, so the reference point isn't
        itself affected by the constraint being evaluated. Feeds the human-confirmed replan
        proposal only — never an automatic regen."""
        start, end = constraint['start_date'], constraint['end_date']
        window_start = max(start, _today_str())
        rest = canonical_sport('rest')
        cs = canonical_sport(constraint['sport']) if constraint.get('sport') else None

        def _load(w_start: str, w_end: str) -> float:
            sessions = [
                w for w in self._db.get_workouts(start_date=w_start, end_date=w_end)
                if canonical_sport(w.get('sport_type', '')) != rest
            ]
            if cs:
                sessions = [w for w in sessions if canonical_sport(w['sport_type']) == cs]
            return sum(_planned_load(w) for w in sessions)

        displaced_load = _load(window_start, end)
        days = (datetime.strptime(end, "%Y-%m-%d").date()
                - datetime.strptime(start, "%Y-%m-%d").date()).days + 1

        trailing_end_date = datetime.strptime(start, "%Y-%m-%d").date() - timedelta(days=1)
        trailing_start_date = trailing_end_date - timedelta(days=6)
        trailing_weekly_load = _load(
            trailing_start_date.strftime("%Y-%m-%d"), trailing_end_date.strftime("%Y-%m-%d")
        )
        displaced_pct = (
            (displaced_load / trailing_weekly_load * 100) if trailing_weekly_load > 0 else 0.0
        )
        return {
            'days': days,
            'displaced_load': displaced_load,
            'trailing_weekly_load': trailing_weekly_load,
            'displaced_pct': displaced_pct,
        }

    def capture_message_constraint(
        self, candidate: Dict[str, Any], default_date_str: str
    ) -> Optional[int]:
        """Creates one durable constraint from a `new_constraints` candidate the CLI has
        already confirmed with the athlete (DESIGN_constraints.md §8 two-confirmation
        flow, step 1). Always `source='message'`, always `binding='soft'` — the LLM can
        never mark an extracted constraint `hard` (trust boundary §8); a genuine hard
        escalation is a deliberate human action (`constraint edit <id> --hard`). Never
        sets `replan=1` either — a large capture only *surfaces a suggestion* to escalate,
        which the human acts on separately."""
        title = (candidate.get('title') or '').strip()
        if not title:
            return None
        start = candidate.get('start_date') or default_date_str
        end = candidate.get('end_date') or start
        if end < start:
            end = start
        cid = self._db.add_constraint(
            title=title, start_date=start, end_date=end, binding='soft',
            sport=candidate.get('sport'), type=candidate.get('type'),
            description=candidate.get('description'), replan=0, source='message',
        )
        constraint = self._db.get_constraint(cid)
        impact = self.constraint_plan_impact(constraint)
        if self.constraint_is_plan_shaping(constraint, impact):
            print(yellow(
                f"  This looks plan-shaping ({impact['days']} days, displaces "
                f"~{impact['displaced_pct']:.0f}% of a typical week). To build it into "
                f"the plan, run 'constraint edit {cid} --replan' or 'plan generate'."
            ))
        return cid

    @staticmethod
    def constraint_is_plan_shaping(
        constraint: Dict[str, Any], impact: Dict[str, Any]
    ) -> bool:
        """Concrete §7 magnitude heuristic for whether to *propose* a replan. Two
        independent triggers, either firing proposes a replan; deliberately no
        per-session "importance" term (TrainMate has no per-workout priority field):

        1. Displaced-load trigger (relative): the constraint's overlapping planned load
           is >= config.replan_displaced_load_pct of the plan's trailing weekly planned
           load (default 50 — wipes out at least half a typical week).
        2. Hard-window floor: the constraint is `hard` and spans >= config.
           replan_hard_span_days days (default 3), regardless of load overlap (it may
           land in a light taper week yet still reshape everything after it).
        """
        if impact['displaced_pct'] >= config.replan_displaced_load_pct:
            return True
        if (
            constraint.get('binding') == 'hard'
            and impact['days'] >= config.replan_hard_span_days
        ):
            return True
        return False

    def plan_generate(
        self, force: bool = False, objective_id: Optional[int] = None, auto_apply: bool = True
    ) -> Tuple[str, List[Dict[str, Any]], bool]:
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

        # Compute plan-window dates (constraints are fetched below with the hashes).
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

        if duration_weeks < MIN_PLAN_WEEKS:
            raise ValueError(
                f"Goal '{next_goal['title']}' is too close "
                f"({duration_weeks:.1f} weeks away from the start date "
                f"{plan_start_date.strftime('%Y-%m-%d')}). "
                f"TrainMate requires at least {MIN_PLAN_WEEKS} weeks to generate a periodization plan."
            )

        if duration_weeks > MAX_PLAN_WEEKS:
            print(cyan(f"Goal '{next_goal['title']}' is {duration_weeks:.1f} "
                  f"weeks away (> {MAX_PLAN_WEEKS} weeks)."))
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
        # All active constraints feed the plan prompt; only the plan-shaping (replan=1)
        # ones fingerprint the plan and are snapshotted, so a tactical "no run Thursday"
        # never trips the reuse-vs-regen decision (DESIGN_constraints.md §7).
        constraints = self._db.get_constraints(today_str)
        replan_constraints = [c for c in constraints if c.get('replan')]
        goals_hash = self.engine._get_goals_hash(objectives)
        constraints_hash = self.engine._get_constraints_hash(replan_constraints)
        config_hash = self.engine._get_config_hash()
        config_snapshot = self._get_config_snapshot()
        goals_snapshot = json.dumps(self.engine._clean_goals(objectives))
        constraints_snapshot = json.dumps(self.engine._clean_constraints(replan_constraints))

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
                and existing_macro['constraints_hash'] == constraints_hash
                and self.config_changed(existing_macro) is None
            ):
                reused = True
                strategy = existing_macro['strategy']
                mesocycles = self._db.get_mesocycles_for_macrocycle(existing_macro['id'])
                print(cyan("Reusing existing periodization strategy (macrocycle and mesocycles) "
                      "from database."))

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
            print(cyan("Goals or plan-shaping constraints have changed, or force generation "
                  "requested. Determining new overall periodization strategy..."))
            guidelines = self._load_science_guidelines()
            profile = config.user_profile
            history_summary = self._get_recent_history_summary(today_str)
            # Planned-vs-actual review of the prior plan (+ cached reconstruction) fed as
            # read-only context (Option A). `prev_macro` here is the existing plan being
            # replaced, or the preceding goal's plan when there is none.
            prior_training_text = self._build_prior_training_context(prev_macro, today_str)
            if prior_training_text:
                print(cyan(bold("\n=== PRIOR TRAINING REVIEW (planned vs actual) ===")))
                print(prior_training_text)
                print(cyan(bold("==================================================\n")))
            macro_data = self.engine._plan_generate_strategy(
                next_goal=next_goal,
                objectives=objectives,
                constraints=constraints,
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
            if next_goal['id'] is not None and auto_apply:
                self._db.save_macrocycle(
                    objective_id=next_goal['id'],
                    strategy=strategy,
                    goals_hash=goals_hash,
                    constraints_hash=constraints_hash,
                    config_hash=config_hash,
                    config_snapshot=config_snapshot,
                    goals_snapshot=goals_snapshot,
                    constraints_snapshot=constraints_snapshot,
                    mesocycles=mesocycles
                )

            print(cyan(bold("\n=== NEW PERIODIZATION STRATEGY (MACROCYCLE) ===")))
            print(f"{bold('Overall Strategy:')}\n{strategy}\n")
            print(bold("Mesocycle Blocks:"))
            for m in mesocycles:
                print(f"- {bold(m['name'])} ({m['start_date']} to {m['end_date']}): {m['focus']}")
            print(cyan(bold("==============================================\n")))

        self._maybe_nudge_bootstrap()
        return strategy, mesocycles, reused

    def plan_apply(
        self, objective_id: int, strategy: str, mesocycles: List[Dict[str, Any]]
    ) -> None:
        """Saves a generated periodization plan to the database."""
        today_str = _today_str()
        objectives = self._db.get_objectives(status='active')
        replan_constraints = [
            c for c in self._db.get_constraints(today_str) if c.get('replan')
        ]
        goals_hash = self.engine._get_goals_hash(objectives)
        constraints_hash = self.engine._get_constraints_hash(replan_constraints)
        config_hash = self.engine._get_config_hash()

        self._db.save_macrocycle(
            objective_id=objective_id,
            strategy=strategy,
            goals_hash=goals_hash,
            constraints_hash=constraints_hash,
            config_hash=config_hash,
            config_snapshot=self._get_config_snapshot(),
            goals_snapshot=json.dumps(self.engine._clean_goals(objectives)),
            constraints_snapshot=json.dumps(self.engine._clean_constraints(replan_constraints)),
            mesocycles=mesocycles
        )

    def _today_workout_completed(
        self, today_str: str, completed_activities: List[Dict[str, Any]]
    ) -> bool:
        """Returns True when today's planned session has a matching completed activity.

        Used by `workout_generate` to decide whether to protect today's workout from a
        regeneration: a session already in the books should be kept as history rather
        than overwritten. Completion is decided by `analyze_adherence` over a one-day
        window so it uses the exact same planned-vs-completed sport matching as the rest
        of the app. A day counts as completed only when a non-rest planned workout for
        today is paired with an activity; rest days and empty days have nothing to protect.
        """
        planned = [
            w for w in self._db.get_workouts(start_date=today_str, end_date=today_str)
            if w['sport_type'] != 'rest'
        ]
        if not planned:
            return False
        today_date_obj = datetime.strptime(today_str, "%Y-%m-%d").date()
        _, matching, _ = analyze_adherence(
            planned_workouts=planned,
            completed_activities=completed_activities or [],
            start_date_obj=today_date_obj,
            history_days=1,
            minor_activity_load_threshold=config.minor_activity_load_threshold,
        )
        return any(r['completed'] for r in matching)

    @staticmethod
    def _rest_workout(date: str, cause: str) -> Dict[str, Any]:
        """A deterministic rest session the hard-constraint pre-pass places on a date the
        athlete has barred from training (DESIGN_constraints.md §6). The title/description
        are tagged "(forced constraint)" so it reads unmistakably as a code-enforced
        override rather than an ordinary planned/adapted rest day. The change_reason names
        the constraint so a later adaptation, which won't see the live constraint list in
        the same run, reads the cause back with the plan."""
        title = 'Rest (forced constraint)'
        return {
            'date': date,
            'sport_type': 'rest',
            'title': title,
            'description': f"[{title}]\nNo training — {cause}.",
            'duration_minutes': 0,
            'rpe': 0,
            'tss': 0,
            'change_reason': f"Rest — {cause}.",
        }

    @classmethod
    def _hard_rest_windows(
        cls, constraints: List[Constraint]
    ) -> List[Tuple[str, str, str]]:
        """The `hard` + no-sport windows — the only edge that skips the LLM (§5): a
        `hard` constraint scoped to one sport is advisory (rendered into the prompt as a
        hard instruction; the LLM picks any substitute itself), so it is deliberately
        excluded here. Returns [(start, end, title)]."""
        return [
            (c['start_date'], c['end_date'], c['title'])
            for c in constraints
            if c.get('binding') == 'hard' and not c.get('sport')
        ]

    @classmethod
    def _enforce_hard_constraints_generate(
        cls, workouts: List[Dict[str, Any]], constraints: List[Constraint]
    ) -> List[Dict[str, Any]]:
        """Forces hard, no-sport constraints onto a freshly generated workout list (§6): a
        blanket hard window replaces its dates with a single rest, deterministically,
        bypassing the LLM for that date entirely. A hard constraint scoped to a sport is
        advisory only — left to the model via the prompt block, not enforced here (§5).
        Operates only on dates the model actually scheduled, so it never invents days
        beyond the generated span."""
        full_rest = cls._hard_rest_windows(constraints)
        if not full_rest:
            return workouts
        out: List[Dict[str, Any]] = []
        rested: set = set()
        for w in workouts:
            day = w.get('date', '')
            fr = next((t for (s, e, t) in full_rest if s <= day <= e), None)
            if fr is not None:
                if day not in rested:
                    rested.add(day)
                    out.append(cls._rest_workout(day, f"constraint '{fr}'"))
                continue
            out.append(w)
        return out

    @classmethod
    def _enforce_hard_constraints_adapt(
        cls, adapted: List[Dict[str, Any]], planned_workouts: List[Workout],
        constraints: List[Constraint], completed_keys: Optional[set], from_date: str
    ) -> List[Dict[str, Any]]:
        """Eases planned sessions to rest on hard, no-sport constraint dates (§6). A hard
        constraint scoped to a sport is advisory only (§5) — left to the model, not
        enforced here. Only touches sessions on or after `from_date` that aren't already
        rest or completed."""
        full_rest = cls._hard_rest_windows(constraints)
        if not full_rest:
            return adapted
        completed = completed_keys or set()
        rest_sport = canonical_sport('rest')

        forced_rest: Dict[str, str] = {}          # date -> constraint title
        for w in planned_workouts:
            day = w.get('date', '')
            if day < from_date:
                continue
            sport = canonical_sport(w.get('sport_type', ''))
            if sport == rest_sport or (day, sport) in completed:
                continue
            fr = next((t for (s, e, t) in full_rest if s <= day <= e), None)
            if fr is not None:
                forced_rest[day] = fr

        if not forced_rest:
            return adapted

        cleaned = []
        for w in adapted:
            day = w.get('date', '')
            if day in forced_rest:
                continue  # whole day replaced with rest below
            cleaned.append(w)

        for day, title in sorted(forced_rest.items()):
            cleaned.append(cls._rest_workout(day, f"constraint '{title}'"))
        return cleaned

    def workout_generate(
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

        # A regeneration normally replaces every workout from today onward, but a session
        # the athlete has already completed should be preserved as history rather than
        # overwritten. When today's planned workout is already in the books, start the
        # regenerated plan tomorrow and leave today's row (and its Calendar event) intact.
        gen_start_str = today_str
        if self._today_workout_completed(today_str, completed_activities):
            gen_start_str = (today_date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
            print(green(
                f"Today's workout is already completed — preserving it and regenerating "
                f"from {gen_start_str}."
            ))
        gen_start_obj = datetime.strptime(gen_start_str, "%Y-%m-%d").date()

        if end_date is not None:
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
            num_days = max(1, (end_date_obj - gen_start_obj).days)
        else:
            num_days = config.workout_generation_span_days

        constraints = self._db.get_constraints(gen_start_str)
        guidelines = self._load_science_guidelines()
        profile = config.user_profile

        # We need all objectives for _get_active_strategy_and_meso_text context
        objectives = self._db.get_objectives(status='active')
        strategy, meso_text = self._get_active_strategy_and_meso_text(
            objectives, objective_id=objective_id
        )
        learnings = self._get_learnings_text()

        plan_data = self.engine._workout_generate_logic(
            objectives=objectives,
            constraints=constraints,
            today_str=today_str,
            start_str=gen_start_str,
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

        # Guard the preserved day: when today's completed session is being kept, drop any
        # workout the model mistakenly dated before the generation start. save_workout
        # matches on date+sport, so a stray today-dated row would silently overwrite the
        # completed session we deliberately kept.
        if gen_start_str != today_str:
            workouts = [w for w in workouts if w.get('date', '') >= gen_start_str]

        # Deterministic hard-constraint pre-pass (DESIGN_constraints.md §6): a `hard`
        # constraint with no sport forces its dates to rest regardless of what the LLM
        # produced; a `hard` constraint scoped to a sport drops that sport on its dates
        # (other sports flow normally). Applied after generation so the guarantee holds
        # even if the model ignores the constraint block it was shown.
        workouts = self._enforce_hard_constraints_generate(workouts, constraints)

        # Archive (don't delete) future workouts from the previous plan so they can be
        # resurrected by `plan rollback`, and tear down their Calendar events first so the
        # old plan doesn't linger on the calendar (see DESIGN_plan_rollback.md). The
        # displaced rows keep their macrocycle_id tag for the matching rollback.
        for ew in self._db.archive_future_workouts(gen_start_str):
            if ew.get('google_event_id'):
                try:
                    self._calendar_syncer.delete_workout_event(ew['google_event_id'])
                except Exception as e:
                    print(red(f"Error deleting Google Calendar event: {e}"))

        saved_workouts: List[Workout] = []
        for w in workouts:
            wid = self._db.save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                duration_minutes=w.get('duration_minutes'),
                rpe=w.get('rpe'),
                tss=w.get('tss'),
                source='generated',
                macrocycle_id=macrocycle['id']
            )
            saved_workouts.append({
                'id': wid,
                'date': w['date'],
                'sport_type': w['sport_type'],
                'title': w['title'],
                'description': w['description'],
                'original_description': w['description'],
                'modification_reason': None,
                'google_event_id': None,
                'duration_minutes': w.get('duration_minutes'),
                'rpe': w.get('rpe'),
                'tss': w.get('tss')
            })

        print(green(f"Generated {len(workouts)} workouts."))
        # Eager sync: push the new plan to Google Calendar straight away so the calendar
        # always mirrors the active plan (the old events were just torn down). Rollback is
        # the symmetric inverse (see DESIGN_plan_rollback.md).
        if saved_workouts:
            try:
                self._calendar_syncer.sync_multiple(saved_workouts)
                print(green("Synced new workouts to Google Calendar."))
            except Exception as e:
                print(red(f"Error syncing to Google Calendar: {e}"))
        return plan_data.get("reasoning", "Plan generated."), saved_workouts

    def plan_rollback(
        self, objective_id: Optional[int] = None, target_macrocycle_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Restores an earlier periodization plan version and its workouts.

        Swaps the active macrocycle for `objective_id` (defaults to the next active goal)
        back to a superseded version — the chronologically previous one by default, or
        `target_macrocycle_id` when given — then reconciles workouts and Calendar
        symmetrically with eager generation: the current plan's future workouts are
        archived (their events torn down) and the restored version's archived workouts are
        resurrected and re-pushed (see DESIGN_plan_rollback.md).

        Returns a summary dict: {from, to, restored_workouts, archived_workouts}.
        Raises ValueError when there is nothing to roll back to.
        """
        if objective_id is not None:
            objective = self._db.get_objective(objective_id)
        else:
            objective = self._db.get_active_objective()
        if not objective:
            raise ValueError("No goal found to roll back.")

        current = self._db.get_macrocycle_for_objective(objective['id'])
        if not current:
            raise ValueError(
                f"Goal '{objective['title']}' has no active plan to roll back."
            )

        if target_macrocycle_id is not None:
            target = self._db.get_macrocycle(target_macrocycle_id)
            if not target or target.get('objective_id') != objective['id']:
                raise ValueError(
                    f"Plan version {target_macrocycle_id} does not belong to goal "
                    f"'{objective['title']}'."
                )
            if target_macrocycle_id == current['id']:
                raise ValueError("That plan version is already active.")
        else:
            target = self._db.get_previous_macrocycle(objective['id'])
            if not target:
                raise ValueError(
                    f"Goal '{objective['title']}' has no earlier plan version to roll "
                    "back to."
                )

        today_str = _today_str()

        # 1. Archive the current plan's future workouts and tear down their events.
        archived = self._db.archive_future_workouts(today_str)
        for ew in archived:
            if ew.get('google_event_id'):
                try:
                    self._calendar_syncer.delete_workout_event(ew['google_event_id'])
                except Exception as e:
                    print(red(f"Error deleting Google Calendar event: {e}"))

        # 2. Flip the active version so date->plan lookups resolve to the restored plan.
        self._db.set_active_macrocycle(target['id'])

        # 3. Resurrect the restored version's workouts and re-push them.
        restored = self._db.restore_macrocycle_workouts(target['id'])
        if restored:
            try:
                self._calendar_syncer.sync_multiple(restored)
            except Exception as e:
                print(red(f"Error syncing to Google Calendar: {e}"))

        return {
            'objective': objective,
            'from': current,
            'to': target,
            'restored_workouts': len(restored),
            'archived_workouts': len(archived),
        }

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

        self.plan_generate(force=force, objective_id=objective_id)
        return self.workout_generate(objective_id=objective_id)

    def _adapt_is_change(self, proposal: Dict[str, Any]) -> bool:
        """True unless `proposal` exactly reproduces an existing same-sport session.

        Backstops the adaptation prompt's "return only changed sessions" rule: a verbatim
        (or cosmetic-only) re-list of an unchanged session is treated as a no-op so it is
        not re-stamped as adapted or re-synced. A sport swap (no same-sport original) or a
        proposal on a date with no current session is always a real change.
        """
        existing = self._db.get_workout(proposal['date'], proposal['sport_type'])
        if not existing:
            return True
        if canonical_sport(existing['sport_type']) != canonical_sport(proposal['sport_type']):
            return True

        def _norm_text(v: Any) -> str:
            return " ".join(str(v or "").split())

        def _norm_num(v: Any) -> Optional[float]:
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        if _norm_text(proposal.get('title')) != _norm_text(existing.get('title')):
            return True
        if _norm_text(proposal.get('description')) != _norm_text(existing.get('description')):
            return True
        for field in ('duration_minutes', 'rpe', 'tss'):
            if _norm_num(proposal.get(field)) != _norm_num(existing.get(field)):
                return True
        return False

    def workout_adapt(
        self, target_date_str: Optional[str] = None, message: Optional[str] = None
    ) -> Tuple[str, List[Workout], List[Dict[str, Any]]]:
        """Evaluates metrics/activities over a rolling window and adapts mesocycle if needed.

        `message` is an optional free-text note from the athlete, passed to the SAME LLM
        call as advisory intent for today (DESIGN_constraints.md §8 — no separate
        classification pass). That one call may also extract constraint-shaped directives
        from the note; they come back as the third element, raw and UNCONFIRMED — the
        caller must confirm each with the athlete (echo + y/N) before persisting it via
        `capture_message_constraint`, per the two-confirmation flow. Nothing here creates
        a constraint row or triggers a plan regen on its own.
        """
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

        # External daily-context signals over the window, so the adaptation can tell a
        # lifestyle-suppressed morning (alcohol/poor sleep the day before) from genuine
        # training fatigue and avoid cutting load on a non-training artifact. Recovery
        # lags the signal by a day, so reach one day before the metrics window to cover
        # the first morning's preceding-day signal.
        context_start_str = (start_date_obj - timedelta(days=1)).strftime("%Y-%m-%d")
        daily_context = self._db.get_daily_context(
            start_date=context_start_str, end_date=target_date_str
        )

        # Determine mesocycle end date for the adaptation range. The plan we adapt runs
        # FORWARD from the target date to here, so workouts must be fetched across the
        # whole span (lookback start -> mesocycle end), not just the backward window —
        # otherwise the LLM never sees already-scheduled future sessions and reinvents
        # them from scratch (losing their sport/title and overwriting the athlete's plan).
        active_meso = self._db.get_active_mesocycle(target_date_str)
        if active_meso:
            meso_end_date_str = active_meso['end_date']
        else:
            meso_end_date_str = (target_date_obj + timedelta(days=6)).strftime("%Y-%m-%d")

        window_workouts = self._db.get_workouts(
            start_date=start_date_str, end_date=meso_end_date_str, include_removed=True
        )
        planned_workouts = [w for w in window_workouts if not w.get('removed')]
        removed_workouts = [w for w in window_workouts if w.get('removed')]

        baseline = self._db.get_baseline(target_date_str)
        baseline_str = format_baseline(baseline)

        # Planned blocks overlapping the window. Activities on dates outside every block
        # are history the plan never governed (e.g. before tool adoption), so they are
        # reported as informational rather than as "unplanned" deviations.
        covered_ranges = self._db.get_mesocycle_ranges(start_date_str, target_date_str)

        # Match planned workouts vs completed activities and compute discrepancies.
        # analyze_adherence only inspects the `history_days` backward window, so the
        # future-dated workouts now in `planned_workouts` are ignored here (no false misses).
        discrepancies, matching_results, informational = analyze_adherence(
            planned_workouts=planned_workouts,
            completed_activities=completed_activities,
            start_date_obj=start_date_obj,
            history_days=history_days,
            minor_activity_load_threshold=config.minor_activity_load_threshold,
            covered_ranges=covered_ranges,
        )

        # Sessions that already have a matching completed Garmin activity are history and
        # cannot be adapted: the evaluation date is the first day of the adaptation range,
        # but the athlete may have already trained today. Without this lock the LLM
        # "adapts" a finished session (typically restating it to match the actual ride),
        # which is meaningless and, on apply, rewrites a past calendar event.
        completed_keys = {
            (m["date"], canonical_sport(m["planned"]["sport_type"]))
            for m in matching_results if m["completed"] is not None
        }

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

        # Active constraints overlapping the adaptation window (target date → mesocycle
        # end), the single directive read path shared with generate (§6).
        constraints = self._db.get_constraints(target_date_str, meso_end_date_str)

        # §8: the athlete's note is passed straight through as advisory intent — no
        # separate classification pass. The same LLM call also extracts any
        # constraint-shaped directives from it (see `new_constraints` below).
        decision = self.engine._workout_adapt_logic(
            target_date_str=target_date_str,
            history_days=history_days,
            start_date_str=start_date_str,
            metrics=metrics,
            completed_activities=completed_activities,
            planned_workouts=planned_workouts,
            baseline_str=baseline_str,
            meso_end_date_str=meso_end_date_str,
            objectives=objectives,
            guidelines=guidelines,
            profile=profile,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            discrepancies=discrepancies,
            informational=informational,
            removed_workouts=removed_workouts,
            daily_context=daily_context,
            completed_keys=completed_keys,
            athlete_message=message,
            constraints=constraints
        )

        # NOTE: daily adaptation is read-only w.r.t. coach learnings
        # (DESIGN_evidence_based_confidence.md §2/§11). It consumes the rendered learnings as
        # context but authors none — durable, evidence-backed observations are written only by
        # the weekly history analysis (`data bootstrap` / `data reflect`), which can attribute
        # them to specific training weeks.

        reason = decision.get("reason", "No adaptation needed.")
        adapted = []
        if decision.get("change_needed"):
            adapted = decision.get("adapted_workouts", [])

        # Drop any proposal that targets an already-completed session — those are locked
        # history (see completed_keys above). This is the load-bearing guard: it holds even
        # if the model ignores the prompt instruction not to adapt finished sessions.
        if completed_keys:
            adapted = [
                w for w in adapted
                if (w.get("date"), canonical_sport(w.get("sport_type", "")))
                not in completed_keys
            ]

        # No-op backstop: the prompt tells the model to return ONLY changed sessions, but
        # if it re-lists one verbatim (or with cosmetic-only churn) anyway, drop it here so
        # an unchanged session is never re-stamped as adapted or needlessly re-synced. A
        # proposal is a real change unless it matches an EXISTING same-sport session on every
        # meaningful field; a sport swap (no same-sport original) or a brand-new date always
        # counts as a change and is kept.
        adapted = [w for w in adapted if self._adapt_is_change(w)]

        # Deterministic hard-constraint pre-pass (§6): force rest onto any future,
        # not-yet-completed planned session that falls under a hard constraint, so the
        # guarantee holds regardless of what the model proposed.
        adapted = self._enforce_hard_constraints_adapt(
            adapted, planned_workouts, constraints, completed_keys, target_date_str
        )

        # §8: constraint-shaped directives the same LLM call extracted from the athlete's
        # note, if any — raw and UNCONFIRMED. The caller must confirm each with the
        # athlete before persisting it (via capture_message_constraint); nothing here
        # writes a row.
        new_constraints = (decision.get("new_constraints") or []) if message else []

        # Filter and structure returned workouts
        return reason, [
            {
                'id': None,
                'date': w['date'],
                'sport_type': w['sport_type'],
                'title': w['title'],
                'description': w['description'],
                'original_description': w['description'],
                # Per-workout note; the long batch rationale travels separately as the
                # returned `reason` and is stamped onto adaptation_summary at apply time.
                # Fall back to the batch reason so an adapted session is never left with a
                # NULL modification_reason (the load-bearing "modified?" flag) if the model
                # omits a per-workout change_reason.
                'modification_reason': w.get('change_reason') or reason,
                'adaptation_summary': reason,
                'google_event_id': None,
                'duration_minutes': w.get('duration_minutes'),
                'rpe': w.get('rpe'),
                'tss': w.get('tss')
            } for w in adapted
        ], new_constraints

    def workout_adapt_apply(
        self, proposed_workouts: List[Dict[str, Any]], reason: str,
        start_date: str, end_date: str
    ) -> None:
        """Saves proposed adapted workouts, cleans up overridden ones, and syncs to Calendar."""
        # 1. Fetch all existing workouts in the adaptation range
        existing_workouts = self._db.get_workouts(
            start_date=start_date, end_date=end_date
        )

        # One timestamp for the whole run, stamped on every session it eases. Lets a
        # re-run see how recently (and how many times) each session was already adapted
        # and hold back from compounding the cut (see save_workout / the adapt prompt).
        adapted_at = datetime.now(timezone.utc).isoformat()

        # Group proposed workouts by date
        proposed_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for pw in proposed_workouts:
            proposed_by_date.setdefault(pw['date'], []).append(pw)

        # 2. Find and delete existing workouts that are being replaced or removed.
        # A cross-sport swap (e.g. strength -> yoga) deletes the planned session and
        # inserts a fresh one, which would otherwise lose all trace of what was planned.
        # Remember the displaced session per date so its replacement can carry the
        # originally-planned description + load through to the Calendar event.
        displaced_by_date: Dict[str, Dict[str, Any]] = {}
        for ew in existing_workouts:
            ew_date = ew['date']
            if ew_date in proposed_by_date:
                # Compare canonically so a proposal for 'strength_training' is recognized
                # as adapting an existing 'strength' session (not overriding/deleting it).
                proposed_sports = {
                    canonical_sport(p['sport_type']) for p in proposed_by_date[ew_date]
                }
                if canonical_sport(ew['sport_type']) not in proposed_sports:
                    print(yellow(f"Removing overridden workout: {ew['title']} ({ew['sport_type']}) "
                          f"on {ew_date}"))
                    displaced_by_date.setdefault(ew_date, ew)
                    if ew.get('google_event_id'):
                        try:
                            self._calendar_syncer.delete_workout_event(ew['google_event_id'])
                        except Exception as e:
                            print(red(f"Error deleting Google Calendar event: {e}"))
                    self._db.delete_workout_by_id(ew['id'])

        # 3. Save new adapted workouts and sync them
        for w in proposed_workouts:
            existing = self._db.get_workout(w['date'], w['sport_type'])
            orig_desc = None
            orig_dur = orig_tss = orig_rpe = None
            ge_id = None
            if existing:
                orig_desc = existing['original_description'] or existing['description']
                ge_id = existing['google_event_id']
            else:
                # No same-sport session to adapt: this proposal swapped in a new sport.
                # If it displaced a planned session that day, inherit that session's
                # initially-planned description and load as this one's "original" snapshot,
                # so the Calendar event surfaces what was originally on the plan.
                displaced = displaced_by_date.get(w['date'])
                if displaced:
                    orig_desc = (
                        displaced.get('original_description') or displaced.get('description')
                    )
                    orig_dur = (
                        displaced.get('original_duration_minutes')
                        or displaced.get('duration_minutes')
                    )
                    orig_tss = displaced.get('original_tss') or displaced.get('tss')
                    orig_rpe = displaced.get('original_rpe') or displaced.get('rpe')
            # Origin is fixed at creation: preserve it when adapting an existing
            # session (None + COALESCE keeps 'manual'/'generated'); only a session
            # adapt newly introduces is coach-authored ('generated').
            source = None if existing else 'generated'

            self._db.save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                original_description=orig_desc or w['description'],
                original_duration_minutes=orig_dur,
                original_tss=orig_tss,
                original_rpe=orig_rpe,
                modification_reason=w.get('modification_reason'),
                adaptation_summary=reason,
                google_event_id=ge_id,
                duration_minutes=w.get('duration_minutes'),
                rpe=w.get('rpe'),
                tss=w.get('tss'),
                source=source,
                adapted_at=adapted_at
            )

            # Sync to Google Calendar
            updated = self._db.get_workout(w['date'], w['sport_type'])
            if updated:
                try:
                    self._calendar_syncer.sync_workout(updated)
                except Exception as e:
                    print(red(f"Error syncing {w['title']} to Google Calendar: {e}"))


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

    def workout_swap_validate(self, swap_ops: List[Dict[str, Any]]) -> List[str]:
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

    def workout_swap_apply(
        self, swap_ops: List[Dict[str, Any]], no_sync: bool = False,
        reason: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Applies the date changes for a swap and syncs the moved workouts.

        Reads each workout's original date before moving it (ops reference distinct ids,
        so reads stay correct across the loop). The athlete's optional `reason` is folded
        into each moved workout's `modification_reason` so the coach sees why the swap
        happened. Returns the updated workout records.
        """
        updated_workouts = []
        for op in swap_ops:
            workout = self._db.get_workout_by_id(op['id'])
            if not workout:
                continue
            mod_reason = f"{SWAP_REASON_PREFIX}{workout['date']} to {op['new_date']}"
            if reason:
                mod_reason += f". Reason given: {reason}"
            self._db.update_workout_date(op['id'], op['new_date'], mod_reason)
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

    def workout_add(
        self, date: str, sport_type: str, title: str, description: str,
        duration_minutes: Optional[int] = None, rpe: Optional[int] = None,
        tss: Optional[int] = None, reason: Optional[str] = None,
        replace_day: bool = False,
    ) -> Tuple[Optional[Workout], List[Workout]]:
        """Manually schedules a workout on `date`, replacing existing sessions that day.

        By default only an existing workout of the *same sport* is replaced, so other
        sports scheduled that day are left untouched. Pass `replace_day=True` to instead
        replace every session that day regardless of sport.

        When sessions are replaced, what was overwritten is recorded on the new
        workout — and therefore on its calendar event — mirroring how `adapt`
        annotates a changed session: the replaced description lands in
        `original_description` (rendered as "Originally:") and each replaced session's
        title plus duration/TSS/RPE are folded into `modification_reason` (rendered as
        "Reason:"). The old rows are deleted before the insert so omitted stats don't
        inherit a replaced session's values. The same-sport session's `google_event_id`
        (if any) is carried onto the new row so its existing calendar event is updated
        in place rather than orphaned; any other replaced sessions' calendar events are
        deleted.

        Returns (saved_workout, list_of_replaced_workouts).
        """
        # Store the coach's canonical sport name (e.g. 'strength' -> 'strength_training')
        # so manual sessions match the vocabulary that generate/adapt speak.
        sport_type = canonical_sport(sport_type)
        if replace_day:
            existing_all = self._db.get_workouts(start_date=date, end_date=date)
        else:
            same = self._db.get_workout(date, sport_type)
            existing_all = [same] if same else []

        # The same-sport session (if any) lends its calendar event to the new workout.
        primary = next(
            (w for w in existing_all
             if canonical_sport(w['sport_type']) == sport_type),
            None,
        )

        orig_desc = description
        ge_id = None
        if primary:
            orig_desc = primary['description']
            ge_id = primary.get('google_event_id')

        headers: List[str] = []
        for w in existing_all:
            stat_parts: List[str] = []
            if w.get('duration_minutes') is not None:
                stat_parts.append(f"{w['duration_minutes']}m")
            if w.get('tss') is not None:
                stat_parts.append(f"TSS {w['tss']}")
            if w.get('rpe') is not None:
                stat_parts.append(f"RPE {w['rpe']}")
            header = w['title']
            if w is not primary:
                header = f"{w['sport_type']} {header}"
            if stat_parts:
                header += f" ({', '.join(stat_parts)})"
            headers.append(header)
            # Other-sport events can't be reused by the new (single) workout; drop them.
            if w is not primary and w.get('google_event_id'):
                try:
                    self._calendar_syncer.delete_workout_event(w['google_event_id'])
                except Exception as e:
                    print(red(f"Error deleting replaced calendar event: {e}"))
            self._db.delete_workout_by_id(w['id'])

        mod_reason = None
        if headers:
            label = "sessions" if len(headers) > 1 else "session"
            mod_reason = f"{MANUAL_REPLACE_REASON_PREFIX}{label}: " + "; ".join(headers)
            if reason:
                mod_reason += f". Reason given: {reason}"

        self._db.save_workout(
            date=date,
            sport_type=sport_type,
            title=title,
            description=description,
            original_description=orig_desc,
            modification_reason=mod_reason,
            google_event_id=ge_id,
            duration_minutes=duration_minutes,
            rpe=rpe,
            tss=tss,
            source='manual',
        )

        saved = self._db.get_workout(date, sport_type)
        if saved:
            try:
                self._calendar_syncer.sync_workout(saved)
                saved = self._db.get_workout(date, sport_type)
            except Exception as e:
                print(red(f"Error syncing {title} to Google Calendar: {e}"))
        return saved, existing_all

    def _resolve_until(self, until_date_str: Optional[str]):
        until_date = _today_date()
        if until_date_str:
            until_date = datetime.strptime(until_date_str, "%Y-%m-%d").date()
        return until_date

    def _set_reflect_watermark(self, through_date: str) -> None:
        """Records how far `reflect` has consumed evidence. Reuses the generic
        `sync_state` table under the 'reflect' key (through_date = reflected-through
        day, last_pull_utc = run timestamp)."""
        self._db.set_sync_state(
            through_date=through_date,
            last_pull_utc=datetime.now(timezone.utc).isoformat(),
            key="reflect",
        )

    def _advance_reflect_watermark(self, through_date: str) -> None:
        """Advances the reflect watermark forward only, so a back-dated --from/--until
        run cannot rewind it and cause future reflects to re-ingest old evidence."""
        existing = self._db.get_sync_state("reflect")
        if existing and existing.get("through_date") and existing["through_date"] >= through_date:
            return
        self._set_reflect_watermark(through_date)

    def _record_bootstrap_run(self, through_date: str) -> None:
        """Marks that a cold-start reconstruction has completed, under the 'bootstrap'
        `sync_state` key. Distinct from the 'reflect' watermark (which `reflect` also
        advances): this records specifically that `bootstrap` itself has run, so a repeat
        invocation can detect it and avoid re-paying for the full reconstruction (and
        rewinding the reflect baseline) by accident."""
        self._db.set_sync_state(
            through_date=through_date,
            last_pull_utc=datetime.now(timezone.utc).isoformat(),
            key="bootstrap",
        )

    def data_bootstrap(
        self, from_date_str: Optional[str] = None, until_date_str: Optional[str] = None,
        days: Optional[int] = None, weeks: Optional[int] = None,
        context: Optional[str] = None, force: bool = False, inspect_only: bool = False,
        no_pull: bool = False, force_pull: bool = False, auto: bool = False
    ) -> Dict[str, Any]:
        """Cold-start backward reconstruction over the full training backlog.

        Reverse-engineers the macro/mesocycle structure and seeds initial coach
        learnings from history. Run once when onboarding (or after a long gap); `reflect`
        then handles the incremental stream. With no date filter, the window is
        auto-detected from the active goal (since the previous goal, else 12 weeks back).

        Establishes the reflect watermark at the window end, so subsequent `reflect` runs
        only ingest evidence newer than this — preventing the same backlog from being
        re-counted (and confidence ratcheted to 'established') on every run.

        Because this is a once-per-onboarding operation, a repeat invocation is detected
        (via the 'bootstrap' `sync_state` key) and confirmed before re-running: `--force`
        proceeds, `--auto` skips, and `--inspect-only` is read-only so it's never gated.
        """
        until_date = self._resolve_until(until_date_str)

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

        # Bootstrap is a once-per-onboarding reconstruction. If it has already run, a
        # repeat is almost always unintended: it re-pays for the full LLM pass and resets
        # the reflect baseline (potentially rewinding it). Confirm before re-running —
        # `--force` is the explicit "yes, redo it" signal; `--inspect-only` is read-only
        # and harmless so it's never gated.
        prior = self._db.get_sync_state("bootstrap")
        if prior and not force and not inspect_only:
            ran_on = (prior.get("last_pull_utc") or "")[:10] or "?"
            print(
                yellow(f"Bootstrap already ran on {ran_on} (through {prior.get('through_date')}). "
                       "For incremental updates use ")
                + green("'data reflect'") + yellow(" instead.")
            )
            if auto:
                print(cyan("Skipping bootstrap (pass --force to re-run)."))
                return {}
            import trainmate_cli as cli
            if not cli.prompt.confirm("Re-run the full bootstrap anyway?"):
                print(cyan("Bootstrap skipped."))
                return {}

        decision = self._run_workout_analysis(
            from_date, until_date, context=context, force=force,
            inspect_only=inspect_only, no_pull=no_pull, force_pull=force_pull,
            horizon="long", label="data_bootstrap",
        )
        # Establish the reflect baseline and record the bootstrap run (read-only inspect
        # mode writes nothing).
        if not inspect_only:
            self._set_reflect_watermark(until_date.strftime("%Y-%m-%d"))
            self._record_bootstrap_run(until_date.strftime("%Y-%m-%d"))
            self._review_learning_proposals(auto=auto)
        return decision

    def data_reflect(
        self, from_date_str: Optional[str] = None, until_date_str: Optional[str] = None,
        days: Optional[int] = None, weeks: Optional[int] = None,
        context: Optional[str] = None, force: bool = False, inspect_only: bool = False,
        no_pull: bool = False, force_pull: bool = False, auto: bool = False
    ) -> Dict[str, Any]:
        """Incremental reflection over evidence accrued since the last reflect.

        Unlike `bootstrap`, the window starts at the reflect watermark (the day after the
        last reflected-through date) unless an explicit date filter is given, so
        overlapping history is never re-counted — the root cause of confidence converging
        to 'established' when the command was run day after day over a sliding window.
        Advances the watermark on success.

        Reuse / integrity invariants are inherited from `_run_workout_analysis`.
        """
        until_date = self._resolve_until(until_date_str)

        explicit = bool(from_date_str or days or weeks)
        watermark = self._db.get_sync_state("reflect")
        if from_date_str:
            from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()
        elif days:
            from_date = until_date - timedelta(days=days - 1)
        elif weeks:
            from_date = until_date - timedelta(weeks=weeks) + timedelta(days=1)
        elif watermark and watermark.get("through_date"):
            from_date = (
                datetime.strptime(watermark["through_date"], "%Y-%m-%d").date()
                + timedelta(days=1)
            )
        else:
            # No watermark yet (bootstrap not run). Reflect over a recent default window
            # rather than dead-ending, but nudge the user toward bootstrap.
            from_date = (
                until_date - timedelta(weeks=DEFAULT_REFLECT_WEEKS) + timedelta(days=1)
            )
            print(yellow(
                f"No reflect baseline found; reflecting over the last {DEFAULT_REFLECT_WEEKS} "
                f"weeks. Run '{green('data bootstrap')}' to reconstruct your full training "
                "history first."
            ))

        if from_date > until_date:
            if explicit:
                raise ValueError(f"Start date {from_date} is after end date {until_date}.")
            since = watermark["through_date"] if watermark else "?"
            print(cyan(f"Nothing new to reflect on since {since}."))
            # Even with no new evidence, surface any staleness demotions that have come due.
            if not inspect_only:
                self._review_learning_proposals(auto=auto)
            return {}

        decision = self._run_workout_analysis(
            from_date, until_date, context=context, force=force,
            inspect_only=inspect_only, no_pull=no_pull, force_pull=force_pull,
            horizon="short", label="data_reflect",
        )
        if not inspect_only:
            self._advance_reflect_watermark(until_date.strftime("%Y-%m-%d"))
            self._review_learning_proposals(auto=auto)
        return decision

    @staticmethod
    def _week_constraints(
        constraints: List[Constraint], week_start, week_end
    ) -> List[Dict[str, Any]]:
        """Constraints overlapping [week_start, week_end] (date objects), each tagged
        'full' (spans the whole in-window week) or 'partial'. Fed to the analysis as
        discounting context only — a constraint may explain an anomaly away but is never
        cited as supporting evidence (DESIGN_constraints.md §2, §6). Dates are ISO strings
        so lexicographic comparison is chronological."""
        ws, we = week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")
        out: List[Dict[str, Any]] = []
        for c in constraints:
            start, end = c.get('start_date'), c.get('end_date')
            if not start or not end or start > we or end < ws:
                continue  # missing dates or no overlap with this week
            out.append({
                "title": c.get('title'),
                "type": c.get('type'),
                "impact": c.get('description') or "",
                "coverage": "full" if (start <= ws and end >= we) else "partial",
            })
        return out

    # Channels of the per-day z, mapping the metric column to its baseline mean/std keys.
    # Sign convention (documented for the LLM): +hrv better, +rhr worse, +sleep better
    # (DESIGN_quantitative_context_impact.md §3).
    _RESPONSE_Z_CHANNELS = (
        ("rhr", "rhr", "rhr_baseline_mean", "rhr_baseline_std"),
        ("hrv", "hrv", "hrv_baseline_mean", "hrv_baseline_std"),
        ("sleep", "sleep_score", "sleep_baseline_mean", "sleep_baseline_std"),
    )

    @staticmethod
    def _day_response_z(
        metric_row: Dict[str, Any], baseline: Optional[Dict[str, Any]]
    ) -> Dict[str, Optional[float]]:
        """Baseline-relative z-score `(value - mean) / std` for ONE morning's rhr/hrv/sleep
        — the shared definition of "notches from normal" used by both the weekly feature
        (averaged over the week) and the context-impact alignment (per morning). A channel
        is None when its metric value is missing, the baseline mean/std is missing, or std
        is zero (undefined). Unrounded; callers round as they emit
        (DESIGN_quantitative_context_impact.md §3)."""
        out: Dict[str, Optional[float]] = {}
        for channel, metric_key, mean_key, std_key in CoachService._RESPONSE_Z_CHANNELS:
            mean = baseline.get(mean_key) if baseline else None
            std = baseline.get(std_key) if baseline else None
            value = metric_row.get(metric_key)
            if mean is None or not std or value is None:
                out[channel] = None  # missing baseline/value or zero std -> undefined
            else:
                out[channel] = (value - mean) / std
        return out

    @staticmethod
    def _week_response_features(
        w_metrics: List[Dict[str, Any]], baseline: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Deterministic body-response features for one week: mean sleep/stress, and
        baseline-relative z-scores `(value - mean) / std` for rhr/hrv/sleep averaged over
        the week's days. A component is None when its baseline mean/std is missing or std
        is zero (undefined), or no metric day carries the value; `vs_baseline_z` is only
        included when at least one component is computable (§3)."""
        def _avg(key: str) -> Optional[float]:
            vals = [m[key] for m in w_metrics if m.get(key) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        def _z(channel: str) -> Optional[float]:
            vals = [
                z for z in (
                    CoachService._day_response_z(m, baseline)[channel] for m in w_metrics
                ) if z is not None
            ]
            return round(sum(vals) / len(vals), 2) if vals else None

        vs_baseline_z = {
            "rhr": _z("rhr"),
            "hrv": _z("hrv"),
            "sleep": _z("sleep"),
        }
        features: Dict[str, Any] = {
            "avg_sleep_score": _avg("sleep_score"),
            "avg_stress": _avg("stress"),
        }
        if any(v is not None for v in vs_baseline_z.values()):
            features["vs_baseline_z"] = vs_baseline_z
        return features

    # Response channels to omit for a context signal whose own construct overlaps them,
    # so the LLM cannot "discover" that bad sleep predicts bad sleep — an echo, not an
    # impact (DESIGN_quantitative_context_impact.md §3.2). Keyed by a substring of the
    # opaque, free-form metric name; the common signals (alcohol, meals) match nothing
    # and exclude nothing.
    _CONTEXT_CHANNEL_EXCLUSIONS = (
        ("sleep", {"sleep"}),
    )

    @staticmethod
    def _norm_signal_value(value: Optional[float]):
        """Pass a logged signal magnitude through for display: drop float noise on whole
        numbers (4.0 -> 4) so doses read cleanly, keep fractional values rounded; None
        (presence-only / unparseable) stays None (§3.1). TrainMate never interprets the
        scale — it only tidies the number."""
        if value is None:
            return None
        f = float(value)
        return int(f) if f.is_integer() else round(f, 2)

    @staticmethod
    def _context_days(
        daily_context: List[Dict[str, Any]],
        metrics: List[Dict[str, Any]],
        activities: List[Dict[str, Any]],
        baseline_for,
        k: int,
        min_signal_days: int = 1,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Deterministic per-episode alignment of external context signals against the
        mornings that bracket them (DESIGN_quantitative_context_impact.md §3–§4). NO
        statistics: pure clustering + join + the existing per-day z.

        For each signal category it (1) clusters the logged signal-days into *episodes* —
        maximal runs separated by fewer than `k` drink-free days (§3.0); (2) emits, per
        episode, the `days` dose sequence (each signal-day's magnitude + that day's
        training load) and the `surrounding_mornings` strip spanning
        `(first − k + 1) … (last + k)`, each morning carrying its preceding day's load and
        the baseline-relative z of the recovery channels; (3) drops any response channel
        that duplicates the signal's own construct (§3.2). Categories below
        `min_signal_days` total signal-days are omitted (§5).

        `baseline_for(date_str)` returns the baseline valid on/just before that morning
        (or None). `k` is the look-ahead, `min_signal_days` the inclusion floor."""
        # Day-of training load: sum of derived load over the day's activities (0 on a rest
        # day), NOT the rolling acute EWMA — the stimulus for a morning is the day before it
        # (§3 "Day-of load").
        load_by_date: Dict[str, float] = {}
        for act in activities:
            load_by_date[act['date']] = load_by_date.get(act['date'], 0.0) + activity_load(act)

        def day_load(date_str: str) -> int:
            return int(round(load_by_date.get(date_str, 0.0)))

        metric_by_date = {m['date']: m for m in metrics}

        # Aggregate signal magnitude per (category, date): a day may carry more than one
        # row of the same category (combined dose); None when no row supplies a value.
        per_cat: Dict[str, Dict[str, Optional[float]]] = {}
        for c in daily_context:
            cat = c.get('metric')
            if not cat:
                continue
            day_map = per_cat.setdefault(cat, {})
            v = c.get('value')
            if v is not None:
                day_map[c['date']] = (day_map.get(c['date']) or 0.0) + float(v)
            else:
                day_map.setdefault(c['date'], None)

        out: Dict[str, List[Dict[str, Any]]] = {}
        for cat in sorted(per_cat.keys()):
            day_map = per_cat[cat]
            signal_dates = sorted(day_map.keys())
            if len(signal_dates) < min_signal_days:
                continue  # too few signal-days to be worth prompting on (§5)

            excluded: set = set()
            cat_l = cat.lower()
            for sub, chans in CoachService._CONTEXT_CHANNEL_EXCLUSIONS:
                if sub in cat_l:
                    excluded |= chans

            # Cluster signal-days into episodes: two consecutive signal-days join the same
            # episode when fewer than k drink-free days separate them (§3.0).
            episodes: List[List[str]] = []
            run = [signal_dates[0]]
            for prev, cur in zip(signal_dates, signal_dates[1:]):
                gap_free = (
                    datetime.strptime(cur, "%Y-%m-%d").date()
                    - datetime.strptime(prev, "%Y-%m-%d").date()
                ).days - 1
                if gap_free < k:
                    run.append(cur)
                else:
                    episodes.append(run)
                    run = [cur]
            episodes.append(run)

            rows: List[Dict[str, Any]] = []
            for ep in episodes:
                first = datetime.strptime(ep[0], "%Y-%m-%d").date()
                last = datetime.strptime(ep[-1], "%Y-%m-%d").date()
                days = [
                    {
                        "date": d,
                        "value": CoachService._norm_signal_value(day_map[d]),
                        "load_tss": day_load(d),
                    }
                    for d in ep
                ]
                mornings = []
                cur = first - timedelta(days=k - 1)
                end = last + timedelta(days=k)
                while cur <= end:
                    m_str = cur.strftime("%Y-%m-%d")
                    prev_str = (cur - timedelta(days=1)).strftime("%Y-%m-%d")
                    zs = CoachService._day_response_z(
                        metric_by_date.get(m_str, {}), baseline_for(m_str)
                    )
                    vs_normal = {
                        ch: round(z, 2)
                        for ch, z in zs.items()
                        if z is not None and ch not in excluded
                    }
                    mornings.append({
                        "morning": m_str,
                        "prev_day_load_tss": day_load(prev_str),
                        "vs_normal": vs_normal,
                    })
                    cur += timedelta(days=1)
                rows.append({"days": days, "surrounding_mornings": mornings})

            out[cat] = rows
        return out

    def _run_workout_analysis(
        self, from_date, until_date,
        context: Optional[str] = None, force: bool = False, inspect_only: bool = False,
        no_pull: bool = False, force_pull: bool = False,
        horizon: str = "long", label: str = "workout_analysis"
    ) -> Dict[str, Any]:
        """Shared core for bootstrap/reflect: builds weekly summaries over
        [from_date, until_date], runs the LLM reconstruction, applies learning deltas,
        and caches the reconstruction under `horizon`.

        Backward-evaluation reuse (DESIGN_backward_evaluation.md §5, §8, §9):
        - The reconstruction is cached per `horizon`, keyed by an evidence fingerprint. If
          the evidence is unchanged since the last run and `force` is False, the cached
          reconstruction is returned without an LLM call.
        - `force` bypasses *reuse* only (recompute even if unchanged); it never bypasses
          the reinforcement integrity invariant — a forced re-run over unchanged evidence
          still suppresses the confidence/recency ratchet.
        - `inspect_only` is read-only: it renders the reconstruction but writes neither coach
          learnings nor the cache.
        """
        from_str = from_date.strftime("%Y-%m-%d")
        until_str = until_date.strftime("%Y-%m-%d")

        print(cyan(f"Analyzing activities from {from_str} to {until_str}..."))

        # Ensure Garmin data covers the analysis window (auto-pull recent/small gaps,
        # surface a command for large backfills) before reading it unless no_pull is True.
        if not no_pull:
            from trainmate import garmin
            garmin.ensure_data(from_str, until_str, force=force_pull)

        metrics = self._db.get_metrics_cache(start_date=from_str, end_date=until_str)
        completed_activities = self._db.get_completed_activities(
            start_date=from_str, end_date=until_str
        )
        # Constraints overlapping the window contextualize anomalies (illness/travel/work)
        # so the model doesn't misattribute them to training. get_constraints(start, end)
        # already returns only overlapping rows (DESIGN_constraints.md §6).
        constraints = self._db.get_constraints(from_str, until_str)
        # External daily context signals (alcohol, sleep, stress, …) ingested from the
        # calendar; they explain recovery anomalies the same way constraints explain load
        # ones (DESIGN_calendar_context_ingest.md §7).
        daily_context = self._db.get_daily_context(start_date=from_str, end_date=until_str)

        # Reuse path: if the evidence is unchanged since the last analysis, return the
        # cached reconstruction instead of paying for another LLM pass (unless --force).
        fingerprint = self.engine._get_evidence_fingerprint(
            completed_activities, metrics, from_str, until_str, constraints, daily_context
        )
        cached = self._db.get_analysis_cache(horizon)
        evidence_unchanged = bool(cached and cached.get("fingerprint") == fingerprint)
        if evidence_unchanged and not force and cached.get("reconstruction"):
            print(cyan("Evidence unchanged since last analysis; reusing cached reconstruction "
                  "(use --force to recompute)."))
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
                    "activities": [],
                    "daily_context": []
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

        for ctx in daily_context:
            c_date = datetime.strptime(ctx['date'], "%Y-%m-%d").date()
            monday = c_date - timedelta(days=c_date.weekday())
            monday_str = monday.strftime("%Y-%m-%d")
            if monday_str in weeks_data:
                weeks_data[monday_str]["daily_context"].append(ctx)

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

            # Deterministic body-response features + overlapping life events for this week
            # (DESIGN_richer_analysis_evidence.md §2–§3). The baseline moves slowly, so one
            # lookup per week (at the week's last in-window day) is enough.
            week_start, week_end = days_in_week[0], days_in_week[-1]
            baseline = self._db.get_baseline(week_end.strftime("%Y-%m-%d"))
            response = self._week_response_features(w_metrics, baseline)
            week_events = self._week_constraints(constraints, week_start, week_end)
            # All context signals for the week, handed to the LLM verbatim (no collapsing
            # of multiple metrics/day — DESIGN_calendar_context_ingest.md §7).
            week_context = sorted(
                (
                    {"date": c["date"], "metric": c["metric"],
                     "value": c["value"], "text": c.get("text")}
                    for c in w_info["daily_context"]
                ),
                key=lambda r: (r["date"], r["metric"]),
            )

            highlights = []
            for act in w_activities:
                is_hi = (
                    (act.get('tss') and act['tss'] >= config.high_intensity_tss_threshold) or
                    (act.get('rpe') and act['rpe'] >= config.high_intensity_rpe_threshold) or
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

            summary = {
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
                "avg_sleep_score": response["avg_sleep_score"],
                "avg_stress": response["avg_stress"],
                "max_acwr": round(max_acwr, 2) if max_acwr is not None else None,
                "rest_days": rest_days,
                "highlights": highlights,
                "constraints": week_events,
            }
            # Baseline-relative deviations, omitted whole when no metric/baseline supports
            # any component (so its absence reads as "no data", not "on baseline").
            if "vs_baseline_z" in response:
                summary["vs_baseline_z"] = response["vs_baseline_z"]
            # Emitted only when present so its absence reads as "no signals logged".
            if week_context:
                summary["daily_context"] = week_context
            weekly_summaries.append(summary)

        # Fetch relevant objectives (occurring on or after from_date)
        all_objectives = self._db.get_objectives()
        objectives = [
            obj for obj in all_objectives
            if datetime.strptime(obj['target_date'], "%Y-%m-%d").date() >= from_date
        ]

        guidelines = self._load_science_guidelines()
        profile = config.user_profile

        # Quantitative context-impact rows (alcohol, big meal, …): episode-aligned dose
        # sequences + bracketing morning strips. These cover the athlete's FULL history of
        # signal-days, not just [from,until] — the point is to let the LLM see the whole
        # pattern, and an incremental reflect window contains almost no drinking history
        # (DESIGN_quantitative_context_impact.md §6). So they are fetched independently of
        # the analysis window.
        context_days = self._context_days(
            daily_context=self._db.get_daily_context(),
            metrics=self._db.get_metrics_cache(),
            activities=self._db.get_completed_activities(),
            baseline_for=self._db.get_baseline,
            k=config.context_days_lookahead,
            min_signal_days=config.context_days_min_signal_days,
        )

        decision = self.engine._data_analyze_logic(
            objectives=objectives,
            guidelines=guidelines,
            profile=profile,
            weekly_summaries=weekly_summaries,
            context_days=context_days,
            learnings=self._get_learnings_text(),
            context=context,
            label=label
        )

        if not inspect_only:
            # Apply evidence-cited learning deltas. The LLM attributes observations to the
            # week_commencing weeks it was shown; the app derives confidence from the
            # accumulated distinct weeks, so re-citing counted weeks (forced re-runs,
            # overlapping windows) is a structural no-op — no reinforcement-suppression flag
            # needed (DESIGN_evidence_based_confidence.md §6, §8).
            available_weeks = sorted(weeks_data.keys())
            source = "bootstrap" if horizon == "long" else "reflect"
            self._apply_learning_updates(decision, available_weeks, source)
            # Cache the reconstruction (everything but the point-in-time deltas) so future
            # runs — and `plan generate` — can reuse it without another LLM call.
            reconstruction = {
                k: v for k, v in decision.items() if k != "learning_updates"
            }
            self._db.save_analysis_cache(
                horizon, fingerprint, from_str, until_str, reconstruction
            )

        return decision


# Singleton instance
coach_service = CoachService()
