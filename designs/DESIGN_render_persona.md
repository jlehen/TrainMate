# Design: The Render Persona

**Status:** Proposed · **Date:** 2026-09-02 · **Branch:** worktree-render-persona-design

## 1. Motivation

Companion mode (DESIGN_bot_simple_frontend.md) started as four opted-in surfaces and a
single helper, `is_simple_render()`. The §11 breadth pass, the runway nudge
(DESIGN_runway_nudge.md §6) and the prose adapt preview each added more, and the
switch is now consulted at fifteen sites across seven CLI files:

| File | Sites | What varies |
|---|---|---|
| `cli/workouts/generate.py` | 7 | adapt's five outcome lines; the `workout generate` preview; the `workout list` body |
| `cli/plans.py` | 3 | the empty state (twice) and the plan body |
| `cli/goals.py` | 1 | the goal list body |
| `cli/progress.py` | 1 | the summary vs the tables |
| `cli/workouts/revisions.py` | 1 | the revision preview |
| `cli/constraints.py` | 1 | the `constraint rm` done line |
| `cli/runway.py` | 1 | the hint goes silent |

Each site is a small honest `if`, and each is correct. The cost is not any one of them;
it is that the companion voice has no home. To check the tone rule (§6 of the bot
design: lead with what is next, never open with a miss) a reviewer reads eight files.
To know which surfaces have opted in, they grep. To add a surface they pick a spot in
a command body and add a sixteenth branch. The expert form, meanwhile, is inline in
the command it belongs to, so the two voices of one surface are never side by side.

The codebase already solved this shape once. `TRAINMATE_FRONTEND` chooses a prompt
*transport*, and no command asks which one it got: `make_prompt()` builds a
`TtyPrompt` or `JsonPrompt` at startup, `runtime.prompt` hands it out, and commands
call `confirm()` on whatever they were given. `TRAINMATE_RENDER` chooses a *voice* and
deserves the same treatment.

## 2. Goals / Non-Goals

**Goals**
- A command body contains no `is_simple_render()` branch. It calls one method on
  `runtime.render` per surface and does not know which persona answered.
- The companion voice lives in one file. Every sentence the tone rule governs sits
  next to its expert twin.
- The opted-in surface list is structural, not a convention: it is the set of methods
  the companion class overrides. Anything not overridden *is* the expert form — the §6
  "fallback that cannot decay" becomes inheritance instead of discipline.
- Byte-for-byte identical output in both modes, with one deliberate exception (§5,
  the sixteenth site). This is a relocation, not a rewrite; the existing
  `tests/test_simple_render.py` and `test_cli_*` suites are the proof.

**Non-goals**
- Folding transport and voice into one "persona" object. Expert-over-Telegram
  (`json` + expert) is the operator's own daily surface, and the dashboard is
  expert-voiced without a TTY. Two orthogonal axes stay two objects on `runtime`.
- Moving the bot-side `simple_ui` branches in `trainmate_bot.py` (keyboard, wrap
  width, `<pre>` vs flowed text, free-text routing, the help card). Those are the
  persona's *behaviour* — what a message is allowed to do — not its wording, and they
  are already confined to one file. §8 sketches the same trick for them if it is
  ever wanted; it is a separate, smaller change.
- Any new companion surface. This design changes where the words live, not which
  words exist.

## 3. Mental model

A **renderer** is the voice a CLI process speaks in. There are two, they share one
method set, and a process gets exactly one for its lifetime:

```
TRAINMATE_RENDER  ──►  make_renderer()  ──►  runtime.render
     (unset)              ExpertRenderer          reports, tables, IDs, operator nudges
     simple               CompanionRenderer       prose, day words, no IDs, no nudges
```

`CompanionRenderer` **extends** `ExpertRenderer`. It overrides the surfaces companion
mode has opted into and inherits the rest. A command never asks which it has; it
describes *what happened* (`adapt_no_change()`, `goal_list(goals, …)`) and the
renderer decides how that reads.

The line builders that already exist — `simple_day_lines`, `simple_goal_lines`,
`simple_plan_lines` and friends — stay pure functions. They are the *how*; the
renderer is the *when*. `bot morning` and `bot constraints` are companion-only by
definition and call the builders directly, as they do today; they never consult the
renderer, because for them there is nothing to choose.

## 4. The renderer

One module, `trainmate/cli/render.py`, holding the pure line builders (moved from
`cli/common.py`, `cli/runway.py` and `cli/plans.py` — see §7) and the two classes.

```python
class ExpertRenderer:
    """The default voice: reports, tables, IDs, operator nudges."""

    # -- adapt (generate.py) --
    def adapt_reason(self, reason: str) -> None: ...
    def adapt_no_change(self) -> None: ...
    def adapt_confirm_words(self) -> Tuple[str, str]:      # (heading, question)
    def adapt_discarded(self) -> None: ...
    def adapt_applied(self) -> None: ...

    # -- listings --
    def workout_list(self, workouts, verdicts, args, *, start_date, end_date, ids, names_a_range) -> None: ...
    def workout_generate_preview(self, proposal) -> bool:  # False when nothing to apply
    def goal_list(self, goals, called_off, show_all, today) -> None: ...
    def plan(self, goal, macrocycle, args) -> None: ...
    def progress(self, payload, args, today, weeks_window) -> None: ...
    def revision_preview(self, proposal, heading: str) -> None: ...

    # -- one-liners --
    def constraint_removed(self, constraint_id: int) -> None: ...
    def no_upcoming_goal(self) -> None: ...                # the `plan show` empty state
    def no_plan_yet(self, goal) -> None: ...               # was the "Run plan generate" pair
    def runway_hint(self, state, today) -> None: ...       # was print_runway_hint
    def adapt_plan_behind(self, state, today) -> None: ... # the adapt refusal (§5)


class CompanionRenderer(ExpertRenderer):
    """The athlete's voice. Overrides only what it words differently; everything
    else falls through to the expert form (DESIGN_bot_simple_frontend.md §6)."""

    def adapt_reason(self, reason):     print(f"\n{wrap_text(reason)}")
    def adapt_no_change(self):          print(green("\nAll clear — the plan stands as it is. 💪"))
    def adapt_confirm_words(self):      return "Here's what I'd change:", "Shall I make these changes?"
    def adapt_discarded(self):          print("\nOkay — nothing changed.")
    def adapt_applied(self):            print(green("Done — your plan is updated. 💪"))
    def goal_list(self, goals, called_off, show_all, today):
        for line in simple_goal_lines(goals, today):
            print(line)
    def no_upcoming_goal(self):         print(SIMPLE_NO_PLAN_LINE)
    def no_plan_yet(self, goal):        print(SIMPLE_NO_PLAN_LINE)
    def runway_hint(self, state, today):
        pass           # the companion week view words this fact itself (runway §6)
    def adapt_plan_behind(self, state, today):
        lines = simple_runway_lines(state, today) if state else [SIMPLE_PLAN_WRAPPED_LINE]
        for line in lines:
            print(wrap_text(line))
    ...


def make_renderer(render: Optional[str] = None):
    """The one place TRAINMATE_RENDER is interpreted (mirrors make_prompt)."""
    if render is None:
        render = os.environ.get("TRAINMATE_RENDER", "")
    return CompanionRenderer() if render.strip().lower() == "simple" else ExpertRenderer()
```

`runtime.py` gains a `render` builder beside `prompt`:

```python
@_builder("render")
def _build_render():
    """The active voice: ExpertRenderer on a terminal and in the operator's chat,
    CompanionRenderer under the simple bot (trainmate.cli.render.make_renderer)."""
    from trainmate.cli.render import make_renderer
    return make_renderer()
```

**Method granularity.** One method per *thing the command has to say*, not per
sentence and not per command. `adapt` has five things to say, so it has five methods;
`goal list` has one. The test is whether the expert and companion forms could ever
want to diverge at that point — if the answer is no, it is not a method, it is a
`print` in the command. Argument lists carry the data the *fuller* form needs (the
expert `workout_list` wants the filter echo; the companion ignores it) so that a
command builds one call and never a per-persona one.

**Renderers draw; they do not judge.** The one rule from the bot design's §10 stands:
no coaching judgement in a formatter. A method may read what the code it wraps reads
today: `_print_plan` loads its mesocycles and, under `--workouts`, the plan's
revisions; the listings ask `schedule_coverage()` where the schedule ends; `progress`
reads the sport preferences from config. Hoisting those reads into the commands would
be a rewrite, not a relocation, and would make the companion form pay for queries only
the expert form uses. Nothing writes between a command's data gathering and its draw,
so where a read happens does not change what it returns.

**Where the confirm lives.** `preview_and_confirm_revision` today both draws the
preview and asks. It splits: `runtime.render.revision_preview(proposal, heading)`
draws, and the caller asks `runtime.prompt.confirm(question)` as before. Drawing is
voice, asking is transport, and the two objects should not call each other.

## 5. The surfaces

What each of today's fifteen sites becomes. "Kind" says what varies: **words** (same
structure, different sentence), **shape** (same data, different rendering), or
**behaviour** (different control flow).

| Today | Kind | Becomes |
|---|---|---|
| `generate.py:193` reason header | words | `render.adapt_reason(reason)` |
| `generate.py:199` no-change line | words | `render.adapt_no_change()` |
| `generate.py:213` heading + question | words | `heading, question = render.adapt_confirm_words()` |
| `generate.py:219` discard line | words | `render.adapt_discarded()` |
| `generate.py:224` applied line | words | `render.adapt_applied()` |
| `generate.py:446` generate preview | shape | `render.workout_generate_preview(proposal)` |
| `generate.py:657` workout list | shape | `render.workout_list(...)` |
| `goals.py:160` goal list | shape | `render.goal_list(...)` |
| `plans.py:568` plan body | shape | `render.plan(...)` |
| `progress.py:919` summary vs tables | shape | `render.progress(...)` |
| `revisions.py:160` preview | shape | `render.revision_preview(...)` |
| `constraints.py:344` done line | words | `render.constraint_removed(id)` |
| `plans.py:552` no macrocycle | words | `render.no_plan_yet(goal)` |
| `plans.py:517` empty-state short-circuit | words | `render.no_upcoming_goal()` — the guard stays, its persona branch goes |
| `runway.py:148` hint silence | behaviour | `render.runway_hint(state, today)` |
| `generate.py:145` plan-behind refusal | behaviour | `render.adapt_plan_behind(state, today)` — the sixteenth site, see below |

**The behaviour sites are wording in disguise:**

- The `plan show` guard at `plans.py:517` keeps its shape — no `--goal`, no `--all`, no
  upcoming goal — and calls `runtime.render.no_upcoming_goal()`, whose expert form
  prints the same notice `_resolve_goal` would have printed one line later.
  `_resolve_goal` itself is untouched: it also serves `plan versions`, `plan diff`,
  `plan rollback` and `plan feedback`, and the 🌱 line is the wrong sentence out of
  `plan rollback`. None of those four has a companion-mode test, so routing the helper
  through the renderer would have changed their output unseen.
- The runway hint's silence is a companion override that draws nothing. The reason — "the companion week view words this fact itself, one fact
  gets one wording per message" — moves from a docstring on a guard to a docstring on
  the override, which is where a reader looking for companion behaviour will look.

The chart caption in `progress` is the one subtle case: the companion form passes its
first summary line to `_emit_chart` as the caption. That stays inside the companion
`progress` override, which is free to call `_emit_chart` itself; the expert override
draws the tables and the chart with the expert caption. The command body calls
`render.progress(...)` once.

**A sixteenth site the grep does not find.** `workout adapt` at `generate.py:145`
never asks which persona it has, but its output still depends on it: when the plan is
behind, it calls the runway hint and, if that returned `False`, prints its own refusal —
"Your plan is behind you … set what's next with `goal add`, then `plan generate`".
The companion hint returns `False` by design, so the companion athlete reads two
commands they have no way to type (bot design §7). `test_runway.py` asserts the refusal
speaks on the simple surface, so the sentence is wanted; its wording is not.

The refusal becomes its own method, `adapt_plan_behind(state, today)`. The expert form
is today's two lines (the hint when the detector fires, the notice when it does not).
The companion form reuses the wording the morning push already has for a plan cliff,
`simple_runway_lines` — "🎉 Your plan wrapped up … when you know what you'd like to
work toward next, tell your coach — setting up a new goal happens from the computer" —
and, when the detector has gone quiet because the plan ended long ago and `state` is
None, the same closing sentence without the celebration lead. This is the one place
the design changes what the companion says, and it is a reuse, not a new voice: the
sentence exists, it was simply not reachable from adapt. The `test_runway.py` assertion
changes with it, from "nothing left to adapt towards" to the coach sentence.

With adapt drawing its own refusal, nothing reads the hint's return value any more —
`status.py` and the second call in `generate.py` already ignore it — so `runway_hint`
returns None and the "did I draw, or did I choose not to" ambiguity goes away.

## 6. Tests

`runtime.render` is a cached singleton, so once any test has built it,
`patch.dict(os.environ, {"TRAINMATE_RENDER": "simple"})` changes the env and not the
object. The failure runs both ways, and the quiet direction is the worse one: the
class-level patcher in `test_runway.py` would build a companion renderer first, and
every expert test after it in the process would print companion prose — most assert on
an exit code or a row, and would pass. Nothing in the harness resets the runtime
between tests today.

The fix is in the harness, not the tests. `run_cli` in `tests/helpers.py` drops the
cached renderer (`runtime.reset("render")`) before invoking `main()`, so each run builds
its voice from the environment exactly as a real CLI process does. The twelve
env-patching sites (`test_runway.py`, `test_cli_bot.py`, `test_cli_workouts.py`,
`test_simple_render.py`) keep working unchanged. The three in `test_simple_render.py`
that exercise `is_simple_render()` itself become tests of `make_renderer()`. Tests of
the pure line builders (`simple_goal_lines` and friends) need nothing; they call the
function and check the lines, as they do today.

## 7. Migration

Five steps, each a commit, each green on the full suite, each leaving both modes
byte-identical. Steps 2–4 can be split further (one command per commit) if a diff
gets uncomfortable.

1. **Scaffold.** Add `cli/render.py` with both classes, every method on
   `ExpertRenderer` delegating to the existing code, and `make_renderer()`. Add the
   `render` builder to `runtime.py` and the `reset("render")` in `run_cli`. No caller
   changes; nothing observable moves.
2. **Words.** The six one-sentence sites (adapt's five, `constraint rm`). Pure string
   relocation; six branches gone.
3. **Shape.** One command at a time: the expert method delegates to the function
   that draws today, and the companion override calls the existing line builder.
   Where the expert block is inline in a command body (`workout list`, the
   `workout generate` preview) it becomes a named function in its own module first.
   The expert blocks stay where they are: they are hundred-line table renderers with
   no sentence-level twin, and moving them would make `render.py` the owner of three
   commands' internals for no reviewable gain. Only the six word sites earn
   side-by-side.
4. **Behaviour.** Route `print_runway_hint` through the renderer (callers:
   `generate.py`, `status.py`); the `plan show` guard calls `no_upcoming_goal()`; the
   adapt refusal calls `adapt_plan_behind()` and its `test_runway.py` assertion moves to
   the coach sentence.
5. **Sweep.** Move the `simple_*` line builders and the `SIMPLE_*` constants from
   `cli/common.py`, `cli/runway.py`, `cli/plans.py` and `cli/workouts/revisions.py`
   (`_simple_preview_lines`) into `cli/render.py` so the companion voice is one file;
   update the `bot.py` imports. Delete `is_simple_render()`; `TRAINMATE_RENDER` is
   now read in `make_renderer` only. Update DESIGN_bot_simple_frontend.md §6 and §8 to
   point here.

**Import direction.** `render.py` imports expert helpers from the command modules at
module level, and the command modules never import `render.py`: they reach it through
`runtime.render`, whose builder defers the import exactly as `prompt`'s does. `bot.py`
is the one module that imports `render.py` directly, and nothing in `render.py`
imports `bot.py`. That graph has no cycle. A function-local import of `render.py`
inside a command module would be the sign the graph has gone wrong, not a fix.

The companion builders and six sentences relocate; the expert table renderers stay
put. One companion sentence changes (step 4, by reuse); everything else is
byte-identical, so the existing golden tests are the acceptance criterion at every
step.

## 8. The bot side (deferred)

`trainmate_bot.py` branches on `simple_ui` about ten times: the reply keyboard, the
wrap width, `<pre>` versus flowed replies, the help card, whether bare text goes to the
router, whether the morning push fires, which menu is registered. These are the
persona's behaviour and they share one file, so they are not this design's problem.

If they ever become one, the same shape fits: an `ExpertChat` / `CompanionChat` pair
with `reply_markup()`, `wrap_width()`, `format_reply(text)`, `menu_commands()`,
`handles_bare_text()`, held by the bot and swapped by `/ui`. The `/ui` switch (bot
design §5.6) would then replace an object instead of flipping a boolean, which is also
what makes it cheap to add a third persona later. Not scheduled.

## 9. Touch points

- `trainmate/cli/render.py` — new: line builders, `ExpertRenderer`,
  `CompanionRenderer`, `make_renderer`.
- `trainmate/runtime.py` — the `render` builder.
- `trainmate/cli/common.py`, `cli/runway.py`, `cli/plans.py`,
  `cli/workouts/revisions.py` — lose the `simple_*` builders and constants (step 5).
- `trainmate/cli/workouts/generate.py`, `cli/goals.py`, `cli/plans.py`,
  `cli/progress.py`, `cli/workouts/revisions.py`, `cli/constraints.py`,
  `cli/runway.py`, `cli/status.py` — command bodies call `runtime.render.*`; inline
  expert blocks become named functions in place.
- `trainmate/cli/bot.py` — import path of the line builders only.
- `tests/helpers.py` — `run_cli` resets the renderer per invocation; the env-patching
  tests are untouched.
- `tests/test_simple_render.py` — the three `is_simple_render()` tests become
  `make_renderer()` tests.
- `designs/DESIGN_bot_simple_frontend.md` §6, §8 — pointer here.
- `trainmate_bot.py` — untouched.

## 10. Decisions & Open Questions

**Resolved**
- Voice and transport stay separate objects (`runtime.render`, `runtime.prompt`);
  neither calls the other (§4).
- Companion extends expert; the override set is the opt-in list (§2, §3).
- Line builders stay pure functions and move into `render.py`; `bot morning` keeps
  calling them directly (§3).
- The renderer draws, the command asks: `preview_and_confirm_revision` splits into a
  render method and a `runtime.prompt.confirm` at the call site (§4).
- Renderers may read what the code they wrap reads; they may not judge (§4).
- Expert table renderers stay in their command modules; `ExpertRenderer` delegates
  (§7 step 3).
- `_resolve_goal` is untouched; only the `plan show` guard goes through the renderer
  (§5).
- `run_cli` rebuilds `runtime.render` per invocation; env patching in tests stays (§6).
- Command modules never import `render.py`; only `bot.py` and the `runtime` builder do
  (§7).
- The adapt plan-behind refusal gets a renderer method; the companion form reuses the
  morning push's "tell your coach" wording rather than naming commands (§5).
- The bot-side persona branches are out of scope (§8).

**Open**
1. Should `ExpertRenderer` be an abstract base with a third, deliberately empty
   `NullRenderer` for the JSON-only paths that print nothing (the web dashboard's
   worker)? Today those paths never reach a rendering command, so: no, until one
   does.
2. `progress` and `workout_list` carry `args` through for their flags (`--sports`,
   `--blocks`, `--weeks`; `--link`, `--verbose`). Cleaner would be a small options
   record, but that is a command refactor, not a persona one. Pass `args`.
