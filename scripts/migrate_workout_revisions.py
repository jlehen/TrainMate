#!/usr/bin/env python3
"""One-off migration: rebuild `workouts` as an append-only revision log.

Implements DESIGN_workout_revisions.md §13. Run once, by hand, against the live database:

    venv/bin/python scripts/migrate_workout_revisions.py --yes

This is a table REBUILD, not `ALTER TABLE` surgery, for two reasons: SQLite cannot
`ADD COLUMN ... NOT NULL` without a default, and step 4 has to control physical insertion
order. The script builds `workouts_new` alongside the old table, fills it, then swaps
names and seals the result with the immutability triggers.

It opens the database with plain sqlite3 rather than through `trainmate.db`, because
constructing a `Database` runs `_init_db`, which would try to create the live view and the
triggers against the pre-migration column set. The script stamps the schema version itself
when it is done, so the next start finds the database current.

Idempotent in the only sense that matters: it refuses to run against a database that has
already been migrated.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trainmate.config import config
from trainmate.db.base import SCHEMA_VERSION
from trainmate.sports import canonical_sport

# The two deterministic prefixes the old writers stamped onto `modification_reason`. They
# live here rather than in the app because this is their last job: the heuristic they fed
# is deleted, and its final act is to seed the data that replaces it (§13 step 3).
SWAP_REASON_PREFIX = "Swapped from "
MANUAL_REPLACE_REASON_PREFIX = "Manually replaced previous "

# What the old `modification_state.modification_status()` answered -> the change kind that
# says the same thing (§13 step 3).
_STATUS_KINDS = {
    "unmodified": "generate",
    "adapted": "adapt",
    "swapped": "swap",
    "replaced": "add",
}


def classify(row) -> str:
    """The change kind a pre-migration row belongs under.

    The old classifier, inlined, plus the one override §13 step 3 names: a row with
    `source = 'manual'` classifies as `add` whatever the heuristic says, because a manual
    session added onto an empty day carries no `modification_reason` and would otherwise
    root its lineage in a `generate` — which §5 would then derive as 'generated', erasing
    its athlete-added standing.
    """
    if row["source"] == "manual":
        return "add"
    reason = row["modification_reason"]
    if not reason:
        return _STATUS_KINDS["unmodified"]
    if row["adaptation_summary"]:
        return _STATUS_KINDS["adapted"]
    original_date = row["original_date"] or row["date"]
    if reason.startswith(SWAP_REASON_PREFIX) or row["date"] != original_date:
        return _STATUS_KINDS["swapped"]
    if reason.startswith(MANUAL_REPLACE_REASON_PREFIX):
        return _STATUS_KINDS["replaced"]
    return _STATUS_KINDS["adapted"]  # legacy adapt: rationale in modification_reason


NEW_TABLE = """
CREATE TABLE workouts_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id INTEGER NOT NULL,
    lineage_id INTEGER,
    date TEXT NOT NULL,
    sport_canonical TEXT NOT NULL,
    sport_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    duration_minutes INTEGER,
    rpe INTEGER,
    tss INTEGER,
    void INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    restored_from INTEGER,
    macrocycle_id INTEGER,
    created_at TEXT,
    benchmark_type TEXT,
    planned_zone_currency TEXT,
    planned_zone1_sec INTEGER,
    planned_zone2_sec INTEGER,
    planned_zone3_sec INTEGER,
    planned_zone4_sec INTEGER,
    planned_zone5_sec INTEGER,
    planned_zone6_sec INTEGER,
    planned_zone7_sec INTEGER
)
"""

_ZONES = tuple(f"planned_zone{i}_sec" for i in range(1, 8))

INSERT_COLUMNS = (
    "change_id", "date", "sport_canonical", "sport_type", "title", "description",
    "duration_minutes", "rpe", "tss", "void", "reason", "macrocycle_id", "created_at",
    "benchmark_type", "planned_zone_currency",
) + _ZONES


def back_up(db_path: str) -> str:
    """Step 0: copy the database file and verify the copy opens, before anything else."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{db_path}.pre-revisions-{stamp}"
    shutil.copy2(db_path, backup)
    probe = sqlite3.connect(backup)
    try:
        probe.execute("SELECT COUNT(*) FROM workouts").fetchone()
    finally:
        probe.close()
    return backup


def already_migrated(conn) -> bool:
    columns = {r[1] for r in conn.execute("PRAGMA table_info(workouts)")}
    return "change_id" in columns


def slot_rows(conn):
    """Every old row, grouped into slots and put in the order §13 step 4 requires.

    Archived rows first, in `(created_at, id)` order, and the live row LAST. That is
    load-bearing rather than cosmetic: the live view's rule is "highest id wins", and
    today's data violates it — `workout rollback` revived old rows in place, so any slot
    ever rolled back holds a live row with a LOWER id than its archived siblings.
    """
    slots = {}
    for row in conn.execute("SELECT * FROM workouts"):
        key = (row["date"], canonical_sport(row["sport_type"]))
        slots.setdefault(key, []).append(row)
    ordered = {}
    for key, rows in slots.items():
        archived = sorted(
            (r for r in rows if r["archived_at"] is not None),
            key=lambda r: (r["created_at"] or r["date"], r["id"]),
        )
        live = sorted((r for r in rows if r["archived_at"] is None), key=lambda r: r["id"])
        ordered[key] = archived + live
    return ordered


def plan_emissions(slots):
    """What to write, as `(slot_key, [(change_key, row, kind_of_emission)])`.

    A row that was adapted in place carries no archived intermediate — adapt edited it —
    so its `original_*` columns are the only record that it was ever walked down. Step 2
    rebuilds that as a real pre-revision, or dropping those columns would make the DO NOT
    COMPOUND guard forget every currently-eased session on migration day.

    A slot whose rows were ALL archived gets a closing **void**. Under the old model an
    empty slot was expressed by absence — every row archived and nothing put back — but
    the live view's rule is "newest revision wins", so carrying those rows over unchanged
    would make the newest one live and put a session back on a day the plan had emptied.
    The void is what says out loud what archival used to leave implicit; it is exactly
    what a modern `generate` writes for a day it no longer fills (§11).
    """
    changes = {}          # key -> {created_at, kind, summary, macrocycle_id}
    emissions = {}        # slot key -> [(change key, row, emission)]

    def remember(key, created_at, kind, summary, macrocycle_id):
        changes.setdefault(key, {
            "created_at": created_at, "kind": kind,
            "summary": summary, "macrocycle_id": macrocycle_id,
        })

    for slot, rows in slots.items():
        emitted = []
        for row in rows:
            born = row["created_at"] or row["date"]
            if (row["adaptation_count"] or 0) > 0:
                origin_kind = "add" if row["source"] == "manual" else "generate"
                origin_key = ("origin", row["id"])
                remember(origin_key, born, origin_kind, None, row["macrocycle_id"])
                emitted.append((origin_key, row, ORIGINAL))
                adapt_key = ("adapt", row["id"])
                remember(adapt_key, row["adapted_at"] or born, "adapt",
                         row["adaptation_summary"], row["macrocycle_id"])
                emitted.append((adapt_key, row, REVISION))
                continue
            kind = classify(row)
            key = ("group", row["archived_at"], kind)
            remember(key, row["archived_at"] or born, kind,
                     row["adaptation_summary"], row["macrocycle_id"])
            emitted.append((key, row, REVISION))
        if all(row["archived_at"] is not None for row in rows):
            last = rows[-1]
            key = ("emptied", last["archived_at"])
            remember(key, last["archived_at"], "generate", None, last["macrocycle_id"])
            emitted.append((key, last, VOID))
        emissions[slot] = emitted
    return changes, emissions


# What one emission is: the row as it stood, the form it was FIRST prescribed with, or a
# void closing a slot the plan emptied.
REVISION, ORIGINAL, VOID = "revision", "original", "void"


def revision_values(row, change_id, emission):
    """One old row as a revision. `ORIGINAL` rebuilds the form it was first prescribed
    with, from the `original_*` columns (§13 step 2); `VOID` says the slot is empty."""
    synthetic_original = emission == ORIGINAL

    def pick(original, current):
        if not synthetic_original:
            return row[current]
        return row[original] if row[original] is not None else row[current]

    removed = emission == VOID or (bool(row["removed"]) and not synthetic_original)
    reason = row["removed_reason"] if removed else row["modification_reason"]
    if emission == VOID:
        reason = None
    return (
        change_id,
        row["date"],
        canonical_sport(row["sport_type"]),
        row["sport_type"],
        row["title"],
        pick("original_description", "description"),
        pick("original_duration_minutes", "duration_minutes"),
        pick("original_rpe", "rpe"),
        pick("original_tss", "tss"),
        1 if removed else 0,
        None if synthetic_original else reason,
        row["macrocycle_id"],
        row["created_at"],
        row["benchmark_type"],
        row["planned_zone_currency"],
    ) + tuple(row[z] for z in _ZONES)


def migrate(conn) -> dict:
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS workouts_new")
    cursor.execute(NEW_TABLE)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workout_changes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT    NOT NULL,
            kind          TEXT    NOT NULL,
            summary       TEXT,
            macrocycle_id INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workout_calendar_state (
            lineage_id                 INTEGER PRIMARY KEY,
            google_event_id            TEXT,
            pushed_signature           TEXT,
            adherence_pushed_signature TEXT
        )
    """)
    cursor.execute("DELETE FROM workout_changes")
    cursor.execute("DELETE FROM workout_calendar_state")

    slots = slot_rows(conn)
    changes, emissions = plan_emissions(slots)

    # Changes go in oldest first, so their ids run with the clock: the undo primitive
    # compares change ids, and `workout batches` lists them newest first.
    change_ids = {}
    for key in sorted(changes, key=lambda k: (changes[k]["created_at"], str(k))):
        spec = changes[key]
        cursor.execute(
            "INSERT INTO workout_changes (created_at, kind, summary, macrocycle_id) "
            "VALUES (?, ?, ?, ?)",
            (spec["created_at"], spec["kind"], spec["summary"], spec["macrocycle_id"]),
        )
        change_ids[key] = int(cursor.lastrowid)

    placeholders = ", ".join("?" * len(INSERT_COLUMNS))
    old_to_lineage = {}
    lineage_calendar = {}
    revisions = 0
    for slot in sorted(emissions):
        lineage = None
        for change_key, row, emission in emissions[slot]:
            cursor.execute(
                f"INSERT INTO workouts_new ({', '.join(INSERT_COLUMNS)}) "
                f"VALUES ({placeholders})",
                revision_values(row, change_ids[change_key], emission),
            )
            new_id = int(cursor.lastrowid)
            revisions += 1
            # Step 5: within a slot, every revision joins the earliest one's lineage. Exact
            # for the common case — a slot's rows ARE one session's revisions — and wrong
            # wherever a pre-migration swap moved a session between slots, which §16
            # accepts: pre-migration history is slot history.
            if lineage is None:
                lineage = new_id
            cursor.execute(
                "UPDATE workouts_new SET lineage_id = ? WHERE id = ?", (lineage, new_id)
            )
            if emission == REVISION:
                old_to_lineage[row["id"]] = lineage
            if row["archived_at"] is None and emission == REVISION:
                # Archival cleared the event handles, so the live row holds the only
                # Calendar state worth carrying (§13 step 6).
                lineage_calendar[lineage] = (
                    row["google_event_id"], row["pushed_signature"],
                    row["marked_signature"],
                )

    for lineage, (event_id, pushed, marked) in lineage_calendar.items():
        if not any((event_id, pushed, marked)):
            continue
        cursor.execute(
            "INSERT INTO workout_calendar_state "
            "(lineage_id, google_event_id, pushed_signature, adherence_pushed_signature) "
            "VALUES (?, ?, ?, ?)",
            (lineage, event_id, pushed, marked),
        )

    # Tidying, not a repair (§15): a benchmark result is recorded after the session is
    # done, so the revision it names is never superseded in normal use — but once lineages
    # exist, naming one is free and strictly more correct.
    repointed = 0
    for result in conn.execute(
        "SELECT id, workout_id FROM benchmark_results WHERE workout_id IS NOT NULL"
    ).fetchall():
        lineage = old_to_lineage.get(result["workout_id"])
        if lineage is None or lineage == result["workout_id"]:
            continue
        cursor.execute(
            "UPDATE benchmark_results SET workout_id = ? WHERE id = ?",
            (lineage, result["id"]),
        )
        repointed += 1

    # Step 7: swap and seal. The view after the rebuild so its `SELECT w.*` binds the final
    # column set, the triggers last so the migration is not blocked by them.
    cursor.execute("DROP VIEW IF EXISTS live_workouts")
    cursor.execute("DROP TABLE workouts")
    cursor.execute("ALTER TABLE workouts_new RENAME TO workouts")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_workouts_slot "
        "ON workouts(date, sport_canonical, id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_workouts_lineage ON workouts(lineage_id, id)"
    )
    cursor.execute("""
        CREATE VIEW live_workouts AS
        SELECT w.* FROM workouts w
        WHERE w.id = (
            SELECT MAX(w2.id) FROM workouts w2
            WHERE w2.date = w.date AND w2.sport_canonical = w.sport_canonical
        )
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS workouts_no_update BEFORE UPDATE ON workouts
        WHEN NOT (OLD.lineage_id IS NULL AND NEW.lineage_id = NEW.id)
        BEGIN SELECT RAISE(ABORT, 'workouts is append-only: append a revision'); END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS workouts_no_delete BEFORE DELETE ON workouts
        BEGIN SELECT RAISE(ABORT, 'workouts is append-only: append a void revision'); END
    """)
    cursor.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
    )
    return {
        "slots": len(emissions),
        "changes": len(change_ids),
        "revisions": revisions,
        "lineages": len(set(old_to_lineage.values())),
        "calendar_rows": len(lineage_calendar),
        "benchmarks_repointed": repointed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt")
    parser.add_argument("--db", default=None, help="Database file (default: config)")
    args = parser.parse_args()

    db_path = args.db or config.db_path
    if not os.path.exists(db_path):
        print(f"No database at {db_path}.")
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if already_migrated(conn):
            print("Already migrated: `workouts` carries `change_id`. Nothing to do.")
            return 0
        rows = conn.execute("SELECT COUNT(*) AS n FROM workouts").fetchone()["n"]
    finally:
        conn.close()

    if not args.yes:
        answer = input(
            f"Rebuild {rows} workout row(s) in {db_path} as an append-only revision "
            "log? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1

    backup = back_up(db_path)
    print(f"Backed up to {backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            summary = migrate(conn)
    except Exception:
        conn.close()
        shutil.copy2(backup, db_path)
        print(f"Migration failed; {db_path} restored from {backup}.")
        raise
    finally:
        conn.close()

    print(
        f"Migrated {rows} row(s) into {summary['revisions']} revision(s) across "
        f"{summary['lineages']} lineage(s) in {summary['slots']} slot(s), under "
        f"{summary['changes']} change(s)."
    )
    print(
        f"Calendar state moved for {summary['calendar_rows']} session(s); "
        f"{summary['benchmarks_repointed']} benchmark result(s) re-pointed at a lineage."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
