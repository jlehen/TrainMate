from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from trainmate.types import Workout
from trainmate.sports import sport_aliases


class WorkoutsMixin:
    """Workouts CRUD and soft-delete / reschedule operations."""

    def save_workout(
        self, date: str, sport_type: str, title: str, description: str,
        original_description: Optional[str] = None,
        modification_reason: Optional[str] = None, google_event_id: Optional[str] = None,
        duration_minutes: Optional[int] = None, rpe: Optional[int] = None,
        tss: Optional[int] = None, original_date: Optional[str] = None,
        original_duration_minutes: Optional[int] = None,
        original_tss: Optional[int] = None, original_rpe: Optional[int] = None,
        removed: bool = False, removed_reason: Optional[str] = None,
        source: Optional[str] = None, adaptation_summary: Optional[str] = None,
        macrocycle_id: Optional[int] = None, adapted_at: Optional[str] = None,
        benchmark_type: Optional[str] = None, clear_benchmark: bool = False,
        planned_zone_currency: Optional[str] = None,
        planned_zone_sec: Optional[List[Optional[int]]] = None
    ) -> int:
        """Saves a workout, updating it if one already exists that day for the same
        sport. Existence is alias-aware (see trainmate.sports), so adapting/regenerating
        a canonical ``strength_training`` updates an existing ``strength`` row in place
        instead of inserting a duplicate; the stored row keeps its original spelling.

        New rows are tagged with the plan version they belong to: `macrocycle_id` if
        given, else the macrocycle governing the workout's date (see DESIGN_plan_rollback.md).
        The tag is fixed at creation and never overwritten on update. Archived rows (from a
        superseded plan version) are ignored by the same-day/same-sport lookup, so a fresh
        generation inserts new rows rather than reviving archived ones.

        Calendar freshness is *not* touched here: `pushed_signature` is left as-is, so any
        content change made through this method automatically reads as `stale` (see
        trainmate.calendar_state). Only a successful push, via `mark_workout_pushed`,
        records a new signature.

        `created_at` is set to now (UTC) on INSERT only and never overwritten on update,
        recording when the session first entered the plan (NULL on legacy rows).

        `original_duration_minutes` / `original_tss` / `original_rpe` snapshot the load the
        session was planned with. When passed explicitly they seed that snapshot (used when a
        cross-sport swap inherits the displaced session's planned load); otherwise they take
        the inserted load on INSERT and are COALESCE-preserved on every UPDATE (the live
        duration/rpe/tss reference resolves to the pre-update value, so the first adaptation of
        a legacy row backfills them), giving the plan a stable "planned vs. current" comparison
        without parsing the description. `original_description` carries the same intent for the
        prose — a swap can seed it with the displaced session's description.

        `adapted_at` marks this save as a `workout adapt` easing: when given (a UTC ISO
        timestamp), it is stored and `adaptation_count` is bumped, giving the daily
        adaptation a recency/frequency signal so it can avoid compounding cuts. All other
        callers (plan/generate, swap, add) leave it None, which preserves both columns
        untouched — a fresh INSERT then starts at count 0 / NULL, so regenerating a plan
        resets the adaptation history of that slot.

        `benchmark_type` COALESCE-preserves so a partial re-save cannot read an omission as
        a deletion; `clear_benchmark=True` is the one way to blank it in place, used when an
        adaptation replaces a test with something that is no longer that test
        (DESIGN_benchmark_workouts.md §3.1/§4.2).

        `planned_zone_currency` + `planned_zone_sec` (a 7-slot list, HR sessions filling
        1-5 and leaving 6-7 None) carry the session's intensity target
        (DESIGN_intensity_distribution.md §9.8). Both COALESCE-preserve when omitted and
        overwrite when given: `adapt` emits them too, because §9.4's drift correction IS
        a rewrite of how a session is prescribed, and leaving them alone would leave them
        describing the prescription that was just replaced. Stored as emitted — a session
        whose zone seconds do not sum to `duration_minutes` is a prescription, not an
        accounting identity, and silently scaling it would put the app back in the
        business of correcting the model rather than aligning for it."""
        zones = list(planned_zone_sec or [None] * 7)[:7]
        zones += [None] * (7 - len(zones))
        aliases = sport_aliases(sport_type)
        placeholders = ",".join("?" * len(aliases))
        if macrocycle_id is None:
            ids = self.get_periodization_ids_for_date(date)
            if ids:
                macrocycle_id = ids[1]
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, google_event_id FROM workouts WHERE date = ? "
                f"AND LOWER(sport_type) IN ({placeholders}) AND archived_at IS NULL",
                (date, *aliases)
            )
            row = cursor.fetchone()
            if row:
                workout_id = row['id']
                ge_id = google_event_id if google_event_id is not None else row['google_event_id']
                cursor.execute("""
                    UPDATE workouts
                    SET title = ?, description = ?,
                        original_description = COALESCE(?, original_description),
                        original_date = COALESCE(?, original_date, date),
                        modification_reason = ?,
                        adaptation_summary = COALESCE(?, adaptation_summary),
                        google_event_id = ?,
                        duration_minutes = COALESCE(?, duration_minutes),
                        rpe = COALESCE(?, rpe),
                        tss = COALESCE(?, tss),
                        original_duration_minutes =
                            COALESCE(original_duration_minutes, ?, duration_minutes),
                        original_tss = COALESCE(original_tss, ?, tss),
                        original_rpe = COALESCE(original_rpe, ?, rpe),
                        removed = ?, removed_reason = ?,
                        source = COALESCE(?, source),
                        benchmark_type =
                            CASE WHEN ? THEN NULL ELSE COALESCE(?, benchmark_type) END,
                        adapted_at = COALESCE(?, adapted_at),
                        adaptation_count = COALESCE(adaptation_count, 0)
                            + CASE WHEN ? IS NOT NULL THEN 1 ELSE 0 END,
                        planned_zone_currency =
                            COALESCE(?, planned_zone_currency),
                        planned_zone1_sec = COALESCE(?, planned_zone1_sec),
                        planned_zone2_sec = COALESCE(?, planned_zone2_sec),
                        planned_zone3_sec = COALESCE(?, planned_zone3_sec),
                        planned_zone4_sec = COALESCE(?, planned_zone4_sec),
                        planned_zone5_sec = COALESCE(?, planned_zone5_sec),
                        planned_zone6_sec = COALESCE(?, planned_zone6_sec),
                        planned_zone7_sec = COALESCE(?, planned_zone7_sec)
                    WHERE id = ?
                """, (title, description, original_description,
                      original_date,
                      modification_reason, adaptation_summary, ge_id,
                      duration_minutes, rpe, tss,
                      original_duration_minutes, original_tss, original_rpe,
                      int(removed), removed_reason, source,
                      int(clear_benchmark), benchmark_type,
                      adapted_at, adapted_at,
                      planned_zone_currency, *zones,
                      workout_id))
            else:
                cursor.execute("""
                    INSERT INTO workouts (
                        date, sport_type, title, description, original_description,
                        original_date, modification_reason, adaptation_summary,
                        google_event_id, duration_minutes, rpe, tss,
                        original_duration_minutes, original_rpe, original_tss,
                        removed, removed_reason, source, macrocycle_id,
                        created_at, adapted_at, adaptation_count, benchmark_type,
                        planned_zone_currency,
                        planned_zone1_sec, planned_zone2_sec, planned_zone3_sec,
                        planned_zone4_sec, planned_zone5_sec, planned_zone6_sec,
                        planned_zone7_sec
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (date, sport_type, title, description,
                      original_description or description,
                      original_date or date,
                      modification_reason, adaptation_summary,
                      google_event_id, duration_minutes,
                      rpe, tss,
                      original_duration_minutes if original_duration_minutes is not None
                          else duration_minutes,
                      original_rpe if original_rpe is not None else rpe,
                      original_tss if original_tss is not None else tss,
                      int(removed), removed_reason, source, macrocycle_id,
                      datetime.now(timezone.utc).isoformat(),
                      adapted_at, 1 if adapted_at else 0, benchmark_type,
                      planned_zone_currency, *zones))
                workout_id = cursor.lastrowid
            conn.commit()
            return int(workout_id)

    def get_workout(self, date: str, sport_type: str) -> Optional[Workout]:
        """Fetches a workout by date and sport type.

        Matching is alias-aware (see trainmate.sports): a lookup for the canonical
        ``strength_training`` finds a row stored under an alias like ``strength`` (and
        vice-versa), case-insensitively, so the coach's canonical vocabulary lines up
        with manually-added or legacy spellings."""
        aliases = sport_aliases(sport_type)
        placeholders = ",".join("?" * len(aliases))
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM workouts WHERE date = ? "
                f"AND LOWER(sport_type) IN ({placeholders}) AND archived_at IS NULL",
                (date, *aliases)
            )
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def get_workouts(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        sport_type: Optional[str] = None,
        include_removed: bool = False,
        include_archived: bool = False
    ) -> List[Workout]:
        """Fetches workouts ordered by date, optionally within a range or by sport type.

        Soft-removed workouts (`removed = 1`, set by `workout rm`) are excluded by
        default so they never appear in listings, comparisons, adaptation inputs, or
        the calendar push. Pass include_removed=True to retrieve them (e.g. to tell
        the coach a session was deliberately cancelled).

        Archived workouts (`archived_at` set — belonging to a superseded plan version,
        see DESIGN_plan_rollback.md) are likewise excluded by default; pass
        include_archived=True only when reconciling plan-version history."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM workouts WHERE 1=1"
            params = []
            if not include_removed:
                query += " AND COALESCE(removed, 0) = 0"
            if not include_archived:
                query += " AND archived_at IS NULL"
            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND date <= ?"
                params.append(end_date)
            if sport_type:
                query += " AND LOWER(sport_type) = ?"
                params.append(sport_type.lower())
            query += " ORDER BY date ASC"
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]  # type: ignore

    def archive_future_workouts(self, from_date: str) -> List[Workout]:
        """Archives (rather than deletes) every live workout on/after `from_date`.

        Returns the affected rows as they were *before* archival (so the caller still
        sees their `google_event_id` and can tear down the Calendar events), then stamps
        them `archived_at = now` and clears their Calendar handle/signature so a later
        restore re-pushes cleanly. Used when a regeneration or `plan rollback` displaces
        the current plan's workouts; they keep their `macrocycle_id` tag so the matching
        rollback can resurrect them (see DESIGN_plan_rollback.md)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM workouts WHERE date >= ? AND archived_at IS NULL",
                (from_date,)
            )
            rows = [dict(r) for r in cursor.fetchall()]  # type: ignore
            cursor.execute(
                "UPDATE workouts SET archived_at = ?, google_event_id = NULL, "
                "pushed_signature = NULL WHERE date >= ? AND archived_at IS NULL",
                (now, from_date)
            )
            conn.commit()
            return rows

    def get_archived_batches(
        self, from_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Lists the archived workout batches, newest first.

        One `archive_future_workouts` call stamps every row it displaces with the same
        `archived_at`, so that timestamp is the batch identity: the set of workouts that
        were live at that moment. `workout rollback` restores one such batch and
        `workout batches` lists them (see DESIGN_plan_rollback.md §9).

        Each entry: {archived_at, workouts, first_date, last_date, macrocycle_ids} plus,
        when `from_date` is given, `restorable` — how many of the batch's rows a restore
        from that date would actually revive (see `restore_workout_batch`)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT archived_at, COUNT(*) AS workouts, MIN(date) AS first_date, "
                "MAX(date) AS last_date, "
                "GROUP_CONCAT(DISTINCT macrocycle_id) AS macro_ids "
                "FROM workouts WHERE archived_at IS NOT NULL "
                "GROUP BY archived_at ORDER BY archived_at DESC"
            )
            batches = []
            for row in cursor.fetchall():
                raw_ids = row['macro_ids'] or ""
                batches.append({
                    'archived_at': row['archived_at'],
                    'workouts': row['workouts'],
                    'first_date': row['first_date'],
                    'last_date': row['last_date'],
                    'macrocycle_ids': sorted(
                        int(i) for i in raw_ids.split(",") if i.strip()
                    ),
                })
            if from_date is None:
                return batches
            cursor.execute(
                "SELECT archived_at, COUNT(*) AS n FROM workouts "
                "WHERE archived_at IS NOT NULL AND date >= ? GROUP BY archived_at",
                (from_date,)
            )
            counts = {r['archived_at']: r['n'] for r in cursor.fetchall()}
            for b in batches:
                b['restorable'] = counts.get(b['archived_at'], 0)
            return batches

    def restore_workout_batch(self, archived_at: str, from_date: str) -> List[Workout]:
        """Un-archives one archived batch, restricted to rows dated `from_date` onward.

        The date floor keeps restore symmetric with `archive_future_workouts`, which only
        ever archives from a given date onward: a batch older than that floor still holds
        rows for days that have since passed, whose slots are occupied by live rows the
        archive step left alone. Restoring those would put two live workouts on the same
        date+sport and re-create Calendar events in the past, so they stay archived
        (see DESIGN_plan_rollback.md §9). Calendar handles were cleared at archival, so
        the caller must re-push what comes back. Returns the restored rows."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM workouts WHERE archived_at = ? AND date >= ?",
                (archived_at, from_date)
            )
            restored = [dict(r) for r in cursor.fetchall()]  # type: ignore
            cursor.execute(
                "UPDATE workouts SET archived_at = NULL "
                "WHERE archived_at = ? AND date >= ?",
                (archived_at, from_date)
            )
            conn.commit()
            return restored

    def restore_macrocycle_workouts(
        self, macrocycle_id: int, from_date: str
    ) -> List[Workout]:
        """Un-archives the most recently archived batch of workouts for a plan version.

        A `plan rollback` to `macrocycle_id` resurrects the workouts that were live when
        that version was last superseded — i.e. the batch sharing the latest `archived_at`
        among that version's archived rows, restored from `from_date` onward. Returns the
        restored rows (empty if the version never had workouts). See DESIGN_plan_rollback.md."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT MAX(archived_at) AS m FROM workouts "
                "WHERE macrocycle_id = ? AND archived_at IS NOT NULL",
                (macrocycle_id,)
            )
            row = cursor.fetchone()
            batch = row['m'] if row else None
        if not batch:
            return []
        return self.restore_workout_batch(batch, from_date)

    def get_workout_by_id(self, workout_id: int) -> Optional[Workout]:
        """Fetches a workout by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,))
            row = cursor.fetchone()
            return dict(row) if row else None  # type: ignore

    def delete_workout_by_id(self, workout_id: int) -> None:
        """Deletes a workout by ID."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
            conn.commit()

    def mark_workout_pushed(
        self, workout_id: int, google_event_id: str, signature: str
    ) -> None:
        """Records a successful Google Calendar push: stores the event handle and the
        signature of the content that was pushed. Freshness is derived from comparing
        this signature against the live content (see trainmate.calendar_state), so this
        is the *only* method that marks a workout `synced`."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET google_event_id = ?, pushed_signature = ? WHERE id = ?",
                (google_event_id, signature, workout_id)
            )
            conn.commit()

    def mark_workout_adherence_pushed(
        self, workout_id: int, signature: str
    ) -> None:
        """Records a successful `workout compare --mark` push: stores the
        adherence-inclusive signature (see trainmate.calendar_state.adherence_signature)
        so a later compare over the same range can skip a no-op Calendar update when the
        event already carries the same verdict. Orthogonal to `pushed_signature` — the
        underlying `sync_workout` already refreshed that."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET marked_signature = ? WHERE id = ?",
                (signature, workout_id)
            )
            conn.commit()

    def mark_workout_removed(
        self, workout_id: int, reason: Optional[str] = None
    ) -> None:
        """Soft-deletes a workout: flags it `removed`.

        The row is kept (excluded from reads by default) so the coach can still be told
        the session was deliberately cancelled, optionally with the athlete's `reason`.
        `google_event_id` is preserved so the calendar event can be updated to be marked
        as deleted on sync; changing `removed` shifts the content hash, so the workout
        reads as `stale` and the next push updates the event (see trainmate.calendar_state)."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET removed = 1, removed_reason = ? WHERE id = ?",
                (reason, workout_id)
            )
            conn.commit()

    def restore_workout(self, workout_id: int) -> None:
        """Restores a soft-deleted workout.

        Clears the `removed` and `removed_reason` flags; the resulting content change
        reads as `stale` so the next push un-deletes the Google Calendar event."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET removed = 0, removed_reason = NULL WHERE id = ?",
                (workout_id,)
            )
            conn.commit()

    def update_workout_date(
        self, workout_id: int, new_date: str, modification_reason: str
    ) -> None:
        """Moves a workout to a new date; the date change reads as `stale` so the next
        push updates the Calendar event (see trainmate.calendar_state).

        If the workout lands back on its original_date the modification_reason
        is cleared — the workout is no longer considered adapted/swapped.
        """
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE workouts SET date = ?, "
                "modification_reason = CASE "
                "  WHEN COALESCE(original_date, date) = ? THEN NULL "
                "  ELSE ? "
                "END "
                "WHERE id = ?",
                (new_date, new_date, modification_reason, workout_id)
            )
            conn.commit()
