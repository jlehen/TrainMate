"""The process-wide singletons, in one place.

Before this module there were four conventions for reaching the same handle — an
import-bound ``db``, a late package attribute, an optional ``dbh=`` parameter, and
``trainmate_cli.db`` — so whether a replacement "took" depended on which idiom the code
under test happened to use, and several modules imported a name only to be patched
through (a decoy that succeeded and did nothing). Everything now reads
``runtime.<name>`` at use time, which makes one assignment authoritative for the whole
process.

Two properties matter and both come from resolving lazily, in ``__getattr__``:

* **No import-time side effects.** Building ``db`` runs the schema migrations and
  writes to the file, so constructing it merely because something imported a module is
  a surprise — ``--help`` should not touch the database. Nothing is built until first
  use.
* **No import cycles.** ``coach`` needs ``prompt``; ``prompt`` lives here; this module
  needs ``coach``. Deferring every import into the accessor breaks the knot, which is
  what the ``sys.modules`` self-alias in trainmate_cli.py and the package self-imports
  in ``garmin`` were each working around.

Assigning a name (``runtime.db = fake``) shadows the accessor permanently for that
process, which is how tests install their own handles — see tests/helpers.py.
"""
from typing import Any

# Name -> how to build it. Every import is deferred into the thunk; see the module
# docstring for why that is load-bearing rather than stylistic.
_BUILDERS = {}


def _builder(name):
    def register(fn):
        _BUILDERS[name] = fn
        return fn
    return register


@_builder("config")
def _build_config():
    from trainmate.config import config
    return config


@_builder("db")
def _build_db():
    """The database handle, with the Calendar reconcile attached.

    Attaching it here is what makes DESIGN_workout_revisions.md §8 unforgettable: every
    workout change closes by reconciling the lineages it touched, and this is the one
    place the running app builds a database, so no command can be missing the pass. A
    handle built directly (an isolated unit test) has no hook and writes nothing to
    Calendar.
    """
    from trainmate.db import Database
    from trainmate.calendar_reconcile import reconcile
    return Database(calendar_hook=reconcile)


@_builder("garmin")
def _build_garmin():
    from trainmate import garmin
    return garmin


@_builder("calendar_syncer")
def _build_calendar_syncer():
    from trainmate.google_calendar import calendar_syncer
    return calendar_syncer


@_builder("coach_service")
def _build_coach_service():
    from trainmate.coach import coach_service
    return coach_service


@_builder("prompt")
def _build_prompt():
    """The active prompt transport: TtyPrompt on a terminal, JsonPrompt under the bot,
    chosen from TRAINMATE_FRONTEND (trainmate.prompt.make_prompt)."""
    from trainmate.prompt import make_prompt
    return make_prompt()


@_builder("render")
def _build_render():
    """The active voice: ExpertRenderer on a terminal and in the operator's chat,
    CompanionRenderer under the simple bot, chosen from TRAINMATE_RENDER
    (trainmate.cli.render.make_renderer).

    Transport and voice are two axes, so they stay two objects: expert-over-Telegram is
    the operator's own daily surface (DESIGN_render_persona.md §2)."""
    from trainmate.cli.render import make_renderer
    return make_renderer()


def __getattr__(name: str) -> Any:
    """Builds a singleton on first access and caches it as a real module attribute.

    Caching into globals() means later reads skip this function entirely, so the cost
    is paid once and an assignment from a test still wins.
    """
    builder = _BUILDERS.get(name)
    if builder is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = builder()
    globals()[name] = value
    return value


def reset(*names: str) -> None:
    """Drops cached singletons so the next access rebuilds them.

    Used by tests that need a genuinely fresh handle; with no arguments, drops all.
    """
    for name in names or tuple(_BUILDERS):
        globals().pop(name, None)


__all__ = ["reset"] + sorted(_BUILDERS)
