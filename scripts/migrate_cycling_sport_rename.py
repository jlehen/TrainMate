#!/usr/bin/env python3
"""One-off migration: normalize stored sport names to the canonical vocabulary, which
now spells cycling `cycling` (DESIGN_intensity_distribution.md §6.1).

`road_biking` stopped being the canonical cycling name: `indoor_cycling` and
`virtual_ride` were already aliases of it, so a trainer session read as "road biking",
and `gravel_cycling` / `cyclocross` fell through `canonical_sport` into rows of their
own. The canonical name is now `cycling`, with all of those as aliases.

Nothing *breaks* without this script — every read path matches alias-aware, so a stored
`road_biking` row still resolves. But the plan, the goal and the benchmark logbook would
keep printing "road_biking" while every intensity report says "cycling", and `goal edit`
no longer accepts `road_biking` as a choice. This aligns what is on screen.

Covers the four columns that hold a sport the app chose to write:

    workouts.sport_type          objectives.sport_type      (comma-separated)
    benchmark_results.sport_type coach_learnings.sports     (comma-separated)

`completed_activities.activity_type` is deliberately NOT touched: it keeps Garmin's raw
string and is canonicalized on read (§6.1, "canonicalize on read, not on write").
Overwriting `gravel_cycling` there would be a lossy write undoable only by a re-pull.

Run once, by hand:

    venv/bin/python scripts/migrate_cycling_sport_rename.py --yes

Idempotent: canonical values map to themselves, so a second run changes nothing.
"""
import argparse
import os
import sys

# Run as `venv/bin/python scripts/<this>` from the repo root: sys.path[0] is scripts/,
# so the package root has to be put on the path explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trainmate.db import db
from trainmate.sports import canonical_sport

# (table, column, is the column a comma-separated list?)
TARGETS = [
    ("workouts", "sport_type", False),
    ("objectives", "sport_type", True),
    ("benchmark_results", "sport_type", False),
    ("coach_learnings", "sports", True),
]


def normalize(value: str, multi: bool) -> str:
    """`value` with every sport token replaced by its canonical name, order preserved
    and duplicates dropped — folding `road_biking,indoor_cycling` to a single `cycling`
    rather than leaving the same sport listed twice."""
    if not value:
        return value
    if not multi:
        return canonical_sport(value)
    seen, out = set(), []
    for token in value.split(","):
        canon = canonical_sport(token.strip())
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append(canon)
    return ",".join(out)


def collect():
    """Every row whose stored value is not already canonical, as
    (table, column, rowid, old, new)."""
    pending = []
    with db._get_connection() as conn:
        for table, column, multi in TARGETS:
            for row in conn.execute(f"SELECT id, {column} AS v FROM {table}"):
                old = row["v"]
                new = normalize(old, multi)
                if new != old:
                    pending.append((table, column, row["id"], old, new))
    return pending


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the confirmation prompt (required for non-interactive/bot use)"
    )
    args = parser.parse_args()

    pending = collect()
    if not pending:
        print("Every stored sport name is already canonical. Nothing to do.")
        return 0

    print(f"{len(pending)} row(s) to normalize:")
    for table, column, rowid, old, new in pending:
        print(f"  {table}.{column} #{rowid}: {old!r} -> {new!r}")

    if not args.yes:
        ans = input("Apply? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    with db._get_connection() as conn:
        cursor = conn.cursor()
        for table, column, rowid, _old, new in pending:
            cursor.execute(
                f"UPDATE {table} SET {column} = ? WHERE id = ?", (new, rowid)
            )
        conn.commit()
    print(f"Normalized {len(pending)} row(s). Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
