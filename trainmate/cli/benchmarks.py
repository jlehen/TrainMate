import argparse
import sys
from typing import Optional, Tuple

import trainmate_cli as cli
from trainmate.util import (
    bold, dim, green, red, yellow, cyan, blue, gray, cmd,
    format_labeled_block, today_str as _today_str,
)
from trainmate.cli.common import fmt_date
from trainmate.sports import canonical_sport
from trainmate.benchmarks import (
    ANCHOR_KINDS, LOGBOOK_KINDS, anchors_for_sport,
    unit_for_kind, format_value, format_delta, is_improvement,
)

# The `--<flag>` each anchor kind is recorded under. dest replaces the dash so
# `--threshold-pace` lands on args.threshold_pace.
_KIND_FLAGS = {kind: "--" + kind.replace("_", "-") for kind in LOGBOOK_KINDS}


def _parse_value(kind: str, raw: str) -> float:
    """Parses a CLI value string for `kind` into the stored float. Pace kinds accept
    either ``M:SS`` (4:30) or a decimal in the unit's base; other kinds are plain
    numbers. Raises ValueError on garbage so the handler can report it."""
    unit = unit_for_kind(kind)
    raw = raw.strip()
    if unit in ("min/km", "sec/100m") and ":" in raw:
        mm, ss = raw.split(":", 1)
        total_seconds = int(mm) * 60 + float(ss)
        return total_seconds / 60.0 if unit == "min/km" else total_seconds
    return float(raw)


def _selected_kind(args: argparse.Namespace) -> Tuple[Optional[str], Optional[str]]:
    """Returns (kind, raw_value) for the single value flag the athlete passed, or
    (None, None) if none was given. Errors out if more than one was given."""
    present = [
        (kind, getattr(args, kind))
        for kind in LOGBOOK_KINDS
        if getattr(args, kind, None) is not None
    ]
    if len(present) > 1:
        flags = ", ".join(_KIND_FLAGS[k] for k, _ in present)
        print(red(f"Give exactly one anchor value per record — got {flags}."))
        sys.exit(1)
    if not present:
        return None, None
    return present[0]


def _benchmark_line(r: dict, prev_value: Optional[float]) -> str:
    """One-line rendering of a logbook row for `list` (and the `record` echo).

    `prev_value` is the next-older value of the same kind, which the signed delta is
    measured against; None when this row is the first of its kind."""
    kind = r["anchor_kind"]
    anchor = ANCHOR_KINDS.get(kind)
    label = anchor.label if anchor else kind
    value = float(r["value"])
    delta_disp = ""
    if prev_value is not None:
        delta = format_delta(kind, value, prev_value)
        if delta:
            better = is_improvement(kind, value, prev_value)
            delta_disp = " " + (green if better else red)(f"({delta})")
    src = r.get("source") or "test"
    src_disp = "" if src == "test" else gray(f" [{src}]")
    return (
        f"ID: {r['id']} | {cyan(fmt_date(r['date']))} | "
        f"{r['sport_type'].upper()} | {bold(label)}: {format_value(kind, value)}"
        f"{delta_disp}{src_disp}"
    )


def run_benchmark_record(args: argparse.Namespace) -> None:
    """Records a benchmark result — propose→confirm, never silent (§5.1)."""
    kind, raw = _selected_kind(args)
    if kind is None:
        flags = ", ".join(_KIND_FLAGS[k] for k in LOGBOOK_KINDS)
        print(red(f"Give an anchor value, one of: {flags}."))
        sys.exit(1)
    try:
        value = _parse_value(kind, str(raw))
    except (ValueError, IndexError):
        print(red(f"Could not parse {_KIND_FLAGS[kind]} value {raw!r}."))
        sys.exit(1)

    sport = canonical_sport(args.sport)
    date = args.date or _today_str()
    unit = unit_for_kind(kind)
    label = ANCHOR_KINDS[kind].label

    # Show the change against the current latest of this kind before touching anything.
    prev = cli.db.get_latest_benchmark(kind)
    new_disp = format_value(kind, value)
    if prev:
        old_val = float(prev["value"])
        delta = format_delta(kind, value, old_val)
        change = (
            f" (was {format_value(kind, old_val)}"
            + (f", {delta}" if delta else "")
            + ")"
        )
    else:
        change = " (first on record)"

    if not args.yes:
        crosses = _crosses_replan_band(kind, value, prev)
        tail = (
            " — record and suggest replanning the next block?"
            if crosses else " — record?"
        )
        if not cli.prompt.confirm(f"New {label} {new_disp}{change}{tail}"):
            print(gray("Not recorded."))
            return

    rid = cli.db.add_benchmark_result(
        date=date, sport_type=sport, anchor_kind=kind, value=value,
        unit=unit, source=args.source, note=args.note,
    )
    row = cli.db.get_benchmark_result(rid)
    if row:
        # `prev` is this kind's previous latest — exactly the predecessor `list` would
        # measure the new row's delta against.
        print(_benchmark_line(row, float(prev["value"]) if prev else None))
    print(green("Benchmark result recorded successfully."))
    if _crosses_replan_band(kind, value, prev):
        print(yellow(
            "This moves your effective threshold past the replan band — run "
            + cmd("plan generate")
            + " to rebuild the next block against the fresh number."
        ))


def _crosses_replan_band(kind: str, value: float, prev: Optional[dict]) -> bool:
    """Whether recording `value` moves this anchor more than the configured replan
    tolerance from the previous latest — the same >5% band config_changed() uses (§3.3).
    A first-ever value doesn't 'cross' (there is nothing to drift from)."""
    if not prev:
        return False
    old_val = float(prev["value"])
    if not old_val:
        return False
    tolerance = cli.config.threshold_replan_pct / 100.0
    return abs(value - old_val) / abs(old_val) > tolerance


def run_benchmark_list(args: argparse.Namespace) -> None:
    """Lists the benchmark logbook, newest first, with per-kind signed deltas (§3.2/§6)."""
    rows = cli.db.get_benchmark_results()
    print(bold(cyan("=== BENCHMARK LOGBOOK ===")))
    if not rows:
        print(dim("(no results recorded yet)"))
        print(gray(
            "Record one with e.g. "
            "'benchmark record cycling --ftp 250'."
        ))
        return

    # For a per-row delta, compare each result to the next-older result OF THE SAME KIND.
    # Rows arrive newest-first; walk each kind's series to find the predecessor.
    by_kind: dict = {}
    for r in rows:
        by_kind.setdefault(r["anchor_kind"], []).append(r)  # already newest-first

    for r in rows:
        series = by_kind[r["anchor_kind"]]
        idx = series.index(r)
        prev_value = (
            float(series[idx + 1]["value"]) if idx + 1 < len(series) else None
        )
        print(_benchmark_line(r, prev_value))
        if r.get("note"):
            print(format_labeled_block("  Note:", r["note"]))


def run_benchmark_rm(args: argparse.Namespace) -> None:
    """Removes a logbook row by ID — the correction path (delete-and-re-record, §6)."""
    row = cli.db.get_benchmark_result(args.id)
    if not row:
        print(red(f"Benchmark result with ID {args.id} not found."))
        sys.exit(1)
    cli.db.delete_benchmark_result(args.id)
    print(green(f"Removed benchmark result ID {args.id}."))


def run_benchmark_wipe(args: argparse.Namespace) -> None:
    """Wipes the entire benchmark logbook after confirmation."""
    if not args.yes:
        if not cli.prompt.confirm(
            "Are you sure you want to wipe all benchmark results?", danger=True
        ):
            print("Wipe cancelled.")
            return
    cli.db.wipe_benchmarks()
    print(green("All benchmark results wiped successfully."))


def add_benchmark_parser(subparsers):
    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="Record and review fitness-test results (the threshold logbook)",
    )
    b_subparsers = benchmark_parser.add_subparsers(
        dest="subcommand", help="Benchmark sub-commands"
    )

    # benchmark record
    b_rec = b_subparsers.add_parser(
        "record",
        help="Record a fitness-test result (e.g. 'record cycling --ftp 250')",
        description=(
            "Record a fitness-test result in the dated logbook — the source the coach "
            "prescribes zones and targets from.\n\n"
            "IMPORTANT: turn OFF Garmin Connect's automatic FTP and lactate-threshold "
            "detection (they are two independent settings). TrainMate treats the values "
            "you record here as authoritative, but Garmin buckets every activity into "
            "zones using ITS OWN thresholds at the time. An auto-detected bump silently "
            "moves the zone boundaries in your activity history, so the same effort lands "
            "one zone lower and a training block looks easier than it was.\n\n"
            "For --e1rm, track ONE lift for now: the logbook has no per-exercise field, so "
            "a deadlift PR logged after a squat PR reads as one e1RM value jumping 70%."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    b_rec.add_argument(
        "sport",
        help="Sport the test belongs to (e.g. cycling, running, swimming, strength)",
    )
    for kind in LOGBOOK_KINDS:
        anchor = ANCHOR_KINDS[kind]
        b_rec.add_argument(
            _KIND_FLAGS[kind], dest=kind,
            help=f"{anchor.label} value ({anchor.unit})",
        )
    b_rec.add_argument("--date", help="Test date (YYYY-MM-DD; default today)")
    b_rec.add_argument("--note", help="Protocol/conditions note")
    b_rec.add_argument(
        "--source", choices=["test", "manual", "modeled"], default="test",
        help="How the value arrived (default: test)",
    )
    b_rec.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    # benchmark list
    b_subparsers.add_parser(
        "list", help="Show the benchmark logbook, newest first"
    )

    # benchmark rm
    b_rm = b_subparsers.add_parser(
        "rm", help="Remove a benchmark result by ID"
    )
    b_rm.add_argument("id", type=int, help="Benchmark result ID to remove")

    # benchmark wipe
    b_wipe = b_subparsers.add_parser(
        "wipe", advanced=True, help="Wipe all benchmark results"
    )
    b_wipe.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )

    return benchmark_parser
