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
    cyan, green, yellow, bold, red, gray, cmd, PMC_TSB_LAG_NOTE,
)
from trainmate.coach.engine import CoachEngine
from trainmate.coach.formatting import format_baseline, _load_science_guidelines
import trainmate.coach.service as _svc


class DataAnalysisMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

    # Channels of the per-day z, mapping the metric column to its baseline mean/std keys.
    # Sign convention (documented for the LLM): +hrv better, +rhr worse, +sleep better
    # (DESIGN_quantitative_context_impact.md §3).
    _RESPONSE_Z_CHANNELS = (
        ("rhr", "rhr", "rhr_baseline_mean", "rhr_baseline_std"),
        ("hrv", "hrv", "hrv_baseline_mean", "hrv_baseline_std"),
        ("sleep", "sleep_score", "sleep_baseline_mean", "sleep_baseline_std"),
    )

    # Response channels to omit for a context signal whose own construct overlaps them,
    # so the LLM cannot "discover" that bad sleep predicts bad sleep — an echo, not an
    # impact (DESIGN_quantitative_context_impact.md §3.2). Keyed by a substring of the
    # opaque, free-form metric name; the common signals (alcohol, meals) match nothing
    # and exclude nothing.
    _CONTEXT_CHANNEL_EXCLUSIONS = (
        ("sleep", {"sleep"}),
    )

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
                       "For incremental updates use " + cmd("data reflect") + " instead.")
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
                until_date - timedelta(weeks=_svc.DEFAULT_REFLECT_WEEKS) + timedelta(days=1)
            )
            print(yellow(
                f"No reflect baseline found; reflecting over the last {_svc.DEFAULT_REFLECT_WEEKS} "
                f"weeks. Run {cmd('data bootstrap')} to reconstruct your full training "
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
                "impact": c.get('description') or "",
                "coverage": "full" if (start <= ws and end >= we) else "partial",
            })
        return out

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
        for channel, metric_key, mean_key, std_key in DataAnalysisMixin._RESPONSE_Z_CHANNELS:
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
                    DataAnalysisMixin._day_response_z(m, baseline)[channel] for m in w_metrics
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
            for sub, chans in DataAnalysisMixin._CONTEXT_CHANNEL_EXCLUSIONS:
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
                        "value": DataAnalysisMixin._norm_signal_value(day_map[d]),
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
                    zs = DataAnalysisMixin._day_response_z(
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

        # PMC weekly trajectory (DESIGN_pmc_fitness_fatigue.md §5.4): the warm-up cutoff
        # and a full in-window date->CTL map, both read once, for end_ctl/week_ramp/min_tsb.
        pmc_cutoff = garmin.pmc_warmup_cutoff_for(
            garmin.pmc_history_start(dbh=self._db), config.pmc_ctl_days
        )
        ctl_by_date = {m['date']: m.get('ctl') for m in metrics}

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

            max_load_ratio = None
            ratios = [
                r for r in (garmin.load_ratio(m.get('atl'), m.get('ctl')) for m in w_metrics)
                if r is not None
            ]
            if ratios:
                max_load_ratio = max(ratios)

            end_ctl, week_ramp, min_tsb = self._pmc_week_summary(
                w_metrics, ctl_by_date, pmc_cutoff
            )

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
                "max_load_ratio": (
                    round(max_load_ratio, 2) if max_load_ratio is not None else None
                ),
                "end_ctl": round(end_ctl, 1) if end_ctl is not None else None,
                "week_ramp": week_ramp,
                "min_tsb": round(min_tsb, 1) if min_tsb is not None else None,
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
        profile = self._effective_profile()

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
