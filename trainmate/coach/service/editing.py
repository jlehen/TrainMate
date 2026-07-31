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
from trainmate.coach.engine import CoachEngine
from trainmate.coach.formatting import format_baseline, _load_science_guidelines
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
