from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.types import Constraint, Workout
from trainmate.adherence import analyze_adherence
from trainmate.coach.proposals import GenerateProposal
from trainmate.sports import canonical_sport
from trainmate import intensity
from trainmate.util import green, yellow, red, cmd, Progress
import trainmate.coach.service as _svc


@contextmanager
def _calendar_progress(total: int, verbose: bool) -> Iterator[Progress]:
    """A batch of Calendar writes under one summary line and a bar; -v keeps the
    per-event lines instead (and no bar, so they scroll undisturbed)."""
    if verbose:
        yield Progress(0)
        return
    # Imported here, not at module load: importing google_calendar builds the syncer
    # singleton, which needs credentials (see runtime._build_calendar_syncer).
    from trainmate.google_calendar import quiet_events
    with quiet_events(), Progress(total) as bar:
        yield bar


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
    def _enforce_rest_windows_adapt(
        cls, adapted: List[Dict[str, Any]], planned_workouts: List[Workout],
        constraints: List[Constraint], completed_keys: Optional[set], from_date: str
    ) -> List[Dict[str, Any]]:
        """Eases planned sessions to rest on `rest` constraint dates (§6). Every other
        constraint is advisory only (§5) — left to the model, not enforced here. Only
        touches sessions on or after `from_date` that aren't already rest or completed."""
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
                print(yellow(
                    f"Dropping {w.get('sport_type', '')} session on {day}: it collides "
                    f"with a scheduled benchmark of the same sport."
                ))
                continue
            out.append(w)
        return out

    def _goal_date_for_macrocycle(self, macrocycle_id: int) -> Optional[date]:
        """The target date of the objective a macrocycle serves, or None if unresolvable."""
        macro = self._db.get_macrocycle(macrocycle_id)
        if not macro:
            return None
        objective = self._db.get_objective(macro.get('objective_id'))
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
            # Goal week wins: a boundary week ending inside it gets no test (§4.1).
            goal_obj = self._goal_date_for_macrocycle(m['macrocycle_id'])
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
            if not has_benchmark:
                print(yellow(
                    f"No benchmark scheduled in the boundary week of '{m.get('name', '')}' "
                    f"({win_start} to {end}). Consider regenerating — a block-boundary "
                    f"fitness test keeps your zones calibrated."
                ))

    def _archive_and_teardown(self, from_date: str, verbose: bool = False) -> List[Workout]:
        """Archives every live workout from `from_date` on and deletes their Calendar events.

        The displaced rows keep their `macrocycle_id` tag and share one `archived_at` batch
        stamp, so a later rollback can resurrect exactly this set (DESIGN_plan_rollback.md).
        Returns the rows as they were before archival."""
        archived = self._db.archive_future_workouts(from_date)
        if not archived:
            return archived
        print(yellow(
            f"Removing {len(archived)} previously planned workout(s) from Google Calendar..."
        ))
        stale = [ew for ew in archived if ew.get('google_event_id')]
        with _calendar_progress(len(stale), verbose) as bar:
            for ew in stale:
                try:
                    self._calendar_syncer.delete_workout_event(ew['google_event_id'])
                except Exception as e:
                    print(red(f"Error deleting Google Calendar event: {e}"))
                bar.step()
        return archived

    def _push_batch(self, workouts: List[Workout], message: str, verbose: bool) -> None:
        """Pushes a whole batch to Calendar under one summary line, bar or per-event lines."""
        if not workouts:
            return
        print(green(message))
        with _calendar_progress(len(workouts), verbose) as bar:
            try:
                for w in workouts:
                    self._calendar_syncer.sync_workout(w)
                    bar.step()
            except Exception as e:
                print(red(f"Error syncing to Google Calendar: {e}"))

    def workout_rollback(
        self, batch: Optional[str] = None, verbose: bool = False
    ) -> Dict[str, Any]:
        """Restores a previously archived batch of workouts, undoing a regeneration.

        The sibling of `plan_rollback` on the workout axis: it swaps the live upcoming
        sessions for an archived batch — the most recent one by default, or the batch
        stamped `batch` — without touching the active plan version, so it also undoes a
        regeneration that never changed the strategy (see DESIGN_plan_rollback.md §9).
        The current sessions are archived (their Calendar events torn down) and the
        target batch is resurrected and re-pushed, from today onward.

        Returns {batch, restored_workouts, archived_workouts, first_date, last_date}.
        Raises ValueError when there is no batch to restore.
        """
        today_str = _svc._today_str()
        batches = self._db.get_archived_batches(from_date=today_str)
        if not batches:
            raise ValueError(
                "No archived workouts to roll back to — nothing has displaced the "
                "current sessions yet."
            )

        # Resolved before anything is archived: the archive below stamps a newer batch,
        # which would otherwise become the default target and restore what it just
        # displaced.
        if batch is not None:
            target = next((b for b in batches if b['archived_at'] == batch), None)
            if not target:
                raise ValueError(f"No archived workout batch stamped {batch}.")
        else:
            target = batches[0]

        if not target['restorable']:
            raise ValueError(
                f"Every workout in that batch ({target['first_date']}..."
                f"{target['last_date']}) is in the past — there is nothing to restore."
            )

        archived = self._archive_and_teardown(today_str, verbose)
        restored = self._db.restore_workout_batch(target['archived_at'], today_str)
        self._push_batch(
            restored,
            f"Restoring {len(restored)} archived workout(s) in Google Calendar...",
            verbose,
        )

        return {
            'batch': target['archived_at'],
            'restored_workouts': len(restored),
            'archived_workouts': len(archived),
            'first_date': min((w['date'] for w in restored), default=None),
            'last_date': max((w['date'] for w in restored), default=None),
        }

    def workout_generate(
        self, end_date: Optional[str] = None, prefer_macro_id: Optional[int] = None
    ) -> GenerateProposal:
        """Proposes workouts (microcycles) from the plan blocks governing the horizon.

        Writes nothing: the caller previews the sessions and passes the proposal back to
        `workout_generate_apply` on a `y`, so a regeneration cannot archive the live plan
        for a proposal the athlete never saw.

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
            gen_end_str = end_date
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
            print(yellow(
                f"Plan ID {macro_id} also covers part of this span; following the more "
                f"recently generated plan instead. Pass "
                + cmd(f"-M {macro_id}", quote=False) + " to follow that one."
            ))
        plan_end = max(b['end_date'] for b in blocks)
        if plan_end < gen_end_str:
            print(yellow(
                f"The plan runs out on {plan_end}, before this horizon ({gen_end_str}) — "
                f"sessions after it have no block to follow. Run "
                + cmd("plan generate") + " to extend the periodization first."
            ))

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
            zone_currencies=self._planning_zone_currencies(today_str)
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
        # apply's teardown are the same set. `include_removed` matches what
        # `archive_future_workouts` actually takes.
        displaced = self._db.get_workouts(start_date=gen_start_str, include_removed=True)

        return GenerateProposal(
            reasoning=plan_data.get("reasoning", "Plan generated."),
            workouts=tuple(workouts),
            displaced=tuple(displaced),
            gen_start=gen_start_str,
        )

    def workout_generate_apply(
        self, proposal: GenerateProposal, verbose: bool = False
    ) -> List[Workout]:
        """Commits an accepted `workout generate` proposal: archive, save, push.

        Archives (rather than deletes) the displaced plan's future workouts so they can be
        resurrected by `plan rollback` / `workout rollback` (DESIGN_plan_rollback.md), then
        saves the proposed sessions and pushes them to Calendar eagerly, so the calendar
        always mirrors the active plan. Returns the persisted rows."""
        self._archive_and_teardown(proposal.gen_start, verbose)

        saved_workouts: List[Workout] = []
        for w in proposal.workouts:
            # The intensity target the coach stated while it still knew the intent
            # (DESIGN_intensity_distribution.md §9.8) — validated, never rescaled.
            zone_currency, zone_sec = intensity.parse_planned_zones(w)
            wid = self._db.save_workout(
                date=w['date'],
                sport_type=w['sport_type'],
                title=w['title'],
                description=w['description'],
                duration_minutes=w.get('duration_minutes'),
                rpe=w.get('rpe'),
                tss=w.get('tss'),
                source='generated',
                benchmark_type=w.get('benchmark_type'),
                macrocycle_id=w.get('macrocycle_id'),
                planned_zone_currency=zone_currency,
                planned_zone_sec=zone_sec
            )
            # Sync from the persisted row, not a hand-built dict: the row's
            # calendar_signature is what freshness is later derived against, so any field
            # the dict omitted (e.g. source='generated') would make the push-time hash
            # disagree and read STALE forever. Re-fetching also lets the eager event carry
            # the same lifecycle footer a later re-push would (created_at, original load).
            saved_workouts.append(self._db.get_workout_by_id(wid))

        self._push_batch(
            saved_workouts,
            f"Creating {len(saved_workouts)} new workout(s) in Google Calendar...",
            verbose,
        )
        return saved_workouts
