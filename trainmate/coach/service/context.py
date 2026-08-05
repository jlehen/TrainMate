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
from trainmate import garmin, intensity, progression
from trainmate.benchmarks import ANCHOR_KINDS, format_value
from trainmate.garmin import activity_load
from trainmate.util import (
    today_str as _today_str, today_date as _today_date,
    cyan, green, yellow, bold, red, gray, wrap_text, days_between, PMC_TSB_LAG_NOTE,
)
from trainmate.coach.engine import CoachEngine
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

            lines.append("Physiological Metrics (15-day average):")
            if rhrs:
                lines.append(f"  - Resting Heart Rate: {sum(rhrs)/len(rhrs):.1f} bpm")
            if hrvs:
                lines.append(f"  - Heart Rate Variability (HRV): {sum(hrvs)/len(hrvs):.1f} ms")
            if sleeps:
                lines.append(f"  - Sleep Score: {sum(sleeps)/len(sleeps):.1f}/100")
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
        """'- Fitness/Fatigue (PMC): CTL .. ATL .. TSB .. ATL:CTL ..' from the latest
        past-warm-up row carrying PMC values, or None."""
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
            ratio = garmin.load_ratio(atl, ctl)
            if ratio is not None:
                parts.append(f"ATL:CTL {ratio:.2f} (relative overload)")
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

    # ------------------------------------------------------ intensity distribution
    def _intensity_block_context(self, as_of: str) -> Optional[str]:
        """The active block's measured intensity distribution for `adapt`
        (DESIGN_intensity_distribution.md §9.3): the block to date as a per-week rate
        beside its stated focus, plus the current week's raw minutes.

        No preceding block and no delta — block-over-block creep is a periodization
        question, and §9.2 gives those to `generate`. Returns None when today falls
        outside every block: `get_active_mesocycle` falls back to the next FUTURE block,
        which would render an empty table for training that has not happened (§8).
        """
        meso = self._db.get_active_mesocycle(as_of)
        if not meso or not (meso['start_date'] <= as_of <= meso['end_date']):
            return None
        return intensity.block_report(
            meso, as_of, self._db.get_completed_activities,
            current_week=True, benchmarks=self._db.get_benchmark_results(),
        )

    # ----------------------------------------------------------- block progress
    def _block_progress_context(self, as_of: str, gen_start: str) -> Optional[str]:
        """The elapsed part of the block `generate` is about to re-plan the remainder of
        (DESIGN_block_progress.md §3): each already-trained week's planned-vs-actual load,
        plus the fitness tests the block has already run.

        Returns None when there is no fulfilled part to report — `as_of` outside every
        block (`get_active_mesocycle` falls back to a future or first block, which would
        describe training that has not happened), or `gen_start` on/before the block's
        first day, where generate IS writing the whole block and has nothing to continue.
        """
        meso = self._db.get_active_mesocycle(as_of)
        if not meso or not (meso['start_date'] <= as_of <= meso['end_date']):
            return None
        elapsed_end = (
            datetime.strptime(gen_start, "%Y-%m-%d").date() - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        if elapsed_end < meso['start_date']:
            return None

        # Fetched once and handed to both renderers: they read the same rows, and the week
        # lines and the test lines must never disagree about what the block contains.
        workouts = self._db.get_workouts(start_date=meso['start_date'], end_date=elapsed_end)
        weeks = self._block_week_lines(meso, as_of, elapsed_end, workouts)
        benchmarks = self._block_benchmark_lines(meso, elapsed_end, workouts)
        if not weeks and not benchmarks:
            return None

        # Anchored on gen_start, not as_of: the header's "N completed weeks" must count the
        # same days the week lines below report, and the two differ by one on the run that
        # preserves a completed session and starts tomorrow.
        lines = [intensity.format_header(meso, gen_start)]
        if weeks:
            lines.append("  Weeks already trained (load the plan asked -> load produced):")
            lines.extend(weeks)
        if benchmarks:
            lines.append("  Fitness tests this block has already run:")
            lines.extend(benchmarks)
        return "\n".join(lines)

    def _block_week_lines(
        self, meso: Dict[str, Any], as_of: str, elapsed_end: str,
        workouts: List[Workout],
    ) -> List[str]:
        """One planned-vs-actual line per Monday-week of the block's elapsed part.

        Reuses `progression.weekly_aggregates`, the same planned-vs-actual weekly maths
        `tm progress` renders, so the coach and the athlete never read different numbers
        for the same week (§3.1). The in-progress week states raw load beside the elapsed
        day count and is never extrapolated, following
        DESIGN_intensity_distribution.md §9.3.
        """
        activities = self._db.get_completed_activities(
            start_date=meso['start_date'], end_date=elapsed_end
        )
        weeks = progression.weekly_aggregates(
            activities, workouts, as_of, progression.meso_bands([meso], []),
            window_end=elapsed_end,
        )
        out: List[str] = []
        for w in weeks:
            when, actual = w['week_commencing'], w['actual_load']
            denom = progression.week_plan_denom(w)
            if w['in_progress']:
                elapsed = min(7, days_between(when, elapsed_end) + 1)
                head = f"    - week of {when} (in progress, {elapsed} of 7 days)"
            else:
                head = f"    - week of {when}"
            if denom is None:
                out.append(f"{head}: actual {actual:.0f}, no plan covered this week")
                continue
            pct = f" ({actual / denom * 100:.0f}%)" if denom else ""
            asked = f"planned {denom:.0f}" + (" so far" if w['in_progress'] else "")
            # Partiality is judged against the BLOCK, not against `partial_plan`: that flag
            # compares the week to the workout rows handed in, so a Monday the athlete had
            # no session on would read as "the plan starts mid-week" when it does not. Only
            # a week the block itself straddles is genuinely incomparable. The in-progress
            # week is always cut short by design, and its day count already says so.
            note = (
                " [block covers only part of this week]"
                if when < meso['start_date'] and not w['in_progress'] else ""
            )
            out.append(f"{head}: {asked}, actual {actual:.0f}{pct}{note}")
        return out

    def _block_benchmark_lines(
        self, meso: Dict[str, Any], elapsed_end: str, workouts: List[Workout],
    ) -> List[str]:
        """The fitness tests the block's elapsed part already ran — what makes the
        generate prompt's BENCHMARK PLACEMENT conditional rather than unconditional (§4).

        Keyed on the planned benchmark sessions, not the logbook: a test the athlete
        performed but never recorded still must not be scheduled twice. A logbook row is
        matched to its session by `workout_id`, falling back to a same-date reading for a
        result recorded without the link; unmatched in-block rows are reported as ad-hoc
        tests.
        """
        start = meso['start_date']
        planned = [w for w in workouts if w.get('benchmark_type')]
        results = self._db.get_benchmark_results()
        by_workout = {r['workout_id']: r for r in results if r.get('workout_id')}
        in_block = [r for r in results if start <= r['date'] <= elapsed_end]

        def measured(r: Dict[str, Any]) -> str:
            anchor = ANCHOR_KINDS.get(r['anchor_kind'])
            label = anchor.label if anchor else r['anchor_kind']
            return f"{label} {format_value(r['anchor_kind'], float(r['value']))}"

        out: List[str] = []
        claimed = set()
        for w in sorted(planned, key=lambda w: w['date']):
            r = by_workout.get(w['id']) or next(
                (x for x in in_block if x['date'] == w['date']), None
            )
            if r:
                claimed.add(r['id'])
            out.append(
                f"    - {w['date']}: {w['benchmark_type']} ({w['sport_type']}) — "
                + (measured(r) if r else "no result recorded")
            )
        for r in sorted(
            (x for x in in_block if x['id'] not in claimed), key=lambda r: r['date']
        ):
            out.append(f"    - {r['date']}: {measured(r)} recorded (no planned test)")
        return out

    def _planning_zone_currencies(self, as_of: str) -> Dict[str, str]:
        """`{sport: 'power'|'hr'}` for the sports the coach may prescribe zone targets in
        (DESIGN_intensity_distribution.md §9.8).

        §9.6's currency rule needs a window and authoring has none — at generation time
        there is only forward plan — so it borrows the display default of 8 trailing
        weeks, and the ordinary case agrees by construction. The transient worth naming
        is the athlete who has just bought a power meter: trailing coverage still says HR
        while the display flips to power as the meter's weeks accumulate, so the future
        half of the table goes dark until the next `workout generate` re-picks the
        currency from fresh coverage. Self-healing, on the same rolling horizon that
        regenerates everything else.
        """
        start = (
            datetime.strptime(as_of, "%Y-%m-%d").date()
            - timedelta(days=7 * intensity.PLANNING_COVERAGE_WEEKS)
        ).strftime("%Y-%m-%d")
        return intensity.currency_by_sport(
            self._db.get_completed_activities(start_date=start, end_date=as_of)
        )

    def _intensity_history_context(
        self, macros: List[Dict[str, Any]], today_str: str
    ) -> List[str]:
        """One intensity report per elapsed block across `macros`, each carrying the
        delta against the block before it (§4.1) — the strategy prompt's view.

        Blocks are walked macrocycle-first and then flattened in order, never fetched by
        a date-ordered mesocycle query: every mesocycle accessor filters
        ``mac.status = 'active'``, which hides the cross-plan case, and dropping that
        filter drags in superseded rollback versions whose blocks overlap the live ones
        and describe training that never happened. Navigating by macrocycle id fixes the
        lineage before any dates are compared, so neither trap can fire.
        """
        blocks: List[Dict[str, Any]] = []
        seen_macros = set()
        for macro in macros:
            if not macro or macro['id'] in seen_macros:
                continue
            seen_macros.add(macro['id'])
            blocks.extend(self._db.get_mesocycles_for_macrocycle(macro['id']))
        benchmarks = self._db.get_benchmark_results()
        reports = []
        for i, meso in enumerate(blocks):
            text = intensity.block_report(
                meso, today_str, self._db.get_completed_activities,
                previous=blocks[i - 1] if i else None, benchmarks=benchmarks,
            )
            if text:
                reports.append(text)
        return reports

    def _build_prior_training_context(
        self, prior_macro: Optional[Dict[str, Any]], today_str: str
    ) -> Optional[str]:
        """Builds a read-only "planned vs actual" review for the strategy prompt
        (DESIGN_backward_evaluation.md §6, Option A).

        Anchored on the *elapsed* mesocycle windows of the prior plan AND of the plan the
        athlete is currently in (§6): each planned block's focus is shown beside what the
        athlete actually did in that window — volume, load, and the per-sport per-zone
        intensity distribution with its block-over-block delta
        (DESIGN_intensity_distribution.md §4.1/§9) — so the model can judge whether the
        block's intent materialized. If a cached backward-evaluation reconstruction exists
        (from `data bootstrap`), its summary, reverse-engineered macro/mesocycle structure,
        and physiological insights are appended — reused without another LLM call (§10).
        Returns None if there is nothing to report.

        This does NOT write to any `feedback` field: under Option A the assessment is
        prompt context only, sidestepping the feedback-lifecycle collision (§11).
        """
        sections: List[str] = []

        # The whole review is pre-wrapped here, not at print time: the zone tables are
        # column-aligned and re-wrapping shreds them (DESIGN_intensity_distribution.md §6).
        width = intensity.PROMPT_WIDTH

        # The current plan's elapsed blocks join the prior plan's: drift diagnosed only
        # one macrocycle late is history (gap 2 of DESIGN_intensity_distribution.md §3).
        reports = self._intensity_history_context(
            [prior_macro, self._db.get_governing_macrocycle()], today_str
        )
        if reports:
            sections.append(
                wrap_text(
                    "PLANNED vs ACTUAL (elapsed blocks — judge whether each block's intent "
                    "materialized). Each block shows its planned focus beside what the "
                    "athlete's sessions ACTUALLY measured, per sport and per zone, as a "
                    "per-week rate over the block's completed weeks, plus the change "
                    "against the block before it. Read the delta as the intensity-creep "
                    "check: weekly TSS can hold flat while easy volume quietly gives way "
                    "to tempo.", width
                )
                + "\n" + "\n".join(reports)
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
                sections.append(wrap_text(
                    f"INFERRED FROM PAST TRAINING{window} (latest data analysis):\n"
                    + "\n".join(recon_lines), width
                ))

        return "\n\n".join(sections) if sections else None
