import io
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

# The clock is imported by value (`from trainmate.util import today_str`), so each
# binding site has to be pinned separately. These are the ones the planning window
# reads; leaving any of them live lets date-based fixtures expire with the calendar.
_CLOCK_SITES = [
    ("trainmate.coach.service._today_str", False),
    ("trainmate.coach.service._today_date", True),
    ("trainmate.db.objectives.today_date", True),
    # A goal's completed/upcoming split is now decided by the date, so the accessors that
    # ask "is this goal behind us?" are clock sites too (DESIGN_backward_evaluation.md §12).
    ("trainmate.db.periodization.today_date", True),
    # `_goal_span_start` opens a goal's own plan window at today, so the goal timeline
    # the `-g` grammar walks is a clock site too (DESIGN_cli_selectors.md §9).
    ("trainmate.cli.plans._today_date", True),
    # The companion plan view dates its blocks against today, the same read the expert
    # view's window makes (DESIGN_render_persona.md §4).
    ("trainmate.cli.render._today_date", True),
]


def _m(minutes):
    """Minutes as seconds — zone fixtures are written in minutes, stored in seconds."""
    return minutes * 60.0


def _hr(sport, mins, coverage=0.95, judged="same"):
    # `judged=None` is the unjudgeable row: every session that week was under the
    # `zone_min_activity_minutes` floor, so the recording can't be graded (§11).
    from trainmate.intensity import ZoneRow
    jc = coverage if judged == "same" else judged
    return ZoneRow(sport, "hr", tuple(_m(v) for v in mins), coverage, jc)


def _pwr(sport, mins, coverage=0.9, judged="same"):
    from trainmate.intensity import ZoneRow
    jc = coverage if judged == "same" else judged
    return ZoneRow(sport, "power", tuple(_m(v) for v in mins), coverage, jc)


def _zweek(mon, rows=(), seconds=None, label="Base 1", in_progress=False, judged="same"):
    # `judged` is the duration over sessions that cleared the floor (§11); it defaults to
    # the whole of `seconds`, the ordinary case where every session is a real workout.
    return {
        "week_commencing": mon, "meso_label": label, "meso_source": "plan",
        "in_progress": in_progress, "actual_load": 0.0, "planned_load": None,
        "zone_rows": list(rows), "sport_seconds": dict(seconds or {}),
        "judged_sport_seconds": dict(
            (seconds or {}) if judged == "same" else (judged or {})
        ),
    }


def pin_clock(testcase, day: str) -> None:
    """Freezes every clock a plan/goal window consults, for the life of one test."""
    as_date = date.fromisoformat(day)
    for target, wants_date in _CLOCK_SITES:
        patcher = patch(target, return_value=as_date if wants_date else day)
        patcher.start()
        testcase.addCleanup(patcher.stop)

# Delete children before parents to satisfy foreign-key constraints. `workouts` is
# absent on purpose: it is append-only and guarded by a trigger, so it is reset through
# `wipe_workouts`, which also clears its changes and Calendar state
# (DESIGN_workout_revisions.md §14).
_ALL_TABLES = [
    "plan_feedback",
    "mesocycles",
    "macrocycles",
    "completed_activities",
    "athlete_metrics_cache",
    "athlete_baselines",
    "coach_learnings",
    "analysis_cache",
    "sync_state",
    "daily_signals",
    "constraints",
    "benchmark_results",
    "objectives",
    "settings",
]


def clear_all_tables(db) -> None:
    db.wipe_workouts()
    with db._get_connection() as conn:
        for table in _ALL_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


# Every module that binds the database handle by value at import time. There is one
# entry per convention the app currently uses; a test that rebinds only some of them
# leaves the rest pointing at the real database, so they are bound together or not at
# all. Modules absent from sys.modules are skipped — a CLI test should not have to
# import the web app to isolate itself.
_DB_BINDING_SITES = (
    ("trainmate.runtime", "db"),
    ("trainmate.db", "db"),
    ("trainmate_cli", "db"),
    ("trainmate_web", "db"),
    ("trainmate.garmin", "db"),
    ("trainmate.garmin.sync", "db"),
    ("trainmate.garmin.pmc", "db"),
    ("trainmate.garmin.load", "db"),
    ("trainmate.garmin.client", "db"),
    ("trainmate.google_calendar", "db"),
)


def rebind_test_db(test_db) -> None:
    """Points every imported handle at `test_db`, and the Calendar at a mock.

    Call after replacing the Database instance (setUpClass), not only at import: a
    module imported later in the run would otherwise keep the handle it captured.

    Writing workouts now reaches Google Calendar on its own — the change handle
    reconciles the lineages it touched (DESIGN_workout_revisions.md §8) — so isolating
    a test from the real database means isolating it from the real calendar too. The
    default mock is installed only when nothing is bound yet, so an active `@patch` on
    `trainmate.runtime.calendar_syncer` still owns the handle for its test.
    """
    from trainmate import runtime
    from trainmate.clock import reset_cache as forget_timezone
    # The athlete timezone is resolved once per process from the settings table, so a
    # handle swap has to drop it or the new database's setting is never read.
    forget_timezone()
    if "calendar_syncer" not in vars(runtime):
        runtime.calendar_syncer = MagicMock()
    from trainmate.calendar_reconcile import reconcile
    test_db.calendar_hook = reconcile
    for module_name, attr in _DB_BINDING_SITES:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        # `vars()`, not hasattr(): trainmate.runtime resolves singletons through a
        # module __getattr__, so asking whether it "has" db would build the real one
        # against the production file — the very thing being avoided here. Assigning
        # shadows the accessor, which is what we want in either case.
        if module_name == "trainmate.runtime" or attr in vars(module):
            setattr(module, attr, test_db)


def unstamp_schema(db) -> None:
    """Makes `db` look un-migrated, so the next _init_db() runs the migrations.

    `_init_db` skips its work when the database is already stamped at the current
    SCHEMA_VERSION. A test that hand-installs a legacy table has produced exactly the
    state a real pre-stamp database is in — no version row — so clearing the stamp is
    what makes the fixture faithful rather than a way around the check.
    """
    with db._get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS schema_version")
        conn.commit()


_UNBOUND = object()   # "nothing was bound yet", as opposed to "bound to None"


def restore_db_handles(testcase) -> None:
    """Puts the process-wide db handles back the way they were once `testcase` ends.

    Call it *before* binding, in a module that must not leave its own Database behind for
    whatever runs next. Saving them is the whole difficulty: both resolve through a module
    `__getattr__`, so simply reading one in order to remember it BUILDS the real Database
    against the production file — which `tests/__init__.py`'s guard then refuses. Two test
    modules each wrote that by hand and each hit it, but only when run on their own: inside
    the full suite an earlier module had already bound a handle, so the read found one
    cached and the bug stayed invisible. Hence one copy, here, next to `rebind_test_db`,
    which dodges the same trap for the same reason.

    A handle nothing had built is restored to *absent*, not to some value: leaving a
    concrete singleton where the lazy accessor used to be would hand the next module a
    stale database instead of one it can still bind.
    """
    from trainmate import runtime
    import trainmate.db

    saved = [(runtime, vars(runtime).get("db", _UNBOUND)),
             (trainmate.db, vars(trainmate.db).get("db", _UNBOUND))]

    def _restore():
        for module, previous in saved:
            if previous is _UNBOUND:
                vars(module).pop("db", None)
            else:
                module.db = previous
    testcase.addCleanup(_restore)


def bind_test_db(db_path: str, fresh: bool = True):
    """Builds an isolated Database at `db_path` and binds it everywhere."""
    from trainmate.db import Database

    if fresh and os.path.exists(db_path):
        os.remove(db_path)
    test_db = Database(db_path=db_path)
    rebind_test_db(test_db)
    return test_db


def run_cli(args: list, input_value: str = "n"):
    """Invoke trainmate_cli.main() and capture stdout/stderr."""
    import trainmate_cli
    from trainmate import runtime

    # The renderer is a cached singleton, so a test that patches TRAINMATE_RENDER after
    # one was built would otherwise get the wrong voice — silently, in the direction
    # that still passes (DESIGN_render_persona.md §6). Each run builds its own, exactly
    # as a real CLI process does.
    runtime.reset("render")

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with patch.object(sys, "argv", ["trainmate_cli.py"] + args):
        with (
            patch("sys.stdout", stdout_buf),
            patch("sys.stderr", stderr_buf),
            patch("builtins.input", return_value=input_value),
        ):
            try:
                trainmate_cli.main()
                exit_code = 0
            except SystemExit as e:
                exit_code = e.code
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


def save_workout(
    db, date, sport_type, title, description="",
    duration_minutes=None, rpe=None, tss=None,
    modification_reason=None, adaptation_summary=None,
    source=None, adapted_at=None, macrocycle_id=None,
    benchmark_type=None, clear_benchmark=False,
    removed=False, removed_reason=None,
    original_description=None, original_date=None,
    original_duration_minutes=None, original_tss=None, original_rpe=None,
    planned_zone_currency=None, planned_zone_sec=None,
    google_event_id=None, pushed_signature="",
    kind=None,
):
    """Writes one session through the change handle, the way a command would.

    A fixture builder, not an app path: most tests need a plan to exist before they
    exercise something else, and this keeps that setup one line long. It speaks the old
    `save_workout` argument list, because that is what those fixtures say, and translates
    it onto the revision model (DESIGN_workout_revisions.md §6):

    * `kind` defaults to what the arguments imply — `add` for a manual session, `adapt`
      when a reason or a batch summary says the session was revised, else `generate`.
    * `original_*` describe a session that has since been walked down, so they are written
      as a real FIRST revision under `generate`, and the arguments proper as the revision
      after it. That is how the lineage derives them now.
    * `removed=True` appends the void `workout rm` would.
    * `google_event_id` lands in `workout_calendar_state`, keyed by the lineage (§8). The
      signature defaults to one that matches nothing, so the session reads `[STALE]` —
      what an event handle with no recorded push always meant.

    Returns the session's lineage id — the id `workout list` prints.
    """
    if kind is None:
        if source == 'manual':
            kind = 'add'
        elif adapted_at or adaptation_summary or modification_reason:
            kind = 'adapt'
        else:
            kind = 'generate'

    originals = {
        'description': original_description,
        'duration_minutes': original_duration_minutes,
        'tss': original_tss,
        'rpe': original_rpe,
    }
    lineage = None
    if original_date or any(v is not None for v in originals.values()):
        with db.workout_change(kind='generate') as change:
            change.append(
                date=original_date or date, sport_type=sport_type, title=title,
                description=originals['description'] or description,
                duration_minutes=(
                    originals['duration_minutes']
                    if originals['duration_minutes'] is not None else duration_minutes
                ),
                rpe=originals['rpe'] if originals['rpe'] is not None else rpe,
                tss=originals['tss'] if originals['tss'] is not None else tss,
                macrocycle_id=macrocycle_id,
            )
        seeded = db.get_workout(original_date or date, sport_type)
        lineage = seeded['id'] if seeded else None

    with db.workout_change(kind=kind, summary=adaptation_summary) as change:
        change.append(
            date=date, sport_type=sport_type, title=title, description=description,
            duration_minutes=duration_minutes, rpe=rpe, tss=tss,
            reason=modification_reason, benchmark_type=benchmark_type,
            clear_benchmark=clear_benchmark, macrocycle_id=macrocycle_id,
            planned_zone_currency=planned_zone_currency,
            planned_zone_sec=planned_zone_sec,
            lineage_id=lineage if original_date and original_date != date else None,
        )
    if google_event_id:
        seeded = db.get_workout(date, sport_type)
        db.mark_workout_pushed(seeded['id'], google_event_id, pushed_signature)
    if removed:
        with db.workout_change(kind='rm') as change:
            change.void(date=date, sport_type=sport_type, reason=removed_reason)
    saved = db.get_workout(date, sport_type)
    return saved['id'] if saved else None
