import io
import sys
from unittest.mock import patch

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
    "lifeevents",
    "objectives",
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
