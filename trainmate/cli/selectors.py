"""One selector grammar for every filtering command (DESIGN_cli_selectors.md).

`-d/--date`, `-m/--mesocycle`, `-M/--macrocycle` and `-g/--goal` all take the same
`A..B` range spelling; `resolve_window` turns whichever of them were given into a single
(start_date, end_date) pair. Each command declares its own default window and direction
when it registers the flags, so no handler re-implements "no filter means the last N days".
"""
import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from trainmate.util import red, today_date as _today_date, today_str as _today_str

# `trainmate_cli` (the db facade) is imported lazily inside the resolvers: it imports the
# CLI package, so a module-level import here is a cycle.

SEP = ".."

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_OFFSET_RE = re.compile(r"([+-]?)(\d+(?:\.\d+)?)([dw])$")

# Every command that filters by range, in the athlete's words. Kept next to the grammar
# so the error messages and the help strings say the same thing.
RANGE_SYNTAX = (
    "DATE, DATE.., ..DATE, DATE..DATE, or a span like 7d / 2w "
    "(dates are YYYY-MM-DD, 'today', or a signed offset like -7d / +2w)"
)


class SelectorError(argparse.ArgumentTypeError):
    """A selector that does not parse. Raised at argparse time, reported like any bad value."""


@dataclass(frozen=True)
class DateRange:
    """A parsed `-d` selector, still unresolved: `today` and offsets resolve at use time."""
    start: Optional[str] = None
    end: Optional[str] = None
    span: Optional[str] = None  # bare span ('7d'): which side of today it covers is the
                                # command's call, not the selector's


@dataclass(frozen=True)
class IdRange:
    """A parsed `-m`/`-M`/`-g` selector. `current` is the bare flag: the one in progress."""
    start: Optional[int] = None
    end: Optional[int] = None
    current: bool = False


CURRENT = IdRange(current=True)


def _offset_days(sign: str, amount: str, unit: str) -> int:
    days = float(amount) * (7 if unit == "w" else 1)
    return int(round(days)) * (-1 if sign == "-" else 1)


def _parse_date_atom(raw: str) -> str:
    """Resolves one endpoint — an ISO date, `today`, or a signed offset — to an ISO date."""
    atom = raw.strip().lower()
    if atom == "today":
        return _today_str()
    if _ISO_RE.match(atom):
        try:
            datetime.strptime(atom, "%Y-%m-%d")
        except ValueError:
            raise SelectorError(f"'{raw}' is not a real date (YYYY-MM-DD).")
        return atom
    match = _OFFSET_RE.match(atom)
    if match and match.group(1):
        return (
            _today_date() + timedelta(days=_offset_days(*match.groups()))
        ).strftime("%Y-%m-%d")
    if match:
        raise SelectorError(
            f"'{raw}' needs a sign to be an endpoint: '-{atom}' for {atom} ago, "
            f"'+{atom}' for {atom} ahead. Without one it is a span, which only stands alone."
        )
    raise SelectorError(f"'{raw}' is not a date. Use {RANGE_SYNTAX}.")


def parse_date_range(raw: str) -> DateRange:
    """argparse type for `-d/--date`. The grammar is `A..B` with either side optional."""
    text = raw.strip()
    if not text:
        raise SelectorError(f"Empty date selector. Use {RANGE_SYNTAX}.")
    if SEP not in text:
        if _OFFSET_RE.match(text.lower()) and not text.startswith(("+", "-")):
            return DateRange(span=text.lower())
        day = _parse_date_atom(text)
        return DateRange(start=day, end=day)
    if text.count(SEP) > 1:
        raise SelectorError(f"'{raw}' has more than one '..'. Use {RANGE_SYNTAX}.")
    left, right = text.split(SEP)
    if not left and not right:
        raise SelectorError(f"'{raw}' bounds nothing. Use {RANGE_SYNTAX}.")
    start = _parse_date_atom(left) if left else None
    end = _parse_date_atom(right) if right else None
    if start and end and start > end:
        raise SelectorError(f"'{raw}' ends before it starts.")
    return DateRange(start=start, end=end)


def _parse_id_atom(raw: str, label: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise SelectorError(f"'{raw}' is not a {label} ID. Use ID, ID.., ..ID or ID..ID.")


def parse_id_range(raw: str, label: str = "mesocycle") -> IdRange:
    """argparse type for `-m`/`-M`/`-g` in their filtering role — same grammar over IDs."""
    text = raw.strip()
    if not text:
        return CURRENT
    if SEP not in text:
        one = _parse_id_atom(text, label)
        return IdRange(start=one, end=one)
    if text.count(SEP) > 1:
        raise SelectorError(f"'{raw}' has more than one '..'.")
    left, right = text.split(SEP)
    if not left and not right:
        raise SelectorError(f"'{raw}' bounds nothing. Use ID, ID.., ..ID or ID..ID.")
    return IdRange(
        start=_parse_id_atom(left, label) if left else None,
        end=_parse_id_atom(right, label) if right else None,
    )


def _maybe_date_atom(raw: str) -> Optional[str]:
    """`_parse_date_atom` for an atom that may not be a date at all: returns the ISO day
    when the spelling *is* a date one, None when it is something else entirely (a block
    name). A malformed date still raises — `2026-13-40` is a typo, not a name."""
    atom = raw.strip().lower()
    if atom == "today" or _ISO_RE.match(atom):
        return _parse_date_atom(raw)
    match = _OFFSET_RE.match(atom)
    if match and match.group(1):
        return _parse_date_atom(raw)
    if match:
        raise SelectorError(
            f"'{raw}' is a span, not a day: a note files to the block covering ONE day. "
            f"Use '-{atom}' for {atom} ago or '+{atom}' for {atom} ahead."
        )
    return None


def _block_listing(mesocycles: list) -> str:
    """This plan's blocks, name and dates, to retry an unmatched atom against — an error
    listing rather than an interactive picker, so the bot behaves identically (§5)."""
    lines = "\n".join(
        f"  [{m['id']}] {m['name']} ({m['start_date']} -> {m['end_date']})"
        for m in mesocycles
    )
    return f"This plan's blocks:\n{lines}"


def resolve_meso_atom(atom, mesocycles: list) -> dict:
    """The ONE block an atom names, resolved against a plan's own blocks
    (DESIGN_plan_feedback.md §5).

    A bare integer is a mesocycle ID, a date atom is the block covering that day, and
    anything else is a case-insensitive infix of a block name that must match exactly
    one. `CURRENT` (a bare `-m`) is the block covering today. Raises `SelectorError`
    naming the plan's blocks when nothing — or more than one thing — matches.

    Single-target on purpose: range spellings are refused, because a note files to one
    block. The range grammar can lift this the day a command wants `-m climb` as a
    filter (DESIGN_cli_selectors.md §1)."""
    if not mesocycles:
        raise SelectorError("This plan has no blocks to file a note against.")
    listing = _block_listing(mesocycles)

    if atom is None or atom == CURRENT or (isinstance(atom, str) and not atom.strip()):
        today = _today_str()
        block = next(
            (m for m in mesocycles if m['start_date'] <= today <= m['end_date']), None
        )
        if not block:
            raise SelectorError(f"No block of this plan covers today ({today}).\n{listing}")
        return block

    text = str(atom).strip()
    if SEP in text:
        raise SelectorError(
            f"'{text}' is a range; a note files to one block, so -m takes a single atom."
        )
    if text.isdigit():
        block = next((m for m in mesocycles if m['id'] == int(text)), None)
        if not block:
            raise SelectorError(
                f"Mesocycle {text} is not part of this plan.\n{listing}"
            )
        return block

    day = _maybe_date_atom(text)
    if day:
        block = next(
            (m for m in mesocycles if m['start_date'] <= day <= m['end_date']), None
        )
        if not block:
            raise SelectorError(f"No block of this plan covers {day}.\n{listing}")
        return block

    matches = [m for m in mesocycles if text.lower() in m['name'].lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SelectorError(f"No block name contains '{text}'.\n{listing}")
    names = ", ".join(f"'{m['name']}'" for m in matches)
    raise SelectorError(f"'{text}' matches several blocks ({names}).\n{listing}")


def parse_single_date(raw: str) -> str:
    """argparse type for the commands that act on ONE day (`workout adapt`, `benchmark
    record`): the same atoms as a range endpoint, but no `..`."""
    if SEP in raw:
        raise SelectorError(f"'{raw}' is a range; this command takes a single date.")
    return _parse_date_atom(raw)


def parse_target(raw: str):
    """argparse type for a positional target: a row ID, or a date selector.

    Returns ``('id', int)`` or ``('date', DateRange)``. A bare integer is always an ID —
    which is why a span has to carry its unit (`7d`, not `7`)."""
    text = raw.strip()
    if text.isdigit():
        return ("id", int(text))
    return ("date", parse_date_range(text))


def add_selector_args(
    parser, *, date=True, meso=False, macro=False, goal=False, sport=False,
    direction="backward", default=None, span_days=7, group=None, horizon=False,
):
    """Registers this command's selector flags and records how it fills the gaps.

    ``direction`` and ``default`` are the command's own policy, read back by
    ``resolve_window``: ``forward`` plans (an open end stays open), ``backward`` reviews
    history (an open end is today), ``none`` sweeps everything it is not told to spare.
    ``default`` is a selector string used only when no dimension is given at all.
    ``group`` puts the flags in a mutually exclusive group while the policy still rides on
    the parser (`workout generate`, where the horizon is one choice among several).
    ``horizon`` says the same flags read as an end date rather than a filter — the command
    always starts today and uses only the window's END (§8) — so the help says so."""
    target = group if group is not None else parser
    lead = "Generate through the END of" if horizon else "Restrict to"
    if date:
        target.add_argument(
            "-d", "--date", dest="date_range", type=parse_date_range, metavar="RANGE",
            help=f"{lead} a date range: {RANGE_SYNTAX}"
                 + (f" (default: {default})" if default else "")
        )
    if meso:
        target.add_argument(
            "-m", "--mesocycle", dest="meso_range", type=parse_id_range, nargs="?",
            const=CURRENT, metavar="RANGE",
            help=f"{lead} a mesocycle range: ID, ID.., ..ID or ID..ID "
                 "(bare -m is the current block)"
        )
    if macro:
        target.add_argument(
            "-M", "--macrocycle", dest="macro_range", nargs="?", const=CURRENT,
            type=lambda raw: parse_id_range(raw, "macrocycle"), metavar="RANGE",
            help=f"{lead} a macrocycle range: ID, ID.., ..ID or ID..ID "
                 "(bare -M is the active plan). List IDs with 'plan versions'."
                 + (" Naming one plan also settles which to follow where two cover the "
                    "same days." if horizon else "")
        )
    if goal:
        target.add_argument(
            "-g", "--goal", dest="goal_range", nargs="?", const=CURRENT,
            type=lambda raw: parse_id_range(raw, "goal"), metavar="RANGE",
            help=(
                "Generate through a goal's target date — i.e. the whole plan: ID, ID.., "
                "..ID or ID..ID (bare -g is the active goal)"
                if horizon else
                "Restrict to the plan span of a goal range: ID, ID.., ..ID or ID..ID "
                "(bare -g is the active goal)"
            )
        )
    if sport:
        target.add_argument(
            "-t", "--type", "--sport-type", "--sport", dest="sport_type",
            help="Filter by sport type"
        )
    parser.set_defaults(_selector_policy=(direction, default, span_days))


def add_single_date_arg(parser, help_text: str):
    """Registers `-d/--date` for a command that acts on one day rather than a range."""
    parser.add_argument(
        "-d", "--date", dest="date", type=parse_single_date, metavar="DATE",
        help=help_text
    )


def _fail(message: str) -> None:
    print(red(f"Error: {message}"))
    sys.exit(1)


def _meso_bounds(meso_id: int) -> tuple[str, str]:
    from trainmate import runtime
    meso = runtime.db.get_mesocycle(meso_id)
    if not meso:
        _fail(f"Mesocycle with ID {meso_id} not found.")
    return meso['start_date'], meso['end_date']


def _current_meso_bounds() -> tuple[str, str]:
    from trainmate import runtime
    meso = runtime.db.get_active_mesocycle(_today_str())
    if not meso:
        _fail("No active mesocycle found.")
    return meso['start_date'], meso['end_date']


def _macro_bounds(macro_id: int) -> tuple[str, str]:
    from trainmate import runtime
    macro = runtime.db.get_macrocycle(macro_id)
    if not macro:
        _fail(f"Macrocycle with ID {macro_id} not found. Run 'plan versions' to list them.")
    mesos = runtime.db.get_mesocycles_for_macrocycle(macro_id)
    if not mesos:
        _fail(f"Macrocycle {macro_id} has no mesocycles.")
    return min(m['start_date'] for m in mesos), max(m['end_date'] for m in mesos)


def _goal_bounds(goal_id: int) -> tuple[str, str]:
    """A goal's span: from the start of its plan to the goal's own target date — which is
    past the last mesocycle when the plan doesn't reach the event yet."""
    from trainmate import runtime
    goal = runtime.db.get_objective(goal_id)
    if not goal:
        _fail(f"Goal with ID {goal_id} not found.")
    return _macro_bounds(_goal_macro_id(goal_id))[0], goal['target_date']


def _goal_macro_id(goal_id: int) -> int:
    from trainmate import runtime
    macro = runtime.db.get_macrocycle_for_objective(goal_id)
    if not macro:
        _fail(f"No plan exists for goal ID {goal_id}.")
    return macro['id']


def _active_goal_id() -> int:
    from trainmate import runtime
    goal = runtime.db.get_active_objective()
    if not goal:
        _fail("No active goal found.")
    return goal['id']


def _active_macro_id() -> int:
    return _goal_macro_id(_active_goal_id())


def _id_window(rng: IdRange, bounds, current) -> tuple[Optional[str], Optional[str]]:
    """Turns an ID range into a date window: the start of the first, the end of the last."""
    if rng.current:
        return current()
    start = bounds(rng.start)[0] if rng.start is not None else None
    end = bounds(rng.end)[1] if rng.end is not None else None
    return start, end


def _date_window(
    rng: DateRange, direction: str, today: str
) -> tuple[Optional[str], Optional[str]]:
    if rng.span is None:
        return rng.start, rng.end
    match = _OFFSET_RE.match(rng.span)
    days = max(1, _offset_days("", match.group(2), match.group(3)))
    if direction == "forward":
        return today, (_today_date() + timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return (_today_date() - timedelta(days=days - 1)).strftime("%Y-%m-%d"), today


def _span_before(end: str, span_days: int) -> str:
    return (
        datetime.strptime(end, "%Y-%m-%d").date() - timedelta(days=span_days - 1)
    ).strftime("%Y-%m-%d")


def resolve_window(args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    """Intersects every selector the athlete gave into one (start_date, end_date).

    Whichever side no selector bounds is filled by the command's own policy — that is the
    only place a command-specific default lives (DESIGN_cli_selectors.md §3)."""
    direction, default, span_days = getattr(
        args, "_selector_policy", ("backward", None, 7)
    )
    today = _today_str()
    windows = []

    date_range = getattr(args, "date_range", None)
    if date_range is not None:
        windows.append(_date_window(date_range, direction, today))
    meso_range = getattr(args, "meso_range", None)
    if meso_range is not None:
        windows.append(_id_window(meso_range, _meso_bounds, _current_meso_bounds))
    macro_range = getattr(args, "macro_range", None)
    if macro_range is not None:
        windows.append(_id_window(
            macro_range, _macro_bounds, lambda: _macro_bounds(_active_macro_id())
        ))
    goal_range = getattr(args, "goal_range", None)
    if goal_range is not None:
        windows.append(_id_window(
            goal_range, _goal_bounds, lambda: _goal_bounds(_active_goal_id())
        ))
    for extra in getattr(args, "_extra_windows", ()):  # positional date targets
        windows.append(extra)

    if not windows:
        if default is None:
            return None, None
        windows.append(_date_window(parse_date_range(default), direction, today))

    starts = [w[0] for w in windows if w[0]]
    ends = [w[1] for w in windows if w[1]]
    start = max(starts) if starts else None
    end = min(ends) if ends else None

    if direction == "forward" and start is None:
        start = today
    elif direction == "backward":
        end = end or today
        if start is None:
            start = _span_before(end, span_days)

    if start and end and start > end:
        _fail(f"The filters do not overlap: {start} is after {end}.")
    return start, end


def has_selector(args: argparse.Namespace) -> bool:
    """True when the athlete bounded the window themselves, rather than taking the default."""
    return any(
        getattr(args, name, None) is not None
        for name in ("date_range", "meso_range", "macro_range", "goal_range")
    )


def split_targets(targets) -> tuple[list, list]:
    """Splits parsed positional targets into (row IDs, date windows)."""
    ids = [value for kind, value in targets or () if kind == "id"]
    ranges = [value for kind, value in targets or () if kind == "date"]
    return ids, ranges
