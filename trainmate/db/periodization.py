from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from trainmate.types import Macrocycle, Mesocycle, PlanFeedback
from trainmate.util import today_date
from trainmate.db.objectives import ARCHIVED


def repair_block_contiguity(
    mesocycles: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Sorts a plan's blocks and re-dates them so consecutive blocks are contiguous.

    Contiguity is asked of the model; this repairs what came back so the stored plan
    always holds it (DOMAIN_MODEL.md §4). End dates are authoritative: a block whose
    start is not the day after its predecessor's end is re-dated to start there, and a
    block ending inside its predecessor is dropped. Returns (blocks, notes), the notes
    naming each repair — empty when nothing needed one. Idempotent, so the write
    boundary re-applies it as a no-op after `plan_apply` has surfaced the notes."""
    ordered = sorted(mesocycles, key=lambda b: (b['start_date'], b['end_date']))
    repaired: List[Dict[str, Any]] = []
    notes: List[str] = []
    for block in ordered:
        if not repaired:
            repaired.append(dict(block))
            continue
        prev = repaired[-1]
        expected = (
            datetime.strptime(prev['end_date'], "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        if block['end_date'] < expected:
            notes.append(
                f"dropped '{block['name']}' ({block['start_date']} to "
                f"{block['end_date']}): it ends inside '{prev['name']}'"
            )
            continue
        if block['start_date'] != expected:
            if block['start_date'] < expected:
                how = f"overlapped '{prev['name']}'"
            else:
                how = f"left a gap after '{prev['name']}'"
            notes.append(
                f"moved the start of '{block['name']}' from {block['start_date']} to "
                f"{expected}: it {how}, which ends {prev['end_date']}"
            )
            block = dict(block, start_date=expected)
        repaired.append(dict(block))
    return repaired, notes


class PeriodizationMixin:
    """Macrocycles & mesocycles: persistence, lookups, and the plan feedback log."""

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

    def get_previous_macrocycle_version(
        self, objective_id: int, before_id: Optional[int] = None
    ) -> Optional[Macrocycle]:
        """Returns the macrocycle version chronologically prior to the active one.

        With `before_id` given, returns the newest version older than that id instead.
        Used by `plan rollback` to walk backwards through plan history one step at a
        time (see DESIGN_plan_rollback.md). Returns None when there is no earlier version.

        Named for the *version* axis on purpose: what comes back is superseded, and its
        blocks sit on the same calendar dates as the active version's while describing
        training that never happened. A retrospective view wants `get_preceding_macrocycle`
        (DESIGN_plan_rollback.md §6.1)."""
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

    def get_preceding_macrocycle(self, objective_id: int) -> Optional[Macrocycle]:
        """The active plan of the goal whose target date immediately precedes this one's.

        The other sense of "the previous plan", and the one a retrospective view wants:
        which plan governed the calendar dates *before* this goal's plan did. A window
        reaching further back than the current plan's first block runs into it, and
        nothing else supplies those blocks.

        Deliberately distinct from `get_previous_macrocycle_version`, which returns an
        earlier *version* of this same goal's plan — superseded, never trained, and
        overlapping the active one's dates (DESIGN_plan_rollback.md §6.1). Only active
        versions are considered here, for the same reason. Returns None when no earlier
        goal has a plan."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT mac.* FROM macrocycles mac
                JOIN objectives o ON mac.objective_id = o.id
                WHERE COALESCE(mac.status, 'active') = 'active'
                  AND o.target_date < (SELECT target_date FROM objectives WHERE id = ?)
                ORDER BY o.target_date DESC, mac.id DESC
                LIMIT 1
            """, (objective_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_governing_macrocycle(self) -> Optional[Macrocycle]:
        """The macrocycle of the *governing objective* — the earliest goal still ahead
        that has a plan (i.e. the objective the current workouts implement). Its
        mesocycles label the timeline weeks (DESIGN_progress_timeline.md §6.1).

        Falls back to the most recent *completed* goal's plan when nothing ahead has one:
        the day after an event, the months of workouts behind the athlete still belong to
        that plan, and dropping the labels then would blank the timeline exactly when it
        is being looked at. Advancing used to be a side effect of marking the goal
        completed by hand; it is now the date's job (DESIGN_backward_evaluation.md §12).

        Deliberately distinct from `get_active_objective()` (next goal, plan-or-not):
        meso labels must follow whichever plan the current workouts implement. Returns
        None when no goal has a macrocycle."""
        live = [o for o in self.get_objectives() if o.get('status') != ARCHIVED]
        today = today_date().strftime("%Y-%m-%d")
        ahead = [o for o in live if str(o['target_date']) >= today]   # ORDER BY date ASC
        for obj in ahead:
            macro = self.get_macrocycle_for_objective(obj['id'])
            if macro:
                return macro
        for obj in reversed([o for o in live if str(o['target_date']) < today]):
            macro = self.get_macrocycle_for_objective(obj['id'])
            if macro:
                return macro
        return None

    def get_mesocycles_for_macrocycle(self, macrocycle_id: int) -> List[Mesocycle]:
        """Fetches all mesocycles in chronological order belonging to a macrocycle."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM mesocycles WHERE macrocycle_id = ? ORDER BY start_date ASC",
                (macrocycle_id,)
            )
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def _get_covering_mesocycles(
        self, start_date: str, end_date: Optional[str] = None,
        prefer_macro_id: Optional[int] = None
    ) -> Tuple[List[Mesocycle], List[int]]:
        """The blocks that ACTUALLY overlap a window — or none — plus the macrocycle IDs
        dropped as conflicts. The strict half of `get_governing_mesocycles`, its one
        caller: an empty answer means no block covers any of these days, which is what
        lets the governing reader fall back only when it should.

        Sequential plans both survive — a long span legitimately crosses from one goal's
        last block into the next goal's first — but two plans covering the *same* dates
        cannot both be followed, so the most recently created wins, the same tiebreak
        get_periodization_ids_for_date makes per day. `prefer_macro_id` settles that
        contest by hand instead.

        `end_date=None` means no upper bound — "every block from here to the plan's end",
        the form `get_constraints` already takes, so no caller has to invent a far-future
        date to stand in for one.
        """
        clauses = ["m.end_date >= ?"]
        params: List[str] = [start_date]
        if end_date is not None:
            clauses.append("m.start_date <= ?")
            params.append(end_date)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active' AND COALESCE(mac.status, 'active') = 'active'
                  AND {' AND '.join(clauses)}
                ORDER BY m.start_date ASC, m.id ASC
            """, params)
            mesos = [dict(row) for row in cursor.fetchall()]
        if not mesos:
            return [], []

        spans: Dict[int, Tuple[str, str]] = {}
        for m in mesos:
            mid = m['macrocycle_id']
            start, end = spans.get(mid, (m['start_date'], m['end_date']))
            spans[mid] = (min(start, m['start_date']), max(end, m['end_date']))

        kept: List[int] = []
        dropped: List[int] = []
        # Newest plan first, unless one was named; a plan is dropped only when it fights
        # an already-kept plan for the same days.
        for mid in sorted(spans, key=lambda i: (i == prefer_macro_id, i), reverse=True):
            start, end = spans[mid]
            if any(start <= spans[k][1] and end >= spans[k][0] for k in kept):
                dropped.append(mid)
            else:
                kept.append(mid)
        return [m for m in mesos if m['macrocycle_id'] in kept], sorted(dropped)

    def get_governing_mesocycles(
        self, start_date: str, end_date: Optional[str] = None,
        prefer_macro_id: Optional[int] = None
    ) -> Tuple[List[Mesocycle], List[int]]:
        """The blocks that govern a window, plus the macrocycle IDs dropped as
        conflicts — the one window reader (DESIGN_cli_selectors.md §8).

        Overlap and arbitration are `_get_covering_mesocycles`'s: whole plans survive or
        drop, the most recently created wins, `prefer_macro_id` overrides. When nothing
        overlaps at all, answers with get_active_mesocycle's nearest block instead — so
        a plan that starts after the window still answers, and an empty result means
        there is genuinely no plan. A caller that treats the answer as covering a *date*
        wants the strict single-date reader, `get_covering_mesocycle`."""
        mesos, dropped = self._get_covering_mesocycles(
            start_date, end_date, prefer_macro_id
        )
        if mesos:
            return mesos, dropped
        nearest = self.get_active_mesocycle(start_date)
        return ([nearest] if nearest else []), []

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

    def get_covering_mesocycle(self, target_date: str) -> Optional[Mesocycle]:
        """The active mesocycle that actually CONTAINS this date, or None. The strict
        reader: what every caller wants that goes on to read the answer's dates as
        covering the target. Only considers active objectives. When overlapping plans
        both contain the date, the most recently created wins — the same tiebreak
        get_periodization_ids_for_date makes, so no two readers name different blocks
        for one day."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active' AND COALESCE(mac.status, 'active') = 'active'
                  AND m.start_date <= ? AND m.end_date >= ?
                ORDER BY mac.id DESC, m.start_date ASC LIMIT 1
            """, (target_date, target_date))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_active_mesocycle(self, target_date: str) -> Optional[Mesocycle]:
        """`get_covering_mesocycle`, falling back to the next future mesocycle, or the
        absolute first one, when none contains the date — the block a command should act
        RELATIVE to, not necessarily one containing the date. A caller that reads the
        answer's dates as covering the target wants the strict reader instead."""
        covering = self.get_covering_mesocycle(target_date)
        if covering:
            return covering
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Fallback 1: first mesocycle that ends in the future
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active' AND COALESCE(mac.status, 'active') = 'active'
                  AND m.end_date >= ?
                ORDER BY m.start_date ASC, mac.id DESC LIMIT 1
            """, (target_date,))
            row = cursor.fetchone()
            if row: return dict(row) # type: ignore

            # Fallback 2: absolute first mesocycle
            cursor.execute("""
                SELECT m.* FROM mesocycles m
                JOIN macrocycles mac ON m.macrocycle_id = mac.id
                JOIN objectives o ON mac.objective_id = o.id
                WHERE o.status = 'active' AND COALESCE(mac.status, 'active') = 'active'
                ORDER BY m.start_date ASC, mac.id DESC LIMIT 1
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
                ORDER BY m.start_date ASC, mac.id DESC LIMIT 1
            """, (after_date,))
            row = cursor.fetchone()
            return dict(row) if row else None # type: ignore

    def add_plan_feedback(
        self, macrocycle_id: int, text: str, mesocycle_id: Optional[int] = None
    ) -> int:
        """Appends one note to a plan's feedback log and returns its id.

        `mesocycle_id` None files the note against the plan as a whole
        (DESIGN_plan_feedback.md §6)."""
        created_at = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO plan_feedback (macrocycle_id, mesocycle_id, created_at, text) "
                "VALUES (?, ?, ?, ?)",
                (macrocycle_id, mesocycle_id, created_at, text)
            )
            conn.commit()
            return int(cursor.lastrowid)

    def list_plan_feedback(self, macrocycle_id: int) -> List[PlanFeedback]:
        """The notes attached to one plan version, **oldest first**, each carrying the
        name of the block it was filed against (None = plan-level).

        One ordering everywhere — this listing, `plan show`, the regeneration prompt — so
        the log reads as a conversation in the order it happened, and a later note reads
        as an amendment of an earlier one (DESIGN_plan_feedback.md §4/§7)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT f.*, m.name AS mesocycle_name
                FROM plan_feedback f
                LEFT JOIN mesocycles m ON f.mesocycle_id = m.id
                WHERE f.macrocycle_id = ?
                ORDER BY f.created_at ASC, f.id ASC
            """, (macrocycle_id,))
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def get_plan_feedback(self, feedback_id: int) -> Optional[PlanFeedback]:
        """One note by id, with the name of the block it was filed against."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT f.*, m.name AS mesocycle_name
                FROM plan_feedback f
                LEFT JOIN mesocycles m ON f.mesocycle_id = m.id
                WHERE f.id = ?
            """, (feedback_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def rm_plan_feedback(self, feedback_id: int) -> None:
        """Deletes one note from the log."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "DELETE FROM plan_feedback WHERE id = ?", (feedback_id,)
            )
            conn.commit()

    def save_macrocycle(
        self, objective_id: int, strategy: str, goals_hash: str,
        constraints_hash: str, mesocycles: List[Dict[str, Any]],
        config_hash: str = "", config_snapshot: str = "", goals_snapshot: str = "",
        constraints_snapshot: str = "", all_constraints_snapshot: str = "",
        profile_snapshot: str = ""
    ) -> int:
        """Saves a macrocycle and its nested mesocycles for the objective.

        The four `*_snapshot` arguments are JSON of the inputs the plan was generated from,
        preserved so they can be shown and judged for drift after the live records change;
        `db/base.py` documents each at its column.

        The previously-active macrocycle for the objective is *superseded* rather than
        deleted (see DESIGN_plan_rollback.md): it and its mesocycles are kept so that
        `plan rollback` can restore them, while the freshly-saved version becomes active.

        Blocks pass through `repair_block_contiguity` before insertion, so within-plan
        gaps and overlaps never reach the table (DOMAIN_MODEL.md §4).
        """
        mesocycles, _ = repair_block_contiguity(mesocycles)
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
                    config_snapshot, profile_snapshot, goals_snapshot,
                    constraints_snapshot, all_constraints_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (objective_id, strategy, goals_hash, constraints_hash, config_hash,
                  config_snapshot or None, profile_snapshot or None,
                  goals_snapshot or None, constraints_snapshot or None,
                  all_constraints_snapshot or None, created_at))
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
        config_snapshot: Optional[str] = None,
        profile_snapshot: Optional[str] = None,
        goals_hash: Optional[str] = None,
        goals_snapshot: Optional[str] = None,
        constraints_hash: Optional[str] = None,
        constraints_snapshot: Optional[str] = None,
    ) -> None:
        """Updates the config hash (and, when provided, the threshold and profile
        snapshots, the goals and the plan-shaping constraints) for a specific macrocycle —
        the "keep current plan, accept new inputs" path.

        The stamp has to clear everything the staleness check flags, or a kept plan flags
        again tomorrow (DESIGN_plan_change_continuity.md §6.5). A value left as None is
        not written, so a caller re-stamping only the hash cannot blank out what the plan
        was generated against."""
        sets, params = ["config_hash = ?"], [config_hash]
        for column, value in (('config_snapshot', config_snapshot),
                              ('profile_snapshot', profile_snapshot),
                              ('goals_hash', goals_hash),
                              ('goals_snapshot', goals_snapshot),
                              ('constraints_hash', constraints_hash),
                              ('constraints_snapshot', constraints_snapshot)):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE macrocycles SET {', '.join(sets)} WHERE id = ?",
                (*params, macrocycle_id)
            )
            conn.commit()

    def save_reshape_verdict(
        self, macrocycle_id: int, key: str, verdict: Optional[str]
    ) -> None:
        """Caches the coach's re-shaping read against the edit it was asked about, so
        `plan show` asks once per edit rather than on every read
        (DESIGN_plan_change_continuity.md §7)."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE macrocycles SET reshape_verdict = ?, reshape_verdict_key = ? "
                "WHERE id = ?",
                (verdict, key, macrocycle_id),
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
