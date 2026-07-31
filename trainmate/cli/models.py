import argparse
import sys
from datetime import datetime, timezone

from trainmate.llm_models import (
    active_source, clear_active_model, configured_models, list_models, set_active_model,
    stored_at,
)
from trainmate.util import bold, cyan, dim, green, red


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


def _active_note() -> str:
    """Why the active model is active — the annotation trailing the marked list row."""
    if active_source() == "config":
        return "active (config default)"
    when = _since(stored_at() or "")
    return f"active (set {when})" if when else "active"


def run_model_list(args: argparse.Namespace) -> None:
    """Prints the configured models, numbered, with the active one marked."""
    print(bold(cyan("=== LLM MODELS ===")))
    for row in list_models():
        marker = green("*") if row["active"] else " "
        number = f"{row['number']:>2} " if row["number"] is not None else " - "
        note = ""
        if row["active"]:
            note = "  " + green(_active_note())
            if row["number"] is None:
                note += dim("  not in config list — `model set N` to move off it")
        print(f"{marker} {number} {row['model']}{note}")

    override = getattr(args, "llm_model", None)
    if override:
        print(dim(f"\nOverridden for this run only by --llm-model: {override}"))
    print(dim("\nPick one with `model set <number>`; edit the list in config.yaml (llm.models)."))


def run_model_set(args: argparse.Namespace) -> None:
    """Stores the chosen model, addressed by list number or full identifier."""
    from trainmate.llm_models import active_model
    from trainmate.openrouter import openrouter_client
    previous = active_model()
    try:
        model = set_active_model(args.model)
    except ValueError as e:
        print(red(str(e)))
        sys.exit(1)
    openrouter_client.reset_model()
    if model == previous:
        print(green(f"Model is {model} (unchanged)."))
        return
    print(green(f"Model set to {model}") + dim(f" (was {previous})."))


def run_model_reset(args: argparse.Namespace) -> None:
    """Forgets the stored choice so the first config entry rules again."""
    from trainmate.openrouter import openrouter_client
    had_choice = clear_active_model()
    openrouter_client.reset_model()
    default = configured_models()[0]
    if not had_choice:
        print(dim(f"No stored choice — already on the config default: {default}."))
        return
    print(green(f"Model reset to the config default: {default}."))


def add_model_parser(subparsers):
    # model command & subparsers — pick the LLM from the config list
    # (DESIGN_model_selection.md §4).
    model_parser = subparsers.add_parser(
        "model",
        help="List the configured LLM models and choose the one to use",
        description=(
            "Show the models listed under 'llm.models' in config.yaml, numbered, with the "
            "active one marked, and switch between them. The choice is stored in the "
            "database and survives restarts; '--llm-model' still overrides it for a single "
            "invocation without storing anything."
        )
    )
    model_subparsers = model_parser.add_subparsers(
        dest="subcommand", help="Model sub-commands"
    )

    # model list
    model_subparsers.add_parser(
        "list",
        help="List the configured models, active one marked",
        description="Numbered list of the configured models. Same as a bare 'model'."
    )

    # model set
    model_set = model_subparsers.add_parser(
        "set", aliases=["use"],
        help="Choose the model to use, by list number or identifier",
        description=(
            "Store the model to use for every subsequent command. Address it by its number "
            "in 'model list' or by its full OpenRouter identifier."
        )
    )
    model_set.add_argument(
        "model", metavar="NUMBER|ID",
        help="List number (e.g. 3) or full identifier (e.g. openai/gpt-5.5)"
    )

    # model reset
    model_subparsers.add_parser(
        "reset",
        help="Forget the stored choice and fall back to the config default",
        description=(
            "Delete the stored choice so the first entry of 'llm.models' is used again."
        )
    )
    return model_parser
