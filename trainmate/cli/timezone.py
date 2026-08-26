"""`timezone` command: show and change the zone every date is computed in.

See DESIGN_user_timezone.md §4.
"""
import argparse
import sys

from trainmate.clock import (
    clear_timezone, describe, now, offset_label, set_timezone, stored_at, stored_name,
)
from trainmate.util import aside, bold, cmd, cyan, dim, fmt_timestamp, green, red


def run_timezone_show(args: argparse.Namespace) -> None:
    """Prints the active zone with the local date and time it produces, so the athlete can
    check it against their watch rather than trust a name."""
    print(bold(cyan("=== TIMEZONE ===")))
    moment = now()
    print(f"  Zone:  {describe()}")
    print(f"  Now:   {moment.strftime('%Y-%m-%d %a %H:%M')}  ({offset_label(moment)})")
    if stored_name():
        print(dim(f"  Set:   {fmt_timestamp(stored_at())}"))
        aside(f"\nChange it with {cmd('timezone set <zone>')}, or {cmd('timezone reset')} "
              "to follow the machine again.")
        return
    aside("\nNo timezone stored — dates follow the machine this runs on. Set your own "
          f"with {cmd('timezone set <zone>')}, e.g. {cmd('timezone set Europe/Paris')}.")


def run_timezone_set(args: argparse.Namespace) -> None:
    """Stores the zone, named as an IANA identifier (case-insensitive)."""
    previous_name, previous_label = stored_name(), describe()
    try:
        name = set_timezone(args.zone)
    except ValueError as e:
        print(red(str(e)))
        sys.exit(1)
    moment = now()
    stamp = f"{moment.strftime('%Y-%m-%d %a %H:%M')} ({offset_label(moment)})"
    if name == previous_name:
        print(green(f"Timezone is {name} (unchanged).") + dim(f" It is now {stamp}."))
        return
    print(green(f"Timezone set to {name}")
          + dim(f" (was {previous_label}). It is now {stamp}."))


def run_timezone_reset(args: argparse.Namespace) -> None:
    """Forgets the stored zone so the machine's own rules again."""
    had_zone = clear_timezone()
    if not had_zone:
        print(dim(f"No timezone stored — already following {describe()}."))
        return
    print(green(f"Timezone reset — dates now follow {describe()}."))


def add_timezone_parser(subparsers):
    # timezone command & subparsers — the zone every date is computed in
    # (DESIGN_user_timezone.md §4).
    timezone_parser = subparsers.add_parser(
        "timezone",
        help="Show the timezone dates are computed in, and change it",
        description=(
            "Every date TrainMate computes — today, which workout is due, which day a "
            "Garmin metric belongs to — is read in this timezone. It is stored in the "
            "database and survives restarts; with nothing stored, dates follow the "
            "machine TrainMate runs on."
        )
    )
    # Read-only at the top level, so a bare `timezone` shows rather than printing help
    # (DESIGN_cli_noargs.md §a3).
    timezone_parser.set_defaults(func=run_timezone_show)
    timezone_subparsers = timezone_parser.add_subparsers(
        dest="subcommand", help="Timezone sub-commands"
    )

    # timezone show
    _show_parser = timezone_subparsers.add_parser(
        "show",
        help="Show the active timezone and the local time it gives",
        description="Same as a bare 'timezone'."
    )
    _show_parser.set_defaults(func=run_timezone_show)

    # timezone set
    timezone_set = timezone_subparsers.add_parser(
        "set", aliases=["use"],
        help="Set the timezone dates are computed in",
        description=(
            "Store the timezone, named the IANA way: 'Europe/Paris', 'America/New_York', "
            "'UTC'. Matching ignores case, and a name that matches nothing lists the "
            "zones containing what you typed — so 'timezone set paris' shows you "
            "'Europe/Paris'."
        )
    )
    timezone_set.set_defaults(func=run_timezone_set)
    timezone_set.add_argument(
        "zone", metavar="ZONE",
        help="IANA timezone name (e.g. Europe/Paris), or part of one to search"
    )

    # timezone reset
    _reset_parser = timezone_subparsers.add_parser(
        "reset",
        help="Forget the stored timezone and follow the machine again",
        description=(
            "Delete the stored timezone so dates follow whatever the machine TrainMate "
            "runs on is set to."
        )
    )
    _reset_parser.set_defaults(func=run_timezone_reset)
    return timezone_parser
