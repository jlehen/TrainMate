import io
import os
import sys
from datetime import date
from unittest.mock import patch

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

# Delete children before parents to satisfy foreign-key constraints.
_ALL_TABLES = [
    "mesocycles",
    "macrocycles",
    "workouts",
    "completed_activities",
    "athlete_metrics_cache",
    "athlete_baselines",
    "coach_learnings",
    "analysis_cache",
    "sync_state",
    "daily_context",
    "constraints",
    "benchmark_results",
    "objectives",
    "settings",
]


def clear_all_tables(db) -> None:
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
    """Points every imported handle at `test_db`.

    Call after replacing the Database instance (setUpClass), not only at import: a
    module imported later in the run would otherwise keep the handle it captured.
    """
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
