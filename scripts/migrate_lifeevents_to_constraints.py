#!/usr/bin/env python3
"""One-off migration: copy `lifeevents` rows into the new `constraints` table
(DESIGN_constraints.md §9/§10).

This is NOT part of `_init_db` — unlike every other schema change in `db/base.py`, a row
copy between two tables that both keep existing afterward has no natural idempotency
guard, so it must be run explicitly, once, by hand:

    venv/bin/python scripts/migrate_lifeevents_to_constraints.py --yes

Each `lifeevents` row becomes one `constraints` row, verbatim (§9):
  - start_date, end_date, title copied as-is
  - event_type   -> type (opaque label now, no enum)
  - impact_description -> description
  - binding = 'soft'   (a life event was never code-enforced; see design §9)
  - replan  = 1        (life events were always plan-shaping)
  - source  = 'lifeevent'

It then backfills `constraints_hash` on every active macrocycle, recomputed exactly the
way the next `plan generate` will (get_constraints(today, end=None) -> filter replan=1 ->
clean -> hash), so the migration does not spuriously invalidate existing plans (§7/§9).

The `lifeevents` table is left intact (read-only) — it is only dropped in a later release,
once the `lifeevent` forwarder is removed (§10 step 8).
"""
import argparse
import json
import sys

from trainmate.db import db
from trainmate.coach.engine import CoachEngine
from trainmate.util import today_str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the confirmation prompt (required for non-interactive/bot use)"
    )
    args = parser.parse_args()

    lifeevents = db.get_lifeevents()
    print(f"Found {len(lifeevents)} lifeevents row(s) to migrate.")

    if not args.yes:
        ans = input(
            "This will copy every lifeevents row into constraints (as soft/plan-shaping) "
            "and backfill constraints_hash on active macrocycles. Continue? [y/N]: "
        ).strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    migrated = 0
    for ev in lifeevents:
        db.add_constraint(
            title=ev["title"],
            start_date=ev["start_date"],
            end_date=ev["end_date"],
            binding="soft",
            sport=None,
            type=ev.get("event_type"),
            description=ev.get("impact_description"),
            replan=1,
            source="lifeevent",
        )
        migrated += 1
    print(f"Migrated {migrated} row(s) into constraints.")

    # Backfill constraints_hash on every active macrocycle so this migration alone never
    # trips the reuse-vs-regen "inputs changed" proposal (§7). The hash is global (not
    # scoped by objective — an accepted limitation, see design §7), so every active
    # macrocycle receives the same recomputed value.
    engine = CoachEngine()
    today = today_str()
    replan_constraints = [c for c in db.get_constraints(today) if c.get("replan")]
    cleaned = engine._clean_constraints(replan_constraints)
    new_hash = engine._get_constraints_hash(replan_constraints)
    print(
        f"Recomputed constraints_hash over {len(replan_constraints)} plan-shaping "
        f"constraint(s): {new_hash}"
    )
    print(f"  (cleaned snapshot preview: {json.dumps(cleaned)[:200]})")

    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM macrocycles WHERE COALESCE(status, 'active') = 'active'"
        )
        active_ids = [row["id"] for row in cursor.fetchall()]
        for macro_id in active_ids:
            cursor.execute(
                "UPDATE macrocycles SET constraints_hash = ? WHERE id = ?",
                (new_hash, macro_id)
            )
        conn.commit()
    print(f"Backfilled constraints_hash on {len(active_ids)} active macrocycle(s).")

    print("Migration complete. The lifeevents table is left intact (read-only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
