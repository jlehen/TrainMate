"""Per-mesocycle, per-sport, per-zone time in zone — the intensity axis TSS folds away.

``load = volume x intensity``, and TSS is the product: given it you can recover neither
factor, so a block whose easy days drifted to tempo reads as flat weekly TSS and a flat
PMC. Zone distribution is the only intensity signal in the schema. The whole rationale —
why every zone is reported separately, why per sport, why rates over completed weeks
only — lives in DESIGN_intensity_distribution.md.

No DB access here (same rule as ``trainmate.sports``): callers pass activity rows or a
fetch callable, so one implementation serves `workout adapt`, the strategy prompt and
the CLI block summary.
"""
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from trainmate.benchmarks import ANCHOR_KINDS, format_delta, format_value
from trainmate.config import config
from trainmate.garmin.load import _rpe_tss, activity_load
from trainmate.sports import canonical_sport, is_strength_sport


class Currency(NamedTuple):
    """One measurement currency and the zone model it is bucketed into (§4/§5)."""
    key: str
    tag: str
    prefix: str
    labels: Tuple[str, ...]


# Power first: it is instantaneous, so it is the currency to read on a short-interval
# session (§7). Every zone stands alone — no Z1-2/Z3/Z4-5 banding (§5).
CURRENCIES: Tuple[Currency, ...] = (
    Currency("power", "[pwr]", "power_zone", (
        "recovery", "endurance", "tempo", "threshold", "VO2max",
        "anaerobic", "neuromuscular",
    )),
    Currency("hr", "[HR]", "zone", (
        "recovery", "aerobic", "tempo", "threshold", "VO2max+",
    )),
)
CURRENCY_BY_KEY: Dict[str, Currency] = {c.key: c for c in CURRENCIES}

# Zone tables are column-aligned, so they are wrapped once here and never re-wrapped
# downstream — a screen-width re-wrap would shred the columns (§6).
PROMPT_WIDTH = 100

# A week's row admits it is incomplete below this — `config.zone_coverage_display_min`
# (0.8). Deliberately NOT `hr_zone_coverage_min` (0.5), which is a "safe to compute load
# from" bar: a week at 55% clears that while missing nearly half its recorded time (§9.6).
#
# The bar is PER SPORT, because uncovered time is not always a recording failure: it is
# also the rest between sets, the chairlift back up, the gentle walking on a hike, the
# held pose. Those seconds sit below zone 1 and no zone claims them, so one bar
# calibrated on continuous efforts condemns every week of every sport that has them.
# Over six months of history cycling and ski touring hold ~0.95 median coverage, while
# strength training holds 0.90 with a 0.49 lower quartile, resort skiing 0.26 and hiking
# 0.15. Each bar below sits near its own sport's 25th percentile, so `!` marks the worst
# quarter of that sport's weeks rather than all of them (§11).
#
# Keys are CANONICAL sports, because the lookup canonicalizes first: an alias key here is
# unreachable and the sport silently falls back to the global bar (§11 rev note 2026-08-04).
COVERAGE_MIN_BY_SPORT: Dict[str, float] = {
    "strength_training": 0.45,
    "downhill_skiing": 0.15,
    "indoor_climbing": 0.15,
    "hiking": 0.10,
    "yoga": 0.05,
}


def coverage_display_min(sport: str) -> float:
    """The bar one sport's weekly row is graded against: a config override first, then
    the shipped per-sport table, then the global default (§11)."""
    key = canonical_sport(sport)
    override = config.zone_coverage_display_min_by_sport.get(key)
    if override is not None:
        return override
    return COVERAGE_MIN_BY_SPORT.get(key, config.zone_coverage_display_min)


def judgeable(act: Dict[str, Any]) -> bool:
    """Whether an activity is big enough to carry a claim about recording quality, or
    about undercounted load (`config.zone_min_activity_minutes`, §11).

    A 5-minute mobility session with a cold strap is not evidence that a 340-TSS week is
    undercounted, and it is not evidence about the strap either — it is below the noise
    floor of both questions. Its load and its zone minutes still count everywhere; only
    its vote on the markers is withheld."""
    return float(act.get("duration_sec") or 0.0) / 60.0 >= config.zone_min_activity_minutes
# Power is instantaneous and so the currency to read, but only once it can see the whole
# window — the fraction it cannot see is the meterless easy commutes (§9.6). Shares a
# number with the bar above and nothing else: that one grades a single week's row, this
# one picks the column a whole table is drawn in.
PREFER_POWER_COVERAGE_MIN = 0.8

# Two claims with different scopes, so two notes: strength is a property of the sport and
# is suppressed when no strength sport is on screen; interval work with rest happens in
# running, cycling and rowing alike and never is (§9.6).
HR_STRENGTH_NOTE = (
    "Note: HR during strength work reflects rest between sets as much as effort — "
    "read those rows beside the session RPE."
)
HR_INTERVAL_NOTE = (
    "Note: HR during interval work with rest reflects the rest as much as the effort "
    "— read those rows beside the session RPE."
)
HR_LAG_NOTE = (
    "Note: HR needs 60-90s to climb, so short VO2max intervals bank most of their "
    "seconds in Z4 — measured by HR a genuine VO2max block looks like a threshold "
    "block. Prefer the power row where one exists."
)
NEVER_SUM_NOTE = (
    "Note: the HR and power rows of one sport are two views of the SAME time, never a "
    "total — a ride with a meter appears in both. Never add them."
)

FetchActivities = Callable[[str, str], List[Dict[str, Any]]]


# --------------------------------------------------------------------- dates

def _d(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _s(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def counted_days(start: str, end: str, as_of: str) -> int:
    """Days of the block that are over: elapsed before `as_of`, capped at the block's
    own span. Zero or negative means the block has not started."""
    span = (_d(end) - _d(start)).days + 1
    return min((_d(as_of) - _d(start)).days, span)


def rate_window(start: str, end: str, as_of: str) -> Optional[Tuple[str, str, int]]:
    """``(window_start, window_end, completed_weeks)`` — the whole 7-day weeks of a block
    finished by `as_of`, the only span a per-week rate may divide (§4).

    Weeks run from the block's own start, not calendar Mondays, so blocks compare
    like for like. The partial tail is excluded from BOTH sides of the division;
    including it understates easy volume every time, in the same direction, because
    the long easy session sits on the weekend. None when under a week has elapsed.
    """
    days = counted_days(start, end, as_of)
    weeks = days // 7 if days > 0 else 0
    if weeks < 1:
        return None
    return start, _s(_d(start) + timedelta(days=weeks * 7 - 1)), weeks


def current_week_window(start: str, end: str, as_of: str) -> Optional[Tuple[str, str, int]]:
    """``(week_start, as_of, day_of_week)`` for the week in progress (§9.3), or None when
    `as_of` sits outside the block or exactly on a week boundary with nothing elapsed."""
    if not (start <= as_of <= end):
        return None
    elapsed = (_d(as_of) - _d(start)).days
    if elapsed < 0:
        return None
    week_start = _d(start) + timedelta(days=(elapsed // 7) * 7)
    return _s(week_start), as_of, elapsed % 7 + 1


def block_weeks(start: str, end: str) -> int:
    """The block's planned length in weeks, rounded up — the denominator of
    '2 completed weeks of 4'."""
    return max(1, ((_d(end) - _d(start)).days + 7) // 7)


# --------------------------------------------------------- zone aggregation

class ZoneRow(NamedTuple):
    """One (canonical sport x currency) row: seconds per zone over a window (§4).

    `coverage` is that currency's recorded seconds over the sport's TOTAL duration in
    the window, so both currencies share one denominator and read comparably (§7): a
    ride with no meter contributes its full duration and zero power seconds.

    `judged_coverage` is the same ratio over the activities big enough to say anything
    about recording quality (`judgeable`), and is what the `!` marker reads; None when
    none of the window's sessions for this sport clear that floor, which is not a
    failing recording but an unanswerable question (§11). `coverage` keeps every
    session, because the header percentage and the currency choice are about how much
    of the training this currency saw — a different question.
    """
    sport: str
    currency: str
    seconds: Tuple[float, ...]
    coverage: float
    judged_coverage: Optional[float] = None

    @property
    def total(self) -> float:
        return sum(self.seconds)

    @property
    def undercounted(self) -> bool:
        """Whether this row should mark itself incomplete (§11): a judgeable coverage
        below the sport's own bar. Unjudgeable rows never mark."""
        return (
            self.judged_coverage is not None
            and self.judged_coverage < coverage_display_min(self.sport)
        )


def sport_durations(activities: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Total recorded duration per canonical sport — `zone_rows`' denominator, exposed
    on its own because a sport can have sessions and no zone rows at all.

    That distinction is the whole reason the weekly table needs three states rather than
    two: no duration means not trained, duration with no zone seconds means trained and
    not recorded, and the two must not render as the same thing (§9.6).
    """
    out: Dict[str, float] = {}
    for act in activities:
        sport = canonical_sport(act.get("activity_type") or "unknown")
        out[sport] = out.get(sport, 0.0) + float(act.get("duration_sec") or 0.0)
    return out


def pick_currency(coverage: Dict[str, float]) -> Optional[str]:
    """The one currency a sport's table is drawn in, from that sport's per-currency
    coverage over the WHOLE window (§9.6).

    Prefer power once it reaches `PREFER_POWER_COVERAGE_MIN`, otherwise take whichever
    currency covers more. Power is more precise at the top end and blind to every ride
    without a meter, so below that bar it cannot answer "did my easy volume shrink" —
    the fraction it cannot see IS the easy commutes. Chosen once over the window and
    never per row: a column that switched currency mid-table would be adding HR minutes
    to power minutes down the page, §6's one prohibition committed vertically.
    """
    power, hr = coverage.get("power") or 0.0, coverage.get("hr") or 0.0
    if power >= PREFER_POWER_COVERAGE_MIN:
        return "power"
    if not power and not hr:
        return None
    return "power" if power > hr else "hr"


def currency_by_sport(activities: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """`{canonical sport: 'power'|'hr'}` over one window — §9.6's currency rule applied to
    raw activity rows, for callers that have no weekly payload to read it from.

    `workout generate` uses it to pick the currency it PRESCRIBES in (§9.8). One rule
    applied twice: get it wrong in either place and the plan is written in a currency the
    table never renders. Sports with no recorded zone seconds are absent from the result
    — swimming is anchored on CSS and strength on e1RM, and neither yields a zone model.
    """
    coverage: Dict[str, Dict[str, float]] = {}
    for row in zone_rows(activities):
        coverage.setdefault(row.sport, {})[row.currency] = row.coverage
    out = {}
    for sport, cov in coverage.items():
        picked = pick_currency(cov)
        if picked:
            out[sport] = picked
    return out


# A sport must hold this share of the window's duration to earn a table by default; the
# rest are named in the footer, never silently dropped.
ZONE_SPORT_MIN_SHARE = 0.10


def window_sport_stats(weeks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per canonical sport over the displayed window: total duration and total recorded
    seconds per currency — everything the sport filter, the 10% floor and the currency
    choice read.

    All three are computed over the DISPLAYED window, which is what the header's share
    figure means. One consequence to expect rather than to fix: `--weeks all` can qualify
    a different set of sports than the default does, and can draw one of them in a
    different currency, because both rules read the window they are given (§9.6).
    """
    stats: Dict[str, Dict[str, Any]] = {}

    def entry(sport: str) -> Dict[str, Any]:
        return stats.setdefault(sport, {"seconds": 0.0, "zone_seconds": {}})

    for week in weeks:
        for sport, secs in (week.get("sport_seconds") or {}).items():
            entry(sport)["seconds"] += secs
        for row in week.get("zone_rows") or []:
            zones = entry(row.sport)["zone_seconds"]
            zones[row.currency] = zones.get(row.currency, 0.0) + row.total
    for agg in stats.values():
        agg["coverage"] = {
            cur: (secs / agg["seconds"] if agg["seconds"] else 0.0)
            for cur, secs in agg["zone_seconds"].items()
        }
    return stats


def select_zone_sports(
    explicit: Optional[Sequence[str]], preferences: Sequence[str],
    stats: Dict[str, Dict[str, Any]],
) -> Tuple[List[str], List[str], List[str]]:
    """`(sports, omitted_low_volume, omitted_no_zone_data)` — which sports get a table.

    Naming sports explicitly overrides all three filters; the default is every
    `sport_preferences` entry, **in config order** (the athlete's own priority list, and
    stable across invocations — a screen someone checks daily must not reshuffle its rows
    because last week's volume moved) that has zone data in the window and holds at least
    `ZONE_SPORT_MIN_SHARE` of its duration.
    """
    if explicit:
        return [canonical_sport(s) for s in explicit], [], []
    total = sum(agg["seconds"] for agg in stats.values())
    sports, low, no_data = [], [], []
    for name in preferences:
        sport = canonical_sport(name)
        agg = stats.get(sport)
        if not agg or not agg["seconds"]:
            continue
        if not any(agg["zone_seconds"].values()):
            no_data.append(sport)
        elif total and agg["seconds"] / total < ZONE_SPORT_MIN_SHARE:
            low.append(sport)
        else:
            sports.append(sport)
    return sports, low, no_data


def zone_currency(
    stats: Dict[str, Dict[str, Any]], sport: str, forced: Optional[str] = None
) -> Optional[str]:
    """The currency one sport's table is drawn in. `forced` (`--power`/`--hr`) wins where
    that currency has data, and has no effect on a sport that has only the other."""
    agg = stats.get(sport)
    if not agg:
        return None
    if forced and agg["zone_seconds"].get(forced):
        return forced
    return pick_currency(agg["coverage"])


# The window the planning currency is chosen over. Authoring has no window of its own —
# at generation time there is only forward plan — so it borrows the display default, and
# the ordinary case agrees by construction (§9.8).
PLANNING_COVERAGE_WEEKS = 8


def parse_planned_zones(
    payload: Dict[str, Any]
) -> Tuple[Optional[str], Optional[List[Optional[int]]]]:
    """`(currency, 7-slot seconds list)` from one LLM-authored workout, or `(None, None)`.

    Validates rather than corrects: an unknown currency, a non-list, or an all-zero
    distribution yields nothing at all, and anything past the currency's zone count is
    dropped. What it does NOT do is scale the seconds to match `duration_minutes` — the
    numbers are a prescription, not an accounting identity, and rescaling them would put
    the app back in the business of correcting the model rather than aligning for it (§7).
    """
    currency = (payload.get("planned_zone_currency") or "").strip().lower()
    spec = CURRENCY_BY_KEY.get(currency)
    raw = payload.get("planned_zone_sec")
    if spec is None or not isinstance(raw, (list, tuple)):
        return None, None
    out: List[Optional[int]] = [None] * 7
    for i in range(min(len(raw), len(spec.labels))):
        try:
            value = int(round(float(raw[i])))
        except (TypeError, ValueError):
            continue
        out[i] = max(0, value)
    if not any(out):
        return None, None
    return currency, out


def planned_zone_seconds(workout: Dict[str, Any]) -> Optional[Tuple[str, Tuple[int, ...]]]:
    """`(currency, seconds per zone)` from a planned workout's columns, or None when the
    session carries no intensity target (§9.8)."""
    currency = (workout.get("planned_zone_currency") or "").strip().lower()
    spec = CURRENCY_BY_KEY.get(currency)
    if spec is None:
        return None
    secs = tuple(
        int(workout.get(f"planned_zone{i}_sec") or 0)
        for i in range(1, len(spec.labels) + 1)
    )
    return (currency, secs) if any(secs) else None


def format_planned_zones(workout: Dict[str, Any]) -> Optional[str]:
    """'Target: ~25min recovery, ~30min aerobic, ~10min threshold' — the prescription an
    athlete can act on, rendered FROM the columns at display time and never stored.

    `description` is in `CALENDAR_FIELDS`, so storing this sentence would mark the row
    stale and re-push the Calendar event on every regeneration that nudges a target by
    two minutes (§9.8). Zone NAMES, not indices: `30 min aerobic` survives a ruler shift
    in a way `30 min Z2` does not — the index is the join key, the name is the
    prescription.
    """
    parsed = planned_zone_seconds(workout)
    if parsed is None:
        return None
    currency, secs = parsed
    labels = CURRENCY_BY_KEY[currency].labels
    parts = [
        f"~{int(round(s / 60.0))}min {labels[i]}"
        for i, s in enumerate(secs) if s
    ]
    return f"Target: {', '.join(parts)}" if parts else None


def zone_rows(activities: Sequence[Dict[str, Any]]) -> List[ZoneRow]:
    """Zone seconds per canonical sport and currency, busiest sport first.

    Currency is per ACTIVITY, not per sport (§6): a ride that recorded both joins its
    sport's HR row and its power row. A currency row exists only where that currency
    has recorded seconds, so strength training never grows an empty `[pwr] 0%` row.
    """
    duration: Dict[str, float] = {}
    acc: Dict[Tuple[str, str], List[float]] = {}
    # The same two totals over judgeable sessions only — the basis for `!` (§11).
    judged_duration: Dict[str, float] = {}
    judged: Dict[Tuple[str, str], float] = {}
    for act in activities:
        sport = canonical_sport(act.get("activity_type") or "unknown")
        secs = float(act.get("duration_sec") or 0.0)
        big = judgeable(act)
        duration[sport] = duration.get(sport, 0.0) + secs
        if big:
            judged_duration[sport] = judged_duration.get(sport, 0.0) + secs
        for cur in CURRENCIES:
            vals = [
                float(act.get(f"{cur.prefix}{i}_sec") or 0.0)
                for i in range(1, len(cur.labels) + 1)
            ]
            if not any(vals):
                continue
            bucket = acc.setdefault((sport, cur.key), [0.0] * len(cur.labels))
            for i, v in enumerate(vals):
                bucket[i] += v
            if big:
                key = (sport, cur.key)
                judged[key] = judged.get(key, 0.0) + sum(vals)

    def _judged_coverage(sport: str, key: str) -> Optional[float]:
        denom = judged_duration.get(sport)
        return (judged.get((sport, key), 0.0) / denom) if denom else None

    rows = [
        ZoneRow(
            sport=sport, currency=key, seconds=tuple(vals),
            coverage=(sum(vals) / duration[sport]) if duration.get(sport) else 0.0,
            judged_coverage=_judged_coverage(sport, key),
        )
        for (sport, key), vals in acc.items()
    ]
    order = [c.key for c in CURRENCIES]
    rows.sort(key=lambda r: (-duration.get(r.sport, 0.0), r.sport, order.index(r.currency)))
    return rows


def planned_zone_rows(workouts: Sequence[Dict[str, Any]]) -> List[ZoneRow]:
    """The same `(sport x currency x zone)` shape, summed over PLANNED sessions — the
    future half of the weekly table (§9.8).

    `coverage` is 1.0 throughout: a prescription is not a recording, so there is no
    measurement gap to report and the `!` marker has nothing to say about these rows.
    """
    acc: Dict[Tuple[str, str], List[float]] = {}
    for w in workouts:
        parsed = planned_zone_seconds(w)
        if parsed is None:
            continue
        currency, secs = parsed
        sport = canonical_sport(w.get("sport_type") or "unknown")
        bucket = acc.setdefault((sport, currency), [0.0] * len(secs))
        for i, s in enumerate(secs):
            bucket[i] += s
    return [
        ZoneRow(sport=sport, currency=key, seconds=tuple(vals), coverage=1.0)
        for (sport, key), vals in acc.items()
    ]


# ------------------------------------------------------------------ render

def fmt_duration(seconds: float) -> str:
    """'55m' under an hour, '3h39' above — the compact form the tables use."""
    minutes = int(round(seconds / 60.0))
    if abs(minutes) < 60:
        return f"{minutes}m"
    sign = "-" if minutes < 0 else ""
    minutes = abs(minutes)
    return f"{sign}{minutes // 60}h{minutes % 60:02d}"


def _signed_duration(seconds: float) -> str:
    body = fmt_duration(abs(seconds))
    return f"+{body}" if seconds >= 0 else f"-{body}"


def _prefix(sport: str, currency: str, sport_width: int) -> str:
    return f"{sport.ljust(sport_width)}  {CURRENCY_BY_KEY[currency].tag.ljust(5)}  "


def _lay_out(prefix: str, cells: Sequence[str], width: int) -> List[str]:
    """A row's cells over as many lines as `width` allows, continuations hanging under
    the first cell so the sport/currency prefix is never repeated."""
    if not cells:
        return []
    cell_w = max(len(c) for c in cells) + 2
    per_line = max(1, (width - len(prefix)) // cell_w)
    lines, pad = [], " " * len(prefix)
    for i in range(0, len(cells), per_line):
        head = prefix if i == 0 else pad
        body = "".join(c.ljust(cell_w) for c in cells[i:i + per_line])
        lines.append((head + body).rstrip())
    return lines


def _zone_cells(row: ZoneRow, divisor: float, with_pct: bool) -> List[str]:
    """One cell per zone, named — bare 'Z4' is markedly less legible to a model than
    'Z4 threshold' (§5). Percentages are of RECORDED zone seconds, so they sum to 100
    and the coverage line carries the recording gap on its own (§4)."""
    labels = CURRENCY_BY_KEY[row.currency].labels
    total = row.total
    cells = []
    for i, label in enumerate(labels, start=1):
        figure = fmt_duration(row.seconds[i - 1] / divisor)
        cell = f"Z{i} {label} {figure}"
        if with_pct:
            pct = (row.seconds[i - 1] / total * 100.0) if total else 0.0
            cell += f" ({pct:.0f}%)"
        cells.append(cell)
    return cells


def format_table(
    rows: Sequence[ZoneRow], divisor: float = 1.0, with_pct: bool = True,
    indent: str = "", width: int = PROMPT_WIDTH,
) -> List[str]:
    """The zone table itself. `divisor` turns totals into a per-week rate."""
    if not rows:
        return []
    sport_width = max(len(r.sport) for r in rows)
    out: List[str] = []
    for row in rows:
        prefix = indent + _prefix(row.sport, row.currency, sport_width)
        out.extend(_lay_out(prefix, _zone_cells(row, divisor, with_pct), width))
    return out


def format_coverage(
    rows: Sequence[ZoneRow], indent: str = "", width: int = PROMPT_WIDTH
) -> List[str]:
    """'Coverage: running 94% HR · cycling 96% HR, 67% power'.

    Low coverage means the effort sat BELOW Z1, not that nothing was done — a model
    reading absence as inactivity prescribes more aerobic volume on top of a base
    block that already had it (§7). Zeros are indistinguishable from NULLs in the
    columns themselves, so this fraction does all that work.
    """
    if not rows:
        return []
    per_sport: Dict[str, List[Tuple[str, str]]] = {}
    order: List[str] = []
    for row in rows:
        if row.sport not in per_sport:
            per_sport[row.sport] = []
            order.append(row.sport)
        name = "power" if row.currency == "power" else "HR"
        per_sport[row.sport].append((name, f"{row.coverage * 100:.0f}% {name}"))
    per_sport = {
        sport: [text for _, text in sorted(entries, key=lambda e: e[0] != "HR")]
        for sport, entries in per_sport.items()
    }
    body = " · ".join(f"{sport} {', '.join(per_sport[sport])}" for sport in order)
    return _wrap(f"Coverage: {body}", indent, width)


def _wrap(text: str, indent: str, width: int) -> List[str]:
    """Plain greedy wrap with a hanging indent, so a long note stays inside `width`."""
    words, lines, cur = text.split(), [], indent
    for word in words:
        candidate = f"{cur} {word}" if cur.strip() else cur + word
        if len(candidate) > width and cur.strip():
            lines.append(cur)
            cur = indent + "  " + word
        else:
            cur = candidate
    if cur.strip():
        lines.append(cur)
    return lines


def format_notes(rows: Sequence[ZoneRow], indent: str = "", width: int = PROMPT_WIDTH) -> List[str]:
    """The measurement caveats that travel WITH the numbers — emitted as facts, never
    corrected for (§7). The app aligns; the LLM reasons."""
    out: List[str] = []
    if any(r.currency == "hr" for r in rows):
        out.extend(_wrap(HR_LAG_NOTE, indent, width))
        out.extend(_wrap(HR_INTERVAL_NOTE, indent, width))
        if any(is_strength_sport(r.sport) for r in rows if r.currency == "hr"):
            out.extend(_wrap(HR_STRENGTH_NOTE, indent, width))
    if len({r.sport for r in rows if r.currency == "power"} & {
        r.sport for r in rows if r.currency == "hr"
    }):
        out.extend(_wrap(NEVER_SUM_NOTE, indent, width))
    return out


# ------------------------------------------------------------------- delta

def format_delta_table(
    rows: Sequence[ZoneRow], weeks: int,
    prev_rows: Sequence[ZoneRow], prev_weeks: int, prev_name: str,
    indent: str = "", width: int = PROMPT_WIDTH,
) -> List[str]:
    """Block-over-block change in per-week rates (§4.1).

    A sport or currency missing from either side is reported as absent rather than as a
    -100% swing: a changed sport mix is not intensity creep.
    """
    if not weeks or not prev_weeks:
        return []
    prev_by_key = {(r.sport, r.currency): r for r in prev_rows}
    cur_by_key = {(r.sport, r.currency): r for r in rows}
    cur_sports = {r.sport for r in rows}
    prev_sports = {r.sport for r in prev_rows}
    sport_width = max([len(r.sport) for r in list(rows) + list(prev_rows)] or [1])
    out: List[str] = []
    for row in rows:
        prefix = indent + _prefix(row.sport, row.currency, sport_width)
        prev = prev_by_key.get((row.sport, row.currency))
        if prev is None:
            what = "not trained" if row.sport not in prev_sports else "no data in this currency"
            out.append(f"{prefix}— {what} in {prev_name}, no comparison")
            continue
        out.extend(_lay_out(prefix, _delta_cells(row, weeks, prev, prev_weeks), width))
    for sport, currency in prev_by_key:
        if (sport, currency) in cur_by_key:
            continue
        prefix = indent + _prefix(sport, currency, sport_width)
        what = "not trained" if sport not in cur_sports else "no data in this currency"
        out.append(f"{prefix}— present in {prev_name}, {what} here")
    return out


def _delta_cells(row: ZoneRow, weeks: int, prev: ZoneRow, prev_weeks: int) -> List[str]:
    labels = CURRENCY_BY_KEY[row.currency].labels
    cells = []
    for i, label in enumerate(labels, start=1):
        now = row.seconds[i - 1] / weeks
        before = prev.seconds[i - 1] / prev_weeks
        change = _signed_duration(now - before)
        if before:
            pct = f"{(now - before) / before * 100:+.0f}%"
        else:
            pct = "new" if now else "—"
        cells.append(f"Z{i} {label} {change} ({pct})")
    return cells


# -------------------------------------------------------------- structural

def format_structural(
    activities: Sequence[Dict[str, Any]],
    benchmarks: Optional[Sequence[Dict[str, Any]]],
    start: str, end: str, indent: str = "", width: int = PROMPT_WIDTH,
) -> List[str]:
    """The non-zone view: session counts, RPE and sRPE load per sport, plus any threshold
    that moved inside the window.

    No sport is routed away from the zone table into this one (§6) — a HIIT kettlebell
    session's Z4 minutes are real work, and dropping them tells a coach to prescribe
    intensity on top of intensity already done. This section sits BESIDE the zone rows,
    never instead of them.
    """
    by_sport: Dict[str, Dict[str, float]] = {}
    order: List[str] = []
    for act in activities:
        sport = canonical_sport(act.get("activity_type") or "unknown")
        if sport not in by_sport:
            by_sport[sport] = {"n": 0, "sec": 0.0, "rpe_sum": 0.0, "rpe_n": 0, "srpe": 0.0}
            order.append(sport)
        agg = by_sport[sport]
        duration_sec = float(act.get("duration_sec") or 0.0)
        agg["n"] += 1
        agg["sec"] += duration_sec
        rpe = act.get("rpe")
        if rpe:
            agg["rpe_sum"] += float(rpe)
            agg["rpe_n"] += 1
            agg["srpe"] += _rpe_tss(float(rpe), duration_sec)

    lines: List[str] = []
    rows = [(s, by_sport[s]) for s in order if by_sport[s]["rpe_n"]]
    kinds = {r["anchor_kind"] for r in (benchmarks or []) if start <= r["date"] <= end}
    label_width = max(
        [len(s) for s, _ in rows]
        + [len((ANCHOR_KINDS[k].label if k in ANCHOR_KINDS else k)) for k in kinds]
        + [4]
    )
    for sport, agg in rows:
        n = int(agg["n"])
        lines.append(
            f"{indent}{sport.ljust(label_width)}  {n} session{'s' if n != 1 else ''}, "
            f"{fmt_duration(agg['sec'])}, avg RPE {agg['rpe_sum'] / agg['rpe_n']:.1f}, "
            f"{agg['srpe']:.0f} sRPE load"
        )
    for line in _benchmark_lines(benchmarks or [], start, end, label_width, indent):
        lines.append(line)
    return lines


def _benchmark_lines(
    benchmarks: Sequence[Dict[str, Any]], start: str, end: str,
    label_width: int, indent: str,
) -> List[str]:
    """One line per anchor kind that has a value in the window, measured against the
    latest reading before it. e1RM collides across lifts (§10) — track one lift."""
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for row in benchmarks:
        by_kind.setdefault(row["anchor_kind"], []).append(row)
    out: List[str] = []
    for kind in sorted(by_kind):
        rows = sorted(by_kind[kind], key=lambda r: (r["date"], r.get("id") or 0))
        inside = [r for r in rows if start <= r["date"] <= end]
        if not inside:
            continue
        before = [r for r in rows if r["date"] < start]
        anchor = ANCHOR_KINDS.get(kind)
        label = (anchor.label if anchor else kind).ljust(label_width)
        latest = float(inside[-1]["value"])
        if not before:
            out.append(f"{indent}{label}  {format_value(kind, latest)} (first on record)")
            continue
        baseline = float(before[-1]["value"])
        delta = format_delta(kind, latest, baseline)
        suffix = f" ({delta})" if delta else ""
        out.append(
            f"{indent}{label}  {format_value(kind, baseline)} -> "
            f"{format_value(kind, latest)}{suffix}"
        )
    return out


# ------------------------------------------------------------ the assembler

def format_header(meso: Dict[str, Any], as_of: str, with_focus: bool = True) -> str:
    """'Build 1 — focus "threshold development" (2 completed weeks of 4, plus 2 days)'.

    Rates, not totals: blocks are unequal length and the current one is always partial,
    so the reader is told exactly what divided the numbers (§4). `with_focus` is off for
    the CLI, which has already printed the focus above the table."""
    start, end = meso["start_date"], meso["end_date"]
    days = max(0, counted_days(start, end, as_of))
    weeks, spare = days // 7, days % 7
    total = block_weeks(start, end)
    parts = [f"{weeks} completed week{'s' if weeks != 1 else ''}"]
    if weeks < total:
        parts[0] += f" of {total}"
    if spare:
        parts.append(f"plus {spare} day{'s' if spare != 1 else ''}")
    if not with_focus:
        return f"{meso['name']} ({', '.join(parts)})"
    focus = meso.get("focus") or "unstated"
    return f"{meso['name']} — focus \"{focus}\" ({', '.join(parts)})"


def block_report(
    meso: Dict[str, Any],
    as_of: str,
    fetch_activities: FetchActivities,
    *,
    current_week: bool = False,
    previous: Optional[Dict[str, Any]] = None,
    benchmarks: Optional[Sequence[Dict[str, Any]]] = None,
    with_focus: bool = True,
    notes: bool = True,
    indent: str = "  ",
    width: int = PROMPT_WIDTH,
) -> Optional[str]:
    """The whole intensity report for one mesocycle, or None when it has not started.

    `fetch_activities(start, end)` returns completed activities over an inclusive window.
    `current_week` adds the in-progress week as RAW minutes beside the elapsed fraction —
    never extrapolated, which would be a fabrication (§9.3). `previous` adds the
    block-over-block delta, which belongs to plan generation only (§4.1/§9.2).

    `notes` off leaves the measurement caveats to the caller: three blocks in a row would
    otherwise repeat them three times, nine lines saying two things (§9.6). It defaults on
    for the prompt paths, which send one block each.

    The block it is given is the block it reports: no fallback to a future or first
    mesocycle, so a not-yet-started block renders nothing rather than an empty table (§8).
    """
    start, end = meso["start_date"], meso["end_date"]
    days = counted_days(start, end, as_of)
    if days <= 0:
        return None

    inner = indent + "  "
    # Prose, so it wraps: at width=48 the header runs 57 characters and would break the
    # column contract the zone rows below it keep (§9.6).
    lines = _wrap(format_header(meso, as_of, with_focus), indent, width)
    through = min(as_of, end)
    elapsed = fetch_activities(start, through)
    # Volume and load beside the distribution: load = volume x intensity, and this
    # feature exists because the third alone cannot recover the first two (§2).
    if not elapsed:
        lines.extend(_wrap(
            f"No completed activities recorded in {start}..{through}.", inner, width
        ))
        return "\n".join(lines)
    lines.extend(_wrap(
        f"Volume and load ({start}..{through}): {len(elapsed)} "
        f"session{'s' if len(elapsed) != 1 else ''}, "
        f"{fmt_duration(sum(float(a.get('duration_sec') or 0.0) for a in elapsed))}, "
        f"{sum(activity_load(a) for a in elapsed):.0f} TSS",
        inner, width,
    ))
    window = rate_window(start, end, as_of)

    if window:
        win_start, win_end, weeks = window
        rows = zone_rows(fetch_activities(win_start, win_end))
        lines.extend(_wrap(
            f"Intensity distribution, per week over {weeks} completed "
            f"week{'s' if weeks != 1 else ''} ({win_start}..{win_end})", inner, width
        ))
    else:
        weeks = 0
        rows = zone_rows(fetch_activities(start, through))
        lines.extend(_wrap(
            f"Intensity distribution, RAW minutes ({start}..{through}) — the block is "
            f"{days} day{'s' if days != 1 else ''} old, too young for a per-week rate",
            inner, width
        ))

    if not rows:
        lines.append(f"{inner}  No zone data recorded in this window.")
    else:
        lines.extend(format_table(rows, divisor=weeks or 1, indent=inner + "  ", width=width))
        lines.extend(format_coverage(rows, indent=inner + "  ", width=width))
        if notes:
            lines.extend(format_notes(rows, indent=inner + "  ", width=width))

    if previous and weeks:
        # A finished block's own last day is over, so measure it one day past its end —
        # counted_days counts days that are OVER, not days that exist.
        prev_window = rate_window(
            previous["start_date"], previous["end_date"],
            _s(_d(previous["end_date"]) + timedelta(days=1)),
        )
        if prev_window:
            p_start, p_end, p_weeks = prev_window
            prev_rows = zone_rows(fetch_activities(p_start, p_end))
            delta = format_delta_table(
                rows, weeks, prev_rows, p_weeks, previous["name"],
                indent=inner + "  ", width=width,
            )
            if delta:
                lines.extend(_wrap(
                    f"Change vs {previous['name']} "
                    f"({p_weeks} completed week{'s' if p_weeks != 1 else ''}), per week",
                    inner, width,
                ))
                lines.extend(delta)

    # With no completed week the raw table above already IS the current week — same
    # window, same numbers — so printing it twice says nothing new.
    if current_week and weeks:
        lines.extend(
            _current_week_lines(start, end, as_of, fetch_activities, inner, width)
        )

    structural = format_structural(
        elapsed, benchmarks, start, through, indent=inner + "  ", width=width,
    )
    if structural:
        lines.extend(_wrap(f"Structural work ({start}..{through})", inner, width))
        lines.extend(structural)

    return "\n".join(lines)


def _current_week_lines(
    start: str, end: str, as_of: str, fetch_activities: FetchActivities,
    inner: str, width: int,
) -> List[str]:
    """The in-progress week: raw minutes and how much of the week has gone (§9.3).

    Two days in and already over the week's whole Z3 allowance is correctable NOW;
    the block-to-date average would take another fortnight to show it."""
    win = current_week_window(start, end, as_of)
    if win is None:
        return []
    week_start, week_end, day = win
    rows = zone_rows(fetch_activities(week_start, week_end))
    lines = _wrap(
        f"Current week so far ({week_start}..{week_end}) — day {day} of 7 "
        f"({day / 7 * 100:.0f}% elapsed). RAW minutes, NOT extrapolated: read them "
        f"against that fraction yourself.", inner, width
    )
    if not rows:
        lines.append(f"{inner}  Nothing recorded yet this week.")
        return lines
    lines.extend(format_table(rows, with_pct=False, indent=inner + "  ", width=width))
    return lines
