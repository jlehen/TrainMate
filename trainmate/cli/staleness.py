"""One owner for "this plan was generated from inputs that have since changed".

`plan show` reports it, `plan keep` dismisses it, `plan generate` and `workout generate`
each offer to act on it. Wording and re-stamp live here once, closing design smell B-8
(DESIGN_plan_staleness.md §9). What changed is shown as a diff, and the two questions
carry the coach's read on whether it reshapes the plan (§10).
"""

from typing import Any, Dict, Optional

from trainmate import runtime
from trainmate.util import bold, cmd, gray, green, notice, red, wrap_text, yellow


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


def print_diff(macro: dict, indent: str = "") -> None:
    """The edit itself, old against new, so the athlete judges the change rather than
    its field name (§10). Silent when there is nothing field-level to show."""
    diff = runtime.coach_service.profile_diff(macro)
    if not diff:
        return
    print()
    for line in diff.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            print(indent + green(line))
        elif line.startswith('-') and not line.startswith('---'):
            print(indent + red(line))
        elif line.startswith('@@'):
            print(indent + gray(line))
        else:
            print(indent + line)
    print()


def verdict_line(verdict: Optional[Dict[str, Any]]) -> Optional[str]:
    """The coach's read as one line, or None when there was none to report."""
    if verdict is None:
        return None
    label = "re-shaping" if verdict['reshaping'] else "keep the plan"
    why = f" {verdict['why']}" if verdict['why'] else ""
    return f"{bold('Coach')}: {yellow(label) if verdict['reshaping'] else green(label)}.{why}"


def explain(change_reason: str, macro: dict) -> Optional[bool]:
    """Everything the athlete gets before either question (§10): the fact, the diff, the
    §2 test, then the coach's read on it. Returns True when the coach calls it
    re-shaping, False for keep, None when no verdict could be had."""
    print(wrap_text(
        f"A plan-shaping input has changed since the last plan generation "
        f"({change_reason})."
    ))
    print_diff(macro)
    print(wrap_text(guidance()))
    verdict = runtime.coach_service.plan_reshape_verdict(macro, change_reason)
    line = verdict_line(verdict)
    if line:
        print()
        print(wrap_text(line))
    print()
    return None if verdict is None else verdict['reshaping']


def confirm_regenerate(change_reason: str, macro: dict) -> bool:
    """The generate-path question. The default follows the coach's read: a coach that
    says "re-shaping" and an app that defaults to No would be two answers (§10)."""
    reshaping = explain(change_reason, macro)
    return runtime.prompt.confirm(
        "Would you like to regenerate the periodization strategy?",
        default=bool(reshaping),
    )


def report(change_reason: str, macro: dict) -> None:
    """The read-only block `plan show` prints under the inputs it contradicts (§9). Shows
    the diff but asks the coach nothing: a read-only command makes no network call."""
    # The reason is itself a sentence ("athlete profile changed: gender, preferences"),
    # so it gets its own line rather than a colon that already has one.
    notice("\n! An input has changed since this plan was generated:")
    notice(f"    {change_reason}")
    print_diff(macro, indent="    ")
    print(wrap_text(f"  {guidance()}"))
    print()
    print(f"  {cmd('plan generate', quote=False)}   {gray('rebuild the periodization')}")
    print(f"  {cmd('plan keep', quote=False)}       "
          f"{gray('keep this plan, stop flagging')}")
    print()
