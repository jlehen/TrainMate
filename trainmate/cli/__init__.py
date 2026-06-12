"""TrainMate command-line interface, split into per-command-family modules.

Handlers live here; the top-level ``trainmate_cli`` module wires them into the
argparse dispatcher and owns the patchable singletons (db, garmin,
calendar_syncer, coach_service) the handlers reference via ``cli.<name>``.
"""
