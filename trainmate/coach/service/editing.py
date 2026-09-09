from contextlib import nullcontext as _nothing
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple, Dict
from trainmate.types import Workout
from trainmate.calendar_reconcile import no_calendar_sync
from trainmate.sports import canonical_sport
import trainmate.coach.service as _svc


class WorkoutEditMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

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
        spikes (a relative-overload proxy), and mesocycle-boundary crossings. An empty list means
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

        # 2. Weekly load spike (relative-overload proxy). Only cross-week swaps shift
        # weekly totals.
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
                    f"TSS (+{delta / before * 100:.0f}%), which may spike your acute load."
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
        """Moves each named session to its new date, under one `swap` change.

        Each op is ``{'id': <lineage id>, 'new_date': 'YYYY-MM-DD'}``. A move appends a
        copy of the session at its destination carrying its own lineage, which is what
        makes the adaptation tally follow it across the move — the guard that stops the
        coach cutting an already-cut session exists for exactly this
        (DESIGN_workout_revisions.md §4). A source slot no other move fills is voided
        first, so the departure is recorded rather than left implicit, and so the moved
        session's newest revision is always the copy (§8).

        Returns the moved sessions as they now stand.
        """
        moves = []
        for op in swap_ops:
            workout = self._db.get_workout_by_id(op['id'])
            if workout:
                moves.append((workout, op['new_date']))
        if not moves:
            return []

        filled = {
            (new_date, canonical_sport(w['sport_type'])) for w, new_date in moves
        }
        with no_calendar_sync() if no_sync else _nothing():
            with self._db.workout_change(kind="swap", summary=reason) as change:
                for workout, _new_date in moves:
                    if (workout['date'], canonical_sport(workout['sport_type'])) in filled:
                        continue
                    change.void(
                        date=workout['date'], sport_type=workout['sport_type'],
                        reason=self._swap_reason(workout, _new_date, reason),
                    )
                for workout, new_date in moves:
                    change.append(
                        date=new_date,
                        sport_type=workout['sport_type'],
                        title=workout['title'],
                        description=workout['description'],
                        duration_minutes=workout.get('duration_minutes'),
                        rpe=workout.get('rpe'),
                        tss=workout.get('tss'),
                        benchmark_type=workout.get('benchmark_type'),
                        reason=self._swap_reason(workout, new_date, reason),
                        lineage_id=workout['id'],
                        planned_zone_currency=workout.get('planned_zone_currency'),
                        planned_zone_sec=[
                            workout.get(f'planned_zone{i}_sec') for i in range(1, 8)
                        ],
                    )
        return [
            moved for moved in (
                self._db.get_workout_by_id(w['id']) for w, _ in moves
            ) if moved
        ]

    @staticmethod
    def _swap_reason(
        workout: Dict[str, Any], new_date: str, reason: Optional[str]
    ) -> str:
        """The note a moved session carries: where it came from, plus the athlete's why."""
        note = f"Swapped from {workout['date']} to {new_date}"
        if reason:
            note += f". Reason given: {reason}"
        return note

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

        A manual session is a new session even over a live one, so it starts its own
        lineage — which is what keeps `source` honest and stops it inheriting the replaced
        session's load, its originals or its Calendar event
        (DESIGN_workout_revisions.md §4). What was overwritten is recorded on the new
        session's note, and therefore on its calendar event: each replaced session's title
        plus duration/TSS/RPE, mirroring how `adapt` annotates a changed session.

        Every replaced session is voided first, the same-sport one included: a session the
        coach wrote and the athlete typed over is marked rather than erased, which is the
        same order `workout generate` uses (DESIGN_plan_change_continuity.md §5.2/§5.3).

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

        headers = [
            self._replaced_header(w, primary=canonical_sport(w['sport_type']) == sport_type)
            for w in existing_all
        ]
        note = None
        if headers:
            label = "sessions" if len(headers) > 1 else "session"
            note = f"Manually replaced previous {label}: " + "; ".join(headers)
        if reason:
            note = f"{note}. Reason given: {reason}" if note else reason

        with self._db.workout_change(
            kind="add", summary=reason,
            commitment_end=self._commitment_window(_svc._today_str()),
        ) as change:
            for w in existing_all:
                change.void(date=date, sport_type=w['sport_type'], reason=note)
            change.append(
                date=date,
                sport_type=sport_type,
                title=title,
                description=description,
                duration_minutes=duration_minutes,
                rpe=rpe,
                tss=tss,
                reason=note,
            )
        return self._db.get_workout(date, sport_type), existing_all

    @staticmethod
    def _replaced_header(workout: Dict[str, Any], primary: bool) -> str:
        """One replaced session, as the new session's note names it."""
        stats = []
        if workout.get('duration_minutes') is not None:
            stats.append(f"{workout['duration_minutes']}m")
        if workout.get('tss') is not None:
            stats.append(f"TSS {workout['tss']}")
        if workout.get('rpe') is not None:
            stats.append(f"RPE {workout['rpe']}")
        header = workout['title'] if primary else f"{workout['sport_type']} {workout['title']}"
        if stats:
            header += f" ({', '.join(stats)})"
        return header
