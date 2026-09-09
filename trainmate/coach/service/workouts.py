from contextlib import nullcontext
from datetime import date, datetime, timedelta
from typing import Any, List, Optional, Tuple, Dict
from trainmate.config import config
from trainmate.types import Constraint, Workout
from trainmate.adherence import analyze_adherence
from trainmate.coach import honoring
from trainmate.coach.proposals import GenerateProposal, StandingLine
from trainmate.coach.revisions import normalize_load_fields, prescription_matches
from trainmate import settings
from trainmate.sports import canonical_sport
from trainmate.benchmarks import MIN_RETEST_DAYS
from trainmate.calendar_reconcile import verbose_events
from trainmate import intensity
from trainmate.util import green, cmd, notice, keep_whole
import trainmate.coach.service as _svc


# What a void says when nothing the coach answered explains it: the second and later
# sessions on a date a rest constraint clears carry the constraint's own title, and
# everything else at least names who did it (DESIGN_plan_change_continuity.md §5.5).
REPLACED_DAY_REASON = "Your coach replaced this day."


class WorkoutGenMixin:
    """Part of :class:`CoachService` — see coach/service/__init__.py."""

    @staticmethod
    def _commitment_window(today_str: str) -> Optional[str]:
        """The last day of the commitment window, or None when it is empty (§4.1).

        `N` days starting today, so `1` covers today alone and `0` covers nothing."""
        days = settings.commitment_days() or 0
        if days <= 0:
            return None
        opened = datetime.strptime(today_str, "%Y-%m-%d").date()
        return (opened + timedelta(days=days - 1)).strftime("%Y-%m-%d")

    @staticmethod
    def _standing_block(
        standing: List[Workout], window_end: Optional[str]
    ) -> List[Workout]:
        """The sessions the coach must answer for (§4.2): the ones inside the commitment
        window, plus every session the athlete added by hand anywhere in the span.

        Both are already bounded by the span, because `standing` is read from it — which
        is the second bound the intersection needs, or a forward-selected run would ask
        the coach about days it cannot write and the preview would lie."""
        block = [
            w for w in standing
            if (window_end is not None and w['date'] <= window_end)
            or w.get('source') == 'manual'
        ]
        return sorted(block, key=lambda w: (w['date'], canonical_sport(w['sport_type'])))

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
    def _rest_workout(
        date: str, cause: str, title: str = 'Rest (forced constraint)'
    ) -> Dict[str, Any]:
        """A deterministic rest session the rest-window pre-pass places on a date the
        athlete has barred from training (DESIGN_constraints.md §6). The title/description
        are tagged "(forced constraint)" so it reads unmistakably as a code-enforced
        override rather than an ordinary planned/adapted rest day. The change_reason names
        the constraint so a later adaptation, which won't see the live constraint list in
        the same run, reads the cause back with the plan.

        `title` names which pass placed the row: the coverage backstop
        (DESIGN_runway_nudge.md §2.1) fills an ordinary "Rest Day", not a forced one."""
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
        gen_end: str, standing: Optional[List[Workout]] = None
    ) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], str]]:
        """Forces `rest` constraints onto a freshly generated workout list (§6): every rest
        date inside the generated span becomes a single rest, deterministically, bypassing
        the LLM for that date entirely. Every other constraint is advisory only — left to
        the model via the prompt block, not enforced here (§5).

        Dates the model simply left out are filled too, not only the ones it scheduled: an
        absent row and an explicit rest day mean different things to adherence (§6). The
        span is the *requested* `gen_start`..`gen_end`, not what the model happened to
        return — a rest window at the tail of the range is exactly the case the model
        answers with silence, so bounding by its last date would reopen the gap (§6).

        The rest row continues the first standing session on the date, so that day keeps
        one event and the constraint's own title is the reason on it; any further standing
        session there is voided under the same reason, which is what the returned
        `{slot: reason}` map carries (DESIGN_plan_change_continuity.md §5.5)."""
        full_rest = cls._hard_rest_windows(constraints)
        if not full_rest:
            return workouts, {}

        span_end = gen_end
        forced: Dict[str, str] = {}          # date -> constraint title
        for (s, e, title) in full_rest:
            day = datetime.strptime(max(s, gen_start), "%Y-%m-%d").date()
            last = datetime.strptime(min(e, span_end), "%Y-%m-%d").date()
            while day <= last:
                forced.setdefault(day.strftime("%Y-%m-%d"), title)
                day += timedelta(days=1)
        if not forced:
            return workouts, {}

        rest_sport = canonical_sport('rest')
        by_date: Dict[str, List[Workout]] = {}
        for live in standing or []:
            by_date.setdefault(live['date'], []).append(live)

        out = [w for w in workouts if w.get('date', '') not in forced]
        void_reasons: Dict[Tuple[str, str], str] = {}
        for day, title in sorted(forced.items()):
            rest = cls._rest_workout(day, f"constraint '{title}'")
            rows = sorted(
                by_date.get(day, []), key=lambda w: canonical_sport(w['sport_type'])
            )
            # A rest day already standing on the date is the slot the rest row lands in,
            # so it is revised in place and no lineage is carried across.
            carrier = None
            standing_sports = {canonical_sport(r['sport_type']) for r in rows}
            if rest_sport not in standing_sports and rows:
                carrier = rows[0]
            if carrier is not None:
                rest['replaces_slot'] = (carrier['date'], carrier['sport_type'])
                rest['replaces_lineage'] = (
                    None if carrier.get('source') == 'manual' else carrier['id']
                )
            for row in rows:
                slot = (row['date'], canonical_sport(row['sport_type']))
                if row is carrier or slot[1] == rest_sport:
                    continue
                void_reasons[slot] = rest['change_reason']
            out.append(rest)
        return out, void_reasons

    @classmethod
    def _fill_coverage_gaps(
        cls, workouts: List[Dict[str, Any]], gen_start: str, gen_end: str
    ) -> List[Dict[str, Any]]:
        """Writes an explicit rest row on every date of the span the proposal left empty,
        so generation covers its whole span by construction (DESIGN_runway_nudge.md §2.1).

        That coverage is what lets the end of the schedule be read straight off the rows:
        a hole then means the schedule stopped, never a rest day the model didn't bother
        to name. A proposal with no sessions at all is left alone — an empty answer is a
        failed generation, and a span of rest is not the way to salvage it."""
        if not workouts:
            return workouts
        covered = {w.get('date', '') for w in workouts}
        day = datetime.strptime(gen_start, "%Y-%m-%d").date()
        last = datetime.strptime(gen_end, "%Y-%m-%d").date()
        filled = list(workouts)
        while day <= last:
            date_str = day.strftime("%Y-%m-%d")
            day += timedelta(days=1)
            if date_str in covered:
                continue
            filled.append(cls._rest_workout(
                date_str, "no session planned for this day", title='Rest Day'
            ))
        return filled

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
    def _dropped_rest(answer: Dict[str, Any], source: Workout) -> Dict[str, Any]:
        """A `drop` as the rest day that takes the session's place (§4.5/§5.4).

        There is one path through apply for every answer but `keep`, and a dropped ride
        leaves exactly what an adapted one does: one event on the day, now titled "Rest
        Day", with the ride underneath it in History and the coach's sentence on it."""
        reason = str(answer.get('change_reason') or '').strip()
        body = reason or f"{source['title']} cancelled by your coach."
        return {
            'date': source['date'],
            'sport_type': 'rest',
            'title': 'Rest Day',
            'description': f"[Rest Day]\n{body}",
            'duration_minutes': 0,
            'rpe': 0,
            'tss': 0,
            'change_reason': reason,
            'replaces': {'date': source['date'], 'sport_type': source['sport_type']},
        }

    @classmethod
    def _resolve_standing(
        cls, workouts: List[Dict[str, Any]], standing: List[Workout],
        gen_start: str, gen_end: str
    ) -> List[Dict[str, Any]]:
        """Maps the coach's answers onto the standing block, so every pass after this one
        sees a uniform list of full sessions
        (DESIGN_plan_change_continuity.md §4.5, §7).

        `keep` becomes the session it names, `drop` becomes a rest day replacing it, and a
        full entry on a date whose only standing session is a rest day replaces that rest
        day whether the coach said so or not. What an entry takes the place of travels on
        it as `replaces_slot`/`replaces_lineage`, which apply turns into a void plus an
        append on the same lineage. A standing session no answer names is kept.
        """
        rest_sport = canonical_sport('rest')
        by_slot = {(w['date'], canonical_sport(w['sport_type'])): w for w in standing}
        per_date: Dict[str, List[Workout]] = {}
        for live in standing:
            per_date.setdefault(live['date'], []).append(live)
        # A rest day and a session on the same date cannot both be true, so the coach is
        # not offered the choice: a full entry there replaces the rest day (§4.5).
        rest_only = {
            day: rows[0] for day, rows in per_date.items()
            if len(rows) == 1 and canonical_sport(rows[0]['sport_type']) == rest_sport
        }

        entries: List[Dict[str, Any]] = []
        for w in workouts:
            if not w.get('drop'):
                entries.append(w)
                continue
            slot = (w.get('date'), canonical_sport(w.get('sport_type', '')))
            source = by_slot.get(slot)
            if source is None or slot[1] == rest_sport:
                notice(
                    f"The coach dropped {w.get('sport_type', '')} on {w.get('date')}, "
                    f"but no session of yours stands there — ignoring it.",
                )
                continue
            entries.append(cls._dropped_rest(w, source))

        def source_of(entry: Dict[str, Any]) -> Optional[Tuple[str, str]]:
            """The standing slot an entry takes a session out of, or None."""
            slot = (entry.get('date'), canonical_sport(entry.get('sport_type', '')))
            replaces = entry.get('replaces')
            if isinstance(replaces, dict) and replaces.get('date'):
                named = (
                    replaces['date'], canonical_sport(replaces.get('sport_type', ''))
                )
                return None if named == slot else named
            rest = rest_only.get(entry.get('date'))
            if rest is None:
                return None
            rest_slot = (rest['date'], rest_sport)
            return None if rest_slot == slot else rest_slot

        # Read before any entry is resolved, so a destination is judged against what the
        # occupant's OWN answer does with it (§4.5).
        vacated = {
            source for source in
            (source_of(e) for e in entries if not e.get('keep'))
            if source is not None
        }
        written = {
            (w.get('date'), canonical_sport(w.get('sport_type', '')))
            for w in entries if not w.get('keep')
        }

        out: List[Dict[str, Any]] = []
        answered: set = set()        # standing slots an accepted entry has spoken for
        taken: set = set()           # destination slots an accepted entry has claimed
        for entry in entries:
            slot = (entry.get('date'), canonical_sport(entry.get('sport_type', '')))
            if entry.get('keep'):
                live = by_slot.get(slot)
                if live is None or slot in written or slot in taken:
                    continue
                out.append({**live, 'keep': True})
                answered.add(slot)
                taken.add(slot)
                continue
            if slot in taken:
                notice(
                    f"Two sessions came back for {slot[0]} ({entry.get('sport_type', '')})"
                    f" — keeping the first and dropping the rest.",
                )
                continue
            source = source_of(entry)
            if source is not None and source not in by_slot:
                notice(
                    f"The coach said this {entry.get('date')} session replaces one on "
                    f"{source[0]} that is not among the sessions it was shown — writing "
                    f"it where it stands and leaving that day alone.",
                )
                source = None
            if source is not None:
                if not (gen_start <= (entry.get('date') or '') <= gen_end):
                    notice(
                        f"The coach moved the {source[0]} session to "
                        f"{entry.get('date')}, outside the days this run writes — "
                        f"leaving it where it is.",
                    )
                    continue
                if source in answered:
                    notice(
                        f"Two answers came back for the {source[0]} session — keeping "
                        f"the first.",
                    )
                    continue
                if slot in by_slot and slot not in vacated:
                    notice(
                        f"The coach moved the {source[0]} session onto "
                        f"{entry.get('date')}, where a session it is keeping already "
                        f"stands — leaving both where they are.",
                    )
                    continue
                occupant = by_slot[source]
                entry = {
                    **entry,
                    'replaces_slot': (occupant['date'], occupant['sport_type']),
                    # A session the athlete added keeps its own lineage: the coach's
                    # replacement starts a new one, or the event would read "[Manual]"
                    # (§5.3).
                    'replaces_lineage': (
                        None if occupant.get('source') == 'manual' else occupant['id']
                    ),
                }
                answered.add(source)
            elif slot in by_slot:
                answered.add(slot)
            out.append(entry)
            taken.add(slot)

        # Silence is how a JSON-mode model fails, and a cancellation has to be said (§4.5).
        for slot, live in by_slot.items():
            if slot in answered:
                continue
            out.append({**live, 'keep': True, 'unmentioned': True})
        return out

    @staticmethod
    def _generate_voids(
        workouts: List[Dict[str, Any]], standing: List[Workout],
        void_reasons: Dict[Tuple[str, str], str]
    ) -> Tuple[Tuple[str, str, str], ...]:
        """Every slot this run ends, with the reason it will carry (§5.5).

        Decided here rather than inside apply so the preview reports the removals that
        will actually happen, including the ones no answer explains."""
        final = {(w['date'], canonical_sport(w['sport_type'])) for w in workouts}
        kept = {
            (w['date'], canonical_sport(w['sport_type']))
            for w in workouts if w.get('keep')
        }
        voids: List[Tuple[str, str, str]] = []
        seen: set = set()
        for w in workouts:
            replaced = w.get('replaces_slot')
            if not replaced:
                continue
            slot = (replaced[0], canonical_sport(replaced[1]))
            if slot in seen:
                continue
            seen.add(slot)
            reason = str(w.get('change_reason') or '').strip() or REPLACED_DAY_REASON
            voids.append((replaced[0], replaced[1], reason))
        for live in standing:
            slot = (live['date'], canonical_sport(live['sport_type']))
            if slot in seen:
                continue
            if slot in final:
                # Written over where it stands. Only a session the athlete added needs a
                # void: it is what keeps its Calendar event, marked, instead of the event
                # being torn down with no trace (§5.3). A coach session is simply
                # superseded by the append.
                if live.get('source') != 'manual' or slot in kept:
                    continue
            seen.add(slot)
            voids.append((
                live['date'], live['sport_type'],
                void_reasons.get(slot) or REPLACED_DAY_REASON,
            ))
        return tuple(voids)

    @staticmethod
    def _becomes(entry: Dict[str, Any], live: Workout) -> Tuple[str, str]:
        """`(outcome, becomes)` for one standing session the run rewrites (§4.5)."""
        if entry.get('date') != live['date']:
            return 'moved', entry['date']
        duration = entry.get('duration_minutes')
        title = entry.get('title') or ''
        becomes = f"{title} {duration}m" if duration else title
        return 'revised', becomes

    @classmethod
    def _standing_lines(
        cls, workouts: List[Dict[str, Any]], block: List[Workout],
        voids: Tuple[Tuple[str, str, str], ...]
    ) -> Tuple[StandingLine, ...]:
        """The §4.5 report, built from what apply will write rather than from what the
        coach answered: the no-op rule silently suppresses a revision whose prescription
        did not move, and the deterministic passes remove sessions no answer mentions."""
        by_slot = {(w['date'], canonical_sport(w['sport_type'])): w for w in workouts}
        by_source: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for w in workouts:
            replaced = w.get('replaces_slot')
            if replaced:
                by_source[(replaced[0], canonical_sport(replaced[1]))] = w
        reasons = {(d, canonical_sport(sp)): r for (d, sp, r) in voids}

        lines: List[StandingLine] = []
        for live in block:
            slot = (live['date'], canonical_sport(live['sport_type']))
            entry = by_slot.get(slot)
            if entry is not None and (
                entry.get('keep') or prescription_matches(entry, live)
            ):
                lines.append(StandingLine(
                    date=live['date'], sport_type=live['sport_type'],
                    title=live['title'],
                    duration_minutes=live.get('duration_minutes'),
                    outcome='kept', mentioned=not entry.get('unmentioned'),
                ))
                continue
            target = entry if entry is not None else by_source.get(slot)
            if target is None:
                lines.append(StandingLine(
                    date=live['date'], sport_type=live['sport_type'],
                    title=live['title'],
                    duration_minutes=live.get('duration_minutes'),
                    outcome='cancelled', reason=reasons.get(slot, ''),
                ))
                continue
            outcome, becomes = cls._becomes(target, live)
            lines.append(StandingLine(
                date=live['date'], sport_type=live['sport_type'], title=live['title'],
                duration_minutes=live.get('duration_minutes'),
                outcome=outcome, becomes=becomes,
                reason=str(target.get('change_reason') or '').strip(),
            ))
        return tuple(lines)

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

        # Back to the start of the block the athlete is in, not to the span: a constraint
        # that ended last week is why three sessions are missing from the block's record,
        # and the coach cannot see the days themselves (§6.1). Only the ones still live
        # are worked around; the rest are context, and are kept out of the honoring and
        # rest-window passes below.
        block_now = self._db.get_covering_mesocycle(today_str)
        constraint_floor = min(
            (block_now or {}).get('start_date') or gen_start_str, gen_start_str
        )
        all_constraints = self._db.get_constraints(constraint_floor)
        constraints = [c for c in all_constraints if c['end_date'] >= gen_start_str]
        past_constraints = [c for c in all_constraints if c['end_date'] < gen_start_str]
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
                + cmd(keep_whole(f"-M {macro_id}"), quote=False) + " to follow that one.",
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
        # The plan this run would rewrite, and the part of it the athlete has already
        # been told about (DESIGN_plan_change_continuity.md §4.2). Read once and used by
        # the prompt, the resolver, the void set and the report.
        standing = self._db.get_workouts(
            start_date=gen_start_str, end_date=gen_end_str
        )
        window_end = self._commitment_window(today_str)
        standing_block = self._standing_block(standing, window_end)
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
            standing_workouts=standing_block,
            commitment_end=window_end,
            past_constraints=past_constraints,
        )

        # NOTE: workout generation is read-only w.r.t. coach learnings (see
        # DESIGN_backward_evaluation.md §11) — it does not apply learning_updates. Durable
        # memory is authored only by `analyze` and `plan generate`.

        # Save workouts to database
        workouts = plan_data.get("workouts", [])

        # Every answer becomes a full session, so each pass below sees one kind of entry
        # and the void loop has nothing to void in the standing block
        # (DESIGN_plan_change_continuity.md §7).
        workouts = self._resolve_standing(
            workouts, standing_block, gen_start_str, gen_end_str
        )
        athlete_note = str(plan_data.get("athlete_note") or "").strip() or None

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
        workouts, void_reasons = self._enforce_rest_windows_generate(
            workouts, constraints, gen_start_str, gen_end_str, standing
        )

        # Boundary-week benchmark post-check (§4.1): warn (don't auto-insert) if a covered
        # block boundary lacks a fitness test. Runs after the rest pass so a rest-covered
        # boundary week is already silenced.
        self._warn_missing_boundary_benchmarks(
            workouts, constraints, blocks, gen_start_str
        )

        # Coverage backstop (DESIGN_runway_nudge.md §2.1): every date of the span carries a
        # row, so a hole is the schedule ending rather than a rest day the model skipped.
        # After the benchmark post-check, which reads the span the model actually reached.
        workouts = self._fill_coverage_gaps(workouts, gen_start_str, gen_end_str)

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
        voids = self._generate_voids(workouts, standing, void_reasons)

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
            voids=voids,
            standing=self._standing_lines(workouts, standing_block, voids),
            athlete_note=athlete_note,
            commitment_end=window_end,
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

        Which slots are voided and why was decided at proposal time, so the report the
        operator accepted and the write are the same set
        (DESIGN_plan_change_continuity.md §5.5). A session that takes another's place —
        a move, a sport change, a drop — carries that session's lineage, so the day keeps
        one Calendar event rather than losing one and gaining another (§4.5).

        The span is `gen_start`..`gen_end`, so sessions outside it survive a bounded
        regeneration untouched (DESIGN_cli_selectors.md §8).

        Returns the sessions the plan now holds in the slots it proposed.
        """
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
                kind="generate", summary=summary, macrocycle_id=span_macro,
                note=proposal.athlete_note,
                commitment_end=proposal.commitment_end,
            ) as change,
        ):
            standing = self._db.get_workouts(
                start_date=proposal.gen_start, end_date=proposal.gen_end or None
            )
            voided_slots = {
                (day, canonical_sport(sport)) for (day, sport, _r) in proposal.voids
            }
            replaced_manual = [
                live for live in standing
                if live.get('source') == 'manual'
                and (live['date'], canonical_sport(live['sport_type'])) in voided_slots
            ]
            for (day, sport_type, reason) in proposal.voids:
                change.void(date=day, sport_type=sport_type, reason=reason)
            # The flag belongs to the TEST, not to the slot: a rewrite of a benchmark's
            # slot that does not re-emit benchmark_type is an ordinary session, and the
            # carry-forward must not make it a test (DESIGN_benchmark_workouts.md §4.2,
            # as adapt already does).
            tested_slots = {
                (live['date'], canonical_sport(live['sport_type']))
                for live in standing if live.get('benchmark_type')
            }
            for w in proposal.workouts:
                # Claimed above, so it escaped the void; writing it again would churn a
                # day that did not change (§7.1).
                if w.get('keep'):
                    continue
                # The intensity target the coach stated while it still knew the intent
                # (DESIGN_intensity_distribution.md §9.8) — validated, never rescaled.
                zone_currency, zone_sec = intensity.parse_planned_zones(w)
                slot = (w['date'], canonical_sport(w['sport_type']))
                change.append(
                    date=w['date'],
                    sport_type=w['sport_type'],
                    title=w['title'],
                    description=w['description'],
                    duration_minutes=w.get('duration_minutes'),
                    rpe=w.get('rpe'),
                    tss=w.get('tss'),
                    # The coach's sentence to the athlete about this day, which is what
                    # puts it on the Calendar's `Reason:` line (§4.5).
                    reason=str(w.get('change_reason') or '').strip() or None,
                    benchmark_type=w.get('benchmark_type'),
                    clear_benchmark=bool(
                        slot in tested_slots and not w.get('benchmark_type')
                    ),
                    macrocycle_id=w.get('macrocycle_id'),
                    lineage_id=w.get('replaces_lineage'),
                    planned_zone_currency=zone_currency,
                    planned_zone_sec=zone_sec,
                )
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
