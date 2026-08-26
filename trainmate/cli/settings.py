"""`settings` command: show the athlete's preferences and change them.

See DESIGN_settings.md §4. `trainmate/settings.py` owns what a setting is and how it
resolves; this file only renders it and parses the command line.
"""
import argparse
import sys
from datetime import datetime, timezone

from trainmate import clock, llm_models, settings
from trainmate.util import (
    aside, bold, cmd, cyan, dim, fmt_timestamp, green, pad_visible, red, visible_len,
    yellow,
)


def _since(iso_utc: str) -> str:
    """Compact 'today'/'Nd ago' for a UTC ISO instant, or '' if unparseable."""
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso_utc)
    except (ValueError, TypeError):
        return ""
    days = delta.days
    if days < 0:
        return ""
    return "today" if days == 0 else f"{days}d ago"


def _source_note(resolved: settings.Resolved) -> str:
    """Why the effective value is what it is — the annotation trailing each row."""
    if resolved.source == "config":
        return "config.yaml"
    if resolved.source == "default":
        return "default"
    when = _since(resolved.set_at or "")
    return f"set {when}" if when else "set"


def _shown_value(setting: settings.Setting, resolved: settings.Resolved) -> str:
    """The value as it reads on screen. A setting whose 'unset' is meaningful says what
    unset *means* rather than showing a blank (DESIGN_settings.md §3)."""
    if resolved.value is None:
        return setting.unset_label or "(unset)"
    return resolved.value


def run_settings_list(args: argparse.Namespace) -> None:
    """Prints every preference with its value and where that value came from; with a
    NAME, prints that one in detail instead."""
    name = getattr(args, "name", None)
    if name:
        _print_detail(name)
        return

    rows = [(setting, settings.resolve(setting.name)) for setting in settings.SETTINGS]
    name_width = max(len(setting.name) for setting, _ in rows)
    value_width = max(visible_len(_shown_value(setting, resolved))
                      for setting, resolved in rows)

    print(bold(cyan("=== SETTINGS ===")))
    group = None
    for setting, resolved in rows:
        if setting.group != group:
            group = setting.group
            print(f"\n{bold(group)}")
        marker = green("*") if resolved.source == "db" else " "
        shown = _shown_value(setting, resolved)
        if resolved.value is None:
            shown = dim(shown)
        print(f"  {marker} {pad_visible(setting.name, name_width)}  "
              f"{pad_visible(shown, value_width)}  {dim(_source_note(resolved))}")

    ignored = [(setting, resolved.config_error) for setting, resolved in rows
               if resolved.config_error]
    if ignored:
        print(yellow("\nIgnored in config.yaml:"))
        for setting, problem in ignored:
            print(yellow(f"  {setting.name}: {problem}"))

    aside(f"\nChange one with {cmd('settings set <name> <value>')}, or "
          f"{cmd('settings reset <name>')} to fall back to config.yaml. "
          f"{cmd('settings list <name>')} explains one setting.")


def _print_detail(name: str) -> None:
    """One setting: what it does, what answers it today, and what it would fall back to."""
    try:
        setting = settings.get(name)
    except ValueError as e:
        print(red(str(e)))
        sys.exit(1)

    resolved = settings.resolve(setting.name)
    print(bold(cyan(f"=== {setting.name.upper()} ===")))
    print(f"  {setting.summary}.")
    print(f"\n  Value:    {_shown_value(setting, resolved)}"
          + dim(f"   {_source_note(resolved)}"))
    if setting.config_label:
        print(f"  Config:   {_config_line(setting)}" + dim(f"   {setting.config_label}"))
    if setting.fallback:
        print(f"  Default:  {setting.fallback}")

    extra = DETAIL_EXTRAS.get(setting.name)
    if extra:
        extra()

    aside(f"\nChange it with {cmd(f'settings set {setting.name} <{setting.value_hint}>')}"
          + (f", or {cmd(f'settings reset {setting.name}')} to forget it."
             if resolved.source == "db" else "."))


def _config_line(setting: settings.Setting) -> str:
    """What config.yaml contributes: its value, 'not set', or why it was ignored."""
    try:
        from_config = setting.from_config()
    except ValueError as e:
        return yellow(f"ignored — {e}")
    return from_config if from_config is not None else dim("not set")


def _detail_coach_model() -> None:
    """The numbered menu `settings set coach-model <n>` picks from
    (DESIGN_model_selection.md §4.1)."""
    print()
    router = settings.router_model()
    for row in llm_models.list_models():
        marker = green("*") if row["active"] else " "
        number = f"{row['number']:>2} " if row["number"] is not None else " - "
        roles = []
        if row["active"]:
            roles.append(green("coach"))
            if row["number"] is None:
                roles.append(dim("not in config list"))
        if router and row["model"] == router:
            roles.append(cyan("router"))
        annotation = "  " + "  ".join(roles) if roles else ""
        print(f"  {marker} {number} {row['model']}{annotation}")


def _detail_timezone() -> None:
    """The local date and time the zone produces, so it can be checked against a watch
    rather than trusted by name (DESIGN_user_timezone.md §4)."""
    moment = clock.now()
    print(f"  Now:      {moment.strftime('%Y-%m-%d %a %H:%M')}"
          + dim(f"   {clock.offset_label(moment)}"))
    if clock.stored_name():
        print(dim(f"  Set:      {fmt_timestamp(clock.stored_at())}"))


# Settings whose detail view carries more than the generic block — a menu to pick from,
# a clock to check. Keyed by name so the registry stays free of display code.
DETAIL_EXTRAS = {
    settings.COACH_MODEL: _detail_coach_model,
    settings.TIMEZONE: _detail_timezone,
}


def run_settings_set(args: argparse.Namespace) -> None:
    """Stores one preference, reporting what it was before."""
    try:
        setting = settings.get(args.name)
        before = _shown_value(setting, settings.resolve(setting.name))
        stored = settings.write(setting.name, args.value)
    except ValueError as e:
        print(red(str(e)))
        sys.exit(1)
    if stored == before:
        print(green(f"{setting.name} is {stored} (unchanged)."))
        return
    print(green(f"{setting.name} set to {stored}") + dim(f" — was {before}."))
    _print_effect(setting)


def run_settings_reset(args: argparse.Namespace) -> None:
    """Forgets one stored preference so config.yaml, or the built-in default, rules."""
    try:
        setting = settings.get(args.name)
        had_value = settings.clear(setting.name)
    except ValueError as e:
        print(red(str(e)))
        sys.exit(1)
    resolved = settings.resolve(setting.name)
    now_reads = f"{_shown_value(setting, resolved)} ({_source_note(resolved)})"
    if not had_value:
        print(dim(f"Nothing stored for {setting.name} — already {now_reads}."))
        return
    print(green(f"{setting.name} reset to {now_reads}."))
    _print_effect(setting)


def _print_effect(setting: settings.Setting) -> None:
    """The one-line consequence of a change that isn't visible from the value alone."""
    if setting.name == settings.TIMEZONE:
        moment = clock.now()
        print(dim(f"It is now {moment.strftime('%Y-%m-%d %a %H:%M')} "
                  f"({clock.offset_label(moment)})."))
    if setting.name in (settings.PUSH, settings.MORNING_TIME, settings.MORNING_DEADLINE,
                        settings.ADAPT_FIRST):
        aside("The running bot picks this up on its next check, within five minutes.")


def add_settings_parser(subparsers):
    # settings command & subparsers — every preference the athlete can change at runtime
    # (DESIGN_settings.md §4).
    settings_parser = subparsers.add_parser(
        "settings",
        help="Show the preferences stored for this athlete, and change them",
        description=(
            "The knobs that can be changed without editing config.yaml: which model the "
            "coach reasons with, the timezone dates are computed in, and when the "
            "Telegram bot opens the day. A value set here is stored in the database and "
            "survives restarts; 'settings reset' drops it so config.yaml rules again. "
            "Credentials, file paths and the coaching thresholds stay in config.yaml."
        )
    )
    # Read-only at the top level, so a bare `settings` lists rather than printing help
    # (DESIGN_cli_noargs.md §a3).
    settings_parser.set_defaults(func=run_settings_list)
    settings_subparsers = settings_parser.add_subparsers(
        dest="subcommand", help="Settings sub-commands"
    )

    # settings list
    settings_list = settings_subparsers.add_parser(
        "list",
        help="List every preference, or explain one",
        description=(
            "With no name, every preference with its value and where that value came "
            "from. With a name, that one setting in detail: what it does, what "
            "config.yaml says, and what it falls back to. Same as a bare 'settings'."
        )
    )
    settings_list.set_defaults(func=run_settings_list)
    settings_list.add_argument(
        "name", metavar="NAME", nargs="?",
        help="Setting to explain (e.g. coach-model); omit for the whole list"
    )

    # settings set
    settings_set = settings_subparsers.add_parser(
        "set", aliases=["use"],
        help="Change one preference",
        description=(
            "Store a preference. The name is the one 'settings' lists, or any "
            "unambiguous prefix of it. A value the setting cannot read is refused and "
            "nothing is written."
        )
    )
    settings_set.set_defaults(func=run_settings_set)
    settings_set.add_argument("name", metavar="NAME", help="Setting to change")
    settings_set.add_argument("value", metavar="VALUE", help="New value")

    # settings reset
    settings_reset = settings_subparsers.add_parser(
        "reset",
        help="Forget one stored preference and fall back to config.yaml",
        description=(
            "Delete the stored value so the config.yaml entry — or the built-in "
            "default, where there is no entry — is used again."
        )
    )
    settings_reset.set_defaults(func=run_settings_reset)
    settings_reset.add_argument("name", metavar="NAME", help="Setting to forget")
    return settings_parser
