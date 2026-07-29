import json
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.db import db
from trainmate.google_calendar import calendar_syncer
from trainmate.types import Objective, Constraint, Workout
from trainmate.adherence import analyze_adherence, planned_load
from trainmate.sports import canonical_sport
from trainmate.modification_state import SWAP_REASON_PREFIX, MANUAL_REPLACE_REASON_PREFIX
from trainmate import garmin
from trainmate.garmin import activity_load
from trainmate.util import (
    today_str as _today_str, today_date as _today_date,
    cyan, green, yellow, bold, red, gray, PMC_TSB_LAG_NOTE,
)
from trainmate.coach.engine import CoachEngine, MIN_PLAN_WEEKS, MAX_PLAN_WEEKS
from trainmate.coach.formatting import format_baseline, _load_science_guidelines
import trainmate.coach.service as _svc


class PromptConfigMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

    def _get_config_hash(self) -> str:
        return self.engine._get_config_hash()

    def effective_thresholds(self) -> Dict[str, float]:
        """The athlete's current threshold anchors — the single set both the coaching
        prompt and the plan-staleness check read through (DESIGN_benchmark_workouts.md
        §3.3, the linchpin accessor).

        Trainable anchors (ftp, lthr, threshold_pace, css, e1rm, mas) come from the latest
        logbook row per kind (§3.2); quasi-fixed physiology (max_hr) stays in config (§3.4).
        No kind is privileged — logbook kinds and max_hr flow through identically (§3.5).
        A logbook value overrides a same-named config value should one linger.
        """
        thresholds: Dict[str, float] = {}
        max_hr = config.user_profile.get('max_hr')
        if max_hr is not None:
            thresholds['max_hr'] = float(max_hr)
        thresholds.update(self._db.latest_thresholds())
        return thresholds

    def _effective_profile(self) -> Dict[str, Any]:
        """`config.user_profile` with the effective threshold anchors overlaid, so every
        engine prompt call prescribes zones/targets from the live logbook values (§3.3).

        The profile dict is passed through as-is; the logbook thresholds simply overlay it.
        The code makes no assumption about which threshold keys the profile does or does not
        carry — a logbook value overrides a same-named profile key, and any other key rides
        through untouched."""
        return {**config.user_profile, **self.effective_thresholds()}

    def _get_config_snapshot(self) -> str:
        """JSON of the current effective threshold anchors, persisted on the macrocycle
        so config_changed() can judge later drift against real values."""
        return json.dumps(self.effective_thresholds(), sort_keys=True)

    def config_changed(self, macro: Dict[str, Any]) -> Optional[str]:
        """Whether config.yaml has drifted plan-shapingly since `macro` was generated.

        Returns a human-readable reason, or None when the plan is still current. Two
        axes (see engine._clean_profile): the fingerprint over plan-shaping profile
        fields, and the effective threshold anchors (§3.3), which only count as drift past
        `coach.threshold_replan_pct` relative change — a small FTP/LTHR retest
        correction feeds the next workout generation without invalidating the
        periodization strategy. Macrocycles predating the threshold snapshot judge on
        the fingerprint alone.

        Snapshot scope is uniform — every anchor kind on record joins it, ftp/lthr are not
        special (§3.5). A kind absent from the OLD snapshot is a *newly recorded* one (e.g.
        a first swim test): it starts feeding prompts immediately and joins drift-checking
        from the next generated plan, so it is skipped here rather than read as instant
        drift. A kind that *disappears* still reads as drift — a threshold the plan relied
        on going missing is real.
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

        current = self.effective_thresholds()
        tolerance = config.threshold_replan_pct / 100.0
        for key in sorted(set(old_thresholds) | set(current)):
            old_val, new_val = old_thresholds.get(key), current.get(key)
            if old_val is None:
                continue  # newly recorded kind — joins drift-checking from the next plan
            if new_val is None:
                return f"{key} was removed"
            if old_val and abs(new_val - old_val) / abs(old_val) > tolerance:
                pct = (new_val - old_val) / old_val * 100.0
                return f"{key} changed {old_val:g} → {new_val:g} ({pct:+.1f}%)"
        return None

    def _get_goals_hash(self, objectives: List[Objective]) -> str:
        return self.engine._get_goals_hash(objectives)

    def _get_constraints_hash(self, constraints: List[Constraint]) -> str:
        return self.engine._get_constraints_hash(constraints)

    def _load_science_guidelines(self) -> str:
        return _load_science_guidelines(_svc.config.app_science_dir, _svc.config.science_dir)

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

    def _maybe_nudge_no_threshold(self) -> None:
        """Cold-start hint when no trainable threshold is on record (§3.4).

        A fresh install has no FTP/LTHR/etc — the coach still plans, prescribing by RPE/HR
        feel (the prompt formatter and drift code both degrade gracefully on absent keys),
        and the very first plan schedules a benchmark to close the gap. So this nudges,
        never refuses. `max_hr` alone (config physiology) does not count as a threshold on
        record — the athlete still has no measured anchor."""
        recorded = {k for k in self.effective_thresholds() if k != 'max_hr'}
        if recorded:
            return
        print(yellow(
            "No fitness thresholds on record — prescriptions will use RPE/HR feel until "
            "you record one ("
        ) + green("'benchmark record …'") + yellow(
            ") or complete the scheduled benchmark."
        ))

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
        profile = self._effective_profile()
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
