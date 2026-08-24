from typing import Any, List, Optional, Dict
from trainmate.config import config
from trainmate.types import Objective, Constraint, Workout, CompletedActivity
from trainmate.util import cyan, days_between, aside
from trainmate.coach.formatting import (
    EASED_DO_NOT_RESTORE, format_metrics_history, format_completed_activities,
    format_baseline, format_planned_workouts, format_planned_workouts_detailed,
    format_removed_workouts, format_daily_context,
)
import trainmate.coach.engine as _eng
from trainmate.sports import CANONICAL_SPORTS


def _sport_type_enum() -> str:
    """The `"sport_type": ...` JSON-schema line(s) shared by the generate and adapt
    prompts, wrapped like the surrounding hand-written schema. Built from
    `CANONICAL_SPORTS` so adding a sport reaches both prompts."""
    head, cont, width = '      "sport_type": ', "        ", 90
    options = [f'"{s}"' for s in CANONICAL_SPORTS] + ['"rest"']
    lines: List[str] = []
    current = head
    for i, option in enumerate(options):
        token = option + ("," if i == len(options) - 1 else " |")
        separator = "" if current in (head, cont) else " "
        if len(current) + len(separator) + len(token) > width:
            lines.append(current)
            current = cont + token
            continue
        current += separator + token
    lines.append(current)
    return "".join(f"{line}\n" for line in lines)


_SPORT_TYPE_ENUM = _sport_type_enum()


def _terminal_window_task(days_left: int, meso_end_date_str: str) -> str:
    """Renders the adapt-prompt section used when the block is about to end.

    An easing proposed here cannot rebound inside the block and the next block is out of
    reach, so the model is biased toward holding load (DESIGN_block_boundary.md §3).
    """
    ending = (
        "ends today" if days_left == 0
        else f"ends in {days_left} day(s), on {meso_end_date_str}"
    )
    return f"""
### THIS BLOCK IS ENDING
The block you are adapting {ending}.
An easing applied now therefore has no runway to rebound — no later session remains in which
to restore the load you shed — and the days after {meso_end_date_str} belong to the next
block, which you can neither adapt nor pre-empt.
Hold the planned load unless the signal is one you would act on even if this were the
block's very last session. Do not deepen a cut to "carry" the athlete into the next block:
it is planned separately, against their metrics as they stand when it is generated.
"""


def _carried_adaptations_task(carried_workouts: Optional[List[Workout]]) -> str:
    """The CARRYING OVER section of the generate TASK.

    A regeneration rewrites the horizon from scratch, so without this the load a prior
    `workout adapt` took off is handed straight back (DESIGN_workout_revisions.md §7.1).
    Gated on there being such a session, so a horizon nothing has eased produces the
    prompt it always did.
    """
    if not carried_workouts:
        return ""
    return """
### CARRYING OVER AN ALREADY-EASED SESSION
The user content includes a section titled "SESSIONS ALREADY EASED BY AN ADAPTATION": the
sessions ahead whose current numbers are not the plan's original prescription but the reduced
form a `workout adapt` already produced, against the athlete's state on the day it ran. Each
tag says how often and how recently it was eased, the note after it says why, and the
"Target:" line under it is that session's prescribed time in zone.

For each of them, decide one of two things and nothing in between:

- KEEP it. Return `{"date": ..., "sport_type": ..., "keep": true}` and no other field. The
  session stays exactly as it stands, down to the interval structure and prose you were not
  shown, and the athlete sees no change on that day. Count its target toward the week's
  intensity distribution when you write the days around it, but do not restate it: a KEEP
  carries no "planned_zone_sec" and anything else attached to one is discarded. This is the
  default — an easing was a considered answer to the athlete's state, and rewriting the day
  from the block's targets hands back the exact load adapt took off, silently.
- REPLACE it. Return it as an ordinary workout, fully written out. Do this when the metrics
  in this prompt show the moment the easing answered has passed, or when the block's
  remainder genuinely needs that day for something else — and say which in your reasoning.

You cannot half-do it: there is no way to keep the session and adjust it, because a KEEP
returns nothing to adjust. If you want the day changed at all, write it out in full.
"""


def _block_progress_task(block_progress: Optional[str]) -> str:
    """The CONTINUING A BLOCK section (DESIGN_block_progress.md §4).

    Gated on the data being present, so a run that starts a block cleanly produces the
    prompt it always did. It also conditions BENCHMARK PLACEMENT above, which on its own
    cannot know a boundary test was already run earlier in the block (§4.1).
    """
    if not block_progress:
        return ""
    return """
### CONTINUING A BLOCK ALREADY UNDER WAY
The user content includes a section titled "BLOCK PROGRESS SO FAR": what the block the
athlete is currently in has already banked — its volume and measured intensity, then each
already-trained week with the load the plan asked of it beside the load the athlete actually
produced, then any fitness test it has already run. Those days are history and are not yours
to write — you are producing this block's REMAINDER, not the block.

Read it as the progression's starting point, not as a fresh block. Carry the ramp on from
where the last completed week left it instead of restarting at week-one volume, and keep
the block's remaining weeks pointed at the focus it was given. If one elapsed week's
planned load dips clearly below the weeks around it, that week WAS this block's deload —
do not schedule a second one; if no such dip has happened yet and the block's design calls
for one, it still belongs in the weeks you are writing.

Where a week's actual load fell well short of what was planned, build from the volume the
athlete actually produced rather than from the plan they did not complete — ramping from an
unfulfilled number spikes the acute load. Where actual ran above planned, do not reward it
with a further jump on top.

This also BOUNDS the BENCHMARK PLACEMENT rule above: a boundary week whose fitness test
already appears in that section has had its test, and must not be given a second one.
Place a benchmark only where this block has not already run it.
"""


def _block_composition_task(block_progress: Optional[str], has_intensity: bool) -> str:
    """The JUDGING THE BLOCK'S COMPOSITION section — the other end of
    DESIGN_intensity_distribution.md §9.4's handoff, which tells `adapt` that an over-hard
    block "belongs to the next `workout generate`" (§9.2a).

    Gated on the zone tables actually having rows, not merely on the block-progress section
    existing: every paragraph below quotes those tables, and an athlete with no zone
    recordings would be pointed at a table that says "no zone data".
    """
    if not block_progress or not has_intensity:
        return ""
    return """
### JUDGING THE BLOCK'S COMPOSITION
The block-progress section carries what the athlete's sessions actually MEASURED, per sport
and zone, beside what the plan PRESCRIBED over the same weeks and beside the block's stated
focus. Composition is yours: how many hard sessions the block holds, and how its easy and
hard work divide. `workout adapt` owns the other half — it sharpens how an already-scheduled
session is prescribed and may not change what the block contains — and it defers exactly
this question to you.

ATTRIBUTE BEFORE YOU ACT. Read the measured table against the PRESCRIBED table first,
because the same divergence from the focus has two opposite causes and one wrong answer:
- Measured tracks the prescription, but neither delivers the focus -> the PLAN is wrong,
  and fixing it is yours. Re-shape the weeks still ahead so the block's hard/easy split
  actually produces what its focus asks for.
- Measured diverges from the prescription -> the athlete is executing something other than
  what was written. That is adapt's lane and it is already correcting it session by
  session. Do NOT re-shape the block to match the deviation: cutting hard sessions because
  easy days were run hard rewards the drift and hands the athlete an easier block for
  ignoring the plan. Hold the composition and keep the prescription honest.
- Both track the focus -> there is nothing to correct here. Carry the design on.

Where a change against the preceding block is shown, that is the periodization signal
proper: intensity creeping up block over block is how a base phase quietly becomes a race
season, and deciding whether the weeks you are writing continue or arrest that trend is the
one intensity judgement no other command can make.

Condition all of this on the coverage line and the power table where one exists. An HR-only
table under-reads a hard session, so a block can measure easy that was not — do not
conclude a block was too soft from heart rate alone.
"""


def _planned_zone_task(zone_currencies: Optional[Dict[str, str]]) -> str:
    """The PRESCRIBING INTENSITY section (DESIGN_intensity_distribution.md §9.8).

    The coach already decides an intensity target — it writes "6x3min @ VO2max" — and is
    the only thing in the system that knows the intent. So it states the distribution as
    structured data while it still knows it, instead of the app parsing it back out of
    prose afterwards (§10). Which currency each sport is planned in is the APP's call,
    not the model's: it comes from the same coverage rule the display uses, so the plan
    is never written in a currency the table cannot render.
    """
    if not zone_currencies:
        return ""
    names = {"power": "power (7 zones)", "hr": "heart rate (5 zones)"}
    lines = "\n".join(
        f"  {sport}: {names.get(cur, cur)}"
        for sport, cur in sorted(zone_currencies.items())
    )
    return f"""
### PRESCRIBING INTENSITY (planned time in zone)
State each session's intensity target as structured data, not only in the prose. The
currency per sport is fixed by what the athlete's recordings actually cover — use
exactly these and nothing else:
{lines}
A sport not listed above (and any rest day) leaves "planned_zone_currency" null and
"planned_zone_sec" empty: swimming is anchored on pace and strength on load, and neither
yields a zone model. Do NOT invent one for them.

Split the session's minutes across the zones the way you intend it to be executed —
warm-up and recovery minutes into the low zones, work minutes into the target zone. A
5x4min VO2max session is mostly Z2 by the clock and that is what to write. Do NOT derive
these numbers from "tss": TSS is duration x intensity folded into one scalar and cannot
be unfolded, and zones computed from it would make planned-vs-measured intensity a
restatement of the adherence percentage that already exists.

The seconds do not have to sum to "duration_minutes" x 60 — they are a prescription, not
an accounting identity. HR sessions fill zones 1-5 and leave 6 and 7 null.
"""


def _carried_keep_field(carried_workouts: Optional[List[Workout]]) -> str:
    """The `keep` member of the generate response schema.

    Third region on the same gate as CARRYING OVER and its data section: a schema that
    offers `keep` where the prompt never explained it is exactly the half-application
    `tests/test_prompt_gates.py` exists to catch.
    """
    if not carried_workouts:
        return ""
    return (
        '      "keep": true (OMIT on an ordinary session. Present ONLY on a session listed\n'
        "        in SESSIONS ALREADY EASED BY AN ADAPTATION that you are keeping as it\n"
        '        stands — see CARRYING OVER — in which case "date" and "sport_type" are\n'
        "        the only other fields to give and every other member here is omitted),\n"
    )


def _planned_zone_fields(zone_currencies: Optional[Dict[str, str]]) -> str:
    """The two response-schema members carrying §9.8's target, declared the way every
    other field is: a prose-annotated JSON example."""
    if not zone_currencies:
        return ""
    return (
        '      "planned_zone_currency": "hr" | "power" | null (the currency for THIS\n'
        "        session's sport, from PRESCRIBING INTENSITY above; null for rest days\n"
        "        and for any sport not listed there),\n"
        '      "planned_zone_sec": [300, 1800, 600, 0, 0, null, null] (seconds intended\n'
        "        in each zone, low to high. Five entries for heart rate, seven for\n"
        "        power; use null or omit entirely when the currency is null),\n"
    )


# Two of adapt's STANDING RULES as constants, so `tests/test_prompt_gates.py` can assert
# which rules the TASK is given against the constants rather than against quoted prose
# (DESIGN_adapt_task_prompt.md §2). The other three are written inline at the call site.
RULE_MOVE_FIRST = (
    "MOVE BEFORE YOU EASE, EASE BEFORE YOU DELETE. Rescheduling a session a day or two\n"
    "   preserves the planned work; deleting it loses it."
)

RULE_BLOCK_NOT_YOURS = (
    "THE BLOCK IS NOT YOURS TO RESHAPE. You adapt the sessions inside it.\n"
    "   No single day's signal — a depressed morning, a note, a drift reading —\n"
    "   is evidence the BLOCK is too hard, and none permanently re-cuts its planned\n"
    "   volume/intensity. When you do believe the block itself is wrong, say so in\n"
    "   \"reason\" and leave it alone."
)


def _standing_rules_task(*rules: str) -> str:
    """The STANDING RULES section, numbered in the order given."""
    body = "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, start=1))
    return f"""
### STANDING RULES
These govern every section below, and none of them restates these rules:
{body}
"""


def _vacate_task() -> str:
    """The RE-FILLING A DATE YOU VACATE section, always-on in the adapt TASK.

    Apply removes a displaced session only on dates the response covers, so a move that
    emits only its destination leaves the original standing — the session twice, on both
    days, and invisibly, since the preview renders only what the proposal targets
    (DESIGN_adapt_task_prompt.md §2).
    """
    return """
### RE-FILLING A DATE YOU VACATE
Moving a session means emitting TWO entries: the session on its new date, and a replacement
on the date it left. A date you vacate is never simply omitted — an omitted date keeps the
session that is already on it, so the work ends up scheduled twice, on both days.
The replacement is normally rest, and its "change_reason" says where the session went
(e.g. "Long ride moved to Sunday — away Saturday."). Use an easy session instead only when
the day should still carry work. Never leave the date empty: an empty date and a planned
rest day mean different things to the athlete's adherence record, so a hole is never the
right way to encode a move.
"""


def _benchmark_task() -> str:
    """The PROTECTING A BENCHMARK section of the adapt TASK
    (DESIGN_benchmark_workouts.md §4.2).

    Its own function rather than inline prose because the closing line — that an unchanged
    benchmark need not be returned — is what makes `workout_revision_apply`'s
    `clear_benchmark` inference sound, so the two must not drift apart.
    """
    return """
### PROTECTING A BENCHMARK — RESCHEDULE, DON'T DILUTE
A session tagged "[BENCHMARK ...]" is a fitness test: measurement, not stimulus, so the
usual "ease the hard day" logic is exactly wrong for it — run tired it reads low and then
mis-scales every workout after it. NEVER reduce, soften or shorten a benchmark, and never
blank the flag on the session that still IS the test. If the athlete will not be fresh on
test day (negative TSB / poor recovery), MOVE it intact — same content, same
benchmark_type — to a later day within THIS block where they will be fresher, and lighten
the days before it; emit the test on its new date and a replacement for its old one.
If it already sits on the block's LAST day and no later in-block day exists,
POSTPONE it: replace it with an ordinary easy session (no benchmark_type) — a compromised
maximal test sets a wrong anchor that mis-scales every session after it, so a skipped test
costs a retest where a bad number costs a block.
The next generated block re-places the test when it is due.
A benchmark you are NOT changing need not be returned at all.

benchmark_type says what a session IS, not which day it sits on — it travels with the test,
not with the date. So any OTHER session you put on a test's date — the replacement left
behind by a move, the easy day of a postponement, or something the athlete asked for
instead — is NOT the test and MUST carry "benchmark_type": null. Copying the flag onto it
files that session as a completed fitness test: a social ride is then read as an FTP
result, and the block believes it has already tested and skips the real one. If you replace
a test rather than move it, say so in the reason and leave the flag off.
"""


class WorkoutLogicMixin:
    """Part of :class:`CoachEngine` — see coach/engine/__init__.py."""

    def _workout_generate_logic(
        self, objectives: List[Objective], constraints: List[Constraint],
        today_str: str, guidelines: str, profile: Optional[Dict[str, Any]],
        strategy: str, meso_text: str, learnings: str,
        num_days: int = 28,
        start_str: Optional[str] = None,
        metrics: Optional[List[Dict[str, Any]]] = None,
        completed_activities: Optional[List[CompletedActivity]] = None,
        baseline: Optional[Dict[str, Any]] = None,
        pmc_warmup_cutoff: Optional[str] = None,
        pmc_context: Optional[str] = None,
        block_progress: Optional[str] = None,
        block_has_intensity: bool = False,
        zone_currencies: Optional[Dict[str, str]] = None,
        anchor_history: Optional[str] = None,
        carried_workouts: Optional[List[Workout]] = None
    ) -> Dict[str, Any]:
        """Queries LLM to generate workouts for a given number of days based on active strategy.

        `start_str` is the first day to schedule (defaults to today). It differs from today
        only when today's session is already completed and must be preserved — generation
        then begins tomorrow so the finished workout isn't overwritten.
        """
        start_str = start_str or today_str
        starting_phrase = "today" if start_str == today_str else start_str
        weeks = num_days / 7
        if weeks == int(weeks):
            duration_desc = f"{int(weeks)} week{'s' if weeks != 1 else ''} ({num_days} days)"
        else:
            duration_desc = f"{num_days} day{'s' if num_days != 1 else ''}"
        # Whether a test is DUE is benchmarks.md §1's call (triggers, cadence, floor);
        # this section names only the slot and the one fact the guidelines cannot know —
        # the tests this same span is placing (DESIGN_benchmark_workouts.md §4.1). The
        # no-test-near-goal carve-out is event logic — a horizon goal has no event for a
        # test to compete with. `objectives` is the upcoming list, date-ascending, so [0]
        # is the goal governing this span.
        if objectives and objectives[0].get('date_type') == 'horizon':
            goal_week_exception = (
                " The athlete's goal date is a training horizon, not a scheduled event,\n"
                "so a boundary week near it is an ordinary boundary."
            )
        else:
            goal_week_exception = (
                " Never place a test inside the last seven days before the goal or the\n"
                "goal's own week — the final block tapers into the event, and a maximal "
                "test there\n"
                "competes with the effort it is meant to serve."
            )
        custom_task = (
            "## TASK\n"
            f"Generate a training schedule for the next {duration_desc} starting from "
            f"{starting_phrase}.\n"
            "Ensure the weekly schedules/microcycles are designed specifically to match the focus, target\n"
            "volume, and intensity of the active mesocycle block(s) the athlete is in during this period, and\n"
            "incorporate any deload weeks or exceptions for the athlete's active constraints in accordance\n"
            "with the science guidelines.\n"
            "\n"
            "### BENCHMARK PLACEMENT (fitness tests — see the BENCHMARK guidelines above)\n"
            "A mesocycle-boundary week (a block's final week) is the natural slot for a fitness test;\n"
            "whether one is DUE there is the BENCHMARK guidelines' call. Apply their re-benchmark\n"
            "triggers, typical cadence and minimum-interval floor — counting any tests you are placing\n"
            "in this same span — and leave a boundary week without a test when none is due. ANCHORS ON\n"
            "RECORD in the user content dates each anchor's last measurement."
            + goal_week_exception
            + " When you do place one: set \"benchmark_type\" to the test kind, precede it with an opener\n"
            "or easy day so the athlete is fresh (positive TSB) on test day, keep the title/description\n"
            "venue-neutral (e.g. \"20-min FTP test or ramp test\" — the athlete's preferences say where\n"
            "they test), and never put it in a week the athlete's constraints put under full rest.\n"
            + _block_progress_task(block_progress)
            + _block_composition_task(block_progress, block_has_intensity)
            + _carried_adaptations_task(carried_workouts)
            + _planned_zone_task(zone_currencies)
            + "\n"
            "## RESPONSE FORMAT\n"
            "You MUST respond with a JSON object containing:\n"
            "{\n"
            '  "reasoning": "How this microcycle design serves the active mesocycle focus, in AT\n'
            '    MOST 4 SENTENCES. The sessions themselves are listed below your prose — describe\n'
            '    the shape of the week and why, not each workout in turn.",\n'
            # Workout generation is read-only w.r.t. coach learnings (see
            # DESIGN_backward_evaluation.md §11): it consumes the rendered learnings in the
            # system prompt but authors none. Tactical/recent observations are better
            # captured by `adapt`, durable ones by `analyze`. Hence no learning_updates here.
            '  "workouts": [\n'
            "    {\n"
            '      "date": "YYYY-MM-DD",\n'
            + _SPORT_TYPE_ENUM +
            '      "title": "Workout Title (e.g., Tempo Run, Long Ride, Rest Day)",\n'
            '      "description": "Start with the title on its own line in brackets followed by a\n'
            '        newline, e.g. \"[Tempo Run]\\n\", then a detailed description of intensity,\n'
            '        duration, heart rate zones, and goals.",\n'
            "      \"duration_minutes\": 60, (Estimated workout duration in minutes, integer. Use 0 for rest days)\n"
            "      \"rpe\": 6, (Expected Rate of Perceived Exertion, integer 1-10. Use 0 for rest days)\n"
            "      \"tss\": 45, (Expected Training Stress Score, integer. Use 0 for rest days)\n"
            + _planned_zone_fields(zone_currencies)
            + _carried_keep_field(carried_workouts) +
            '      "benchmark_type": null (Normally null. Set ONLY on a scheduled fitness\n'
            "        test — see BENCHMARK PLACEMENT — to the test kind, e.g. \"ftp_20min\" |\n"
            '        "ftp_ramp" | "run_threshold_30min" | "run_5k_tt" | "css_400_200" |\n'
            '        "e1rm" | "mas_cooper". An ordinary training session leaves it null.)\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )
        system_prompt = self._build_system_prompt(
            objectives=objectives,
            constraints=constraints,
            guidelines=guidelines,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            profile=profile,
            custom_task=custom_task
        )
        user_content = (
            f"Today's date is {today_str}. "
            f"Please generate the microcycles (workouts) for the next {duration_desc} "
            f"starting from {starting_phrase}."
        )
        if start_str != today_str:
            user_content += (
                f" Today's ({today_str}) session is already completed and must NOT be "
                f"regenerated — the first workout you schedule must be dated {start_str}."
            )

        history_text_parts = []
        # What BENCHMARK PLACEMENT's interval rule reads against — without dates the
        # model cannot know whether an anchor is due (DESIGN_benchmark_workouts.md §4.1).
        if anchor_history:
            history_text_parts.append(
                "## ANCHORS ON RECORD\n"
                "The dated logbook behind the profile's thresholds. A 'manual' or "
                "'modeled' value is\nan assumption, not a measurement — only a test "
                "starts the interval clock.\n" + anchor_history
            )
        # First of the history sections: it frames what the metrics and activities below
        # mean — the same volume reads differently in a block's first week than its last.
        # Same gate as the task section above, so the two never disagree about its presence.
        if block_progress:
            history_text_parts.append(
                "## BLOCK PROGRESS SO FAR\n"
                "The part of the current block already trained — see CONTINUING A BLOCK "
                f"ALREADY UNDER WAY.\n{block_progress}"
            )
        if metrics:
            metrics_text = format_metrics_history(metrics, pmc_warmup_cutoff)
            # The single CTL ramp line + warm-up flag ride beside the per-day block (not
            # repeated per day), so the prompt that sets next week's load sees the fitness
            # trajectory (§5.2).
            if pmc_context:
                metrics_text += "\n" + pmc_context
            history_text_parts.append(
                f"## ATHLETE'S METRICS HISTORY (PAST 15 DAYS)\n{metrics_text}"
            )
        if baseline:
            baseline_str = format_baseline(baseline)
            history_text_parts.append(
                f"## BASELINE REFERENCE\n{baseline_str}"
            )
        if completed_activities:
            completed_text = format_completed_activities(completed_activities)
            history_text_parts.append(
                f"## ACTUAL COMPLETED GARMIN ACTIVITIES IN WINDOW\n{completed_text}"
            )

        # Last, closest to where the model starts writing: unlike the sections above it
        # is not context about the athlete but a claim on the output. Same gate as
        # CARRYING OVER above, so the two never disagree about its presence.
        if carried_workouts:
            history_text_parts.append(
                "## SESSIONS ALREADY EASED BY AN ADAPTATION\n"
                "These are the only sessions you should try to carry over — every other "
                "day in this\nwindow is yours to write from scratch, and a date this list "
                "does not name is not\nspoken for.\n"
                + format_planned_workouts(
                    carried_workouts, eval_date=today_str,
                    easing_closer=EASED_DO_NOT_RESTORE,
                )
            )

        if history_text_parts:
            user_content += "\n\n" + "\n\n".join(history_text_parts)

        aside("Querying OpenRouter to generate training workouts (microcycles)...", cyan)
        plan_data = _eng.openrouter_client.complete(
            system_prompt, user_content, label="workout_generate"
        )
        return plan_data

    def _workout_adapt_logic(
        self, target_date_str: str, history_days: int, start_date_str: str,
        metrics: List[Dict[str, Any]], completed_activities: List[CompletedActivity],
        planned_workouts: List[Workout], baseline_str: str,
        meso_end_date_str: str, objectives: List[Objective], constraints: List[Constraint],
        guidelines: str, profile: Optional[Dict[str, Any]], strategy: str,
        meso_text: str, learnings: str, discrepancies: List[str],
        informational: Optional[List[CompletedActivity]] = None,
        removed_workouts: Optional[List[Workout]] = None,
        daily_context: Optional[List[Dict[str, Any]]] = None,
        completed_keys: Optional[set] = None,
        athlete_message: Optional[str] = None,
        pmc_warmup_cutoff: Optional[str] = None,
        pmc_context: Optional[str] = None,
        intensity_context: Optional[str] = None,
        zone_currencies: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Queries LLM to evaluate metrics/activities and adapt workouts if needed.

        `athlete_message` is an optional free-text note for THIS adaptation only; when
        present it is surfaced as a clearly-bounded section of the user content and the
        model is told to weigh it as today's intent without treating it as a durable
        signal about the block.
        """
        # has_message gates FOUR regions that sit hundreds of lines apart: the clause
        # spliced into the change_reason wording, the note-handling instructions, the
        # "new_constraints" schema member, and the note DATA section. They must appear
        # together or the model is told about a section that isn't present.
        # tests/test_prompt_gates.py asserts that, so the invariant survives edits here.
        has_message = bool(athlete_message and athlete_message.strip())
        # Same gate discipline: the drift branch, the CORRECTING EXECUTION DRIFT
        # instructions and the drift DATA section move together.
        has_intensity = bool(intensity_context and intensity_context.strip())
        # Shared change_reason wording, with the note-footprint clause spliced in only when
        # a note could actually have driven the change.
        change_reason_field = (
            '      "change_reason": "One short sentence on why THIS specific session\n'
            '        changed, e.g. \"Cut to easy Z2 to shed intensity.\"'
            + (
                ' If an external\n'
                '        constraint from the athlete\'s note drove the change rather than\n'
                '        the metrics, name that cause here so a future run without the note\n'
                '        understands it, e.g. \"Rest — athlete away, no training access this\n'
                '        day.\"' if has_message else ''
            )
            + '\n        Keep it to a single sentence of at most 20 words; do not restate\n'
              '        the overall reason.",\n'
        )
        # The fourth branch the TASK is missing (§9.1): every existing branch treats
        # adaptation as a response to fatigue or absence, and an athlete running their
        # easy days at Z3 is neither — they showed up for everything and feel fine.
        drift_branch = "" if not has_intensity else (
            "- If the block's measured intensity distribution has diverged from its stated\n"
            "  focus, correct the prescriptions of the sessions still ahead — even when\n"
            "  recovery metrics are fine. A healthy athlete executing the wrong workout is\n"
            "  the case no other branch here covers.\n"
        )
        # All five: adapt is the pass that sees the metrics, the adherence window and the
        # athlete's note, so every rule has something to bind to here (§9). The last three
        # are adapt's alone and live here rather than as hoisted constants.
        standing_rules = _standing_rules_task(
            RULE_MOVE_FIRST,
            RULE_BLOCK_NOT_YOURS,
            "NAME THE CAUSE. Every session you change carries a \"change_reason\" (see the\n"
            "   schema); when something other than the metrics drove it, that cause belongs\n"
            "   there.",
            "NOT EVERY GAP IS A MISS. Activities listed as informational fell on dates no\n"
            "   plan governed; sessions listed as deliberately removed are the athlete's own\n"
            "   plan edits. Count both when judging load and intent — neither is an adherence\n"
            "   failure.",
            "RECOVERY METRICS LAG. A morning reflects what came before it, not what you\n"
            "   schedule after it. Two sections below turn on this.",
        )
        custom_task = f"""
## TASK
Analyze the athlete's actual workout adherence and physiological metrics trajectory over
the past {history_days} days: completed activities against planned workouts, the calculated
discrepancies (misses, workload/duration differences, rest violations), and the rolling
baseline against the daily metrics sequence for signs of accumulated fatigue.

Based on this, determine if we need to adapt the training plan for the remainder of
the active mesocycle block (from {target_date_str} to {meso_end_date_str}).
- If they are showing high fatigue or injury risk (e.g. elevated RHR, depressed HRV,
  poor sleep, or ATL:CTL > 1.3 without a planned overload reason), replace hard workouts
  with recovery or rest.
- If they have missed key workouts, adjust the remaining workouts to safely build back
  volume without spiking the acute load too fast.
- If they are fully recovered and on track, keep the plan as scheduled or make minor
  optimal adjustments.
{drift_branch}{standing_rules}
### WHAT YOU MAY NOT TOUCH
Sessions tagged "[COMPLETED — locked history, not adaptable]" have already been performed,
including any the athlete trained earlier on the evaluation date. Do NOT adapt them, and
never restate a finished session to match what was actually done — adapt only sessions
still ahead. Sessions tagged "[athlete-added]" are the athlete's own deliberate intent:
preserve them as planned unless fatigue or injury risk clearly warrants easing.

### ATTRIBUTING A DEPRESSED MORNING — TRAINING FATIGUE vs LIFESTYLE NOISE
By rule 5, read the externally-logged daily-context signal from the DAY BEFORE a depressed
morning: if one (e.g. alcohol, a bad night, high stress) explains the dip, that suppression
is transient lifestyle noise, NOT accumulated training fatigue.
That changes WHY, not WHAT TO DO TODAY: a suppressed body trains a hard session poorly and
with more risk regardless of cause, so easing or moving today's hard session remains a fair
call on acute readiness. What it changes is what the day is EVIDENCE of — reserve genuine
load REDUCTIONS for fatigue the TRAINING actually caused (a depressed morning following
genuinely hard days, with no lifestyle signal to explain it). When a hard day AND a
lifestyle signal coincide, both may contribute — weigh them rather than blaming training.

### DO NOT COMPOUND A PRIOR ADAPTATION
Sessions tagged "[ALREADY EASED by a prior adaptation ...]" are NOT the original plan —
their numbers are the reduced form a previous adaptation already produced. Rule 5 again:
the morning after an easing still looks depressed from the very fatigue you already acted
on, and reading that as "still too hard" spirals the load down without ever letting it
rebound. Default to HOLDING the already-eased form. Cut further only if the metrics have
clearly WORSENED since it was eased, or a genuinely NEW signal (a hard completed session, a
fresh constraint/context event) warrants it — and the more recently and more times it was
already eased (see the tag), the higher your bar. Restoring load toward the original as the
athlete recovers is encouraged; deepening an already-fresh cut is not.
"""

        # How to encode a move at all — its own section rather than a clause inside the
        # benchmark text, because an ordinary move relies on it too
        # (DESIGN_adapt_task_prompt.md §2).
        custom_task += _vacate_task()

        # Why a test may never be softened, and why moving it is the model's call and not a
        # deterministic pass: DESIGN_benchmark_workouts.md §4.2. Shared, scope-parametrized,
        # so the two prompts that reschedule tests cannot drift apart.
        custom_task += _benchmark_task()

        # Adapt owns execution, generate owns periodization (§9.2): changing what zone
        # Tuesday's run is prescribed at is adapt's call; changing how many hard sessions
        # the block contains is not.
        if has_intensity:
            custom_task += """
### CORRECTING EXECUTION DRIFT
The block summary shows what the athlete's sessions ACTUALLY measured, per sport
and zone, beside the block's stated focus — as a per-week rate over the block's
completed weeks, then the current week's raw minutes so far with how much of that
week has elapsed. The current week is NOT extrapolated: read it against the
elapsed fraction yourself.

A measured picture that disagrees with the focus is an execution error, not a
fatigue signal, and it is yours to fix — by changing HOW the remaining sessions
are prescribed, not how much they contain. Hold duration and planned TSS; sharpen
the intensity target and give it an explicit guard rail the athlete can act on
mid-session (a HR ceiling, a pace cap, "walk the hills").
- Drift upward means the athlete WANTS more, so do not only cap it: say where the
  appetite may legitimately go, in the batch-level reason, and spend it in the
  block's own currency — in a volume block, more easy minutes; in an intensity
  block, a fuller effort on the days already designated hard.
- Drift downward means under-execution, so the guard rail becomes a floor and the
  advice is about how to reach it. Condition this on the power table where one
  exists — HR lag makes under-execution look real when it is not.

This is never a load reduction. If the block genuinely contains too much hard work
— as opposed to easy work being run too hard — that is composition, and it
belongs to the next `workout generate`, not to you.
"""

        # §9.2 gives adapt the intensity factor of a scheduled session, and §9.4's drift
        # correction IS a rewrite of how a session is prescribed — so a session whose
        # zones adapt leaves alone would keep describing the prescription it just
        # replaced, and the future half of the zone table would grade the athlete against
        # a target no longer on the page (§9.8).
        custom_task += _planned_zone_task(zone_currencies)

        # Inside the block's terminal window a cut cannot rebound before the block ends
        # (DESIGN_block_boundary.md §3). Outside it the prompt is unchanged.
        days_left = days_between(target_date_str, meso_end_date_str)
        if 0 <= days_left <= config.adapt_terminal_window_days:
            custom_task += _terminal_window_task(days_left, meso_end_date_str)

        if has_message:
            custom_task += """
### ATHLETE'S NOTE FOR TODAY
The user content includes a section titled "ATHLETE'S NOTE FOR THIS ADAPTATION": a
free-text note the athlete attached to THIS run — extra intent or constraints the metrics
can't show (e.g. a niggle to protect, no access to a sport/venue on a given day, or how
they feel). Weigh it as today's intent alongside the data: honour stated constraints, and
let it tip a judgement call. It is advisory, not an override — do NOT schedule clearly
unsafe load just because the athlete asks (if recovery signals warrant easing, ease and say
why). It speaks for this adaptation only and is never durable evidence about the block.

### EXTRACTING A DURABLE CONSTRAINT FROM THE NOTE
Separately from adapting today's sessions, decide whether the note ALSO states something
the coach must work around beyond today: unavailability, a time/intensity cap, an injury
layoff, a venue/equipment limit, or a stated preference with a date or date range (e.g.
"no run Thursday", "only 45 min today", "broke my ankle, out 6 weeks"). If so, return it in
"new_constraints" below — one entry per distinct directive, exactly as if the athlete had
run `constraint add`. A note only about how they feel right now ("felt flat, ease today") is
NOT durable — leave "new_constraints" empty for it. When unsure, leave it out: a
durable-looking note mis-filed as a constraint is worse than a missed one. This is
extraction only — never invent a plan-shaping escalation, and never omit "start_date"/
"end_date" (default both to today when the note doesn't say). Extracted constraints are
always advisory; the deterministic-rest and plan-shaping escalations are deliberate human
actions and the app, not you, decides those.
"""

        custom_task += """
### DURABLE OBSERVATIONS ARE READ-ONLY HERE
This daily adaptation is READ-ONLY with respect to the coach's durable observations:
use the COACH LEARNINGS as context, but do NOT emit any learning updates here — durable,
evidence-backed observations are authored only by the weekly history analysis
(`data bootstrap` / `data reflect`).
"""

        # Each entry is one top-level member of the response object, without its trailing
        # comma — the ",\n".join below places the separators, so no code hand-writes a
        # comma and the has_message branch can't desync the punctuation.
        schema_members = [
            '  "change_needed": true | false',
            (
                '  "reason": "Overall rationale for the whole adaptation: the readiness/load\n'
                '    picture and the strategy applied across the block, in AT MOST 3 SENTENCES\n'
                '    (~60 words). Adapt runs daily, so this is the line the athlete reads most\n'
                '    often — name the signal you acted on and what you did about it, and leave\n'
                '    out the readings that did NOT change your mind. This is the batch-level\n'
                '    summary, shared by every adapted workout below — do NOT repeat it per\n'
                '    workout; keep per-workout notes in "change_reason"."'
            ),
            (
                '  "adapted_workouts": [\n'
                "    // Include ONLY sessions you are actually changing. Omit any session that\n"
                "    // stays exactly as planned — it is preserved automatically, so re-listing\n"
                "    // an unchanged session (even verbatim) is wrong and counts as a spurious\n"
                "    // adaptation. EXCEPTION: if you change one session on a date that holds\n"
                "    // ANOTHER session of a different sport you are keeping, include BOTH that\n"
                "    // day so the kept one is not dropped.\n"
                "    {\n"
                '      "date": "YYYY-MM-DD",\n'
                + _SPORT_TYPE_ENUM +
                '      "title": "Adapted Workout Title",\n'
                + change_reason_field +
                '      "description": "Start with the title on its own line in brackets followed by a\n'
                '        newline, e.g. \"[Tempo Run]\\n\", then an adapted description of intensity,\n'
                '        duration, heart rate zones, and goals.",\n'
                '      "duration_minutes": 45,\n'
                '      "rpe": 5,\n'
                '      "tss": 30,\n'
                + _planned_zone_fields(zone_currencies) +
                '      "benchmark_type": null (Preserve VERBATIM on the row that still IS\n'
                "        the test — a moved/kept test must stay a test. null on EVERY\n"
                "        other session, including one that takes over a test's date.\n"
                "        Never invent one here. See PROTECTING A BENCHMARK.)\n"
                "    }\n"
                "  ]"
            ),
        ]
        if has_message:
            schema_members.append(
                '  "new_constraints": [\n'
                "    // Optional. Directives extracted from the athlete's note this run (see\n"
                "    // EXTRACTING A DURABLE CONSTRAINT above). Every entry is created exactly as\n"
                "    // if the athlete had run `constraint add`. Omit entirely, or leave empty, if\n"
                "    // the note was only a one-off nudge about today.\n"
                "    {\n"
                '      "title": "the directive, stated short (required)",\n'
                '      "start_date": "YYYY-MM-DD (required; default today)",\n'
                '      "end_date": "YYYY-MM-DD (required; == start for a single day)",\n'
                '      "description": "optional richer context or null/omit"\n'
                "    }\n"
                "  ]"
            )
        custom_task += (
            "\n## RESPONSE FORMAT\n"
            "You MUST respond with a JSON object containing:\n{\n"
            + ",\n".join(schema_members)
            + "\n}\n"
        )
        system_prompt = self._build_system_prompt(
            objectives=objectives,
            constraints=constraints,
            guidelines=guidelines,
            strategy=strategy,
            meso_text=meso_text,
            learnings=learnings,
            profile=profile,
            custom_task=custom_task
        )

        metrics_text = format_metrics_history(metrics, pmc_warmup_cutoff)
        # Single ramp line + warm-up flag beside the per-day block, so adapt sees the
        # fatigue trajectory (§5.2).
        if pmc_context:
            metrics_text += "\n" + pmc_context
        context_text = (
            format_daily_context(daily_context) if daily_context
            else "No external daily-context signals logged in this window."
        )
        discrepancy_text = (
            "\n".join(discrepancies) if discrepancies
            else "No discrepancies detected (athlete fully on track)."
        )
        planned_text = format_planned_workouts_detailed(
            planned_workouts, completed_keys, eval_date=target_date_str
        )
        completed_text = format_completed_activities(completed_activities)

        removed_section = ""
        if removed_workouts:
            removed_section = (
                "\n## WORKOUTS REMOVED BY ATHLETE (deliberately cancelled — not misses)\n"
                + format_removed_workouts(removed_workouts) + "\n"
            )

        informational_section = ""
        if informational:
            informational_section = (
                "\n## ACTIVITIES OUTSIDE ANY PLAN (informational — load counts, "
                "but not adherence failures)\n" + format_completed_activities(informational) + "\n"
            )

        # Ephemeral, this-run-only note from the athlete (see custom_task guidance). Same
        # has_message gate as the instructions above, so the two never disagree. Omitted
        # entirely when absent so a message-less run is byte-for-byte the prior behaviour.
        message_section = ""
        if has_message:
            message_section = (
                "\n## ATHLETE'S NOTE FOR THIS ADAPTATION\n"
                "Free-text intent/constraints for today only — advisory, not an override;\n"
                "do not treat as durable evidence about the block.\n"
                f"{athlete_message.strip()}\n"
            )

        # The measured block summary (§9.3). Kept out of the metrics block on purpose:
        # this is an execution signal, not a readiness one, and the two must not blur.
        intensity_section = ""
        if has_intensity:
            intensity_section = (
                "\n## MEASURED INTENSITY DISTRIBUTION OF THE ACTIVE BLOCK\n"
                "What the athlete's sessions actually recorded, per sport and zone —\n"
                "see CORRECTING EXECUTION DRIFT.\n"
                f"{intensity_context.strip()}\n"
            )

        user_content = f"""
Evaluation Date: {target_date_str}
Adaptation Range: {target_date_str} to {meso_end_date_str}
{message_section}{intensity_section}

## ATHLETE'S METRICS HISTORY (PAST {history_days} DAYS)
{metrics_text}

## EXTERNALLY-LOGGED DAILY CONTEXT (alcohol, poor sleep, stress, etc.)
{context_text}

## BASELINE REFERENCE
{baseline_str}

## PLANNED WORKOUTS
The recent window for adherence, plus already-scheduled sessions through the
adaptation range. This is the full forward plan for CONTEXT — most of it will
usually be fine and should be left untouched; return a session in "adapted_workouts"
only if you are genuinely changing it (the schema's "adapted_workouts" comment covers
omitting unchanged sessions and why re-listing one is a spurious adaptation).
When you DO change a session, modify it in place: preserve its date and sport_type
unless deliberately swapping the sport. Only invent a brand-new session for a date that
currently has none.
Each session below includes its full description so you can reuse its specifics —
interval structure, heart-rate zones, rest/recovery durations — when you carry a changed
session over largely as-is. Adapt as boldly as the athlete's state warrants, but only
where their state actually warrants it; the descriptions are here only so detail you are
keeping isn't lost for lack of being restated:
{planned_text}
{removed_section}
## ACTUAL COMPLETED GARMIN ACTIVITIES IN WINDOW
{completed_text}

## ADHERENCE DISCREPANCIES & VIOLATIONS
{discrepancy_text}
{informational_section}"""
        aside(f"Querying OpenRouter to evaluate adaptation for the remainder of the mesocycle "
             f"({target_date_str} -> {meso_end_date_str})...", cyan)
        decision = _eng.openrouter_client.complete(
            system_prompt, user_content, label="workout_adapt"
        )
        return decision
