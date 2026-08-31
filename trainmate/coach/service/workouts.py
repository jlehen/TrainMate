from contextlib import nullcontext
from datetime import date, datetime, timedelta
from typing import Any, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.types import Constraint, Workout
from trainmate.adherence import analyze_adherence
from trainmate.coach import honoring
from trainmate.coach.proposals import GenerateProposal
from trainmate.coach.revisions import normalize_load_fields
from trainmate.sports import canonical_sport
from trainmate.benchmarks import MIN_RETEST_DAYS
from trainmate.calendar_reconcile import verbose_events
from trainmate import intensity
from trainmate.util import green, cmd, notice
import trainmate.coach.service as _svc


class WorkoutGenMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

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
        """A deterministic rest session the rest-window pre-pass places on a date the
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
        """The `rest` windows — the only edge that skips the LLM (§5): a full no-training
        window whose dates are forced to rest. Every other constraint is advisory prose
        the LLM honors itself, so it is deliberately excluded here. Returns
        [(start, end, title)]."""
        return [
            (c['start_date'], c['end_date'], c['title'])
            for c in constraints
            if c.get('rest')
        ]

    @classmethod
    def _enforce_rest_windows_generate(
        cls, workouts: List[Dict[str, Any]], constraints: List[Constraint], gen_start: str,
        gen_end: str
    ) -> List[Dict[str, Any]]:
        """Forces `rest` constraints onto a freshly generated workout list (§6): every rest
        date inside the generated span becomes a single rest, deterministically, bypassing
        the LLM for that date entirely. Every other constraint is advisory only — left to
        the model via the prompt block, not enforced here (§5).

        Dates the model simply left out are filled too, not only the ones it scheduled: an
        absent row and an explicit rest day mean different things to adherence (§6). The
        span is the *requested* `gen_start`..`gen_end`, not what the model happened to
        return — a rest window at the tail of the range is exactly the case the model
        answers with silence, so bounding by its last date would reopen the gap (§6)."""
        full_rest = cls._hard_rest_windows(constraints)
        if not full_rest:
            return workouts

        span_end = gen_end
        forced: Dict[str, str] = {}          # date -> constraint title
        for (s, e, title) in full_rest:
            day = datetime.strptime(max(s, gen_start), "%Y-%m-%d").date()
            last = datetime.strptime(min(e, span_end), "%Y-%m-%d").date()
            while day <= last:
                forced.setdefault(day.strftime("%Y-%m-%d"), title)
                day += timedelta(days=1)
        if not forced:
            return workouts

        out = [w for w in workouts if w.get('date', '') not in forced]
        out.extend(
            cls._rest_workout(day, f"constraint '{title}'")
            for day, title in sorted(forced.items())
        )
        return out

    @classmethod
    def _forced_rest_days(
        cls, planned_workouts: List[Workout], constraints: List[Constraint],
        completed_keys: Optional[set], from_date: str
    ) -> Dict[str, str]:
        """`date -> constraint title` for the days a `rest` constraint clears outright.

        One reading of the rule, because two callers act on it: the pass below replaces
        those days with rest, and the adaptation drops anything it was holding there."""
        full_rest = cls._hard_rest_windows(constraints)
        if not full_rest:
            return {}
        completed = completed_keys or set()
        rest_sport = canonical_sport('rest')

        forced_rest: Dict[str, str] = {}
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
        return forced_rest

    @classmethod
    def _enforce_rest_windows_revision(
        cls, adapted: List[Dict[str, Any]], planned_workouts: List[Workout],
        constraints: List[Constraint], completed_keys: Optional[set], from_date: str
    ) -> List[Dict[str, Any]]:
        """Eases planned sessions to rest on `rest` constraint dates (§6). Every other
        constraint is advisory only (§5) — left to the model, not enforced here. Only
        touches sessions on or after `from_date` that aren't already rest or completed."""
        forced_rest = cls._forced_rest_days(
            planned_workouts, constraints, completed_keys, from_date
        )
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

    @staticmethod
    def _drop_benchmark_collisions(
        workouts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Same-day collision rule (§4.1): save_workout keys on (date, sport), so a second
        same-sport session on a benchmark's date would silently overwrite the test. On a
        date holding a benchmark of sport X, drop any OTHER proposed sport-X session and
        warn. The benchmark is identified by its flag — no guessing."""
        # date -> {canonical sport with a benchmark that day}
        bench_sports: Dict[str, set] = {}
        for w in workouts:
            if w.get('benchmark_type'):
                bench_sports.setdefault(w.get('date', ''), set()).add(
                    canonical_sport(w.get('sport_type', ''))
                )
        if not bench_sports:
            return workouts
        out: List[Dict[str, Any]] = []
        for w in workouts:
            day = w.get('date', '')
            sport = canonical_sport(w.get('sport_type', ''))
            if (not w.get('benchmark_type')
                    and sport in bench_sports.get(day, set())):
                notice(
                    f"Dropping {w.get('sport_type', '')} session on {day}: it collides "
                    f"with a scheduled benchmark of the same sport.",
                )
                continue
            out.append(w)
        return out

    @staticmethod
    def _resolve_kept(
        workouts: List[Dict[str, Any]], carried: List[Workout]
    ) -> List[Dict[str, Any]]:
        """Swaps each `keep` entry for the session it names, so every pass after this one
        sees a uniform list of full sessions (DESIGN_workout_revisions.md §7.1).

        A KEEP naming a slot no carried session occupies is dropped, and an explicit
        session for the same slot beats a KEEP of it. The marker rides on the resolved
        dict, so a pass that replaces the session drops the marker with it.
        """
        by_slot = {
            (w['date'], canonical_sport(w['sport_type'])): w for w in carried
        }
        written = {
            (w.get('date'), canonical_sport(w.get('sport_type', '')))
            for w in workouts if not w.get('keep')
        }
        out: List[Dict[str, Any]] = []
        for w in workouts:
            if not w.get('keep'):
                out.append(w)
                continue
            slot = (w.get('date'), canonical_sport(w.get('sport_type', '')))
            live = by_slot.get(slot)
            if live is None or slot in written:
                continue
            out.append({**live, 'keep': True})
        return out

    def _event_date_for_macrocycle(self, macrocycle_id: int) -> Optional[date]:
        """The event date a macrocycle's boundary tests must keep clear of — None when
        unresolvable, and None for a horizon goal, whose date has no event a test could
        compete with."""
        macro = self._db.get_macrocycle(macrocycle_id)
        if not macro:
            return None
        objective = self._db.get_objective(macro.get('objective_id'))
        if (objective or {}).get('date_type') == 'horizon':
            return None
        target = (objective or {}).get('target_date')
        if not target:
            return None
        try:
            return datetime.strptime(target, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _warn_missing_boundary_benchmarks(
        self, workouts: List[Dict[str, Any]], constraints: List[Constraint],
        mesocycles: List[Dict[str, Any]], gen_start: str
    ) -> None:
        """Boundary-week post-check (§4.1): warn — don't auto-insert — when a covered
        mesocycle-boundary week ended up with no benchmark. Same spirit as the rest-window
        pass, but a surfaced warning the athlete can act on (regenerate), not a silent fix.
        Stays quiet when the boundary week sits under a `rest` constraint — rest wins — and
        for a boundary week reaching into the goal's own week, where a maximal test would
        compete with the event it is meant to serve (§4.1).

        `mesocycles` are the blocks governing this span, which may come from more than one
        plan when a long horizon runs from one goal into the next — so the goal week that
        silences a test is read per block, not once for the run."""
        if not workouts or not mesocycles:
            return
        dated = [w for w in workouts if w.get('date')]
        span_end = max(w['date'] for w in dated)
        rest_windows = self._hard_rest_windows(constraints)
        for m in mesocycles:
            end = m.get('end_date', '')
            # Only boundary weeks whose end falls inside the generated span.
            if not (gen_start <= end <= span_end):
                continue
            end_obj = datetime.strptime(end, "%Y-%m-%d").date()
            # Goal week wins: a boundary week ending inside it gets no test (§4.1) —
            # unless the goal is a horizon, which has no event week to protect.
            goal_obj = self._event_date_for_macrocycle(m['macrocycle_id'])
            if goal_obj and end_obj > goal_obj - timedelta(days=7):
                continue
            win_start = (end_obj - timedelta(days=6)).strftime("%Y-%m-%d")
            # Rest wins: a full-rest window overlapping the boundary week silences the check.
            if any(s <= end and e >= win_start for (s, e, _t) in rest_windows):
                continue
            # A test already run earlier in this boundary week counts (§4.1): regenerating
            # mid-boundary-week would otherwise advise regenerating again to recover a
            # benchmark the athlete has already done. Bounded below gen_start because the
            # displaced plan's future rows are still live here — only an accepted proposal
            # archives them — and would answer for sessions this run proposes to replace.
            already_run = any(
                w.get('benchmark_type') and win_start <= w['date'] < gen_start
                for w in self._db.get_workouts(start_date=win_start, end_date=end)
            )
            has_benchmark = already_run or any(
                w.get('benchmark_type') and win_start <= w['date'] <= end
                for w in dated
            )
            if has_benchmark:
                continue
            # The §1 interval floor wins: a test inside MIN_RETEST_DAYS of this boundary
            # means none is due here, so warning would nag the athlete into violating the
            # floor the generator just honored. Any anchor silences — the check cannot
            # know which anchor a missing test would have measured, and a false silence
            # costs one un-nudged athlete where a false nag contradicts the plan.
            recent_start = (
                end_obj - timedelta(days=MIN_RETEST_DAYS)
            ).strftime("%Y-%m-%d")
            recent_test = (
                any(w.get('benchmark_type') and recent_start <= w['date'] < win_start
                    for w in dated)
                or any(
                    w.get('benchmark_type') and w['date'] < gen_start
                    for w in self._db.get_workouts(
                        start_date=recent_start, end_date=end)
                )
                or any(
                    r.get('source') == 'test' and recent_start <= r['date'] <= end
                    for r in self._db.get_benchmark_results()
                )
            )
            if not recent_test:
                notice(
                    f"No benchmark scheduled in the boundary week of '{m.get('name', '')}' "
                    f"({win_start} to {end}), and none tested in the {MIN_RETEST_DAYS} "
                    f"days before it. If a test is due, consider regenerating.",
                )

    def workout_rollback(
        self, change_id: Optional[int] = None, verbose: bool = False
    ) -> Dict[str, Any]:
        """Undoes a workout change — and every change made after it (§10).

        The sibling of `plan_rollback` on the workout axis: it puts the sessions back the
        way they were the moment before `change_id` ran, without touching the active plan
        version, so it also undoes a regeneration that never changed the strategy. With no
        target it undoes the newest change, which is what makes an adapt undoable on its
        own — point-in-time reverts what came AFTER the target, never what came before
        (DESIGN_plan_rollback.md §9, DESIGN_workout_revisions.md §10).

        Returns {change, restored_workouts, first_date, last_date, unhonored}.
        Raises ValueError when there is nothing to undo.
        """
        today_str = _svc._today_str()
        changes = self._db.get_workout_changes(from_date=today_str)
        if not changes:
            raise ValueError(
                "No workout changes to roll back — nothing has been written yet."
            )
        if change_id is None:
            change_id = changes[0]["id"]
        target = next((c for c in changes if c["id"] == change_id), None)
        if target is None:
            raise ValueError(f"No workout change #{change_id}.")

        with verbose_events() if verbose else nullcontext():
            restored, unhonored = self._db.rollback_to_change(
                change_id, today_str,
                summary=f"Undo of change #{change_id} ({target['kind']}).",
            )
        return {
            'change': target,
            'restored_workouts': len(restored),
            'first_date': min((w['date'] for w in restored), default=None),
            'last_date': max((w['date'] for w in restored), default=None),
            # The restored plan predates these honorings, so they are unhonored again and
            # the caller says so (§8).
            'unhonored': unhonored,
        }

    def workout_generate(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None,
        prefer_macro_id: Optional[int] = None
    ) -> GenerateProposal:
        """Proposes workouts (microcycles) from the plan blocks governing the span.

        Writes nothing: the caller previews the sessions and passes the proposal back to
        `workout_generate_apply` on a `y`, so a regeneration cannot archive the live plan
        for a proposal the athlete never saw.

        `start_date` opens the span and never reaches into the past — the caller's
        selectors pick which days are rebuilt, and the rest of the plan is left alone
        (DESIGN_cli_selectors.md §8).

        Which plan applies is read off the dates being generated, not off a goal the
        caller names: the goal was only ever an indirection to the macrocycle, and the
        blocks a span falls in are what actually shape the sessions
        (DESIGN_cli_selectors.md §8)."""
        if not self._db.get_active_objective():
            return GenerateProposal(
                reasoning="No active goals found. TrainMate needs at least one objective."
            )

        today_str = _svc._today_str()
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

        # The span opens where the selectors put it, never in the past: yesterday is
        # history, not a day to re-plan (§8).
        gen_start_str = max(start_date or today_str, today_str)

        # A regeneration replaces every workout in the span, but a session the athlete has
        # already completed should be preserved as history rather than overwritten. When
        # today's planned workout is already in the books, start the regenerated plan
        # tomorrow and leave today's row (and its Calendar event) intact.
        if gen_start_str == today_str and self._today_workout_completed(
            today_str, completed_activities
        ):
            gen_start_str = (today_date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
            print(green(
                f"Today's workout is already completed — preserving it and regenerating "
                f"from {gen_start_str}."
            ))
        gen_start_obj = datetime.strptime(gen_start_str, "%Y-%m-%d").date()

        if end_date is not None:
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
            # Inclusive of end_date itself — a `-g` span reads as "through this goal's
            # target date", so the race day belongs in it (§8).
            num_days = max(1, (end_date_obj - gen_start_obj).days + 1)
            gen_end_str = max(end_date, gen_start_str)
        else:
            num_days = config.workout_generation_span_days
            gen_end_str = (gen_start_obj + timedelta(days=num_days - 1)).strftime("%Y-%m-%d")

        constraints = self._db.get_constraints(gen_start_str)
        self._maybe_nudge_no_threshold()

        # The blocks governing the days about to be written — the whole periodization
        # input to this run, resolved from the window itself (§8).
        blocks, dropped_macros = self._db.get_governing_mesocycles(
            gen_start_str, gen_end_str, prefer_macro_id=prefer_macro_id
        )
        if not blocks:
            raise ValueError(
                "No active periodization strategy found. Run "
                + cmd("plan generate") + " first."
            )
        for macro_id in dropped_macros:
            notice(
                f"Plan ID {macro_id} also covers part of this span; following the more "
                f"recently generated plan instead. Pass "
                + cmd(f"-M {macro_id}", quote=False) + " to follow that one.",
            )
        plan_end = max(b['end_date'] for b in blocks)
        if plan_end < gen_end_str:
            notice(
                f"The plan runs out on {plan_end}, before this horizon ({gen_end_str}) — "
                f"sessions after it have no block to follow. Run "
                + cmd("plan generate") + " to extend the periodization first.",
            )

        # All upcoming goals still reach the prompt as context; only the blocks above
        # decide what the sessions are shaped like.
        ctx = self._coach_context(constraints, blocks=blocks)
        objectives = ctx.objectives
        guidelines, profile = ctx.guidelines, ctx.profile
        strategy, meso_text, learnings = ctx.strategy, ctx.meso_text, ctx.learnings

        pmc_cutoff, pmc_context = self._pmc_prompt_context(today_str)
        # What the block has already banked, when this run re-plans only its remainder
        # (DESIGN_block_progress.md §3). Anchored on gen_start, so the day preserved for a
        # completed session counts as history rather than as a day still to write.
        block_progress, block_has_intensity = self._block_progress_context(
            today_str, gen_start_str
        )
        # What a prior `workout adapt` already eased, so the regeneration does not hand
        # back the load adapt took off (DESIGN_workout_revisions.md §7.1).
        carried = [
            w for w in self._db.get_workouts(
                start_date=gen_start_str, end_date=gen_end_str
            )
            if (w.get('adaptation_count') or 0) > 0
        ]
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
            baseline=baseline,
            pmc_warmup_cutoff=pmc_cutoff,
            pmc_context=pmc_context,
            block_progress=block_progress,
            block_has_intensity=block_has_intensity,
            zone_currencies=self._planning_zone_currencies(today_str),
            anchor_history=self._anchor_history_text(gen_start_str),
            carried_workouts=carried
        )

        # NOTE: workout generation is read-only w.r.t. coach learnings (see
        # DESIGN_backward_evaluation.md §11) — it does not apply learning_updates. Durable
        # memory is authored only by `analyze` and `plan generate`.

        # Save workouts to database
        workouts = plan_data.get("workouts", [])

        # Each `keep` becomes the session it names, so every pass below sees one kind of
        # entry (DESIGN_workout_revisions.md §7.1).
        workouts = self._resolve_kept(workouts, carried)

        # Integers, before the preview and the save both read these numbers.
        normalize_load_fields(workouts)

        # Guard the preserved day: when today's completed session is being kept, drop any
        # workout the model mistakenly dated before the generation start. save_workout
        # matches on date+sport, so a stray today-dated row would silently overwrite the
        # completed session we deliberately kept.
        if gen_start_str != today_str:
            workouts = [w for w in workouts if w.get('date', '') >= gen_start_str]

        # Same-day collision guard (§4.1): drop any non-benchmark session that shares a
        # date+sport with a scheduled benchmark, before the (date, sport)-keyed save can
        # let it overwrite the test.
        workouts = self._drop_benchmark_collisions(workouts)

        # Deterministic rest-window pre-pass (DESIGN_constraints.md §6): a `rest`
        # constraint forces its dates to rest regardless of what the LLM produced. Every
        # other constraint is advisory and left to the model. Applied after generation so
        # the guarantee holds even if the model ignores the constraint block it was shown.
        workouts = self._enforce_rest_windows_generate(
            workouts, constraints, gen_start_str, gen_end_str
        )

        # Boundary-week benchmark post-check (§4.1): warn (don't auto-insert) if a covered
        # block boundary lacks a fitness test. Runs after the rest pass so a rest-covered
        # boundary week is already silenced.
        self._warn_missing_boundary_benchmarks(
            workouts, constraints, blocks, gen_start_str
        )

        # Which plan version each session belongs to, per date: a span long enough to run
        # from one goal's last block into the next goal's first produces workouts from two
        # macrocycles, and `plan rollback` accounting keys off this tag. Days past the last
        # block fall back to the plan that governed the start.
        def _macro_for(date_str: str) -> int:
            for b in blocks:
                if b['start_date'] <= date_str <= b['end_date']:
                    return b['macrocycle_id']
            return blocks[0]['macrocycle_id']

        for w in workouts:
            w['macrocycle_id'] = _macro_for(w['date'])

        # Chronological, because the preview is read as a plan and the model returns the
        # sessions in whatever order it wrote them.
        workouts.sort(key=lambda w: (w['date'], w.get('sport_type', '')))

        # The live plan this would displace, read here so the preview's warning and the
        # apply's teardown are the same set. Bounded at both ends: days past the span keep
        # the sessions they already have (§8).
        displaced = self._db.get_workouts(
            start_date=gen_start_str, end_date=gen_end_str, include_removed=True
        )

        return GenerateProposal(
            reasoning=plan_data.get("reasoning", "Plan generated."),
            workouts=tuple(workouts),
            displaced=tuple(displaced),
            gen_start=gen_start_str,
            gen_end=gen_end_str,
            # Checked against the range about to be WRITTEN, not the fetched set, which is
            # open-ended (DESIGN_constraint_honoring.md §2).
            covered_constraint_ids=honoring.covered_ids(
                constraints, gen_start_str, gen_end_str
            ),
        )

    def workout_generate_apply(
        self, proposal: GenerateProposal, verbose: bool = False
    ) -> List[Workout]:
        """Commits an accepted `workout generate` proposal: one change, then one reconcile.

        Every day the new plan does not fill, *inside the generated span*, gets a void
        revision; every day it does fill gets a revision — unless the prescription is
        identical to what is already live, in which case §9 suppresses it and the day is
        left alone. A day the plan KEEPS is claimed but not written: it is spared the void
        and appends nothing, so the session and its Calendar event carry on untouched
        (§7.1). The voids go first, so a session the plan drops is ended before
        anything else can take its slot (§8). Calendar follows from the change handle's
        reconcile pass, so nothing here pushes.

        The span is `gen_start`..`gen_end`, so sessions outside it survive a bounded
        regeneration untouched (DESIGN_cli_selectors.md §8).

        Returns the sessions the plan now holds in the slots it proposed.
        """
        proposed_slots = {
            (w['date'], canonical_sport(w['sport_type'])) for w in proposal.workouts
        }
        summary = proposal.reasoning
        # The plan version governing the span's start — context for `workout batches`,
        # while each row keeps the per-date tag every scoping read uses (§3).
        span_macro = next(
            (w.get('macrocycle_id') for w in proposal.workouts
             if w.get('macrocycle_id') is not None), None
        )
        with (
            verbose_events() if verbose else nullcontext(),
            self._db.workout_change(
                kind="generate", summary=summary, macrocycle_id=span_macro
            ) as change,
        ):
            for live in self._db.get_workouts(
                start_date=proposal.gen_start, end_date=proposal.gen_end or None
            ):
                if (live['date'], canonical_sport(live['sport_type'])) in proposed_slots:
                    continue
                change.void(
                    date=live['date'], sport_type=live['sport_type'],
                    reason="Not in the regenerated plan",
                )
            for w in proposal.workouts:
                # Claimed above, so it escaped the void; writing it again would churn a
                # day that did not change (§7.1).
                if w.get('keep'):
                    continue
                # The intensity target the coach stated while it still knew the intent
                # (DESIGN_intensity_distribution.md §9.8) — validated, never rescaled.
                zone_currency, zone_sec = intensity.parse_planned_zones(w)
                change.append(
                    date=w['date'],
                    sport_type=w['sport_type'],
                    title=w['title'],
                    description=w['description'],
                    duration_minutes=w.get('duration_minutes'),
                    rpe=w.get('rpe'),
                    tss=w.get('tss'),
                    benchmark_type=w.get('benchmark_type'),
                    macrocycle_id=w.get('macrocycle_id'),
                    planned_zone_currency=zone_currency,
                    planned_zone_sec=zone_sec,
                )
            replaced_manual = list(change.replaced_manual)
            change_id = change.id

        # The plan owns the horizon, so a regeneration may replace a session the athlete
        # added — but it says which, and names the change to undo if they disagree (§12).
        for w in replaced_manual:
            notice(
                f"Replaced the session you added on {w['date']}: {w['title']} "
                f"({w['sport_type']}). Run {cmd(f'workout rollback --batch {change_id}')} "
                "to bring it back.",
            )

        # The same warrant every other constraint this run built around gets (§8).
        honoring.stamp(self._db, proposal.covered_constraint_ids)

        # The sessions the plan now holds in the slots it proposed — which is not the same
        # as the revisions it appended, because §9 leaves an unchanged day alone.
        dates = [w['date'] for w in proposal.workouts]
        if not dates:
            return []
        live = self._db.get_workouts(start_date=min(dates), end_date=max(dates))
        by_slot = {(w['date'], canonical_sport(w['sport_type'])): w for w in live}
        return [by_slot[slot] for slot in
                ((w['date'], canonical_sport(w['sport_type'])) for w in proposal.workouts)
                if slot in by_slot]
