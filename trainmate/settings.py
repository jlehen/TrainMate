"""The athlete's preferences: which knobs change at runtime, and how the config file and
the database combine to answer each one.

See DESIGN_settings.md. `config.yaml` carries the install default, the `settings` table
carries the athlete's override, and `resolve()` is the one place the two are combined.
Each knob's storage key stays owned by the module that reads it
(`clock.TIMEZONE_SETTING`, `llm_models.LLM_MODEL_SETTING`); this module owns the
athlete-facing name, the validator and the resolution order. `trainmate.db` is imported
lazily inside each function, so importing this module never opens the database.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from trainmate import clock, llm_models
from trainmate.config import config
from trainmate.util import cmd

# Athlete-facing names — the vocabulary `settings set` accepts and every caller passes.
COACH_MODEL = "coach-model"
ROUTER_MODEL = "router-model"
TIMEZONE = "timezone"
PUSH = "push"
MORNING_TIME = "morning-time"
MORNING_DEADLINE = "morning-deadline"
ADAPT_FIRST = "adapt-first"


def parse_hhmm(token: Any) -> str:
    """A local time as 'HH:MM', zero-padded. Raises ValueError with a printable message."""
    raw = str(token if token is not None else "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        raise ValueError(f"'{raw}' is not a time — write it as HH:MM, e.g. 07:30.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"'{raw}' is not a time — hours run 00-23 and minutes 00-59.")
    return f"{hour:02d}:{minute:02d}"


def parse_switch(token: Any) -> str:
    """An on/off knob, stored as the word itself so a stored value reads as it displays."""
    raw = str(token if token is not None else "").strip().lower()
    if raw in ("on", "true", "yes", "y", "1", "enabled"):
        return "on"
    if raw in ("off", "false", "no", "n", "0", "disabled"):
        return "off"
    raise ValueError(f"'{raw}' is not on or off — say 'on' or 'off'.")


def _is_on(stored: Optional[str]) -> bool:
    return stored == "on"


@dataclass(frozen=True)
class Setting:
    """One preference: what it is called, where it is stored, and how a value is read.

    `parse` maps a typed token to the stored form and raises ValueError with a
    ready-to-print message; `fallback` is the built-in default, None when "unset" is
    itself a meaningful state (§3). `on_change` drops whatever cache the new value
    invalidates. A knob config.yaml can seed names its key in `config_path`; one whose
    default is not a plain key (the model menu's first entry) supplies `config_default`.
    """
    name: str
    key: str
    group: str
    summary: str
    value_hint: str
    parse: Callable[[Any], str]
    config_path: Optional[Tuple[str, ...]] = None
    config_default: Optional[Callable[[], Optional[str]]] = None
    config_hint: Optional[str] = None
    fallback: Optional[str] = None
    unset_label: Optional[str] = None
    coerce: Callable[[Optional[str]], Any] = lambda stored: stored
    on_change: Optional[Callable[[], None]] = None

    def from_config(self) -> Optional[str]:
        """What config.yaml says, in stored form, or None when it says nothing.

        The file's value goes through the same parser a typed one does, so an unquoted
        `07:30` — a sexagesimal integer to PyYAML — is caught rather than obeyed (§3)."""
        if self.config_default:
            return self.config_default()
        if not self.config_path:
            return None
        raw = config.raw(*self.config_path)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None  # a key emptied out says nothing, the same as an absent one
        return self.parse(raw)

    @property
    def config_label(self) -> Optional[str]:
        """The config.yaml key behind this setting, named for the detail view. None when
        the file has no say in it."""
        if self.config_hint:
            return self.config_hint
        return ".".join(self.config_path) if self.config_path else None


def _reset_model_cache() -> None:
    """Drops the model the OpenRouter client resolved on first use — one process serves
    many commands in `tm shell` and in the bot (DESIGN_model_selection.md §3.1)."""
    from trainmate.openrouter import openrouter_client
    openrouter_client.reset_model()


# Registration order is listing order; the group heading breaks it up on screen.
SETTINGS: List[Setting] = [
    Setting(
        name=COACH_MODEL,
        key=llm_models.LLM_MODEL_SETTING,
        group="Coach",
        summary="Model the coach reasons with",
        value_hint="NUMBER|ID",
        parse=llm_models.resolve_token,
        # Not a plain key: the menu's first entry is the default (DESIGN_model_selection.md §1).
        config_default=lambda: config.default_llm_model,
        config_hint="llm.models (first entry)",
        on_change=_reset_model_cache,
    ),
    Setting(
        name=ROUTER_MODEL,
        key="router_llm_model",
        group="Coach",
        summary="Cheaper model that reads free-text messages in simple chat mode",
        value_hint="NUMBER|ID",
        # Off-menu identifiers are refused here too: `llm.models` is the one allowlist
        # both model roles pick from (§4).
        parse=llm_models.resolve_token,
        config_path=("llm", "router_model"),
        unset_label=f"(follows {COACH_MODEL})",
    ),
    Setting(
        name=TIMEZONE,
        key=clock.TIMEZONE_SETTING,
        group="Clock",
        summary="Timezone every date is computed in",
        value_hint="ZONE",
        parse=clock.resolve,
        # No config key: a zone is the athlete's, not the install's
        # (DESIGN_user_timezone.md §3).
        unset_label="(this machine)",
        on_change=clock.reset_cache,
    ),
    Setting(
        name=PUSH,
        key="push_enabled",
        group="Morning push",
        summary="Whether the bot opens the day by itself",
        value_hint="on|off",
        parse=parse_switch,
        config_path=("telegram", "push", "enabled"),
        fallback="on",
        coerce=_is_on,
    ),
    Setting(
        name=MORNING_TIME,
        key="push_morning_time",
        group="Morning push",
        summary="Local time the morning push fires",
        value_hint="HH:MM",
        parse=parse_hhmm,
        config_path=("telegram", "push", "morning_time"),
        fallback="08:00",
    ),
    Setting(
        name=MORNING_DEADLINE,
        key="push_morning_deadline",
        group="Morning push",
        summary="Local time after which a missed push is skipped rather than caught up",
        value_hint="HH:MM",
        parse=parse_hhmm,
        config_path=("telegram", "push", "morning_deadline"),
        fallback="15:00",
    ),
    Setting(
        name=ADAPT_FIRST,
        key="push_adapt_first",
        group="Morning push",
        summary="Run the daily adaptation before the push (an LLM call every morning)",
        value_hint="on|off",
        parse=parse_switch,
        config_path=("telegram", "push", "adapt_first"),
        fallback="off",
        coerce=_is_on,
    ),
]


@dataclass(frozen=True)
class Resolved:
    """What a setting currently answers, and why: `source` is 'db' (the athlete set it),
    'config' (config.yaml) or 'default' (built in). `config_error` carries the complaint
    when config.yaml holds a value this setting cannot read — the listing shows it
    instead of every command that asks failing (§3)."""
    value: Optional[str]
    source: str
    set_at: Optional[str] = None
    config_error: Optional[str] = None


def names() -> List[str]:
    """Every setting name, in listing order."""
    return [setting.name for setting in SETTINGS]


def get(name: str) -> Setting:
    """The setting `name` addresses — exactly, or by an unambiguous prefix, the way every
    command level resolves (DESIGN_cli_noargs.md §d). Raises ValueError with a printable
    message when it names nothing, or several things."""
    token = (name or "").strip().lower()
    if not token:
        raise ValueError(f"Name a setting — {cmd('settings')} lists them.")
    for setting in SETTINGS:
        if setting.name == token:
            return setting
    near = [setting for setting in SETTINGS if setting.name.startswith(token)]
    if len(near) == 1:
        return near[0]
    if not near:
        raise ValueError(f"'{name}' is not a setting — {cmd('settings')} lists them.")
    listing = ", ".join(setting.name for setting in near)
    raise ValueError(f"'{name}' matches several settings: {listing}.")


def stored(name: str) -> Optional[str]:
    """The raw stored value, or None when the athlete never set this one."""
    from trainmate.db import db
    return db.get_setting(get(name).key)


def resolve(name: str) -> Resolved:
    """Combines the stored row, the config file and the built-in default (§3)."""
    from trainmate.db import db
    setting = get(name)
    row = db.get_setting_row(setting.key)
    if row:
        return Resolved(value=row["value"], source="db", set_at=row["updated_at"])
    try:
        from_config = setting.from_config()
    except ValueError as e:
        return Resolved(value=setting.fallback, source="default", config_error=str(e))
    if from_config is not None:
        return Resolved(value=from_config, source="config")
    return Resolved(value=setting.fallback, source="default")


def value(name: str) -> Any:
    """The effective value, as callers use it: a bool for an on/off knob, the string
    otherwise, and None for a setting whose 'unset' is itself meaningful (§3)."""
    setting = get(name)
    resolved = resolve(name)
    return None if resolved.value is None else setting.coerce(resolved.value)


def write(name: str, token: Any) -> str:
    """Validates `token`, stores it, and drops whatever cache it invalidates. Returns the
    stored form. Raises ValueError as the setting's parser does, having written nothing."""
    from trainmate.db import db
    setting = get(name)
    parsed = setting.parse(token)
    db.set_setting(setting.key, parsed)
    if setting.on_change:
        setting.on_change()
    return parsed


def clear(name: str) -> bool:
    """Forgets the stored value so config.yaml (or the built-in default) rules again.
    True when a row was actually removed."""
    from trainmate.db import db
    setting = get(name)
    cleared = db.clear_setting(setting.key)
    if setting.on_change:
        setting.on_change()
    return cleared


# --- Named readers, one per knob with no home module of its own ---
# `coach-model` and `timezone` are read through llm_models/clock, which own the rest of
# their behaviour; the morning-push knobs and the router role have no such module.

def push_enabled() -> bool:
    """Whether the bot sends the morning push (DESIGN_bot_simple_frontend.md §4.3)."""
    return value(PUSH)


def morning_time() -> str:
    """Local HH:MM the morning push fires (DESIGN_bot_simple_frontend.md §4.3)."""
    return value(MORNING_TIME)


def morning_deadline() -> str:
    """Local HH:MM after which a missed push is skipped for the day (§4.3)."""
    return value(MORNING_DEADLINE)


def adapt_first() -> bool:
    """Whether `bot morning` adapts before rendering (DESIGN_bot_simple_frontend.md §4.2)."""
    return value(ADAPT_FIRST)


def router_model() -> Optional[str]:
    """The model `bot route` classifies with, or None to follow the coaching model
    (DESIGN_bot_simple_frontend.md §5.4)."""
    return value(ROUTER_MODEL)
