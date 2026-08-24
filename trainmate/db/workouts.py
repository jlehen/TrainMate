"""The append-only `workouts` table (DESIGN_workout_revisions.md).

A row is one revision of one session, never updated and never deleted. The highest `id`
in a `(date, sport_canonical)` slot is the live one; everything else is history. Writes
go through `workout_change`, the only door onto the table (§6). Reads go through the
`live_workouts` view and come back hydrated with the fields the lineage now derives —
`original_*`, the adaptation tally, `source` — so everything above this module sees the
same dict shape it always did (§5).
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import (
    Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple,
)
from trainmate.types import Constraint, Workout
from trainmate.sports import canonical_sport

# One value per command invocation, fixed at write time (§3).
CHANGE_KINDS = (
    "generate", "adapt", "swap", "add", "rm",
    "restore", "rollback", "stand-down", "reinstate",
)

# The voids the athlete asked for, as against the ones the plan produced. A session
# `workout rm` cancelled or a goal stood down is a decision, and both the coach and
# Calendar treat it as one: the prompt calls it a deliberate cancellation and the event
# stays, retitled. A day a generate or an adapt simply stopped scheduling is neither.
ATHLETE_VOID_KINDS = ("rm", "stand-down")

_ZONE_COLUMNS = tuple(f"planned_zone{i}_sec" for i in range(1, 8))

# The physical revision columns, in INSERT order. `id` is assigned by SQLite.
REVISION_COLUMNS = (
    "change_id", "lineage_id", "date", "sport_canonical", "sport_type",
    "title", "description", "duration_minutes", "rpe", "tss",
    "void", "reason", "restored_from", "macrocycle_id", "created_at",
    "benchmark_type", "planned_zone_currency",
) + _ZONE_COLUMNS

# What "the same prescription" means for the §9 no-op rule. `reason` is deliberately
# out: if the prescription did not move, the session was held, and a rationale for
# holding is not worth a revision.
PRESCRIPTION_FIELDS = (
    "date", "sport_canonical", "sport_type", "title", "description",
    "duration_minutes", "rpe", "tss", "benchmark_type", "planned_zone_currency",
    "void",
) + _ZONE_COLUMNS


def _same_prescription(row: Dict[str, Any], live: Dict[str, Any]) -> bool:
    """Whether a proposed revision prescribes exactly what the live one already does (§9)."""
    return all(row.get(f) == live[f] for f in PRESCRIPTION_FIELDS)


def _fell(new: Any, old: Any) -> bool:
    """Whether a load field dropped. Both values must be present: a missing number is a
    gap in the data, not an easing."""
    if new is None or old is None:
        return False
    return float(new) < float(old)


def _eased(revision: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> bool:
    """Whether an adapt revision actually reduced load against its own predecessor (§7)."""
    if previous is None:
        return False
    return (
        _fell(revision["duration_minutes"], previous["duration_minutes"])
        or _fell(revision["tss"], previous["tss"])
    )


def _resolve_kind(
    by_id: Dict[int, Dict[str, Any]], revision: Dict[str, Any]
) -> str:
    """The kind that describes the *form* a revision holds.

    A rollback/restore/reinstate copy re-establishes an earlier prescription, so it reads
    as whatever that prescription was — the same `restored_from` jump the adaptation tally
    makes (§7). Every other revision reads as its own change kind."""
    seen: Set[int] = set()
    current = revision
    while current["restored_from"] is not None and current["id"] not in seen:
        seen.add(current["id"])
        restored = by_id.get(current["restored_from"])
        if restored is None:
            break
        current = restored
    return current["kind"]


def _adaptation_tally(
    revisions: List[Dict[str, Any]], head_id: int
) -> Tuple[int, Optional[str]]:
    """Walks a lineage backwards from `head_id` and counts the easings still standing (§7).

    Not a `COUNT(*)`: a rollback/restore copy jumps over the span it undid, and a
    `generate` re-prescribes the session so easings of the previous prescription stop
    describing it. Returns `(adaptation_count, adapted_at)`.
    """
    by_id = {r["id"]: r for r in revisions}
    position = {r["id"]: i for i, r in enumerate(revisions)}
    count, adapted_at = 0, None
    seen: Set[int] = set()
    current = by_id.get(head_id)
    while current is not None and current["id"] not in seen:
        seen.add(current["id"])
        if current["restored_from"] is not None:
            current = by_id.get(current["restored_from"])
            continue
        if current["kind"] == "generate":
            break
        index = position[current["id"]]
        previous = revisions[index - 1] if index else None
        if current["kind"] == "adapt" and _eased(current, previous):
            count += 1
            if adapted_at is None:
                adapted_at = current["change_created_at"]
        current = previous
    return count, adapted_at


class WorkoutChange:
    """One command's worth of appends, all under a single `workout_changes` row (§6).

    The handle is the enforcement, not a convenience: a writer cannot append a revision
    without a change row, because `append` is the only door in, and cannot forget the
    Calendar reconcile, because the handle schedules it on close (§8).
    """

    def __init__(self, db, conn, change_id: int, kind: str, created_at: str) -> None:
        self._db = db
        self._conn = conn
        self.id = change_id
        self.kind = kind
        self.created_at = created_at
        # Every lineage this change looked at, written or not: the §8 pass compares each
        # against `workout_calendar_state`, so including an unchanged one costs a hash
        # and catches a session whose event was never pushed.
        self.touched_lineages: Set[int] = set()
        self.appended: List[int] = []
        # Sessions the athlete added by hand that a `generate` replaced, so the command
        # can name them (§12). Only `generate` fills this.
        self.replaced_manual: List[Dict[str, Any]] = []

    # --- reading the slot ---

    @property
    def conn(self):
        """The transaction every append in this change shares."""
        return self._conn

    def live_revision(self, date: str, sport_canonical: str) -> Optional[Dict[str, Any]]:
        """The newest revision in a slot — the live one. Reads `workouts` directly
        because this IS the append path; everything else reads the view."""
        row = self._conn.execute(
            "SELECT * FROM workouts WHERE date = ? AND sport_canonical = ? "
            "ORDER BY id DESC LIMIT 1",
            (date, sport_canonical),
        ).fetchone()
        return dict(row) if row else None

    def _lineage_is_manual(self, lineage_id: int) -> bool:
        """Whether a lineage was started by `workout add` — its first change kind (§5)."""
        row = self._conn.execute(
            "SELECT c.kind FROM workouts w JOIN workout_changes c ON c.id = w.change_id "
            "WHERE w.lineage_id = ? ORDER BY w.id ASC LIMIT 1",
            (lineage_id,),
        ).fetchone()
        return bool(row) and row["kind"] == "add"

    # --- the lineage rules (§4) ---

    def _lineage_for(
        self, live: Optional[Dict[str, Any]], explicit: Optional[int]
    ) -> Tuple[Optional[int], Optional[Dict[str, Any]], bool]:
        """Which lineage a revision joins. Returns `(lineage_id, superseded, manual)`:
        `lineage_id` None means start a new one, `superseded` is the live revision whose
        lineage this append ends, and `manual` flags the athlete-added case §12 reports.

        "Next occupant of the slot" and "same session" are different things, so this is
        not a blanket inherit-from-the-slot (§4)."""
        if explicit is not None:
            # A swap — or an adapt moving a session — carries the moved session's lineage
            # to the destination, not the destination slot's.
            return explicit, None, False
        if live is None or live["void"]:
            # Appending over a void is a new session, not a resurrection of the removed
            # one: it must not inherit its Calendar event, its originals or its tally.
            return None, live, False
        if self.kind == "add":
            return None, live, False
        if self.kind == "generate" and self._lineage_is_manual(live["lineage_id"]):
            # The plan owns the horizon, so the generate proceeds — but under a new
            # lineage, which is also what keeps `source` honest.
            return None, live, True
        return live["lineage_id"], None, False

    # --- writing ---

    def _write(
        self, row: Dict[str, Any],
        decision: Tuple[Optional[int], Optional[Dict[str, Any]], bool],
        live: Optional[Dict[str, Any]],
    ) -> Optional[int]:
        """Inserts one revision, or returns None when §9 suppresses it as a no-op."""
        lineage_id, superseded, manual = decision
        if live is not None and live["lineage_id"] is not None:
            self.touched_lineages.add(live["lineage_id"])
        if lineage_id is not None:
            self.touched_lineages.add(lineage_id)
        if live is not None and _same_prescription(row, live):
            return None

        row["change_id"] = self.id
        row["lineage_id"] = lineage_id
        cursor = self._conn.cursor()
        columns = ", ".join(REVISION_COLUMNS)
        placeholders = ", ".join("?" * len(REVISION_COLUMNS))
        cursor.execute(
            f"INSERT INTO workouts ({columns}) VALUES ({placeholders})",
            [row.get(c) for c in REVISION_COLUMNS],
        )
        revision_id = int(cursor.lastrowid)
        if lineage_id is None:
            # A first revision is born with a NULL lineage — the id does not exist until
            # the insert assigns it — and is seeded here. The one UPDATE the §14 trigger
            # lets through, and it may write nothing but the row's own id.
            cursor.execute(
                "UPDATE workouts SET lineage_id = id WHERE id = ?", (revision_id,)
            )
            self.touched_lineages.add(revision_id)
        if manual and superseded is not None:
            self.replaced_manual.append(dict(superseded))
        self.appended.append(revision_id)
        return revision_id

    def append(
        self, *, date: str, sport_type: str, title: str,
        description: Optional[str] = None,
        duration_minutes: Optional[int] = None, rpe: Optional[int] = None,
        tss: Optional[int] = None, reason: Optional[str] = None,
        benchmark_type: Optional[str] = None, clear_benchmark: bool = False,
        planned_zone_currency: Optional[str] = None,
        planned_zone_sec: Optional[Sequence[Optional[int]]] = None,
        macrocycle_id: Optional[int] = None, lineage_id: Optional[int] = None,
        restored_from: Optional[int] = None,
    ) -> Optional[int]:
        """Appends one revision of the session in `(date, sport_type)`.

        The new revision is the slot's live one merged with what is supplied here (§6
        step 2), so a partial re-save cannot read an omission as a deletion: `title` and
        `description` are always taken as given, every other field carries forward when
        omitted. `clear_benchmark` is the one way to blank `benchmark_type` in place, for
        an adaptation that replaces a test with something that is no longer that test
        (DESIGN_benchmark_workouts.md §4.2). `lineage_id` names the session explicitly,
        which is what a swap's destination needs (§4).

        Returns the new revision's id, or None when §9 suppressed it as a no-op.
        """
        sport_canonical = canonical_sport(sport_type)
        live = self.live_revision(date, sport_canonical)
        decision = self._lineage_for(live, lineage_id)
        # Only a revision that CONTINUES the session already in the slot carries its
        # fields forward. One that starts a new lineage — a manual `add`, a session
        # appended over a void, a swap's destination — must not inherit the previous
        # occupant's load, or an `add` with no --duration would silently adopt it (§4/§6).
        continues = live is not None and decision[0] == live["lineage_id"]
        base = live if continues else {}
        row: Dict[str, Any] = {
            "date": date,
            "sport_canonical": sport_canonical,
            "sport_type": sport_type,
            "title": title,
            "description": description,
            "void": 0,
            "reason": reason,
            "restored_from": restored_from,
        }
        for field, supplied in (
            ("duration_minutes", duration_minutes), ("rpe", rpe), ("tss", tss),
            ("planned_zone_currency", planned_zone_currency),
        ):
            row[field] = supplied if supplied is not None else base.get(field)
        zones = list(planned_zone_sec or [])[:7]
        zones += [None] * (7 - len(zones))
        for column, supplied in zip(_ZONE_COLUMNS, zones):
            row[column] = supplied if supplied is not None else base.get(column)
        if clear_benchmark:
            row["benchmark_type"] = None
        else:
            row["benchmark_type"] = (
                benchmark_type if benchmark_type is not None
                else base.get("benchmark_type")
            )
        row["macrocycle_id"] = self._db._macrocycle_tag(
            date, macrocycle_id, base.get("macrocycle_id")
        )
        # When the session first entered the plan. Carried across a lineage's revisions;
        # a new session starts its own clock at the change that created it.
        row["created_at"] = base.get("created_at") or self.created_at
        return self._write(row, decision, live)

    def void(
        self, *, date: str, sport_type: str, reason: Optional[str] = None
    ) -> Optional[int]:
        """Appends a revision saying this slot now holds no session (§3).

        A void carries the lineage of the session it ends — the removed or departing one,
        never a fresh one: it is the last chapter of a lineage, not a first (§4). Returns
        None when the slot is already empty or already void.
        """
        sport_canonical = canonical_sport(sport_type)
        live = self.live_revision(date, sport_canonical)
        if live is None or live["void"]:
            return None
        row = {k: live[k] for k in REVISION_COLUMNS}
        row["void"] = 1
        row["reason"] = reason
        row["restored_from"] = None
        return self._write(row, (live["lineage_id"], None, False), live)

    def restore(
        self, revision: Dict[str, Any], reason: Optional[str] = None
    ) -> Optional[int]:
        """Appends a copy of an earlier revision, stamped `restored_from`.

        Restore is a duplicate rather than an un-flag: the copy gets a new, higher id and
        becomes live by the same rule as everything else, so there is no second mechanism
        (§5). The stamp is what lets the adaptation tally skip the span this undid (§7).
        """
        row = {k: revision[k] for k in REVISION_COLUMNS}
        row["restored_from"] = revision["id"]
        if reason is not None:
            row["reason"] = reason
        live = self.live_revision(revision["date"], revision["sport_canonical"])
        return self._write(row, (revision["lineage_id"], None, False), live)


class WorkoutsMixin:
    """Reads and writes over the append-only workouts log."""

    # ------------------------------------------------------------------ writing

    @contextmanager
    def workout_change(
        self, kind: str, summary: Optional[str] = None,
        macrocycle_id: Optional[int] = None,
    ) -> Iterator[WorkoutChange]:
        """Opens the single write path onto `workouts` (§6).

        One `workout_changes` row per command invocation, written even when the pass
        appends nothing — an adapt that looked at the metrics and held is a real event
        (§3). Everything inside shares one transaction; the §8 Calendar reconcile runs
        over the lineages the change touched once that transaction has committed.
        """
        if kind not in CHANGE_KINDS:
            raise ValueError(f"unknown workout change kind: {kind!r}")
        created_at = datetime.now(timezone.utc).isoformat()
        change: Optional[WorkoutChange] = None
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO workout_changes (created_at, kind, summary, macrocycle_id) "
                "VALUES (?, ?, ?, ?)",
                (created_at, kind, summary, macrocycle_id),
            )
            change = WorkoutChange(self, conn, int(cursor.lastrowid), kind, created_at)
            yield change
        self._reconcile_calendar(change.touched_lineages)

    def _reconcile_calendar(self, lineage_ids: Set[int]) -> None:
        """Runs the §8 pass, if a Calendar is attached (see BaseDB.calendar_hook)."""
        if not lineage_ids or self.calendar_hook is None:
            return
        self.calendar_hook(self, sorted(lineage_ids))

    def _macrocycle_tag(
        self, date: str, supplied: Optional[int], inherited: Optional[int]
    ) -> Optional[int]:
        """Which plan version a revision belongs to: what the caller named, else what the
        session already carried, else the macrocycle governing its date."""
        if supplied is not None:
            return supplied
        if inherited is not None:
            return inherited
        ids = self.get_periodization_ids_for_date(date)
        return ids[1] if ids else None

    # ------------------------------------------------------------------ hydration

    def _lineage_revisions(
        self, conn, lineage_ids: Sequence[int]
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Every revision of the named lineages, oldest first, with its change joined.

        One query for the whole result set: hydration must not turn a sixty-row listing
        into two hundred lineage lookups (§5)."""
        if not lineage_ids:
            return {}
        placeholders = ",".join("?" * len(lineage_ids))
        rows = conn.execute(
            "SELECT w.*, c.kind AS kind, c.created_at AS change_created_at, "
            "       c.summary AS change_summary "
            "FROM workouts w JOIN workout_changes c ON c.id = w.change_id "
            f"WHERE w.lineage_id IN ({placeholders}) "
            "ORDER BY w.lineage_id ASC, w.id ASC",
            tuple(lineage_ids),
        ).fetchall()
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["lineage_id"], []).append(dict(row))
        return grouped

    def _calendar_state_rows(
        self, conn, lineage_ids: Sequence[int]
    ) -> Dict[int, Dict[str, Any]]:
        if not lineage_ids:
            return {}
        placeholders = ",".join("?" * len(lineage_ids))
        rows = conn.execute(
            f"SELECT * FROM workout_calendar_state WHERE lineage_id IN ({placeholders})",
            tuple(lineage_ids),
        ).fetchall()
        return {row["lineage_id"]: dict(row) for row in rows}

    @staticmethod
    def _hydrated(
        row: Dict[str, Any], revisions: List[Dict[str, Any]],
        calendar: Optional[Dict[str, Any]],
    ) -> Workout:
        """One live revision as the dict the rest of the app reads (§5).

        `id` is the LINEAGE id, which is what makes the athlete-visible ids stable and
        what `workout rm` / `workout swap` address; the physical row id travels as
        `revision_id` for the history surfaces."""
        by_id = {r["id"]: r for r in revisions}
        head = by_id.get(row["id"], dict(row, kind=None, change_summary=None))
        first = revisions[0] if revisions else row
        count, adapted_at = _adaptation_tally(revisions, row["id"])
        void = bool(row["void"])
        calendar = calendar or {}
        source_kind = revisions[0]["kind"] if revisions else None
        return {  # type: ignore[return-value]
            "id": row["lineage_id"],
            "revision_id": row["id"],
            "date": row["date"],
            "sport_type": row["sport_type"],
            "title": row["title"],
            "description": row["description"],
            "original_description": first["description"],
            "pushed_signature": calendar.get("pushed_signature"),
            "adherence_pushed_signature": calendar.get("adherence_pushed_signature"),
            # A revision's note is either why it changed or why it was cancelled, and the
            # change kind says which (§3).
            "modification_reason": None if void else row["reason"],
            "adaptation_summary": head.get("change_summary"),
            "change_kind": _resolve_kind(by_id, head) if revisions else None,
            "google_event_id": calendar.get("google_event_id"),
            "removed": void,
            "removed_reason": row["reason"] if void else None,
            "source": "manual" if source_kind == "add" else "generated",
            "duration_minutes": row["duration_minutes"],
            "rpe": row["rpe"],
            "tss": row["tss"],
            "macrocycle_id": row["macrocycle_id"],
            "original_date": first["date"],
            "created_at": row["created_at"],
            "adapted_at": adapted_at,
            "adaptation_count": count,
            "original_duration_minutes": first["duration_minutes"],
            "original_tss": first["tss"],
            "original_rpe": first["rpe"],
            "benchmark_type": row["benchmark_type"],
            "planned_zone_currency": row["planned_zone_currency"],
            **{column: row[column] for column in _ZONE_COLUMNS},
        }

    def _hydrate(self, conn, rows: List[Dict[str, Any]]) -> List[Workout]:
        if not rows:
            return []
        lineage_ids = sorted({r["lineage_id"] for r in rows if r["lineage_id"]})
        revisions = self._lineage_revisions(conn, lineage_ids)
        calendar = self._calendar_state_rows(conn, lineage_ids)
        return [
            self._hydrated(
                row, revisions.get(row["lineage_id"], []),
                calendar.get(row["lineage_id"]),
            )
            for row in rows
        ]

    @staticmethod
    def _speaking(conn, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Drops a live revision that its lineage no longer speaks through.

        A cross-sport swap leaves the moved session's lineage with a live void at the slot
        it left AND a live copy where it landed; the copy speaks for the lineage, or a
        listing would show one session twice and a push would fight itself (§8). Only a
        void can be the stale half, so only voids need checking.
        """
        stale_candidates = sorted({r["lineage_id"] for r in rows if r["void"]})
        if not stale_candidates:
            return rows
        placeholders = ",".join("?" * len(stale_candidates))
        heads = {
            row["lineage_id"]: row["head"]
            for row in conn.execute(
                "SELECT lineage_id, MAX(id) AS head FROM live_workouts "
                f"WHERE lineage_id IN ({placeholders}) GROUP BY lineage_id",
                tuple(stale_candidates),
            )
        }
        return [
            r for r in rows
            if not r["void"] or heads.get(r["lineage_id"]) == r["id"]
        ]

    # ------------------------------------------------------------------ reading

    def get_workout(self, date: str, sport_type: str) -> Optional[Workout]:
        """The live session in one slot, or None when the slot is empty.

        Matching is canonical (see trainmate.sports), so a lookup for
        ``strength_training`` finds a session stored under ``strength``. A cancelled
        session still answers here — callers filter on `removed`, as they always did."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM live_workouts WHERE date = ? AND sport_canonical = ?",
                (date, canonical_sport(sport_type)),
            ).fetchone()
            if not row:
                return None
            rows = self._speaking(conn, [dict(row)])
            hydrated = self._hydrate(conn, rows)
            return hydrated[0] if hydrated else None

    def get_workouts(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        sport_type: Optional[str] = None,
        include_removed: bool = False,
    ) -> List[Workout]:
        """The live sessions, ordered by date, optionally within a range or by sport.

        Cancelled sessions (a void revision — `workout rm`, a goal stood down, an adapt
        dropping a session) are excluded by default so they never appear in listings,
        comparisons, adaptation inputs or the calendar push. Pass include_removed=True to
        retrieve them, e.g. to tell the coach a session was deliberately cancelled.

        There is no `include_archived` any more: a superseded revision is not a flagged
        row but an older sibling in its slot, and the history surfaces read it through
        `get_plan_revisions` / `get_workout_changes` (§5, §10).
        """
        query = "SELECT * FROM live_workouts WHERE 1=1"
        params: List[Any] = []
        if not include_removed:
            query += " AND void = 0"
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        if sport_type:
            query += " AND sport_canonical = ?"
            params.append(canonical_sport(sport_type))
        query += " ORDER BY date ASC, id ASC"
        with self._get_connection() as conn:
            rows = [dict(r) for r in conn.execute(query, params).fetchall()]
            return self._hydrate(conn, self._speaking(conn, rows))

    def get_workout_by_id(self, workout_id: int) -> Optional[Workout]:
        """The live session a lineage id names, or None when it no longer holds a slot.

        `workout_id` is a lineage: the id `workout list` printed and the one `workout rm`
        or `workout swap` was given. Resolving it to the lineage's newest live revision is
        what stops those commands addressing a dead revision (§5)."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM live_workouts WHERE lineage_id = ? ORDER BY id DESC LIMIT 1",
                (workout_id,),
            ).fetchone()
            if not row:
                return None
            hydrated = self._hydrate(conn, [dict(row)])
            return hydrated[0] if hydrated else None

    def get_workout_revision(self, revision_id: int) -> Optional[Dict[str, Any]]:
        """One physical revision, raw — the history reader behind restore and rollback."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM workouts WHERE id = ?", (revision_id,)
            ).fetchone()
            return dict(row) if row else None

    def count_future_workouts_for_macrocycles(
        self, macrocycle_ids: Sequence[int], from_date: str
    ) -> int:
        """How many live sessions on/after `from_date` those plan versions generated.

        What `goal rm` would strand: deleting the goal cascades the versions away but not
        the sessions tagged with them (DESIGN_backward_evaluation.md §14)."""
        if not macrocycle_ids:
            return 0
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM live_workouts "
                "WHERE date >= ? AND void = 0 "
                f"AND macrocycle_id IN ({','.join('?' * len(macrocycle_ids))})",
                [from_date] + list(macrocycle_ids),
            ).fetchone()
            return int(row["n"]) if row else 0

    def count_untagged_future_workouts(self, from_date: str) -> int:
        """How many live sessions on/after `from_date` carry no `macrocycle_id`.

        Those rows predate the plan-version tag, so no goal can claim them and calling a
        goal off leaves them alone — reported rather than swept
        (DESIGN_backward_evaluation.md §14)."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM live_workouts "
                "WHERE date >= ? AND void = 0 AND macrocycle_id IS NULL",
                (from_date,),
            ).fetchone()
            return int(row["n"]) if row else 0

    def live_workouts_for_macrocycles(
        self, macrocycle_ids: Sequence[int], from_date: str
    ) -> List[Workout]:
        """The live sessions on/after `from_date` those plan versions own — what a goal
        stand-down voids (§10)."""
        if not macrocycle_ids:
            return []
        with self._get_connection() as conn:
            rows = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM live_workouts WHERE date >= ? AND void = 0 "
                    f"AND macrocycle_id IN ({','.join('?' * len(macrocycle_ids))}) "
                    "ORDER BY date ASC, id ASC",
                    [from_date] + list(macrocycle_ids),
                ).fetchall()
            ]
            return self._hydrate(conn, self._speaking(conn, rows))

    def get_plan_revisions(self) -> List[Dict[str, Any]]:
        """Every revision ever appended, raw, flagged with whether it is still live.

        `plan show`'s per-version listing, which is history rather than plan: it asks what
        a plan version scheduled, including what a later version displaced (§5)."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT w.*, "
                "  (w.id = (SELECT MAX(w2.id) FROM workouts w2 "
                "           WHERE w2.date = w.date "
                "             AND w2.sport_canonical = w.sport_canonical)) AS live "
                "FROM workouts w ORDER BY w.date ASC, w.id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_lineage_revisions(self, lineage_id: int) -> List[Dict[str, Any]]:
        """Every form one session has ever had, oldest first, with its change joined.

        The whole story of a single session rather than of a plan version — what the
        Calendar event renders as its history (DESIGN_calendar_lineage.md §4)."""
        with self._get_connection() as conn:
            return self._lineage_revisions(conn, [lineage_id]).get(lineage_id, [])

    # ------------------------------------------------------------------ changes

    def get_change(self, change_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM workout_changes WHERE id = ?", (change_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_workout_changes(
        self, from_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Every change, newest first — the list `workout batches` renders (§10).

        The batch key moved from `archived_at` (stamped on the rows a command killed) to
        `change_id` (carried by the rows a command created), so every write is a batch and
        every batch is undoable, adapts and manual edits included. A change that appended
        nothing is listed too, flagged `held`.

        Each entry: {id, created_at, kind, summary, workouts, first_date, last_date,
        macrocycle_ids, held} plus, when `from_date` is given, `restorable` — how many of
        its revisions are dated from there on.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT c.id, c.created_at, c.kind, c.summary, "
                "       COUNT(w.id) AS workouts, MIN(w.date) AS first_date, "
                "       MAX(w.date) AS last_date, "
                "       GROUP_CONCAT(DISTINCT w.macrocycle_id) AS macro_ids "
                "FROM workout_changes c LEFT JOIN workouts w ON w.change_id = c.id "
                "GROUP BY c.id ORDER BY c.id DESC"
            ).fetchall()
            changes = []
            for row in rows:
                raw_ids = row["macro_ids"] or ""
                changes.append({
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "kind": row["kind"],
                    "summary": row["summary"],
                    "workouts": row["workouts"],
                    "first_date": row["first_date"],
                    "last_date": row["last_date"],
                    "macrocycle_ids": sorted(
                        int(i) for i in raw_ids.split(",") if i.strip()
                    ),
                    "held": row["workouts"] == 0,
                })
            if from_date is None:
                return changes
            counts = {
                r["change_id"]: r["n"] for r in conn.execute(
                    "SELECT change_id, COUNT(*) AS n FROM workouts WHERE date >= ? "
                    "GROUP BY change_id", (from_date,)
                )
            }
            for change in changes:
                change["restorable"] = counts.get(change["id"], 0)
            return changes

    def newest_change_for_macrocycles(
        self, macrocycle_ids: Sequence[int]
    ) -> Optional[int]:
        """The newest change that appended any revision tagged with one of these plan
        versions — what `plan rollback` derives its target from (§10)."""
        if not macrocycle_ids:
            return None
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(change_id) AS c FROM workouts "
                f"WHERE macrocycle_id IN ({','.join('?' * len(macrocycle_ids))})",
                tuple(macrocycle_ids),
            ).fetchone()
            return int(row["c"]) if row and row["c"] is not None else None

    def next_change_after(self, change_id: int) -> Optional[int]:
        """The change that ran immediately after `change_id`, if any."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT MIN(id) AS c FROM workout_changes WHERE id > ?", (change_id,)
            ).fetchone()
            return int(row["c"]) if row and row["c"] is not None else None

    # ------------------------------------------------------------------ undo

    def rollback_to_change(
        self, change_id: int, from_date: str, summary: Optional[str] = None
    ) -> Tuple[List[Workout], List[Constraint]]:
        """Point-in-time undo: reverts change N and every change after it (§10).

        Worked per SLOT rather than per lineage, because a change can end a lineage by
        appending over it — a `generate` landing on a manual session, an `add` replacing a
        generated one — and the session to bring back is then the slot's previous
        occupant, whose own lineage the change never touched. For every slot N or a later
        change wrote, the revision live there just before N is copied forward, stamped
        `restored_from`; a slot that held nothing before N gets a void.

        The date floor is the same rule `archive_future_workouts` had: appending a copy
        into a past slot would silently make it the live session for a day already
        trained (DESIGN_plan_rollback.md §9).

        Returns `(restored sessions, constraints un-honored by the restore)`.
        """
        target = self.get_change(change_id)
        if target is None:
            raise ValueError(f"No workout change #{change_id}.")
        with self.workout_change(kind="rollback", summary=summary) as change:
            slots = change.conn.execute(
                "SELECT DISTINCT date, sport_canonical FROM workouts "
                "WHERE change_id >= ? AND date >= ? ORDER BY date ASC",
                (change_id, from_date),
            ).fetchall()
            for slot in slots:
                previous = change.conn.execute(
                    "SELECT * FROM workouts WHERE date = ? AND sport_canonical = ? "
                    "AND change_id < ? ORDER BY id DESC LIMIT 1",
                    (slot["date"], slot["sport_canonical"], change_id),
                ).fetchone()
                if previous is None:
                    live = change.live_revision(slot["date"], slot["sport_canonical"])
                    if live is not None:
                        change.void(
                            date=live["date"], sport_type=live["sport_type"],
                            reason="Undone by a rollback",
                        )
                    continue
                change.restore(dict(previous))
            # The restored plan predates these honorings and cannot reflect them (§10).
            unhonored = self.clear_honored_after(target["created_at"], from_date)
            appended = list(change.appended)
        restored = [
            w for w in self.hydrate_revisions(appended) if not w["removed"]
        ]
        return restored, unhonored

    def hydrate_revisions(self, revision_ids: Sequence[int]) -> List[Workout]:
        """The given physical revisions as hydrated sessions, in date order.

        What a command that just appended reads back, so it reports the sessions it wrote
        rather than re-deriving them from a window."""
        if not revision_ids:
            return []
        placeholders = ",".join("?" * len(revision_ids))
        with self._get_connection() as conn:
            rows = [
                dict(r) for r in conn.execute(
                    f"SELECT * FROM workouts WHERE id IN ({placeholders}) "
                    "ORDER BY date ASC, id ASC",
                    tuple(revision_ids),
                ).fetchall()
            ]
            return self._hydrate(conn, rows)

    def revision_before_live_void(self, lineage_id: int) -> Optional[Dict[str, Any]]:
        """The revision a lineage's live void ended — what `workout restore` copies back.

        None when the lineage's live revision is not a void, or when the void is the
        lineage's first revision and there is nothing behind it."""
        with self._get_connection() as conn:
            void = conn.execute(
                "SELECT * FROM live_workouts WHERE lineage_id = ? ORDER BY id DESC LIMIT 1",
                (lineage_id,),
            ).fetchone()
            if not void or not void["void"]:
                return None
            ended = conn.execute(
                "SELECT * FROM workouts WHERE lineage_id = ? AND id < ? "
                "ORDER BY id DESC LIMIT 1",
                (lineage_id, void["id"]),
            ).fetchone()
            return dict(ended) if ended else None

    def stood_down_sessions(
        self, macrocycle_ids: Sequence[int], from_date: str
    ) -> List[Dict[str, Any]]:
        """The revisions a goal's live `stand-down` voids ended — what reinstating it
        copies back (§10).

        Read off the live view, so a slot regenerated or edited since the stand-down no
        longer has that void live and is skipped: something else owns it now. Each entry
        is a raw revision plus `stood_down_at`, the void's change timestamp, which is what
        the caller un-honors constraints against.
        """
        if not macrocycle_ids:
            return []
        with self._get_connection() as conn:
            voids = conn.execute(
                "SELECT w.id AS void_id, w.lineage_id AS lineage_id, "
                "       c.created_at AS stood_down_at "
                "FROM live_workouts w JOIN workout_changes c ON c.id = w.change_id "
                "WHERE w.void = 1 AND c.kind = 'stand-down' AND w.date >= ? "
                f"AND w.macrocycle_id IN ({','.join('?' * len(macrocycle_ids))}) "
                "ORDER BY w.lineage_id ASC",
                [from_date] + list(macrocycle_ids),
            ).fetchall()
            sessions = []
            for void in voids:
                ended = conn.execute(
                    "SELECT * FROM workouts WHERE lineage_id = ? AND id < ? "
                    "ORDER BY id DESC LIMIT 1",
                    (void["lineage_id"], void["void_id"]),
                ).fetchone()
                if ended is None or ended["date"] < from_date:
                    continue
                sessions.append(dict(ended, stood_down_at=void["stood_down_at"]))
            return sessions

    # ------------------------------------------------------------------ calendar

    def get_calendar_state(self, lineage_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM workout_calendar_state WHERE lineage_id = ?",
                (lineage_id,),
            ).fetchone()
            return dict(row) if row else None

    def mark_workout_pushed(
        self, lineage_id: int, google_event_id: str, signature: str
    ) -> None:
        """Records a successful Google Calendar push: the event handle and the signature
        of the content that was pushed. Freshness is derived by comparing that signature
        against the live content (see trainmate.calendar_state), so this is the *only*
        method that marks a session `synced`. Keyed by lineage, because there is one
        event per session and it must follow it across edits and date moves (§8)."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO workout_calendar_state "
                "  (lineage_id, google_event_id, pushed_signature) VALUES (?, ?, ?) "
                "ON CONFLICT(lineage_id) DO UPDATE SET "
                "  google_event_id = excluded.google_event_id, "
                "  pushed_signature = excluded.pushed_signature",
                (lineage_id, google_event_id, signature),
            )
            conn.commit()

    def mark_workout_adherence_pushed(self, lineage_id: int, signature: str) -> None:
        """Records a successful `workout compare --mark` push: the adherence-inclusive
        signature (see trainmate.calendar_state.adherence_signature), so a later compare
        over the same range can skip a no-op Calendar update when the event already
        carries the same verdict. Orthogonal to `pushed_signature` — the underlying
        `sync_workout` already refreshed that (§8)."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO workout_calendar_state "
                "  (lineage_id, adherence_pushed_signature) VALUES (?, ?) "
                "ON CONFLICT(lineage_id) DO UPDATE SET "
                "  adherence_pushed_signature = excluded.adherence_pushed_signature",
                (lineage_id, signature),
            )
            conn.commit()

    def clear_calendar_state(self, lineage_id: int) -> None:
        """Forgets a session's Calendar event, after the event itself has been deleted."""
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM workout_calendar_state WHERE lineage_id = ?", (lineage_id,)
            )
            conn.commit()
