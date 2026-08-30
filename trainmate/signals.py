"""Shipped vocabulary for daily signals, and the one rule that keys on its spelling.

`metric` stays opaque free text everywhere else — nothing here makes the app interpret a
category (DESIGN_signal_authoring.md §1). What this module holds is a *suggested*
vocabulary rendered into the adapt prompt, so the LLM reuses one string per category
instead of coining a new one per occasion, and the exclusion table that decides which
response channel a category would otherwise correlate with itself.

The two live together on purpose: `SIGNAL_CHANNEL_EXCLUSIONS` matches by substring, so
renaming a category can silently switch its self-correlation guard off
(DESIGN_signal_extraction.md §4).

The vocabulary half is dependency-free, so the CLI, the prompt builder, config and the
analysis layer can all import it. `write_signal_days` reaches the calendar through
`runtime`, which has no module-level imports of its own, so that stays cycle-free too.
"""
import difflib
import textwrap
from datetime import datetime, timedelta
from typing import Dict, Iterator, List, Optional, Tuple

from trainmate import runtime

# Categories shipped with the app, each mapped to the gloss shown to the LLM. Config
# augments and overrides this (`coach.signal_metrics`); situational categories that do not
# apply to every athlete — altitude, menstrual_cycle, medication — are left to config
# rather than shipped here (DESIGN_signal_extraction.md §5).
#
# A category name is an exact-match clustering key, so its spelling is effectively
# permanent: renaming one splits its history into two categories that never cluster.
DEFAULT_SIGNAL_METRICS: Dict[str, str] = {
    "alcohol": (
        "alcohol drunk the evening before; value = number of standard drinks "
        "(1 drink = 10 g pure alcohol ~ 250 ml beer at 5%, 100 ml wine at 12%, "
        "30 ml spirits at 40%). Count the drinks, do not rate the hangover."
    ),
    "disturbed_sleep": (
        "a short or broken night, whatever the cause — trouble falling asleep, woken by "
        "a child/noise/pain, a deliberate late night; value = hours actually slept if "
        "known. Not for a night the watch already scored badly on its own."
    ),
    "illness": (
        "your own illness, or a vaccination reaction — not a family member's illness "
        "(that is disturbed_sleep or stress)."
    ),
    "heat": (
        "ambient heat you trained, worked or slept in; value = the day's peak air "
        "temperature in whole degrees Celsius (e.g. 34). Log it from about 25 C upward, "
        "when the heat is a load rather than the weather."
    ),
    "stress": "life stress outside training — work, family, money, a bad week.",
    "travel": "time-zone change, a long drive/flight, or nights in an unfamiliar bed.",
    "underfuelling": (
        "eating clearly less than the training asked for — a deliberate cut, a skipped "
        "day of meals, a long session done fasted."
    ),
}

# `metric` substring -> response channels dropped from that category's aligned rows, so a
# signal is never correlated against a reading that measures the same construct
# (DESIGN_quantitative_signal_impact.md §3.2). Substring, not equality: every sleep-ish
# spelling (disturbed_sleep, poor_sleep) has to trip it, which is why a sleep category
# whose name omits "sleep" would silently lose the guard (DESIGN_signal_extraction.md §4).
SIGNAL_CHANNEL_EXCLUSIONS: Tuple[Tuple[str, set], ...] = (
    ("sleep", {"sleep"}),
)


def excluded_channels(metric: str) -> set:
    """Response channels to drop for `metric`, per the substring table above."""
    metric_l = (metric or "").lower()
    excluded: set = set()
    for substring, channels in SIGNAL_CHANNEL_EXCLUSIONS:
        if substring not in metric_l:
            continue
        excluded |= channels
    return excluded


def normalize_metric(metric: str) -> str:
    """The one spelling rule for a category key: trimmed and lowercased.

    SQLite compares TEXT case-sensitively and `daily_signals.metric` has no COLLATE
    NOCASE, so `Heat` and `heat` would cluster as two categories
    (DESIGN_signal_extraction.md §3).
    """
    return str(metric or "").strip().lower()


def format_vocabulary(vocabulary: Dict[str, str], usage: List[dict]) -> str:
    """Renders the prompt's category list: name, how many days it already has, gloss
    (DESIGN_signal_extraction.md §5).

    `vocabulary` is config-merged name -> gloss; `usage` is `list_signal_metrics()` rows.
    Categories already logged but absent from the vocabulary are appended, so the list
    always shows what is actually in use. The day-counts are what teach the LLM that a
    category recurs rather than describing one occasion.
    """
    counts = {normalize_metric(row["metric"]): row for row in usage}
    names = list(vocabulary.keys())
    for name in sorted(counts.keys()):
        if name in vocabulary:
            continue
        names.append(name)

    lines = []
    for name in names:
        row = counts.get(name)
        seen = "not yet used"
        if row:
            days = row["count"]
            seen = f"{days} day{'s' if days != 1 else ''}, last {row['last_date']}"
        gloss = vocabulary.get(name) or "logged previously, no description configured"
        lines.append(textwrap.fill(
            f"- {name} ({seen}): {gloss}", width=100, subsequent_indent="  "
        ))
    return "\n".join(lines)


def known_metrics(vocabulary: Dict[str, str], usage: List[dict]) -> List[str]:
    """Every category the athlete might reasonably reuse: the config-merged vocabulary plus
    anything already logged (DESIGN_signal_extraction.md §5).

    Takes both inputs rather than reading them, so `config` (which imports this module) and
    the CLI can each call it without a cycle or a coach-service build.
    """
    known = set(vocabulary)
    known |= {normalize_metric(row["metric"]) for row in usage}
    return sorted(k for k in known if k)


def nearest_known(metric: str, known: List[str], cutoff: float = 0.6) -> Optional[str]:
    """The closest existing category to `metric`, or None when nothing is close.

    Advisory only: a near-miss changes what the athlete is ASKED, never what is stored, so
    `sleep_debt` can never be silently folded into `sleep` and inherit its channel
    exclusion (DESIGN_signal_extraction.md §6).

    The cutoff sits between the drift spellings worth catching (`heatwave` vs `heat`,
    0.667) and the closest pair of genuinely distinct shipped categories (`stress` vs
    `travel`, 0.500) — tests/test_signal_extraction.py pins both ends. A false offer costs
    one extra y/N; a miss costs a silently fragmented category, so it errs low.
    """
    others = [k for k in known if k != metric]
    matches = difflib.get_close_matches(metric, others, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def date_range(start: str, end: str) -> Iterator[str]:
    """Yields each YYYY-MM-DD from start to end inclusive."""
    day = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    while day <= last:
        yield day.strftime("%Y-%m-%d")
        day += timedelta(days=1)


def signal_summary(metric: str, value: Optional[float], label: str) -> str:
    """Builds the calendar entry / LLM blurb. A provided label gets the value appended in
    parentheses (`severe heatwave (38.0)`); with no label it's `Metric: value` (matching
    ingested events like `Alcohol: 2.0`) — DESIGN_signal_authoring.md §3."""
    if label:
        return f"{label} ({value})" if value is not None else label
    base = metric[:1].upper() + metric[1:]
    return f"{base}: {value}" if value is not None else base


def write_signal_days(
    start_date: str, end_date: str, metric: str, value: Optional[float], text: str
) -> List[dict]:
    """Writes one tagged all-day event per day in [start, end] and mirrors each row back.

    The calendar is the source of truth — `daily_signals.google_event_id` is NOT NULL — so
    a signal cannot be stored locally alone (DESIGN_signal_authoring.md §2). Upserts by
    (date, metric): a day already carrying this category is updated, not duplicated.
    Returns the rows written; days whose calendar write failed are simply absent.
    """
    written: List[dict] = []
    for day in date_range(start_date, end_date):
        existing = runtime.db.get_daily_signals(day, day, metric=metric)
        existing_id = existing[0]["google_event_id"] if existing else None
        event_id = runtime.calendar_syncer.add_signal_event(
            day, metric, value, text, existing_id
        )
        if not event_id:
            continue
        runtime.db.upsert_daily_signal_by_event(event_id, day, metric, value, text)
        written.extend(runtime.db.get_daily_signals(day, day, metric=metric))
    return written
