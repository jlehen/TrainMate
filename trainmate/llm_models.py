"""Which LLM TrainMate talks to: the config menu, the stored choice, and how they combine.

See DESIGN_model_selection.md. `config.llm_models` is the menu, the `settings.llm_model` row
is the choice, and the display numbers are positions in the menu — never stored, so reordering
the config cannot repoint an existing choice. `trainmate.db` is imported lazily inside each
function so importing this module (and `trainmate.openrouter` through it) never opens the DB.
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
    return stored_model() or config.default_llm_model


def active_source() -> str:
    """Where `active_model` came from — 'db' (stored choice) or 'config' (first menu entry)."""
    return "db" if stored_model() else "config"


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
    """Maps a `model set` argument — a menu number or a full identifier — to a model id.

    Raises ValueError with a ready-to-print message if it names nothing on the menu. Off-menu
    identifiers are refused on purpose: the menu is the allowlist, and `--llm-model` is the
    escape hatch for a one-off model you don't want to keep.
    """
    token = token.strip()
    models = configured_models()
    if token.isdigit():
        number = int(token)
        if not 1 <= number <= len(models):
            raise ValueError(
                f"No model numbered {number} — the list has {len(models)} "
                f"{'entry' if len(models) == 1 else 'entries'}. Run {cmd('model')} to see them."
            )
        return models[number - 1]
    if token in models:
        return token
    raise ValueError(
        f"'{token}' is not in the config list. Run {cmd('model')} to see the list, or "
        "add it to `llm.models` in config.yaml."
    )


def set_active_model(token: str) -> str:
    """Stores the model `token` names and returns its identifier. Raises ValueError as
    `resolve_token` does; nothing is written when it raises."""
    from trainmate.db import db
    model = resolve_token(token)
    db.set_setting(LLM_MODEL_SETTING, model)
    return model


def clear_active_model() -> bool:
    """Forgets the stored choice so the config default rules again. True if one was stored."""
    from trainmate.db import db
    return db.clear_setting(LLM_MODEL_SETTING)
