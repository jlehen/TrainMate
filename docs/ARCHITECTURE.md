# TrainMate Architecture & System Manifest

This document is the primary reference for coding agents. Read it before
reading source files — in most cases it will be sufficient. Read a source file
only when you need to change it or when a specific detail is not covered here.

**How this document is organized.** Each numbered section is **current-state
reference** — terse, lookup-oriented facts. The *why* behind non-obvious design
choices (and what they replaced) lives in one place: [§15 Design Rationale &
History](#15-design-rationale--history) and the `DESIGN_*.md` files. Reference
sections therefore link to §15 rather than re-explaining a decision. Each concept
has **one canonical home**; other sections point to it instead of paraphrasing
(e.g. the coach-learnings / confidence model is canonical in
[§3](#3-coach-package-architecture), and [§4](#4-database--key-patterns) /
[§5](#5-database-schema) only list the methods and columns that implement it).

## Contents

1. [System Overview](#1-system-overview)
2. [Module Map](#2-module-map) · [Change recipes](#change-recipes-where-to-edit-for-a-given-task)
3. [coach Package Architecture](#3-coach-package-architecture) — **canonical** coach-learnings / confidence model
   · [CoachEngine](#coachengine) · [CoachService](#coachservice)
4. [Database — Key Patterns](#4-database--key-patterns)
5. [Database Schema](#5-database-schema) — [workouts](#workouts) · [Workout state: three axes](#workout-state--three-orthogonal-axes-not-one-enum) · [other tables](#completed_activities)
6. [Singletons](#6-singletons)
7. [CLI Commands Reference](#7-cli-commands-reference)
8. [Web API Endpoints](#8-web-api-endpoints)
9. [Configuration (`config.yaml`)](#9-configuration-configyaml)
10. [Key Data Flows](#10-key-data-flows) — [Plan gen](#plan-generation-plan-generate) · [Workout gen](#workout-generation-workout-generate) · [Adaptation](#daily-adaptation-workout-adapt) · [Data pull](#data-pull-data-pull-and-auto-ensure) · [Analysis](#data-analysis-data-bootstrap--data-reflect)
11. [Terminology: Plans vs. Workouts](#11-terminology-plans-vs-workouts)
12. [Sports Science & Coaching Mathematics](#12-sports-science--coaching-mathematics)
13. [Daily Signals (Calendar Ingest)](#13-daily-signal-calendar-ingest)
14. [Testing](#14-testing)
15. [Design Rationale & History](#15-design-rationale--history)

---

## 1. System Overview

TrainMate is a local AI sports-science coaching application. The user
configures goals and constraints; TrainMate generates periodized training plans
(macrocycle → mesocycles) and workout schedules (microcycles), then adapts them
daily based on Garmin metrics. Plans and workouts can be pushed to Google
Calendar.

```
  +--------------------------------------------------+
  |               User Interface Layer               |
  |  trainmate_cli.py (shim) + trainmate/cli/        |
  |  trainmate_web.py (Flask)                        |
  |  trainmate_bot.py (Telegram → CLI subprocess)    |
  +---------------------------+----------------------+
                              |
  +---------------------------v----------------------+
  |               Coaching Logic Layer               |
  |  trainmate/coach/service/ ─ CoachService          |
  |    │ orchestrates DB + calendar + LLM calls       |
  |  trainmate/coach/engine/  ─ CoachEngine           |
  |    │ pure logic: prompt building, hash, LLM calls │
  |  trainmate/coach/formatting.py (pure helpers)    |
  |  trainmate/openrouter.py  (OpenRouter LLM client) |
  |  trainmate/adherence.py   (plan vs actual diff)   |
  |  trainmate/plan_diff.py   (plan version vs version)|
  |  trainmate/plan_lineage.py (blocks, in trained order)|
  +---------------------------+----------------------+
                              |
  +---------------------------v----------------------+
  |              Data & Integration Layer            |
  |  trainmate/db/              (SQLite CRUD)        |
  |  trainmate/garmin/          (Garmin direct pull) |
  |  trainmate/google_calendar.py (Calendar sync)    |
  +--------------------------------------------------+
```

---

## 2. Module Map

Every module exposes one **singleton** at module level (see [§6](#6-singletons) for
the list). UIs and tests import the singleton directly — never instantiate the
classes themselves.

### Entry Points

- **`trainmate_cli.py`** — thin entry point: argparse dispatcher (`main()`) and its
  helpers. No business logic. Singletons live in `trainmate/runtime.py`
  ([§6](#6-singletons)), which handlers read directly, so nothing under `trainmate/`
  imports this module. `run_once` — the one function both `main` and the REPL call —
  brackets each command with a journal run (`run.start`/`run.end`), just inside the two
  existing error boundaries, so a failed command records its own traceback with no new
  handler anywhere (DESIGN_logging.md §3/§5.4).
- **`trainmate/cli/`** — per-command-family handler modules (`run_*()`): `status`,
  `progress`, `goals`, `constraints`, `benchmarks`, `signals`, `learnings`,
  `plans`, `data`, `settings`, `journal`, `bot`, the `workouts/` package
  (`parser`/`generate`/`edit`/`revisions`/`_helpers`), plus the shared modules:
  `common` (renderers and the adherence pairing), `selectors` (the range grammar),
  `argparse_ext` (parser/help extensions), `render` (the two voices, [§6](#6-singletons)),
  `candidates` (the note-capture confirm loops), `staleness` (the changed-input wording)
  and `runway` (the end-of-schedule nudge every daily surface draws — the db-reads
  wrapper around `progression.runway` plus the wordings, DESIGN_runway_nudge.md §3).
- **`trainmate_web.py`** — Flask REST API behind the dashboard. **Read-only**: GET
  handlers over `db` and the shared pure modules, no writes, no Garmin, no LLM, no
  Calendar ([§8](#8-web-api-endpoints)).
- **`trainmate_bot.py`** — Telegram chat front-end. Launch with `./tm-bot`. Each
  fact below:
  - **Model:** a message is treated as a CLI command line (leading `/` optional) and
    run through `trainmate_cli.py` *as a subprocess*; stdout+stderr are ANSI-stripped
    and streamed back as `<pre>` replies. Running the real CLI keeps the bot in
    permanent parity with every command/flag and isolates each call.
  - **Interactive commands** work over chat via the prompt broker ([§6](#6-singletons)):
    the CLI is launched with `TRAINMATE_FRONTEND=json` and `-u` over a *persistent,
    unbuffered* subprocess, so a `confirm`/`choose`/`text` prompt arrives as a
    sentinel-framed request line instead of blocking on `input()`. The bot renders it
    as an inline keyboard (confirm → Yes/No, ⚠️ for `danger`; choose → one button per
    option) or an awaited text reply, then writes the answer back to the child's stdin
    so the command resumes.
  - **State / safety:** one in-flight `_Session` per chat holds the process +
    pending-prompt state; a per-prompt `nonce` (in the button `callback_data`) rejects
    stale taps. `/cancel` and an idle `telegram.prompt_timeout_seconds` send a
    cancellation the CLI turns into a clean abort; a between-output
    `telegram.command_timeout_seconds` kills a silent runaway.
  - **Polling runs continuously**, a command in flight or not, so a `✋ Stop` tap,
    `/cancel` or `/restart` reaches the bot while a command is computing. `_serve()`
    still drives the Application/Updater lifecycle by hand instead of
    `Application.run_polling()`, but only so `_pause_polling` can close the long-poll
    from inside the `/restart` handler before its hard exit. This reverses the
    mid-command pause of DESIGN_bot_restart.md §5.1 (DESIGN_bot_stop_button.md §5).
  - **Stopping a coach call.** Every LLM command emits `TM-FLUSH` right after its
    "Working on it — this usually takes about 40s." notice; the bot attaches a `✋ Stop`
    inline button to the message that flush sends, with `callback_data` `stop:{nonce}`
    naming the running command. Tapping it kills the subprocess (the same kill
    `/cancel` does) and replies "Stopped."; the button is retired by the next output, by
    the command ending, or by the tap itself, and a tap on a retired one is told the
    command already finished (DESIGN_bot_stop_button.md).
  - **Self-restart.** `./tm-bot` is a **supervisor**, not just the venv bootstrap: it
    selects its mode from the `TM_BOT_SUPERVISED` env var it sets on itself — default
    invocation = a loop that relaunches a child of itself, `TM_BOT_SUPERVISED=1` = the
    `exec trainmate_bot.py` worker — and traps SIGINT/SIGTERM to TERM-then-KILL the
    child rather than orphan it. `RESTART_EXIT_CODE = 75` is a **cross-file contract**
    (`tm-bot` and `trainmate_bot.py` must stay in sync): only 75 relaunches, every other
    exit (crash included) ends the supervisor too — no backoff, no crash recovery, by
    design. `/restart` is a bot command beside `/cancel` and `/start`, under the same
    allowlist: it kills any live session's subprocess and stops the Updater
    (module-level `restart_teardown`), replies, then `os._exit(75)`
    (DESIGN_bot_restart.md §4/§5.2).
  - **Access** is gated by a numeric chat-id allowlist (`telegram.allowed_chat_ids`).
    Token + allowlist live under a `telegram:` block in `config.yaml` (or
    `TELEGRAM_BOT_TOKEN`).
  - **Pure helpers** (`parse_message_to_argv`, `chunk_text`, `format_reply`,
    `is_authorized`, plus prompt-protocol helpers `parse_prompt_request`,
    `prompt_buttons`, `decode_callback`) are import-safe without
    `python-telegram-bot` (imported lazily in `main`) and unit-tested in
    `tests/test_bot.py`.
  - **Photo protocol:** a sibling one-way sentinel to `TM-PROMPT` — `trainmate.prompt.
    PHOTO_SENTINEL`/`emit_photo(path, caption)` writes `\x1eTM-PHOTO {json}`; `_drive()`
    recognises it (`parse_photo_request`) beside `parse_prompt_request`, sends the file
    via `bot.send_photo` with the payload's `caption`, and unlinks it in a `finally`.
    Any other unrecognised `\x1e`-prefixed line is dropped rather than forwarded as chat
    text, so a future sentinel degrades gracefully on a stale bot build. Currently used
    by `tm progress --chart` (DESIGN_progress_timeline.md §7.2); any future CLI command
    can reuse the same transport.
  - **Simple ("companion") mode** — `telegram.ui: simple`, DESIGN_bot_simple_frontend.md.
    The same pipeline gains a persona for a non-technical athlete; expert mode is
    untouched. A persistent reply keyboard (two labels per row) maps labels onto fixed
    argv (`SIMPLE_KEYBOARD`): today, the week, goals, the periodization plan, progress
    (DESIGN_bot_simple_frontend.md §5.1, §11); "💬 Talk to me" only shows the capture
    prompt — every non-label message, tapped or not, is classified by `tm bot route`
    (a hidden CLI command calling `llm.router_model`) and mapped to argv from the bot's
    own `ROUTER_INTENT_ARGV` table — the model picks an intent, never argv. The tap's
    one effect is invisible and can only help: while it is live, text the router calls
    `unclear` rides the `bot capture note` inbox instead of bouncing, so the button never
    redirects a message, only rescues one (§5.2, §12.3).
  - **Writes reach chat through three shapes, and no fourth**
    (DESIGN_bot_simple_frontend.md §12). A **view** runs fixed argv. A **picker** —
    `tm bot constraints`, `tm bot goals` — renders the list in companion prose and
    attaches a button row whose leaves carry a deterministic single-ID command
    (`constraint rm <id>`, `goal rm <id>`, which archives); the model picks *that*
    something should change, the athlete's tap picks *which*. A **capture** —
    `tm bot capture <intent> "<text>" [--id N]` — is a second, domain-focused LLM call on
    the same router role: it extracts typed fields, the CLI previews them in companion
    prose *rendered from real rows*, and a `TM-PROMPT` confirm makes it real. An
    operation fitting none of the three belongs to the expert vocabulary, which is why
    `plan generate`, wipes, `--purge`, model roles and `restart` stay typed. Notes
    (`add_constraint`/`add_signal`) share one `bot capture note` inbox instead of riding
    `adapt -m`: recording is instant and cheap, and the coach becomes an *offer* — a
    "🔄 Adjust the plan around it" button after every persisted capture, and a "📨 Send it
    to your coach as written" button when the extraction finds nothing. Edits
    (`edit_goal`, `edit_constraint`) *nominate* their object from rows the CLI gave the
    model — its own domain plus the upcoming sessions, because "my long run" and "my
    marathon" do not respect domain lines; a session-shaped ask is handed to the coach,
    several close candidates become a picker whose leaves re-enter the capture with
    `--id` pinned, and nothing anywhere executes without a confirm on a CLI-rendered
    preview. `change_setting` is bounded by the `ROUTABLE_SETTINGS` allowlist in
    `cli/settings.py` (`morning-time`, `morning-deadline`, `push`), given to the
    extraction *and* enforced after it; anything else earns a one-line refusal naming
    the operator rather than the router's "unclear". Signals get no removal counterpart
    on purpose: they are backward-looking evidence, not a rule that keeps shaping the
    schedule (DESIGN_bot_simple_frontend.md §5.5). Subprocesses
    additionally get `TRAINMATE_RENDER=simple`, read once by
    `cli/render.make_renderer` and handed out as `runtime.render` — the voice axis,
    beside `runtime.prompt`'s transport axis. A command calls one method per thing it
    has to say and never asks which persona answered; `CompanionRenderer` extends
    `ExpertRenderer`, so its override set *is* the opted-in list (`workout list`, `workout compare`,
    `progress`, the five adapt outcome lines, `goal list`, `plan show`, `workout
    generate`'s preview, the revision preview, `constraint rm`, the runway hint's
    silence, the adapt plan-behind refusal, the note-candidate confirms, `goal
    add`/`edit`/`rm`, `settings set`, the plan-shaping and replan notices) and anything
    else falls back to the expert form (DESIGN_render_persona.md). Every command a tap
    can reach goes through it, which is what keeps expert command text — `plan
    generate`, `constraint edit --replan`, `goal edit --status active` — out of a chat
    whose reader cannot run any of them. `bot morning`, `bot constraints`, `bot goals`,
    `bot block` and the `bot capture` family are companion-only by definition and call the line
    builders in `cli/render.py` directly. Companion output is prose,
    sent plain instead of `<pre>`. A third one-way sentinel, `BUTTONS_SENTINEL`/
    `emit_buttons` (`\x1eTM-BUTTONS {json}`), attaches a *non-blocking* inline button
    row (`ui:` callback namespace, token-invalidated) whose taps feed a canned
    utterance back through the normal pipeline. An asyncio scheduler (`_push_loop`)
    spawns `tm bot morning` inside the `telegram.push.morning_time`→`morning_deadline`
    window; idempotency lives in the `settings` row `push_morning_last`, so the bot
    process stays stateless. That window runs into the afternoon, so the push grades
    today's sessions (§5) before briefing them: a day already trained gets a
    congratulation and no buttons, since the button row's only offers are ways to change
    a session still ahead. When the schedule is about to run out the push adds one line
    and, on a span or block cliff, one further button whose argv comes from the detector
    (`workout generate [-m ..<id>]`) — the single narrow exception to the guardrail that
    keeps generation off the tappable surface, and never reachable through the router
    (DESIGN_runway_nudge.md §6). The companion week view draws that same button under its
    end-of-schedule note, gated on the listing crossing the cliff as well as the detector
    firing, so the surface that names the gap is also the one that can close it. Only one
    button row is live per chat: a newer row — the morning push included — retires the
    pending one, and a tap that finds its row stale says so rather than silently losing
    the message behind it (§12.3). Free text saying what to train for next routes to
    `add_goal`, a capture that creates the goal row; the periodization built on it stays
    the operator's typed work, and the reply says so by name.
    Slash-prefixed text is always the expert path, and `/ui`
    flips the persona of a running bot in memory — `telegram.ui` decides again at the
    next restart (DESIGN_bot_simple_frontend.md §5.6).
  - **Output is quieter here than on a terminal.** Because `_drive` buffers the whole
    run and flushes it as one message, progress narration arrives *after* the work it
    describes, ahead of the answer. So `TRAINMATE_FRONTEND=json` also switches off
    "asides" — `trainmate.util.aside`, used for progress lines, cache-reuse notes,
    defaulting notices, next-step hints and standing caveats. Answers, warnings and
    errors are unaffected. `TRAINMATE_VERBOSE=1/0` overrides either way; there is no CLI
    flag, since `-v/--verbose` already means "more detail in this listing" on seven
    sub-commands (DESIGN_output_verbosity.md).
  - **Flush protocol:** a fourth one-way sentinel, `FLUSH_SENTINEL`/`emit_flush()`
    (`\x1eTM-FLUSH {}`), recognised by `is_flush_request()` — it carries no payload and
    its only effect is to end the buffered message where it stands. `openrouter.complete`
    emits one immediately before the POST, so the setup an LLM command printed is
    delivered *before* the tens of seconds it then spends silent, instead of arriving
    glued to the answer. `emit_flush` is a no-op unless `is_json_frontend()`, so the
    frame never reaches a terminal (DESIGN_output_verbosity.md §7.3).
  - **The wait notice:** the last line inside that flushed message. Chat suppresses every
    aside, so without it the athlete reads nothing at all for the tens of seconds an LLM
    command spends silent. `openrouter._announce_wait` prints
    `Working on it — this usually takes about 40s.`, where the number is the median `ms`
    of recent successful `llm.call` records for the same label and model
    (`journal.llm_durations`) — no new storage, and no estimate at all until two past
    calls exist. A terminal gets the same number folded into the aside it already prints.
    `complete(..., wait_notice=False)` suppresses it for `tm bot route`, whose output
    nobody reads (DESIGN_output_verbosity.md §8).

### Package `trainmate/`

| File                 | Class / Singleton    | Purpose                                          |
|----------------------|----------------------|--------------------------------------------------|
| `types.py`           | —                    | TypedDicts: `Objective`, `Constraint`, `DailySignal`, `Workout` (the hydrated session, not a table row — §5), `CompletedActivity` (incl. `bike_avg_watts`, `zone1_sec`–`zone5_sec`, `power_zone1_sec`–`power_zone7_sec`), `AthleteMetric`, `AthleteBaseline`, `Macrocycle`, `Mesocycle`, `PlanFeedback`, `PlanProposal` |
| `config.py`          | `config`             | Reads `config.yaml`; exposes typed properties.   |
| `prompt.py`          | (`cli.prompt`)       | Front-end-agnostic prompt broker: `confirm`/`choose`/`ask_text` over `TtyPrompt` (`input()`) or `JsonPrompt` (chat/web). Journals every answer on the asking run (DESIGN_logging.md §5.6). See [§6](#6-singletons). |
| `db/`                | `db`                 | SQLite wrapper; `Database` composed from         |
|                      |                      | per-domain mixins. Full CRUD for all tables.     |
| `coach/honoring.py`  | —                    | Which coach pass owns a constraint, and whether  |
|                      |                      | one has happened. Two rules, one owner each:     |
|                      |                      | `covers`/`covered_ids`/`stamp` (who may claim    |
|                      |                      | `honored_at`, and the one write), and            |
|                      |                      | `constraint_window`/`needs_a_pass`/              |
|                      |                      | `constraints_needing_a_pass` (whether the plan   |
|                      |                      | reflects a directive yet — the `status` line,    |
|                      |                      | `constraint list`/`show` and the add-time nudge  |
|                      |                      | all ask HERE, so they cannot disagree).          |
|                      |                      | DESIGN_constraint_honoring.md §2/§4.             |
| `coach/revisions.py` | —                    | The pure helpers behind a `RevisionProposal`:    |
|                      |                      | `RevisionPair`, `pair_revisions`,                |
|                      |                      | `normalize_load_fields`, and                     |
|                      |                      | `structure_revision` (the row shape `adapt`      |
|                      |                      | writes). Apart from                              |
|                      |                      | `proposals.py`, which holds only the frozen      |
|                      |                      | records the coach hands the CLI.                 |
| `coach/`             | `coach_service`      | `service/` `CoachService` orchestrator +         |
|                      |                      | `engine/` `CoachEngine` pure logic +             |
|                      |                      | `formatting.py` prompt helpers. Both are         |
|                      |                      | packages of mixins (see [§3](#3-coach-package-architecture)). |
| `openrouter.py`      | `openrouter_client`  | HTTP client for OpenRouter; always expects       |
|                      |                      | `json_object` response. `.model` resolves lazily |
|                      |                      | on first use (see `llm_models.py`).              |
|                      |                      | Two objects in one reply (a model               |
|                      |                      | correcting itself): the last wins. Prints the    |
|                      |                      | wait notice before every call — see the flush    |
|                      |                      | protocol above.                                  |
| `llm_models.py`      | —                    | Which model to query: the `llm.models` config    |
|                      |                      | menu and the stored choice read back through the |
|                      |                      | registry (`list_models`, `active_model`,         |
|                      |                      | `resolve_token` — the validator for both model   |
|                      |                      | roles) — DESIGN_model_selection.md.              |
| `garmin/`            | module functions     | A **package** (`client`/`load`/`pmc`/`sync`), all |
|                      |                      | re-exported from `__init__.py` so `from trainmate |
|                      |                      | import garmin` and `patch.object(garmin, …)` are  |
|                      |                      | unchanged. `client.py` = login/fetch +            |
|                      |                      | `_derivation_pad_days`; `load.py` = the load model|
|                      |                      | (`measured_tss`, `activity_load`, `load_method`,  |
|                      |                      | `rpe_divergence`); `pmc.py` = `compute_pmc`,      |
|                      |                      | `load_ratio`, `recompute_derived`, `backfill_tss` |
|                      |                      | (see §12); `sync.py` = `pull`/`ensure_data`, the  |
|                      |                      | `sync_state` watermark, the per-process memo, and |
|                      |                      | the bridge that rides a Calendar daily-signal    |
|                      |                      | sync along with every pull (§13).                 |
| `google_calendar.py` | `calendar_syncer`    | Creates/updates/deletes all-day Google Calendar  |
|                      |                      | events for workouts (outbound), and ingests       |
|                      |                      | tagged signal events into `daily_signals` |
|                      |                      | (inbound — `sync_calendar_signals`, see §13).    |
| `calendar_reconcile.py` | —                 | The pass that makes Calendar agree with the      |
|                      |                      | workouts log after a change commits: per lineage, |
|                      |                      | push, retitle `[Deleted]`/`[Cancelled]`, or tear  |
|                      |                      | the event down (DESIGN_workout_revisions.md §8).  |
|                      |                      | `leaves_trace` is which removals keep their event |
|                      |                      | (DESIGN_plan_change_continuity.md §5.2). Scheduled|
|                      |                      | by the change handle, attached in `runtime`, so no|
|                      |                      | command carries Calendar code.                    |
| `calendar_lineage.py` | —                   | Renders a session's revision history as the      |
|                      |                      | `History` block of its Calendar event: every      |
|                      |                      | earlier form, newest first, with its date, load,  |
|                      |                      | target, reason and body                           |
|                      |                      | (DESIGN_calendar_lineage.md).                     |
| `adherence.py`       | —                    | `analyze_adherence()` + `classify_adherence()`   |
|                      |                      | pure functions; compare planned vs completed     |
|                      |                      | (the latter yields a per-workout verdict). Also  |
|                      |                      | exposes `planned_load()` (public), the expected- |
|                      |                      | load valuation `progression.py` reuses. The      |
|                      |                      | `pending_from` cutoff keeps an unfinished day's  |
|                      |                      | untrained sessions out of the misses (below).    |
|                      |                      | `STATUS_LABELS` is the one athlete-facing word   |
|                      |                      | per verdict, shared by the `workout list`        |
|                      |                      | marker, the web badge and the Calendar tag.      |
| `plan_diff.py`       | —                    | Compares two periodization plan versions:        |
|                      |                      | `resolve_versions` (which two, over a passed-in  |
|                      |                      | db handle) + `diff_plans` → strategy prose, each  |
|                      |                      | side's feedback notes (an append-only log is not  |
|                      |                      | prose-diffed), mesocycle fates, input deltas.     |
|                      |                      | Format-free, so `cli/plans.py` renders it as text |
|                      |                      | and `/api/plan/diff` returns it as JSON. Also     |
|                      |                      | owns `input_snapshots()` (the goals/constraints/  |
|                      |                      | threshold JSON columns), which `plan show` reads. |
| `plan_lineage.py`    | —                    | `plan_lineage(dbh, macros)` — the blocks of a set |
|                      |                      | of plans, flattened in the order they were        |
|                      |                      | trained; `delta_baseline(blocks, i)` — what block |
|                      |                      | `i` reports its change against, and nothing       |
|                      |                      | across a plan boundary (`--blocks` only; the      |
|                      |                      | strategy prompt wants the cross-season delta).    |
|                      |                      | The one place the "walk by macrocycle id, never   |
|                      |                      | by date" rule lives, shared by `tm progress       |
|                      |                      | --blocks` and the strategy prompt's planned-vs-   |
|                      |                      | actual review. DESIGN_plan_rollback.md §6.1       |
| `progression.py`     | —                    | Pure functions merging past (measured) + future  |
|                      |                      | (planned) load into one series and folding the   |
|                      |                      | stored CTL/ATL/TSB series forward across the seam |
|                      |                      | (`daily_loads`, `fitness_series` — reads stored   |
|                      |                      | rows + anchored fold, `weekly_aggregates`,        |
|                      |                      | `meso_bands`). Each week dict carries             |
|                      |                      | `sport_seconds` **and** `judged_sport_seconds`    |
|                      |                      | (only sessions clearing                           |
|                      |                      | `config.zone_min_activity_minutes`) — the latter  |
|                      |                      | is what the "trained but nothing recorded in this |
|                      |                      | currency" `!` marker reads, in both the CLI grid  |
|                      |                      | and `/api/zones`. `week_plan_denom` is the §3     |
|                      |                      | comparable-days rule (elapsed slice for the       |
|                      |                      | in-progress week, full planned total otherwise),  |
|                      |                      | shared by `cli/progress.py` and `chart.py` so the |
|                      |                      | table and the chart cannot disagree.              |
|                      |                      | `assemble_timeline` builds the                    |
|                      |                      | whole payload; `clip_payload` windows it (see §12,|
|                      |                      | §15, DESIGN_progress_timeline.md).                |
| `timeline.py`        | —                    | The one row-fetching path (`build_timeline_payload`) |
|                      |                      | behind `tm progress` and `/api/timeline.png`, so  |
|                      |                      | both surfaces assemble one identical payload.     |
| `chart.py`           | —                    | `render_timeline_png(payload)` — the single §2    |
|                      |                      | two-panel chart drawing (matplotlib, lazy import, |
|                      |                      | `Agg`), shared by the bot photo and the web PNG.  |
| `sports.py`          | —                    | Canonical sport vocabulary (`SPORT_MAPPING`,     |
|                      |                      | `CANONICAL_SPORTS`, `canonical_sport`,           |
|                      |                      | `sport_aliases`, `STRENGTH_SPORTS`);             |
|                      |                      | dependency-free so DB + adherence share it       |
|                      |                      | without a cycle.                                 |
| `intensity.py`       | —                    | Per-(mesocycle × canonical sport × currency ×    |
|                      |                      | zone) time in zone: `zone_rows` aggregates,      |
|                      |                      | `sport_durations` supplies its denominator,      |
|                      |                      | `pick_currency` chooses the one column a table   |
|                      |                      | is drawn in, `rate_window` supplies the          |
|                      |                      | completed-weeks divisor, `block_report` renders  |
|                      |                      | one block (rate table, coverage, caveats,        |
|                      |                      | block-over-block delta, current week, structural |
|                      |                      | row). `planned_zone_rows` /                      |
|                      |                      | `format_planned_zones` do the same for the       |
|                      |                      | coach's *prescribed* distribution (§9.8). No DB  |
|                      |                      | access — callers pass a `fetch(start, end)`      |
|                      |                      | callable, so `adapt`, the strategy prompt,       |
|                      |                      | `status` and `progress` share one implementation |
|                      |                      | (DESIGN_intensity_distribution.md).              |
| `clock.py`           | —                    | The athlete's timezone: `now()`, `to_local()` and  |
|                      |                      | the zone maths behind the `timezone` setting.      |
|                      |                      | `util.today_date()` is its caller — no other       |
|                      |                      | module calls `date.today()`                        |
|                      |                      | (DESIGN_user_timezone.md).                        |
| `settings.py`        | —                    | The preference registry: one `Setting` per knob    |
|                      |                      | the athlete can change at runtime, its validator,  |
|                      |                      | and the one resolver combining the stored row,     |
|                      |                      | `config.yaml` and the built-in default. The single |
|                      |                      | writer for every `settings` row                    |
|                      |                      | (DESIGN_settings.md).                              |
| `journal.py`         | —                    | The run journal: `logs/runs/YYYY-MM-DD.jsonl`,   |
|                      |                      | one JSON object per line, bracketed by a         |
|                      |                      | `run.start`/`run.end` pair per command. The      |
|                      |                      | operational record, as opposed to the domain     |
|                      |                      | one the tables hold. Read back in exactly one    |
|                      |                      | place: `llm_durations()` answers "how long does  |
|                      |                      | this command usually take" from past `llm.call`  |
|                      |                      | rows (DESIGN_output_verbosity.md §8). Owns the   |
|                      |                      | writer (one `os.write`                           |
|                      |                      | on an O_APPEND fd, records bounded at 8 KB,      |
|                      |                      | never raises), the module-level run stack, the   |
|                      |                      | reader, and retention. Imports nothing but       |
|                      |                      | `config`: the file name and every `ts` come from |
|                      |                      | the system clock in **UTC**, because asking      |
|                      |                      | `clock` for the athlete's day would open and     |
|                      |                      | migrate the database — on `tm help`, and inside  |
|                      |                      | the one path that must survive the database      |
|                      |                      | being unreachable (DESIGN_logging.md §4).        |
| `util.py`            | —                    | ANSI color helpers (`bold`, `green`, `red`, …),  |
|                      |                      | `cmd` (every "run X" call to action), `wrap_text`,|
|                      |                      | `format_labeled_text`, `strip_ansi`, `Progress`  |
|                      |                      | (self-erasing bar, silent off a terminal), and   |
|                      |                      | the four output verbs: `print` (the answer),     |
|                      |                      | `aside`/`asides_enabled` (a hint or standing     |
|                      |                      | caveat, terminal only,                           |
|                      |                      | DESIGN_output_verbosity.md), `step` (what the    |
|                      |                      | app is doing right now — prints exactly where    |
|                      |                      | `aside` prints, and journals it), `warn`/`fail`  |
|                      |                      | (an operational warning or failure — always      |
|                      |                      | prints, folds in the colour and the `Warning: `/ |
|                      |                      | `Error: ` prefix, journals at `warn`/`error`).   |
|                      |                      | A domain refusal ("no active plan") is an answer |
|                      |                      | and keeps its own `print` (DESIGN_logging.md     |
|                      |                      | §5.1/§5.3). Also                                 |
|                      |                      | `fmt_date`/`fmt_span`/`fmt_timestamp` — the one  |
|                      |                      | date renderer for every surface: a displayed day |
|                      |                      | carries its abbreviated weekday                  |
|                      |                      | (`2026-06-05 Fri`). Mirrored in the dashboard as |
|                      |                      | `fmtDate`/`fmtSpan` in `static/app.js`. Two      |
|                      |                      | places stay bare for want of room: `progress`'s  |
|                      |                      | `MM-DD` week column (48-column budget) and the   |
|                      |                      | `data bootstrap` reconstruction windows.         |

### Change recipes (where to edit for a given task)

Start here when you know *what* you want to change but not *which files*. The data
flow for each lives in [§10](#10-key-data-flows).

| To change…                       | Edit these                                                                 |
|----------------------------------|----------------------------------------------------------------------------|
| Daily adaptation logic           | `coach/service/adaptation.py:workout_adapt*`, `coach/engine/workouts.py:_workout_adapt_logic`, prompt helpers in `coach/formatting.py` ([§10](#daily-adaptation-workout-adapt)) |
| Plan / strategy generation       | `coach/service/planning.py:plan_generate`, `coach/engine/planning.py:_plan_generate_strategy` ([§10](#plan-generation-plan-generate)) |
| Plan version comparison / display | `trainmate/plan_diff.py` (comparison + snapshot parsing), `cli/plans.py` (text rendering), `/api/plan/diff` in `trainmate_web.py`, `loadPlanDiff()`/`render*` in `static/app.js` |
| Plan feedback (the athlete's notes on the plan) | `db/periodization.py` (`add_/list_/get_/rm_plan_feedback` over the `plan_feedback` table), `cli/plans.py:run_plan_feedback` + `cli/selectors.py:resolve_meso_atom` (the `-m` atom), `coach/service/planning.py` (the regen gate disjunct + prompt assembly), `coach/engine/planning.py` (the prompt section), DESIGN_plan_feedback.md |
| Workout generation span          | `coach/service/workouts.py:workout_generate`, `cli/workouts/generate.py:_resolve_span`, `cli/workouts/parser.py` (flag parsing), `config.workout_generation_span_days` |
| Commitment window                | `settings.commitment_days`, `coach/service/workouts.py:_commitment_window`/`_standing_block`/`_resolve_standing`, `coach/formatting.py:format_standing_workouts`, `calendar_reconcile.py:leaves_trace`, `workout_changes.commitment_end` |
| Telling the athlete a plan-shaping input changed since the plan was built | `config.py` (`plan_profile`/`changed_plan_profile_fields`/`plan_config_hash` — the partition and the fields that moved), `coach/service/prompt.py:config_changed` (the judgment, both axes), **`cli/staleness.py`** (canonical for everything the athlete sees: the reason, the §2 test said out loud, the re-stamp, and the four surfaces' shared wording), and the surfaces that draw it: `cli/plans.py` (`plan show` reports, `plan keep` dismisses, `plan generate` offers), `cli/workouts/generate.py`, `cli/status.py` (a pointer to `plan show`, nothing more), `trainmate_web.py` (a read-only banner off `plan_config_hash()`, deliberately not through the engine — §8). Built in **one** place for the same reason the runway nudge is: three call sites each phrasing a two-sentence explanation is how they drift (DESIGN_plan_staleness.md §9) |
| Telling the athlete the schedule is running out | `progression.py` (`coverage_end`, `runway` — the pure detector and its four kinds), `cli/runway.py` (the row fetch, every wording, the morning-push button), and the four surfaces that draw it: `cli/workouts/generate.py` (`workout adapt`'s hint and refusal, `workout list`'s marker), `cli/status.py`, `cli/bot.py:run_bot_morning`, `config.runway_warning_days`, DESIGN_runway_nudge.md. The wording is built in **one** place on purpose — the hint used to live on `workout adapt` alone, which is how `status` came to answer differently on the same morning (§3 of that doc) |
| Generation covering every date of its span | `coach/engine/workouts.py` (the TASK sentence), `coach/service/workouts.py:_fill_coverage_gaps` (the deterministic backstop, over the same `_rest_workout` factory the rest-window pre-pass uses), DESIGN_runway_nudge.md §2.1. The invariant is what lets the end of the schedule be read straight off the rows, with no margin |
| Knowing whether the plan reflects a constraint | `coach/honoring.py` (**canonical** for `honored_at`: what it means, who may stamp it, the write, and `needs_a_pass` — whether the plan is missing a directive at all), `coach/proposals.py` (`covered_constraint_ids`, decided at proposal time on both proposal types so apply never re-derives it), `coach/service/adaptation.py` + `coach/service/workouts.py` (the two stamping commands), `cli/constraints.py:_maybe_point_at_honor` (the add-time message naming the block and the run that would build it in), `cli/status.py`, `cli/common.py:constraint_line`/`report_unhonored`, `db/constraints.py` (`mark_honored`, `clear_honored`, `clear_honored_after`), DESIGN_constraint_honoring.md. There is deliberately **no dedicated command** and no SQL half-copy of the predicate in `db/` — §5 of that doc records why |
| Coach-learnings / confidence     | `db/learnings.py`, `coach/service/prompt.py` (`_apply_learning_updates`), model is **canonical** in [§3](#3-coach-package-architecture) |
| Backward analysis (bootstrap/reflect) | `coach/service/analysis.py:_run_workout_analysis`, `coach/engine/analysis.py:_data_analyze_logic` ([§10](#data-analysis-data-bootstrap--data-reflect)) |
| Garmin pull / metrics / load model | `trainmate/garmin/sync.py` (`pull`, `ensure_data`), `garmin/load.py` (`activity_load`), `garmin/pmc.py` (PMC + `recompute_derived`), see [§12](#12-sports-science--coaching-mathematics) |
| Progress timeline / PMC projection | `trainmate/progression.py` (pure math), `trainmate/timeline.py` (shared row-fetch), `trainmate/chart.py` (PNG), `cli/progress.py` (text), `/api/timeline.png` in `trainmate_web.py`, see [§12](#fitnessfatigueform-pmc-model), DESIGN_progress_timeline.md |
| Intensity distribution / time in zone | `trainmate/intensity.py` (aggregation + prompt-width rendering + which sports qualify and in which currency — `window_sport_stats`/`select_zone_sports`/`zone_currency`, shared by the CLI tables and `/api/zones`), `coach/service/context.py` (`_intensity_block_context` for adapt, `_intensity_history_context` for the strategy prompt, `_block_progress_context` for workout generate — the only consumer passing `block_report`'s `previous=` and `fetch_workouts=`, since block-over-block creep and measured-vs-prescribed attribution are periodization questions (§9.2a), `_planning_zone_currencies` for §9.8), `cli/status.py`, `cli/progress.py` (the weekly grid — it shares the load table's week column and 48-column budget), `progression.weekly_aggregates` (where the rows join the payload), `cli/data.py` (`--zones`), `/api/zones` + the Progress tab's tables in `static/app.js`, DESIGN_intensity_distribution.md. Undercount markers are proportional: `intensity.judgeable` (`config.zone_min_activity_minutes`) withholds a too-short session's vote, and the coverage bar is per sport (`intensity.COVERAGE_MIN_BY_SPORT`, overridable via `config.zone_coverage_display_min_by_sport`) because rest between sets is not a failed recording. Both maps' keys must be **canonical** sports — `coverage_display_min()` canonicalizes before the lookup, so an alias key is dead and silently reverts to the global bar |
| Planned time in zone (a session's intensity target) | `db/base.py` (`planned_zone_currency`, `planned_zone1..7_sec` on `workouts`), `db/workouts.py:WorkoutChange.append`, `intensity.parse_planned_zones` / `format_planned_zones`, `coach/engine/workouts.py` (`_planned_zone_task`, `_planned_zone_fields` — both prompts), `google_calendar.py` + `coach/formatting.py` (rendered from the columns, never stored), DESIGN_intensity_distribution.md §9.8 |
| Calendar push / daily-signal ingest | `trainmate/google_calendar.py`, see [§13](#13-daily-signal-calendar-ingest) |
| Workout state (modified/calendar/removed) | `trainmate/calendar_state.py`, `db/workouts.py`, `cli/workouts/_helpers.py::modification_markers` ([§5](#workout-state--three-orthogonal-axes-not-one-enum)) |
| What became of a planned session (the adherence verdict) | `adherence.py` (`classify_adherence` + `STATUS_LABELS`, the vocabulary), `cli/common.py` (`adherence_results` — the one DB-backed pairing — `adherence_verdicts` keyed by workout id, and `format_actual` for the effort it graded against), `cli/workouts/_helpers.py::adherence_marker` (the marker `workout list` prints), `cli/workouts/generate.py::_list_verdicts` (which span the listing grades, and the pull it needs), `google_calendar.py` (title tag), `/api/workouts` + `renderWorkoutCard` in `static/app.js` (the badge) ([§5](#workout-state--three-orthogonal-axes-not-one-enum)) |
| Which timezone dates are read in | `trainmate/clock.py` (the zone, the cache, the fallback), `util.today_date`/`fmt_timestamp` (the only callers), the push loop in `trainmate_bot.py`, DESIGN_user_timezone.md. Changing it is one row of `settings` |
| A preference the athlete can change at runtime | `trainmate/settings.py` (the registry: one `Setting`, its validator, its config key, its cache hook), `cli/settings.py` (the listing and the two rich detail views), and the reader that consumes it — `llm_models.active_model`, `clock.active_zone`, or a named reader in `settings.py` for the morning-push knobs. Adding one is a registry entry, not a command, DESIGN_settings.md |
| A CLI command                    | `trainmate/cli/<family>.py` (`run_*`), dispatcher in `trainmate_cli.py` ([§7](#7-cli-commands-reference)) |
| A message telling the athlete to run something | wrap the command in `util.cmd()`, nested *inside* the line's colour call, so it renders as the bright shade of that colour — and emit it with `util.aside`, not `print`: a "you could now run X" hint is side information |
| Whether a line reaches the chat front-end | `util.aside` (side information, terminal only) vs `print` (the answer, warnings, errors). Building a list of lines rather than printing? gate on `util.asides_enabled()`. DESIGN_output_verbosity.md §3 |
| Recording that something happened | Nothing new to call: `util.step` (what the app is doing), `util.warn`/`util.fail` (something outside the app did not work) print and journal in one go, and `run_once` already brackets the command. Reach for `trainmate/journal.py` directly only for a record with structured fields (`journal.record("garmin.pull", …)`) or for what must never reach the athlete (`journal.debug` — the tier that replaced `except Exception: pass`, pinned by `tests/test_journal.py`). The event name comes from the closed nine-word vocabulary in `journal.EVENTS`; severity is `lvl`, not a new name. Never copy something a table already holds — that is the domain record, and it outlives this one. What the athlete *answered* needs nothing at all: `runtime.prompt` journals every `confirm`/`choose` itself (§5.6). DESIGN_logging.md §2/§4.2/§5 |
| Reading back what a command did  | `tm journal` (`trainmate/cli/journal.py`), or `logs/runs/*.jsonl` with `jq`. A run's prompts are `logs/llm_exchanges/*<run id>*` — the id in the filename is the join, not the timestamp, because those names come from the machine's local clock while the journal is UTC. DESIGN_logging.md §6/§7 |
| How long the coach's prose is    | `coach/engine/prompt.py` (`## WRITING FOR THE ATHLETE`, shared by every command built on `_build_system_prompt`) + the per-field caps in each `## RESPONSE FORMAT`. Check `coach/formatting.py` first: a field re-injected into later prompts must not be capped (DESIGN_output_verbosity.md §5.1) |
| A web *view* of existing data    | a GET in `trainmate_web.py` + a panel in `static/app.js` ([§8](#8-web-api-endpoints)) |
| A web endpoint that would *write* | it does not go in the web app — add the CLI command instead ([§8](#8-web-api-endpoints)) |
| The Telegram bot                 | `trainmate_bot.py` (runs the CLI as a subprocess) ([§2](#entry-points)) |
| DB schema / a new column         | the relevant `db/*.py` mixin + the table in [§5](#5-database-schema) |

---

## 3. coach Package Architecture

The `trainmate/coach/` package re-exports its public API from `__init__.py` (so
`from trainmate.coach import coach_service` keeps working) and is split into
three submodules:

- `formatting.py` — pure prompt-formatting helpers (no I/O, no LLM):
  `format_metrics_history`, `format_completed_activities`,
  `format_planned_workouts`, `format_planned_workouts_detailed` (adapt-only
  variant that includes each session's full description so the model preserves
  interval/rest detail it isn't deliberately changing, tags a session already
  trained `[COMPLETED — locked history, not adaptable]` or `[PARTIAL — …]` with
  what was actually performed, from `adherence.performed_sessions`
  ([§15](#a-session-already-behind-us-carries-its-verdict)), and tags
  already-eased sessions `[ALREADY EASED …]` with
  recency/count from `adapted_at`/`adaptation_count` so a re-run doesn't compound
  the cut), `format_removed_workouts`, `format_daily_signals`
  (renders the window's `daily_signals` rows into the adapt prompt),
  `format_baseline`, `_load_science_guidelines`.
- `engine/` — `CoachEngine` (prompt construction, hashing, LLM calls), assembled
  from mixins (`prompt`, `planning`, `workouts`, `analysis`). Owns the
  `openrouter_client` binding — **patch target for tests:**
  `trainmate.coach.engine.openrouter_client`.
- `service/` — `CoachService` + the `coach_service` singleton (data I/O,
  caching, orchestration), assembled from mixins (`context`, `prompt`, `planning`,
  `workouts`, `adaptation`, `editing`, `analysis`). Owns the `db` /
  `calendar_syncer` / `config` bindings — **patch targets:**
  `trainmate.coach.service.db`, etc. Both packages re-export everything from their
  `__init__.py`, so the patch targets and import paths are the flat ones above.

### `_load_science_guidelines(app_science_dir, science_dir) → str`
Module-level function in `formatting.py`. Concatenates all `*.md` files from
`trainmate/science/` (built-in) and `science/` (user-provided), **one `====` banner per
source** so the coach can tell whose material it is reading, and so the documents' own
markdown headings are visibly not the prompt's (`DESIGN_prompt_structure.md` §3). The
banners are emitted here, not by the three call sites. Returns `""` when neither directory
holds documents, so no empty banner is produced. Called by
`CoachService._load_science_guidelines()`.

**The two corpora are layered, and the layering is one-directional.**
`trainmate/science/` (built-in) owns *how to measure and what the words mean*; `science/`
(user) owns *what to do*. So a user document may cite a built-in one; a built-in one may
never cite a user document by name, because the user corpus is gitignored and may be empty
or contain anything. Concretely, a built-in file must not state a duration, a loading ratio,
a taper magnitude, a session count, or a block order — those are the user layer's to set,
and a built-in file that fixes one silently overrides the philosophy the athlete supplied.
Each built-in file therefore opens with an `AUTHORITY:` line naming its role
(`VOCABULARY` / `DIAGNOSTIC` / `MEASUREMENT` / `PROTECTIVE`), and the user files with the
authority they claim over each other (`PRESCRIPTIVE` / `REFERENCE ONLY`). The same
precedence is stated to the coach at run time, in each block's `provenance` line — the file
headers alone would not tell it which corpus yields, and the user corpus can be empty or
say anything.

The one carve-out: **a built-in file may hold a hard rule when that rule can only ever
reduce load.** `recovery_metrics.md` is protective in whole, and `training_load.md` §5's
final directive is flagged inline as a floor. Anything that would add or sustain load is a
default the user layer overrides.

### `CoachEngine`
**Pure business logic — no DB or I/O.** All methods are prefixed `_` (called by
`CoachService` or directly by tests).

- **`_build_system_prompt(...)`** — assembles the main LLM system prompt
  (guidelines, strategy, goals, constraints, athlete profile). Every prompt in the app —
  system and user message alike — uses one section hierarchy: `## NAME` for a top-level
  section, `### NAME` for a sub-section of `## TASK`, and a `====` banner only around a
  verbatim quoted document (`DESIGN_prompt_structure.md` §2). `_render_constraints`
  renders the active directives block (`title | dates | enforcement | description`, where
  enforcement is "no training (rest enforced)" or "advisory").
- **`_format_athlete_profile(profile)`** — formats the (effective) profile into a
  readable prompt segment. Threshold anchors render generically from
  `trainmate.benchmarks.ANCHOR_KINDS` (label + unit), so a new anchor kind shows up with
  no prompt-code change (`DESIGN_benchmark_workouts.md` §3.5).
- **`_clean_goals(objectives)` / `_clean_constraints(constraints)`** — the
  planning-relevant fields, normalized and stably ordered. Single source of truth
  shared by the hash functions and the snapshots persisted on the macrocycle.
  `_clean_constraints` is fed only the plan-shaping (replan=1) constraints.
  `date_type` is included only when `horizon`, so goals predating the field (all
  `event`) keep their hash and no plan flags stale on upgrade; flipping a goal
  either way still changes the hash ([§15](#goal-dates-event-vs-training-horizon)).
- **`_get_goals_hash(objectives)`** — SHA-256 of the `_clean_goals` list.
- **`_get_constraints_hash(constraints)`** — SHA-256 of the `_clean_constraints` list.
- **`_get_config_hash()`** — SHA-256 of `_clean_profile()`: the `user_profile` block minus
  the threshold anchors (`max_hr`/`lthr`/`ftp`) and minus the fields that reach the prompt
  but cannot shape a periodization — `name`, top-level `equipment`, and each day's
  `equipment` within `weekly_schedule` (that day's hours, `max_sessions` and
  `certainty_percent` stay in). The partition and its rationale are
  `DESIGN_plan_staleness.md` §3–§4; the exclusions are a denylist so a profile field added
  later counts as plan-shaping until someone decides otherwise (§6). Thresholds are instead
  snapshotted raw on the macrocycle (via `CoachService.effective_thresholds()`) and only
  flag the plan stale past `coach.threshold_replan_pct` relative drift (default 5%) — see
  `CoachService.config_changed()`. Trainable thresholds now live in the `benchmark_results`
  logbook, not config (`DESIGN_benchmark_workouts.md` §3.4); the service overlays them onto
  the profile for prompts and the drift snapshot. Prompt-context knobs
  (`metrics_lookback_days`) are not fingerprinted at all.
  The macrocycle also stores `profile_snapshot` — the plan-shaping fields as JSON — so the
  staleness reason can name what moved (`"athlete profile changed: sport_preferences"`)
  rather than only that something did (`DESIGN_plan_staleness.md` §5).
- **`_plan_generate_strategy(...)`** — LLM call → `{strategy, mesocycles}` covering the
  plan start through the goal date, whatever the horizon. Label `plan_generate`.
  Branches on the goal's `date_type`: a `horizon` goal's task forbids pinning a
  peak/taper/realization phase to the date and drops the `Peak & Taper, Race/Event`
  phase examples ([§15](#goal-dates-event-vs-training-horizon)).
  Branches again on `current_block`, the mesocycle the athlete is mid-way through: when
  one is offered, the task quotes it and lets the model either keep it as the first
  mesocycle **at its original start date** or discard it and start on the plan start,
  stating which in the strategy. A block re-dated to today would contain none of the
  sessions already trained under it, since blocks own their sessions by date containment
  (`DESIGN_block_progress.md` §7).
  Builds its own system prompt rather than calling `_build_system_prompt`, which states
  the ACTIVE strategy and blocks as settled fact — the very artifact this call produces;
  the plan prompt shows the *previous* strategy instead. It takes `learnings` explicitly
  for that reason: the shared builder's `COACH LEARNINGS` section is the one part it does
  want, and without the argument this was the only coach call blind to the observations
  the analysis flow authors (DESIGN_backward_evaluation.md §10.1).
- **`_plan_reshape_verdict(change_reason, diff_text, strategy, mesocycles)`** — the
  cheap preliminary behind the staleness question: given what changed and the plan in
  force, would the coach reshape the periodization? → `{reshaping, why}`. Label
  `plan_verdict`. Called only through `CoachService.plan_reshape_verdict` (below).
- **`_workout_generate_logic(...)`** — LLM call → `{reasoning, workouts[]}`. Accepts
  `num_days` (default 28) driving the horizon and `start_str` (defaults to today) for
  the first day to schedule — the prompt tells the model to begin there. When
  `block_progress` is supplied it appends a `CONTINUING A BLOCK ALREADY UNDER WAY`
  section (`_block_progress_task`) and renders that data as the first user-content
  section, so a mid-block regeneration continues the block's ramp instead of restarting
  it, does not repeat a deload already taken, and does not re-place a fitness test the
  block already ran — that clause **bounds** the otherwise-unconditional BENCHMARK
  PLACEMENT rule (DESIGN_block_progress.md §4). With `block_has_intensity` it further
  appends `JUDGING THE BLOCK'S COMPOSITION` (`_block_composition_task`), the other end of
  DESIGN_intensity_distribution.md §9.4's handoff (§9.2a): composition is generate's, but
  only after attributing the divergence — measured-vs-prescribed decides whether the plan
  or the athlete is off, and re-shaping the block around a mis-execution is forbidden by
  name. The flag rides separately from `block_progress` because that section quotes the
  zone tables and must not be promised when they have no rows. **Read-only** w.r.t.
  learnings. Label `workout_generate`.
  Its TASK also states the coverage invariant — every date of the span carries an entry,
  a rest day explicitly (DESIGN_runway_nudge.md §2.1) — and
  `coach/service/workouts.py:_fill_coverage_gaps` backstops it deterministically, so the
  end of the schedule can be read off the rows rather than guessed at.
- **`_workout_adapt_logic(...)`** — LLM call →
  `{change_needed, reason, adapted_workouts[]}`. **Read-only** w.r.t. learnings.
  The TASK states its cross-cutting rules once, as `STANDING RULES` immediately after
  the decision branches; the sections below cite them rather than restate them, so a
  rule has one wording and cannot drift copy by copy (DESIGN_adapt_task_prompt.md §2).
  Within `config.adapt_terminal_window_days` of the block's end it appends a
  `THIS BLOCK IS ENDING` section biasing the model toward holding load, since a cut
  there cannot rebound (DESIGN_block_boundary.md §3). There is **no separate
  classification pass** for `--message`: the same call also extracts any
  constraint-shaped directives from the note and returns them as `new_constraints`
  (DESIGN_constraints.md §8). Label `workout_adapt`.
- **`_data_analyze_logic(...)`** — LLM call → `{macrocycle_summary,
  inferred_macrocycle, inferred_mesocycles[], physiological_insights[],
  learning_updates[]}`. Reverse-engineers cycles from weekly summaries. Label
  `workout_analysis`.

**Coach learnings via evidence-cited deltas:** only `_data_analyze_logic`
(the `data bootstrap`/`data reflect` flow) emits a `learning_updates` array
(shared prompt field `LEARNING_UPDATES_FIELD`). `_workout_generate_logic` **and**
`_workout_adapt_logic` are **read-only** — they consume the rendered learnings but author
none (DESIGN_backward_evaluation.md §11; DESIGN_evidence_based_confidence.md §2).
The app owns the merge via `CoachService._apply_learning_updates()` →
`db.apply_learning_deltas(deltas, available_weeks, source)`, so a model that omits
an existing learning cannot lose it. Deltas the app cannot act on are still skipped, but
the merge returns `{"applied", "skipped"}` and a non-zero skip is reported — otherwise
"the model authored nothing" and "the app understood nothing" look the same
(DESIGN_backward_evaluation.md §13). Each learning carries a **sport scope**
(`sports`) and an **app-computed confidence** (`tentative`/`moderate`/`established`).
The LLM **no longer sets confidence** — it only attributes each observation to the
training **week(s)** it was shown (`week_commencing` Mondays). The five ops:
- `{"op": "add", text, sports?, evidence:[weeks]}` — new record; seeds its
  supporting basis from `evidence`; confidence derived.
- `{"op": "revise", id, text?, sports?, evidence?:[weeks]}` — edit fields; if
  `evidence` given, also adds supporting weeks.
- `{"op": "reinforce", id, evidence:[weeks]}` — add supporting weeks (no
  reword). **No `evidence` ⇒ no-op.**
- `{"op": "contradict", id, evidence:[weeks]}` — add contradicting weeks; may
  trigger a *proposed* demotion.
- `{"op": "retire", id}` — hard delete (basis cascades).

Cited weeks are validated against `available_weeks` (the analysed window's
Mondays); weeks outside it are dropped (skip-malformed philosophy).

**Confidence = f(evidence basis)** (DESIGN_evidence_based_confidence.md §3).
`net = distinct supporting weeks − distinct contradicting weeks`; thresholds
(`config.learning_confidence_thresholds`, default moderate 3 / established 5) map
`net` to a level. The retirement sentinel is returned only when `net ≤ 0` **and**
`contradicting_weeks > 0`: a learning with no basis at all rests at the `tentative`
floor, without which every freshly added learning would be proposed for retirement on
its first recompute. **Upgrades auto-apply; downgrades are proposed, not applied** —
`db._recompute_confidence()` writes the lower level to `proposed_confidence` and
leaves the live `confidence` intact. Re-citing counted weeks is a structural
no-op (the `UNIQUE(learning_id, week_commencing, polarity)` constraint), so re-running /
`--force` / overlapping windows cannot inflate confidence. `last_reinforced_at`
refreshes only when a *new* supporting week lands (or on a staleness demotion).

**Decay (soft) + staleness demotion:** a learning is *dormant* once unreinforced
past its confidence budget (`config.learning_staleness_days`: tentative 21d / moderate
60d / established 180d, via `db.learning_is_dormant()`). Dormant records stay in
the DB, are listed by `learnings list` (marked), and are **excluded from prompts**. Crossing the
budget also **proposes a one-level staleness demotion** (`derive_staleness_proposals`);
accepting it re-arms the clock at the lower (shorter) budget, so an untouched
learning walks established → moderate → tentative → retire over real time.

**Propose / confirm flow:** pending downgrades (contradiction- or
staleness-driven) are resolved **interactively** at the end of `data
bootstrap`/`data reflect` — accept (`db.demote_learning`), keep
(`db.keep_learning` — dismiss + affirm: drop the −1 rows for a contradiction, or
refresh recency for staleness), or skip — or out of band via `learnings demote
<id>` / `learnings keep <id>`. `--auto` skips the prompts: staleness demotions
apply directly; contradiction demotions stay queued for the next interactive
review. The bottom rung differs by mode: under `--auto` there is no queue to park the
last step in, so a dormant *tentative* learning is **hard-DELETEd** from
`coach_learnings` (basis cascading); interactively the same learning stops one rung
earlier, as a pending `retire` proposal.
`CoachService._get_learnings_text()` renders only active learnings as
`[id|sports|confidence] text` into every using flow's prompt.

**Inspection / curation:** the `learnings` command family is the home for viewing
and hand-curating records — `list` (filters: `--sport`/`--confidence`/`--dormant`),
`show <id>` (full text + per-week evidence basis), `edit`, `rm`, `demote`, `keep`,
`wipe`. `status` carries only a one-line summary (`N active, M dormant, K pending
demotion`) pointing at `learnings list`.

**Cold-start nudge:** when there are no active learnings, `plan generate` and
`status` suggest running `data bootstrap` (the only flow that authors learnings).

**Migration:** learnings predating the evidence model are grandfathered with a
synthetic supporting basis sized to sustain their stored level
(`db._grandfather_learning_evidence()`, source `migration`), so the first
recompute does not silently demote them (see [§15](#15-design-rationale--history)).

### `CoachService`
**Orchestrator — owns all DB and calendar access.** Exposes the public API
called by the UIs.

- **`plan_generate(force, objective_id, auto_apply, fresh, start_date, show_context)`** —
  fetches objectives/constraints, checks hashes (constraints hash covers only the
  plan-shaping `replan=1` rows), calls `CoachEngine._plan_generate_strategy()`, returns a
  `PlanProposal`. Saves to DB only
  under `auto_apply`; otherwise the caller passes the proposal to `plan_apply()` once
  the athlete accepts. The plan window has no minimum or maximum length (§10, step 5).
  `fresh` (CLI `--fresh`) withholds the `PREVIOUS PERIODIZATION STRATEGY` block and its
  `CONTINUITY WITH THE PREVIOUS PLAN` instruction, so the strategy is written without the
  plan in place to build on; it implies `force`, since there is nothing to reuse when the
  point is to depart. Everything derived from what the athlete *did* is unaffected — the
  planned-vs-actual review still covers the replaced plan's elapsed blocks, as do the
  history summary, the learnings and the athlete's plan feedback
  (DESIGN_backward_evaluation.md §6.1).
- **`workout_generate(start_date, end_date, prefer_macro_id)`** — requires an existing
  macrocycle.
  Writes nothing: it returns a `GenerateProposal` (reasoning, the proposed sessions
  already tagged with their date's `macrocycle_id`, the live plan they would displace,
  and `gen_start`), which the caller previews and hands back to
  `workout_generate_apply()` once the athlete accepts — the same
  propose-then-apply split as `plan_generate`/`plan_apply`.
  Applies the deterministic rest-window pre-pass to the generated workouts
  (`_enforce_rest_windows_generate(workouts, constraints, gen_start, gen_end)`: every
  `rest = 1` date in the *requested* span `[gen_start, gen_end]` — not the span the model
  happened to return — becomes a single rest session, **including
  dates the model returned nothing for** — an absent row and an explicit rest day mean
  different things to adherence. Every other constraint is advisory, left to the model)
  before proposing, so the forced rest days are visible in the preview.
  - **Preserves a completed session:** if today's planned workout already has a
    matching completed activity (`_today_workout_completed`, a one-day
    `analyze_adherence` pass), generation starts *tomorrow*; otherwise today. A
    defensive filter drops any model-emitted workout dated before the start.
  - **Horizon:** `num_days` from `end_date` (or `config.workout_generation_span_days`)
    relative to the start, then `CoachEngine._workout_generate_logic()`.
- **`workout_generate_apply(proposal)`** — the accepted half. Archives the previous
  proposed sessions under one `generate` change, voiding every day from `gen_start` the
  new plan does not fill and appending the rest with the `macrocycle_id` the proposal
  already carries. It touches no Calendar code: the change handle's reconcile pass makes
  the calendar mirror the active plan when the change commits, under one summary line and
  a progress bar. Appending (not deleting) makes regeneration undoable via
  `workout_rollback` or `plan_rollback` (DESIGN_workout_revisions.md §10).
- **`plan_apply(objective_id, strategy, mesocycles, fingerprints=None)`** — persists an
  already-generated strategy + mesocycles, returning the goal id it saved under.
  `fingerprints` are the goals/constraints/config hashes and snapshots taken when the
  strategy was generated (`PlanFingerprints`, `coach/proposals.py`) and are stored
  verbatim. Omitting them re-reads the inputs as they are *now*, which records an edit
  made between generating and accepting as though the strategy had been built from it —
  exactly the drift the staleness detector exists to catch. Only callers applying a plan
  they did not just generate should leave it unset.
- **`replan(force, objective_id)`** — convenience: `plan_generate` (auto-applied) then
  `workout_generate` + `workout_generate_apply`, with no preview — the unattended path
  ([§11](#11-terminology-plans-vs-workouts)).
- **`workout_adapt(target_date_str, message=None)`** — fetches metrics + workouts in
  the rolling window, calls `CoachEngine._workout_adapt_logic()`, returns a
  `RevisionProposal` (`coach/proposals.py`) carrying `reason`, `workouts`, `pairs`/
  `removals` and the two candidate lists below; caller decides whether to apply.
  - **`message`** (CLI `-m/--message`): a fast-capture inbox. There is no separate
    classification call — the same adapt call may return two kinds of candidate
    extracted from the note, both raw and **unconfirmed**, both gated on the same
    `has_message` flag:
    - `new_constraints` — constraint-shaped directives (DESIGN_constraints.md §8). The
      CLI confirms each with the athlete, then persists it via
      `capture_message_constraint` (singular, one call per confirmed candidate) as a
      `constraint` row (`source='message'`, `replan=0`, honored this run and every
      future run). Auto-capture never sets `replan=1` — a plan-shaping capture only
      *surfaces a suggestion* to escalate.
    - `new_signals` — the external causes acting on the athlete on given days
      (DESIGN_signal_extraction.md). Confirmed one at a time before the adaptation
      preview, then written through `capture_message_signal` → `signals.write_signal_days`,
      the same calendar-first path `signal add` uses ([§13](#13-daily-signal-calendar-ingest)).
      `value` survives only when it is a real number, so a model's guess cannot enter
      the quantitative path as a measurement.
  - **Rest-window pre-pass** (`_enforce_rest_windows_revision`): eases any future,
    not-yet-completed planned session under a `rest = 1` constraint to rest, regardless
    of the model's proposals.
  - **Block firewall (write side):** any proposal dated past the block end is dropped —
    the next block was never shown to the model, so a post-boundary date is a
    hallucination ([§15](#15-design-rationale--history), DESIGN_block_boundary.md §1).
  - **Requires a block:** `workout_adapt` raises "No active periodization strategy found"
    when `get_active_mesocycle` returns nothing, as `workout_generate` does. Adapt's
    judgements are all relative to the block, so there is nothing to adapt towards
    without one (DESIGN_block_boundary.md §6).
  - **Constraint magnitude** (`constraint_plan_impact` / `constraint_is_plan_shaping`):
    the §7 heuristic behind the `constraint add`/`edit` replan proposal. Two independent
    triggers, either firing: displaced planned load ≥ `config.replan_displaced_load_pct`
    of the trailing week's planned load, or a `rest` window spanning ≥
    `config.replan_rest_span_days`. Deliberately **no key-session term** — TrainMate has
    no priority field at all. Human-confirmed, never auto-regen.
  - **Only-changes contract:** the prompt shows the whole forward plan through the
    mesocycle end but instructs the model to return **only sessions it is changing** —
    omitted sessions are preserved (apply never drops a date with no proposal). A date the
    model *does* touch is the exception: it keeps only the sports named on it, so a
    same-day session of another sport is named with a **keep marker** —
    `{date, sport_type, keep: true}` and nothing else (DESIGN_workout_revisions.md §9.1).
  - **Locked history:** any proposal targeting a `(date, sport)` that already has a
    matching completed activity (incl. one done earlier today) is dropped — a finished
    workout is never "adapted".
  - **No-op backstop** (`_revision_is_change`): a proposal reproducing an existing
    same-sport session on every meaningful field (title, description, duration/RPE/TSS —
    whitespace- and int/float-insensitive) is **held, not dropped**: no revision row, but
    it still counts as proposed on its date, so the displacement rule cannot void the
    session the re-list was protecting. Held slots travel on `RevisionProposal.held`, which
    both the preview and apply union into the date's proposed sports (§9.1).
- **`workout_revision_apply(proposal)`** — the accepted half of `workout_adapt`. Opens
  one `adapt` change: a session the pass drops becomes a **void** revision (never a
  `DELETE`), a cross-sport substitution is a void at the source plus a revision at the
  destination on the same lineage, and every revised session appends with its own
  `modification_reason`; the batch `reason` lands on the `workout_changes` row. Voids go
  first. Touches no Calendar code — the change handle's reconcile pass does — and stamps
  `honored_at` on the constraints the proposal covered (`honoring.stamp`). Nothing is
  stamped on the row for recency: whether a revision counts as an easing is derived at
  read time from the lineage ([§5](#workout-state--three-orthogonal-axes-not-one-enum)).
- **`workout_revision_record_no_change(proposal)`** — the other outcome. An adapt that
  looked at the metrics and held still writes its `workout_changes` row (appending
  nothing) and stamps `honored_at`, so "no adaptation needed" is a recorded event and a
  valid honoring claim ([§10](#daily-adaptation-workout-adapt), step 6).
- **`workout_add(date, sport_type, title, description, …, replace_day=False)`** —
  manually schedules a workout (deterministic, no LLM), **replacing** any same-sport
  session that day — or every session that day with `replace_day`. A manual session is a
  *new* session, so it starts its own lineage and inherits neither the replaced session's
  load nor its Calendar event (DESIGN_workout_revisions.md §4). What it overwrote is
  recorded on its note — each replaced title + duration/TSS/RPE, plus the athlete's
  `--reason` — and rendered as "Reason:" on the event. Load re-balancing is left to
  `workout_adapt`. (See [§11](#11-terminology-plans-vs-workouts).)
- **`data_bootstrap(...)` / `data_reflect(...)`** — read past training from completed
  activities + metrics via the shared `_run_workout_analysis` core, which passes the
  horizon down so each asks its own question (§10.3). `bootstrap` = cold-start over the
  full backlog (horizon `long`), reverse-engineers the cycles, sets the reflect
  watermark; `reflect` = incremental since the watermark (horizon `short`), recent
  response only, advances it. Reflect's window ends on the last **completed** week
  unless an end date is named, so a part-week is never cited as a whole one and a run
  with nothing complete since the watermark makes no LLM call (§10.4). Both reuse
  `analysis_cache` on unchanged evidence; `force` recomputes; `inspect_only` renders
  without writing. A response that parses but carries no readable content raises before
  anything is written — caching it would pin the emptiness behind the fingerprint and move
  the watermark as if the history had been read; a partly-readable one saves and names the
  parts it could not use. See DESIGN_backward_evaluation.md §5, §8, §9, §13.
- **`_build_prior_training_context(prior_macros, today)`** — builds the read-only
  "planned vs actual" review injected into the `plan generate` strategy prompt
  (Option A). Anchored on the elapsed mesocycle windows of every plan handed in **and of
  the plan the athlete is currently in** (it adds `get_governing_macrocycle()` itself) —
  drift diagnosed only one macrocycle late is
  history. The caller passes both the *preceding goal's* plan and the one being
  *replaced*; they are different macrocycles whenever three or more planned goals chain
  (DESIGN_backward_evaluation.md §6.1). Each block carries its volume/load line plus the per-sport per-zone intensity
  table and the **block-over-block delta**, which is the intensity-creep check and lives
  here only: it is a periodization question, so `adapt` never sees it
  (DESIGN_intensity_distribution.md §4.1/§9.2) — beside **what the plan prescribed** over
  the same weeks (§9.2a) and **one planned-vs-actual load line per week**, without which a
  half-missed block reads exactly like a completed one. Folds in every cached
  reconstruction (`_cached_reconstructions()`): summary, inferred macro/mesocycle blocks,
  and physiological insights. Writes nothing anywhere — it is prompt context, never a
  note in the plan's feedback log. The whole review is wrapped **once**, at build time, and printed
  verbatim — the zone tables are column-aligned and a screen-width re-wrap shreds them.
- **`_intensity_history_context(macros, today)`** — the block walk behind the above.
  Navigates **macrocycle-first**, orders the lineages by their first block's start date,
  then flattens, so each block's predecessor is the previous element (including across a
  plan boundary) and the delta baseline is the block that actually preceded it —
  *argument* order is not chronological, since the plan being replaced can be for a later
  goal than the governing one. Never a
  date-ordered mesocycle query: every mesocycle accessor filters `mac.status = 'active'`,
  which hides exactly the cross-plan case, and dropping that filter drags in superseded
  rollback versions whose blocks overlap the live ones and describe training that never
  happened.
- **`plan_rm(objective_id)`** — deletes macrocycle + mesocycles for that objective
  (cascades in DB; removes *all* versions, active and superseded).
- **`plan_rollback(objective_id, target_macrocycle_id)`** — restores a superseded plan
  version (the chronologically previous one by default, or a specific id) and its
  workouts. Flips the active macrocycle, then undoes every workout change made after that
  version's newest one, through the same point-in-time primitive `workout rollback` uses.
  The restore is deliberately unscoped: restoring the whole moment is what keeps one
  session from ending up live in two slots (DESIGN_plan_rollback.md,
  DESIGN_workout_revisions.md §10).
- **`effective_thresholds()`** — the linchpin accessor (`DESIGN_benchmark_workouts.md`
  §3.3): the athlete's current threshold anchors, `max_hr` from config overlaid with the
  latest logbook row per kind (`db.latest_thresholds()`). `_effective_profile()` merges
  these onto `config.user_profile`, and every engine prompt call (generate/adapt/plan/
  analysis) passes that merged profile so zones are prescribed from live values.
- **`_get_config_hash()`** — delegates to `CoachEngine._get_config_hash()`.
  **`_get_config_snapshot()`** — `effective_thresholds()` as JSON, snapshotted on the
  macrocycle. **`_get_profile_snapshot()`** — `config.plan_profile()` as JSON, snapshotted
  alongside it so staleness can name the field that moved.
- **`config_changed(macro)`** — the single staleness judgment, reached from the CLI only
  through `cli/staleness.py` (which owns the wording, the four surfaces and the re-stamp)
  and directly by the `plan_generate` reuse
  check. Returns a human-readable reason when the plan-shaping profile fingerprint
  mismatches or an effective threshold anchor drifted more than
  `coach.threshold_replan_pct` from the macrocycle's `config_snapshot`, else None. A kind
  absent from the old snapshot (newly recorded) is skipped; a kind that disappears counts
  as drift (§3.5). `e1rm` is skipped entirely: the logbook has no per-exercise field, so a
  deadlift PR after a squat PR reads as one anchor jumping ~70% and would invalidate a
  whole periodization — it still joins the snapshot and the prompt, it just never trips a
  replan (DESIGN_benchmark_workouts.md §3.3). Macrocycles without a snapshot (legacy)
  judge on the fingerprint alone. The profile half of the reason names the fields that
  moved when the macrocycle carries a `profile_snapshot`, and degrades to a bare
  "athlete profile changed" when it does not (`DESIGN_plan_staleness.md` §5).
- **`plan_reshape_verdict(macro, change_reason)`** — the coach's read on whether the
  change `config_changed` reported would have reshaped `macro`: `{reshaping, why}`, or
  `None`. Reached from `cli/staleness.py` when the changed-input question is put to the
  athlete. Fails open on purpose — any error, a malformed reply, or `--show-llm-prompt-only`
  leaves the athlete with the question and no verdict, since the staleness question must
  never hang on the network (`DESIGN_plan_staleness.md` §10).
- **`_get_coach_system_prompt(objectives, constraints, ...)`** — builds the system
  prompt without making an LLM call (used by tests).

**Singleton:** `coach_service = CoachService()` at the bottom of `coach/service/__init__.py`.
Import as:
```python
from trainmate.coach import coach_service
```

---

## 4. Database — Key Patterns

**Package:** `trainmate/db/` · **Singleton:** `db = Database()` (in `__init__.py`)

`Database` is composed from per-domain mixins — `base.py` (`BaseDB`:
connection + schema setup), `objectives.py`, `constraints.py`,
`signals.py`, `benchmarks.py`, `workouts.py`, `activities.py`, `learnings.py`,
`analysis.py`, `periodization.py`, `settings.py`, `wipes.py` — all re-exported from
`__init__.py` so
`from trainmate.db import ...` is unchanged.

- Every method opens a fresh `sqlite3` connection (context manager), commits,
  and closes.
- `foreign_keys = ON` is set on every connection; cascades are used on
  macrocycles→mesocycles.
- `db.workout_change(kind, summary, macrocycle_id)` is the **only** write path onto
  `workouts`. Everything inside one `with` block shares a transaction and a
  `workout_changes` row; `change.append(...)` merges what it is given onto the slot's live
  revision and inserts a new one, `change.void(...)` says the slot now holds no session,
  and `change.restore(revision)` appends a stamped copy of an older one. A proposed
  revision whose prescription matches the live one is not written. On close the handle
  runs the Calendar reconcile over the lineages it touched
  (DESIGN_workout_revisions.md §6/§8).
- **Sport-type matching is alias-aware** (`trainmate/sports.py`, `SPORT_MAPPING`):
  the coach's prompts/generated/adapted workouts speak canonical names
  (`strength_training`), while manual (`workout add`) or legacy rows may use an alias
  (`strength`). `workout add` normalizes input via `canonical_sport()`; `get_workout`
  and the append path key on `sport_canonical`, so a canonical lookup or write resolves
  an aliased session instead of reporting it missing or inserting a duplicate; the stored
  `sport_type` keeps the spelling as written. Adaptation's override check compares
  canonically too. `adherence.py` imports `canonical_sport` from `trainmate/sports.py`
  (kept dependency-free to avoid the `adherence → garmin → trainmate.db` import cycle).
- **The canonical sports are** `running`, `cycling`, `hiking`, `strength_training`,
  `yoga`, `ski_touring`, `rowing` and `downhill_skiing`. `downhill_skiing` covers
  lift-served skiing *and* snowboarding (Garmin files both under one resort type) and is
  deliberately kept apart from `ski_touring`: no sustained climb, so the load profile and
  the prescriptions differ. `CANONICAL_SPORTS` (declaration order of `SPORT_MAPPING`) is
  the single source of truth for the `goal add`/`goal edit` `--sport` choices
  (`trainmate/cli/goals.py`) and for the `sport_type` enum in the generate/adapt prompts
  (`_SPORT_TYPE_ENUM` in `trainmate/coach/engine/workouts.py`, wrapped to match the
  hand-written schema around it) — adding a sport to `SPORT_MAPPING` reaches all three.
- **The canonical cycling name is `cycling`**, with `road_biking`, `road_cycling`,
  `gravel_cycling`, `mountain_biking`, `cyclocross`, `bmx`, `indoor_cycling`,
  `virtual_ride` and `biking` as its aliases — they all share one set of Garmin cycling
  zone boundaries, which is the criterion for sharing a zone-table row
  (DESIGN_intensity_distribution.md §6.1). This replaced a second, disagreeing
  vocabulary: `garmin/load.py::CYCLING_TERMS` held substring *fragments* matched loosely
  by `sync.py`, and had `gravel_cycling`/`cyclocross` that `SPORT_MAPPING` lacked, so a
  gravel ride got its power zones fetched and then fell through into a row of its own.
  `CYCLING_TERMS` is gone; `sync.py`'s power gate is now
  `canonical_sport(type_key) == "cycling"`. That gate is a **classifier, not a
  pre-filter** — Garmin reports `avgPower` for running too, and running watts scored
  against a cycling FTP are meaningless. Matching is exact, not substring: an
  unrecognised type surfaces as its own row so you can see which alias to add, and
  `data pull -d START..END` re-fills the window once you have.
- **Canonicalize on read, not on write.** `completed_activities.activity_type` keeps
  Garmin's raw string; every read path goes through `canonical_sport()`. Overwriting
  `gravel_cycling` with `cycling` in the column would be a lossy write undoable only by
  a re-pull. `scripts/migrate_cycling_sport_rename.py` is the one exception, and it
  touches only `workouts.sport_type` (the plan's own vocabulary, which should agree with
  what the reports print).
- `rollback_to_change(change_id, from_date)` is the **one** undo primitive, and it is
  point-in-time: it reverts that change *and every change after it*. It works per slot
  rather than per lineage, because a change can end a lineage by appending over it — a
  `generate` landing on a manual session, an `add` replacing a generated one — and the
  session to bring back is then the slot's previous occupant. For every slot the target or
  a later change wrote, the revision live there just before it is copied forward, stamped
  `restored_from`; a slot that held nothing gets a void. `workout rollback`, `plan
  rollback` (targeted at the moment just after the restored version's newest change) and
  the goal reinstate all reduce to this. The floor is the same rule archival had:
  appending a copy into a past slot would silently make it the live session for a day
  already trained (DESIGN_plan_rollback.md §9, DESIGN_workout_revisions.md §10).

### Methods by domain

Each domain mixin follows the same naming convention, so the full signatures are
discoverable by reading the mixin in `trainmate/db/` (grep the file named for the
domain). The convention: `add_*`/`save_*` (writers; `save_*` is an upsert),
`get_*`/`get_*s` (single-row by id / ranged-or-filtered list), `update_*` (partial
`**kwargs` patch), `delete_*` (hard delete), `wipe_*` (clear the domain). Only the
methods whose behavior is *not* obvious from that convention are called out below.

- **Objectives** (`objectives.py`) — plain CRUD; nothing beyond the convention.
- **Constraints** (`constraints.py`) — the unified directive object. Beyond the CRUD
  convention: `get_constraints(start, end)` returns rows overlapping a window (open-ended
  when `end` is None — the plan form).
- **Daily Signals** (`signals.py`) — external signals are reconciled **by
  Calendar event id**, so the writer/deleter are `*_by_event(google_event_id, …)`
  variants alongside the id-based ones used by the `signal` command. Cleared by
  `wipe_metrics` (see §13).
- **Workouts** (`workouts.py`) — the naming convention does not apply here, because the
  table is a log rather than a set of rows to edit. The whole write side is
  `workout_change` (see Key Patterns above); there is no `save_`, no `update_`, no
  `delete_`. On the read side, `get_workouts` / `get_workout` / `get_workout_by_id` return
  the *hydrated* session — the live revision plus what its lineage derives (`original_*`,
  the adaptation tally, `source`) — so everything above this module sees the dict shape it
  always did. `get_workout_by_id` takes a **lineage** id, the one `workout list` prints,
  and resolves it to that session's newest live revision, which is what stops `workout rm`
  addressing a dead revision. Voids are excluded unless `include_removed=True`; there is
  no `include_archived` any more. The history readers are `get_plan_revisions` (every
  revision, flagged live) and `get_workout_changes` (the batch list `workout batches`
  renders). The push recorders `mark_workout_pushed` / `mark_workout_adherence_pushed`
  are the **only** writers of `pushed_signature` / `adherence_pushed_signature`, and they
  write `workout_calendar_state`, not the log. Undo: `rollback_to_change` (above).
- **Completed Activities** (`activities.py`) — `save_completed_activity` upserts on
  `activity_id`.
- **Metrics & Baselines** (`activities.py`) — `get_baseline(date)` returns the
  *closest prior* baseline. The scoped wipes (in `wipes.py`) are the non-obvious part:
  `wipe_garmin_data(start, end)` also clears the evidence-derived `analysis_cache`
  and, on a *full* wipe, resets the garmin/reflect/bootstrap watermarks (a dated wipe
  leaves them, since re-pull detects gaps by row presence);
  `wipe_calendar_signals(start, end)` always resets the Calendar sync token (the
  incremental sync otherwise can't backfill deleted rows); `wipe_metrics()` = both.
  `wipe_garmin_data` runs `garmin.recompute_derived()` **itself**, after its own
  transaction commits: deleted load stays baked into every later day's CTL/ATL EWMA
  until a sweep re-derives it (DESIGN_pmc_fitness_fatigue.md §4). It used to sit at the
  command layer (`cli/data.py`) to dodge a garmin↔db import cycle, which meant any
  other caller silently corrupted the derived metrics; the lazy `runtime` singletons
  removed the cycle, so the invariant now belongs to the operation.
- **Coach Learnings** (`learnings.py`) — `get_learnings()` annotates each record with
  a computed `dormant` flag and its `proposed_confidence`; `add_learning` seeds a
  synthetic basis sustaining the level; plus the evidence/decay mutators
  (`apply_learning_deltas`, `recompute_all_confidence`, `derive_staleness_proposals`,
  `demote_learning`, `keep_learning`) and module-level helpers/constants
  (`CONFIDENCE_LEVELS`, `RETIRE_PROPOSAL`, `derive_confidence`, `step_down`,
  `learning_is_dormant`; the staleness budgets are config, not a constant —
  `config.learning_staleness_days`). The
  evidence/confidence/decay model these implement is **canonical** in
  [§3](#3-coach-package-architecture); tables in [§5](#5-database-schema).
  Periodization strategy lives in `macrocycles`, not here.
- **Analysis Cache** (`analysis.py`) — caches a backward-evaluation reconstruction
  (one row per `horizon`) keyed by an evidence fingerprint, so a re-run over unchanged
  data reuses it instead of re-calling the LLM; `save_analysis_cache` /
  `get_analysis_cache` (which parses `reconstruction` from JSON) /
  `wipe_analysis_cache()` — the last is the single wipe path, which `wipe_garmin_data()`
  delegates to rather than inlining its own DELETE. See DESIGN_backward_evaluation.md §5.1.
- **Macrocycles/Mesocycles** (`periodization.py`) — versioned: `save_macrocycle`
  **supersedes** the objective's existing active version (marks it `superseded`, keeps
  it) and inserts the new active one, running the blocks through
  `repair_block_contiguity` on the way in so within-plan gaps and overlaps never reach
  the table (DOMAIN_MODEL.md §4); `set_active_macrocycle(id)` promotes a version
  and supersedes the rest. `get_macrocycle_for_objective` returns the active version
  only, while `get_macrocycle(id)` / `get_macrocycle_versions` /
  `get_previous_macrocycle_version` reach any version for walk-back navigation.
  `get_preceding_macrocycle` is the other "previous plan" — the previous *goal's* active
  one, for retrospective views that must not walk superseded versions; `plan_lineage.py`
  is the shared walk. Plan versioning + rollback: DESIGN_plan_rollback.md (§6.1 for the
  two accessors). The plan's feedback log lives here too: `add_plan_feedback` /
  `list_plan_feedback` (joined with block names, oldest first) / `get_plan_feedback` /
  `rm_plan_feedback`, replacing the `update_*_feedback` overwrite slots
  (DESIGN_plan_feedback.md §6).

---

## 5. Database Schema

SQLite database at `trainmate.db` (path from `config.db_path`), in **WAL** mode with a
5-second busy timeout — CLI, web app and bot are concurrent surfaces over one file, so a
pull overlapping a dashboard refresh is ordinary rather than exceptional.

`_init_db` brings a database up to `SCHEMA_VERSION` (`db/base.py`) and records that in a
`schema_version` table. Later starts see the stamp and do nothing, so `tm --help`
performs no I/O; the DDL used to run in full — around 630 lines, writes included — on
every process start. The migrations stay idempotent (`CREATE TABLE IF NOT EXISTS`,
`_add_column` guarded by `PRAGMA table_info`), so the stamp is a way to skip work rather
than a ledger to replay: clearing it re-runs everything. Bump `SCHEMA_VERSION` when the
DDL changes — reusing the number a previous commit already stamped is silent, since every
existing database skips the new migration while fresh ones (and so the tests) look fine;
`test_db_lifecycle.py` fingerprints the schema and fails on an unbumped change. Guarded
ALTERs replaced `try: ALTER … except OperationalError: pass`,
which also swallowed "database is locked" and let a locked database half-migrate in
silence.

`db.transaction()` runs several writes as one connection and one commit, and rolls the
lot back on an exception. Methods called inside it join automatically — their own
`conn.commit()` is deferred to the end — so a PMC recompute over a long history costs
one connection instead of one per day.

### objectives
| Column        | Type       | Notes                                                |
|---------------|------------|------------------------------------------------------|
| `id`          | INTEGER PK |                                                      |
| `title`       | TEXT       |                                                      |
| `target_date` | TEXT       | YYYY-MM-DD                                           |
| `sport_type`  | TEXT       | Single or comma-separated (e.g. `running,cycling`) |
| `status`      | TEXT       | `active` or `archived` **only** — see below           |
| `description` | TEXT       |                                                      |
| `date_type`   | TEXT       | `event` (default) or `horizon` — see below            |

`date_type` says what `target_date` means: an **event** is a day something happens
on, so the plan peaks and tapers for it; a **horizon** is just how far the athlete
wants to train toward the goal — the plan still ends around the date, but with an
ordinary training block, no peak/taper pinned to it, and the goal-week
no-benchmark carve-out does not apply. The planning prompt branches on it
(`engine/planning.py`), goal lines everywhere tag it
(`engine/prompt.py:_render_goal_lines`), and `_clean_goals` includes it only when
`horizon` so pre-existing plans keep their `goals_hash`. Rationale in
[§15](#goal-dates-event-vs-training-horizon).

`status` records one thing: whether the goal was **called off**. Completion is not
stored — a goal that is not archived and whose `target_date` has passed *is*
completed, derived on every read by `db.objectives.goal_state()` →
`upcoming | completed | archived`. That one function is what every surface (`goal
list`, `status`, the web view) renders, so no two of them can disagree. A one-off
migration in `db/base.py` rewrites any legacy `completed` row to `active`, and
`goal edit --status` offers only `active`/`archived`. Rationale — and the two
opposite failure modes the old three-value column produced — in
DESIGN_backward_evaluation.md §12.

The accessors name which question they ask: **`upcoming_objectives()`** (not
archived, date not passed) is "the goals that matter" at every planning and picker
site; **`get_active_objective()`** with no ID is the next goal still ahead, while
its ID form resolves any non-archived goal, past or future; and
**`get_preceding_objectives()`** deliberately includes completed goals, which are
the point of the lookup. `progression.plan_gap()` filters only `!= archived` — its
`target_date > plan_end_date` comparison already answers the date question.

### constraints
The single directive object — everything the athlete asks the coach to work around,
at any horizon (DESIGN_constraints.md). Supersedes `lifeevents`.
| Column        | Type       | Notes                                                     |
|---------------|------------|-----------------------------------------------------------|
| `id`          | INTEGER PK |                                                           |
| `start_date`  | TEXT       | YYYY-MM-DD                                                 |
| `end_date`    | TEXT       | YYYY-MM-DD (== start for a single day)                     |
| `rest`        | INTEGER    | 0/1 — the **single** deterministic edge (rev 6): 1 = a no-training window whose dates skip the LLM and are forced to rest. Everything else is advisory prose the coach honors by judgement |
| `title`       | TEXT       | The directive, stated short; the `list` display string    |
| `description` | TEXT       | Optional richer context, read by the LLM                  |
| `replan`      | INTEGER    | 1 = escalated to plan-shaping (built into the plan, §7)    |
| `source`      | TEXT       | `manual` \| `message` \| `lifeevent` (migration)          |
| `created`     | TEXT       | UTC ISO                                                    |
| `honored_at`  | TEXT       | UTC ISO of the last coach pass that had this directive in scope **with authority over every day of it still ahead** — `workout generate` or `workout adapt`. NULL ⟺ the plan does not reflect it yet. Deliberately NOT a claim that the plan changed. Two rules, both owned by `coach/honoring.py` and nowhere else: who may stamp (`covers`), and whether the plan is missing the directive at all (`needs_a_pass` — unstamped, `replan = 0`, a non-empty window, and **at least one session scheduled in that window**, since a window with nothing in it is nothing to reshuffle). `status`, `constraint list`/`show` and the add-time message all call that one predicate; there is deliberately no SQL half-copy of it in `db/constraints.py`, because that is how they came to disagree (DESIGN_constraint_honoring.md §2/§4). Cleared by a `constraint edit` that moves the window or rewrites the directive, and by a rollback restoring a plan older than the honoring |

Index: `idx_constraints_start` on `start_date`. Rev 6 dropped the pre-rev-6
`binding`/`sport`/`type` columns (a hard/soft × sport matrix plus an opaque label) in
favour of the single `rest` flag; `db/base.py` drops them with guarded DDL.

The old `lifeevents` table it superseded has been dropped (its rows were migrated
into `constraints` as advisory — `rest = 0`, since rev 6 maps the old `soft` binding to
advisory — with replan=1, source=`lifeevent`).

### workouts

**Append-only.** A row is one *revision* of one session and is never updated or deleted;
every change appends. The newest revision in a `(date, sport_canonical)` slot is the live
one, and everything else is history. Two triggers enforce that (see
[Workout state](#workout-state--three-orthogonal-axes-not-one-enum) below); several state
facts are **derived, not stored**. See DESIGN_workout_revisions.md.

| Column                  | Type       | Notes                                            |
|-------------------------|------------|--------------------------------------------------|
| `id`                    | INTEGER PK | The revision id. Monotonic (AUTOINCREMENT), which is what makes "highest id in the slot" mean "newest". |
| `change_id`             | INTEGER    | NOT NULL — the `workout_changes` row that appended this revision. |
| `lineage_id`            | INTEGER    | Stable session identity, surviving both edits and date moves. Equals `id` on a session's first revision. Nullable only *inside* the inserting transaction: a first revision is born NULL and seeded before commit, the one UPDATE the trigger permits. |
| `date`                  | TEXT       | YYYY-MM-DD (scheduled day)                       |
| `sport_canonical`       | TEXT       | The slot key (`trainmate.sports.canonical_sport`). |
| `sport_type`            | TEXT       | The spelling as written.                         |
| `title`                 | TEXT       |                                                  |
| `description`           | TEXT       |                                                  |
| `duration_minutes`      | INTEGER    |                                                  |
| `rpe`                   | INTEGER    | Expected RPE 1–10 (excluded from `pushed_signature`) |
| `tss`                   | INTEGER    | Expected Training Stress Score                   |
| `void`                  | INTEGER    | 0/1 — 1 ⟺ this slot holds no session as of this revision. When it is the newest revision in the slot there is no session that day; when it is not, it is history like any other row. |
| `reason`                | TEXT       | This revision's note: why it changed, or why it was cancelled. The change kind says which. |
| `restored_from`         | INTEGER    | Only on a `rollback`/`restore`/`reinstate` copy: the revision it duplicates. The adaptation tally follows it to skip spans that were undone. |
| `macrocycle_id`         | INTEGER    | Plan version this revision belongs to — the per-date tag every scoping read uses (goal stand-down, `plan show`). |
| `created_at`            | TEXT       | UTC ISO — when the SESSION entered the plan, carried across its revisions. Distinct from the change's own timestamp. |
| `benchmark_type`        | TEXT       | Benchmark identity: non-NULL ⟺ this session is a fitness test (`ftp_20min`, `run_5k_tt`, `e1rm`, …). Creation-time intent, carried forward by the append merge so a partial re-save preserves it; `change.append(clear_benchmark=True)` is the one way to blank it, used when an adaptation replaces a test with something that is no longer that test (`DESIGN_benchmark_workouts.md` §4.2). Rendered as `[BENCHMARK]` in `workout list`. |
| `planned_zone_currency` | TEXT       | `hr` \| `power` (`DESIGN_intensity_distribution.md` §9.8) |
| `planned_zone1..7_sec`  | INTEGER    | Planned time in zone.                            |

Indexes: `idx_workouts_slot (date, sport_canonical, id)` serves the live view;
`idx_workouts_lineage (lineage_id, id)` serves every lineage derivation.

View `live_workouts`: the newest revision per slot. Every reader keeps its own `WHERE`
and changes only its `FROM`.

### workout_changes

One row per command invocation that wrote workouts — written even when the pass appended
nothing, because an adapt that looked at the metrics and held is a real event.

| Column          | Type       | Notes                                             |
|-----------------|------------|---------------------------------------------------|
| `id`            | INTEGER PK | The batch key. `workout rollback` undoes a change and everything after it. |
| `created_at`    | TEXT       | UTC ISO, when the command ran.                    |
| `kind`          | TEXT       | `generate` · `adapt` · `swap` · `add` · `rm` · `restore` · `rollback` · `stand-down` · `reinstate`. Fixed at write time; one invocation has exactly one kind. |
| `summary`       | TEXT       | The batch rationale — what `adaptation_summary` used to copy onto every row. |
| `macrocycle_id` | INTEGER    | The plan version in force when this ran: context for `workout batches`, distinct from the per-row tag. |
| `note`          | TEXT       | The coach's one line to the athlete about this change, for the morning push. NULL on a change with nothing they would notice — which is most of them (DESIGN_plan_change_continuity.md §6.3). |
| `commitment_end`| TEXT       | Last day of the commitment window in force when this ran, so a removal is judged by the window it was written under rather than by the one standing when the Calendar sync happens to run (§5.2). |

### workout_calendar_state

Sync bookkeeping, keyed by **lineage** rather than revision: there is one Calendar event
per session and it must follow that session across both edits and date moves. Off the row
because a successful push is not a prescription change — left there, `workout push -f`
alone would double the table.

| Column                       | Type       | Notes                                    |
|------------------------------|------------|------------------------------------------|
| `lineage_id`                 | INTEGER PK |                                          |
| `google_event_id`            | TEXT       | Non-NULL ⟺ a Calendar event exists (may be stale) |
| `pushed_signature`           | TEXT       | What the last ordinary push sent. Only writer: `mark_workout_pushed`. |
| `adherence_pushed_signature` | TEXT       | What the last `compare --mark` adherence push sent. Only writer: `mark_workout_adherence_pushed`. |

#### Workout state = three orthogonal axes (not one enum)

*Archived* is no longer an axis. A superseded revision is not a flagged row but a position
in a chain: an older sibling in its slot. What remains is three independent facts, none
stored as a status string.

**1. Modified?** = the change kind of the live revision. No heuristic, no string prefixes,
no precedence rule — the kind was recorded when the change ran:

| Live revision's change kind | Reads as |
|---|---|
| `generate` | unmodified |
| `adapt` | adapted |
| `swap` | swapped |
| `add` | replaced |

  - A `rollback`/`restore`/`reinstate` copy re-establishes an earlier prescription, so it
    reads as whatever *that* prescription was — the same `restored_from` jump the tally
    makes. `rm`/`stand-down` produce voids, which carry `[REMOVED]` instead.
  - **Markers:** `workout list` renders the kind *and* the standing easings, so a session
    eased twice and then swapped reads `[SWAPPED, ADAPTED ×2]`. Two facts rather than one,
    which is why there is no longer a rule about which wins. `modification_markers` in
    `cli/workouts/_helpers.py` is the single renderer; the web API serves its output.
  - **The adaptation tally** (`adaptation_count` / `adapted_at`) is derived by walking the
    lineage backwards from the live revision: jump over any span a `restored_from` copy
    undid, stop at the first `generate` (a re-prescription; easings of the old form do not
    describe the new one), and count the `adapt` revisions whose duration or TSS fell
    against their own predecessor. Because the walk follows the *lineage*, it survives a
    swap — which is the whole reason `lineage_id` exists. The daily adaptation renders it
    as the `[ALREADY EASED …]` tag so a re-run holds an already-eased session instead of
    stacking another cut onto still-lagging recovery.

**2. Calendar state** = derived by `trainmate.calendar_state.calendar_status(workout)`:

| Result | Condition |
|--------|-----------|
| `unpushed` | `google_event_id IS NULL` |
| `synced` | `pushed_signature == ` current calendar-field hash |
| `stale` | `pushed_signature != ` current hash |

  - `mark_workout_pushed` is the **only** writer of `pushed_signature`. Under revisions
    this falls out rather than being arranged: the live row after any change is a
    *different row*, its hash differs from the stored signature, and it reads stale.
    `CALENDAR_FIELDS` carries `revision_id`, which is what makes *any* appended revision
    move the hash — the event renders the whole lineage as its history, so a lineage that
    grew is an event that changed (DESIGN_calendar_lineage.md §6).
  - **Who keeps the Calendar true.** Every workout change ends with one **reconcile pass**
    over the lineages it touched, after commit (`trainmate/calendar_reconcile.py`). The
    change handle schedules it, so the only way to write workouts already schedules the
    reconcile, and no command carries Calendar code. Per lineage, keyed on its **newest
    revision**, live in its slot or not (`get_lineage_head`): a session that still owns
    its slot and whose signature differs is pushed; one that no longer owns it was
    superseded, and its event goes. A void is decided by `leaves_trace`
    (DESIGN_plan_change_continuity.md §5.2) — the athlete's own removal, a session they
    added, or one removed inside the commitment window the change ran under keeps its
    event, retitled, and gets one if it never had one; every other void's event is
    deleted and its state row dropped. Reading the lineage rather than the slot is what
    lets a marker written and then covered in the same run survive the run that wrote it.
  - **What the event says.** Title tags first (`[Done]`/`[Manual]`/`[Adapted]`/
    `[Deleted]`/`[Cancelled]`), then the body: the current load line, the current
    description, its `Reason:`, the intensity target, the **`History` block** — every
    earlier revision of the lineage, newest first, each with its date, load, target,
    reason and body (`calendar_lineage.py`, DESIGN_calendar_lineage.md) — and last the
    `Planned: … · Last adapted: … · Adapted ×N` lifecycle line over the goal/macro/meso/
    workout ids. A session that has never been revised has no history block and renders
    exactly as before.
  - **Stale is a retry marker, not a defect.** It persists only when the push could not
    land (offline, API error) or was declined (`swap --no-sync`). `workout push` defaults
    to **today onward**, so `warn_stale_before` (in `cli/workouts/_helpers.py`) reports
    anything stranded stale in the past and the `-d START..` window to recover it.
  - **Orphans** are the reverse direction: an event whose session is gone (fresh DB,
    restored backup, a wipe that skipped Calendar) can no longer be named locally, so
    `workout prune-calendar` sweeps from the calendar side — `list_workout_events` finds
    them by the `source=TrainMate` tag and deletes any id no lineage claims.
  - **Backward adherence marking** is the past-looking counterpart to the forward push:
    for each *strictly past* planned workout with an event, it re-renders the event with a
    verdict from `adherence.classify_adherence` — a `[Done]`/`[Missed]`/`[Partial]`/
    `[Rest OK]`/`[Rest broken]` title tag and an `Adherence:` description header.
    Today/future are skipped. Runs **by default** on `data pull` and `workout compare`
    (`--no-mark` skips); best-effort, no-op without a calendar. Shared pipeline
    `mark_adherence_range`→`mark_adherence_from_results` in `cli/common.py`. Each push
    stamps `adherence_pushed_signature` (calendar fields **plus** verdict) so a later pass
    skips a no-op write; kept in its **own** column, and any edit to the session
    invalidates it so a re-mark follows.
  - **The verdict is shown where the session is listed**, not only on the Calendar:
    `workout list` marks every listed session dated today or earlier
    `[DONE]`/`[PARTIAL]`/`[MISSED]`/`[REST OK]`/`[REST BROKEN]`, or `[NOT YET]` for one
    still ahead of the athlete today, and `-v` names the activity it was graded against
    plus what a `[PARTIAL]` differed by. `/api/workouts` hands the dashboard the same
    verdict as each row's `adherence` (status, word, reasons, matched activity), rendered
    as a badge. One pairing behind all three — `cli/common.py:adherence_verdicts` over
    `adherence_results` — so a terminal line, a Calendar event and a web card cannot
    disagree about whether a session happened. The listing grades **the whole window**,
    never the rows it happens to be showing: matching is per-day and first-come, so a
    narrowed listing that graded only its own rows would hand an activity to whichever
    session survived the filter. Because the listing now reports on completed activities,
    it freshens Garmin over that past span first (`--no-pull` skips it); a listing
    entirely in the future costs neither a pull nor a query.

**3. Removed?** = the live revision is a **void**. `workout rm` appends one carrying the
athlete's reason; the session is not deleted, and everything before the void is still in
the log. `get_workouts` excludes voids by default (`include_removed=False`), so they
vanish from `workout list`/`compare`, adherence, generation and the web API, and are
**not** counted as misses. The adapt flow re-fetches them (`include_removed=True`) and
surfaces them to the coach as deliberate cancellations. A goal stood down, a session an
adapt dropped and a session a plan no longer holds are voids too — the change kind is what
tells them apart, and it is what decides whether the Calendar event is kept or torn down.

### completed_activities
| Column              | Type    | Notes                                              |
|---------------------|---------|----------------------------------------------------|
| `activity_id`       | TEXT PK | Garmin activity ID                                 |
| `date`              | TEXT    | YYYY-MM-DD                                         |
| `start_time`        | TEXT    |                                                    |
| `activity_name`     | TEXT    |                                                    |
| `activity_type`     | TEXT    | Garmin type string, raw (e.g. `running`, `gravel_cycling`); canonicalized on read |
| `duration_sec`      | REAL    |                                                    |
| `distance_km`       | REAL    |                                                    |
| `elevation_gain_m`  | REAL    |                                                    |
| `avg_hr`            | INTEGER |                                                    |
| `max_hr`            | INTEGER |                                                    |
| `rpe`               | INTEGER | User-entered in Garmin (directWorkoutRpe); NULL if not entered (never synthesised) |
| `tss`               | REAL    | Measured TSS only: power TSS if a power meter recorded, else hrTSS; NULL if neither. Training *load* is derived on the fly, not stored — see Load model below |
| `bike_avg_watts`    | INTEGER | From Garmin; NULL for non-bike activities          |
| `zone1_sec`–`zone5_sec`     | INTEGER | Time in each HR zone (seconds); NULL if missing |
| `power_zone1_sec`–`power_zone7_sec` | INTEGER | Time in each Coggan power zone (seconds); NULL unless a power meter recorded |

### activity_match_decisions
The athlete's answer to a planned-vs-completed pairing the matcher had to guess at
([§15](#a-pairing-the-matcher-had-to-guess-at-is-a-question-not-a-fact)). Keyed by
activity and sport rather than workout id, because workout rows are replaced on every
revision and the athlete must not be asked again. Written by
`CoachService.record_match_decision`; every adherence surface passes
`db.get_rejected_matches()` into `analyze_adherence`.

| Column            | Type       | Notes                                              |
|-------------------|------------|----------------------------------------------------|
| `activity_id`     | TEXT       | PK together with `sport_canonical`                 |
| `sport_canonical` | TEXT       | The planned session's slot sport                   |
| `accepted`        | INTEGER    | 0/1 — 0 = the pairing is discarded and the session reads not done |
| `decided_at`      | TEXT       | UTC ISO                                            |

### athlete_metrics_cache
| Column            | Type    | Notes                  |
|-------------------|---------|------------------------|
| `date`            | TEXT PK | YYYY-MM-DD             |
| `rhr`             | INTEGER | Resting heart rate     |
| `hrv`             | INTEGER | Overnight HRV average  |
| `sleep_score`     | INTEGER | 0–100                  |
| `stress`          | INTEGER |                        |
| `ctl`             | REAL    | Fitness — CTL, 42-day EWMA of daily load (PMC). NULL in the leading-edge warm-up window and on pre-recompute rows |
| `atl`             | REAL    | Fatigue — ATL, 7-day EWMA of daily load |
| `tsb`             | REAL    | Form — TSB = CTL(yesterday) − ATL(yesterday) |

CTL/ATL/TSB (the Performance Management Chart, DESIGN_pmc_fitness_fatigue.md) are the
whole stored load model, computed in `garmin.recompute_derived()` over every calendar
day (rest days decay the EWMAs) and upserted onto existing metrics rows. The two time
constants (CTL/ATL) are config-backed under `garmin:`; the defaults are the supported
configuration. The warm-up window (first τ_ctl days of history) is suppressed at every
surface, and a static "still warming up" flag is shown while total history is short
(< 3·τ_ctl).

**ATL:CTL ratio.** Relative overload — fatigue against the athlete's own fitness base —
is *derived at read time* by `garmin.pmc.load_ratio(atl, ctl)`, never stored: it is a
division of two columns already on the row. It replaced the stored ACWR
(`acute_workload`/`chronic_workload`/`acwr`, dropped by the guarded DDL in
`db/base.py`); see DESIGN_load_ratio.md.

### athlete_baselines
28-day rolling baseline computed during `garmin.recompute_derived()` (a full
sweep run after every pull).

| Column                      | Type    |
|-----------------------------|---------|
| `date`                      | TEXT PK |
| `rhr_baseline_mean`         | REAL    |
| `rhr_baseline_std`          | REAL    |
| `hrv_baseline_mean`         | REAL    |
| `hrv_baseline_std`          | REAL    |
| `sleep_baseline_mean`       | REAL    |
| `sleep_baseline_std`        | REAL    |

### sync_state
Per-source sync progress, one row per `key`. The `garmin` row holds the pull
watermark: `through_date` is the forward high-water mark (local YYYY-MM-DD) and
only ever advances; `last_pull_utc` is an instant compared against now for the
freshness interval. The `calendar_signals` row instead holds `sync_token` (the
opaque Calendar `nextSyncToken`) with `through_date` NULL. Each source populates
only the columns it uses. See §10 (Data Pull), §13 (Daily Signals),
`DESIGN_garmin_direct_pull.md`, and `DESIGN_calendar_signal_ingest.md`.

| Column          | Type    | Notes                                            |
|-----------------|---------|--------------------------------------------------|
| `key`           | TEXT PK | Source key: `garmin` or `calendar_signals`       |
| `through_date`  | TEXT    | Garmin forward high-water mark (local YYYY-MM-DD)|
| `last_pull_utc` | TEXT    | ISO instant of last successful sync              |
| `sync_token`    | TEXT    | Calendar `nextSyncToken` (calendar_signals row)  |

### settings
App preferences that outlive one invocation but aren't training data — a generic
key/value store, so the next single-value preference needs no schema change.
Untouched by every `wipe` (a data wipe is about training history). Every athlete-facing
key is one entry in the `trainmate/settings.py` registry, written only by `settings set`
(DESIGN_settings.md): `llm_model` and `router_llm_model`, the coaching and routing model
identifiers; `timezone`, the IANA zone every date is computed in;
`workout_commitment_days` (`commitment-days`), how many days from today the athlete is
treated as already committed to; `push_enabled`, `push_morning_time`,
`push_morning_deadline` and `push_adapt_first`, the morning-push window and its switches.
Two internal markers are the exception, not preferences: `push_morning_last`, the
per-day idempotency stamp (`DESIGN_bot_simple_frontend.md` §4.3), and `push_note_last`,
the id of the last change whose line to the athlete the push delivered
(DESIGN_plan_change_continuity.md §6.4).

| Column       | Type    | Notes                                              |
|--------------|---------|----------------------------------------------------|
| `key`        | TEXT PK | Preference name (`llm_model`, `timezone`, …)        |
| `value`      | TEXT    | Stored value (a model identifier, a zone name, …) |
| `updated_at` | TEXT    | UTC ISO instant of the last write                  |

### daily_signals
External daily signals (alcohol, sleep, stress, …) ingested from tagged
Google Calendar events. TrainMate is domain-agnostic: `metric` is an opaque
category and `value` an optional numeric magnitude. Reconciled by
`google_event_id` (upsert on edit, delete on cancellation). See §13 and
`DESIGN_calendar_signal_ingest.md`.

Three producers write these rows: the external syncer, `signal add`
(`DESIGN_signal_authoring.md`), and `workout adapt --message`, which extracts signal
candidates from the note and writes the ones the athlete confirms
(`DESIGN_signal_extraction.md`). All three go through the calendar first —
`google_event_id` is NOT NULL — and all three share `signals.write_signal_days`.
`trainmate/signals.py` also holds the suggested category vocabulary
(`DEFAULT_SIGNAL_METRICS`, augmented by `coach.signal_metrics` in config) and
`SIGNAL_CHANNEL_EXCLUSIONS`, the substring table that stops a category being correlated
against a reading measuring the same thing. Vocabulary and exclusion table live in one
file because the exclusion matches on the category's spelling: a sleep category named
without "sleep" in it silently loses the guard.

| Column            | Type        | Notes                                          |
|-------------------|-------------|------------------------------------------------|
| `id`              | INTEGER PK  | Autoincrement                                  |
| `date`            | TEXT        | YYYY-MM-DD the signal applies to               |
| `metric`          | TEXT        | Opaque category, e.g. `alcohol`                |
| `value`           | REAL        | Optional numeric magnitude (NULL if untagged)  |
| `text`            | TEXT        | Human blurb (summary/description) for the coach|
| `google_event_id` | TEXT UNIQUE | Calendar event id — reconciliation key         |
| `updated`         | TEXT        | Event `updated` RFC3339 (debug)                |

### benchmark_results
The dated fitness-test logbook (`DESIGN_benchmark_workouts.md` §3.2) — the single home
for the athlete's trainable thresholds now that `ftp`/`lthr` have left config (§3.4). One
row per measurement; "latest" is newest by `date`, `id` as tiebreak. The latest row per
`anchor_kind` is what `CoachService.effective_thresholds()` feeds the coaching prompt and
the plan-staleness snapshot. Two further consumers read the logbook **rows** rather than
the effective set — the intensity block report (`intensity._benchmark_lines`, into the
coaching prompt and `tm progress`) and the read-only `GET /api/benchmarks` — both via
`benchmarks.with_previous()`, which supplies the per-row delta against the previous row of
the same kind. No privileged kinds — cycling FTP and a first swim CSS flow
identically (§3.5). Vocabulary (kind → label, unit, better-direction) lives in
`trainmate/benchmarks.py`, which also holds `SPORT_ANCHORS`/`anchors_for_sport()`: the
plausible anchors per **canonical** sport (aliases resolve through `canonical_sport`),
behind a *warning* on an implausible pair (`record swimming --ftp 250` warns and records
anyway). Never an error — `sport_type` is a label, the effective threshold is keyed on
`anchor_kind` alone, and an unknown sport stays silent. CRUD in `db/benchmarks.py`; CLI
verb `benchmark record`/`list`/`rm` (+ hidden `wipe`) (`cli/benchmarks.py`).

| Column        | Type       | Notes                                                     |
|---------------|------------|-----------------------------------------------------------|
| `id`          | INTEGER PK | Autoincrement                                             |
| `date`        | TEXT       | YYYY-MM-DD the test was performed                         |
| `sport_type`  | TEXT       | Canonicalized sport                                       |
| `anchor_kind` | TEXT       | `ftp` \| `lthr` \| `threshold_pace` \| `css` \| `e1rm` \| `mas` |
| `value`       | REAL       | The measured number (pace kinds stored in base unit)     |
| `unit`        | TEXT       | `W` \| `bpm` \| `min/km` \| `sec/100m` \| `kg` \| `km/h`  |
| `source`      | TEXT       | `test` \| `manual` \| `modeled` (last anticipates Phase 3)|
| `workout_id`  | INTEGER    | Nullable link to the planned benchmark it satisfied       |
| `note`        | TEXT       | Free text (protocol, conditions)                          |
| `created`     | TEXT       | UTC ISO                                                    |

### coach_learnings
Discrete, addressable athlete-observation records. Confidence is **app-computed**
from the `learning_evidence` basis (below), not asserted by the LLM. Full model:
[§3](#3-coach-package-architecture).

| Column                | Type       | Notes                                                  |
|-----------------------|------------|--------------------------------------------------------|
| `id`                  | INTEGER PK | Referenced by `revise`/`reinforce`/`contradict`/`retire` deltas |
| `text`                | TEXT       | LLM-generated observation                              |
| `sports`              | TEXT       | Comma-separated sport scope, or `general` (default)    |
| `confidence`          | TEXT       | App-computed: `tentative` / `moderate` / `established` |
| `proposed_confidence` | TEXT       | Pending, human-confirmable **downgrade** (`retire` = propose retirement); NULL when none |
| `created_at`          | TEXT       | ISO timestamp                                          |
| `updated_at`          | TEXT       | ISO timestamp; last content/metadata change            |
| `last_reinforced_at`  | TEXT       | ISO timestamp; drives decay → `dormant` (see §3); refreshed only by a *new* supporting week or a staleness demotion |

### learning_evidence
The per-learning **evidence basis**: the distinct training **weeks** backing each
learning, tagged supporting/contradicting; `confidence` is a pure function of it.
The `UNIQUE(learning_id, week_commencing, polarity)` constraint is the dedup
guarantee (re-citing a counted week is an `INSERT OR IGNORE` no-op). Full model:
[§3](#3-coach-package-architecture); DESIGN_evidence_based_confidence.md §5.

| Column            | Type       | Notes                                              |
|-------------------|------------|----------------------------------------------------|
| `id`              | INTEGER PK |                                                    |
| `learning_id`     | INTEGER    | FK → coach_learnings.id (ON DELETE CASCADE)        |
| `week_commencing` | TEXT       | YYYY-MM-DD (Monday) — the evidence anchor          |
| `polarity`        | INTEGER    | +1 supporting · −1 contradicting                   |
| `source`          | TEXT       | `reflect` \| `bootstrap` \| `manual` \| `migration` (`plan` is a reserved value in the schema comment; `plan generate` is a non-writer, so nothing emits it) |
| `created_at`      | TEXT       | ISO timestamp                                      |

`UNIQUE(learning_id, week_commencing, polarity)`

### macrocycles
| Column            | Type                  | Notes                                            |
|-------------------|-----------------------|--------------------------------------------------|
| `id`              | INTEGER PK            |                                                  |
| `objective_id`    | INTEGER FK→objectives | Cascade delete                                   |
| `strategy`        | TEXT                  | LLM-generated strategy text                      |
| `goals_hash`      | TEXT                  | SHA-256 of objectives at generation time         |
| `constraints_hash`| TEXT                  | SHA-256 of the plan-shaping (replan=1) constraints at generation time (§7) |
| `config_hash`     | TEXT                  | SHA-256 of the plan-shaping `user_profile`       |
|                   |                       | fields (thresholds, `name` and equipment excluded |
|                   |                       | — `DESIGN_plan_staleness.md` §3)                  |
| `config_snapshot` | TEXT                  | JSON of the effective threshold anchors (`max_hr` |
|                   |                       | + logbook kinds) the plan was generated with;     |
|                   |                       | staleness only past `coach.threshold_replan_pct`  |
|                   |                       | drift. NULL on plans predating the column. Shown  |
|                   |                       | by `plan show`, compared by `plan diff`.          |
| `profile_snapshot`| TEXT                  | JSON of the plan-shaping profile fields the plan  |
|                   |                       | was generated with, so the staleness reason can   |
|                   |                       | name which one moved. NULL on plans predating the |
|                   |                       | column — those get the unnamed reason (§5).       |
| `goals_snapshot`  | TEXT                  | JSON of the goals the plan was generated from    |
|                   |                       | (same cleaned data the hash covers); NULL on     |
|                   |                       | plans predating the column. Shown by `plan show` |
|                   |                       | and the web strategy card.                       |
| `constraints_snapshot` | TEXT             | JSON of the plan-shaping constraints the plan was |
|                   |                       | generated from; NULL on pre-snapshot plans (older |
|                   |                       | plans fall back to a legacy `lifeevents_snapshot`) |
| `created_at`      | TEXT                  | ISO timestamp                                    |
| `status`          | TEXT                  | `active` or `superseded`. Exactly one active     |
|                   |                       | version per objective; readers filter on active. |
|                   |                       | Defaults to `active` (legacy rows). See           |
|                   |                       | DESIGN_plan_rollback.md.                          |
| `superseded_at`   | TEXT                  | ISO timestamp a version stopped being active;     |
|                   |                       | NULL while active.                               |

Regenerating a plan **supersedes** the prior version (kept) rather than deleting it, so
`plan rollback` can restore an earlier version and its workouts (DESIGN_plan_rollback.md).

### mesocycles
| Column          | Type                    | Notes                                   |
|-----------------|-------------------------|-----------------------------------------|
| `id`            | INTEGER PK              |                                         |
| `macrocycle_id` | INTEGER FK→macrocycles  | Cascade delete                          |
| `name`          | TEXT                    | E.g. "Base Building"                    |
| `start_date`    | TEXT                    | YYYY-MM-DD                              |
| `end_date`      | TEXT                    | YYYY-MM-DD                              |
| `focus`         | TEXT                    | E.g. "Zone 2 aerobic base, high volume" |

### plan_feedback
The plan's feedback log — an append-only list of notes the athlete addressed to the
**next** plan version, written while this one is in force (DESIGN_plan_feedback.md §6).
Replaces the single `feedback` slot that used to sit on `macrocycles` and `mesocycles`,
so a second thought adds to the first instead of overwriting it.

| Column          | Type                    | Notes                                   |
|-----------------|-------------------------|-----------------------------------------|
| `id`            | INTEGER PK              | The handle `plan feedback --rm` takes   |
| `macrocycle_id` | INTEGER FK→macrocycles  | The plan version the note was written   |
|                 |                         | against. Cascade delete                 |
| `mesocycle_id`  | INTEGER FK→mesocycles   | The block it was filed to; NULL =       |
|                 |                         | plan-level. Cascade delete              |
| `created_at`    | TEXT                    | ISO-8601 UTC, full precision; orders    |
|                 |                         | the log oldest first everywhere         |
| `text`          | TEXT                    | The note, verbatim — never LLM-touched  |
|                 |                         | at capture                              |

**Pending** := rows whose `macrocycle_id` is the goal's *active* macrocycle. There is no
consumed flag: supersession is the consumption event, so a regeneration that is previewed
and declined leaves the notes pending, and a `plan rollback` makes an earlier version's
notes pending again. Pending notes are a plan input, so their presence alone makes
`plan generate` regenerate without `--force` (DESIGN_plan_feedback.md §7).

### analysis_cache
Cached backward-evaluation reconstruction (inferred cycles + insights), keyed by
an evidence fingerprint. One row per `horizon`; cleared by `wipe_metrics`. See
DESIGN_backward_evaluation.md §5.1.

The `horizon` **selects the question, not just the slot**: `_data_analyze_logic` takes it
and branches, so `long` (`data bootstrap`) asks for the periodization structure while
`short` (`data reflect`) asks only how the athlete responded and is told not to infer
macro/mesocycles — a few weeks cannot support the claim
(DESIGN_backward_evaluation.md §10.3). Three forward consumers read the table —
`plan generate`'s prior-training context via `CoachService._cached_reconstructions()`;
`trainmate/timeline.py`, which feeds the reconstruction's `inferred_mesocycles` into
`progression.assemble_timeline()` as `~`-prefixed bands wherever no planned block covers
the span (DESIGN_progress_timeline.md §6.1); and `data show-analysis`, which renders a
slot as stored. The timeline reads `long` alone; the plan prompt replays **both** slots,
`short` only when its window *starts* after `long`'s ends — a window that re-reads
bootstrap's weeks on its way past them would show one body of evidence twice. Neither
consumer checks the fingerprint — it is consulted only in the *writing* flow, so a
months-old reconstruction can be replayed (the covered window is printed alongside it),
and `_maybe_warn_stale_analysis()` says so once the latest of the two windows falls
`coach.analysis_staleness_days` behind. That warning is judged over the same accessor the
prompt reads, so the `data reflect` it points at is a command that can clear it
(DESIGN_backward_evaluation.md §10.2).

| Column           | Type       | Notes                                              |
|------------------|------------|----------------------------------------------------|
| `id`             | INTEGER PK |                                                    |
| `horizon`        | TEXT       | `long` \| `short` — UNIQUE; the cache slot         |
| `fingerprint`    | TEXT       | Hash of the per-activity load fields (`date`, type, `duration_sec`, `tss`, `rpe`, `zone1..5_sec` — not merely the id set, so a corrected re-pull invalidates the cache) + metrics + overlapping constraints + the window's `daily_signals` + the full-history `signal_days` block as computed + window |
| `window_start`   | TEXT       | YYYY-MM-DD                                         |
| `window_end`     | TEXT       | YYYY-MM-DD                                         |
| `reconstruction` | TEXT       | JSON: inferred cycles + physiological insights     |
| `created_at`     | TEXT       | ISO timestamp                                      |

---

## 6. Singletons

`trainmate/runtime.py` owns the process-wide singletons. Read them off the module at
use time — never instantiate the classes, and never bind the value at import:

```python
from trainmate import runtime

runtime.config           # Config
runtime.db               # Database
runtime.coach_service    # CoachService
runtime.calendar_syncer  # CalendarSyncer
runtime.garmin           # module functions (pull, ensure_data, …)
runtime.prompt           # the prompt broker (see below)
runtime.render           # the renderer — the voice (see below)
```

`runtime` resolves each name on first access and caches it, so **importing a module
never builds a Database or opens the file**; `--help` does no I/O. Assigning
(`runtime.db = fake`) shadows the accessor for the process, which is the single
override point for tests.

Reading at use time is what makes one assignment authoritative. Binding by value
(`from trainmate.db import db`) captures whatever existed at import and is invisible to
a later override — that mismatch is why replacements used to "take" for some modules
and not others. `from trainmate.db import db` still works for callers that want their
own handle, but prefer `runtime.db`.

`trainmate_cli.py` is a plain entry point: it holds `main()` and its helpers, and
nothing under `trainmate/` imports it. Handlers reach singletons through `runtime`, so
the import cycle that forced the old `sys.modules.setdefault("trainmate_cli", …)`
self-alias no longer exists.

`openrouter_client.model` is a lazily-resolved property, not a plain attribute: it reads
the stored choice from the database on first use, so importing the module never opens the
DB. Assigning to it pins a model for the invocation (how `--llm-model` overrides the stored
choice); `reset_model()` drops the cache so the next call re-resolves — what `settings set coach-model`
calls, since the REPL runs many commands in one process (DESIGN_model_selection.md §3.1).

`clock.active_zone()` is the same shape for the athlete's timezone: the
`settings.timezone` row is read on first use and cached for the process, since
`util.today_date()` asks on every call. `clock.reset_cache()` drops it — the registry's
`on_change` hook for the `timezone` setting, and what the bot's push loop calls each tick
because `settings set` runs in a CLI subprocess (DESIGN_user_timezone.md §2/§3).

`trainmate/garmin/` exposes module-level functions rather than a singleton, all
re-exported from its `__init__.py`: `pull()`, `ensure_data()`, `reset_memo()` (tests),
`recompute_derived()`, `backfill_tss()`, `compute_pmc()`, `load_ratio()`,
`activity_load()` / `load_method()` / `rpe_divergence()`, plus the `GarminClient` class
and `GarminAuthRequired`.

The **prompt broker** is `runtime.prompt`. `CoachService` takes it as a constructor
argument (`prompt_instance=`, defaulting to `runtime.prompt`) so the service can ask a
question without importing a frontend — it used to do `import trainmate_cli as cli`
mid-method, which meant any non-terminal caller got a terminal conversation.
`make_prompt()` selects the transport from
`TRAINMATE_FRONTEND`: `TtyPrompt` (the default — `input()` with `[y/N]`, EOF→default)
or `JsonPrompt` (`json` — the Telegram bot). Every interactive `input()` site routes
through `cli.prompt.confirm(message, danger=…)` / `cli.prompt.choose(message, [Choice…],
default=…)` / `cli.prompt.ask_text(...)`. `JsonPrompt` writes one sentinel-framed
request line (`\x1eTM-PROMPT {json}`, fields `v/id/type/message/default/danger/choices`)
to stdout and blocks reading one JSON answer line (`{v,id,answer}` or `{v,id,cancelled}`)
from stdin. A cancellation raises `PromptCancelled`, an ordinary `Exception`. It once
subclassed `BaseException` so the handlers' broad `except Exception` nets could not
mistake a deliberate abort for a command error; those nets are gone, a structural test
fails on any `except Exception` enclosing a prompt call, and `run_once` catches it before
its own error boundary — `main` prints `Cancelled.`. Garmin MFA (`garmin/client.py`)
stays outside the broker: it's gated by `sys.stdin.isatty()` and raises
`GarminAuthRequired` off a TTY, so it never hangs the bot.

The broker is also where an answer is **journalled**, in `_record_answer` — one `info`
`note` reading `<question> → yes|no`, with `d.answer` for querying. It goes here and not
at the ~29 call sites because this is the only thing that asks, so a question added later
is covered the day it lands; `tests/test_journal.py` fails on any bare `input()` under
`trainmate/cli/` or `trainmate/coach/` to keep that true. Three cases stay apart: EOF (cron,
a pipe) records `defaulted` rather than a decision nobody made, a cancel records the
question that was open, and `ask_text` is not journalled at all because its answer is the
athlete's free text (DESIGN_logging.md §4.4). The record lands on the innermost open run of
the process that writes — under the bot that is the CLI subprocess, so it carries the
command's run id and not the bot's. The two imports it needs are deferred so this module
still imports nothing beyond the stdlib. DESIGN_logging.md §5.6

The **renderer** is `runtime.render`, the same shape one axis over: the broker chooses a
*transport*, the renderer chooses a *voice*. `cli/render.make_renderer()` reads
`TRAINMATE_RENDER` once and returns `ExpertRenderer` (reports, tables, IDs, operator
nudges) or `CompanionRenderer` (prose, day words, no IDs, no commands the athlete cannot
type). They are two objects and not one "persona" because the axes are independent:
expert-over-Telegram is the operator's own daily surface, and the web dashboard is
expert-voiced with no TTY. A command body holds no `if simple:` branch — it calls
`runtime.render.<what happened>(…)`, and `CompanionRenderer`'s override set is the
opted-in list; `cli/render.py` also holds every companion line builder, so the whole
voice reads in one file. The expert table renderers stay in their command modules
(`print_workout_table`, `print_plan`, `print_progress_report`, …) and `ExpertRenderer`
delegates. Command modules never import `cli/render.py` — the builder in `runtime.py`
defers that import, which is what keeps the graph acyclic. `tests/helpers.run_cli`
drops the cached renderer per invocation so each run reads the environment as a real CLI
process does. DESIGN_render_persona.md

For tests, call `tests.helpers.rebind_test_db(test_db)`: it sets `runtime.db` plus the
remaining by-value sites in one call, so a module cannot be left reading a different
handle than its neighbours. `tests/__init__.py` installs two backstops before anything
imports the app — `sqlite3.connect` refuses the production database and `socket.connect`
refuses remote hosts — because both seams otherwise fail silently and leave a green
test that measured nothing. `tests/test_isolation_guards.py` asserts they still fire.

Names still patched where they are *used* rather than through `runtime`:
`trainmate.coach.engine.openrouter_client` (the LLM seam). The clock is patched at its
source instead — `tests/helpers.pin_clock` freezes `trainmate.clock.now`, the one instant
`today_date()` reads — so a module that imported `today_str`/`today_date` by value is
pinned with it. That was a list of the individual import sites and had drifted to 6 of
the 24 that exist, which is how real dates reached fixtures and expired them.

---

## 7. CLI Commands Reference

Invoked as `python trainmate_cli.py [--llm-model MODEL] <command> [subcommand] [args]`.
`trainmate_cli.py` holds only `main()` (the argparse dispatcher) and the
patchable singletons; the handler functions, named
`run_<command>_<subcommand>()`, live in the `trainmate/cli/` package
(one module per command family: `status`, `progress`, `goals`, `constraints`,
`benchmarks`, `signals`, `learnings`, `plans`, `data`, `settings`, `journal`, `bot`
(hidden: `bot morning`/`route`/`constraints`/`goals`/`block`/`capture`, spawned by the
Telegram bot —
DESIGN_bot_simple_frontend.md; `candidates.py` holds the confirm loops that turn a
note's extracted constraints and signals into rows, shared by `workout adapt -m` and
`bot capture note` so both inboxes ask the same questions),
plus the
`workouts/` **package** — `parser`/`generate`/`edit`/`revisions`/`_helpers`;
`selectors.py` holds the shared range grammar, `argparse_ext.py` the parser/help
extensions, `render.py` the two voices ([§6](#6-singletons)) and `staleness.py` the
changed-input wording). `help` is the one
exception — it just introspects the parser tree (`_print_command_tree` in
`trainmate_cli.py`), so it has no handler of its own.

**Short forms** (DESIGN_cli_noargs.md §d): any prefix that matches exactly one
command at its level *is* that command — `pl g` is `plan generate`, `constr ed` is
`constraint edit` — so the "Short form" column below lists examples, not a closed
set. An ambiguous prefix (`p` → `plan`/`progress`)
is refused, naming the candidates. Only shorthands that are *not* prefixes
(`lm`, `df`, `rb`, `sm`, `sa`, `san`, `use`) or that pick a winner among an
ambiguous set (`s` → `status`, `workout a` → `adapt`, `workout p` → `push`,
`signal l` → `list`, `data b` → `bootstrap`) stay registered as real aliases.
`rm` deliberately gets no winner — `r` stays ambiguous rather than shortening the
destructive command. Anything in the column that is *not* in those two registered
sets is a prefix, and a prefix silently breaks the day a sibling with the same first
letters lands; DESIGN_cli_noargs.md §d is the canonical authority on the distinction.

**LLM commands share two flags** the table omits: `--show-llm-prompt-only` prints the
prompt the command would send and exits before the call (`plan generate`, `workout
generate`/`adapt`/`swap`, `data bootstrap`/`reflect`), and the root `--llm-model MODEL`
pins the coaching model for one invocation ([§6](#6-singletons)).

**Range filters** (DESIGN_cli_selectors.md): every command that filters by span takes the
same four selectors — `-d/--date`, `-m/--mesocycle`, `-M/--macrocycle`, `-g/--goal` — over
one grammar (`A..B`, either side optional; a bare span carries its unit, `7d`/`2w`). They
intersect, and `resolve_window` (`cli/selectors.py`) turns them into one (start, end).
Each command declares its own default window and direction (`forward`/`backward`/`none`)
when it registers the flags via `add_selector_args`, so no handler carries a private
"no filter means…" rule. Those five letters (plus `-t/--type`) mean the same thing at every
level of the tree; `TestSelectorVocabularyInvariants` walks the real parser and pins it.
`resolve_meso_atom` sits beside that machinery as its **single-target** counterpart: the
one block an atom names — mesocycle ID, a date it covers, or an infix of its name —
resolved against one plan's blocks, which is what `plan feedback -m` files a note to
(DESIGN_plan_feedback.md §5).

**A bare command group** (`goal`, `constraint`, `benchmark`, `signal`, `learnings`,
`workout`, `data`, `plan`, `bot`, and the root) prints that level's full help and exits 1.
`build_parser` reads `named_subparsers` off the parser tree rather than listing it, so a
group added later is answered without a second edit — `bot` was missing from the old
literal and fell through to the *root* help. `tests/test_dispatch.py` walks the same
tree. This
is *not* argparse's missing-argument path, so it gets neither the "the following
arguments are required" line nor the chat short form — under `TRAINMATE_FRONTEND=json`
the whole help block is sent to Telegram. The documented exception is a group with a
single read-only view that is its whole state (`settings`), which acts bare instead
(DESIGN_cli_noargs.md §a3).

| Command      | Subcommand   | Short form | Description                                                            |
|--------------|--------------|----------|--------------------------------------------------------------------------|
| `help`       | —            | —        | Print every command and sub-command with its one-line help, recursing through the whole sub-parser tree (unlike `--help`, which only shows one level) |
| `shell`      | —            | —        | The REPL (`_repl` in `trainmate_cli.py`): reads command lines until EOF and runs each through `run_once`, so every line is its own journal run under one parent run |
| `status`     | —            | `s`      | Show active goals, recent metrics, coach learnings. Also names the end of the scheduled workouts when it is near or just behind, with the exact command that extends it — printed outside the goal branch, so the "nothing is planned beyond it" case reaches the athlete who has no goal on record (DESIGN_runway_nudge.md §4) |
| `goal`       | `add`        | `g a`    | Add objective (`TITLE DATE SPORT…` positional, `--desc`, `--date-type`)  |
| `goal`       | `edit`       | `g e`    | Edit objective by ID. `--status archived` calls the goal off: it stands its upcoming sessions down and clears their Calendar events, keeping the plan, its versions and its feedback. `--status active` reinstates the goal and offers those sessions back, floored at today (DESIGN_backward_evaluation.md §14) |
| `goal`       | `rm`         | `g r`    | Call the goal off — the same action as `goal edit --status archived`, under the verb people reach for: it stands the goal's upcoming sessions down, keeps the plan, its versions and its feedback, and does not ask, because `goal edit --status active` brings it all back. `--purge` is the destructive form for a goal entered by mistake: it deletes the objective and everything the cascade takes with it, printing that inventory plus the count of sessions it would strand and asking first; `-y` skips that prompt (DESIGN_backward_evaluation.md §14.5) |
| `goal`       | `list`       | `g l`    | List the goals that matter — upcoming and completed. Called-off goals are hidden and counted in a footer; `-a/--all` shows them (§14.5) |
| `goal`       | `wipe`       | —        | Delete all objectives                                                    |
| `constraint` | `add`        | `cons a` | Author a directive (positional `TITLE`, `--start`, `--end`, `--desc`, `--rest`, `--replan`/`--no-replan`; never prompts — see DESIGN_cli_noargs.md §a2) |
| `constraint` | `edit`       | `cons e` | Adjust scope / rest / text / replan by ID (`--rest`/`--no-rest`)        |
| `constraint` | `rm`         | `cons r` | Remove a directive by ID                                                |
| `constraint` | `list`       | `cons l` | List directives from the current mesocycle onward (`-a`/`--all`, `-v`, selectors `-d`/`-m`/`-M`/`-g`; default anchor: active mesocycle start, else show all) |
| `constraint` | `show`       | `cons s` | Show a directive in detail (incl. plan-shaping status and whether a coach pass has honored it) |
| `constraint` | `wipe`       | —        | Delete all constraints                                                  |
| `benchmark`  | `record`     | `be rec` | Log a fitness-test result to the `benchmark_results` logbook: positional `SPORT` plus one anchor flag (`--ftp`, `--lthr`, `--threshold-pace`, `--css`, `--e1rm`, `--mas`), `-d/--date` (default today), `--note`, `--source` (`test` default). An implausible sport/anchor pair (`SPORT_ANCHORS`) warns and asks; `-y` skips that prompt ([§5](#benchmark_results)). `b` alone is ambiguous with `bot` |
| `benchmark`  | `list`       | `be l`   | The logbook newest first, each row with its delta against the previous row of the same kind |
| `benchmark`  | `rm`         | `be rm`  | Delete one logbook row by ID                                            |
| `benchmark`  | `wipe`       | —        | Delete the whole logbook (hidden; `-y`)                                 |
| `signal`     | `add`        | `sig a`  | Author daily signal(s) (positional `METRIC [TEXT…]` or `-l/--label`, `--value N`, `-d RANGE` — no `-m`/`-M`, a signal spans days not blocks; one tagged all-day event per day) |
| `signal`     | `rm`         | `sig r`  | Remove signal(s) by positional ID(s) or metric, and/or selectors `-d`/`-m`/`-M`/`-g` (deletes calendar event + local row) |
| `signal`     | `list`       | `sig l`  | List signals (positional `METRIC` or `--metric`, selectors `-d`/`-m`/`-M`/`-g`; default window `metrics_lookback_days`) |
| `signal`     | `list-metrics` | `sig lm` | Show distinct metrics in use with counts and date span                   |
| `learnings`  | `list`       | `l`      | Show coach learnings (`-t/--type/--sport`, `--confidence`, `--dormant`)            |
| `learnings`  | `show`       | —        | Show a learning's full text + per-week evidence basis by ID             |
| `learnings`  | `edit`       | —        | Edit a learning's text (`ID TEXT`, both positional)                     |
| `learnings`  | `rm`         | `r`      | Delete a learning by ID                                                 |
| `learnings`  | `demote`     | —        | Accept a pending confidence downgrade by ID                            |
| `learnings`  | `keep`       | —        | Dismiss + affirm a pending downgrade by ID                             |
| `learnings`  | `wipe`       | —        | Delete all coach learnings                                             |
| `plan`       | `generate`   | `pl g`   | Generate/reuse macrocycle+mesocycles (`-f` to force, `-y/--auto` to apply without the preview, `--fresh` for a clean slate that withholds the plan in place from the prompt, `--show-llm-context` to also print the planned-vs-actual review the prompt carries — off by default, DESIGN_output_verbosity.md §7). `-g/--goal [RANGE]` takes the shared range grammar: one ID plans that goal alone, bounded to its own span (opening the day after the goal before it); a range plans every upcoming goal it covers, in date order, one strategy call each — `-g ..2` is everything through goal 2 (DESIGN_cli_selectors.md §9) |
| `plan`       | `show`       | `pl s`   | Show a periodization plan: strategy, snapshotted inputs (goals, constraints, threshold anchors), mesocycle timeline with each block's workout count/duration/load. Also flags any of those inputs that have changed since the plan was generated, with the test to judge it by and both routes out — `plan generate` or `plan keep` (DESIGN_plan_staleness.md §9); a superseded version is never flagged. Flags: `-g/--goal ID` (any status, not just active), `-M/--macrocycle ID` for a superseded version — each plan version IS a macrocycle, `-a/--all` for every goal that has a plan, `-w/--workouts` to list each mesocycle's sessions |
| `plan`       | `keep`       | —        | Record the current profile/goals/thresholds against the active plan **without** regenerating it (`-g/--goal ID`), clearing the changed-input flag `plan show` raises. The dismiss half of staleness: same write as declining at the `plan generate` prompt, reachable without spending a strategy call (DESIGN_plan_staleness.md §9) |
| `plan`       | `versions`   | `pl v`   | List a goal's kept plan versions — active + superseded — with IDs and dates (`-g/--goal ID`) |
| `plan`       | `diff`       | `pl df`  | Compare two plan versions (`[PLAN_ID_A] [PLAN_ID_B]`, `-g/--goal ID`): strategy prose, each version's attached feedback notes, mesocycles added/removed/renamed/re-dated, and snapshotted input deltas. No ID → previous vs active; one ID → that vs active. Prose rewritten wholesale collapses to a note unless `--full`. Comparison logic in `trainmate/plan_diff.py`, shared with `/api/plan/diff` |
| `plan`       | `rollback`   | `pl rb`  | Restore a superseded plan version + its workouts (`-g/--goal ID`, `-M/--macrocycle ID`, `-y`); defaults to the chronologically previous version. The inverse of eager generation (DESIGN_plan_rollback.md) |
| `plan`       | `rm`         | `pl rm`  | Delete plan for a goal ID. The old `pl d` alias is gone — `d` now prefixes `diff` |
| `plan`       | `feedback`   | `pl f`   | Append a note about the plan to its append-only log — bare text is plan-level, `-m [ATOM]` files it to one block by name-infix / date / mesocycle ID (bare `-m` = the current block). Bare run lists what is pending, `--rm ID [-y]` deletes one, `--replan` regenerates straight away, `-g/--goal ID` targets another goal's plan. No LLM at capture; the next `plan generate` reads the whole log and must address every note (DESIGN_plan_feedback.md) |
| `plan`       | `wipe`       | —        | Delete all plans                                                         |
| `progress`   | `[SPORT ...]` | `pr`    | Show the progress timeline: measured load to date, plan-projected forward (CTL/ATL/TSB), weekly planned-vs-actual bars (`-w/--weeks N`, `--chart [PATH]` for a PNG, `--explain` for the TSB-lag note; DESIGN_progress_timeline.md). `-z`/`--zones` (implied by naming a sport) adds one weekly time-in-zone table per sport — measured behind today, prescribed ahead of it (`--blocks` for block grain, `--power`/`--hr` to force the currency; DESIGN_intensity_distribution.md §9.6/§9.8). The sport argument scopes the **zone tables only**: CTL/ATL/TSB, the projection and the load table stay whole-athlete |
| `workout`    | `list`       | `w l`    | Show planned workouts. Defaults to a 7-day window from today. Positional `TARGET…` (workout IDs and/or date selectors, e.g. `wo li 12 15 -v`) plus the shared selectors `-d`/`-m`/`-M`/`-g` and `-t/--type TYPE`, `--removed`, `-l/--link` (each synced session's Calendar event link) (DESIGN_cli_selectors.md). Every listed session dated **today or earlier** also carries its adherence verdict — `[DONE]`/`[PARTIAL]`/`[MISSED]`/`[REST OK]`/`[REST BROKEN]`, or `[NOT YET]` for one still ahead today — and `-v` adds the matched activity and the mismatch behind a `[PARTIAL]`. Freshens Garmin over that past span unless `--no-pull` ([§5](#workout-state--three-orthogonal-axes-not-one-enum)). A listing whose range runs past the last scheduled session ends on one gray marker naming that — unconditional, a fact of the listing rather than a warning; an empty listing renders it alone (DESIGN_runway_nudge.md §4). |
| `workout`    | `show`       | `w sh`   | `workout list -v` under a name that says what it does: the same handler with the detail flag pinned on, the same positional targets and the same selectors. `wo sh 12` details one session; a bare `wo sh` details the same 7-day window `list` lists. Adding it made the bare `w s` prefix ambiguous — `swap` now needs `w sw` |
| `workout`    | `compare`    | `w c`    | Compare planned vs completed (`analyze_adherence()`): prints PLANNED/ACTUAL per day, flags misses (red), rest violations (red), unplanned high-load (yellow), then a discrepancy summary. Today's untrained sessions read `(not yet — still ahead today)` and are not misses (`pending_from`, [§10](#10-key-data-flows)). Same selectors as `workout list`; default 14-day lookback; a bare span (`-d 7d`) looks *back*; end capped at today. |
| `workout`    | `generate`   | `w g`    | Generate workouts from the plan blocks covering the days generated (the dates pick the plan, not a goal — DESIGN_cli_selectors.md §8). No selector → from the day after the schedule stops (today once it has run out) for `config.workout_generation_span_days` (28 default), so a run adds days rather than rewriting covered ones; refused when the plan is already covered to its last day. Span flags (mutually exclusive, **both** ends of the resolved window are used, and a span never opens before today): `-g/--goal [ID]` = the goal's whole plan span; `-d`; `-m` = that block's own days; `-M` (which also settles which plan to follow where two cover the same days). Lists the proposed sessions the way `workout list` renders them and asks before writing; on a `y` it archives the span's existing workouts, leaves the days outside it alone, and pushes the new ones to Calendar immediately. `-f/-y` skips both prompts, but the report of what changed for the sessions inside the commitment window still prints (DESIGN_plan_change_continuity.md §4.4). |
| `workout`    | `add`        | `w add`  | Schedule one session by hand, no LLM (`DATE SPORT TITLE` positional, `--desc`, `--duration`, `--rpe`, `--tss`, `--reason`). Replaces any same-sport session that day — every session with `--replace-day` — and names what it replaced on the new session's note ([§11](#11-terminology-plans-vs-workouts)). Inside the commitment window the replaced session keeps its Calendar event, marked `[Deleted]` — the athlete's own hand (DESIGN_plan_change_continuity.md §5.1/§5.2). `w a` is `adapt`, so this one needs the full word |
| `workout`    | `rm`         | `w rm`   | Soft-remove by ID (`ID REASON`, both positional): marks `removed`, marks the Calendar event deleted; kept in DB, hidden from list/compare, shown to coach as a cancellation. |
| `workout`    | `restore`    | `w res`  | Bring a cancelled session back by ID: appends a copy of the revision its void ended, and the reconcile removes the `[Deleted]` mark. Unrelated to `workout rollback`, which undoes a whole change. |
| `workout`    | `rollback`   | `w rb`   | Undo a workout change **and every change after it**, putting the sessions back the way they were the moment before it ran (`--batch N` per `workout batches`, default #1 the newest; `-y`). Any change qualifies, an adapt included. Leaves the active plan version alone — unlike `plan rollback` (DESIGN_workout_revisions.md §10). Unrelated to `workout restore`. |
| `workout`    | `batches`    | `w b`    | List every command that wrote workouts, newest first: positional `#N`, when, kind, revision count, date span, plan version. A pass that appended nothing reads `(held)`. Every entry is undoable, including the newest — there is no separate unnumbered `live` row, because the change that wrote the plan in force is itself in the list (DESIGN_workout_revisions.md §10) |
| `workout`    | `adapt`      | `w a`    | Run daily adaptation check (`-d/--date` one day: `YYYY-MM-DD`, `today`, `-1d`; `-m` athlete note — kept, since adapt takes no block selector; `--lookback DAYS` overrides `metrics_lookback_days` for this run; `-y` auto-apply). Draws the same end-of-schedule hint `status` does, and **refuses** outright when every block of the plan is behind today — there is nothing to adapt towards, and it used to close with a green all-clear over an empty calendar (DESIGN_runway_nudge.md §4) |
| `workout`    | `push`       | `w p`    | Sync planned workouts to Google Calendar. Defaults to today onward; pushes only unsynced unless `-f`/`--force` re-pushes already-synced ones. |
| `workout`    | `swap`       | `w sw`   | Swap two workouts by dates (`<date> <date>`) or IDs (`<id> <id>`), same kind on both sides, plus a mandatory positional `REASON`. Runs recovery checks (consecutive hard days, load spikes, mesocycle crossings), prompts on warnings unless `-f`; syncs unless `--no-sync`; the reason is folded into `modification_reason`. |
| `workout`    | `wipe`       | —        | Delete all workouts                                                      |
| `workout`    | `prune-calendar` | —    | Delete Calendar workout events that no local row references — the orphans a fresh DB, a restored backup, or a wipe that never reached Calendar leaves behind. Ownership read from the `source=TrainMate` tag, not from stored ids; events of soft-removed workouts are kept. `-d RANGE` windows it (as on `data wipe`), `-n`/`--dry-run` previews, `-y` skips the prompt |
| `data`       | `pull`       | `d p`    | Fetch Garmin activities/metrics and Google Calendar signals (`-d RANGE`/`--metrics-only`/`--activities-only`/`--sleep`). Defaults to the last 2 days ending today. |
| `data`       | `bootstrap`  | `d b`    | Cold-start reconstruction over the full backlog; seeds evidence-based learnings, sets the reflect watermark. Flags: `-d RANGE`, `--context`, `--force`, `--inspect-only`, `--auto`. No date filter → window auto-detected (since previous goal, else 12 wk). |
| `data`       | `reflect`    | `d r`    | Incremental analysis since the reflect watermark; updates learnings + physiological insights (no cycle inference — §10.3) and resolves pending demotions (same flags as `bootstrap`). Window ends on the last completed week unless an end date is given, so a mid-week run with nothing complete costs nothing. `--auto`: unattended — staleness demotions auto-apply, contradiction ones stay queued. |
| `data`       | `show-metrics` | `d sm` | Show athlete metrics over a date range (default 7-day lookback). Selectors `-d`/`-m`/`-M`/`-g` plus `-a`/`--all`, `--no-pull`, `--csv`. |
| `data`       | `show-activities` | `d sa` | Show completed activities over a date range (default 7-day lookback). Selectors `-d`/`-m`/`-M`/`-g` plus `-a`/`--all`, `-t/--type` filter, `--no-pull`, `--csv`. |
| `data`       | `show-analysis` | `d san` | Show the reconstruction stored by the last `bootstrap` — inferred macro focus, the mesocycle blocks `progress` draws as `~` bands, physiological insights. Strictly read-only (renders the slot; never calls the LLM, unlike `bootstrap --inspect-only`). `--short` reads `reflect`'s slot instead; flags when activities post-date the slot's window. |
| `data`       | `backfill-tss` | —      | Recompute the measured `tss` for all stored activities under the current zone model (no Garmin calls), then refresh derived workload |
| `data`       | `wipe`       | `--garmin`, `--calendar`, `-d RANGE`, `-y` | Delete cached data. No scope flag = everything (Garmin evidence + daily signals) and reset watermarks; `--garmin`/`--calendar` narrow the scope; date flags restrict to a window |
| `settings`   | `list`       | `se l`   | Every preference with its value and where that value came from — the stored row, `config.yaml`, or the built-in default. With a NAME, that one setting in detail: the numbered model menu for `coach-model`, the local clock for `timezone`. A bare `settings` lists — the read-only-family exception (DESIGN_cli_noargs.md §a3, applied by DESIGN_settings.md §4) |
| `settings`   | `set`        | `se s`, `se use` | Change one preference: `settings set coach-model 3`, `settings set timezone Europe/Paris`, `settings set morning-time 07:00`. The name takes any unambiguous prefix. Validated by the setting's own parser — a value it cannot read is refused and nothing is written. Stored in `settings`; survives restarts |
| `settings`   | `reset`      | `se r`   | Forget one stored preference so `config.yaml`, or the built-in default, rules again |
| `journal`    | —            | `j`      | The operational record: one row per command run, newest first — id, when (athlete's zone), source, command, wall time, model calls · tokens, and how it ended (`ok`, `warn`, `cancelled`, `FAILED`, `?` for a run with no `run.end`), with a gray legend under the table glossing the outcomes on screen. Every run that did not simply finish also gets a line under the table saying why — the exception for a `FAILED` one, the first warning it logged otherwise — so `warn` is never a status you have to open a second command to decode. Runs that only looked — `list`, `show`, `status`, `journal`, any `-h` — are left out unless `-a` asks for them, unless they went wrong or called a model; command lines and those reasons are clipped to the width of the screen unless `-v` asks for them in full (DESIGN_logging.md §7.1–§7.3). Filters: `-n N`, the shared `-d RANGE`, `--source`, `--command`, `--failed`, plus `--cost` for the by-model/by-command token rollup and `--follow` to tail the file live (DESIGN_logging.md §7) |
| `journal`    | `show`       | `j 5a0e` | Everything one run wrote, by id prefix: where it ran, each event as an offset from its start, the LLM exchange files it produced, the traceback if it failed, and the runs it spawned. The bare `journal <id>` form is the same command; an ambiguous prefix lists what it matched |
| `journal`    | `prune`      | `j p`    | Force the retention sweep now — journal days past `logging.retain_days`, exchange files past `logging.retain_exchange_days`. Otherwise it runs at most once a UTC day, off the first command to finish (DESIGN_logging.md §10) |

---

## 8. Web API Endpoints

Flask server at `trainmate_web.py`, runs on port 5000. Static files served from
`static/`.

**The dashboard is read-only.** It reads the database and renders it: it never writes a
row, never calls Garmin, never calls the LLM, and never touches Google Calendar. Every
one of those is a CLI (or bot) action. The rule is enforced, not merely documented — a
`before_request` guard 405s every mutating verb, and `tests/test_web.py::TestReadOnly`
fails if any route is registered with one.

It is also the one surface that opens **no journal run**: a read-only GET neither changes
anything nor costs anything, so there is nothing to bracket, and keeping the run stack out
of Flask's threaded request handling is the second reason. Only failures are recorded — an
`internal` record with no run id, written by an `errorhandler` that re-raises so Flask
still renders the response it would have (DESIGN_logging.md §13).

That is a deliberate demotion from the previous contract ("the API tracks the CLI feature
set"), which decayed silently: parity was achieved once, in June 2026, and every feature
added CLI-first afterwards — daily-signal authoring, the benchmark logbook, model
selection — was simply missing from the web with nothing to signal it. A surface that
only reads has no parity to lose. New CLI commands add a *view* here when their data is
worth looking at, and cost nothing when it is not.

Consequences worth having: no request can leave the database in a state the CLI did not
put it in, so the app is safe to leave running and cannot race the CLI or the bot over a
workout row; and it needs neither the Calendar service-account credentials nor an LLM key
to start, because it imports neither `google_calendar` nor `coach_service`. The one thing
it did need from the coaching engine — the plan-shaping config fingerprint behind the
"config changed" banner — now lives in `config.plan_config_hash()`, which the engine
delegates to, so the two cannot drift.

The **front-end** (`static/index.html` + `static/app.js`) is organized into six
top-level tabs, all lazy-loaded on first show:

- **Dashboard** — status, recovery/load metrics, sync freshness, coach-memory summary,
  objective and constraint listings, the active LLM (`settings list coach-model`), and the
  strategy card
  (philosophy, the goals/constraints snapshot the plan was generated from, the pending
  feedback log, the mesocycle timeline — a block's own notes appear in its details panel
  — and a "Plan versions & compare" disclosure whose
  superseded entries render `/api/plan/diff` into `#plan-diff-panel`).
- **Workouts** — date/sport/removed-filtered list with the derived state markers, the
  compare/adherence view, and the archived-batch listing.
- **Progress** — an `<img>` framing the server-rendered `/api/timeline.png` chart with
  `8w`/`26w`/`all` quick ranges (DESIGN_progress_timeline.md §7.3/§8.5), plus the **time
  in zone** tables: the web form of `tm progress -z`, one table per qualifying sport,
  measured behind today and prescribed ahead of it, each week also drawn as a stacked
  proportion bar (DESIGN_intensity_distribution.md §9.6/§9.8).
- **Benchmarks** — the current effective threshold set and the logbook, newest first,
  each row carrying its direction-aware delta against the previous row of the same kind
  (DESIGN_benchmark_workouts.md §3.2/§6).
- **Learnings** — filterable list with per-week evidence.
- **History** — activities and recovery-metric tables, and the **daily-signal**
  visualisation: the metric vocabulary (`signal list-metrics`) as chips, then one
  calendar strip per metric shaded within that metric's own range, over the raw rows.

Panels that used to carry a button now name the command that does the job
(`tm plan generate`, `tm workout swap`, `tm learnings demote`, …), including the
long-standing CLI-only flows `data pull`/`bootstrap`/`reflect` (interactive / MFA-bound,
DESIGN_garmin_direct_pull.md §11). Sport filter dropdowns are built from the sports
actually present in the data rather than a hardcoded `<option>` list — the old list had
itself fallen behind the canonical sports.

Endpoints delegate rather than re-derive, so the two surfaces cannot disagree:
`GET /api/workouts` annotates rows with `calendar_status` + `modification_status` and,
for a row today or earlier, the `adherence` verdict `workout list` marks (§5),
`/api/workouts/compare` reuses `analyze_adherence`, `/api/plan/diff` returns
`plan_diff.diff_plans` verbatim, and `/api/zones` picks its sports and currencies with
`intensity.window_sport_stats`/`select_zone_sports`/`zone_currency` — the same three
functions the CLI tables call, which moved from `cli/progress.py` into `intensity.py` for
exactly that reason.

| Method | Path                            | Description                                  |
|--------|---------------------------------|----------------------------------------------|
| GET    | `/api/status`                   | Active goal, latest metrics, coach learnings (under `coach_learnings.learnings` + `.summary`), macrocycle+mesocycles, `config_mismatch`, `sync_state` (data freshness) |
| GET    | `/api/objectives`               | All objectives (`goal list`)                 |
| GET    | `/api/constraints`              | Active + upcoming directives — the read view of `constraint list`. Its window is a rolling `metrics_lookback_days` plus everything upcoming, **not** the CLI's active-mesocycle anchor |
| GET    | `/api/workouts`                 | List workouts (`?start_date=&end_date=&sport_type=&include_removed=`). Rows carry derived `calendar_status` + `modification_status`, plus `adherence` (`{status, label, reasons, completed}`, null for a row still ahead of us) from the CLI's own `adherence_verdicts`. No pull — §8. |
| GET    | `/api/workouts/compare`         | Plan-vs-actual adherence (`workout compare`); no `ensure_data`. `?start_date=&end_date=&sport=` (default 14-day lookback, end capped at today) → `{filters, days[], discrepancies[], informational[]}` |
| GET    | `/api/workouts/batches`         | Workout changes, newest first (`{batches:[{id, created_at, kind, summary, workouts, held, restorable, first_date, last_date, macrocycle_ids}]}`); undoing one is `workout rollback` |
| GET    | `/api/plan`                     | Active plan for a goal (`plan show`): `?goal_id=` (default next active) → `{goal, macrocycle, mesocycles}` |
| GET    | `/api/plan/versions`            | Plan versions for a goal (`?goal_id=`; active + superseded) |
| GET    | `/api/plan/diff`                | Compare two plan versions (`?goal_id=&from_version=&to_version=`; defaults to previous vs active) → `{goal, diff}`, the same `plan_diff.diff_plans` structure the CLI renders. `{error, code}` + 400/404 when the pair cannot be formed |
| GET    | `/api/zones`                    | Per-sport, per-zone time in zone (`progress -z`). `?weeks=N\|all` (default 8), `?sport=` (repeatable, overrides the volume filter), `?currency=hr\|power` → `{window, sports[{sport, currency, zone_labels, coverage, weeks[]}], omitted}`. Weeks past today carry the *prescribed* zones (`future: true`); a future week planned in the other currency reports `currency_mismatch` rather than converting (§9.8) |
| GET    | `/api/benchmarks`               | Benchmark logbook newest first + current thresholds (`benchmark list`). `?sport=&kind=`; each row carries `formatted`, a direction-aware `delta` vs the previous row of its kind, and `improvement` |
| GET    | `/api/learnings`                | List coach learnings (`?sport=&confidence=&dormant=`) + `summary` |
| GET    | `/api/learnings/<id>/evidence`  | Per-week evidence basis (supporting/contra)  |
| GET    | `/api/timeline.png`             | Progress timeline as a PNG image (same §7.2 renderer as the bot photo): merged past/planned load + projected CTL/ATL/TSB. `?weeks=N` (default 8, ≥1 else 400; `all` = full history) re-windows the past half. Not cached. matplotlib absent → 503 with install hint |
| GET    | `/api/activities`               | Completed activities (`?start_date=&end_date=`) |
| GET    | `/api/daily-signals`            | Daily-signals (`signal list`; `?start_date=&end_date=&metric=`) |
| GET    | `/api/daily-signals/metrics`    | Distinct signal metrics with counts + first/last date (`signal list-metrics`) |
| GET    | `/api/metrics`                  | Cached metrics (range, else last 30 days)    |
| GET    | `/api/models`                   | Configured LLM menu with the active entry marked (`settings list coach-model`) → `{models, active, source, set_at}` |

Every other verb on every path returns **405** `{error, method, path}`.

Writes live in the CLI: `goal`/`constraint`/`signal`/`benchmark` authoring,
`plan generate`/`rollback`/`feedback`, `workout add`/`swap`/`rm`/`restore`/`adapt`/
`generate`/`push`/`rollback`, `learnings edit`/`demote`/`keep`/`rm`, `settings set`, and
`data pull`/`bootstrap`/`reflect`.

---

## 9. Configuration (`config.yaml`)

The file is `config.yaml` at the repo root unless the `TRAINMATE_CONFIG` env var names
another one — that is how a second athlete runs from the same checkout: own config, own
`database:`, own `science/` guidelines, own `logs/`, own Telegram token and Garmin
account; only the code is shared. An explicitly named file must exist and parse (a typo
aborts rather than silently running against the primary athlete's database). Relative
`database:`, `science_dir:`, `logging.dir`, `service_account_file` and
`garmin.token_dir` values resolve against the config file's directory — or, when the
top-level `data_dir:` key is set, against that directory (itself config-file-relative),
which moves the whole instance's state under one prefix without repeating it on every
path key — the Garmin token store (default `.garminconnect` beside the config,
DESIGN_garmin_direct_pull.md §11) included, so a `data_dir:` instance keeps its own:
garminconnect resumes a stored token before it looks at the configured email, so two
athletes sharing one store would silently pull one Garmin account's data. Two athletes
share one set of guidelines only by pointing `science_dir:` at the same absolute path,
never by default.

Keys — dotted names are nested blocks (`llm.api_key` is `llm:` → `api_key:`). Everything
but the credentials is optional and falls back to the default shown:

| Key                    | Type | Description                                                   |
|------------------------|------|---------------------------------------------------------------|
| `llm.api_key`          | str  | OpenRouter key; the `OPENROUTER_API_KEY` env var wins when set |
| `llm.models`           | list | Models both model roles pick from, in display order; the first entry is the default until `settings set coach-model` picks another. Absent/empty → `google/gemini-3.5-flash` alone (DESIGN_model_selection.md §1) |
| `google.calendar_id`   | str  | Target calendar ID                                            |
| `garmin.email` / `garmin.password` | str | Garmin login; config.yaml only (kept out of the environment) |
| `garmin.token_dir`     | str  | Garmin token store (default `.garminconnect` beside the config, or under `data_dir:`) |
| `refresh_minutes`      | int | Throttle window shared by Garmin pulls **and** Calendar-signal syncs; reads inside it reuse the cache. Top-level, default 120; read as `config.data_refresh_minutes` |
| `garmin.mutable_days` / `garmin.backfill_prompt_days` / `garmin.initial_backfill_days` / `garmin.throttle_seconds` | — | Auto-ensure tuning (defaults 3 / 30 / 90 / 0.2; see [§10 Data Pull](#data-pull-data-pull-and-auto-ensure)) |
| `garmin.pmc_ctl_days` / `garmin.pmc_atl_days` | int | PMC time constants (42 / 7 — the supported configuration, [§12](#fitnessfatigueform-pmc-model)) |
| `garmin.hr_zone_coverage_min` / `garmin.zone_min_activity_minutes` / `garmin.zone_coverage_display_min` / `garmin.zone_coverage_display_min_by_sport` | — | Load-model coverage gate (0.5), the too-short-to-judge cut-off (20 min) and the display coverage bars (0.8, per canonical sport) — [§12](#load-model-per-activity), DESIGN_intensity_distribution.md |
| `google.service_account_file` | str | Path to service account JSON (default: `service_account.json`) |
| `data_dir`             | str  | Directory every relative path key below resolves against, replacing the config file's directory as the base; itself config-file-relative. Absent → the config file's directory (the pre-`data_dir` rule) |
| `database`             | str  | SQLite file this instance operates on; a relative value resolves against the config file's directory / `data_dir:` (default: `trainmate.db`) |
| `science_dir`          | str  | Directory whose `*.md` files become the ATHLETE-PROVIDED science block in every coaching prompt (`coach/formatting.py:_load_science_guidelines`); a relative value resolves against the config file's directory (default: `science`). The app's own `trainmate/science/` is not configurable |
| `logging.dir`          | str  | Root of the two operator log directories — `runs/` (the journal, read with `tm journal`) and `llm_exchanges/` (the full prompts). Relative to the config file's directory, like `database:` (default: `logs`). DESIGN_logging.md §6 |
| `logging.level`        | str  | Lowest level that reaches the journal file: `debug`\|`info`\|`warn`\|`error` (default `info`). `debug` turns on the records for exceptions the app deliberately swallows on screen |
| `logging.retain_days` / `logging.retain_exchange_days` | int | Days each directory keeps (default 90 each). The sweep runs at most once a UTC day, off the first command to finish; `tm journal prune` forces one |
| `llm.router_model`     | str  | Cheaper model the bot's free-text router (`tm bot route`) and its capture extractions (`tm bot capture`) use; a role, not a `settings list coach-model` entry. Absent → the active coaching model (DESIGN_bot_simple_frontend.md §5.4, §12.2) |
| `telegram.ui`          | str  | Bot persona: `expert` (default) or `simple` — the companion mode (DESIGN_bot_simple_frontend.md §3) |
| `telegram.operator_name` | str | What the companion calls the human who runs the CLI. "Coach" is already the app in the athlete's vocabulary, so the operator gets a word of their own; absent → "the person who set this up for you" (DESIGN_render_persona.md §5) |
| `telegram.push.*`      | —    | Morning push (simple ui only): `enabled` (default true), `morning_time` (`08:00`), `morning_deadline` (`15:00`), `adapt_first` (default false → run `workout adapt -y` before rendering) |
| `telegram.bot_token` / `telegram.allowed_chat_ids` | — | The bot's token (or the `TELEGRAM_BOT_TOKEN` env var) and the numeric chat-id allowlist ([§2](#entry-points)) |
| `telegram.command_timeout_seconds` / `telegram.prompt_timeout_seconds` / `telegram.wrap_width` | — | The silent-run watchdog (180), the idle-prompt cancel (300) and the chat wrap width (48) |
| `web.host` / `web.port` / `web.debug` | — | Where the dashboard binds (`127.0.0.1` / 5000 / false) |
| `coach.metrics_lookback_days` | int | Rolling window for adaptation (default: 15)                  |
| `coach.adapt_terminal_window_days` | int | How close to a block's end counts as its terminal window (default: 3). Gates what the coach **model** is told (`THIS BLOCK IS ENDING`); the CLI's end-of-schedule hint runs on the knob below |
| `coach.runway_warning_days` | int | How many days ahead the daily surfaces announce that the scheduled workouts run out — and how many days past the end they keep saying so before going quiet (default: 7). DESIGN_runway_nudge.md §7 |
| `coach.workout_generation_span_days` | int | Default span length for `workout generate` (default: 28)     |
| `coach.workout_commitment_days` | int | Days from today the athlete is treated as already committed to: the sessions standing there reach the `workout generate` prompt to be answered for one by one, and a removal there keeps its Calendar event, marked (default: 7; 0 disables both). Also a setting (`settings set commitment-days N`). DESIGN_plan_change_continuity.md §4.1 |
| `coach.replan_displaced_load_pct` / `coach.replan_rest_span_days` | — | The two constraint-magnitude triggers behind the `constraint add`/`edit` replan proposal (defaults 50 % / 3 days; [§3](#coachservice)) |
| `coach.minor_activity_load_threshold` | float| Workload score below which an activity is "minor"            |
|                         |      | (default: 25). Controls rest-day violations and unplanned    |
|                         |      | activity visibility (shown as gray/minor if below threshold,  |
|                         |      | yellow/unplanned if above). Mismatch tolerance for planned   |
|                         |      | workouts is dynamically computed from expected workload.     |
| `coach.rpe_divergence_ratio` | float| sRPE-load ÷ measured-load above which a session is flagged   |
|                         |      | to the coach as "felt harder than measured" (default: 1.5;   |
|                         |      | set very high to disable)                                    |
| `learning_confidence_thresholds` | dict | Distinct net supporting weeks to reach each confidence |
|                         |      | level: `{moderate: 3, established: 5}` (defaults). Tentative ≥1 |
|                         |      | and proposed-retirement ≤0 are fixed. Re-levels on recompute. |
| `coach.threshold_replan_pct` | float| Relative drift (%) a physiological threshold may move from   |
|                         |      | the plan-generation value before the plan is flagged stale   |
|                         |      | (default: 5).                                                |
| `user_profile`          | dict | Athlete profile block (see below); no threshold required     |

`user_profile` keys: `name`, `birth_year`, `gender`, `max_hr`, `weekly_target_hours`,
`sport_preferences`, `chronic_injuries`, `preferences`, `equipment`,
`weekly_schedule`. `weekly_schedule` maps day names to
`{total_available_hours, max_sessions, certainty_percent, equipment}`; it is
optional — without it the coach places sessions on any day, sized by
`weekly_target_hours`, and the prompt says so instead of listing days. Trainable
thresholds (`ftp`/`lthr`/…) are **not** here — they live in the `benchmark_results`
logbook (`DESIGN_benchmark_workouts.md` §3.4); config keeps only quasi-fixed `max_hr`. A
threshold-less profile is a valid cold start (the coach nudges, never refuses).

Not every key flags the active plan stale when edited. `name`, `equipment` and each day's
`equipment` reach the prompt but do not shape the periodization, so editing one proposes no
replan; the rest of the block does (`DESIGN_plan_staleness.md` §3–§4).

---

## 10. Key Data Flows

### Plan Generation (`plan generate`)
1. `CoachService.plan_generate()` fetches active objectives +
   constraints.
2. Computes `goals_hash`, `constraints_hash` (plan-shaping constraints only), `config_hash`.
3. If existing macrocycle has matching goals/constraints hashes,
   `config_changed()` reports no drift, **no plan feedback is pending** and
   `force=False` → reuse. Pending notes are a plan input, so they open the gate on
   their own — feedback applies without `--force` (DESIGN_plan_feedback.md §7).
4. Otherwise: builds a read-only **planned-vs-actual review** via
   `_build_prior_training_context()` (Option A — anchored on the elapsed mesocycle
   windows of the prior plan and of the current one, each with its per-sport per-zone
   intensity table, block-over-block delta, prescribed-zone table and per-week
   planned-vs-actual load lines, plus every cached reconstruction's summary,
   reverse-engineered macro/mesocycle blocks, and physiological insights;
   written to no `feedback` field) and passes it as
   `prior_training_text` into `CoachEngine._plan_generate_strategy()` →
   LLM → `{strategy, mesocycles}`. Active coach learnings ride alongside it as
   `learnings`, read-only. See DESIGN_backward_evaluation.md §6, §10.1.
   The review is echoed to the screen only under `--show-llm-context`
   (`plan_generate(show_context=...)`): it runs to ~164 lines, 321 once re-laid-out at
   Telegram's width, and pushes the strategy below the fold. Off the flag the display
   copy — a second, width-specific build of the same review — is not made at all, and a
   one-line aside names the flag instead (DESIGN_output_verbosity.md §7).
5. The plan window runs from the start date to the goal, with **no minimum or
   maximum length** — how to periodize a three-week run-in or a two-year horizon
   is a question the science guidelines answer, and TrainMate does not pre-empt
   it with a duration threshold. The only check is that the window exists (goal
   strictly after the plan start). TrainMate never invents intermediate goals;
   an athlete who wants a tune-up event as a milestone adds it as a goal, and
   `plan_generate` then plans to whichever goal comes first.
   The one way the window reaches *earlier* than the plan start is a kept in-flight
   block: replanning mid-block, the model may carry that block over at its original
   start date rather than cutting it at today (`DESIGN_block_progress.md` §7). Offered
   only when the plan starts today, the covering block began before today, and this is
   not a `--fresh` run.
6. Saves new macrocycle + mesocycles to DB (old ones deleted via
   `save_macrocycle`).

The strategy/plan and adapt prompts also receive the current PMC block: the latest
Fitness/Fatigue (CTL/ATL/TSB) line, the single CTL ramp line, and the still-warming-up
flag (DESIGN_pmc_fitness_fatigue.md §5.2). Forward taper projection — projecting
event-day TSB over the plan's own workouts — is a deferred Phase 2 follow-up.

### Workout Generation (`workout generate`)
1. CLI resolves the generation **span**, both ends of it (`_resolve_span`): whatever
   `-d`/`-m`/`-M`/`-g` select (`-g 7` = goal 7's whole plan; `-d 4w`; `-m 5` = block 5's
   own first-to-last day), with an unselected start meaning the day after the schedule
   stops — today once it has run out, and refused when the plan is covered to its last day
   — and an unselected end meaning `config.workout_generation_span_days` (default 28). A
   selector's own unbounded start is still filled with today by `resolve_window`, so only
   the fully unselected case carries on. A span never opens before today. One mutually exclusive group: a span is one choice. `_warn_span_change` names
   the days a run no longer touches while that reading is still new. See
   DESIGN_cli_selectors.md §8.
1b. Before spending the LLM call, the CLI confirms it when live workouts already exist
   inside that span — a regen is archive-and-rebuild, not fill-in, so a repeat run
   would otherwise cost a call the athlete never meant to spend. The question names the
   count, span, how many were hand-added, and the span being rebuilt. `-f/--force` skips
   it (and the apply gate at step 5, and the out-of-date-plan warning) for unattended runs.
2. `CoachService.workout_generate(start_date=..., end_date=..., prefer_macro_id=...)`
   clamps the start to today, computes `num_days` from `(end_date − gen_start)`, then
   resolves the periodization blocks governing `[gen_start, gen_end]` via
   `db.get_governing_mesocycles` — no goal is named, the dates decide
   (DESIGN_cli_selectors.md §8). Sequential plans both apply; two plans over the same days
   are settled by recency (or by `-M`), and a span reaching past the last block is
   reported. Raises "run `plan generate`" when no block governs the span at all.
3. Fetches metrics history (last `metrics_lookback_days` days) + baseline.
3b. `_block_progress_context(today, gen_start)` builds the elapsed part of the block whose
   remainder this run is writing (DESIGN_block_progress.md). Two halves in one section:
   **volume/adherence** — each already-trained Monday-week's planned-vs-actual load via
   `progression.weekly_aggregates`, the same maths `tm progress` renders, so coach and
   athlete never read different numbers — plus the fitness tests the block has already run;
   and **composition** — `intensity.block_report` with two arguments adapt never passes:
   `previous=` for the block-over-block delta, and `fetch_workouts=` for what the plan
   PRESCRIBED over the same rate window, from §9.8's `planned_zone_sec`. That pair separates
   a mis-designed block (measured tracks the prescription but not the focus — generate's to
   fix) from a mis-executed one (measured diverges — adapt's, and re-shaping the block around
   it would reward the drift). This is DESIGN_intensity_distribution.md **§9.2a**, the
   amendment that lands §9.4's handoff; §9.2's line itself is unchanged. Threaded as its own
   `block_progress` argument, deliberately *not* via `meso_text` (§9.3). Anchored on
   `gen_start`, so the day preserved for an already-completed session counts as history.
   Returns `(text, has_intensity)`: the composition prompt section quotes the zone tables, so
   it is gated on those tables having rows, asked of `intensity.measured_window` — the same
   window `block_report` builds the table from — so gate and table cannot disagree. Nothing
   is emitted when today falls outside every block, when `gen_start` is on/before the block's
   first day (generate is writing the whole block), or when nothing is banked — the prompt is
   then byte-identical to before.
3c. **The commitment window** (`commitment-days`, default 7, DESIGN_plan_change_continuity.md
   §4). The sessions already standing in the span that the athlete has read — today
   through `today + N − 1` — plus every session they added by hand anywhere in the span,
   reach the prompt as "SESSIONS ALREADY STANDING", each tagged `[COMMITTED]`,
   `[BENCHMARK: …]`, `[ADDED BY THE ATHLETE]` or `[REST DAY]`, with what it was first
   prescribed as when an adaptation has eased it. Both bounds matter: a bare run extends
   into empty days and sees an empty block, and a forward-selected run must not be asked
   about days it cannot write. The coach also gets the constraints that ended earlier in
   the current block, marked past — they are why a week in the block's record went quiet
   (§6.1).
4. Calls `CoachEngine._workout_generate_logic(num_days=...)` → LLM →
   `{reasoning, athlete_note?, workouts[]}`. **Read-only** w.r.t. coach learnings (see
   [§3](#3-coach-package-architecture)).
4b. `_resolve_standing` maps the answers onto that block: `keep` becomes the session it
   names, `drop` becomes a rest day replacing it, a full entry on a rest-only date
   replaces the rest day whether or not the coach said so, and a standing session no
   answer mentions is kept. What an entry takes the place of travels on it as
   `replaces_slot`/`replaces_lineage`. A `replaces` is refused — with a notice, both
   sessions left standing — when its destination is outside the span, its source is not
   in the block, its target is claimed twice, or its destination is occupied by a session
   that stands (§4.5). The deterministic passes run after: a `rest` constraint's row
   continues the first standing session on the date and voids any other under the
   constraint's own title (§5.5).
5. `workout_generate` returns the sessions as a `GenerateProposal` — nothing written yet.
   It carries the voids this run will make, with their reasons, and a `StandingLine` per
   session the athlete was already told about, computed from what apply *will* write
   rather than from what the coach answered: the no-op rule silently suppresses a
   wording-only revision, and the deterministic passes remove days no answer mentions.
   The CLI prints that report, then the sessions one per line through the *same*
   `workout_line` renderer as `workout list` (which drops the `ID:` column when there is
   no row yet), so the plan being accepted reads exactly like the plan that will be listed
   afterwards, then asks. Declining leaves the live plan untouched; `-f/-y` accepts
   without asking but still prints the report.
6. On a `y`, `workout_generate_apply(proposal)` opens one `generate` change, stamped with
   the coach's line to the athlete (`note`) and the window in force (`commitment_end`).
   The proposal's voids go first, so a session the plan drops is ended before anything
   else can take its slot — and so a session the athlete added is marked rather than
   erased. Then every day the plan fills gets a revision tagged with the `macrocycle_id`
   the proposal carries, its `change_reason` in the row's `reason` column, and the lineage
   of the session it replaces when it replaces one — unless the prescription is identical
   to what is already live, in which case nothing is written and the day is left alone. A
   day the plan KEEPS is spared both. Calendar follows from the change handle's reconcile
   pass, not from the command. A session the athlete added by hand that the plan removed
   is named, with the change id to undo it. Undoable via `workout rollback`, or
   `plan rollback` to step the strategy back with it
   (see [§3](#3-coach-package-architecture)).

### Daily Adaptation (`workout adapt`)
1. `CoachService.workout_adapt()` fetches metrics + planned workouts + completed
   activities in window. Workouts in the window are fetched with
   `include_removed=True` and partitioned into active (planned) vs `removed`;
   removed ones are passed to `_workout_adapt_logic` and rendered in the prompt as
   deliberate cancellations (not misses). It also fetches the window's
   `daily_signals` rows (reaching one day before the metrics window, since recovery
   lags the signal) so the LLM can attribute a depressed morning to lifestyle noise
   (alcohol/poor sleep the day before) vs genuine training fatigue. It may still ease or
   **reschedule** today's hard session for acute readiness, but must not read a
   lifestyle-suppressed morning as evidence the *block* is too hard (no permanent cut to
   planned volume, not counted as training fatigue). This is what makes the
   quantitative-signal learning actually move a decision rather than stay inert at the
   daily load call (DESIGN_quantitative_signal_impact.md §6.1). An optional `-m/--message`
   athlete note for this run is passed through verbatim and rendered as a bounded prompt
   section (advisory, ephemeral — see `workout_adapt` in
   [§3](#3-coach-package-architecture)).
2. `analyze_adherence()` (`adherence.py`) computes discrepancies (misses,
   duration/load mismatches, rest violations) over the **active** workouts only —
   removed workouts never count as misses. The window *ends on the evaluation date*, so
   `pending_from=target_date` marks that day's untrained sessions **pending**, not
   missed: adapt runs in the morning and the athlete has not had the day yet. Assuming
   a session will still happen is the default; withdrawing it is `workout remove`, and
   saying it won't happen is the `-m/--message` note. Reporting it as a miss told the
   coach work had been skipped and invited it to reschedule sessions nobody skipped —
   how a benchmark once ended up scheduled on two dates at once. Only the *absence* of
   an activity is deferred this way: a rest violation, an unplanned ride, or a
   partially-executed session on that date is evidence already in hand and still
   reports. Consumers read the `pending` flag on each matching row rather than
   re-deriving the date rule.
3. Finds active mesocycle for the target date → sets `meso_end_date` for
   adaptation range.
3b. `_intensity_block_context()` builds the block's **measured intensity distribution**
   (`trainmate/intensity.py`): the block to date as a per-week rate over its *completed*
   weeks, per canonical sport and per zone, beside its stated focus, plus the current
   week's raw minutes and elapsed fraction. Threaded as its own `intensity_context`
   argument — deliberately *not* via `meso_text`, which is shared with plan generation
   and must not grow this section (DESIGN_intensity_distribution.md §9.3). It gates a
   fourth TASK branch and the `CORRECTING EXECUTION DRIFT` prompt section: the existing
   three branches all treat adaptation as a response to fatigue or absence, and an
   athlete running their easy days at Z3 is neither — perfect attendance, normal RHR/HRV.
   Nothing is emitted when today falls outside every block.
4. Calls `CoachEngine._workout_adapt_logic()` → LLM → `{change_needed, reason,
   adapted_workouts[]}`. **Read-only w.r.t. coach learnings** (see
   [§3](#3-coach-package-architecture)).
5. Drops any proposal dated past the adaptation range end (the block firewall's write
   side) and any targeting an already-completed session, then returns
   a `RevisionProposal` ([§3](#coachservice)) — caller decides whether to apply, and
   confirms each extracted constraint and signal candidate before persisting it.
6. If applied: `workout_revision_apply()` opens one `adapt` change. A session the pass
   overrides with nothing becomes a **void** — this path used to `DELETE` the row, with no
   way back. A session it substitutes cross-sport becomes a void at the source plus a
   revision at the destination carrying the same lineage, the swap shape, which is what
   keeps the adaptation tally following the session. Every revised session appends with
   its own note; the batch rationale lands on the change row. Nothing here touches
   Calendar — the reconcile does (see [§5](#5-database-schema)).

   There is no `adapted_at` to stamp any more, and so no flag to carry or forget. Whether
   a revision counts as an easing is decided at read time, by comparing it against its own
   predecessor in the lineage: a drift correction that rewrites the prescription and holds
   the load never counts. That was the interim measure DESIGN_intensity_distribution.md
   §9.5 flagged; DESIGN_workout_revisions.md §7 is the fix it named.

### Data Pull (`data pull`) and auto-ensure

Data is pulled **directly from Garmin Connect** (`trainmate/garmin/`). Full
design: `DESIGN_garmin_direct_pull.md`.

1. `garmin.pull(start, end)` logs into Garmin (token persistence; TTY-gated
   MFA), fetches activities (storing the **measured** TSS — power TSS or hrTSS —
   and the user's `directWorkoutRpe` if entered; neither is synthesised) and
   daily metrics. It writes a row to `athlete_metrics_cache` for **every day in
   range — even all-null ones** — so the table's date coverage records what has
   been pulled. Activities with low HR-zone coverage and no RPE are reported in
   an aggregated warning (their load is an underestimate).
2. `garmin.recompute_derived()` runs a **full sweep** over all cached days: the PMC
   EWMAs CTL/ATL/TSB (`compute_pmc`, walking every calendar day so rest days decay)
   and the 28-day RHR/HRV/sleep baseline — that is the whole sweep. The ATL:CTL load
   ratio is derived at read time and never swept (§5, §12). A full sweep is cheap
   locally and avoids windowed-recompute bugs.
3. The `sync_state` watermark advances (`through_date` forward only,
   `last_pull_utc` = now).
4. `bike_avg_watts` and `zone1_sec`–`zone5_sec` come from Garmin (NULL when
   absent) and feed `format_completed_activities` in `coach/formatting.py` verbatim.

**Auto-ensure.** Read-side commands call `garmin.ensure_data(start, end)` at
entry (idempotent per process via an in-memory memo). It pulls the
derivation-padded required window (pad = `max(28, ⌈1.5·τ_ctl⌉)` = 63 days at defaults)
where the gap is small/recent and **prints a copy-pastable `data pull` command for
large backfills** (cold start, big forward/backward gaps), always continuing with
cached data. Gaps lying entirely *before* the requested window — derivation-pad
warm-up data the user never asked to view, bounded by the pad itself — always pull
automatically, so widening the pad in an upgrade self-heals instead of nagging. The same entry point also
rides along a best-effort Calendar daily-signal sync (`google_calendar.sync_calendar_signals`),
gated by the same `data_refresh_minutes` throttle. When that throttle keeps a read on
cached data (Garmin or Calendar), a one-line note says so. These commands support
`--no-pull` to bypass the sync entirely (cache-only) and `--force-pull` to refresh even
within the throttle window (the two are mutually exclusive). Calendar dates use the
athlete's timezone (`util.today_str`/`today_date`, DESIGN_user_timezone.md §1);
stored instants stay UTC. The web app never calls this — it is a pure reader (see §1).

### Data Analysis (`data bootstrap` / `data reflect`)
Both commands share the `CoachService._run_workout_analysis()` core; they differ
only in how the window is resolved and whether they set vs. advance the reflect
watermark (stored in `sync_state` under the `reflect` key).
- **`data bootstrap`** (cold-start, run once): resolves a wide window
  automatically from active/preceding goals (else 12 weeks back) when no date
  filter is given, runs under horizon `long`, and **sets** the reflect watermark
  to the window end. This is the reconstruction `plan generate` reuses — and the one
  `progress timeline` draws its `~`-prefixed inferred bands from (see `analysis_cache`
  in [§5](#5-database-schema) for both consumers and the horizon-slot rule). Completion
  is recorded under the `bootstrap` `sync_state` key; a repeat run is detected and
  confirmed before re-running (`--force` proceeds, `--auto` skips, `--inspect-only`
  is never gated), since re-running re-pays for the LLM pass and resets the baseline.
- **`data reflect`** (incremental): starts the window at the day *after* the
  reflect watermark (or an explicit date filter), runs under horizon `short`, and
  **advances** the watermark forward only. Because overlapping history is never
  re-ingested, repeated runs no longer ratchet confidence to `established`. With
  no watermark yet it falls back to a recent window and nudges toward
  `data bootstrap`; with no new evidence it returns early without an LLM call.

The shared core then:
1. Queries completed activities, physiological metrics, and constraints
   overlapping the window (discounting context only — a constraint may explain an
   anomaly away, never support a learning).
2. Builds the `signal_days` block (step 3a below — it moves *ahead* of the cache check,
   because it is hashed), then computes the evidence fingerprint (per-activity load
   fields + metrics + overlapping constraints + the window's `daily_signals` + the
   full-history `signal_days` block as computed) and checks `analysis_cache[horizon]`.
   If the fingerprint matches and `--force` is absent → returns the cached
   reconstruction (no LLM call). `--force` recomputes regardless. The check lives only
   here, in the *writing* flow; the readers of the cache never re-verify it.
3. Groups metrics and activities week-by-week using Monday-commencing ISO weeks,
   enriching each weekly summary (DESIGN_richer_analysis_evidence.md) with the
   constraints overlapping that week (tagged `full`/`partial` against the week's
   **in-window** span, not calendar Monday–Sunday — so a truncated first/last week can
   read `full` for a constraint that covers only the in-window part), `avg_sleep_score`/
   `avg_stress`, and `vs_baseline_z` (deterministic rhr/hrv/sleep z-scores vs the
   rolling baseline, omitted when unsupported). All deterministic — no extra LLM
   call. The per-day z is computed by the shared `_day_response_z(metric_row,
   baseline)` static; `_week_response_features` averages it over the week.
3a. Builds `signal_days` — episode-aligned external-signal impact rows
   (`_signal_days`, DESIGN_quantitative_signal_impact.md). Per signal category it
   clusters logged signal-days into *episodes* (runs separated by fewer than `k`
   drink-free days, `k = signal_days_lookahead`, default 3) and emits, per episode,
   a `days` dose sequence ({date, value, day-of `load_tss`}) plus a
   `surrounding_mornings` strip spanning `(first − k + 1) … (last + k)` — each
   morning tagged with its preceding day's load and the `_day_response_z` recovery
   deltas, dropping any channel that duplicates the signal's own construct. Pure
   clustering + join + the existing z — **no statistics**. Unlike the weekly
   summaries this is fetched over the athlete's **full signal-day history** (not the
   analysis window), so the LLM sees the whole pattern even on an incremental
   reflect; categories below `signal_days_min_days` (default 1 — show whatever
   exists) are dropped, same-day same-category rows are summed into one dose, and the
   block plus its prompt guide are rendered only when non-empty. Because the block is
   built before step 2 and hashed **as computed**, a signal or activity edited *outside*
   the analysis window still invalidates the cached reconstruction. The rows are
   recomputed each run (never stored as a learning); only the LLM's conclusion
   becomes a `coach_learnings` row, citing the in-window weeks the signal-days fall
   in (DESIGN_evidence_based_confidence.md §6).
4. Queries `CoachEngine._data_analyze_logic()` (now also handed `signal_days`) -> LLM ->
   `{macrocycle_summary, inferred_macrocycle, inferred_mesocycles[],
   physiological_insights[], learning_updates[]}`.
5. Unless `--inspect-only`: applies `learning_updates` deltas — the LLM attributes
   each observation to the `week_commencing` weeks it was shown; the app validates
   them against the window, dedupes into each learning's evidence basis, and
   re-derives confidence (upgrade auto / downgrade proposed; see
   [§3](#3-coach-package-architecture)). It then caches the reconstruction in
   `analysis_cache`.
6. Unless `--inspect-only`: `_review_learning_proposals(auto)` sweeps staleness
   demotions and resolves pending downgrades — interactively (accept / keep / skip)
   or, under `--auto`, applying staleness directly while leaving contradiction
   proposals queued. See DESIGN_evidence_based_confidence.md §7.

---

## 11. Terminology: Plans vs. Workouts

- **Plan** = periodization strategy: one *active* macrocycle (per objective, with
  superseded versions kept) + mesocycle blocks.  Commands:
  `plan generate/show/keep/versions/diff/rm/rollback/feedback`. `plan versions` lists every
  kept version; `plan show --macrocycle <id>` renders a specific (e.g. superseded) one,
  `--all` every goal's, `--workouts` each mesocycle's sessions; `plan diff` compares two
  versions.
- **Workouts** = daily microcycle activities implementing the mesocycle focus.
  Commands: `workout generate/adapt/push/swap/add/rollback/batches`. `workout generate`
  lists what it proposes and, on a `y`, pushes it to Calendar eagerly; `workout rollback`
  undoes one change and everything after it (`workout batches` lists them), and
  `plan rollback` does the same while also stepping the strategy back
  (DESIGN_plan_rollback.md, DESIGN_workout_revisions.md §10).

A `workout add` manually schedules a single session on a date (athlete-driven,
not coach-driven, and LLM-free). It **replaces** any existing same-sport workout
that day — or, with `--replace-day`, **every** session that day regardless of
sport — recording the overwritten session(s) on the new session's note the way
an adaptation does (`CoachService.workout_add`, §3): each replaced title +
duration/TSS/RPE plus the athlete's optional `--reason` become the
`modification_reason` (rendered "Reason:"), and other-sport entries are prefixed
with their sport. The new session starts its own lineage, so it inherits neither
the replaced session's load nor its Calendar event: a new event is created and
every replaced session's event is torn down. The `source='manual'` surfaces on the
Calendar event as a `[Manual]` summary prefix (composing with `[Adapted]` when
the manual add also replaced a session), so athlete-added sessions are
distinguishable at a glance from coach-generated ones. Load re-balancing of
surrounding days is intentionally **not** done here — run `workout adapt` for that.

A `workout swap` exchanges the dates of two workouts (or moves one onto an
empty rest day). Moved workouts get a `modification_reason` recording the swap
(`Swapped from X to Y`, plus the athlete's mandatory reason appended as
`. Reason given: …`), so they read as `swapped` ([§5](#5-database-schema)), are
re-synced by `workout push`, and are visibly distinguished from untouched ones.
If a swap returns a workout to its
`original_date`, the `modification_reason` is cleared to `NULL` — the workout
is no longer considered modified. The `modification_reason` is surfaced to the
coach in the adaptation prompt (`format_planned_workouts_detailed`), so a swap informs
the coach symmetrically to how `workout rm`'s `removed_reason` does. Swaps are
validated first (`CoachService.workout_swap_validate`): the
new schedule is simulated and the user is warned about newly-created >2-day
high-intensity streaks, weekly load spikes (a relative-overload proxy — a pure
weekly-TSS-delta heuristic), and mesocycle-boundary crossings.

Plan must be generated before workouts. Workouts cover the span the selector flags on
`workout generate` name, both ends of it — by default from the day after the schedule
stops, onward for `workout_generation_span_days` (in `config.yaml`, falling back to 28
days).
`replan()` calls `plan_generate` then `workout_generate` + `workout_generate_apply` in one
step (always uses the config default, and applies without a preview — it is the
unattended path).

---

## 12. Sports Science & Coaching Mathematics

### Load model (per activity)
The stored `tss` column is the **objective measurement only**; the training
**load** is derived on the fly (`garmin.activity_load`) via a best-available
fallback — methods are never blended or max-ed:

1. **Power TSS** (Coggan 7-zone, `POWER_ZONE_TSS_PER_SEC`) when a power meter
   recorded — `TSS/s = IF² × 100 / 3600` per zone (Z6/Z7 extrapolated, Z7 IF
   capped at 1.60).
2. **hrTSS** (Friel 5-zone, `HR_ZONE_TSS_PER_SEC`) when HR-zone coverage
   ≥ `config.hr_zone_coverage_min` (`garmin.hr_zone_coverage_min`, default 0.5).
   Coverage = Σ(HR-zone secs)/duration; guards
   against Garmin's HR zone-1 floor zeroing out low-intensity work (yoga, easy
   walks, lift-served skiing).
3. **Session RPE** (Foster sRPE = `RPE × 10 × hours`) when the user entered an
   RPE and power is absent / HR is too sparse. **RPE is user-entered only and
   never computed from power or HR.**

When method 3 should apply but no RPE was entered, the weak hrTSS (or 0) is kept
and the activity is counted in an aggregated underestimate warning. There is no
activity-type special case — coverage + the divergence flag cover strength and
hybrid sessions (e.g. kettlebell HIIT: hrTSS captures the cardio, divergence
flags the muscular cost).

### RPE divergence (external vs internal load)
`garmin.rpe_divergence` flags sessions where the measurement came from power/HR but the
user's RPE implies ≥ `rpe_divergence_ratio` (config, default 1.5) × the measured load —
i.e. it felt harder than it measured (heat, sleep debt, muscular damage). For those rows
`activity_load()` takes **the load from RPE instead**, and `load_method()` returns the
distinct `rpe_divergence` provenance, so every downstream number (PMC, weekly load,
zones) carries the bump. The stored `tss` measurement is left untouched; the ratio is
what explains the bump to the coach.

### Workload per activity
`Workload = activity_load(act)` — the single fallback value above.

### ATL:CTL load ratio
Relative overload — fatigue against the athlete's own fitness base.
`garmin.pmc.load_ratio(atl, ctl)` = ATL ÷ CTL straight off the PMC EWMAs below, derived
at **read time** and never stored; `None` when either is NULL or CTL ≤ 0 (no base to
divide by). Colouring (`util.color_load_ratio`) is **overload-only**: > 1.5 red,
1.3–1.5 yellow, at or below 1.3 bare — a *low* ratio is phase-dependent, and
`training_load.md` §4 judges the ratio against the planned block rather than a universal
band. This replaced the rolling-sum acute/chronic/ACWR model (7/28-day sums with the
0.8–1.3 "sweet spot"), whose stored columns are dropped by guarded DDL in `db/base.py`
and whose universal band fought block periodization. See DESIGN_load_ratio.md.

### Fitness/Fatigue/Form (PMC) model
Two layers, one recurrence. The EWMAs are the whole stored load model, and the ATL:CTL
ratio above is derived *from* them — different questions off one series: asymptotic
fitness/form (CTL/ATL/TSB) and scale-invariant relative overload (the ratio).

**Backward core** (`garmin/pmc.py`, DESIGN_pmc_fitness_fatigue.md): the classic
Coggan discrete `1/τ` EWMAs walked over every calendar day of history —
`compute_pmc()`, with TSB = *yesterday's* CTL − ATL (day-entering form) —
stored per day on `athlete_metrics_cache` (`ctl`/`atl`/`tsb`) by every
`recompute_derived()` sweep. Time constants come from config
(`pmc_ctl_days`/`pmc_atl_days`, default 42/7; the defaults are the supported
configuration). Leading-edge warm-up blanking (`pmc_warmup_cutoff_for`),
the young-DB caveat (`pmc_data_caveat`), and the ramp rate (`pmc_ramp`)
gate/derive display values. Consumers: coach prompts, `tm status` and
`tm data show-metrics` — the last two render the triple through the shared
`util.pmc_cells`, and derive the one warm-up cutoff through
`cli/common.py:pmc_warmup_cutoff`. (`workout adapt` prints only a one-line day count;
its metrics-trajectory table was removed.)

**Projection layer** (`trainmate/progression.py`, DESIGN_progress_timeline.md):
this is PMC Phase 2, generalized to the full daily series. Past days read the
stored series verbatim (never recomputed — `tm progress` and `tm status` must
agree); the *anchor* is the latest stored row **strictly before today** with
non-NULL PMC, and from it the same recurrence is folded forward
(`compute_pmc(..., seed=(ctl, atl))`) over the merged actual-then-planned daily
load series (past: `activity_load` above; future: `adherence.planned_load` over
non-removed `workouts` — see DESIGN_progress_timeline.md §3 for the seam rule),
stopping at the last generated workout. `compute_pmc` stores its outputs at
**full precision** (rounding moved to display) so the fold reproduces the stored
series bit-exactly. `assemble_timeline` builds the whole payload; `timeline.
build_timeline_payload` is the one row-fetching path behind both surfaces.
Consumers: `tm progress` (CLI, numbers-first + optional `--chart` PNG), the
Telegram bot (text for free via CLI parity, plus the photo transport for the
chart), and `GET /api/timeline.png` (the web **Progress** tab, framing the same
`chart.render_timeline_png` PNG). One computation and one chart renderer feed
all three; only the delivery differs.

### Daily Readiness Signals
- HRV drops > 1 std below baseline mean → flag potential overtraining
- RHR rises > 1 std above baseline mean (min +3 bpm) → flag potential
  overtraining
- `workout adapt` acts on these signals over the rolling
  `metrics_lookback_days` window, but first discounts a depressed morning that a
  logged `daily_signals` signal the day before explains (lifestyle noise, not training
  fatigue): today's session may still be eased or rescheduled for acute readiness, but
  the block's planned load is not cut on a non-training artifact.

### Science Guidelines Files
- `trainmate/science/` — built-in: `benchmarks.md`, `periodization.md`,
  `recovery_metrics.md`, `training_load.md`, `zones.md`
- `science/` (`science_dir`, gitignored) — user-provided; empty by default; any `.md`
  files added here are injected into every LLM prompt. `science.samples/` holds
  ready-made sets to copy from, one directory per training philosophy (see README).
- The two are layered — built-in owns measurement and vocabulary, user owns
  prescription, and the citation only runs one way. Contract in
  [§3](#_load_science_guidelinesapp_science_dir-science_dir--str).

---

## 13. Daily Signals (Calendar Ingest)

External daily signals the coach should factor in — alcohol, sleep quality,
stress, big meals, a heatwave — reach TrainMate through the **single existing
Google Calendar**, not through app-specific features. Producers write one all-day
event per signal-day, tagged in `extendedProperties.private`:
`source=trainmate-context` (the positive marker, configurable via
`calendar_signal_tag`), `metric` (opaque category), and an optional numeric
`value`. Two producers exist: a separate syncer (out of scope, mirroring
`GarminScraper`) for spreadsheet-backed streams, and TrainMate's own `signal`
command (outbound, below) for ad-hoc signals. Full specs:
`DESIGN_calendar_signal_ingest.md` (ingest) and `DESIGN_signal_authoring.md`
(authoring).

**Inbound flow:**

```
Calendar (tagged events) ──► google_calendar.sync_calendar_signals
   ──► calendar_syncer.sync_signals (syncToken; server-side filtered on the full
                                     pull only, client-side otherwise)
   ──► db.upsert/delete_daily_signal_by_event ──► daily_signals table
   ──► coach analysis weekly summaries (per-week `daily_signals`)
```

- **Distinguishing events:** TrainMate writes workouts tagged `source=TrainMate` and
  ingests only events tagged `source=trainmate-context` (or the configured
  `calendar_signal_tag`). The server-side `privateExtendedProperty` filter applies to
  the **full pull only** — the API forbids it alongside a `syncToken` — so the
  incremental stream carries every changed event and is filtered **client-side** before
  anything is parsed. The guarantee is "untagged events are never *ingested*", not
  "never fetched".
- **Sync, not append:** incremental via Calendar `syncToken` — edits upsert by
  `google_event_id`, cancellations delete. A cancelled event arrives stripped of its
  extended properties, so the tag guard cannot run on it: `_ingest_signal_event` counts
  it as a change only when `delete_daily_signal_by_event` actually removed a row —
  otherwise cancelled workouts and cancelled private appointments would inflate the
  reported count on the unfiltered incremental path. First run / expired token (HTTP 410)
  falls back to a full pull of all tagged events (no date horizon needed; the
  list is bulk and sparse). Token persisted in `sync_state[calendar_signals]`.
- **Cadence:** rides along `data pull` (force) and the auto-ensure-before-read
  path (`garmin.ensure_data` → bridge `_sync_calendar_signals`, throttled to the
  Garmin refresh window and memoized once per process). Best-effort: a missing
  calendar config or any Calendar error is swallowed with a warning. The gating
  and error handling live in `google_calendar.sync_calendar_signals`; `garmin/sync.py`
  only bridges to it via a guarded lazy import.
- **Coach use:** two complementary paths. (1) *Qualitative* — each week's summary
  carries a `daily_signals` list (all rows, no collapsing) the LLM reads beside the
  metrics, the same way `constraints` contextualize anomalies. (2) *Quantitative*
  (`signal_days`, step 3a above; DESIGN_quantitative_signal_impact.md) — the
  optional numeric `value` is aligned per-episode against the bracketing mornings'
  recovery and day-of load, full-history, so the LLM can read dose-response,
  persistence, and the drink-and-hard-day confound. Both are hashed into the analysis
  evidence fingerprint, so an added/edited/deleted signal invalidates the cached
  reconstruction **unconditionally** — in-window rows are hashed as fields, and rows
  outside the window reach the hash through the full-history `signal_days` block, which
  is hashed as computed (§10, step 2/3a). Because that block is hashed as computed,
  `signal_days_lookahead` and `signal_days_min_days` are fingerprinted
  transitively: editing either invalidates the cached reconstruction. That is a
  deliberate, narrow exception to config values not being hashed here — a different knob
  produces a different prompt, so the cached answer is not an answer to the current
  question (DESIGN_quantitative_signal_impact.md §8).

**Outbound flow (first-party authoring — the `signal` command):** for
ad-hoc signals where standing up a syncer is overkill (a heatwave), the user can
author the same tagged events directly, since the private-property tag is
unsettable from the Calendar UI. `signal add` writes one tagged all-day event
per day in a range (`add_signal_event`, idempotent upsert-by-(date, metric)) and
mirrors the rows locally via `upsert_daily_signal_by_event` so they appear before
the next pull. `signal rm` deletes the **calendar event** (`delete_event`) before
the local row, so a full re-pull (`data wipe --calendar`) can't resurrect it.
`signal list` (default window: `metrics_lookback_days`, the coach's metrics-lookback
window) and `signal list-metrics` inspect what's recorded. Authored events are
indistinguishable from synced ones downstream — `sync_signals`, the analysis
prompt, and the evidence fingerprint are untouched. Handlers: `cli/signals.py`;
spec: `DESIGN_signal_authoring.md`.

---

## 14. Testing

Tests use `unittest`. Run with:
```
venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

| File                           | What it tests                                                   |
|--------------------------------|-----------------------------------------------------------------|
| `tests/test_adaptation_*.py`   | One file per service mixin: `_adapt` (`workout_adapt()` end-to-end), |
|                                | `_swap` (validation/apply), `_add`, and `_adherence`             |
|                                | (`analyze_adherence()` — misses, tolerances, violations)        |
| `tests/test_analysis.py`       | `data_bootstrap`/`data_reflect`: date resolution, weekly |
|                                | aggregation, cache reuse/force/inspect_only, learnings           |
|                                | injection, reflect watermark advance/skip, bootstrap re-run      |
|                                | guard, per-week constraints + body-response z-scores             |
| `tests/test_constraints.py`    | constraint DB windowing, hard-rest pre-pass, §7 magnitude, §8 message capture |
| `tests/test_cli_*.py`          | One file per command family: output and argument handling, with |
|                                | the service mocked. `test_dispatch.py` walks the parser tree     |
|                                | itself (every leaf binds a handler, every bare group self-helps) |
| `tests/test_calendar.py`       | `calendar_syncer.sync_workout` event description formatting      |
| `tests/test_calendar_lineage.py` | The `History` block an event carries: which revisions, in what order, and when the event goes stale |
| `tests/test_coach_format.py`   | `coach/formatting.py` — the coach-prompt renderers:              |
|                                | `format_completed_activities` (HR/power-zone rendering) and      |
|                                | `format_metrics_history` (None omission, warm-up suppression)    |
| `tests/test_db.py`             | `Database` CRUD, evidence-based confidence (derivation, dedup,   |
|                                | week validation, contradiction/demote/keep, staleness,          |
|                                | grandfather migration), decay, `analysis_cache`                 |
| `tests/test_feedback.py`       | The `plan feedback` log: capture/list/`--rm`, the `-m` atom      |
|                                | (ID, date, name-infix, bare), the pending→consumed lifecycle,    |
|                                | the regen gate + prompt section, the one-off slot migration      |
| `tests/test_intensity.py`      | `intensity.py`: the completed-weeks divisor (first six days,    |
|                                | partial tail excluded from both sides, finished block, weeks    |
|                                | from the block start not Mondays), coverage with a meterless    |
|                                | ride, canonical `cycling` folding, every-zone-named rendering    |
|                                | inside the prompt width, the raw non-extrapolated current week,  |
|                                | per-sport delta suppression, the structural row keeping a        |
|                                | HIIT strength session's hard minutes in the zone table, the     |
|                                | strength/interval note split, the coverage-based currency        |
|                                | choice, and §9.8's planned-zone parse/render                     |
| `tests/test_cli_progress.py`   | `cli/progress.py` formatting: the load table, plus the weekly    |
|                                | zone grid — capped cells, the 48-column power table with a       |
|                                | 10h+ Z2, `—`/`!`/`+` as three distinct facts, the display        |
|                                | coverage bar distinct from `hr_zone_coverage_min`, sport         |
|                                | selection in config order, the orphan-week note, and the         |
|                                | `hr_sparse` week the load table now marks; plus the formatting   |
|                                | helpers and `render_progress`: sparkline/bar scaling, label      |
|                                | truncation, weekly-row rendering (past/in-progress/future/       |
|                                | uncovered), plan-gap vs per-objective projection, lapsed/no-plan/|
|                                | still-warming banners, the part-week marker, and the 48-column   |
|                                | width budget (via `visible_len`)                                 |
| `tests/test_periodization.py`  | `plan_generate`, `workout_generate`, hash logic, |
|                                | system-prompt building                                          |
| `tests/test_garmin.py`         | Garmin transforms (load model), zone parsing, watermark/         |
|                                | auto-ensure policy, recompute, `backfill_tss`                    |
| `tests/test_progression.py`    | `progression.py` pure functions: merged-load seam rule (incl. the |
|                                | zero-load-activity case), stored-read past + anchored fold        |
|                                | (closed-form decay, morning-pull today-row ignored, trailing-NULL |
|                                | anchor skip, no-anchor suppression, non-default τ continuity),    |
|                                | generated-only plan-end clamp, Monday bucketing, in-progress      |
|                                | elapsed split (today only once synced), §6.1 majority-overlap     |
|                                | labeling, part-week plan coverage, band trimming,             |
|                                | `assemble_timeline` payload + coded warnings, `select_weeks`/`clip_payload`, empty states |
| `tests/test_runway.py`         | End-of-runway nudges: `progression.runway`'s four kinds and its   |
|                                | two windows (run-up + passed state), the manual-row and rest-row  |
|                                | rules, the §4 wordings (day zero, past tense, the `-m ..<id>` the |
|                                | block cliff names), the surfaces — `status` outside its goal      |
|                                | branch, `workout adapt`'s refusal over a finished plan, `workout  |
|                                | list`'s marker, the morning push's line/button/silence — plus the |
|                                | §2.1 coverage invariant and a structural check that nothing       |
|                                | outside `cli/runway.py` builds the wording again                  |
| `tests/test_pmc.py`            | `compute_pmc` (also): `seed=(0,0)` reproduces from-zero, split/re- |
|                                | fold reproduces the unsplit series exactly, full-precision output |
| `tests/test_web.py`            | (also) the read-only invariant (every route GET-only, mutating   |
|                                | verbs 405, no Calendar/LLM import), the read views added with it |
|                                | (benchmarks, signal vocabulary, models, plan show, zones), and  |
|                                | `GET /api/timeline.png`: PNG magic bytes, `?weeks` validation,   |
|                                | matplotlib-absent 503, payload shape via the shared builder      |
| `tests/test_utils.py`          | `util.py` helpers (text wrapping, ANSI width, `color_load_ratio`,|
|                                | `default_wrap_width` and the TRAINMATE_WRAP_WIDTH override)      |
| `tests/test_cli_settings.py`   | the `settings` command and its registry: config vs stored row vs |
|                                | `--llm-model` override (DESIGN_model_selection.md §3)            |
| `tests/test_clock.py`          | the athlete timezone: how a name resolves, that `today_date`     |
|                                | reads the stored zone (two zones 26h apart never share a         |
|                                | calendar date), that stored UTC instants render local, and the   |
|                                | `settings set timezone` path (DESIGN_user_timezone.md)           |
| `tests/test_journal.py`        | the run journal (DESIGN_logging.md §11): the writer (one line    |
|                                | per record, the 8 KB bound, a traceback elided in the middle, an |
|                                | unwritable directory that neither raises nor repeats itself),    |
|                                | the reader skipping a torn line, the run bracket (every start    |
|                                | has an end; a raising command records its traceback and          |
|                                | `failed`; a cancel is `cancelled`; three lines in `tm shell`     |
|                                | make four runs with one parent), retention and its once-a-day    |
|                                | stamp, `tm journal`'s three views, the `step`/`warn`/`fail`      |
|                                | verbs, the prompt answers (a declined confirm on the asking      |
|                                | run, EOF marked `defaulted`, a cancel naming the open question,  |
|                                | `ask_text` never journalled) — and two structural passes, one    |
|                                | failing on any broad `except` whose body is a bare `pass`, one   |
|                                | on any bare `input()` under `cli/` or `coach/`                   |

Tests get a fresh SQLite file per module (`tests.test_db_path`, below) and bind it
through `tests.helpers.rebind_test_db` / `bind_test_db`, which set `runtime.db` and the
remaining by-value sites in one call ([§6](#6-singletons)). `openrouter_client` is mocked
via `@patch`.

`tests/__init__.py` installs two suite-wide guards at import time, before any test
module is collected: the Calendar ride-along inside `garmin.ensure_data` is stubbed
out, and any non-loopback socket connect raises. Without them the suite reached the
real account — creating calendar events and consuming the incremental sync token
that `data pull` depends on. A test needing network behaviour mocks its client.

The same module hands out the per-module SQLite paths through `tests.test_db_path`, from
a temporary directory private to the process and removed at exit. The files used to sit
in `tests/`, swept there by a glob — so a second concurrent run deleted the databases the
first was still writing and both collapsed. `tests/test_isolation_guards.py` fails on any
module that builds a database path beside the tests again.

It installs a third seam for the same reason: every command a test runs opens a journal
run, so `logging.dir` is redirected to a scratch directory (swept at exit) and
`TRAINMATE_SOURCE=test` is set. Without it a suite run appends several hundred KB of
`test` runs to the operator's own journal.

Fixture dates ride on today (`_days_out(...)`/`GOAL_DATE`) rather than on fixed
dates wherever the code compares them against the clock: a plan window needs its
goal in the future, so a hardcoded date silently expires the test once it passes.

Integration / manual test scripts (not part of the test suite, and they do reach
the real Calendar):
- `scripts/run_integration.py`, `scripts/run_calendar.py`

---

## 15. Design Rationale & History

*Why* the current design looks the way it does, and what it replaced. The
reference sections above describe only the current state; this section explains
the non-obvious choices. The `DESIGN_*.md` files hold the full deep-dives.

### The mesocycle boundary is a firewall, not a range to widen
`workout adapt` adapts forward only to the end of the block containing the evaluation
date, so its runway shrinks to nothing as that block ends. Widening the range into the
next block would let a daily, lag-prone recovery signal rewrite periodization that
`plan`/`workout generate` own. Instead both sides are made aware of the boundary: the
prompt gains a terminal-window section, and the CLI points at
`workout generate -m ..<id>`, which already re-reads the same recent-metrics window. That
flag names the next block's own span, so the ending block's remaining days are left as
they stand (DESIGN_cli_selectors.md §8).

The CLI half of that hint has since been folded into the end-of-runway detector
(`cli/runway.py`, DESIGN_runway_nudge.md §3): a block boundary with no fresh sessions
after it is one of the four ways the schedule can run out, and it is now announced on
every daily surface rather than on `workout adapt` alone. Only the *prompt* side still
runs on `adapt_terminal_window_days` — that gate is about what the coach model is told
and must stay tight.

The firewall is enforced on **both** sides: the read bound (`get_workouts` capped at the
block end) and, on the write side, `workout_adapt` dropping any proposal dated past the
adaptation range end. A hallucinated post-boundary date therefore cannot be written, and
the apply range — derived from the surviving proposals — cannot stretch into the next
block. See DESIGN_block_boundary.md.

### Saying so rather than widening it
The firewall above is not about the *range*; it is about what would ride along with it.
Adapt's whole input is a backward window of recovery metrics, so a longer reach would give
this morning's HRV authority over a session four weeks out, where it has no predictive
claim. A constraint dated past the boundary is therefore built in by the next
`workout generate` whose span reaches it — which re-plans those days outright, against
the blocks that govern them, rather than carrying today's load judgement across to them.

What was missing was not reach but *notice*: nothing said the plan had yet to reflect a
directive already on record. `constraints.honored_at` is that signal, and one predicate
(`coach/honoring.py:needs_a_pass`) answers it for the `status` line, `constraint
list`/`show` and the message printed when the constraint is added — which names the
landing block and the `workout generate -m <id>` that would cover it. See
DESIGN_constraint_honoring.md; §5 there records why this is a signal rather than a third
command.

### The line that is only true while it scrolls past
Both front-ends run the same CLI, but they do not read it the same way: the bot buffers a
whole run and delivers it as one message, so `Auto-syncing Garmin …` and
`Querying OpenRouter …` land *after* the work is done, above the answer, on a 48-column
phone screen. The split is therefore not verbose-vs-terse but **is this line still true
when it arrives** — progress narration is information about the present, and history when
it isn't. So `util.aside` prints only where output is live (terminal, or
`TRAINMATE_VERBOSE=1`), while answers, warnings and errors always print. `garmin.pull`
shows why the line, not the content, is what moves: its step narration is an aside, but
for `data pull` the sync *is* the answer, so `pull` returns a one-line summary the handler
prints. The same reasoning already existed locally in `cli/progress.py`, which had put
`PMC_TSB_LAG_NOTE` behind `--explain` because "printing it on every invocation trained the
eye to skip it"; this generalises it. The prompts got the matching half — a shared
`## WRITING FOR THE ATHLETE` section plus per-field sentence caps on the rationale fields
— with `plan generate`'s `strategy` deliberately exempt, because it is re-injected into
every later prompt rather than read once. See DESIGN_output_verbosity.md.

One thing the aside tier could not absorb: `plan generate` echoed its whole
`PRIOR TRAINING REVIEW` prompt block, ~164 lines and 321 at Telegram's width. Too big to
skim past on *either* front-end, so it is off by default on both — `--show-llm-context`
asks for it — which no aside is. And because the wait is the other half of the problem,
`openrouter.complete` emits a `\x1eTM-FLUSH` marker just before the POST, so whatever a
command printed on its way in is delivered before it goes silent rather than after
(DESIGN_output_verbosity.md §7).

### The journal reuses the lines it already prints
The app used to record one thing well — the full text of every LLM call — and that pile of
documents could not answer "what happened at 6:15 this morning", "when did the Garmin sync
start failing", or "what has this cost me". What was missing was not detail but a spine:
nothing said *this run happened, this is what it did, this is how it ended*.

The trap in fixing that is adding a logging call at every interesting place, so the calls
drift out of step with the code. They are not added here. The verbs above already sit at
the moments worth recording, already worded for a human, so they were given a second job:
the trace half of the aside tier became `step`, which prints exactly where it printed and
also journals; the operational warnings became `warn`/`fail`. One `try`/`except` in
`run_once` then buys every failed command its own traceback — which used to be discarded
unless the athlete passed `--debug`, which they never did, because the run that mattered
had already finished.

The same reasoning put one more record in one more place. The verbs cover what the app
says; they do not cover what it hears. A declined `workout generate` returns from its
handler normally, so its run ends `ok` — indistinguishable in the listing from the run that
applied, same duration, same tokens, and two of those answers quietly re-stamp a config
hash on the way past. The answer is therefore recorded in the prompt broker, which is the
one place all 29 questions pass through, rather than beside each of them.

Two consequences are worth knowing before editing any of it. The journal's day file is the
one date in this app that is **not** the athlete's, because resolving their zone reads a
setting, which builds the database, which migrates it — on `tm help`, and inside the code
path whose job is to survive the database being unreachable. Display converts back through
their zone like every other stored instant. And the tier the output design had no name for
is the one that matters most: things the app must not *say* but must not *forget* —
`journal.debug`, which is what the ten `except Exception: pass` handlers became. See
DESIGN_logging.md.

### The adapt TASK: standing rules, not restated ones
The adapt prompt's TASK is one always-on body plus five conditional sections, each written
at its own time against its own design doc. Each had independently re-derived the same house
rules — "prefer rescheduling over deleting" and "do not reshape the mesocycle" were each
restated in several sections, one of which said the latter twice within itself. The cost
that matters is not the tokens but the divergence: each copy was phrased against its own
local concern, so the same rule slowly stopped meaning the same thing, and nothing caught it.

The rules are now stated once as `STANDING RULES`, placed *after* the decision branches (the
branches are the task; the rules bound how it is expressed). Sections cite them in a clause.
A rule never displaces a *mechanic*, though — `PROTECTING A BENCHMARK` still spells out how
to encode a move, and the drift section still carries its escalation to `workout generate` in
full, because that escalation must stay gated rather than float up into an always-on rule.
See DESIGN_adapt_task_prompt.md.

### Prompt structure: one hierarchy, three markers
Every prompt was assembled from parts written at different times, each announcing itself
its own way: the system prompt in `ALL CAPS:`, the user content mostly in `Title Case:`,
and the science documents in whatever markdown their author used. `TASK:` and
`BENCHMARK PLACEMENT:` were the same shape, so nothing said the second was *part of* the
first — which it is, structurally, since both live in one `custom_task` string.

Now: `## NAME` for a top-level section, `### NAME` for a sub-section of `## TASK`, and a
`====` banner only around a document quoted verbatim. Names stay ALL CAPS because the task
prose cites sections *by name* ("the section titled `BLOCK PROGRESS SO FAR`"); the marker
carries the level, the caps carry the identity. Both messages use the same scheme.

The science documents keep their banner rather than folding into the markdown scheme: they
*are* markdown, with their own `#`/`##`/`###` at arbitrary depth, so the banner is what says
"quoted — these headings are its own", and it is precisely what lets the frame use `##`
safely everywhere else. Two banners, one per source (app / athlete-provided), each stating
its provenance. See DESIGN_prompt_structure.md.

### Workouts are a log, so the log is what is stored
The table was already mostly history — three hundred-odd rows for a two-month span, of
which sixty were live — but history was second class. Three write paths disagreed about
what happened to the old version (archive and rebuild, edit in place, hard `DELETE`), the
third losing sessions unrecoverably on a normal day's use. Seven columns faked a history a
chain gives for free, a heuristic file reconstructed a fact nobody recorded, and undo was
keyed on a timestamp stamped on the rows that *died* — so an adapt, which killed nothing,
could not be undone on its own.

`workouts` is now append-only: a row is a revision, never updated and never deleted, and
the newest revision in a slot is the live one. Two SQL triggers enforce that where it
cannot be skipped, exempting exactly one transition — seeding a first revision's lineage
with its own id, before commit — and pinning the value it may write. See
DESIGN_workout_revisions.md; the shape is in [§5](#workouts).

- **The lineage is not optional.** `(date, sport_canonical)` answers "what happened to
  Tuesday's ride"; `lineage_id` answers "what happened to *this* ride". The second is what
  lets `adaptation_count` / `adapted_at` be deleted as columns and counted from the chain
  instead — and it is load-bearing rather than tidy. Count over the slot alone and a swap
  resets the tally, so the DO NOT COMPOUND guard goes quiet on the morning the session
  moved: exactly when life got in the way, recovery is worst, and the guard matters most.
- **A change is a row.** Each command invocation writes one `workout_changes` row and
  points every revision it appends at it, so the batch key moved from death to birth.
  Every write is therefore a batch and every batch is undoable by one point-in-time
  primitive — including an adapt, on its own, without reverting the generation beneath it.
- **Modification kind is recorded, not inferred.** It is the change kind of the live
  revision. The heuristic that sniffed magic string prefixes, `date != original_date` and
  `source == 'manual'` — with a documented catch-all for rows predating a split — is
  deleted, along with the precedence rule it needed. The marker and the tally are two
  facts now, so a session eased twice and then swapped simply reads both.
- **Recency is derived too, now.** `adapted_at` / `adaptation_count` were stored because
  no other field carried *when* or *how often* a session was eased. The chain carries it:
  walk the lineage backwards, jump over any span a rollback undid, stop at the first
  `generate`, and count the adapts that actually cut load. One integer column, or two
  hand-maintained fields forever.
- **Calendar state moved off the row** and into `workout_calendar_state`, keyed by
  lineage. Left there, immutability would break the moment a push succeeded: `workout push
  -f` alone would double the table with rows that changed nothing an athlete would call a
  change. The event lifecycle used to piggyback on archival; it is now one reconcile pass
  per change, scheduled by the change handle rather than remembered by each command.
- **No-op revisions are suppressed.** `workout generate` rebuilds a 28-day span every
  run and most days come back unchanged. Without the rule, "what happened to Tuesday"
  answers with six identical rows and one real change. Storage was never the concern —
  legibility was.
- **Calendar freshness still falls out** rather than being arranged: the live row after
  any change is a *different row*, so its hash differs from the stored signature and it
  reads `stale` without anyone writing a flag. The signature deliberately excludes `rpe`
  (never reaches Calendar). Backward adherence marking keeps its own
  `adherence_pushed_signature` rather than reusing `pushed_signature`, because folding the
  verdict into that hash would make every marked past row read `stale`.

### Coach learnings: confidence dropped `suppress_reinforcement`
Confidence is now a pure function of the per-learning evidence basis. Because
re-citing a counted `(week, polarity)` is a `UNIQUE`-constrained `INSERT OR IGNORE`
no-op, re-running / `--force` / overlapping windows cannot inflate confidence — so
the old `suppress_reinforcement` flag became unnecessary and was removed. Learnings
predating this model are grandfathered with a synthetic basis sized to sustain their
stored level (`db._grandfather_learning_evidence()`, source `migration`) so the first
recompute doesn't silently demote them. Full model:
[§3](#3-coach-package-architecture); DESIGN_evidence_based_confidence.md.

### Load model: single fallback, no additive blend
`Workload = activity_load(act)` picks one best-available method (power TSS → hrTSS →
sRPE); the former `TSS + RPE × hours` additive blend was removed because it
double-counted internal and external load. RPE is user-entered only and never
synthesised from power/HR. See [§12](#12-sports-science--coaching-mathematics).

### Data pull: direct from Garmin
Data is pulled directly from Garmin Connect; the former Google Sheets ingestion
path is gone. See DESIGN_garmin_direct_pull.md.

### Progress timeline: one computation, three renderers
Past load (measured) and future load (planned) previously lived in disconnected
views — `workout compare` (per-day adherence, no accumulation) and the workout/
mesocycle listings (periodization visible in the data but never drawn). `tm
progress` / the bot photo / the web **Progress** tab draw them as one continuous
timeline instead, with the stored CTL/ATL/TSB series folded forward across the seam
so the projection visibly moves the instant `adapt`/`generate`/`swap`/`remove`
rewrite future `workouts`. `trainmate/progression.py` is the single row-in/row-out
computation (`assemble_timeline`); `trainmate/timeline.py` is the one db-reads path
both front-ends call, so CLI and endpoint render one identical payload (the fix for
the rev-4 divergence where each caller assembled its own; the shared builder *is* the
pin, which is why rev 9 deleted the CLI≡endpoint equivalence test as ceremony —
it had decayed to `assertEqual(f(db), f(db))`). `chart.py` is the single PNG renderer
shared by the bot photo and `/api/timeline.png`. A week shows planned totals iff it
contains non-removed `workouts` rows — the same rows the total is summed from, so the
flag and the figure can never disagree, and it stays independent of the cosmetic meso
label (a labeling nit can't silently delete planned data). The v1 web tab frames the
server-rendered
PNG; the interactive uPlot tab and its JSON endpoint are a follow-on (§8.5). The
endpoint is deliberately uncached (a fingerprint scheme would just re-derive "did
anything change" at higher complexity than recomputing a few hundred rows). Full
design: DESIGN_progress_timeline.md; PMC model: [§12](#fitnessfatigueform-pmc-model).

### Goal dates: event vs. training horizon
Not every goal happens on its date — some dates only say how far the athlete wants to
train. Description prose could not express that: a goal stating, in capitals, that its
date was indicative and not a race still got a freshening phase pinned to the date,
because the event framing is structural — the planning task ("the last mesocycle must
end on or around the goal date"), its response format ("Peak & Taper, Race/Event"),
and the user message ("'{title}' on {date}") all restate it, and one `Details:` field
loses that argument every time. `objectives.date_type` (`event` default | `horizon`)
makes the meaning a field that both the prompts and deterministic code branch on: a
horizon plan still ends around the date (a plan needs an end), but its last block is
an ordinary training block with no peak/taper pinned to the date, and the goal-week
no-benchmark carve-out does not apply — there is no event for a maximal test to
compete with. `_clean_goals` includes the field only when it is `horizon`: an event
goal — every goal predating the field — hashes exactly as before, so shipping the
field did not flag existing plans stale, while flipping a goal either way adds or
removes the key, changes `goals_hash`, and prompts the replan that change warrants.
Current-state reference: [§5 objectives](#objectives).

### A session already behind us carries its verdict
`workout list` used to answer only "what was planned", so the athlete asking what they
did last week got the plan read back at them and had to run `workout compare` to find out
which of it actually happened — two commands for one question, and the listing quietly
implying that a session it drew was a session that took place. The verdict now rides on
the listing itself, today included: an untrained session on an unfinished day reads
`[NOT YET]` rather than `[MISSED]`, which is the `pending_from` rule
([§10](#10-key-data-flows)) surfacing where it is finally visible.

Nothing new grades anything. `adherence_results` is the single DB-backed pairing —
`analyze_adherence` over a window — and `adherence_verdicts` keys `classify_adherence`'s
answer by workout id; the Calendar marker, the terminal marker and the web badge are three
renderings of that one map. The wording is shared too (`adherence.STATUS_LABELS`): the
Calendar tag map was the only place a verdict had ever been given a word, so a second
surface would have invented a second wording, which is exactly how `[Partial]` on a
calendar event and something else in a listing would have come to describe one session.

Two things this forced. The listing grades its **whole** window rather than the rows it is
showing, because matching is per-day and first-come — hand it only the rows that survived
a `--type` filter and the day's activity goes to whoever is left, so a rest day beside a
ride would read `[REST BROKEN]` the moment the ride was filtered out. And the listing
became a command that reads completed activities, so it freshens Garmin like every other
one that does; the cost is bounded by scoping the pull to the past part of the window,
which leaves the ordinary forward-looking `workout list` entirely offline.

### The adapt prompt reads the verdict, not just the pairing
`workout adapt` is the fourth reader of that map, and for a while it was the one that
ignored it. It asked only whether a planned session had matched *anything*, and tagged
every match `[COMPLETED — locked history, not adaptable]` — collapsing `done` and
`partial` into one word. That is wrong in a specific and costly way, because
`indoor_cardio` is an alias of `strength_training`: a ten-minute warm-up logged before a
lift the athlete then abandoned pairs with the 65-minute session it was supposed to open.
The prompt told the coach a session that never happened was in the bank, and the guard
that backs the tag dropped any proposal touching it — so the coach could not salvage the
rest of the day even after reading, three sections lower, its own discrepancy line saying
10 minutes were performed against 65 planned.

`adherence.performed_sessions` now hands the prompt `classify_adherence`'s verdict plus the
duration and load actually recorded, and the tag states them: a partial says it is partial
and reports what was performed. The numbers, not an adjective, carry the meaning — a
partial is as often an overshoot as a shortfall, so the tag stays neutral and lets the
planned figures already on the line above do the comparing.

The lock narrows with it. History is still history: any match on a day already behind the
evaluation date stays locked, partial included, because rewriting a past calendar event is
meaningless whatever the verdict. On the **evaluation date** a session becomes history only
once its planned *time* was actually spent. Duration is the test, not load — strength load
read off heart rate is unreliable enough that this codebase already falls back to sRPE for
it, but minutes in the gym are not in doubt. So a session abandoned after its warm-up stays
adaptable for the rest of the day, which lands it in exactly the state an untrained session
on an unfinished day was already in (`pending`, above): on the evaluation date, a session is
locked only once the planned time has been spent. What the athlete already did travels with
it, so the coach salvages the remainder rather than re-prescribing the whole session.

### A pairing the matcher had to guess at is a question, not a fact
Everything above assumes the pairing is right. Sometimes it is a guess, and the 27th was
one: `SPORT_MAPPING` lists `indoor_cardio` among `strength_training`'s aliases, so a
ten-minute warm-up logged before an abandoned lift was the only candidate on the day and
first-come load-sorted matching handed it the 65-minute session. Grading that pairing more
honestly does not help, because the pairing itself is what is wrong — the athlete did none
of the session, and calling it "partial, 10m performed" is a better-worded version of the
same false claim.

Only the athlete can settle it, so `adherence.is_ambiguous_match` marks the pairings worth
asking about and `pending_match_questions` raises them **before** the LLM call, not after:
a wrong pairing does not merely mislabel a row in a listing, it tells the coach a session
was performed, and by the time a proposal comes back that premise is already baked into it.

The test is duration alone. By the time a pairing exists the sport check has already done
its work — a ride never reaches a strength session, whatever its length — so the type is
not what is in doubt. What is left undecided is that an activity of the right sport ran far
shorter than planned, which is either the session cut short or something else entirely (a
warm-up, a fragment). Nothing in the data separates those two readings, and the athlete's
answer is different in each case: count it as partially performed, or discard it and let
the session read as not done.

That applies to an exact sport match as readily as an aliased one. A 10-minute
`strength_training` activity against a 65-minute lift is exactly as unclear as a 10-minute
`indoor_cardio` one; an earlier version of this excluded exact matches on the theory that
they must be a cut session, which is only one of the two things it can be. An activity that
ran roughly its planned length is never questioned, so an ordinary day costs nothing.
Measured over the athlete's full history: 19 pairings made, one question raised, on the day
that prompted this.

The answer persists in `activity_match_decisions`, keyed by `(activity_id, sport)` rather
than by workout id — workout rows are replaced on every revision, so a workout id would go
stale the next time the day is adapted and the athlete would be asked again. Every
adherence surface passes `db.get_rejected_matches()` into `analyze_adherence`, so the
terminal listing, the web badge and the coach cannot disagree about what counts as done. A
rejected activity is passed over for that session and stays available to the rest of the
day's pairing, which is what makes it fall through to the unplanned/informational path
rather than vanishing.

The question is a refinement, never a gate: `--auto` and the unattended runs skip it and
fall back to the matcher's own answer, which the duration rule above already makes sane.


### The sport check only does its work if it matches exactly
The section above rests on "a ride never reaches a strength session, whatever its length."
It did. `SPORT_MAPPING` lists `fitness` among `strength_training`'s aliases, and matching
fell back to a substring test — `t in act_type` — so Garmin's `e_bike_fitness` contained an
alias and a 34-minute e-bike ride was handed a 10-minute evening circuit.

The damage was not the mislabelled row. The pairing graded `partial`, and because the ride
ran *longer* than the circuit there was no duration shortfall, so `is_ambiguous_match` had
nothing to question and `performed_sessions` marked the session `locked` — history, not to
be rewritten. When the athlete said that evening that the circuit was off, the adapt could
only add a copy to the next day; it was not permitted to withdraw the original. A loose
string test became a plan the athlete never chose.

The substring fallback had no legitimate work to do: across both instances every real
pairing was already an exact alias hit, and the only matches it added were wrong ones. It
also quietly folded every resort ski day into `ski_touring` — each `downhill_skiing` alias
ends in `skiing`, which `ski_touring` also lists — collapsing the one distinction
`SPORT_MAPPING` draws most deliberately. Matching now canonicalizes both sides and compares
them exactly, which is the rule the zone vocabulary already settled on for the same reason
(DESIGN_intensity_distribution.md §6.1 "Exact over substring"): an unrecognised type stays
unrecognised, visible as its own row, and is repaired by adding an alias — not by a net
that guesses. `e_bike_fitness` is deliberately left unmapped; a motor-assisted ride is not
a cycling session, and its load still reaches the PMC through `completed_activities`.
