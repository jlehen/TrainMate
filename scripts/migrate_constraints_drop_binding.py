#!/usr/bin/env python3
"""One-off migration: backfill `constraints_hash` after the rev-6 constraint
simplification (DESIGN_constraints.md rev 6).

The schema change itself — add a `rest` flag, drop `binding`/`sport`/`type` — is pure,
idempotent DDL and runs automatically in `db/base.py::_init_db`. This script handles the
one thing that can't: the plan staleness fingerprint.

`constraints_hash` is computed over the cleaned `replan = 1` constraints, and the clean
shape lost its `binding`/`sport`/`type` keys in rev 6. So a plan built around a
plan-shaping constraint now hashes differently than when it was stored, and the next
`plan generate` would offer a one-time spurious "inputs changed — replan?" prompt. This
recomputes the hash the exact way the next `plan generate` will
(get_constraints(today, end=None) -> filter replan=1 -> clean -> hash) and overwrites it
on every active macrocycle, so migrating alone never trips that proposal (§7).

Run once, by hand:

    venv/bin/python scripts/migrate_constraints_drop_binding.py --yes

Idempotent: recomputing and overwriting with the same value is harmless, and a plan with
no plan-shaping constraints hashes an empty list either way, so this is a no-op for it.
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

    if not args.yes:
        ans = input(
            "This backfills constraints_hash on every active macrocycle so the rev-6 "
            "constraint simplification does not spuriously invalidate existing plans. "
            "Continue? [y/N]: "
        ).strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

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
    print("Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
