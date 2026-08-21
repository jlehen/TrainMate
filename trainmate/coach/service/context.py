from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.types import Workout
from trainmate import garmin, intensity, progression
from trainmate.plan_lineage import plan_lineage
from trainmate.benchmarks import ANCHOR_KINDS, format_value
from trainmate.util import wrap_text, days_between, PMC_TSB_LAG_NOTE


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
        outside every block — the next FUTURE block would render an empty table for
        training that has not happened (§8).
        """
        meso = self._db.get_covering_mesocycle(as_of)
        if not meso:
            return None
        return intensity.block_report(
            meso, as_of, self._db.get_completed_activities,
            current_week=True, benchmarks=self._db.get_benchmark_results(),
        )

    # ----------------------------------------------------------- block progress
    def _block_progress_context(
        self, as_of: str, gen_start: str
    ) -> Tuple[Optional[str], bool]:
        """The elapsed part of the block `generate` is about to re-plan the remainder of
        (DESIGN_block_progress.md §3): its measured intensity distribution beside what the
        plan prescribed and beside the preceding block, each already-trained week's
        planned-vs-actual load, and the fitness tests the block has already run.

        Returns `(text, has_intensity)`, a pair like `_pmc_prompt_context`'s: the
        composition TASK section quotes the zone tables, so it must be gated on those
        tables actually having rows rather than on the section merely existing (§5.1).

        `text` is None when there is no fulfilled part to report — `as_of` outside every
        block (a future or first block would describe training that has not happened),
        or `gen_start` on/before the block's first day, where generate IS writing the
        whole block and has nothing to continue.
        """
        meso = self._db.get_covering_mesocycle(as_of)
        if not meso:
            return None, False
        elapsed_end = (
            datetime.strptime(gen_start, "%Y-%m-%d").date() - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        if elapsed_end < meso['start_date']:
            return None, False

        # Fetched once and handed to both renderers: they read the same rows, and the week
        # lines and the test lines must never disagree about what the block contains.
        workouts = self._db.get_workouts(start_date=meso['start_date'], end_date=elapsed_end)
        weeks = self._block_week_lines(meso, as_of, elapsed_end, workouts)
        benchmarks = self._block_benchmark_lines(meso, elapsed_end, workouts)
        # Everything is anchored on gen_start, not as_of: history ends the day before the
        # first day being written, so the header's "N completed weeks", the week lines and
        # the zone windows all count the same days. The two differ by one on the run that
        # preserves an already-completed session and starts tomorrow.
        report = intensity.block_report(
            meso, gen_start, self._db.get_completed_activities,
            current_week=True, previous=self._preceding_mesocycle(meso),
            benchmarks=self._db.get_benchmark_results(),
            fetch_workouts=self._db.get_workouts, indent="",
        )
        # Gated on banked evidence, NOT on `report`: a started block with nothing recorded
        # still yields a report ("no completed activities in ..."), and pairing that with a
        # task section about carrying a ramp on from the last completed week describes a
        # week that does not exist. Generate already sees the empty activity list.
        if not weeks and not benchmarks:
            return None, False

        # `block_report` opens with the same `format_header` line, so it stands in for the
        # header when present rather than being stacked under a second copy of it.
        lines = [report] if report else [intensity.format_header(meso, gen_start)]
        if weeks:
            lines.append("  Weeks already trained (load the plan asked -> load produced):")
            lines.extend(weeks)
        if benchmarks:
            lines.append("  Fitness tests this block has already run:")
            lines.extend(benchmarks)
        return "\n".join(lines), bool(report) and self._block_has_zone_rows(meso, gen_start)

    def _block_has_zone_rows(self, meso: Dict[str, Any], as_of: str) -> bool:
        """Whether the block's zone table will have rows — asked of the same window
        `block_report` builds that table from, via `intensity.measured_window`, so the
        prompt's gate and the table can never disagree (§5.1)."""
        win_start, win_end, _ = intensity.measured_window(
            meso['start_date'], meso['end_date'], as_of
        )
        return bool(intensity.zone_rows(
            self._db.get_completed_activities(start_date=win_start, end_date=win_end)
        ))

    def _preceding_mesocycle(
        self, meso: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """The block immediately before `meso` in its own macrocycle, for the
        block-over-block delta — the periodization signal proper (§5).

        Navigated by macrocycle id rather than by a date-ordered mesocycle query
        (DESIGN_plan_rollback.md §6.1).
        """
        blocks = self._db.get_mesocycles_for_macrocycle(meso['macrocycle_id'])
        earlier = [b for b in blocks if b['start_date'] < meso['start_date']]
        return max(earlier, key=lambda b: b['start_date']) if earlier else None

    def _block_week_lines(
        self, meso: Dict[str, Any], as_of: str, elapsed_end: str,
        workouts: List[Workout], indent: str = "    ",
    ) -> List[str]:
        """One planned-vs-actual line per Monday-week of the block's elapsed part.

        Reuses `progression.weekly_aggregates`, the same planned-vs-actual weekly maths
        `tm progress` renders, so the coach and the athlete never read different numbers
        for the same week (§3.1). The in-progress week states raw load beside the elapsed
        day count and is never extrapolated, following
        DESIGN_intensity_distribution.md §9.3.

        `indent` only differs because the two prompts nest their blocks differently: the
        strategy prompt sits each report one level in, since it sends several.
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
                head = f"{indent}- week of {when} (in progress, {elapsed} of 7 days)"
            else:
                head = f"{indent}- week of {when}"
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

    def _anchor_history_text(self, gen_start: str) -> str:
        """ANCHORS ON RECORD: each anchor's latest value with its date and source — the
        dates BENCHMARK PLACEMENT's interval floor is judged against (§4.1). Planned test
        sessions count too, like §4.1's de-dup: a test performed but never recorded must
        still hold the interval. Bounded below `gen_start` — the displaced plan's future
        rows are live here and must not answer for days this run is rewriting."""
        lines: List[str] = []
        latest: Dict[str, Dict[str, Any]] = {}
        tested: Dict[str, str] = {}
        for r in self._db.get_benchmark_results():  # newest first
            latest.setdefault(r['anchor_kind'], r)
            if r.get('source') == 'test':
                tested.setdefault(r['anchor_kind'], r['date'])
        for kind, r in latest.items():
            anchor = ANCHOR_KINDS.get(kind)
            label = anchor.label if anchor else kind
            when = (f"last tested {tested[kind]}" if kind in tested
                    else "never measured by a test")
            lines.append(
                f"  - {label}: {format_value(kind, float(r['value']))} — recorded "
                f"{r['date']} ({r.get('source', 'test')}); {when}"
            )
        # Tests on the calendar in the typical-cadence horizon (benchmarks.md §1).
        lookback = (
            datetime.strptime(gen_start, "%Y-%m-%d").date() - timedelta(days=90)
        ).strftime("%Y-%m-%d")
        for w in self._db.get_workouts(start_date=lookback, end_date=gen_start):
            if w.get('benchmark_type') and w['date'] < gen_start:
                lines.append(
                    f"  - Planned test on the calendar: {w['date']} "
                    f"{w['benchmark_type']} ({w['sport_type']})"
                )
        return "\n".join(lines)

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

        Each block's delta baseline is the block before it in the flattened lineage —
        across plan boundaries too, unlike `tm progress --blocks`: reviewing one season
        against the last is what this prompt is for (DESIGN_plan_rollback.md §6.1).
        """
        blocks = plan_lineage(self._db, macros)
        benchmarks = self._db.get_benchmark_results()
        reports = []
        for i, meso in enumerate(blocks):
            text = intensity.block_report(
                meso, today_str, self._db.get_completed_activities,
                previous=blocks[i - 1] if i else None, benchmarks=benchmarks,
                fetch_workouts=self._db.get_workouts,
            )
            if not text:
                continue
            # Elapsed part only: a finished block ends where it ended, the current one at
            # today. Both sides of every week line are cut to the same span.
            elapsed_end = min(today_str, meso['end_date'])
            weeks = self._block_week_lines(
                meso, today_str, elapsed_end,
                self._db.get_workouts(start_date=meso['start_date'], end_date=elapsed_end),
                indent="      ",
            )
            if weeks:
                text += "\n    Weekly load (what the plan asked -> what was produced)\n"
                text += "\n".join(weeks)
            reports.append(text)
        return reports

    def _build_prior_training_context(
        self, prior_macros: List[Optional[Dict[str, Any]]], today_str: str
    ) -> Optional[str]:
        """Builds a read-only "planned vs actual" review for the strategy prompt
        (DESIGN_backward_evaluation.md §6, Option A).

        Anchored on the *elapsed* mesocycle windows of every plan given AND of the plan the
        athlete is currently in (§6, §6.1): each planned block's focus is shown beside what the
        athlete actually did in that window — volume, load, the per-sport per-zone
        intensity distribution against both its block-over-block delta and what the plan
        prescribed (DESIGN_intensity_distribution.md §4.1/§9/§9.2a), and each week's
        planned load beside the load produced — so the model can judge whether the block's
        intent materialized and whether it was actually carried out. Every cached
        backward-evaluation reconstruction then follows, reused without another LLM call
        (§10, §10.2). Returns None if there is nothing to report.

        This writes nothing — not a row in the plan's feedback log either: under Option A
        the assessment is prompt context only, sidestepping the lifecycle collision (§11).
        """
        sections: List[str] = []

        # The whole review is pre-wrapped here, not at print time: the zone tables are
        # column-aligned and re-wrapping shreds them (DESIGN_intensity_distribution.md §6).
        width = intensity.PROMPT_WIDTH

        # The current plan's elapsed blocks join the prior plans': drift diagnosed only
        # one macrocycle late is history (gap 2 of DESIGN_intensity_distribution.md §3).
        reports = self._intensity_history_context(
            [*prior_macros, self._db.get_governing_macrocycle()], today_str
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
                    "to tempo.\n"
                    "Two more comparisons decide WHOSE problem a divergence is. Against "
                    "'What the plan PRESCRIBED', a block that measures off its focus but "
                    "tracks its prescription was MIS-DESIGNED — reshape the blocks still "
                    "ahead; one that diverges from the prescription was mis-executed, "
                    "which the daily adaptation owns, so do not reward it by planning "
                    "the easier block it drifted toward. Against the weekly load lines, "
                    "a block whose weeks came in far under what was asked was not the "
                    "block that was planned: build the next one from the load the athlete "
                    "actually produced, not from the load they were prescribed.", width
                )
                + "\n" + "\n".join(reports)
            )

        for cached in self._cached_reconstructions():
            recon_lines = self._reconstruction_lines(cached["reconstruction"])
            if not recon_lines:
                continue
            window = ""
            if cached.get("window_start") and cached.get("window_end"):
                window = f" ({cached['window_start']}..{cached['window_end']})"
            sections.append(wrap_text(
                f"INFERRED FROM PAST TRAINING{window} — {cached['label']}:\n"
                + "\n".join(recon_lines), width
            ))

        return "\n\n".join(sections) if sections else None

    def _cached_reconstructions(self) -> List[Dict[str, Any]]:
        """The cached history analyses the strategy prompt replays, oldest window first:
        `data bootstrap`'s reconstruction, then `data reflect`'s latest one when it begins
        past bootstrap's window (DESIGN_backward_evaluation.md §10.2).

        Each row is its cache row plus a `label` naming the command behind it, and this is
        the single accessor for both what the prompt gets and how far behind it is — so the
        staleness warning can never name a window the prompt did not actually read.
        """
        out: List[Dict[str, Any]] = []
        long_row = self._db.get_analysis_cache("long")
        long_end = (long_row or {}).get("window_end") or ""
        if long_row and long_row.get("reconstruction"):
            out.append({**long_row, "label": "full history reconstruction"})
        short_row = self._db.get_analysis_cache("short")
        short_start = (short_row or {}).get("window_start") or ""
        # Replay reflect only where it covers ground bootstrap never saw. Gating on its
        # START (not its end) rejects a window that merely reaches further while re-reading
        # weeks bootstrap already read — two accounts of one body of evidence (§10.2).
        if short_row and short_row.get("reconstruction") and short_start > long_end:
            out.append({**short_row, "label": "most recent reflection"})
        return out

    @staticmethod
    def _reconstruction_lines(recon: Dict[str, Any]) -> List[str]:
        """One cached reconstruction rendered for the prompt: its summary, the
        reverse-engineered periodization structure, and the physiological insights.

        The structure is fed so the new plan can build on the real prior arc — where
        base/build/recovery fell, how consistent each block was — rather than re-deriving
        it (DESIGN_backward_evaluation.md §10).
        """
        lines: List[str] = []
        if recon.get("macrocycle_summary"):
            lines.append(f"Summary: {recon['macrocycle_summary']}")
        im = recon.get("inferred_macrocycle") or {}
        if im.get("overall_focus"):
            span = ""
            if im.get("start_date") and im.get("end_date"):
                span = f" ({im['start_date']}..{im['end_date']})"
            lines.append(f"Reconstructed macrocycle focus{span}: {im['overall_focus']}")
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
            lines.append(f"- {name}{m_span}{detail_txt}")
        for ins in (recon.get("physiological_insights") or []):
            lines.append(f"- {ins}")
        return lines
