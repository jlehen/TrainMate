"""Workout CLI handlers, split by verb group; all re-exported so
``from trainmate.cli.workouts import run_workout_*`` keeps working."""
from trainmate.cli.workouts._helpers import _fmt_ts, _resolve_workout_end_date, _resolve_workout_date_range, _resolve_swap_ops
from trainmate.cli.workouts.generate import (run_workout_adapt, run_workout_generate,
    run_workout_list, run_workout_compare, run_workout_batches, run_workout_rollback)
from trainmate.cli.workouts.edit import run_workout_push, run_workout_rm, run_workout_restore, run_workout_swap, run_workout_add, run_workout_wipe, run_workout_prune_calendar
from trainmate.cli.workouts.parser import add_workout_parser
