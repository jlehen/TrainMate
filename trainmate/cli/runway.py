"""The end-of-schedule nudge, shared by every surface that draws it
(DESIGN_runway_nudge.md).

One fetch, one detector, one command named — so `workout adapt`, `status` and the
morning push cannot answer the same morning differently (§3). The detection itself is
`progression.runway`, pure over rows; this module is the thin db-reads wrapper around it
plus the wordings each surface asks for, the same split `timeline.py` makes around
`progression.assemble_timeline`.
"""
from typing import Any, Dict, List, Optional, Tuple

from trainmate import progression
from trainmate.config import config
from trainmate.cli.common import is_simple_render, simple_when
from trainmate.progression import RUNWAY_BLOCK, RUNWAY_PLAN_END_NEXT_GOAL, RUNWAY_SPAN
from trainmate.util import (
    cmd, days_between, fmt_date, gray, notice, today_str as _today_str,
)

# What the morning push offers on a span or block cliff (§6). The label is the athlete's;
# the argv behind it comes from the detector, never from the free-text router.
RUNWAY_BUTTON_LABEL = "📅 Plan my next weeks"

# What the push says instead of the rest-day line once the schedule is exhausted (§6):
# an empty day is then the schedule running out, not a coaching decision.
SIMPLE_PASSED_LINE = "You've finished everything on the schedule 🎉"

# The one-liner the companion week view ends on when its window crosses the cliff (§6),
# mirroring the expert listing's marker.
SIMPLE_END_NOTE = "That's the end of the current schedule."


def _plan_blocks() -> List[Dict[str, Any]]:
    """The blocks of the plan the current workouts implement.

    `get_governing_macrocycle`, deliberately not `upcoming_objectives`: that filter drops
    a goal the day after its target date, which is exactly the morning the wrap-up
    message matters most (DESIGN_runway_nudge.md §2)."""
    from trainmate import runtime
    macro = runtime.db.get_governing_macrocycle()
    if not macro:
        return []
    return runtime.db.get_mesocycles_for_macrocycle(macro["id"])


def current_runway(as_of: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """`progression.runway` over the rows this instance holds, or None when nothing
    fires. `as_of` defaults to today; `workout adapt` passes its evaluation date."""
    from trainmate import runtime
    return progression.runway(
        runtime.db.get_workouts(),
        _plan_blocks(),
        runtime.db.get_objectives(),
        as_of or _today_str(),
        config.runway_warning_days,
    )


def schedule_coverage() -> Tuple[Optional[str], Optional[str]]:
    """`(last covered date, plan end)` — the raw facts behind the detector, for the
    listing marker that draws them whether or not the nudge is firing (§4)."""
    from trainmate import runtime
    blocks = _plan_blocks()
    ends = [str(b["end_date"]) for b in blocks if b.get("end_date")]
    return progression.coverage_end(runtime.db.get_workouts()), max(ends) if ends else None


def schedule_exhausted(as_of: str) -> bool:
    """Whether the generated schedule has already run out on `as_of`. Distinct from the
    detector firing: a schedule that ran out months ago is exhausted and silent (§6)."""
    last_covered, _ = schedule_coverage()
    return last_covered is not None and last_covered < as_of


def plan_is_behind(as_of: str) -> bool:
    """Whether every block of the governing plan ended before `as_of` — the state in
    which `workout adapt` has nothing to adapt towards and refuses (§4)."""
    blocks = _plan_blocks()
    ends = [str(b["end_date"]) for b in blocks if b.get("end_date")]
    return bool(ends) and max(ends) < as_of


def _when(days_left: int, date_str: str) -> str:
    """'today' / 'in 4 day(s), on <date>' / '3 day(s) ago, on <date>' — the old block
    hint's idiom, extended to the passed state. Day zero never reads 'in 0 day(s)' (§4)."""
    if days_left == 0:
        return "today"
    if days_left > 0:
        return f"in {days_left} day(s), on {fmt_date(date_str)}"
    return f"{-days_left} day(s) ago, on {fmt_date(date_str)}"


def _plan_left(today: str, plan_end: str) -> str:
    """How much periodization is left, for the span wording: weeks once there is a week
    of it, days below that."""
    days = days_between(today, plan_end)
    if days >= 7:
        weeks = round(days / 7)
        return f"covers {weeks} more week{'s' if weeks != 1 else ''}"
    if days >= 1:
        return f"covers {days} more day{'s' if days != 1 else ''}"
    return "ends today"


def runway_hint_lines(state: Dict[str, Any], today: str) -> List[str]:
    """The §4 hint: the fact, then the exact command. Two lines whatever the cliff, so
    every surface prints the same shape and the athlete learns one place to look."""
    days_left = state["days_left"]
    when = _when(days_left, state["last_covered_date"])
    kind = state["kind"]

    if kind == RUNWAY_BLOCK:
        meso_id = state["next_mesocycle"]["id"]
        return [
            f"This block {'ends' if days_left >= 0 else 'ended'} {when}, and the next "
            f"one has no fresh sessions.",
            "Run " + cmd(f"workout generate -m ..{meso_id}")
            + " to plan it against current metrics.",
        ]
    if kind == RUNWAY_SPAN:
        return [
            f"Scheduled workouts {'run' if days_left >= 0 else 'ran'} out {when}.",
            f"Your plan {_plan_left(today, state['plan_end'])} — run "
            + cmd("workout generate") + " to schedule the next span.",
        ]

    verb = "ends" if days_left >= 0 else "ended"
    last = "today" if days_left == 0 else f"on {fmt_date(state['last_covered_date'])}"
    if kind == RUNWAY_PLAN_END_NEXT_GOAL:
        obj = state["objective"]
        return [
            f"Your plan {verb} with its last session {last}.",
            f"Next up: {obj['title']} ({fmt_date(str(obj['target_date']))}) — run "
            + cmd("workout generate -g") + " to build toward it.",
        ]
    # RUNWAY_PLAN_END_NO_GOAL — the last of the four kinds.
    return [
        f"Your plan {verb} with its last session {last} — nothing is planned beyond it.",
        "Set what's next with " + cmd("goal add") + ", then " + cmd("plan generate") + ".",
    ]


def print_runway_hint(state: Optional[Dict[str, Any]], today: str) -> bool:
    """Draws the §4 hint and says whether it drew anything.

    Silent under `TRAINMATE_RENDER=simple`: the companion surface words the same fact
    itself, and one fact gets one wording per message (§6)."""
    if not state or is_simple_render():
        return False
    print()
    for line in runway_hint_lines(state, today):
        notice(line)
    print()
    return True


def _crossing(end_date: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    """`(last covered date, plan end)` when a listing ending on `end_date` runs past the
    end of the schedule, else None. An open-ended listing always crosses it."""
    last_covered, plan_end = schedule_coverage()
    if last_covered is None:
        return None
    if end_date is not None and end_date <= last_covered:
        return None
    return last_covered, plan_end


def list_end_marker(end_date: Optional[str]) -> Optional[str]:
    """The one gray line `workout list` draws after the last session when the listed
    range crosses the end of the schedule (§4).

    Unconditional — a fact of the listing, not a warning, so it ignores the runway
    window entirely."""
    crossing = _crossing(end_date)
    if crossing is None:
        return None
    last_covered, plan_end = crossing
    tail = (
        f"plan continues to {fmt_date(plan_end)}"
        if plan_end and plan_end > last_covered else "end of plan"
    )
    return gray(f"— end of scheduled workouts ({tail}) —")


def simple_end_note(end_date: Optional[str]) -> Optional[str]:
    """The companion week view's version of the marker above (§6)."""
    return SIMPLE_END_NOTE if _crossing(end_date) else None


def simple_runway_lines(state: Dict[str, Any], today: str) -> List[str]:
    """The morning push's companion wording for one runway state (§6).

    Span and block cliffs read as an offer, because the button beside them performs it;
    a plan cliff reads as a wrap-up, because periodization is operator work in companion
    mode and there is nothing here for the athlete to tap."""
    kind, days_left = state["kind"], state["days_left"]
    if kind in (RUNWAY_BLOCK, RUNWAY_SPAN):
        if days_left < 0:
            return ["Want me to plan the next few weeks?"]
        when = simple_when(state["last_covered_date"], today)
        return [f"Heads up — your schedule runs out {when}. "
                "Want me to plan the next few weeks?"]

    wrap_up = (
        "wrapped up" if days_left < 0
        else f"wraps up {simple_when(state['last_covered_date'], today)}"
    )
    lead = f"🎉 Your plan {wrap_up} — that's the goal you've been training toward!"
    if kind == RUNWAY_PLAN_END_NEXT_GOAL:
        obj = state["objective"]
        when = simple_when(str(obj["target_date"]), today)
        return [f"{lead} Next up is {obj['title']} ({when}) — your coach sets that "
                "stretch up from the computer."]
    return [f"{lead} When you know what you'd like to work toward next, tell your "
            "coach — setting up a new goal happens from the computer."]


def runway_argv(state: Optional[Dict[str, Any]]) -> Optional[str]:
    """The command the §6 button sends, or None for the cliffs that offer no button.

    The narrow amendment to DESIGN_bot_simple_frontend.md §7's guardrail: `workout
    generate` becomes tappable here and only here, with argv the detector chose."""
    if not state:
        return None
    if state["kind"] == RUNWAY_BLOCK:
        return f"workout generate -m ..{state['next_mesocycle']['id']}"
    if state["kind"] == RUNWAY_SPAN:
        return "workout generate"
    return None


def runway_buttons(state: Optional[Dict[str, Any]]) -> List[dict]:
    """The push's runway row: one button, or none at all. Its own gate, independent of
    the session rows' `if ahead:` — each condition answers its own question (§6)."""
    utterance = runway_argv(state)
    if not utterance:
        return []
    return [{"label": RUNWAY_BUTTON_LABEL, "send": utterance}]
