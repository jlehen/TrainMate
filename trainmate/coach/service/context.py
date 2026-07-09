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


class PmcContextMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

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
            # PMC (CTL/ATL/TSB) + the single ramp line, so the strategy/plan prompt can
            # reason about current freshness and a sustainable build rate against the
            # science directives (DESIGN_pmc_fitness_fatigue.md §5.2).
            for pmc_line in self._pmc_summary_lines(metrics, today_str):
                lines.append("  " + pmc_line)
        else:
            lines.append("Physiological Metrics (Past 15 days):\n  - No metrics found.")

        return "\n".join(lines)

    # ------------------------------------------------------------------ PMC helpers
    def _pmc_summary_lines(
        self, metrics: List[Dict[str, Any]], as_of: str
    ) -> List[str]:
        """The PMC block for the data summary (strategy/plan prompt): the latest
        Fitness/Fatigue line, the single CTL ramp line, the §3.3(b) still-warming-up flag,
        and the TSB-lag footnote — each omitted when it has nothing to say. Order: values,
        then trust/caveat."""
        out: List[str] = []
        # History start (and the cutoff/caveat derived from it) is read ONCE here and
        # passed down — no helper below re-derives it.
        start = garmin.pmc_history_start(dbh=self._db)
        cutoff = garmin.pmc_warmup_cutoff_for(start, config.pmc_ctl_days)
        latest = self._pmc_latest_line(metrics, cutoff)
        if latest:
            out.append(latest)
        ramp = self._pmc_ramp_line(cutoff)
        if ramp:
            out.append(ramp)
        caveat = self._pmc_caveat_line(garmin.pmc_data_caveat(start, as_of))
        if caveat:
            out.append(caveat)
        # The footnote explains the TSB lag, so only a line actually showing TSB needs it.
        if latest and "TSB" in latest:
            out.append(PMC_TSB_LAG_NOTE)
        return out

    def _pmc_latest_line(
        self, metrics: List[Dict[str, Any]], warmup_cutoff: Optional[str]
    ) -> Optional[str]:
        """'- Fitness/Fatigue (PMC): CTL .. ATL .. TSB ..' from the latest past-warm-up
        row carrying PMC values, or None."""
        for m in reversed(metrics):
            if warmup_cutoff and m['date'] < warmup_cutoff:
                continue
            ctl, atl, tsb = m.get('ctl'), m.get('atl'), m.get('tsb')
            if ctl is None and atl is None and tsb is None:
                continue
            parts = []
            if ctl is not None:
                parts.append(f"CTL {ctl:.1f} (fitness)")
            if atl is not None:
                parts.append(f"ATL {atl:.1f} (fatigue)")
            if tsb is not None:
                parts.append(f"TSB {tsb:.1f} (form)")
            return "- Fitness/Fatigue (PMC): " + ", ".join(parts)
        return None

    def _pmc_ramp_line(self, cutoff: Optional[str]) -> Optional[str]:
        """The single '- CTL ramp rate: +4.2/week (last 7 days)' line, computed from the
        FULL stored CTL series (never a short prompt window, so a small window can't drop
        it — §3.1/§5.2). None inside the warm-up window, at a <7-day span edge, or when
        the -7d baseline itself lands in the warm-up zone (pmc_ramp's straddle guard)."""
        all_metrics = self._db.get_metrics_cache()
        ctl_by_date = {m['date']: m.get('ctl') for m in all_metrics}
        latest = None
        for m in all_metrics:
            if m.get('ctl') is None:
                continue
            if cutoff and m['date'] < cutoff:
                continue
            latest = m['date']
        if latest is None:
            return None
        ramp = garmin.pmc_ramp(ctl_by_date, latest, warmup_cutoff=cutoff)
        if ramp is None:
            return None
        return f"- CTL ramp rate: {ramp:+.1f}/week (last 7 days)"

    def _pmc_caveat_line(self, cav: Optional[Dict[str, Any]]) -> Optional[str]:
        """Renders the §3.3(b) static "still warming up" flag (a garmin.pmc_data_caveat
        dict, or None) as a summary line, stated as a *condition* (not an assertion that
        fitness is understated — a genuine beginner's low CTL is correct).

        While N < τ_ctl the ENTIRE history is still inside the §3.3(a) warm-up window, so
        every surface suppresses the values themselves; then this line explains the
        absence instead of caveating numbers the prompt doesn't contain."""
        if not cav:
            return None
        ctl_days = config.pmc_ctl_days
        if cav["n_days"] < ctl_days:
            return (
                f"- PMC (CTL/ATL/TSB): suppressed — only {cav['n_days']} days of "
                f"history; values are leading-edge warm-up artifacts for the first "
                f"{ctl_days} days."
            )
        return (
            f"- PMC data caveat: CTL is based on {cav['n_days']} days of history "
            f"(a {ctl_days}-day average needs months to settle). If the athlete "
            f"trained regularly before {cav['history_start']}, true fitness is higher "
            f"than shown and low TSB / high ramp are partly warm-up artifacts; if "
            f"they did not, the low values are real."
        )

    def _pmc_prompt_context(
        self, as_of: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """(warmup_cutoff, extra_lines) for the generate/adapt metrics block: the single
        ramp line and the still-warming-up flag — a single line each beside the per-day
        block, never repeated per day (§5.2). History start is read ONCE and passed down."""
        start = garmin.pmc_history_start(dbh=self._db)
        cutoff = garmin.pmc_warmup_cutoff_for(start, config.pmc_ctl_days)
        lines: List[str] = []
        ramp = self._pmc_ramp_line(cutoff)
        if ramp:
            lines.append(ramp)
        caveat = self._pmc_caveat_line(garmin.pmc_data_caveat(start, as_of))
        if caveat:
            lines.append(caveat)
        return cutoff, ("\n".join(lines) if lines else None)

    @staticmethod
    def _pmc_week_summary(
        w_metrics: List[Dict[str, Any]],
        ctl_by_date: Dict[str, Optional[float]],
        warmup_cutoff: Optional[str],
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """(end_ctl, week_ramp, min_tsb) for one week of the analysis digest (§5.4):
        end_ctl = last day's CTL, min_tsb = deepest overload, week_ramp = end_ctl vs CTL
        7 days earlier. Guards: a week entirely inside the warm-up window emits all None
        (no phantom overreach from seeding artifacts); week_ramp comes from pmc_ramp,
        which applies the §3.1 nearest-earlier interior-gap rule, the straddle guard
        (a -7d lookback before the cutoff would ramp off a warm-up baseline), and the
        first-week boundary omit (no baseline in the window slice)."""
        end_ctl = week_ramp = min_tsb = None
        past_warmup = [
            m for m in w_metrics
            if (warmup_cutoff is None or m['date'] >= warmup_cutoff)
        ]
        ctl_days = [m for m in past_warmup if m.get('ctl') is not None]
        if ctl_days:
            end_row = max(ctl_days, key=lambda m: m['date'])
            end_ctl = end_row['ctl']
            week_ramp = garmin.pmc_ramp(
                ctl_by_date, end_row['date'], warmup_cutoff=warmup_cutoff
            )
        tsbs = [m['tsb'] for m in past_warmup if m.get('tsb') is not None]
        if tsbs:
            min_tsb = min(tsbs)
        return end_ctl, week_ramp, min_tsb

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
