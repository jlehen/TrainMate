"""Making Google Calendar agree with the workouts log (DESIGN_workout_revisions.md §8).

The event lifecycle used to piggyback on archival: archiving a plan cleared the event
handles and the service tore the events down, and a restore re-pushed. An append-only
table has no such hook, and nothing may creep into the append primitive to replace it — a
Calendar call inside the write path would put network I/O in a transaction and fail
workout writes on push errors.

Instead every change ends with one pass over the lineages it touched, after commit. The
`workout_change` handle schedules it, so the only way to write workouts already schedules
the reconcile; `runtime._build_db` is what attaches this module to the database handle.
"""
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from trainmate import runtime
from trainmate.calendar_state import calendar_status
from trainmate.db.workouts import ATHLETE_VOID_KINDS
from trainmate.util import Progress, fail, green

# Whether a pass renders as one summary line and a bar, or as the per-event lines. The
# commands that take `-v` flip this around their write.
_verbose: bool = False

# Set by `no_sync()` while a command the athlete asked not to sync is writing.
_suppressed: bool = False


@contextmanager
def no_calendar_sync() -> Iterator[None]:
    """Makes the pass a no-op, for the commands that offer `--no-sync`.

    The athlete asked for the local change only. Nothing is lost: freshness is derived
    from the stored signature, so the sessions read `[STALE]` and the next
    `workout push` sends them (§8)."""
    global _suppressed
    was, _suppressed = _suppressed, True
    try:
        yield
    finally:
        _suppressed = was


@contextmanager
def verbose_events() -> Iterator[None]:
    """Keeps the per-event Calendar lines, and no bar, so they scroll undisturbed (`-v`)."""
    global _verbose
    was, _verbose = _verbose, True
    try:
        yield
    finally:
        _verbose = was


def reconcile(db, lineage_ids: Sequence[int]) -> None:
    """Pushes, moves or tears down the Calendar events of the given lineages.

    Keyed on each lineage's newest revision, not on a slot: a swap leaves the moved
    session with a void where it left and a copy where it landed, and the copy must speak
    for the lineage or this would delete an event it should move (§8).

    Decided first and executed second, so one batch of Calendar round-trips renders as one
    summary line and a bar rather than a page of per-event chatter.
    """
    if _suppressed:
        return
    pushes, teardowns = _plan(db, lineage_ids)
    total = len(pushes) + len(teardowns)
    if not total:
        return
    syncer = runtime.calendar_syncer
    with _framing(total) as bar:
        for workout in pushes:
            _push(syncer, workout)
            bar.step()
        for lineage_id, event_id in teardowns:
            _tear_down(db, syncer, lineage_id, event_id)
            bar.step()
    print(green("Google Calendar updated: " + _summary(len(pushes), len(teardowns)) + "."))


@contextmanager
def _framing(total: int) -> Iterator[Progress]:
    """A batch of Calendar writes under one bar; `-v` keeps the per-event lines instead."""
    if _verbose:
        yield Progress(0)
        return
    # Imported here, not at module load: importing google_calendar builds the syncer
    # singleton, which needs credentials (see runtime._build_calendar_syncer).
    from trainmate.google_calendar import quiet_events
    with quiet_events(), Progress(total) as bar:
        yield bar


def leaves_trace(head: Dict[str, Any]) -> bool:
    """Whether a void keeps its Calendar event, retitled, rather than the event being
    torn down (DESIGN_plan_change_continuity.md §5.2).

    A day the athlete was counting on does not disappear: it is marked, with a reason.
    Which days those are is the void's own three clauses — the athlete asked for it, it
    was a session they added, or it fell inside the commitment window the change that
    removed it ran under.
    """
    if head["change_kind"] in ATHLETE_VOID_KINDS:
        return True
    if head.get("source") == "manual":
        return True
    commitment_end = head.get("commitment_end")
    return commitment_end is not None and head["date"] <= commitment_end


def _plan(
    db, lineage_ids: Sequence[int]
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, Optional[str]]]]:
    """What each lineage needs: a push, a teardown, or nothing.

    Read from the lineage's newest revision rather than from its slot: a void covered by
    a new session in the same slot is never the slot's live row, and a marker written and
    then covered in one run would be torn down the instant it was written (§5.2).
    """
    pushes: List[Dict[str, Any]] = []
    teardowns: List[Tuple[int, Optional[str]]] = []
    for lineage_id in lineage_ids:
        head = db.get_lineage_head(lineage_id)
        state = db.get_calendar_state(lineage_id)
        event_id = (state or {}).get("google_event_id")
        if head is None:
            if state:
                teardowns.append((lineage_id, event_id))
            continue
        if not head["removed"]:
            # A session, but not its slot's live one: something was appended over it, so
            # the lineage was superseded and its event belongs to nothing.
            if head.get("superseded"):
                if state:
                    teardowns.append((lineage_id, event_id))
                continue
            if calendar_status(head) != "synced":
                pushes.append(head)
            continue
        # A void that leaves a trace keeps its event, retitled — and gets one when it
        # never had one, or a session written and dropped between two syncs would leave
        # no trace at all (§5.2).
        if leaves_trace(head):
            if calendar_status(head) != "synced":
                pushes.append(head)
            continue
        if state:
            teardowns.append((lineage_id, event_id))
    return pushes, teardowns


def _summary(pushed: int, removed: int) -> str:
    parts = []
    if pushed:
        parts.append(f"{pushed} event(s) pushed")
    if removed:
        parts.append(f"{removed} removed")
    return ", ".join(parts)


def _push(syncer, workout) -> None:
    try:
        syncer.sync_workout(workout)
    except Exception as e:
        fail(f"Google Calendar sync of {workout['title']} failed: {e}")


def _tear_down(db, syncer, lineage_id: int, event_id: Optional[str]) -> None:
    if event_id:
        try:
            syncer.delete_workout_event(event_id)
        except Exception as e:
            fail(f"Google Calendar event delete failed: {e}")
    db.clear_calendar_state(lineage_id)
