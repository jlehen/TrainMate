"""Where "now" comes from: the athlete's timezone and the one clock every date reads.

See DESIGN_user_timezone.md. The `settings.timezone` row holds an IANA zone name and is
written by `tm timezone set`; with no row the machine's own zone rules (§3).
`util.today_date()` imports this module on every date call, so `trainmate.db` is imported
lazily inside each function here — importing it must never open the database.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, available_timezones

TIMEZONE_SETTING = "timezone"

# The resolved zone, or None for "follow the machine". Resolved once per process because
# today_date() asks on every call; `reset_cache` is what `timezone set` calls afterwards.
_UNRESOLVED = object()
_zone: object = _UNRESOLVED


def stored_name() -> Optional[str]:
    """The zone name stored in the database, or None when the machine's zone rules."""
    from trainmate.db import db
    return db.get_setting(TIMEZONE_SETTING)


def stored_at() -> Optional[str]:
    """UTC ISO instant the stored zone was last written, or None if nothing is stored."""
    from trainmate.db import db
    row = db.get_setting_row(TIMEZONE_SETTING)
    return row["updated_at"] if row else None


def reset_cache() -> None:
    """Drops the resolved zone so the next date call re-reads the setting."""
    global _zone
    _zone = _UNRESOLVED


def active_zone() -> Optional[ZoneInfo]:
    """The zone every date is computed in, or None to follow the machine (§3)."""
    global _zone
    if _zone is not _UNRESOLVED:
        return _zone  # type: ignore[return-value]
    try:
        name = stored_name()
    except sqlite3.Error:
        # today_date() sits on every code path, so a database that momentarily cannot be
        # read degrades to the machine's zone for this call rather than taking the command
        # down — and is not cached, so the next call tries again (§3).
        return None
    _zone = _resolve_stored(name)
    return _zone  # type: ignore[return-value]


def _resolve_stored(name: Optional[str]) -> Optional[ZoneInfo]:
    """The stored name as a zone. A name this machine's tzdata does not carry warns and
    falls back rather than aborting every command (§3)."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        from trainmate.util import cmd, yellow
        print(yellow(
            f"Warning: stored timezone '{name}' is unknown on this machine — using the "
            f"machine's own timezone. Set a valid one with {cmd('timezone set <zone>')}."
        ))
        return None


def now() -> datetime:
    """The current instant, aware, in the athlete's zone. The one clock every "what day is
    it" computation reads (util.today_date)."""
    zone = active_zone()
    return datetime.now(zone) if zone else datetime.now().astimezone()


def to_local(dt: datetime) -> datetime:
    """Moves an instant into the athlete's zone for display. A naive value is read as UTC,
    which is what every stored timestamp column holds (§5)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    zone = active_zone()
    return dt.astimezone(zone) if zone else dt.astimezone()


def offset_label(dt: Optional[datetime] = None) -> str:
    """The zone's UTC offset at `dt` (default: now) as 'UTC+02:00'."""
    moment = dt or now()
    raw = moment.strftime("%z") or "+0000"
    return f"UTC{raw[:3]}:{raw[3:5]}"


def describe() -> str:
    """The active zone as one display string: the stored name, or what the machine says
    when nothing is stored — no IANA name is invented for the machine's zone (§3)."""
    zone = active_zone()
    if zone is not None:
        return str(zone)
    local = datetime.now().astimezone()
    return f"the machine's timezone ({local.tzname()}, {offset_label(local)})"


def resolve(token: str) -> str:
    """Maps a `timezone set` argument to a canonical IANA zone name.

    Matching is case-insensitive, so 'europe/paris' lands on 'Europe/Paris'. Raises
    ValueError with a ready-to-print message — naming a city or a region lists the zones
    that contain it, which is how an athlete finds their own name (§4)."""
    name = (token or "").strip()
    if not name:
        raise ValueError("Name a timezone, e.g. 'Europe/Paris'.")
    zones = available_timezones()
    if name in zones:
        return name
    by_lower = {zone.lower(): zone for zone in zones}
    if name.lower() in by_lower:
        return by_lower[name.lower()]
    near = sorted(zone for zone in zones if name.lower() in zone.lower())
    if not near:
        raise ValueError(
            f"'{name}' is not a known timezone. Names look like 'Europe/Paris' or "
            "'America/New_York'; search with part of a city or region name."
        )
    shown = near[:12]
    listing = "\n".join(f"  {zone}" for zone in shown)
    more = f"\n  ... and {len(near) - len(shown)} more" if len(near) > len(shown) else ""
    raise ValueError(f"'{name}' is not a timezone name. Did you mean:\n{listing}{more}")


def set_timezone(token: str) -> str:
    """Stores the zone `token` names and returns its canonical name. Raises ValueError as
    `resolve` does; nothing is written and no cache is dropped when it raises."""
    from trainmate.db import db
    name = resolve(token)
    db.set_setting(TIMEZONE_SETTING, name)
    reset_cache()
    return name


def clear_timezone() -> bool:
    """Forgets the stored zone so the machine's own rules again. True if one was stored."""
    from trainmate.db import db
    cleared = db.clear_setting(TIMEZONE_SETTING)
    reset_cache()
    return cleared
