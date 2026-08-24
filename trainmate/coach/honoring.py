"""Whether the plan reflects a constraint yet, and who may say that it does.

`constraints.honored_at` means: *a coach pass had this directive in scope, with authority
over every day of it still ahead* (DESIGN_constraint_honoring.md §2). Two rules follow
from it and both live here rather than being restated at each site:

* **who may stamp** — `workout adapt` and `workout generate` claim the column over two
  different ranges (`covers`/`covered_ids`/`stamp`).
* **whether the plan is missing it** — `needs_a_pass`, asked by the `status` line,
  `constraint list`/`show` and the add-time nudge. Hand-written copies of that rule are
  how `constraint show` came to flag exactly the plan-shaping directives the `status`
  line deliberately skips.
"""
from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional, Tuple

from trainmate.types import Constraint


def _day(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _iso(day) -> str:
    return day.strftime("%Y-%m-%d")


def constraint_window(
    start_date: str, end_date: str, today: str
) -> Optional[Tuple[str, str]]:
    """The days of this constraint a future pass could still act on: its own dates,
    clipped to TOMORROW — today belongs to `workout adapt`, which runs with the full
    metrics picture.

    None when the constraint ends today or earlier: only adapt's own day is left of it.
    """
    tomorrow = _day(today) + timedelta(days=1)
    if _day(end_date) < tomorrow:
        return None
    return _iso(max(_day(start_date), tomorrow)), end_date


def needs_a_pass(db: Any, constraint: Constraint, today: str) -> bool:
    """Whether the plan is missing this directive and something could still be done about
    it (DESIGN_constraint_honoring.md §2).

    Four terms, cheapest first. Already stamped and there is nothing to report. A
    `replan = 1` directive is built into the plan by `plan generate`, so it is unstamped
    but not unhandled. A window with nothing left of it is adapt's day only. And a window
    holding no sessions has nothing to reshuffle, so naming it would be a nudge the
    athlete can act on in no way at all.
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
    """Every directive the plan does not reflect yet, oldest first.

    The `status` count comes from this one call, so every screen that reports the gap
    reports the same set.
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
    the proposal, so apply never re-derives it from a set edited since (§3)."""
    return tuple(
        c['id'] for c in constraints if covers(c, range_start, range_end)
    )


def stamp(db: Any, constraint_ids: Iterable[int]) -> None:
    """Records the pass. The one write site — every command reaches `honored_at` here."""
    for constraint_id in constraint_ids:
        db.mark_honored(constraint_id)
