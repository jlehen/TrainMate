from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from trainmate.types import Macrocycle, Mesocycle


class PeriodizationMixin:
    """Macrocycles & mesocycles: persistence, lookups, and feedback."""

    def get_macrocycle_for_objective(self, objective_id: int) -> Optional[Macrocycle]:
        """Fetches the active macrocycle for a specific objective.

        Superseded plan versions (kept for `plan rollback`, see DESIGN_plan_rollback.md)
        are excluded — only the one currently-active macrocycle is returned. Legacy rows
        predating the version axis default to 'active'."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM macrocycles WHERE objective_id = ? "
                "AND COALESCE(status, 'active') = 'active' "
                "ORDER BY id DESC LIMIT 1",
                (objective_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_macrocycle_versions(self, objective_id: int) -> List[Macrocycle]:
        """Returns every macrocycle version for an objective, newest first.

        Includes the active version and all superseded ones, for rollback target
        selection and history display (see DESIGN_plan_rollback.md)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM macrocycles WHERE objective_id = ? ORDER BY id DESC",
                (objective_id,)
            )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_previous_macrocycle(
        self, objective_id: int, before_id: Optional[int] = None
    ) -> Optional[Macrocycle]:
        """Returns the macrocycle version chronologically prior to the active one.

        With `before_id` given, returns the newest version older than that id instead.
        Used by `plan rollback` to walk backwards through plan history one step at a
        time (see DESIGN_plan_rollback.md). Returns None when there is no earlier version."""
        if before_id is None:
            active = self.get_macrocycle_for_objective(objective_id)
            if not active:
                return None
            before_id = active['id']
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM macrocycles WHERE objective_id = ? AND id < ? "
                "ORDER BY id DESC LIMIT 1",
                (objective_id, before_id)
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def set_active_macrocycle(self, macrocycle_id: int) -> None:
        """Makes `macrocycle_id` the active version for its objective, superseding any
        other active version (see DESIGN_plan_rollback.md). Idempotent."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT objective_id FROM macrocycles WHERE id = ?", (macrocycle_id,)
            )
            row = cursor.fetchone()
            if not row:
                return
            objective_id = row['objective_id']
            # Supersede whichever version is currently active for this objective.
            cursor.execute(
                "UPDATE macrocycles SET status = 'superseded', superseded_at = ? "
                "WHERE objective_id = ? AND COALESCE(status, 'active') = 'active' "
                "AND id != ?",
                (now, objective_id, macrocycle_id)
            )
            # Promote the target.
            cursor.execute(
                "UPDATE macrocycles SET status = 'active', superseded_at = NULL "
                "WHERE id = ?",
                (macrocycle_id,)
            )
            conn.commit()

    def get_macrocycle(self, macrocycle_id: int) -> Optional[Macrocycle]:
        """Fetches a specific macrocycle by its unique ID, regardless of status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM macrocycles WHERE id = ?", (macrocycle_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_last_macrocycle(self) -> Optional[Macrocycle]:
        """Fetches the absolute latest macrocycle created."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM macrocycles ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_governing_macrocycle(self) -> Optional[Macrocycle]:
        """The macrocycle of the *governing objective* — the earliest active objective
        that has a plan (i.e. the objective the current workouts implement). Its
        mesocycles label the timeline weeks (DESIGN_progress_timeline.md §6.1).

        Deliberately distinct from `get_active_objective()` (earliest active,
        plan-or-not) and from Phase 2's priority-ordered event pick: meso labels must
        follow whichever plan the current workouts implement. Returns None when no
        active objective has a macrocycle yet."""
        for obj in self.get_objectives(status='active'):  # ORDER BY target_date ASC
            macro = self.get_macrocycle_for_objective(obj['id'])
            if macro:
                return macro
        return None

    def get_governance_versions(self) -> List[Dict[str, Any]]:
        """Every macrocycle *version* — all objectives, active AND superseded,
        completed objectives included — as
        ``{objective_id, created_at, ranges: [(start, end), ...]}`` where ``ranges``
        are the version's mesocycle spans.

        The raw material for the version-in-force governance rule
        (DESIGN_progress_timeline.md §6.1): a week is governed iff, per objective, the
        latest version created before the week ended covers it — so a week whose plan
        was later superseded still counts as having had a plan. Superseded versions
        are kept (see DESIGN_plan_rollback.md), so they are included here on purpose;
        this is why it can't reuse `get_mesocycle_ranges` (active-only)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # One LEFT JOIN instead of a per-macrocycle mesocycle query (N+1); LEFT so a
            # macrocycle with no mesocycles yet still surfaces (empty ranges), which the
            # version-in-force rule relies on — it can still be the latest version and
            # shadow an earlier one.
            cursor.execute("""
                SELECT mac.id, mac.objective_id, mac.created_at,
                       m.start_date, m.end_date
                FROM macrocycles mac
                LEFT JOIN mesocycles m ON m.macrocycle_id = mac.id
                ORDER BY mac.id ASC, m.start_date ASC
            """)
            rows = [dict(row) for row in cursor.fetchall()]
        by_macro: "Dict[Any, Dict[str, Any]]" = {}
        order: List[Any] = []
        for r in rows:
            entry = by_macro.get(r['id'])
            if entry is None:
                entry = {
                    'objective_id': r['objective_id'],
                    'created_at': r['created_at'],
                    'ranges': [],
                }
                by_macro[r['id']] = entry
                order.append(r['id'])
            if r['start_date'] is not None and r['end_date'] is not None:
                entry['ranges'].append((r['start_date'], r['end_date']))
        return [by_macro[mid] for mid in order]

    def get_mesocycles_for_macrocycle(self, macrocycle_id: int) -> List[Mesocycle]:
        """Fetches all mesocycles in chronological order belonging to a macrocycle."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM mesocycles WHERE macrocycle_id = ? ORDER BY start_date ASC",
                (macrocycle_id,)
            )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_mesocycle_ranges(self, start_date: str, end_date: str) -> List[Tuple[str, str]]:
        """Returns the (start_date, end_date) spans of all mesocycles overlapping the
        given window, across every objective regardless of status.

        Used to decide whether a date fell inside *any* planned block: an activity on a
        covered date with no matching workout is a genuine deviation, whereas one outside
        all coverage is just history the plan never governed (e.g. before tool adoption,
        or an unplanned off-season stretch). Objective status is intentionally not
        filtered — a since-completed objective still planned its dates — but superseded
        plan *versions* are excluded so an old version's ranges don't double-count the
        same dates as the active one (see DESIGN_plan_rollback.md)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.start_date, m.end_date FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                WHERE COALESCE(mac.status, 'active') = 'active'
                  AND m.start_date <= ? AND m.end_date >= ?
                ORDER BY m.start_date ASC
            """, (end_date, start_date))
            return [(row['start_date'], row['end_date']) for row in cursor.fetchall()]

    def get_periodization_ids_for_date(
        self, date: str
    ) -> Optional[Tuple[int, int, int]]:
        """Resolves the (objective_id, macrocycle_id, mesocycle_id) covering a date.

        Returns the mesocycle whose span contains the given date, along with its
        parent macrocycle and objective ids. When overlapping blocks exist across
        objectives, the most recently created macrocycle wins. Returns None if no
        mesocycle covers the date. Used to stamp calendar events with traceability
        back to the plan that produced them.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT mac.objective_id AS objective_id,
                       m.macrocycle_id AS macrocycle_id,
                       m.id AS mesocycle_id
                FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                WHERE COALESCE(mac.status, 'active') = 'active'
                  AND m.start_date <= ? AND m.end_date >= ?
                ORDER BY mac.id DESC, m.start_date ASC
                LIMIT 1
            """, (date, date))
            row = cursor.fetchone()
            if not row:
                return None
            return (
                row['objective_id'],
                row['macrocycle_id'],
                row['mesocycle_id'],
            )

    def get_mesocycle(self, mesocycle_id: int) -> Optional[Mesocycle]:
        """Fetches a specific mesocycle by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM mesocycles WHERE id = ?", (mesocycle_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_active_mesocycle(self, target_date: str) -> Optional[Mesocycle]:
        """Finds the active mesocycle block for a given date.

        Falls back to the next future mesocycle, or the absolute first mesocycle
        if none contain or follow the target date. Only considers active objectives.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Primary: mesocycle containing target_date
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active' AND COALESCE(mac.status, 'active') = 'active'
                  AND m.start_date <= ? AND m.end_date >= ?
                ORDER BY m.start_date ASC LIMIT 1
            """, (target_date, target_date))
            row = cursor.fetchone()
            if row: return dict(row) # type: ignore

            # Fallback 1: first mesocycle that ends in the future
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active' AND COALESCE(mac.status, 'active') = 'active'
                  AND m.end_date >= ?
                ORDER BY m.start_date ASC LIMIT 1
            """, (target_date,))
            row = cursor.fetchone()
            if row: return dict(row) # type: ignore

            # Fallback 2: absolute first mesocycle
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active' AND COALESCE(mac.status, 'active') = 'active'
                ORDER BY m.start_date ASC LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None # type: ignore

    def get_next_mesocycle(self, after_date: str) -> Optional[Mesocycle]:
        """Finds the earliest active mesocycle starting strictly after `after_date`.

        Unlike get_active_mesocycle this never falls back: no block ahead means None.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active' AND COALESCE(mac.status, 'active') = 'active'
                  AND m.start_date > ?
                ORDER BY m.start_date ASC LIMIT 1
            """, (after_date,))
            row = cursor.fetchone()
            return dict(row) if row else None # type: ignore

    def update_macrocycle_feedback(self, macrocycle_id: int, feedback: str) -> None:
        """Saves user feedback for a specific macrocycle strategy."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "UPDATE macrocycles SET feedback = ? WHERE id = ?",
                (feedback, macrocycle_id)
            )
            conn.commit()

    def update_mesocycle_feedback(self, mesocycle_id: int, feedback: str) -> None:
        """Saves user feedback for a specific training phase/mesocycle block."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "UPDATE mesocycles SET feedback = ? WHERE id = ?",
                (feedback, mesocycle_id)
            )
            conn.commit()

    def save_macrocycle(
        self, objective_id: int, strategy: str, goals_hash: str,
        constraints_hash: str, mesocycles: List[Dict[str, Any]],
        config_hash: str = "", config_snapshot: str = "", goals_snapshot: str = "",
        constraints_snapshot: str = ""
    ) -> int:
        """Saves a macrocycle and its nested mesocycles for the objective.

        goals_snapshot/constraints_snapshot are JSON of the goals and plan-shaping
        constraints the plan was generated from (the same cleaned data the hashes
        fingerprint), preserved so the inputs can be shown later even after the live
        records change. config_snapshot is JSON of the physiological thresholds the
        plan was generated with, kept as raw values (not a hash) so staleness can be
        judged against a drift tolerance (coach/service.config_changed).

        The previously-active macrocycle for the objective is *superseded* rather than
        deleted (see DESIGN_plan_rollback.md): it and its mesocycles are kept so that
        `plan rollback` can restore them, while the freshly-saved version becomes active.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Retire any currently-active version for this objective (kept for rollback).
            cursor.execute(
                "UPDATE macrocycles SET status = 'superseded', superseded_at = ? "
                "WHERE objective_id = ? AND COALESCE(status, 'active') = 'active'",
                (created_at, objective_id)
            )

            cursor.execute("""
                INSERT INTO macrocycles (
                    objective_id, strategy, goals_hash, constraints_hash, config_hash,
                    config_snapshot, goals_snapshot, constraints_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (objective_id, strategy, goals_hash, constraints_hash, config_hash,
                  config_snapshot or None, goals_snapshot or None,
                  constraints_snapshot or None, created_at))
            macrocycle_id = cursor.lastrowid

            for meso in mesocycles:
                cursor.execute("""
                    INSERT INTO mesocycles (macrocycle_id, name, start_date, end_date, focus)
                    VALUES (?, ?, ?, ?, ?)
                """, (macrocycle_id, meso['name'], meso['start_date'], meso['end_date'],
                      meso['focus']))

            conn.commit()
            return int(macrocycle_id)

    def update_macrocycle_config_hash(
        self, macrocycle_id: int, config_hash: str,
        config_snapshot: Optional[str] = None
    ) -> None:
        """Updates the config hash (and, when provided, the threshold snapshot) for a
        specific macrocycle — the "keep current plan, accept new config" path."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if config_snapshot is not None:
                cursor.execute(
                    "UPDATE macrocycles SET config_hash = ?, config_snapshot = ? "
                    "WHERE id = ?",
                    (config_hash, config_snapshot, macrocycle_id)
                )
            else:
                cursor.execute(
                    "UPDATE macrocycles SET config_hash = ? WHERE id = ?",
                    (config_hash, macrocycle_id)
                )
            conn.commit()

    def delete_macrocycle_for_objective(self, objective_id: int) -> None:
        """Deletes the macrocycle and its nested mesocycles for a specific objective."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "DELETE FROM macrocycles WHERE objective_id = ?",
                (objective_id,)
            )
            conn.commit()
