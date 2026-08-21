"""Which coach pass owns a constraint, and whether one has happened yet.

`constraints.honored_at` means: *a coach pass had this directive in scope, with authority
over every day of it still ahead* (DESIGN_constraint_reschedule.md §8). Two rules follow
from it and both live here rather than being restated at each site:

* **who may stamp** — three commands claim the column over three different ranges
  (`covers`/`covered_ids`/`stamp`); the shape that let `workout accommodate`'s no-change
  path ship with no stamp at all.
* **whether the window tier is worth offering** — `needs_a_pass`, asked by the sweep, the
  `status` line, `constraint list`/`show` and the add-time nudge. Five hand-written copies
  is how `constraint show` came to recommend `workout accommodate` for exactly the
  plan-shaping directives the sweep deliberately skips.
"""
from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional, Tuple

from trainmate.config import config
from trainmate.types import Constraint


def _day(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _iso(day) -> str:
    return day.strftime("%Y-%m-%d")


def constraint_window(
    start_date: str, end_date: str, today: str
) -> Optional[Tuple[str, str]]:
    """The §5 window a `workout accommodate` pass would evaluate for this constraint: its
    own dates ± `config.accommodate_spill_days`, clipped to TOMORROW — today is adapt's,
    and a metric-blind pass must not race it there.

    None when the constraint ends today or earlier: only adapt's own day is left of it.

    Here rather than on the service because `needs_a_pass` below has to ask the same
    question, and both the CLI and the service already ask it.
    """
    tomorrow = _day(today) + timedelta(days=1)
    if _day(end_date) < tomorrow:
        return None
    spill = timedelta(days=config.accommodate_spill_days)
    window_start = max(_day(start_date) - spill, tomorrow)
    return _iso(window_start), _iso(_day(end_date) + spill)


def needs_a_pass(db: Any, constraint: Constraint, today: str) -> bool:
    """Whether the window tier is worth offering for this directive (§8/§10).

    Four terms, cheapest first. Already stamped and there is nothing to offer. A
    `replan = 1` directive belongs to the plan-shaping tier — `workout generate` stamps it
    when its horizon reaches it, so it is unstamped but not unhandled. A window with
    nothing left of it is adapt's day only. And a window holding no sessions has nothing
    to reshuffle, so naming it would be a nudge the athlete can act on in no way at all.
    """
    if constraint.get('honored_at'):
        return False
    if constraint.get('replan'):
        return False
    window = constraint_window(constraint['start_date'], constraint['end_date'], today)
    if not window:
        return False
    return bool(db.get_workouts(start_date=window[0], end_date=window[1]))


def constraints_needing_a_pass(db: Any, today: str) -> List[Constraint]:
    """Every directive the window tier should be offered for, oldest first.

    The sweep's subject and the `status` line's count come from this one call, so the
    screen that nags and the command that acts can never disagree about the set.
    """
    return [c for c in db.get_constraints(start=today) if needs_a_pass(db, c, today)]


def covers(constraint: Constraint, range_start: str, range_end: str) -> bool:
    """Whether a pass over `range_start..range_end` had authority over every day of
    `constraint` still ahead of it.

    Which reduces to: the constraint ENDS inside the range. Its start may sit before
    `range_start` — no command has authority over days already behind it, so demanding the
    literal whole window would leave any constraint already under way flagged forever. It
    may not end before the range begins, or the pass never had it in scope at all.
    """
    return range_start <= constraint['end_date'] <= range_end


def covered_ids(
    constraints: Iterable[Constraint], range_start: str, range_end: str
) -> Tuple[int, ...]:
    """The ids a pass over this range may stamp. Decided at proposal time and carried on
    the proposal, so apply never re-derives it from a set edited since (§8)."""
    return tuple(
        c['id'] for c in constraints if covers(c, range_start, range_end)
    )


def stamp(db: Any, constraint_ids: Iterable[int]) -> None:
    """Records the pass. The one write site — every command reaches `honored_at` here."""
    for constraint_id in constraint_ids:
        db.mark_honored(constraint_id)
