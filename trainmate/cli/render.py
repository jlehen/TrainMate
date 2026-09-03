"""The voice a CLI process speaks in (DESIGN_render_persona.md).

TRAINMATE_RENDER picks one renderer at startup and `runtime.render` hands it out, the
way TRAINMATE_FRONTEND picks a prompt transport: a command says *what happened* and
never asks which persona heard it. `CompanionRenderer` extends `ExpertRenderer`, so the
set of methods it overrides IS the list of surfaces companion mode has opted into, and
anything it does not override falls back to the expert form (§2, §3).

The companion voice lives here in full — the pure line builders below, then the
sentences on the class. The expert table renderers stay in the command modules they
belong to and `ExpertRenderer` delegates to them (§7 step 3). Command modules never
import this one: they reach it through `runtime.render`, whose builder defers the
import (§7).
"""
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from trainmate import progression, runtime
from trainmate.coach.proposals import RevisionProposal
from trainmate.config import config
from trainmate.progression import RUNWAY_BLOCK, RUNWAY_PLAN_END_NEXT_GOAL, RUNWAY_SPAN
from trainmate.sports import canonical_sport
from trainmate.prompt import emit_buttons
from trainmate.util import (
    bold, cmd, days_between, dim, fmt_date, green, notice, wrap_text,
    today_date as _today_date, today_str as _today_str,
)
from trainmate.cli.goals import print_goal_row, print_goal_table, report_archived_sessions
from trainmate.cli.plans import print_plan
from trainmate.cli.progress import emit_chart, print_progress_report
from trainmate.cli.runway import (
    crossing_the_end, current_runway, runway_buttons, runway_hint_lines,
)
from trainmate.cli.workouts.generate import (
    print_calendar_marked, print_generate_preview, print_workout_compare,
    print_workout_table,
)
from trainmate.cli.workouts.revisions import (
    print_revision_preview, rewritten_text_only, wording_block_lines, wording_blocks,
)

# --- The companion line builders (DESIGN_bot_simple_frontend.md §6) ---
# Pure functions over rows: the *how* of the companion voice, where the renderer below
# is the *when*. `bot morning` and `bot constraints` are companion-only by definition
# and call them directly, never through a renderer (DESIGN_render_persona.md §3).

REST_DAY_LINE = "Rest day — enjoy it 🎉"

# A session the athlete has already trained, in companion voice. Only `done` and
# `partial` earn a line — DESIGN_bot_simple_frontend.md §6 says why the other verdicts
# say nothing at all. The statuses are a constant because the morning push reads them
# too, and "already trained" has to mean the same thing on both surfaces (§4.1).
SIMPLE_DONE_LINE = "✅ Already done — nice work 💪"
SIMPLE_DONE_STATUSES = ("done", "partial")

# Emoji per canonical sport for the simple session lines, keyed by the names in
# `sports.CANONICAL_SPORTS`; unknown sports get the generic one rather than nothing,
# so a new sport never renders bare. `rest` is in the table so a taper week reads as
# intended rest rather than as a generic session (DESIGN_runway_nudge.md §6).
SPORT_EMOJI = {
    "running": "🏃",
    "cycling": "🚴",
    "hiking": "🥾",
    "strength_training": "🏋️",
    "yoga": "🧘",
    "ski_touring": "🎿",
    "rowing": "🚣",
    "downhill_skiing": "⛷️",
    "rest": "🛌",
}
DEFAULT_SPORT_EMOJI = "🎽"


def sport_emoji(sport_type: Optional[str]) -> str:
    """The emoji standing in for the sport column in simple session lines."""
    return SPORT_EMOJI.get((sport_type or "").strip().lower(), DEFAULT_SPORT_EMOJI)


def simple_session_line(w: Dict[str, Any], lead: Optional[str] = None) -> str:
    """One simple-mode line for a session: '🏃 Today: Easy run — 40 min'.

    `lead` is the day word ('Today', '2026-08-25 Tue'); omitted for a bare line."""
    duration = w.get("duration_minutes")
    duration_str = f" — {duration} min" if duration else ""
    prefix = f"{sport_emoji(w.get('sport_type'))} "
    if lead:
        prefix += f"{lead}: "
    return f"{prefix}{w.get('title') or w.get('sport_type', 'Session')}{duration_str}"


def simple_day_lines(
    workouts: List[Dict[str, Any]], date_str: str,
    verdicts: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[str]:
    """Simple rendering of one day's schedule: session line(s) plus the wrapped
    description (the coach's actual prescription), or the one-line rest message.
    Any empty day gets the rest line, whatever the reason it is empty
    (DESIGN_bot_simple_frontend.md §10).

    `verdicts` is `adherence_verdicts`' map; a session already trained gets the done
    line, and every other verdict renders as it did before (§6 tone rule)."""
    if not workouts:
        return [REST_DAY_LINE]
    day_word = "Today" if date_str == _today_str() else fmt_date(date_str)
    lines: List[str] = []
    for w in workouts:
        lines.append(simple_session_line(w, lead=day_word))
        status = ((verdicts or {}).get(w.get("id")) or {}).get("status")
        if status in SIMPLE_DONE_STATUSES:
            lines.append(SIMPLE_DONE_LINE)
        description = (w.get("description") or "").strip()
        if description:
            lines.append(wrap_text(description))
    return lines


def simple_week_lines(
    workouts: List[Dict[str, Any]],
    verdicts: Optional[Dict[int, Dict[str, Any]]] = None,
    end_note: Optional[str] = None,
) -> List[str]:
    """Simple rendering of a multi-day window: one dated line per session, no
    descriptions, ending on an encouraging count. An empty window is a break, not
    a gap (§6 tone rule).

    `verdicts` is `adherence_verdicts`' map; a session already trained gets a ✅
    instead of its sport emoji, so the listing doubles as her calendar — done behind,
    plan ahead (DESIGN_bot_simple_frontend.md §11).

    `end_note` names the end of the schedule when the window crosses it, the companion
    form of the expert listing's marker (DESIGN_runway_nudge.md §6)."""
    if not workouts:
        # An empty window past the cliff is the schedule having run out, not a break the
        # coach chose — so the note replaces the break line rather than following it (§4).
        return [end_note] if end_note else ["Nothing on the schedule — enjoy the break 🎉"]
    lines = ["🗓 Coming up:"]
    done = 0
    for w in workouts:
        day = datetime.strptime(w["date"], "%Y-%m-%d").strftime("%a %d")
        status = ((verdicts or {}).get(w.get("id")) or {}).get("status")
        if status in SIMPLE_DONE_STATUSES:
            done += 1
            lines.append(f"{day} · ✅ {simple_session_line(w)}")
        else:
            lines.append(f"{day} · {simple_session_line(w)}")
    if end_note:
        lines.append(end_note)
    count = len(workouts)
    session_word = "session" if count == 1 else "sessions"
    if done:
        lines.append(f"\n{done} of {count} {session_word} already done — keep it rolling 💪")
    else:
        lines.append(f"\n{count} {session_word} planned — you've got this 💪")
    return lines


def simple_activity_line(act: Dict[str, Any]) -> str:
    """One completed activity in companion words: '🚴 Morning Ride — 90 min'."""
    name = act.get("activity_name") or act.get("activity_type") or "Activity"
    emoji = sport_emoji(canonical_sport(act.get("activity_type")))
    return f"{emoji} {name} — {simple_activity_minutes(act)} min"


def simple_activity_minutes(act: Dict[str, Any]) -> int:
    return round((act.get("duration_sec") or 0) / 60)


def simple_compare_lines(
    days: List[Tuple[str, list, list]], start: str, end: str, today: str,
) -> List[str]:
    """Simple rendering of a look back: one dated line per planned session and per
    extra activity, in a four-glyph vocabulary that needs no legend — ✅ followed the
    plan, ❌ did not, ➕ an effort the plan did not ask for, ⏳ still ahead today
    (DESIGN_bot_simple_frontend.md §6). A kept rest day is a ✅ like any other session;
    a rest day trained through is a ❌ that says what was done instead.

    `days` is `compare_days`' list; the caller has already dropped the efforts too small
    to mention. The closing count follows the §6 tone rule: what was done leads, the
    gap is a plain number after it, and a window with nothing behind it is not a miss."""
    lines = [f"🔎 Looking back, {simple_span_words(start, end, today)}:"]
    total = done = 0
    for date_str, results, unplanned in days:
        day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d")
        for r in results:
            w = r["planned"]
            act = r["completed"]
            if w["sport_type"] == "rest":
                if act:
                    lines.append(
                        f"{day} · ❌ 🛌 Rest day, but you trained: {simple_activity_line(act)}"
                    )
                elif r.get("pending"):
                    lines.append(f"{day} · 🛌 Rest day")
                else:
                    lines.append(f"{day} · ✅ 🛌 Rest day")
                continue
            if r.get("pending"):
                lines.append(f"{day} · ⏳ {simple_session_line(w)}")
                continue
            total += 1
            if act:
                done += 1
                lines.append(
                    f"{day} · ✅ {simple_session_line(w)} "
                    f"(you did {simple_activity_minutes(act)} min)"
                )
            else:
                lines.append(f"{day} · ❌ {simple_session_line(w)}")
        for act in unplanned:
            lines.append(f"{day} · ➕ {simple_activity_line(act)}, not on the plan")
    if len(lines) == 1:
        return ["Nothing to look back on yet — your sessions are ahead of you 💪"]
    session_word = "session" if total == 1 else "sessions"
    if total == 0:
        lines.append("\nNo sessions were due — rest well 🎉")
    elif done == total:
        lines.append(f"\nAll {total} {session_word} done — brilliant 🎉")
    elif done:
        lines.append(f"\n{done} of {total} {session_word} done — keep it rolling 💪")
    else:
        lines.append(f"\n0 of {total} {session_word} done — the plan is ready when you are 💪")
    return lines


def simple_date_word(date_str: str) -> str:
    """A date in companion words — 'Sat Sep 26' — no ISO form, no year (§6). The
    countdown beside it (`simple_when`) carries the year information a reader needs."""
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %b %d")


def simple_when(date_str: str, today: str) -> str:
    """How far away a date is, in companion words: 'today', 'tomorrow', days inside two
    weeks, then weeks, then months. Rough on purpose — a countdown is a feeling here,
    not a schedule (DESIGN_bot_simple_frontend.md §11)."""
    days = days_between(today, date_str)
    if days < 0:
        return "passed"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days < 14:
        return f"in {days} days"
    if days < 112:
        return f"in {round(days / 7)} weeks"
    return f"in {round(days / 30.4)} months"


def simple_day_word(date_str: str, today: str) -> str:
    """One date as the companion names it: 'today', else the day word (§6)."""
    return "today" if date_str == today else simple_date_word(date_str)


def simple_span_words(start: str, end: str, today: str) -> str:
    """A window in companion words: one day word, or 'Thu Sep 04 to Sun Sep 07'. The
    companion never shows an ISO span, on any surface that renders one (§6)."""
    span = simple_day_word(start, today)
    if end != start:
        span = f"{span} to {simple_day_word(end, today)}"
    return span


def simple_metric_words(metric: str) -> str:
    """A signal category as prose: `disturbed_sleep` reads 'disturbed sleep'. The
    underscore form is the storage key and stays expert detail (§6)."""
    return (metric or "").replace("_", " ").strip()


def simple_constraint_lines(constraints: List[Dict[str, Any]], today: str) -> List[str]:
    """Simple rendering of the directives the coach works around: one bullet per
    constraint, dates as day words, no IDs or tier tags (the expert `constraint list`
    keeps those). Empty reads as a clean slate, not a gap (§6 tone rule)."""
    if not constraints:
        return ["Nothing on the list — no rules to work around right now. "
                "Just tell me when something comes up 💬"]
    lines = ["📌 I'm working around:"]
    for c in constraints:
        bullet = "🛌" if c.get("rest") else "•"
        span = simple_span_words(c["start_date"], c["end_date"], today)
        lines.append(f"{bullet} {c['title']} — {span}")
    return lines


def simple_progress_lines(payload: Dict[str, Any], today: str) -> List[str]:
    """The two-line simple `progress` summary: a fitness-trend sentence (from the
    CTL series, ~28 days back) and a chart legend. Every branch keeps the §6 tone
    rule — a falling CTL reads as freshening up, not as decay."""
    days = payload.get("days") or []
    dated = [(d["date"], d.get("ctl")) for d in days
             if d.get("ctl") is not None and d["date"] <= today]
    trend = "Your training story is just getting started 🌱"
    if dated:
        now_date, now_ctl = dated[-1]
        base_cutoff = (
            datetime.strptime(now_date, "%Y-%m-%d") - timedelta(days=28)
        ).strftime("%Y-%m-%d")
        # Baseline: the newest sample at or before the cutoff; a shorter history
        # falls back to its earliest sample.
        older = [ctl for date, ctl in dated if date <= base_cutoff]
        base = older[-1] if older else dated[0][1]
        if base and base > 0 and len(dated) > 1:
            delta_pct = (now_ctl - base) / base * 100
            if delta_pct > 3:
                trend = f"Fitness is climbing — up {delta_pct:.0f}% this month 📈"
            elif delta_pct < -3:
                trend = "You're freshening up — recent rest is banking energy 🔋"
            else:
                trend = "Fitness is holding steady — consistency is doing its job 👍"
    return [
        trend,
        "The chart shows your fitness building up top, and week-by-week training "
        "below — keep stacking those weeks 💪",
    ]


def simple_goal_line(goal: Dict[str, Any], today: str) -> str:
    """One goal as the companion says it: sport emoji(s), title, day word and countdown.

    Its own function because a capture previews a goal it has not stored yet in exactly
    this wording — which is what makes a wrong `date_type` guess visible in the preview's
    first line rather than in the database (DESIGN_bot_simple_frontend.md §12.5)."""
    date_word = simple_date_word(str(goal["target_date"]))
    when = simple_when(str(goal["target_date"]), today)
    # 'on' a date something happens on; 'by ~' a horizon that only bounds the plan —
    # the same wording rule as the expert view.
    if goal.get("date_type") == "horizon":
        date_part = f"by ~{date_word} ({when})"
    else:
        date_part = f"on {date_word} ({when})"
    emoji = " ".join(sport_emoji(s) for s in str(goal["sport_type"]).split(","))
    return f"{emoji} {goal['title']} — {date_part}"


def simple_goal_lines(goals: List[Dict[str, Any]], today: str) -> List[str]:
    """Simple rendering of the goals view: each goal still ahead with a day word and a
    countdown, then the completed ones as one celebration line. No IDs or state tags
    (the expert `goal list` keeps those); an archived goal was called off and says
    nothing at all (§6 tone rule). Empty reads as an invitation, not a gap."""
    from trainmate.db.objectives import GOAL_COMPLETED, GOAL_UPCOMING, goal_state
    upcoming = [g for g in goals if goal_state(g, today) == GOAL_UPCOMING]
    completed = [g for g in goals if goal_state(g, today) == GOAL_COMPLETED]
    lines: List[str] = []
    if upcoming:
        lines.append("🎯 What you're training for:")
        for g in upcoming:
            lines.append(simple_goal_line(g, today))
            description = (g.get("description") or "").strip()
            if description:
                lines.append(wrap_text(description))
    else:
        lines.append(
            "No goal on the horizon right now — once one is set, your training "
            "will build toward it 🎯"
        )
    if completed:
        count = len(completed)
        goal_word = "goal" if count == 1 else "goals"
        lines.append(f"\n✅ {count} {goal_word} already behind you — nice collection 🏆")
    return lines


def simple_goal_edit_lines(
    goal: Dict[str, Any], changes: Dict[str, Any], today: str
) -> List[str]:
    """A proposed goal edit, drawn from the REAL row beside what it would become
    (DESIGN_bot_simple_frontend.md §12.4).

    The row half is what makes a wrong nomination die visibly: the athlete reads the goal
    the model picked, in the words she knows it by, before anything is written."""
    lines = [f"Your goal {simple_goal_line(goal, today)}"]
    if "title" in changes:
        lines.append(f"→ rename it to “{changes['title']}”")
    if "target_date" in changes:
        lines.append(
            f"→ move it to {simple_date_word(changes['target_date'])} "
            f"({simple_when(changes['target_date'], today)})"
        )
    if "description" in changes:
        lines.append(f"→ note against it: {changes['description']}")
    return lines


def simple_constraint_edit_lines(
    constraint: Dict[str, Any], changes: Dict[str, Any], today: str
) -> List[str]:
    """A proposed change to one of the rules the coach works around (§12.4), rendered
    from the stored row the same way `simple_constraint_lines` renders the list."""
    span = simple_span_words(constraint["start_date"], constraint["end_date"], today)
    lines = [f"Your rule “{constraint['title']}” — {span}"]
    if "title" in changes:
        lines.append(f"→ restate it as “{changes['title']}”")
    if "start_date" in changes or "end_date" in changes:
        start = changes.get("start_date", constraint["start_date"])
        end = changes.get("end_date", constraint["end_date"])
        lines.append(f"→ make it {simple_span_words(start, end, today)}")
    if "description" in changes:
        lines.append(f"→ note against it: {changes['description']}")
    return lines


def simple_plan_shaping_line(impact: Dict[str, Any]) -> str:
    """How big a directive just captured turns out to be, without the commands that would
    escalate it: building it into the plan is `plan generate`, which is operator work, and
    the adjust offer beside this capture is what the athlete can actually do (§12.10)."""
    return (
        f"That's a big one — it covers {impact['days']} days and takes out a good part "
        "of a normal week."
    )


def simple_focus_snippet(text: str, limit: int = 220) -> str:
    """The opening of a mesocycle's focus, for the plan view: the first sentence when
    one ends within `limit` chars, else a word-boundary cut with an ellipsis. The full
    prescription is expert detail (§11); the companion gets the headline."""
    text = " ".join(text.split())
    cut = text.find(". ")
    if 0 <= cut < limit:
        return text[:cut + 1]
    if len(text) <= limit:
        return text
    return text[:text.rfind(" ", 0, limit)] + "…"


def simple_block_window(start: str, end: str) -> str:
    """A training block's span as the plan view's leading column — 'Aug 17 – Sep 06'.
    Weekday and year go, where `simple_date_word` keeps the weekday: a block boundary is
    a week rather than an appointment, and the column has to stay scannable (§11.1)."""
    def month_day(date_str: str) -> str:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%b %d")
    return f"{month_day(start)} – {month_day(end)}"


def simple_block_length(total_days: int) -> str:
    """How long a block runs. Exact-week blocks read in weeks; anything ragged reads in
    days rather than as a rounded lie."""
    if total_days % 7 == 0:
        weeks = total_days // 7
        return "1 week" if weeks == 1 else f"{weeks} weeks"
    return f"{total_days} days"


def simple_plan_lines(
    goal: Dict[str, Any], macrocycle: Dict[str, Any],
    mesocycles: List[Dict[str, Any]], today: str,
) -> List[str]:
    """Simple rendering of one periodization plan: the road to the goal — blocks done,
    the block the athlete is in (with its focus), blocks ahead — closed by the goal day.
    Strategy prose, IDs, feedback and snapshotted inputs stay expert detail (§11).

    Each block leads with its window, then the marker, the way the week and look-back
    views lead with the day: one date column down the left edge (§11.1)."""
    lines = [f"🧭 The road to {goal['title']}:"]
    if macrocycle.get("status") == "superseded":
        lines.append("(an older version of the plan — a newer one has replaced it)")
    if not mesocycles:
        lines.append("No training blocks drawn up yet — check back soon 🌱")
        return lines
    for m in mesocycles:
        start, end = str(m["start_date"]), str(m["end_date"])
        total_days = max(1, days_between(start, end) + 1)
        lead = f"{simple_block_window(start, end)} · "
        # The window already says when and how long, so each tail carries only what it
        # cannot: nothing behind her, how far into the current block, length ahead.
        if end < today:
            lines.append(f"{lead}✅ {m['name']}")
            continue
        if start <= today:
            total_weeks = max(1, -(-total_days // 7))  # ceiling
            week_now = min(total_weeks, days_between(start, today) // 7 + 1)
            lines.append(
                f"{lead}📍 {m['name']} — you're here, week {week_now} of {total_weeks}"
            )
            focus = (m.get("focus") or "").strip()
            if focus:
                lines.append(wrap_text(simple_focus_snippet(focus)))
            continue
        lines.append(f"{lead}⏳ {m['name']} — {simple_block_length(total_days)}")
    date_word = simple_date_word(str(goal["target_date"]))
    when = simple_when(str(goal["target_date"]), today)
    if goal.get("date_type") == "horizon":
        lines.append(f"\n🏁 Building toward ~{date_word} ({when}) — keep stacking 💪")
    else:
        lines.append(f"\n🏁 The big day: {date_word} ({when}) — you've got this 💪")
    return lines


def _is_rest(w: Optional[dict]) -> bool:
    return bool(w) and (w.get('sport_type') or '').lower() == 'rest'


def _simple_was_clause(pw: dict, existing: Optional[dict]) -> str:
    """The parenthetical after a proposed session, saying what it replaces."""
    if not existing:
        return "new"
    if rewritten_text_only(pw, existing):
        return "same session, wording updated"
    if _is_rest(existing):
        return "was a rest day"
    parts = []
    if existing.get('title') != pw.get('title'):
        parts.append(existing['title'])
    if (existing.get('duration_minutes') or 0) != (pw.get('duration_minutes') or 0):
        parts.append(f"{existing.get('duration_minutes') or 0} min")
    return "was " + ", ".join(parts) if parts else "adjusted"


def simple_revision_lines(proposal: RevisionProposal) -> List[str]:
    """The revision as companion prose: one paragraph per touched day, no table and no
    diff signs, so the phone can flow it (DESIGN_bot_simple_frontend.md §6). The
    per-session reason is skipped when it merely repeats the batch reason printed above."""
    today = _today_str()
    entries = []
    for pair in proposal.pairs:
        pw, existing = pair.proposal, pair.original
        day = "Today" if pw['date'] == today else simple_date_word(pw['date'])
        block = [f"{simple_session_line(pw, lead=day)} ({_simple_was_clause(pw, existing)})"]
        why = (pw.get('modification_reason') or '').strip()
        if why and why != (proposal.reason or '').strip():
            block.append(why)
        paragraphs = ["\n".join(block)]
        if rewritten_text_only(pw, existing):
            # A blank line between blocks, so each Was/Now pair reads as one passage.
            paragraphs.extend(
                "\n".join(wording_block_lines(b)) for b in wording_blocks(pw, existing)
            )
        entries.append((pw['date'], "\n\n".join(paragraphs)))
    for ew in proposal.removals:
        day = "Today" if ew['date'] == today else simple_date_word(ew['date'])
        entries.append((ew['date'], f"🗑 {day}: {ew['title']} — dropped"))
    entries.sort(key=lambda e: e[0])
    return [text for _, text in entries]


# What simple mode says wherever the expert view would suggest `plan generate`: the
# athlete in companion mode cannot run it — the operator sets the plan up
# (DESIGN_bot_simple_frontend.md §11).
SIMPLE_NO_PLAN_LINE = (
    "No training plan here yet — it will appear once your goal is set up 🌱"
)

# What the push says instead of the rest-day line once the schedule is exhausted
# (DESIGN_runway_nudge.md §6): an empty day is then the schedule running out, not a
# coaching decision.
SIMPLE_PASSED_LINE = "You've finished everything on the schedule 🎉"

# The one-liner the companion week view ends on when its window crosses the cliff (§6),
# mirroring the expert listing's marker.
SIMPLE_END_NOTE = "That's the end of the current schedule."


def simple_end_note(end_date: Optional[str]) -> Optional[str]:
    """The companion week view's version of `runway.list_end_marker`
    (DESIGN_runway_nudge.md §6)."""
    return SIMPLE_END_NOTE if crossing_the_end(end_date) else None


def simple_end_buttons(end_date: Optional[str]) -> List[dict]:
    """The offer beside the companion week view's end note (§6).

    Two gates, so the rule stays one sentence: the listing must cross the end of the
    schedule (else the button has no context to sit under) and the nudge must be live
    (else `runway_buttons` returns nothing anyway). The note can therefore draw alone,
    but a button never draws without it."""
    if crossing_the_end(end_date) is None:
        return []
    return runway_buttons(current_runway())


def simple_plan_wrapped_line() -> str:
    """What the companion says once the plan is behind and a new goal is the only way
    forward: the next step, named as operator work (DESIGN_render_persona.md §5).

    Its own function because two surfaces say it — the morning push after the wrap-up
    lead, and `workout adapt`'s refusal on its own."""
    return (
        "When you know what you'd like to work toward next, tell "
        f"{config.telegram_operator_name} — setting up a new goal happens from the "
        "computer."
    )


def simple_plan_setup_line() -> str:
    """What the companion says once a goal exists but the periodization for it does not
    (DESIGN_bot_simple_frontend.md §12.5).

    `simple_plan_wrapped_line`'s sentence family — never "your coach", which formally
    means the app: a plan gets set up from the computer, by the operator, and a goal row
    being cheap is exactly why the periodization built on it stays behind the §7 line."""
    return (
        "The training plan for it gets set up from the computer — "
        f"{config.telegram_operator_name} takes care of that part."
    )


def simple_runway_lines(state: Dict[str, Any], today: str) -> List[str]:
    """The morning push's companion wording for one runway state
    (DESIGN_runway_nudge.md §6).

    Span and block cliffs read as an offer, because the button beside them performs it;
    a plan cliff reads as a wrap-up, because periodization is operator work in companion
    mode and there is nothing here for the athlete to tap."""
    kind, days_left = state["kind"], state["days_left"]
    if kind in (RUNWAY_BLOCK, RUNWAY_SPAN):
        if days_left < 0:
            return ["Want me to plan the next few weeks?"]
        when = simple_when(state["last_covered_date"], today)
        return [f"Heads up — your schedule runs out {when}. "
                "Want me to plan the next few weeks?"]

    wrap_up = (
        "wrapped up" if days_left < 0
        else f"wraps up {simple_when(state['last_covered_date'], today)}"
    )
    lead = f"🎉 Your plan {wrap_up} — that's the goal you've been training toward!"
    if kind == RUNWAY_PLAN_END_NEXT_GOAL:
        obj = state["objective"]
        when = simple_when(str(obj["target_date"]), today)
        return [f"{lead} Next up is {obj['title']} ({when}) — that stretch gets set up "
                f"from the computer, by {config.telegram_operator_name}."]
    return [f"{lead} {simple_plan_wrapped_line()}"]


def _iso_span(start: str, end: str) -> str:
    """A window the expert way — one date, or `start..end`, the form the candidate
    confirms have always asked in."""
    return start if start == end else f"{start}..{end}"


# --- The two voices (DESIGN_render_persona.md §4) ---


class ExpertRenderer:
    """The default voice: reports, tables, IDs, operator nudges.

    One method per thing a command has to say, never per sentence and never per
    command (§4). A method may read what the code it wraps reads; it may not judge —
    no coaching decision belongs in a formatter (DESIGN_bot_simple_frontend.md §10)."""

    # -- workout adapt (cli/workouts/generate.py) --

    def adapt_reason(self, reason: str) -> None:
        print(f"\n{bold('Decision Summary')}:\n{wrap_text(reason)}")

    def adapt_no_change(self) -> None:
        print(green(
            "\nAll metrics are green and workout plan is on track. "
            "No changes recommended."
        ))

    def adapt_confirm_words(self) -> Tuple[str, str]:
        """The preview's heading and the question that follows it. The renderer only
        supplies the words; `runtime.prompt` asks (§4)."""
        return (
            "PROPOSED WORKOUT ADAPTATIONS:",
            "Apply these adaptations to your training plan and sync to Calendar?",
        )

    def adapt_discarded(self) -> None:
        print("\nAdaptations discarded.")

    def adapt_applied(self) -> None:
        print(green("Adaptations applied and synced to calendar successfully."))

    # -- confirming a note's candidates (cli/candidates.py) --

    def constraint_candidate_question(
        self, title: str, start: str, end: str, today: str
    ) -> str:
        return f"Add constraint: {title} ({_iso_span(start, end)})?"

    def constraint_captured(
        self, constraint_id: int, title: str, start: str, end: str, today: str
    ) -> None:
        print(green(
            f"Captured constraint [{constraint_id}]: {title} ({_iso_span(start, end)})"
        ))

    def constraint_candidate_discarded(self) -> None:
        print("Discarded — not saved as a constraint.")

    def constraint_plan_shaping(self, constraint_id: int, impact: Dict[str, Any]) -> None:
        """What a capture says when the directive it just stored is big enough to
        reshape the plan (DESIGN_constraints.md §7). Names the escalation commands,
        which is the operator's next step and no one else's."""
        notice(
            f"  This looks plan-shaping ({impact['days']} days, displaces "
            f"~{impact['displaced_pct']:.0f}% of a typical week). To build it into "
            "the plan, run " + cmd(f"constraint edit {constraint_id} --replan")
            + " or " + cmd("plan generate") + ".",
        )

    def signal_candidate_question(
        self, metric: str, value: Optional[float], start: str, end: str, today: str,
        *, new_category: bool,
    ) -> str:
        shown = "" if value is None else f" = {value}"
        span = _iso_span(start, end)
        if new_category:
            return f"Log as NEW category '{metric}'{shown} on {span}?"
        return f"Log signal: {metric}{shown} on {span}?"

    def signal_logged(
        self, metric: str, days: int, start: str, end: str, today: str
    ) -> None:
        print(green(
            f"Logged {metric} ({days} day{'s' if days != 1 else ''}, "
            f"{_iso_span(start, end)})."
        ))

    def signal_candidate_discarded(self) -> None:
        print("Discarded — not logged as a signal.")

    def signal_not_logged(self, metric: str) -> None:
        notice(f"Could not log '{metric}' — no calendar write succeeded.")

    # -- listings --

    def workout_list(
        self, workouts: list, verdicts: dict, args, *, start_date: Optional[str],
        end_date: Optional[str], ids: list, names_a_range: bool,
    ) -> None:
        print_workout_table(
            workouts, verdicts, args, start_date=start_date, end_date=end_date,
            ids=ids, names_a_range=names_a_range,
        )

    def workout_generate_preview(self, proposal) -> bool:
        """Draws the proposal; False when there is nothing to apply."""
        return print_generate_preview(proposal)

    def workout_compare(
        self, days: list, *, start_date: str, end_date: str, sport_filter: Optional[str],
        discrepancies: list, informational: list, covered_ranges: list,
    ) -> None:
        print_workout_compare(
            days, start_date=start_date, end_date=end_date, sport_filter=sport_filter,
            discrepancies=discrepancies, informational=informational,
            covered_ranges=covered_ranges,
        )

    def calendar_marked(self, marked: int) -> None:
        print_calendar_marked(marked)

    def goal_list(self, goals: list, called_off: list, show_all: bool, today: str) -> None:
        print_goal_table(goals, called_off, show_all)

    def plan(self, goal: dict, macrocycle: dict, args) -> None:
        print_plan(goal, macrocycle, args)

    def progress(self, payload: dict, args, today: str, weeks_window: int) -> None:
        print_progress_report(payload, args, today, weeks_window)

    def revision_preview(self, proposal: RevisionProposal, heading: str) -> None:
        print_revision_preview(proposal, heading)

    # -- goals a tap can reach (DESIGN_bot_simple_frontend.md §12.5, §12.6) --

    def goal_row(self, goal: dict, today: str) -> None:
        """The goal as it now stands, after an add or an edit."""
        print_goal_row(goal)

    def goal_added(self) -> None:
        print(green("Goal added successfully. Run " + cmd("plan generate")
                    + " to generate training cycles."))

    def goal_updated(self) -> None:
        print(green("Goal updated successfully. Run " + cmd("plan generate")
                    + " to regenerate training cycles if needed."))

    def goal_stood_down(self, archived: dict) -> None:
        """What calling a goal off did to the schedule."""
        report_archived_sessions(archived)

    def goal_called_off(self, goal: dict, archived: dict, today: str) -> None:
        """`goal rm` in full — the row, what stood down, and the way back. Its own
        method rather than the three above in sequence because the companion says the
        whole of it in one sentence (§12.6)."""
        print_goal_row(goal)
        self.goal_stood_down(archived)
        print(green(
            "Goal called off. Its plan, versions and feedback are kept — "
            + cmd(f"goal edit {goal['id']} --status active") + " brings it back."
        ))

    # -- constraints an edit can reach (§12.4) --

    def constraint_replan_offer(self, impact: Dict[str, Any]) -> Optional[str]:
        """States a directive's magnitude and returns the question that offers to build
        it into the plan — or None where that escalation is not the reader's to make.

        Returning the question rather than asking it keeps voice and transport apart
        (§4); returning None is how the companion says the fact and stops, since
        `plan generate` is operator work (DESIGN_bot_simple_frontend.md §12.9)."""
        notice(
            f"This {impact['days']}-day constraint displaces "
            f"~{impact['displaced_pct']:.0f}% of a typical week's planned load."
        )
        return "Replan around it?"

    def constraint_honor_hint(self, constraint_id: int) -> None:
        """Where a plan-shaping directive lands, and what would build it in
        (DESIGN_constraint_honoring.md §4)."""
        from trainmate.cli.constraints import point_at_honor
        point_at_honor(constraint_id)

    # -- settings (§12.7) --

    def setting_changed(self, name: str, stored: str, before: str) -> None:
        if stored == before:
            print(green(f"{name} is {stored} (unchanged)."))
            return
        print(green(f"{name} set to {stored}") + dim(f" — was {before}."))

    # -- one-liners --

    def constraint_removed(self, constraint_id: int) -> None:
        print(green(f"Constraint [{constraint_id}] removed."))

    def no_upcoming_goal(self) -> None:
        """`plan show`'s empty state — the sentence `_resolve_goal` would print."""
        notice("No active goals found. TrainMate needs at least one goal.")

    def no_plan_yet(self, goal: dict) -> None:
        notice(f"No active macrocycle strategy found for goal '{goal['title']}'.")
        print(green(f"Run {cmd('plan generate')} to create one."))

    def runway_hint(self, state: Optional[Dict[str, Any]], today: str) -> None:
        """The end-of-schedule nudge: the fact, then the exact command
        (DESIGN_runway_nudge.md §4)."""
        if not state:
            return
        print()
        for line in runway_hint_lines(state, today):
            notice(line)
        print()

    def adapt_plan_behind(self, state: Optional[Dict[str, Any]], today: str) -> None:
        """Why `workout adapt` refuses when the whole periodization is behind us
        (DESIGN_render_persona.md §5). The hint says it when the detector is still
        firing; past that window the refusal says it on its own."""
        if state:
            self.runway_hint(state, today)
            return
        notice(
            "Your plan is behind you — there is nothing left to adapt towards. Set "
            "what's next with " + cmd("goal add") + ", then " + cmd("plan generate")
            + "."
        )


class CompanionRenderer(ExpertRenderer):
    """The athlete's voice: prose, day words, no IDs, no commands they cannot type.

    Overrides only what it words differently; everything else falls through to the
    expert form, which is what makes that fallback structural rather than a discipline
    (DESIGN_bot_simple_frontend.md §6). Tone rule: lead with what is next, state gaps as
    neutral facts after the lead, never open with a miss."""

    # -- workout adapt --

    def adapt_reason(self, reason: str) -> None:
        # No report header: the reason is already prose.
        print(f"\n{wrap_text(reason)}")

    def adapt_no_change(self) -> None:
        print(green("\nAll clear — the plan stands as it is. 💪"))

    def adapt_confirm_words(self) -> Tuple[str, str]:
        return "Here's what I'd change:", "Shall I make these changes?"

    def adapt_discarded(self) -> None:
        print("\nOkay — nothing changed.")

    def adapt_applied(self) -> None:
        print(green("Done — your plan is updated. 💪"))

    # -- confirming a note's candidates --
    # The companion's main felt surface once notes stop riding the coach: she says
    # something, and this is what asks before anything is stored
    # (DESIGN_bot_simple_frontend.md §12.3). Day words, no IDs, no ISO spans (§6).

    def constraint_candidate_question(
        self, title: str, start: str, end: str, today: str
    ) -> str:
        return f"Shall I remember that? “{title}” — {simple_span_words(start, end, today)}"

    def constraint_captured(
        self, constraint_id: int, title: str, start: str, end: str, today: str
    ) -> None:
        print(green("Noted — I'll work around that 👍"))

    def constraint_candidate_discarded(self) -> None:
        print("Okay — I won't note that one.")

    def constraint_plan_shaping(self, constraint_id: int, impact: Dict[str, Any]) -> None:
        """The same fact, without the commands: reshaping the plan around it is one tap
        away on the offer that follows this capture, and `plan generate` is operator work
        the athlete cannot run (DESIGN_bot_simple_frontend.md §12.10)."""
        print(wrap_text(simple_plan_shaping_line(impact)))

    def signal_candidate_question(
        self, metric: str, value: Optional[float], start: str, end: str, today: str,
        *, new_category: bool,
    ) -> str:
        words = simple_metric_words(metric)
        shown = "" if value is None else f" ({value:g})"
        when = simple_span_words(start, end, today)
        if new_category:
            # The ladder's second rung still reads as one question, but says plainly that
            # this is a kind of note she has not logged before (§6 of the signal design).
            return f"That's a new one for me — log it as “{words}”{shown}, {when}?"
        return f"Shall I log that? “{words}”{shown} — {when}"

    def signal_logged(
        self, metric: str, days: int, start: str, end: str, today: str
    ) -> None:
        print(green("Logged — thanks for telling me 👍"))

    def signal_candidate_discarded(self) -> None:
        print("Okay — I won't log that one.")

    def signal_not_logged(self, metric: str) -> None:
        print("Hmm — that didn't save. Tell me again in a bit?")

    # -- listings --

    def workout_list(
        self, workouts: list, verdicts: dict, args, *, start_date: Optional[str],
        end_date: Optional[str], ids: list, names_a_range: bool,
    ) -> None:
        # A single-day window reads as the day, any other window as the week ahead
        # (DESIGN_bot_simple_frontend.md §6).
        if start_date and start_date == end_date:
            for line in simple_day_lines(workouts, start_date, verdicts):
                print(line)
            return
        for line in simple_week_lines(
            workouts, verdicts,
            end_note=simple_end_note(end_date) if names_a_range else None,
        ):
            print(line)
        # The note states that the schedule stops; the button is what she does about it,
        # offered where she is already looking (DESIGN_runway_nudge.md §6).
        buttons = simple_end_buttons(end_date) if names_a_range else []
        if buttons:
            emit_buttons(buttons)

    def workout_generate_preview(self, proposal) -> bool:
        # The sessions render the way her week view does — the runway button makes this
        # preview reachable by tap, so it must not be a table (DESIGN_runway_nudge.md §6).
        print(f"\n{wrap_text(proposal.reasoning)}\n")
        if not proposal.workouts:
            notice("The coach proposed no sessions — nothing to apply.")
            return False
        for line in simple_week_lines(list(proposal.workouts)):
            print(line)
        print()
        return True

    def workout_compare(
        self, days: list, *, start_date: str, end_date: str, sport_filter: Optional[str],
        discrepancies: list, informational: list, covered_ranges: list,
    ) -> None:
        # The discrepancy list, the load-from-RPE note and the in-block/off-plan
        # distinction are expert detail: the glyph on each line is the whole verdict here,
        # and an effort under the minor-load bar is not mentioned at all
        # (DESIGN_bot_simple_frontend.md §6).
        kept = []
        for date_str, results, unplanned in days:
            worth_a_line = [
                a for a in unplanned
                if runtime.garmin.activity_load(a) >= config.minor_activity_load_threshold
            ]
            if results or worth_a_line:
                kept.append((date_str, results, worth_a_line))
        for line in simple_compare_lines(kept, start_date, end_date, _today_str()):
            print(line)

    def calendar_marked(self, marked: int) -> None:
        # Bookkeeping on the operator's Calendar; chat is not the audit surface (§6).
        return

    def goal_list(self, goals: list, called_off: list, show_all: bool, today: str) -> None:
        for line in simple_goal_lines(goals, today):
            print(line)

    def plan(self, goal: dict, macrocycle: dict, args) -> None:
        mesocycles = runtime.db.get_mesocycles_for_macrocycle(macrocycle['id'])
        for line in simple_plan_lines(
            goal, macrocycle, mesocycles, _today_date().strftime("%Y-%m-%d")
        ):
            print(line)

    def progress(self, payload: dict, args, today: str, weeks_window: int) -> None:
        lines = simple_progress_lines(payload, today)
        for line in lines:
            print(wrap_text(line))
        chart_arg = getattr(args, "chart", False)
        if not chart_arg:
            return
        # The chart captions itself with the trend line, where the expert form captions
        # it with the table's first row (DESIGN_bot_simple_frontend.md §6).
        emit_chart(
            chart_arg,
            progression.clip_payload_for_weeks(
                payload, weeks_window, today, cap_future=True
            ),
            lines[0],
        )

    def revision_preview(self, proposal: RevisionProposal, heading: str) -> None:
        print(f"\n{heading}")
        for entry in simple_revision_lines(proposal):
            print(f"\n{entry}")

    # -- goals a tap can reach --

    def goal_row(self, goal: dict, today: str) -> None:
        from trainmate.db.objectives import GOAL_ARCHIVED, goal_state
        # An archived goal was called off and says nothing at all in companion mode
        # (§11 tone rule) — the sentence that follows is the whole of what she hears.
        if goal_state(goal, today) == GOAL_ARCHIVED:
            return
        print(simple_goal_line(goal, today))

    def goal_added(self) -> None:
        print(green("All set 🎯"))
        print(wrap_text(simple_plan_setup_line()))

    def goal_updated(self) -> None:
        print(green("Done — that's updated 👍"))

    def goal_stood_down(self, archived: dict) -> None:
        if archived.get('archived_workouts'):
            print(wrap_text(
                "I've cleared the sessions that were building toward it — nothing else "
                "on your schedule changes."
            ))

    def goal_called_off(self, goal: dict, archived: dict, today: str) -> None:
        # The tone rule does the mourning: this is the last time she hears about it, and
        # nothing here names the reinstate command, which is the operator's (§12.6).
        print(green(f"Okay — {goal['title']} is off the list."))
        self.goal_stood_down(archived)

    # -- constraints an edit can reach --

    def constraint_replan_offer(self, impact: Dict[str, Any]) -> Optional[str]:
        print(wrap_text(simple_plan_shaping_line(impact)))
        return None

    def constraint_honor_hint(self, constraint_id: int) -> None:
        """Draws nothing: every route it names — `workout generate`, `plan generate` —
        is operator work the athlete cannot run (§12.9). The same shape as
        `runway_hint`, and for the same reason."""

    # -- settings --

    def setting_changed(self, name: str, stored: str, before: str) -> None:
        # The confirm already read the change back as its effect; this only says it took
        # (DESIGN_bot_simple_frontend.md §12.7).
        print(green("Done — that's set 👍" if stored != before
                    else "That's already how it is 👍"))

    # -- one-liners --

    def constraint_removed(self, constraint_id: int) -> None:
        print(green("Done — I've dropped that one and will stop working around it 👍"))

    def no_upcoming_goal(self) -> None:
        print(SIMPLE_NO_PLAN_LINE)

    def no_plan_yet(self, goal: dict) -> None:
        print(SIMPLE_NO_PLAN_LINE)

    def runway_hint(self, state: Optional[Dict[str, Any]], today: str) -> None:
        """Draws nothing: the companion week view and the morning push word this fact
        themselves, and one fact gets one wording per message
        (DESIGN_runway_nudge.md §6)."""

    def adapt_plan_behind(self, state: Optional[Dict[str, Any]], today: str) -> None:
        # The morning push's plan-cliff wording, reached from adapt: the sentence
        # already exists, it just was not reachable from here
        # (DESIGN_render_persona.md §5).
        lines = simple_runway_lines(state, today) if state else [
            simple_plan_wrapped_line()
        ]
        for line in lines:
            print(wrap_text(line))


def make_renderer(render: Optional[str] = None):
    """The one place TRAINMATE_RENDER is interpreted (mirrors `prompt.make_prompt`)."""
    if render is None:
        render = os.environ.get("TRAINMATE_RENDER", "")
    if render.strip().lower() == "simple":
        return CompanionRenderer()
    return ExpertRenderer()
