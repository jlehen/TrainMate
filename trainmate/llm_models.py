"""Which LLM TrainMate talks to: the config menu, the stored choice, and how they combine.

See DESIGN_model_selection.md. `config.llm_models` is the menu, the `settings.llm_model` row
is the choice, and the display numbers are positions in the menu — never stored, so reordering
the config cannot repoint an existing choice. Writing the choice is `settings set coach-model`
(DESIGN_settings.md); this module reads it. `trainmate.db` and `trainmate.settings` are
imported lazily inside each function so importing this module (and `trainmate.openrouter`
through it) never opens the DB.
"""

from typing import Any, Dict, List, Optional

from trainmate.config import config
from trainmate.util import cmd

LLM_MODEL_SETTING = "llm_model"


def configured_models() -> List[str]:
    """The menu, in display order. Position i is model number i+1."""
    return config.llm_models


def stored_model() -> Optional[str]:
    """The model identifier stored in the database, or None if the config default rules."""
    from trainmate.db import db
    return db.get_setting(LLM_MODEL_SETTING)


def stored_at() -> Optional[str]:
    """UTC ISO instant the stored choice was last written, or None if nothing is stored."""
    from trainmate.db import db
    row = db.get_setting_row(LLM_MODEL_SETTING)
    return row["updated_at"] if row else None


def active_model() -> str:
    """The model to query: the stored choice if there is one, else the config default. The
    per-invocation `--llm-model` override sits above both and is applied by the client."""
    # Imported here, not at module scope: trainmate.settings imports this module for the
    # menu validator, and it owns the one stored-then-config resolution (DESIGN_settings.md §3).
    from trainmate import settings
    return settings.value(settings.COACH_MODEL)


def active_source() -> str:
    """Where `active_model` came from — 'db' (stored choice) or 'config' (first menu entry)."""
    from trainmate import settings
    return settings.resolve(settings.COACH_MODEL).source


def list_models() -> List[Dict[str, Any]]:
    """The menu as display rows: {number, model, active}. An active model that is no longer in
    the config list is appended with number=None — still used, but it has no menu position to
    show (DESIGN_model_selection.md §3.3)."""
    active = active_model()
    models = configured_models()
    rows: List[Dict[str, Any]] = [
        {"number": i, "model": model, "active": model == active}
        for i, model in enumerate(models, start=1)
    ]
    if active not in models:
        rows.append({"number": None, "model": active, "active": True})
    return rows


def resolve_token(token: str) -> str:
    """Maps a model token — a menu number or a full identifier — to a model id.

    The validator behind `settings set coach-model` and `settings set router-model`, and the
    reader for `llm.router_model` in config.yaml, so it takes whatever YAML produced. Raises
    ValueError with a ready-to-print message if the token names nothing on the menu. Off-menu
    identifiers are refused on purpose: the menu is the one allowlist both model roles pick
    from, and `--llm-model` is the escape hatch for a one-off model you don't want to keep.
    """
    token = str(token).strip()
    models = configured_models()
    if token.isdigit():
        number = int(token)
        if not 1 <= number <= len(models):
            raise ValueError(
                f"No model numbered {number} — the list has {len(models)} "
                f"{'entry' if len(models) == 1 else 'entries'}. Run "
                f"{cmd('settings list coach-model')} to see them."
            )
        return models[number - 1]
    if token in models:
        return token
    raise ValueError(
        f"'{token}' is not in the config list. Run {cmd('settings list coach-model')} to see "
        "the list, or add it to `llm.models` in config.yaml."
    )
