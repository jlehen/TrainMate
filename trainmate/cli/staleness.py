"""One owner for "this plan was generated from inputs that have since changed".

`plan show` reports it, `plan keep` dismisses it, `plan generate` and `workout generate`
each offer to act on it. Wording and re-stamp live here once, closing design smell B-8
(DESIGN_plan_staleness.md §9).
"""

from typing import Optional

from trainmate import runtime
from trainmate.util import cmd, gray, notice, wrap_text


def reason(macro: dict) -> Optional[str]:
    """Why `macro` is stale, or None when it is still current."""
    return runtime.coach_service.config_changed(macro)


def guidance() -> str:
    """The §2 test said out loud, because `preferences` over-triggers by design (§4)."""
    return (
        "Regenerate only if the change would have altered the block structure, phase "
        "order or volume ramp. Wording, tone or how sessions are described: keep the "
        f"plan — your next {cmd('workout generate')} picks it up anyway."
    )


def stamp(macro: dict) -> None:
    """Records the current inputs against `macro`, so the change stops being flagged."""
    runtime.db.update_macrocycle_config_hash(
        macro['id'],
        runtime.coach_service._get_config_hash(),
        runtime.coach_service._get_config_snapshot(),
        runtime.coach_service._get_profile_snapshot(),
    )


def kept_line() -> str:
    """What every "keep it" route says back, so the three cannot drift apart (§9)."""
    return (
        "Keeping the current periodization strategy. It is now recorded against your "
        "current profile and thresholds, so this change won't be flagged again."
    )


def confirm_regenerate(change_reason: str) -> bool:
    """The generate-path question: the fact, the guidance, then the ask."""
    return runtime.prompt.confirm(wrap_text(
        f"A plan-shaping input has changed since the last plan generation "
        f"({change_reason}).\n\n{guidance()}\n\n"
        "Would you like to regenerate the periodization strategy?"
    ))


def report(change_reason: str) -> None:
    """The read-only block `plan show` prints under the inputs it contradicts (§9)."""
    # The reason is itself a sentence ("athlete profile changed: gender, preferences"),
    # so it gets its own line rather than a colon that already has one.
    notice("\n! An input has changed since this plan was generated:")
    notice(f"    {change_reason}")
    print()
    print(wrap_text(f"  {guidance()}"))
    print()
    print(f"  {cmd('plan generate', quote=False)}   {gray('rebuild the periodization')}")
    print(f"  {cmd('plan keep', quote=False)}       "
          f"{gray('keep this plan, stop flagging')}")
    print()
