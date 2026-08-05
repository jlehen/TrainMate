import io
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
]


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
